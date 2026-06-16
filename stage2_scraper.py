"""
Stage 2: Scrape all facility listings for every city URL in all_cities.csv.

Resume-safe: tracks completed cities in checkpoint.json.
If stopped (Ctrl+C, crash, etc.), restart the script — it skips already-done
cities and continues from the exact stop point.

Output (per city):
  output/<state>/<city>_<state>.csv          — main paginated listings
  output/<state>/<city>_<state>_nearby.csv   — featured/nearby listings (if any)

Usage:
    python stage2_scraper.py
    python stage2_scraper.py --cities-csv all_cities.csv
    python stage2_scraper.py --output-dir output --checkpoint checkpoint.json
"""

import argparse
import csv
import json
import math
import re
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── Constants ─────────────────────────────────────────────────────────────────

FACILITY_RE  = re.compile(r"https?://www\.seniorly\.com/assisted-living/[^/]+/[^/]+/[^/]+$")
SERVICE_KW   = {"Assisted Living", "Memory Care", "Independent Living",
                "Continuing Care", "Nursing Home", "Board and Care"}
FIELDS       = [
    "city_url", "page_num",
    "name", "address", "city", "state_abbr", "zip",
    "services", "price", "rating", "verified", "best_of", "url",
]

# ── Selenium setup ─────────────────────────────────────────────────────────────

def make_driver(headless: bool = True) -> webdriver.Chrome:
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
    return webdriver.Chrome(options=opts)


def slow_scroll(driver, steps: int = 5, pause: float = 0.5) -> None:
    for i in range(1, steps + 1):
        driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {i/steps})")
        time.sleep(pause)
    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(0.4)


# ── Page parsing ───────────────────────────────────────────────────────────────

def get_total_results(driver) -> int | None:
    try:
        el = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.mt-4.text-center"))
        )
        m = re.search(r"of\s+([\d,]+)\s+results", el.text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def parse_article(article, card_type: str) -> dict:
    data = {"card_type": card_type}

    # URL — first inner <a> with a 4-segment assisted-living path
    data["url"] = ""
    for a in article.find_elements(By.CSS_SELECTOR, "a[href]"):
        href = a.get_attribute("href") or ""
        if FACILITY_RE.match(href):
            data["url"] = href
            break

    full_text = article.text.strip()
    lines     = [l.strip() for l in full_text.splitlines() if l.strip()]

    # Name
    h3s = article.find_elements(By.TAG_NAME, "h3")
    data["name"] = h3s[0].text.strip() if h3s else ""
    name_idx = next((i for i, l in enumerate(lines) if l == data["name"]), -1)

    # Address
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
    data["services"] = next((l for l in lines if any(k in l for k in SERVICE_KW)), "")

    # Price
    pm = re.search(r"From \$([\d,]+)/mo", full_text)
    data["price"] = f"${pm.group(1)}/mo" if pm else ""

    # Rating
    rm = re.search(r"\b(10\.0|[1-9]\.\d)\b", full_text)
    data["rating"] = rm.group(1) if rm else ""

    data["verified"] = "Yes" if "Verified"  in full_text else "No"
    data["best_of"]  = "Yes" if "Best of"   in full_text else "No"

    return data


def extract_page_listings(driver, page_num: int) -> list[dict]:
    slow_scroll(driver)
    seen, listings = set(), []

    for art in driver.find_elements(By.TAG_NAME, "article"):
        cls = art.get_attribute("class") or ""
        if   "grid-rows-[328px_auto]" in cls:
            card_type = "main"
        elif "grid-rows-[200px_auto]" in cls:
            card_type = "extra"
        else:
            continue

        data = parse_article(art, card_type)
        if not data["url"] or data["url"] in seen:
            continue
        seen.add(data["url"])
        data["page_num"] = page_num
        listings.append(data)

    return listings


# ── City scraper ───────────────────────────────────────────────────────────────

def scrape_city(driver, city_url: str) -> list[dict]:
    driver.get(city_url)
    time.sleep(3)

    total = get_total_results(driver)
    if total is None:
        print(f"    No results — skip")
        return []

    total_pages = math.ceil(total / 20)
    print(f"    {total} results → {total_pages} pages")

    all_listings, seen_urls = [], set()

    for page_num in range(1, total_pages + 1):
        if page_num > 1:
            driver.get(f"{city_url}?page-number={page_num}")
            time.sleep(3)

        for l in extract_page_listings(driver, page_num):
            l["city_url"] = city_url
            if l["url"] not in seen_urls:
                seen_urls.add(l["url"])
                all_listings.append(l)

    main_n   = sum(1 for l in all_listings if l["card_type"] == "main")
    nearby_n = sum(1 for l in all_listings if l["card_type"] == "extra")
    print(f"    main={main_n}  nearby={nearby_n}")
    return all_listings


# ── CSV output ─────────────────────────────────────────────────────────────────

def save_city_csvs(listings: list[dict], city_url: str, output_dir: Path) -> None:
    parts     = city_url.rstrip("/").split("/")
    city_slug = parts[-1]
    state_slug = parts[-2]

    state_dir = output_dir / state_slug
    state_dir.mkdir(parents=True, exist_ok=True)

    main_rows   = [l for l in listings if l["card_type"] == "main"]
    nearby_rows = [l for l in listings if l["card_type"] == "extra"]

    def write(rows, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    write(main_rows, state_dir / f"{city_slug}_{state_slug}.csv")
    if nearby_rows:
        write(nearby_rows, state_dir / f"{city_slug}_{state_slug}_nearby.csv")


# ── Checkpoint ─────────────────────────────────────────────────────────────────

def load_checkpoint(path: Path) -> set[str]:
    if path.exists():
        with open(path) as f:
            return set(json.load(f).get("completed", []))
    return set()


def save_checkpoint(path: Path, completed: set[str]) -> None:
    with open(path, "w") as f:
        json.dump({"completed": sorted(completed)}, f, indent=2)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cities-csv",  default="all_cities.csv")
    parser.add_argument("--output-dir",  default="output")
    parser.add_argument("--checkpoint",  default="checkpoint.json")
    parser.add_argument("--headless",    action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()

    cities_csv   = Path(args.cities_csv)
    output_dir   = Path(args.output_dir)
    checkpoint_p = Path(args.checkpoint)

    # Load city list
    with open(cities_csv, newline="", encoding="utf-8") as f:
        all_cities = list(csv.DictReader(f))

    total_cities = len(all_cities)

    # Load checkpoint — skip already-done cities
    completed = load_checkpoint(checkpoint_p)
    remaining = [c for c in all_cities if c["url"] not in completed]

    print(f"Cities total   : {total_cities}")
    print(f"Already done   : {len(completed)}")
    print(f"Remaining      : {len(remaining)}")

    if not remaining:
        print("All cities already scraped.")
        return

    print(f"Driver opens/closes per city (fresh incognito session each time).\n")

    try:
        for idx, city_row in enumerate(remaining, start=1):
            city_url   = city_row["url"]
            city_name  = city_row["city"]
            state_name = city_row["state"]
            done_count = len(completed)

            print(f"[{done_count + idx}/{total_cities}] {state_name} / {city_name}")
            print(f"    {city_url}")

            # Open a fresh driver for every city
            driver = make_driver(headless=args.headless)
            try:
                listings = scrape_city(driver, city_url)
                if listings:
                    save_city_csvs(listings, city_url, output_dir)
                completed.add(city_url)
            except Exception as e:
                print(f"    ERROR: {e} — will retry on next run")
            finally:
                driver.quit()

            # Save checkpoint after every city (whether success or error)
            save_checkpoint(checkpoint_p, completed)

    except KeyboardInterrupt:
        print("\n\nStopped by user. Progress saved to checkpoint.json")
        print(f"Completed: {len(completed)}/{total_cities} cities")
        print("Re-run the script to resume from this point.")

    print(f"\nDone. {len(completed)}/{total_cities} cities scraped.")


if __name__ == "__main__":
    main()
