"""
Champions Area Apartment Comp Scraper
Runs daily via GitHub Actions at noon CST.
Uses Firecrawl API to scrape Houston apartment websites and appends to history.json.

Setup: Add your Firecrawl API key to a .env file in this directory:
    FIRECRAWL_API_KEY=fc-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Or set it as a Windows environment variable.
"""

import json
import os
import re
import sys
from datetime import date, timedelta
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
                    "prop":         entry["id"],
                    "name":         entry["name"],
                    "url":          entry["website"],
                    "availUnknown": entry.get("availUnknown", False),
                })
                seen.add(entry["id"])
    return sites


def build_extract_prompt():
    today = date.today()
    cutoff = today + timedelta(days=2)
    return (
        f"Today's date is {today.strftime('%B %d, %Y')}. "
        "Extract all apartment unit listings with their floorplan name/type, "
        "number of bedrooms, number of bathrooms, square footage, monthly rent "
        "(use null if it says 'call for pricing' or no price is shown), "
        "and availability status. "
        "For the 'count' field: count only units that are VACANT RIGHT NOW. "
        "A unit is currently vacant if its availability text says 'Available Now', "
        f"'Available Immediately', or a specific date on or before {cutoff.strftime('%B %d, %Y')}. "
        "Any unit with a move-in date further in the future is pre-leasing only — "
        "set count=0 for those. Do NOT count future move-in dates as currently available. "
        f"Example: if today is {today.strftime('%B %d')} and a unit says 'Available {(today + timedelta(days=10)).strftime('%B %d')}', count=0. "
        "If it says 'Available Now' or a date within 2 days, count=1. "
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


# ── CONFIDENCE SCORING ─────────────────────────────────────────────────────────
def calculate_confidence(units, avail_unknown=False):
    """
    Returns a confidence % (0-100) reflecting how reliable the availability
    data is for this property.

    90 = per-unit 'Available Now' / specific dates  (high quality)
    70 = mix of per-unit and aggregate counts        (medium quality)
    50 = aggregate counts only ('X Available')       (lower quality)
    25 = property known not to publish availability  (availUnknown flag)
    15 = no units scraped at all                     (scrape failed)
    """
    if avail_unknown:
        return 25

    if not units:
        return 15

    total = len(units)
    avail_now  = 0
    dated      = 0
    aggregate  = 0

    date_pattern = re.compile(r'\d{1,2}/\d{1,2}/\d{2,4}')
    month_pattern = re.compile(
        r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b', re.I
    )
    agg_pattern = re.compile(r'\d+\s+available', re.I)

    for u in units:
        avail_text = str(u.get("avail", "")).lower().strip()
        if "available now" in avail_text or "available immediately" in avail_text:
            avail_now += 1
        elif date_pattern.search(avail_text) or month_pattern.search(avail_text):
            dated += 1
        elif agg_pattern.search(avail_text):
            aggregate += 1

    per_unit_ratio = (avail_now + dated) / total
    agg_ratio      = aggregate / total

    if per_unit_ratio >= 0.7:
        return 90   # Strong per-unit signal
    elif per_unit_ratio >= 0.4:
        return 70   # Decent per-unit signal
    elif per_unit_ratio >= 0.1:
        return 55   # Weak per-unit signal
    elif agg_ratio >= 0.5:
        return 50   # Aggregate counts only
    else:
        return 35   # Little or no availability data


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
    all_units      = []
    confidence_map = {}
    today_str      = str(date.today())
    print(f"[{today_str}] Starting comp scrape — {len(sites)} site(s)...")

    for site in sites:
        print(f"  Scraping {site['name']}...")
        raw_units = []
        try:
            extract_params = {
                "prompt": build_extract_prompt(),
                "schema": EXTRACT_SCHEMA,
            }
            # Handle firecrawl-py v1 (scrape_url) and v2 (scrape)
            if hasattr(app, "scrape_url"):
                try:
                    result = app.scrape_url(
                        site["url"],
                        formats=["extract"],
                        extract=extract_params,
                    )
                except TypeError:
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

            # Parse result (dict = older v1, object = newer)
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
                    "count": int(u["count"]) if u.get("count") is not None else 1,
                })

            print(f"    -> {len(raw_units)} unit(s) extracted")

        except Exception as e:
            print(f"    ERROR scraping {site['name']}: {e}", file=sys.stderr)

        confidence_map[site["prop"]] = calculate_confidence(
            raw_units, avail_unknown=site.get("availUnknown", False)
        )
        print(f"    -> confidence: {confidence_map[site['prop']]}%")

    return all_units, confidence_map


# ── HISTORY UPDATE ─────────────────────────────────────────────────────────────
def update_history(units, confidence_map):
    today = str(date.today())

    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = []

    existing = next((i for i, s in enumerate(history) if s["date"] == today), None)
    entry = {"date": today, "units": units, "confidence": confidence_map}

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
    units, confidence_map = scrape_all()

    if not units:
        print("WARNING: No units scraped. history.json not updated.", file=sys.stderr)
        sys.exit(1)

    print(f"\nTotal units scraped: {len(units)}")
    print(f"Confidence scores:   {confidence_map}")
    update_history(units, confidence_map)
    print("Done.")
