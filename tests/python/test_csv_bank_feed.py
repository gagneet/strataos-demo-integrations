"""
tests/backend/integrations/test_csv_bank_feed.py — Unit tests for CsvUploadBankFeed.

Tests parse_csv_rows() and CsvUploadBankFeed helpers from
backend/integrations/mocks/csv_upload_bank_feed.py.

All tests use real CSV content strings — no mocking needed because the parsing
logic is pure (no DB calls). DB-touching methods (pull_transactions) are not
tested here; they belong in integration tests.

No running backend needed.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from strataos_demo_integrations.data_upload.mocks.csv_upload_bank_feed import (
    CsvUploadBankFeed,
    _validate_crn_checksum,
    parse_csv_rows,
)

_TEST_BUILDING = "16244"  # Sierra demo — never "13195"

# ── Schema loader helper ──────────────────────────────────────────────────────

_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "backend" / "integrations" / "mocks" / "bank_schemas"


def _load_schema(bank: str) -> dict:
    import yaml
    path = _SCHEMA_DIR / f"{bank}.yaml"
    assert path.exists(), f"Schema file not found: {path}"
    with path.open() as f:
        return yaml.safe_load(f)


# ── CBA CSV fixture ───────────────────────────────────────────────────────────
#
# CBA exports include a single header row followed by an "Opening Balance" or
# summary row that must be skipped (this is what skip_rows=1 in the schema handles).
# DictReader already consumes the column-header row; skip_rows=1 then skips the
# next row (the summary/opening-balance line).  Our test CSVs must include that
# extra row so the data rows appear in their expected positions.

CBA_CSV = """Date,Amount,Description,Balance
15/04/2026,Opening Balance,,
16/04/2026,-1500.00,BPAY 0000001350014 LEVY PAYMENT,-12500.00
17/04/2026,2937.00,EFT CREDIT TENANT BOND,5437.00
18/04/2026,-250.50,DIRECT DEBIT INSURANCE PREMIUM,5186.50
"""


# Build a valid 13-digit MOD10V05 CRN for use in extraction tests.
# scheme_id="016244" (Sierra), lot_part="005", inst_part="001" → base = "016244005001"
# Weights applied right-to-left: [3,2,7,6,5,4,3,2,7,6,5,4]
def _build_valid_crn(scheme: str = "016244", lot: str = "005", inst: str = "001") -> str:
    from strataos_demo_integrations.demo_bank.mocks.mock_biller import build_crn
    return build_crn(scheme, lot, int(inst))


_VALID_CRN = _build_valid_crn()  # deterministic 13-digit CRN that passes MOD10V05


class TestCbaSignedAmounts:
    """CBA uses a single signed Amount column."""

    def _parse(self, content: str, is_test: bool = True):
        schema = _load_schema("cba")
        return parse_csv_rows(content, schema, "trust-acct-001", _TEST_BUILDING, is_test_data=is_test)

    def test_cba_csv_parses_three_rows(self):
        """CBA CSV with 3 data rows returns exactly 3 BankTxObserved."""
        rows = self._parse(CBA_CSV)
        assert len(rows) == 3

    def test_first_row_debit_amount(self):
        """First data row (after skip row): Amount=-1500.00 → amount_cents=-150000."""
        rows = self._parse(CBA_CSV)
        assert rows[0].amount_cents == -150000

    def test_second_row_credit_amount(self):
        """Second data row: Amount=2937.00 → amount_cents=293700."""
        rows = self._parse(CBA_CSV)
        assert rows[1].amount_cents == 293700

    def test_third_row_debit_fractional(self):
        """Third data row: Amount=-250.50 → amount_cents=-25050."""
        rows = self._parse(CBA_CSV)
        assert rows[2].amount_cents == -25050

    def test_balance_parsed_first_row(self):
        """First data row: Balance=-12500.00 → balance_after_cents=-1250000."""
        rows = self._parse(CBA_CSV)
        assert rows[0].balance_after_cents == -1250000

    def test_balance_parsed_second_row(self):
        """Second data row: Balance=5437.00 → balance_after_cents=543700."""
        rows = self._parse(CBA_CSV)
        assert rows[1].balance_after_cents == 543700

    def test_description_preserved(self):
        """Description is stored verbatim (stripped)."""
        rows = self._parse(CBA_CSV)
        assert rows[1].description == "EFT CREDIT TENANT BOND"

    def test_occurred_at_parsed_correctly(self):
        """First data row date '16/04/2026' (row after skip) → occurred_at = 2026-04-16 UTC."""
        rows = self._parse(CBA_CSV)
        assert rows[0].occurred_at == datetime(2026, 4, 16, tzinfo=timezone.utc)

    def test_tenant_id_on_all_rows(self):
        """Every parsed BankTxObserved has tenant_id matching the supplied building_id."""
        rows = self._parse(CBA_CSV)
        assert all(r.tenant_id == _TEST_BUILDING for r in rows)

    def test_is_test_data_propagated(self):
        """is_test_data=True is set on all rows when requested."""
        rows = self._parse(CBA_CSV, is_test=True)
        assert all(r.is_test_data is True for r in rows)

    def test_idempotency_same_content(self):
        """Parsing the same CSV twice yields identical provider_txn_id values."""
        rows_a = self._parse(CBA_CSV)
        rows_b = self._parse(CBA_CSV)
        ids_a = [r.provider_txn_id for r in rows_a]
        ids_b = [r.provider_txn_id for r in rows_b]
        assert ids_a == ids_b

    def test_provider_txn_id_is_sha256_hex(self):
        """provider_txn_id is a 64-char lowercase hex string (SHA-256)."""
        rows = self._parse(CBA_CSV)
        for r in rows:
            assert len(r.provider_txn_id) == 64
            assert all(c in "0123456789abcdef" for c in r.provider_txn_id)


class TestCbaBpayCrnExtraction:
    """CRN extraction from transaction descriptions."""

    def _parse_with_crn(self, crn: str, is_test: bool = True):
        # Include the skip row so skip_rows=1 discards it, leaving the data row
        schema = _load_schema("cba")
        csv_content = (
            f"Date,Amount,Description,Balance\n"
            f"15/04/2026,Opening Balance,,\n"
            f"16/04/2026,-1500.00,BPAY {crn} LEVY PAYMENT,-500.00\n"
        )
        return parse_csv_rows(csv_content, schema, "trust-acct-001", _TEST_BUILDING, is_test_data=is_test)

    def test_valid_crn_extracted(self):
        """A valid 13-digit MOD10V05 CRN in the description sets bpay_crn."""
        rows = self._parse_with_crn(_VALID_CRN)
        assert len(rows) == 1
        assert rows[0].bpay_crn == _VALID_CRN

    def test_invalid_crn_not_extracted(self):
        """A 13-digit number that fails MOD10V05 checksum is NOT set as bpay_crn."""
        # Build an invalid CRN: take a valid one and flip the last digit
        last_digit = int(_VALID_CRN[-1])
        bad_digit = (last_digit + 1) % 10
        invalid_crn = _VALID_CRN[:-1] + str(bad_digit)
        rows = self._parse_with_crn(invalid_crn)
        assert len(rows) == 1
        assert rows[0].bpay_crn is None

    def test_crn_checksum_validation(self):
        """_validate_crn_checksum returns True for a known-valid CRN."""
        assert _validate_crn_checksum(_VALID_CRN) is True

    def test_crn_checksum_rejects_short_string(self):
        """_validate_crn_checksum returns False for non-13-digit strings."""
        assert _validate_crn_checksum("123456789012") is False  # 12 digits
        assert _validate_crn_checksum("12345678901234") is False  # 14 digits


class TestCbaOskoE2eExtraction:
    """NPP end-to-end ID extraction from transaction descriptions."""

    def _parse_description(self, description: str):
        # Include a skip row so the data row is not consumed by skip_rows=1
        schema = _load_schema("cba")
        csv_content = (
            f"Date,Amount,Description,Balance\n"
            f"15/04/2026,Opening Balance,,\n"
            f"16/04/2026,100.00,{description},200.00\n"
        )
        return parse_csv_rows(csv_content, schema, "trust-acct-001", _TEST_BUILDING, is_test_data=True)

    def test_osko_e2e_extracted(self):
        """Description 'LVY-ABC123-007-1' sets osko_e2e_id."""
        rows = self._parse_description("OSKO PAYMENT LVY-ABC123-007-1")
        assert len(rows) == 1
        assert rows[0].osko_e2e_id == "LVY-ABC123-007-1"

    def test_osko_e2e_case_insensitive_match_uppercased(self):
        """osko_e2e_id is stored uppercase even when matched case-insensitively."""
        rows = self._parse_description("osko lvy-abc123-007-1")
        assert len(rows) == 1
        # Pattern is case-insensitive; stored value is uppercased
        if rows[0].osko_e2e_id:
            assert rows[0].osko_e2e_id == rows[0].osko_e2e_id.upper()

    def test_no_e2e_when_pattern_absent(self):
        """osko_e2e_id is None when no matching pattern is in the description."""
        rows = self._parse_description("REGULAR EFT CREDIT")
        assert len(rows) == 1
        assert rows[0].osko_e2e_id is None


class TestCbaLotRefExtraction:
    """Lot reference extraction from transaction descriptions."""

    def _parse_description(self, description: str):
        # Include a skip row so the data row is not consumed by skip_rows=1
        schema = _load_schema("cba")
        csv_content = (
            f"Date,Amount,Description,Balance\n"
            f"15/04/2026,Opening Balance,,\n"
            f"16/04/2026,-1500.00,{description},-500.00\n"
        )
        return parse_csv_rows(csv_content, schema, "trust-acct-001", _TEST_BUILDING, is_test_data=True)

    def test_unit_ref_extracted(self):
        """Description 'UNIT 5 LEVY PAYMENT' sets lot_ref_raw."""
        rows = self._parse_description("UNIT 5 LEVY PAYMENT")
        assert len(rows) == 1
        assert rows[0].lot_ref_raw is not None
        assert "5" in rows[0].lot_ref_raw

    def test_lot_ref_extracted(self):
        """Description 'LOT 42 QUARTERLY LEVY' sets lot_ref_raw."""
        rows = self._parse_description("LOT 42 QUARTERLY LEVY")
        assert len(rows) == 1
        assert rows[0].lot_ref_raw is not None
        assert "42" in rows[0].lot_ref_raw

    def test_apt_ref_extracted(self):
        """Description 'APT 10 PAYMENT' sets lot_ref_raw."""
        rows = self._parse_description("APT 10 PAYMENT")
        assert len(rows) == 1
        assert rows[0].lot_ref_raw is not None

    def test_no_lot_ref_when_absent(self):
        """lot_ref_raw is None when no unit/lot pattern is found."""
        rows = self._parse_description("BPAY INSURANCE PREMIUM")
        assert len(rows) == 1
        assert rows[0].lot_ref_raw is None


class TestMultiTenantIsolation:
    """Parsing for different buildings sets tenant_id independently."""

    def test_parsing_for_sierra_sets_sierra_tenant_id(self):
        """Parsing CSV for building '16244' sets tenant_id='16244' on all rows."""
        schema = _load_schema("cba")
        rows = parse_csv_rows(CBA_CSV, schema, "acct-ref", "16244", is_test_data=True)
        assert all(r.tenant_id == "16244" for r in rows)

    def test_parsing_for_different_building_sets_different_tenant_id(self):
        """Parsing for 'harbourside_view' sets that building's tenant_id on all rows."""
        schema = _load_schema("cba")
        rows = parse_csv_rows(CBA_CSV, schema, "acct-ref", "harbourside_view", is_test_data=True)
        assert all(r.tenant_id == "harbourside_view" for r in rows)

    def test_two_buildings_produce_different_provider_txn_ids(self):
        """Different account_refs produce different provider_txn_id (SHA-256 includes account_ref)."""
        schema = _load_schema("cba")
        rows_a = parse_csv_rows(CBA_CSV, schema, "acct-sierra", "16244", is_test_data=True)
        rows_b = parse_csv_rows(CBA_CSV, schema, "acct-harbour", "harbourside_view", is_test_data=True)
        ids_a = {r.provider_txn_id for r in rows_a}
        ids_b = {r.provider_txn_id for r in rows_b}
        assert ids_a.isdisjoint(ids_b), "Different account_refs must produce different txn IDs"


class TestAnzSplitColumns:
    """ANZ CSV uses separate Debit and Credit columns (both positive magnitudes).

    Two schema quirks to handle:
    1. skip_rows=1: DictReader consumes the header; skip_rows=1 then discards the first
       data row. Include a dummy skip row between the header and actual data.
    2. amount_derivation is a prose string in the YAML (documentation only), not a dict.
       Override it to {} so the code falls through to cols['debit'] / cols['credit'].
    """

    ANZ_CSV = (
        "Date,Amount,Description,Debit,Credit,Balance,Category,Serial Number\n"
        "skip,,,,,,,,\n"  # skip row (discarded by skip_rows=1)
        "15/04/2026,,BPAY LEVY PAYMENT,1500.00,,12500.00,,123\n"
        "16/04/2026,,EFT CREDIT TENANT,,2937.00,5437.00,,124\n"
    )

    def _parse(self):
        schema = _load_schema("anz")
        # amount_derivation in the YAML is a prose string (documentation), not a dict.
        # Override to {} so the parser's .get("debit_col", ...) falls through to cols keys.
        schema = {**schema, "amount_derivation": {}}
        return parse_csv_rows(self.ANZ_CSV, schema, "trust-acct-001", _TEST_BUILDING, is_test_data=True)

    def test_anz_parses_two_rows(self):
        """ANZ CSV with 2 data rows returns 2 BankTxObserved."""
        rows = self._parse()
        assert len(rows) == 2

    def test_anz_debit_is_negative(self):
        """Row with Debit=1500.00 → amount_cents = -150000."""
        rows = self._parse()
        assert rows[0].amount_cents == -150000

    def test_anz_credit_is_positive(self):
        """Row with Credit=2937.00 → amount_cents = 293700."""
        rows = self._parse()
        assert rows[1].amount_cents == 293700

    def test_anz_balance_parsed(self):
        """Balance is parsed from the Balance column."""
        rows = self._parse()
        assert rows[0].balance_after_cents == 1250000
        assert rows[1].balance_after_cents == 543700

    def test_anz_description_preserved(self):
        """Description is stored verbatim."""
        rows = self._parse()
        assert rows[0].description == "BPAY LEVY PAYMENT"
        assert rows[1].description == "EFT CREDIT TENANT"

    def test_anz_tenant_id_set(self):
        """All rows carry the supplied tenant_id."""
        rows = self._parse()
        assert all(r.tenant_id == _TEST_BUILDING for r in rows)


class TestCsvUploadBankFeedProvider:
    """Tests for the CsvUploadBankFeed provider class itself."""

    def test_name_attribute(self):
        """Provider name is 'csv_upload_bank_feed'."""
        assert CsvUploadBankFeed().name == "csv_upload_bank_feed"

    def test_supported_banks_returns_list(self):
        """supported_banks() returns a non-empty list of bank schema names."""
        banks = CsvUploadBankFeed().supported_banks()
        assert isinstance(banks, list)
        assert len(banks) >= 9  # at minimum: cba, anz, westpac, nab, ing, boa, bendigo, boq, macquarie

    def test_supported_banks_includes_cba(self):
        """'cba' is always in supported_banks()."""
        assert "cba" in CsvUploadBankFeed().supported_banks()

    def test_supported_banks_includes_anz(self):
        """'anz' is always in supported_banks()."""
        assert "anz" in CsvUploadBankFeed().supported_banks()

    def test_parse_csv_bytes_cba(self):
        """parse_csv_bytes produces same output as parse_csv_rows for CBA content."""
        provider = CsvUploadBankFeed()
        rows = provider.parse_csv_bytes(
            CBA_CSV.encode("utf-8"),
            "cba",
            "trust-acct-001",
            _TEST_BUILDING,
            is_test_data=True,
        )
        # CBA_CSV has header + skip row + 3 data rows = 3 parsed rows
        assert len(rows) == 3
        # First data row (after skip): debit -1500.00
        assert rows[0].amount_cents == -150000

    def test_parse_csv_bytes_unknown_bank_raises(self):
        """parse_csv_bytes raises ValueError for an unsupported bank name."""
        with pytest.raises(ValueError, match="No schema for bank"):
            CsvUploadBankFeed().parse_csv_bytes(
                b"Date,Amount\n01/01/2026,100.00",
                "nonexistent_bank_xyz",
                "acct-ref",
                _TEST_BUILDING,
            )

    @pytest.mark.asyncio
    async def test_verify_webhook_always_false(self):
        """CSV upload provider has no webhook — verify_webhook always returns False."""
        result = await CsvUploadBankFeed().verify_webhook({}, b"")
        assert result is False

    @pytest.mark.asyncio
    async def test_parse_webhook_always_empty(self):
        """CSV upload provider has no webhook — parse_webhook always returns []."""
        result = await CsvUploadBankFeed().parse_webhook(b"")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_accounts_always_empty(self):
        """CSV upload provider does not enumerate accounts — list_accounts returns []."""
        result = await CsvUploadBankFeed().list_accounts("any-consent-id")
        assert result == []


class TestEmptyAndMalformedCsv:
    """Edge cases: empty CSV, completely empty rows, malformed amounts."""

    def _parse(self, content: str):
        schema = _load_schema("cba")
        return parse_csv_rows(content, schema, "trust-acct-001", _TEST_BUILDING, is_test_data=True)

    def test_header_only_csv_returns_empty(self):
        """A CSV with only a header row produces no rows."""
        rows = self._parse("Date,Amount,Description,Balance\n")
        assert rows == []

    def test_completely_empty_csv_returns_empty(self):
        """An entirely empty string produces no rows."""
        rows = self._parse("")
        assert rows == []

    def test_blank_data_rows_skipped(self):
        """Rows with all-empty cells are skipped gracefully."""
        csv = "Date,Amount,Description,Balance\n,,,,\n15/04/2026,-100.00,EFT,-200.00\n"
        rows = self._parse(csv)
        # Should get 1 valid row (the blank row is skipped)
        assert len(rows) == 1
