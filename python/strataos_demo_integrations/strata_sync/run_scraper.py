#!/usr/bin/env python3
"""
Strata Portal Sync — browser scraper subprocess.
Run by the FastAPI strata_sync router as a background process.

Workflow:
  1. Connect to MongoDB and update job status
  2. Launch Playwright (non-headless via xvfb-run virtual display)
  3. Log in to the strata management portal
  4. Set job status → waiting_pin and poll MongoDB for the PIN
  5. Submit PIN, navigate to committee reports
  6. Scrape financials, owner positions, and bank accounts
  7. Enrich and clean the scraped data
  8. Save preview to MongoDB → set status=preview
  9. Poll for user confirm/discard decision
 10. On confirm: upsert to MongoDB and mark complete
     On discard: mark cancelled, nothing written

Usage:
  python run_scraper.py --job-id <uuid> --building-id <id>
"""
import os
import random
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import argparse
import asyncio

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv

load_dotenv(BACKEND_DIR / ".env")

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

PORTAL_LOGIN_URL = "https://my.civiumstrata.com.au/login.aspx"
PORTAL_REPORT_URL = "https://my.civiumstrata.com.au/committeerpt.aspx"

# Injected into every page to mask automation signals
_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
const _plugins = [
  { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',              description: 'Portable Document Format' },
  { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
  { name: 'Native Client',      filename: 'internal-nacl-plugin',             description: '' },
];
Object.defineProperty(navigator, 'plugins', {
  get: () => Object.assign(_plugins, { item: (i) => _plugins[i], namedItem: (n) => _plugins.find(p => p.name === n) || null, length: _plugins.length }),
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-AU', 'en'] });
if (window.outerHeight === 0) {
  Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight + 85 });
  Object.defineProperty(window, 'outerWidth',  { get: () => window.innerWidth });
}
window.chrome = { app: { isInstalled: false }, runtime: { id: undefined, connect: function(){}, sendMessage: function(){} } };
const _origQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
if (_origQuery) {
  window.navigator.permissions.query = (p) =>
    p.name === 'notifications' ? Promise.resolve({ state: 'default' }) : _origQuery(p);
}
"""

_BSB_RE = re.compile(r'\b(\d{3}-\d{3})\b')
_ACCT_RE = re.compile(r'\b(\d{6,10})\b')
# Matches DD/MM/YYYY or D/M/YYYY — used to detect transaction detail rows
_DATE_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{4}$')


# ─── MongoDB helpers ──────────────────────────────────────────────────────────

async def update_job(jobs, job_id: str, **kwargs):
    kwargs["updated_at"] = datetime.now(timezone.utc).isoformat()
    await jobs.update_one({"job_id": job_id}, {"$set": kwargs})


async def wait_for_pin(jobs, job_id: str, timeout_secs: int = 300) -> str:
    """Poll MongoDB every 3 s for up to timeout_secs seconds waiting for PIN."""
    deadline = asyncio.get_event_loop().time() + timeout_secs
    while asyncio.get_event_loop().time() < deadline:
        job = await jobs.find_one({"job_id": job_id})
        if job and job.get("pin"):
            return job["pin"]
        await asyncio.sleep(3)
    raise TimeoutError("PIN not entered within the allowed time")


async def wait_for_confirm(jobs, job_id: str, timeout_secs: int = 600) -> str:
    """Poll MongoDB every 3 s for confirm_action='confirm' or 'discard'."""
    deadline = asyncio.get_event_loop().time() + timeout_secs
    while asyncio.get_event_loop().time() < deadline:
        job = await jobs.find_one({"job_id": job_id})
        if job and job.get("confirm_action") in ("confirm", "discard"):
            return job["confirm_action"]
        await asyncio.sleep(3)
    raise TimeoutError("Preview not confirmed or discarded within the allowed time")


# ─── Playwright helpers ───────────────────────────────────────────────────────

async def human_type(element, text: str):
    await element.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    for char in text:
        await element.type(char, delay=random.randint(60, 160))
    await asyncio.sleep(random.uniform(0.1, 0.3))


async def human_move_and_click(page, locator):
    box = await locator.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.1, 0.3))
    await locator.click()


async def _debug_screenshot(page, label: str):
    try:
        await page.screenshot(path=f"/tmp/strata_scraper_{label}.png", full_page=True)
    except Exception:
        pass


async def best_effort_network_idle(page, timeout: int = 6000):
    """Record slow network-idle waits without making portal progress depend on them."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception as exc:
        print(f"[WARN] networkidle not reached within {timeout}ms: {exc}", flush=True)


async def wait_for_any_visible(page, selectors: list[str], timeout: int = 30000):
    deadline = asyncio.get_event_loop().time() + (timeout / 1000)
    last_error = None
    while asyncio.get_event_loop().time() < deadline:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception as exc:
                last_error = exc
        await asyncio.sleep(0.25)

    try:
        url = page.url
    except Exception:
        url = "<unknown>"
    try:
        title = await page.title()
    except Exception:
        title = "<unknown>"
    detail = f"Timed out waiting for portal report content at {url!r} (title={title!r})"
    if last_error:
        detail += f"; last selector error: {last_error}"
    raise TimeoutError(detail)


async def open_committee_report(page, report_nav_timeout: int = 12000):
    """Open the committee report and verify page content, not network quietness.

    The reported failure was a Playwright timeout while waiting for
    ``networkidle`` on ``committeerpt.aspx``.  That wait state is useful as a
    settling hint, but it is not a reliable readiness signal for this portal.
    Success here requires either a known report navigation label, or a table
    while the browser is still on the committee-report URL.  A redirect back to
    login after a bad or expired PIN must not be treated as a successful scrape.
    """
    await page.goto(PORTAL_REPORT_URL, wait_until="domcontentloaded", timeout=60000)
    await best_effort_network_idle(page, timeout=6000)

    try:
        return await wait_for_any_visible(
            page,
            [
                "a:has-text('Building Financials')",
                "text=Building Financials",
                "a:has-text('Owner Positions')",
                "text=Owner Positions",
            ],
            timeout=report_nav_timeout,
        )
    except TimeoutError:
        pass

    current_url = page.url.lower()
    if "committeerpt.aspx" not in current_url:
        raise TimeoutError(
            f"Committee report did not open after PIN verification; current URL is {page.url!r}"
        )

    return await wait_for_any_visible(
        page,
        ["table"],
        timeout=30000,
    )


# ─── Data parsing ─────────────────────────────────────────────────────────────

def parse_money(val: str) -> float:
    val = str(val).replace("$", "").replace(",", "").strip()
    is_credit = "(CR)" in val or "(cr)" in val
    val = val.replace("(CR)", "").replace("(cr)", "").strip()
    try:
        amount = float(val)
        return -amount if is_credit else amount
    except ValueError:
        return 0.0


def extract_table_rows(raw_text: str, min_cols: int = 2) -> list:
    rows = []
    for line in raw_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) >= min_cols:
            rows.append(cols)
    return rows


async def expand_financial_rows(page) -> int:
    """
    Click all category expand controls on the Building Financials page so that
    invoice detail rows become visible before inner_text() reads the table.

    The Civium portal renders collapsed categories as hidden <tr> rows.  Clicking
    the category row (or its expand icon) toggles visibility.  We try four
    strategies in order:

    1. Click every <tr onclick>, waiting for any ASP.NET __doPostBack server
       response before moving on.  A 2.5 s timeout on expect_response
       distinguishes postbacks (server round-trip) from client-side JS toggles
       (no network request fires).
    2. Click explicit expand images / JS links inside table cells.
    3. Force-show all hidden <tr> elements via JS: covers inline display:none AND
       Bootstrap/SharePoint CSS classes (d-none, hidden, collapse, HideOnLoad).
    4. Re-read onclick rows after postbacks may have re-rendered the DOM and
       injected additional collapsed sections.

    Returns the number of rows that were clicked or forced visible.
    """
    expanded = 0

    # Strategy 1 — <tr onclick="..."> rows (ASP.NET GridView / custom JS toggle)
    # Snapshot before the loop: a server postback re-renders the GridView and
    # destroys locator references obtained after the fact.
    onclick_rows = await page.locator("tr[onclick]").all()
    for row in onclick_rows:
        try:
            if not await row.is_visible():
                continue
            # Wrap the click in expect_response to detect __doPostBack calls.
            # If the click triggers a form POST to the same .aspx URL the
            # response arrives here; if it's a pure client-side toggle no
            # request fires and the TimeoutError is caught.
            try:
                async with page.expect_response(
                        lambda r: ".aspx" in r.url and r.status == 200,
                        timeout=2500,
                ) as resp_info:
                    await row.click()
                await resp_info.value  # drain response body
                # Server postback — wait for DOM / UpdatePanel to settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    # networkidle can hang if the portal has background polling;
                    # fall back to a fixed wait
                    await asyncio.sleep(random.uniform(1.5, 2.5))
            except Exception:
                # No network request within 2.5 s → client-side JS toggle;
                # already done, just add a short render pause
                await asyncio.sleep(random.uniform(0.15, 0.35))
            expanded += 1
        except Exception:
            pass

    # Strategy 2 — explicit expand controls: '+' images, arrow icons, JS links
    for sel in [
        "td img[src*='plus' i]",
        "td img[src*='expand' i]",
        "td img[src*='open' i]",
        "td img[src*='arrow' i]",
        "td a[href*='javascript' i]",
        "input[type='image'][src*='plus' i]",
        "span[class*='expand' i]",
        "a[class*='expand' i]",
    ]:
        try:
            for el in await page.locator(sel).all():
                try:
                    if await el.is_visible():
                        try:
                            async with page.expect_response(
                                    lambda r: ".aspx" in r.url and r.status == 200,
                                    timeout=2500,
                            ) as resp_info:
                                await el.click()
                            await resp_info.value
                            try:
                                await page.wait_for_load_state("networkidle", timeout=6000)
                            except Exception:
                                await asyncio.sleep(random.uniform(1.5, 2.5))
                        except Exception:
                            await asyncio.sleep(random.uniform(0.1, 0.25))
                        expanded += 1
                except Exception:
                    pass
        except Exception:
            pass

    # Strategy 3 — force-show hidden rows via JS.
    # Handles: inline style.display="none", computed display:none from a
    # stylesheet, and Bootstrap/SharePoint CSS utility classes.
    try:
        forced = await page.evaluate("""
            () => {
                let count = 0;
                const HIDDEN_CLASSES = ['d-none', 'hidden', 'hide', 'collapse',
                                        'collapsed', 'HideOnLoad', 'ms-hide',
                                        'ng-hide', 'invisible'];
                document.querySelectorAll('tr').forEach(tr => {
                    // Inline style takes priority
                    if (tr.style.display === 'none') {
                        tr.style.display = '';
                        count++;
                        return;
                    }
                    // Computed style covers stylesheet-driven display:none
                    if (getComputedStyle(tr).display === 'none') {
                        tr.style.setProperty('display', 'table-row', 'important');
                        count++;
                        return;
                    }
                    // CSS utility-class hiding
                    for (const cls of HIDDEN_CLASSES) {
                        if (tr.classList.contains(cls)) {
                            tr.classList.remove(cls);
                            count++;
                            break;
                        }
                    }
                });
                return count;
            }
        """)
        expanded += forced
    except Exception:
        pass

    # Strategy 4 — after postbacks the portal may inject new collapsed sections;
    # do a second pass of onclick rows that weren't visible on the first pass.
    # Compare by count, not object identity — Playwright Locators are new objects
    # each call so `r not in onclick_rows` is always True and causes duplicate clicks.
    try:
        second_pass = await page.locator("tr[onclick]").all()
        new_rows = second_pass[len(onclick_rows):]
        for row in new_rows:
            try:
                if not await row.is_visible():
                    continue
                try:
                    async with page.expect_response(
                            lambda r: ".aspx" in r.url and r.status == 200,
                            timeout=2500,
                    ) as resp_info:
                        await row.click()
                    await resp_info.value
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        await asyncio.sleep(random.uniform(1.5, 2.5))
                except Exception:
                    await asyncio.sleep(random.uniform(0.15, 0.35))
                expanded += 1
            except Exception:
                pass
    except Exception:
        pass

    if expanded:
        await asyncio.sleep(random.uniform(1.5, 2.5))

    return expanded


async def extract_financials(page) -> list:
    await page.wait_for_selector("table", timeout=20000)

    # Expand all collapsed category sections so detail rows are visible.
    tr_before = await page.locator("tr").count()
    n_expanded = await expand_financial_rows(page)
    tr_after = await page.locator("tr").count()
    print(
        f"[INFO] expand_financial_rows: {n_expanded} action(s) → "
        f"{tr_before} → {tr_after} <tr> rows",
        flush=True,
    )
    await _debug_screenshot(page, "after_expand_financials")

    # Skip rows that are section headers, income rows, total/summary rows
    SKIP = [
        "hoolihan", "denman", "managed by", "proprietors", "category",
        "name", "planned", "actual", "variance", "previous", "folio",
        "total", "surplus", "deficit",
        "levy income",  # income rows — not expense line items
    ]
    records, seen = [], set()
    current_record = None  # track the most recently seen category row

    for table in await page.locator("table").all():
        raw = await table.inner_text()
        for cols in extract_table_rows(raw, min_cols=2):
            cat = cols[0].strip()
            if not cat:
                continue

            # Transaction detail row: col[0] is a date (DD/MM/YYYY or D/M/YYYY).
            # Attach to the most recently parsed category as a nested transaction.
            # Format: Date | Invoice/Ref | Supplier | Details | Amount
            if _DATE_RE.match(cat):
                if current_record is not None:
                    # Amount is the last column containing "$"
                    amount_raw = next(
                        (c.strip() for c in reversed(cols) if "$" in c.strip()), "0"
                    )
                    current_record.setdefault("transactions", []).append({
                        "date": cat,
                        "invoice_ref": cols[1].strip() if len(cols) > 1 else "",
                        "supplier": cols[2].strip() if len(cols) > 2 else "",
                        "details": cols[3].strip() if len(cols) > 3 else "",
                        "amount": parse_money(amount_raw),
                    })
                continue

            # Category summary row checks
            if any(p in cat.lower() for p in SKIP):
                continue
            if not any("$" in c for c in cols[1:]):
                continue
            key = cat.lower()
            if key in seen:
                continue
            seen.add(key)

            record = {
                "category": cat,
                "planned": parse_money(cols[1]) if len(cols) > 1 else 0.0,
                "actual": parse_money(cols[2]) if len(cols) > 2 else 0.0,
                "variance": parse_money(cols[3]) if len(cols) > 3 else 0.0,
                "previous": parse_money(cols[4]) if len(cols) > 4 else 0.0,
                "transactions": [],
            }
            records.append(record)
            current_record = record

    return records


async def extract_owners(page) -> list:
    """
    Extract owner levy positions from the Owner Positions table.

    Portal table columns (6):
      1. Lot        (int — same value as Unit for this building)
      2. Unit       (int — maps to UA001-UA070 or TH071-TH087)
      3. Owner      (str — multiple owners joined by ' & ' or ' and ')
      4. UOE        (int — Unit of Entitlement)
      5. Committee  (checkbox flag — ignored)
      6. Outstanding (money — positive = arrears, "(CR)" suffix = credit,
                      "$0.00" = no outstanding balance this period)

    Uses per-cell Playwright locators instead of inner_text() + tab-split to
    avoid fragility with ASP.NET WebForms table rendering.
    """
    await page.wait_for_selector("table", timeout=20000)
    records, seen = [], set()

    for table in await page.locator("table").all():
        rows = await table.locator("tr").all()
        for row in rows:
            # Read each <td> individually — reliable regardless of whitespace rendering
            cells = await row.locator("td").all_inner_texts()
            cells = [c.strip() for c in cells]

            # Require at least 5 columns (6-col format: Lot, Unit, Owner, UOE,
            # Committee, Outstanding; accept 5 in case Committee col is absent)
            if len(cells) < 5:
                continue

            # col[0]=Lot and col[1]=Unit must both be integers
            try:
                lot = int(cells[0])
                unit = int(cells[1])
            except ValueError:
                continue

            owner = cells[2].strip()
            # Skip header rows or empty owner cells
            if not owner or owner.lower() in ("owner", "name"):
                continue

            key = f"{lot}-{unit}"
            if key in seen:
                continue
            seen.add(key)

            try:
                uoe = int(cells[3])
            except (ValueError, IndexError):
                uoe = 0

            # col[4] = Committee checkbox (ignored)
            # col[5] = Outstanding in 6-col layout; col[4] in 5-col fallback
            outstanding_raw = cells[5] if len(cells) > 5 else cells[4]
            balance = parse_money(outstanding_raw)

            # Unit number: 1-70 → UA001-UA070, 71-87 → TH071-TH087
            unit_number = f"UA{unit:03d}" if unit <= 70 else f"TH{unit:03d}"

            records.append({
                "lot": lot,
                "unit": unit,
                "unit_number": unit_number,
                "owner": owner,
                "uoe": uoe,
                "balance": balance,
            })

    return records


async def extract_bank_accounts(page) -> list:
    """Detect bank rows by Australian BSB pattern (ddd-ddd).

    Bug fix: only collect tab-separated tokens that contain "$" as dollar
    amounts.  The old condition also matched long digit-only strings (e.g. the
    account number 260611108) which caused the account number to be treated as
    the admin balance ($260,611,108) instead of the real balance ($16,412.64).
    """
    records, seen = [], set()
    for table in await page.locator("table").all():
        raw = await table.inner_text()
        for line in raw.split("\n"):
            bsb_m = _BSB_RE.search(line)
            if not bsb_m:
                continue
            bsb = bsb_m.group(1)
            if bsb in seen:
                continue
            seen.add(bsb)
            # Account number: first long numeric string after the BSB
            acct_m = _ACCT_RE.search(line[bsb_m.end():])
            # Dollar amounts only — tokens with "$" so account numbers (plain
            # digits, no "$") are never mistaken for balances
            amounts = [parse_money(t) for t in line.split("\t") if "$" in t]
            records.append({
                "bsb": bsb,
                "account_number": acct_m.group(1) if acct_m else None,
                "account_name": line.strip()[:120],
                "admin_balance": amounts[0] if len(amounts) > 0 else 0.0,
                "sinking_balance": amounts[1] if len(amounts) > 1 else 0.0,
                "total_balance": amounts[2] if len(amounts) > 2 else 0.0,
            })
    return records


# ─── Data enrichment ──────────────────────────────────────────────────────────

def _classify_fund(category: str) -> str:
    """
    Classify as capital_works (sinking fund) or admin.

    All keywords are deliberate multi-word phrases to prevent substring false-positives:
    - "roof repairs"  not bare "roof"    → avoids matching "Roofing Repairs & Maintenance" (admin)
    - "capital works" not bare "capital" → avoids over-broad matches
    - NO bare "upgrade" or "improvement" → avoids matching admin categories that contain those words
    """
    cat = category.lower()
    sinking_kw = [
        "capital works",
        "roof repairs",
        "lift replacement",
        "lift repair",
        "garage door replace",
        "sprinkler system",
        "plumbing & drainage works",
        "fire protection replace",
    ]
    return "capital_works" if any(kw in cat for kw in sinking_kw) else "admin"


def enrich_financials(records: list) -> list:
    return [
        {
            **r,
            "fund": _classify_fund(r["category"]),
            "variance_pct": round((r["variance"] / r["planned"] * 100), 2) if r.get("planned") else 0.0,
        }
        for r in records
    ]


def split_owner_name(combined: str) -> tuple:
    """
    Split 'Owner A & Owner B', 'Owner A and Owner B', or 'Owner A, Owner B'
    into two names.  Returns (primary, secondary_or_empty).

    Delimiters tried in order (case-insensitive):
      ' & '   — most common portal format
      ' and ' — occasionally used
      ', '    — older portal rendering (e.g. "Mr A, Ms B")

    Consistent with the existing owner_name / owner_name_b convention in the
    units collection.
    """
    for sep in (" & ", " and ", ", "):
        idx = combined.lower().find(sep.lower())
        if idx >= 0:
            return combined[:idx].strip(), combined[idx + len(sep):].strip()
    return combined.strip(), ""


def enrich_owners(records: list) -> list:
    def status(bal):
        return "ARREARS" if bal > 0 else ("CREDIT" if bal < 0 else "CLEAR")

    enriched = []
    for r in records:
        owner_a, owner_b = split_owner_name(r.get("owner", ""))
        enriched.append({
            **r,
            "status": status(r["balance"]),
            "owner_name": owner_a,
            "owner_name_b": owner_b or None,
        })
    return enriched


def build_summary(building_id: str, owners: list, financials: list) -> dict:
    in_arrears = [o for o in owners if o["status"] == "ARREARS"]
    in_credit = [o for o in owners if o["status"] == "CREDIT"]
    clear = [o for o in owners if o["status"] == "CLEAR"]

    arrears_total = round(sum(o["balance"] for o in in_arrears), 2)
    credit_total = round(abs(sum(o["balance"] for o in in_credit)), 2)
    total_lots = len(owners)
    collection_rate = round(((total_lots - len(in_arrears)) / total_lots * 100), 1) if total_lots else 0
    risk_level = "LOW" if collection_rate >= 95 else ("MEDIUM" if collection_rate >= 88 else "HIGH")
    top_arrears = sorted(in_arrears, key=lambda x: x["balance"], reverse=True)[:5]
    overruns = sorted([f for f in financials if f["variance"] < 0 and f["planned"] > 0], key=lambda x: x["variance"])[
        :5]
    admin_fin = [f for f in financials if f["fund"] == "admin"]
    cw_fin = [f for f in financials if f["fund"] == "capital_works"]

    return {
        "building_id": building_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_lots": total_lots,
        "arrears_count": len(in_arrears),
        "credit_count": len(in_credit),
        "clear_count": len(clear),
        "arrears_total": arrears_total,
        "credit_total": credit_total,
        "collection_rate": collection_rate,
        "risk_level": risk_level,
        "top_arrears": [
            {"lot": o["lot"], "unit_number": o["unit_number"], "owner": o["owner"], "balance": o["balance"]}
            for o in top_arrears
        ],
        "budget_overruns": [
            {
                "category": f["category"],
                "fund": f["fund"],
                "planned": f["planned"],
                "actual": f["actual"],
                "overspend": abs(f["variance"]),
                "overspend_pct": abs(f["variance_pct"]),
            }
            for f in overruns
        ],
        "admin_fund": {
            "total_planned": round(sum(f["planned"] for f in admin_fin), 2),
            "total_actual": round(sum(f["actual"] for f in admin_fin), 2),
        },
        "capital_works_fund": {
            "total_planned": round(sum(f["planned"] for f in cw_fin), 2),
            "total_actual": round(sum(f["actual"] for f in cw_fin), 2),
        },
    }


# ─── MongoDB upsert ───────────────────────────────────────────────────────────

async def upsert_to_mongo(mdb, building_id: str, financials: list, owners: list, summary: dict, bank_accounts: list):
    now = datetime.now(timezone.utc).isoformat()

    # Australian financial year: July–June
    yr, mo = int(now[:4]), int(now[5:7])
    fy_start = yr - 1 if mo < 7 else yr
    financial_year = f"{fy_start}-{fy_start + 1}"

    for fin in financials:
        await mdb["strata_financials"].update_one(
            {"building_id": building_id, "category": fin["category"], "financial_year": financial_year},
            {"$set": {**fin, "building_id": building_id, "financial_year": financial_year, "updated_at": now}},
            upsert=True,
        )

    # Build a set of known unit_numbers for this building so we can guard against
    # mis-mapped lots silently missing the units collection.
    valid_units = {
        u["unit_number"]
        async for u in mdb["units"].find(
            {"building_id": building_id}, {"unit_number": 1}
        )
    }

    for owner in owners:
        un = owner["unit_number"]

        # Log owner name changes (informational only — no new collection created)
        existing = await mdb["strata_owners"].find_one(
            {"building_id": building_id, "unit_number": un},
            {"owner": 1},
        )
        if existing and existing.get("owner") and existing["owner"] != owner.get("owner", ""):
            print(
                f"[INFO] Owner name change on {un}: "
                f"{existing['owner']!r} → {owner.get('owner')!r}",
                flush=True,
            )

        # strata_owners stores the portal record including pre-split owner_name /
        # owner_name_b so callers can read either the combined or split form.
        await mdb["strata_owners"].update_one(
            {"building_id": building_id, "unit_number": un},
            {"$set": {**owner, "building_id": building_id, "updated_at": now}},
            upsert=True,
        )
        if un not in valid_units:
            print(f"[WARN] unit_number {un!r} (lot {owner.get('lot')}) not found in "
                  f"units collection — owner name NOT updated. "
                  f"Check the lot→unit_number mapping.", flush=True)
            continue
        # Update the units master registry with the portal's current owner name only.
        # Portal balance lives exclusively in strata_owners (single source of truth).
        # balance_owing / balance_credit are owned by the levy system — never touched here.
        unit_fields = {
            "owner_name": owner.get("owner_name") or owner.get("owner", ""),
        }
        if owner.get("owner_name_b"):
            unit_fields["owner_name_b"] = owner["owner_name_b"]
        await mdb["units"].update_one(
            {"building_id": building_id, "unit_number": un},
            {"$set": unit_fields},
        )

    for acct in bank_accounts:
        await mdb["bank_accounts"].update_one(
            {"building_id": building_id, "bsb": acct["bsb"]},
            {"$set": {**acct, "building_id": building_id, "updated_at": now}},
            upsert=True,
        )

    await mdb["building_summaries"].update_one(
        {"building_id": building_id},
        {"$set": {**summary, "updated_at": now}},
        upsert=True,
    )

    # Bridge scraped data into levy accounting collections so financial dashboards
    # reflect the portal snapshot without a manual CSV upload.
    await _sync_scraper_to_levy_collections(mdb, building_id, financial_year, financials, owners)


async def _sync_scraper_to_levy_collections(mdb, building_id: str, financial_year: str, financials: list,
                                            owners: list) -> None:
    """
    Mirror logic of strata_sync._sync_to_levy_collections but using raw mdb.
    financial_year: "2025-2026"  →  year = "2026"
    """
    import uuid as _uuid
    year = financial_year.split("-")[1] if "-" in financial_year else financial_year
    now = datetime.now(timezone.utc).isoformat()
    _TOTAL_UOE_FALLBACK = 10000

    admin_planned = 0.0
    admin_actual = 0.0
    cw_planned = 0.0
    cw_actual = 0.0

    # 1. Upsert levy_categories
    for fin in financials:
        fund = fin.get("fund", "admin")
        fund_type = "sinking" if fund == "capital_works" else "administrative"
        planned = round(float(fin.get("planned", 0)), 2)
        actual = round(float(fin.get("actual", 0)), 2)
        if fund == "admin":
            admin_planned += planned
            admin_actual += actual
        else:
            cw_planned += planned
            cw_actual += actual
        status = "on_track"
        if planned > 0 and actual > planned:
            status = "over_budget"
        elif actual < planned:
            status = "under_budget"
        await mdb["levy_categories"].update_one(
            {"building_id": building_id, "year": year, "name": fin["category"]},
            {"$set": {
                "building_id": building_id, "plan_id": building_id, "year": year,
                "fund_type": fund_type, "name": fin["category"], "budgeted_amount": planned,
                "actual_amount": actual,
                "previous_actual": round(float(fin.get("previous", 0)), 2),
                "variance": round(float(fin.get("variance", planned - actual)), 2),
                "status": status, "data_source": "scraper", "updated_at": now,
            }},
            upsert=True,
        )

    # 2. Create / update annual_levies
    total_uoe_from_owners = sum(int(o.get("uoe") or 0) for o in owners) if owners else 0
    existing_levy = await mdb["annual_levies"].find_one({"building_id": building_id, "year": year})
    if not existing_levy and (admin_planned > 0 or cw_planned > 0):
        await mdb["annual_levies"].insert_one({
            "id": str(_uuid.uuid4()), "building_id": building_id, "plan_id": building_id,
            "year": year, "status": "partial_actual",
            "total_uoe": total_uoe_from_owners or _TOTAL_UOE_FALLBACK,
            "admin_fund": {
                "levy_income": round(admin_planned, 2), "total_income": round(admin_planned, 2),
                "total_expenses": round(admin_actual, 2), "opening_balance": 0.0,
                "closing_balance": 0.0, "surplus_deficit": round(admin_planned - admin_actual, 2),
            },
            "sinking_fund": {
                "levy_income": round(cw_planned, 2), "total_income": round(cw_planned, 2),
                "total_expenses": round(cw_actual, 2), "opening_balance": 0.0,
                "closing_balance": 0.0, "surplus_deficit": round(cw_planned - cw_actual, 2),
            },
            "payment_schedule": [], "admin_levy_per_uoe_annual": 0.0,
            "admin_levy_per_uoe_quarterly": 0.0, "sinking_levy_per_uoe_annual": 0.0,
            "sinking_levy_per_uoe_quarterly": 0.0,
            "data_source": "scraper_import", "is_synthetic": True,
            "created_at": now, "updated_at": now,
        })
    elif existing_levy:
        upd: dict = {
            "admin_fund.total_expenses": round(admin_actual, 2),
            "sinking_fund.total_expenses": round(cw_actual, 2),
            "updated_at": now,
        }
        if existing_levy.get("is_synthetic") or existing_levy.get("data_source") == "scraper_import":
            upd.update({
                "admin_fund.levy_income": round(admin_planned, 2),
                "admin_fund.total_income": round(admin_planned, 2),
                "sinking_fund.levy_income": round(cw_planned, 2),
                "sinking_fund.total_income": round(cw_planned, 2),
            })
        await mdb["annual_levies"].update_one({"building_id": building_id, "year": year}, {"$set": upd})

    # 3. Update unit_levy_ledger
    if not owners:
        return
    levy_doc = await mdb["annual_levies"].find_one({"building_id": building_id, "year": year})
    admin_inc = float((levy_doc or {}).get("admin_fund", {}).get("total_income", 0))
    cw_inc = float((levy_doc or {}).get("sinking_fund", {}).get("total_income", 0))
    ann_total = admin_inc + cw_inc
    t_uoe = int((levy_doc or {}).get("total_uoe") or total_uoe_from_owners or _TOTAL_UOE_FALLBACK)
    a_ratio = (admin_inc / ann_total) if ann_total > 0 else 0.75
    c_ratio = (cw_inc / ann_total) if ann_total > 0 else 0.25
    today_month = datetime.now(timezone.utc).month
    q_billed = max(1, min(4, sum(1 for q in [3, 6, 9, 12] if today_month >= q)))

    for owner in owners:
        un = owner.get("unit_number")
        if not un:
            continue
        uoe = int(owner.get("uoe") or 0)
        net_bal = round(float(owner.get("balance", 0)), 2)
        if uoe > 0 and ann_total > 0 and t_uoe > 0:
            t_levied = round((uoe / t_uoe) * ann_total * q_billed / 4, 2)
        else:
            t_levied = 0.0
        t_paid = round(max(0.0, t_levied - net_bal), 2)
        existing_l = await mdb["unit_levy_ledger"].find_one(
            {"building_id": building_id, "unit_number": un, "year": year}
        )
        if existing_l:
            upd = {"net_balance": net_bal, "updated_at": now}
            if existing_l.get("data_source") == "scraper":
                upd.update({
                    "total_levied": t_levied, "total_paid": t_paid,
                    "admin_levied": round(t_levied * a_ratio, 2),
                    "admin_paid": round(t_paid * a_ratio, 2),
                    "admin_closing": round(net_bal * a_ratio, 2),
                    "sinking_levied": round(t_levied * c_ratio, 2),
                    "sinking_paid": round(t_paid * c_ratio, 2),
                    "sinking_closing": round(net_bal * c_ratio, 2),
                })
            await mdb["unit_levy_ledger"].update_one(
                {"building_id": building_id, "unit_number": un, "year": year}, {"$set": upd}
            )
        else:
            await mdb["unit_levy_ledger"].insert_one({
                "id": str(_uuid.uuid4()), "building_id": building_id, "year": year,
                "unit_number": un, "lot_number": "", "uoe": uoe, "property_type": "",
                "admin_opening": 0.0, "admin_levied": round(t_levied * a_ratio, 2),
                "admin_paid": round(t_paid * a_ratio, 2),
                "admin_closing": round(net_bal * a_ratio, 2),
                "sinking_opening": 0.0, "sinking_levied": round(t_levied * c_ratio, 2),
                "sinking_paid": round(t_paid * c_ratio, 2),
                "sinking_closing": round(net_bal * c_ratio, 2),
                "total_levied": t_levied, "total_paid": t_paid, "net_balance": net_bal,
                "data_source": "scraper", "created_at": now, "updated_at": now,
            })


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(job_id: str, building_id: str):
    client = AsyncIOMotorClient(MONGO_URL)
    mdb = client[DB_NAME]
    jobs = mdb["strata_sync_jobs"]

    try:
        portal_email = os.environ.get("PORTAL_EMAIL")
        portal_password = os.environ.get("PORTAL_PASSWORD")
        if not portal_email or not portal_password:
            await update_job(
                jobs, job_id,
                status="error",
                error="PORTAL_EMAIL or PORTAL_PASSWORD not set. Add them to backend/.env",
            )
            return

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,  # xvfb-run provides the virtual display
                slow_mo=50,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="en-AU",
                timezone_id="Australia/Sydney",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "en-AU,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            await context.add_init_script(_STEALTH_SCRIPT)
            page = await context.new_page()

            # ── Login ──────────────────────────────────────────────────────────
            await update_job(jobs, job_id, status="starting", message="Logging into the strata portal...")
            await page.goto(PORTAL_LOGIN_URL, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(1.5, 3.0))

            email_input = page.locator("input[type='email']:visible, input[type='text']:visible").first
            await email_input.wait_for(state="visible", timeout=15000)
            await email_input.click()
            await asyncio.sleep(random.uniform(0.3, 0.6))
            await email_input.fill(portal_email)
            await asyncio.sleep(random.uniform(0.4, 0.8))

            password_input = page.locator("input[type='password']:visible").first
            await password_input.wait_for(state="visible", timeout=10000)
            await password_input.click()
            await asyncio.sleep(random.uniform(0.3, 0.6))
            await password_input.fill(portal_password)
            await asyncio.sleep(random.uniform(0.8, 1.5))

            await _debug_screenshot(page, "before_login_click")

            login_btn = page.locator(
                "button:has-text('Login'), input[type='submit'][value*='Login' i], input[type='button'][value*='Login' i]"
            ).first
            await login_btn.wait_for(state="visible", timeout=10000)
            await human_move_and_click(page, login_btn)

            try:
                await page.wait_for_url(lambda url: "login.aspx" not in url.lower(), timeout=15000)
            except Exception:
                await page.wait_for_load_state("networkidle", timeout=15000)

            await asyncio.sleep(random.uniform(1.0, 2.0))
            await _debug_screenshot(page, "after_login_click")

            # ── Wait for PIN ───────────────────────────────────────────────────
            await update_job(
                jobs, job_id,
                status="waiting_pin",
                message="Check your email — enter the PIN sent to you",
            )
            try:
                pin = await wait_for_pin(jobs, job_id, timeout_secs=300)
            except TimeoutError:
                await _debug_screenshot(page, "pin_timeout")
                await update_job(jobs, job_id, status="error", error="PIN timeout — no PIN entered within 5 minutes")
                await browser.close()
                return

            # ── Submit PIN ─────────────────────────────────────────────────────
            await update_job(jobs, job_id, status="scraping", message="Verifying PIN and logging in...")
            await asyncio.sleep(random.uniform(0.5, 1.0))

            pin_input = None
            for selector in [
                "input[name*='PIN' i]", "input[id*='PIN' i]",
                "input[name*='Code' i]", "input[id*='Code' i]",
                "input[type='text']:visible", "input[type='number']:visible",
            ]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        pin_input = loc
                        break
                except Exception:
                    continue

            if pin_input is None:
                await _debug_screenshot(page, "pin_input_not_found")
                await update_job(jobs, job_id, status="error", error="Could not find PIN input field on page")
                await browser.close()
                return

            await human_type(pin_input, pin)
            await asyncio.sleep(random.uniform(0.5, 1.0))

            verify_btn = None
            for selector in [
                "input[value='Verify' i]", "button:has-text('Verify')",
                "input[value='Submit' i]", "button:has-text('Submit')",
                "input[type='submit']", "button[type='submit']",
            ]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        verify_btn = loc
                        break
                except Exception:
                    continue

            if verify_btn:
                await human_move_and_click(page, verify_btn)
            else:
                await pin_input.press("Enter")

            await best_effort_network_idle(page, timeout=20000)
            await asyncio.sleep(random.uniform(2.0, 3.5))
            await _debug_screenshot(page, "after_pin_verify")

            # ── Navigate to committee reports ──────────────────────────────────
            await update_job(jobs, job_id, status="scraping", message="Opening committee reports...")
            await open_committee_report(page)
            await asyncio.sleep(random.uniform(3.0, 5.0))

            # ── Scrape financials ──────────────────────────────────────────────
            await update_job(jobs, job_id, status="scraping", message="Extracting budget data...")
            for selector in ["text=Building Financials", "a:has-text('Building Financials')"]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await human_move_and_click(page, loc)
                        await asyncio.sleep(random.uniform(2.0, 4.0))
                        break
                except Exception:
                    continue
            financial_data = await extract_financials(page)

            # ── Scrape bank accounts (same page) ──────────────────────────────
            bank_data = await extract_bank_accounts(page)

            # ── Scrape owner positions ─────────────────────────────────────────
            await update_job(jobs, job_id, status="scraping", message="Extracting owner levy positions...")
            for selector in ["text=Owner Positions", "a:has-text('Owner Positions')"]:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await human_move_and_click(page, loc)
                        await asyncio.sleep(random.uniform(3.0, 6.0))
                        break
                except Exception:
                    continue
            owner_data = await extract_owners(page)

            await browser.close()

        # ── Enrich & build preview ─────────────────────────────────────────────
        await update_job(
            jobs, job_id,
            status="cleaning",
            message=f"Cleaning {len(financial_data)} budget items and {len(owner_data)} owner records...",
        )
        financials_clean = enrich_financials(financial_data)
        owners_clean = enrich_owners(owner_data)
        summary = build_summary(building_id, owners_clean, financials_clean)
        bank_accounts_clean = bank_data  # no enrichment needed

        # ── Preview gate — user must confirm before any DB writes ──────────────
        admin_fin = [f for f in financials_clean if f["fund"] == "admin"]
        cw_fin = [f for f in financials_clean if f["fund"] == "capital_works"]
        await update_job(
            jobs, job_id,
            status="preview",
            message=(
                    f"Review {len(admin_fin)} admin + {len(cw_fin)} sinking fund items, "
                    f"{len(owners_clean)} owner positions"
                    + (f", {len(bank_accounts_clean)} bank account(s)" if bank_accounts_clean else "")
                    + " — confirm to save or discard to cancel."
            ),
            preview_data={
                "financials": financials_clean,
                "owners": owners_clean,
                "bank_accounts": bank_accounts_clean,
                "summary": summary,
            },
        )

        try:
            action = await wait_for_confirm(jobs, job_id, timeout_secs=600)
        except TimeoutError:
            await update_job(
                jobs, job_id,
                status="error",
                error="Preview timed out — no confirmation received within 10 minutes. Start a new sync and confirm promptly.",
            )
            return

        if action == "discard":
            await update_job(
                jobs, job_id,
                status="cancelled",
                message="Preview discarded — no data was written to the system.",
            )
            return

        # ── Confirmed: write to DB ─────────────────────────────────────────────
        await update_job(jobs, job_id, status="syncing", message="Saving data to the system...")
        await upsert_to_mongo(mdb, building_id, financials_clean, owners_clean, summary, bank_accounts_clean)

        await update_job(
            jobs, job_id,
            status="complete",
            message=(
                f"Sync complete — {len(financials_clean)} budget items, "
                f"{len(owners_clean)} owner records updated"
            ),
            result=summary,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    except Exception as exc:
        await update_job(jobs, job_id, status="error", error=str(exc), error_detail=traceback.format_exc())
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strata portal sync subprocess")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--building-id", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.job_id, args.building_id))
