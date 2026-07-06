"""
backend/integrations/demo_bank/seed.py

# @featuretrace:demo_bank — Demo Bank seed: account and transaction fixtures for demo buildings.
# Layer: seed
# Data flow: seed function → ingestion.ensure_account() + ingestion._upsert_transaction()
#            → demo_bank_accounts / demo_bank_transactions (building-scoped).
# Related: backend/integrations/demo_bank/ingestion.py
#          backend/seeds/demo_customer.py (calls seed_acme_demo)
# Toggle: demo_bank_feed_enabled
# Collection: demo_bank_transactions, demo_bank_accounts, demo_bank_import_batches

Seeds are idempotent: running twice produces the same state.
All seed transactions use source_type="seed".
Acme Demo uses is_test_data=True by default (synthetic building — records must be swept by test cleanup).
East Gate uses is_test_data=False by default (production demo data that is intentionally persistent).
Test suites pass is_test_data=True to isolate their records.

East Gate (13195): 2 years of realistic admin + sinking fund movements.
Acme Demo (UP-DEMO-001): 2 years of synthetic demo transactions (14 lots).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


# ── East Gate seed data (anonymised from Strata Web actuals) ─────────────────────

_EGR_ADMIN_ACCOUNT = {
    "account_ref": "EGR-ADMIN-001",
    "account_name": "East Gate Residences — Admin Fund",
    "account_type": "trust_admin",
    "bsb": "062-000",
    "account_number_masked": "****4201",
    "opening_balance_cents": 4_25000,  # $4,250.00
}

_EGR_SINKING_ACCOUNT = {
    "account_ref": "EGR-SINKING-001",
    "account_name": "East Gate Residences — Sinking Fund",
    "account_type": "trust_sinking",
    "bsb": "062-000",
    "account_number_masked": "****4202",
    "opening_balance_cents": 89_50000,  # $89,500.00
}

# 24 months of representative transactions (FY2024 + FY2025)
# Each tuple: (date_str, amount_cents, direction, description, channel)
_EGR_ADMIN_TRANSACTIONS = [
    # FY2024 Q1 levy receipts
    ("2024-07-15", 350_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2024-07-15", 350_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2024-07-16", 350_00, "credit", "BPAY PAYMENT - LOT 3 - QUARTERLY LEVY", "BPAY"),
    ("2024-07-16", 350_00, "credit", "BPAY PAYMENT - LOT 4 - QUARTERLY LEVY", "BPAY"),
    ("2024-07-17", 350_00, "credit", "BPAY PAYMENT - LOT 5 - QUARTERLY LEVY", "BPAY"),
    ("2024-07-18", 350_00, "credit", "DEFT PAYMENT LOT 6 Q1 2024-25", "DEFT"),
    ("2024-07-19", 350_00, "credit", "DEFT PAYMENT LOT 7 Q1 2024-25", "DEFT"),
    ("2024-07-22", 350_00, "credit", "EFT CREDIT UNIT 8 LEVY JUL 2024", "EFT"),
    # FY2024 Q1 expenses
    ("2024-07-25", 1_20000, "debit", "CLEANING SERVICES - COMMON AREA JUL 2024", "EFT"),
    ("2024-07-31", 45000, "debit", "WATER CORP INVOICE #WC-2024-0731", "EFT"),
    ("2024-08-01", 85000, "debit", "GARDEN MAINTENANCE - AUG 2024", "EFT"),
    # FY2024 Q2 levy receipts
    ("2024-10-14", 350_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2024-10-14", 350_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2024-10-15", 350_00, "credit", "BPAY PAYMENT - LOT 3 - QUARTERLY LEVY", "BPAY"),
    ("2024-10-16", 700_00, "credit", "EFT CREDIT UNIT 4 LOT 4 LEVY Q2 2024-25 DOUBLE", "EFT"),
    # Bank fees
    ("2024-10-31", 1_500, "debit", "BANK ACCOUNT KEEPING FEE OCT 2024", "FEE"),
    # FY2024 Q3 levy receipts
    ("2025-01-13", 350_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2025-01-13", 350_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2025-01-14", 350_00, "credit", "BPAY PAYMENT - LOT 3 - QUARTERLY LEVY", "BPAY"),
    ("2025-01-17", 350_00, "credit", "DEFT PAYMENT LOT 5 Q3 2024-25", "DEFT"),
    ("2025-01-20", 45000, "debit", "INSURANCE PREMIUM INSTALLMENT JAN 2025", "EFT"),
    # FY2024 Q4 levy receipts
    ("2025-04-14", 350_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2025-04-14", 350_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2025-04-15", 350_00, "credit", "BPAY PAYMENT - LOT 3 - QUARTERLY LEVY", "BPAY"),
    # FY2025 Q1 levy receipts
    ("2025-07-14", 360_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2025-07-14", 360_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2025-07-15", 360_00, "credit", "BPAY PAYMENT - LOT 3 - QUARTERLY LEVY", "BPAY"),
    ("2025-07-16", 360_00, "credit", "BPAY PAYMENT - LOT 4 - QUARTERLY LEVY", "BPAY"),
    # FY2025 expenses
    ("2025-07-28", 1_30000, "debit", "CLEANING SERVICES - COMMON AREA JUL 2025", "EFT"),
    ("2025-07-31", 48000, "debit", "WATER CORP INVOICE #WC-2025-0731", "EFT"),
    ("2025-10-14", 360_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2025-10-15", 360_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2025-10-31", 1_500, "debit", "BANK ACCOUNT KEEPING FEE OCT 2025", "FEE"),
    ("2026-01-13", 360_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2026-01-14", 360_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
    ("2026-04-14", 360_00, "credit", "BPAY PAYMENT - LOT 1 - QUARTERLY LEVY", "BPAY"),
    ("2026-04-15", 360_00, "credit", "BPAY PAYMENT - LOT 2 - QUARTERLY LEVY", "BPAY"),
]

_EGR_SINKING_TRANSACTIONS = [
    ("2024-07-15", 160_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2024-07-15", 160_00, "credit", "BPAY PAYMENT - LOT 2 - SINKING FUND LEVY", "BPAY"),
    ("2024-07-16", 160_00, "credit", "BPAY PAYMENT - LOT 3 - SINKING FUND LEVY", "BPAY"),
    ("2024-10-14", 160_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2024-10-14", 160_00, "credit", "BPAY PAYMENT - LOT 2 - SINKING FUND LEVY", "BPAY"),
    ("2024-12-31", 2_85000, "credit", "INTEREST CREDIT FY2024 Q2 SINKING FUND", "INTEREST"),
    ("2025-01-13", 160_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2025-04-14", 160_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2025-06-30", 3_10000, "credit", "INTEREST CREDIT FY2024 YEAR END SINKING", "INTEREST"),
    ("2025-07-14", 165_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2025-07-14", 165_00, "credit", "BPAY PAYMENT - LOT 2 - SINKING FUND LEVY", "BPAY"),
    ("2025-10-14", 165_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2026-01-13", 165_00, "credit", "BPAY PAYMENT - LOT 1 - SINKING FUND LEVY", "BPAY"),
    ("2026-06-30", 3_25000, "credit", "INTEREST CREDIT FY2025 YEAR END SINKING", "INTEREST"),
]


async def seed_east_gate(
    db,
    building_id: str = "13195",
    is_test_data: bool = False,
) -> dict:
    """Seed 2 years of admin + sinking fund transactions for East Gate Residences.

    Idempotent: re-running produces the same state (upsert on idempotency_key).
    Does NOT touch levy_payments, unit_levy_ledger, or any finance.* table.
    """
    from strataos_demo_integrations.demo_bank.ingestion import ensure_account, _upsert_transaction, _recompute_balance

    await ensure_account(db, building_id, is_test_data=is_test_data, **_EGR_ADMIN_ACCOUNT)
    await ensure_account(db, building_id, is_test_data=is_test_data, **_EGR_SINKING_ACCOUNT)

    inserted = 0
    skipped = 0

    for date_str, amount_cents, direction, description, channel in _EGR_ADMIN_TRANSACTIONS:
        dt = _dt(date.fromisoformat(date_str))
        upserted = await _upsert_transaction(
            db=db,
            building_id=building_id,
            account_ref=_EGR_ADMIN_ACCOUNT["account_ref"],
            source_type="seed",
            source_batch_id=None,
            is_test_data=is_test_data,
            posted_date=dt,
            effective_date=dt,
            amount_cents=amount_cents,
            direction=direction,
            description=description,
            reference=None,
            payer_name=None,
            payment_channel=channel,
            running_balance_cents=None,
        )
        if upserted:
            inserted += 1
        else:
            skipped += 1

    for date_str, amount_cents, direction, description, channel in _EGR_SINKING_TRANSACTIONS:
        dt = _dt(date.fromisoformat(date_str))
        upserted = await _upsert_transaction(
            db=db,
            building_id=building_id,
            account_ref=_EGR_SINKING_ACCOUNT["account_ref"],
            source_type="seed",
            source_batch_id=None,
            is_test_data=is_test_data,
            posted_date=dt,
            effective_date=dt,
            amount_cents=amount_cents,
            direction=direction,
            description=description,
            reference=None,
            payer_name=None,
            payment_channel=channel,
            running_balance_cents=None,
        )
        if upserted:
            inserted += 1
        else:
            skipped += 1

    await _recompute_balance(db, building_id, _EGR_ADMIN_ACCOUNT["account_ref"])
    await _recompute_balance(db, building_id, _EGR_SINKING_ACCOUNT["account_ref"])

    logger.info(
        "Demo Bank seed_east_gate: building=%s inserted=%d skipped=%d is_test_data=%s",
        building_id, inserted, skipped, is_test_data,
    )
    return {"building_id": building_id, "inserted": inserted, "skipped": skipped}


# ── Acme Demo seed data (synthetic — 14 lots) ────────────────────────────────

_ACME_ADMIN_ACCOUNT = {
    "account_ref": "ACME-ADMIN-001",
    "account_name": "Acme StrataOS Demo — Admin Fund",
    "account_type": "trust_admin",
    "bsb": "082-401",
    "account_number_masked": "****8801",
    "opening_balance_cents": 12_00000,  # $12,000.00
}

_ACME_SINKING_ACCOUNT = {
    "account_ref": "ACME-SINKING-001",
    "account_name": "Acme StrataOS Demo — Capital Works Fund",
    "account_type": "trust_sinking",
    "bsb": "082-401",
    "account_number_masked": "****8802",
    "opening_balance_cents": 65_00000,  # $65,000.00
}

# 14 lots × 4 quarters × 2 years = up to 112 levy receipts + expenses/interest
_ACME_LOT_QUARTERLY_ADMIN = 425_00      # $425.00 per lot per quarter
_ACME_LOT_QUARTERLY_SINKING = 195_00   # $195.00 per lot per quarter
_ACME_LOTS = list(range(1, 15))        # lots 1–14

_ACME_QUARTERLY_DATES = [
    # FY2024
    "2024-07-15", "2024-10-14", "2025-01-13", "2025-04-14",
    # FY2025
    "2025-07-14", "2025-10-14", "2026-01-13", "2026-04-14",
]

_ACME_EXPENSES_ADMIN = [
    ("2024-07-28", 1_50000, "debit", "BUILDING MANAGER MONTHLY FEE JUL 2024", "EFT"),
    ("2024-08-05", 65000, "debit", "COMMON AREA ELECTRICITY AUG 2024", "EFT"),
    ("2024-09-15", 2_20000, "debit", "ANNUAL INSURANCE PREMIUM 2024", "EFT"),
    ("2024-10-31", 2_500, "debit", "BANK FEE OCT 2024", "FEE"),
    ("2024-11-10", 1_50000, "debit", "BUILDING MANAGER FEE NOV 2024", "EFT"),
    ("2025-01-20", 90000, "debit", "LIFT MAINTENANCE CONTRACT JAN 2025", "EFT"),
    ("2025-07-28", 1_60000, "debit", "BUILDING MANAGER MONTHLY FEE JUL 2025", "EFT"),
    ("2025-09-15", 2_35000, "debit", "ANNUAL INSURANCE PREMIUM 2025", "EFT"),
    ("2025-10-31", 2_500, "debit", "BANK FEE OCT 2025", "FEE"),
    ("2026-01-20", 95000, "debit", "LIFT MAINTENANCE CONTRACT JAN 2026", "EFT"),
]

_ACME_INTEREST_SINKING = [
    ("2024-12-31", 4_50000, "credit", "INTEREST CREDIT DEC 2024 CAPITAL WORKS FUND", "INTEREST"),
    ("2025-06-30", 4_80000, "credit", "INTEREST CREDIT JUN 2025 CAPITAL WORKS FUND", "INTEREST"),
    ("2025-12-31", 5_10000, "credit", "INTEREST CREDIT DEC 2025 CAPITAL WORKS FUND", "INTEREST"),
    ("2026-06-30", 5_40000, "credit", "INTEREST CREDIT JUN 2026 CAPITAL WORKS FUND", "INTEREST"),
]


async def seed_acme_demo(
    db,
    building_id: str = "UP-DEMO-001",
    is_test_data: bool = True,
) -> dict:
    """Seed 2 years of admin + sinking fund transactions for the Acme demo building.

    Idempotent. Called from seeds/demo_customer.py.
    """
    from strataos_demo_integrations.demo_bank.ingestion import ensure_account, _upsert_transaction, _recompute_balance

    await ensure_account(db, building_id, is_test_data=is_test_data, **_ACME_ADMIN_ACCOUNT)
    await ensure_account(db, building_id, is_test_data=is_test_data, **_ACME_SINKING_ACCOUNT)

    inserted = 0
    skipped = 0

    # Levy receipts — admin fund
    for date_str in _ACME_QUARTERLY_DATES:
        dt = _dt(date.fromisoformat(date_str))
        for lot in _ACME_LOTS:
            upserted = await _upsert_transaction(
                db=db,
                building_id=building_id,
                account_ref=_ACME_ADMIN_ACCOUNT["account_ref"],
                source_type="seed",
                source_batch_id=None,
                is_test_data=is_test_data,
                posted_date=dt,
                effective_date=dt,
                amount_cents=_ACME_LOT_QUARTERLY_ADMIN,
                direction="credit",
                description=f"BPAY PAYMENT - LOT {lot} - QUARTERLY ADMIN LEVY",
                reference=None,
                payer_name=f"Owner Lot {lot}",
                payment_channel="BPAY",
                running_balance_cents=None,
            )
            if upserted:
                inserted += 1
            else:
                skipped += 1

    # Admin fund expenses
    for date_str, amount_cents, direction, description, channel in _ACME_EXPENSES_ADMIN:
        dt = _dt(date.fromisoformat(date_str))
        upserted = await _upsert_transaction(
            db=db,
            building_id=building_id,
            account_ref=_ACME_ADMIN_ACCOUNT["account_ref"],
            source_type="seed",
            source_batch_id=None,
            is_test_data=is_test_data,
            posted_date=dt,
            effective_date=dt,
            amount_cents=amount_cents,
            direction=direction,
            description=description,
            reference=None,
            payer_name=None,
            payment_channel=channel,
            running_balance_cents=None,
        )
        if upserted:
            inserted += 1
        else:
            skipped += 1

    # Levy receipts — sinking/capital works fund
    for date_str in _ACME_QUARTERLY_DATES:
        dt = _dt(date.fromisoformat(date_str))
        for lot in _ACME_LOTS:
            upserted = await _upsert_transaction(
                db=db,
                building_id=building_id,
                account_ref=_ACME_SINKING_ACCOUNT["account_ref"],
                source_type="seed",
                source_batch_id=None,
                is_test_data=is_test_data,
                posted_date=dt,
                effective_date=dt,
                amount_cents=_ACME_LOT_QUARTERLY_SINKING,
                direction="credit",
                description=f"BPAY PAYMENT - LOT {lot} - CAPITAL WORKS LEVY",
                reference=None,
                payer_name=f"Owner Lot {lot}",
                payment_channel="BPAY",
                running_balance_cents=None,
            )
            if upserted:
                inserted += 1
            else:
                skipped += 1

    # Sinking fund interest
    for date_str, amount_cents, direction, description, channel in _ACME_INTEREST_SINKING:
        dt = _dt(date.fromisoformat(date_str))
        upserted = await _upsert_transaction(
            db=db,
            building_id=building_id,
            account_ref=_ACME_SINKING_ACCOUNT["account_ref"],
            source_type="seed",
            source_batch_id=None,
            is_test_data=is_test_data,
            posted_date=dt,
            effective_date=dt,
            amount_cents=amount_cents,
            direction=direction,
            description=description,
            reference=None,
            payer_name=None,
            payment_channel=channel,
            running_balance_cents=None,
        )
        if upserted:
            inserted += 1
        else:
            skipped += 1

    await _recompute_balance(db, building_id, _ACME_ADMIN_ACCOUNT["account_ref"])
    await _recompute_balance(db, building_id, _ACME_SINKING_ACCOUNT["account_ref"])

    logger.info(
        "Demo Bank seed_acme_demo: building=%s inserted=%d skipped=%d is_test_data=%s",
        building_id, inserted, skipped, is_test_data,
    )
    return {"building_id": building_id, "inserted": inserted, "skipped": skipped}


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _cli_main() -> None:
    import argparse

    ROOT = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(ROOT / "backend"))

    from dotenv import load_dotenv
    load_dotenv(ROOT / "backend" / ".env")

    from database import db

    parser = argparse.ArgumentParser(description="Seed Demo Bank transactions")
    parser.add_argument("--building", choices=["east_gate", "acme", "both"], default="both")
    parser.add_argument("--test-data", action="store_true",
                        help="Mark all seeded records as is_test_data=True")
    args = parser.parse_args()

    if args.building in ("east_gate", "both"):
        result = await seed_east_gate(db, is_test_data=args.test_data)
        print(f"East Gate: {result}")

    if args.building in ("acme", "both"):
        result = await seed_acme_demo(db, is_test_data=args.test_data)
        print(f"Acme Demo: {result}")


if __name__ == "__main__":
    asyncio.run(_cli_main())
