"""
Seed script for master demo financial balances.
"""

try:
    from database import db
except ImportError:
    db = None

PLAN_ID = "13195"

DEMO_FINANCE = {
    "admin_fund_balance": 180000,
    "sinking_fund_balance": 320000,
    "annual_maintenance_spend": 42000
}


async def seed_demo_finance():
    if db is None:
        print("Database not available.")
        return

    print("Seeding demo financial data...")
    # Update annual_levies for 2026 with demo balances
    await db.annual_levies.update_one(
        {"year": "2026", "plan_id": PLAN_ID},
        {"$set": {
            "admin_fund.closing_balance": DEMO_FINANCE["admin_fund_balance"],
            "sinking_fund.closing_balance": DEMO_FINANCE["sinking_fund_balance"],
            "status": "actual"
        }},
        upsert=True
    )
    print("Financial data seeded.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(seed_demo_finance())
