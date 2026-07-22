"""
backend/integrations/demo_bank/provider.py

# @featuretrace:demo_bank — DemoBankFeed: BankFeedProvider implementation backed by MongoDB.
# Layer: service
# Data flow: demo_bank_transactions (Mongo) → pull_transactions() → BankTxObserved → MatchingEngine.
# Related: backend/integrations/protocols.py (BankFeedProvider contract)
#          backend/integrations/envelopes.py (BankTxObserved)
#          backend/integrations/demo_bank/ingestion.py (writes demo_bank_transactions)
#          backend/integrations/registry.py (registration)
# Toggle: demo_bank_feed_enabled
# Collection: demo_bank_transactions, demo_bank_accounts
# Tests: tests/backend/test_demo_bank_provider.py

Signed-amount contract:
  demo_bank_transactions.direction = "credit" → BankTxObserved.amount_cents > 0
  demo_bank_transactions.direction = "debit"  → BankTxObserved.amount_cents < 0

The MatchingEngine, reconciliation layer, and FinancialCoreService all rely on this
convention. DemoBankFeed enforces it here; ingestion.py stores the raw absolute value.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from integrations.envelopes import BankAccountMetadata, BankTxObserved
from strataos_demo_integrations.demo_bank.schemas import PROVIDER

logger = logging.getLogger(__name__)

# Signal extraction patterns (same as ingestion.py — no shared state needed)
_CRN_RE = re.compile(r"\b(\d{13})\b")
_E2E_RE = re.compile(r"\b(LVY-[A-Z0-9]{4,10}-[A-Z0-9]{2,6}-\d{1,4})\b", re.IGNORECASE)
_LOT_RE = re.compile(
    r"\b(?:UNIT|LOT|APT|APARTMENT|TH|TOWNHOUSE|VILLA|PENTHOUSE)\s*(\d{1,4})\b",
    re.IGNORECASE,
)
_MOD10V05_WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4]


def _validate_crn(crn: str) -> bool:
    if len(crn) != 13 or not crn.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(reversed(crn[:12]), _MOD10V05_WEIGHTS))
    return ((10 - (total % 10)) % 10) == int(crn[12])


def _extract_signals(description: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    crn: Optional[str] = None
    e2e: Optional[str] = None
    lot: Optional[str] = None
    for m in _CRN_RE.finditer(description):
        if _validate_crn(m.group(1)):
            crn = m.group(1)
            break
    e2e_m = _E2E_RE.search(description)
    if e2e_m:
        e2e = e2e_m.group(1).upper()
    lot_m = _LOT_RE.search(description)
    if lot_m:
        lot = lot_m.group(0).upper()
    return crn, e2e, lot


def _signed_amount(amount_cents: int, direction: str) -> int:
    """Apply the signed-amount contract using normalized provider direction.

    Only an explicit debit/outgoing direction is negative. Unknown legacy values
    default to credit because historical levy receipts are incoming funds.
    """
    abs_amount = abs(amount_cents)
    direction_norm = str(direction or "credit").strip().lower()
    return -abs_amount if direction_norm in {"debit", "expense", "payment_out"} else abs_amount


def _posted_date_since_filter(since: datetime) -> dict:
    """Match both current Date values and legacy YYYY-MM-DD string dates."""
    return {
        "$or": [
            {"posted_date": {"$gte": since}},
            {"posted_date": {"$type": "string", "$gte": since.date().isoformat()}},
        ]
    }


def _syncable_transaction_filter(since: datetime) -> dict:
    """Match provider-visible rows without widening live production scope.

    Current Demo Bank rows carry status="posted"/"pending". Some historical
    East Gate staging rows predate that field, so missing status is allowed only
    after the caller has already constrained building_id/account_ref/is_test_data.

    "synthetic_from_budget" rows are excluded unconditionally (East Gate 13195
    financial-corruption investigation, 2026-07-22): they are fabricated
    payments computed as unit_uoe x levy_per_uoe_quarterly with a randomized
    payment date (see historical_levy_reconstruction_service.py in the
    strata-management repo), not an observed bank movement. Demo Bank's
    contract is to stage cash movements a real or emulated bank actually
    reported; a budget-derived assumption is not that, regardless of how
    confident the reconstruction is. Never widen this back to "$exists: False"
    catching them by accident — every synthetic_from_budget row is expected to
    set source_type explicitly.
    """
    return {
        "$and": [
            _posted_date_since_filter(since),
            {"$or": [
                {"status": {"$in": ["posted", "pending"]}},
                {"status": {"$exists": False}},
            ]},
            {"source_type": {"$ne": "synthetic_from_budget"}},
        ]
    }


class DemoBankFeed:
    """BankFeedProvider backed by MongoDB demo_bank_transactions collection.

    Registered as "demo_bank_feed" in the ProviderRegistry. Used by East Gate (13195)
    and Acme Demo (UP-DEMO-001) buildings. New buildings without a preference setting
    continue to use the default csv_upload_bank_feed.

    pull_transactions() reads demo_bank_transactions filtered by account_ref and since
    timestamp, applies the signed-amount conversion, and yields BankTxObserved envelopes
    identical in shape to those produced by CsvUploadBankFeed. The MatchingEngine and
    all downstream consumers see no difference between providers.
    """

    name = PROVIDER  # "demo_bank_feed"

    async def pull_transactions(
        self,
        account_ref: str,
        since: datetime,
    ) -> AsyncIterator[BankTxObserved]:
        """Yield production BankTxObserved rows for one account since `since`.

        Current rows must be posted/pending. Legacy historical rows with no
        status are also included after building/account scoping. Test data is
        always excluded on this production path.
        """
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()

        # Normalise since to a timezone-aware datetime
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        cursor = db._db.demo_bank_transactions.find(
            {
                "building_id": building_id,
                "account_ref": account_ref,
                **_syncable_transaction_filter(since),
                "is_test_data": {"$ne": True},
            }
        ).sort("posted_date", 1)

        async for doc in cursor:
            try:
                yield self._doc_to_envelope(doc)
            except Exception as exc:
                logger.warning(
                    "DemoBankFeed: skipping malformed transaction doc _id=%s: %s",
                    doc.get("_id"),
                    exc,
                )

    async def pull_transactions_include_test(
        self,
        account_ref: str,
        since: datetime,
        building_id: str,
    ) -> AsyncIterator[BankTxObserved]:
        """Test-only variant that includes is_test_data rows.

        Used by seed scripts and integration tests. Production code must use
        pull_transactions() which excludes test data.
        """
        from database import db

        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        cursor = db._db.demo_bank_transactions.find(
            {
                "building_id": building_id,
                "account_ref": account_ref,
                **_syncable_transaction_filter(since),
            }
        ).sort("posted_date", 1)

        async for doc in cursor:
            try:
                yield self._doc_to_envelope(doc)
            except Exception as exc:
                logger.warning(
                    "DemoBankFeed(test): skipping malformed doc _id=%s: %s",
                    doc.get("_id"),
                    exc,
                )

    def _doc_to_envelope(self, doc: dict) -> BankTxObserved:
        """Convert a demo_bank_transactions document to a BankTxObserved envelope.

        Applies the signed-amount contract. Extracts BPAY/NPP/lot signals from
        description if not already stored on the document.
        """
        amount_cents = _signed_amount(
            amount_cents=int(doc["amount_cents"]),
            direction=doc.get("direction", "credit"),
        )

        # Use stored signals if present; fall back to description extraction
        bpay_crn = doc.get("bpay_crn")
        osko_e2e_id = doc.get("osko_e2e_id")
        lot_ref_raw = doc.get("lot_ref_raw")
        if not any([bpay_crn, osko_e2e_id, lot_ref_raw]):
            bpay_crn, osko_e2e_id, lot_ref_raw = _extract_signals(doc.get("description", ""))

        posted_date = doc["posted_date"]
        if isinstance(posted_date, str):
            posted_date = datetime.fromisoformat(posted_date)
        if posted_date.tzinfo is None:
            posted_date = posted_date.replace(tzinfo=timezone.utc)

        # Phase 0B provenance contract: populate the envelope's optional metadata
        # field whenever the Mongo document carries reconstruction provenance, so
        # strata-management's bank_feeds.py no longer strictly needs its Mongo
        # side-channel lookup to preserve it — this provider now emits everything
        # a reconstruction-aware consumer needs directly in BankTxObserved.
        metadata: Optional[dict] = None
        if doc.get("transaction_origin") or doc.get("reconstruction_batch_id"):
            metadata = {
                "transaction_origin": doc.get("transaction_origin"),
                "reconstruction_batch_id": doc.get("reconstruction_batch_id"),
                "reconstruction_version": doc.get("reconstruction_version"),
                "assumption_code": doc.get("assumption_code"),
                "levy_component": doc.get("levy_component"),
                "source_document_ids": doc.get("source_snapshot_ids") or [],
            }

        return BankTxObserved(
            provider_txn_id=doc["external_transaction_id"],
            tenant_id=doc["building_id"],
            account_ref=doc["account_ref"],
            occurred_at=posted_date,
            amount_cents=amount_cents,
            description=doc.get("description", ""),
            balance_after_cents=doc.get("running_balance_cents"),
            bpay_crn=bpay_crn,
            osko_e2e_id=osko_e2e_id,
            lot_ref_raw=lot_ref_raw,
            is_test_data=bool(doc.get("is_test_data", False)),
            metadata=metadata,
        )

    async def verify_webhook(
        self,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> bool:
        # Demo Bank has no real webhook path. Future: validate HMAC signature
        # from a configured webhook secret when simulating bank-push events.
        return False

    async def parse_webhook(
        self,
        raw_body: bytes,
    ) -> list:
        # Demo Bank webhook simulation is handled via POST /demo-bank/webhooks/replay
        # (Phase 2). The provider itself returns empty list here.
        return []

    async def list_accounts(
        self,
        consent_id: str,
    ) -> list[BankAccountMetadata]:
        """Return account metadata for the current building context."""
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        accounts = await db._db.demo_bank_accounts.find(
            {
                "building_id": building_id,
                "status": "active",
                "is_test_data": {"$ne": True},
            }
        ).to_list(length=50)

        return [
            BankAccountMetadata(
                account_ref=a["account_ref"],
                bsb=a.get("bsb", "000-000"),
                account_number=a.get("account_number_masked", ""),
                account_name=a.get("account_name", ""),
                institution_name="Demo Bank",
                currency=a.get("currency", "AUD"),
                tenant_id=a["building_id"],
            )
            for a in accounts
        ]
