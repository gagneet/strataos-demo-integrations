"""
backend/integrations/demo_bank/schemas.py

# @featuretrace:demo_bank — Pydantic models for Demo Bank API and ingestion layer.
# Layer: model
# Data flow: HTTP request → schema validation → ingestion.py → demo_bank_transactions (building-scoped).
# Related: backend/integrations/demo_bank/ingestion.py
#          backend/routers/demo_bank.py
# Toggle: demo_bank_feed_enabled
# Collection: demo_bank_transactions, demo_bank_accounts, demo_bank_import_batches
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ── Constants ──────────────────────────────────────────────────────────────────

PROVIDER = "demo_bank_feed"

AccountType = Literal["trust_admin", "trust_sinking", "operating", "investment"]
Direction = Literal["credit", "debit"]
PaymentChannel = Literal["BPAY", "DEFT", "EFT", "CARD", "INTEREST", "FEE", "CHEQUE", "NPP", "OTHER"]
TransactionStatus = Literal["posted", "pending", "reversed"]
SyncStatus = Literal["pending", "synced", "failed", "ignored"]
# NOTE: "synthetic_from_budget" and "historical_mongo" are already live in production
# data (East Gate migration_027) and already trusted by strata-management's
# financial_matching.py:_PROMOTABLE_SOURCE_TYPES — kept here for consistency rather
# than introduced. The five reconstruction_* values are new, added for the historical
# reconstruction pipeline (see reconstruction_batch_schemas.py).
SourceType = Literal[
    "csv_upload", "strata_web_payment", "manual", "seed",
    "synthetic_from_budget", "historical_mongo",
    "historical_reconstruction", "agm_reconstruction",
    "financial_statement_reconstruction", "owner_ledger_import",
    "strata_platform_scrape",
]
# Coarser than SourceType — distinguishes "did StrataOS observe this at a real bank"
# from "did StrataOS model/reconstruct this transaction". Every demo_bank_transactions
# document should carry one going forward; existing rows may have it absent (treat
# missing as unknown, not as observed_bank_feed).
TransactionOrigin = Literal[
    "observed_bank_feed", "imported_bank_statement", "migrated_owner_ledger",
    "reconstructed_historical", "demo_seed", "manual_adjustment",
]
# Ordinary levies vs a special levy raised outside the normal quarterly cycle.
# Matching-clarity field only — the Postgres levy obligation-level classification
# lives on finance.levy_runs.levy_run_type in strata-management, not here.
LevyComponent = Literal["ordinary", "special_levy"]
ImportStatus = Literal["pending", "completed", "failed", "partial"]


# ── Account schemas ────────────────────────────────────────────────────────────

class DemoBankAccountCreate(BaseModel):
    account_ref: str = Field(..., min_length=1, max_length=64)
    account_name: str = Field(..., min_length=1, max_length=200)
    account_type: AccountType
    bsb: str = Field(..., pattern=r"^\d{3}-?\d{3}$")
    account_number_masked: str = Field(..., min_length=4, max_length=20)
    currency: str = Field(default="AUD")
    opening_balance_cents: int = Field(default=0)
    is_test_data: bool = False


class DemoBankAccountResponse(BaseModel):
    id: str
    building_id: str
    provider: str
    account_ref: str
    account_name: str
    account_type: AccountType
    bsb: str
    account_number_masked: str
    currency: str
    opening_balance_cents: int
    current_balance_cents: int
    status: str
    is_test_data: bool
    created_at: datetime
    updated_at: datetime


class DemoBankBalanceResponse(BaseModel):
    account_ref: str
    account_name: str
    current_balance_cents: int
    opening_balance_cents: int
    currency: str
    as_of: datetime


# ── Transaction schemas ────────────────────────────────────────────────────────

class ManualTransactionRequest(BaseModel):
    """Body for POST /demo-bank/transactions/manual (super_admin only)."""

    account_ref: str = Field(..., min_length=1, max_length=64)
    posted_date: str = Field(..., description="ISO date string: YYYY-MM-DD")
    amount_cents: int = Field(..., gt=0, description="Absolute positive integer cents")
    direction: Direction
    description: str = Field(..., min_length=1, max_length=500)
    reference: Optional[str] = Field(default=None, max_length=200)
    payer_name: Optional[str] = Field(default=None, max_length=200)
    payment_channel: PaymentChannel = Field(default="OTHER")
    effective_date: Optional[str] = Field(default=None, description="ISO date string: YYYY-MM-DD")

    @field_validator("amount_cents")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be a positive integer (use direction for sign)")
        return v


class DemoBankTransactionResponse(BaseModel):
    id: str
    building_id: str
    account_ref: str
    provider: str
    external_transaction_id: str
    posted_date: str
    effective_date: Optional[str]
    amount_cents: int
    direction: Direction
    description: str
    reference: Optional[str]
    payer_name: Optional[str]
    payment_channel: str
    source_type: SourceType
    source_batch_id: Optional[str]
    idempotency_key: str
    running_balance_cents: Optional[int]
    status: TransactionStatus
    sync_status: SyncStatus
    last_sync_attempt_at: Optional[datetime]
    finance_bank_transaction_ref: Optional[str]
    sync_error: Optional[str]
    evidence_document_id: Optional[str]
    is_test_data: bool
    created_at: datetime
    # ── Reconstruction provenance (optional — absent on observed/manual/CSV rows) ──
    transaction_origin: Optional[TransactionOrigin] = None
    reconstruction_batch_id: Optional[str] = None
    reconstruction_version: Optional[int] = None
    assumption_code: Optional[str] = None
    levy_component: Optional[LevyComponent] = None


class TransactionListResponse(BaseModel):
    building_id: str
    account_ref: str
    transactions: list[DemoBankTransactionResponse]
    total: int
    page: int
    page_size: int


# ── Import batch schemas ───────────────────────────────────────────────────────

class CsvImportRequest(BaseModel):
    """Form fields accompanying POST /demo-bank/import/csv (file upload)."""

    account_ref: str = Field(..., min_length=1, max_length=64)
    bank_name: str = Field(..., min_length=1, max_length=64,
                           description="Must match a YAML schema in bank_schemas/ e.g. 'cba', 'nab'")
    is_test_data: bool = False


class StrataWebImportRequest(BaseModel):
    """Body for POST /demo-bank/import/strata_web."""

    financial_year: str = Field(..., min_length=4, max_length=10,
                                description="e.g. '2025' or '2024-2025'")
    account_ref: str = Field(..., min_length=1, max_length=64)
    is_test_data: bool = False


class ImportBatchResponse(BaseModel):
    id: str
    building_id: str
    source_type: SourceType
    bank_name: Optional[str]
    filename: Optional[str]
    file_hash: Optional[str]
    account_ref: str
    imported_count: int
    skipped_count: int
    error_count: int
    import_status: ImportStatus
    is_test_data: bool
    created_at: datetime


class ImportResultResponse(BaseModel):
    batch_id: str
    import_status: ImportStatus
    imported_count: int
    skipped_count: int
    error_count: int
    duplicate_batch: bool = False
    message: str
