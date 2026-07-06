"""Demo scheme seed — seeds a complete Strata Management chain in Postgres.

The demo always exists so the dashboard has something meaningful to render
after first super-admin login, and so anyone evaluating the platform can
log in as the Strata Manager or Strata Admin to see what those roles
look like before onboarding their own building.

Hierarchy created (mirrors the real customer-onboarding flow)
-------------------------------------------------------------

    StrataOS Demo Strata Management        (core.tenants, is_demo=TRUE)
        ├── admin@demo.strataos.live       (core.users, role=strata_admin)
        ├── manager@demo.strataos.live     (core.users, role=strata_manager)
        └── StrataOS Demo Tower            (core.schemes, is_demo=TRUE)
            └── 10 lots with varied entitlements
                (1BR, 2BR, 2BR+study, 3BR, penthouse mix)

Both platform super admins also receive scheme-scoped role assignments so
they can switch into the demo via the building switcher.

Idempotent — every INSERT uses ``ON CONFLICT … DO UPDATE`` on natural keys,
so re-runs converge to the state described here.

Usage::

    cd backend
    DATABASE_URL=... python3 seeds/demo_scheme.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# Allow running directly from the backend/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import text
from db_postgres.session import async_session_context

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Deterministic UUIDv5 namespace so re-runs and parallel environments converge.
_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # standard URL namespace
DEMO_TENANT_ID = str(uuid.uuid5(_NS, "strataos-demo-tenant"))
DEMO_TENANT_NAME = "StrataOS Demo Strata Management"

DEMO_SCHEME_NUMBER = "DEMO-0001"
DEMO_SCHEME_NAME = "StrataOS Demo Tower"
DEMO_JURISDICTION = "ACT"

# Bcrypt(12) of "DemoUser$01". Generated with:
#   python3 -c "import bcrypt; print(bcrypt.hashpw(b'DemoUser\$01', bcrypt.gensalt(12)).decode())"
# This is a documented demo-account password — the hash is intentionally
# committed so anyone can log in to evaluate the platform.
DEMO_USER_PASSWORD_HASH = "$2b$12$sz.hfqb5JRxiNuipv8iM1evKkuqFSWxSN3O/WuddAZJGWyBwA.g7q"

DEMO_USERS = [
    {
        "email": "admin@demo.strataos.live",
        "full_name": "Demo Strata Admin",
        "first_name": "Demo",
        "last_name": "Admin",
        "role": "strata_admin",
    },
    {
        "email": "manager@demo.strataos.live",
        "full_name": "Demo Strata Manager",
        "first_name": "Demo",
        "last_name": "Manager",
        "role": "strata_manager",
    },
]

# 10 lots with varied bedroom mix, sqm, and entitlement units.
# Total entitlement = 1199, total floor area = 928 sqm — realistic spread for
# a small mixed-typology block.
DEMO_LOTS = [
    {"lot_number": "1", "unit_number": "DEMO001", "lot_use": "residential", "floor_area_sqm": 58.5,
     "entitlement_units": 75},
    {"lot_number": "2", "unit_number": "DEMO002", "lot_use": "residential", "floor_area_sqm": 60.0,
     "entitlement_units": 78},
    {"lot_number": "3", "unit_number": "DEMO003", "lot_use": "residential", "floor_area_sqm": 56.0,
     "entitlement_units": 72},
    {"lot_number": "4", "unit_number": "DEMO004", "lot_use": "residential", "floor_area_sqm": 82.5,
     "entitlement_units": 105},
    {"lot_number": "5", "unit_number": "DEMO005", "lot_use": "residential", "floor_area_sqm": 85.0,
     "entitlement_units": 110},
    {"lot_number": "6", "unit_number": "DEMO006", "lot_use": "residential", "floor_area_sqm": 88.0,
     "entitlement_units": 115},
    {"lot_number": "7", "unit_number": "DEMO007", "lot_use": "residential", "floor_area_sqm": 95.0,
     "entitlement_units": 122},
    {"lot_number": "8", "unit_number": "DEMO008", "lot_use": "residential", "floor_area_sqm": 110.0,
     "entitlement_units": 140},
    {"lot_number": "9", "unit_number": "DEMO009", "lot_use": "residential", "floor_area_sqm": 118.0,
     "entitlement_units": 152},
    {"lot_number": "10", "unit_number": "DEMO010", "lot_use": "residential", "floor_area_sqm": 175.0,
     "entitlement_units": 230},
]


# ──────────────────────────────────────────────────────────────────────────────
# Seed
# ──────────────────────────────────────────────────────────────────────────────

async def seed() -> None:
    async with async_session_context() as session:
        # 1. Demo tenant — flagged is_demo so reports/analytics filter it out.
        await session.execute(
            text("""
                 INSERT INTO core.tenants (tenant_id, tenant_name, is_demo)
                 VALUES (:tid, :name, TRUE) ON CONFLICT (tenant_id) DO
                 UPDATE
                     SET tenant_name = EXCLUDED.tenant_name,
                     is_demo = TRUE
                 """),
            {"tid": DEMO_TENANT_ID, "name": DEMO_TENANT_NAME},
        )
        print(f"[seed] Demo tenant upserted: {DEMO_TENANT_ID}")

        # 2. Switch RLS context to the demo tenant for all writes inside it.
        await session.execute(
            text("SET LOCAL app.tenant_id = :tid"),
            {"tid": DEMO_TENANT_ID}
        )

        # 3. Demo scheme. Note: ``CAST(:jur AS …)`` avoids the SQLAlchemy
        #    ``:param::type`` parse-collision; the parser would otherwise drop
        #    the bind parameter and leave a literal ``:jur`` in the emitted SQL.
        result = await session.execute(
            text("""
                 INSERT INTO core.schemes
                 (tenant_id, jurisdiction, scheme_number, scheme_name,
                  status, is_demo)
                 VALUES (:tid,
                         CAST(:jur AS compliance.jurisdiction_code),
                         :num, :name,
                         CAST('active' AS core.record_status),
                         TRUE) ON CONFLICT (tenant_id, jurisdiction, scheme_number) DO
                 UPDATE
                     SET scheme_name = EXCLUDED.scheme_name,
                     is_demo = TRUE,
                     updated_at = NOW()
                     RETURNING CAST (scheme_id AS TEXT)
                 """),
            {
                "tid": DEMO_TENANT_ID,
                "jur": DEMO_JURISDICTION,
                "num": DEMO_SCHEME_NUMBER,
                "name": DEMO_SCHEME_NAME,
            },
        )
        scheme_id = result.scalar()
        print(f"[seed] Demo scheme upserted: {scheme_id}")

        # 4. Demo lots.
        for lot in DEMO_LOTS:
            await session.execute(
                text("""
                     INSERT INTO core.lots
                     (tenant_id, scheme_id, lot_number, unit_number,
                      lot_use, entitlement_units, floor_area_sqm)
                     VALUES (:tid, :sid, :lot_no, :unit_no,
                             :use, :ent, :area) ON CONFLICT (scheme_id, lot_number) DO
                     UPDATE
                         SET unit_number = EXCLUDED.unit_number,
                         lot_use = EXCLUDED.lot_use,
                         entitlement_units = EXCLUDED.entitlement_units,
                         floor_area_sqm = EXCLUDED.floor_area_sqm,
                         updated_at = NOW()
                     """),
                {
                    "tid": DEMO_TENANT_ID,
                    "sid": scheme_id,
                    "lot_no": lot["lot_number"],
                    "unit_no": lot["unit_number"],
                    "use": lot["lot_use"],
                    "ent": lot["entitlement_units"],
                    "area": lot["floor_area_sqm"],
                },
            )
        total_ent = sum(l["entitlement_units"] for l in DEMO_LOTS)
        total_sqm = sum(l["floor_area_sqm"] for l in DEMO_LOTS)
        print(f"[seed] {len(DEMO_LOTS)} demo lots upserted "
              f"(total entitlement={total_ent}, floor area={total_sqm:.1f} sqm)")

        # 5. Demo Strata Manager + Strata Admin users — both inside the
        #    demo tenant. Same bcrypt hash for both (DemoUser$01).
        demo_user_ids: list[str] = []
        for u in DEMO_USERS:
            r = await session.execute(
                text("""
                     INSERT INTO core.users
                     (tenant_id, email, full_name, first_name, last_name,
                      password_hash, role, is_active, is_approved)
                     VALUES (:tid, :email, :full_name, :first_name, :last_name,
                             :pw_hash, CAST(:role AS core.user_role), TRUE, TRUE) ON CONFLICT (tenant_id, email) DO
                     UPDATE
                         SET full_name = EXCLUDED.full_name,
                         first_name = EXCLUDED.first_name,
                         last_name = EXCLUDED.last_name,
                         role = EXCLUDED.role,
                         password_hash = EXCLUDED.password_hash,
                         is_active = TRUE,
                         is_approved = TRUE,
                         updated_at = NOW()
                         RETURNING user_id::TEXT
                     """),
                {
                    "tid": DEMO_TENANT_ID,
                    "email": u["email"],
                    "full_name": u["full_name"],
                    "first_name": u["first_name"],
                    "last_name": u["last_name"],
                    "pw_hash": DEMO_USER_PASSWORD_HASH,
                    "role": u["role"],
                },
            )
            uid = r.scalar()
            demo_user_ids.append(uid)
            print(f"[seed] Demo user upserted: {u['email']} ({u['role']}) → {uid}")

            # 5a. Scheme-scoped role assignment so the building switcher
            #     surfaces the demo when these users log in.
            await session.execute(
                text("""
                     INSERT INTO core.user_role_assignments
                         (tenant_id, user_id, scheme_id, role, is_active)
                     VALUES (:tid, :uid, :sid, CAST(:role AS core.user_role),
                             TRUE) ON CONFLICT (user_id, scheme_id, role)
                     WHERE scheme_id IS NOT NULL
                         DO
                     UPDATE SET is_active = TRUE, granted_at = NOW()
                     """),
                {"tid": DEMO_TENANT_ID, "uid": uid,
                 "sid": scheme_id, "role": u["role"]},
            )

        # NOTE: super admins are deliberately NOT given scheme-scoped role
        # assignments to the demo. SAs sit above the org tree (Silverfox
        # SaaS-admin scope, not tied to any Strata Management Organisation
        # or building) and are resolved cross-tenant by
        # ``identity_repo.list_all_active_schemes``. Granting them a
        # scheme-scoped row here would cause the auth login flow to
        # auto-resolve them into the demo (single scheme assignment →
        # auto-default), making them appear as if they "belong to" the
        # demo. They should choose a scheme via the building switcher,
        # never be defaulted into one.

    print("\n[seed] Done — demo Strata Management chain ready.")
    print(f"       Demo tenant: {DEMO_TENANT_ID}")
    print(f"       Demo scheme: {scheme_id} ({DEMO_SCHEME_NUMBER} — {DEMO_SCHEME_NAME})")
    print(f"       Demo users:  admin@demo.strataos.live, manager@demo.strataos.live")
    print(f"       Demo password: DemoUser$01 (bcrypt 12 rounds)")


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set. Source backend/.env or set it manually.")
        sys.exit(1)
    asyncio.run(seed())
