# @featuretrace:levy — CSV bulk-import router: unit owners, annual levy, budget categories, unit levy status.
# Layer: router
# Data flow: staff CSV upload -> POST /financial-import/{sheet_type} -> financial_import_service.py
#            -> units / annual_levies / levy_categories / unit_levy_ledger (building-scoped).
# Related: backend/services/financial_import_service.py
#           backend/routers/finance.py (_upsert_ledger_for_payment — the other, non-CSV writer of the same fields)
#           backend/scripts/ingest/strata_web_portal_ingest.py (separate staging path, does not feed this router)
# Collection: units, annual_levies, levy_categories, unit_levy_ledger, financial_import_logs

# IMPORTANT: process_unit_levy_status_csv() replaces unit_levy_ledger.total_paid/net_balance wholesale
#            ($set of the full doc) — a re-upload after payments were recorded via finance.py's
#            POST /levy-payments -> _upsert_ledger_for_payment will silently overwrite those payment-driven
#            totals, contradicting finance.py's own "never write total_paid/net_balance from external
#            imports" invariant comment.
import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from bson import ObjectId

import asyncio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from database import db
from strataos_demo_integrations.data_upload.models import FinancialYearImportResponse, ImportResult
from strataos_demo_integrations.data_upload.service import (
    process_annual_levy_csv,
    process_budget_categories_csv,
    process_unit_levy_status_csv,
    process_unit_owners_csv,
)
from utils.auth import effective_role, get_current_building, get_current_user
from utils.file_scan import scan_upload
from utils.helpers import create_audit_log
from utils.permissions import get_user_permissions

router = APIRouter(prefix="/financial-import", tags=["Financial Import"])
portal_snapshots_router = APIRouter(prefix="", tags=["Financial Import"])

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
_ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
}
_ALLOWED_ROLES = {"super_admin", "strata_manager", "strata_admin"}


def _check_permissions(current_user: dict) -> None:
    """Raise 403 if user lacks finance management permission or an allowed role."""
    permissions = get_user_permissions(current_user)
    if not permissions.can_manage_finances:
        raise HTTPException(status_code=403, detail="Not authorized to manage finances")
    if effective_role(current_user) not in _ALLOWED_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{effective_role(current_user)}' is not permitted for financial year imports",
        )


async def _read_validated_file(file: UploadFile) -> bytes:
    """Read upload, enforce size, content-type, and Magika content scan."""
    content = await file.read()
    if len(content) > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    ct = (file.content_type or "").split(";")[0].strip().lower()
    if ct and ct not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ct}'. Please upload a CSV file.",
        )
    await scan_upload(content, context="csv", filename=file.filename or "", max_size_bytes=_MAX_FILE_SIZE)
    return content


async def _save_import_log(
        building_id: str,
        financial_year: str,
        result: ImportResult,
        created_by: str,
        import_id: str,
) -> None:
    """Persist a summary record to financial_import_logs."""
    now = datetime.now(timezone.utc).isoformat()
    await db.financial_import_logs.insert_one(
        {
            "id": import_id,
            "building_id": building_id,
            "financial_year": financial_year,
            "sheet_type": result.sheet_type,
            "imported": result.imported,
            "updated": result.updated,
            "skipped": result.skipped,
            "errors_count": len(result.errors),
            "created_by": created_by,
            "created_at": now,
        }
    )


def _build_response(
        import_id: str,
        building_id: str,
        financial_year: str,
        results: list,
        created_by: str,
) -> FinancialYearImportResponse:
    # Each upload endpoint processes exactly one sheet type; take the first result
    result = results[0] if results else ImportResult(sheet_type="unknown")
    errors_count = len(result.errors)
    if errors_count > 0 and result.imported == 0 and result.updated == 0:
        status = "failed"
    elif errors_count > 0:
        status = "partial"
    else:
        status = "completed"

    return FinancialYearImportResponse(
        import_id=import_id,
        building_id=building_id,
        financial_year=financial_year,
        sheet_type=result.sheet_type,
        status=status,
        result=result,
        created_at=datetime.now(timezone.utc).isoformat(),
        created_by=created_by,
    )


# ── Upload endpoints ──────────────────────────────────────────────────────────


@router.post("/unit-owners", response_model=FinancialYearImportResponse)
async def upload_unit_owners(
        financial_year: str = Form(...),
        file: UploadFile = File(...),
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Upload owner/unit details CSV. Upserts the units collection."""
    _check_permissions(current_user)
    content = await _read_validated_file(file)

    result = await process_unit_owners_csv(content, building_id, current_user["id"])
    import_id = str(uuid.uuid4())

    await _save_import_log(building_id, financial_year, result, current_user["id"], import_id)
    asyncio.create_task(
        create_audit_log(
            action="import",
            resource_type="financial_import",
            resource_id=import_id,
            user_id=current_user["id"],
            user_name=current_user.get("name", current_user["id"]),
            details={
                "sheet_type": "unit_owners",
                "financial_year": financial_year,
                "imported": result.imported,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": len(result.errors),
            },
            building_id=building_id,
        )
    )

    return _build_response(import_id, building_id, financial_year, [result], current_user["id"])


@router.post("/annual-levy", response_model=FinancialYearImportResponse)
async def upload_annual_levy(
        financial_year: str = Form(...),
        file: UploadFile = File(...),
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Upload annual levy summary CSV. Upserts the annual_levies collection."""
    _check_permissions(current_user)
    content = await _read_validated_file(file)

    result = await process_annual_levy_csv(content, building_id, current_user["id"])
    import_id = str(uuid.uuid4())

    await _save_import_log(building_id, financial_year, result, current_user["id"], import_id)
    asyncio.create_task(
        create_audit_log(
            action="import",
            resource_type="financial_import",
            resource_id=import_id,
            user_id=current_user["id"],
            user_name=current_user.get("name", current_user["id"]),
            details={
                "sheet_type": "annual_levy",
                "financial_year": financial_year,
                "imported": result.imported,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": len(result.errors),
            },
            building_id=building_id,
        )
    )

    return _build_response(import_id, building_id, financial_year, [result], current_user["id"])


@router.post("/budget-categories", response_model=FinancialYearImportResponse)
async def upload_budget_categories(
        financial_year: str = Form(...),
        file: UploadFile = File(...),
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Upload budget categories CSV. Upserts the levy_categories collection."""
    _check_permissions(current_user)
    content = await _read_validated_file(file)

    result = await process_budget_categories_csv(content, building_id, current_user["id"])
    import_id = str(uuid.uuid4())

    await _save_import_log(building_id, financial_year, result, current_user["id"], import_id)
    asyncio.create_task(
        create_audit_log(
            action="import",
            resource_type="financial_import",
            resource_id=import_id,
            user_id=current_user["id"],
            user_name=current_user.get("name", current_user["id"]),
            details={
                "sheet_type": "budget_categories",
                "financial_year": financial_year,
                "imported": result.imported,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": len(result.errors),
            },
            building_id=building_id,
        )
    )

    return _build_response(import_id, building_id, financial_year, [result], current_user["id"])


@router.post("/unit-levy-status", response_model=FinancialYearImportResponse)
async def upload_unit_levy_status(
        financial_year: str = Form(...),
        file: UploadFile = File(...),
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Upload per-unit levy status CSV. Upserts the unit_levy_ledger collection."""
    _check_permissions(current_user)
    content = await _read_validated_file(file)

    result = await process_unit_levy_status_csv(
        content, building_id, current_user["id"], financial_year
    )
    import_id = str(uuid.uuid4())

    await _save_import_log(building_id, financial_year, result, current_user["id"], import_id)
    asyncio.create_task(
        create_audit_log(
            action="import",
            resource_type="financial_import",
            resource_id=import_id,
            user_id=current_user["id"],
            user_name=current_user.get("name", current_user["id"]),
            details={
                "sheet_type": "unit_levy_status",
                "financial_year": financial_year,
                "imported": result.imported,
                "updated": result.updated,
                "skipped": result.skipped,
                "errors": len(result.errors),
            },
            building_id=building_id,
        )
    )

    return _build_response(import_id, building_id, financial_year, [result], current_user["id"])


# ── History ───────────────────────────────────────────────────────────────────


@router.get("/history")
async def get_import_history(
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
        limit: int = 50,
):
    """List the most recent financial import log entries for this building."""
    _check_permissions(current_user)

    cursor = (
        db.financial_import_logs.find(
            {"building_id": building_id}, {"_id": 0}
        )
        .sort("created_at", -1)
        .limit(max(1, min(limit, 200)))
    )
    logs = await cursor.to_list(length=None)
    return {"logs": logs, "total": len(logs)}




def _cents(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _require_portal_snapshot_reviewer(current_user: dict) -> None:
    if effective_role(current_user) not in {"super_admin", "strata_manager"}:
        raise HTTPException(status_code=403, detail="Role not permitted to review portal snapshots")


def _require_portal_snapshot_approver(current_user: dict) -> None:
    if effective_role(current_user) != "super_admin":
        raise HTTPException(status_code=403, detail="Only super_admin may approve portal snapshots")


def _annual_levy_diff(snapshot: dict[str, Any], annual_levy: dict[str, Any] | None) -> dict[str, Any]:
    if not annual_levy:
        return {"annual_levy_exists": False}
    admin = annual_levy.get("admin_fund") or {}
    sinking = annual_levy.get("sinking_fund") or {}
    current = {
        "admin_closing_balance_cents": _cents(admin.get("closing_balance")),
        "sinking_closing_balance_cents": _cents(sinking.get("closing_balance")),
    }
    portal = {
        "admin_balance_cents": int(snapshot.get("raw_admin_fund_balance_cents") or 0),
        "sinking_balance_cents": int(snapshot.get("raw_sinking_fund_balance_cents") or 0),
    }
    return {
        "annual_levy_exists": True,
        "annual_levy_id": str(annual_levy.get("id") or annual_levy.get("_id")),
        "current": current,
        "portal": portal,
        "delta_cents": {
            "admin": portal["admin_balance_cents"] - current["admin_closing_balance_cents"],
            "sinking": portal["sinking_balance_cents"] - current["sinking_closing_balance_cents"],
        },
    }


@portal_snapshots_router.get("/financials/portal-snapshots")
async def list_portal_snapshots(
        year: str = Query(...),
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    _require_portal_snapshot_reviewer(current_user)
    cursor = db._db.staging_strata_web_snapshots.find(
        {"building_id": building_id, "financial_year": str(year), "is_test_data": {"$ne": True}}
    ).sort("snapshot_date", -1)
    snapshots = await cursor.to_list(length=100)
    annual_levy = await db.annual_levies.find_one({"building_id": building_id, "year": str(year)})
    return {
        "building_id": building_id,
        "financial_year": str(year),
        "snapshots": [
            {**_json_safe(snapshot), "annual_levy_diff": _annual_levy_diff(snapshot, annual_levy)}
            for snapshot in snapshots
        ],
    }


@portal_snapshots_router.post("/financials/portal-snapshots/{snapshot_id}/approve")
async def approve_portal_snapshot(
        snapshot_id: str,
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    _require_portal_snapshot_approver(current_user)
    try:
        oid = ObjectId(snapshot_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid snapshot_id") from exc
    now = datetime.now(timezone.utc)
    result = await db._db.staging_strata_web_snapshots.update_one(
        {"_id": oid, "building_id": building_id, "is_test_data": {"$ne": True}},
        {"$set": {"approved_by": str(current_user.get("id")), "approved_at": now, "approval_status": "approved"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Portal snapshot not found")
    snapshot = await db._db.staging_strata_web_snapshots.find_one({"_id": oid, "building_id": building_id})
    return {"snapshot": _json_safe(snapshot), "promoted_to_postgres": False}

# ── CSV Templates ─────────────────────────────────────────────────────────────

_TEMPLATES: dict[str, tuple[str, str]] = {
    "unit_owners": (
        "lot_number,unit_number,unit_type,mixed_use_type,primary_owner_name,"
        "secondary_owner_name,owner_email,uoe,asset_value,status,notes\n"
        "1,101,apartment,,John Smith,Jane Smith,john@example.com,115,,"
        "owner_occupied,\n",
        "unit_owners_template.csv",
    ),
    "annual_levy": (
        "financial_year,admin_levy_per_uoe_proposed,admin_levy_per_uoe_actual,"
        "sinking_levy_per_uoe_proposed,sinking_levy_per_uoe_actual,"
        "admin_total_income_proposed,admin_total_income_actual,"
        "admin_total_expenses_proposed,admin_total_expenses_actual,"
        "admin_opening_balance,admin_closing_balance_projected,admin_closing_balance_actual,"
        "sinking_total_income_proposed,sinking_total_income_actual,"
        "sinking_total_expenses_proposed,sinking_total_expenses_actual,"
        "sinking_opening_balance,sinking_closing_balance_projected,sinking_closing_balance_actual\n"
        "2026-2027,34.087,,,9.950,,340870.20,,99504.90,,"
        "10000,,,10000,,,5000,,\n",
        "annual_levy_template.csv",
    ),
    "budget_categories": (
        "financial_year,fund_type,category_name,budgeted_amount,actual_amount,description\n"
        "2026-2027,admin,Administration & Management,85000,,\n"
        "2026-2027,sinking,Painting & Decorating,25000,,\n",
        "budget_categories_template.csv",
    ),
    "unit_levy_status": (
        "lot_number,unit_number,financial_year,"
        "admin_opening_balance,admin_levied,admin_paid,admin_closing_balance,"
        "sinking_opening_balance,sinking_levied,sinking_paid,sinking_closing_balance,"
        "levy_status,q1_amount,q1_paid,q1_date,q2_amount,q2_paid,q2_date,"
        "q3_amount,q3_paid,q3_date,q4_amount,q4_paid,q4_date,arrears_amount,notes\n"
        "1,101,2026-2027,0,3922.10,3922.10,0,0,1144.76,1144.76,0,"
        "current,1241.22,1241.22,2026-07-01,1241.22,1241.22,2026-10-01,"
        "1241.22,1241.22,2027-01-01,1341.20,1341.20,2027-04-01,0,\n",
        "unit_levy_status_template.csv",
    ),
}


@router.get("/templates/{template_type}")
async def download_template(
        template_type: str,
        current_user: dict = Depends(get_current_user),
        building_id: str = Depends(get_current_building),
):
    """Download a CSV template for the given import type."""
    _check_permissions(current_user)

    if template_type not in _TEMPLATES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown template type '{template_type}'. "
                   f"Valid types: {', '.join(_TEMPLATES.keys())}",
        )

    csv_content, filename = _TEMPLATES[template_type]

    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
