"""
Geocode-only pass for charlotte-mecklenburg.json.

Takes the JSON produced by scraper.py (or by the Chrome MCP scrape) and
fills in latitude/longitude on every restaurant whose coordinates are
still null. Uses OpenStreetMap Nominatim at the polite 1 req/sec rate
and caches every successful lookup so that re-runs are nearly free.

Why this exists separately from scraper.py:

    scraper.py does scrape + geocode in one shot. If you already have a
    fresh JSON (e.g. from a Chrome-driven scrape) and only need to fill
    in coordinates, running the full scrape again is wasteful. This
    script does just the geocoding half.

Usage:

    cd "C:\\Users\\gregs\\OneDrive\\Desktop\\SimTech Affiliate\\Inspect My Chef\\Data\\charlotte-scraper"
    .venv\\Scripts\\activate
    python geocode_only.py

Optional env vars:

    INPUT_JSON      override the input file path
    OUTPUT_JSON     override the output file path (defaults to input — overwrites in place)
    MAX             only geocode the first N missing addresses (good for a quick smoke test)
    RESUME_FROM     skip records with state_id sort key < this value
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

# ----------------------------------------------------------------------
# Config

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, 'output')
DEFAULT_JSON = os.path.join(OUTPUT_DIR, 'charlotte-mecklenburg.json')
GEOCODE_CACHE = os.path.join(OUTPUT_DIR, 'geocode-cache.json')

INPUT_JSON = os.environ.get('INPUT_JSON', DEFAULT_JSON)
OUTPUT_JSON = os.environ.get('OUTPUT_JSON', INPUT_JSON)
MAX = int(os.environ['MAX']) if os.environ.get('MAX') else None
RESUME_FROM = os.environ.get('RESUME_FROM')

NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
NOMINATIM_RATE_LIMIT_S = 1.05

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 '
    'InspectMyChef-Geocoder/1.0'
)

# Periodic save: flush cache + bundle every N successful geocodes so an
# interruption only costs a few addresses, not the whole run.
SAVE_EVERY = 25

# ----------------------------------------------------------------------
# Cache I/O


def load_cache() -> dict[str, dict]:
    if not os.path.exists(GEOCODE_CACHE):
        return {}
    try:
        with open(GEOCODE_CACHE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f'  warning: {GEOCODE_CACHE} was malformed — starting fresh', file=sys.stderr)
        return {}


def save_cache(cache: dict[str, dict]) -> None:
    """Write cache directly (no tmp + rename).

    Atomic rename via os.replace() races with OneDrive's file watcher on
    OneDrive-synced paths and can leave the target as a zero-byte file.
    Direct write is less safe against process crash but compatible with
    OneDrive. Worst case: a crash mid-write costs the latest 25 records.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(GEOCODE_CACHE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)


def save_bundle(bundle: dict) -> None:
    """Write the bundle directly (no tmp + rename). Same OneDrive reason
    as save_cache: atomic rename gets eaten by OneDrive sync."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(bundle, f, indent=2)


# ----------------------------------------------------------------------
# Geocoding


def geocode(
    session: requests.Session,
    address: str,
    cache: dict[str, dict],
) -> Optional[tuple[float, float]]:
    """Look up coords for an address. Cache hits are free.

    Returns None for any kind of miss (not found, timeout, malformed
    response). The negative result is also cached so re-runs don't
    re-attempt every failure.
    """
    if address in cache:
        c = cache[address]
        if c.get('lat') is not None and c.get('lng') is not None:
            return c['lat'], c['lng']
        return None
    try:
        time.sleep(NOMINATIM_RATE_LIMIT_S)
        r = session.get(
            NOMINATIM_URL,
            params={
                'q': address,
                'format': 'json',
                'limit': 1,
                'countrycodes': 'us',
            },
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


# ----------------------------------------------------------------------
# Orchestrate


def main() -> int:
    if not os.path.exists(INPUT_JSON):
        print(f'ERROR: {INPUT_JSON} does not exist. Run the scraper first.', file=sys.stderr)
        return 1

    print(f'Reading {INPUT_JSON}...')
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        bundle = json.load(f)

    restaurants = bundle.get('restaurants', [])
    total = len(restaurants)
    have_coords = sum(
        1 for r in restaurants
        if r.get('latitude') is not None and r.get('longitude') is not None
    )
    missing = total - have_coords
    print(f'  {total} restaurants total — {have_coords} already geocoded, {missing} missing')

    if missing == 0:
        print('Nothing to do. Every restaurant already has coordinates.')
        return 0

    cache = load_cache()
    print(f'  loaded {len(cache)} cached geocodes')

    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    # Install a Ctrl+C handler that flushes cache + bundle before exiting.
    interrupted = {'flag': False}

    def handle_sigint(signum, frame):
        interrupted['flag'] = True
        print('\nInterrupt received — saving progress before exiting...', file=sys.stderr)

    signal.signal(signal.SIGINT, handle_sigint)

    attempts = 0
    hits = 0
    skipped_resume = 0
    estimated_seconds = missing * NOMINATIM_RATE_LIMIT_S
    print(
        f'  estimated time: {estimated_seconds / 60:.1f} minutes '
        f'(roughly {NOMINATIM_RATE_LIMIT_S:.2f}s per missing record; '
        'cache hits are instant)'
    )

    start = time.monotonic()
    for idx, r in enumerate(restaurants):
        if interrupted['flag']:
            break
        if r.get('latitude') is not None and r.get('longitude') is not None:
            continue
        if RESUME_FROM and (r.get('id') or '') < RESUME_FROM:
            skipped_resume += 1
            continue
        if MAX is not None and attempts >= MAX:
            print(f'  hit MAX={MAX} cap. Stopping.')
            break

        address = r.get('address') or ''
        if not address or address == '(no address)':
            continue

        attempts += 1
        latlng = geocode(session, address, cache)
        if latlng:
            r['latitude'] = latlng[0]
            r['longitude'] = latlng[1]
            hits += 1

        if attempts % SAVE_EVERY == 0:
            elapsed = time.monotonic() - start
            rate = attempts / elapsed if elapsed > 0 else 0
            remaining = (missing - attempts) / rate if rate > 0 else 0
            print(
                f'  [{idx + 1}/{total}] attempted {attempts}, '
                f'success {hits} ({hits / attempts:.0%}), '
                f'ETA {remaining / 60:.1f} min'
            )
            save_cache(cache)
            save_bundle(bundle)

    save_cache(cache)

    # Refresh the bundle metadata before final save.
    bundle['fetchedAt'] = bundle.get('fetchedAt') or datetime.now(timezone.utc).isoformat()
    bundle['geocodedAt'] = datetime.now(timezone.utc).isoformat()
    bundle['count'] = len(restaurants)
    save_bundle(bundle)

    final_have = sum(
        1 for r in restaurants
        if r.get('latitude') is not None and r.get('longitude') is not None
    )
    print()
    print(f'Wrote {OUTPUT_JSON}')
    print(
        f'  geocoded coverage: {final_have}/{total} '
        f'({final_have / total:.1%})'
    )
    if skipped_resume:
        print(f'  resume mode skipped {skipped_resume} records before RESUME_FROM')
    if interrupted['flag']:
        print('  exited via Ctrl+C — re-run to continue from cache.')
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
