"""
Strata Portal Sync Router
Manages two sync workflows:
  1. Browser-based:  Click → Login → PIN → Scrape → Clean → Store
  2. Postman push:   POST /strata/sync/push with pre-cleaned JSON payload

Jobs are tracked in strata_sync_jobs (global, not tenant-scoped).

After each sync the scraped data is bridged into the levy accounting collections
(levy_categories, annual_levies, unit_levy_ledger) so that financial dashboards
reflect the portal snapshot immediately.  Records created this way are tagged
data_source="scraper" and is_synthetic=True so user-uploaded data takes precedence.
"""
import logging
import os
import subprocess
import sys
import uuid

logger = logging.getLogger(__name__)
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Literal, Optional

from database import db
from services.ownership_transfer_detection_service import detect_and_create_portal_owner_transfer
from utils.auth import get_current_user, get_current_building

router = APIRouter(prefix="/strata", tags=["Strata Sync"])

_ADMIN_ROLES = {"super_admin", "ec_member", "strata_admin", "strata_manager"}

# When this toggle is enabled the direct unit_levy_ledger write path is bypassed.
# Payments reach the ledger via Demo Bank → MatchingEngine → FinancialCoreService instead.
_DIRECT_WRITE_TOGGLE = "disable_strata_sync_direct_write"


def _jobs():
    """Raw (non-tenant-scoped) Motor collection for sync job tracking."""
    return db._db["strata_sync_jobs"]


# ─── Pydantic models ──────────────────────────────────────────────────────────

class PinSubmit(BaseModel):
    job_id: str
    pin: str


class PreviewConfirm(BaseModel):
    job_id: str
    action: Literal["confirm", "discard"]


class TransactionItem(BaseModel):
    date: str
    invoice_ref: Optional[str] = ""
    supplier: Optional[str] = ""
    details: Optional[str] = ""
    amount: float = 0.0


class FinancialItem(BaseModel):
    category: str
    planned: float = 0.0
    actual: float = 0.0
    variance: float = 0.0
    previous: float = 0.0
    fund: Optional[str] = None  # "admin" | "capital_works" — stored as "capital_works" (Sinking Fund)
    variance_pct: Optional[float] = None
    transactions: Optional[list[TransactionItem]] = None  # individual invoice lines per category


class OwnerItem(BaseModel):
    lot: Optional[int] = None
    unit: Optional[int] = None
    unit_number: str  # e.g. "UA042" or "TH071"
    owner: Optional[str] = None
    uoe: Optional[int] = None
    balance: float = 0.0
    status: Optional[str] = None  # "ARREARS" | "CREDIT" | "CLEAR" — inferred if omitted


class PushPayload(BaseModel):
    """
    Accepted by POST /strata/sync/push for direct Postman ingestion.
    Provide at least one of financials or owners.
    """
    financials: Optional[list[FinancialItem]] = None
    owners: Optional[list[OwnerItem]] = None


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _classify_fund(category: str) -> str:
    # All multi-word phrases — no bare "upgrade" or "improvement" to prevent false positives.
    # e.g. bare "upgrade" would misclassify "Management Upgrade Fee" as capital_works.
    cw_kw = [
        "capital works", "roof repairs", "lift replacement", "lift repair",
        "garage door replace", "sprinkler system",
        "plumbing & drainage works", "fire protection replace",
    ]
    return "capital_works" if any(kw in category.lower() for kw in cw_kw) else "admin"


def _classify_status(balance: float) -> str:
    if balance > 0:
        return "ARREARS"
    if balance < 0:
        return "CREDIT"
    return "CLEAR"


def _build_summary(building_id: str, owners: list, financials: list) -> dict:
    in_arrears = [o for o in owners if o["status"] == "ARREARS"]
    in_credit = [o for o in owners if o["status"] == "CREDIT"]
    clear = [o for o in owners if o["status"] == "CLEAR"]

    arrears_total = round(sum(o["balance"] for o in in_arrears), 2)
    credit_total = round(abs(sum(o["balance"] for o in in_credit)), 2)
    total_lots = len(owners)
    collection_rate = round(((total_lots - len(in_arrears)) / total_lots * 100), 1) if total_lots else 0

    risk_level = "LOW" if collection_rate >= 95 else ("MEDIUM" if collection_rate >= 88 else "HIGH")

    top_arrears = sorted(in_arrears, key=lambda x: x["balance"], reverse=True)[:5]
    overruns = [f for f in financials if f["variance"] < 0 and f["planned"] > 0]
    overruns_sorted = sorted(overruns, key=lambda x: x["variance"])[:5]
    admin_fin = [f for f in financials if f["fund"] == "admin"]
    cw_fin = [f for f in financials if f["fund"] == "capital_works"]

    return {
        "building_id": building_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_lots": total_lots,
        "arrears_count": len(in_arrears),
        "credit_count": len(in_credit),
        "clear_count": len(clear),
        "arrears_total": arrears_total,
        "credit_total": credit_total,
        "collection_rate": collection_rate,
        "risk_level": risk_level,
        "top_arrears": [
            {"lot": o.get("lot"), "unit_number": o["unit_number"], "owner": o.get("owner"), "balance": o["balance"]}
            for o in top_arrears
        ],
        "budget_overruns": [
            {
                "category": f["category"],
                "fund": f["fund"],
                "planned": f["planned"],
                "actual": f["actual"],
                "overspend": abs(f["variance"]),
                "overspend_pct": abs(f.get("variance_pct", 0)),
            }
            for f in overruns_sorted
        ],
        "admin_fund": {
            "total_planned": round(sum(f["planned"] for f in admin_fin), 2),
            "total_actual": round(sum(f["actual"] for f in admin_fin), 2),
        },
        "capital_works_fund": {
            "total_planned": round(sum(f["planned"] for f in cw_fin), 2),
            "total_actual": round(sum(f["actual"] for f in cw_fin), 2),
        },
    }


async def _upsert_financials(building_id: str, financials: list, now: str):
    # Determine current financial year from year in `now` timestamp
    year = int(now[:4])
    month = int(now[5:7])
    # Financial year is July-June; if before July, current FY started last year
    fy_start = year - 1 if month < 7 else year
    financial_year = f"{fy_start}-{fy_start + 1}"

    for fin in financials:
        await db.strata_financials.update_one(
            {"building_id": building_id, "category": fin["category"], "financial_year": financial_year},
            {"$set": {**fin, "building_id": building_id, "financial_year": financial_year, "updated_at": now}},
            upsert=True,
        )


def _split_owner_name(combined: str) -> tuple:
    """
    Split 'Owner A & Owner B', 'Owner A and Owner B', or 'Owner A, Owner B'
    → (primary, secondary_or_empty).  Delimiters tried in priority order:
    ' & ' → ' and ' → ', '
    """
    for sep in (" & ", " and ", ", "):
        idx = combined.lower().find(sep.lower())
        if idx >= 0:
            return combined[:idx].strip(), combined[idx + len(sep):].strip()
    return combined.strip(), ""


_log = logging.getLogger(__name__)


async def _upsert_owners(building_id: str, owners: list, now: str):
    # Guard: pre-fetch known unit_numbers so a mis-mapped lot never silently
    # misses the units collection. Lots 1-70 → UA001-UA070; 71+ → TH071-TH087.
    valid_units = {
        u["unit_number"]
        async for u in db._db["units"].find(
            {"building_id": building_id}, {"unit_number": 1}
        )
    }
    for owner in owners:
        un = owner["unit_number"]

        # Log name changes (informational — no new collection created)
        existing = await db.strata_owners.find_one(
            {"building_id": building_id, "unit_number": un},
            {"owner": 1},
        )
        if existing and existing.get("owner") and existing["owner"] != owner.get("owner", ""):
            _log.info(
                "strata_sync: owner name change on %s: %r → %r",
                un, existing["owner"], owner.get("owner"),
            )

        # Split combined owner name into owner_name / owner_name_b using the
        # existing multiple-owners convention (owner_name, owner_name_b fields)
        owner_combined = owner.get("owner") or ""
        owner_a, owner_b = _split_owner_name(owner_combined)
        try:
            detection = await detect_and_create_portal_owner_transfer(
                db,
                building_id,
                un,
                owner_combined,
                detected_at=now,
            )
            if detection.get("created"):
                _log.info(
                    "strata_sync: created owner transfer request %s for portal owner drift on %s",
                    detection.get("id"),
                    un,
                )
        except Exception as exc:
            _log.warning(
                "strata_sync: owner transfer detection failed for %s/%s: %s",
                building_id,
                un,
                exc,
            )
        owner_doc = {
            **owner,
            "owner_name": owner_a,
            "owner_name_b": owner_b or None,
            "building_id": building_id,
            "updated_at": now,
        }
        await db.strata_owners.update_one(
            {"building_id": building_id, "unit_number": un},
            {"$set": owner_doc},
            upsert=True,
        )
        if un not in valid_units:
            _log.warning(
                "strata_sync: unit_number %r (lot %s) not found in units collection "
                "— owner name NOT updated. Check lot→unit_number mapping.",
                un, owner.get("lot"),
            )
            continue
        # Update the units master registry with the portal's current owner name only.
        # Portal balance lives exclusively in strata_owners (single source of truth).
        # balance_owing / balance_credit are managed by the levy system — never touched here.
        # A re-scrape that drops from two owners to one must clear the stale
        # owner_name_b, not just skip setting it — otherwise the old second
        # owner's name lingers on the unit forever (e.g. "A & B" long after
        # the portal only reports "A").
        update_ops: dict = {"$set": {"owner_name": owner_a}}
        if owner_b:
            update_ops["$set"]["owner_name_b"] = owner_b
        else:
            update_ops["$unset"] = {"owner_name_b": ""}
        await db._db["units"].update_one(
            {"building_id": building_id, "unit_number": un},
            update_ops,
        )


_TOTAL_UOE_FALLBACK = 10000  # East Gate default; overridden by sum of scraped UOEs


async def _sync_to_levy_collections(
        building_id: str,
        financial_year: str,
        financials: list,
        owners: list,
) -> None:
    """
    Bridge scraped portal data into the levy accounting collections so that
    financial dashboards reflect the portal snapshot without a manual CSV upload.

    financial_year format: "2025-2026"  →  year = "2026" (end year, matches annual_levies.year)

    Rules:
    - levy_categories: always upserted from financials (portal is authoritative for budget lines)
    - annual_levies: created (is_synthetic=True) if missing; income totals updated only when
      the record is synthetic — never overwrites AGM-ratified data (is_synthetic=False)
    - unit_levy_ledger: created with data_source="scraper" if missing; net_balance is always
      refreshed; levied/paid are recalculated only for scraper-sourced records
    """
    year = financial_year.split("-")[1] if "-" in financial_year else financial_year
    now = datetime.now(timezone.utc).isoformat()

    admin_planned = 0.0
    admin_actual = 0.0
    cw_planned = 0.0
    cw_actual = 0.0

    # ── 1. Upsert levy_categories ─────────────────────────────────────────────
    for fin in financials:
        fund = fin.get("fund", "admin")
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

        await db.levy_categories.update_one(
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

    # ── 2. Create / update annual_levies ──────────────────────────────────────
    total_uoe_from_owners = sum(int(o.get("uoe") or 0) for o in owners) if owners else 0
    existing_levy = await db.annual_levies.find_one({"building_id": building_id, "year": year})

    if not existing_levy and (admin_planned > 0 or cw_planned > 0):
        await db.annual_levies.insert_one({
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
    elif existing_levy:
        update_fields: dict = {
            "admin_fund.total_expenses": round(admin_actual, 2),
            "sinking_fund.total_expenses": round(cw_actual, 2),
            "updated_at": now,
        }
        if existing_levy.get("is_synthetic") or existing_levy.get("data_source") == "scraper_import":
            update_fields.update({
                "admin_fund.levy_income": round(admin_planned, 2),
                "admin_fund.total_income": round(admin_planned, 2),
                "sinking_fund.levy_income": round(cw_planned, 2),
                "sinking_fund.total_income": round(cw_planned, 2),
            })
        await db.annual_levies.update_one(
            {"building_id": building_id, "year": year},
            {"$set": update_fields},
        )

    # ── 3. Update unit_levy_ledger from owners ────────────────────────────────
    if not owners:
        return

    levy_doc = await db.annual_levies.find_one({"building_id": building_id, "year": year})
    admin_income = float((levy_doc or {}).get("admin_fund", {}).get("total_income", 0))
    cw_income = float((levy_doc or {}).get("sinking_fund", {}).get("total_income", 0))
    annual_levy_total = admin_income + cw_income
    total_uoe = int((levy_doc or {}).get("total_uoe") or total_uoe_from_owners or _TOTAL_UOE_FALLBACK)
    admin_ratio = (admin_income / annual_levy_total) if annual_levy_total > 0 else 0.75
    cw_ratio = (cw_income / annual_levy_total) if annual_levy_total > 0 else 0.25

    # Quarters billed: Q1 due March, Q2 June, Q3 September, Q4 December
    today_month = datetime.now(timezone.utc).month
    quarters_billed = sum(1 for q in [3, 6, 9, 12] if today_month >= q)
    quarters_billed = max(1, min(4, quarters_billed))

    for owner in owners:
        un = owner.get("unit_number")
        if not un:
            continue
        uoe = int(owner.get("uoe") or 0)
        net_balance = round(float(owner.get("balance", 0)), 2)

        if uoe > 0 and annual_levy_total > 0 and total_uoe > 0:
            unit_annual = (uoe / total_uoe) * annual_levy_total
            total_levied = round(unit_annual * quarters_billed / 4, 2)
        else:
            total_levied = 0.0

        total_paid = round(max(0.0, total_levied - net_balance), 2)
        admin_levied = round(total_levied * admin_ratio, 2)
        admin_paid = round(total_paid * admin_ratio, 2)
        sinking_levied = round(total_levied * cw_ratio, 2)
        sinking_paid = round(total_paid * cw_ratio, 2)

        existing_ledger = await db.unit_levy_ledger.find_one(
            {"building_id": building_id, "unit_number": un, "year": year}
        )
        if existing_ledger:
            update_fields = {"net_balance": net_balance, "updated_at": now}

            if existing_ledger.get("data_source") == "scraper":
                # Scraper-owned records: recalculate everything from portal figures
                _admin_opening = existing_ledger.get("admin_opening", 0.0)
                _sinking_opening = existing_ledger.get("sinking_opening", 0.0)
                _admin_closing = round(net_balance * admin_ratio, 2)
                _sinking_closing = round(net_balance * cw_ratio, 2)
                _admin_interest = round(
                    _admin_closing - (_admin_opening + admin_levied - admin_paid), 2
                )
                _sinking_interest = round(
                    _sinking_closing - (_sinking_opening + sinking_levied - sinking_paid), 2
                )
                update_fields.update({
                    "total_levied": total_levied,
                    "total_paid": total_paid,
                    "admin_levied": admin_levied,
                    "admin_paid": admin_paid,
                    "admin_closing": _admin_closing,
                    "admin_interest": _admin_interest,
                    "sinking_levied": sinking_levied,
                    "sinking_paid": sinking_paid,
                    "sinking_closing": _sinking_closing,
                    "sinking_interest": _sinking_interest,
                })
            else:
                # Externally reconciled records (data_source = strata_web_actuals, manual, etc.):
                # net_balance comes from the portal (authoritative point-in-time balance).
                # Derive total_paid from the existing levied/opening values so all fields
                # stay internally consistent after each scrape — without a CSV re-import.
                # Formula: total_paid = opening + total_levied - net_balance
                _ex_admin_lev = float(existing_ledger.get("admin_levied", 0) or 0)
                _ex_sink_lev = float(existing_ledger.get("sinking_levied", 0) or 0)
                _ex_admin_open = float(existing_ledger.get("admin_opening", 0) or 0)
                _ex_sink_open = float(existing_ledger.get("sinking_opening", 0) or 0)
                _ex_total_levied = _ex_admin_lev + _ex_sink_lev
                _ex_total_open = _ex_admin_open + _ex_sink_open
                new_total_paid = round(max(0.0, _ex_total_open + _ex_total_levied - net_balance), 2)

                if _ex_total_levied > 0:
                    _levy_ratio = _ex_admin_lev / _ex_total_levied
                else:
                    _levy_ratio = admin_ratio
                new_admin_paid = round(new_total_paid * _levy_ratio, 2)
                new_sink_paid = round(new_total_paid - new_admin_paid, 2)
                new_admin_closing = round(_ex_admin_open + _ex_admin_lev - new_admin_paid, 2)
                new_sink_closing = round(_ex_sink_open + _ex_sink_lev - new_sink_paid, 2)
                update_fields.update({
                    "total_paid": new_total_paid,
                    "admin_paid": new_admin_paid,
                    "sinking_paid": new_sink_paid,
                    "admin_closing": new_admin_closing,
                    "sinking_closing": new_sink_closing,
                    "total_closing": round(new_admin_closing + new_sink_closing, 2),
                    "strata_web_balance_snapshot": net_balance,
                    "strata_web_balance_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                })

            await db.unit_levy_ledger.update_one(
                {"building_id": building_id, "unit_number": un, "year": year},
                {"$set": update_fields},
            )
        else:
            _admin_closing = round(net_balance * admin_ratio, 2)
            _sinking_closing = round(net_balance * cw_ratio, 2)
            # opening is 0 for scraper-created records; interest absorbs the portal
            # balance remainder so that closing = opening + levied − paid + interest
            _admin_interest = round(_admin_closing - (admin_levied - admin_paid), 2)
            _sinking_interest = round(_sinking_closing - (sinking_levied - sinking_paid), 2)
            await db.unit_levy_ledger.insert_one({
                "id": str(uuid.uuid4()),
                "building_id": building_id,
                "year": year,
                "unit_number": un,
                "lot_number": "",
                "uoe": uoe,
                "property_type": "",
                "admin_opening": 0.0,
                "admin_levied": admin_levied,
                "admin_paid": admin_paid,
                "admin_closing": _admin_closing,
                "admin_interest": _admin_interest,
                "sinking_opening": 0.0,
                "sinking_levied": sinking_levied,
                "sinking_paid": sinking_paid,
                "sinking_closing": _sinking_closing,
                "sinking_interest": _sinking_interest,
                "total_levied": total_levied,
                "total_paid": total_paid,
                "net_balance": net_balance,
                "data_source": "scraper",
                "created_at": now,
                "updated_at": now,
            })


# ─── Browser-based sync endpoints ────────────────────────────────────────────

@router.post("/sync/start", status_code=201)
async def start_sync(
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Launch the portal scraper subprocess and return a job_id to poll."""
    role = current_user.get("effective_role") or current_user.get("role", "")
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin role required to sync portal data")

    # Cancel any active jobs for this building (only one at a time)
    await _jobs().update_many(
        {
            "building_id": building_id,
            "status": {"$in": ["starting", "waiting_pin", "scraping", "cleaning", "preview", "syncing"]},
        },
        {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc).isoformat()}},
    )

    job_id = str(uuid.uuid4())
    await _jobs().insert_one(
        {
            "job_id": job_id,
            "building_id": building_id,
            "source": "browser",
            "status": "starting",
            "message": "Initialising scraper...",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pin": None,
            "confirm_action": None,
            "preview_data": None,
            "result": None,
            "error": None,
        }
    )

    script_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "run_scraper.py")
    )
    # xvfb-run -a provides a virtual display so the browser can run with
    # headless=False, which avoids server-side headless detection by the portal.
    log_path = f"/tmp/strata_scraper_{job_id}.log"
    child_env = {
        **os.environ,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    subprocess.Popen(
        ["/usr/bin/xvfb-run", "-a", sys.executable, script_path, "--job-id", job_id, "--building-id", building_id],
        env=child_env,
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )

    return {"job_id": job_id, "status": "starting"}


@router.post("/sync/pin")
async def submit_pin(
        body: PinSubmit,
        current_user: dict = Depends(get_current_user),
):
    """Store the PIN so the waiting scraper subprocess can read it and continue."""
    role = current_user.get("effective_role") or current_user.get("role", "")
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin role required")

    job = await _jobs().find_one({"job_id": body.job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "waiting_pin":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not waiting for a PIN (current status: {job['status']})",
        )

    await _jobs().update_one(
        {"job_id": body.job_id},
        {"$set": {"pin": body.pin.strip(), "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True}


@router.post("/sync/preview/confirm")
async def confirm_preview(
        body: PreviewConfirm,
        current_user: dict = Depends(get_current_user),
):
    """Confirm or discard the scraped preview data. Sets confirm_action so the
    waiting scraper subprocess can proceed to write (confirm) or exit (discard)."""
    role = current_user.get("effective_role") or current_user.get("role", "")
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin role required")

    job = await _jobs().find_one({"job_id": body.job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "preview":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not in preview state (current status: {job['status']})",
        )

    await _jobs().update_one(
        {"job_id": body.job_id},
        {"$set": {"confirm_action": body.action, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True, "action": body.action}


@router.get("/sync/status/{job_id}")
async def get_status(
        job_id: str,
        current_user: dict = Depends(get_current_user),
):
    """Poll the status of a sync job."""
    job = await _jobs().find_one({"job_id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/sync/latest")
async def get_latest(
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Return the most recent completed sync job and building summary."""
    jobs_list = await (
        _jobs()
        .find({"building_id": building_id, "status": "complete"}, {"_id": 0})
        .sort("started_at", -1)
        .limit(1)
        .to_list(1)
    )
    summary_list = await (
        db._db["building_summaries"]
        .find({"building_id": building_id}, {"_id": 0})
        .sort("generated_at", -1)
        .limit(1)
        .to_list(1)
    )
    return {
        "last_job": jobs_list[0] if jobs_list else None,
        "summary": summary_list[0] if summary_list else None,
    }


# ─── Postman push endpoint ────────────────────────────────────────────────────

@router.post("/sync/push", status_code=201)
async def push_data(
        payload: PushPayload,
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """
    Direct data push — use this from Postman to ingest pre-cleaned data
    without running the browser scraper.

    Supply one or both of:
      - financials: list of budget line items
      - owners:     list of owner levy positions

    The endpoint automatically infers missing 'fund' and 'status' fields,
    computes the building health summary, and upserts everything into MongoDB.

    Example body:
      {
        "financials": [
          {"category": "Cleaning", "planned": 45000, "actual": 42000, "variance": 3000}
        ],
        "owners": [
          {"unit_number": "TH017", "owner": "Avneet Rooprai", "balance": 0}
        ]
      }
    """
    role = current_user.get("effective_role") or current_user.get("role", "")
    if role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin role required to push sync data")

    if not payload.financials and not payload.owners:
        raise HTTPException(status_code=422, detail="Provide at least one of 'financials' or 'owners'")

    now = datetime.now(timezone.utc).isoformat()
    job_id = str(uuid.uuid4())
    counts = {}

    # ── Financials ──────────────────────────────────────────────────────────
    financials_clean = []
    if payload.financials:
        for item in payload.financials:
            fin = item.model_dump()
            if not fin.get("fund"):
                fin["fund"] = _classify_fund(fin["category"])
            if fin.get("variance_pct") is None:
                fin["variance_pct"] = (
                    round((fin["variance"] / fin["planned"] * 100), 2) if fin.get("planned") else 0.0
                )
            financials_clean.append(fin)
        await _upsert_financials(building_id, financials_clean, now)
        counts["financials"] = len(financials_clean)

    # ── Owners ──────────────────────────────────────────────────────────────
    owners_clean = []
    if payload.owners:
        for item in payload.owners:
            owner = item.model_dump()
            if not owner.get("status"):
                owner["status"] = _classify_status(owner["balance"])
            owners_clean.append(owner)
        await _upsert_owners(building_id, owners_clean, now)
        counts["owners"] = len(owners_clean)

    # ── Summary (only when both datasets present) ────────────────────────────
    summary = None
    if financials_clean and owners_clean:
        summary = _build_summary(building_id, owners_clean, financials_clean)
        await db._db["building_summaries"].update_one(
            {"building_id": building_id},
            {"$set": {**summary, "updated_at": now}},
            upsert=True,
        )

    # ── Bridge into levy accounting collections ───────────────────────────────
    # Compute the same financial_year string that _upsert_financials uses.
    yr, mo = int(now[:4]), int(now[5:7])
    fy_start = yr - 1 if mo < 7 else yr
    financial_year = f"{fy_start}-{fy_start + 1}"

    from db_postgres.repos import config_repo
    direct_write_disabled = await config_repo.resolve_feature_toggle(
        building_id, _DIRECT_WRITE_TOGGLE, default=False
    )
    if direct_write_disabled:
        logger.info(
            "strata_sync: %s enabled — skipping direct unit_levy_ledger write for building %s",
            _DIRECT_WRITE_TOGGLE, building_id,
        )
    else:
        await _sync_to_levy_collections(building_id, financial_year, financials_clean, owners_clean)

    # ── Job record ────────────────────────────────────────────────────────────
    await _jobs().insert_one(
        {
            "job_id": job_id,
            "building_id": building_id,
            "source": "push",
            "status": "complete",
            "message": f"Push ingest complete — {counts}",
            "started_at": now,
            "updated_at": now,
            "completed_at": now,
            "result": summary,
            "error": None,
        }
    )

    return {
        "job_id": job_id,
        "status": "complete",
        "counts": counts,
        "summary": summary,
    }


# ─── Read-only data endpoints ─────────────────────────────────────────────────

@router.get("/financials")
async def get_financials(
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Return all synced budget line items for this building."""
    items = await (
        db.strata_financials
        .find({"building_id": building_id}, {"_id": 0})
        .sort("category", 1)
        .to_list(200)
    )
    return items


@router.get("/owners")
async def get_owners(
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Return all synced owner levy positions for this building."""
    items = await (
        db.strata_owners
        .find({"building_id": building_id}, {"_id": 0})
        .sort("unit_number", 1)
        .to_list(200)
    )
    return items


@router.get("/summary")
async def get_summary(
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Return the current building health summary."""
    docs = await (
        db._db["building_summaries"]
        .find({"building_id": building_id}, {"_id": 0})
        .sort("generated_at", -1)
        .limit(1)
        .to_list(1)
    )
    if not docs:
        raise HTTPException(status_code=404, detail="No summary found — run a sync first")
    return docs[0]
