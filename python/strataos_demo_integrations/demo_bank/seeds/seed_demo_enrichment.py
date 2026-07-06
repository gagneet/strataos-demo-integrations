"""
Demo data enrichment for Sierra (16244) and Harbourview (18932).

Adds the financial and activity data that the /dashboard page needs:
  - annual_levies      (2025, 2026 per building)
  - unit_levy_ledger   (per unit per year)
  - announcements      (3 per building)
  - meetings           (1 past AGM + 1 upcoming EC meeting)
  - events             (1 upcoming community event)
  - maintenance_requests (2 per building)

IMPORTANT: Does NOT touch building_id 13195 (East Gate Residences).

Run standalone:
  python3 -m seeds.seed_demo_enrichment
"""

import uuid
from datetime import datetime, timezone, timedelta

import asyncio

try:
    from database import db
except ImportError:
    db = None

NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()


def _id() -> str:
    return str(uuid.uuid4())


# ─── Sierra (16244) ────────────────────────────────────────────────────────────

SIERRA = "16244"
SIERRA_UNITS_OWNED = ["S001", "S002", "S003", "S010", "S015", "S020", "S025", "S030", "S035"]
SIERRA_ADMIN_LEVY_PER_UNIT = 2500.00  # per year
SIERRA_SINKING_LEVY_PER_UNIT = 750.00  # per year


def _sierra_annual_levy(year: str) -> dict:
    n = len(SIERRA_UNITS_OWNED)
    admin_total = SIERRA_ADMIN_LEVY_PER_UNIT * n
    sinking_total = SIERRA_SINKING_LEVY_PER_UNIT * n
    admin_expenses = round(admin_total * 0.82, 2)
    sinking_expenses = round(sinking_total * 0.50, 2)
    admin_opening = round(admin_total * 0.10, 2)
    sinking_opening = round(sinking_total * 0.30, 2)
    return {
        "id": _id(),
        "plan_id": SIERRA,
        "building_id": SIERRA,
        "year": year,
        "status": "approved",
        "data_origin": "seed",
        "is_seed_data": True,
        "total_uoe": float(n),
        "period_note": f"FY {year}–{int(year) + 1}",
        "admin_fund": {
            "levy_income": admin_total,
            "other_income": 0.0,
            "total_income": admin_total,
            "total_expenses": admin_expenses,
            "opening_balance": admin_opening,
            "closing_balance": round(admin_opening + admin_total - admin_expenses, 2),
            "surplus_deficit": round(admin_total - admin_expenses, 2),
            "current_balance": round(admin_opening + admin_total - admin_expenses, 2),
        },
        "admin_levy_per_uoe_annual": SIERRA_ADMIN_LEVY_PER_UNIT,
        "admin_levy_per_uoe_quarterly": round(SIERRA_ADMIN_LEVY_PER_UNIT / 4, 2),
        "sinking_fund": {
            "levy_income": sinking_total,
            "other_income": 0.0,
            "total_income": sinking_total,
            "total_expenses": sinking_expenses,
            "opening_balance": sinking_opening,
            "closing_balance": round(sinking_opening + sinking_total - sinking_expenses, 2),
            "surplus_deficit": round(sinking_total - sinking_expenses, 2),
            "current_balance": round(sinking_opening + sinking_total - sinking_expenses, 2),
        },
        "sinking_levy_per_uoe_annual": SIERRA_SINKING_LEVY_PER_UNIT,
        "sinking_levy_per_uoe_quarterly": round(SIERRA_SINKING_LEVY_PER_UNIT / 4, 2),
        "payment_schedule": "quarterly",
        "notes": f"Sierra {year} levy schedule — demo seed data",
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
    }


def _sierra_ledger_entries(year: str) -> list:
    entries = []
    paid_ratio = 0.90 if year == "2025" else 0.80
    for i, unit in enumerate(SIERRA_UNITS_OWNED):
        admin_levied = SIERRA_ADMIN_LEVY_PER_UNIT
        sinking_levied = SIERRA_SINKING_LEVY_PER_UNIT
        admin_paid = round(admin_levied * paid_ratio, 2)
        sinking_paid = round(sinking_levied * paid_ratio, 2)
        entries.append({
            "id": _id(),
            "plan_id": SIERRA,
            "building_id": SIERRA,
            "year": year,
            "unit_number": unit,
            "lot_number": i + 1,
            "uoe": 1.0,
            "property_type": "apartment",
            "admin_opening": 0.0,
            "admin_levied": admin_levied,
            "admin_paid": admin_paid,
            "admin_closing": round(admin_levied - admin_paid, 2),
            "sinking_opening": 0.0,
            "sinking_levied": sinking_levied,
            "sinking_paid": sinking_paid,
            "sinking_closing": round(sinking_levied - sinking_paid, 2),
            "total_levied": round(admin_levied + sinking_levied, 2),
            "total_paid": round(admin_paid + sinking_paid, 2),
            "net_balance": round((admin_levied - admin_paid) + (sinking_levied - sinking_paid), 2),
            "created_at": NOW_ISO,
            "updated_at": NOW_ISO,
        })
    return entries


SIERRA_ANNOUNCEMENTS = [
    {
        "id": _id(),
        "title": "Welcome to Sierra Residents Portal",
        "content": "We are pleased to launch the Sierra digital residents portal. You can now view levy notices, access community documents, and stay connected with your neighbours.",
        "priority": "normal",
        "is_public": True,
        "expires_at": None,
        "created_by": "system",
        "created_by_name": "James Chen (Chairman)",
        "created_at": (NOW - timedelta(days=14)).isoformat(),
        "history": [],
        "building_id": SIERRA,
    },
    {
        "id": _id(),
        "title": "Q2 2026 Levy Notices Issued",
        "content": "Quarterly levy notices for Q2 2026 have been issued. Payment is due by 1 June 2026. Owners can pay via BPAY or direct transfer. Contact the strata manager if you need a payment arrangement.",
        "priority": "high",
        "is_public": True,
        "expires_at": None,
        "created_by": "system",
        "created_by_name": "Building Manager",
        "created_at": (NOW - timedelta(days=7)).isoformat(),
        "history": [],
        "building_id": SIERRA,
    },
    {
        "id": _id(),
        "title": "Lobby Renovation — Works Start 15 April",
        "content": "The lobby renovation project approved at the AGM will commence on 15 April 2026. Works are expected to take 3 weeks. Alternative entrance via car park level B during this period.",
        "priority": "normal",
        "is_public": True,
        "expires_at": None,
        "created_by": "system",
        "created_by_name": "Lisa Wang (Secretary)",
        "created_at": (NOW - timedelta(days=3)).isoformat(),
        "history": [],
        "building_id": SIERRA,
    },
]

SIERRA_MEETINGS = [
    {
        "id": _id(),
        "title": "Annual General Meeting 2025",
        "description": "Annual General Meeting for Sierra Owners Corporation. Election of EC members, approval of 2025-2026 budget, and general business.",
        "meeting_date": (NOW - timedelta(days=60)).strftime("%Y-%m-%dT18:00:00+10:00"),
        "location": "Sierra Common Room, Level 1",
        "agenda": ["Election of EC members", "Adoption of 2025-2026 budget", "Levy schedule approval",
                   "General business"],
        "attendees": [],
        "minutes": "AGM held successfully. James Chen re-elected as Chairperson. Budget of $29,250 approved for 2026.",
        "status": "completed",
        "created_by": "system",
        "created_at": (NOW - timedelta(days=65)).isoformat(),
        "updated_at": (NOW - timedelta(days=55)).isoformat(),
        "building_id": SIERRA,
    },
    {
        "id": _id(),
        "title": "EC Meeting — April 2026",
        "description": "Monthly Executive Committee meeting. Review maintenance requests, lobby renovation update, and financial report.",
        "meeting_date": (NOW + timedelta(days=12)).strftime("%Y-%m-%dT18:30:00+10:00"),
        "location": "Sierra Common Room, Level 1",
        "agenda": ["Lobby renovation progress", "Maintenance requests review", "Q1 financial report",
                   "Any other business"],
        "attendees": [],
        "minutes": "",
        "status": "scheduled",
        "created_by": "system",
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
        "building_id": SIERRA,
    },
]

SIERRA_EVENTS = [
    {
        "id": _id(),
        "title": "Community BBQ — Welcome Spring",
        "description": "Join your Sierra neighbours for a community BBQ on the rooftop terrace. All residents welcome. BYOB — food provided by the EC.",
        "event_type": "social",
        "start_date": (NOW + timedelta(days=18)).strftime("%Y-%m-%dT12:00:00+10:00"),
        "end_date": (NOW + timedelta(days=18)).strftime("%Y-%m-%dT15:00:00+10:00"),
        "location": "Sierra Rooftop Terrace, Level 9",
        "is_recurring": False,
        "recurrence_rule": None,
        "source": "building",
        "source_url": None,
        "is_public": False,
        "created_by": "system",
        "created_at": NOW_ISO,
        "building_id": SIERRA,
    },
]

SIERRA_MAINTENANCE = [
    {
        "id": _id(),
        "title": "Lift B Inspection Required",
        "description": "Lift B (south core) is showing intermittent fault codes. Annual compliance inspection is due. Lift technician to be engaged.",
        "category": "Lift/Elevator",
        "location": "South Core, all levels",
        "priority": "high",
        "images": [],
        "status": "approved",
        "submitted_by": "system",
        "submitted_by_name": "David Park (Treasurer)",
        "assigned_contractor": None,
        "contractor_name": None,
        "estimated_cost": 1800.0,
        "actual_cost": None,
        "purchase_order_id": None,
        "invoice_id": None,
        "approval_history": [],
        "notes": "Schedule with ACT Lift Services before end of April.",
        "created_at": (NOW - timedelta(days=5)).isoformat(),
        "updated_at": (NOW - timedelta(days=5)).isoformat(),
        "completed_at": None,
        "building_id": SIERRA,
    },
    {
        "id": _id(),
        "title": "Pool Pump Replacement",
        "description": "The main pool circulation pump has failed. Replacement pump ordered. Pool currently closed until repairs complete.",
        "category": "Pool/Spa",
        "location": "Level 2 Pool Area",
        "priority": "medium",
        "images": [],
        "status": "in_progress",
        "submitted_by": "system",
        "submitted_by_name": "James Chen (Chairman)",
        "assigned_contractor": None,
        "contractor_name": "Aqua Services Pty Ltd",
        "estimated_cost": 3200.0,
        "actual_cost": None,
        "purchase_order_id": None,
        "invoice_id": None,
        "approval_history": [],
        "notes": "Parts arrived. Installation scheduled for this week.",
        "created_at": (NOW - timedelta(days=10)).isoformat(),
        "updated_at": (NOW - timedelta(days=2)).isoformat(),
        "completed_at": None,
        "building_id": SIERRA,
    },
]

# ─── Harbourview (18932) ───────────────────────────────────────────────────────

HARBOUR = "18932"
HARBOUR_UNITS_OWNED = ["H001", "H002", "H010"]
HARBOUR_ADMIN_LEVY_PER_UNIT = 3600.00
HARBOUR_SINKING_LEVY_PER_UNIT = 1200.00


def _harbour_annual_levy(year: str) -> dict:
    n = len(HARBOUR_UNITS_OWNED)
    admin_total = HARBOUR_ADMIN_LEVY_PER_UNIT * n
    sinking_total = HARBOUR_SINKING_LEVY_PER_UNIT * n
    admin_expenses = round(admin_total * 0.78, 2)
    sinking_expenses = round(sinking_total * 0.45, 2)
    admin_opening = round(admin_total * 0.15, 2)
    sinking_opening = round(sinking_total * 0.40, 2)
    return {
        "id": _id(),
        "plan_id": HARBOUR,
        "building_id": HARBOUR,
        "year": year,
        "status": "approved",
        "data_origin": "seed",
        "is_seed_data": True,
        "total_uoe": float(n),
        "period_note": f"FY {year}–{int(year) + 1}",
        "admin_fund": {
            "levy_income": admin_total,
            "other_income": 0.0,
            "total_income": admin_total,
            "total_expenses": admin_expenses,
            "opening_balance": admin_opening,
            "closing_balance": round(admin_opening + admin_total - admin_expenses, 2),
            "surplus_deficit": round(admin_total - admin_expenses, 2),
            "current_balance": round(admin_opening + admin_total - admin_expenses, 2),
        },
        "admin_levy_per_uoe_annual": HARBOUR_ADMIN_LEVY_PER_UNIT,
        "admin_levy_per_uoe_quarterly": round(HARBOUR_ADMIN_LEVY_PER_UNIT / 4, 2),
        "sinking_fund": {
            "levy_income": sinking_total,
            "other_income": 0.0,
            "total_income": sinking_total,
            "total_expenses": sinking_expenses,
            "opening_balance": sinking_opening,
            "closing_balance": round(sinking_opening + sinking_total - sinking_expenses, 2),
            "surplus_deficit": round(sinking_total - sinking_expenses, 2),
            "current_balance": round(sinking_opening + sinking_total - sinking_expenses, 2),
        },
        "sinking_levy_per_uoe_annual": HARBOUR_ADMIN_LEVY_PER_UNIT,
        "sinking_levy_per_uoe_quarterly": round(HARBOUR_SINKING_LEVY_PER_UNIT / 4, 2),
        "payment_schedule": "quarterly",
        "notes": f"Harbourview {year} levy schedule — demo seed data",
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
    }


def _harbour_ledger_entries(year: str) -> list:
    entries = []
    paid_ratio = 1.0 if year == "2025" else 0.75
    for i, unit in enumerate(HARBOUR_UNITS_OWNED):
        admin_levied = HARBOUR_ADMIN_LEVY_PER_UNIT
        sinking_levied = HARBOUR_SINKING_LEVY_PER_UNIT
        admin_paid = round(admin_levied * paid_ratio, 2)
        sinking_paid = round(sinking_levied * paid_ratio, 2)
        entries.append({
            "id": _id(),
            "plan_id": HARBOUR,
            "building_id": HARBOUR,
            "year": year,
            "unit_number": unit,
            "lot_number": i + 1,
            "uoe": 1.0,
            "property_type": "apartment",
            "admin_opening": 0.0,
            "admin_levied": admin_levied,
            "admin_paid": admin_paid,
            "admin_closing": round(admin_levied - admin_paid, 2),
            "sinking_opening": 0.0,
            "sinking_levied": sinking_levied,
            "sinking_paid": sinking_paid,
            "sinking_closing": round(sinking_levied - sinking_paid, 2),
            "total_levied": round(admin_levied + sinking_levied, 2),
            "total_paid": round(admin_paid + sinking_paid, 2),
            "net_balance": round((admin_levied - admin_paid) + (sinking_levied - sinking_paid), 2),
            "created_at": NOW_ISO,
            "updated_at": NOW_ISO,
        })
    return entries


HARBOUR_ANNOUNCEMENTS = [
    {
        "id": _id(),
        "title": "Welcome to Harbourview Residents Portal",
        "content": "Welcome to the Harbourview digital community portal. Access levy notices, building documents, and stay informed about your building.",
        "priority": "normal",
        "is_public": True,
        "expires_at": None,
        "created_by": "system",
        "created_by_name": "Helen Morris (Chairman)",
        "created_at": (NOW - timedelta(days=10)).isoformat(),
        "history": [],
        "building_id": HARBOUR,
    },
    {
        "id": _id(),
        "title": "Marina View Terrace — Maintenance Works",
        "content": "Waterproofing and tile works on the marina-view terrace will take place 7–14 April 2026. The terrace will be inaccessible during this period. We apologise for any inconvenience.",
        "priority": "high",
        "is_public": True,
        "expires_at": None,
        "created_by": "system",
        "created_by_name": "Building Manager",
        "created_at": (NOW - timedelta(days=5)).isoformat(),
        "history": [],
        "building_id": HARBOUR,
    },
    {
        "id": _id(),
        "title": "2026 Levy Notices — Q2",
        "content": "Q2 2026 levy notices are now available. Quarterly levies of $1,200 (sinking) and $900 (admin) per unit are due by 1 June 2026.",
        "priority": "normal",
        "is_public": True,
        "expires_at": None,
        "created_by": "system",
        "created_by_name": "Building Manager",
        "created_at": (NOW - timedelta(days=2)).isoformat(),
        "history": [],
        "building_id": HARBOUR,
    },
]

HARBOUR_MEETINGS = [
    {
        "id": _id(),
        "title": "Annual General Meeting 2025",
        "description": "Annual General Meeting for Harbourview Residences Owners Corporation.",
        "meeting_date": (NOW - timedelta(days=45)).strftime("%Y-%m-%dT18:00:00+11:00"),
        "location": "Harbourview Function Room, Level 2",
        "agenda": ["Election of EC", "Budget approval 2025-2026", "Capital works fund report", "General business"],
        "attendees": [],
        "minutes": "AGM held. Helen Morris elected Chairperson. Annual budget of $14,400 approved.",
        "status": "completed",
        "created_by": "system",
        "created_at": (NOW - timedelta(days=50)).isoformat(),
        "updated_at": (NOW - timedelta(days=40)).isoformat(),
        "building_id": HARBOUR,
    },
    {
        "id": _id(),
        "title": "EC Meeting — April 2026",
        "description": "Executive Committee meeting. Terrace maintenance update, insurance renewal, and financial review.",
        "meeting_date": (NOW + timedelta(days=8)).strftime("%Y-%m-%dT18:00:00+11:00"),
        "location": "Harbourview Function Room, Level 2",
        "agenda": ["Terrace works progress", "Insurance renewal quote", "Q1 financial summary", "Any other business"],
        "attendees": [],
        "minutes": "",
        "status": "scheduled",
        "created_by": "system",
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
        "building_id": HARBOUR,
    },
]

HARBOUR_EVENTS = [
    {
        "id": _id(),
        "title": "Harbourview Sundowner — April Edition",
        "description": "Monthly sundowner on the marina terrace. Enjoy sunset views with your neighbours. Drinks and nibbles provided.",
        "event_type": "social",
        "start_date": (NOW + timedelta(days=22)).strftime("%Y-%m-%dT17:30:00+11:00"),
        "end_date": (NOW + timedelta(days=22)).strftime("%Y-%m-%dT20:00:00+11:00"),
        "location": "Marina View Terrace, Level 8",
        "is_recurring": True,
        "recurrence_rule": "FREQ=MONTHLY",
        "source": "building",
        "source_url": None,
        "is_public": False,
        "created_by": "system",
        "created_at": NOW_ISO,
        "building_id": HARBOUR,
    },
]

HARBOUR_MAINTENANCE = [
    {
        "id": _id(),
        "title": "Marina Terrace Waterproofing",
        "description": "Waterproofing membrane on the Level 8 marina terrace has failed. Water ingress reported in unit H010 ceiling. Urgent repair required.",
        "category": "Waterproofing",
        "location": "Level 8 Marina Terrace",
        "priority": "urgent",
        "images": [],
        "status": "in_progress",
        "submitted_by": "system",
        "submitted_by_name": "Anna Lee (Owner H010)",
        "assigned_contractor": None,
        "contractor_name": "Sydney Waterproofing Specialists",
        "estimated_cost": 8500.0,
        "actual_cost": None,
        "purchase_order_id": None,
        "invoice_id": None,
        "approval_history": [],
        "notes": "Works approved at EC meeting 18 March. Contractor mobilising.",
        "created_at": (NOW - timedelta(days=15)).isoformat(),
        "updated_at": (NOW - timedelta(days=3)).isoformat(),
        "completed_at": None,
        "building_id": HARBOUR,
    },
    {
        "id": _id(),
        "title": "Intercom System Upgrade",
        "description": "The building intercom is end-of-life. Quotations obtained for full IP video intercom replacement.",
        "category": "Security/Access",
        "location": "All levels and lobby",
        "priority": "medium",
        "images": [],
        "status": "submitted",
        "submitted_by": "system",
        "submitted_by_name": "Peter Nguyen (EC)",
        "assigned_contractor": None,
        "contractor_name": None,
        "estimated_cost": 12000.0,
        "actual_cost": None,
        "purchase_order_id": None,
        "invoice_id": None,
        "approval_history": [],
        "notes": "Three quotes received. Pending EC approval at April meeting.",
        "created_at": (NOW - timedelta(days=8)).isoformat(),
        "updated_at": (NOW - timedelta(days=8)).isoformat(),
        "completed_at": None,
        "building_id": HARBOUR,
    },
]


# ─── Seed Runner ───────────────────────────────────────────────────────────────

async def _seed_building(
        building_id: str,
        annual_levy_fn,
        ledger_fn,
        announcements: list,
        meetings: list,
        events: list,
        maintenance: list,
):
    name = "Sierra" if building_id == SIERRA else "Harbourview"
    print(f"\n[enrich] === {name} ({building_id}) ===")

    # annual_levies
    added_levies = 0
    for year in ["2025", "2026"]:
        exists = await db.annual_levies.find_one({"year": year, "building_id": building_id})
        if not exists:
            await db.annual_levies.insert_one(annual_levy_fn(year))
            added_levies += 1
    print(f"[enrich] annual_levies: {added_levies} added")

    # unit_levy_ledger
    added_ledger = 0
    for year in ["2025", "2026"]:
        for entry in ledger_fn(year):
            exists = await db.unit_levy_ledger.find_one({
                "unit_number": entry["unit_number"], "year": year, "building_id": building_id
            })
            if not exists:
                await db.unit_levy_ledger.insert_one(entry)
                added_ledger += 1
    print(f"[enrich] unit_levy_ledger: {added_ledger} added")

    # announcements
    added_ann = 0
    for ann in announcements:
        exists = await db.announcements.find_one({"title": ann["title"], "building_id": building_id})
        if not exists:
            await db.announcements.insert_one(ann)
            added_ann += 1
    print(f"[enrich] announcements: {added_ann} added")

    # meetings
    added_mtg = 0
    for mtg in meetings:
        exists = await db.meetings.find_one({"title": mtg["title"], "building_id": building_id})
        if not exists:
            await db.meetings.insert_one(mtg)
            added_mtg += 1
    print(f"[enrich] meetings: {added_mtg} added")

    # events
    added_evt = 0
    for evt in events:
        exists = await db.events.find_one({"title": evt["title"], "building_id": building_id})
        if not exists:
            await db.events.insert_one(evt)
            added_evt += 1
    print(f"[enrich] events: {added_evt} added")

    # maintenance_requests
    added_maint = 0
    for req in maintenance:
        exists = await db.maintenance_requests.find_one({"title": req["title"], "building_id": building_id})
        if not exists:
            await db.maintenance_requests.insert_one(req)
            added_maint += 1
    print(f"[enrich] maintenance_requests: {added_maint} added")


async def seed_demo_enrichment():
    if db is None:
        print("[enrich] No DB connection — skipping")
        return

    print("[enrich] Starting demo data enrichment (Sierra + Harbourview only)...")
    print("[enrich] NOTE: East Gate (13195) data is NOT touched.")

    await _seed_building(
        SIERRA,
        _sierra_annual_levy,
        _sierra_ledger_entries,
        SIERRA_ANNOUNCEMENTS,
        SIERRA_MEETINGS,
        SIERRA_EVENTS,
        SIERRA_MAINTENANCE,
    )

    await _seed_building(
        HARBOUR,
        _harbour_annual_levy,
        _harbour_ledger_entries,
        HARBOUR_ANNOUNCEMENTS,
        HARBOUR_MEETINGS,
        HARBOUR_EVENTS,
        HARBOUR_MAINTENANCE,
    )

    print("\n[enrich] Demo enrichment complete.")


if __name__ == "__main__":
    asyncio.run(seed_demo_enrichment())
