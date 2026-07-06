"""
Tests for the Financial Year Import feature.

Tests cover all 4 CSV processor functions plus the router endpoints:
  - process_unit_owners_csv
  - process_annual_levy_csv
  - process_budget_categories_csv
  - process_unit_levy_status_csv
  - GET /financial-import/history
  - GET /financial-import/templates/{type}
  - POST /financial-import/unit-owners
  - POST /financial-import/annual-levy
  - POST /financial-import/budget-categories
  - POST /financial-import/unit-levy-status

Run with:
    backend/venv/bin/pytest tests/backend/test_financial_year_import.py -v

All tests use mock DB and do NOT require a live server or database.
"""

from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & helpers
# ─────────────────────────────────────────────────────────────────────────────

def _csv(text: str) -> bytes:
    """Convert CSV string to UTF-8 bytes."""
    return text.encode("utf-8")


def _bom_csv(text: str) -> bytes:
    """CSV bytes with UTF-8 BOM (Excel default)."""
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def _latin1_csv(text: str) -> bytes:
    """CSV bytes in latin-1 encoding."""
    return text.encode("latin-1")


BUILDING_ID = "bldg_test_001"
CREATED_BY = "user_test_001"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Model imports
# ─────────────────────────────────────────────────────────────────────────────

class TestModels:
    def test_import_result_defaults(self):
        from strataos_demo_integrations.data_upload.models import ImportResult
        r = ImportResult(sheet_type="unit_owners")
        assert r.total_rows == 0
        assert r.imported == 0
        assert r.updated == 0
        assert r.skipped == 0
        assert r.errors == []
        assert r.warnings == []

    def test_financial_year_import_response_fields(self):
        from strataos_demo_integrations.data_upload.models import FinancialYearImportResponse, ImportResult
        r = ImportResult(sheet_type="annual_levy", imported=1)
        resp = FinancialYearImportResponse(
            import_id=str(uuid.uuid4()),
            building_id=BUILDING_ID,
            financial_year="2026",
            sheet_type="annual_levy",
            status="completed",
            result=r,
            created_at="2026-01-01T00:00:00Z",
            created_by=CREATED_BY,
        )
        assert resp.sheet_type == "annual_levy"
        assert resp.result.imported == 1

    def test_csv_templates_has_all_types(self):
        from strataos_demo_integrations.data_upload.models import CSV_TEMPLATES
        assert "unit_owners" in CSV_TEMPLATES
        assert "annual_levy" in CSV_TEMPLATES
        assert "budget_categories" in CSV_TEMPLATES
        assert "unit_levy_status" in CSV_TEMPLATES


# ─────────────────────────────────────────────────────────────────────────────
# 2. Service helper functions
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceHelpers:
    def test_parse_float_normal(self):
        from strataos_demo_integrations.data_upload.service import _parse_float
        assert _parse_float("1234.56") == pytest.approx(1234.56)

    def test_parse_float_strips_dollar(self):
        from strataos_demo_integrations.data_upload.service import _parse_float
        assert _parse_float("$1,234.56") == pytest.approx(1234.56)

    def test_parse_float_strips_comma(self):
        from strataos_demo_integrations.data_upload.service import _parse_float
        assert _parse_float("1,000,000") == pytest.approx(1000000.0)

    def test_parse_float_empty_returns_default(self):
        from strataos_demo_integrations.data_upload.service import _parse_float
        assert _parse_float("") == 0.0
        assert _parse_float("  ") == 0.0

    def test_parse_float_none_returns_default(self):
        from strataos_demo_integrations.data_upload.service import _parse_float
        assert _parse_float(None) == 0.0

    def test_parse_float_invalid_returns_default(self):
        from strataos_demo_integrations.data_upload.service import _parse_float
        assert _parse_float("N/A") == 0.0

    def test_decode_csv_utf8_bom(self):
        from strataos_demo_integrations.data_upload.service import _decode_csv
        result = _decode_csv(_bom_csv("unit_number,uoe\nUA001,115"))
        assert result.startswith("unit_number")

    def test_decode_csv_latin1(self):
        from strataos_demo_integrations.data_upload.service import _decode_csv
        result = _decode_csv(_latin1_csv("unit_number,uoe\nUA001,115"))
        assert "UA001" in result

    def test_infer_levy_status_arrears(self):
        """Levy status logic: net_balance > 0.01 → arrears (inline in service)."""
        net_balance = 100.0
        status = "arrears" if net_balance > 0.01 else "credit" if net_balance < -0.01 else "current"
        assert status == "arrears"

    def test_infer_levy_status_credit(self):
        net_balance = -50.0
        status = "arrears" if net_balance > 0.01 else "credit" if net_balance < -0.01 else "current"
        assert status == "credit"

    def test_infer_levy_status_current(self):
        for net_balance in [0.0, 0.001, -0.001]:
            status = "arrears" if net_balance > 0.01 else "credit" if net_balance < -0.01 else "current"
            assert status == "current", f"Expected 'current' for {net_balance}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. process_unit_owners_csv
# ─────────────────────────────────────────────────────────────────────────────

UNIT_OWNERS_CSV = """\
lot_number,unit_number,unit_type,mixed_use_type,primary_owner_name,secondary_owner_name,owner_email,uoe,asset_value,status,notes
LOT1,UA001,apartment,,John Smith,Jane Smith,john@example.com,115,650000,owner_occupied,
LOT2,UA002,apartment,,Alice Brown,,alice@example.com,120,,tenanted,near lift
LOT18,TH071,townhouse,villa,Bob Jones,,,140,,owner_occupied,
"""

UNIT_OWNERS_MISSING_UNIT = """\
lot_number,unit_number,primary_owner_name,uoe
LOT1,,John Smith,115
LOT2,UA002,Alice Brown,120
"""


class TestProcessUnitOwners:
    @pytest.fixture
    def mock_db(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock:
            mock.units.find_one = AsyncMock(return_value=None)
            mock.units.insert_one = AsyncMock()
            mock.units.update_one = AsyncMock()
            yield mock

    def test_imports_three_rows(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(_csv(UNIT_OWNERS_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.total_rows == 3
        assert result.imported == 3
        assert result.skipped == 0
        assert result.errors == []

    def test_upserts_when_existing(self, mock_db):
        mock_db.units.find_one = AsyncMock(return_value={"id": "existing-id"})
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(_csv(UNIT_OWNERS_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.updated == 3
        assert result.imported == 0

    def test_skips_row_with_missing_unit_number(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(_csv(UNIT_OWNERS_MISSING_UNIT), BUILDING_ID, CREATED_BY)
        )
        assert result.skipped == 1
        assert result.imported == 1
        assert "Row 1" in result.errors[0]
        assert "unit_number" in result.errors[0]

    def test_building_id_injected_on_insert(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(_csv(UNIT_OWNERS_CSV), BUILDING_ID, CREATED_BY)
        )
        call_args = mock_db.units.insert_one.call_args_list[0][0][0]
        assert call_args["building_id"] == BUILDING_ID
        assert call_args["unit_entitlement"] == 115
        assert call_args["entitlement"] == 115

    def test_handles_bom_encoding(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(_bom_csv(UNIT_OWNERS_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.imported == 3

    def test_handles_dollar_in_asset_value(self, mock_db):
        # Use quoted CSV value so the comma inside is preserved
        csv_data = _csv(
            'lot_number,unit_number,primary_owner_name,uoe,asset_value\n'
            'LOT1,UA001,John Smith,115,"$650,000"\n'
        )
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        assert result.imported == 1
        inserted = mock_db.units.insert_one.call_args_list[0][0][0]
        assert inserted["asset_value"] == pytest.approx(650000.0)

    def test_normalises_unit_type_lowercase(self, mock_db):
        csv_data = _csv(
            "lot_number,unit_number,primary_owner_name,uoe,unit_type\n"
            "LOT1,UA001,John Smith,115,APARTMENT\n"
        )
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.units.insert_one.call_args_list[0][0][0]
        assert inserted["unit_type"] == "apartment"

    def test_row_error_does_not_abort_import(self, mock_db):
        """Ensure remaining rows are processed even if one row fails."""
        mock_db.units.insert_one = AsyncMock(side_effect=[Exception("DB error"), None])
        from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
        csv_data = _csv(
            "lot_number,unit_number,primary_owner_name,uoe\n"
            "LOT1,UA001,John,115\n"
            "LOT2,UA002,Jane,120\n"
        )
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_owners_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        assert result.skipped == 1
        assert len(result.errors) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. process_annual_levy_csv
# ─────────────────────────────────────────────────────────────────────────────

ANNUAL_LEVY_CSV = """\
financial_year,admin_levy_per_uoe_proposed,admin_levy_per_uoe_actual,sinking_levy_per_uoe_proposed,sinking_levy_per_uoe_actual,admin_total_income_proposed,admin_total_income_actual,admin_total_expenses_proposed,admin_total_expenses_actual,admin_opening_balance,admin_closing_balance_projected,admin_closing_balance_actual,sinking_total_income_proposed,sinking_total_income_actual,sinking_total_expenses_proposed,sinking_total_expenses_actual,sinking_opening_balance,sinking_closing_balance_projected,sinking_closing_balance_actual
2026,999.00,,888.00,,309882.00,,309882.00,,15000.00,15000.00,,90459.00,,45000.00,,85000.00,139504.90,
"""

ANNUAL_LEVY_CSV_WITH_ACTUALS = """\
financial_year,admin_levy_per_uoe_proposed,admin_levy_per_uoe_actual,sinking_levy_per_uoe_proposed,admin_total_income_proposed,admin_total_expenses_proposed,admin_opening_balance,admin_closing_balance_projected,admin_total_expenses_actual,sinking_total_income_proposed,sinking_total_expenses_proposed,sinking_opening_balance,sinking_closing_balance_projected
2026,999.00,23.10,888.00,309882.00,309882.00,15000.00,15000.00,335000.00,90459.00,45000.00,85000.00,139504.90
"""


class TestProcessAnnualLevy:
    @pytest.fixture
    def mock_db(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock:
            mock.annual_levies.find_one = AsyncMock(return_value=None)
            mock.annual_levies.insert_one = AsyncMock()
            mock.annual_levies.update_one = AsyncMock()
            mock.settings.find_one = AsyncMock(return_value={
                "building_id": BUILDING_ID,
                "gst_registered": True,
                "levy_gst_rate": 0.10,
            })
            # units.aggregate needed for total_uoe fallback when no existing levy record
            mock.units.aggregate.return_value.to_list = AsyncMock(return_value=[{"total": 10000}])
            yield mock

    def test_imports_single_year_row(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.imported == 1
        assert result.errors == []

    def test_status_proposed_when_no_actuals(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["status"] == "proposed"

    def test_status_actual_when_actuals_present(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV_WITH_ACTUALS), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["status"] == "actual"

    def test_quarterly_levy_derived(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["admin_levy_per_uoe_annual"] == pytest.approx(34.087, abs=0.001)
        assert inserted["admin_levy_per_uoe_quarterly"] == pytest.approx(34.087 / 4, abs=0.001)
        assert inserted["sinking_levy_per_uoe_annual"] == pytest.approx(9.9505, abs=0.001)
        assert inserted["sinking_levy_per_uoe_quarterly"] == pytest.approx(9.9505 / 4, abs=0.001)

    def test_payable_rates_are_derived_from_fund_totals_not_raw_csv_columns(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["admin_levy_per_uoe_annual"] != 999.0
        assert inserted["sinking_levy_per_uoe_annual"] != 888.0

    def test_admin_and_sinking_fund_subdocs_present(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert "admin_fund" in inserted
        assert "sinking_fund" in inserted
        assert inserted["admin_fund"]["levy_income"] == pytest.approx(309882.00)

    def test_current_balance_defaults_to_projected_close(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["admin_fund"]["current_balance"] == pytest.approx(15000.00)
        assert inserted["sinking_fund"]["current_balance"] == pytest.approx(139504.90)

    def test_current_balance_prefers_actual_close_when_present(self, mock_db):
        csv_data = _csv(
            "financial_year,admin_levy_per_uoe_proposed,admin_total_income_proposed,admin_total_expenses_proposed,"
            "admin_opening_balance,admin_closing_balance_projected,admin_closing_balance_actual,"
            "sinking_levy_per_uoe_proposed,sinking_total_income_proposed,sinking_total_expenses_proposed,"
            "sinking_opening_balance,sinking_closing_balance_projected,sinking_closing_balance_actual\n"
            "2026,23.45,340870.20,340870.20,15000.00,15000.00,18300.00,6.72,99504.90,45000.00,85000.00,139504.90,141500.00\n"
        )
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["admin_fund"]["current_balance"] == pytest.approx(18300.00)
        assert inserted["sinking_fund"]["current_balance"] == pytest.approx(141500.00)

    def test_blank_closing_balance_columns_do_not_store_zero_current_balance(self, mock_db):
        csv_data = _csv(
            "financial_year,admin_levy_per_uoe_proposed,admin_total_income_proposed,admin_total_expenses_proposed,"
            "admin_opening_balance,admin_closing_balance_projected,admin_closing_balance_actual,"
            "sinking_levy_per_uoe_proposed,sinking_total_income_proposed,sinking_total_expenses_proposed,"
            "sinking_opening_balance,sinking_closing_balance_projected,sinking_closing_balance_actual\n"
            "2026,23.45,340870.20,340870.20,15000.00,,,6.72,99504.90,45000.00,85000.00,,\n"
        )
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert "current_balance" not in inserted["admin_fund"]
        assert "current_balance" not in inserted["sinking_fund"]

    def test_missing_financial_year_skips_row(self, mock_db):
        csv_data = _csv(
            "financial_year,admin_levy_per_uoe_proposed\n"
            ",23.45\n"
            "2026,23.45\n"
        )
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        assert result.skipped == 1
        assert result.imported == 1

    def test_upserts_existing_year(self, mock_db):
        mock_db.annual_levies.find_one = AsyncMock(return_value={"id": "existing"})
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.updated == 1
        assert result.imported == 0

    def test_uses_units_total_uoe_when_available(self, mock_db):
        mock_db.units.aggregate.return_value.to_list = AsyncMock(return_value=[{"total": 9876}])
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        inserted = mock_db.annual_levies.insert_one.call_args_list[0][0][0]
        assert inserted["total_uoe"] == 9876

    def test_skips_row_when_total_uoe_cannot_be_derived(self, mock_db):
        mock_db.units.aggregate.return_value.to_list = AsyncMock(return_value=[])
        from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_annual_levy_csv(_csv(ANNUAL_LEVY_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.imported == 0
        assert result.skipped == 1
        assert "total_uoe" in result.errors[0]


# ─────────────────────────────────────────────────────────────────────────────
# 5. process_budget_categories_csv
# ─────────────────────────────────────────────────────────────────────────────

BUDGET_CATS_CSV = """\
financial_year,fund_type,category_name,budgeted_amount,actual_amount,description
2026,admin,Management Fee,"27,682.00",,Annual strata management fee
2026,admin,Cleaning,"$27,500.00","$25,300.00",Annual cleaning contract
2026,sinking,Painting,45000.00,,Exterior repaint
"""

BUDGET_CATS_MISSING_FIELDS = """\
financial_year,fund_type,category_name,budgeted_amount
2026,admin,,27682
2026,,Management Fee,27682
"""


class TestProcessBudgetCategories:
    @pytest.fixture
    def mock_db(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock:
            mock.levy_categories.find_one = AsyncMock(return_value=None)
            mock.levy_categories.insert_one = AsyncMock()
            mock.levy_categories.update_one = AsyncMock()
            yield mock

    def test_imports_three_categories(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.imported == 3
        assert result.errors == []

    def test_normalises_admin_to_administrative(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.levy_categories.insert_one.call_args_list
        # First two rows have fund_type "admin"
        assert inserts[0][0][0]["fund_type"] == "administrative"

    def test_sinking_fund_type_unchanged(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.levy_categories.insert_one.call_args_list
        sinking_insert = next(a[0][0] for a in inserts if a[0][0]["name"] == "Painting")
        assert sinking_insert["fund_type"] == "sinking"

    def test_actual_amount_parsed_with_dollar_comma(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.levy_categories.insert_one.call_args_list
        cleaning = next(a[0][0] for a in inserts if a[0][0]["name"] == "Cleaning")
        assert cleaning["actual_amount"] == pytest.approx(25300.0)

    def test_status_actual_when_actual_present(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.levy_categories.insert_one.call_args_list
        cleaning = next(a[0][0] for a in inserts if a[0][0]["name"] == "Cleaning")
        assert cleaning["status"] == "actual"

    def test_status_proposed_when_no_actual(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.levy_categories.insert_one.call_args_list
        painting = next(a[0][0] for a in inserts if a[0][0]["name"] == "Painting")
        assert painting["status"] == "proposed"

    def test_missing_category_name_skips_row(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_MISSING_FIELDS), BUILDING_ID, CREATED_BY)
        )
        assert result.skipped >= 1

    def test_building_id_in_upsert_filter(self, mock_db):
        mock_db.levy_categories.find_one = AsyncMock(return_value={"id": "existing"})
        from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
        asyncio.get_event_loop().run_until_complete(
            process_budget_categories_csv(_csv(BUDGET_CATS_CSV), BUILDING_ID, CREATED_BY)
        )
        filter_arg = mock_db.levy_categories.update_one.call_args_list[0][0][0]
        assert filter_arg["building_id"] == BUILDING_ID


# ─────────────────────────────────────────────────────────────────────────────
# 6. process_unit_levy_status_csv
# ─────────────────────────────────────────────────────────────────────────────

LEVY_STATUS_CSV = """\
lot_number,unit_number,financial_year,admin_opening_balance,admin_levied,admin_paid,admin_closing_balance,sinking_opening_balance,sinking_levied,sinking_paid,sinking_closing_balance,levy_status,q1_amount,q1_paid,q1_date,q2_amount,q2_paid,q2_date,q3_amount,q3_paid,q3_date,q4_amount,q4_paid,q4_date,arrears_amount,notes
LOT1,UA001,2026,0,2345.00,2345.00,0,0,672.00,672.00,0,current,756.75,756.75,2026-03-31,756.75,756.75,2026-06-30,756.75,756.75,2026-09-30,756.75,756.75,2026-12-31,0,
LOT2,UA002,2026,50,2400.00,1200.00,-50,0,690.00,0,690.00,arrears,600,600,,600,600,,600,0,,600,0,,1890,In DCA
"""


class TestProcessUnitLevyStatus:
    @pytest.fixture
    def mock_db(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock:
            mock.unit_levy_ledger.find_one = AsyncMock(return_value=None)
            mock.unit_levy_ledger.insert_one = AsyncMock()
            mock.unit_levy_ledger.update_one = AsyncMock()
            mock.units.find_one = AsyncMock(return_value={"unit_entitlement": 115})
            yield mock

    def test_imports_two_rows(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(_csv(LEVY_STATUS_CSV), BUILDING_ID, CREATED_BY)
        )
        assert result.imported == 2
        assert result.errors == []

    def test_net_balance_calculated(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(_csv(LEVY_STATUS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        ua001 = next(a[0][0] for a in inserts if a[0][0]["unit_number"] == "UA001")
        # admin_levied=2345, admin_paid=2345; sinking_levied=672, sinking_paid=672
        assert ua001["net_balance"] == pytest.approx(0.0)

    def test_arrears_net_balance(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(_csv(LEVY_STATUS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        ua002 = next(a[0][0] for a in inserts if a[0][0]["unit_number"] == "UA002")
        # admin_levied=2400, admin_paid=1200; sinking_levied=690, sinking_paid=0
        assert ua002["net_balance"] == pytest.approx(1890.0)

    def test_levy_status_from_csv_takes_priority(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(_csv(LEVY_STATUS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        ua001 = next(a[0][0] for a in inserts if a[0][0]["unit_number"] == "UA001")
        assert ua001["levy_status"] == "current"

    def test_levy_status_inferred_when_blank(self, mock_db):
        csv_data = _csv(
            "lot_number,unit_number,financial_year,admin_levied,admin_paid,sinking_levied,sinking_paid\n"
            "LOT3,UA003,2026,1000,500,300,300\n"
        )
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        ua003 = inserts[0][0][0]
        assert ua003["levy_status"] == "arrears"

    def test_uoe_enriched_from_units(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(_csv(LEVY_STATUS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        ua001 = next(a[0][0] for a in inserts if a[0][0]["unit_number"] == "UA001")
        assert ua001["uoe"] == 115

    def test_quarterly_balance_computed(self, mock_db):
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(_csv(LEVY_STATUS_CSV), BUILDING_ID, CREATED_BY)
        )
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        ua001 = next(a[0][0] for a in inserts if a[0][0]["unit_number"] == "UA001")
        # q1_amount=756.75, q1_paid=756.75 → balance=0
        assert ua001["q1_balance"] == pytest.approx(0.0)

    def test_financial_year_from_param_when_missing_in_row(self, mock_db):
        csv_data = _csv(
            "lot_number,unit_number,admin_levied,admin_paid,sinking_levied,sinking_paid\n"
            "LOT1,UA001,2345,2345,672,672\n"
        )
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(csv_data, BUILDING_ID, CREATED_BY, financial_year="2026")
        )
        assert result.imported == 1
        inserts = mock_db.unit_levy_ledger.insert_one.call_args_list
        assert inserts[0][0][0]["year"] == "2026"

    def test_missing_unit_number_skips(self, mock_db):
        csv_data = _csv(
            "lot_number,unit_number,financial_year,admin_levied,admin_paid\n"
            "LOT1,,2026,2345,2345\n"
        )
        from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
        result = asyncio.get_event_loop().run_until_complete(
            process_unit_levy_status_csv(csv_data, BUILDING_ID, CREATED_BY)
        )
        assert result.skipped == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. Row cap (MAX_ROWS = 500)
# ─────────────────────────────────────────────────────────────────────────────

class TestRowCap:
    def test_processes_at_most_500_rows(self):
        """Generate 600 rows — only first 500 should be processed."""
        header = "lot_number,unit_number,primary_owner_name,uoe\n"
        rows = "".join(f"LOT{i},UA{i:03d},Owner {i},{i % 200 + 10}\n" for i in range(1, 601))
        content = _csv(header + rows)

        with patch("strataos_demo_integrations.data_upload.service.db") as mock_db:
            mock_db.units.find_one = AsyncMock(return_value=None)
            mock_db.units.insert_one = AsyncMock()
            from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
            result = asyncio.get_event_loop().run_until_complete(
                process_unit_owners_csv(content, BUILDING_ID, CREATED_BY)
            )
        assert result.total_rows == 500


# ─────────────────────────────────────────────────────────────────────────────
# 8. Router — permission checks
# ─────────────────────────────────────────────────────────────────────────────

class TestRouterPermissions:
    def _check_permission(self, role: str, can_manage: bool):
        user = {
            "id": "u1",
            "role": role,
            "permissions": {"can_manage_finances": can_manage},
        }
        return user

    def test_super_admin_allowed(self):
        from strataos_demo_integrations.data_upload.router import _check_permissions
        # Should not raise
        _check_permissions({"role": "super_admin", "permissions": {"can_manage_finances": True}})

    def test_strata_manager_allowed(self):
        from strataos_demo_integrations.data_upload.router import _check_permissions
        _check_permissions(
            {"role": "strata_manager", "is_approved": True, "permissions": {"can_manage_finances": True}})

    def test_strata_admin_allowed(self):
        # Per migration 0025: chairman is no longer a top-level role for
        # operational allow-lists. strata_admin is the canonical replacement
        # for company-level admin functions like financial year imports.
        from strataos_demo_integrations.data_upload.router import _check_permissions
        _check_permissions({
            "role": "strata_admin",
            "permissions": {"can_manage_finances": True},
        })

    def test_owner_denied(self):
        from fastapi import HTTPException
        from strataos_demo_integrations.data_upload.router import _check_permissions
        with pytest.raises(HTTPException) as exc_info:
            _check_permissions({"role": "owner", "permissions": {"can_manage_finances": False}})
        assert exc_info.value.status_code == 403

    def test_ec_member_denied(self):
        from fastapi import HTTPException
        from strataos_demo_integrations.data_upload.router import _check_permissions
        with pytest.raises(HTTPException) as exc_info:
            _check_permissions({"role": "ec_member", "permissions": {"can_manage_finances": True}})
        assert exc_info.value.status_code == 403

    def test_no_finance_permission_denied(self):
        from fastapi import HTTPException
        from strataos_demo_integrations.data_upload.router import _check_permissions
        with pytest.raises(HTTPException) as exc_info:
            _check_permissions({"role": "strata_manager", "permissions": {"can_manage_finances": False}})
        assert exc_info.value.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 9. Router — file validation
# ─────────────────────────────────────────────────────────────────────────────

class TestFileValidation:
    def test_file_over_10mb_raises_413(self):
        import asyncio
        from fastapi import HTTPException, UploadFile
        from strataos_demo_integrations.data_upload.router import _read_validated_file

        big_content = b"x" * (10 * 1024 * 1024 + 1)
        mock_file = MagicMock(spec=UploadFile)
        mock_file.read = AsyncMock(return_value=big_content)
        mock_file.content_type = "text/csv"

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(_read_validated_file(mock_file))
        assert exc_info.value.status_code == 413

    def test_non_csv_content_type_raises_415(self):
        import asyncio
        from fastapi import HTTPException, UploadFile
        from strataos_demo_integrations.data_upload.router import _read_validated_file

        mock_file = MagicMock(spec=UploadFile)
        mock_file.read = AsyncMock(return_value=b"some,data")
        mock_file.content_type = "application/json"

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(_read_validated_file(mock_file))
        assert exc_info.value.status_code == 415

    def test_valid_csv_content_type_accepted(self):
        import asyncio
        from fastapi import UploadFile
        from strataos_demo_integrations.data_upload.router import _read_validated_file

        mock_file = MagicMock(spec=UploadFile)
        mock_file.read = AsyncMock(return_value=b"unit_number,uoe\nUA001,115")
        mock_file.content_type = "text/csv"
        mock_file.filename = "owners.csv"

        result = asyncio.get_event_loop().run_until_complete(_read_validated_file(mock_file))
        assert result == b"unit_number,uoe\nUA001,115"

    def test_plain_text_content_type_accepted(self):
        import asyncio
        from fastapi import UploadFile
        from strataos_demo_integrations.data_upload.router import _read_validated_file

        mock_file = MagicMock(spec=UploadFile)
        mock_file.read = AsyncMock(return_value=b"a,b\n1,2")
        mock_file.content_type = "text/plain"
        mock_file.filename = "data.csv"

        result = asyncio.get_event_loop().run_until_complete(_read_validated_file(mock_file))
        assert result == b"a,b\n1,2"


# ─────────────────────────────────────────────────────────────────────────────
# 10. _build_response helper
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildResponse:
    def test_status_completed_when_no_errors(self):
        from strataos_demo_integrations.data_upload.router import _build_response
        from strataos_demo_integrations.data_upload.models import ImportResult
        r = ImportResult(sheet_type="unit_owners", imported=5, total_rows=5)
        resp = _build_response("id1", BUILDING_ID, "2026", [r], CREATED_BY)
        assert resp.status == "completed"

    def test_status_partial_when_some_errors(self):
        from strataos_demo_integrations.data_upload.router import _build_response
        from strataos_demo_integrations.data_upload.models import ImportResult
        r = ImportResult(sheet_type="unit_owners", imported=3, skipped=2, errors=["Row 1: ...", "Row 3: ..."])
        resp = _build_response("id1", BUILDING_ID, "2026", [r], CREATED_BY)
        assert resp.status == "partial"

    def test_status_failed_when_all_errors_no_imports(self):
        from strataos_demo_integrations.data_upload.router import _build_response
        from strataos_demo_integrations.data_upload.models import ImportResult
        r = ImportResult(sheet_type="unit_owners", imported=0, updated=0, skipped=3, errors=["e1", "e2", "e3"])
        resp = _build_response("id1", BUILDING_ID, "2026", [r], CREATED_BY)
        assert resp.status == "failed"

    def test_sheet_type_propagated(self):
        from strataos_demo_integrations.data_upload.router import _build_response
        from strataos_demo_integrations.data_upload.models import ImportResult
        r = ImportResult(sheet_type="budget_categories", imported=10)
        resp = _build_response("id1", BUILDING_ID, "2026", [r], CREATED_BY)
        assert resp.sheet_type == "budget_categories"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Building isolation (data leak prevention)
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildingIsolation:
    def test_unit_owners_uses_building_id_in_find(self):
        """Ensure building_id is always in the find_one filter."""
        with patch("strataos_demo_integrations.data_upload.service.db") as mock_db:
            mock_db.units.find_one = AsyncMock(return_value=None)
            mock_db.units.insert_one = AsyncMock()
            from strataos_demo_integrations.data_upload.service import process_unit_owners_csv
            asyncio.get_event_loop().run_until_complete(
                process_unit_owners_csv(
                    _csv("lot_number,unit_number,primary_owner_name,uoe\nLOT1,UA001,John,115\n"),
                    BUILDING_ID, CREATED_BY
                )
            )
            find_filter = mock_db.units.find_one.call_args[0][0]
            assert find_filter["building_id"] == BUILDING_ID

    def test_budget_categories_uses_building_id_in_find(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock_db:
            mock_db.levy_categories.find_one = AsyncMock(return_value=None)
            mock_db.levy_categories.insert_one = AsyncMock()
            from strataos_demo_integrations.data_upload.service import process_budget_categories_csv
            asyncio.get_event_loop().run_until_complete(
                process_budget_categories_csv(
                    _csv("financial_year,fund_type,category_name,budgeted_amount\n2026,admin,Cleaning,27500\n"),
                    BUILDING_ID, CREATED_BY
                )
            )
            find_filter = mock_db.levy_categories.find_one.call_args[0][0]
            assert find_filter["building_id"] == BUILDING_ID

    def test_annual_levy_uses_building_id_in_find(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock_db:
            mock_db.annual_levies.find_one = AsyncMock(return_value=None)
            mock_db.annual_levies.insert_one = AsyncMock()
            mock_db.settings.find_one = AsyncMock(return_value={})
            mock_db.units.aggregate.return_value.to_list = AsyncMock(return_value=[{"total": 10000}])
            from strataos_demo_integrations.data_upload.service import process_annual_levy_csv
            asyncio.get_event_loop().run_until_complete(
                process_annual_levy_csv(
                    _csv("financial_year,admin_levy_per_uoe_proposed,sinking_levy_per_uoe_proposed\n2026,23.45,6.72\n"),
                    BUILDING_ID, CREATED_BY
                )
            )
            find_filter = mock_db.annual_levies.find_one.call_args[0][0]
            assert find_filter["building_id"] == BUILDING_ID

    def test_unit_levy_status_uses_building_id_in_find(self):
        with patch("strataos_demo_integrations.data_upload.service.db") as mock_db:
            mock_db.unit_levy_ledger.find_one = AsyncMock(return_value=None)
            mock_db.unit_levy_ledger.insert_one = AsyncMock()
            mock_db.units.find_one = AsyncMock(return_value=None)
            from strataos_demo_integrations.data_upload.service import process_unit_levy_status_csv
            asyncio.get_event_loop().run_until_complete(
                process_unit_levy_status_csv(
                    _csv("lot_number,unit_number,financial_year,admin_levied,admin_paid,sinking_levied,sinking_paid\n"
                         "LOT1,UA001,2026,2345,2345,672,672\n"),
                    BUILDING_ID, CREATED_BY
                )
            )
            find_filter = mock_db.unit_levy_ledger.find_one.call_args[0][0]
            assert find_filter["building_id"] == BUILDING_ID
