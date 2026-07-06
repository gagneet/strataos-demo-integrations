"""
backend/integrations/mocks/mock_biller.py — Mock BillerProvider.

# @featuretrace:financial_integration_v2 — mock CRN allocation and payment feedback.
# Layer: service
# Data flow: levy raise → allocate_reference → CRN → levy notice → owner payment reference (building-scoped).
# Related: backend/integrations/protocols.py
#          backend/routers/finance.py

Generates 13-digit CRNs using MOD10V05 (positional weighting), structured as:
  [scheme_id:6][lot_id:3][instalment_seq:3][check_digit:1]

scheme_id derivation from building_id:
  - Numeric IDs (e.g. "13195"): last 6 digits, zero-padded left → "013195"
  - Non-numeric IDs (e.g. "harbourside_view"): first 6 decimal digits of
    SHA-256 of the string, padded to 6 → deterministic, collision-resistant.

Allocations are persisted in mock_biller_allocations with a unique index on
(tenant_id, scheme_id, lot_id, instalment_seq) so the same (scheme, lot,
instalment) always returns the same CRN — idempotent across restarts.

DEFT feedback simulation: when the mock bank feed ingests a transaction whose
description contains a known CRN, the mock biller emits a BillerPaymentObserved
envelope on the next pull_payment_feedback call.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from integrations.envelopes import (
    BillerBatchRecord,
    BillerPaymentObserved,
    BillerReference,
    generate_ulid,
)

logger = logging.getLogger(__name__)

# MOD10V05 positional weights applied right-to-left across the 12 base digits
_WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4]

# Shared BPAY biller code for the mock provider
_MOCK_BILLER_CODE = "000001"


def _scheme_id_from_building(building_id: str) -> str:
    """Derive a deterministic 6-digit numeric scheme_id from a building_id."""
    digits = "".join(c for c in building_id if c.isdigit())
    if digits:
        # Use last 6 numeric digits (right-anchored) and zero-pad left
        return digits[-6:].zfill(6)
    # Non-numeric: deterministic hash → pick first 6 decimal digits of SHA-256
    h = hashlib.sha256(building_id.encode()).hexdigest()
    decimal_digits = "".join(c for c in h if c.isdigit())
    return decimal_digits[:6].ljust(6, "0")


def compute_mod10v05_check(twelve_digits: str) -> str:
    """Return the single MOD10V05 check digit for a 12-digit base string."""
    total = sum(int(d) * w for d, w in zip(reversed(twelve_digits), _WEIGHTS))
    return str((10 - (total % 10)) % 10)


def validate_mod10v05(crn: str) -> bool:
    """Return True iff crn is a valid 13-digit MOD10V05 CRN."""
    if len(crn) != 13 or not crn.isdigit():
        return False
    return compute_mod10v05_check(crn[:12]) == crn[12]


def build_crn(scheme_id: str, lot_id: str, instalment_seq: int) -> str:
    """Construct a 13-digit MOD10V05 CRN."""
    # Extract numeric component of lot_id; zero-pad to 3 digits
    lot_digits = "".join(c for c in lot_id if c.isdigit())
    lot_part = lot_digits[-3:].zfill(3) if lot_digits else "000"
    inst_part = str(instalment_seq % 1000).zfill(3)
    base = scheme_id[:6].zfill(6) + lot_part + inst_part  # 12 chars
    return base + compute_mod10v05_check(base)


class MockBiller:
    """Mock BillerProvider using MOD10V05 CRNs and MongoDB-persisted allocations.

    name = "mock_biller"
    """

    name = "mock_biller"

    async def allocate_reference(
            self,
            scheme_id: str,
            lot_id: str,
            instalment_seq: int,
    ) -> BillerReference:
        """Return (or create) the CRN for a given scheme/lot/instalment triple.

        Idempotent: the same inputs always produce the same CRN, persisted in
        mock_biller_allocations so the allocation survives server restarts.
        """
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        numeric_scheme = _scheme_id_from_building(scheme_id)
        crn = build_crn(numeric_scheme, lot_id, instalment_seq)

        existing = await db.mock_biller_allocations.find_one(
            {
                "tenant_id": building_id,
                "scheme_id": numeric_scheme,
                "lot_id": lot_id,
                "instalment_seq": instalment_seq,
            }
        )
        if not existing:
            await db.mock_biller_allocations.insert_one(
                {
                    "tenant_id": building_id,
                    "scheme_id": numeric_scheme,
                    "lot_id": lot_id,
                    "instalment_seq": instalment_seq,
                    "crn": crn,
                    "biller_code": _MOCK_BILLER_CODE,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        display = (
            f"{crn[:7]} {crn[7:10]} {crn[10:12]} {crn[12]}"
        )
        return BillerReference(
            crn=crn,
            biller_code=_MOCK_BILLER_CODE,
            check_digit_algorithm="MOD10V05",
            display_format=display,
            scheme_id=numeric_scheme,
            lot_id=lot_id,
            instalment_seq=instalment_seq,
            tenant_id=building_id,
        )

    async def pull_payment_feedback(
            self,
            since: datetime,
    ) -> AsyncIterator[BillerPaymentObserved]:
        """Yield BillerPaymentObserved for any inbox tx whose description has a known CRN.

        This simulates the DEFT TXN-file feedback loop: when the mock bank feed
        ingests a transaction with a CRN in its description, this method discovers
        it and emits the per-lot decomposition on the next poll.
        """
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()

        # Find all known CRNs for this building
        alloc_cursor = db.mock_biller_allocations.find({"tenant_id": building_id})
        known_crns: dict[str, dict] = {}
        async for alloc in alloc_cursor:
            known_crns[alloc["crn"]] = alloc

        if not known_crns:
            return

        # Find inbox events since the watermark that contain a known CRN
        cursor = db.integration_inbox.find(
            {
                "tenant_id": building_id,
                "event_type": "bank_tx_observed",
                "occurred_at": {"$gte": since.isoformat()},
                "payload.bpay_crn": {"$in": list(known_crns)},
                "biller_feedback_emitted": {"$ne": True},
            }
        ).sort("occurred_at", 1)

        async for doc in cursor:
            payload = doc.get("payload", {})
            crn = payload.get("bpay_crn")
            alloc = known_crns.get(crn)
            if not alloc:
                continue

            try:
                occurred_at = datetime.fromisoformat(payload["occurred_at"])
            except (KeyError, ValueError):
                occurred_at = datetime.now(timezone.utc)

            yield BillerPaymentObserved(
                provider_feedback_id=generate_ulid(),
                crn=crn,
                lot_id=alloc["lot_id"],
                tenant_id=building_id,
                occurred_at=occurred_at,
                amount_cents=payload.get("amount_cents", 0),
                channel="BPAY",
                is_reversal=False,
                is_test_data=payload.get("is_test_data", False),
            )

            # Mark this inbox event as having feedback emitted (idempotency)
            await db.integration_inbox.update_one(
                {"_id": doc["_id"]},
                {"$set": {"biller_feedback_emitted": True}},
            )

    async def reconcile_batch(
            self,
            batch_date: datetime,
    ) -> BillerBatchRecord:
        """Sum all CRN-matched payments for the given date as a consolidated batch."""
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        date_str = batch_date.date().isoformat()

        pipeline = [
            {
                "$match": {
                    "tenant_id": building_id,
                    "event_type": "bank_tx_observed",
                    "occurred_at": {
                        "$gte": f"{date_str}T00:00:00+00:00",
                        "$lt": f"{date_str}T23:59:59+00:00",
                    },
                    "payload.bpay_crn": {"$exists": True, "$ne": None},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": "$payload.amount_cents"},
                    "count": {"$sum": 1},
                }
            },
        ]
        rows = await db.integration_inbox.aggregate(pipeline).to_list(1)
        agg = rows[0] if rows else {"total": 0, "count": 0}

        return BillerBatchRecord(
            batch_date=batch_date,
            total_amount_cents=agg["total"],
            tenant_id=building_id,
            provider_batch_id=f"MOCK-{date_str}",
            transaction_count=agg["count"],
        )
