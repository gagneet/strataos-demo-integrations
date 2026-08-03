# @featuretrace:levy — Tests for run_scraper.py's _sync_scraper_to_levy_collections, the write
#   path the real browser-scraper subprocess uses (as opposed to router.py's push_data endpoint,
#   which has its own, separately-tested _sync_to_levy_collections).
# Layer: test
# Related: strataos_demo_integrations/strata_sync/run_scraper.py
#          backend/tasks/GAP-FIN-035-collection-rate-2026-parity-expense-pipeline.md (Item 2)
"""Regression tests for GAP-FIN-035 Item 2's two fixes:

1. _sync_scraper_to_levy_collections previously only recomputed unit_levy_ledger's derived
   fields (total_levied/total_paid/admin_*/sinking_*) when the existing doc's data_source ==
   "scraper" -- but a reconciled doc never sets that field, so its derived fields went stale
   while net_balance kept moving on every re-scrape. Fixed to skip recompute only when the doc
   carries reconciliation_note/reconciliation_source (i.e. is explicitly reconciled).
2. The function had zero disable_strata_sync_direct_write toggle check, unlike router.py's
   push_data endpoint which already checked it before calling its own _sync_to_levy_collections.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strataos_demo_integrations.strata_sync.run_scraper import _sync_scraper_to_levy_collections

BUILDING_ID = "13195"


def _mock_mdb(existing_ledger_doc=None, existing_levy_doc=None):
    """A dict-subscriptable mock mirroring how run_scraper.py accesses collections
    (mdb["unit_levy_ledger"], not mdb.unit_levy_ledger)."""
    collections = {
        "levy_categories": MagicMock(update_one=AsyncMock()),
        "annual_levies": MagicMock(
            find_one=AsyncMock(return_value=existing_levy_doc),
            insert_one=AsyncMock(),
            update_one=AsyncMock(),
        ),
        "unit_levy_ledger": MagicMock(
            find_one=AsyncMock(return_value=existing_ledger_doc),
            update_one=AsyncMock(),
            insert_one=AsyncMock(),
        ),
    }
    mdb = MagicMock()
    mdb.__getitem__.side_effect = collections.__getitem__
    return mdb, collections


def _owner(unit_number="UA001", uoe=100, balance=250.0):
    return {"unit_number": unit_number, "uoe": uoe, "balance": balance}


def _financial(category="Cleaning", planned=1000.0, actual=900.0):
    return {"category": category, "fund": "admin", "planned": planned, "actual": actual}


@pytest.mark.asyncio
async def test_toggle_enabled_skips_all_writes():
    """Fix 2: when disable_strata_sync_direct_write is enabled for the building, the function
    must write NOTHING -- not levy_categories, not annual_levies, not unit_levy_ledger."""
    mdb, collections = _mock_mdb()

    with patch(
        "db_postgres.repos.config_repo.resolve_feature_toggle",
        new=AsyncMock(return_value=True),
    ):
        await _sync_scraper_to_levy_collections(
            mdb, BUILDING_ID, "2025-2026", [_financial()], [_owner()],
        )

    collections["levy_categories"].update_one.assert_not_called()
    collections["annual_levies"].insert_one.assert_not_called()
    collections["annual_levies"].update_one.assert_not_called()
    collections["unit_levy_ledger"].update_one.assert_not_called()
    collections["unit_levy_ledger"].insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_disabled_writes_proceed_normally():
    """When the toggle is off (default), the function must still write as before -- this fix
    must not silently disable syncing for every building, only when explicitly toggled."""
    mdb, collections = _mock_mdb(
        existing_levy_doc={
            "building_id": BUILDING_ID, "year": "2026", "total_uoe": 10000,
            "admin_fund": {"total_income": 1000.0}, "sinking_fund": {"total_income": 300.0},
        },
    )

    with patch(
        "db_postgres.repos.config_repo.resolve_feature_toggle",
        new=AsyncMock(return_value=False),
    ):
        await _sync_scraper_to_levy_collections(
            mdb, BUILDING_ID, "2025-2026", [_financial()], [_owner()],
        )

    collections["levy_categories"].update_one.assert_called_once()
    # No existing ledger doc in this fixture -> the function's insert branch runs, not update.
    collections["unit_levy_ledger"].insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_reconciled_ledger_doc_net_balance_updates_but_derived_fields_protected():
    """Fix 1: a doc carrying reconciliation_note (the real-world shape a reconciled 2026 record
    has, per east_gate_2021_2025_ledger_sync.py and its 2026 equivalent) must have net_balance
    updated (so the portal cross-check stays current) but total_levied/total_paid/admin_*/
    sinking_* must NOT be overwritten by the coarse scraper-derived estimate."""
    existing_ledger = {
        "building_id": BUILDING_ID, "unit_number": "UA001", "year": "2026",
        "reconciliation_note": "back-solved from portal balance",
        "reconciliation_source": "strata_web_portal_scrape",
        "total_levied": 3545.02, "total_paid": 3800.00,
    }
    mdb, collections = _mock_mdb(
        existing_ledger_doc=existing_ledger,
        existing_levy_doc={
            "building_id": BUILDING_ID, "year": "2026", "total_uoe": 10000,
            "admin_fund": {"total_income": 1000.0}, "sinking_fund": {"total_income": 300.0},
        },
    )

    with patch(
        "db_postgres.repos.config_repo.resolve_feature_toggle",
        new=AsyncMock(return_value=False),
    ):
        await _sync_scraper_to_levy_collections(
            mdb, BUILDING_ID, "2025-2026", [_financial()], [_owner(balance=100.0)],
        )

    collections["unit_levy_ledger"].update_one.assert_called_once()
    call_args = collections["unit_levy_ledger"].update_one.call_args
    set_doc = call_args[0][1]["$set"]
    assert "net_balance" in set_doc
    assert set_doc["net_balance"] == 100.0
    # The reconciled figures must not be present in this update's $set -- protected.
    assert "total_levied" not in set_doc
    assert "total_paid" not in set_doc
    assert "admin_levied" not in set_doc


@pytest.mark.asyncio
async def test_unreconciled_ledger_doc_recomputes_derived_fields_even_without_data_source():
    """The old buggy condition required data_source == "scraper" to recompute. A doc with
    NEITHER data_source NOR reconciliation_note/reconciliation_source (e.g. a plain first-pass
    scraper-created doc predating this fix) must still get its derived fields recomputed --
    the new condition is opt-out (skip if reconciled), not opt-in (only if flagged scraper)."""
    existing_ledger = {
        "building_id": BUILDING_ID, "unit_number": "UA001", "year": "2026",
        "total_levied": 0.0, "total_paid": 0.0,
    }
    mdb, collections = _mock_mdb(
        existing_ledger_doc=existing_ledger,
        existing_levy_doc={
            "building_id": BUILDING_ID, "year": "2026", "total_uoe": 10000,
            "admin_fund": {"total_income": 1000.0}, "sinking_fund": {"total_income": 300.0},
        },
    )

    with patch(
        "db_postgres.repos.config_repo.resolve_feature_toggle",
        new=AsyncMock(return_value=False),
    ):
        await _sync_scraper_to_levy_collections(
            mdb, BUILDING_ID, "2025-2026", [_financial()], [_owner(balance=100.0)],
        )

    set_doc = collections["unit_levy_ledger"].update_one.call_args[0][1]["$set"]
    assert "total_levied" in set_doc
    assert "admin_levied" in set_doc
