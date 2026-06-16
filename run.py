"""
Seniorly scraper — entry point.

Phases:
  providers   Scrape /providers → community URLs → outputs/providers_communities.csv
  details     Visit each community, save JSON + images → outputs/communities/
  all         Run providers then details (default)

Usage:
    python run.py                              # run everything then push to git
    python run.py providers                    # phase 1 only
    python run.py details                      # phase 2 only
    python run.py all --limit 10 --visible     # first 10, show browser
    python run.py details --workers 3          # 3 parallel workers
    python run.py details --no-images          # skip image download
    python run.py all --no-push                # skip git push at the end
"""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from providers_scraper import run as run_providers
from detail_scraper import run as run_details, DEFAULT_CSV

REPO_DIR = Path(__file__).parent


def git_push():
    print("\n── Pushing outputs to git ────────────────────────────────")
    try:
        # Stage all outputs
        subprocess.run(["git", "add", "outputs/"], cwd=REPO_DIR, check=True)

        # Check if there is anything to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=REPO_DIR,
        )
        if result.returncode == 0:
            print("Git: nothing new to commit.")
            return

        # Commit
        subprocess.run(
            ["git", "commit", "-m", "Add scraped community data and images"],
            cwd=REPO_DIR, check=True,
        )

        # Push
        subprocess.run(["git", "push"], cwd=REPO_DIR, check=True)
        print("Git: pushed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Git push failed: {e}")


def parse_args():
    ap = argparse.ArgumentParser(description="Seniorly scraper")
    ap.add_argument("phase",       nargs="?", default="all",
                    choices=["all", "providers", "details"],
                    help="Which phase to run (default: all)")
    ap.add_argument("--limit",     type=int, default=0,    help="Max items per phase (0=all)")
    ap.add_argument("--workers",   type=int, default=2,    help="Parallel browser workers")
    ap.add_argument("--visible",   action="store_true",    help="Show browser windows")
    ap.add_argument("--no-images", action="store_true",    help="Skip image download")
    ap.add_argument("--no-push",   action="store_true",    help="Skip git push at the end")
    ap.add_argument("--input",     type=Path, default=DEFAULT_CSV,
                    help="Input CSV for details phase (default: outputs/providers_communities.csv)")
    return ap.parse_args()


async def main():
    args = parse_args()
    headless = not args.visible

    if args.phase in ("all", "providers"):
        print("\n── Phase 1: providers → community URLs ──────────────────")
        await run_providers(headless=headless, limit=args.limit, workers=args.workers)

    if args.phase in ("all", "details"):
        print("\n── Phase 2: community detail pages + images ─────────────")
        await run_details(
            input_csv=args.input,
            headless=headless,
            limit=args.limit,
            workers=args.workers,
            download_imgs=not args.no_images,
        )

    if not args.no_push:
        git_push()

    print("\nAll done.")


if __name__ == "__main__":
    asyncio.run(main())
