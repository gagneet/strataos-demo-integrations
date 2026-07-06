"""
Seed data for gap closure compliance features.

Creates:
  - 1 sample jurisdiction config (ACT) for East Gate Residences
  - Sample compliance registers (fire, lift, asbestos, pool)
  - Sample insurance policies
  - Sample audit record
  - Sample trust accounts
  - Sample bank transactions (mock)
  - Sample decisions

Run with:
  cd /path/to/backend
  python -m seeds.compliance_demo
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asyncio
import logging

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_str(days_offset: int = 0) -> str:
    return str(date.today() + timedelta(days=days_offset))


EASTGATE_BUILDING_ID = "eastgate_residences"
SEED_USER_ID = "system_seed"


async def seed(db) -> None:
    """Seed all compliance demo data."""
    # ── 1. Jurisdiction Config (ACT) ──────────────────────────────────────────
    existing = await db.jurisdiction_config.find_one({"building_id": EASTGATE_BUILDING_ID})
    if not existing:
        await db.jurisdiction_config.insert_one({
            "building_id": EASTGATE_BUILDING_ID,
            "state": "ACT",
            "rule_overrides": {
                # Eastgate's EC voted a 12% levy interest rate by special resolution
                "levy_interest_rate_default_percent": {
                    "value": 12,
                    "unit": "percent",
                    "legislation": "UTMA 2011 s.76 — Eastgate EC Special Resolution 2024-AGM-06",
                    "effective": "2024-07-01",
                    "notes": "EC resolved 12% per annum by special resolution at 2024 AGM.",
                },
            },
            "created_at": _now_iso(),
        })
        logger.info("✅ Jurisdiction config seeded (ACT)")
    else:
        logger.info("   Jurisdiction config already exists — skipped")

    # ── 2. Trust Ledger Accounts ──────────────────────────────────────────────
    accounts = [
        {
            "building_id": EASTGATE_BUILDING_ID,
            "fund_type": "admin_fund",
            "account_type": "asset",
            "account_code": "1001",
            "account_name": "NAB Trust Account — Admin Fund",
            "bsb": "082-001",
            "account_number": "enc:placeholder",  # encrypted in production
            "opening_balance": 125_000.00,
            "current_balance": 125_000.00,
            "currency": "AUD",
            "is_active": True,
            "created_at": _now_iso(),
        },
        {
            "building_id": EASTGATE_BUILDING_ID,
            "fund_type": "capital_works_fund",
            "account_type": "asset",
            "account_code": "1002",
            "account_name": "NAB Trust Account — Capital Works Fund",
            "bsb": "082-001",
            "account_number": "enc:placeholder",
            "opening_balance": 85_000.00,
            "current_balance": 85_000.00,
            "currency": "AUD",
            "is_active": True,
            "created_at": _now_iso(),
        },
    ]
    for acc in accounts:
        existing = await db.trust_ledger_accounts.find_one({
            "building_id": EASTGATE_BUILDING_ID,
            "account_code": acc["account_code"],
        })
        if not existing:
            await db.trust_ledger_accounts.insert_one(acc)
    logger.info("✅ Trust ledger accounts seeded")

    # ── 3. Sample Insurance Policies ──────────────────────────────────────────
    insurance_seed = [
        {
            "building_id": EASTGATE_BUILDING_ID,
            "policy_type": "building",
            "insurer_name": "IAG (CGU Insurance)",
            "policy_number": "POL-2025-BLD-001",
            "cover_amount": 18_500_000.00,  # Replacement value
            "premium_annual": 42_500.00,
            "excess_amount": 5_000.00,
            "start_date": "2025-07-01",
            "expiry_date": _date_str(days_offset=90),
            "status": "active",
            "quotes_count": 3,
            "commission_percent": 15.0,
            "commission_amount": 6375.00,
            "broker_name": "Steadfast Group",
            "connected_insurer": False,
            "notes": "Full replacement value. Strata Community Insurance product.",
            "created_at": _now_iso(),
            "created_by": SEED_USER_ID,
        },
        {
            "building_id": EASTGATE_BUILDING_ID,
            "policy_type": "public_liability",
            "insurer_name": "IAG (CGU Insurance)",
            "policy_number": "POL-2025-PLI-001",
            "cover_amount": 20_000_000.00,
            "public_liability_amount": 20_000_000.00,  # ACT minimum $10M
            "premium_annual": 8_200.00,
            "excess_amount": 2_500.00,
            "start_date": "2025-07-01",
            "expiry_date": _date_str(days_offset=90),
            "status": "active",
            "quotes_count": 3,
            "commission_percent": 15.0,
            "commission_amount": 1230.00,
            "broker_name": "Steadfast Group",
            "connected_insurer": False,
            "notes": "$20M cover — exceeds ACT minimum of $10M.",
            "created_at": _now_iso(),
            "created_by": SEED_USER_ID,
        },
    ]
    for pol in insurance_seed:
        existing = await db.insurance_policies.find_one({
            "building_id": EASTGATE_BUILDING_ID,
            "policy_number": pol["policy_number"],
        })
        if not existing:
            await db.insurance_policies.insert_one(pol)
    logger.info("✅ Insurance policies seeded")

    # ── 4. Compliance Registers ───────────────────────────────────────────────
    register_types_meta = {
        "fire": {
            "standard": "AS 1851",
            "legislation": "AS 1851:2012",
            "description": "Fire Protection System Maintenance Register",
        },
        "lift": {
            "standard": "AS/NZS 1735",
            "legislation": "AS/NZS 1735",
            "description": "Lift and Escalator Safety Register",
        },
        "asbestos": {
            "standard": "SWA Asbestos Code",
            "legislation": "Safe Work Australia Code of Practice",
            "description": "Asbestos Management Register",
        },
        "pool": {
            "standard": "State-specific",
            "legislation": "ACT Public Pools Act",
            "description": "Pool Safety Register",
        },
    }
    reg_ids = {}
    for reg_type, meta in register_types_meta.items():
        existing = await db.compliance_registers.find_one({
            "building_id": EASTGATE_BUILDING_ID,
            "register_type": reg_type,
        })
        if not existing:
            result = await db.compliance_registers.insert_one({
                "building_id": EASTGATE_BUILDING_ID,
                "register_type": reg_type,
                "title": meta["description"],
                "standard": meta["standard"],
                "legislation": meta["legislation"],
                "item_count": 0,
                "overdue_count": 0,
                "review_frequency_months": 12,
                "next_review_date": _date_str(365),
                "created_at": _now_iso(),
                "created_by": SEED_USER_ID,
            })
            reg_ids[reg_type] = str(result.inserted_id)
    logger.info("✅ Compliance registers seeded")

    # ── 5. Sample Audit Record (Eastgate $440K > $250K threshold) ────────────
    existing_audit = await db.audit_records.find_one({
        "building_id": EASTGATE_BUILDING_ID,
        "financial_year": "2025-2026",
    })
    if not existing_audit:
        await db.audit_records.insert_one({
            "building_id": EASTGATE_BUILDING_ID,
            "financial_year": "2025-2026",
            "annual_budget_aud": 440_375.10,
            "lot_count": 87,
            "audit_required": True,
            "audit_requirement_reason": "Budget $440,375 > $250,000 threshold",
            "audit_legislation": "UTMA 2011 s.82",
            "status": "pending",
            "auditor_name": None,
            "auditor_firm": None,
            "audit_opinion": None,
            "agm_date": "2026-05-15",  # expected AGM date
            "report_due_date": "2026-05-01",
            "created_at": _now_iso(),
            "created_by": SEED_USER_ID,
        })
        logger.info("✅ Audit record seeded (required: budget > $250K)")
    else:
        logger.info("   Audit record already exists — skipped")

    # ── 6. Sample Bank Transactions ───────────────────────────────────────────
    sample_txs = [
        {
            "building_id": EASTGATE_BUILDING_ID,
            "account_id": "demo_account_1",
            "fund_type": "admin_fund",
            "external_tx_id": "demo-tx-001",
            "date": _date_str(-3),
            "amount": 2500.00,
            "signed_amount": 2500.00,
            "description": "LEVY PAYMENT - UNIT 45 TH045",
            "type": "credit",
            "balance": 125000.00,
            "matched": True,
            "match_confidence": 100,
            "matched_to_id": None,
            "source": "demo",
            "ingested_at": _now_iso(),
        },
        {
            "building_id": EASTGATE_BUILDING_ID,
            "account_id": "demo_account_1",
            "fund_type": "admin_fund",
            "external_tx_id": "demo-tx-002",
            "date": _date_str(-2),
            "amount": 4200.00,
            "signed_amount": -4200.00,
            "description": "ACTEWAGL ELECTRICITY COMMON AREAS",
            "type": "debit",
            "balance": 120800.00,
            "matched": False,
            "match_confidence": 0,
            "matched_to_id": None,
            "source": "demo",
            "ingested_at": _now_iso(),
        },
        {
            "building_id": EASTGATE_BUILDING_ID,
            "account_id": "demo_account_1",
            "fund_type": "admin_fund",
            "external_tx_id": "demo-tx-003",
            "date": _date_str(-1),
            "amount": 880.00,
            "signed_amount": -880.00,
            "description": "PLUMBING INVOICE INV-2026-0042 UNIT 12",
            "type": "debit",
            "balance": 119920.00,
            "matched": False,
            "match_confidence": 0,
            "matched_to_id": None,
            "source": "demo",
            "ingested_at": _now_iso(),
        },
    ]
    for tx in sample_txs:
        existing = await db.bank_transactions.find_one({
            "building_id": EASTGATE_BUILDING_ID,
            "external_tx_id": tx["external_tx_id"],
        })
        if not existing:
            await db.bank_transactions.insert_one(tx)
    logger.info("✅ Sample bank transactions seeded")

    # ── 7. Sample Decisions ────────────────────────────────────────────────────
    sample_decisions = [
        {
            "building_id": EASTGATE_BUILDING_ID,
            "decision_number": "DEC-2025-0001",
            "meeting_type": "agm",
            "meeting_date": "2025-05-20",
            "motion_title": "Adoption of 2025-2026 Administrative Fund Budget",
            "motion_text": "That the owners corporation adopt the 2025-2026 administrative fund budget of $340,870.20.",
            "resolution_type": "ordinary",
            "vote_for": 65,
            "vote_against": 3,
            "vote_abstain": 1,
            "result": "passed",
            "tags": ["budget", "admin_fund", "2025-2026"],
            "created_at": _now_iso(),
            "created_by": SEED_USER_ID,
        },
        {
            "building_id": EASTGATE_BUILDING_ID,
            "decision_number": "DEC-2025-0002",
            "meeting_type": "agm",
            "meeting_date": "2025-05-20",
            "motion_title": "Levy Interest Rate — Special Resolution",
            "motion_text": "That the owners corporation sets the levy interest rate at 12% per annum pursuant to UTMA 2011 s.76.",
            "resolution_type": "special",
            "vote_for": 72,
            "vote_against": 2,
            "vote_abstain": 0,
            "result": "passed",
            "tags": ["levy_interest", "special_resolution"],
            "created_at": _now_iso(),
            "created_by": SEED_USER_ID,
        },
    ]
    for dec in sample_decisions:
        existing = await db.decisions.find_one({
            "building_id": EASTGATE_BUILDING_ID,
            "decision_number": dec["decision_number"],
        })
        if not existing:
            await db.decisions.insert_one(dec)
    logger.info("✅ Sample decisions seeded")

    logger.info("\n🎉 Compliance demo seed complete!")


async def main() -> None:
    import os
    from dotenv import load_dotenv
    from pymongo import AsyncMongoClient

    load_dotenv()
    mongo_url = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
    db_name = os.getenv("DB_NAME", "strata_production")

    logger.info("Connecting to MongoDB: %s / %s", mongo_url, db_name)
    client = AsyncMongoClient(mongo_url)
    db = client[db_name]

    await seed(db)
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
