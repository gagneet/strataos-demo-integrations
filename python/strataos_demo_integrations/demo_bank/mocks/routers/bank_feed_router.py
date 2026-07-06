"""
backend/integrations/mocks/routers/bank_feed_router.py — CSV bank feed upload endpoint.

# @featuretrace:financial_integration_v2 — mock bank feed HTTP ingestion.
# Layer: router
# Data flow: treasurer CSV upload → parse → integration_inbox → matching engine (building-scoped).
# Related: csv_upload_bank_feed.py (parser), envelopes.py (BankTxObserved).

Mounted at /api/integrations/mock/bank-feed/ when financial_integration_layer_v2
feature toggle is enabled for the building.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pymongo.errors import BulkWriteError
from pydantic import BaseModel

from integrations.envelopes import compute_idempotency_key
from strataos_demo_integrations.data_upload.mocks.csv_upload_bank_feed import CsvUploadBankFeed
from utils.auth import get_current_building, get_current_user
from utils.permissions import require_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/mock/bank-feed", tags=["Mock Bank Feed"])

_feed = CsvUploadBankFeed()


class BankFeedUploadResponse(BaseModel):
    accepted: int
    duplicate: int
    errors: int
    bank: str
    account_ref: str


@router.post("/upload", response_model=BankFeedUploadResponse)
async def upload_bank_statement(
        file: UploadFile = File(...),
        bank: str = Form(..., description="Bank schema name (e.g. 'cba', 'nab', 'westpac')"),
        account_ref: str = Form(..., description="Trust bank account internal reference"),
        building_id: str = Depends(get_current_building),
        current_user: dict = Depends(require_feature("financial_integration_layer_v2")),
) -> BankFeedUploadResponse:
    """Upload a CSV bank statement for the mock bank feed provider.

    Parses the statement using the named bank schema, converts rows to
    BankTxObserved envelopes, and inserts them into integration_inbox.
    Re-uploading the same file is idempotent — duplicates are silently skipped.
    """
    from database import db

    from utils.auth import effective_role as _effective_role
    _role = _effective_role(current_user)
    if _role not in {"super_admin", "strata_admin", "ec_member", "strata_manager"}:
        raise HTTPException(status_code=403, detail="Strata manager or EC member role required")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10 MB guard
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    try:
        txns = _feed.parse_csv_bytes(content, bank, account_ref, building_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    now = datetime.now(timezone.utc)
    docs = []
    for tx in txns:
        raw_payload = {
            "provider_txn_id": tx.provider_txn_id,
            "account_ref": tx.account_ref,
            "occurred_at": tx.occurred_at.isoformat(),
            "amount_cents": tx.amount_cents,
            "description": tx.description,
        }
        docs.append({
            "tenant_id": building_id,
            "idempotency_key": compute_idempotency_key(building_id, raw_payload),
            "provider_event_id": tx.provider_txn_id,
            "event_type": "bank_tx_observed",
            "occurred_at": tx.occurred_at.isoformat(),
            "received_at": now.isoformat(),
            "account_ref": account_ref,
            "status": "pending",
            "payload": tx.model_dump(mode="json"),
        })

    # Bulk insert — ordered=False lets MongoDB continue past duplicate-key errors.
    # One round trip instead of N, regardless of statement size.
    accepted = 0
    duplicate = 0
    errors = 0
    if docs:
        try:
            result = await db.integration_inbox.insert_many(docs, ordered=False)
            accepted = len(result.inserted_ids)
        except BulkWriteError as bwe:
            for err in bwe.details.get("writeErrors", []):
                if err.get("code") == 11000:
                    duplicate += 1
                else:
                    logger.warning("Bulk insert error for tx: %s", err)
                    errors += 1
            accepted = bwe.details.get("nInserted", 0)

    logger.info(
        "Bank statement upload complete: building=%s bank=%s accepted=%d duplicate=%d errors=%d",
        building_id, bank, accepted, duplicate, errors,
    )
    return BankFeedUploadResponse(
        accepted=accepted,
        duplicate=duplicate,
        errors=errors,
        bank=bank,
        account_ref=account_ref,
    )


@router.get("/supported-banks")
async def list_supported_banks(
        current_user: dict = Depends(get_current_user),
) -> dict:
    """List all bank schemas available for CSV upload."""
    return {"banks": _feed.supported_banks()}
