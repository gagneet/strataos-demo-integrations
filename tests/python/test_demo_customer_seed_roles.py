# @featuretrace:owner-transfers — Regression tests for demo_customer.py's role
# normalization helpers, found during the 'chairman' role-literal sweep, and for the
# buildings/memberships write-shape bug found during the 2026-07-02 financial
# browser-verification audit.
# Layer: test
# Related: backend/seeds/demo_customer.py
#          docs/architecture/financial_browser_verification.md
from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SEED_PATH = Path(__file__).resolve().parents[2] / "backend" / "seeds" / "demo_customer.py"


@pytest.fixture(scope="module")
def seed_module():
    spec = importlib.util.spec_from_file_location("demo_customer_seed", _SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestNormalizePgUserRole:
    def test_chairman_normalizes_to_ec_member(self, seed_module):
        assert seed_module._normalize_pg_user_role("chairman") == "ec_member"

    def test_other_roles_pass_through_unchanged(self, seed_module):
        assert seed_module._normalize_pg_user_role("strata_manager") == "strata_manager"
        assert seed_module._normalize_pg_user_role("ec_member") == "ec_member"


class TestEcPositionForRawRole:
    def test_chairman_maps_to_chairman_position(self, seed_module):
        assert seed_module._ec_position_for_raw_role("chairman") == "CHAIRMAN"

    def test_treasurer_and_secretary_dict_keys_are_currently_unreachable(self, seed_module):
        """Pre-existing (not introduced by this fix) narrower gap: _normalize_pg_user_role
        only maps 'chairman' -> 'ec_member', so a raw role of 'treasurer'/'secretary' never
        normalizes to 'ec_member' and _ec_position_for_raw_role returns None for both —
        the TREASURER/SECRETARY dict entries only fire if some future PORTAL_USERS row's
        raw role literally normalizes to 'ec_member' while equalling 'treasurer'/'secretary',
        which cannot currently happen. Documented here rather than silently assumed."""
        assert seed_module._ec_position_for_raw_role("treasurer") is None
        assert seed_module._ec_position_for_raw_role("secretary") is None

    def test_bare_ec_member_defaults_to_member_position(self, seed_module):
        assert seed_module._ec_position_for_raw_role("ec_member") == "MEMBER"

    def test_non_ec_role_returns_none(self, seed_module):
        """A non-EC role like strata_manager must not get an ec_position at all —
        only normalize_pg_user_role()=='ec_member' rows are EC-position-shaped."""
        assert seed_module._ec_position_for_raw_role("strata_manager") is None
        assert seed_module._ec_position_for_raw_role("super_admin") is None


class TestPortalUsersMongoWriteRegression:
    """Regression: PORTAL_USERS' chairman entry used to be written to MongoDB's
    db.users with the literal role='chairman' (never normalized on the Mongo path,
    only on the Postgres path) — MongoDB is the live operational store, so this demo
    chairman account was effectively broken for every _effective_role()-gated feature
    until the write path applied the same normalization as Postgres."""

    def test_chairman_portal_user_role_normalizes_for_mongo_write(self, seed_module):
        chair_entry = next(pu for pu in seed_module.PORTAL_USERS if pu["role"] == "chairman")
        mongo_role = seed_module._normalize_pg_user_role(chair_entry["role"])
        ec_position = seed_module._ec_position_for_raw_role(chair_entry["role"])

        assert mongo_role == "ec_member"
        assert ec_position == "CHAIRMAN"

    def test_no_portal_user_has_an_unnormalizable_role(self, seed_module):
        """Every PORTAL_USERS role must normalize to a real top-level UserRole —
        i.e. _normalize_pg_user_role must never be a no-op identity on 'chairman'."""
        for pu in seed_module.PORTAL_USERS:
            normalized = seed_module._normalize_pg_user_role(pu["role"])
            assert normalized != "chairman"


def _mock_pg_session():
    """A minimal async-context-manager mock standing in for async_session_context().
    session.execute() returns a MagicMock whose .scalar_one() gives a fake UUID string
    (only layer_5's Postgres portal-user INSERT ... RETURNING user_id needs this)."""
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one = MagicMock(return_value="00000000-0000-0000-0000-000000000000")
    session.execute = AsyncMock(return_value=exec_result)

    @asynccontextmanager
    async def _ctx():
        yield session

    return _ctx, session


def _mock_mongo_db():
    db = MagicMock()
    for name in ("buildings", "units", "memberships", "users", "feature_toggles"):
        collection = MagicMock()
        collection.update_one = AsyncMock()
        setattr(db, name, collection)
    return db


class TestBuildingsDocumentWriteShape:
    """Regression: layer_1_structural() previously wrote the Acme demo tenant's
    db.buildings document with a `building_id` field but no `id` field, and no
    `is_active` field. Every db.buildings consumer in this codebase (utils/auth.py's
    get_current_user()/get_current_building() legacy-Mongo-session path, server.py,
    cron/*, services/*) queries by `id` + `is_active` — so the malformed doc could
    never be found, and every non-PG-token (i.e. every owner/EC-member) login to this
    tenant failed with 403 'Building not found or inactive' on every request. Found
    live during the 2026-07-02 financial browser-verification audit."""

    @pytest.mark.asyncio
    async def test_buildings_doc_has_id_and_is_active(self, seed_module, monkeypatch):
        pg_ctx, _session = _mock_pg_session()
        mongo_db = _mock_mongo_db()
        monkeypatch.setattr(seed_module, "async_session_context", pg_ctx)
        monkeypatch.setattr(seed_module, "_mongo_db", lambda: mongo_db)

        await seed_module.layer_1_structural()

        mongo_db.buildings.update_one.assert_awaited_once()
        filter_arg, update_arg = mongo_db.buildings.update_one.call_args[0]
        doc = update_arg["$set"]
        assert doc["id"] == seed_module.ACME_BUILDING_ID
        assert doc["is_active"] is True
        # building_id is kept too — some legacy code (and this doc's own upsert filter)
        # still reads it; the fix adds `id`, it does not remove `building_id`.
        assert doc["building_id"] == seed_module.ACME_BUILDING_ID
        assert filter_arg == {"building_id": seed_module.ACME_BUILDING_ID}


class TestMembershipsWriteRegression:
    """Regression: layer_5_toggles_and_users() previously never wrote db.memberships
    at all for any of its 17 users (3 portal users + 14 owners). utils/auth.py's
    get_current_user() legacy-Mongo-session path requires a matching, active
    membership document to authorize any non-super_admin request — without it, every
    owner account in this demo tenant has always been unable to authenticate, on every
    request, since this seed was first written. Found live during the 2026-07-02
    financial browser-verification audit (confirmed via a real owner login before/after
    the fix, not just by reading the seed code)."""

    @pytest.mark.asyncio
    async def test_every_portal_user_and_owner_gets_a_membership(self, seed_module, monkeypatch):
        pg_ctx, _session = _mock_pg_session()
        mongo_db = _mock_mongo_db()
        monkeypatch.setattr(seed_module, "async_session_context", pg_ctx)
        monkeypatch.setattr(seed_module, "_mongo_db", lambda: mongo_db)

        await seed_module.layer_5_toggles_and_users()

        expected_count = len(seed_module.PORTAL_USERS) + len(seed_module.OWNERS)
        assert mongo_db.memberships.update_one.await_count == expected_count

        for call in mongo_db.memberships.update_one.call_args_list:
            filter_arg, update_arg = call[0]
            doc = update_arg["$set"]
            assert filter_arg["building_id"] == seed_module.ACME_BUILDING_ID
            assert doc["building_id"] == seed_module.ACME_BUILDING_ID
            assert doc["is_active"] is True
            assert isinstance(doc["roles"], list) and len(doc["roles"]) > 0
            assert "chairman" not in doc["roles"]  # same invariant as the role sweep above

    @pytest.mark.asyncio
    async def test_owner_membership_carries_their_unit_in_units_list(self, seed_module, monkeypatch):
        pg_ctx, _session = _mock_pg_session()
        mongo_db = _mock_mongo_db()
        monkeypatch.setattr(seed_module, "async_session_context", pg_ctx)
        monkeypatch.setattr(seed_module, "_mongo_db", lambda: mongo_db)

        await seed_module.layer_5_toggles_and_users()

        owner_calls = [
            call for call in mongo_db.memberships.update_one.call_args_list
            if call[0][1]["$set"]["roles"] == ["owner"]
        ]
        assert len(owner_calls) == len(seed_module.OWNERS)
        for call in owner_calls:
            doc = call[0][1]["$set"]
            assert len(doc["units"]) == 1
            assert doc["units"][0] in {o["unit"] for o in seed_module.OWNERS}
