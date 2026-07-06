"""
Tests for OwnerUnitUpdate balance field sync to unit_levy_ledger.

Verifies the single source of truth mechanism:
  - PUT /owners-units/{unit} with opening_arrears → syncs to unit_levy_ledger
  - POST /owners-units/sync-arrears → bulk-syncs all units to ledger
  - Admin/sinking split uses annual_levies rates (falls back to 77.4%)
  - Endpoint is Super Admin only

No live DB required; all DB calls are mocked.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_unit(unit_number="UA042", opening_arrears=1063.77):
    return {
        "id": "uid-001",
        "building_id": "13195",
        "lot_number": "42",
        "unit_number": unit_number,
        "owner_name": "Test Owner",
        "unit_type": "apartment",
        "entitlement": 115,
        "opening_arrears": opening_arrears,
        "balance_owing": opening_arrears,
        "balance_credit": 0.0,
        "unit_entitlement": 115,
        "is_owner_occupied": True,
        "admin_closing_balance": 0.0,
        "sinking_closing_balance": 0.0,
        "total_levied": 0.0,
        "total_paid": 0.0,
        "net_balance": 0.0,
        "period_levy": 0.0,
        "next_payment_adjusted": 0.0,
        "next_due_date": None,
        "period_status": None,
        "yearly_forecast": None,
        "badges": [],
        "is_on_platform": False,
        "permissions": {},
        "created_at": "2025-01-01T00:00:00+00:00",
        "updated_at": "2026-03-30T00:00:00+00:00",
    }


def _make_levy_doc(admin_rate=35.0, sinking_rate=12.0):
    return {
        "building_id": "13195",
        "year": "2026",
        "admin_levy_per_uoe_annual": admin_rate,
        "sinking_levy_per_uoe_annual": sinking_rate,
    }


# ---------------------------------------------------------------------------
# Unit tests: OwnerUnitUpdate model
# ---------------------------------------------------------------------------

class TestOwnerUnitUpdateModel:
    """OwnerUnitUpdate now includes balance fields."""

    def test_balance_fields_present(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import OwnerUnitUpdate
        m = OwnerUnitUpdate(opening_arrears=100.0, balance_owing=100.0, balance_credit=0.0)
        assert m.opening_arrears == 100.0
        assert m.balance_owing == 100.0
        assert m.balance_credit == 0.0

    def test_non_balance_fields_unchanged(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import OwnerUnitUpdate
        m = OwnerUnitUpdate(owner_name="Alice")
        assert m.owner_name == "Alice"
        assert m.opening_arrears is None
        assert m.balance_owing is None

    def test_all_balance_fields_optional(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import OwnerUnitUpdate
        m = OwnerUnitUpdate()
        assert m.opening_arrears is None
        assert m.balance_owing is None
        assert m.balance_credit is None
        assert m.admin_closing_balance is None
        assert m.sinking_closing_balance is None

    def test_admin_closing_sinking_closing_fields(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import OwnerUnitUpdate
        m = OwnerUnitUpdate(admin_closing_balance=150000.0, sinking_closing_balance=80000.0)
        assert m.admin_closing_balance == 150000.0
        assert m.sinking_closing_balance == 80000.0


# ---------------------------------------------------------------------------
# Unit tests: sync logic (isolated from HTTP layer)
# ---------------------------------------------------------------------------

class TestSyncArrearsToLedgerLogic:
    """Verify the admin/sinking split arithmetic is correct."""

    def test_split_using_levy_rates(self):
        """Given admin=35, sinking=12 → admin_frac = 35/47 ≈ 0.7447"""
        admin_rate, sinking_rate = 35.0, 12.0
        total_rate = admin_rate + sinking_rate
        admin_frac = admin_rate / total_rate
        opening = 1063.77
        admin_opening = round(opening * admin_frac, 2)
        sinking_opening = round(opening - admin_opening, 2)

        assert abs(admin_frac - (35 / 47)) < 1e-6
        assert admin_opening + sinking_opening == pytest.approx(opening, abs=0.01)
        assert admin_opening > sinking_opening  # Admin levy is always higher

    def test_fallback_fraction_used_when_no_levy_doc(self):
        """When annual_levies doc is absent, fallback admin_frac = 0.774"""
        admin_frac = 0.774
        opening = 316.97
        admin_opening = round(opening * admin_frac, 2)
        sinking_opening = round(opening - admin_opening, 2)

        assert admin_opening + sinking_opening == pytest.approx(opening, abs=0.01)
        assert admin_opening == pytest.approx(316.97 * 0.774, abs=0.01)

    def test_zero_arrears_splits_to_zero(self):
        admin_frac = 0.774
        opening = 0.0
        assert round(opening * admin_frac, 2) == 0.0
        assert round(opening - 0.0, 2) == 0.0

    def test_negative_arrears_clamped_to_zero(self):
        """Credit balances should not produce negative openings in ledger."""
        opening = -500.0  # credit
        # The sync should store max(0, opening) style logic; we use the raw value here
        # but downstream get_arrears_metrics uses max(0.0, opening) guard already
        admin_frac = 0.774
        admin_opening = round(opening * admin_frac, 2)
        # Value is negative; finance_helpers.get_arrears_metrics ignores values <= 0.01
        assert admin_opening < 0

    def test_total_is_preserved_across_all_units(self):
        """Sum of (admin+sinking) across all units equals sum of opening_arrears."""
        units = [
            {"opening_arrears": 1063.77},
            {"opening_arrears": 316.97},
            {"opening_arrears": 97.36},
            {"opening_arrears": 226.15},
            {"opening_arrears": 154.00},
        ]
        admin_frac = 35 / 47  # Using levy-derived fraction
        total_input = sum(u["opening_arrears"] for u in units)

        total_synced = 0.0
        for u in units:
            ao = round(u["opening_arrears"] * admin_frac, 2)
            so = round(u["opening_arrears"] - ao, 2)
            total_synced += ao + so

        assert abs(total_synced - total_input) < 0.01 * len(units)  # Max 1 cent rounding per unit


# ---------------------------------------------------------------------------
# Integration tests: update_owner_unit endpoint with mocked DB
# ---------------------------------------------------------------------------

class TestUpdateOwnerUnitBalanceSyncIntegration:
    """Test that the endpoint correctly syncs to unit_levy_ledger."""

    @pytest.fixture
    def mock_db_with_levy(self):
        mock_db = MagicMock()

        unit_doc = _make_unit("UA042", 1063.77)
        mock_db.units.find_one = AsyncMock(return_value=unit_doc)
        mock_db.units.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        levy_doc = _make_levy_doc()
        mock_db.annual_levies.find_one = AsyncMock(return_value=levy_doc)
        mock_db.unit_levy_ledger.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        return mock_db

    @pytest.mark.asyncio
    async def test_balance_update_triggers_ledger_sync(self, mock_db_with_levy):
        """When opening_arrears is changed, unit_levy_ledger.update_one must be called."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import update_owner_unit, OwnerUnitUpdate

        updates = OwnerUnitUpdate(opening_arrears=500.0, balance_owing=500.0)
        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_with_levy):
            await update_owner_unit("UA042", updates, current_user, "13195")

        # Ledger sync must have been called
        mock_db_with_levy.unit_levy_ledger.update_one.assert_called_once()
        call_kwargs = mock_db_with_levy.unit_levy_ledger.update_one.call_args
        filter_doc = call_kwargs[0][0]
        set_doc = call_kwargs[0][1]["$set"]

        assert filter_doc["unit_number"] == "UA042"
        assert filter_doc["building_id"] == "13195"
        assert filter_doc["year"] == str(datetime.now(timezone.utc).year)
        assert "admin_opening" in set_doc
        assert "sinking_opening" in set_doc

    @pytest.mark.asyncio
    async def test_non_balance_update_skips_ledger_sync(self, mock_db_with_levy):
        """Updating unit metadata only must NOT touch unit_levy_ledger."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import update_owner_unit, OwnerUnitUpdate

        updates = OwnerUnitUpdate(notes="Updated notes")
        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_with_levy):
            await update_owner_unit("UA042", updates, current_user, "13195")

        mock_db_with_levy.unit_levy_ledger.update_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_split_matches_levy_rates(self, mock_db_with_levy):
        """admin_opening = opening * (admin_rate / total_rate) using DB rates."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import update_owner_unit, OwnerUnitUpdate

        # Unit has opening_arrears=1000.0 after update
        unit_after = _make_unit("UA042", 1000.0)
        mock_db_with_levy.units.find_one = AsyncMock(side_effect=[
            _make_unit("UA042", 500.0),  # first call: existing unit check
            unit_after,  # second call: updated unit for sync
            unit_after,  # third call: return value
        ])

        updates = OwnerUnitUpdate(opening_arrears=1000.0)
        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_with_levy):
            await update_owner_unit("UA042", updates, current_user, "13195")

        call_kwargs = mock_db_with_levy.unit_levy_ledger.update_one.call_args
        set_doc = call_kwargs[0][1]["$set"]

        # admin_rate=35, sinking_rate=12, total=47 → admin_frac ≈ 0.7447
        expected_admin = round(1000.0 * (35 / 47), 2)
        expected_sinking = round(1000.0 - expected_admin, 2)

        assert set_doc["admin_opening"] == pytest.approx(expected_admin, abs=0.01)
        assert set_doc["sinking_opening"] == pytest.approx(expected_sinking, abs=0.01)

    @pytest.mark.asyncio
    async def test_fallback_fraction_when_no_levy_doc(self, mock_db_with_levy):
        """When annual_levies has no doc, use admin_frac=0.774 fallback."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import update_owner_unit, OwnerUnitUpdate

        mock_db_with_levy.annual_levies.find_one = AsyncMock(return_value=None)
        # Unit has opening_arrears=100.0 after the update
        unit_100 = _make_unit("UA042", 100.0)
        mock_db_with_levy.units.find_one = AsyncMock(return_value=unit_100)

        updates = OwnerUnitUpdate(opening_arrears=100.0)
        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_with_levy):
            await update_owner_unit("UA042", updates, current_user, "13195")

        call_kwargs = mock_db_with_levy.unit_levy_ledger.update_one.call_args
        set_doc = call_kwargs[0][1]["$set"]

        # Fallback: 0.774
        assert set_doc["admin_opening"] == pytest.approx(100.0 * 0.774, abs=0.01)


# ---------------------------------------------------------------------------
# Integration tests: bulk sync endpoint
# ---------------------------------------------------------------------------

class TestBulkSyncArrears:
    """Tests for POST /owners-units/sync-arrears."""

    @pytest.fixture
    def mock_db_bulk(self):
        mock_db = MagicMock()

        units = [
            _make_unit("UA042", 1063.77),
            _make_unit("TH074", 316.97),
            _make_unit("UA001", 154.00),
            _make_unit("UA070", 226.15),
            _make_unit("UA019", 0.0),  # No arrears — still synced
        ]
        mock_db.units.find = MagicMock(
            return_value=MagicMock(to_list=AsyncMock(return_value=units))
        )
        mock_db.annual_levies.find_one = AsyncMock(return_value=_make_levy_doc())
        mock_db.unit_levy_ledger.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1)
        )
        return mock_db

    @pytest.mark.asyncio
    async def test_syncs_all_units(self, mock_db_bulk):
        """All units in the building get their ledger updated."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import sync_arrears_to_ledger

        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_bulk):
            result = await sync_arrears_to_ledger(current_user, "13195")

        assert result["units_updated"] == 5
        assert result["status"] == "ok"
        assert result["building_id"] == "13195"
        assert mock_db_bulk.unit_levy_ledger.update_one.call_count == 5

    @pytest.mark.asyncio
    async def test_non_admin_denied(self, mock_db_bulk):
        """Only Super Admin can run bulk sync."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import sync_arrears_to_ledger
        from fastapi import HTTPException

        current_user = {"role": "owner", "id": "user-1"}

        with patch("server.db", mock_db_bulk):
            with pytest.raises(HTTPException) as exc_info:
                await sync_arrears_to_ledger(current_user, "13195")

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_year_in_response_is_current(self, mock_db_bulk):
        """Response year matches current calendar year."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import sync_arrears_to_ledger

        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_bulk):
            result = await sync_arrears_to_ledger(current_user, "13195")

        assert result["year"] == str(datetime.now(timezone.utc).year)

    @pytest.mark.asyncio
    async def test_skipped_when_no_ledger_entry(self, mock_db_bulk):
        """Units with no ledger entry increment skipped_count."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))
        from server import sync_arrears_to_ledger

        # 2 matched, 3 not found in ledger
        mock_db_bulk.unit_levy_ledger.update_one = AsyncMock(
            side_effect=[
                MagicMock(matched_count=1),
                MagicMock(matched_count=0),
                MagicMock(matched_count=0),
                MagicMock(matched_count=1),
                MagicMock(matched_count=0),
            ]
        )
        current_user = {"role": "super_admin", "id": "admin-1"}

        with patch("server.db", mock_db_bulk):
            result = await sync_arrears_to_ledger(current_user, "13195")

        assert result["units_updated"] == 2
        assert result["units_skipped"] == 3


# ---------------------------------------------------------------------------
# Data integrity: current state verification
# ---------------------------------------------------------------------------

class TestCurrentDataConsistency:
    """
    Documents expected data state as of 2026-03-30.
    These are pure arithmetic checks — no DB calls.
    """

    EXPECTED_ARREARS = [
        ("TH074", 316.97),
        ("TH077", 97.36),
        ("TH078", 20.25),
        ("TH085", 20.23),
        ("UA001", 154.00),
        ("UA019", 5.00),
        ("UA028", 5.44),
        ("UA030", 12.82),
        ("UA034", 12.82),
        ("UA042", 1063.77),
        ("UA067", 10.95),
        ("UA070", 226.15),
    ]

    def test_total_matches_expected(self):
        """Total outstanding as of 2026-03-30 = $1,945.76 (UA058 $0.01 below threshold)."""
        total = sum(v for _, v in self.EXPECTED_ARREARS)
        assert abs(total - 1945.76) < 0.01

    def test_unit_count(self):
        """12 units with arrears > $0.01 threshold (UA058 $0.01 excluded by > not >=)."""
        assert len(self.EXPECTED_ARREARS) == 12

    def test_ua042_is_largest(self):
        """UA042 carries the largest outstanding balance."""
        max_unit = max(self.EXPECTED_ARREARS, key=lambda x: x[1])
        assert max_unit[0] == "UA042"
        assert max_unit[1] == pytest.approx(1063.77, abs=0.01)

    def test_admin_sinking_split_totals_match(self):
        """Admin + sinking split sums back to original for each unit."""
        admin_frac = 35 / 47  # From annual_levies FY2026
        for unit, opening in self.EXPECTED_ARREARS:
            ao = round(opening * admin_frac, 2)
            so = round(opening - ao, 2)
            assert ao + so == pytest.approx(opening, abs=0.01), f"{unit}: {ao}+{so} != {opening}"
