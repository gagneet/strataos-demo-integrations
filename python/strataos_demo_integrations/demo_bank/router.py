"""
backend/routers/demo_bank.py

# @featuretrace:demo_bank — Demo Bank HTTP API: CSV import, Strata Web import, manual inject,
#                           account balance, transaction list, import batch audit log.
# Layer: router
# Data flow: HTTP request → role guard → ingestion.py → demo_bank_transactions (building-scoped)
#            → ImportResultResponse / DemoBankTransactionResponse
# Related: backend/integrations/demo_bank/ingestion.py
#          backend/integrations/demo_bank/schemas.py
#          backend/integrations/demo_bank/provider.py
#          backend/integrations/registry.py
# Toggle: demo_bank_feed_enabled
# Collection: demo_bank_transactions, demo_bank_accounts, demo_bank_import_batches
# Tests: tests/backend/test_demo_bank_provider.py

Role guards use the effective-role pattern throughout:
  _role = current_user.get("effective_role") or current_user.get("role", "guest")

Never uses user["role"] directly. Never references "chairman" as a top-level role.
"""
from __future__ import annotations

import logging
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel

from strataos_demo_integrations.demo_bank.ingestion import (
    import_csv,
    import_strata_web_snapshot,
    inject_manual,
)
from strataos_demo_integrations.demo_bank.schemas import (
    StrataWebImportRequest,
    DemoBankAccountResponse,
    DemoBankBalanceResponse,
    DemoBankTransactionResponse,
    ImportBatchResponse,
    ImportResultResponse,
    ManualTransactionRequest,
    TransactionListResponse,
)
from utils.auth import get_current_building, get_current_user
from utils.file_scan import scan_upload
from utils.permissions import require_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo-bank", tags=["Demo Bank"])

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

# ── Role helpers ──────────────────────────────────────────────────────────────

_FINANCE_ROLES = {"super_admin", "strata_admin", "strata_manager"}
_MANAGER_ROLES = {"super_admin", "strata_admin", "strata_manager", "ec_member"}
_ADMIN_ONLY = {"super_admin"}


def _require_role(current_user: dict, allowed: set[str], detail: str) -> str:
    role = current_user.get("effective_role") or current_user.get("role", "guest")
    if role not in allowed:
        raise HTTPException(status_code=403, detail=detail)
    return role


def _json_safe_doc(doc: dict) -> dict:
    """Convert ObjectId and datetime fields for JSON serialisation."""
    result = {}
    for k, v in doc.items():
        if k == "_id":
            result["id"] = str(v)
        elif isinstance(v, ObjectId):
            result[k] = str(v)
        else:
            result[k] = v
    return result


# ── CSV import ────────────────────────────────────────────────────────────────

@router.post("/import/csv", response_model=ImportResultResponse)
async def import_csv_endpoint(
    file: UploadFile = File(..., description="Bank CSV export file"),
    account_ref: str = Form(..., description="Demo Bank account reference e.g. EGR-ADMIN-001"),
    bank_name: str = Form(..., description="Bank schema name e.g. cba, nab, westpac"),
    is_test_data: bool = Form(False),
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> ImportResultResponse:
    """Upload a bank CSV export into the Demo Bank staging layer.

    Parses the file using the named bank YAML schema, normalises rows into
    demo_bank_transactions, and enforces SHA-256 idempotency. Re-uploading
    the same file returns the original batch with duplicate_batch=True.

    Never writes to levy_payments, unit_levy_ledger, or finance.* tables.
    """
    from database import db

    _require_role(current_user, _FINANCE_ROLES, "Strata manager role required for CSV import")

    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    # Virus / content scan (no-op in dev if scanner not configured)
    try:
        await scan_upload(content, file.filename or "upload.csv")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"File scan rejected upload: {exc}")

    try:
        result = await import_csv(
            db=db,
            building_id=building_id,
            account_ref=account_ref,
            bank_name=bank_name,
            file_content=content,
            filename=file.filename or "upload.csv",
            uploaded_by=str(current_user.get("id") or current_user.get("_id") or "unknown"),
            is_test_data=is_test_data,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "Demo Bank CSV import: building=%s account=%s bank=%s imported=%d skipped=%d errors=%d duplicate=%s",
        building_id, account_ref, bank_name,
        result["imported_count"], result["skipped_count"],
        result["error_count"], result["duplicate_batch"],
    )
    return ImportResultResponse(**result)


# ── Strata Web snapshot import ────────────────────────────────────────────────────

# @featuretrace:levy — Terminus of the Strata Web ingestion chain that stays inside staging; does not
#                      feed the live levy ledger.
# Layer: router
# Data flow: staging_strata_web_snapshots -> POST /demo-bank/import/strata_web -> ingestion.import_strata_web_snapshot()
#            -> demo_bank_transactions (building-scoped).
# Related: backend/integrations/demo_bank/ingestion.py (import_strata_web_snapshot)
#           backend/scripts/ingest/strata_web_portal_ingest.py (produces staging_strata_web_snapshots)
#           backend/routers/financial_matching.py (the broken downstream hop)
# Collection: staging_strata_web_snapshots, demo_bank_transactions

# IMPORTANT: rows land here as a bank-feed staging record only. Promotion into levy_payments/
#            unit_levy_ledger requires a human to run the match_review_queue "allocate" decide flow
#            in financial_matching.py. The allocate decide flow now calls _post_payment_to_ledger(),
#            which mirrors each approved receipt into Mongo levy_payments + unit_levy_ledger so
#            that all current Mongo-primary dashboards (FinancePage, CollectionRatePage,
#            OwnerDashboard, etc.) see the payment immediately after allocation.
@router.post("/import/strata_web", response_model=ImportResultResponse)
async def import_strata_web_endpoint(
    body: StrataWebImportRequest,
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> ImportResultResponse:
    """Promote payment-evidence rows from staging_strata_web_snapshots to Demo Bank.

    GUARD: Only dated payment movements are accepted. Balance snapshots, arrears
    totals, and budget figures are rejected and logged as warnings.

    This endpoint does NOT write any values to the financial ledger.
    """
    from database import db

    _require_role(current_user, _ADMIN_ONLY, "Super admin role required for Strata Web import")

    try:
        result = await import_strata_web_snapshot(
            db=db,
            building_id=building_id,
            financial_year=body.financial_year,
            account_ref=body.account_ref,
            is_test_data=body.is_test_data,
        )
    except Exception as exc:
        logger.exception("Demo Bank Strata Web import error (building=%s): %s", building_id, exc)
        raise HTTPException(status_code=500, detail=f"Strata Web import failed: {exc}")

    logger.info(
        "Demo Bank Strata Web import: building=%s year=%s account=%s imported=%d skipped=%d",
        building_id, body.financial_year, body.account_ref,
        result["imported_count"], result["skipped_count"],
    )
    return ImportResultResponse(**result)


# ── Strata Web balance-delta inference (candidate generation only) ───────────

class StrataWebInferCandidatesRequest(BaseModel):
    financial_year: str
    current_snapshot_id: Optional[str] = None


@router.post("/strata-web/infer-candidates")
async def infer_strata_web_balance_delta_candidates(
    body: StrataWebInferCandidatesRequest,
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> dict:
    """Infer candidate payment transactions from consecutive Strata Web snapshots.

    GAP-FIN-015 blocker 4: this compares two consecutive staging_strata_web_snapshots
    documents and writes CANDIDATE Demo Bank transactions (requires_review=True) — it
    NEVER writes to unit_levy_ledger directly, and candidates still have to pass
    through the normal matching/review/promotion path like any other input. On-demand
    only (manager-triggered) — not wired into any scheduler.
    """
    from database import db
    from services.strata_web_balance_inference_service import (
        derive_strata_web_balance_delta_transactions,
    )

    _require_role(current_user, _MANAGER_ROLES, "Manager role required for balance-delta inference")

    try:
        result = await derive_strata_web_balance_delta_transactions(
            db=db,
            building_id=building_id,
            financial_year=body.financial_year,
            current_snapshot_id=body.current_snapshot_id,
        )
    except Exception as exc:
        logger.exception("Strata Web balance-delta inference error (building=%s): %s", building_id, exc)
        raise HTTPException(status_code=500, detail=f"Balance-delta inference failed: {exc}")

    logger.info(
        "Strata Web balance-delta inference: building=%s year=%s created=%d skipped=%d",
        building_id, body.financial_year, result["candidates_created"], result["candidates_skipped"],
    )
    return result


# ── Manual transaction injection ──────────────────────────────────────────────

@router.post("/transactions/manual")
async def inject_manual_endpoint(
    body: ManualTransactionRequest,
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> dict:
    """Inject a single manually-specified bank transaction (super_admin only).

    Uses the same idempotency mechanism as CSV import — submitting the same
    transaction twice is a safe no-op.
    """
    from database import db

    _require_role(current_user, _ADMIN_ONLY, "Super admin role required for manual transaction injection")

    try:
        result = await inject_manual(
            db=db,
            building_id=building_id,
            req=body,
            injected_by=str(current_user.get("id") or current_user.get("_id") or "unknown"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "Demo Bank manual inject: building=%s account=%s amount=%d direction=%s upserted=%s",
        building_id, body.account_ref, body.amount_cents, body.direction, result["upserted"],
    )
    return result


# ── Account list ──────────────────────────────────────────────────────────────

@router.get("/accounts", response_model=list[DemoBankAccountResponse])
async def list_accounts(
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> list[DemoBankAccountResponse]:
    """List all Demo Bank accounts for the current building."""
    from database import db

    _require_role(current_user, _MANAGER_ROLES, "Manager role required")

    accounts = await db._db.demo_bank_accounts.find(
        {"building_id": building_id, "is_test_data": {"$ne": True}}
    ).to_list(length=50)

    return [DemoBankAccountResponse(**_json_safe_doc(a)) for a in accounts]


# ── Account balance ───────────────────────────────────────────────────────────

@router.get("/accounts/{account_ref}/balance", response_model=DemoBankBalanceResponse)
async def get_account_balance(
    account_ref: str,
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> DemoBankBalanceResponse:
    """Return current balance for a Demo Bank account."""
    from database import db

    _require_role(current_user, _MANAGER_ROLES, "Manager role required")

    account = await db._db.demo_bank_accounts.find_one(
        {"building_id": building_id, "account_ref": account_ref}
    )
    if not account:
        raise HTTPException(status_code=404, detail=f"Account {account_ref!r} not found")

    return DemoBankBalanceResponse(
        account_ref=account["account_ref"],
        account_name=account.get("account_name", ""),
        current_balance_cents=account.get("current_balance_cents", 0),
        opening_balance_cents=account.get("opening_balance_cents", 0),
        currency=account.get("currency", "AUD"),
        as_of=account.get("updated_at", account.get("created_at")),
    )


# ── Transaction list ──────────────────────────────────────────────────────────

@router.get("/accounts/{account_ref}/transactions", response_model=TransactionListResponse)
async def list_transactions(
    account_ref: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    direction: Optional[str] = Query(default=None, description="Filter: credit | debit"),
    sync_status: Optional[str] = Query(default=None, description="Filter: pending | synced | failed | ignored"),
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> TransactionListResponse:
    """Return paginated Demo Bank transactions for an account."""
    from database import db

    _require_role(current_user, _MANAGER_ROLES, "Manager role required")

    query: dict = {
        "building_id": building_id,
        "account_ref": account_ref,
        "is_test_data": {"$ne": True},
    }
    if direction in ("credit", "debit"):
        query["direction"] = direction
    if sync_status in ("pending", "synced", "failed", "ignored"):
        query["sync_status"] = sync_status

    total = await db._db.demo_bank_transactions.count_documents(query)
    skip = (page - 1) * page_size

    cursor = (
        db._db.demo_bank_transactions.find(query)
        .sort("posted_date", -1)
        .skip(skip)
        .limit(page_size)
    )
    docs = await cursor.to_list(length=page_size)

    transactions = [DemoBankTransactionResponse(**_json_safe_doc(d)) for d in docs]
    return TransactionListResponse(
        building_id=building_id,
        account_ref=account_ref,
        transactions=transactions,
        total=total,
        page=page,
        page_size=page_size,
    )


# ── Import batch audit log ────────────────────────────────────────────────────

@router.get("/import-batches", response_model=list[ImportBatchResponse])
async def list_import_batches(
    limit: int = Query(default=50, ge=1, le=200),
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> list[ImportBatchResponse]:
    """Return recent Demo Bank import batches for audit purposes."""
    from database import db

    _require_role(current_user, _MANAGER_ROLES, "Manager role required")

    batches = await (
        db._db.demo_bank_import_batches.find(
            {"building_id": building_id, "is_test_data": {"$ne": True}}
        )
        .sort("created_at", -1)
        .to_list(length=limit)
    )
    return [ImportBatchResponse(**_json_safe_doc(b)) for b in batches]


@router.get("/import-batches/{batch_id}", response_model=ImportBatchResponse)
async def get_import_batch(
    batch_id: str,
    building_id: str = Depends(get_current_building),
    current_user: dict = Depends(require_feature("demo_bank_feed_enabled")),
) -> ImportBatchResponse:
    """Return a single import batch by ID."""
    from database import db

    _require_role(current_user, _MANAGER_ROLES, "Manager role required")

    try:
        oid = ObjectId(batch_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid batch_id")

    batch = await db._db.demo_bank_import_batches.find_one(
        {"_id": oid, "building_id": building_id}
    )
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found")

    return ImportBatchResponse(**_json_safe_doc(batch))


# ── Supported banks ───────────────────────────────────────────────────────────

@router.get("/supported-banks")
async def list_supported_banks(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """List bank schema names available for CSV upload."""
    from pathlib import Path
    schema_dir = Path(__file__).parent.parent / "integrations" / "mocks" / "bank_schemas"
    banks = sorted(p.stem for p in schema_dir.glob("*.yaml"))
    return {"banks": banks}
