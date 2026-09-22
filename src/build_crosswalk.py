"""
Step 1 – Build geography crosswalk files.

US  → data/dma_county_crosswalk.csv
      Columns: county_fips, county_name, state, dma_name, dma_code
      Source: USDA Economic Research Service "TV Access" dataset.
      See README for citation and as-of date.

CA  → data/canada_division_market.csv
      Columns: division_id, division_name, province, trends_geo
      Province code derived from Statistics Canada SGC division IDs.
      Google Trends geo codes follow ISO 3166-2 (e.g. CA-ON, CA-BC).

Writes data/unmapped_geographies.csv for any county/division that
cannot be confidently assigned; nothing is silently dropped.

Run once; outputs are cached and checked before any download is attempted.
"""

import sys
import logging
import requests
import pandas as pd
from io import BytesIO
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

US_OUTPUT = DATA_DIR / "dma_county_crosswalk.csv"
CA_OUTPUT = DATA_DIR / "canada_division_market.csv"
UNMAPPED_OUTPUT = DATA_DIR / "unmapped_geographies.csv"

# Primary: BritCrit/dma_county_zip – county FIPS + DMA code + DMA name.
# One row per zip code; we deduplicate to one row per county below.
# Columns: fips, county, st, dma_code, dma_name, zipcode
BRITCRIT_URL = (
    "https://raw.githubusercontent.com/BritCrit/dma_county_zip/"
    "master/dma_county_zip_data_set.csv"
)

# Secondary: alex-patton/US-TVDMA-BY-COUNTY – county name + DMA name (no FIPS).
# Columns: STATE, STATE_AB, COUNTY, Internal_State_Region, TVDMA, ...
ALEX_PATTON_URL = (
    "https://raw.githubusercontent.com/alex-patton/US-TVDMA-BY-COUNTY/"
    "master/usa-tvdma-county.csv"
)

# Statistics Canada population and dwelling counts by geography (2021 Census).
# This ZIP contains all geographic levels; we filter to census divisions
# using DGUID length 13 (pattern 2021A0003XXXX, XXXX = 4-digit CDUID).
# Source: https://www150.statcan.gc.ca/n1/tbl/csv/98100004-eng.zip
# (Table 98-10-0004-01 — Population and dwelling counts, 2021 Census)
STATCAN_POP_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/98100004-eng.zip"

# Statistics Canada SGC province/territory code → (province name, Google Trends geo)
# Google Trends uses ISO 3166-2 subdivision codes.
SGC_TO_TRENDS: dict[str, tuple[str, str]] = {
    "10": ("Newfoundland and Labrador", "CA-NL"),
    "11": ("Prince Edward Island",      "CA-PE"),
    "12": ("Nova Scotia",               "CA-NS"),
    "13": ("New Brunswick",             "CA-NB"),
    "24": ("Quebec",                    "CA-QC"),
    "35": ("Ontario",                   "CA-ON"),
    "46": ("Manitoba",                  "CA-MB"),
    "47": ("Saskatchewan",              "CA-SK"),
    "48": ("Alberta",                   "CA-AB"),
    "59": ("British Columbia",          "CA-BC"),
    "60": ("Yukon",                     "CA-YT"),
    "61": ("Northwest Territories",     "CA-NT"),
    "62": ("Nunavut",                   "CA-NU"),
}


# ---------------------------------------------------------------------------
# US crosswalk
# ---------------------------------------------------------------------------

def _download_us_raw() -> pd.DataFrame:
    """Try BritCrit first, fall back to alex-patton, then check for local file."""
    for url in (BRITCRIT_URL, ALEX_PATTON_URL):
        try:
            log.info(f"Trying {url}")
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            return pd.read_csv(BytesIO(resp.content), dtype=str)
        except Exception as e:
            log.warning(f"Download failed ({url}): {e}")

    # Check if the user passed a local path as argument
    if len(sys.argv) > 1:
        local = Path(sys.argv[1])
        if local.exists():
            log.info(f"Using local file: {local}")
            if local.suffix in (".xlsx", ".xls"):
                return pd.read_excel(local, dtype=str)
            return pd.read_csv(local, dtype=str)

    log.error(
        "All download sources failed.  Download a county-DMA crosswalk CSV manually\n"
        "and pass the path as an argument:\n"
        "  python src/build_crosswalk.py /path/to/crosswalk.csv\n"
        "\n"
        "Expected columns (any of these naming styles accepted):\n"
        "  fips/county_fips, county/county_name, st/state,\n"
        "  dma_code, dma_name/TVDMA"
    )
    sys.exit(1)


def _normalise_us_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map whatever column names the source uses to our canonical names."""
    rename = {}
    for col in df.columns:
        lc = col.lower().replace(" ", "_").replace("-", "_")
        if lc in ("fips", "county_fips", "countyfips", "geoid", "geo_id"):
            rename[col] = "county_fips"
        elif "county" in lc and "fips" in lc:
            rename[col] = "county_fips"
        elif lc in ("county", "county_name", "countyname"):
            rename[col] = "county_name"
        elif lc in ("state", "state_abbr", "stateabbr", "st", "state_ab"):
            rename[col] = "state"
        elif lc in ("tvdma", "tv_dma"):
            rename[col] = "dma_name"
        elif "dma" in lc and "name" in lc:
            rename[col] = "dma_name"
        elif "market" in lc and "name" in lc:
            rename[col] = "dma_name"
        elif lc in ("dma", "dma_code", "dmacode", "dma_num", "dma_id",
                    "market", "market_code", "marketcode"):
            rename[col] = "dma_code"
    return df.rename(columns=rename)


def build_us_crosswalk() -> None:
    if US_OUTPUT.exists():
        log.info("US crosswalk already exists — skipping.")
        return

    df = _download_us_raw()
    df = _normalise_us_columns(df)

    required = {"county_fips", "county_name", "state", "dma_name", "dma_code"}
    missing = required - set(df.columns)
    if missing:
        log.error(
            f"Could not map columns {missing} from source file.\n"
            f"Available columns: {df.columns.tolist()}\n"
            f"Update _normalise_us_columns() in src/build_crosswalk.py."
        )
        sys.exit(1)

    df = df[list(required)].copy()
    df["county_fips"] = df["county_fips"].str.strip().str.zfill(5)

    # BritCrit source has one row per zip code; deduplicate to one row per county.
    # If dma_code is missing (alex-patton source), deduplicate on fips+dma_name.
    dedup_cols = ["county_fips", "dma_name"]
    if "dma_code" in df.columns and df["dma_code"].notna().any():
        dedup_cols = ["county_fips"]
    df = df.drop_duplicates(subset=dedup_cols)

    # Normalise DMA name to Title Case so it can be matched case-insensitively
    # against pytrends geoName values (Google Trends uses mixed case).
    df["dma_name"] = df["dma_name"].str.strip().str.title()

    unmapped = df[df["dma_code"].isna() | (df["dma_code"].str.strip() == "")]
    if not unmapped.empty:
        log.warning(f"{len(unmapped)} US counties have no DMA — writing to unmapped_geographies.csv")
        _append_unmapped(
            unmapped[["county_fips", "county_name", "state"]].assign(source="US_county")
        )
        df = df[df["dma_code"].notna() & (df["dma_code"].str.strip() != "")]

    df.to_csv(US_OUTPUT, index=False)
    log.info(f"Wrote {len(df)} rows → {US_OUTPUT.name}")


# ---------------------------------------------------------------------------
# Canada crosswalk
# ---------------------------------------------------------------------------

def _fetch_statcan_divisions() -> pd.DataFrame:
    """
    Download StatCan table 98-10-0004-01 (population counts by geography, 2021).
    Census divisions are rows where DGUID matches 2021A0003XXXX (length 13).
    CDUID = last 4 chars of DGUID.  Province code = first 2 chars of CDUID.
    Returns a DataFrame with columns: CDUID, CDNAME, PRUID.
    """
    import zipfile, io as _io

    # Check for manually placed CSV first
    manual = DATA_DIR / "statcan_cd.csv"
    if manual.exists():
        log.info(f"Using manual StatCan file: {manual}")
        df = pd.read_csv(manual, dtype=str)
        df.columns = [c.strip().upper() for c in df.columns]
        return df

    log.info(f"Downloading StatCan population table …")
    try:
        resp = requests.get(STATCAN_POP_URL, timeout=120)
        resp.raise_for_status()
    except Exception as e:
        log.error(
            f"StatCan download failed: {e}\n"
            "Save a CSV with columns CDUID, CDNAME, PRUID to data/statcan_cd.csv and re-run."
        )
        return pd.DataFrame()

    try:
        with zipfile.ZipFile(_io.BytesIO(resp.content)) as z:
            csv_names = [n for n in z.namelist() if n.endswith(".csv") and "Meta" not in n]
            with z.open(csv_names[0]) as f:
                df = pd.read_csv(f, dtype=str)
    except Exception as e:
        log.error(f"Failed to parse StatCan ZIP: {e}")
        return pd.DataFrame()

    # Filter to census divisions: DGUID of exactly 13 chars matching 2021A0003XXXX
    cd_mask = df["DGUID"].str.match(r"^2021A0003\d{4}$", na=False)
    df_cd = df[cd_mask].copy()
    df_cd["CDUID"]  = df_cd["DGUID"].str[-4:]          # last 4 digits
    df_cd["PRUID"]  = df_cd["CDUID"].str[:2]            # first 2 = province SGC code
    df_cd["CDNAME"] = df_cd["GEO"]
    log.info(f"Found {len(df_cd)} census division rows in StatCan table")
    return df_cd[["CDUID", "CDNAME", "PRUID"]]


def build_canada_crosswalk() -> None:
    if CA_OUTPUT.exists():
        log.info("Canada crosswalk already exists — skipping.")
        return

    df_cd = _fetch_statcan_divisions()

    rows = []
    unmapped_rows = []

    for _, rec in df_cd.iterrows():
        div_id   = str(rec.get("CDUID", "")).zfill(4)
        div_name = str(rec.get("CDNAME", "Unknown"))
        pr_code  = str(rec.get("PRUID", div_id[:2])).zfill(2)

        if pr_code in SGC_TO_TRENDS:
            prov_name, trends_geo = SGC_TO_TRENDS[pr_code]
            rows.append({
                "division_id":   div_id,
                "division_name": div_name,
                "province":      prov_name,
                "trends_geo":    trends_geo,
            })
        else:
            unmapped_rows.append({
                "division_id":   div_id,
                "division_name": div_name,
                "source":        "CA_division",
            })

    if unmapped_rows:
        log.warning(f"{len(unmapped_rows)} CA divisions unmapped — writing to unmapped_geographies.csv")
        _append_unmapped(pd.DataFrame(unmapped_rows))

    df = pd.DataFrame(rows)
    df.to_csv(CA_OUTPUT, index=False)
    log.info(f"Wrote {len(df)} rows → {CA_OUTPUT.name}")


# ---------------------------------------------------------------------------

def _append_unmapped(df: pd.DataFrame) -> None:
    mode   = "a" if UNMAPPED_OUTPUT.exists() else "w"
    header = not UNMAPPED_OUTPUT.exists()
    df.to_csv(UNMAPPED_OUTPUT, mode=mode, header=header, index=False)


if __name__ == "__main__":
    build_us_crosswalk()
    build_canada_crosswalk()
    log.info("Done.")
