"""
backend/integrations/mocks/mock_ocr.py — Mock OCRProvider.

# @featuretrace:financial_integration_v2 — mock invoice text extraction.
# Layer: service
# Data flow: PDF upload → MockOCR → OCRExtractionResult → AP draft invoice (building-scoped).
# Related: backend/integrations/protocols.py
#          backend/routers/ap_supplier_upload.py
#          backend/services/invoice_ocr_service.py

Regex + heuristic extraction from plain-text invoice content. Deliberately
not as accurate as AWS Textract, but:
  - Deterministic: same bytes always yield the same result (tests depend on this).
  - Offline: no external API call needed.
  - Per-field confidence scores: surfaces low-confidence fields in the approval UI.

The mock reads the PDF as UTF-8 text if possible (for text-layer PDFs) or
falls back to the raw bytes decoded with errors='replace'. Real PDFs from
suppliers typically have text layers; scanned images require Textract.

For bank statement extraction, CSV content is decoded and split by line;
each line is matched against the bank schemas in csv_upload_bank_feed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from integrations.envelopes import (
    OCRExtractionResult,
    OCRFieldResult,
    OCRLineItem,
    BankTxObserved,
)

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────

# ABN: 11 digits, optional spaces every 3 digits
_ABN_RE = re.compile(r"\b(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b")

# Australian invoice numbers (common patterns)
_INV_NUM_RE = re.compile(
    r"(?:invoice\s*(?:no\.?|number|#)?\s*:?\s*)([A-Z0-9][-A-Z0-9/]{2,20})",
    re.IGNORECASE,
)

# Date patterns (DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD)
_DATE_RE = re.compile(
    r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{4}|\d{4}[/\-]\d{2}[/\-]\d{2})\b"
)

# Dollar amounts: $1,234.56 or 1234.56
_AMOUNT_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")

# GST / Tax total line
_GST_RE = re.compile(
    r"(?:gst|tax|goods\s+and\s+services\s+tax)\s*:?\s*\$?\s*(\d[\d,\.]+)",
    re.IGNORECASE,
)

# Total line
_TOTAL_RE = re.compile(
    r"(?:total|amount\s+due|amount\s+payable|invoice\s+total)\s*:?\s*\$?\s*(\d[\d,\.]+)",
    re.IGNORECASE,
)

# "Tax Invoice" marker
_TAX_INVOICE_RE = re.compile(r"\btax\s+invoice\b", re.IGNORECASE)

# Vendor name: first non-empty line that looks like a company name
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:pty\.?\s*ltd\.?|ltd\.?|pty|limited|inc\.?|incorporated|"
    r"& co\.?|and co\.?|services|solutions|group|australia)\b",
    re.IGNORECASE,
)


def _parse_cents(amount_str: str) -> Optional[int]:
    """Convert '1,234.56' → 123456. Returns None on failure."""
    try:
        clean = amount_str.replace(",", "").replace("$", "").strip()
        return int(round(float(clean) * 100))
    except (ValueError, TypeError):
        return None


def _confidence(value: Optional[str], pattern_matched: bool) -> float:
    """Assign a confidence score based on whether a value was found."""
    if value is None:
        return 0.0
    return 0.85 if pattern_matched else 0.50


def _extract_text(document_bytes: bytes) -> str:
    """Best-effort text extraction from bytes (text-layer PDFs or plain text)."""
    # Try UTF-8 first (text-layer PDFs, plain text invoices)
    try:
        text = document_bytes.decode("utf-8-sig")
        if len(text) > 20:
            return text
    except UnicodeDecodeError:
        pass
    # Latin-1 fallback
    return document_bytes.decode("latin-1", errors="replace")


class MockOCR:
    """Mock OCRProvider using regex and heuristic extraction.

    name = "mock_ocr"

    Suitable for:
      - Text-layer PDFs (most supplier invoices emailed as PDF)
      - Plain text invoice templates in invoice_templates/
      - CI testing (deterministic, offline, no credentials)

    Not suitable for:
      - Scanned paper invoices (no OCR capability)
      - Handwritten content
    """

    name = "mock_ocr"

    async def extract_invoice(
            self,
            document_bytes: bytes,
            mime_type: str,
    ) -> OCRExtractionResult:
        """Extract invoice fields from a PDF or text document."""
        text = _extract_text(document_bytes)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        # ── Vendor name ───────────────────────────────────────────────────────
        vendor_name_val: Optional[str] = None
        for line in lines[:10]:
            if _COMPANY_SUFFIX_RE.search(line) and len(line) < 80:
                vendor_name_val = line
                break
        if not vendor_name_val and lines:
            vendor_name_val = lines[0][:80]

        # ── ABN ───────────────────────────────────────────────────────────────
        abn_val: Optional[str] = None
        abn_m = _ABN_RE.search(text)
        if abn_m:
            abn_val = abn_m.group(1).replace(" ", "")

        # ── Invoice number ────────────────────────────────────────────────────
        inv_val: Optional[str] = None
        inv_m = _INV_NUM_RE.search(text)
        if inv_m:
            inv_val = inv_m.group(1).strip()

        # ── Dates (first two found: issue then due) ───────────────────────────
        dates = _DATE_RE.findall(text)
        issue_date_val = dates[0] if len(dates) > 0 else None
        due_date_val = dates[1] if len(dates) > 1 else None

        # ── Amounts ───────────────────────────────────────────────────────────
        total_cents: Optional[int] = None
        gst_cents: Optional[int] = None

        total_m = _TOTAL_RE.search(text)
        if total_m:
            total_cents = _parse_cents(total_m.group(1))

        gst_m = _GST_RE.search(text)
        if gst_m:
            gst_cents = _parse_cents(gst_m.group(1))

        if total_cents is None:
            # Fall back to the largest dollar amount found
            amounts = [_parse_cents(m.group(1)) for m in _AMOUNT_RE.finditer(text)]
            amounts = [a for a in amounts if a and a > 0]
            if amounts:
                total_cents = max(amounts)

        # ── Tax invoice marker ────────────────────────────────────────────────
        is_tax_invoice = bool(_TAX_INVOICE_RE.search(text))

        # ── Confidence ───────────────────────────────────────────────────────
        fields_found = sum([
            vendor_name_val is not None,
            abn_val is not None,
            inv_val is not None,
            issue_date_val is not None,
            total_cents is not None,
        ])
        overall = round(fields_found / 5, 2)

        return OCRExtractionResult(
            vendor_name=OCRFieldResult(
                value=vendor_name_val,
                confidence=_confidence(vendor_name_val, bool(_COMPANY_SUFFIX_RE.search(text))),
            ),
            vendor_abn=OCRFieldResult(
                value=abn_val,
                confidence=0.90 if abn_val else 0.0,
            ),
            invoice_number=OCRFieldResult(
                value=inv_val,
                confidence=0.88 if inv_val else 0.0,
            ),
            issue_date=OCRFieldResult(
                value=issue_date_val,
                confidence=0.80 if issue_date_val else 0.0,
            ),
            due_date=OCRFieldResult(
                value=due_date_val,
                confidence=0.75 if due_date_val else 0.0,
            ),
            total_cents=total_cents,
            gst_cents=gst_cents,
            subtotal_cents=(total_cents - gst_cents)
            if (total_cents and gst_cents) else None,
            raw_text=text[:5000],
            overall_confidence=overall,
            is_tax_invoice=is_tax_invoice,
            provider=self.name,
        )

    async def extract_bank_statement(
            self,
            document_bytes: bytes,
    ) -> list[BankTxObserved]:
        """Extract transactions from a CSV bank statement.

        For Phase 1 this delegates to csv_upload_bank_feed's parser using
        the CBA schema as a best-effort default. The upload router provides
        the explicit bank name; this method is for automated ingestion paths.
        """
        from strataos_demo_integrations.data_upload.mocks.csv_upload_bank_feed import CsvUploadBankFeed, _load_schema, parse_csv_rows
        from request_context import get_ctx_building_id

        building_id = get_ctx_building_id()
        schema = _load_schema("cba")
        if schema is None:
            logger.warning("CBA schema not found; returning empty transaction list.")
            return []

        try:
            text = document_bytes.decode("utf-8-sig", errors="replace")
            return parse_csv_rows(text, schema, "unknown", building_id)
        except Exception as exc:
            logger.warning("extract_bank_statement failed: %s", exc)
            return []
