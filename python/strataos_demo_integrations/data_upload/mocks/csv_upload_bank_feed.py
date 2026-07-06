"""
backend/integrations/mocks/csv_upload_bank_feed.py — Mock BankFeedProvider.

# @featuretrace:financial_integration_v2 — mock CSV/OFX/QIF bank feed ingestion.
# Layer: domain
# Data flow: treasurer CSV upload → parse → BankTxObserved → integration_inbox (building-scoped).
# Related: backend/integrations/mocks/routers/bank_feed_router.py
#          backend/integrations/envelopes.py
#          backend/integrations/matching/engine.py

Provides the `csv_upload_bank_feed` provider. Accepts monthly bank statement
exports from any Australian bank whose schema is defined in bank_schemas/.
Normalises every row into a BankTxObserved envelope with a deterministic
provider_txn_id so re-uploading the same file is idempotent.

Regex extraction:
  - 13-digit BPAY CRN (MOD10V05 check digit validated before setting bpay_crn)
  - NPP End-to-End IDs matching the pattern LVY-XXXXXX-XXX-N or similar
  - Lot references: "UNIT N", "LOT N", "APT N", "TH N" etc.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

import yaml

from integrations.envelopes import BankAccountMetadata, BankTxObserved

logger = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).parent / "bank_schemas"

# ── Regex patterns ────────────────────────────────────────────────────────────

_CRN_RE = re.compile(r"\b(\d{13})\b")
_E2E_RE = re.compile(r"\b(LVY-[A-Z0-9]{4,10}-[A-Z0-9]{2,6}-\d{1,4})\b", re.IGNORECASE)
_LOT_RE = re.compile(
    r"\b(?:UNIT|LOT|APT|APARTMENT|TH|TOWNHOUSE|VILLA|PENTHOUSE)\s*(\d{1,4})\b",
    re.IGNORECASE,
)

# MOD10V05 weights applied right-to-left to the 12 base digits
_MOD10V05_WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4]


def _validate_crn_checksum(crn: str) -> bool:
    if len(crn) != 13 or not crn.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(reversed(crn[:12]), _MOD10V05_WEIGHTS))
    expected = (10 - (total % 10)) % 10
    return expected == int(crn[12])


def _extract_signals(description: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (bpay_crn, osko_e2e_id, lot_ref_raw) from a transaction description."""
    crn: Optional[str] = None
    e2e: Optional[str] = None
    lot: Optional[str] = None

    for m in _CRN_RE.finditer(description):
        if _validate_crn_checksum(m.group(1)):
            crn = m.group(1)
            break

    e2e_m = _E2E_RE.search(description)
    if e2e_m:
        e2e = e2e_m.group(1).upper()

    lot_m = _LOT_RE.search(description)
    if lot_m:
        lot = lot_m.group(0).upper()

    return crn, e2e, lot


def _provider_txn_id(account_ref: str, date_str: str, amount_cents: int,
                     description: str, balance_str: str) -> str:
    """Deterministic SHA-256 txn ID — re-uploading the same file is idempotent."""
    payload = f"{account_ref}|{date_str}|{amount_cents}|{description}|{balance_str}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_schema(bank_name: str) -> Optional[dict]:
    path = _SCHEMA_DIR / f"{bank_name}.yaml"
    if not path.exists():
        return None
    with path.open() as f:
        return yaml.safe_load(f)


def _cents(value: str) -> int:
    """Parse a monetary string to integer cents. Strips $, commas, spaces."""
    cleaned = value.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not cleaned or cleaned == "-":
        return 0
    return int(round(float(cleaned) * 100))


def _parse_date(date_str: str, fmt: str) -> datetime:
    return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)


def parse_csv_rows(
        content: str,
        schema: dict,
        account_ref: str,
        tenant_id: str,
        is_test_data: bool = False,
) -> list[BankTxObserved]:
    """Parse CSV content using a bank schema into BankTxObserved envelopes."""
    skip = schema.get("skip_rows", 1)
    date_fmt = schema.get("date_format", "%d/%m/%Y")
    amount_mode = schema.get("amount_mode", "signed")
    cols: dict = schema.get("columns", {})
    # amount_derivation may be a prose string in the YAML (documentation only)
    # or a dict with explicit column-key overrides. Treat non-dict values as empty.
    _raw_derivation = schema.get("amount_derivation", {})
    amount_derivation: dict = _raw_derivation if isinstance(_raw_derivation, dict) else {}

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)

    result: list[BankTxObserved] = []
    for row in rows[skip:] if skip > 0 else rows:
        # Skip completely empty rows
        if not any(v and v.strip() for v in row.values()):
            continue

        try:
            date_col = cols.get("date", "Date")
            date_str = row.get(date_col, "").strip()
            if not date_str:
                continue
            occurred_at = _parse_date(date_str, date_fmt)

            description_col = cols.get("description", "Description")
            description = row.get(description_col, "").strip()

            balance_col = cols.get("balance", "Balance")
            balance_raw = row.get(balance_col, "").strip()
            balance_cents: Optional[int] = _cents(balance_raw) if balance_raw else None

            # Amount calculation depends on the bank schema's amount_mode
            if amount_mode == "signed":
                amount_col = cols.get("amount", "Amount")
                raw_val = row.get(amount_col, "0")
                amount_cents = _cents(raw_val)
            else:
                # Split debit/credit columns
                debit_col = amount_derivation.get("debit_col", cols.get("debit", "Debit"))
                credit_col = amount_derivation.get("credit_col", cols.get("credit", "Credit"))
                debit_raw = row.get(debit_col, "").strip()
                credit_raw = row.get(credit_col, "").strip()
                debit_cents = _cents(debit_raw) if debit_raw else 0
                credit_cents = _cents(credit_raw) if credit_raw else 0
                # Debit = money out (negative), Credit = money in (positive)
                amount_cents = credit_cents - debit_cents

            bpay_crn, e2e_id, lot_ref = _extract_signals(description)

            txn_id = _provider_txn_id(
                account_ref, date_str, amount_cents, description, balance_raw
            )

            result.append(BankTxObserved(
                provider_txn_id=txn_id,
                tenant_id=tenant_id,
                account_ref=account_ref,
                occurred_at=occurred_at,
                amount_cents=amount_cents,
                description=description,
                balance_after_cents=balance_cents,
                bpay_crn=bpay_crn,
                osko_e2e_id=e2e_id,
                lot_ref_raw=lot_ref,
                is_test_data=is_test_data,
            ))
        except Exception as exc:
            logger.warning("Skipping malformed CSV row %r: %s", row, exc)

    return result


class CsvUploadBankFeed:
    """Mock BankFeedProvider backed by treasurer CSV/OFX/QIF uploads.

    name = "csv_upload_bank_feed"

    This is the default provider for all buildings. A building switches to
    Basiq or another real provider by updating its integration_provider_preference
    setting — no code change required.
    """

    name = "csv_upload_bank_feed"

    async def pull_transactions(
            self,
            account_ref: str,
            since: datetime,
    ) -> AsyncIterator[BankTxObserved]:
        """Pull from integration_inbox for this account_ref since a timestamp.

        Unlike real bank-feed providers (which call an external API), the CSV
        feed populates the inbox at upload time. pull_transactions reads back
        from the inbox so the interface is consistent across all providers.
        """
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        cursor = db.integration_inbox.find(
            {
                "tenant_id": building_id,
                "event_type": "bank_tx_observed",
                "occurred_at": {"$gte": since.isoformat() if hasattr(since, "isoformat") else since},
                "account_ref": account_ref,
            }
        ).sort("occurred_at", 1)

        async for doc in cursor:
            yield BankTxObserved(**doc["payload"])

    async def verify_webhook(
            self,
            headers: dict[str, str],
            raw_body: bytes,
    ) -> bool:
        # CSV upload provider has no webhook path — always False.
        return False

    async def parse_webhook(
            self,
            raw_body: bytes,
    ) -> list:
        return []

    async def list_accounts(
            self,
            consent_id: str,
    ) -> list[BankAccountMetadata]:
        # CSV upload accounts are configured per-building in trust_accounts.
        # Return empty list; the upload UI does not require listing first.
        return []

    # ── Upload helpers (used by the bank_feed_router) ─────────────────────────

    def parse_csv_bytes(
            self,
            content: bytes,
            bank_name: str,
            account_ref: str,
            tenant_id: str,
            is_test_data: bool = False,
    ) -> list[BankTxObserved]:
        """Parse raw CSV bytes using the named bank schema."""
        schema = _load_schema(bank_name)
        if schema is None:
            raise ValueError(
                f"No schema for bank {bank_name!r}. "
                f"Available: {[p.stem for p in _SCHEMA_DIR.glob('*.yaml')]}"
            )
        text = content.decode("utf-8-sig", errors="replace")
        return parse_csv_rows(text, schema, account_ref, tenant_id, is_test_data)

    def supported_banks(self) -> list[str]:
        return [p.stem for p in sorted(_SCHEMA_DIR.glob("*.yaml"))]
