"""
Seed script for historical demo work orders.
"""

from datetime import datetime, timedelta, timezone

try:
    from database import db
except ImportError:
    db = None

PLAN_ID = "13195"


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


DEMO_WORK_ORDERS = [
    {
        "id": "wo-demo-1",
        "title": "Lift door alignment",
        "description": "Adjusting sensors on Level 3",
        "asset_id": "asset-lift-motor",
        "facility_id": "fac-lift",
        "estimated_cost": 850,
        "status": "completed",
        "created_at": _days_ago(300)
    },
    {
        "id": "wo-demo-2",
        "title": "Garage door sensor repair",
        "description": "Optical sensor obstructed",
        "asset_id": "asset-garage-motor",
        "facility_id": "fac-security",
        "estimated_cost": 420,
        "status": "completed",
        "created_at": _days_ago(250)
    },
    {
        "id": "wo-demo-3",
        "title": "Fire panel battery test",
        "description": "Routine compliance check",
        "asset_id": "asset-fire-panel",
        "facility_id": "fac-fire",
        "estimated_cost": 200,
        "status": "completed",
        "created_at": _days_ago(180)
    },
    {
        "id": "wo-demo-4",
        "title": "Emergency Lift Repair",
        "description": "Lift stuck on G",
        "asset_id": "asset-lift-motor",
        "facility_id": "fac-lift",
        "estimated_cost": 1200,
        "status": "completed",
        "created_at": _days_ago(120)
    },
    {
        "id": "wo-demo-5",
        "title": "Garage Motor Replacement Inquiry",
        "description": "Intermittent failure",
        "asset_id": "asset-garage-motor",
        "facility_id": "fac-security",
        "estimated_cost": 450,
        "status": "completed",
        "created_at": _days_ago(60)
    }
]


async def seed_demo_workorders():
    if db is None:
        print("Database not available.")
        return

    print("Seeding demo work orders...")
    for wo in DEMO_WORK_ORDERS:
        wo["building_id"] = PLAN_ID
        wo["maintenance_request_id"] = "req-" + wo["id"]
        wo["supplier_type"] = "General"
        wo["created_by"] = "system-seed"
        wo["updated_at"] = wo["created_at"]

        await db.work_orders.update_one(
            {"id": wo["id"]},
            {"$set": wo},
            upsert=True
        )

        # Seed matching invoice for cost tracking
        invoice = {
            "id": "inv-" + wo["id"],
            "work_order_id": wo["id"],
            "asset_id": wo.get("asset_id"),
            "facility_id": wo.get("facility_id"),
            "amount": wo["estimated_cost"],
            "total_amount": wo["estimated_cost"] * 1.1,
            "status": "paid",
            "payment_status": "paid",
            "created_at": wo["created_at"]
        }
        await db.invoices.update_one(
            {"id": invoice["id"]},
            {"$set": invoice},
            upsert=True
        )

    print(f"Seeded {len(DEMO_WORK_ORDERS)} work orders and invoices.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(seed_demo_workorders())
