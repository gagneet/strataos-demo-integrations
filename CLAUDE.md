# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is a **satellite package**, not a standalone app: it holds the Data Upload, Strata Sync, and
Demo Bank code extracted from `gagneet/strata-management` (the main StrataOS repo) so it can be
versioned separately. It is re-imported back into that app as a dependency.

Both halves are non-functional in isolation:

- **Python** (`python/strataos_demo_integrations/`) imports directly from the main app's `backend/`
  package tree — `database`, `services.*`, `utils.*`, `integrations.protocols`, `integrations.registry`.
  These are absolute imports (`from database import db`, `from utils.auth import get_current_user`),
  not relative — they only resolve when a `backend/` directory is *also* on `sys.path` at runtime.
- **Frontend** (`frontend/src/`) expects to be consumed by the main app's Next.js build via
  `transpilePackages`, so `@/*` alias imports (`@/contexts/AuthContext`, `@/components/ui/*`) resolve
  against the host app's `src/` tree.

When reading code here, assume the surrounding `backend/`/`src/` context exists elsewhere — don't
treat missing imports as bugs to fix by adding stub modules.

## Commands

### Python

```bash
# Self-contained modules (strata_sync scraping/parsing logic — no backend/ dependency)
PYTHONPATH=python python3 -m pytest python/strataos_demo_integrations/strata_sync/test_*.py -v

# Syntax-only check (what CI actually runs — see .github/workflows/ci.yml)
python -m py_compile $(find python/strataos_demo_integrations -name '*.py')
```

Most of `tests/python/*.py` **cannot run standalone in this repo**. They import both
`strataos_demo_integrations.*` and main-app modules (e.g. `services.bank_feed_service`,
`database`), and several add `../../backend` to `sys.path`. These tests are the shared/canonical
copies used by the main `strata-management` repo's test suite (`tests/backend/`); they only pass
when this package and a sibling `backend/` are both importable — i.e. when run from within (or
symlinked/checked out alongside) the main monorepo. Don't expect `pytest tests/python/` to pass
in a bare checkout of this repo — that is expected, not a regression to fix.

The two tests living *inside* the package (`python/strataos_demo_integrations/strata_sync/
test_committee_report_scraper.py`, `test_scrape_civium_committee_report.py`) are the exception —
they test pure parsing/normalization logic with no `backend/` dependency and run fine standalone.

### Frontend

No standalone build/lint/test tooling lives in this repo — `package.json` only declares npm
`exports` (see "Package layout" below). CI's frontend check (`.github/workflows/ci.yml`) just
validates `package.json` is parseable and every export target file exists:

```bash
node -e "require('./package.json')"
for f in $(node -e "console.log(Object.values(require('./package.json').exports).filter(p => p.endsWith('.jsx')).join(' '))"); do
  test -f "$f" || echo "Missing export target: $f"
done
```

Actual linting/type-checking/building of the `.jsx` files happens in the host app's Next.js build.

## Package layout and why it's shaped this way

```
package.json                        # npm root — see note below
python/pyproject.toml               # pip package: strataos-demo-integrations
python/strataos_demo_integrations/
├── data_upload/   # Financial CSV bulk import (router, service, models) + CSV-upload mock bank feed
├── strata_sync/   # Portal browser-scraping sync ("Strata Sync" screen) + scraper subprocess script
└── demo_bank/     # Demo Bank BankFeedProvider implementation, mock Biller/ABA/Accounting/OCR
                   # providers, demo bank router, demo seed scripts, bootstrap/activation scripts
frontend/src/
├── data-upload/   # FinancialYearImportPage, FinancialDataManagementPage
└── strata-sync/   # StrataSyncPage
```

- **`package.json` lives at the repo root**, not under `frontend/`, even though its `exports` point
  into `frontend/src/`. This is required for the main app's Yarn 1 (classic) git dependency
  (`github:gagneet/strataos-demo-integrations#<tag>`) to resolve — unlike pip's
  `#subdirectory=python`, Yarn classic has no portable equivalent, so root is the only place the
  consumer's package manager will find it.
- The pip side uses the opposite layout: `python/pyproject.toml` + `#subdirectory=python` in the
  main repo's `backend/requirements.txt` pin.
- Bumping a release here means: tag this repo, then bump the pin in `strata-management`
  (`backend/requirements.txt` for Python, `frontend/package.json` for the npm dependency).

## Feature-trace header convention

Nearly every module and test file opens with a structured comment block used to trace a feature
across layers. When editing a file, keep this header in sync with what you change:

```python
# @featuretrace:<feature-slug> — one-line description
# Layer: domain | service | router | model | seed | cron | test
# Data flow: <source> → <transform/step> → <sink>  (building-scoped where relevant)
# Related: <other files this one depends on or is depended on by>
# Toggle: <feature_toggle_name(s), if gated>
# Collection: <Mongo collections touched, if any>
# Tests: <test file(s) covering this>
```

Grep `@featuretrace:<slug>` to find every file belonging to one feature (e.g. `demo_bank`,
`financial_integration_v2`, `levy`, `financial_matching`). The first line of the docstring is
usually the file's path *in the main app* (e.g. `backend/integrations/demo_bank/provider.py`),
which is the historical/logical location, not necessarily this repo's path — don't "fix" it to
match this repo's directory layout.

## Architecture

### Demo Bank (`demo_bank/`)

A stateful bank-feed **emulator and evidence-staging layer**, not a real bank integration. Pipeline:

```
CSV / Strata Web snapshot / manual entry / historical reconstruction
  → ingestion.py (normalise, upsert)
  → demo_bank_transactions (MongoDB, building-scoped)
  → DemoBankFeed.pull_transactions() [provider.py]
  → BankTxObserved envelope
  → MatchingEngine / FinancialCoreService (main app)
```

- `provider.py` implements the main app's `BankFeedProvider` Protocol so Demo Bank is
  interchangeable with real providers from the matching engine's point of view.
- **Signed-amount contract** (enforced in `provider.py`, relied on across the matching/reconciliation
  layer): `direction == "credit"` → positive `amount_cents`; `direction == "debit"` → negative.
  `ingestion.py` stores the raw absolute value; sign is applied only at the provider boundary.
- Historical-reconstruction manifests are approved out-of-band, then materialised through this same
  module as ordinary transactions tagged `transaction_origin=reconstructed_historical`, so they flow
  through the identical sync/matching/ledger pipeline as any observed bank statement.
- Demo Bank never writes to `levy_payments`, `unit_levy_ledger`, or any `finance.*` Postgres table
  directly — only to its own Mongo collections (`demo_bank_transactions`, `demo_bank_accounts`,
  `demo_bank_import_batches`, `demo_bank_reconstruction_batches`, `demo_bank_reconstruction_manifests`).
- Gated by feature toggles `demo_bank_feed_enabled` and `historical_financial_reconstruction`.

### Strata Sync (`strata_sync/`)

Scrapes third-party strata management portals (e.g. Civium) via a browser-automation subprocess and
bridges results into the financial dashboards:

```
Browser click → portal login → PIN → scrape → clean → preview
  → POST /strata/sync/push (or Postman push of pre-cleaned JSON)
  → levy_categories / annual_levies / unit_levy_ledger  (data_source="scraper", is_synthetic=True)
```

- Sync jobs are tracked in `strata_sync_jobs`, which is **global**, not tenant/building-scoped.
- Records this path creates are tagged `data_source="scraper"` / `is_synthetic=True` so user-uploaded
  data always takes precedence over scraped data.
- Feature toggle `disable_strata_sync_direct_write` bypasses the direct `unit_levy_ledger` write path
  entirely — when set, payments must instead flow Demo Bank → MatchingEngine → FinancialCoreService.
  Any change to the ledger-writing path in `router.py` or `run_scraper.py` needs to respect this toggle.
- `run_scraper.py` scrapes both the Building Financials tab (category rows expanded in place,
  producing per-category `transactions` — the inline expense detail) and the separate, paginated
  Invoices tab (`extract_invoices()`, looping the ASP.NET GridView pager). Because both views expose
  the *same* underlying invoices, `reconcile_transactions_with_invoices()` runs before the preview is
  built: for transactions dated inside the current financial year (July–June) it matches inline
  entries to Invoices-tab rows on date+amount+invoice_ref and replaces the inline copy with the
  authoritative tab version; prior-year inline rows are left untouched (the Invoices tab only
  paginates the current year); ambiguous matches and invoices with no matching category are never
  silently dropped — they surface as `preview_data.unmatched_invoices` for manual review before
  confirm. `committee_report_scraper.py` (used by the standalone `strataos-scrape-committee-report`
  CLI, not by `run_scraper.py`) has its own independent, more general-purpose pagination
  implementation for the same portal — `run_scraper.py` deliberately mirrors rather than imports it
  (see the "mirror don't import" note below).
- `run_scraper.py` deliberately does **not** import sibling modules from this package (e.g.
  `committee_report_scraper.py`) even though nothing technically prevents it — it's launched via
  `subprocess.Popen([sys.executable, script_path, ...])` as a bare script, not as part of the
  package, and its existing code comments establish the convention of mirroring shared logic
  (constants, parsing helpers, pagination) inline rather than depending on package-relative imports.
  Follow this precedent for any new logic added to `run_scraper.py`.
- `router.py` handles the interactive browser-sync flow; `run_scraper.py` is the subprocess entry
  point invoked by that flow and has its own, separately-tested sync-to-ledger function
  (`_sync_scraper_to_levy_collections`) distinct from `router.py`'s `push_data`-endpoint path — the
  two are not the same code and both need toggle/recompute-gating checked independently.

### Data Upload (`data_upload/`)

CSV bulk import for unit owners, annual levies, budget categories, and unit levy status, plus a
CSV-driven mock bank feed:

```
Staff CSV upload → POST /financial-import/{sheet_type} → service.py
  → units / annual_levies / levy_categories / unit_levy_ledger  (building-scoped)
```

- **Known landmine** (documented in `router.py`'s header comment): `process_unit_levy_status_csv()`
  replaces `unit_levy_ledger.total_paid`/`net_balance` with a wholesale `$set`. A re-upload after
  payments were recorded via the main app's `POST /levy-payments` will silently clobber those
  payment-driven totals — contradicting that endpoint's own "never overwrite from external imports"
  invariant. Be careful when touching either write path.
- `mocks/csv_upload_bank_feed.py` is a separate, simpler mock bank feed (CSV/OFX/QIF) distinct from
  Demo Bank — it writes to `integration_inbox`, not `demo_bank_transactions`.

### Role/permission pattern (all routers)

Routers use the **effective-role** pattern, never the raw role field:

```python
_role = current_user.get("effective_role") or current_user.get("role", "guest")
```

Never reference `"chairman"` as a top-level role (it's a display label, not a role value). Follow
this pattern exactly when adding or modifying role guards — the routers here reuse `utils.auth` and
`utils.permissions` from the main app rather than reimplementing role logic.

### Multi-tenancy

Every collection listed under a module's `Collection:` header is **building-scoped** unless the
module explicitly says otherwise (Strata Sync's `strata_sync_jobs` is the one call-out exception).
Tests that touch these collections should verify cross-building isolation, matching the pattern in
`tests/python/test_demo_bank_provider.py`'s multi-tenant coverage.
