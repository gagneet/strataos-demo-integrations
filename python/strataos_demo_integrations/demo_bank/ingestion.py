"""
backend/integrations/demo_bank/ingestion.py

# @featuretrace:demo_bank — Demo Bank ingestion: CSV, Strata Web payment rows, manual entry.
# Layer: service
# Data flow: raw source (CSV bytes / Strata Web snapshot row / manual POST body)
#            → normalise → upsert demo_bank_transactions (building-scoped)
#            → update demo_bank_accounts running balance
# Related: backend/integrations/demo_bank/provider.py (reads from demo_bank_transactions)
#          backend/integrations/demo_bank/schemas.py (Pydantic models)
#          backend/integrations/mocks/csv_upload_bank_feed.py (CSV parser reused)
#          backend/routers/demo_bank.py (HTTP surface)
# Toggle: demo_bank_feed_enabled
# Collection: demo_bank_transactions, demo_bank_accounts, demo_bank_import_batches

Invariants enforced here:
- amount_cents is always stored as a positive int; direction field carries the sign.
- idempotency_key = sha256(building_id|provider|account_ref|external_transaction_id).
- All upserts use update_one(..., upsert=True) — never insert_one.
- No writes to levy_payments, unit_levy_ledger, or finance.* Postgres tables.
- is_test_data propagates from every caller through to every created document.
- Strata Web guard: only dated payment movements may become demo_bank_transactions.
  Balance snapshots, arrears totals, and budget figures are rejected with a log warning.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bson import ObjectId

from strataos_demo_integrations.demo_bank.reconstruction_batch_schemas import (
    ReconstructionBatch,
    ReconstructionManifest,
)
from strataos_demo_integrations.demo_bank.schemas import (
    PROVIDER,
    ImportStatus,
    ManualTransactionRequest,
)

logger = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).parent.parent / "data_upload" / "mocks" / "bank_schemas"

# Regex patterns reused from csv_upload_bank_feed for signal extraction
_CRN_RE = re.compile(r"\b(\d{13})\b")
_E2E_RE = re.compile(r"\b(LVY-[A-Z0-9]{4,10}-[A-Z0-9]{2,6}-\d{1,4})\b", re.IGNORECASE)
_LOT_RE = re.compile(
    r"\b(?:UNIT|LOT|APT|APARTMENT|TH|TOWNHOUSE|VILLA|PENTHOUSE)\s*(\d{1,4})\b",
    re.IGNORECASE,
)
_MOD10V05_WEIGHTS = [3, 2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4]


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _external_txn_id(
    account_ref: str,
    posted_date_iso: str,
    amount_cents: int,
    direction: str,
    description: str,
    running_balance_cents: Optional[int] = None,
) -> str:
    """Deterministic stable ID for a CSV/manual row without a provider-assigned ID.

    Includes direction so credit and debit of the same amount/date/description
    do not collide. Includes running_balance where available to distinguish two
    owners paying the same levy amount on the same day.
    """
    parts = "|".join([
        account_ref,
        posted_date_iso,
        str(abs(amount_cents)),
        direction,
        description.strip().upper(),
        str(running_balance_cents) if running_balance_cents is not None else "",
    ])
    return hashlib.sha256(parts.encode()).hexdigest()


def _idempotency_key(building_id: str, account_ref: str, external_txn_id: str) -> str:
    parts = "|".join([building_id, PROVIDER, account_ref, external_txn_id])
    return hashlib.sha256(parts.encode()).hexdigest()


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── CSV ingestion ─────────────────────────────────────────────────────────────

async def import_csv(
    db,
    building_id: str,
    account_ref: str,
    bank_name: str,
    file_content: bytes,
    filename: str,
    uploaded_by: str,
    is_test_data: bool = False,
) -> dict:
    """Parse a bank CSV export and upsert rows into demo_bank_transactions.

    Returns a dict matching ImportResultResponse fields.
    Re-uploading the same file (same SHA-256) is a no-op: returns the original
    batch with duplicate_batch=True.
    """
    import yaml

    fhash = _file_hash(file_content)

    # Batch-level idempotency: same file → return existing batch
    existing_batch = await db._db.demo_bank_import_batches.find_one(
        {"building_id": building_id, "file_hash": fhash, "is_test_data": {"$ne": not is_test_data}}
    )
    if existing_batch:
        logger.info(
            "Demo Bank CSV already imported (batch=%s building=%s)",
            existing_batch["_id"],
            building_id,
        )
        return {
            "batch_id": str(existing_batch["_id"]),
            "import_status": existing_batch.get("import_status", "completed"),
            "imported_count": existing_batch.get("imported_count", 0),
            "skipped_count": existing_batch.get("skipped_count", 0),
            "error_count": existing_batch.get("error_count", 0),
            "duplicate_batch": True,
            "message": "File already imported — returning original batch.",
        }

    schema_path = _SCHEMA_DIR / f"{bank_name}.yaml"
    if not schema_path.exists():
        available = [p.stem for p in _SCHEMA_DIR.glob("*.yaml")]
        raise ValueError(
            f"No bank schema for {bank_name!r}. Available: {available}"
        )
    with schema_path.open() as f:
        schema = yaml.safe_load(f)

    # Create batch record (pending) before parsing so we have an ID for rows
    batch_doc = {
        "building_id": building_id,
        "source_type": "csv_upload",
        "bank_name": bank_name,
        "filename": filename,
        "file_hash": fhash,
        "uploaded_by": uploaded_by,
        "account_ref": account_ref,
        "imported_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "import_status": "pending",
        "evidence_document_id": None,
        "original_file_storage_ref": None,
        "is_test_data": is_test_data,
        "created_at": _now(),
    }
    batch_result = await db._db.demo_bank_import_batches.insert_one(batch_doc)
    batch_id = batch_result.inserted_id

    rows = _parse_csv_to_rows(file_content, schema, account_ref, building_id)
    imported = skipped = errors = 0

    for row in rows:
        try:
            upserted = await _upsert_transaction(
                db=db,
                building_id=building_id,
                account_ref=account_ref,
                source_type="csv_upload",
                source_batch_id=batch_id,
                is_test_data=is_test_data,
                **row,
            )
            if upserted:
                imported += 1
            else:
                skipped += 1
        except Exception as exc:
            logger.warning("Demo Bank CSV row error (building=%s): %s — %r", building_id, exc, row)
            errors += 1

    status: ImportStatus = "completed" if errors == 0 else ("partial" if imported > 0 else "failed")
    await db._db.demo_bank_import_batches.update_one(
        {"_id": batch_id},
        {"$set": {
            "imported_count": imported,
            "skipped_count": skipped,
            "error_count": errors,
            "import_status": status,
        }},
    )

    # Recompute account running balance
    await _recompute_balance(db, building_id, account_ref)

    return {
        "batch_id": str(batch_id),
        "import_status": status,
        "imported_count": imported,
        "skipped_count": skipped,
        "error_count": errors,
        "duplicate_batch": False,
        "message": f"Imported {imported} transactions ({skipped} skipped, {errors} errors).",
    }


def _parse_csv_to_rows(content: bytes, schema: dict, account_ref: str, building_id: str) -> list[dict]:
    """Parse CSV bytes using a bank YAML schema into normalised row dicts."""
    import csv
    import io

    text = content.decode("utf-8-sig", errors="replace")
    skip = schema.get("skip_rows", 1)
    date_fmt = schema.get("date_format", "%d/%m/%Y")
    amount_mode = schema.get("amount_mode", "signed")
    cols: dict = schema.get("columns", {})
    raw_deriv = schema.get("amount_derivation", {})
    amount_derivation: dict = raw_deriv if isinstance(raw_deriv, dict) else {}

    reader = csv.DictReader(io.StringIO(text))
    all_rows = list(reader)
    rows_to_parse = all_rows[skip:] if skip > 0 else all_rows

    result = []
    for row in rows_to_parse:
        if not any(v and v.strip() for v in row.values()):
            continue
        try:
            date_col = cols.get("date", "Date")
            date_str = row.get(date_col, "").strip()
            if not date_str:
                continue

            try:
                dt = datetime.strptime(date_str, date_fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning("Unparseable date %r in CSV row — skipping", date_str)
                continue

            description_col = cols.get("description", "Description")
            description = row.get(description_col, "").strip()

            balance_col = cols.get("balance", "Balance")
            balance_raw = row.get(balance_col, "").strip()
            running_balance_cents: Optional[int] = _cents_from_str(balance_raw) if balance_raw else None

            if amount_mode == "signed":
                amount_col = cols.get("amount", "Amount")
                raw_val = row.get(amount_col, "0")
                signed_cents = _cents_from_str(raw_val)
            else:
                debit_col = amount_derivation.get("debit_col", cols.get("debit", "Debit"))
                credit_col = amount_derivation.get("credit_col", cols.get("credit", "Credit"))
                debit_raw = row.get(debit_col, "").strip()
                credit_raw = row.get(credit_col, "").strip()
                debit_cents = _cents_from_str(debit_raw) if debit_raw else 0
                credit_cents = _cents_from_str(credit_raw) if credit_raw else 0
                signed_cents = credit_cents - debit_cents

            direction = "credit" if signed_cents >= 0 else "debit"
            amount_cents = abs(signed_cents)

            result.append({
                "posted_date": dt,
                "effective_date": dt,
                "amount_cents": amount_cents,
                "direction": direction,
                "description": description,
                "reference": None,
                "payer_name": None,
                "payment_channel": _infer_channel(description),
                "running_balance_cents": running_balance_cents,
            })
        except Exception as exc:
            logger.warning("Skipping malformed CSV row %r: %s", row, exc)

    return result


def _cents_from_str(value: str) -> int:
    cleaned = value.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not cleaned or cleaned in ("-", ""):
        return 0
    return int(round(float(cleaned) * 100))


def _infer_channel(description: str) -> str:
    upper = description.upper()
    if "BPAY" in upper:
        return "BPAY"
    if "DEFT" in upper:
        return "DEFT"
    if "INTEREST" in upper or "INT CREDIT" in upper:
        return "INTEREST"
    if "FEE" in upper or "SERVICE CHARGE" in upper:
        return "FEE"
    if any(k in upper for k in ("OSKO", "NPP", "FAST", "LVY-")):
        return "NPP"
    return "EFT"


# ── Strata Web payment ingestion ──────────────────────────────────────────────────

# Fields in a staging_strata_web_snapshot that are balance/derived data, NOT bank transactions.
# Any snapshot that ONLY has these fields (no dated payment amounts) is rejected.
_CIVIUM_BALANCE_FIELDS = frozenset({
    "raw_admin_fund_balance_cents",
    "raw_sinking_fund_balance_cents",
    "raw_arrears_total_cents",
    "raw_credit_total_cents",
    "raw_collection_rate",
    "per_unit_balances",
})

_CIVIUM_PAYMENT_REQUIRED = frozenset({"amount_cents", "payment_date"})


async def import_strata_web_snapshot(
    db,
    building_id: str,
    financial_year: str,
    account_ref: str,
    is_test_data: bool = False,
) -> dict:
    """Promote payment-movement rows from staging_strata_web_snapshots to Demo Bank.

    GUARD: Only rows with a dated payment amount may become demo_bank_transactions.
    Balance snapshots, arrears totals, and budget figures are rejected with a warning.

    The staging_strata_web_snapshots collection stores balance snapshots (admin balance,
    sinking balance, per-unit arrears). These are NOT bank transactions and must not
    be converted into demo_bank_transactions documents.

    Returns an ImportResultResponse-compatible dict.
    """
    from bson import ObjectId

    year_candidates = [financial_year]
    if "-" not in financial_year:
        try:
            y = int(financial_year)
            year_candidates.append(f"{y}-{y + 1}")
        except ValueError:
            pass

    snapshots = await db._db.staging_strata_web_snapshots.find(
        {
            "building_id": building_id,
            "financial_year": {"$in": year_candidates},
        }
    ).to_list(length=500)

    if not snapshots:
        return {
            "batch_id": None,
            "import_status": "completed",
            "imported_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "duplicate_batch": False,
            "message": f"No Strata Web snapshots found for building {building_id} year {financial_year}.",
        }

    # Create batch record
    batch_doc = {
        "building_id": building_id,
        "source_type": "strata_web_payment",
        "bank_name": None,
        "filename": f"strata_web_snapshot_{financial_year}",
        "file_hash": None,
        "uploaded_by": "system",
        "account_ref": account_ref,
        "imported_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "import_status": "pending",
        "evidence_document_id": None,
        "original_file_storage_ref": None,
        "is_test_data": is_test_data,
        "created_at": _now(),
    }
    batch_result = await db._db.demo_bank_import_batches.insert_one(batch_doc)
    batch_id = batch_result.inserted_id

    imported = skipped = errors = 0

    for snapshot in snapshots:
        # Apply the Strata Web guard: reject balance-only snapshots
        payment_rows = _extract_strata_web_payments(snapshot, building_id)
        if not payment_rows:
            logger.warning(
                "Demo Bank Strata Web guard: snapshot %s for building %s (year %s) "
                "contains no dated payment movements — skipping entirely. "
                "Fields present: %s",
                snapshot.get("_id"),
                building_id,
                financial_year,
                list(snapshot.keys()),
            )
            skipped += 1
            continue

        for payment in payment_rows:
            try:
                upserted = await _upsert_transaction(
                    db=db,
                    building_id=building_id,
                    account_ref=account_ref,
                    source_type="strata_web_payment",
                    source_batch_id=batch_id,
                    is_test_data=is_test_data,
                    **payment,
                )
                if upserted:
                    imported += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning(
                    "Demo Bank Strata Web row error (building=%s snapshot=%s): %s",
                    building_id,
                    snapshot.get("_id"),
                    exc,
                )
                errors += 1

    status: ImportStatus = "completed" if errors == 0 else ("partial" if imported > 0 else "failed")
    await db._db.demo_bank_import_batches.update_one(
        {"_id": batch_id},
        {"$set": {
            "imported_count": imported,
            "skipped_count": skipped,
            "error_count": errors,
            "import_status": status,
        }},
    )

    if imported > 0:
        await _recompute_balance(db, building_id, account_ref)

    return {
        "batch_id": str(batch_id),
        "import_status": status,
        "imported_count": imported,
        "skipped_count": skipped,
        "error_count": errors,
        "duplicate_batch": False,
        "message": (
            f"Strata Web import: {imported} payment rows imported, "
            f"{skipped} snapshots/rows skipped (balance data rejected by guard), "
            f"{errors} errors."
        ),
    }


def _extract_strata_web_payments(snapshot: dict, building_id: str) -> list[dict]:
    """Extract only dated payment movements from a Strata Web snapshot.

    A staging_strata_web_snapshot primarily stores balance data (admin balance, sinking
    balance, per_unit_balances). These must NOT become bank transactions.

    The snapshot may optionally contain a 'payments' list added by future scrapers
    that capture individual dated payment receipts. Only those rows qualify.
    """
    payments_raw = snapshot.get("payments") or []
    result = []

    for p in payments_raw:
        payment_date_str = p.get("payment_date") or p.get("date")
        amount_raw = p.get("amount_cents") or p.get("amount")

        if not payment_date_str or amount_raw is None:
            logger.warning(
                "Demo Bank Strata Web guard: rejected payment row missing date or amount "
                "(building=%s snapshot=%s row=%r)",
                building_id,
                snapshot.get("_id"),
                p,
            )
            continue

        try:
            amount_cents = int(amount_raw) if str(amount_raw).lstrip("-").isdigit() else int(round(float(str(amount_raw).replace(",", "")) * 100))
        except (ValueError, TypeError):
            logger.warning(
                "Demo Bank Strata Web guard: rejected row with unparseable amount %r "
                "(building=%s snapshot=%s)",
                amount_raw,
                building_id,
                snapshot.get("_id"),
            )
            continue

        try:
            dt = datetime.fromisoformat(str(payment_date_str)[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(
                "Demo Bank Strata Web guard: rejected row with unparseable date %r "
                "(building=%s)",
                payment_date_str,
                building_id,
            )
            continue

        signed = int(amount_cents)
        direction = "credit" if signed >= 0 else "debit"
        description = p.get("description") or p.get("narration") or f"Strata Web payment {str(snapshot.get('_id', ''))[:8]}"
        lot_ref = str(p.get("lot_number") or p.get("lot") or "")

        result.append({
            "posted_date": dt,
            "effective_date": dt,
            "amount_cents": abs(signed),
            "direction": direction,
            "description": description,
            "reference": p.get("reference") or p.get("crn"),
            "payer_name": p.get("owner_name") or (f"Lot {lot_ref}" if lot_ref else None),
            "payment_channel": p.get("channel") or "BPAY",
            "running_balance_cents": None,
        })

    return result


# ── Historical reconstruction ingestion ─────────────────────────────────────────
#
# Distinct from the CSV/Strata-Web direct-ingestion paths above: this entrypoint
# never sees a raw source row. It only ever writes rows that already exist inside
# an approved, immutable ReconstructionManifest (built by the caller from budget/
# AGM/audited-statement facts, GST/UOE apportionment, and payment-pattern
# modelling — none of which happens in this file). It must never recompute an
# amount or a date; every value comes verbatim from manifest.transactions.
#
# This does NOT touch or weaken the Strata Web guard above — that guard protects
# a different, direct-ingestion code path (raw snapshot rows that must never
# become bank transactions unless they already carry a dated payment amount).
# Reconstruction ingestion is for facts that were never bank transactions in the
# first place and were deliberately, reviewably, modelled into them.

_MANIFEST_ELIGIBLE_BATCH_STATUSES = frozenset({"approved", "generation_ready"})


async def import_historical_reconstruction(
    db,
    building_id: str,
    batch: ReconstructionBatch,
    manifest: ReconstructionManifest,
) -> dict:
    """Materialise every row in an approved ReconstructionManifest as a Demo Bank txn.

    Refuses unless `batch.status` has passed approval (`approved` or
    `generation_ready`) — a defense-in-depth check independent of whatever gate
    the calling orchestrator (strata-management's reconstruction_batch_service)
    already applied. Every row is upserted via the same `_upsert_transaction()`
    used by CSV/Strata-Web ingestion, so it inherits the same idempotent
    `$setOnInsert` behaviour — replaying the same manifest twice is a no-op.

    Returns a dict shaped like ImportResultResponse plus a `batch_id` field.
    """
    if batch.batch_id != manifest.batch_id:
        raise ValueError(
            f"Manifest {manifest.manifest_id} belongs to batch {manifest.batch_id}, "
            f"not the supplied batch {batch.batch_id}."
        )
    if batch.building_id != building_id:
        raise ValueError(
            f"Batch {batch.batch_id} belongs to building {batch.building_id}, "
            f"not the requested building {building_id}."
        )
    if batch.status not in _MANIFEST_ELIGIBLE_BATCH_STATUSES:
        raise ValueError(
            f"Batch {batch.batch_id} has status={batch.status!r}; historical "
            f"reconstruction rows may only be materialised for a batch in "
            f"{sorted(_MANIFEST_ELIGIBLE_BATCH_STATUSES)}."
        )
    if not manifest.transactions:
        return {
            "batch_id": batch.batch_id,
            "import_status": "completed",
            "imported_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "duplicate_batch": False,
            "message": "Manifest contains no transactions — nothing to materialise.",
        }

    imported = skipped = errors = 0
    touched_account_refs: set[str] = set()

    for row in manifest.transactions:
        try:
            upserted = await _upsert_transaction(
                db=db,
                building_id=building_id,
                account_ref=row.account_ref,
                source_type="historical_reconstruction",
                source_batch_id=batch.batch_id,
                is_test_data=batch.is_test_data,
                posted_date=datetime.combine(row.posted_date, datetime.min.time()).replace(tzinfo=timezone.utc),
                effective_date=datetime.combine(row.posted_date, datetime.min.time()).replace(tzinfo=timezone.utc),
                amount_cents=row.amount_cents,
                direction=row.direction,
                description=row.description,
                reference=None,
                payer_name=None,
                payment_channel="OTHER",
                running_balance_cents=None,
                confidence="high",
                provenance_class="reconstruction",
                evidence_type="historical_reconstruction_manifest",
                formula_version=manifest.generator_version,
                source_snapshot_ids=list(manifest.input_document_hashes),
                requires_review=False,
                date_basis="reconstructed_from_approved_manifest",
                unit_number=row.unit_number,
                transaction_origin="reconstructed_historical",
                reconstruction_batch_id=batch.batch_id,
                reconstruction_version=manifest.version,
                assumption_code=row.assumption_code,
                levy_component=row.levy_component,
            )
            touched_account_refs.add(row.account_ref)
            if upserted:
                imported += 1
            else:
                skipped += 1
        except Exception as exc:
            logger.warning(
                "Demo Bank reconstruction row error (building=%s batch=%s unit=%s): %s",
                building_id, batch.batch_id, row.unit_number, exc,
            )
            errors += 1

    for account_ref in touched_account_refs:
        await _recompute_balance(db, building_id, account_ref)

    status: ImportStatus = "completed" if errors == 0 else ("partial" if imported > 0 else "failed")
    return {
        "batch_id": batch.batch_id,
        "import_status": status,
        "imported_count": imported,
        "skipped_count": skipped,
        "error_count": errors,
        "duplicate_batch": False,
        "message": (
            f"Historical reconstruction: {imported} transactions materialised "
            f"({skipped} already existed, {errors} errors) from manifest "
            f"{manifest.manifest_id} v{manifest.version}."
        ),
    }


# ── Manual injection ──────────────────────────────────────────────────────────

async def inject_manual(
    db,
    building_id: str,
    req: ManualTransactionRequest,
    injected_by: str,
    is_test_data: bool = False,
) -> dict:
    """Inject a single manually-specified transaction (super_admin only).

    Returns a dict with the inserted/existing document ID and idempotency status.
    """
    try:
        dt = datetime.fromisoformat(req.posted_date).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Invalid posted_date {req.posted_date!r}: {exc}") from exc

    effective_dt = dt
    if req.effective_date:
        try:
            effective_dt = datetime.fromisoformat(req.effective_date).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    ext_id = _external_txn_id(
        account_ref=req.account_ref,
        posted_date_iso=dt.date().isoformat(),
        amount_cents=req.amount_cents,
        direction=req.direction,
        description=req.description,
        running_balance_cents=None,
    )

    upserted = await _upsert_transaction(
        db=db,
        building_id=building_id,
        account_ref=req.account_ref,
        source_type="manual",
        source_batch_id=None,
        is_test_data=is_test_data,
        posted_date=dt,
        effective_date=effective_dt,
        amount_cents=req.amount_cents,
        direction=req.direction,
        description=req.description,
        reference=req.reference,
        payer_name=req.payer_name,
        payment_channel=req.payment_channel,
        running_balance_cents=None,
    )

    await _recompute_balance(db, building_id, req.account_ref)

    return {
        "external_transaction_id": ext_id,
        "upserted": upserted,
        "message": "Transaction created." if upserted else "Duplicate — transaction already exists.",
    }


# ── Core upsert ───────────────────────────────────────────────────────────────

async def _upsert_transaction(
    db,
    building_id: str,
    account_ref: str,
    source_type: str,
    source_batch_id: Optional[ObjectId | str],
    is_test_data: bool,
    posted_date: datetime,
    effective_date: datetime,
    amount_cents: int,
    direction: str,
    description: str,
    reference: Optional[str],
    payer_name: Optional[str],
    payment_channel: str,
    running_balance_cents: Optional[int],
    # ── Provenance/confidence fields (additive) ──────────────────────────────
    # Populated by deterministic/reconstructed sources (historical reconstruction,
    # Strata Web balance-delta inference) that know more about a transaction's
    # origin than a real bank feed ever can. Left at their safe defaults for the
    # existing CSV/manual/Strata-Web-payment call sites, which describe genuinely
    # bank-observed or manually-entered rows.
    confidence: Optional[str] = None,
    provenance_class: Optional[str] = None,
    evidence_type: Optional[str] = None,
    formula_version: Optional[str] = None,
    source_snapshot_ids: Optional[list[str]] = None,
    supersedes_event_id: Optional[str] = None,
    requires_review: bool = False,
    date_basis: Optional[str] = None,
    # unit_number: deterministic sources (synthetic reconstruction, confirmed
    # Strata Web payment rows) know the paying unit exactly. This lets the
    # high-confidence promotion path resolve the lot directly instead of relying
    # on lot_ref_raw regex extraction, which is only appropriate for real,
    # free-text bank descriptions.
    unit_number: Optional[str] = None,
    # ── Historical-reconstruction batch/manifest linkage (additive) ──────────
    # Populated only by import_historical_reconstruction(). Left at their safe
    # defaults (None) for every existing call site (CSV/Strata-Web/manual).
    transaction_origin: Optional[str] = None,
    reconstruction_batch_id: Optional[str] = None,
    reconstruction_version: Optional[int] = None,
    assumption_code: Optional[str] = None,
    levy_component: Optional[str] = None,
) -> bool:
    """Upsert one demo_bank_transaction. Returns True if a new document was inserted."""
    ext_id = _external_txn_id(
        account_ref=account_ref,
        posted_date_iso=posted_date.date().isoformat(),
        amount_cents=amount_cents,
        direction=direction,
        description=description,
        running_balance_cents=running_balance_cents,
    )
    idem_key = _idempotency_key(building_id, account_ref, ext_id)
    bpay_crn, osko_e2e_id, lot_ref_raw = _extract_signals(description)

    doc = {
        "building_id": building_id,
        "account_ref": account_ref,
        "provider": PROVIDER,
        "external_transaction_id": ext_id,
        "posted_date": posted_date,
        "effective_date": effective_date,
        "amount_cents": amount_cents,
        "direction": direction,
        "description": description,
        "reference": reference,
        "payer_name": payer_name,
        "payment_channel": payment_channel,
        "bpay_crn": bpay_crn,
        "osko_e2e_id": osko_e2e_id,
        "lot_ref_raw": lot_ref_raw,
        "unit_number": unit_number,
        "raw_payload": {},
        "source_type": source_type,
        "source_batch_id": source_batch_id,
        "idempotency_key": idem_key,
        "running_balance_cents": running_balance_cents,
        "status": "posted",
        "sync_status": "pending",
        "last_sync_attempt_at": None,
        "finance_bank_transaction_ref": None,
        "sync_error": None,
        "evidence_document_id": None,
        "original_file_storage_ref": None,
        "source_sha256": None,
        "is_test_data": is_test_data,
        "confidence": confidence,
        "provenance_class": provenance_class,
        "evidence_type": evidence_type,
        "formula_version": formula_version,
        "source_snapshot_ids": source_snapshot_ids or [],
        "supersedes_event_id": supersedes_event_id,
        "requires_review": requires_review,
        "date_basis": date_basis,
        "transaction_origin": transaction_origin,
        "reconstruction_batch_id": reconstruction_batch_id,
        "reconstruction_version": reconstruction_version,
        "assumption_code": assumption_code,
        "levy_component": levy_component,
        "created_at": _now(),
    }

    result = await db._db.demo_bank_transactions.update_one(
        {"building_id": building_id, "idempotency_key": idem_key},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return result.upserted_id is not None


# ── Balance recomputation ─────────────────────────────────────────────────────

async def _recompute_balance(db, building_id: str, account_ref: str) -> None:
    """Recompute current_balance_cents on the demo_bank_accounts document.

    Uses opening_balance_cents + sum of all posted credits - sum of all posted debits.
    This is a read-only recompute; it never writes to any finance table.
    """
    account = await db._db.demo_bank_accounts.find_one(
        {"building_id": building_id, "account_ref": account_ref}
    )
    if not account:
        return

    opening = account.get("opening_balance_cents", 0)

    pipeline = [
        {"$match": {
            "building_id": building_id,
            "account_ref": account_ref,
            "status": {"$in": ["posted", "pending"]},
        }},
        {"$group": {
            "_id": "$direction",
            "total": {"$sum": "$amount_cents"},
        }},
    ]
    _agg_cursor = await db._db.demo_bank_transactions.aggregate(pipeline)
    totals = {doc["_id"]: doc["total"] async for doc in _agg_cursor}
    credits = totals.get("credit", 0)
    debits = totals.get("debit", 0)
    current = opening + credits - debits

    await db._db.demo_bank_accounts.update_one(
        {"building_id": building_id, "account_ref": account_ref},
        {"$set": {"current_balance_cents": current, "updated_at": _now()}},
    )


# ── Account ensure ────────────────────────────────────────────────────────────

async def ensure_account(
    db,
    building_id: str,
    account_ref: str,
    account_name: str,
    account_type: str,
    bsb: str = "000-000",
    account_number_masked: str = "****0000",
    opening_balance_cents: int = 0,
    is_test_data: bool = False,
) -> str:
    """Upsert a demo_bank_accounts document. Returns the account_ref."""
    now = _now()
    await db._db.demo_bank_accounts.update_one(
        {"building_id": building_id, "account_ref": account_ref},
        {"$setOnInsert": {
            "building_id": building_id,
            "provider": PROVIDER,
            "account_ref": account_ref,
            "account_name": account_name,
            "account_type": account_type,
            "bsb": bsb,
            "account_number_masked": account_number_masked,
            "currency": "AUD",
            "opening_balance_cents": opening_balance_cents,
            "current_balance_cents": opening_balance_cents,
            "status": "active",
            "is_test_data": is_test_data,
            "created_at": now,
            "updated_at": now,
        }},
        upsert=True,
    )
    return account_ref
