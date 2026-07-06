# strataos-demo-integrations

Extracted **Data Upload**, **Strata Sync**, and **Demo Bank** code for the StrataOS strata-management
platform, split out of `gagneet/strata-management` so it can be versioned and maintained separately.

## Important: this is not a standalone package

The Python modules under `python/strataos_demo_integrations/` still import directly from the main
app's `backend/` package tree (`database`, `services.*`, `utils.*`, `integrations.protocols`,
`integrations.registry`). This is intentional — extracting those dependencies too was out of scope
for the initial split (it would touch far more of the main codebase). As a result:

- This package only works when installed into a Python process that **also** has
  `gagneet/strata-management`'s `backend/` directory on `sys.path` (which is already true for
  `uvicorn server:app` run from `backend/`, and for the test suite via `tests/backend/conftest.py`).
- The frontend package under `frontend/src/` similarly expects to be consumed by the main app's
  Next.js build, via `transpilePackages`, so that its `@/*` alias imports (`@/contexts/AuthContext`,
  `@/components/ui/*`) resolve against the host app's `src/` tree.

In other words: treat this as a **relocated subset of `strata-management`'s source**, imported back as
a dependency — not an independently deployable service.

## Layout

```
python/strataos_demo_integrations/
├── data_upload/   # Financial CSV bulk import (router, service, models) + CSV-upload mock bank feed
├── strata_sync/   # Portal browser-scraping sync ("Strata Sync" screen) + scraper subprocess script
└── demo_bank/     # Demo Bank BankFeedProvider implementation, mock Biller/ABA/Accounting/OCR providers,
                   # demo bank router, demo seed scripts, bootstrap/activation scripts

frontend/src/
├── data-upload/   # FinancialYearImportPage, FinancialDataManagementPage
└── strata-sync/   # StrataSyncPage
```

## Consumed by

`gagneet/strata-management`:
- `backend/requirements.txt` — `strataos-demo-integrations @ git+https://github.com/gagneet/strataos-demo-integrations.git@<tag>#subdirectory=python`
- `frontend/package.json` — `"@strataos/demo-integrations-frontend": "github:gagneet/strataos-demo-integrations#<tag>"`

Release a new tag here and bump the pin in the main repo to update.
