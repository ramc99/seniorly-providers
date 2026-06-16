"""
Stage 1: Extract city list for a given US state from Seniorly.

Two page patterns handled automatically:
  - City-index states (e.g. Arizona): state page lists cities via <a><u>City</u></a>
  - Paginated states (e.g. Alabama): state page shows facilities; extract unique cities
    from facility addresses across all pages.

Usage:
    python stage1_city_list.py --state arizona
    python stage1_city_list.py --state alabama
    python stage1_city_list.py --state arizona --output cities.csv
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.seniorly.com/assisted-living"


def make_state_url(state: str) -> str:
    return f"{BASE_URL}/{state.lower().replace(' ', '-')}"


def get_city_index(page, state: str) -> list[dict]:
    """Extract city links from states that show a city-index page."""
    state_url = make_state_url(state)
    page.goto(state_url, wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1000)

    links = page.eval_on_selector_all(
        "a:has(u)",
        "function(els) { return els.map(function(e) { return {href: e.getAttribute('href'), text: e.innerText.trim()}; }); }",
    )

    cities = []
    pattern = re.compile(rf"/assisted-living/{re.escape(state.lower().replace(' ', '-'))}/[^/]+$")
    for link in links:
        href = link.get("href", "") or ""
        # Normalize to relative path
        if href.startswith("https://www.seniorly.com"):
            href = href[len("https://www.seniorly.com"):]
        if pattern.match(href):
            city_slug = href.rstrip("/").split("/")[-1]
            city_name = link["text"].replace(" Assisted Living", "").strip()
            cities.append({
                "city": city_name,
                "city_slug": city_slug,
                "url": f"https://www.seniorly.com{href}",
            })
    return cities


def get_cities_from_pagination(page, state: str) -> list[dict]:
    """For states without a city index, paginate facilities and collect unique cities."""
    state_url = make_state_url(state)
    seen_cities: dict[str, str] = {}
    page_num = 1

    while True:
        url = state_url if page_num == 1 else f"{state_url}?page-number={page_num}"
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(1000)

        cards = page.query_selector_all("article[data-testid='card']")
        if not cards:
            break

        # Each card is wrapped in an <a> with href /assisted-living/state/city/facility
        links = page.eval_on_selector_all(
            f"a[href*='/assisted-living/{state.lower()}/']",
            "function(els) { return els.map(function(e) { return e.getAttribute('href'); }); }",
        )

        facility_pattern = re.compile(
            rf"^/assisted-living/{re.escape(state.lower())}/([^/]+)/[^/]+$"
        )
        for href in links:
            m = facility_pattern.match(href or "")
            if m:
                city_slug = m.group(1)
                if city_slug not in seen_cities:
                    city_name = city_slug.replace("-", " ").title()
                    city_url = f"{BASE_URL}/{state.lower()}/{city_slug}"
                    seen_cities[city_slug] = city_url

        # Check if next page exists
        next_link = page.query_selector(f"a[href*='page-number={page_num + 1}']")
        if not next_link:
            break
        page_num += 1
        print(f"  Page {page_num} scraped ({len(seen_cities)} cities so far)...")

    return [
        {"city": slug.replace("-", " ").title(), "city_slug": slug, "url": url}
        for slug, url in sorted(seen_cities.items())
    ]


def scrape_city_list(state: str) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        print(f"Checking state page for {state}...")
        state_url = make_state_url(state)
        page.goto(state_url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)

        # Detect page type: city-index vs paginated facilities
        u_links = page.query_selector_all("a:has(u)")
        city_pattern = re.compile(
            rf"https?://www\.seniorly\.com/assisted-living/{re.escape(state.lower().replace(' ', '-'))}/[^/]+$"
            rf"|^/assisted-living/{re.escape(state.lower().replace(' ', '-'))}/[^/]+$"
        )
        city_index_links = [
            l for l in u_links
            if city_pattern.match(l.get_attribute("href") or "")
        ]

        if city_index_links:
            print(f"City-index page detected ({len(city_index_links)} cities). Extracting...")
            cities = get_city_index(page, state)
        else:
            print("Paginated facilities page detected. Extracting cities from all pages...")
            cities = get_cities_from_pagination(page, state)

        browser.close()
        return cities


def save_csv(cities: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["city", "city_slug", "url"])
        writer.writeheader()
        writer.writerows(cities)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1: Extract Seniorly city list by state")
    parser.add_argument("--state", required=True, help="State name (e.g. arizona, alabama, new-york)")
    parser.add_argument("--output", help="Output CSV path (default: <state>_cities.csv)")
    args = parser.parse_args()

    state = args.state.lower().strip()
    output_path = Path(args.output) if args.output else Path(f"{state.replace(' ', '-')}_cities.csv")

    cities = scrape_city_list(state)

    if not cities:
        print(f"No cities found for state: {state}", file=sys.stderr)
        sys.exit(1)

    save_csv(cities, output_path)
    print(f"\nDone. {len(cities)} cities saved to {output_path}")
    for c in cities[:5]:
        print(f"  {c['city']:<25} {c['url']}")
    if len(cities) > 5:
        print(f"  ... and {len(cities) - 5} more")


if __name__ == "__main__":
    main()
