"""
Financial Import Models — Pydantic schemas for the Financial Year CSV Upload feature.

Collections written to:
  - units                  : unit_owners CSV
  - annual_levies          : annual_levy CSV
  - levy_categories        : budget_categories CSV
  - unit_levy_ledger       : unit_levy_status CSV
  - financial_import_logs  : audit trail for every import
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any


class UnitOwnerRow(BaseModel):
    """Parsed row from owner/unit details CSV."""
    lot_number: str
    unit_number: str
    unit_type: str = "apartment"  # apartment | townhouse | villa
    mixed_use_type: Optional[str] = None
    primary_owner_name: str
    secondary_owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    uoe: int = 0  # unit of entitlement (out of 10000)
    asset_value: Optional[float] = None
    status: str = "owner_occupied"  # owner_occupied | tenanted | vacant | investment
    notes: Optional[str] = None


class AnnualLevySummaryRow(BaseModel):
    """Parsed row from annual levy summary CSV."""
    financial_year: str
    admin_levy_per_uoe_proposed: float = 0.0
    admin_levy_per_uoe_actual: Optional[float] = None
    sinking_levy_per_uoe_proposed: float = 0.0
    sinking_levy_per_uoe_actual: Optional[float] = None
    admin_total_income_proposed: float = 0.0
    admin_total_income_actual: Optional[float] = None
    admin_total_expenses_proposed: float = 0.0
    admin_total_expenses_actual: Optional[float] = None
    admin_opening_balance: float = 0.0
    admin_closing_balance_projected: float = 0.0
    admin_closing_balance_actual: Optional[float] = None
    sinking_total_income_proposed: float = 0.0
    sinking_total_income_actual: Optional[float] = None
    sinking_total_expenses_proposed: float = 0.0
    sinking_total_expenses_actual: Optional[float] = None
    sinking_opening_balance: float = 0.0
    sinking_closing_balance_projected: float = 0.0
    sinking_closing_balance_actual: Optional[float] = None


class BudgetCategoryRow(BaseModel):
    """Parsed row from budget categories CSV."""
    financial_year: str
    fund_type: str  # admin | sinking | administrative
    category_name: str
    budgeted_amount: float = 0.0
    actual_amount: Optional[float] = None
    description: Optional[str] = None


class UnitLevyStatusRow(BaseModel):
    """Parsed row from per-unit levy status CSV."""
    lot_number: str
    unit_number: str
    financial_year: str
    admin_opening_balance: float = 0.0
    admin_levied: float = 0.0
    admin_paid: float = 0.0
    admin_closing_balance: float = 0.0
    sinking_opening_balance: float = 0.0
    sinking_levied: float = 0.0
    sinking_paid: float = 0.0
    sinking_closing_balance: float = 0.0
    levy_status: str = "current"  # current | arrears | credit | partial | prepaid
    q1_amount: float = 0.0
    q1_paid: float = 0.0
    q1_date: Optional[str] = None
    q2_amount: float = 0.0
    q2_paid: float = 0.0
    q2_date: Optional[str] = None
    q3_amount: float = 0.0
    q3_paid: float = 0.0
    q3_date: Optional[str] = None
    q4_amount: float = 0.0
    q4_paid: float = 0.0
    q4_date: Optional[str] = None
    arrears_amount: float = 0.0
    notes: Optional[str] = None


class ImportResult(BaseModel):
    """Result of a single CSV import operation."""
    sheet_type: str  # "unit_owners" | "annual_levy" | "budget_categories" | "unit_levy_status"
    total_rows: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = []
    warnings: List[str] = []


class FinancialYearImportResponse(BaseModel):
    """Top-level response for a single financial year import request."""
    model_config = ConfigDict(extra="ignore")
    import_id: str
    building_id: str
    financial_year: str
    sheet_type: str
    status: str  # "completed" | "partial" | "failed"
    result: ImportResult
    created_at: str
    created_by: str


class ImportHistoryEntry(BaseModel):
    """Single entry in the import history log."""
    model_config = ConfigDict(extra="ignore")
    id: str
    building_id: str
    financial_year: str
    sheet_type: str
    total_rows: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors_count: int = 0
    status: str
    created_by: str
    created_at: str


# ─────────────────────────────────────────────────────────────────────────────
# CSV template metadata — (header_row, sample_data_row)
# ─────────────────────────────────────────────────────────────────────────────

CSV_TEMPLATES: Dict[str, Any] = {
    "unit_owners": (
        "lot_number,unit_number,unit_type,mixed_use_type,primary_owner_name,"
        "secondary_owner_name,owner_email,uoe,asset_value,status,notes",
        "LOT1,UA001,apartment,,John Smith,Jane Smith,john.smith@example.com,115,650000,owner_occupied,",
    ),
    "annual_levy": (
        "financial_year,admin_levy_per_uoe_proposed,admin_levy_per_uoe_actual,"
        "sinking_levy_per_uoe_proposed,sinking_levy_per_uoe_actual,"
        "admin_total_income_proposed,admin_total_income_actual,"
        "admin_total_expenses_proposed,admin_total_expenses_actual,"
        "admin_opening_balance,admin_closing_balance_projected,admin_closing_balance_actual,"
        "sinking_total_income_proposed,sinking_total_income_actual,"
        "sinking_total_expenses_proposed,sinking_total_expenses_actual,"
        "sinking_opening_balance,sinking_closing_balance_projected,sinking_closing_balance_actual",
        "2026,23.45,,6.72,,340870.20,,340870.20,,15000.00,15000.00,"
        ",99504.90,,45000.00,,85000.00,139504.90,",
    ),
    "budget_categories": (
        "financial_year,fund_type,category_name,budgeted_amount,actual_amount,description",
        "2026,admin,Management Fee,27682.00,,Annual strata management fee",
    ),
    "unit_levy_status": (
        "lot_number,unit_number,financial_year,"
        "admin_opening_balance,admin_levied,admin_paid,admin_closing_balance,"
        "sinking_opening_balance,sinking_levied,sinking_paid,sinking_closing_balance,"
        "levy_status,q1_amount,q1_paid,q1_date,q2_amount,q2_paid,q2_date,"
        "q3_amount,q3_paid,q3_date,q4_amount,q4_paid,q4_date,arrears_amount,notes",
        "LOT1,UA001,2026,0.00,2345.00,2345.00,0.00,0.00,672.00,672.00,0.00,"
        "current,756.75,756.75,2026-03-31,756.75,756.75,2026-06-30,"
        "756.75,756.75,2026-09-30,756.75,756.75,2026-12-31,0.00,",
    ),
}

__all__ = [
    "UnitOwnerRow",
    "AnnualLevySummaryRow",
    "BudgetCategoryRow",
    "UnitLevyStatusRow",
    "ImportResult",
    "FinancialYearImportResponse",
    "ImportHistoryEntry",
    "CSV_TEMPLATES",
]
