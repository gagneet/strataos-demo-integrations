"""
backend/seeds/matching_demo_data.py — Seed 20 synthetic match_review_queue entries
per test building for Phase 3 demo and test purposes.

Covers all eight layer scenarios:
  L1  — exact CRN match (score 1.0) → auto_allocated
  L2  — NPP E2E ID (score 0.95) → auto_allocated
  L3  — partial CRN in description (score 0.90) → pending (at threshold)
  L4  — unit ref + amount + timing (score 0.85) × 2 → pending
  L5  — Jaro-Winkler name + exact amount (score 0.80) × 2 → pending
  L6  — surname fuzzy (score 0.60) × 2 → pending
  L7  — exact amount unique (score 0.50) × 2 → pending
  agency sweep (large deposit from known agency) → pending
  L8  — unidentified × 7 → pending

All records carry is_test_data=True.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import AsyncMongoClient

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BUILDINGS = ["13195", "16244"]
_SLA_HOURS = 24
_NOW = datetime.now(timezone.utc)

# Valid 13-digit MOD10V05 CRNs per building (precomputed via mock_biller.build_crn):
#   "13195" → scheme "013195", lots 001-009
#   "16244" → scheme "016244", lots 001-009
_CRNS: dict[str, list[str]] = {
    "13195": [
        "0131950010015",  # lot 001 inst 001 — MOD10V05 verified
        "0131950020013",  # lot 002 inst 001 — MOD10V05 verified
        "0131950030011",  # lot 003 inst 001 — MOD10V05 verified
    ],
    "16244": [
        "0162440010019",  # lot 001 inst 001 — MOD10V05 verified
        "0162440020017",  # lot 002 inst 001 — MOD10V05 verified
        "0162440030015",  # lot 003 inst 001 — MOD10V05 verified
    ],
}

_LOTS: dict[str, list[dict]] = {
    "13195": [
        {"lot_id": "lot-13195-001", "unit_number": "1", "owner_name": "Margaret Thompson",
         "open_levy_cents": 153000, "due_date": "2026-03-31", "crn": _CRNS["13195"][0]},
        {"lot_id": "lot-13195-002", "unit_number": "2", "owner_name": "David Chen",
         "open_levy_cents": 153000, "due_date": "2026-03-31", "crn": _CRNS["13195"][1]},
        {"lot_id": "lot-13195-003", "unit_number": "3", "owner_name": "Sarah Williams",
         "open_levy_cents": 182500, "due_date": "2026-03-31", "crn": _CRNS["13195"][2]},
        {"lot_id": "lot-13195-004", "unit_number": "4", "owner_name": "Robert Johnson",
         "open_levy_cents": 142000, "due_date": "2026-03-31"},
        {"lot_id": "lot-13195-005", "unit_number": "5", "owner_name": "Jennifer Park",
         "open_levy_cents": 165000, "due_date": "2026-03-31",
         "osko_e2e_id": "NPP20260401EAST001"},
    ],
    "16244": [
        {"lot_id": "lot-16244-001", "unit_number": "1", "owner_name": "Ahmed Al-Hassan",
         "open_levy_cents": 210000, "due_date": "2026-03-31", "crn": _CRNS["16244"][0]},
        {"lot_id": "lot-16244-002", "unit_number": "2", "owner_name": "Lisa Nguyen",
         "open_levy_cents": 210000, "due_date": "2026-03-31", "crn": _CRNS["16244"][1]},
        {"lot_id": "lot-16244-003", "unit_number": "3", "owner_name": "James O'Brien",
         "open_levy_cents": 195000, "due_date": "2026-03-31", "crn": _CRNS["16244"][2]},
        {"lot_id": "lot-16244-004", "unit_number": "4", "owner_name": "Priya Sharma",
         "open_levy_cents": 185000, "due_date": "2026-03-31"},
        {"lot_id": "lot-16244-005", "unit_number": "5", "owner_name": "Michael Brown",
         "open_levy_cents": 220000, "due_date": "2026-03-31",
         "osko_e2e_id": "NPP20260401SIER001"},
    ],
}


def _tx(*, building_id: str, amount_cents: int, description: str,
        bpay_crn: str | None = None, osko_e2e_id: str | None = None,
        lot_ref_raw: str | None = None) -> dict:
    return {
        "provider_txn_id": f"demo-{building_id}-{_NOW.timestamp():.0f}",
        "tenant_id": building_id,
        "account_ref": f"trust-{building_id}",
        "occurred_at": (_NOW - timedelta(days=1)).isoformat(),
        "received_at": _NOW.isoformat(),
        "amount_cents": amount_cents,
        "description": description,
        "bpay_crn": bpay_crn,
        "osko_e2e_id": osko_e2e_id,
        "lot_ref_raw": lot_ref_raw,
        "is_test_data": True,
    }


def _queue_entry(*, building_id: str, scenario: str, tx: dict, candidates: list[dict],
                 best_score: float, best_layer: str, best_lot_id: str | None,
                 match_type: str, status: str,
                 all_scores: list | None = None,
                 inbox_event_id_suffix: str = "") -> dict:
    sla_delta = 48 if match_type == "agency_sweep" else _SLA_HOURS
    return {
        "tenant_id": building_id,
        "building_id": building_id,
        "inbox_event_id": f"demo-{building_id}-{scenario}{inbox_event_id_suffix}",
        "status": status,
        "match_type": match_type,
        "tx": tx,
        "candidates": candidates,
        "all_scores": all_scores or [],
        "best_score": best_score,
        "best_layer": best_layer,
        "best_lot_id": best_lot_id,
        "sla_due_at": (_NOW + timedelta(hours=sla_delta)).isoformat(),
        "created_at": _NOW.isoformat(),
        "decided_at": None,
        "decided_by": None,
        "decision": None,
        "candidates_snapshot": None,
        "is_test_data": True,
    }


def _build_entries(building_id: str) -> list[dict]:
    lots = _LOTS[building_id]
    crns = _CRNS[building_id]
    entries = []

    # L1 — exact CRN (auto_allocated)
    entries.append(_queue_entry(
        building_id=building_id, scenario="l1-exact-crn",
        tx=_tx(building_id=building_id, amount_cents=lots[0]["open_levy_cents"],
               description=f"BPAY LEVY {crns[0]}", bpay_crn=crns[0]),
        candidates=lots,
        best_score=1.0, best_layer="L1_exact_crn", best_lot_id=lots[0]["lot_id"],
        match_type="auto", status="auto_allocated",
    ))

    # L2 — NPP E2E ID (auto_allocated)
    entries.append(_queue_entry(
        building_id=building_id, scenario="l2-npp-e2e",
        tx=_tx(building_id=building_id, amount_cents=lots[4]["open_levy_cents"],
               description="NPP LEVY PAYMENT", osko_e2e_id=lots[4].get("osko_e2e_id")),
        candidates=lots,
        best_score=0.95, best_layer="L2_npp_e2e", best_lot_id=lots[4]["lot_id"],
        match_type="auto", status="auto_allocated",
    ))

    # L3 — partial CRN in description (pending — at default 0.90 threshold)
    entries.append(_queue_entry(
        building_id=building_id, scenario="l3-partial-crn",
        tx=_tx(building_id=building_id, amount_cents=lots[1]["open_levy_cents"],
               description=f"INTERNET BANKING {crns[1][:-1]} LEVY"),
        candidates=lots,
        best_score=0.90, best_layer="L3_partial_crn", best_lot_id=lots[1]["lot_id"],
        match_type="review", status="pending",
    ))

    # L4 — unit ref + amount + timing × 2
    for idx, lot_idx in enumerate([2, 3]):
        lot = lots[lot_idx]
        entries.append(_queue_entry(
            building_id=building_id, scenario=f"l4-unit-ref-{idx + 1}",
            tx=_tx(building_id=building_id, amount_cents=lot["open_levy_cents"],
                   description=f"LEVY PAYMENT UNIT {lot['unit_number']} STRATA",
                   lot_ref_raw=lot["unit_number"]),
            candidates=lots,
            best_score=0.85, best_layer="L4_unit_ref_amount_timing",
            best_lot_id=lot["lot_id"], match_type="review", status="pending",
        ))

    # L5 — Jaro-Winkler name + exact amount × 2
    for idx, lot_idx in enumerate([0, 1]):
        lot = lots[lot_idx]
        # Slight misspelling so JW is needed but above 0.88
        misspelled = lot["owner_name"].replace("a", "e", 1)
        entries.append(_queue_entry(
            building_id=building_id, scenario=f"l5-jw-name-{idx + 1}",
            tx=_tx(building_id=building_id, amount_cents=lot["open_levy_cents"],
                   description=f"EFT FROM {misspelled.upper()}"),
            candidates=lots,
            best_score=0.80, best_layer="L5_jw_name_exact_amount",
            best_lot_id=lot["lot_id"], match_type="review", status="pending",
        ))

    # L6 — surname fuzzy × 2
    for idx, lot_idx in enumerate([2, 3]):
        lot = lots[lot_idx]
        surname = lot["owner_name"].split()[-1]
        entries.append(_queue_entry(
            building_id=building_id, scenario=f"l6-surname-fuzzy-{idx + 1}",
            tx=_tx(building_id=building_id, amount_cents=999900,  # wrong amount
                   description=f"LEVY {surname.upper()}"),
            candidates=lots,
            best_score=0.60, best_layer="L6_surname_fuzzy",
            best_lot_id=lot["lot_id"], match_type="review", status="pending",
        ))

    # L7 — exact amount unique × 2 (use lots[2] and lots[3] with unique amounts)
    for idx, lot_idx in enumerate([2, 3]):
        lot = lots[lot_idx]
        entries.append(_queue_entry(
            building_id=building_id, scenario=f"l7-exact-amount-{idx + 1}",
            tx=_tx(building_id=building_id, amount_cents=lot["open_levy_cents"],
                   description="DIRECT CREDIT STRATA LEVY"),
            candidates=[lots[lot_idx]],  # only 1 candidate — unique amount
            best_score=0.50, best_layer="L7_exact_amount_unique",
            best_lot_id=lot["lot_id"], match_type="review", status="pending",
        ))

    # Agency sweep
    max_levy = max(lot["open_levy_cents"] for lot in lots)
    entries.append(_queue_entry(
        building_id=building_id, scenario="agency-sweep",
        tx=_tx(building_id=building_id, amount_cents=max_levy * 5,
               description="STRATA LEVY COLLECTION RAY WHITE GUNGAHLIN"),
        candidates=lots,
        best_score=0.0, best_layer="", best_lot_id=None,
        match_type="agency_sweep", status="pending",
    ))

    # L8 — unidentified × 7
    for i in range(7):
        entries.append(_queue_entry(
            building_id=building_id, scenario="unidentified",
            tx=_tx(building_id=building_id, amount_cents=50000 + i * 10000,
                   description=f"REF{building_id}{i:03d} UNKNOWN PAYMENT"),
            candidates=lots,
            best_score=0.0, best_layer="L8_unidentified", best_lot_id=None,
            match_type="unidentified", status="pending",
            inbox_event_id_suffix=f"-{i}",
        ))

    return entries


async def seed(db) -> None:
    # Seed payer_entities for agency sweep detection.
    agency = {
        "bsb": "012345",
        "account_number": "987654321",
        "entity_name": "ray white gungahlin",
        "entity_type": "real_estate_agency",
        "created_at": _NOW.isoformat(),
    }
    existing = await db.payer_entities.find_one({"bsb": agency["bsb"],
                                                 "account_number": agency["account_number"]})
    if not existing:
        await db.payer_entities.insert_one(agency)
        logger.info("Inserted payer_entity: %s", agency["entity_name"])
    else:
        logger.info("payer_entity already exists: %s (skipped)", agency["entity_name"])

    for building_id in _BUILDINGS:
        entries = _build_entries(building_id)
        inserted = 0
        for entry in entries:
            existing = await db.match_review_queue.find_one({
                "tenant_id": entry["tenant_id"],
                "inbox_event_id": entry["inbox_event_id"],
            })
            if existing:
                continue
            await db.match_review_queue.insert_one(entry)
            inserted += 1

        logger.info("Seeded %d/%d match_review_queue entries for building=%s",
                    inserted, len(entries), building_id)


async def main() -> None:
    mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.getenv("DB_NAME", "strata_management")
    client = AsyncMongoClient(mongo_url)
    db = client[db_name]
    try:
        await seed(db)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
