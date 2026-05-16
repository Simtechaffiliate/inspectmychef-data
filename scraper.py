"""
Mecklenburg County (NC) restaurant-inspection scraper — Playwright edition.

Why Playwright instead of requests+BeautifulSoup: the CDPEHS portal is an
ASP.NET WebForms application. URL parameters are ignored, filters require a
real form submission, and pagination is driven by __doPostBack JavaScript
that depends on __VIEWSTATE / __EVENTVALIDATION tokens. Rather than fight
that machinery from a static HTTP client, we drive a real Chromium browser
the same way a person would:

  1. Open the table page
  2. Pick "1 - Restaurant" in the Establishment Type dropdown
  3. Click Search
  4. Scrape each results page, click Next, repeat
  5. Geocode unique addresses via Nominatim
  6. Write JSON in the mobile app's Restaurant[] shape

Setup (one-time):

    pip install -r requirements.txt
    playwright install chromium       # ~150 MB browser download

Run:

    python scraper.py

Tweaks via env vars:

    HEADLESS=0          # show the browser window (useful for debugging)
    MAX_PAGES=999       # safety cap on pagination
    SKIP_GEOCODE=1      # skip the slow geocoding phase
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from playwright.sync_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ──────────────────────────────────────────────────────────────────────────────
# Config

CDPEHS_URL = (
    'https://public.cdpehs.com/NCENVPBL/ESTABLISHMENT/'
    'ShowESTABLISHMENTTablePage.aspx?ESTTST_CTY=60'
)

# Selectors — verified live against the CDPEHS portal 2026-05-15 via Chrome
# DevTools. The grid id is fixed; the form/pagination controls live under the
# ctl00$... naming hierarchy.
SEL_EST_TYPE_DROPDOWN = 'select[id*="EST_TYPE_IDFilter"]'
SEL_SEARCH_BUTTON = '#ctl00_PageContent_FilterButton__Button'
SEL_RESULTS_GRID = '#VW_PUBLIC_ESTINSPTableControlGrid'
# Pagination controls (image buttons + text inputs):
SEL_PAGE_SIZE_INPUT = '#ctl00_PageContent_Pagination__PageSize'
SEL_PAGE_SIZE_BUTTON = '#ctl00_PageContent_Pagination__PageSizeButton'
SEL_CURRENT_PAGE_INPUT = '#ctl00_PageContent_Pagination__CurrentPage'
SEL_NEXT_PAGE = '#ctl00_PageContent_Pagination__NextPage'
SEL_TOTAL_PAGES = '#ctl00_PageContent_Pagination__TotalPages'

# Page size we ask the portal to render. 200 is the largest size the portal
# accepts without rejecting, and shrinks 54k+ rows from 5455 pages to 273.
PAGE_SIZE = int(os.environ.get('PAGE_SIZE', '200'))

HEADLESS = os.environ.get('HEADLESS', '1') != '0'
MAX_PAGES = int(os.environ.get('MAX_PAGES', '500'))
SKIP_GEOCODE = os.environ.get('SKIP_GEOCODE', '0') == '1'
# If a page contributes zero new restaurants, we've scanned all unique
# establishments (subsequent pages are just older inspection records for
# restaurants we've already captured). Quit after this many "all dupes" pages.
EARLY_STOP_DUPE_PAGES = int(os.environ.get('EARLY_STOP_DUPE_PAGES', '3'))

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output')
OUTPUT_JSON = os.path.join(OUTPUT_DIR, 'charlotte-mecklenburg.json')
GEOCODE_CACHE = os.path.join(OUTPUT_DIR, 'geocode-cache.json')

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 '
    'InspectMyChef-Scraper/2.0'
)

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
NOMINATIM_RATE_LIMIT_S = 1.05

# ──────────────────────────────────────────────────────────────────────────────
# Playwright-driven scraping


def setup_filters(page: Page) -> None:
    """Filter to Restaurants and increase the page size to PAGE_SIZE rows.

    Two postbacks happen here:
      1. EST_TYPE filter + Search → drops from ~125k all-establishments down
         to ~54.5k restaurant inspections.
      2. Set Pagination__PageSize to PAGE_SIZE and click PageSizeButton →
         drops from 5455 pages (10/page) to ~273 pages (200/page).
    """
    print('  setting Establishment Type = "1 - Restaurant"...')
    page.wait_for_selector(SEL_EST_TYPE_DROPDOWN, timeout=30000)
    page.select_option(SEL_EST_TYPE_DROPDOWN, '1')
    print('  clicking Search...')
    page.click(SEL_SEARCH_BUTTON)
    page.wait_for_selector(SEL_RESULTS_GRID, timeout=30000)
    page.wait_for_timeout(1500)

    print(f'  setting page size to {PAGE_SIZE}...')
    page.wait_for_selector(SEL_PAGE_SIZE_INPUT, timeout=30000)
    page.fill(SEL_PAGE_SIZE_INPUT, str(PAGE_SIZE))
    page.click(SEL_PAGE_SIZE_BUTTON)
    # Wait for the grid to actually rerender with the bigger page. We poll
    # the row count until it reflects PAGE_SIZE+1 (header) or until the
    # total-pages span shows a number that makes sense for the new size.
    for _ in range(60):
        page.wait_for_timeout(500)
        row_count = page.evaluate(
            "() => document.getElementById('VW_PUBLIC_ESTINSPTableControlGrid')?.rows.length || 0"
        )
        if row_count > 11:  # 11 = header + 10 default rows
            break
    total_pages = page.evaluate(
        "() => document.querySelector('#ctl00_PageContent_Pagination__TotalPages')?.innerText || ''"
    )
    print(f'  grid now shows {row_count - 1} rows; total pages: {total_pages}')


def parse_current_page(page: Page) -> list[dict]:
    """Pull all restaurant rows from the currently displayed results grid."""
    # We extract row data in-browser so we can preserve newlines in the address.
    rows = page.evaluate(
        """
        () => {
          const grid = document.getElementById('VW_PUBLIC_ESTINSPTableControlGrid');
          if (!grid) return [];
          const out = [];
          for (const tr of grid.rows) {
            const cells = tr.cells;
            if (cells.length < 9) continue;
            // Columns: hidden, date, name, address (multi-line),
            // state-id, est-type, score, grade, inspector, hidden
            const text = (c) => (c?.innerText || '').trim();
            out.push({
              date: text(cells[1]),
              name: text(cells[2]),
              address_raw: text(cells[3]),
              state_id: text(cells[4]),
              est_type: text(cells[5]),
              score: text(cells[6]),
              grade: text(cells[7]),
            });
          }
          return out;
        }
        """
    )
    parsed: list[dict] = []
    for r in rows:
        # Skip the header row that has the column titles.
        if r['date'].lower() == 'inspection date':
            continue
        if not r['state_id']:
            continue
        # Address: "1115 N BREVARD ST STALL 17\nCHARLOTTE, NC 28206"
        addr_lines = [ln.strip() for ln in r['address_raw'].splitlines() if ln.strip()]
        street = addr_lines[0] if addr_lines else ''
        city, state, zip_code = '', '', ''
        if len(addr_lines) > 1:
            m = re.match(r'(.+?),\s*([A-Z]{2})\s+(\d{5})', addr_lines[1])
            if m:
                city = m.group(1).strip()
                state = m.group(2)
                zip_code = m.group(3)

        try:
            score: Optional[float] = float(r['score'])
        except (TypeError, ValueError):
            score = None

        iso_date: Optional[str]
        try:
            iso_date = datetime.strptime(r['date'], '%m/%d/%Y').date().isoformat()
        except (TypeError, ValueError):
            iso_date = None

        grade = r['grade'] if r['grade'] and r['grade'] != 'N/A' else None
        parsed.append({
            'state_id': r['state_id'],
            'name': r['name'],
            'address_street': street,
            'city': city.title() if city else 'Charlotte',
            'state': state or 'NC',
            'zip': zip_code,
            'establishment_type': r['est_type'],
            'score': score,
            'grade': grade,
            'inspection_date': iso_date,
        })
    return parsed


def go_to_next_page(page: Page) -> bool:
    """Click the Next pagination image button. Returns False at last page.

    Detects "this is the last page" two ways:
      1. The next button isn't visible/enabled.
      2. CurrentPage value doesn't advance after a generous wait window.
    """
    next_btn = page.locator(SEL_NEXT_PAGE).first
    try:
        next_btn.wait_for(state='visible', timeout=2000)
    except PlaywrightTimeoutError:
        return False
    # Snapshot before/after state to confirm the grid actually changed. We
    # use CurrentPage value AND first-row state_id — either changing is proof
    # of advancement.
    before = page.evaluate(
        """
        () => {
          const cp = document.querySelector('#ctl00_PageContent_Pagination__CurrentPage');
          const grid = document.getElementById('VW_PUBLIC_ESTINSPTableControlGrid');
          const firstId = grid && grid.rows.length > 1 && grid.rows[1].cells.length > 4
            ? (grid.rows[1].cells[4].innerText || '').trim() : null;
          return { page: cp ? cp.value : null, firstId };
        }
        """
    )
    # no_wait_after=True: don't let Playwright block waiting for the
    # post-click navigation to settle. ASP.NET postbacks on CDPEHS can
    # take 30+ seconds (the default Playwright timeout) and we have our
    # own polling loop below that watches the grid for the update.
    next_btn.click(no_wait_after=True)
    for _ in range(60):
        page.wait_for_timeout(500)
        after = page.evaluate(
            """
            () => {
              const cp = document.querySelector('#ctl00_PageContent_Pagination__CurrentPage');
              const grid = document.getElementById('VW_PUBLIC_ESTINSPTableControlGrid');
              const firstId = grid && grid.rows.length > 1 && grid.rows[1].cells.length > 4
                ? (grid.rows[1].cells[4].innerText || '').trim() : null;
              return { page: cp ? cp.value : null, firstId };
            }
            """
        )
        if after['page'] and after['page'] != before['page']:
            return True
        if after['firstId'] and after['firstId'] != before['firstId']:
            return True
    return False


def scrape_all(browser: Browser) -> list[dict]:
    context = browser.new_context(user_agent=USER_AGENT)
    page = context.new_page()
    print(f'Opening {CDPEHS_URL}')
    page.goto(CDPEHS_URL, wait_until='domcontentloaded', timeout=60000)
    setup_filters(page)

    all_rows: list[dict] = []
    seen_state_ids: set[str] = set()
    page_num = 1
    consecutive_dupe_pages = 0
    while True:
        rows = parse_current_page(page)
        new_rows = [r for r in rows if r['state_id'] not in seen_state_ids]
        for r in new_rows:
            seen_state_ids.add(r['state_id'])
        all_rows.extend(new_rows)
        print(
            f'  page {page_num}: parsed {len(rows)} rows '
            f'({len(new_rows)} new; unique total: {len(all_rows)})'
        )
        # Early-stop heuristic: results are ordered by inspection date desc,
        # so once we stop seeing new state IDs we've covered every active
        # establishment.
        if rows and not new_rows:
            consecutive_dupe_pages += 1
            if consecutive_dupe_pages >= EARLY_STOP_DUPE_PAGES:
                print(
                    f'  {consecutive_dupe_pages} consecutive pages with no '
                    'new establishments — stopping early.'
                )
                break
        else:
            consecutive_dupe_pages = 0

        if page_num >= MAX_PAGES:
            print(f'  hit MAX_PAGES safety cap ({MAX_PAGES}).')
            break
        if not go_to_next_page(page):
            print('  no further pages.')
            break
        page_num += 1
    context.close()
    return all_rows


# ──────────────────────────────────────────────────────────────────────────────
# Geocoding


def load_geocode_cache() -> dict[str, dict]:
    if not os.path.exists(GEOCODE_CACHE):
        return {}
    with open(GEOCODE_CACHE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_geocode_cache(cache: dict[str, dict]) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(GEOCODE_CACHE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)


def geocode(
    session: requests.Session,
    address: str,
    cache: dict[str, dict],
) -> Optional[tuple[float, float]]:
    if address in cache:
        c = cache[address]
        if c.get('lat') is not None and c.get('lng') is not None:
            return c['lat'], c['lng']
        return None
    try:
        time.sleep(NOMINATIM_RATE_LIMIT_S)
        r = session.get(
            NOMINATIM_URL,
            params={'q': address, 'format': 'json', 'limit': 1},
            headers={'User-Agent': USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        results = r.json()
        if results:
            lat = float(results[0]['lat'])
            lng = float(results[0]['lon'])
            cache[address] = {'lat': lat, 'lng': lng}
            return lat, lng
    except Exception as e:
        print(f'  geocode error for "{address}": {e}', file=sys.stderr)
    cache[address] = {'lat': None, 'lng': None}
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrate


def dedupe_to_latest(rows: list[dict]) -> list[dict]:
    """One entry per state_id, keep the most recent inspection."""
    by_id: dict[str, dict] = {}
    for r in rows:
        sid = r['state_id']
        existing = by_id.get(sid)
        if existing is None:
            by_id[sid] = r
            continue
        a = r['inspection_date'] or ''
        b = existing['inspection_date'] or ''
        if a > b:
            by_id[sid] = r
    return list(by_id.values())


def to_restaurant_shape(
    rows: list[dict],
    session: Optional[requests.Session],
    cache: dict[str, dict],
) -> list[dict]:
    out: list[dict] = []
    geocoded_count = 0
    for idx, r in enumerate(rows, start=1):
        full_address = (
            f'{r["address_street"]}, {r["city"]}, {r["state"]} {r["zip"]}'
        ).strip(', ').strip()

        latlng: Optional[tuple[float, float]] = None
        if session is not None and full_address:
            latlng = geocode(session, full_address, cache)
            if latlng:
                geocoded_count += 1
            if idx % 25 == 0:
                print(
                    f'  geocoded {idx}/{len(rows)} (hits: {geocoded_count})'
                )
                save_geocode_cache(cache)

        out.append({
            'id': f'us-mck-{r["state_id"]}',
            'name': r['name'],
            'address': full_address or '(no address)',
            'city': r['city'] or 'Charlotte',
            'country': 'US',
            'region': 'Charlotte',
            'cuisine': r['establishment_type'] or None,
            'latestInspection': {
                'inspectionDate': r['inspection_date'] or '1970-01-01',
                'score': r['score'],
                'grade': r['grade'],
                'gradeDate': r['inspection_date'],
                'violations': [],
            },
            'latitude': latlng[0] if latlng else None,
            'longitude': latlng[1] if latlng else None,
            'officialUrl': (
                'https://public.cdpehs.com/NCENVPBL/ESTABLISHMENT/'
                'ShowESTABLISHMENTTablePage.aspx'
                f'?ESTTST_CTY=60&kw=&sval='
                f'{r["name"].split()[0] if r["name"] else ""}'
                '&est_type=1'
            ),
        })
    return out


def main() -> int:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cache = load_geocode_cache()
    print(f'Loaded {len(cache)} cached geocodes.')

    print(f'Launching Chromium (headless={HEADLESS})...')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            raw_rows = scrape_all(browser)
        finally:
            browser.close()

    print(f'Total inspection rows: {len(raw_rows)}')
    unique = dedupe_to_latest(raw_rows)
    print(f'Unique establishments after dedupe: {len(unique)}')

    if SKIP_GEOCODE:
        print('SKIP_GEOCODE=1 — writing JSON without coordinates.')
        restaurants = to_restaurant_shape(unique, None, cache)
    else:
        print(
            f'Geocoding {len(unique)} addresses via Nominatim '
            '(~1 sec each; cache used where possible)...'
        )
        geo_session = requests.Session()
        geo_session.headers.update({'User-Agent': USER_AGENT})
        restaurants = to_restaurant_shape(unique, geo_session, cache)
        save_geocode_cache(cache)

    bundle = {
        'fetchedAt': datetime.now(timezone.utc).isoformat(),
        'source': 'public.cdpehs.com (NC state Environmental Health)',
        'jurisdiction': 'us-nc-mecklenburg',
        'description': (
            'Restaurant inspections from Mecklenburg County, NC '
            '(Charlotte metro). Scraped from the NC state Environmental '
            'Health Public portal via Playwright.'
        ),
        'count': len(restaurants),
        'restaurants': restaurants,
    }
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(bundle, f, indent=2)
    print(f'\nWrote {OUTPUT_JSON} ({len(restaurants)} restaurants)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
