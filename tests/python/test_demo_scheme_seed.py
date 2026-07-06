"""Demo scheme seed — invariants and idempotency.

These tests run against the live Postgres database (gated on
``RUN_INTEGRATION_TESTS=1``) and verify the full demo Strata Management
chain seeded by ``seeds/demo_scheme.py``:

1. Seed runs cleanly to completion.
2. Exactly one demo tenant exists (``core.tenants.is_demo = TRUE``).
3. Exactly one demo scheme exists (``core.schemes.is_demo = TRUE``).
4. Demo scheme has the expected scheme_number, name, jurisdiction.
5. Demo scheme has 10 lots with varied entitlements (sum > 1000).
6. The two demo users (Strata Manager + Strata Admin) exist and are
   role-assigned to the demo scheme.
7. Both platform super admins have scheme-scoped role assignments for
   the demo.
8. Re-running the seed is a no-op (no duplicates).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

# tests/backend/ → up 2 = project root → into backend/
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_RUN = os.getenv("RUN_INTEGRATION_TESTS") == "1"
pytestmark = pytest.mark.skipif(not _RUN, reason="integration test; set RUN_INTEGRATION_TESTS=1 to enable")


@pytest_asyncio.fixture
async def db_session():
    """Yield a session with the demo tenant context set.

    Most ``core.*`` tables enforce strict tenant_isolation RLS without a
    bypass clause (only ``core.users`` and ``core.user_invitations`` honour
    the bypass sentinel). To verify the demo data, the fixture sets
    ``app.tenant_id`` to the demo tenant — that's how production code
    reads demo rows after a super admin switches into the demo via the
    building switcher.
    """
    from db_postgres.session import async_session_context
    from strataos_demo_integrations.demo_bank.seeds.demo_scheme import DEMO_TENANT_ID

    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, false)"),
            {"tid": DEMO_TENANT_ID},
        )
        yield session


@pytest_asyncio.fixture
async def db_session_bypass():
    """Yield a session with the RLS bypass sentinel — only safe for queries
    against tables whose policy honours the sentinel (``core.users``)."""
    from db_postgres.session import async_session_context
    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000000', false)")
        )
        yield session


@pytest.mark.asyncio
async def test_seed_runs_cleanly(db_session):
    from seeds import demo_scheme
    await demo_scheme.seed()


@pytest.mark.asyncio
async def test_exactly_one_demo_tenant(db_session):
    from seeds import demo_scheme
    await demo_scheme.seed()
    n = (await db_session.execute(
        text("SELECT count(*) FROM core.tenants WHERE is_demo = TRUE")
    )).scalar()
    assert n == 1


@pytest.mark.asyncio
async def test_exactly_one_demo_scheme(db_session):
    from seeds import demo_scheme
    await demo_scheme.seed()
    n = (await db_session.execute(
        text("SELECT count(*) FROM core.schemes WHERE is_demo = TRUE")
    )).scalar()
    assert n == 1


@pytest.mark.asyncio
async def test_demo_scheme_has_expected_constants(db_session):
    from seeds import demo_scheme
    await demo_scheme.seed()
    row = (await db_session.execute(
        text("""
             SELECT scheme_number, scheme_name, jurisdiction::text, is_demo
             FROM core.schemes
             WHERE is_demo = TRUE
             """)
    )).fetchone()
    assert row is not None
    assert row[0] == demo_scheme.DEMO_SCHEME_NUMBER
    assert row[1] == demo_scheme.DEMO_SCHEME_NAME
    assert row[2] == demo_scheme.DEMO_JURISDICTION
    assert row[3] is True


@pytest.mark.asyncio
async def test_demo_lots_have_varied_entitlements(db_session):
    from seeds import demo_scheme
    await demo_scheme.seed()

    rows = (await db_session.execute(
        text("""
             SELECT count(*),
                    sum(entitlement_units),
                    count(DISTINCT entitlement_units),
                    sum(floor_area_sqm)
             FROM core.lots l
                      JOIN core.schemes s ON s.scheme_id = l.scheme_id
             WHERE s.is_demo = TRUE
             """)
    )).fetchone()
    lot_count, total_ent, distinct_ents, total_sqm = rows
    assert lot_count == len(demo_scheme.DEMO_LOTS)
    assert total_ent > 1000, f"expected total entitlement > 1000, got {total_ent}"
    assert distinct_ents >= 5, (
        f"expected variety in entitlement units (≥5 distinct values); "
        f"got {distinct_ents}. The seed should not produce uniform lots."
    )
    assert total_sqm > 800, f"expected total floor area > 800 sqm, got {total_sqm}"


@pytest.mark.asyncio
async def test_demo_users_exist_with_correct_roles(db_session_bypass):
    """Reads ``core.users`` cross-tenant — uses the bypass-sentinel session
    since the demo users live in the demo tenant, not the platform tenant."""
    from seeds import demo_scheme
    await demo_scheme.seed()

    rows = (await db_session_bypass.execute(
        text("""
             SELECT email::text, role::text, is_active, is_approved
             FROM core.users
             WHERE email IN ('admin@demo.strataos.live', 'manager@demo.strataos.live')
             ORDER BY email
             """)
    )).fetchall()
    assert len(rows) == 2

    by_email = {r[0]: r for r in rows}
    assert by_email["admin@demo.strataos.live"][1] == "strata_admin"
    assert by_email["manager@demo.strataos.live"][1] == "strata_manager"
    for r in rows:
        assert r[2] is True, f"{r[0]} not active"
        assert r[3] is True, f"{r[0]} not approved"


@pytest.mark.asyncio
async def test_demo_users_can_login_with_seeded_password(db_session):
    """The committed bcrypt hash must verify against ``DemoUser$01``."""
    import bcrypt
    from seeds import demo_scheme
    assert bcrypt.checkpw(b"DemoUser$01", demo_scheme.DEMO_USER_PASSWORD_HASH.encode())


@pytest.mark.asyncio
async def test_demo_users_have_scheme_role_assignments(db_session):
    from seeds import demo_scheme
    await demo_scheme.seed()
    n = (await db_session.execute(
        text("""
             SELECT count(*)
             FROM core.user_role_assignments ura
                      JOIN core.users u ON u.user_id = ura.user_id
                      JOIN core.schemes s ON s.scheme_id = ura.scheme_id
             WHERE u.email IN ('admin@demo.strataos.live', 'manager@demo.strataos.live')
               AND s.is_demo = TRUE
               AND ura.is_active = TRUE
             """)
    )).scalar()
    assert n == 2


@pytest.mark.asyncio
async def test_super_admins_have_no_scheme_scoped_assignments(db_session_bypass):
    """Super admins live ABOVE the org tree — they must hold only their global
    (``scheme_id IS NULL``) role assignment from ``seeds/super_admins.py``,
    never a scheme-scoped one.

    Granting an SA a scheme-scoped assignment causes the auth login flow to
    auto-resolve them into that single scheme (treating them as if they
    "belong to" that building), which violates the SaaS-admin model: SAs
    should pick a scheme via the building switcher, not be defaulted into
    one. This test guards against re-introducing that bug in any seed.
    """
    from db_postgres.session import async_session_context
    from seeds import demo_scheme
    await demo_scheme.seed()

    sa_rows = (await db_session_bypass.execute(
        text("""
             SELECT user_id::TEXT, tenant_id::TEXT, email::text
             FROM core.users
             WHERE role = 'super_admin'::core.user_role AND is_active = TRUE
             """)
    )).fetchall()

    offenders = []
    for user_id, sa_tenant_id, sa_email in sa_rows:
        async with async_session_context() as s:
            await s.execute(
                text("SELECT set_config('app.tenant_id', :t, false)"),
                {"t": sa_tenant_id},
            )
            scheme_scoped = (await s.execute(
                text("""
                     SELECT count(*)
                     FROM core.user_role_assignments
                     WHERE user_id::TEXT = :uid
                       AND scheme_id IS NOT NULL
                       AND is_active = TRUE
                     """),
                {"uid": user_id},
            )).scalar()
            if scheme_scoped > 0:
                offenders.append((sa_email, scheme_scoped))

    assert not offenders, (
        f"Super admins must not have scheme-scoped role assignments. "
        f"Offenders: {offenders}. SAs are above the org tree; cross-tenant "
        f"scheme access is granted via core.schemes RLS bypass, not a "
        f"per-scheme user_role_assignments row."
    )


@pytest.mark.asyncio
async def test_seed_does_not_create_extra_super_admins(db_session_bypass):
    """Invariant: only the two platform super admins ever exist in core.users.

    The demo seed must never create a new ``super_admin`` user — the demo
    Strata Management Company seeds a Building Admin + Strata Manager only.
    Any seed change that accidentally promotes a demo user to ``super_admin``
    fails this test loudly, before the role escapes into production.
    """
    from seeds import demo_scheme
    await demo_scheme.seed()

    rows = (await db_session_bypass.execute(
        text("""
             SELECT email::text
             FROM core.users
             WHERE role = 'super_admin'::core.user_role
               AND is_active = TRUE
             ORDER BY email
             """)
    )).fetchall()
    emails = [r[0] for r in rows]

    expected = {
        "administrator@strataos.live",
        "gagneet@silverfoxtechnologies.com.au",
    }
    assert set(emails) == expected, (
        f"Unexpected super_admin set. Expected exactly {expected}, "
        f"got {set(emails)}. The demo seed must never create or promote a "
        f"super_admin — only Building Admin / Strata Manager roles."
    )


@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session):
    """Running the seed twice must produce the same row counts."""
    from seeds import demo_scheme
    await demo_scheme.seed()

    async def counts():
        t = (await db_session.execute(text("SELECT count(*) FROM core.tenants WHERE is_demo = TRUE"))).scalar()
        s = (await db_session.execute(text("SELECT count(*) FROM core.schemes WHERE is_demo = TRUE"))).scalar()
        l = (await db_session.execute(text(
            "SELECT count(*) FROM core.lots l JOIN core.schemes s ON s.scheme_id = l.scheme_id WHERE s.is_demo = TRUE"
        ))).scalar()
        u = (await db_session.execute(text(
            "SELECT count(*) FROM core.users WHERE email IN ('admin@demo.strataos.live','manager@demo.strataos.live')"
        ))).scalar()
        a = (await db_session.execute(text("""
                                           SELECT count(*)
                                           FROM core.user_role_assignments ura
                                                    JOIN core.schemes s ON s.scheme_id = ura.scheme_id
                                           WHERE s.is_demo = TRUE
                                             AND ura.is_active = TRUE
                                           """))).scalar()
        return t, s, l, u, a

    before = await counts()
    await demo_scheme.seed()
    after = await counts()

    assert before == after, f"Re-running the seed changed counts: before={before} after={after}"
