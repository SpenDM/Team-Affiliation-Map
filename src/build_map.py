"""
Step 5 – Join scores onto geographic boundaries and write map.geojson.

Downloads boundary files if not already cached locally:
  • US counties: Census Bureau TIGER/Line cartographic boundary (5m scale)
    https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_county_5m.zip
  • Canada census divisions: Statistics Canada 2021 Census boundary file
    https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lcd_000b21a_e.zip

Output: output/map.geojson
  Properties per feature:
    geo_id, geo_name, dominant_team, dominant_league, margin,
    dominant_score, runner_up_team, runner_up_score
    (all raw scores preserved for tooltip use)

Rendering (colours, legend, interactivity) is left for a follow-up step.
"""

import io
import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT       = Path(__file__).parent.parent
DATA_DIR   = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

SCORES_CSV    = OUTPUT_DIR / "scores_unbalanced.csv"
MAP_GEOJSON   = OUTPUT_DIR / "map.geojson"

# Cached boundary file paths (so re-runs don't re-download)
US_SHAPEFILE_ZIP  = DATA_DIR / "cb_2022_us_county_5m.zip"
CA_SHAPEFILE_ZIP  = DATA_DIR / "lcd_000b21a_e.zip"

US_BOUNDARY_URL = (
    "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_county_5m.zip"
)
CA_BOUNDARY_URL = (
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/"
    "sip-pis/boundary-limites/files-fichiers/lcd_000b21a_e.zip"
)


# ---------------------------------------------------------------------------
# Boundary loaders
# ---------------------------------------------------------------------------

def _download_if_missing(url: str, dest: Path) -> None:
    if dest.exists():
        log.info(f"  Using cached {dest.name}")
        return
    log.info(f"  Downloading {url} …")
    resp = requests.get(url, timeout=300, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    log.info(f"  Saved → {dest.name}")


def load_us_counties() -> gpd.GeoDataFrame:
    _download_if_missing(US_BOUNDARY_URL, US_SHAPEFILE_ZIP)
    gdf = gpd.read_file(f"zip://{US_SHAPEFILE_ZIP}")
    # TIGER columns: GEOID (5-digit FIPS), NAME (county name), STATEFP, etc.
    gdf = gdf.rename(columns={"GEOID": "geo_id", "NAME": "geo_name"})
    return gdf[["geo_id", "geo_name", "geometry"]]


def load_ca_divisions() -> gpd.GeoDataFrame:
    _download_if_missing(CA_BOUNDARY_URL, CA_SHAPEFILE_ZIP)
    gdf = gpd.read_file(f"zip://{CA_SHAPEFILE_ZIP}")
    # StatCan columns: CDUID (4-digit division code), CDNAME
    rename = {}
    for col in gdf.columns:
        uc = col.upper()
        if uc == "CDUID":
            rename[col] = "geo_id"
        elif uc == "CDNAME":
            rename[col] = "geo_name"
    gdf = gdf.rename(columns=rename)
    if "geo_id" not in gdf.columns:
        raise ValueError(
            f"Could not find CDUID column in Canada boundary file. "
            f"Available: {gdf.columns.tolist()}"
        )
    gdf["geo_id"] = gdf["geo_id"].astype(str).str.zfill(4)
    return gdf[["geo_id", "geo_name", "geometry"]]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not SCORES_CSV.exists():
        raise FileNotFoundError(
            f"{SCORES_CSV} not found — run score.py first."
        )

    scores = pd.read_csv(SCORES_CSV, dtype=str)
    scores = scores.rename(columns={"county_fips_or_division_id": "geo_id"})

    log.info("Loading US county boundaries …")
    us_gdf = load_us_counties()
    log.info("Loading Canada census division boundaries …")
    ca_gdf = load_ca_divisions()

    # Reproject Canada to match US CRS (NAD83 geographic) before concatenating
    ca_gdf = ca_gdf.to_crs(us_gdf.crs)
    boundaries = pd.concat([us_gdf, ca_gdf], ignore_index=True)

    merged = boundaries.merge(scores, on="geo_id", how="left")

    # Prefer geo_name from boundaries (authoritative) over scores
    if "county_name" in merged.columns:
        merged["geo_name"] = merged["geo_name"].fillna(merged["county_name"])
        merged = merged.drop(columns=["county_name"], errors="ignore")

    unmatched = merged[merged["dominant_team"].isna()]
    if not unmatched.empty:
        log.warning(
            f"{len(unmatched)} geographic units have no score "
            f"(check crosswalk coverage or run fetch+score again)."
        )

    out_cols = [
        "geo_id", "geo_name",
        "dominant_team", "dominant_league", "margin",
        "dominant_score", "runner_up_team", "runner_up_score",
        "geometry",
    ]
    out_cols = [c for c in out_cols if c in merged.columns]
    merged = gpd.GeoDataFrame(merged[out_cols], geometry="geometry", crs=us_gdf.crs)

    merged.to_file(MAP_GEOJSON, driver="GeoJSON")
    log.info(f"Wrote {len(merged)} features → {MAP_GEOJSON.name}")


if __name__ == "__main__":
    main()
