"""
backend/integrations/demo_bank/__init__.py

# @featuretrace:demo_bank — Demo Bank provider package: stateful bank-feed emulator.
# Layer: domain
# Data flow: CSV/Strata Web/manual → ingestion.py → demo_bank_transactions (Mongo)
#            → DemoBankFeed.pull_transactions() → BankTxObserved → MatchingEngine
#            Approved ReconstructionManifest → import_historical_reconstruction() →
#            demo_bank_transactions (Mongo, transaction_origin=reconstructed_historical)
# Related: backend/integrations/demo_bank/provider.py
#          backend/integrations/demo_bank/ingestion.py
#          backend/integrations/demo_bank/reconstruction_batch_schemas.py
#          backend/integrations/demo_bank/seed.py
#          backend/integrations/protocols.py
#          backend/routers/demo_bank.py
#          backend/services/reconstruction_batch_service.py (strata-management orchestrator)
# Toggle: demo_bank_feed_enabled, historical_financial_reconstruction
# Collection: demo_bank_transactions, demo_bank_accounts, demo_bank_import_batches,
#             demo_bank_reconstruction_batches, demo_bank_reconstruction_manifests
# Tests: tests/backend/test_demo_bank_provider.py, tests/python/test_import_historical_reconstruction.py

Demo Bank is a bank-feed emulator and evidence-staging layer. It receives
CSV / scraped / manual / synthetic transactions, stores them with full provenance,
and exposes them via the BankFeedProvider Protocol so the MatchingEngine and
FinancialCoreService receive standard BankTxObserved envelopes. It is also the
substitute banking layer for buildings whose historical bank-feed data is
unavailable: approved historical-reconstruction manifests are materialised here
as ordinary Demo Bank transactions, tagged transaction_origin=reconstructed_historical
so they are never confused with an observed bank statement, and then flow through
the same sync/matching/ledger pipeline as any other provider.

It never writes to levy_payments, unit_levy_ledger, or any finance.* Postgres table.
"""
from strataos_demo_integrations.demo_bank.provider import DemoBankFeed

__all__ = ["DemoBankFeed"]
