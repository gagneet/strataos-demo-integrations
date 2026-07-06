"""
# @featuretrace:demo-seed — Acme Strata Demo customer seed
# Layer: seed
# Data flow: deploy.sh --seed → demo_customer.py → MongoDB tenant records (building-scoped),
#   Postgres core.tenants/schemes/lots (tenant-scoped), feature toggle overrides, sample
#   workflow_requests (building-scoped)
# Related: scripts/deployment/deploy.sh
#          scripts/data_cleanup/remove_legacy_demo_buildings.py
#          docs/architecture/mindmap/01_identity_access.md

Layered seed generator for the Acme Strata Demo customer.

Creates a complete, realistic demo building (StrataOS Demo Residences,
UP-DEMO-001) managed by Acme Strata Demo.  All records use deterministic
UUIDv5 keys so the seed is fully idempotent — running it twice produces
exactly the same state.

Layers (each is a standalone function):
  1 — Structural: tenant, scheme, lots, parties, owners
  2 — Ownership history and transfers (bitemporal)
  3 — Financial structure: funds, GL accounts
  4 — Levy issuances and payment history (arrears on 2 units)
  5 — Feature toggles and portal user accounts
  6 — Cosmetic depth + dashboard v2 signals (maintenance, announcements, workflow, compliance)

Usage:
    cd backend
    python3 seeds/demo_customer.py                  # seed all layers
    python3 seeds/demo_customer.py --layer 4        # seed specific layer
    python3 seeds/demo_customer.py --tear-down      # remove all Acme demo data
"""
from __future__ import annotations

import asyncio
import argparse
import hashlib
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from motor.motor_asyncio import AsyncIOMotorClient
from sqlalchemy import text

from db_postgres.session import async_session_context
from services.financial_core.domain.entities import SchemeRef
from services.financial_core.genesis import (
    SYSTEM_CUTOVER_USER_NAME,
    post_import_based_genesis_cutover,
)

log = logging.getLogger("demo_customer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

# ---------------------------------------------------------------------------
# Deterministic identifiers — uuid5 ensures idempotency across runs
# ---------------------------------------------------------------------------

_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_u5 = lambda key: str(uuid.uuid5(_NS, f"acme-demo:{key}"))  # noqa: E731

ACME_TENANT_ID      = _u5("tenant")
ACME_SCHEME_ID      = _u5("scheme")
ACME_ADMIN_FUND_ID  = _u5("fund:admin")
ACME_SINKING_ID     = _u5("fund:sinking")

# Building / scheme identifiers
ACME_BUILDING_ID    = "UP-DEMO-001"           # MongoDB building_id
ACME_SCHEME_NUMBER  = "UP-DEMO-001"
ACME_SCHEME_NAME    = "StrataOS Demo Residences"
ACME_TENANT_NAME    = "Acme Strata Demo"
ACME_ABN            = "53 004 085 616"        # synthetic, valid check digit format
ACME_JURISDICTION   = "ACT"

_BYPASS_UUID = "00000000-0000-0000-0000-000000000000"

# Reference date — treat as "today" for levy and arrears calculations
_TODAY = date(2026, 5, 4)

# Bcrypt(12) of "DemoUser$01" — same as demo_scheme.py (intentionally public)
_DEMO_PW_HASH = "$2b$12$sz.hfqb5JRxiNuipv8iM1evKkuqFSWxSN3O/WuddAZJGWyBwA.g7q"

# ---------------------------------------------------------------------------
# Layer 1 data: lots + owners
# ---------------------------------------------------------------------------

# 14 lots totalling exactly 1,000 UOE: 10 apartments + 4 townhouses
LOTS = [
    # Apartments A1-A10
    {"lot_number": "1",  "unit_number": "A1",  "unit_type": "apartment",  "uoe": 52},
    {"lot_number": "2",  "unit_number": "A2",  "unit_type": "apartment",  "uoe": 52},
    {"lot_number": "3",  "unit_number": "A3",  "unit_type": "apartment",  "uoe": 55},
    {"lot_number": "4",  "unit_number": "A4",  "unit_type": "apartment",  "uoe": 55},
    {"lot_number": "5",  "unit_number": "A5",  "unit_type": "apartment",  "uoe": 55},
    {"lot_number": "6",  "unit_number": "A6",  "unit_type": "apartment",  "uoe": 58},
    {"lot_number": "7",  "unit_number": "A7",  "unit_type": "apartment",  "uoe": 58},
    {"lot_number": "8",  "unit_number": "A8",  "unit_type": "apartment",  "uoe": 58},
    {"lot_number": "9",  "unit_number": "A9",  "unit_type": "apartment",  "uoe": 58},
    {"lot_number": "10", "unit_number": "A10", "unit_type": "apartment",  "uoe": 49},
    # Townhouses T1-T4
    {"lot_number": "11", "unit_number": "T1",  "unit_type": "townhouse",  "uoe": 112},
    {"lot_number": "12", "unit_number": "T2",  "unit_type": "townhouse",  "uoe": 112},
    {"lot_number": "13", "unit_number": "T3",  "unit_type": "townhouse",  "uoe": 113},
    {"lot_number": "14", "unit_number": "T4",  "unit_type": "townhouse",  "uoe": 113},
]
assert sum(l["uoe"] for l in LOTS) == 1000, "Total UOE must equal 1000"

# Current owners per lot
OWNERS = [
    {"unit": "A1",  "name": "James Mitchell",               "name_b": "",                     "owner_type": "individual", "email": "james.mitchell@acmedemo.au",    "phone": "0411 234 567"},
    {"unit": "A2",  "name": "Sarah Chen",                   "name_b": "David Chen",            "owner_type": "joint",      "email": "s.chen@acmedemo.au",            "phone": "0422 345 678"},
    {"unit": "A3",  "name": "Priya Sharma",                 "name_b": "",                     "owner_type": "individual", "email": "priya.sharma@acmedemo.au",      "phone": "0433 456 789"},
    {"unit": "A4",  "name": "Michael O'Brien",              "name_b": "Emma O'Brien",          "owner_type": "joint",      "email": "mobrien@acmedemo.au",           "phone": "0444 567 890"},
    {"unit": "A5",  "name": "Yuki Tanaka",                  "name_b": "",                     "owner_type": "individual", "email": "yuki.tanaka@acmedemo.au",       "phone": "0455 678 901"},
    {"unit": "A6",  "name": "ABC Investment Trust Pty Ltd", "name_b": "",                     "owner_type": "corporate",  "email": "accounts@abcinvest.au",         "phone": "02 6100 0001"},
    {"unit": "A7",  "name": "Robert Williams",              "name_b": "Anne Williams",         "owner_type": "joint",      "email": "r.williams@acmedemo.au",        "phone": "0477 890 123"},
    {"unit": "A8",  "name": "Neha Gupta",                   "name_b": "",                     "owner_type": "individual", "email": "neha.gupta@acmedemo.au",        "phone": "0488 901 234"},
    {"unit": "A9",  "name": "Thomas Murphy",                "name_b": "Caitlin Murphy",        "owner_type": "joint",      "email": "tmurphy@acmedemo.au",           "phone": "0499 012 345"},
    {"unit": "A10", "name": "Wei Zhang",                    "name_b": "",                     "owner_type": "individual", "email": "wei.zhang@acmedemo.au",         "phone": "0400 123 456"},
    {"unit": "T1",  "name": "Brendan Fraser",               "name_b": "Nicole Fraser",         "owner_type": "joint",      "email": "bfraser@acmedemo.au",           "phone": "0411 222 333"},
    {"unit": "T2",  "name": "Aisha Mohammed",               "name_b": "",                     "owner_type": "individual", "email": "aisha.m@acmedemo.au",           "phone": "0422 333 444"},
    {"unit": "T3",  "name": "Lucas Moreau",                 "name_b": "Isabelle Moreau",       "owner_type": "joint",      "email": "lucas.moreau@acmedemo.au",      "phone": "0433 444 555"},
    {"unit": "T4",  "name": "Rajesh Patel",                 "name_b": "",                     "owner_type": "individual", "email": "rajesh.patel@acmedemo.au",      "phone": "0444 555 666"},
]

# Arrears configuration: A6 = 60-day, A10 = 120-day
_ARREARS_UNITS = {
    "A6":  {"days": 60,  "missed_quarters": 1},
    "A10": {"days": 120, "missed_quarters": 2},
}

# Portal users (Postgres + MongoDB)
PORTAL_USERS = [
    {"email": "manager@acmestrata.demo", "role": "strata_manager", "name": "Alex Demo Manager",   "first": "Alex",   "last": "Manager"},
    {"email": "chair@stratademo.au",     "role": "chairman",        "name": "Cameron Demo Chair",  "first": "Cameron","last": "Chair"},
    {"email": "member@stratademo.au",    "role": "ec_member",       "name": "Jordan Demo Member",  "first": "Jordan", "last": "Member"},
]

# Annual levy totals (including GST)
ADMIN_ANNUAL  = 140_000.00
SINKING_ANNUAL = 70_000.00
TOTAL_UOE = 1000
DEMO_CUTOVER_AS_AT = date(2026, 3, 31)
DEMO_ADMIN_OPENING_BALANCE = 18_750.00
DEMO_SINKING_OPENING_BALANCE = 71_250.00
DEMO_BANK_NAME = "Acme Demo Bank"
DEMO_BANK_PROVIDER = "mock"
DEMO_BANK_MODE = "mock"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mongo_client():
    url = os.getenv("MONGO_URL", "mongodb://localhost:27018")
    return AsyncIOMotorClient(url)

def _mongo_db():
    client = _mongo_client()
    db_name = os.getenv("DB_NAME", "strata_production")
    return client[db_name]

def _lot_uoe(unit_number: str) -> int:
    return next(l["uoe"] for l in LOTS if l["unit_number"] == unit_number)

def _lot_number(unit_number: str) -> str:
    return next(l["lot_number"] for l in LOTS if l["unit_number"] == unit_number)

def _quarterly_levy(uoe: int, fund: str) -> float:
    annual = ADMIN_ANNUAL if fund == "admin" else SINKING_ANNUAL
    return round(annual * uoe / TOTAL_UOE / 4, 2)

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _q_due_date(year: int, quarter: int) -> date:
    return [date(year, 3, 31), date(year, 6, 1), date(year, 9, 1), date(year, 12, 1)][quarter - 1]

# ---------------------------------------------------------------------------
# Layer 1 — Structural (tenant, scheme, lots in Postgres + building/units in Mongo)
# ---------------------------------------------------------------------------

async def layer_1_structural():
    log.info("Layer 1 — structural (Postgres + MongoDB)")

    # Postgres
    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"), {"u": _BYPASS_UUID}
        )

        # Tenant. Acme is a seeded sandbox customer, not the singleton platform
        # demo chain guarded by the global unique is_demo index.
        await session.execute(text("""
            INSERT INTO core.tenants
                (tenant_id, tenant_name, legal_name, abn, is_self_managed, status, is_demo)
            VALUES (:id, :name, :legal, :abn, FALSE, 'active', FALSE)
            ON CONFLICT (tenant_id) DO UPDATE
                SET tenant_name = EXCLUDED.tenant_name,
                    status      = EXCLUDED.status
        """), {"id": ACME_TENANT_ID, "name": ACME_TENANT_NAME,
               "legal": "Acme Strata Pty Ltd", "abn": ACME_ABN})

        # Set tenant context for child inserts
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"), {"u": ACME_TENANT_ID}
        )

        # Scheme
        await session.execute(text("""
            INSERT INTO core.schemes
                (scheme_id, tenant_id, jurisdiction, scheme_number, scheme_name,
                 legal_name, abn, status, is_demo)
            VALUES (:sid, :tid, CAST(:jur AS compliance.jurisdiction_code),
                    :num, :name, :legal, :abn, 'active', FALSE)
            ON CONFLICT (tenant_id, jurisdiction, scheme_number) DO UPDATE
                SET scheme_name = EXCLUDED.scheme_name,
                    status      = EXCLUDED.status
        """), {"sid": ACME_SCHEME_ID, "tid": ACME_TENANT_ID, "jur": ACME_JURISDICTION,
               "num": ACME_SCHEME_NUMBER, "name": ACME_SCHEME_NAME,
               "legal": "The Owners — Strata Plan UP-DEMO-001", "abn": "00 000 000 000"})

        # Lots
        for lot in LOTS:
            lot_id = _u5(f"lot:{lot['lot_number']}")
            await session.execute(text("""
                INSERT INTO core.lots
                    (lot_id, scheme_id, tenant_id, lot_number, unit_number,
                     lot_use, entitlement_units)
                VALUES (:lid, :sid, :tid, :num, :unum, 'residential', :ent)
                ON CONFLICT (scheme_id, lot_number) DO UPDATE
                    SET entitlement_units = EXCLUDED.entitlement_units
            """), {"lid": lot_id, "sid": ACME_SCHEME_ID, "tid": ACME_TENANT_ID,
                   "num": lot["lot_number"], "unum": lot["unit_number"],
                   "ent": lot["uoe"]})

    log.info("  Postgres: tenant, scheme, %d lots — done", len(LOTS))

    # MongoDB — building + units
    db = _mongo_db()

    building_doc = {
        # NOTE: every other db.buildings consumer in this codebase (utils/auth.py's
        # get_current_user()/get_current_building() Mongo-legacy-session building
        # resolution, server.py, cron/*, services/*) queries this collection by the
        # top-level `id` field, not `building_id` — confirmed by grep across the whole
        # backend. This doc previously only set `building_id`, so it silently never
        # matched db.buildings.find_one({"id": ..., "is_active": True}), which made
        # EVERY non-PG-token (i.e. every owner/EC-member) login to this demo tenant
        # fail with 403 "Building not found or inactive" on every authenticated
        # request — a real, live bug, not a hypothetical one, found during the
        # 2026-07-02 financial browser-verification audit.
        "id": ACME_BUILDING_ID,
        "building_id": ACME_BUILDING_ID,
        "name": ACME_SCHEME_NAME,
        "plan_number": ACME_SCHEME_NUMBER,
        "jurisdiction": ACME_JURISDICTION,
        "address": "1 Demo Circuit",
        "suburb": "Canberra",
        "state": "ACT",
        "postcode": "2600",
        "lot_count": len(LOTS),
        "total_uoe": TOTAL_UOE,
        "is_demo": True,
        "is_active": True,
        "status": "active",
        "strata_manager": ACME_TENANT_NAME,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.buildings.update_one(
        {"building_id": ACME_BUILDING_ID},
        {"$set": building_doc},
        upsert=True,
    )

    for lot in LOTS:
        owner = next(o for o in OWNERS if o["unit"] == lot["unit_number"])
        unit_doc = {
            "building_id": ACME_BUILDING_ID,
            "unit_number": lot["unit_number"],
            "lot_number": f"LOT{lot['lot_number']}",
            "unit_type": lot["unit_type"],
            "entitlement": lot["uoe"],
            "owner_name": owner["name"],
            "owner_name_b": owner["name_b"],
            "owner_email": owner["email"],
            "is_demo": True,
            "bedrooms": 3 if lot["unit_type"] == "townhouse" else 2,
            "bathrooms": 2 if lot["unit_type"] == "townhouse" else 1,
            "car_spaces": 2 if lot["unit_type"] == "townhouse" else 1,
            "address": f"{lot['unit_number']}/1 Demo Circuit, Canberra ACT 2600",
            "balance_owing": 0.0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        await db.units.update_one(
            {"building_id": ACME_BUILDING_ID, "unit_number": lot["unit_number"]},
            {"$set": unit_doc},
            upsert=True,
        )

    log.info("  MongoDB: building + %d units — done", len(LOTS))


# ---------------------------------------------------------------------------
# Layer 2 — Ownership history (6 transfers, bitemporal)
# ---------------------------------------------------------------------------

TRANSFERS = [
    {"unit": "A1",  "months_ago": 3,  "prev": "Kevin O'Sullivan",           "settlement_date": _TODAY - timedelta(days=90)},
    {"unit": "A3",  "months_ago": 18, "prev": "David Park",                 "settlement_date": _TODAY - timedelta(days=548)},
    {"unit": "A7",  "months_ago": 6,  "prev": "Jenny Chen",                 "settlement_date": _TODAY - timedelta(days=180)},
    {"unit": "A9",  "months_ago": 9,  "prev": "Karen Lee & Steve Lee",      "settlement_date": _TODAY - timedelta(days=274)},
    {"unit": "T2",  "months_ago": 14, "prev": "Fatima Al-Hassan",           "settlement_date": _TODAY - timedelta(days=425)},
    {"unit": "T3",  "months_ago": 22, "prev": "Marc Dupont",                "settlement_date": _TODAY - timedelta(days=670)},
]


async def layer_2_ownership_history():
    log.info("Layer 2 — ownership history (%d transfers)", len(TRANSFERS))

    # Postgres: ownership_periods
    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"), {"u": ACME_TENANT_ID}
        )
        for t in TRANSFERS:
            lot_id = _u5(f"lot:{_lot_number(t['unit'])}")
            period_id = _u5(f"ownership:{t['unit']}:prev")
            settle = t["settlement_date"]
            new_owner = next(o for o in OWNERS if o["unit"] == t["unit"])
            previous_party_id = _u5(f"party:historical:{t['prev']}")
            current_party_id = _u5(f"party:current:{new_owner['name']}")

            await session.execute(text("""
                INSERT INTO core.parties
                    (party_id, tenant_id, party_type, legal_name, preferred_name, metadata,
                     status, created_at, updated_at)
                VALUES (:id, :tid, 'individual', :name, :name, '{}'::jsonb, 'active', :now, :now)
                ON CONFLICT (party_id) DO UPDATE
                    SET legal_name = EXCLUDED.legal_name,
                        preferred_name = EXCLUDED.preferred_name,
                        updated_at = EXCLUDED.updated_at
            """), {
                "id": previous_party_id,
                "tid": ACME_TENANT_ID,
                "name": t["prev"],
                "now": _now(),
            })
            await session.execute(text("""
                INSERT INTO core.parties
                    (party_id, tenant_id, party_type, legal_name, preferred_name, primary_email, metadata,
                     status, created_at, updated_at)
                VALUES (:id, :tid, 'individual', :name, :name, :email, '{}'::jsonb, 'active', :now, :now)
                ON CONFLICT (party_id) DO UPDATE
                    SET legal_name = EXCLUDED.legal_name,
                        preferred_name = EXCLUDED.preferred_name,
                        primary_email = COALESCE(EXCLUDED.primary_email, core.parties.primary_email),
                        updated_at = EXCLUDED.updated_at
            """), {
                "id": current_party_id,
                "tid": ACME_TENANT_ID,
                "name": new_owner["name"],
                "email": new_owner["email"],
                "now": _now(),
            })

            # Historic period (previous owner)
            await session.execute(text("""
                INSERT INTO core.ownership_periods
                    (ownership_period_id, scheme_id, tenant_id, lot_id, owner_party_id,
                     valid_from, valid_to, recorded_from, source_document_id, notes, created_at)
                VALUES (:id, :sid, :tid, :lid, :party_id, :from_, :to_, :rec, 'demo_seed',
                        'Synthetic historical predecessor', :rec)
                ON CONFLICT (ownership_period_id) DO UPDATE
                    SET owner_party_id = EXCLUDED.owner_party_id,
                        valid_from = EXCLUDED.valid_from,
                        valid_to = EXCLUDED.valid_to,
                        recorded_from = EXCLUDED.recorded_from,
                        notes = EXCLUDED.notes
            """), {
                "id": period_id, "sid": ACME_SCHEME_ID, "tid": ACME_TENANT_ID,
                "lid": lot_id, "party_id": previous_party_id,
                "from_": date(2020, 1, 1),
                "to_": settle,
                "rec": _now(),
            })

            # Current period (new owner)
            current_id = _u5(f"ownership:{t['unit']}:current")
            await session.execute(text("""
                INSERT INTO core.ownership_periods
                    (ownership_period_id, scheme_id, tenant_id, lot_id, owner_party_id,
                     valid_from, valid_to, recorded_from, source_document_id, notes, created_at)
                VALUES (:id, :sid, :tid, :lid, :party_id, :from_, NULL, :rec, 'demo_seed',
                        'Synthetic current ownership period', :rec)
                ON CONFLICT (ownership_period_id) DO UPDATE
                    SET owner_party_id = EXCLUDED.owner_party_id,
                        valid_from = EXCLUDED.valid_from,
                        valid_to = EXCLUDED.valid_to,
                        recorded_from = EXCLUDED.recorded_from,
                        notes = EXCLUDED.notes
            """), {
                "id": current_id, "sid": ACME_SCHEME_ID, "tid": ACME_TENANT_ID,
                "lid": lot_id, "party_id": current_party_id,
                "from_": settle + timedelta(days=1),
                "rec": _now(),
            })

    # MongoDB: ownership_transfer_log
    db = _mongo_db()
    for t in TRANSFERS:
        new_owner = next(o for o in OWNERS if o["unit"] == t["unit"])
        await db.ownership_transfer_log.update_one(
            {"building_id": ACME_BUILDING_ID, "unit_number": t["unit"],
             "transfer_date": t["settlement_date"].isoformat()},
            {"$set": {
                "building_id": ACME_BUILDING_ID,
                "unit_number": t["unit"],
                "transfer_date": t["settlement_date"].isoformat(),
                "previous_owner_name": t["prev"],
                "new_owner_name": new_owner["name"],
                "confidence": "High",
                "data_source": "demo_seed",
                "is_demo": True,
                "imported_at": _now(),
            }},
            upsert=True,
        )

    log.info("  Layer 2 — done")


# ---------------------------------------------------------------------------
# Layer 3 — Financial structure (funds + GL accounts in Postgres, annual_levies in Mongo)
# ---------------------------------------------------------------------------

async def layer_3_financial_structure():
    log.info("Layer 3 — financial structure")

    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"), {"u": ACME_TENANT_ID}
        )

        # Admin Fund
        await session.execute(text("""
            INSERT INTO finance.funds
                (fund_id, scheme_id, tenant_id, fund_code, fund_name, fund_type,
                 opening_balance_cents, status)
            VALUES (:id, :sid, :tid, 'ADMIN', 'Administrative Fund', 'admin',
                    0, 'active')
            ON CONFLICT (fund_id) DO UPDATE SET fund_name = EXCLUDED.fund_name
        """), {"id": ACME_ADMIN_FUND_ID, "sid": ACME_SCHEME_ID, "tid": ACME_TENANT_ID})

        # Sinking Fund
        await session.execute(text("""
            INSERT INTO finance.funds
                (fund_id, scheme_id, tenant_id, fund_code, fund_name, fund_type,
                 opening_balance_cents, status)
            VALUES (:id, :sid, :tid, 'SINK', 'Capital Works Fund', 'capital_works',
                    0, 'active')
            ON CONFLICT (fund_id) DO UPDATE SET fund_name = EXCLUDED.fund_name
        """), {"id": ACME_SINKING_ID, "sid": ACME_SCHEME_ID, "tid": ACME_TENANT_ID})

        existing_journal_count = await session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM finance.journal_entries
                WHERE scheme_id = :scheme_id
                """
            ),
            {"scheme_id": ACME_SCHEME_ID},
        )
        if int(existing_journal_count.scalar() or 0) == 0:
            _, cutover_record = await post_import_based_genesis_cutover(
                session,
                scheme_ref=SchemeRef(
                    tenant_id=uuid.UUID(ACME_TENANT_ID),
                    scheme_id=uuid.UUID(ACME_SCHEME_ID),
                ),
                opening_balances=[
                    {
                        "fund_type": "admin",
                        "opening_balance_cents": int(round(DEMO_ADMIN_OPENING_BALANCE * 100)),
                        "as_at_date": DEMO_CUTOVER_AS_AT.isoformat(),
                        "bank_name": DEMO_BANK_NAME,
                        "account_name": "Acme Strata Demo Administration Fund",
                        "bsb": "062-000",
                        "account_number": "12345678",
                        "evidence_source": "synthetic_demo_cutover_seed",
                        "notes": "Synthetic demo bootstrap approved for deploy seed.",
                    },
                    {
                        "fund_type": "sinking",
                        "opening_balance_cents": int(round(DEMO_SINKING_OPENING_BALANCE * 100)),
                        "as_at_date": DEMO_CUTOVER_AS_AT.isoformat(),
                        "bank_name": DEMO_BANK_NAME,
                        "account_name": "Acme Strata Demo Capital Works Fund",
                        "bsb": "062-000",
                        "account_number": "87654321",
                        "evidence_source": "synthetic_demo_cutover_seed",
                        "notes": "Synthetic demo bootstrap approved for deploy seed.",
                    },
                ],
                posted_by_user_id=None,
                posted_by_user_name=SYSTEM_CUTOVER_USER_NAME,
                building_id=ACME_BUILDING_ID,
                is_test_data=False,
                bank_provider=DEMO_BANK_PROVIDER,
                bank_mode=DEMO_BANK_MODE,
                enable_external_api_finance=False,
                enable_shadow_reads=False,
            )
            log.info(
                "  Postgres: canonical cutover bootstrap — done (%s)",
                ", ".join(cutover_record["enabled_feature_keys"]),
            )
        else:
            log.info("  Postgres: existing journal entries detected — skipping genesis bootstrap")

    log.info("  Postgres: admin + sinking funds — done")

    # MongoDB: annual_levies
    db = _mongo_db()
    _now_dt = _now()
    for year_offset, year in enumerate([2025, 2026]):
        doc = {
            "building_id": ACME_BUILDING_ID,
            "year": str(year),
            "status": "actual" if year == 2025 else "proposed",
            "is_demo": True,
            "total_uoe": TOTAL_UOE,
            "admin_levy_per_uoe_annual": round(ADMIN_ANNUAL / TOTAL_UOE, 4),
            "admin_levy_per_uoe_quarterly": round(ADMIN_ANNUAL / TOTAL_UOE / 4, 4),
            "sinking_levy_per_uoe_annual": round(SINKING_ANNUAL / TOTAL_UOE, 4),
            "sinking_levy_per_uoe_quarterly": round(SINKING_ANNUAL / TOTAL_UOE / 4, 4),
            "admin_fund": {
                "levy_income": ADMIN_ANNUAL,
                "total_expenses": round(ADMIN_ANNUAL * 0.91, 2),
                "opening_balance": 12_000.00 if year == 2025 else 15_000.00,
                "closing_balance": 15_000.00 if year == 2025 else 0.0,
            },
            "sinking_fund": {
                "levy_income": SINKING_ANNUAL,
                "total_expenses": round(SINKING_ANNUAL * 0.35, 2) if year == 2025 else 0.0,
                "opening_balance": 45_000.00 if year == 2025 else 69_500.00,
                "closing_balance": 69_500.00 if year == 2025 else 0.0,
            },
            "payment_schedule": [
                {"quarter": f"Q{q}", "due_date": _q_due_date(year, q).isoformat()}
                for q in range(1, 5)
            ],
            "proposed_admin_expenses": round(ADMIN_ANNUAL * 0.95, 0),
            "proposed_sinking_expenses": round(SINKING_ANNUAL * 0.4, 0),
            "category_breakdown": {
                "insurance": 32000.0,
                "sinking_fund": SINKING_ANNUAL,
                "management_fee": 14500.0,
                "cleaning_grounds": 23500.0,
                "maintenance": 26500.0,
                "utilities": 11800.0,
                "other": 31700.0,
            },
            "created_at": _now_dt,
            "updated_at": _now_dt,
        }
        await db.annual_levies.update_one(
            {"building_id": ACME_BUILDING_ID, "year": str(year)},
            {"$set": doc},
            upsert=True,
        )

    log.info("  MongoDB: annual_levies (2025-2026) — done")


# ---------------------------------------------------------------------------
# Layer 4 — Levy issuances + payment history
# ---------------------------------------------------------------------------

async def layer_4_levies_and_payments():
    log.info("Layer 4 — levies and payments")
    db = _mongo_db()
    _now_dt = _now()

    # Generate quarters: all of 2025 + Q1 2026
    quarters = [(2025, q) for q in range(1, 5)] + [(2026, 1)]

    # Units in arrears (don't pay certain quarters)
    arrears_skip: dict[str, list[tuple]] = {
        "A6":  [(2026, 1)],                  # missed Q1 2026 → ~34 days overdue
        "A10": [(2025, 4), (2026, 1)],        # missed Q4 2025 + Q1 2026 → ~155 days oldest
    }

    total_levied = 0
    total_paid = 0

    for lot in LOTS:
        un = lot["unit_number"]
        uoe = lot["uoe"]
        skipped = arrears_skip.get(un, [])

        admin_levied = 0.0
        sinking_levied = 0.0
        admin_paid = 0.0
        sinking_paid = 0.0

        for year, q in quarters:
            admin_q = _quarterly_levy(uoe, "admin")
            sinking_q = _quarterly_levy(uoe, "sinking")
            due_date = _q_due_date(year, q)
            is_skipped = (year, q) in skipped

            admin_levied += admin_q
            sinking_levied += sinking_q

            if not is_skipped:
                # Realistic payment delay: 5-20 days after due date
                delay_seed = (ord(un[0]) + q + year) % 16 + 5
                paid_date = due_date + timedelta(days=delay_seed)
                # Only record payment if due date is in the past
                if due_date <= _TODAY:
                    admin_paid += admin_q
                    sinking_paid += sinking_q
                    pay_id = _u5(f"payment:{un}:{year}:Q{q}")
                    payment_doc = {
                        "id": pay_id,
                        "building_id": ACME_BUILDING_ID,
                        "unit_number": un,
                        "lot_number": f"LOT{lot['lot_number']}",
                        "fund_type": "both",
                        "amount": round(admin_q + sinking_q, 2),
                        "amount_cents": int(round((admin_q + sinking_q) * 100)),
                        "admin_amount": admin_q,
                        "sinking_amount": sinking_q,
                        "year": str(year),
                        "quarter": f"Q{q}",
                        "due_date": due_date.isoformat(),
                        "paid_date": paid_date.isoformat(),
                        "status": "confirmed",
                        "is_demo": True,
                        "created_at": _now_dt.isoformat(),
                        "updated_at": _now_dt.isoformat(),
                    }
                    await db.levy_payments.update_one(
                        {"id": pay_id}, {"$set": payment_doc}, upsert=True
                    )

        # unit_levy_ledger entry for 2025 (full year) and 2026 (YTD)
        for year, qs in [(2025, [1, 2, 3, 4]), (2026, [1])]:
            admin_l = sum(_quarterly_levy(uoe, "admin") for _ in qs)
            sink_l = sum(_quarterly_levy(uoe, "sinking") for _ in qs)
            admin_p = sum(
                _quarterly_levy(uoe, "admin")
                for q in qs if (year, q) not in skipped and _q_due_date(year, q) <= _TODAY
            )
            sink_p = sum(
                _quarterly_levy(uoe, "sinking")
                for q in qs if (year, q) not in skipped and _q_due_date(year, q) <= _TODAY
            )
            ledger_id = _u5(f"ledger:{un}:{year}")
            await db.unit_levy_ledger.update_one(
                {"id": ledger_id},
                {"$set": {
                    "id": ledger_id,
                    "building_id": ACME_BUILDING_ID,
                    "unit_number": un,
                    "lot_number": f"LOT{lot['lot_number']}",
                    "year": str(year),
                    "uoe": uoe,
                    "admin_levied": round(admin_l, 2),
                    "sinking_levied": round(sink_l, 2),
                    "admin_paid": round(admin_p, 2),
                    "sinking_paid": round(sink_p, 2),
                    "total_levied": round(admin_l + sink_l, 2),
                    "total_paid": round(admin_p + sink_p, 2),
                    "opening_arrears": 0.0,
                    "admin_closing": round(admin_p - admin_l, 2),
                    "sinking_closing": round(sink_p - sink_l, 2),
                    "net_balance": round((admin_l + sink_l) - (admin_p + sink_p), 2),
                    "is_demo": True,
                }},
                upsert=True,
            )

        # Update units.balance_owing for arrears units
        if un in arrears_skip:
            owing = sum(
                _quarterly_levy(uoe, "admin") + _quarterly_levy(uoe, "sinking")
                for (year, q) in skipped if _q_due_date(year, q) <= _TODAY
            )
            await db.units.update_one(
                {"building_id": ACME_BUILDING_ID, "unit_number": un},
                {"$set": {"balance_owing": round(owing, 2)}},
            )
        total_levied += admin_levied + sinking_levied
        total_paid += admin_paid + sinking_paid

    arrears_units = [un for un in arrears_skip]
    log.info("  Levy records: %d lots, total levied=$%.0f, total paid=$%.0f",
             len(LOTS), total_levied, total_paid)
    log.info("  Arrears: %s", arrears_units)
    log.info("  Layer 4 — done")


# ---------------------------------------------------------------------------
# Layer 5 — Feature toggles + portal user accounts
# ---------------------------------------------------------------------------

# Feature toggles that should be ON for a real customer by default
_DEFAULT_ON_FEATURES = [
    "maintenance", "announcements", "documents", "levy_management",
    "financial_reporting", "compliance", "community", "smart_requests",
    "owner_dashboard", "tenant_portal", "work_orders", "e_voting",
    "strata_roll", "budget_management", "parcels", "proposals",
    "volunteer", "savings",
]


def _normalize_pg_user_role(role: str) -> str:
    if role == "chairman":
        return "ec_member"
    return role


def _ec_position_for_raw_role(raw_role: str) -> str | None:
    """Map a legacy raw role slug (e.g. PORTAL_USERS' 'chairman') to an ec_position
    value. Returns None for roles that aren't EC-member-shaped at all."""
    if _normalize_pg_user_role(raw_role) != "ec_member":
        return None
    return {
        "chairman": "CHAIRMAN",
        "treasurer": "TREASURER",
        "secretary": "SECRETARY",
    }.get(str(raw_role).strip().lower(), "MEMBER")

async def layer_5_toggles_and_users():
    log.info("Layer 5 — feature toggles + users")
    db = _mongo_db()

    # Feature toggle overrides for Acme demo building
    for feature in _DEFAULT_ON_FEATURES:
        await db.feature_toggles.update_one(
            {"building_id": ACME_BUILDING_ID, "feature_key": feature},
            {"$set": {
                "building_id": ACME_BUILDING_ID,
                "feature_key": feature,
                "is_enabled": True,
                "is_demo": True,
                "updated_at": _now(),
            }},
            upsert=True,
        )

    # Portal user accounts in MongoDB
    for pu in PORTAL_USERS:
        user_id = _u5(f"user:{pu['email']}")
        # 'chairman' is not a top-level role (see rules/post-compact-critical.md) — normalize
        # before writing to Mongo (the live operational store) the same way the Postgres path
        # below does, and carry the EC sub-position across so chairman-specific UI/permission
        # checks (user.ec_position === 'CHAIRMAN') still work for this seeded demo user.
        mongo_role = _normalize_pg_user_role(pu["role"])
        ec_position = _ec_position_for_raw_role(pu["role"])
        user_doc = {
            "id": user_id,
            "building_id": ACME_BUILDING_ID,
            "email": pu["email"],
            "full_name": pu["name"],
            "role": mongo_role,
            "password_hash": _DEMO_PW_HASH,
            "is_approved": True,
            "is_active": True,
            "is_demo": True,
            "created_at": _now(),
        }
        if ec_position:
            user_doc["ec_position"] = ec_position
        await db.users.update_one(
            {"building_id": ACME_BUILDING_ID, "email": pu["email"]},
            {"$set": user_doc},
            upsert=True,
        )
        # db.memberships is what utils/auth.py's get_current_user() legacy-Mongo-session
        # path actually authorizes non-super_admin access against (a user.building_id
        # field alone is NOT sufficient — see the 2026-07-02 financial browser-
        # verification audit finding: this was previously never written for ANY of this
        # seed's users, so every non-PG-token session for this tenant got a 403 "You do
        # not have access to this building" on every authenticated request).
        await db.memberships.update_one(
            {"building_id": ACME_BUILDING_ID, "user_id": user_id},
            {"$set": {
                "id": _u5(f"membership:{pu['email']}"),
                "user_id": user_id,
                "building_id": ACME_BUILDING_ID,
                "roles": [mongo_role],
                "is_active": True,
                "is_primary": True,
                "units": [],
                "updated_at": _now(),
            },
             "$setOnInsert": {"created_at": _now()}},
            upsert=True,
        )

    # Owner user accounts for the 14 owners
    for owner in OWNERS:
        user_id = _u5(f"user:owner:{owner['unit']}")
        uoe = _lot_uoe(owner["unit"])
        await db.users.update_one(
            {"building_id": ACME_BUILDING_ID, "unit_number": owner["unit"],
             "role": "owner"},
            {"$set": {
                "id": user_id,
                "building_id": ACME_BUILDING_ID,
                "unit_number": owner["unit"],
                "email": owner["email"],
                "full_name": owner["name"],
                "role": "owner",
                "password_hash": _DEMO_PW_HASH,
                "is_approved": True,
                "is_active": True,
                "is_demo": True,
                "created_at": _now(),
            }},
            upsert=True,
        )
        await db.memberships.update_one(
            {"building_id": ACME_BUILDING_ID, "user_id": user_id},
            {"$set": {
                "id": _u5(f"membership:owner:{owner['unit']}"),
                "user_id": user_id,
                "building_id": ACME_BUILDING_ID,
                "roles": ["owner"],
                "is_active": True,
                "is_primary": True,
                "units": [owner["unit"]],
                "updated_at": _now(),
            },
             "$setOnInsert": {"created_at": _now()}},
            upsert=True,
        )

    log.info("  %d feature toggles, %d portal users, %d owner accounts — done",
             len(_DEFAULT_ON_FEATURES), len(PORTAL_USERS), len(OWNERS))

    # Postgres: portal users + role assignments
    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"), {"u": ACME_TENANT_ID}
        )
        for pu in PORTAL_USERS:
            pg_role = _normalize_pg_user_role(pu["role"])
            ec_position = _ec_position_for_raw_role(pu["role"])
            pg_uid = _u5(f"pguser:{pu['email']}")
            result = await session.execute(text("""
                INSERT INTO core.users
                    (user_id, tenant_id, email, full_name, first_name, last_name,
                     password_hash, role, default_scheme_id, is_active, is_approved)
                VALUES
                    (:uid, :tid, :email, :name, :fn, :ln, :pw,
                     CAST(:role AS core.user_role), :scheme_id, TRUE, TRUE)
                ON CONFLICT (tenant_id, email) DO UPDATE
                    SET full_name = EXCLUDED.full_name,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        role = EXCLUDED.role,
                        default_scheme_id = EXCLUDED.default_scheme_id,
                        password_hash = EXCLUDED.password_hash,
                        is_active = TRUE,
                        is_approved = TRUE,
                        updated_at = NOW()
                RETURNING user_id::TEXT
            """), {
                "uid": pg_uid, "tid": ACME_TENANT_ID, "email": pu["email"],
                "name": pu["name"], "fn": pu["first"], "ln": pu["last"],
                "pw": _DEMO_PW_HASH,
                "role": pg_role,
                "scheme_id": ACME_SCHEME_ID,
            })
            pg_uid = result.scalar_one()
            await session.execute(text("""
                INSERT INTO core.user_role_assignments
                    (tenant_id, user_id, scheme_id, role, ec_position, is_active)
                VALUES (:tid, :uid, :sid, CAST(:role AS core.user_role), :ec_position, TRUE)
                ON CONFLICT (user_id, scheme_id, role)
                WHERE scheme_id IS NOT NULL
                DO UPDATE SET is_active = TRUE,
                              granted_at = NOW(),
                              ec_position = COALESCE(EXCLUDED.ec_position, core.user_role_assignments.ec_position)
            """), {
                "tid": ACME_TENANT_ID,
                "uid": pg_uid,
                "sid": ACME_SCHEME_ID,
                "role": pg_role,
                "ec_position": ec_position,
            })

    log.info("  Postgres: %d portal users — done", len(PORTAL_USERS))


# ---------------------------------------------------------------------------
# Layer 6 — Cosmetic depth + dashboard v2 signals
# ---------------------------------------------------------------------------

async def layer_6_cosmetic_depth():
    log.info("Layer 6 — cosmetic depth + dashboard v2 signals")
    db = _mongo_db()
    now = _now()

    # Announcements
    announcements = [
        {
            "id": _u5("announce:agm-notice"),
            "title": "Annual General Meeting — 15 June 2026",
            "body": "The 2026 AGM for StrataOS Demo Residences will be held on "
                    "15 June 2026 at 6:30 PM in the Level 1 meeting room. "
                    "Agenda and proxy forms will be distributed by 1 June 2026.",
            "category": "governance",
            "is_pinned": True,
        },
        {
            "id": _u5("announce:lift-maintenance"),
            "title": "Lift Maintenance — 20 May 2026",
            "body": "Scheduled lift maintenance will be carried out on 20 May 2026 "
                    "between 8:00 AM and 12:00 PM. During this time both lifts may "
                    "be out of service. Please plan accordingly.",
            "category": "maintenance",
            "is_pinned": False,
        },
        {
            "id": _u5("announce:agm-date-locked"),
            "title": "AGM date locked — 12 August 2026",
            "body": "The committee has locked the AGM date for 12 August 2026. Proxy reminders will start 14 days before close.",
            "category": "governance",
            "is_pinned": True,
        },
    ]
    for ann in announcements:
        await db.announcements.update_one(
            {"building_id": ACME_BUILDING_ID, "id": ann["id"]},
            {"$set": {**ann, "building_id": ACME_BUILDING_ID,
                      "is_demo": True, "created_at": now.isoformat(), "updated_at": now.isoformat(), "is_active": True}},
            upsert=True,
        )

    await db.agm.update_one(
        {"building_id": ACME_BUILDING_ID, "id": _u5("agm:2026")},
        {"$set": {
            "id": _u5("agm:2026"),
            "building_id": ACME_BUILDING_ID,
            "title": "Annual General Meeting 2026",
            "date": "2026-08-12",
            "time": "18:30",
            "location": "Level 1 Meeting Room",
            "status": "scheduled",
            "proxy_deadline": "2026-08-05",
            "is_demo": True,
            "is_test_data": False,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }},
        upsert=True,
    )

    # Maintenance requests
    maint = [
        {
            "id": _u5("maint:lobby-lights"),
            "title": "Lobby lighting fault — Level 1",
            "description": "Three fluorescent tubes in the Level 1 lobby have failed.",
            "category": "electrical", "priority": "medium",
            "status": "in_progress", "unit_number": "A3",
        },
        {
            "id": _u5("maint:garage-door"),
            "title": "Garage door slow response — Bay 7",
            "description": "The B1 garage roller door takes 30+ seconds to open.",
            "category": "mechanical", "priority": "low",
            "status": "open", "unit_number": "T1",
        },
    ]
    for req in maint:
        await db.maintenance_requests.update_one(
            {"building_id": ACME_BUILDING_ID, "id": req["id"]},
            {"$set": {**req, "building_id": ACME_BUILDING_ID,
                      "is_demo": True, "created_at": now}},
            upsert=True,
        )

    # Workflow requests used by dashboard /analytics/diff-since fallback.
    workflow_requests = [
        {
            "id": _u5("workflow:roof-waterproofing"),
            "request_number": "WF-2001",
            "request_type": "maintenance",
            "title": "Roof waterproofing quote follow-up",
            "description": "Awaiting committee confirmation before contractor booking.",
            "unit_number": "T2",
            "priority": "high",
            "status": "awaiting_review",
            "sla_breached": True,
        },
        {
            "id": _u5("workflow:bylaw-pets"),
            "request_number": "WF-2002",
            "request_type": "governance",
            "title": "Pet by-law clarification request",
            "description": "Owner requested clarification on balcony pet enclosures.",
            "unit_number": "A7",
            "priority": "normal",
            "status": "in_progress",
            "sla_breached": False,
        },
    ]
    for req in workflow_requests:
        await db.workflow_requests.update_one(
            {"building_id": ACME_BUILDING_ID, "id": req["id"]},
            {"$set": {
                **req,
                "building_id": ACME_BUILDING_ID,
                "submitted_by_user_id": _u5("user:owner:A7"),
                "is_demo": True,
                "is_test_data": False,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )

    # Compliance items used by compliance cards and /analytics/diff-since fallback.
    compliance_items = [
        {
            "id": _u5("compliance:fire-door-audit"),
            "title": "Annual fire door inspection",
            "status": "pending",
            "risk_level": "medium",
            "due_date": (_TODAY + timedelta(days=14)).isoformat(),
        },
        {
            "id": _u5("compliance:exit-lighting"),
            "title": "Emergency exit lighting certification",
            "status": "overdue",
            "risk_level": "high",
            "due_date": (_TODAY - timedelta(days=9)).isoformat(),
        },
    ]
    for item in compliance_items:
        await db.compliance_items.update_one(
            {"building_id": ACME_BUILDING_ID, "id": item["id"]},
            {"$set": {
                **item,
                "building_id": ACME_BUILDING_ID,
                "is_demo": True,
                "is_test_data": False,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        )

    # Dashboard v2 cockpit depth: mirrors tasks/new-dashboard/manager.jsx
    # without touching production East Gate data.
    admin_user_id = _u5("user:manager@acmestrata.demo")
    chair_user_id = _u5("user:chair@stratademo.au")
    owner_a6_id = _u5("user:owner:A6")

    richer_workflows = [
        {"id": _u5("workflow:levy-overdue-a6"), "request_number": "UA042", "request_type": "levy_query", "title": "Levy overdue 90 days", "description": "A6 is 90 days behind on quarterly levies and needs Notice 1.", "unit_number": "A6", "priority": "urgent", "status": "overdue", "sla_breached": True, "created_at": now - timedelta(days=9), "submitted_by_user_id": owner_a6_id},
        {"id": _u5("workflow:lift-service"), "request_number": "WO-318", "request_type": "maintenance", "title": "Quarterly lift service overdue", "description": "Lift contractor service window has passed; assign vendor and notify residents.", "unit_number": "Common", "priority": "urgent", "status": "overdue", "sla_breached": True, "created_at": now - timedelta(days=3), "submitted_by_user_id": admin_user_id},
        {"id": _u5("workflow:invoice-bluepoint"), "request_number": "INV-1142", "request_type": "invoice_approval", "title": "BluePoint Plumbing invoice awaiting approval", "description": "Validated invoice for basement pump repair is ready for approval.", "unit_number": "B1", "priority": "high", "status": "overdue", "sla_breached": True, "created_at": now - timedelta(days=2), "submitted_by_user_id": admin_user_id},
        {"id": _u5("workflow:payment-plan-t4"), "request_number": "TH074", "request_type": "levy_query", "title": "Payment plan request", "description": "Owner requested a staged repayment plan for arrears.", "unit_number": "T4", "priority": "high", "status": "overdue", "sla_breached": True, "created_at": now - timedelta(days=1), "submitted_by_user_id": _u5("user:owner:T4")},
        {"id": _u5("workflow:agm-proxies"), "request_number": "AGM-26", "request_type": "governance", "title": "Proxy forms not yet returned", "description": "Follow up 7 owners before the AGM proxy deadline.", "unit_number": "All", "priority": "normal", "status": "overdue", "sla_breached": True, "created_at": now - timedelta(hours=18), "submitted_by_user_id": chair_user_id},
        {"id": _u5("workflow:bylaw-nudge"), "request_number": "BY-09", "request_type": "governance", "title": "Pet by-law motion reminder", "description": "Committee voting reminder for BY-09 before close of business Friday.", "unit_number": "All", "priority": "normal", "status": "overdue", "sla_breached": True, "created_at": now - timedelta(hours=12), "submitted_by_user_id": chair_user_id},
        {"id": _u5("workflow:autoresolved-pet"), "request_number": "AUTO-221", "request_type": "bylaw_query", "title": "Pet by-law FAQ auto-resolved", "description": "Smart intake answered a resident by-law question using approved wording.", "unit_number": "A7", "priority": "low", "status": "auto_resolved", "sla_breached": False, "created_at": now - timedelta(hours=3), "submitted_by_user_id": _u5("user:owner:A7")},
    ]
    for req in richer_workflows:
        created = req.pop("created_at")
        await db.workflow_requests.update_one(
            {"building_id": ACME_BUILDING_ID, "id": req["id"]},
            {"$set": {
                **req,
                "building_id": ACME_BUILDING_ID,
                "subject": req["title"],
                "body": req["description"],
                "assigned_to": admin_user_id if req["status"] != "auto_resolved" else None,
                "assigned_to_name": "Alex Demo Manager" if req["status"] != "auto_resolved" else None,
                "needs_human_review": req["status"] in {"overdue", "awaiting_review"},
                "auto_resolved": req["status"] == "auto_resolved",
                "auto_resolution_attempted": True,
                "auto_resolution_confidence": 0.91 if req["status"] == "auto_resolved" else 0.42,
                "sla_due_at": (created + timedelta(hours=48)).isoformat(),
                "is_demo": True,
                "is_test_data": False,
                "created_at": created.isoformat(),
                "updated_at": now.isoformat(),
            }},
            upsert=True,
        )

    work_orders = [
        {"id": _u5("wo:lift-service"), "title": "Quarterly lift service", "status": "overdue", "vendor_name": "Sterling Lifts", "approved_budget": 4200.0, "created_at": now - timedelta(days=20)},
        {"id": _u5("wo:garage-door"), "title": "Garage roller door repair", "status": "pending_approval", "vendor_name": "East Coast Electrical", "approved_budget": 6800.0, "created_at": now - timedelta(days=4)},
        {"id": _u5("wo:pump-repair"), "title": "Basement pump repair", "status": "completed", "vendor_name": "BluePoint Plumbing", "approved_budget": 3840.0, "created_at": now - timedelta(days=18), "completed_at": now - timedelta(days=2)},
    ]
    for wo in work_orders:
        await db.work_orders.update_one(
            {"building_id": ACME_BUILDING_ID, "id": wo["id"]},
            {"$set": {**wo, "building_id": ACME_BUILDING_ID, "is_demo": True, "is_test_data": False, "updated_at": now.isoformat()}},
            upsert=True,
        )

    spend_jobs = [
        ("BluePoint Plumbing", 14200, 12),
        ("East Coast Electrical", 8900, 7),
        ("Sterling Lifts", 11200, 4),
        ("GreenScape Gardens", 7600, 24),
    ]
    for idx, (vendor, spend, jobs) in enumerate(spend_jobs):
        await db.maintenance_requests.update_one(
            {"building_id": ACME_BUILDING_ID, "id": _u5(f"maint:completed:{idx}")},
            {"$set": {
                "id": _u5(f"maint:completed:{idx}"),
                "building_id": ACME_BUILDING_ID,
                "title": f"Completed vendor work - {vendor}",
                "description": "Synthetic completed work for the dashboard v2 vendor scorecard.",
                "status": "completed",
                "vendor_name": vendor,
                "assigned_vendor": vendor,
                "actual_cost": float(spend),
                "jobs": jobs,
                "completed_at": (now - timedelta(days=idx * 24 + 6)).isoformat(),
                "is_demo": True,
                "is_test_data": False,
                "created_at": (now - timedelta(days=idx * 24 + 30)).isoformat(),
                "updated_at": now.isoformat(),
            }},
            upsert=True,
        )

    for row in [
        (2026, 71_250, 70_000, 18_500, 122_750, {"pump": 8500}),
        (2027, 122_750, 72_500, 24_000, 171_250, {"fire_panel": 14000, "roof_membrane": 10000}),
        (2028, 171_250, 75_000, 36_000, 210_250, {"garage_door": 18000, "painting": 18000}),
        (2029, 210_250, 78_000, 190_000, 98_250, {"facade": 190000}),
        (2030, 98_250, 82_000, 42_000, 138_250, {"security": 22000, "landscape": 20000}),
        (2031, 138_250, 86_000, 31_000, 193_250, {"waterproofing": 31000}),
        (2032, 193_250, 91_000, 210_000, 74_250, {"lift_cab": 210000}),
        (2033, 74_250, 96_000, 29_000, 141_250, {"carpark": 29000}),
        (2034, 141_250, 101_000, 33_000, 209_250, {"balustrades": 33000}),
        (2035, 209_250, 106_000, 41_000, 274_250, {"paint_touchups": 41000}),
    ]:
        year, opening, contribution, expenditure, closing, cats = row
        await db.sinking_fund_plan.update_one(
            {"building_id": ACME_BUILDING_ID, "year": str(year)},
            {"$set": {
                "building_id": ACME_BUILDING_ID,
                "year": str(year),
                "opening_balance": opening,
                "contribution": contribution,
                "expenditure": expenditure,
                "closing_balance": closing,
                "categories": cats,
                "category_labels": {k: k.replace("_", " ").title() for k in cats},
                "is_actual": year == 2026,
                "is_demo": True,
                "is_test_data": False,
                "updated_at": now.isoformat(),
            }},
            upsert=True,
        )

    for idx, item in enumerate([
        {"id": _u5("compliance:lift-quarterly"), "title": "Lift - Quarterly Service", "status": "overdue", "risk_level": "high", "due_date": (now.date() - timedelta(days=3)).isoformat()},
        {"id": _u5("compliance:insurance-renewal"), "title": "Building Insurance Renewal", "status": "pending", "risk_level": "medium", "due_date": (now.date() + timedelta(days=16)).isoformat()},
        {"id": _u5("compliance:fire-cert-lodged"), "title": "Fire Safety Certificate Lodged", "status": "completed", "risk_level": "low", "due_date": (now.date() - timedelta(days=1)).isoformat()},
        {"id": _u5("compliance:whs-risk"), "title": "WHS - Risk Assessment", "status": "pending", "risk_level": "medium", "due_date": (now.date() + timedelta(days=99)).isoformat()},
    ]):
        await db.compliance_items.update_one(
            {"building_id": ACME_BUILDING_ID, "id": item["id"]},
            {"$set": {**item, "building_id": ACME_BUILDING_ID, "is_demo": True, "is_test_data": False, "created_at": (now - timedelta(days=idx + 1)).isoformat(), "updated_at": now.isoformat()}},
            upsert=True,
        )

    for proposal in [
        {"id": _u5("proposal:pet-bylaw"), "proposal_number": "BY-09", "title": "Pet by-law motion", "description": "Clarify balcony pet enclosure approval process.", "proposal_type": "bylaw_change", "status": "open", "voting_closes_at": (now + timedelta(hours=36)).isoformat(), "voting_deadline": (now + timedelta(hours=36)).isoformat(), "votes_for": 4, "votes_against": 1, "votes_abstain": 0, "total_lots": len(LOTS)},
        {"id": _u5("proposal:visitor-parking"), "proposal_number": "BY-08", "title": "Visitor parking enforcement", "description": "Introduce a clearer breach workflow for repeat visitor bay misuse.", "proposal_type": "governance", "status": "passed", "voting_closes_at": (now - timedelta(days=1)).isoformat(), "voting_deadline": (now - timedelta(days=1)).isoformat(), "votes_for": 9, "votes_against": 2, "votes_abstain": 1, "total_lots": len(LOTS)},
    ]:
        await db.proposals.update_one(
            {"building_id": ACME_BUILDING_ID, "id": proposal["id"]},
            {"$set": {**proposal, "building_id": ACME_BUILDING_ID, "created_by": chair_user_id, "created_by_name": "Cameron Demo Chair", "is_demo": True, "is_test_data": False, "created_at": (now - timedelta(days=4)).isoformat(), "updated_at": now.isoformat()}},
            upsert=True,
        )

    await db.parcels.update_one(
        {"building_id": ACME_BUILDING_ID, "id": _u5("parcel:a6-auspost")},
        {"$set": {"id": _u5("parcel:a6-auspost"), "building_id": ACME_BUILDING_ID, "unit_number": "A6", "carrier": "auspost", "status": "received", "received_date": (now - timedelta(days=2)).isoformat(), "is_demo": True, "is_test_data": False, "created_at": (now - timedelta(days=2)).isoformat()}},
        upsert=True,
    )

    await db.savings_events.update_one(
        {"building_id": ACME_BUILDING_ID, "id": _u5("saving:insurance-tender")},
        {"$set": {"id": _u5("saving:insurance-tender"), "building_id": ACME_BUILDING_ID, "category": "insurance", "description": "Insurance renewal tender saved against incumbent quote.", "original_cost_cents": 4820000, "final_cost_cents": 4215000, "amount_saved_cents": 605000, "saved_cents": 605000, "saving_method": "Three-quote market test", "resident_summary": "Insurance renewal tender saved the building $6,050 this week.", "financial_year": "2026", "verified": True, "shown_to": [], "is_demo": True, "is_test_data": False, "date": (now - timedelta(days=1)).isoformat(), "created_at": (now - timedelta(hours=10)).isoformat(), "updated_at": now.isoformat()}},
        upsert=True,
    )

    for event in [
        {"id": _u5("volunteer:garden-day"), "title": "Courtyard planting morning", "status": "open", "scheduled_date": (now + timedelta(days=12)).isoformat(), "event_date": (now + timedelta(days=12)).isoformat(), "estimated_contractor_cost_cents": 180000, "registered_count": 5},
        {"id": _u5("volunteer:bulk-waste"), "title": "Bulk waste room reset", "status": "completed", "scheduled_date": (now - timedelta(days=21)).isoformat(), "event_date": (now - timedelta(days=21)).isoformat(), "estimated_contractor_cost_cents": 95000, "registered_count": 7},
    ]:
        await db.volunteer_events.update_one(
            {"building_id": ACME_BUILDING_ID, "id": event["id"]},
            {"$set": {**event, "building_id": ACME_BUILDING_ID, "description": "Demo volunteer event for community dashboard depth.", "location": "Level 1 courtyard", "created_by": admin_user_id, "created_by_name": "Alex Demo Manager", "credit_cents_per_hour": 2500, "estimated_hours": 2.5, "is_demo": True, "is_test_data": False, "created_at": (now - timedelta(days=14)).isoformat(), "updated_at": now.isoformat()}},
            upsert=True,
        )

    await db.capital_shock_risks.update_one(
        {"building_id": ACME_BUILDING_ID},
        {"$set": {
            "building_id": ACME_BUILDING_ID,
            "capital_shock_index": {
                "rows": [
                    {"year": 2029, "description": "Facade remediation", "estimated_cost": 190000, "risk_level": "high"},
                    {"year": 2032, "description": "Lift cab replacement", "estimated_cost": 210000, "risk_level": "high"},
                    {"year": 2034, "description": "Balustrade renewal", "estimated_cost": 33000, "risk_level": "medium"},
                ],
                "next_shock": {"year": 2029, "description": "Facade remediation", "estimated_cost": 190000, "risk_level": "high"},
            },
            "is_demo": True,
            "is_test_data": False,
            "updated_at": now.isoformat(),
        }},
        upsert=True,
    )

    await db.levy_fairness_results_v2.update_one(
        {"building_id": ACME_BUILDING_ID},
        {"$set": {
            "building_id": ACME_BUILDING_ID,
            "score": 84.6,
            "lbfi_score": 84.6,
            "grade": "Good",
            "lbfi": {"current_score": 84.6, "D": 0.1543},
            "overpay_group": "Townhouses",
            "overpay_amount": 5672,
            "underpay_group": "Apartments",
            "underpay_amount": 5672,
            "impact_by_group": [
                {"group_name": "Townhouses", "net_subsidy": 5672},
                {"group_name": "Apartments", "net_subsidy": -5672},
                {"group_name": "Lift-served units", "net_subsidy": 1271},
            ],
            "top_drivers": [
                {"name": "Lift System", "annual_amount": 3564, "share_pct": 0.63},
                {"name": "Common Area Finishes", "annual_amount": 1271, "share_pct": 0.22},
                {"name": "Balcony Waterproofing", "annual_amount": 578, "share_pct": 0.10},
            ],
            "drivers": [
                {"name": "Lift System", "annual_amount": 3564, "share_pct": 0.63},
                {"name": "Common Area Finishes", "annual_amount": 1271, "share_pct": 0.22},
                {"name": "Balcony Waterproofing", "annual_amount": 578, "share_pct": 0.10},
            ],
            "computed_at": now.isoformat(),
            "is_demo": True,
            "is_test_data": False,
        }},
        upsert=True,
    )

    await db.building_summaries.update_one(
        {"building_id": ACME_BUILDING_ID},
        {"$set": {
            "id": ACME_BUILDING_ID + "_summary",
            "building_id": ACME_BUILDING_ID,
            "total_lots": len(LOTS),
            "occupied_lots": len(LOTS),
            "total_levies_ytd_cents": 5250000,
            "arrears_cents": 1207500,
            "arrears_rate": 23.0,
            "arrears_lots": 2,
            "sinking_fund_balance_cents": 12275000,
            "admin_fund_balance_cents": 1875000,
            "open_maintenance_requests": 5,
            "open_work_orders": 2,
            "overdue_work_orders": 1,
            "open_proposals": 1,
            "volunteer_events_ytd": 1,
            "savings_ytd_cents": 605000,
            "health_score": 72,
            "health_grade": "B+",
            "building_health_score": 72,
            "building_health_grade": "B+",
            "next_compliance_item": "Building Insurance Renewal",
            "registered_pets": 4,
            "open_smart_requests": 5,
            "upcoming_bookings": 2,
            "financial_year": "2026",
            "is_demo": True,
            "is_test_data": False,
            "computed_at": now.isoformat(),
        }},
        upsert=True,
    )

    # Quarter-level rows feed the owner PaymentStreakCard without changing
    # annual building totals used by finance/building-overview.
    for owner in OWNERS:
        unit = owner["unit"]
        broken = unit in {"A6", "A10"}
        for idx, (year, quarter) in enumerate([(2026, "Q1"), (2025, "Q4"), (2025, "Q3"), (2025, "Q2"), (2025, "Q1")]):
            paid_on_time = not (broken and idx == 0)
            quarter_year = f"{year}-{quarter}"
            await db.unit_levy_ledger.update_one(
                {"id": _u5(f"ledger-quarter:{unit}:{year}:{quarter}")},
                {"$set": {
                    "id": _u5(f"ledger-quarter:{unit}:{year}:{quarter}"),
                    "building_id": ACME_BUILDING_ID,
                    "unit_number": unit,
                    "lot_number": f"LOT{_lot_number(unit)}",
                    # Keep quarter rows unique against the (building_id, year, unit_number) index.
                    "year": quarter_year,
                    "year_numeric": year,
                    "quarter": quarter,
                    "status": "overdue" if not paid_on_time else "paid",
                    "paid_on_time": paid_on_time,
                    "due_date": _q_due_date(year, int(quarter[1])).isoformat(),
                    "is_demo": True,
                    "is_test_data": False,
                }},
                upsert=True,
            )

    log.info(
        "  %d announcements, %d maintenance requests, %d workflow requests, %d compliance items — done",
        len(announcements),
        len(maint),
        len(workflow_requests),
        len(compliance_items),
    )


# ---------------------------------------------------------------------------
# Tear-down
# ---------------------------------------------------------------------------

async def tear_down():
    log.info("TEAR DOWN — removing all Acme Strata Demo data")

    # MongoDB
    db = _mongo_db()
    mongo_collections = [
        "buildings", "units", "users", "annual_levies", "unit_levy_ledger",
        "levy_payments", "feature_toggles", "announcements", "maintenance_requests",
        "workflow_requests", "compliance_items", "ownership_transfer_log", "strata_owners", "agm",
        "work_orders", "sinking_fund_plan", "proposals", "parcels",
        "savings_events", "volunteer_events", "capital_shock_risks",
        "levy_fairness_results_v2", "building_summaries",
        "dashboard_v2_signals",
    ]
    for coll_name in mongo_collections:
        result = await db[coll_name].delete_many({"building_id": ACME_BUILDING_ID})
        if result.deleted_count:
            log.info("  MongoDB %-35s  deleted=%d", coll_name, result.deleted_count)

    # Postgres — in FK order
    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"), {"u": _BYPASS_UUID}
        )
        for tbl in [
            "finance.funds", "core.ownership_periods",
            "core.lots", "core.users", "core.schemes",
        ]:
            try:
                r = await session.execute(
                    text(f"DELETE FROM {tbl} WHERE tenant_id = :tid RETURNING 1"),  # noqa: S608
                    {"tid": ACME_TENANT_ID},
                )
                cnt = len(r.fetchall())
                if cnt:
                    log.info("  Postgres %-35s  deleted=%d", tbl, cnt)
            except Exception as exc:
                log.warning("  Postgres %-35s  SKIP — %s", tbl, exc)

        await session.execute(
            text("DELETE FROM core.tenants WHERE tenant_id = :tid"),
            {"tid": ACME_TENANT_ID},
        )
        log.info("  Postgres core.tenants deleted")

    log.info("Tear-down complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def layer_7_dashboard_v2_signals():
    """
    Seed `dashboard_v2_signals` documents consumed by GET /analytics/dashboard-v2-extras.
    Provides: emergency_contacts, building insurance, suburb market signals,
    per-unit cost categories (council/water/insurance/land tax/utilities),
    per-unit comparable sales, per-unit badges, and per-unit open requests.
    Idempotent — every document is keyed by (building_id, scope, [unit_number]).
    """
    db = _mongo_db()
    now = _now()
    log.info(
        "LAYER 7 — Acme dashboard_v2_signals (emergency contacts, market, cost-of-ownership, badges)"
    )

    BUILDING_SIGNALS = {
        "building_id": ACME_BUILDING_ID,
        "scope": "building",
        "is_demo": True,
        "is_test_data": False,
        "updated_at": now.isoformat(),
        "emergency_contacts": [
            {"label": "Building Manager",  "phone": "02 6100 4421",  "detail": "Mon–Fri 9am–5pm"},
            {"label": "After hours",       "phone": "0411 220 994",  "detail": "Urgent only"},
            {"label": "Lift hotline",      "phone": "1300 442 938",  "detail": "Sterling Lifts"},
            {"label": "Plumbing",          "phone": "02 6109 3300",  "detail": "BluePoint"},
        ],
        "insurance": {
            "policy": "Acme Strata Demo - building insurance",
            "renews_on": (now + timedelta(days=42)).date().isoformat(),
            "no_premium_hike": True,
            "insurer": "Sample Mutual",
        },
        "market_signals": {
            "suburb":               "Denman Prospect",
            "benchmark_label":      "ACT median",
            "median_levy_pct_delta": -8.2,
        },
    }
    await db.dashboard_v2_signals.update_one(
        {"building_id": ACME_BUILDING_ID, "scope": "building"},
        {"$set": BUILDING_SIGNALS},
        upsert=True,
    )

    # Per-unit market estimates (vary by unit_type and uoe; conservative demo numbers).
    suburb_median_2br = 875_000
    suburb_median_3br = 1_120_000
    trend_years_apt   = ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]
    trend_apt         = [690_000, 720_000, 755_000, 800_000, 845_000, 880_000, 905_000]
    trend_th          = [880_000, 920_000, 975_000, 1_030_000, 1_080_000, 1_115_000, 1_155_000]

    comps_apartments = [
        {"unit": "A2",  "sold_date": (now - timedelta(days=22)).date().isoformat(), "price": 905_000, "bedrooms": 2},
        {"unit": "A8",  "sold_date": (now - timedelta(days=48)).date().isoformat(), "price": 945_000, "bedrooms": 2},
        {"unit": "A5",  "sold_date": (now - timedelta(days=70)).date().isoformat(), "price": 882_000, "bedrooms": 2},
    ]
    comps_townhouses = [
        {"unit": "T1",  "sold_date": (now - timedelta(days=35)).date().isoformat(), "price": 1_180_000, "bedrooms": 3},
        {"unit": "T3",  "sold_date": (now - timedelta(days=82)).date().isoformat(), "price": 1_140_000, "bedrooms": 3},
    ]

    for lot in LOTS:
        is_townhouse = lot["unit_type"] == "townhouse"
        estimate     = 1_155_000 if is_townhouse else 905_000
        rental_yield = 4.5       if is_townhouse else 4.1
        days_on_mkt  = 24        if is_townhouse else 21
        bldg_premium = 5.4       if is_townhouse else 5.1
        median       = suburb_median_3br if is_townhouse else suburb_median_2br
        comps        = comps_townhouses  if is_townhouse else comps_apartments

        # Per-unit cost categories (council/water/insurance/land tax/utilities).
        cost_categories = [
            {"name": "Council rates",       "annual": 2400 if is_townhouse else 2240, "color": "#0EA5E9"},
            {"name": "Water (fixed share)", "annual": 620  if is_townhouse else 540,  "color": "#16A34A"},
            {"name": "Building insurance",  "annual": 1320 if is_townhouse else 1180, "color": "#7C3AED"},
            {"name": "Land tax",            "annual": 3400 if is_townhouse else 2980, "color": "#E11D48"},
            {"name": "Utilities + comms",   "annual": 1380 if is_townhouse else 1236, "color": "#F59E0B"},
        ]

        signal_doc = {
            "building_id":          ACME_BUILDING_ID,
            "scope":                "unit",
            "unit_number":          lot["unit_number"],
            "lot_number":           lot["lot_number"],
            "unit_type":            lot["unit_type"],
            "is_demo":              True,
            "is_test_data":         False,
            "updated_at":           now.isoformat(),
            "badges":               {
                "active_voter": lot["unit_number"] in {"A2", "A4", "A7", "T1", "T3"},
                "event_host":   lot["unit_number"] in {"A1", "T2"},
            },
            "market_signals": {
                "estimate":             estimate,
                "suburb_median":        median,
                "yoy_pct":              6.2 if is_townhouse else 6.0,
                "rental_yield_pct":     rental_yield,
                "days_on_market":       days_on_mkt,
                "building_premium_pct": bldg_premium,
                "trend_years":          trend_years_apt,
                "trend_values":         trend_th if is_townhouse else trend_apt,
                "comparable_sales":     comps,
                "benchmark_label":      "ACT median",
                "median_levy_pct_delta": -8.2,
            },
            "cost_categories":     cost_categories,
            "cost_yoy_delta_pct":  -3.2,
            "owner_requests":      [
                {
                    "id":        _u5(f"v2-req:{lot['unit_number']}:1"),
                    "reference": f"REQ-12{lot['lot_number'].zfill(2)}A",
                    "status":    "in_progress",
                    "title":     "Balcony tile lifting · contractor scheduled",
                    "summary":   "Last update 2h ago by BluePoint · ETA 1.5 days",
                    "needs_reply": False,
                    "href":      "/dashboard/maintenance",
                },
                {
                    "id":        _u5(f"v2-req:{lot['unit_number']}:2"),
                    "reference": f"REQ-12{lot['lot_number'].zfill(2)}B",
                    "status":    "awaiting_owner",
                    "title":     "Photo of leak requested",
                    "summary":   "Manager requested access window details before dispatch.",
                    "needs_reply": True,
                    "href":      "/dashboard/maintenance",
                },
            ] if lot["unit_number"] in {"A1", "A2", "A4", "T1"} else [],
        }
        await db.dashboard_v2_signals.update_one(
            {"building_id": ACME_BUILDING_ID, "scope": "unit", "unit_number": lot["unit_number"]},
            {"$set": signal_doc},
            upsert=True,
        )

    log.info(
        "  building signals + %d unit signals — done",
        len(LOTS),
    )


_ALL_LAYERS = [
    layer_1_structural,
    layer_2_ownership_history,
    layer_3_financial_structure,
    layer_4_levies_and_payments,
    layer_5_toggles_and_users,
    layer_6_cosmetic_depth,
    layer_7_dashboard_v2_signals,
]


async def seed(layers: list[int] | None = None):
    log.info("=== Acme Strata Demo seed (UP-DEMO-001) ===")
    funcs = [_ALL_LAYERS[i - 1] for i in layers] if layers else _ALL_LAYERS
    for fn in funcs:
        await fn()
    log.info("=== Seed complete ===")


def main():
    parser = argparse.ArgumentParser(description="Seed Acme Strata Demo customer")
    parser.add_argument("--tear-down", action="store_true", help="Remove all Acme demo data")
    parser.add_argument("--layer", type=int, metavar="N", action="append",
                        help="Only run specific layer(s) (1-6, repeatable)")
    args = parser.parse_args()

    if args.tear_down:
        asyncio.run(tear_down())
    else:
        asyncio.run(seed(args.layer))


if __name__ == "__main__":
    main()
