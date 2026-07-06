#!/usr/bin/env python3
"""backend/scripts/activate_demo_bank_pipeline.py

# @featuretrace:financial_matching — GAP-FIN-015 Phase 1 activation driver.
# Layer: cron
# Data flow: operator CLI -> core.feature_toggle_overrides (per-building) ->
#            routers.bank_feeds.sync_demo_bank_transactions() -> finance.bank_transactions ->
#            integrations.matching.engine.match() -> match_review_queue -> (building-scoped).
# Related: backend/routers/bank_feeds.py
#          backend/routers/financial_matching.py
#          docs/architecture/levy_ledger_honesty_roadmap_2026-07-05.md

Turns on the already-built Demo Bank -> bank-feeds/sync -> MatchingEngine pipeline for one
building (per-building override only, never global — see toggle_classification.py and
feature-toggle-governance.md) and drives a sync pass over whatever demo_bank_transactions
are already staged for that building, so real allocate decisions actually reach
finance.receipts instead of the pipeline sitting inert.

Defaults to a dry run. Pass --apply to actually change toggles and run the sync.

Run:
  python3 backend/scripts/activate_demo_bank_pipeline.py --building-id 13195
  python3 backend/scripts/activate_demo_bank_pipeline.py --building-id 13195 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from db_postgres.repos import config_repo  # noqa: E402
from request_context import set_ctx_building_id  # noqa: E402

_PIPELINE_TOGGLES = (
    "demo_bank_feed_enabled",
    "bank_feeds_sync_enabled",
    "financial_integration_layer_v2",
)
# GAP-FIN-015 correction (2026-07-05, live DB verification): "financial_matching_enabled" does not
# exist as a toggle anywhere in the codebase — the real router-level gate for both /bank-feeds and
# /financial-matching is "financial_integration_layer_v2" (require_feature call sites in
# routers/bank_feeds.py:57 and routers/financial_matching.py). For building 13195,
# financial_integration_layer_v2 and bank_feeds_sync_enabled already resolve True via pre-existing
# per-scheme overrides — this script's toggle upserts are idempotent no-ops for those two on 13195;
# demo_bank_feed_enabled is the only one still off. See
# docs/architecture/gap_fin_015_live_db_verification_2026-07-05.md §2.

_REASON = (
    "GAP-FIN-015 Phase 1: activate existing Demo Bank -> MatchingEngine pipeline "
    "per-building so already-staged demo_bank_transactions can post real allocations."
)

_SYSTEM_USER_EMAIL = "system:gap-fin-015-activation"


async def _resolve_actor(actor_email: str | None) -> tuple[str | None, str]:
    """Return (user_id, decided_by_label) — falls back to the oldest active super_admin."""
    actor_id = await config_repo.resolve_actor_user_id(None, actor_email, require_existing=True)
    return actor_id, (actor_email or _SYSTEM_USER_EMAIL)


async def enable_toggles(building_id: str, apply: bool, actor_email: str | None) -> dict:
    results: dict[str, object] = {}
    for key in _PIPELINE_TOGGLES:
        if not apply:
            results[key] = {"dry_run": True, "would_enable": True}
            continue
        results[key] = await config_repo.upsert_feature_toggle_override(
            building_id, key, True,
            actor_email=actor_email,
            reason=_REASON,
        )
    return results


async def run_sync(
        building_id: str,
        apply: bool,
        include_test_data: bool,
        actor_id: str | None,
        decided_by: str,
) -> dict:
    if not apply:
        return {"dry_run": True}

    from routers.bank_feeds import sync_demo_bank_transactions, BankFeedSyncRequest

    set_ctx_building_id(building_id)
    payload = BankFeedSyncRequest(include_test_data=include_test_data)
    system_user = {
        "id": actor_id,
        "email": decided_by,
        "full_name": "GAP-FIN-015 Activation Script",
        "role": "super_admin",
        "effective_role": "super_admin",
    }
    return await sync_demo_bank_transactions(
        payload=payload,
        current_user=system_user,
        building_id=building_id,
    )


async def run(
        building_id: str,
        apply: bool,
        actor_email: str | None,
        include_test_data: bool,
) -> dict:
    # _resolve_actor() hits the live Postgres DB — skip it entirely in dry-run
    # mode so --apply is the only path that requires DB connectivity at all.
    if apply:
        actor_id, decided_by = await _resolve_actor(actor_email)
    else:
        actor_id, decided_by = None, (actor_email or _SYSTEM_USER_EMAIL)
    toggle_result = await enable_toggles(building_id, apply, actor_email)
    sync_result = await run_sync(building_id, apply, include_test_data, actor_id, decided_by)
    return {
        "building_id": building_id,
        "applied": apply,
        "actor": decided_by,
        "toggles": toggle_result,
        "sync": sync_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--building-id", required=True, help="Target building_id, e.g. 13195")
    parser.add_argument(
        "--actor-email", default=None,
        help="Existing core.users email to attribute the toggle change to; "
             "defaults to the oldest active super_admin",
    )
    parser.add_argument(
        "--include-test-data", action="store_true",
        help="Also sync demo_bank_transactions rows flagged is_test_data=True",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually enable toggles and run the sync; without this flag, only prints intent",
    )
    args = parser.parse_args()

    result = asyncio.run(run(args.building_id, args.apply, args.actor_email, args.include_test_data))
    print(json.dumps(result, default=str, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
