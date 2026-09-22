"""
Step 3 – Fetch Google Trends interest-by-region for all 229 teams.

Batching strategy
-----------------
pytrends compares at most 5 terms per call and returns 0–100 scores
scaled *within that batch only*.  To make batches comparable we include
one shared anchor in every batch:

    anchor: Dallas Cowboys (T009) – broad, stable, non-seasonal

    batch size: anchor + 4 target teams = 5 terms/call

229 teams − 1 anchor = 228 target teams → 57 batches.

Each batch is fetched twice:
  • geo='US', resolution='DMA'    → data/raw/batch_us_NNN.json
  • geo='CA', resolution='REGION' → data/raw/batch_ca_NNN.json

If a cache file already exists, that batch is skipped entirely —
safe to re-run after crashes.

Rate limiting
-------------
Exponential backoff on 429s (base 2s, cap 60s, up to 5 retries).
Flat 1–2s sleep between every batch regardless.
"""

import json
import logging
import random
import sys
import time
from pathlib import Path

import pandas as pd
from pytrends.request import TrendReq

from _backoff import with_backoff

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT      = Path(__file__).parent.parent
DATA_DIR  = ROOT / "data"
RAW_DIR   = DATA_DIR / "raw"
TEAMS_CSV = DATA_DIR / "teams.csv"

RAW_DIR.mkdir(parents=True, exist_ok=True)

ANCHOR_ID  = "T009"   # Dallas Cowboys
TIMEFRAME  = "today 5-y"
BATCH_SIZE = 4        # target teams per batch (+ 1 anchor = 5 total)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(geo: str, batch_idx: int) -> Path:
    return RAW_DIR / f"batch_{geo.lower()}_{batch_idx:03d}.json"


def _load_or_fetch(
    pytrends: TrendReq,
    queries: list[str],
    geo: str,
    resolution: str,
    batch_idx: int,
) -> pd.DataFrame:
    """
    Return interest_by_region DataFrame for the batch.
    Reads from cache if available; otherwise fetches and writes cache.

    The JSON cache schema:
    {
      "geo": "US",
      "resolution": "DMA",
      "queries": [...],
      "data": [{"geoName": ..., "query1": val, ...}, ...]   # records orientation
    }
    """
    path = _cache_path(geo, batch_idx)

    if path.exists():
        log.info(f"  Cache hit: {path.name}")
        with open(path) as f:
            cached = json.load(f)
        df = pd.DataFrame(cached["data"])
        if "geoName" in df.columns:
            df = df.set_index("geoName")
        return df

    def _fetch() -> pd.DataFrame:
        pytrends.build_payload(queries, timeframe=TIMEFRAME, geo=geo)
        return pytrends.interest_by_region(
            resolution=resolution,
            inc_low_vol=True,
            inc_geo_code=False,
        )

    df = with_backoff(_fetch)

    # Persist raw response before any processing
    cache_obj = {
        "geo":        geo,
        "resolution": resolution,
        "queries":    queries,
        "data":       df.reset_index().rename(columns={"index": "geoName"}).to_dict(orient="records"),
    }
    with open(path, "w") as f:
        json.dump(cache_obj, f)

    log.info(f"  Fetched and cached: {path.name} ({len(df)} regions)")
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    teams = pd.read_csv(TEAMS_CSV, dtype=str).fillna("")

    anchor_row = teams[teams["team_id"] == ANCHOR_ID]
    if anchor_row.empty:
        log.error(f"Anchor team {ANCHOR_ID} not found in teams.csv")
        sys.exit(1)

    anchor_query = anchor_row.iloc[0]["search_query"]
    targets = teams[teams["team_id"] != ANCHOR_ID]["search_query"].tolist()

    batches = [targets[i : i + BATCH_SIZE] for i in range(0, len(targets), BATCH_SIZE)]
    log.info(
        f"Anchor: {anchor_query!r} | "
        f"{len(targets)} target teams → {len(batches)} batches"
    )

    pytrends = TrendReq(
        hl="en-US",
        tz=360,
        retries=2,
        backoff_factor=2,
        timeout=(10, 25),
        requests_args={
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        },
    )

    for geo, resolution in [("US", "DMA"), ("CA", "REGION")]:
        log.info(f"\n=== Fetching {geo} ({resolution}) ===")
        for i, batch in enumerate(batches):
            queries = [anchor_query] + batch
            path = _cache_path(geo, i)
            if path.exists():
                log.info(f"  [{i+1}/{len(batches)}] {path.name} already cached, skipping")
                continue
            log.info(f"  [{i+1}/{len(batches)}] batch {i:03d}: {[q[:30] for q in batch]}")
            try:
                _load_or_fetch(pytrends, queries, geo, resolution, i)
            except Exception as e:
                log.error(f"  Batch {i} failed after retries: {e}")
                log.error("  Cached batches so far are safe; re-run to resume.")
                sys.exit(1)

            time.sleep(random.uniform(4.0, 7.0))

    log.info("\nAll batches complete.")


if __name__ == "__main__":
    main()
