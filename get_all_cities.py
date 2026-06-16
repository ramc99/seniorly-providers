"""
Scrape every city listed on Seniorly for all 50 US states.
Outputs: all_cities.csv  (state, city, city_slug, url)

Usage:
    python get_all_cities.py
    python get_all_cities.py --states arizona alabama texas   # specific states only
    python get_all_cities.py --workers 4                      # parallel browsers (default 3)
"""

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.seniorly.com/assisted-living"

US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california",
    "colorado", "connecticut", "delaware", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new-hampshire", "new-jersey",
    "new-mexico", "new-york", "north-carolina", "north-dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode-island", "south-carolina",
    "south-dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west-virginia", "wisconsin", "wyoming",
]


def slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").title()


def scrape_state(state: str) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        try:
            cities = _scrape_state_inner(page, state)
        finally:
            browser.close()
    return cities


def _scrape_state_inner(page, state: str) -> list[dict]:
    state_url = f"{BASE_URL}/{state}"

    # Load state page
    page.goto(state_url, wait_until="networkidle")
    page.wait_for_timeout(1200)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(800)

    # City-level URL pattern: /assisted-living/<state>/<city>  (no deeper segments)
    city_url_re = re.compile(
        rf"(https?://www\.seniorly\.com)?/assisted-living/{re.escape(state)}/[^/?#]+$"
    )

    # Collect all city-index links visible on the state page (Arizona / Texas / partial states)
    all_hrefs = page.eval_on_selector_all(
        "a[href]",
        "function(els) { return els.map(function(e) { return {href: e.getAttribute('href'), text: e.innerText.trim()}; }); }",
    )
    city_links = [item for item in all_hrefs if city_url_re.match(item.get("href") or "")]

    # Check whether the state page also paginates facility cards.
    # Some states have a partial city index (e.g. Colorado=1, Massachusetts=6) AND
    # paginated facilities — we need both to get a complete city list.
    has_facility_cards = bool(page.query_selector("article[data-testid='card']"))

    if city_links and not has_facility_cards:
        # Pure city-index state (Arizona, Florida, etc.) — index is complete
        return _extract_city_index(city_links, state)
    elif has_facility_cards:
        # Paginate facilities to collect all city slugs, then merge with any index links
        paginated = _extract_from_pagination(page, state)
        if city_links:
            index_cities = _extract_city_index(city_links, state)
            # Merge: prefer index entry (has clean city name), fill in extras from pagination
            merged = {c["city_slug"]: c for c in paginated}
            for c in index_cities:
                merged[c["city_slug"]] = c  # index name wins over slug-derived name
            return sorted(merged.values(), key=lambda r: r["city"])
        return paginated
    else:
        return []


def _extract_city_index(city_links: list[dict], state: str) -> list[dict]:
    """Build city list from anchor links already collected from the state page."""
    seen: dict[str, dict] = {}
    for item in city_links:
        href = item.get("href") or ""
        if href.startswith("/"):
            href = f"https://www.seniorly.com{href}"
        city_slug = href.rstrip("/").split("/")[-1]
        if city_slug in seen:
            continue
        # Use link text if meaningful, otherwise derive from slug
        raw_text = item.get("text", "").replace(" Assisted Living", "").strip()
        city_name = raw_text if raw_text else city_slug.replace("-", " ").title()
        seen[city_slug] = {
            "state": slug_to_name(state),
            "state_slug": state,
            "city": city_name,
            "city_slug": city_slug,
            "url": href,
        }
    return sorted(seen.values(), key=lambda r: r["city"])


def _extract_from_pagination(page, state: str) -> list[dict]:
    """States where the state page paginates facilities (e.g. Alabama)."""
    state_url = f"{BASE_URL}/{state}"
    facility_re = re.compile(rf"^/assisted-living/{re.escape(state)}/([^/]+)/[^/]+$")
    seen: dict[str, str] = {}
    page_num = 1

    while True:
        # Collect facility hrefs already loaded on current page
        links = page.eval_on_selector_all(
            f"a[href*='/assisted-living/{state}/']",
            "function(els) { return els.map(function(e) { return e.getAttribute('href'); }); }",
        )
        for href in links:
            m = facility_re.match(href or "")
            if m:
                city_slug = m.group(1)
                if city_slug not in seen:
                    seen[city_slug] = f"{BASE_URL}/{state}/{city_slug}"

        # Try to go to next page
        page_num += 1
        next_link = page.query_selector(f"a[href*='page-number={page_num}']")
        if not next_link:
            break
        next_url = f"{state_url}?page-number={page_num}"
        page.goto(next_url, wait_until="networkidle")
        page.wait_for_timeout(800)

    return [
        {
            "state": slug_to_name(state),
            "state_slug": state,
            "city": slug.replace("-", " ").title(),
            "city_slug": slug,
            "url": url,
        }
        for slug, url in sorted(seen.items())
    ]


def scrape_all(states: list[str], workers: int) -> list[dict]:
    results = []
    failed = []

    print(f"Scraping {len(states)} states with {workers} parallel workers...\n")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_state = {executor.submit(scrape_state, s): s for s in states}
        for future in as_completed(future_to_state):
            state = future_to_state[future]
            try:
                cities = future.result()
                results.extend(cities)
                print(f"  ✓ {slug_to_name(state):<20} {len(cities):>3} cities")
            except Exception as exc:
                failed.append(state)
                print(f"  ✗ {slug_to_name(state):<20} ERROR: {exc}", file=sys.stderr)

    if failed:
        print(f"\nFailed states: {', '.join(failed)}", file=sys.stderr)

    # Sort by state then city
    results.sort(key=lambda r: (r["state"], r["city"]))
    return results


def save_csv(rows: list[dict], path: Path) -> None:
    fields = ["state", "state_slug", "city", "city_slug", "url"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape all Seniorly city listings by state")
    parser.add_argument("--states", nargs="+", help="Specific state slugs to scrape (default: all 50)")
    parser.add_argument("--workers", type=int, default=3, help="Parallel browser workers (default: 3)")
    parser.add_argument("--output", default="all_cities.csv", help="Output CSV file")
    args = parser.parse_args()

    states = args.states if args.states else US_STATES
    # Normalize input
    states = [s.lower().replace(" ", "-") for s in states]

    rows = scrape_all(states, workers=args.workers)

    output = Path(args.output)
    save_csv(rows, output)

    print(f"\nTotal: {len(rows)} cities across {len(states)} states → {output}")


if __name__ == "__main__":
    main()
