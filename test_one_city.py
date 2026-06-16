"""
Test scraper: one city URL, all pages, all listing details.
Uses Selenium incognito/headless. Driver stays open across all pages.

Card types per page:
  - Main cards   (grid-rows-[328px_auto]): the paginated 20 listings
  - Extra cards  (grid-rows-[200px_auto]): featured/nearby listings shown outside the page
"""

import csv
import math
import re
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

TEST_URL = "https://www.seniorly.com/assisted-living/arizona/phoenix"
OUTPUT_CSV = "test_phoenix.csv"

FACILITY_RE = re.compile(r"https?://www\.seniorly\.com/assisted-living/[^/]+/[^/]+/[^/]+$")
SERVICE_KW = {"Assisted Living", "Memory Care", "Independent Living",
               "Continuing Care", "Nursing Home", "Board and Care"}


def make_driver(headless=True):
    opts = Options()
    opts.add_argument("--incognito")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1400,900")
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)
    return driver


def slow_scroll(driver, steps=5, pause=0.5):
    for i in range(1, steps + 1):
        driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {i/steps})")
        time.sleep(pause)
    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(0.5)


def get_total_results(driver):
    """Parse '1 - 20 of 67 results' from the bottom counter div."""
    try:
        el = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.mt-4.text-center"))
        )
        text = el.text.strip()
        m = re.search(r"of\s+([\d,]+)\s+results", text)
        if m:
            total = int(m.group(1).replace(",", ""))
            print(f"  Results: {text}  → {total} total")
            return total
    except Exception as e:
        print(f"  Results count not found: {e}")
    return None


def parse_article(article, card_type, driver):
    """Extract all fields from a single article element."""
    data = {"card_type": card_type}

    # Facility URL — first <a> inside with a 4-segment assisted-living path
    inner_links = article.find_elements(By.CSS_SELECTOR, "a[href]")
    data["url"] = ""
    for a in inner_links:
        href = a.get_attribute("href") or ""
        if FACILITY_RE.match(href):
            data["url"] = href
            break

    full_text = article.text.strip()
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]

    # Name — h3 text
    h3s = article.find_elements(By.TAG_NAME, "h3")
    data["name"] = h3s[0].text.strip() if h3s else ""

    name_idx = next((i for i, l in enumerate(lines) if l == data["name"]), -1)

    # Address — line after name matching "Street, City, ST 00000"
    data.update({"address": "", "city": "", "state_abbr": "", "zip": ""})
    for line in lines[name_idx + 1:]:
        m = re.match(r"^(.+),\s+([^,]+),\s+([A-Z]{2})\s+(\d{5})$", line)
        if m:
            data["address"]    = m.group(1).strip()
            data["city"]       = m.group(2).strip()
            data["state_abbr"] = m.group(3).strip()
            data["zip"]        = m.group(4).strip()
            break

    # Services
    data["services"] = ""
    for line in lines:
        if any(kw in line for kw in SERVICE_KW):
            data["services"] = line
            break

    # Price
    pm = re.search(r"From \$([\d,]+)/mo", full_text)
    data["price"] = f"${pm.group(1)}/mo" if pm else ""

    # Rating — standalone decimal like 9.8
    rm = re.search(r"\b(10\.0|[1-9]\.\d)\b", full_text)
    data["rating"] = rm.group(1) if rm else ""

    data["verified"] = "Yes" if "Verified" in full_text else "No"
    data["best_of"]  = "Yes" if "Best of" in full_text else "No"

    return data


def extract_page_listings(driver, page_num):
    """Extract main + extra listings from the currently loaded page."""
    slow_scroll(driver)

    seen_urls = set()
    listings = []

    articles = driver.find_elements(By.TAG_NAME, "article")

    for art in articles:
        cls = art.get_attribute("class") or ""
        if "grid-rows-[328px_auto]" in cls:
            card_type = "main"
        elif "grid-rows-[200px_auto]" in cls:
            card_type = "extra"
        else:
            continue  # skip unrecognized article types

        data = parse_article(art, card_type, driver)

        # Skip if no URL resolved (some extra cards link to other pages)
        if not data["url"]:
            continue
        if data["url"] in seen_urls:
            continue
        seen_urls.add(data["url"])

        data["page_num"] = page_num
        listings.append(data)

    main_count  = sum(1 for l in listings if l["card_type"] == "main")
    extra_count = sum(1 for l in listings if l["card_type"] == "extra")
    print(f"  Main: {main_count}  Extra: {extra_count}  Total: {len(listings)}")
    return listings


def scrape_city(driver, city_url):
    print(f"\n{'='*70}")
    print(f"URL: {city_url}")

    driver.get(city_url)
    time.sleep(3)

    total = get_total_results(driver)
    if total is None:
        print("  Skipping — no results count")
        return []

    total_pages = math.ceil(total / 20)
    print(f"  Pages: {total_pages}")

    all_listings = []
    page_seen_urls = set()

    for page_num in range(1, total_pages + 1):
        print(f"\n  --- Page {page_num}/{total_pages} ---")
        if page_num > 1:
            driver.get(f"{city_url}?page-number={page_num}")
            time.sleep(3)

        page_listings = extract_page_listings(driver, page_num)
        for l in page_listings:
            l["city_url"] = city_url
            if l["url"] not in page_seen_urls:
                page_seen_urls.add(l["url"])
                all_listings.append(l)
            else:
                print(f"    [dup] {l['name']}")

        # Show first 3
        for l in page_listings[:3]:
            print(f"    [{l['card_type']}] {l['name'][:40]:<40} | {l['city']:<15} | {l['price']:<10} | {l['rating']}")

    print(f"\n  Total unique listings: {len(all_listings)}")
    return all_listings


FIELDS = [
    "city_url", "page_num",
    "name", "address", "city", "state_abbr", "zip",
    "services", "price", "rating", "verified", "best_of", "url"
]


def city_state_slug(city_url: str) -> tuple[str, str]:
    """Extract city and state slugs from a city URL.
    e.g. https://www.seniorly.com/assisted-living/arizona/phoenix → ('phoenix', 'arizona')
    """
    parts = city_url.rstrip("/").split("/")
    # parts: ['https:', '', 'www.seniorly.com', 'assisted-living', 'arizona', 'phoenix']
    city  = parts[-1]
    state = parts[-2]
    return city, state


def save_split_csvs(listings: list[dict], city_url: str, output_dir: Path) -> None:
    """Save main listings and nearby listings to separate CSVs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    city, state = city_state_slug(city_url)

    main_listings   = [l for l in listings if l["card_type"] == "main"]
    nearby_listings = [l for l in listings if l["card_type"] == "extra"]

    # Main file: <city>_<state>.csv
    main_path = output_dir / f"{city}_{state}.csv"
    with open(main_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(main_listings)
    print(f"  Main     → {main_path}  ({len(main_listings)} rows)")

    # Nearby file: <city>_<state>_nearby.csv  (only if there are extras)
    if nearby_listings:
        nearby_path = output_dir / f"{city}_{state}_nearby.csv"
        with open(nearby_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(nearby_listings)
        print(f"  Nearby   → {nearby_path}  ({len(nearby_listings)} rows)")
    else:
        print(f"  Nearby   → none")


def main():
    print("Starting Selenium (headless incognito)...")
    driver = make_driver(headless=True)
    try:
        listings = scrape_city(driver, TEST_URL)
        save_split_csvs(listings, TEST_URL, Path("output"))
        print("\nSample main (first 3):")
        for l in [x for x in listings if x["card_type"] == "main"][:3]:
            print(f"  {l['name']} | {l['city']}, {l['state_abbr']} | {l['price']} | {l['rating']}")
        print("\nSample nearby (first 3):")
        for l in [x for x in listings if x["card_type"] == "extra"][:3]:
            print(f"  {l['name']} | {l['city']}, {l['state_abbr']} | {l['price']} | {l['rating']}")
    finally:
        driver.quit()
        print("\nDriver closed.")


if __name__ == "__main__":
    main()
