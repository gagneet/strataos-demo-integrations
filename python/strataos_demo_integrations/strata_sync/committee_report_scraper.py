"""Playwright extraction helpers for Civium's committee report pages.

This module deliberately has no StrataOS database imports.  It can be used by
the in-app sync subprocess and by the standalone CLI without granting either
path permission to write financial records.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPORT_URL = "https://my.civiumstrata.com.au/committeerpt.aspx"
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_MONEY_RE = re.compile(r"^\(?-?\$?[\d,]+(?:\.\d{1,2})?\)?(?:\s*CR)?$", re.I)
_NEXT_TEXT = re.compile(r"^(?:next|>|»|›)$", re.I)


@dataclass(frozen=True)
class PageRows:
    """One visible table-page snapshot and its stable content fingerprint."""

    rows: list[dict[str, Any]]
    fingerprint: str


def _clean(value: str) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def _slug(value: str, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", _clean(value).lower()).strip("_")
    return value or fallback


def normalize_table(headers: list[str], body: list[list[str]]) -> list[dict[str, Any]]:
    """Convert DOM table cells into lossless row dictionaries.

    Duplicate/empty headings are made stable (``column_1``, ``amount_2``), and
    the original ordered cells are retained in ``_cells``.  Keeping both forms
    matters because WebForms detail rows often span columns differently from
    their parent summary row.
    """
    width = max([len(headers), *(len(row) for row in body)], default=0)
    keys: list[str] = []
    counts: dict[str, int] = {}
    for index in range(width):
        base = _slug(headers[index] if index < len(headers) else "", f"column_{index + 1}")
        counts[base] = counts.get(base, 0) + 1
        keys.append(base if counts[base] == 1 else f"{base}_{counts[base]}")

    result = []
    for row_index, source in enumerate(body, start=1):
        cells = [_clean(cell) for cell in source]
        if not any(cells):
            continue
        record = {key: cells[i] if i < len(cells) else "" for i, key in enumerate(keys)}
        record["_row_number"] = row_index
        record["_cells"] = cells
        result.append(record)
    return result


def parse_money(value: str) -> float | None:
    """Parse portal money while preserving non-money values as ``None``."""
    text = _clean(value)
    if not text or not _MONEY_RE.match(text):
        return None
    credit = "cr" in text.lower() or text.startswith("(")
    number = re.sub(r"[^\d.-]", "", text)
    try:
        amount = float(number)
    except ValueError:
        return None
    return -abs(amount) if credit else amount


def enrich_money_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add numeric companions without altering the portal's original strings."""
    enriched = []
    for row in rows:
        copy = dict(row)
        for key, value in row.items():
            if key.startswith("_") or not isinstance(value, str):
                continue
            parsed = parse_money(value)
            if parsed is not None:
                copy[f"{key}_numeric"] = parsed
        cells = row.get("_cells", [])
        copy["_is_detail_row"] = bool(cells and _DATE_RE.match(cells[0]))
        enriched.append(copy)
    return enriched


async def _visible_table_snapshot(page) -> PageRows:
    """Read visible report tables cell-by-cell and fingerprint their contents."""
    tables = await page.locator("table:visible").all()
    all_rows: list[dict[str, Any]] = []
    for table_index, table in enumerate(tables, start=1):
        dom_rows = await table.locator("tr:visible").all()
        matrix: list[list[str]] = []
        header_index = -1
        for index, row in enumerate(dom_rows):
            cells = await row.locator("th, td").all_inner_texts()
            cells = [_clean(cell) for cell in cells]
            if not any(cells):
                continue
            matrix.append(cells)
            if header_index < 0 and await row.locator("th").count():
                header_index = len(matrix) - 1
        if not matrix:
            continue
        if header_index < 0:
            header_index = 0
        headers = matrix[header_index]
        body = matrix[header_index + 1 :]
        for record in normalize_table(headers, body):
            record["_table_number"] = table_index
            all_rows.append(record)

    payload = json.dumps(all_rows, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return PageRows(all_rows, hashlib.sha256(payload).hexdigest())


async def expand_all_rows(page, *, max_controls: int = 500) -> int:
    """Expand every collapsed caret without accidentally collapsing open rows.

    The selectors prefer controls whose accessibility state or class says they
    are collapsed.  A bounded fallback handles Civium's small caret image (the
    supplied reference) when no semantic state is present.  Each control is
    marked in the DOM before clicking so client-side redraws cannot click it
    twice; server postbacks naturally replace the DOM and are handled by the
    next pass.
    """
    selectors = [
        "[aria-expanded='false']",
        ".collapsed:visible",
        "img[src*='expand' i]:visible",
        "img[src*='plus' i]:visible",
        "img[src*='right' i]:visible",
        "img[src*='arrow' i]:visible",
        "input[type='image'][src*='plus' i]:visible",
        "a[title*='expand' i]:visible",
        "button[title*='expand' i]:visible",
        # Civium also uses an unlabelled triangle image inside an onclick row.
        # Requiring the image avoids clicking unrelated interactive table rows.
        "tr[onclick]:has(img):visible",
    ]
    clicked = 0
    seen: set[str] = set()
    for _ in range(max_controls):
        control = None
        for selector in selectors:
            candidates = page.locator(f"{selector}:not([data-strataos-expanded])")
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible():
                    signature = "|".join([
                        selector,
                        _clean(await candidate.inner_text()),
                        (await candidate.get_attribute("src")) or "",
                        (await candidate.get_attribute("onclick")) or "",
                    ])
                    if signature in seen:
                        continue
                    seen.add(signature)
                    control = candidate
                    break
            if control is not None:
                break
        if control is None:
            break
        await control.evaluate("el => el.setAttribute('data-strataos-expanded', '1')")
        try:
            await control.click(timeout=5000)
            clicked += 1
            await asyncio.sleep(0.4)
        except Exception:
            continue
    return clicked


async def click_report_tab(page, name: str) -> None:
    selectors = [
        f"a:has-text('{name}')",
        f"button:has-text('{name}')",
        f"input[value='{name}' i]",
        f"text={name}",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() and await locator.is_visible():
            await locator.click()
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1.0)
            return
    raise RuntimeError(f"Could not find the {name!r} tab on {page.url}")


async def _next_page_control(page):
    """Return a visible, enabled pager control, or ``None`` at the last page."""
    selectors = [
        "a[rel='next']:visible",
        "a[aria-label*='next' i]:visible",
        "button[aria-label*='next' i]:visible",
        "input[value*='next' i]:visible",
        "a:has-text('Next'):visible",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() and await locator.is_enabled():
            return locator

    # ASP.NET GridView pagers frequently use a bare >, ›, » or an image.
    for locator in await page.locator("table a:visible, table input[type='image']:visible").all():
        text = _clean(await locator.inner_text())
        title = _clean((await locator.get_attribute("title")) or "")
        alt = _clean((await locator.get_attribute("alt")) or "")
        if _NEXT_TEXT.match(text) or "next" in f"{title} {alt}".lower():
            if await locator.is_enabled():
                return locator
    return None


async def extract_paginated_tab(page, name: str, *, expand_rows: bool = False, max_pages: int = 500) -> list[dict[str, Any]]:
    """Extract all visible rows from a report tab across WebForms pagination."""
    await click_report_tab(page, name)
    await page.wait_for_selector("table", timeout=20000)
    collected: list[dict[str, Any]] = []
    fingerprints: set[str] = set()

    for page_number in range(1, max_pages + 1):
        if expand_rows:
            await expand_all_rows(page)
        snapshot = await _visible_table_snapshot(page)
        if snapshot.fingerprint in fingerprints:
            raise RuntimeError(f"{name} pagination repeated page {page_number}; stopping to prevent a loop")
        fingerprints.add(snapshot.fingerprint)
        for row in enrich_money_fields(snapshot.rows):
            row["_page_number"] = page_number
            collected.append(row)

        next_control = await _next_page_control(page)
        if next_control is None:
            break
        old_fingerprint = snapshot.fingerprint
        await next_control.click()
        await page.wait_for_load_state("domcontentloaded")
        for _ in range(40):
            await asyncio.sleep(0.25)
            if (await _visible_table_snapshot(page)).fingerprint != old_fingerprint:
                break
        else:
            raise RuntimeError(f"{name} Next control did not advance from page {page_number}")
    else:
        raise RuntimeError(f"{name} exceeded the safety limit of {max_pages} pages")

    if not collected:
        raise RuntimeError(f"{name} returned no rows")
    return collected


async def scrape_committee_report(page) -> dict[str, Any]:
    """Scrape expanded Building Financials and every page of Invoices."""
    financials = await extract_paginated_tab(page, "Building Financials", expand_rows=True)
    invoices = await extract_paginated_tab(page, "Invoices", expand_rows=False)
    return {
        "source_url": page.url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "building_financials": financials,
        "invoices": invoices,
        "counts": {"building_financial_rows": len(financials), "invoice_rows": len(invoices)},
    }


def write_exports(result: dict[str, Any], output_dir: Path) -> list[Path]:
    """Write an audit-friendly JSON snapshot plus flattened CSV exports."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    json_path = output_dir / "committee_report.json"
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    written.append(json_path)

    for key, filename in (("building_financials", "building_financials.csv"), ("invoices", "invoices.csv")):
        rows = result[key]
        fieldnames = sorted({field for row in rows for field in row if field != "_cells"})
        path = output_dir / filename
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        written.append(path)
    return written
