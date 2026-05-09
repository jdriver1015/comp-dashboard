"""
Champions Area Apartment Comp Scraper
Runs daily via GitHub Actions at noon CST.
Uses Playwright (free headless browser) + Google Gemini API (free tier)
instead of Firecrawl — no paid credits needed.

Setup:
  1. Get a free Gemini API key at https://aistudio.google.com/apikey
  2. Add it to a .env file in this directory:
       GEMINI_API_KEY=AIza...
     OR set it as a GitHub Actions secret named GEMINI_API_KEY.
"""

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# ── CONFIG ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).parent
HISTORY_FILE    = SCRIPT_DIR / "data" / "history.json"
PROPERTIES_FILE = SCRIPT_DIR / "data" / "properties.json"

load_dotenv(SCRIPT_DIR / ".env")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")


# ── LOAD SITES ─────────────────────────────────────────────────────────────────
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


# ── PLAYWRIGHT PAGE FETCH ──────────────────────────────────────────────────────
def fetch_page_text(url: str, pw) -> str:
    """
    Render the page with a headless Chromium browser and return
    the visible text content (trimmed to 14k chars for Gemini).
    """
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.new_page()
    try:
        # Use "domcontentloaded" first, then wait — avoids timeout on slow SPAs
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(6_000)   # extra time for lazy-loaded JS

        # Try clicking a "View All" or "See All" button if present
        for selector in [
            'button:has-text("View All")',
            'button:has-text("See All")',
            'a:has-text("View All Floorplans")',
            '[data-tab="availability"]',
        ]:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=1_000):
                    btn.click()
                    page.wait_for_timeout(2_000)
                    break
            except Exception:
                pass

        text = page.inner_text("body")
        return text[:14_000]          # ~3k tokens, well inside Gemini's free limit
    except Exception as e:
        print(f"    Playwright error fetching {url}: {e}", file=sys.stderr)
        return ""
    finally:
        ctx.close()
        browser.close()


# ── GEMINI EXTRACTION ──────────────────────────────────────────────────────────
EXTRACT_PROMPT = """You are extracting apartment floor plan data from the text of an apartment website.

Return ONE JSON object per floor plan TYPE (not per individual unit).
For each floor plan include:
  plan  = floor plan name or code (string)
  br    = bedrooms (integer)
  ba    = bathrooms (integer or float)
  sqft  = square footage (integer)
  rent  = lowest listed monthly rent (integer), or null if "call for pricing"
  avail = the availability text shown on the page (string)
  count = TOTAL number of units available for that plan (integer)

Rules for count:
  - "X Available" directly → count = X
  - Shows 1 featured unit PLUS "X Other Available Units" link → count = 1 + X
  - "Last Available" (no other units) → count = 1
  - "Call for pricing" or no availability shown → count = 0

Return ONLY a valid JSON array — no markdown fences, no explanation.
Example: [{"plan":"A1","br":1,"ba":1,"sqft":750,"rent":1200,"avail":"Available Now","count":3}]

Page text:
"""


GEMINI_MODELS = [
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-2.0-flash-lite",
]


def extract_with_gemini(page_text: str, property_name: str, client) -> list:
    """
    Send page text to Gemini and parse the returned JSON array.
    Tries models in order; retries on 429 with the wait time from the error.
    """
    prompt = f'Property: "{property_name}"\n\n' + EXTRACT_PROMPT + page_text

    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )
                raw = response.text.strip()
                # Strip accidental markdown code fences
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$",          "", raw)
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "units" in data:
                    return data["units"]
                return []

            except Exception as e:
                err = str(e)
                # Rate limited — parse the suggested wait time and retry
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    wait_match = re.search(r"retry in (\d+(?:\.\d+)?)s", err, re.I)
                    wait = float(wait_match.group(1)) + 2 if wait_match else 30
                    # limit: 0 means this model has zero quota — skip it entirely
                    if "limit: 0" in err:
                        print(f"    Model {model} unavailable (quota=0), trying next…")
                        break
                    print(f"    Rate limited on {model}, waiting {wait:.0f}s (attempt {attempt+1}/3)…")
                    time.sleep(wait)
                    continue
                print(f"    Gemini extraction error for {property_name} [{model}]: {e}", file=sys.stderr)
                break  # Non-rate-limit error — try next model

    print(f"    All Gemini models exhausted for {property_name}", file=sys.stderr)
    return []


# ── CONFIDENCE SCORING ─────────────────────────────────────────────────────────
def calculate_confidence(units, avail_unknown=False):
    """
    Returns a confidence % (0-100) reflecting how reliable the availability
    data is for this property.
    90 = per-unit 'Available Now' / specific dates   (high quality)
    70 = mix of per-unit and aggregate counts         (medium quality)
    50 = aggregate counts only ('X Available')        (lower quality)
    25 = property known not to publish availability   (availUnknown flag)
    15 = no units scraped at all                      (scrape failed)
    """
    if avail_unknown:
        return 25
    if not units:
        return 15

    total         = len(units)
    avail_now     = 0
    dated         = 0
    aggregate     = 0
    date_pattern  = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
    month_pattern = re.compile(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", re.I
    )
    agg_pattern   = re.compile(r"\d+\s+available", re.I)

    for u in units:
        t = str(u.get("avail", "")).lower().strip()
        if "available now" in t or "available immediately" in t:
            avail_now += 1
        elif date_pattern.search(t) or month_pattern.search(t):
            dated += 1
        elif agg_pattern.search(t):
            aggregate += 1

    per_unit_ratio = (avail_now + dated) / total
    agg_ratio      = aggregate / total

    if   per_unit_ratio >= 0.7: return 90
    elif per_unit_ratio >= 0.4: return 70
    elif per_unit_ratio >= 0.1: return 55
    elif agg_ratio      >= 0.5: return 50
    else:                       return 35


# ── SCRAPE ALL ─────────────────────────────────────────────────────────────────
def scrape_all():
    if not GEMINI_KEY:
        print("ERROR: No GEMINI_API_KEY found.", file=sys.stderr)
        print("Get a free key at https://aistudio.google.com/apikey", file=sys.stderr)
        print("Then add it to .env as:  GEMINI_API_KEY=AIza...", file=sys.stderr)
        sys.exit(1)

    sites = load_sites()
    if not sites:
        print("ERROR: No sites to scrape. Check data/properties.json.", file=sys.stderr)
        sys.exit(1)

    from google import genai
    client = genai.Client(api_key=GEMINI_KEY)

    from playwright.sync_api import sync_playwright

    all_units      = []
    confidence_map = {}
    today_str      = str(date.today())
    print(f"[{today_str}] Starting comp scrape — {len(sites)} site(s)...")

    with sync_playwright() as pw:
        for site in sites:
            print(f"  Scraping {site['name']}...")
            raw_units = []
            try:
                page_text = fetch_page_text(site["url"], pw)
                if not page_text:
                    print(f"    -> No page content — skipping Gemini call", file=sys.stderr)
                else:
                    raw_units = extract_with_gemini(page_text, site["name"], client)
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

                # Respect Gemini free-tier rate limit (15 req/min)
                time.sleep(5)

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

    history = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)

    entry    = {"date": today, "units": units, "confidence": confidence_map}
    existing = next((i for i, s in enumerate(history) if s["date"] == today), None)

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
