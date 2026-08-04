"""Standalone Civium committee-report scraper with an interactive PIN prompt."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path

from .committee_report_scraper import REPORT_URL, scrape_committee_report, write_exports


LOGIN_URL = "https://my.civiumstrata.com.au/login.aspx"


async def _visible(page, selectors: list[str]):
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                return locator
        await asyncio.sleep(0.25)
    raise RuntimeError(f"None of the expected controls were visible: {selectors}")


async def run(output_dir: Path, *, headless: bool) -> list[Path]:
    """Log in, request the PIN interactively, and export read-only snapshots."""
    email = os.environ.get("PORTAL_EMAIL") or input("Civium portal email: ").strip()
    password = os.environ.get("PORTAL_PASSWORD") or getpass.getpass("Civium portal password: ")
    if not email or not password:
        raise RuntimeError("Portal email and password are required")

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(locale="en-AU", timezone_id="Australia/Sydney")
        page = await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            email_input = await _visible(page, ["input[type='email']", "input[type='text']:visible"])
            password_input = await _visible(page, ["input[type='password']:visible"])
            await email_input.fill(email)
            await password_input.fill(password)
            login = await _visible(page, [
                "button:has-text('Login')", "input[type='submit'][value*='Login' i]",
                "input[type='button'][value*='Login' i]",
            ])
            await login.click()
            await page.wait_for_load_state("domcontentloaded")

            pin = getpass.getpass("PIN sent by Civium (input hidden): ").strip()
            if not pin:
                raise RuntimeError("PIN is required")
            pin_input = await _visible(page, [
                "input[name*='PIN' i]", "input[id*='PIN' i]",
                "input[name*='Code' i]", "input[id*='Code' i]",
                "input[type='number']:visible", "input[type='text']:visible",
            ])
            await pin_input.fill(pin)
            submit = await _visible(page, [
                "input[value='Verify' i]", "button:has-text('Verify')",
                "input[value='Submit' i]", "button:has-text('Submit')",
                "input[type='submit']", "button[type='submit']",
            ])
            await submit.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=60000)
            if "committeerpt.aspx" not in page.url.lower():
                raise RuntimeError(f"PIN verification failed or session expired; redirected to {page.url}")

            result = await scrape_committee_report(page)
            return write_exports(result, output_dir)
        finally:
            await context.close()
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Civium Building Financials and all Invoice pages")
    parser.add_argument("--output-dir", type=Path, default=Path("civium-export"))
    parser.add_argument("--headless", action="store_true", help="Hide Chromium (headed is the default for diagnosis)")
    args = parser.parse_args()
    paths = asyncio.run(run(args.output_dir, headless=args.headless))
    print("Export complete:")
    for path in paths:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
