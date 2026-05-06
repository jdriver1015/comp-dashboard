"""
Champions Area Apartment Comp Scraper
Runs daily via Windows Task Scheduler at 8:00 AM CST.
Uses Firecrawl API to scrape 4 Houston apartment websites and appends to history.json.

Setup: Add your Firecrawl API key to a .env file in this directory:
    FIRECRAWL_API_KEY=fc-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Or set it as a Windows environment variable.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from firecrawl import FirecrawlApp

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
HISTORY_FILE = SCRIPT_DIR / "data" / "history.json"

load_dotenv(SCRIPT_DIR / ".env")
API_KEY = os.getenv("FIRECRAWL_API_KEY", "")

PROPERTIES_FILE = SCRIPT_DIR / "data" / "properties.json"


def load_sites():
    """Build the scrape list from data/properties.json (subjects + all comps)."""
    if not PROPERTIES_FILE.exists():
        print(f"WARNING: {PROPERTIES_FILE} not found — no sites to scrape.", file=sys.stderr)
        return []

    with open(PROPERTIES_FILE, encoding="utf-8") as f:
        props = json.load(f)

    sites, seen = [], set()
    for p in props:
        for entry in [p] + p.get("comps", []):
            if entry.get("id") not in seen and entry.get("website"):
                sites.append({
                    "prop": entry["id"],
                    "name": entry["name"],
                    "url":  entry["website"],
                })
                seen.add(entry["id"])
    return sites

EXTRACT_PROMPT = (
    "Extract all apartment unit listings with their floorplan name/type, "
    "number of bedrooms, number of bathrooms, square footage, monthly rent "
    "(use null if it says 'call for pricing' or no price is shown), "
    "and availability status. "
    "For the 'count' field: set it to the number of units of that plan that are "
    "CURRENTLY available (i.e. 'Available Now' or move-in date within 2 days). "
    "Units with a future move-in date beyond 2 days should have count=0 — "
    "they are pre-leasing, not currently vacant. "
    "Return as a JSON array of objects with fields: "
    "plan (string), br (integer), ba (integer or float), sqft (integer), "
    "rent (integer or null), avail (string), count (integer)."
)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "plan":  {"type": "string"},
                    "br":    {"type": "integer"},
                    "ba":    {"type": ["integer", "number"]},
                    "sqft":  {"type": "integer"},
                    "rent":  {"type": ["integer", "null"]},
                    "avail": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["plan", "br", "sqft"],
            },
        }
    },
}


# ── SCRAPE ─────────────────────────────────────────────────────────────────────
def scrape_all():
    if not API_KEY:
        print("ERROR: No FIRECRAWL_API_KEY found.", file=sys.stderr)
        print("Create a .env file in the comp-dashboard folder with:", file=sys.stderr)
        print("  FIRECRAWL_API_KEY=fc-your-key-here", file=sys.stderr)
        sys.exit(1)

    sites = load_sites()
    if not sites:
        print("ERROR: No sites to scrape. Add properties to data/properties.json.", file=sys.stderr)
        sys.exit(1)

    app = FirecrawlApp(api_key=API_KEY)
    all_units = []
    today_str = str(date.today())
    print(f"[{today_str}] Starting comp scrape — {len(sites)} site(s)...")

    for site in sites:
        print(f"  Scraping {site['name']}...")
        try:
            extract_params = {
                "prompt": EXTRACT_PROMPT,
                "schema": EXTRACT_SCHEMA,
            }
            # Call scrape — handle v1 old-style (params dict),
            # v1 new-style (keyword args), and v2 (app.scrape)
            if hasattr(app, "scrape_url"):
                try:
                    # v1 newer: keyword args
                    result = app.scrape_url(
                        site["url"],
                        formats=["extract"],
                        extract=extract_params,
                    )
                except TypeError:
                    # v1 older: params dict
                    result = app.scrape_url(
                        site["url"],
                        params={"formats": ["extract"], "extract": extract_params},
                    )
            else:
                result = app.scrape(
                    site["url"],
                    formats=["extract"],
                    extract=extract_params,
                )

            # Result may be an object (newer) or a plain dict (older v1)
            raw_units = []
            if isinstance(result, dict):
                data = result.get("extract") or result.get("llm_extraction")
            elif hasattr(result, "extract"):
                data = result.extract
            else:
                data = None

            if data:
                if isinstance(data, dict) and "units" in data:
                    raw_units = data["units"]
                elif isinstance(data, list):
                    raw_units = data

            for u in raw_units:
                all_units.append({
                    "prop":  site["prop"],
                    "plan":  str(u.get("plan", "")).strip() or "N/A",
                    "br":    int(u.get("br") or 0),
                    "ba":    u.get("ba") or 1,
                    "sqft":  int(u.get("sqft") or 0),
                    "rent":  int(u["rent"]) if u.get("rent") else None,
                    "avail": str(u.get("avail", "")).strip() or "Unknown",
                    "count": int(u.get("count") or 1),
                })

            print(f"    -> {len(raw_units)} unit(s) extracted")

        except Exception as e:
            print(f"    ERROR scraping {site['name']}: {e}", file=sys.stderr)

    return all_units


# ── HISTORY UPDATE ─────────────────────────────────────────────────────────────
def update_history(units):
    today = str(date.today())

    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = []

    existing = next((i for i, s in enumerate(history) if s["date"] == today), None)
    entry = {"date": today, "units": units}

    if existing is not None:
        history[existing] = entry
        print(f"  Updated existing entry for {today}")
    else:
        history.append(entry)
        print(f"  Appended new entry for {today} (total snapshots: {len(history)})")

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    print(f"  Saved -> {HISTORY_FILE}")


# ── ENTRY POINT ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    units = scrape_all()

    if not units:
        print("WARNING: No units scraped. history.json not updated.", file=sys.stderr)
        sys.exit(1)

    print(f"\nTotal units scraped: {len(units)}")
    update_history(units)
    print("Done.")
