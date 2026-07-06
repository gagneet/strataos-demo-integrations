"""
Seed demo dataset for Maintenance Intelligence and decision-ready dashboards.
"""
from datetime import datetime, timezone

try:
    from database import db
except ImportError:
    db = None

PLAN_ID = "13195"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(date_str: str) -> str:
    return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


BENEFIT_GROUPS = [
    {
        "id": "bg-all",
        "name": "ALL_LOTS",
        "description": "Benefits every unit",
        "allocation_rule": {"allocation_type": "unit_entitlement"},
    },
    {
        "id": "bg-tower",
        "name": "APARTMENTS_ONLY",
        "description": "Only apartment residents benefit",
        "allocation_rule": {"allocation_type": "unit_entitlement"},
    },
    {
        "id": "bg-th",
        "name": "TOWNHOUSES_ONLY",
        "description": "Only townhouse residents benefit",
        "allocation_rule": {"allocation_type": "unit_entitlement"},
    },
    {
        "id": "bg-garage",
        "name": "GARAGE_USERS",
        "description": "Residents with garage access",
        "allocation_rule": {"allocation_type": "equal_split"},
    },
]

ZONES = [
    {"id": "zone-basement", "name": "Basement Garage", "description": "Basement parking and access"},
]

FACILITIES = [
    {
        "id": "fac-garage-access",
        "name": "Garage Access System",
        "category": "Access Control",
        "zone_id": "zone-basement",
        "benefit_group_id": "bg-garage",
    },
    {
        "id": "fac-lift",
        "name": "Lift System",
        "category": "Vertical Transport",
        "zone_id": "zone-tower",
        "benefit_group_id": "bg-tower",
    },
    {
        "id": "fac-fire",
        "name": "Fire Safety Systems",
        "category": "Safety",
        "zone_id": "zone-grounds",
        "benefit_group_id": "bg-all",
    },
]

ASSETS = [
    {
        "id": "asset-garage-motor-a",
        "name": "Garage Door Motor A",
        "category": "Access Control",
        "facility_id": "fac-garage-access",
        "zone_id": "zone-basement",
        "benefit_group_id": "bg-garage",
        "installation_date": "2015-01-01T00:00:00Z",
        "expected_lifespan_years": 12,
        "replacement_cost_estimate": 4500,
        "maintenance_frequency_months": 6,
        "last_service_date": "2023-01-10T00:00:00Z",
        "notes": "Demo asset for maintenance anomaly + replacement recommendation.",
    },
    {
        "id": "asset-lift-motor-a",
        "name": "Lift Motor A",
        "category": "Lifts",
        "facility_id": "fac-lift",
        "zone_id": "zone-tower",
        "benefit_group_id": "bg-tower",
        "installation_date": "2015-06-01T00:00:00Z",
        "expected_lifespan_years": 15,
        "replacement_cost_estimate": 220000,
        "maintenance_frequency_months": 12,
        "last_service_date": "2025-09-01T00:00:00Z",
        "notes": "Capital shock demo asset.",
    },
    {
        "id": "asset-lift-control-panel",
        "name": "Lift Control Panel",
        "category": "Lifts",
        "facility_id": "fac-lift",
        "zone_id": "zone-tower",
        "benefit_group_id": "bg-tower",
        "installation_date": "2018-01-01T00:00:00Z",
        "expected_lifespan_years": 15,
        "replacement_cost_estimate": 40000,
        "maintenance_frequency_months": 12,
        "last_service_date": "2025-10-01T00:00:00Z",
        "notes": "Healthy lift control asset.",
    },
    {
        "id": "asset-fire-alarm-panel",
        "name": "Fire Alarm Panel",
        "category": "Fire Safety",
        "facility_id": "fac-fire",
        "zone_id": "zone-grounds",
        "benefit_group_id": "bg-all",
        "installation_date": "2012-01-01T00:00:00Z",
        "expected_lifespan_years": 15,
        "replacement_cost_estimate": 40000,
        "maintenance_frequency_months": 6,
        "last_service_date": "2025-07-15T00:00:00Z",
        "notes": "Triggers capital planning for 2027 replacement.",
    },
]

WORK_ORDERS = [
    {
        "id": "wo-garage-2024-03-14",
        "title": "Garage door stuck halfway",
        "description": "Motor stall and gearbox adjustment",
        "asset_id": "asset-garage-motor-a",
        "facility_id": "fac-garage-access",
        "benefit_group_id": "bg-garage",
        "estimated_cost": 280,
        "status": "completed",
        "created_at": _iso("2024-03-14T09:00:00"),
    },
    {
        "id": "wo-garage-2024-07-09",
        "title": "Motor overheating",
        "description": "Thermal overload warning",
        "asset_id": "asset-garage-motor-a",
        "facility_id": "fac-garage-access",
        "benefit_group_id": "bg-garage",
        "estimated_cost": 320,
        "status": "completed",
        "created_at": _iso("2024-07-09T11:30:00"),
    },
    {
        "id": "wo-garage-2024-10-21",
        "title": "Sensor failure",
        "description": "Safety sensor recalibration",
        "asset_id": "asset-garage-motor-a",
        "facility_id": "fac-garage-access",
        "benefit_group_id": "bg-garage",
        "estimated_cost": 450,
        "status": "completed",
        "created_at": _iso("2024-10-21T15:10:00"),
    },
    {
        "id": "wo-garage-2025-01-10",
        "title": "Motor repair",
        "description": "Motor winding repair and service",
        "asset_id": "asset-garage-motor-a",
        "facility_id": "fac-garage-access",
        "benefit_group_id": "bg-garage",
        "estimated_cost": 900,
        "status": "completed",
        "created_at": _iso("2025-01-10T10:15:00"),
    },
]


async def seed_demo_intelligence_dataset():
    if db is None:
        print("Database not available.")
        return

    from request_context import set_ctx_building_id
    set_ctx_building_id(PLAN_ID)

    print("Seeding maintenance intelligence demo dataset...")

    # Ensure baseline demo building data exists
    try:
        from strataos_demo_integrations.demo_bank.seeds.seed_demo_building import seed_demo_building
        await seed_demo_building()
    except Exception:
        pass

    # Benefit groups
    for bg in BENEFIT_GROUPS:
        bg_doc = {**bg, "building_id": PLAN_ID, "created_at": _now()}
        await db.benefit_groups.update_one({"id": bg["id"]}, {"$set": bg_doc}, upsert=True)

    # Zones
    for zone in ZONES:
        zone_doc = {**zone, "building_id": PLAN_ID, "created_at": _now()}
        await db.zones.update_one({"id": zone["id"]}, {"$set": zone_doc}, upsert=True)

    # Facilities
    for fac in FACILITIES:
        fac_doc = {**fac, "building_id": PLAN_ID, "created_at": _now()}
        await db.facilities.update_one({"id": fac["id"]}, {"$set": fac_doc}, upsert=True)

    # Assets
    for asset in ASSETS:
        asset_doc = {**asset, "building_id": PLAN_ID, "updated_at": _now()}
        await db.building_assets.update_one({"id": asset["id"]}, {"$set": asset_doc}, upsert=True)

    # Work orders + invoices
    for wo in WORK_ORDERS:
        work_order = {
            **wo,
            "building_id": PLAN_ID,
            "maintenance_request_id": f"req-{wo['id']}",
            "supplier_type": "General",
            "created_by": "system-seed",
            "updated_at": wo["created_at"],
        }
        await db.work_orders.update_one({"id": wo["id"]}, {"$set": work_order}, upsert=True)

        invoice = {
            "id": f"inv-{wo['id']}",
            "work_order_id": wo["id"],
            "asset_id": wo["asset_id"],
            "facility_id": wo["facility_id"],
            "amount": wo["estimated_cost"],
            "total_amount": round(wo["estimated_cost"] * 1.1, 2),
            "status": "paid",
            "payment_status": "paid",
            "created_at": wo["created_at"],
        }
        await db.invoices.update_one({"id": invoice["id"]}, {"$set": invoice}, upsert=True)

    try:
        from services.maintenance_intelligence_service import recompute_all_maintenance_intelligence
        await recompute_all_maintenance_intelligence(PLAN_ID)
    except Exception:
        pass

    print("Demo maintenance intelligence dataset seeded.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(seed_demo_intelligence_dataset())
