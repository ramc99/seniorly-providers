"""
Seniorly detail scraper — Phase 2.

For each community URL (from providers_communities.csv or any CSV with a 'url' column):
  - Extracts full structured data (name, address, phone, care types, pricing,
    rating, description, amenities, reviews)
  - Downloads all images to outputs/images/<slug>/
  - Saves JSON to outputs/data/<slug>.json

Resume-safe: skips communities whose JSON already exists.

Usage:
    python detail_scraper.py                             # process all from providers_communities.csv
    python detail_scraper.py --input my_urls.csv        # custom input CSV
    python detail_scraper.py --limit 20                 # first 20 only
    python detail_scraper.py --workers 2                # parallel workers
    python detail_scraper.py --visible                  # visible browser
    python detail_scraper.py --no-images                # skip image download
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

OUTPUT_DIR      = Path(__file__).parent / "outputs"
COMMUNITIES_DIR = OUTPUT_DIR / "communities"
DEFAULT_CSV     = OUTPUT_DIR / "providers_communities.csv"
OUT_CSV         = OUTPUT_DIR / "communities_detail.csv"

CSV_FIELDS = [
    "name", "slug", "url", "address", "city", "state", "zip",
    "phone", "email", "website", "care_types", "pricing",
    "rating", "review_count", "verified", "best_of",
    "description", "amenities", "image_count", "lat", "lng",
]

MAX_IMAGES   = 50
IMG_SIZE     = "1920"     # replace thumbnail size with this in cdn URLs
DELAY_MS     = 2000
RESTART_EVERY = 15

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


# ── Helpers ────────────────────────────────────────────────────────────────────

def url_to_slug(url: str) -> str:
    """Convert a community URL to a filesystem-safe slug."""
    path = urlparse(url).path.strip("/")
    return re.sub(r"[^\w-]", "_", path).strip("_")[:200]


def upgrade_img_url(url: str) -> str:
    """Replace small resolution in Seniorly CDN URL with a larger one."""
    return re.sub(r"/(\d+)x/", f"/{IMG_SIZE}x/", url)


async def safe_text(locator) -> str:
    try:
        if await locator.count():
            return (await locator.first.inner_text()).strip()
    except Exception:
        pass
    return ""


# ── __NEXT_DATA__ extraction ───────────────────────────────────────────────────

def _extract_from_next_data(raw_json: str) -> dict:
    """Pull community data from Next.js page props."""
    result = {}
    try:
        nd = json.loads(raw_json)
        props = nd.get("props", {}).get("pageProps", {})

        # Flatten several known prop shapes
        community = (
            props.get("community") or
            props.get("provider") or
            props.get("listing") or
            {}
        )
        if not community and props:
            # Try first value that's a dict with a 'name' key
            for v in props.values():
                if isinstance(v, dict) and v.get("name"):
                    community = v
                    break

        if not community:
            return result

        result["name"]        = community.get("name", "")
        result["slug"]        = community.get("slug", "")
        result["care_types"]  = community.get("careTypes") or community.get("care_types") or []
        result["description"] = community.get("description") or community.get("about") or ""
        result["phone"]       = community.get("phone") or community.get("phoneNumber") or ""
        result["email"]       = community.get("email", "")
        result["website"]     = community.get("website", "")
        result["license"]     = community.get("licenseNumber") or community.get("license") or ""
        result["rating"]      = community.get("seniorlyScore") or community.get("rating") or ""
        result["review_count"] = community.get("reviewCount") or community.get("totalReviews") or 0
        result["verified"]    = community.get("isVerified") or community.get("verified") or False
        result["best_of"]     = community.get("isBestOf") or community.get("bestOf") or False

        # Address
        addr = community.get("address") or {}
        if isinstance(addr, dict):
            result["address"]   = addr.get("street") or addr.get("address1") or ""
            result["city"]      = addr.get("city") or addr.get("cityName") or ""
            result["state"]     = addr.get("state") or addr.get("stateCode") or ""
            result["zip"]       = addr.get("zip") or addr.get("zipCode") or ""
            result["lat"]       = addr.get("latitude") or addr.get("lat") or ""
            result["lng"]       = addr.get("longitude") or addr.get("lng") or ""
        elif isinstance(addr, str):
            result["address"] = addr

        # Pricing
        pricing = community.get("pricing") or community.get("startingPrices") or {}
        if isinstance(pricing, dict):
            result["pricing"] = pricing
        elif isinstance(pricing, (int, float, str)):
            result["pricing"] = {"starting": pricing}

        # Amenities
        amenities = community.get("amenities") or community.get("features") or []
        if isinstance(amenities, list):
            result["amenities"] = [
                (a.get("name") or a.get("label") or a) if isinstance(a, dict) else a
                for a in amenities
            ]

        # Images — list of dicts or strings
        photos = (
            community.get("photos") or
            community.get("images") or
            community.get("media") or
            []
        )
        img_urls = []
        for ph in photos:
            if isinstance(ph, str):
                img_urls.append(ph)
            elif isinstance(ph, dict):
                src = ph.get("url") or ph.get("src") or ph.get("href") or ""
                if src:
                    img_urls.append(src)
        result["image_urls"] = [upgrade_img_url(u) for u in img_urls if "cdn.seniorly.com" in u or "seniorly" in u]

        # Reviews
        reviews = community.get("reviews") or []
        if isinstance(reviews, list):
            result["reviews"] = [
                {
                    "author":  r.get("author") or r.get("authorName") or r.get("name") or "",
                    "rating":  r.get("rating") or r.get("stars") or "",
                    "text":    r.get("text") or r.get("body") or r.get("content") or "",
                    "date":    r.get("date") or r.get("createdAt") or "",
                }
                for r in reviews[:50]
                if isinstance(r, dict)
            ]

    except Exception as e:
        log.debug("__NEXT_DATA__ parse error: %s", e)
    return result


# ── HTML fallback extraction ───────────────────────────────────────────────────

async def _extract_html_fallback(page) -> dict:
    """DOM-based extraction when __NEXT_DATA__ is absent or incomplete."""
    result = {}

    # Name
    result["name"] = await safe_text(page.locator("h1").first)

    # Address — look for address-like text
    for sel in [
        '[itemprop="address"]',
        '[data-testid="address"]',
        '[class*="Address"]',
        '[class*="address"]',
        'address',
    ]:
        t = await safe_text(page.locator(sel).first)
        if t:
            result["address"] = t
            break

    # Phone
    for sel in ['a[href^="tel:"]', '[data-testid="phone"]', '[class*="phone" i]']:
        el = page.locator(sel).first
        if await el.count():
            href = await el.get_attribute("href") or ""
            text = await safe_text(el)
            result["phone"] = re.sub(r"^tel:", "", href).strip() or text
            break

    # Description
    for sel in ['[data-testid="description"]', '[class*="Description"]', 'section p']:
        t = await safe_text(page.locator(sel).first)
        if t and len(t) > 50:
            result["description"] = t[:2000]
            break

    # Rating
    for sel in ['[data-testid*="score"]', '[class*="Score"]', '[class*="rating" i]']:
        t = await safe_text(page.locator(sel).first)
        m = re.search(r"(\d+\.?\d*)", t)
        if m:
            result["rating"] = m.group(1)
            break

    # Pricing — find $X,XXX/mo patterns
    page_text = await page.inner_text("body")
    prices = re.findall(r"\$[\d,]+/mo", page_text)
    if prices:
        result["pricing"] = {"found": list(dict.fromkeys(prices))}

    # Care types
    care_kw = ["Assisted Living", "Memory Care", "Independent Living",
               "Nursing Home", "Continuing Care", "Board and Care"]
    result["care_types"] = [kw for kw in care_kw if kw in page_text]

    # Images — all cdn.seniorly.com URLs visible in page source
    source = await page.content()
    raw_imgs = re.findall(
        r'https://(?:cdn|res)\.seniorly\.com/[^\s"\'<>)]+\.(?:jpg|jpeg|png|webp)',
        source,
        re.IGNORECASE,
    )
    seen = set()
    img_urls = []
    for u in raw_imgs:
        upgraded = upgrade_img_url(u)
        if upgraded not in seen:
            seen.add(upgraded)
            img_urls.append(upgraded)
    result["image_urls"] = img_urls[:MAX_IMAGES]

    return result


# ── Image downloader ───────────────────────────────────────────────────────────

async def download_images(img_urls: list[str], slug: str, client: httpx.AsyncClient) -> list[str]:
    """Download images concurrently. Returns list of saved filenames."""
    dest_dir = COMMUNITIES_DIR / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    async def fetch_one(url: str, idx: int) -> str | None:
        ext = re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I)
        ext = ext.group(1).lower() if ext else "jpg"
        fname = f"{idx:03d}.{ext}"
        fpath = dest_dir / fname
        if fpath.exists():
            return fname
        try:
            resp = await client.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            fpath.write_bytes(resp.content)
            return fname
        except Exception as e:
            log.debug("Image download failed %s: %s", url, e)
            return None

    tasks = [fetch_one(u, i + 1) for i, u in enumerate(img_urls[:MAX_IMAGES])]
    results = await asyncio.gather(*tasks)
    saved = [r for r in results if r]
    log.info("  Downloaded %d/%d images → %s", len(saved), len(img_urls), dest_dir.name)
    return saved


# ── CSV helper ─────────────────────────────────────────────────────────────────

def _write_community_csv(data: dict, slug: str, community_dir: Path, provider_name: str = "") -> dict:
    """Write per-community CSV and return the flat row dict."""
    safe = re.sub(r"[^\w-]", "_", provider_name).strip("_") if provider_name else ""
    fname = f"{safe}_phase2" if safe else ((data.get("slug") or slug.split("_")[-1]).strip("_") or slug)
    csv_path = community_dir / f"{fname}.csv"
    flat = {}
    for f in CSV_FIELDS:
        v = data.get(f, "")
        flat[f] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else (v if v is not None else "")
    with open(csv_path, "w", newline="", encoding="utf-8") as cf:
        w = csv.DictWriter(cf, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerow(flat)
    return flat


# ── Per-community scraper ──────────────────────────────────────────────────────

async def scrape_community(page, url: str, download_imgs: bool, client: httpx.AsyncClient, provider_name: str = "") -> dict:
    slug = url_to_slug(url)
    community_dir = COMMUNITIES_DIR / slug
    json_path = community_dir / "data.json"

    if json_path.exists():
        log.info("  Skip (done): %s", slug[:60])
        return {"skipped": True, "url": url}

    community_dir.mkdir(parents=True, exist_ok=True)

    log.info("  Scraping: %s", url)
    data = {"url": url, "slug": slug}

    try:
        await page.goto(url, wait_until="load", timeout=10_000)
        await page.wait_for_timeout(1000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except (PlaywrightTimeout, PlaywrightError) as e:
        log.warning("  Network error (%s): %s", type(e).__name__, url)
        data["error"] = str(e)[:120]
        flat = _write_community_csv(data, slug, community_dir, provider_name)
        return {**data, "_csv_row": flat}

    # Try __NEXT_DATA__ first
    raw_nd = await page.evaluate(
        "() => { try { return JSON.stringify(window.__NEXT_DATA__); } catch(e) { return null; } }"
    )
    if raw_nd:
        nd_data = _extract_from_next_data(raw_nd)
        if nd_data.get("name"):
            data.update(nd_data)
            log.info("  [__NEXT_DATA__] %s", data.get("name", "")[:60])
        else:
            log.debug("  __NEXT_DATA__ had no name — falling back to HTML")

    # HTML fallback for missing fields
    if not data.get("name") or not data.get("image_urls"):
        html_data = await _extract_html_fallback(page)
        for k, v in html_data.items():
            if not data.get(k):
                data[k] = v
        if html_data.get("image_urls") and not data.get("image_urls"):
            data["image_urls"] = html_data["image_urls"]

    img_urls = data.get("image_urls", [])
    data["image_count"] = len(img_urls)

    # Download images
    if download_imgs and img_urls:
        saved = await download_images(img_urls, slug, client)
        data["images_saved"] = saved
    else:
        data["images_saved"] = []

    # Save JSON inside community folder
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    flat = _write_community_csv(data, slug, community_dir)

    log.info("  Saved: %s  (%d images, %s)",
             data.get("name", slug)[:50], len(img_urls), community_dir.name)

    return {**data, "_csv_row": flat}


# ── Browser helper ─────────────────────────────────────────────────────────────

async def new_page(p, headless: bool):
    browser = await p.chromium.launch(headless=headless, args=BROWSER_ARGS)
    ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=UA)
    page = await ctx.new_page()
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, page


# ── Worker ─────────────────────────────────────────────────────────────────────

async def worker(worker_id: int, queue: asyncio.Queue, headless: bool,
                 download_imgs: bool, client: httpx.AsyncClient,
                 csv_writer, csv_lock: asyncio.Lock, csv_file):
    """Reuses one browser per worker, restarts every RESTART_EVERY communities."""
    done = 0
    browser = page = None

    async with async_playwright() as p:
        while True:
            item = await queue.get()
            if item is None:
                queue.put_nowait(None)
                break

            url           = item["url"]
            provider_name = item.get("provider_name", "")

            # Launch or restart browser
            if browser is None or done % RESTART_EVERY == 0:
                if browser:
                    await browser.close()
                    log.info("  [W%d] Browser restarted after %d communities", worker_id, done)
                browser, page = await new_page(p, headless)

            try:
                result = await scrape_community(page, url, download_imgs, client, provider_name)
                if result.get("_csv_row"):
                    async with csv_lock:
                        csv_writer.writerow(result["_csv_row"])
                        csv_file.flush()
            except Exception as e:
                log.error("Worker %d error on %s: %s", worker_id, url, e)
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                browser = page = None

            done += 1
            await asyncio.sleep(DELAY_MS / 1000)

        if browser:
            await browser.close()

    log.info("Worker %d finished (%d communities)", worker_id, done)


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(input_csv: Path, headless: bool = True, limit: int = 0,
              workers: int = 2, download_imgs: bool = True):
    COMMUNITIES_DIR.mkdir(parents=True, exist_ok=True)

    if not input_csv.exists():
        log.error("Input CSV not found: %s", input_csv)
        sys.exit(1)

    with open(input_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    items = []
    seen = set()
    for row in rows:
        url = row.get("url") or row.get("URL") or row.get("community_url") or ""
        url = url.strip()
        if url and url not in seen:
            seen.add(url)
            items.append({"url": url, "provider_name": row.get("provider_name", "")})

    if limit:
        items = items[:limit]

    # Skip already done
    pending = [it for it in items if not (COMMUNITIES_DIR / url_to_slug(it["url"]) / "data.json").exists()]
    log.info("%d unique URLs, %d pending, %d workers", len(items), len(pending), workers)

    if not pending:
        log.info("All communities already scraped.")
        return

    queue: asyncio.Queue = asyncio.Queue()
    for it in pending:
        queue.put_nowait(it)
    queue.put_nowait(None)   # sentinel

    is_new = not OUT_CSV.exists()
    csv_file   = open(OUT_CSV, "a", newline="", encoding="utf-8")
    csv_writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if is_new:
        csv_writer.writeheader()
    csv_lock = asyncio.Lock()

    async with httpx.AsyncClient(
        headers={"User-Agent": UA},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        await asyncio.gather(*[
            worker(i + 1, queue, headless, download_imgs, client, csv_writer, csv_lock, csv_file)
            for i in range(workers)
        ])

    csv_file.close()
    log.info("CSV → %s", OUT_CSV)

    log.info("Done. Data → %s  Images → %s", COMMUNITIES_DIR, OUTPUT_DIR / "images")


def main():
    ap = argparse.ArgumentParser(description="Scrape Seniorly community detail pages + images")
    ap.add_argument("--input",     type=Path, default=DEFAULT_CSV, help="Input CSV with 'url' column")
    ap.add_argument("--limit",     type=int,  default=0,           help="Max communities (0=all)")
    ap.add_argument("--workers",   type=int,  default=2,           help="Parallel workers")
    ap.add_argument("--visible",   action="store_true",            help="Show browser window")
    ap.add_argument("--no-images", action="store_true",            help="Skip image download")
    args = ap.parse_args()
    asyncio.run(run(
        input_csv=args.input,
        headless=not args.visible,
        limit=args.limit,
        workers=args.workers,
        download_imgs=not args.no_images,
    ))


if __name__ == "__main__":
    main()
