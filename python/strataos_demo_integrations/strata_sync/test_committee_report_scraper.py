from strataos_demo_integrations.strata_sync.committee_report_scraper import (
    enrich_money_fields,
    normalize_table,
    parse_money,
)


def test_normalize_table_preserves_detail_rows_and_duplicate_headers():
    rows = normalize_table(
        ["Date", "Amount", "Amount", ""],
        [["4/8/2026", "$1,250.50", "$125.05", "Lift service"]],
    )
    assert rows == [{
        "date": "4/8/2026",
        "amount": "$1,250.50",
        "amount_2": "$125.05",
        "column_4": "Lift service",
        "_row_number": 1,
        "_cells": ["4/8/2026", "$1,250.50", "$125.05", "Lift service"],
    }]


def test_money_parser_handles_australian_portal_credit_formats():
    assert parse_money("$1,234.56") == 1234.56
    assert parse_money("$45.00 CR") == -45.0
    assert parse_money("($12.50)") == -12.5
    assert parse_money("invoice 123") is None


def test_enrichment_adds_numeric_values_without_losing_source_text():
    [row] = enrich_money_fields([{"date": "4/8/2026", "amount": "$9.95", "_cells": ["4/8/2026", "$9.95"]}])
    assert row["amount"] == "$9.95"
    assert row["amount_numeric"] == 9.95
    assert row["_is_detail_row"] is True
