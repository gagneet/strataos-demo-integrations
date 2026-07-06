"""
Seed script for demo building, zones, facilities, and benefit groups.
"""

try:
    from database import db
except ImportError:
    db = None

PLAN_ID = "13195"

BUILDING = {
    "id": PLAN_ID,
    "name": "East Gate Residences",
    "address": "14 Hoolihan Street, Denman Prospect ACT 2611",
    "lots": 87,
    "year_built": 2018
}

ZONES = [
    {"id": "zone-tower", "name": "Apartment Tower", "description": "Main residential tower"},
    {"id": "zone-th", "name": "Townhouse Block", "description": "External townhouses"},
    {"id": "zone-basement", "name": "Basement Garage", "description": "Parking and storage areas"},
    {"id": "zone-grounds", "name": "Shared Grounds", "description": "Common landscaping and perimeter"}
]

BENEFIT_GROUPS = [
    {
        "id": "bg-all",
        "name": "ALL_LOTS",
        "description": "Benefits every unit",
        "allocation_rule": {"allocation_type": "unit_entitlement"}
    },
    {
        "id": "bg-tower",
        "name": "APARTMENTS_ONLY",
        "description": "Only for tower residents",
        "allocation_rule": {"allocation_type": "unit_entitlement"}
    },
    {
        "id": "bg-th",
        "name": "TOWNHOUSES_ONLY",
        "description": "Only for townhouse residents",
        "allocation_rule": {"allocation_type": "unit_entitlement"}
    },
    {
        "id": "bg-basement",
        "name": "BASEMENT_USERS",
        "description": "Only for those with parking/storage",
        "allocation_rule": {"allocation_type": "equal_split"}
    }
]

FACILITIES = [
    {"id": "fac-lift", "name": "Lift System", "category": "Vertical Transport", "zone_id": "zone-tower",
     "benefit_group_id": "bg-tower"},
    {"id": "fac-fire", "name": "Fire Safety Systems", "category": "Safety", "zone_id": "zone-grounds",
     "benefit_group_id": "bg-all"},
    {"id": "fac-hvac", "name": "Basement Ventilation", "category": "HVAC", "zone_id": "zone-basement",
     "benefit_group_id": "bg-basement"},
    {"id": "fac-lighting", "name": "Common Lighting", "category": "Electrical", "zone_id": "zone-grounds",
     "benefit_group_id": "bg-all"},
    {"id": "fac-security", "name": "Security Systems", "category": "Security", "zone_id": "zone-grounds",
     "benefit_group_id": "bg-all"}
]

ASSETS = [
    {
        "id": "asset-lift-motor",
        "name": "Lift Motor A",
        "category": "Lifts",
        "facility_id": "fac-lift",
        "installation_date": "2018-01-01T00:00:00Z",
        "expected_lifespan_years": 25,
        "replacement_cost_estimate": 65000,
        "maintenance_frequency_months": 12
    },
    {
        "id": "asset-fire-panel",
        "name": "Main Fire Panel",
        "category": "Fire",
        "facility_id": "fac-fire",
        "installation_date": "2018-01-01T00:00:00Z",
        "expected_lifespan_years": 15,
        "replacement_cost_estimate": 12000,
        "maintenance_frequency_months": 6
    },
    {
        "id": "asset-garage-motor",
        "name": "Main Garage Motor",
        "category": "Security",
        "facility_id": "fac-security",
        "installation_date": "2018-01-01T00:00:00Z",
        "expected_lifespan_years": 12,
        "replacement_cost_estimate": 4500,
        "maintenance_frequency_months": 6
    }
]


async def seed_demo_building():
    if db is None:
        print("Database not available.")
        return

    print(f"Seeding demo building {PLAN_ID}...")

    # 1. Building
    await db.buildings.update_one(
        {"id": BUILDING["id"]},
        {"$set": BUILDING},
        upsert=True
    )

    # 2. Zones
    for z in ZONES:
        z["building_id"] = PLAN_ID
        await db.zones.update_one({"id": z["id"]}, {"$set": z}, upsert=True)

    # 3. Benefit Groups
    for bg in BENEFIT_GROUPS:
        bg["building_id"] = PLAN_ID
        await db.benefit_groups.update_one({"id": bg["id"]}, {"$set": bg}, upsert=True)

    # 4. Facilities
    for f in FACILITIES:
        f["building_id"] = PLAN_ID
        await db.facilities.update_one({"id": f["id"]}, {"$set": f}, upsert=True)

    # 5. Assets
    for a in ASSETS:
        a["building_id"] = PLAN_ID
        # Inherit zone/bg from facility
        fac = next((f for f in FACILITIES if f["id"] == a["facility_id"]), None)
        if fac:
            a["zone_id"] = fac["zone_id"]
            a["benefit_group_id"] = fac["benefit_group_id"]

        await db.building_assets.update_one({"id": a["id"]}, {"$set": a}, upsert=True)

    print("Demo building seeded successfully.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(seed_demo_building())
