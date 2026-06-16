"""
Seniorly providers scraper — Phase 1.

Step 1: Scrapes https://www.seniorly.com/providers for all provider hrefs.
Step 2: For each provider page, paginates through community listings.
Output: outputs/providers_communities.csv

Usage:
    python providers_scraper.py                # all providers
    python providers_scraper.py --limit 5      # first 5 providers only
    python providers_scraper.py --workers 3    # parallel provider workers
    python providers_scraper.py --visible      # visible browser
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

BASE_URL     = "https://www.seniorly.com"
PROVIDERS_URL = f"{BASE_URL}/providers"
OUTPUT_DIR   = Path(__file__).parent / "outputs"
OUT_CSV      = OUTPUT_DIR / "providers_communities.csv"
CHECKPOINT   = OUTPUT_DIR / "providers_checkpoint.json"

CARE_TYPE_PREFIXES = (
    "/assisted-living/",
    "/memory-care/",
    "/independent-living/",
    "/nursing-homes/",
    "/continuing-care-retirement-community/",
    "/board-and-care-home/",
    "/in-home-care/",
)

FIELDS = ["provider_name", "provider_url", "name", "address", "services", "price", "rating", "url"]

BROWSER_ARGS = [
    "--incognito",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1920,1080",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ── Browser helper ─────────────────────────────────────────────────────────────

async def new_page(p, headless: bool):
    browser = await p.chromium.launch(headless=headless, args=BROWSER_ARGS)
    ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=UA)
    page = await ctx.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, page


# ── Provider list ──────────────────────────────────────────────────────────────

RESTART_EVERY = 15

async def get_provider_hrefs(page) -> list[dict]:
    """Return [{name, url}] for all providers on /providers."""
    log.info("Loading %s …", PROVIDERS_URL)
    await page.goto(PROVIDERS_URL, wait_until="load", timeout=60_000)
    await page.wait_for_timeout(2000)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1500)

    # Extract all /providers/<slug> links (depth 1 only — no sub-paths)
    providers = await page.evaluate("""
        () => {
            const seen = new Set();
            const results = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.getAttribute('href');
                if (!href) return;
                const m = href.match(/^\\/providers\\/([\\w-]+)\\/?$/);
                if (!m) return;
                const url = 'https://www.seniorly.com' + href.replace(/\\/$/, '');
                if (seen.has(url)) return;
                seen.add(url);
                const name = (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' ');
                results.push({name: name || m[1], url});
            });
            return results;
        }
    """)
    log.info("Found %d providers", len(providers))
    return providers


# ── Community extraction ───────────────────────────────────────────────────────

def _is_community_href(href: str) -> bool:
    if href.startswith("https://www.seniorly.com"):
        href = href[len("https://www.seniorly.com"):]
    return any(href.startswith(p) for p in CARE_TYPE_PREFIXES) and href.count("/") >= 4


async def _extract_community_card(card) -> dict | None:
    """Extract data from one community card element."""
    try:
        # The <a href> wraps the <article> as its parent
        href = await card.evaluate(
            "el => (el.closest('a[href]') || el.parentElement || {}).getAttribute?.('href') || ''"
        ) or ""
        if not href:
            # Fallback: any link inside the card
            for a in await card.locator("a[href]").all():
                h = await a.get_attribute("href") or ""
                if _is_community_href(h):
                    href = h
                    break
        if not _is_community_href(href):
            return None

        if href.startswith("http"):
            url = href
        else:
            url = BASE_URL + href
        text = (await card.inner_text()).strip()

        # Parse name — first non-empty line
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        name = lines[0] if lines else ""

        # Address — line with a digit (street number)
        address = next((l for l in lines[1:] if re.search(r"\d", l) and len(l) > 10), "")

        # Services — known care-type keywords
        care_kw = {"Assisted Living", "Memory Care", "Independent Living",
                   "Nursing Home", "Continuing Care", "Board and Care", "In-Home Care"}
        services = ", ".join(l for l in lines if any(kw in l for kw in care_kw))

        # Price — $X,XXX/mo pattern
        price_m = re.search(r"\$[\d,]+/mo", text)
        price = price_m.group(0) if price_m else ""

        # Rating — 9.x/10 or simple decimal
        rating_m = re.search(r"\b(\d\.\d)\b", text)
        rating = rating_m.group(1) if rating_m else ""

        return {"name": name, "address": address, "services": services,
                "price": price, "rating": rating, "url": url}
    except Exception as e:
        log.debug("Card extract error: %s", e)
        return None


async def _next_page_button(page):
    """Return the Next button locator, or None if not found / already last page."""
    for sel in [
        'button[aria-label="Next page"]',
        'a[aria-label="Next page"]',
        'button:has-text("Next")',
        'a:has-text("Next")',
        '[data-testid="pagination-next"]',
        'li.next > a',
        'button[aria-label*="next" i]',
        'a[aria-label*="next" i]',
    ]:
        btn = page.locator(sel).first
        if await btn.count():
            disabled = await btn.get_attribute("disabled") or await btn.get_attribute("aria-disabled") or ""
            if disabled.lower() not in ("true", "disabled", ""):
                return btn
            # it exists but might be enabled — check via class
            cls = await btn.get_attribute("class") or ""
            if "disabled" not in cls.lower():
                return btn
    return None


async def scrape_provider_communities(page, provider: dict) -> list[dict]:
    """Paginate through a provider page and collect all community listings."""
    url   = provider["url"]
    pname = provider["name"]
    results = []
    page_num = 0

    log.info("[%s] Loading %s", pname, url)
    try:
        await page.goto(url, wait_until="load", timeout=60_000)
        await page.wait_for_timeout(1500)
    except PlaywrightTimeout:
        log.warning("[%s] Timeout loading provider page", pname)
        return results

    while True:
        page_num += 1
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)

        # Try to extract from __NEXT_DATA__ first
        next_data = await page.evaluate("() => { try { return JSON.stringify(window.__NEXT_DATA__); } catch(e) { return null; } }")
        if next_data:
            try:
                nd = json.loads(next_data)
                # Walk props to find community list
                props = nd.get("props", {}).get("pageProps", {})
                communities = (
                    props.get("communities") or
                    props.get("providers") or
                    props.get("listings") or
                    []
                )
                if communities and isinstance(communities, list):
                    for c in communities:
                        slug = c.get("slug") or c.get("url") or c.get("id", "")
                        care = c.get("careType") or c.get("care_type") or "assisted-living"
                        st   = (c.get("state") or c.get("stateSlug") or "").lower().replace(" ", "-")
                        city = (c.get("city") or c.get("citySlug") or "").lower().replace(" ", "-")
                        if slug and st and city:
                            community_url = f"{BASE_URL}/{care}/{st}/{city}/{slug}"
                            results.append({
                                "provider_name": pname,
                                "provider_url":  url,
                                "name":     c.get("name", ""),
                                "address":  c.get("address", ""),
                                "services": care,
                                "price":    str(c.get("startingPrice") or c.get("price") or ""),
                                "rating":   str(c.get("seniorlyScore") or c.get("rating") or ""),
                                "url":      community_url,
                            })
                    log.info("[%s] p%d  %d communities via __NEXT_DATA__ (%d total)",
                             pname, page_num, len(communities), len(results))
                    # Check for more pages via __NEXT_DATA__
                    total_count = props.get("totalCount") or props.get("total") or 0
                    if total_count and len(results) >= int(total_count):
                        break
            except Exception as e:
                log.debug("[%s] __NEXT_DATA__ parse error: %s", pname, e)

        # HTML fallback — look for community cards
        if page_num == 1 and not results:
            # Broad card selector — try various container patterns
            card_sels = [
                "article",
                "[data-testid='community-card']",
                "[class*='CommunityCard']",
                "[class*='community-card']",
                "[class*='ListingCard']",
                "li:has(a[href*='/assisted-living/'])",
                "div:has(a[href*='/assisted-living/'])",
            ]
            for sel in card_sels:
                cards = page.locator(sel)
                count = await cards.count()
                if count > 2:
                    log.info("[%s] p%d  found %d cards via '%s'", pname, page_num, count, sel)
                    for i in range(count):
                        data = await _extract_community_card(cards.nth(i))
                        if data:
                            data.update({"provider_name": pname, "provider_url": url})
                            results.append(data)
                    break

        # Paginate
        next_btn = await _next_page_button(page)
        if not next_btn:
            log.info("[%s] No next page — done at p%d (%d total)", pname, page_num, len(results))
            break
        await next_btn.click()
        await page.wait_for_timeout(2500)

    return results


# ── Checkpoint helpers ─────────────────────────────────────────────────────────

def load_checkpoint() -> set:
    if CHECKPOINT.exists():
        return set(json.loads(CHECKPOINT.read_text()).get("done", []))
    return set()


def save_checkpoint(done: set):
    CHECKPOINT.write_text(json.dumps({"done": sorted(done)}, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

async def _worker(worker_id: int, queue: asyncio.Queue, headless: bool,
                  csv_writer, csv_file, lock: asyncio.Lock, done: set):
    """One worker — pulls providers from queue, reuses browser, restarts every RESTART_EVERY."""
    browser = page = None
    count = 0

    async with async_playwright() as p:
        while True:
            pv = await queue.get()
            if pv is None:
                queue.put_nowait(None)  # re-add sentinel for other workers
                break

            # Launch or restart browser
            if browser is None or count % RESTART_EVERY == 0:
                if browser:
                    await browser.close()
                    log.info("[W%d] Browser restarted after %d providers", worker_id, count)
                browser, page = await new_page(p, headless)

            try:
                communities = await scrape_provider_communities(page, pv)
            except Exception as e:
                log.error("[W%d] Error on %s: %s", worker_id, pv["name"], e)
                communities = []

            # Per-provider CSV
            safe_name = re.sub(r"[^\w-]", "_", pv["name"]).strip("_") or "provider"
            provider_csv = OUTPUT_DIR / f"{safe_name}_phase1.csv"
            with open(provider_csv, "w", newline="", encoding="utf-8") as pf:
                pw = csv.DictWriter(pf, fieldnames=FIELDS)
                pw.writeheader()
                for c in communities:
                    pw.writerow({f: c.get(f, "") for f in FIELDS})

            async with lock:
                for c in communities:
                    csv_writer.writerow({f: c.get(f, "") for f in FIELDS})
                csv_file.flush()
                done.add(pv["url"])
                save_checkpoint(done)
                log.info("[W%d] Saved %d communities for [%s] → %s", worker_id, len(communities), pv["name"], provider_csv.name)

            count += 1

        if browser:
            await browser.close()

    log.info("[W%d] Done (%d providers)", worker_id, count)


async def run(headless: bool = True, limit: int = 0, workers: int = 2):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    done = load_checkpoint()

    # Get provider list using a single short-lived browser
    async with async_playwright() as p:
        browser, page = await new_page(p, headless)
        try:
            providers = await get_provider_hrefs(page)
        finally:
            await browser.close()

    if limit:
        providers = providers[:limit]

    pending = [pv for pv in providers if pv["url"] not in done]
    log.info("%d providers total, %d pending, %d workers", len(providers), len(pending), workers)

    if not pending:
        log.info("All providers already done.")
        return

    # Open CSV (append mode so we can resume)
    is_new = not OUT_CSV.exists()
    csv_file   = open(OUT_CSV, "a", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
    if is_new:
        csv_writer.writeheader()
    csv_file.flush()

    lock = asyncio.Lock()
    queue: asyncio.Queue = asyncio.Queue()
    for pv in pending:
        queue.put_nowait(pv)
    queue.put_nowait(None)  # sentinel

    await asyncio.gather(*[
        _worker(i + 1, queue, headless, csv_writer, csv_file, lock, done)
        for i in range(workers)
    ])

    csv_file.close()
    log.info("Done. Output → %s", OUT_CSV)


def main():
    ap = argparse.ArgumentParser(description="Scrape Seniorly /providers community list")
    ap.add_argument("--limit",   type=int, default=0,     help="Max providers to scrape (0=all)")
    ap.add_argument("--workers", type=int, default=2,     help="Parallel browser workers")
    ap.add_argument("--visible", action="store_true",     help="Show browser window")
    args = ap.parse_args()
    asyncio.run(run(headless=not args.visible, limit=args.limit, workers=args.workers))


if __name__ == "__main__":
    main()
