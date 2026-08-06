# @featuretrace:strata_sync — Tests for run_scraper.py's Invoices-tab/Building-Financials
#   reconciliation, which stops the same invoice being counted once inline (row-expansion
#   detail under a Building Financials category) and again on the paginated Invoices tab.
# Layer: test
# Related: strataos_demo_integrations/strata_sync/run_scraper.py
#          (reconcile_transactions_with_invoices, _parse_portal_date,
#           _financial_year_window, _current_financial_year)
"""Regression tests for reconcile_transactions_with_invoices() and its date/FY helpers.

Coverage:
1. Exact match (date + amount + invoice_ref) inside the current FY window replaces
   the inline transaction with the Invoices-tab row, tagged with its category.
2. A transaction dated outside the FY window is left untouched even if a same-
   date+amount invoice exists (prior-year reference rows aren't reconciled).
3. An ambiguous loose match (same date+amount, different invoice_ref, more than one
   candidate) is left unresolved — the inline transaction is kept as-is rather than
   risking a wrong merge.
4. Invoices with no matching inline transaction are returned as unmatched_invoices,
   never silently dropped.
5. _parse_portal_date / _financial_year_window / _current_financial_year edge cases.
"""
from __future__ import annotations

from datetime import datetime, timezone

from strataos_demo_integrations.strata_sync.run_scraper import (
    _current_financial_year,
    _financial_year_window,
    _parse_portal_date,
    reconcile_transactions_with_invoices,
)

FY = "2025-2026"  # window: 2025-07-01 .. 2026-06-30


def _financials(transactions):
    return [{"category": "Cleaning", "planned": 1000.0, "actual": 900.0, "variance": 100.0,
             "previous": 800.0, "transactions": transactions}]


def test_exact_match_replaces_inline_transaction_and_tags_category():
    inline = [{"date": "15/08/2025", "invoice_ref": "INV-1", "supplier": "ACME",
               "details": "old details", "amount": 250.0}]
    invoice = {"date": "15/08/2025", "invoice_ref": "INV-1", "supplier": "ACME Pty Ltd",
               "details": "August cleaning", "amount": 250.0}

    reconciled, unmatched = reconcile_transactions_with_invoices(_financials(inline), [invoice], FY)

    [txn] = reconciled[0]["transactions"]
    assert txn["supplier"] == "ACME Pty Ltd"
    assert txn["details"] == "August cleaning"
    assert txn["category"] == "Cleaning"
    assert unmatched == []


def test_transaction_outside_fy_window_is_left_untouched():
    inline = [{"date": "15/06/2025", "invoice_ref": "INV-1", "supplier": "ACME",
               "details": "prior year", "amount": 250.0}]
    invoice = {"date": "15/06/2025", "invoice_ref": "INV-1", "supplier": "ACME Pty Ltd",
               "details": "August cleaning", "amount": 250.0}

    reconciled, unmatched = reconcile_transactions_with_invoices(_financials(inline), [invoice], FY)

    [txn] = reconciled[0]["transactions"]
    assert txn["supplier"] == "ACME"  # unchanged
    assert txn["details"] == "prior year"
    assert unmatched == [invoice]  # never matched, never dropped


def test_ambiguous_loose_match_is_left_unresolved():
    inline = [{"date": "15/08/2025", "invoice_ref": "", "supplier": "ACME",
               "details": "old details", "amount": 250.0}]
    invoices = [
        {"date": "15/08/2025", "invoice_ref": "INV-1", "supplier": "ACME", "details": "a", "amount": 250.0},
        {"date": "15/08/2025", "invoice_ref": "INV-2", "supplier": "ACME", "details": "b", "amount": 250.0},
    ]

    reconciled, unmatched = reconcile_transactions_with_invoices(_financials(inline), invoices, FY)

    [txn] = reconciled[0]["transactions"]
    assert txn["details"] == "old details"  # left as-is, no wrong merge
    assert len(unmatched) == 2  # both candidate invoices stay visible for manual review


def test_unmatched_invoices_never_silently_dropped():
    inline = [{"date": "15/08/2025", "invoice_ref": "INV-1", "supplier": "ACME",
               "details": "matched", "amount": 250.0}]
    invoices = [
        {"date": "15/08/2025", "invoice_ref": "INV-1", "supplier": "ACME", "details": "matched", "amount": 250.0},
        {"date": "20/08/2025", "invoice_ref": "INV-9", "supplier": "Other Co", "details": "no category",
         "amount": 75.0},
    ]

    reconciled, unmatched = reconcile_transactions_with_invoices(_financials(inline), invoices, FY)

    assert len(reconciled[0]["transactions"]) == 1
    assert unmatched == [invoices[1]]


def test_financial_year_window_is_july_to_june():
    start, end = _financial_year_window("2025-2026")
    assert (start.year, start.month, start.day) == (2025, 7, 1)
    assert (end.year, end.month, end.day) == (2026, 6, 30)


def test_parse_portal_date_accepts_single_and_double_digit_forms():
    assert _parse_portal_date("5/8/2025").isoformat() == "2025-08-05"
    assert _parse_portal_date("15/08/2025").isoformat() == "2025-08-15"


def test_parse_portal_date_rejects_non_date_text():
    assert _parse_portal_date("Opening Balance") is None
    assert _parse_portal_date("") is None


def test_current_financial_year_before_and_after_july_rollover():
    assert _current_financial_year(datetime(2026, 6, 30, tzinfo=timezone.utc)) == "2025-2026"
    assert _current_financial_year(datetime(2026, 7, 1, tzinfo=timezone.utc)) == "2026-2027"
