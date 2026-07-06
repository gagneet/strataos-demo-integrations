"""Teardown script: remove StrataOS Demo Tower (DEMO-0001) and
Phase1 Ops Scheme from Postgres and MongoDB.

Usage (from backend/):
    python3 scripts/db/drop_demo_and_phase1_schemes.py [--dry-run]

Flags:
    --dry-run   Print what would be deleted without executing.

Safety:
    - Never touches East Gate (building_id="13195" / scheme_number="13195").
    - Never touches UP-DEMO-001 (StrataOS Demo Residences / Acme demo).
    - Only removes rows whose source_system or scheme_number matches the
      Demo Tower / Phase1 targets.
    - After running, add DISABLE_DEMO_BOOTSTRAP=true to backend/.env so
      Demo Tower does not auto-recreate on the next server restart.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

DRY_RUN = "--dry-run" in sys.argv


def _check_dependencies() -> None:
    """Verify runtime dependencies are from the project venv, not system Python.

    Aborts early with a clear message if SQLAlchemy is the wrong version or
    asyncpg is missing — both happen when the script is run with system Python
    instead of backend/venv/bin/python3.
    """
    print(f"  Python  : {sys.executable}")
    try:
        import sqlalchemy
        print(f"  SQLAlchemy: {sqlalchemy.__version__}  ({sqlalchemy.__file__})")
        try:
            from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: F401
            print("  async_sessionmaker: available (SQLAlchemy ≥ 2.0)")
        except ImportError:
            print("  async_sessionmaker: NOT available (SQLAlchemy < 2.0)")
            print("  -> Compatible sessionmaker fallback will be used.")
    except ImportError:
        print("  ERROR: SQLAlchemy is not installed in this Python environment.")
        print(f"  Run: {sys.executable} -m pip install -r requirements.txt")
        sys.exit(1)

    try:
        import asyncpg  # noqa: F401
        print(f"  asyncpg   : available")
    except ImportError:
        print("  ERROR: asyncpg is NOT installed. This script requires the project venv.")
        print(f"  Fix: cd backend && source venv/bin/activate && python3 {' '.join(sys.argv)}")
        print(f"  Or : venv/bin/python3 {' '.join(sys.argv)}")
        sys.exit(1)

    print()

# ── Protected scheme numbers — never touch these ────────────────────────────
PROTECTED = {"13195", "UP-DEMO-001"}

# ── Targets ─────────────────────────────────────────────────────────────────
DEMO_TOWER_SCHEME_NUMBER = "DEMO-0001"

# Phase1 Ops Scheme may appear under various names; match by name pattern
PHASE1_NAME_PATTERNS = ["Phase1 Ops", "Phase 1 Ops", "phase1", "phase1_ops"]

_BYPASS_UUID = "00000000-0000-0000-0000-000000000000"

_PG_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── PostgreSQL teardown ──────────────────────────────────────────────────────

def _quote_pg_identifier(identifier: str) -> str:
    if not _PG_IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"Unsafe PostgreSQL identifier: {identifier!r}")
    return f'"{identifier}"'


async def _pg_delete_scheme_references(session, scheme_id: str, label: str) -> None:
    """Delete rows from tables that directly reference core.schemes.scheme_id."""
    from collections import defaultdict

    from sqlalchemy import text

    rows = await session.execute(
        text(
            """
            SELECT ns.nspname AS schema_name,
                   cls.relname AS table_name,
                   attr.attname AS column_name
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            JOIN unnest(con.conkey) WITH ORDINALITY cols(attnum, ord) ON TRUE
            JOIN pg_attribute attr
              ON attr.attrelid = con.conrelid
             AND attr.attnum = cols.attnum
            WHERE con.contype = 'f'
              AND con.confrelid = 'core.schemes'::regclass
              AND attr.attname = 'scheme_id'
              AND NOT (ns.nspname = 'core' AND cls.relname IN ('schemes', 'lots'))
            """
        )
    )
    refs = rows.fetchall()
    columns_by_table = defaultdict(list)
    for row in refs:
        _quote_pg_identifier(row.schema_name)
        _quote_pg_identifier(row.table_name)
        _quote_pg_identifier(row.column_name)
        columns_by_table[(row.schema_name, row.table_name)].append(row.column_name)

    if not columns_by_table:
        return

    table_values = ", ".join(
        f"('{schema_name}', '{table_name}')"
        for schema_name, table_name in sorted(columns_by_table)
    )
    dependency_rows = await session.execute(
        text(
            f"""
            WITH target_tables(schema_name, table_name) AS (VALUES {table_values})
            SELECT child_ns.nspname AS child_schema,
                   child_cls.relname AS child_table,
                   parent_ns.nspname AS parent_schema,
                   parent_cls.relname AS parent_table
            FROM pg_constraint con
            JOIN pg_class child_cls ON child_cls.oid = con.conrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child_cls.relnamespace
            JOIN pg_class parent_cls ON parent_cls.oid = con.confrelid
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent_cls.relnamespace
            JOIN target_tables child_target
              ON child_target.schema_name = child_ns.nspname
             AND child_target.table_name = child_cls.relname
            JOIN target_tables parent_target
              ON parent_target.schema_name = parent_ns.nspname
             AND parent_target.table_name = parent_cls.relname
            WHERE con.contype = 'f'
              AND NOT (child_ns.nspname = parent_ns.nspname AND child_cls.relname = parent_cls.relname)
            """
        )
    )

    children_by_parent = defaultdict(set)
    for row in dependency_rows.fetchall():
        child = (row.child_schema, row.child_table)
        parent = (row.parent_schema, row.parent_table)
        children_by_parent[parent].add(child)

    ordered_tables = []
    temporary = set()
    permanent = set()

    def visit(table):
        if table in permanent:
            return
        if table in temporary:
            ordered_tables.append(table)
            permanent.add(table)
            return
        temporary.add(table)
        for child in sorted(children_by_parent.get(table, ())):
            visit(child)
        temporary.remove(table)
        permanent.add(table)
        ordered_tables.append(table)

    for table in sorted(columns_by_table):
        visit(table)

    seen = set()
    for schema, table in ordered_tables:
        if (schema, table) in seen:
            continue
        seen.add((schema, table))
        schema_name = _quote_pg_identifier(schema)
        table_name = _quote_pg_identifier(table)
        predicates = []
        for column in columns_by_table[(schema, table)]:
            column_name = _quote_pg_identifier(column)
            predicates.append(f"{column_name} = CAST(:sid AS UUID)")
        result = await session.execute(
            text(f"DELETE FROM {schema_name}.{table_name} WHERE {' OR '.join(predicates)}"),
            {"sid": scheme_id},
        )
        if result.rowcount:
            print(f"  Deleted {result.rowcount} rows from {schema}.{table} for {label}")


async def _pg_delete_tenant_references(session, tenant_id: str, label: str) -> None:
    """Delete rows from tables that directly reference core.tenants.tenant_id."""
    from collections import defaultdict

    from sqlalchemy import text

    rows = await session.execute(
        text(
            """
            SELECT ns.nspname AS schema_name,
                   cls.relname AS table_name,
                   attr.attname AS column_name
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            JOIN unnest(con.conkey) WITH ORDINALITY cols(attnum, ord) ON TRUE
            JOIN pg_attribute attr
              ON attr.attrelid = con.conrelid
             AND attr.attnum = cols.attnum
            WHERE con.contype = 'f'
              AND con.confrelid = 'core.tenants'::regclass
              AND attr.attname = 'tenant_id'
              AND NOT (ns.nspname = 'core' AND cls.relname IN ('tenants', 'schemes'))
            """
        )
    )
    refs = rows.fetchall()
    columns_by_table = defaultdict(list)
    for row in refs:
        _quote_pg_identifier(row.schema_name)
        _quote_pg_identifier(row.table_name)
        _quote_pg_identifier(row.column_name)
        columns_by_table[(row.schema_name, row.table_name)].append(row.column_name)

    if not columns_by_table:
        return

    table_values = ", ".join(
        f"('{schema_name}', '{table_name}')"
        for schema_name, table_name in sorted(columns_by_table)
    )
    dependency_rows = await session.execute(
        text(
            f"""
            WITH target_tables(schema_name, table_name) AS (VALUES {table_values})
            SELECT child_ns.nspname AS child_schema,
                   child_cls.relname AS child_table,
                   parent_ns.nspname AS parent_schema,
                   parent_cls.relname AS parent_table
            FROM pg_constraint con
            JOIN pg_class child_cls ON child_cls.oid = con.conrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child_cls.relnamespace
            JOIN pg_class parent_cls ON parent_cls.oid = con.confrelid
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent_cls.relnamespace
            JOIN target_tables child_target
              ON child_target.schema_name = child_ns.nspname
             AND child_target.table_name = child_cls.relname
            JOIN target_tables parent_target
              ON parent_target.schema_name = parent_ns.nspname
             AND parent_target.table_name = parent_cls.relname
            WHERE con.contype = 'f'
              AND NOT (child_ns.nspname = parent_ns.nspname AND child_cls.relname = parent_cls.relname)
            """
        )
    )

    children_by_parent = defaultdict(set)
    for row in dependency_rows.fetchall():
        child = (row.child_schema, row.child_table)
        parent = (row.parent_schema, row.parent_table)
        children_by_parent[parent].add(child)

    ordered_tables = []
    temporary = set()
    permanent = set()

    def visit(table):
        if table in permanent:
            return
        if table in temporary:
            ordered_tables.append(table)
            permanent.add(table)
            return
        temporary.add(table)
        for child in sorted(children_by_parent.get(table, ())):
            visit(child)
        temporary.remove(table)
        permanent.add(table)
        ordered_tables.append(table)

    for table in sorted(columns_by_table):
        visit(table)

    seen = set()
    for schema, table in ordered_tables:
        if (schema, table) in seen:
            continue
        seen.add((schema, table))
        schema_name = _quote_pg_identifier(schema)
        table_name = _quote_pg_identifier(table)
        predicates = []
        for column in columns_by_table[(schema, table)]:
            column_name = _quote_pg_identifier(column)
            predicates.append(f"{column_name} = CAST(:tid AS UUID)")
        result = await session.execute(
            text(f"DELETE FROM {schema_name}.{table_name} WHERE {' OR '.join(predicates)}"),
            {"tid": tenant_id},
        )
        if result.rowcount:
            print(f"  Deleted {result.rowcount} rows from {schema}.{table} for {label}")


async def _pg_delete_lot_references(session, scheme_id: str, label: str) -> None:
    """Delete rows that reference lots in the target scheme before lots are removed."""
    from sqlalchemy import text

    rows = await session.execute(
        text(
            """
            SELECT ns.nspname AS schema_name,
                   cls.relname AS table_name,
                   attr.attname AS column_name
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            JOIN unnest(con.conkey) WITH ORDINALITY cols(attnum, ord) ON TRUE
            JOIN pg_attribute attr
              ON attr.attrelid = con.conrelid
             AND attr.attnum = cols.attnum
            WHERE con.contype = 'f'
              AND con.confrelid = 'core.lots'::regclass
              AND NOT (ns.nspname = 'core' AND cls.relname = 'lots')
            ORDER BY ns.nspname, cls.relname
            """
        )
    )

    for row in rows.fetchall():
        schema_name = _quote_pg_identifier(row.schema_name)
        table_name = _quote_pg_identifier(row.table_name)
        column_name = _quote_pg_identifier(row.column_name)
        result = await session.execute(
            text(
                f"DELETE FROM {schema_name}.{table_name} "
                f"WHERE {column_name} IN ("
                f"SELECT lot_id FROM core.lots WHERE scheme_id = CAST(:sid AS UUID)"
                f")"
            ),
            {"sid": scheme_id},
        )
        if result.rowcount:
            print(f"  Deleted {result.rowcount} rows from {row.schema_name}.{row.table_name} for {label}")


async def _pg_drop_scheme_by_number(session, scheme_number: str, label: str) -> None:
    if scheme_number in PROTECTED:
        print(f"  SKIP {label}: protected scheme {scheme_number}")
        return

    row = await session.execute(
        __import__("sqlalchemy").text(
            "SELECT tenant_id, scheme_id FROM core.schemes WHERE scheme_number = :n"
        ),
        {"n": scheme_number},
    )
    result = row.first()
    if not result:
        print(f"  NOT FOUND in Postgres: {label} ({scheme_number})")
        return

    tenant_id = str(result.tenant_id)
    scheme_id = str(result.scheme_id)
    print(f"  Found {label}: tenant_id={tenant_id}, scheme_id={scheme_id}")

    if DRY_RUN:
        print(f"  [DRY-RUN] Would delete lots, scheme, and tenant for {label}")
        return

    from sqlalchemy import text

    # 1. Set tenant context for scheme-scoped dependent data.
    await session.execute(
        text("SET LOCAL app.tenant_id = :tid"),
        {"tid": tenant_id}
    )
    await _pg_delete_scheme_references(session, scheme_id, label)
    await _pg_delete_lot_references(session, scheme_id, label)

    # Users are retained; remove only the default pointer to the scheme being dropped.
    result = await session.execute(
        text(
            """
            UPDATE core.users
               SET default_scheme_id = NULL
             WHERE default_scheme_id = CAST(:sid AS UUID)
            """
        ),
        {"sid": scheme_id},
    )
    if result.rowcount:
        print(f"  Cleared default_scheme_id on {result.rowcount} users for {label}")

    await session.execute(
        text("DELETE FROM core.lots WHERE scheme_id = CAST(:sid AS UUID)"),
        {"sid": scheme_id},
    )
    print(f"  Deleted lots for {label}")

    # 2. Back to bypass for scheme + tenant
    await session.execute(
        text("SET LOCAL app.tenant_id = :u"),
        {"u": _BYPASS_UUID}
    )
    await session.execute(
        text("DELETE FROM core.schemes WHERE scheme_id = CAST(:sid AS UUID)"),
        {"sid": scheme_id},
    )
    print(f"  Deleted scheme for {label}")

    # Only delete tenant if it has no other schemes
    count_row = await session.execute(
        text("SELECT COUNT(*) FROM core.schemes WHERE tenant_id = CAST(:tid AS UUID)"),
        {"tid": tenant_id},
    )
    remaining = count_row.scalar() or 0
    if remaining == 0:
        await _pg_delete_tenant_references(session, tenant_id, label)
        await session.execute(
            text("DELETE FROM core.tenants WHERE tenant_id = CAST(:tid AS UUID)"),
            {"tid": tenant_id},
        )
        print(f"  Deleted tenant for {label} (no remaining schemes)")
    else:
        print(f"  Kept tenant for {label} ({remaining} other scheme(s) remain)")


async def _pg_drop_phase1_by_name(session) -> None:
    from sqlalchemy import text

    rows = await session.execute(
        text(
            """
            SELECT tenant_id, scheme_id, scheme_number, scheme_name
            FROM core.schemes
            WHERE (scheme_name ILIKE '%Phase1 Ops%'
                   OR scheme_name ILIKE '%Phase 1 Ops%'
                   OR scheme_name ILIKE '%phase1_ops%')
              AND scheme_number NOT IN ('13195', 'UP-DEMO-001', 'DEMO-0001')
            """
        )
    )
    candidates = rows.fetchall()
    if not candidates:
        print("  NOT FOUND in Postgres: Phase1 Ops Scheme (by name)")
        return

    for row in candidates:
        scheme_number = str(row.scheme_number)
        if scheme_number in PROTECTED:
            print(f"  SKIP protected: {scheme_number}")
            continue
        await _pg_drop_scheme_by_number(session, scheme_number, f"Phase1 Ops Scheme ({scheme_number})")


async def run_postgres_teardown() -> None:
    from db_postgres.session import async_session_context
    from sqlalchemy import text

    async with async_session_context() as session:
        await session.execute(
            text("SET LOCAL app.tenant_id = :u"),
            {"u": _BYPASS_UUID}
        )

        print("\n=== Postgres: StrataOS Demo Tower (DEMO-0001) ===")
        await _pg_drop_scheme_by_number(session, DEMO_TOWER_SCHEME_NUMBER, "StrataOS Demo Tower")

        print("\n=== Postgres: Phase1 Ops Scheme (by name) ===")
        await _pg_drop_phase1_by_name(session)

        if not DRY_RUN:
            await session.commit()
            print("\n  Postgres commit done.")


# ── MongoDB teardown ─────────────────────────────────────────────────────────

async def run_mongo_teardown() -> None:
    from database import db
    from request_context import set_ctx_building_id

    # Demo Tower is Postgres-only — no Mongo records to remove.
    print("\n=== MongoDB: StrataOS Demo Tower (DEMO-0001) ===")
    print("  Postgres-only building — no MongoDB records.")

    # Phase1 Ops Scheme: search by building_id matching known Phase1 patterns
    print("\n=== MongoDB: Phase1 Ops Scheme (by building_id/name patterns) ===")

    # Find in buildings collection first
    candidates = await db.buildings.find(
        {
            "$or": [
                {"name": {"$regex": "Phase.*1.*Ops|Phase1", "$options": "i"}},
                {"building_id": {"$regex": "phase1|PHASE1", "$options": "i"}},
            ],
            "building_id": {"$nin": ["13195", "UP-DEMO-001", "DEMO-0001"]},
        },
        {"building_id": 1, "name": 1, "_id": 0},
    ).to_list(20)

    if not candidates:
        print("  NOT FOUND in MongoDB: Phase1 Ops Scheme")
        return

    for bld in candidates:
        bid = bld.get("building_id")
        if bid in PROTECTED or bid in {"DEMO-0001"}:
            print(f"  SKIP protected: {bid}")
            continue

        name = bld.get("name", bid)
        print(f"  Found: building_id={bid}, name={name}")

        if DRY_RUN:
            collections_affected = [
                "buildings", "units", "annual_levies", "unit_levy_ledger",
                "levy_payments", "expense_transactions", "announcements",
                "workflow_requests", "compliance_items", "meetings",
                "feature_toggles", "settings",
            ]
            print(f"  [DRY-RUN] Would wipe {len(collections_affected)} collections for building_id={bid}")
            continue

        set_ctx_building_id(bid)
        # Remove building record + all scoped collections
        scoped_collections = [
            "units", "annual_levies", "unit_levy_ledger", "levy_payments",
            "expense_transactions", "announcements", "workflow_requests",
            "compliance_items", "meetings", "feature_toggles", "settings",
            "building_assets", "facilities", "benefit_groups",
            "levy_fairness_results_v2", "financial_summary",
        ]
        for col_name in scoped_collections:
            col = db[col_name]
            result = await col.delete_many({"building_id": bid})
            if result.deleted_count:
                print(f"    Deleted {result.deleted_count} docs from {col_name}")

        await db.buildings.delete_one({"building_id": bid})
        print(f"  Deleted buildings record for {bid}")


async def main() -> None:
    _check_dependencies()
    mode = "DRY-RUN" if DRY_RUN else "LIVE"
    print(f"\n{'=' * 60}")
    print(f" Drop Demo Tower + Phase1 Ops Scheme — {mode}")
    print(f"{'=' * 60}")
    print(" Protected (never touched): 13195, UP-DEMO-001")

    from config import DATABASE_URL as _db_url  # noqa: F401
    if _db_url:
        await run_postgres_teardown()
    else:
        print("\n[WARN] DATABASE_URL not set — skipping Postgres teardown")

    await run_mongo_teardown()

    print("\nDone.")
    if not DRY_RUN:
        print(
            "\nNEXT STEP: Add the following to backend/.env to prevent "
            "Demo Tower from auto-recreating on next server restart:\n"
            "  DISABLE_DEMO_BOOTSTRAP=true"
        )


if __name__ == "__main__":
    asyncio.run(main())
