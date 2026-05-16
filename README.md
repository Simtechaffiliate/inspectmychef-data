# Charlotte / Mecklenburg County restaurant inspection scraper

Pulls restaurant inspection data from the NC State Environmental Health public portal at `public.cdpehs.com` and writes a JSON file in the same shape the Inspect My Chef mobile app consumes.

## What it produces

`output/charlotte-mecklenburg.json` — one entry per restaurant (deduped by state ID, most recent inspection wins). Format mirrors `Restaurant` from `App/src/data/types.ts`:

```json
{
  "fetchedAt": "2026-05-15T22:00:00Z",
  "source": "public.cdpehs.com (NC state Environmental Health)",
  "jurisdiction": "us-nc-mecklenburg",
  "count": 2847,
  "restaurants": [
    {
      "id": "us-mck-2060018931",
      "name": "CARNITAS GUANAJUATO",
      "address": "5534 ALBEMARLE RD SUITE 101, Charlotte, NC 28212",
      "city": "Charlotte",
      "country": "US",
      "region": "Charlotte",
      "cuisine": "1 - Restaurant",
      "latestInspection": {
        "inspectionDate": "2026-05-15",
        "score": 96.5,
        "grade": "A",
        "gradeDate": "2026-05-15",
        "violations": []
      },
      "latitude": 35.1827,
      "longitude": -80.7398,
      "officialUrl": "https://public.cdpehs.com/..."
    }
  ]
}
```

## Setup (Windows)

```powershell
cd "C:\Users\gregs\OneDrive\Desktop\SimTech Affiliate\Inspect My Chef\Data\charlotte-scraper"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python scraper.py
```

## Runtime expectations

- **Pagination phase:** ~5–10 minutes. The portal exposes ~54,500 inspection
  records (one per inspection event) across 273 pages of 200 rows. The
  scraper dedupes by state ID and **stops early** after 3 consecutive pages
  contribute no new restaurants — so in practice it covers ~10–20 pages
  before hitting that termination, since the grid is sorted newest-first.
- **Geocoding phase:** ~1 second per unique address — for ~3,000
  establishments this is **~50 minutes**. Subsequent runs use
  `output/geocode-cache.json` so most addresses are skipped.
- **Total first run:** ~1 hour. Re-runs after cache: ~10 minutes.

## Pagination model (verified 2026-05-15)

The CDPEHS grid uses two text-input postbacks rather than visible
prev/next links. The scraper drives them via:

- `#ctl00_PageContent_FilterButton__Button` — initial Search.
- `#ctl00_PageContent_Pagination__PageSize` + `__PageSizeButton` — set to
  200 to minimize round-trips.
- `#ctl00_PageContent_Pagination__NextPage` — image button that advances
  one page at a time. The scraper detects advancement by watching the
  CurrentPage input value and the first-row state ID.

## Re-running on a schedule

Once the data shape is stable, this script gets wired into GitHub Actions on a weekly cron. The resulting JSON gets committed back to the data repo, and the mobile app fetches the latest version via the jsDelivr CDN.

## Caveats

- **Violations aren't included yet.** The CDPEHS portal renders violations on a separate drill-down page that's a JS postback. A future iteration can scrape those too.
- **Geocoding uses OpenStreetMap Nominatim** (free, 1 req/sec rate limit). The cache prevents hitting them on every run.
- **`officialUrl`** points to a CDPEHS search-page URL filtered by the restaurant's first-word name. CDPEHS uses ASP.NET postbacks for individual records, so we don't have stable per-record URLs.
