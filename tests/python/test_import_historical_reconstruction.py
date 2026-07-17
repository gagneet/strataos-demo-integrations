"""
tests/python/test_import_historical_reconstruction.py

# @featuretrace:demo_bank — Unit tests for historical-reconstruction ingestion.
# Layer: test
# Data flow: test → ingestion.import_historical_reconstruction() → mock DB → assertions
# Related: backend/integrations/demo_bank/ingestion.py
#          backend/integrations/demo_bank/reconstruction_batch_schemas.py
# Toggle: historical_financial_reconstruction
# Tests: this file

Coverage targets:
1. Manifest-only generation — refuses a batch whose status hasn't passed approval.
2. Refuses a manifest/batch mismatch (wrong batch_id or building_id).
3. Provenance fields land correctly on every materialised transaction.
4. levy_component split (ordinary vs special_levy) is preserved verbatim.
5. Idempotent replay — materialising the same manifest twice creates no duplicates.
6. The existing Strata Web guard is untouched by this addition (regression smoke test).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from strataos_demo_integrations.demo_bank.reconstruction_batch_schemas import (
    ReconstructedTransactionRow,
    ReconstructionBatch,
    ReconstructionManifest,
)

_BUILDING_A = "16244"  # Sierra demo — never "13195" in tests


def _make_db(upsert_new: bool = True):
    db = MagicMock()

    tx_coll = MagicMock()
    upsert_result = MagicMock()
    upsert_result.upserted_id = "new-id-001" if upsert_new else None
    tx_coll.update_one = AsyncMock(return_value=upsert_result)
    tx_coll.find_one = AsyncMock(return_value=None)
    aggregate_cursor = MagicMock()
    aggregate_cursor.__aiter__ = MagicMock(return_value=iter([]))
    tx_coll.aggregate = MagicMock(return_value=aggregate_cursor)

    acct_coll = MagicMock()
    acct_coll.find_one = AsyncMock(return_value=None)
    acct_coll.update_one = AsyncMock()

    db._db = MagicMock()
    db._db.demo_bank_transactions = tx_coll
    db._db.demo_bank_accounts = acct_coll
    return db


def _row(**overrides) -> ReconstructedTransactionRow:
    defaults = dict(
        account_ref="ADMIN-16244",
        unit_number="1",
        financial_year="2024",
        quarter=1,
        fund_type="admin",
        levy_component="ordinary",
        posted_date=date(2024, 3, 15),
        amount_cents=110000,
        amount_ex_gst_cents=100000,
        gst_cents=10000,
        direction="credit",
        assumption_code="quarterly_regular",
        description="Reconstructed Q1 2024 admin levy — Lot 1",
        transaction_sequence=1,
    )
    defaults.update(overrides)
    return ReconstructedTransactionRow(**defaults)


def _batch(status: str = "approved", **overrides) -> ReconstructionBatch:
    defaults = dict(
        batch_id="batch-001",
        building_id=_BUILDING_A,
        financial_year_start=2021,
        financial_year_end=2026,
        reconstruction_method="gst_uoe_largest_remainder_v5",
        status=status,
        is_test_data=True,
    )
    defaults.update(overrides)
    return ReconstructionBatch(**defaults)


def _manifest(batch_id: str = "batch-001", transactions=None, **overrides) -> ReconstructionManifest:
    defaults = dict(
        manifest_id="manifest-001",
        batch_id=batch_id,
        building_id=_BUILDING_A,
        input_fact_hash="abc123",
        generator_version="historical-levy-reconstruction-v5",
        expected_transaction_count=1,
        expected_credit_cents=110000,
        transactions=transactions if transactions is not None else [_row()],
        manifest_hash="deadbeef",
    )
    defaults.update(overrides)
    return ReconstructionManifest(**defaults)


# ── 1. Approval-gate ──────────────────────────────────────────────────────────

class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_refuses_batch_not_yet_approved(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        batch = _batch(status="needs_review")
        manifest = _manifest()

        with pytest.raises(ValueError, match="needs_review"):
            await import_historical_reconstruction(db, _BUILDING_A, batch, manifest)

        db._db.demo_bank_transactions.update_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_approved_batch(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        result = await import_historical_reconstruction(db, _BUILDING_A, _batch(status="approved"), _manifest())
        assert result["imported_count"] == 1
        assert result["error_count"] == 0

    @pytest.mark.asyncio
    async def test_accepts_generation_ready_batch(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        result = await import_historical_reconstruction(
            db, _BUILDING_A, _batch(status="generation_ready"), _manifest()
        )
        assert result["imported_count"] == 1


# ── 2. Manifest/batch consistency guards ──────────────────────────────────────

class TestConsistencyGuards:
    @pytest.mark.asyncio
    async def test_refuses_manifest_batch_id_mismatch(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        batch = _batch(batch_id="batch-001")
        manifest = _manifest(batch_id="batch-999")
        with pytest.raises(ValueError, match="batch-999"):
            await import_historical_reconstruction(db, _BUILDING_A, batch, manifest)

    @pytest.mark.asyncio
    async def test_refuses_building_id_mismatch(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        batch = _batch(building_id=_BUILDING_A)
        manifest = _manifest()
        with pytest.raises(ValueError, match="building"):
            await import_historical_reconstruction(db, "99999", batch, manifest)


# ── 3. Provenance fields ──────────────────────────────────────────────────────

class TestProvenanceFields:
    @pytest.mark.asyncio
    async def test_every_row_carries_reconstruction_provenance(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        batch = _batch(batch_id="batch-001")
        manifest = _manifest(version=3)
        await import_historical_reconstruction(db, _BUILDING_A, batch, manifest)

        call_args = db._db.demo_bank_transactions.update_one.call_args_list[0]
        doc = call_args.args[1]["$setOnInsert"]
        assert doc["transaction_origin"] == "reconstructed_historical"
        assert doc["reconstruction_batch_id"] == "batch-001"
        assert doc["reconstruction_version"] == 3
        assert doc["assumption_code"] == "quarterly_regular"
        assert doc["source_type"] == "historical_reconstruction"
        assert doc["confidence"] == "high"
        assert doc["provenance_class"] == "reconstruction"

    @pytest.mark.asyncio
    async def test_levy_component_ordinary_vs_special_preserved(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        rows = [
            _row(unit_number="1", levy_component="ordinary", transaction_sequence=1),
            _row(unit_number="1", levy_component="special_levy", transaction_sequence=2),
        ]
        manifest = _manifest(transactions=rows, expected_transaction_count=2)
        await import_historical_reconstruction(db, _BUILDING_A, _batch(), manifest)

        docs = [c.args[1]["$setOnInsert"] for c in db._db.demo_bank_transactions.update_one.call_args_list]
        components = {d["levy_component"] for d in docs}
        assert components == {"ordinary", "special_levy"}


# ── 4. Idempotent replay ──────────────────────────────────────────────────────

class TestIdempotentReplay:
    @pytest.mark.asyncio
    async def test_second_materialisation_is_a_noop_via_setoninsert(self):
        """_upsert_transaction always issues $setOnInsert upserts — replaying the
        same manifest must never use insert_one, and a DB that reports no new
        upsert (upserted_id=None, i.e. the row already existed) must be counted
        as skipped, not imported."""
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db_second_run = _make_db(upsert_new=False)
        result = await import_historical_reconstruction(db_second_run, _BUILDING_A, _batch(), _manifest())

        db_second_run._db.demo_bank_transactions.insert_one.assert_not_called()
        assert result["imported_count"] == 0
        assert result["skipped_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_manifest_is_a_clean_noop(self):
        from strataos_demo_integrations.demo_bank.ingestion import import_historical_reconstruction

        db = _make_db()
        result = await import_historical_reconstruction(
            db, _BUILDING_A, _batch(), _manifest(transactions=[], expected_transaction_count=0)
        )
        assert result == {
            "batch_id": "batch-001",
            "import_status": "completed",
            "imported_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "duplicate_batch": False,
            "message": "Manifest contains no transactions — nothing to materialise.",
        }
        db._db.demo_bank_transactions.update_one.assert_not_called()


# ── 5. Strata Web guard remains untouched ─────────────────────────────────────

class TestStrataWebGuardUnmodified:
    def test_balance_only_snapshot_still_rejected(self):
        """Regression smoke test: adding reconstruction ingestion must not weaken
        the existing direct-ingestion guard. A snapshot with only balance/arrears
        fields (no 'payments' list) must still yield zero extractable rows."""
        from strataos_demo_integrations.demo_bank.ingestion import _extract_strata_web_payments

        balance_only_snapshot = {
            "_id": "snap-1",
            "raw_admin_fund_balance_cents": 500000,
            "raw_sinking_fund_balance_cents": 200000,
            "raw_arrears_total_cents": 15000,
        }
        result = _extract_strata_web_payments(balance_only_snapshot, _BUILDING_A)
        assert result == []

    def test_dated_payment_row_still_accepted(self):
        from strataos_demo_integrations.demo_bank.ingestion import _extract_strata_web_payments

        snapshot_with_payments = {
            "_id": "snap-2",
            "payments": [
                {"payment_date": "2024-03-15", "amount_cents": 110000, "lot_number": "1"},
            ],
        }
        result = _extract_strata_web_payments(snapshot_with_payments, _BUILDING_A)
        assert len(result) == 1
        assert result[0]["amount_cents"] == 110000
