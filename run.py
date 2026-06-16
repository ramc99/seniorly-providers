"""Entry point for Seniorly scraper project."""

import subprocess
import sys


def main():
    print("Seniorly Scraper")
    print("================")
    print("Stage 1 - City list:  python stage1_city_list.py --state <state>")
    print("")
    print("Examples:")
    print("  python stage1_city_list.py --state arizona")
    print("  python stage1_city_list.py --state alabama")
    print("  python stage1_city_list.py --state 'new-york' --output ny_cities.csv")


if __name__ == "__main__":
    main()
