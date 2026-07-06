"""
backend/integrations/mocks/mock_aba_writer.py — Mock PaymentInitiationProvider.

# @featuretrace:financial_integration_v2 — mock Cemtex ABA file generator.
# Layer: service
# Data flow: approved invoices → PaymentRun → ABA file → trust bank upload artifact (building-scoped).
# Related: backend/integrations/protocols.py
#          backend/domain/invoice_lifecycle.py

Generates byte-perfect Cemtex Direct Entry (ABA) files:
  - Record type 0 (Descriptive): 120 chars
  - Record type 1 (Detail): 120 chars per disbursement
  - Record type 7 (File Total): 120 chars

ABA specification reference: APCA Australian Payments Clearing Association,
Direct Entry User's Guide. Key constraints:
  - Every line is exactly 120 characters (no CRLF — just LF between records).
  - Amounts are right-justified, zero-filled integer cents (NO decimal point).
  - Lodgement Reference (invoice number) occupies positions 63–80 (18 chars).
  - The SHA-256 of the generated file is stored on the payment_run document
    for non-repudiation.

ABA files are written to ABA_UPLOAD_PATH/{tenant_id}/{run_id}.aba.
ABA_UPLOAD_PATH defaults to /mnt/uploads/aba; override via environment variable.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from integrations.envelopes import EventEnvelope, PaymentRun, PaymentRunAccepted, generate_ulid

logger = logging.getLogger(__name__)

_ABA_UPLOAD_PATH = Path(os.environ.get("ABA_UPLOAD_PATH", "/tmp/aba"))


def _bsb(raw: str) -> str:
    """Normalise BSB to nnn-nnn format (7 chars)."""
    digits = "".join(c for c in raw if c.isdigit())[:6].zfill(6)
    return f"{digits[:3]}-{digits[3:]}"


def _lj(value: str, width: int) -> str:
    """Left-justify and space-pad to exactly `width` chars."""
    return value[:width].ljust(width)


def _rj_zero(value: int, width: int) -> str:
    """Right-justify an integer with zero-padding to exactly `width` chars."""
    return str(abs(value))[:width].zfill(width)


def _rj_str(value: str, width: int) -> str:
    """Right-justify a string with space-padding to exactly `width` chars."""
    return value[:width].rjust(width)


def _acct(raw: str) -> str:
    """Right-justify account number to 9 chars (zero-padded, digits only)."""
    digits = "".join(c for c in raw if c.isdigit())[:9]
    return digits.rjust(9)


def _build_descriptive_record(run: PaymentRun) -> str:
    """Build the 120-char Type-0 Descriptive Record."""
    date_str = run.processing_date.strftime("%d%m%y")

    record = (
            "0"  # 1:    Record type
            + _bsb(run.originating_bsb)  # 2-8:  BSB (nnn-nnn)
            + _acct(run.originating_account)  # 9-17: Account number
            + " "  # 18:   Reserved
            + "01"  # 19-20: Sequence number
            + _lj(run.bank_abbreviation, 3)  # 21-23: Bank abbreviation
            + _lj(run.originating_account_name, 32)  # 24-55: User name
            + _rj_str(run.apca_user_id[:7], 7)  # 56-62: APCA user ID
            + _lj(run.description, 12)  # 63-74: Description
            + date_str  # 75-80: Process date DDMMYY
            + " " * 40  # 81-120: Blank
    )
    assert len(record) == 120, f"Type-0 record length={len(record)}"
    return record


def _build_detail_record(line, originating_bsb: str, originating_account: str,
                         remitter_name: str) -> str:
    """Build a 120-char Type-1 Detail Record for a single disbursement."""
    record = (
            "1"  # 1:     Record type
            + _bsb(line.bsb)  # 2-8:   Destination BSB
            + _acct(line.account_number)  # 9-17:  Destination account
            + " "  # 18:    Indicator (blank)
            + line.transaction_code[:2].zfill(2)  # 19-20: Transaction code
            + _rj_zero(line.amount_cents, 10)  # 21-30: Amount (cents, no decimal)
            + _lj(line.payee_name, 32)  # 31-62: Account title
            + _lj(line.lodgement_reference, 18)  # 63-80: Lodgement reference
            + _bsb(originating_bsb)  # 81-87: Trace BSB
            + _acct(originating_account)  # 88-96: Trace account
            + _lj(remitter_name, 16)  # 97-112: Remitter name
            + "0" * 8  # 113-120: Withholding tax (zero)
    )
    assert len(record) == 120, f"Type-1 record length={len(record)}"
    return record


def _build_total_record(net_cents: int, credit_cents: int, debit_cents: int,
                        record_count: int) -> str:
    """Build the 120-char Type-7 File Total Record."""
    record = (
            "7"  # 1:     Record type
            + "999-999"  # 2-8:   BSB fill
            + " " * 12  # 9-20:  Blank
            + _rj_zero(abs(net_cents), 10)  # 21-30: Net total
            + _rj_zero(credit_cents, 10)  # 31-40: Credit total
            + _rj_zero(debit_cents, 10)  # 41-50: Debit total
            + " " * 24  # 51-74: Blank
            + _rj_zero(record_count, 6)  # 75-80: Record count
            + " " * 40  # 81-120: Blank
    )
    assert len(record) == 120, f"Type-7 record length={len(record)}"
    return record


def generate_aba_bytes(run: PaymentRun) -> bytes:
    """Generate a complete Cemtex ABA file from a PaymentRun.

    Returns the raw file bytes. Every line is exactly 120 chars.
    Lines are separated by LF (\\n). The last line has no trailing newline.
    """
    lines: list[str] = [_build_descriptive_record(run)]

    credit_cents = 0
    debit_cents = 0

    for line in run.lines:
        lines.append(_build_detail_record(
            line,
            run.originating_bsb,
            run.originating_account,
            run.originating_account_name[:16],
        ))
        if line.amount_cents >= 0:
            credit_cents += line.amount_cents
        else:
            debit_cents += abs(line.amount_cents)

    net_cents = credit_cents - debit_cents
    lines.append(_build_total_record(net_cents, credit_cents, debit_cents, len(run.lines)))

    content = "\n".join(lines)
    return content.encode("ascii")


class MockAbaWriter:
    """Mock PaymentInitiationProvider that writes Cemtex ABA files.

    name = "mock_aba_writer"

    The generated file is byte-perfect per the APCA Direct Entry spec.
    Its SHA-256 is persisted on the payment_run document for non-repudiation.

    Settlement simulation: a separate ARQ task (Phase 6) will advance
    payment_runs from "accepted" to "settled" after 3 business days.
    """

    name = "mock_aba_writer"

    async def initiate_batch(
            self,
            payment_run: PaymentRun,
    ) -> PaymentRunAccepted:
        """Generate the ABA file, persist it, store SHA-256, return acceptance."""
        from database import db

        aba_bytes = generate_aba_bytes(payment_run)
        sha256 = hashlib.sha256(aba_bytes).hexdigest()

        # Persist the file to disk
        out_dir = _ABA_UPLOAD_PATH / payment_run.tenant_id
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{payment_run.run_id}.aba"
        out_path.write_bytes(aba_bytes)
        logger.info("ABA file written: %s (sha256=%s)", out_path, sha256[:12])

        now = datetime.now(timezone.utc)

        # Upsert the payment_run document (idempotent on run_id)
        await db.payment_runs.update_one(
            {"tenant_id": payment_run.tenant_id, "run_id": payment_run.run_id},
            {
                "$setOnInsert": {
                    "tenant_id": payment_run.tenant_id,
                    "run_id": payment_run.run_id,
                    "idempotency_key": payment_run.idempotency_key,
                    "description": payment_run.description,
                    "line_count": len(payment_run.lines),
                    "credit_cents": sum(
                        ln.amount_cents for ln in payment_run.lines if ln.amount_cents >= 0
                    ),
                    "debit_cents": sum(
                        abs(ln.amount_cents) for ln in payment_run.lines if ln.amount_cents < 0
                    ),
                    "processing_date": payment_run.processing_date.isoformat(),
                    "aba_file_path": str(out_path),
                    "aba_sha256": sha256,
                    "status": "accepted",
                    "provider": self.name,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            },
            upsert=True,
        )

        return PaymentRunAccepted(
            provider_reference=f"MOCK-ABA-{payment_run.run_id}",
            run_id=payment_run.run_id,
            status="accepted",
            aba_file_path=str(out_path),
            aba_sha256=sha256,
            submitted_at=now,
        )

    async def pull_status_updates(
            self,
            since: datetime,
    ) -> AsyncIterator[EventEnvelope]:
        """Yield settlement/failure events for in-flight runs.

        The actual settlement simulation is handled by an ARQ task in Phase 6.
        This method polls payment_runs for any recently settled runs.
        """
        from database import db
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        cursor = db.payment_runs.find(
            {
                "tenant_id": building_id,
                "status": "settled",
                "settled_at": {"$gte": since.isoformat()},
                "settlement_event_emitted": {"$ne": True},
            }
        )
        async for doc in cursor:
            yield EventEnvelope(
                provider_event_id=doc["run_id"],
                idempotency_key=f"settled:{doc['run_id']}",
                tenant_id=building_id,
                occurred_at=datetime.fromisoformat(doc.get("settled_at", since.isoformat())),
                event_type="payment_run_settled",
                raw={"run_id": doc["run_id"], "aba_sha256": doc.get("aba_sha256")},
            )
            await db.payment_runs.update_one(
                {"_id": doc["_id"]},
                {"$set": {"settlement_event_emitted": True}},
            )

    async def cancel_payment(self, reference: str) -> bool:
        """Cancellation is not supported once an ABA file has been written."""
        logger.warning(
            "cancel_payment called on mock_aba_writer (reference=%s) — "
            "ABA files cannot be cancelled once written. Return False.",
            reference,
        )
        return False
