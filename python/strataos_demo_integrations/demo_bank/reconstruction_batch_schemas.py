"""
backend/integrations/demo_bank/reconstruction_batch_schemas.py

# @featuretrace:demo_bank — Historical reconstruction batch + immutable manifest models.
# Layer: model
# Data flow: extracted financial facts (AGM/AP/CSV) → ReconstructionBatch (workflow state)
#            → ReconstructionManifest (immutable approved payload)
#            → ingestion.import_historical_reconstruction() → demo_bank_transactions.
# Related: backend/integrations/demo_bank/ingestion.py (consumes an approved manifest)
#          backend/integrations/demo_bank/schemas.py (SourceType, TransactionOrigin)
#          backend/services/reconstruction_batch_service.py (strata-management orchestrator)
# Toggle: historical_financial_reconstruction, historical_reconstruction_posting
# Collection: demo_bank_reconstruction_batches, demo_bank_reconstruction_manifests

The batch is workflow state (who reviewed it, what status it is in, running totals).
The manifest is the exact, immutable, approved data payload a sync/generate step is
allowed to act on. Once a batch reaches "approved", its current manifest is frozen —
any recalculation must create a new manifest version and moves the batch back out of
"approved" until re-approved. Nothing here talks to PostgreSQL or MongoDB directly;
these are pure Pydantic models used by ingestion.py and by the strata-management
orchestration service.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# ── Status machine ──────────────────────────────────────────────────────────────
#
# Invariants (enforced by the orchestrating service, not by these models):
#   - "approved"          requires reviewed_by, approved_by, and manifest_hash all set.
#   - "generated"          means Demo Bank rows exist matching the approved manifest exactly.
#   - "synced"              means every manifest row exists in finance.bank_transactions.
#   - "posted"              means every posting-eligible row completed receipt/expense + journal.
#   - "partially_posted"    is a visible, recoverable state (not a dead end).
#   - "superseded" batches cannot be synced.
#   - "reversed"            retains the complete audit history; never deleted.
ReconstructionBatchStatus = Literal[
    "draft",
    "extracted",
    "validation_failed",
    "needs_review",
    "approved",
    "generation_ready",
    "generated",
    "syncing",
    "synced",
    "matching",
    "partially_posted",
    "posted",
    "failed",
    "superseded",
    "reversed",
]

# Terminal/frozen statuses a batch cannot leave except via an explicit new version.
TERMINAL_BATCH_STATUSES: frozenset[ReconstructionBatchStatus] = frozenset(
    {"superseded", "reversed"}
)

# Statuses in which the governing manifest must be treated as immutable.
MANIFEST_LOCKED_STATUSES: frozenset[ReconstructionBatchStatus] = frozenset(
    {
        "approved", "generation_ready", "generated", "syncing", "synced",
        "matching", "partially_posted", "posted",
    }
)


class ReconstructionBatch(BaseModel):
    """Workflow state for one historical-reconstruction run for one building/year-range.

    Does not carry the generated transactions themselves — see ReconstructionManifest.
    """

    batch_id: str
    building_id: str
    onboarding_session_id: Optional[str] = None
    financial_year_start: int
    financial_year_end: int

    source_document_ids: list[str] = Field(default_factory=list)
    source_organisation: Optional[str] = None
    source_application: Optional[str] = None
    source_account_reference: Optional[str] = None

    reconstruction_method: str  # e.g. "gst_uoe_largest_remainder_v5"
    reconstruction_version: int = 1
    random_seed: Optional[int] = None
    payment_pattern_profile: Optional[str] = None

    gst_basis: Optional[str] = None  # "inclusive" | "exclusive"
    gst_rate: Optional[float] = None
    total_uoe: Optional[int] = None
    unit_count: Optional[int] = None

    expected_admin_cents: int = 0
    expected_sinking_cents: int = 0
    expected_special_levy_cents: int = 0
    expected_expense_cents: int = 0

    generated_credit_count: int = 0
    generated_debit_count: int = 0
    generated_credit_cents: int = 0
    generated_debit_cents: int = 0

    unresolved_arrears_cents: int = 0
    unresolved_credit_cents: int = 0
    reconciliation_variance_cents: int = 0

    status: ReconstructionBatchStatus = "draft"
    created_by: Optional[str] = None
    reviewed_by: Optional[str] = None
    approved_by: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    posted_at: Optional[datetime] = None

    assumptions: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    validation_results: dict[str, Any] = Field(default_factory=dict)
    manifest_hash: Optional[str] = None

    is_test_data: bool = False

    def is_manifest_locked(self) -> bool:
        return self.status in MANIFEST_LOCKED_STATUSES

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_BATCH_STATUSES


class ReconstructedTransactionRow(BaseModel, frozen=True):
    """One planned Demo Bank transaction inside a manifest — not yet written anywhere."""

    account_ref: str
    unit_number: str
    financial_year: str
    quarter: Optional[int] = None  # None for annual/lump-sum/special-levy rows
    fund_type: Literal["admin", "sinking"]
    levy_component: Literal["ordinary", "special_levy"] = "ordinary"
    posted_date: date
    amount_cents: int  # GST-inclusive, owner-payable, always positive
    amount_ex_gst_cents: int
    gst_cents: int
    direction: Literal["credit", "debit"] = "credit"
    assumption_code: str  # e.g. "quarterly_regular", "annual_lump_sum", "arrears_catch_up"
    description: str
    transaction_sequence: int  # disambiguates repeats within the same (unit, year, quarter, component)


class YearFundReconciliationLine(BaseModel, frozen=True):
    year: str
    fund_type: Literal["admin", "sinking"]
    expected_levy_total_cents: int
    generated_credit_cents: int
    ending_arrears_cents: int
    owner_credit_cents: int
    variance_cents: int
    within_tolerance: bool


class LotReconciliationLine(BaseModel, frozen=True):
    unit_number: str
    financial_year: str
    fund_type: Literal["admin", "sinking"]
    expected_cents: int
    generated_cents: int
    variance_cents: int


class FundBalanceReconciliationLine(BaseModel, frozen=True):
    account_ref: str
    opening_balance_cents: int
    reconstructed_credits_cents: int
    reconstructed_debits_cents: int
    closing_balance_cents: int
    matches_expected_closing: bool


class ReconstructionManifest(BaseModel):
    """The immutable, approved data payload a generate/sync step is allowed to act on.

    Once a governing ReconstructionBatch reaches an approved/locked status (see
    MANIFEST_LOCKED_STATUSES), this object must never be mutated in place — any
    recalculation creates a new ReconstructionManifest with version = prior + 1 and
    supersedes_manifest_id pointing at the old one, and the batch must move back out
    of "approved" until the new manifest is re-approved.
    """

    manifest_id: str
    batch_id: str
    building_id: str
    version: int = 1

    input_document_hashes: list[str] = Field(default_factory=list)
    input_fact_hash: str  # sha256 over the extracted facts this manifest was built from
    calculation_configuration: dict[str, Any] = Field(default_factory=dict)
    random_seed: Optional[int] = None
    generator_version: str

    expected_transaction_count: int
    expected_credit_cents: int
    expected_debit_cents: int = 0

    transactions: list[ReconstructedTransactionRow] = Field(default_factory=list)
    year_fund_reconciliation: list[YearFundReconciliationLine] = Field(default_factory=list)
    lot_reconciliation: list[LotReconciliationLine] = Field(default_factory=list)
    fund_balance_reconciliation: list[FundBalanceReconciliationLine] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    manifest_hash: str  # sha256 over the canonical serialisation of this manifest

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    generated_by: Optional[str] = None
    supersedes_manifest_id: Optional[str] = None

    is_test_data: bool = False


# ── Idempotency helpers ─────────────────────────────────────────────────────────
#
# Exact formulas — do not invent alternatives (see docs/migration/
# historical_ledger_reconciliation_plan02.md amendment 9). Identity chain that must
# stay traceable end to end:
#   Demo Bank external_transaction_id
#     -> BankTxObserved.provider_txn_id
#     -> finance.bank_transactions external reference
#     -> receipt/expense idempotency key
#     -> journal idempotency key

def batch_idempotency_seed(
    *, building_id: str, source_fact_hash: str, reconstruction_method: str, reconstruction_version: int,
) -> str:
    """Input string for sha256(...) batch idempotency — caller hashes this."""
    return "|".join([
        building_id, source_fact_hash, reconstruction_method, str(reconstruction_version),
    ])


def transaction_idempotency_seed(
    *, batch_id: str, account_ref: str, unit_number: str, financial_year: str,
    quarter: Optional[int], levy_component: str, transaction_sequence: int,
) -> str:
    """Input string for sha256(...) transaction idempotency — caller hashes this.

    Never based on the generated description text alone — description wording can
    change between reconstruction-method versions without the transaction's identity
    changing.
    """
    return "|".join([
        batch_id, account_ref, unit_number, financial_year,
        str(quarter) if quarter is not None else "annual",
        levy_component, str(transaction_sequence),
    ])
