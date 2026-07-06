"""Demo tenant + scheme bootstrap — guarantees a non-empty SA selector.

The platform should never present a Super Admin with an empty building
switcher. The simplest, most durable guarantee is to seed a single
demo tenant + scheme on startup if one isn't already present.

Two structural guard-rails make this safe to call on every backend start:

1. Migration 0024 added a partial unique index that allows exactly one
   ``is_demo = TRUE`` row in each of ``core.tenants`` and ``core.schemes``.
   Re-running the seed upserts on the natural keys without violating
   that invariant.
2. ``seeds.demo_scheme.seed()`` itself is idempotent (every INSERT uses
   ``ON CONFLICT … DO UPDATE``).

This module is the thin existence-check that decides whether to call the
full seed. It's called from the FastAPI ``startup`` event with broad
exception handling — a missing DATABASE_URL or transient connection
error must never prevent the rest of the backend from booting.

The demo flag (``is_demo``) is independent of the test-data flag
(``is_test_data`` introduced in migration 0029). Demo data is *production*
data that customers see — it must persist. Test data is transient and
swept by the pytest session-end hook.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from db_postgres.session import async_session_context

logger = logging.getLogger(__name__)

_BYPASS_UUID = "00000000-0000-0000-0000-000000000000"


async def _has_active_demo() -> bool:
    """Return True iff an active demo tenant + scheme already exist.

    Uses the RLS bypass sentinel because the demo lives in its own
    tenant; without bypass, a connection without ``app.tenant_id`` set
    sees zero rows and the caller would re-run the seed needlessly
    (still idempotent, but noisy in logs).
    """
    async with async_session_context() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :u, true)"),
            {"u": _BYPASS_UUID},
        )
        result = await session.execute(
            text("""
                 SELECT EXISTS (SELECT 1
                                FROM core.schemes s
                                         JOIN core.tenants t ON t.tenant_id = s.tenant_id
                                WHERE s.is_demo = TRUE
                                  AND t.is_demo = TRUE
                                  AND s.status = 'active')
                 """)
        )
        return bool(result.scalar())


async def ensure_demo_chain() -> None:
    """Idempotently guarantee a demo Strata Management chain exists.

    Returns silently in four cases:
      - DISABLE_DEMO_BOOTSTRAP=true env var is set (explicit opt-out).
      - DATABASE_URL is unset (running against Mongo only).
      - A demo tenant + scheme already exist (the common case).
      - The seed module imports fail (graceful degradation — the SA
        selector will simply be empty until the seed is run manually).

    Never raises. Logs at INFO on first-time seed and at DEBUG when the
    demo already exists.
    """
    import os
    if os.environ.get("DISABLE_DEMO_BOOTSTRAP", "").lower() in {"true", "1", "yes"}:
        logger.info("Demo bootstrap: disabled via DISABLE_DEMO_BOOTSTRAP env var — skipping")
        return

    try:
        from config import DATABASE_URL
    except Exception:
        return
    if not DATABASE_URL:
        return

    try:
        already = await _has_active_demo()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Demo bootstrap: existence check failed: %s", exc)
        return

    if already:
        logger.debug("Demo bootstrap: chain already present, skipping seed")
        return

    try:
        from strataos_demo_integrations.demo_bank.seeds.demo_scheme import seed as _seed_demo_chain
    except Exception as exc:  # noqa: BLE001
        logger.warning("Demo bootstrap: seed module import failed: %s", exc)
        return

    try:
        await _seed_demo_chain()
        logger.info("Demo bootstrap: seeded demo Strata Management chain")
    except Exception as exc:  # noqa: BLE001
        # ON CONFLICT clauses make the seed safe to re-run, but a partial
        # failure (e.g. RLS misconfiguration on a brand-new DB) is better
        # logged loudly than silent.
        logger.exception("Demo bootstrap: seed failed: %s", exc)
