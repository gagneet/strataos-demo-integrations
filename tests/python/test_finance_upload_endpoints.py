"""
Tests for the two new finance upload endpoints:
  POST /finance/upload-budget-actuals  — rich combined CSV import
  POST /finance/fund-balances          — fund balance snapshot

Run with:
    backend/venv/bin/pytest tests/backend/test_finance_upload_endpoints.py -v
"""
import io
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../backend"))

# ─── Fixtures ────────────────────────────────────────────────────────────────

VALID_CSV = """\
year,category_id,category_name,planned,actual,variance,previous_actual
2026,101,Accountant - Professional Fees,1182.00,0.00,1182.00,1168.84
2026,110,Cleaning,27500.00,4520.73,22979.27,27738.00
2026,123,Insurance Premiums,37500.00,6691.73,30808.27,6746.27
2026,108,CCTV System,1455.00,1480.00,-25.00,1440.00
2026,127,Lift Repairs,0.00,2560.00,-2560.00,0.00
"""

MISSING_COLS_CSV = """\
category_name,planned
Cleaning,27500
"""

PARTIAL_CSV = """\
year,category_id,category_name,planned,actual,variance,previous_actual
2026,101,Accountant - Professional Fees,1182.00,0.00,1182.00,1168.84
,110,Cleaning,27500.00,4520.73,22979.27,27738.00
"""

BUDGET_CATEGORIES_HEADER_CSV = """\
year,fund_type,name,budgeted_amount,actual_amount,description
2026,administrative,Insurance,85000,84500,Building and public liability
2026,sinking,Lift Replacement,45000,,Scheduled for Q3
"""

ACTUALS_HEADER_CSV = """\
year,fund_type,name,actual_amount
2026,administrative,Insurance,84500
2026,sinking,Lift Replacement,44800
"""

UNIT_LEDGER_HEADER_CSV = """\
year,lot_number,unit_number,uoe,property_type,admin_opening,admin_levied,admin_paid,admin_closing,sinking_opening,sinking_levied,sinking_paid,sinking_closing,total_levied,total_paid,net_balance
2026,LOT1,UA001,115,apartment,0,365.14,365.14,0,0,103.23,103.23,0,468.37,468.37,0
"""


def _make_upload_file(content: str, filename: str = "test.csv"):
    """Create a mock UploadFile."""
    from fastapi import UploadFile
    file_obj = io.BytesIO(content.encode("utf-8"))
    return UploadFile(filename=filename, file=file_obj)


def _admin_user():
    return {
        "id": str(uuid.uuid4()),
        "email": "admin@eastgate.com",
        "role": "super_admin",
        "permissions": {
            "can_manage_finances": True,
        },
    }


def _strata_manager():
    return {
        "id": str(uuid.uuid4()),
        "email": "manager@eastgate.com",
        "role": "strata_manager",
        "permissions": {
            "can_manage_finances": True,
        },
    }


def _owner_user():
    return {
        "id": str(uuid.uuid4()),
        "email": "owner@eastgate.com",
        "role": "owner",
        "permissions": {
            "can_manage_finances": False,
        },
    }


# ─── parse_rich_csv logic tests (pure Python, no DB) ─────────────────────────

class TestRichCsvParsing:
    """Tests for client-side CSV parsing logic mirrored in backend."""

    def _parse_rows(self, text):
        """Replicate the server-side row parsing for unit testing."""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        header = [h.strip().lower() for h in lines[0].split(",")]

        def get(parts, col):
            idx = header.index(col) if col in header else -1
            return parts[idx].strip().replace("$", "").replace(",", "") if idx >= 0 else ""

        rows = []
        for line in lines[1:]:
            parts = line.split(",")
            year = get(parts, "year")
            name = get(parts, "category_name")
            if not year or not name:
                continue
            planned = float(get(parts, "planned") or 0)
            actual = float(get(parts, "actual") or 0)
            rows.append({"year": year, "name": name, "planned": planned, "actual": actual})
        return rows

    def test_parses_5_rows(self):
        rows = self._parse_rows(VALID_CSV)
        assert len(rows) == 5

    def test_over_budget_detected(self):
        rows = self._parse_rows(VALID_CSV)
        cctv = next(r for r in rows if r["name"] == "CCTV System")
        assert cctv["actual"] > cctv["planned"]  # 1480 > 1455

    def test_zero_budget_lift_repairs(self):
        rows = self._parse_rows(VALID_CSV)
        lift = next(r for r in rows if r["name"] == "Lift Repairs")
        assert lift["planned"] == 0.0
        assert lift["actual"] == 2560.0

    def test_amounts_parsed_correctly(self):
        rows = self._parse_rows(VALID_CSV)
        cleaning = next(r for r in rows if r["name"] == "Cleaning")
        assert cleaning["planned"] == 27500.0
        assert cleaning["actual"] == 4520.73


# ─── upload_budget_actuals endpoint tests ────────────────────────────────────

class TestUploadBudgetActualsEndpoint:
    """Tests for POST /finance/upload-budget-actuals."""

    @pytest.mark.asyncio
    async def test_success_upserts_rows(self):
        from routers.finance import upload_budget_actuals

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=None)
        mock_db.levy_categories.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.find_one = AsyncMock(return_value=None)
        mock_db.annual_levies.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        upload_file = _make_upload_file(VALID_CSV)
        upload_file.read = AsyncMock(return_value=VALID_CSV.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            result = await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert result["imported"] == 5
        assert result["skipped"] == 0
        assert mock_db.levy_categories.insert_one.call_count == 5

    @pytest.mark.asyncio
    async def test_existing_record_updated_not_inserted(self):
        from routers.finance import upload_budget_actuals

        existing_doc = {"id": str(uuid.uuid4()), "name": "Cleaning"}
        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=existing_doc)
        mock_db.levy_categories.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        mock_db.levy_categories.insert_one = AsyncMock()
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.find_one = AsyncMock(return_value=None)
        mock_db.annual_levies.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        mock_perms = MagicMock()
        mock_perms.can_manage_finances = True

        upload_file = _make_upload_file(VALID_CSV)
        upload_file.read = AsyncMock(return_value=VALID_CSV.encode("utf-8"))

        with patch("routers.finance.db", mock_db), \
                patch("routers.finance.get_user_permissions", return_value=mock_perms):
            result = await upload_budget_actuals(
                file=upload_file,
                current_user=_strata_manager(),
                building_id="13195",
            )

        assert result["imported"] == 5
        # All 5 found as existing → update_one called 5 times, insert_one never
        assert mock_db.levy_categories.update_one.call_count == 5
        mock_db.levy_categories.insert_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_required_columns_raises_400(self):
        from fastapi import HTTPException
        from routers.finance import upload_budget_actuals

        upload_file = _make_upload_file(MISSING_COLS_CSV)
        upload_file.read = AsyncMock(return_value=MISSING_COLS_CSV.encode("utf-8"))

        with pytest.raises(HTTPException) as exc_info:
            await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )
        assert exc_info.value.status_code == 400
        assert "Missing required columns" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_owner_role_forbidden(self):
        from fastapi import HTTPException
        from routers.finance import upload_budget_actuals

        upload_file = _make_upload_file(VALID_CSV)
        upload_file.read = AsyncMock(return_value=VALID_CSV.encode("utf-8"))

        with pytest.raises(HTTPException) as exc_info:
            await upload_budget_actuals(
                file=upload_file,
                current_user=_owner_user(),
                building_id="13195",
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_partial_csv_skips_bad_rows(self):
        from routers.finance import upload_budget_actuals

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=None)
        mock_db.levy_categories.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.find_one = AsyncMock(return_value=None)
        mock_db.annual_levies.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        upload_file = _make_upload_file(PARTIAL_CSV)
        upload_file.read = AsyncMock(return_value=PARTIAL_CSV.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            result = await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        # Row 1 valid, row 2 has empty year → skipped
        assert result["imported"] == 1
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_category_id_stored_as_int(self):
        from routers.finance import upload_budget_actuals

        inserted_docs = []

        async def capture_insert(doc):
            inserted_docs.append(doc)
            return MagicMock()

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=None)
        mock_db.levy_categories.insert_one = AsyncMock(side_effect=capture_insert)
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.find_one = AsyncMock(return_value=None)
        mock_db.annual_levies.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        single_row = "year,category_id,category_name,planned,actual,variance,previous_actual\n2026,101,Cleaning,27500,4520,22980,27738\n"
        upload_file = _make_upload_file(single_row)
        upload_file.read = AsyncMock(return_value=single_row.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert len(inserted_docs) == 1
        assert inserted_docs[0]["category_id"] == 101
        assert isinstance(inserted_docs[0]["category_id"], int)

    @pytest.mark.asyncio
    async def test_sinking_fund_inferred_from_category_id_200(self):
        from routers.finance import upload_budget_actuals

        inserted_docs = []

        async def capture_insert(doc):
            inserted_docs.append(doc)
            return MagicMock()

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=None)
        mock_db.levy_categories.insert_one = AsyncMock(side_effect=capture_insert)
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.find_one = AsyncMock(return_value=None)
        mock_db.annual_levies.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        sinking_row = "year,category_id,category_name,planned,actual,variance,previous_actual\n2026,201,Roof Works,55000,22272,32728,50000\n"
        upload_file = _make_upload_file(sinking_row)
        upload_file.read = AsyncMock(return_value=sinking_row.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert inserted_docs[0]["fund_type"] == "sinking"

    @pytest.mark.asyncio
    async def test_over_budget_status_set(self):
        from routers.finance import upload_budget_actuals

        inserted_docs = []

        async def capture_insert(doc):
            inserted_docs.append(doc)
            return MagicMock()

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=None)
        mock_db.levy_categories.insert_one = AsyncMock(side_effect=capture_insert)
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.find_one = AsyncMock(return_value=None)
        mock_db.annual_levies.insert_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        over_row = "year,category_id,category_name,planned,actual,variance,previous_actual\n2026,108,CCTV System,1455.00,1480.00,-25.00,1440.00\n"
        upload_file = _make_upload_file(over_row)
        upload_file.read = AsyncMock(return_value=over_row.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert inserted_docs[0]["status"] == "over_budget"

    @pytest.mark.asyncio
    async def test_empty_file_raises_400(self):
        from fastapi import HTTPException
        from routers.finance import upload_budget_actuals

        upload_file = _make_upload_file("")
        upload_file.read = AsyncMock(return_value=b"")

        with pytest.raises(HTTPException) as exc_info:
            await upload_budget_actuals(
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )
        assert exc_info.value.status_code == 400


class TestLegacyFinanceUploadEndpoints:
    @pytest.mark.asyncio
    async def test_budget_categories_accepts_header_csv_without_form_year(self):
        from routers.finance import upload_budget_categories

        inserted_docs = []

        async def capture_insert(doc):
            inserted_docs.append(doc)
            return MagicMock()

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.find_one = AsyncMock(return_value=None)
        mock_db.levy_categories.insert_one = AsyncMock(side_effect=capture_insert)

        upload_file = _make_upload_file(BUDGET_CATEGORIES_HEADER_CSV)
        upload_file.read = AsyncMock(return_value=BUDGET_CATEGORIES_HEADER_CSV.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            result = await upload_budget_categories(
                year=None,
                fund_type=None,
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert result["inserted"] == 2
        assert inserted_docs[0]["year"] == "2026"
        assert inserted_docs[0]["fund_type"] == "administrative"
        assert inserted_docs[0]["actual_amount"] == pytest.approx(84500.0)
        assert inserted_docs[1]["fund_type"] == "sinking"

    @pytest.mark.asyncio
    async def test_actuals_accept_header_csv_without_form_year(self):
        from routers.finance import upload_actual_expenses

        update_calls = []

        async def capture_update(filter_q, update_q):
            update_calls.append((filter_q, update_q))
            return MagicMock(matched_count=1)

        mock_db = MagicMock()
        mock_db.levy_categories = MagicMock()
        mock_db.levy_categories.update_one = AsyncMock(side_effect=capture_update)

        upload_file = _make_upload_file(ACTUALS_HEADER_CSV)
        upload_file.read = AsyncMock(return_value=ACTUALS_HEADER_CSV.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            result = await upload_actual_expenses(
                year=None,
                fund_type=None,
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert result["updated"] == 2
        assert update_calls[0][0]["year"] == "2026"
        assert update_calls[0][0]["fund_type"] == "administrative"
        assert update_calls[1][0]["fund_type"] == "sinking"
        assert update_calls[1][1]["$set"]["actual_amount"] == pytest.approx(44800.0)

    @pytest.mark.asyncio
    async def test_unit_ledger_accepts_header_csv_with_year_column(self):
        from routers.finance import upload_unit_ledger

        replace_calls = []

        async def capture_replace(filter_q, doc, upsert=False):
            replace_calls.append((filter_q, doc, upsert))
            return MagicMock()

        units_cursor = MagicMock()
        units_cursor.to_list = AsyncMock(return_value=[{
            "lot_number": "LOT1",
            "unit_number": "UA001",
            "unit_entitlement": 115,
            "entitlement": 1.15,
            "unit_type": "apartment",
        }])

        mock_db = MagicMock()
        mock_db.units = MagicMock()
        mock_db.units.find.return_value = units_cursor
        mock_db.unit_levy_ledger = MagicMock()
        mock_db.unit_levy_ledger.replace_one = AsyncMock(side_effect=capture_replace)

        upload_file = _make_upload_file(UNIT_LEDGER_HEADER_CSV)
        upload_file.read = AsyncMock(return_value=UNIT_LEDGER_HEADER_CSV.encode("utf-8"))

        with patch("routers.finance.db", mock_db):
            result = await upload_unit_ledger(
                year=None,
                file=upload_file,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert result["inserted"] == 1
        _, doc, upsert = replace_calls[0]
        assert upsert is True
        assert doc["year"] == "2026"
        assert doc["uoe"] == pytest.approx(115)
        assert doc["unit_number"] == "UA001"


# ─── update_fund_balances endpoint tests ─────────────────────────────────────

class TestUpdateFundBalancesEndpoint:
    """Tests for POST /finance/fund-balances."""

    @pytest.mark.asyncio
    async def test_success_creates_reconciliation_records(self):
        from routers.finance import update_fund_balances

        mock_db = MagicMock()
        mock_db.bank_reconciliations = MagicMock()
        mock_db.bank_reconciliations.replace_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        mock_perms = MagicMock()
        mock_perms.can_manage_finances = True

        with patch("routers.finance.db", mock_db), \
                patch("routers.finance.get_user_permissions", return_value=mock_perms):
            result = await update_fund_balances(
                admin_balance=9187.44,
                sinking_balance=193337.03,
                as_of_date="2026-03-15",
                financial_year="2026-2027",
                notes="March 2026 reconciliation",
                current_user=_strata_manager(),
                building_id="13195",
            )

        assert result["admin_balance"] == 9187.44
        assert result["sinking_balance"] == 193337.03
        assert abs(result["total"] - 202524.47) < 0.01
        assert result["as_of_date"] == "2026-03-15"
        # Two replace_one calls — one per fund type
        assert mock_db.bank_reconciliations.replace_one.call_count == 2

    @pytest.mark.asyncio
    async def test_annual_levies_updated(self):
        from routers.finance import update_fund_balances

        update_calls = []

        async def capture_update(filter_q, update_q, upsert=False):
            update_calls.append((filter_q, update_q))
            return MagicMock(matched_count=1)

        mock_db = MagicMock()
        mock_db.bank_reconciliations = MagicMock()
        mock_db.bank_reconciliations.replace_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.update_one = AsyncMock(side_effect=capture_update)

        with patch("routers.finance.db", mock_db):
            await update_fund_balances(
                admin_balance=9187.44,
                sinking_balance=193337.03,
                as_of_date="2026-03-15",
                financial_year="2026-2027",
                notes=None,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert len(update_calls) == 1
        _, update_doc = update_calls[0]
        assert update_doc["$set"]["admin_fund.current_balance"] == 9187.44
        assert update_doc["$set"]["sinking_fund.current_balance"] == 193337.03

    @pytest.mark.asyncio
    async def test_owner_role_forbidden(self):
        from fastapi import HTTPException
        from routers.finance import update_fund_balances

        with pytest.raises(HTTPException) as exc_info:
            await update_fund_balances(
                admin_balance=9187.44,
                sinking_balance=193337.03,
                as_of_date="2026-03-15",
                financial_year="2026-2027",
                notes=None,
                current_user=_owner_user(),
                building_id="13195",
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_total_calculation_correct(self):
        from routers.finance import update_fund_balances

        mock_db = MagicMock()
        mock_db.bank_reconciliations = MagicMock()
        mock_db.bank_reconciliations.replace_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.update_one = AsyncMock(return_value=MagicMock())

        with patch("routers.finance.db", mock_db):
            result = await update_fund_balances(
                admin_balance=9187.44,
                sinking_balance=193337.03,
                as_of_date="2026-03-15",
                financial_year="2026-2027",
                notes=None,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert result["total"] == round(9187.44 + 193337.03, 2)

    @pytest.mark.asyncio
    async def test_year_extracted_for_annual_levies_query(self):
        """financial_year '2026-2027' should query annual_levies with year='2026'."""
        from routers.finance import update_fund_balances

        filter_queries = []

        async def capture_update(filter_q, update_q, upsert=False):
            filter_queries.append(filter_q)
            return MagicMock(matched_count=1)

        mock_db = MagicMock()
        mock_db.bank_reconciliations = MagicMock()
        mock_db.bank_reconciliations.replace_one = AsyncMock(return_value=MagicMock())
        mock_db.annual_levies = MagicMock()
        mock_db.annual_levies.update_one = AsyncMock(side_effect=capture_update)

        with patch("routers.finance.db", mock_db):
            await update_fund_balances(
                admin_balance=1000.0,
                sinking_balance=2000.0,
                as_of_date="2026-03-15",
                financial_year="2026-2027",
                notes=None,
                current_user=_admin_user(),
                building_id="13195",
            )

        assert filter_queries[0]["year"] == "2026"
