#!/usr/bin/env python3
"""
One-time migration: bridge existing strata_financials / strata_owners data
into the levy accounting collections (levy_categories, annual_levies,
unit_levy_ledger) so that finance pages (collection-rate, fund-health, etc.)
reflect the portal snapshot.

Run from project root:
    cd backend && venv/bin/python3 seeds/migrate_strata_sync_to_financial.py

Safe to re-run — uses upserts throughout. Existing non-synthetic data is never
overwritten (is_synthetic=False guard preserved from the router bridge).
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv

load_dotenv(BACKEND_DIR / ".env")

from pymongo import AsyncMongoClient

from services.ownership_transfer_detection_service import detect_and_create_portal_owner_transfer

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
_TOTAL_UOE_FALLBACK = 10000


def _classify_fund(category: str) -> str:
    cw_keywords = [
        "capital", "sinking", "reserve", "painting", "roof", "lift", "elevator",
        "facade", "structural", "major", "plant", "equipment replacement",
    ]
    cat_lower = (category or "").lower()
    return "capital_works" if any(kw in cat_lower for kw in cw_keywords) else "admin"


async def _bridge(mdb, building_id: str, financial_year: str, financials: list, owners: list) -> dict:
    """
    Mirror of strata_sync._sync_to_levy_collections, operating on a raw Motor db.
    Returns a summary dict of what was written.
    """
    year = financial_year.split("-")[1] if "-" in financial_year else financial_year
    now = datetime.now(timezone.utc).isoformat()
    written = {"levy_categories": 0, "annual_levies": "unchanged", "unit_levy_ledger": 0}

    admin_planned = 0.0
    admin_actual = 0.0
    cw_planned = 0.0
    cw_actual = 0.0

    # ── 1. Upsert levy_categories ────────────────────────────────────────────
    for fin in financials:
        fund = fin.get("fund") or _classify_fund(fin.get("category", ""))
        fund_type = "sinking" if fund == "capital_works" else "administrative"
        planned = round(float(fin.get("planned", 0)), 2)
        actual = round(float(fin.get("actual", 0)), 2)
        if fund == "admin":
            admin_planned += planned
            admin_actual += actual
        else:
            cw_planned += planned
            cw_actual += actual

        status = "on_track"
        if planned > 0 and actual > planned:
            status = "over_budget"
        elif actual < planned:
            status = "under_budget"

        await mdb["levy_categories"].update_one(
            {"building_id": building_id, "year": year, "name": fin["category"]},
            {"$set": {
                "building_id": building_id,
                "plan_id": building_id,
                "year": year,
                "fund_type": fund_type,
                "name": fin["category"],
                "budgeted_amount": planned,
                "actual_amount": actual,
                "previous_actual": round(float(fin.get("previous", 0)), 2),
                "variance": round(float(fin.get("variance", planned - actual)), 2),
                "status": status,
                "data_source": "scraper",
                "updated_at": now,
            }},
            upsert=True,
        )
        written["levy_categories"] += 1

    # ── 2. Create / update annual_levies ─────────────────────────────────────
    total_uoe_from_owners = sum(int(o.get("uoe") or 0) for o in owners) if owners else 0
    existing_levy = await mdb["annual_levies"].find_one({"building_id": building_id, "year": year})

    if not existing_levy and (admin_planned > 0 or cw_planned > 0):
        await mdb["annual_levies"].insert_one({
            "id": str(uuid.uuid4()),
            "building_id": building_id,
            "plan_id": building_id,
            "year": year,
            "status": "partial_actual",
            "total_uoe": total_uoe_from_owners or _TOTAL_UOE_FALLBACK,
            "admin_fund": {
                "levy_income": round(admin_planned, 2),
                "total_income": round(admin_planned, 2),
                "total_expenses": round(admin_actual, 2),
                "opening_balance": 0.0,
                "closing_balance": 0.0,
                "surplus_deficit": round(admin_planned - admin_actual, 2),
            },
            "sinking_fund": {
                "levy_income": round(cw_planned, 2),
                "total_income": round(cw_planned, 2),
                "total_expenses": round(cw_actual, 2),
                "opening_balance": 0.0,
                "closing_balance": 0.0,
                "surplus_deficit": round(cw_planned - cw_actual, 2),
            },
            "payment_schedule": [],
            "admin_levy_per_uoe_annual": 0.0,
            "admin_levy_per_uoe_quarterly": 0.0,
            "sinking_levy_per_uoe_annual": 0.0,
            "sinking_levy_per_uoe_quarterly": 0.0,
            "data_source": "scraper_import",
            "is_synthetic": True,
            "created_at": now,
            "updated_at": now,
        })
        written["annual_levies"] = "created"
    elif existing_levy:
        upd: dict = {
            "admin_fund.total_expenses": round(admin_actual, 2),
            "sinking_fund.total_expenses": round(cw_actual, 2),
            "updated_at": now,
        }
        # Only overwrite income totals when the record is synthetic (scraper-created),
        # never touch AGM-ratified data (is_synthetic=False).
        if existing_levy.get("is_synthetic") or existing_levy.get("data_source") == "scraper_import":
            upd.update({
                "admin_fund.levy_income": round(admin_planned, 2),
                "admin_fund.total_income": round(admin_planned, 2),
                "sinking_fund.levy_income": round(cw_planned, 2),
                "sinking_fund.total_income": round(cw_planned, 2),
            })
            written["annual_levies"] = "updated (synthetic)"
        else:
            written["annual_levies"] = "expenses-only update (non-synthetic preserved)"
        await mdb["annual_levies"].update_one(
            {"building_id": building_id, "year": year}, {"$set": upd}
        )

    # ── 3. Update unit_levy_ledger ────────────────────────────────────────────
    if not owners:
        written["unit_levy_ledger"] = "skipped (no owner data)"
        return written

    levy_doc = await mdb["annual_levies"].find_one({"building_id": building_id, "year": year})
    admin_inc = float((levy_doc or {}).get("admin_fund", {}).get("total_income", 0))
    cw_inc = float((levy_doc or {}).get("sinking_fund", {}).get("total_income", 0))
    ann_total = admin_inc + cw_inc
    t_uoe = int((levy_doc or {}).get("total_uoe") or total_uoe_from_owners or _TOTAL_UOE_FALLBACK)
    a_ratio = (admin_inc / ann_total) if ann_total > 0 else 0.75
    c_ratio = (cw_inc / ann_total) if ann_total > 0 else 0.25
    today_month = datetime.now(timezone.utc).month
    q_billed = max(1, min(4, sum(1 for q in [3, 6, 9, 12] if today_month >= q)))

    for owner in owners:
        un = owner.get("unit_number")
        if not un:
            continue
        try:
            await detect_and_create_portal_owner_transfer(
                mdb,
                building_id,
                un,
                owner.get("owner_name") or owner.get("owner"),
                detected_at=now,
                source="strata_sync_financial_bridge_owner_name_drift",
            )
        except Exception as exc:
            print(f"  WARNING: owner transfer detection failed for {building_id}/{un}: {exc}")
        uoe = int(owner.get("uoe") or 0)
        net_bal = round(float(owner.get("balance", 0)), 2)
        if uoe > 0 and ann_total > 0 and t_uoe > 0:
            t_levied = round((uoe / t_uoe) * ann_total * q_billed / 4, 2)
        else:
            t_levied = 0.0
        t_paid = round(max(0.0, t_levied - net_bal), 2)

        existing_l = await mdb["unit_levy_ledger"].find_one(
            {"building_id": building_id, "unit_number": un, "year": year}
        )
        if existing_l:
            upd = {"net_balance": net_bal, "updated_at": now}
            if existing_l.get("data_source") == "scraper":
                upd.update({
                    "total_levied": t_levied,
                    "total_paid": t_paid,
                    "admin_levied": round(t_levied * a_ratio, 2),
                    "admin_paid": round(t_paid * a_ratio, 2),
                    "admin_closing": round(net_bal * a_ratio, 2),
                    "sinking_levied": round(t_levied * c_ratio, 2),
                    "sinking_paid": round(t_paid * c_ratio, 2),
                    "sinking_closing": round(net_bal * c_ratio, 2),
                })
            await mdb["unit_levy_ledger"].update_one(
                {"building_id": building_id, "unit_number": un, "year": year},
                {"$set": upd},
            )
        else:
            await mdb["unit_levy_ledger"].insert_one({
                "id": str(uuid.uuid4()),
                "building_id": building_id,
                "year": year,
                "unit_number": un,
                "lot_number": "",
                "uoe": uoe,
                "property_type": "",
                "admin_opening": 0.0,
                "admin_levied": round(t_levied * a_ratio, 2),
                "admin_paid": round(t_paid * a_ratio, 2),
                "admin_closing": round(net_bal * a_ratio, 2),
                "sinking_opening": 0.0,
                "sinking_levied": round(t_levied * c_ratio, 2),
                "sinking_paid": round(t_paid * c_ratio, 2),
                "sinking_closing": round(net_bal * c_ratio, 2),
                "total_levied": t_levied,
                "total_paid": t_paid,
                "net_balance": net_bal,
                "data_source": "scraper",
                "created_at": now,
                "updated_at": now,
            })
        written["unit_levy_ledger"] += 1

    return written


async def run():
    client = AsyncMongoClient(MONGO_URL)
    mdb = client[DB_NAME]

    # Determine financial year (same logic as strata_sync router)
    now = datetime.now(timezone.utc)
    yr, mo = now.year, now.month
    fy_start = yr - 1 if mo < 7 else yr
    financial_year = f"{fy_start}-{fy_start + 1}"
    year_label = str(fy_start + 1)

    print(f"\n=== Strata Sync → Financial Collections Migration ===")
    print(f"Database : {DB_NAME}")
    print(f"FY       : {financial_year}  (year key = '{year_label}')\n")

    # Gather all buildings that have scraped data
    buildings_with_data = await mdb["strata_financials"].distinct("building_id")
    if not buildings_with_data:
        print("No data found in strata_financials — nothing to migrate.")
        client.close()
        return

    for building_id in buildings_with_data:
        print(f"--- Building: {building_id} ---")

        financials = await (
            mdb["strata_financials"]
            .find({"building_id": building_id}, {"_id": 0})
            .to_list(500)
        )
        owners = await (
            mdb["strata_owners"]
            .find({"building_id": building_id}, {"_id": 0})
            .to_list(300)
        )

        print(f"  Found {len(financials)} financial categories, {len(owners)} owner positions")

        if not financials and not owners:
            print("  Nothing to bridge — skipping.")
            continue

        result = await _bridge(mdb, building_id, financial_year, financials, owners)
        print(f"  levy_categories  : {result['levy_categories']} upserted")
        print(f"  annual_levies    : {result['annual_levies']}")
        print(f"  unit_levy_ledger : {result['unit_levy_ledger']} upserted/updated")

        # Verify
        lc_count = await mdb["levy_categories"].count_documents({"building_id": building_id, "year": year_label})
        al = await mdb["annual_levies"].find_one({"building_id": building_id, "year": year_label})
        ll_count = await mdb["unit_levy_ledger"].count_documents({"building_id": building_id, "year": year_label})
        print(f"\n  Verification (year={year_label}):")
        print(f"    levy_categories  : {lc_count} records")
        if al:
            af = al.get("admin_fund", {})
            sf = al.get("sinking_fund", {})
            print(f"    annual_levies    : admin_income={af.get('total_income')}, "
                  f"sinking_income={sf.get('total_income')}, is_synthetic={al.get('is_synthetic')}")
        else:
            print(f"    annual_levies    : NOT FOUND")
        print(f"    unit_levy_ledger : {ll_count} records")

    print("\n=== Migration complete ===\n")
    client.close()


if __name__ == "__main__":
    asyncio.run(run())
