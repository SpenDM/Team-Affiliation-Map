# Sports Fandom Map

US + Canada map showing the dominant sports team by county (US) / census division (Canada), using Google Trends search interest as the fandom proxy.

229 teams across NFL, NBA, NHL, MLB, College Football, and College Basketball.

---

## How to run

Install dependencies first:

```bash
pip install -r requirements.txt
```

Run each step in order.  Steps 1–2 run once; Steps 3–5 can be re-run freely without hitting the network once the cache is warm.

### Step 1 – Build geography crosswalk

```bash
python src/build_crosswalk.py
```

Downloads and processes:
- `data/dma_county_crosswalk.csv` — US county FIPS → Nielsen DMA
- `data/canada_division_market.csv` — Canada census division → Google Trends geo

Any county or division that cannot be confidently mapped is written to `data/unmapped_geographies.csv` for manual review rather than being silently dropped.

If the automatic download fails, see [Crosswalk sources](#crosswalk-sources) below.

### Step 2 – Resolve Google entity IDs

```bash
python src/resolve_entities.py
```

Calls pytrends `suggestions()` for every team in `data/teams.csv` whose `google_entity_id` column is blank, and writes the resolved IDs back in place.  Teams with no confident match are logged to `data/unresolved_entities.csv` for manual resolution.

Skips rows that already have an entity ID — safe to re-run.

### Step 3 – Fetch Google Trends data

```bash
python src/fetch_trends.py
```

Batches 229 teams into groups of 4 + 1 shared anchor (Dallas Cowboys, T009).  Each batch is fetched once for US (DMA resolution) and once for Canada (province resolution).  Raw JSON responses are cached to `data/raw/` before any processing.

**This step takes a while** — ~57 batches × 2 geos × 1–2s sleep = ~2–4 minutes minimum, longer if pytrends rate-limits.  That is expected and fine.  The script is safe to interrupt and resume: it skips any batch whose cache file already exists.

### Step 4 – Score (unbalanced v1)

```bash
python src/score.py
```

Reads all cached batches, normalizes by anchor value, expands DMA/province scores to county/division level, and picks the dominant team per county.  Writes:

```
output/scores_unbalanced.csv
```

Freely re-runnable without touching the network.

### Step 5 – Build map

```bash
python src/build_map.py
```

Downloads Census TIGER county boundaries (US) and Statistics Canada census division boundaries (Canada) — cached locally after first run.  Joins the scores and writes:

```
output/map.geojson
```

Freely re-runnable without touching the network once the boundary files are cached.

---

## Crosswalk sources

### US county → DMA

The script tries two sources in order:

1. **BritCrit/dma_county_zip** (primary)  
   URL: `https://raw.githubusercontent.com/BritCrit/dma_county_zip/master/dma_county_zip_data_set.csv`  
   GitHub: https://github.com/BritCrit/dma_county_zip  
   Columns: `fips, county, st, dma_code, dma_name, zipcode` — one row per zip code, deduplicated to county level by the script.

2. **alex-patton/US-TVDMA-BY-COUNTY** (fallback)  
   URL: `https://raw.githubusercontent.com/alex-patton/US-TVDMA-BY-COUNTY/master/usa-tvdma-county.csv`  
   GitHub: https://github.com/alex-patton/US-TVDMA-BY-COUNTY  
   Columns: `STATE, STATE_AB, COUNTY, TVDMA, ...` — county name + DMA name only (no FIPS; matched by name).

If both fail, download any county-DMA crosswalk CSV manually and pass it as an argument:

```bash
python src/build_crosswalk.py /path/to/crosswalk.csv
```

Nielsen DMA boundaries are commercially copyrighted; these crosswalks are reconstructed from public broadcast licensing data.  **As-of date**: last verified against BritCrit dataset, September 2026.

### Canada census divisions

Census division geographic attributes are fetched from Statistics Canada's 2021 Census geographic reference files.  Province-level Google Trends geo codes (ISO 3166-2, e.g. `CA-ON`) are derived deterministically from the first two digits of the Statistics Canada SGC division code.

---

## pytrends dependency and known flakiness

`pytrends` is an **unofficial** Python wrapper for the Google Trends API.  Google does not publish an official API, so pytrends is subject to:

- **429 rate limits** — the scripts use exponential backoff (2s base, 60s cap, 5 retries) plus a 1–2s inter-batch sleep.  If you hit sustained 429s, wait a few hours and re-run; the cache means you won't re-fetch what already succeeded.
- **Response format changes** — Google occasionally changes the Trends response structure.  If parsing breaks, check the pytrends issue tracker for a newer release.
- **Relative scoring** — all values are 0–100 scaled *within a batch*, not across calls.  The anchor-relative normalization in `score.py` makes batches comparable.

---

## `data/raw/` is the source of truth

`data/raw/batch_*.json` files are the cached raw API responses.  `score.py` and `build_map.py` work entirely from these files and the crosswalk CSVs — they never touch the network.  Protect these cache files; re-fetching 114 batches from an unofficial API takes time and risks rate limits.

---

## Repo structure

```
data/
  teams.csv                    # 229 teams, checked into repo
  dma_county_crosswalk.csv     # built by Step 1
  canada_division_market.csv   # built by Step 1
  unmapped_geographies.csv     # any counties/divisions we couldn't map
  unresolved_entities.csv      # teams whose Google entity ID wasn't found
  raw/                         # cached Trends API responses (one JSON per batch)
src/
  build_crosswalk.py           # Step 1
  resolve_entities.py          # Step 2
  fetch_trends.py              # Step 3
  score.py                     # Step 4
  build_map.py                 # Step 5
  _backoff.py                  # shared exponential-backoff helper
output/
  scores_unbalanced.csv
  map.geojson
```

---

## Out of scope for this pass

- League-balanced and sqrt-balanced scoring (planned next; `score.py` is structured so only the normalization function changes)
- Map rendering, colors, legend, interactivity
- MLS; college basketball programs outside the 21 in `teams.csv`
