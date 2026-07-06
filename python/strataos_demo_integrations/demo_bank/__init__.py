"""
backend/integrations/demo_bank/__init__.py

# @featuretrace:demo_bank — Demo Bank provider package: stateful bank-feed emulator.
# Layer: domain
# Data flow: CSV/Strata Web/manual → ingestion.py → demo_bank_transactions (Mongo)
#            → DemoBankFeed.pull_transactions() → BankTxObserved → MatchingEngine
# Related: backend/integrations/demo_bank/provider.py
#          backend/integrations/demo_bank/ingestion.py
#          backend/integrations/demo_bank/seed.py
#          backend/integrations/protocols.py
#          backend/routers/demo_bank.py
# Toggle: demo_bank_feed_enabled
# Collection: demo_bank_transactions, demo_bank_accounts, demo_bank_import_batches
# Tests: tests/backend/test_demo_bank_provider.py

Demo Bank is a bank-feed emulator and evidence-staging layer. It receives
CSV / scraped / manual / synthetic transactions, stores them with full provenance,
and exposes them via the BankFeedProvider Protocol so the MatchingEngine and
FinancialCoreService receive standard BankTxObserved envelopes.

It never writes to levy_payments, unit_levy_ledger, or any finance.* Postgres table.
"""
from strataos_demo_integrations.demo_bank.provider import DemoBankFeed

__all__ = ["DemoBankFeed"]
