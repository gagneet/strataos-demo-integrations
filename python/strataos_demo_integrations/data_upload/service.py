# @featuretrace:levy — CSV row processors for financial_import.py: parses + upserts levy/ledger collections.
# Layer: service
# Data flow: financial_import.py POST /financial-import/* -> process_*_csv() -> units / annual_levies /
#            levy_categories / unit_levy_ledger (building-scoped).
# Related: backend/routers/financial_import.py
#           backend/routers/finance.py (_upsert_ledger_for_payment — parallel writer of unit_levy_ledger)
# Collection: units, annual_levies, levy_categories, unit_levy_ledger
import csv
import io
import uuid
from datetime import datetime, timezone

from typing import List, Tuple

from database import db
from services.settings_service import get_general_settings_or_default
from utils.finance_helpers import get_levy_rate_breakdown

_MAX_ROWS = 500


def _year_int_or_none(year: str):
    try:
        return int(str(year)[:4])
    except (TypeError, ValueError):
        return None


async def _current_levy_year(building_id: str, settings_doc: dict | None = None) -> int:
    """The calendar year that is CURRENT for this building's own levy cycle today.

    Mirrors routers.finance._resolve_current_levy_year's formula deliberately
    without importing that function: it calls services.settings_service.
    get_general_settings without skip_pg=True, i.e. it tries a PostgreSQL read
    first. This module only ever writes to legacy Mongo collections
    (annual_levies, levy_categories, unit_levy_ledger) — no other code path
    here touches Postgres — so pulling in a PG dependency for a pure
    year-validation check would be an unrelated, untested regression (and
    would break every existing pure-Mongo-mock test in this suite). skip_pg=True
    keeps this Mongo-only, consistent with the rest of the file.
    """
    if settings_doc is None:
        settings_doc = await get_general_settings_or_default(
            building_id, {"_id": 0, "financial_year_start_month": 1}, settings_db=db, skip_pg=True,
        )
    fy_start_month = int((settings_doc or {}).get("financial_year_start_month") or 1)
    now = datetime.now(timezone.utc)
    return now.year if (fy_start_month <= 1 or now.month >= fy_start_month) else now.year - 1


def _parse_float(val: str) -> float:
    """Parse float from string, stripping $, commas, spaces."""
    if not val:
        return 0.0
    try:
        return float(str(val).strip().replace("$", "").replace(",", "").replace(" ", "") or "0")
    except (ValueError, TypeError):
        return 0.0


def _decode_csv(content: bytes) -> str:
    """Decode CSV bytes, handling BOM and latin-1 fallback."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _csv_rows(content: bytes) -> Tuple[List[str], List[dict]]:
    """Return (headers, rows) from CSV bytes using csv.DictReader."""
    text = _decode_csv(content)
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = list(reader)
    return headers, rows


async def process_unit_owners_csv(content: bytes, building_id: str, created_by: str):
    """Process owner/unit details CSV. Upserts units collection."""
    from strataos_demo_integrations.data_upload.models import ImportResult
    result = ImportResult(sheet_type="unit_owners")

    try:
        headers, rows = _csv_rows(content)
    except Exception as e:
        result.errors.append(f"CSV parse error: {str(e)}")
        return result

    rows = rows[:_MAX_ROWS]
    result.total_rows = len(rows)
    now = datetime.now(timezone.utc).isoformat()

    for i, row in enumerate(rows, 1):
        try:
            row = {k.strip(): v.strip() for k, v in row.items() if k}

            lot_number = row.get("lot_number", "").strip()
            unit_number = row.get("unit_number", "").strip()
            if not unit_number:
                result.errors.append(f"Row {i}: missing unit_number")
                result.skipped += 1
                continue

            uoe_raw = row.get("uoe", row.get("unit_entitlement", "0"))
            uoe = int(_parse_float(uoe_raw))

            update_fields = {
                "lot_number": lot_number,
                "unit_number": unit_number,
                "unit_type": row.get("unit_type", "apartment").strip().lower(),
                "mixed_use_type": row.get("mixed_use_type", "").strip() or None,
                "owner_name": row.get("primary_owner_name", row.get("owner_name", "")).strip(),
                "owner_name_b": row.get("secondary_owner_name", row.get("owner_name_b", "")).strip() or None,
                "owner_email": row.get("owner_email", "").strip() or None,
                "unit_entitlement": uoe,
                "entitlement": uoe,
                "asset_value": _parse_float(row.get("asset_value", "0")) or None,
                "status": row.get("status", "owner_occupied").strip(),
                "notes": row.get("notes", "").strip() or None,
                "updated_at": now,
                "building_id": building_id,
            }

            existing = await db.units.find_one(
                {"building_id": building_id, "unit_number": unit_number}, {"_id": 0, "id": 1}
            )

            if existing:
                await db.units.update_one(
                    {"building_id": building_id, "unit_number": unit_number},
                    {"$set": update_fields}
                )
                result.updated += 1
            else:
                update_fields["id"] = str(uuid.uuid4())
                update_fields["created_at"] = now
                await db.units.insert_one(update_fields)
                result.imported += 1

        except Exception as e:
            result.errors.append(f"Row {i}: {str(e)}")
            result.skipped += 1

    return result


async def process_annual_levy_csv(content: bytes, building_id: str, created_by: str):
    """Process annual levy summary CSV. Upserts annual_levies collection."""
    from strataos_demo_integrations.data_upload.models import ImportResult
    result = ImportResult(sheet_type="annual_levy")

    try:
        headers, rows = _csv_rows(content)
    except Exception as e:
        result.errors.append(f"CSV parse error: {str(e)}")
        return result

    rows = rows[:_MAX_ROWS]
    result.total_rows = len(rows)
    now = datetime.now(timezone.utc).isoformat()
    settings_doc = await get_general_settings_or_default(building_id, {"_id": 0}, settings_db=db)
    # East Gate 13195 investigation (2026-07-22): this endpoint had zero year
    # validation at all — the confirmed root cause of a phantom FY2027
    # annual_levies/unit_levy_ledger/levy_categories batch. A future levy year is
    # legitimate for a proposed/draft budget (e.g. AGM-adopted next-year budget)
    # but never for status="actual" — resolved once per upload, not per row.
    current_levy_year = await _current_levy_year(building_id, settings_doc=settings_doc)

    for i, row in enumerate(rows, 1):
        try:
            row = {k.strip(): v.strip() for k, v in row.items() if k}
            year = row.get("financial_year", "").strip()
            if not year:
                result.errors.append(f"Row {i}: missing financial_year")
                result.skipped += 1
                continue

            def fv(key, default=0.0):
                return _parse_float(row.get(key, str(default)))

            def fv_opt(key):
                val = row.get(key, "").strip()
                return _parse_float(val) if val else None

            admin_levy_proposed = fv("admin_levy_per_uoe_proposed")
            admin_levy_actual = fv_opt("admin_levy_per_uoe_actual")
            sinking_levy_proposed = fv("sinking_levy_per_uoe_proposed")
            sinking_levy_actual = fv_opt("sinking_levy_per_uoe_actual")

            admin_income_proposed = fv("admin_total_income_proposed")
            admin_income_actual = fv_opt("admin_total_income_actual")
            admin_exp_proposed = fv("admin_total_expenses_proposed")
            admin_exp_actual = fv_opt("admin_total_expenses_actual")
            admin_opening = fv("admin_opening_balance")
            admin_closing_proj_raw = row.get("admin_closing_balance_projected", "").strip()
            admin_closing_proj = fv("admin_closing_balance_projected")
            admin_closing_actual_raw = row.get("admin_closing_balance_actual", "").strip()
            admin_closing_actual = fv_opt("admin_closing_balance_actual")

            sinking_income_proposed = fv("sinking_total_income_proposed")
            sinking_income_actual = fv_opt("sinking_total_income_actual")
            sinking_exp_proposed = fv("sinking_total_expenses_proposed")
            sinking_exp_actual = fv_opt("sinking_total_expenses_actual")
            sinking_opening = fv("sinking_opening_balance")
            sinking_closing_proj_raw = row.get("sinking_closing_balance_projected", "").strip()
            sinking_closing_proj = fv("sinking_closing_balance_projected")
            sinking_closing_actual_raw = row.get("sinking_closing_balance_actual", "").strip()
            sinking_closing_actual = fv_opt("sinking_closing_balance_actual")

            # Only persist current_balance when the CSV explicitly supplies a closing balance field.
            # Blank columns should not materialize as 0.0 because that prevents the runtime fallback
            # from using legacy closing_balance fields on older annual_levies documents.
            admin_current_balance = None
            if admin_closing_actual_raw:
                admin_current_balance = admin_closing_actual
            elif admin_closing_proj_raw:
                admin_current_balance = admin_closing_proj

            sinking_current_balance = None
            if sinking_closing_actual_raw:
                sinking_current_balance = sinking_closing_actual
            elif sinking_closing_proj_raw:
                sinking_current_balance = sinking_closing_proj

            total_income_proposed = admin_income_proposed + sinking_income_proposed
            has_actuals = admin_levy_actual is not None or admin_exp_actual is not None
            status = "actual" if has_actuals else "proposed"

            year_int = _year_int_or_none(year)
            if year_int is not None and year_int > current_levy_year and status == "actual":
                result.errors.append(
                    f"Row {i}: financial_year={year} is beyond the building's current "
                    f"levy year ({current_levy_year}) with actual figures present — "
                    f"future years may only be imported as proposed/draft budgets"
                )
                result.skipped += 1
                continue

            # Single query: check if record exists AND retrieve total_uoe in one call.
            # Never hardcode total_uoe=10000 — Sierra=9, Harbourview=3.
            existing = await db.annual_levies.find_one(
                {"year": year, "building_id": building_id}, {"_id": 0, "id": 1, "total_uoe": 1}
            )
            if existing and existing.get("total_uoe"):
                total_uoe_val = existing["total_uoe"]
            else:
                uoe_agg = await db.units.aggregate([
                    {"$match": {"building_id": building_id}},
                    {
                        "$project": {
                            "effective_uoe": {
                                "$ifNull": ["$unit_entitlement", "$entitlement"]
                            }
                        }
                    },
                    {"$group": {"_id": None, "total": {"$sum": "$effective_uoe"}}},
                ]).to_list(1)
                total_uoe_val = (uoe_agg[0]["total"] if uoe_agg else 0) or 0
                if total_uoe_val <= 0:
                    result.errors.append(
                        f"Row {i}: could not derive total_uoe from units; import unit owners before annual levy data"
                    )
                    result.skipped += 1
                    continue

            compatibility_rates = get_levy_rate_breakdown(
                {
                    "year": year,
                    "building_id": building_id,
                    "total_uoe": total_uoe_val,
                    "proposed_admin_expenses": admin_income_proposed,
                    "proposed_sinking_expenses": sinking_income_proposed,
                    "admin_fund": {"levy_income": admin_income_proposed},
                    "sinking_fund": {"levy_income": sinking_income_proposed},
                },
                settings_doc=settings_doc,
            )

            levy_doc = {
                "year": year,
                "building_id": building_id,
                "status": status,
                "plan_id": building_id,
                "total_uoe": total_uoe_val,
                # Compatibility only: owner-payable per-UOE rates are always derived
                # from the canonical ex-GST fund totals plus the building GST settings.
                "admin_levy_per_uoe_annual": round(compatibility_rates["admin_payable_annual"], 4),
                "admin_levy_per_uoe_quarterly": round(compatibility_rates["admin_payable_quarterly"], 4),
                "sinking_levy_per_uoe_annual": round(compatibility_rates["sinking_payable_annual"], 4),
                "sinking_levy_per_uoe_quarterly": round(compatibility_rates["sinking_payable_quarterly"], 4),
                "total_income_proposed": total_income_proposed,
                "admin_fund": {
                    "levy_income": admin_income_proposed,
                    "levy_income_actual": admin_income_actual,
                    "total_income": admin_income_proposed,
                    "total_income_actual": admin_income_actual,
                    "total_expenses": admin_exp_proposed,
                    "total_expenses_actual": admin_exp_actual,
                    "opening_balance": admin_opening,
                    "closing_balance": admin_closing_proj,
                    "closing_balance_actual": admin_closing_actual,
                    "surplus_deficit": admin_closing_proj - admin_opening,
                },
                "sinking_fund": {
                    "levy_income": sinking_income_proposed,
                    "levy_income_actual": sinking_income_actual,
                    "total_income": sinking_income_proposed,
                    "total_income_actual": sinking_income_actual,
                    "total_expenses": sinking_exp_proposed,
                    "total_expenses_actual": sinking_exp_actual,
                    "opening_balance": sinking_opening,
                    "closing_balance": sinking_closing_proj,
                    "closing_balance_actual": sinking_closing_actual,
                    "surplus_deficit": sinking_closing_proj - sinking_opening,
                },
                "admin_levy_per_uoe_actual": admin_levy_actual,
                "sinking_levy_per_uoe_actual": sinking_levy_actual,
                "updated_at": now,
            }

            if existing:
                if admin_current_balance is not None:
                    levy_doc["admin_fund"]["current_balance"] = admin_current_balance
                if sinking_current_balance is not None:
                    levy_doc["sinking_fund"]["current_balance"] = sinking_current_balance
                await db.annual_levies.update_one(
                    {"year": year, "building_id": building_id},
                    {"$set": levy_doc}
                )
                result.updated += 1
            else:
                levy_doc["id"] = str(uuid.uuid4())
                levy_doc["created_at"] = now
                if admin_current_balance is not None:
                    levy_doc["admin_fund"]["current_balance"] = admin_current_balance
                if sinking_current_balance is not None:
                    levy_doc["sinking_fund"]["current_balance"] = sinking_current_balance
                await db.annual_levies.insert_one(levy_doc)
                result.imported += 1

        except Exception as e:
            result.errors.append(f"Row {i}: {str(e)}")
            result.skipped += 1

    return result


async def process_budget_categories_csv(content: bytes, building_id: str, created_by: str):
    """Process budget categories CSV. Upserts levy_categories collection."""
    from strataos_demo_integrations.data_upload.models import ImportResult
    result = ImportResult(sheet_type="budget_categories")

    try:
        headers, rows = _csv_rows(content)
    except Exception as e:
        result.errors.append(f"CSV parse error: {str(e)}")
        return result

    rows = rows[:_MAX_ROWS]
    result.total_rows = len(rows)
    now = datetime.now(timezone.utc).isoformat()
    # East Gate 13195 investigation (2026-07-22): see process_annual_levy_csv —
    # same missing-validation root cause, same fix. Resolved once per upload.
    current_levy_year = await _current_levy_year(building_id)

    for i, row in enumerate(rows, 1):
        try:
            row = {k.strip(): v.strip() for k, v in row.items() if k}
            year = row.get("financial_year", "").strip()
            category_name = row.get("category_name", "").strip()
            fund_type_raw = row.get("fund_type", "admin").strip().lower()

            if not year or not category_name:
                result.errors.append(f"Row {i}: missing financial_year or category_name")
                result.skipped += 1
                continue

            # Normalize: admin/administrative -> administrative; sinking stays sinking
            fund_type = "administrative" if fund_type_raw in ("admin", "administrative") else fund_type_raw

            budgeted = _parse_float(row.get("budgeted_amount", "0"))
            actual_raw = row.get("actual_amount", "").strip()
            actual = _parse_float(actual_raw) if actual_raw else None
            description = row.get("description", "").strip() or None
            status = "actual" if actual is not None else "proposed"

            year_int = _year_int_or_none(year)
            if year_int is not None and year_int > current_levy_year and status == "actual":
                result.errors.append(
                    f"Row {i}: financial_year={year} is beyond the building's current "
                    f"levy year ({current_levy_year}) with an actual_amount present — "
                    f"future years may only be imported as proposed budget categories"
                )
                result.skipped += 1
                continue

            cat_doc = {
                "year": year,
                "financial_year": year,
                "fund_type": fund_type,
                "name": category_name,
                "budgeted_amount": budgeted,
                "actual_amount": actual if actual is not None else 0.0,
                "description": description,
                "status": status,
                "building_id": building_id,
                "updated_at": now,
            }

            existing = await db.levy_categories.find_one(
                {"year": year, "building_id": building_id, "name": category_name, "fund_type": fund_type},
                {"_id": 0, "id": 1}
            )

            if existing:
                await db.levy_categories.update_one(
                    {"year": year, "building_id": building_id, "name": category_name, "fund_type": fund_type},
                    {"$set": cat_doc}
                )
                result.updated += 1
            else:
                cat_doc["id"] = str(uuid.uuid4())
                cat_doc["created_at"] = now
                await db.levy_categories.insert_one(cat_doc)
                result.imported += 1

        except Exception as e:
            result.errors.append(f"Row {i}: {str(e)}")
            result.skipped += 1

    return result


async def process_unit_levy_status_csv(
        content: bytes, building_id: str, created_by: str, financial_year: str = ""
):
    """Process per-unit levy status CSV. Upserts unit_levy_ledger collection."""
    from strataos_demo_integrations.data_upload.models import ImportResult
    result = ImportResult(sheet_type="unit_levy_status")

    try:
        headers, rows = _csv_rows(content)
    except Exception as e:
        result.errors.append(f"CSV parse error: {str(e)}")
        return result

    rows = rows[:_MAX_ROWS]
    result.total_rows = len(rows)
    now = datetime.now(timezone.utc).isoformat()
    # East Gate 13195 investigation (2026-07-22): unlike the budget/levy-schedule
    # CSVs above, this endpoint writes unit_levy_ledger — actual per-unit
    # levied/paid figures, a payment-shaped fact with no "proposed" concept — so
    # a future levy year is never legitimate here, regardless of any status field.
    # This is the confirmed source of East Gate's 87-document phantom "2027"
    # unit_levy_ledger batch.
    current_levy_year = await _current_levy_year(building_id)

    for i, row in enumerate(rows, 1):
        try:
            row = {k.strip(): v.strip() for k, v in row.items() if k}

            unit_number = row.get("unit_number", "").strip()
            lot_number = row.get("lot_number", "").strip()
            year = row.get("financial_year", financial_year).strip()

            if not unit_number or not year:
                result.errors.append(f"Row {i}: missing unit_number or financial_year")
                result.skipped += 1
                continue

            year_int = _year_int_or_none(year)
            if year_int is not None and year_int > current_levy_year:
                result.errors.append(
                    f"Row {i}: financial_year={year} is beyond the building's current "
                    f"levy year ({current_levy_year}) — unit levy ledger data cannot be "
                    f"imported for a future year"
                )
                result.skipped += 1
                continue

            def fv(key):
                return _parse_float(row.get(key, "0"))

            admin_levied = fv("admin_levied")
            admin_paid = fv("admin_paid")
            sinking_levied = fv("sinking_levied")
            sinking_paid = fv("sinking_paid")
            total_levied = admin_levied + sinking_levied
            total_paid = admin_paid + sinking_paid
            net_balance = total_levied - total_paid  # positive = owes money

            levy_status = row.get("levy_status", "").strip().lower() or (
                "arrears" if net_balance > 0.01 else "credit" if net_balance < -0.01 else "current"
            )

            quarterly_data = {}
            for q in ["q1", "q2", "q3", "q4"]:
                quarterly_data[f"{q}_amount"] = fv(f"{q}_amount")
                quarterly_data[f"{q}_paid"] = fv(f"{q}_paid")
                quarterly_data[f"{q}_date"] = row.get(f"{q}_date", "").strip() or None
                quarterly_data[f"{q}_balance"] = round(
                    quarterly_data[f"{q}_amount"] - quarterly_data[f"{q}_paid"], 2
                )

            ledger_doc = {
                "year": year,
                "financial_year": year,
                "unit_number": unit_number,
                "lot_number": lot_number,
                "building_id": building_id,
                "uoe": 0,  # enriched below from units collection
                "admin_opening": fv("admin_opening_balance"),
                "admin_levied": admin_levied,
                "admin_paid": admin_paid,
                "admin_closing": fv("admin_closing_balance"),
                "sinking_opening": fv("sinking_opening_balance"),
                "sinking_levied": sinking_levied,
                "sinking_paid": sinking_paid,
                "sinking_closing": fv("sinking_closing_balance"),
                "total_levied": total_levied,
                "total_paid": total_paid,
                "net_balance": round(net_balance, 2),
                "levy_status": levy_status,
                "arrears_amount": fv("arrears_amount") or max(0, net_balance),
                "notes": row.get("notes", "").strip() or None,
                **quarterly_data,
                "updated_at": now,
            }

            # Enrich UOE from units collection
            unit_doc = await db.units.find_one(
                {"building_id": building_id, "unit_number": unit_number},
                {"_id": 0, "unit_entitlement": 1}
            )
            if unit_doc and unit_doc.get("unit_entitlement"):
                ledger_doc["uoe"] = unit_doc["unit_entitlement"]

            existing = await db.unit_levy_ledger.find_one(
                {"year": year, "building_id": building_id, "unit_number": unit_number},
                {"_id": 0, "id": 1}
            )

            if existing:
                await db.unit_levy_ledger.update_one(
                    {"year": year, "building_id": building_id, "unit_number": unit_number},
                    {"$set": ledger_doc}
                )
                result.updated += 1
            else:
                ledger_doc["id"] = str(uuid.uuid4())
                ledger_doc["created_at"] = now
                await db.unit_levy_ledger.insert_one(ledger_doc)
                result.imported += 1

        except Exception as e:
            result.errors.append(f"Row {i}: {str(e)}")
            result.skipped += 1

    return result
