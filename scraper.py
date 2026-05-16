"""
Mecklenburg County (NC) restaurant-inspection scraper — Playwright edition.

CI-safe: poll loops wrap page.evaluate() in try/except to survive the
"Execution context destroyed" exception raised when ASP.NET postback
navigations land mid-poll. Streets are title-cased to match the geocode
cache key format (cache was built from earlier Chrome scrapes that did
the same). SKIP_GEOCODE=1 makes scraper.py write JSON without coordinates
so the separate geocode_only.py step can fill them in with maximum cache
reuse.

Env vars: HEADLESS, MAX_PAGES, SKIP_GEOCODE, PAGE_SIZE, EARLY_STOP_DUPE_PAGES.
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

CDPEHS_URL = (
    'https://public.cdpehs.com/NCENVPBL/ESTABLISHMENT/'
    'ShowESTABLISHMENTTablePage.aspx?ESTTST_CTY=60'
)

SEL_EST_TYPE_DROPDOWN = 'select[id*="EST_TYPE_IDFilter"]'
SEL_SEARCH_BUTTON = '#ctl00_PageContent_FilterButton__Button'
SEL_RESULTS_GRID = '#VW_PUBLIC_ESTINSPTableControlGrid'
SEL_PAGE_SIZE_INPUT = '#ctl00_PageContent_Pagination__PageSize'
SEL_PAGE_SIZE_BUTTON = '#ctl00_PageContent_Pagination__PageSizeButton'
SEL_NEXT_PAGE = '#ctl00_PageContent_Pagination__NextPage'

PAGE_SIZE = int(os.environ.get('PAGE_SIZE', '200'))
HEADLESS = os.environ.get('HEADLESS', '1') != '0'
MAX_PAGES = int(os.environ.get('MAX_PAGES', '500'))
SKIP_GEOCODE = os.environ.get('SKIP_GEOCODE', '0') == '1'
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


def setup_filters(page: Page) -> None:
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
    row_count = 0
    for _ in range(60):
        page.wait_for_timeout(500)
        try:
            row_count = page.evaluate(
                "() => document.getElementById('VW_PUBLIC_ESTINSPTableControlGrid')?.rows.length || 0"
            )
        except Exception:
            row_count = 0
        if row_count > 11:
            break
    try:
        total_pages = page.evaluate(
            "() => document.querySelector('#ctl00_PageContent_Pagination__TotalPages')?.innerText || ''"
        )
    except Exception:
        total_pages = ''
    print(f'  grid now shows {row_count - 1} rows; total pages: {total_pages}')


def parse_current_page(page: Page) -> list[dict]:
    rows = page.evaluate(
        """
        () => {
          const grid = document.getElementById('VW_PUBLIC_ESTINSPTableControlGrid');
          if (!grid) return [];
          const out = [];
          for (const tr of grid.rows) {
            const cells = tr.cells;
            if (cells.length < 9) continue;
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
        if r['date'].lower() == 'inspection date':
            continue
        if not r['state_id']:
            continue
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
        try:
            iso_date: Optional[str] = datetime.strptime(r['date'], '%m/%d/%Y').date().isoformat()
        except (TypeError, ValueError):
            iso_date = None

        grade = r['grade'] if r['grade'] and r['grade'] != 'N/A' else None
        # Title-case the street so the resulting address matches the geocode
        # cache key format (cache was built from prior Chrome-side scrapes
        # that did the same). With ALLCAPS streets the cache misses ~50% of
        # entries and the CI run blows past the 60-minute job timeout.
        parsed.append({
            'state_id': r['state_id'],
            'name': r['name'],
            'address_street': street.title(),
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
    next_btn = page.locator(SEL_NEXT_PAGE).first
    try:
        next_btn.wait_for(state='visible', timeout=2000)
    except PlaywrightTimeoutError:
        return False
    try:
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
    except Exception:
        before = {'page': None, 'firstId': None}
    next_btn.click(no_wait_after=True)
    for _ in range(60):
        page.wait_for_timeout(500)
        try:
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
        except Exception:
            continue
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
        print(f'  page {page_num}: parsed {len(rows)} rows ({len(new_rows)} new; total: {len(all_rows)})')
        if rows and not new_rows:
            consecutive_dupe_pages += 1
            if consecutive_dupe_pages >= EARLY_STOP_DUPE_PAGES:
                print(f'  stopping early after {consecutive_dupe_pages} duplicate pages.')
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


def load_geocode_cache() -> dict:
    if not os.path.exists(GEOCODE_CACHE):
        return {}
    with open(GEOCODE_CACHE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_geocode_cache(cache: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(GEOCODE_CACHE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)


def geocode(session, address, cache):
    if address in cache:
        c = cache[address]
        if c.get('lat') is not None and c.get('lng') is not None:
            return c['lat'], c['lng']
        return None
    try:
        time.sleep(NOMINATIM_RATE_LIMIT_S)
        r = session.get(NOMINATIM_URL, params={'q': address, 'format': 'json', 'limit': 1},
                        headers={'User-Agent': USER_AGENT}, timeout=20)
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


def dedupe_to_latest(rows):
    by_id = {}
    for r in rows:
        sid = r['state_id']
        ex = by_id.get(sid)
        if ex is None:
            by_id[sid] = r
            continue
        if (r['inspection_date'] or '') > (ex['inspection_date'] or ''):
            by_id[sid] = r
    return list(by_id.values())


def to_restaurant_shape(rows, session, cache):
    out = []
    geocoded_count = 0
    for idx, r in enumerate(rows, start=1):
        full_address = f'{r["address_street"]}, {r["city"]}, {r["state"]} {r["zip"]}'.strip(', ').strip()
        latlng = None
        if session is not None and full_address:
            latlng = geocode(session, full_address, cache)
            if latlng:
                geocoded_count += 1
            if idx % 25 == 0:
                print(f'  geocoded {idx}/{len(rows)} (hits: {geocoded_count})')
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
            'officialUrl': None,
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
        print(f'Geocoding {len(unique)} addresses via Nominatim...')
        geo_session = requests.Session()
        geo_session.headers.update({'User-Agent': USER_AGENT})
        restaurants = to_restaurant_shape(unique, geo_session, cache)
        save_geocode_cache(cache)
    bundle = {
        'fetchedAt': datetime.now(timezone.utc).isoformat(),
        'source': 'public.cdpehs.com (NC state Environmental Health)',
        'jurisdiction': 'us-nc-mecklenburg',
        'description': 'Restaurant inspections from Mecklenburg County, NC.',
        'count': len(restaurants),
        'restaurants': restaurants,
    }
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(bundle, f, indent=2)
    print(f'\nWrote {OUTPUT_JSON} ({len(restaurants)} restaurants)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
