"""
Step 4 – Score teams by geography (unbalanced v1).

Architecture note for the balanced variant
-------------------------------------------
Two concerns are kept separate so the balanced variant only needs to
swap the normalization function:

  normalize_anchor(raw_df, anchor_query)
      Unbalanced path: divide each cell by the anchor's value in that
      geo.  Returns a DataFrame of anchor-relative scores.

  normalize_balanced(raw_df, league_totals)  ← not built yet
      Balanced path: divide by league national total instead.  Reuses
      expand_to_geography() and pick_winners() unchanged.

Outputs
-------
output/scores_unbalanced.csv
  county_fips_or_division_id, county_name, dominant_team, dominant_league,
  dominant_score, runner_up_team, runner_up_score, margin
"""

import json
import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT       = Path(__file__).parent.parent
DATA_DIR   = ROOT / "data"
RAW_DIR    = DATA_DIR / "raw"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

TEAMS_CSV         = DATA_DIR / "teams.csv"
US_CROSSWALK      = DATA_DIR / "dma_county_crosswalk.csv"
CA_CROSSWALK      = DATA_DIR / "canada_division_market.csv"
SCORES_OUTPUT          = OUTPUT_DIR / "scores_unbalanced.csv"
SCORES_BALANCED_OUTPUT = OUTPUT_DIR / "scores_balanced.csv"

ANCHOR_ID = "T009"  # Dallas Cowboys


# ---------------------------------------------------------------------------
# Load raw batch data
# ---------------------------------------------------------------------------

def load_batches(geo: str) -> list[dict]:
    """Return list of raw batch dicts for the given geo ('US' or 'CA')."""
    prefix = f"batch_{geo.lower()}_"
    paths  = sorted(RAW_DIR.glob(f"{prefix}*.json"))
    if not paths:
        raise FileNotFoundError(
            f"No cached batches found for geo={geo} in {RAW_DIR}. "
            f"Run fetch_trends.py first."
        )
    batches = []
    for p in paths:
        with open(p) as f:
            batches.append(json.load(f))
    log.info(f"Loaded {len(batches)} {geo} batches from {RAW_DIR}")
    return batches


# ---------------------------------------------------------------------------
# Normalization functions (keep separate for balanced-variant reuse)
# ---------------------------------------------------------------------------

def normalize_anchor(
    batch: dict,
    anchor_query: str,
) -> pd.DataFrame:
    """
    Unbalanced normalization: divide each team's raw interest by the
    anchor team's interest in the same geo row.

    Returns a DataFrame indexed by geo name, columns = non-anchor queries,
    values = anchor-relative scores (float, NaN where anchor was 0).
    """
    df = pd.DataFrame(batch["data"])
    if "geoName" in df.columns:
        df = df.set_index("geoName")
    # Drop rows where anchor has zero interest (avoid division by zero)
    if anchor_query not in df.columns:
        log.warning(f"Anchor query '{anchor_query}' not found in batch columns; skipping batch.")
        return pd.DataFrame()

    anchor_vals = df[anchor_query].replace(0, float("nan"))
    target_cols = [c for c in df.columns if c != anchor_query]
    normalized  = df[target_cols].div(anchor_vals, axis=0)
    return normalized


def normalize_balanced(
    batch: dict,
    league_totals: dict[str, float],
    anchor_query: str,
) -> pd.DataFrame:
    """
    Balanced normalization (stub for future variant).

    Divide each team's raw interest by the league's national total
    interest rather than by the anchor value.  league_totals maps
    search_query → national average interest.

    The county-expansion and winner-picking logic below is unchanged.
    """
    raise NotImplementedError(
        "Balanced scoring is a planned variant — implement normalize_balanced() "
        "and call it in main() with a 'balanced' flag."
    )


def location_quotient(
    geo_scores: dict[str, dict[str, float]],
    min_dmas: int = 5,
) -> dict[str, dict[str, float]]:
    """
    Location-quotient balancing applied to already-aggregated geo_scores.

    Divides each team's DMA score by that team's mean across ALL DMAs:
        LQ[team, dma] = anchor_relative[team, dma] / mean_over_dmas(anchor_relative[team])

    A value > 1 → more popular here than the team's national average.
    A value < 1 → less popular here.

    This suppresses teams that are uniformly popular everywhere (Alabama Football
    has fans in every state, so its raw anchor-relative scores are high everywhere)
    and amplifies genuine local concentration (the hometown NFL franchise that nobody
    outside the market searches for).

    Teams present in fewer than `min_dmas` are excluded — their "national average"
    is too thin to be meaningful.
    """
    # Collect all DMA-level scores per team
    team_all: dict[str, list[float]] = {}
    for scores in geo_scores.values():
        for team, score in scores.items():
            team_all.setdefault(team, []).append(score)

    # National mean per team (require ≥ min_dmas DMAs and non-zero average)
    national_mean: dict[str, float] = {
        team: sum(vals) / len(vals)
        for team, vals in team_all.items()
        if len(vals) >= min_dmas and sum(vals) > 0
    }

    return {
        dma: {
            team: score / national_mean[team]
            for team, score in scores.items()
            if team in national_mean and national_mean[team] > 0
        }
        for dma, scores in geo_scores.items()
    }


# ---------------------------------------------------------------------------
# Geography expansion
# ---------------------------------------------------------------------------

def _build_team_lookup(teams: pd.DataFrame) -> dict[str, dict]:
    """Map search_query → {team_id, team_name, league}."""
    return {
        row["search_query"]: {
            "team_id":   row["team_id"],
            "team_name": row["team_name"],
            "league":    row["league"],
        }
        for _, row in teams.iterrows()
    }


def expand_to_geography(
    geo_scores: dict[str, dict[str, float]],
    crosswalk: pd.DataFrame,
    geo_col: str,
    id_col: str,
    name_col: str,
) -> pd.DataFrame:
    """
    Expand DMA- or province-level scores out to every county/division
    that maps to that DMA/province.

    geo_scores: {geo_name: {search_query: anchor_relative_score}}
    crosswalk:  DataFrame with columns [id_col, name_col, geo_col, ...]
    geo_col:    column in crosswalk matching geo_name key (e.g. 'dma_name' or 'trends_geo')
    id_col:     output geography identifier column (e.g. 'county_fips')
    name_col:   human-readable name column

    Returns long-form DataFrame: [id_col, name_col, search_query, score]

    DMA name matching is case-insensitive to handle differences between
    pytrends geoName casing and the crosswalk source casing.
    """
    # Build a case-insensitive lookup from normalised geo name → scores
    normalised_scores = {k.strip().lower(): v for k, v in geo_scores.items()}

    rows = []
    unmatched: set[str] = set()
    for _, cw_row in crosswalk.iterrows():
        geo_key = str(cw_row[geo_col]).strip().lower()
        scores  = normalised_scores.get(geo_key)
        if scores is None:
            unmatched.add(cw_row[geo_col])
            continue
        for query, score in scores.items():
            rows.append({
                id_col:         cw_row[id_col],
                name_col:       cw_row[name_col],
                "search_query": query,
                "score":        score,
            })

    if unmatched:
        sample = sorted(unmatched)[:5]
        log.warning(
            f"{len(unmatched)} crosswalk {geo_col} values had no pytrends match "
            f"(sample: {sample}). Check that fetch_trends.py has run for this geo."
        )

    if not rows:
        return pd.DataFrame(columns=[id_col, name_col, "search_query", "score"])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Winner selection
# ---------------------------------------------------------------------------

def pick_winners(
    long_df: pd.DataFrame,
    id_col: str,
    name_col: str,
    team_lookup: dict[str, dict],
) -> pd.DataFrame:
    """
    For each geography unit, pick the team with the highest score as
    dominant; keep the full ranking for runner-up and margin calculation.

    Returns one row per geography with:
      id_col, name_col, dominant_team, dominant_league, dominant_score,
      runner_up_team, runner_up_score, margin
    """
    long_df = long_df.dropna(subset=["score"]).copy()
    long_df["score"] = pd.to_numeric(long_df["score"], errors="coerce")
    long_df = long_df.dropna(subset=["score"])

    # Sort descending within each geography
    ranked = (
        long_df
        .sort_values("score", ascending=False)
        .groupby(id_col, sort=False)
    )

    output_rows = []
    for geo_id, group in ranked:
        group = group.reset_index(drop=True)
        if group.empty:
            continue

        top = group.iloc[0]
        top_info = team_lookup.get(top["search_query"], {})

        runner_up_team  = ""
        runner_up_score = float("nan")
        if len(group) > 1:
            ru = group.iloc[1]
            runner_up_team  = team_lookup.get(ru["search_query"], {}).get("team_name", ru["search_query"])
            runner_up_score = ru["score"]

        output_rows.append({
            id_col:              geo_id,
            name_col:            top[name_col],
            "dominant_team":     top_info.get("team_name", top["search_query"]),
            "dominant_league":   top_info.get("league", ""),
            "dominant_score":    top["score"],
            "runner_up_team":    runner_up_team,
            "runner_up_score":   runner_up_score,
            "margin":            top["score"] - runner_up_score if runner_up_score == runner_up_score else float("nan"),
        })

    return pd.DataFrame(output_rows)


# ---------------------------------------------------------------------------
# Geography name normalisation
# ---------------------------------------------------------------------------

_US_STATE_CODES: frozenset[str] = frozenset({
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc","pr","vi","gu","as","mp",
})

# Maps normalized BritCrit crosswalk DMA name → normalized pytrends geoName.
# Applied after _normalize_us_dma() so both keys and values are already
# lower-case and state-stripped.
_US_DMA_ALIASES: dict[str, str] = {
    "boston":                                    "boston-manchester",
    "cedar rapids-waterloo-dubuque":             "cedar rapids-waterloo-iowa city-dubuque",
    "fort smith-fay-sprngdl":                    "fort smith-fayetteville-springdale-rogers",
    "greenville-spartanburg-asheville":          "greenville-spartanburg-asheville-anderson",
    "harlingen-weslaco-brownsville":             "harlingen-weslaco-brownsville-mcallen",
    "huntsville-decatur-florence":               "huntsville-decatur",
    "lincoln-hastings-kearney plus":             "lincoln-hastings-kearney",
    "myrtle beach-florence":                     "florence-myrtle beach",
    "paducah-cape girardeau-harrisburg":         "paducah-cape girardeau-harrisburg-mount vernon",
    "santa barbara-san mar-san luis obispo":     "santa barbara-santa maria-san luis obispo",
    "tampa-saint petersburg-sarasota":           "tampa-saint petersburg",
    "tucson-sierra vista":                       "tucson",
    "washington-hagrstwn":                       "washington",
    "wichita-hutchinson plus":                   "wichita-hutchinson",
    "yakima-pasco-rchlnd-knnwck":                "yakima-pasco-richland-kennewick",
}


def _normalize_us_dma(name: str) -> str:
    """
    Canonical form for US DMA names used on BOTH pytrends keys and crosswalk
    dma_name so they can be matched reliably.

    Handles: trailing/embedded state codes ('TX', ', MT', 'VT-Plattsburgh NY'),
    parenthetical sub-markets ('(Canton)', '(Hagerstown MD)'), common
    abbreviations ('Ft.' → 'Fort', 'St.' → 'Saint'), ampersand separators
    ('Hartford & New Haven CT').

    Order matters: strip states BEFORE expanding abbreviations so that the MT
    state code (Montana) is removed before 'Mt.' expansion runs.
    """
    s = name.strip()
    # Remove parenthetical content (sub-markets, alternate names).
    # \s* before '(' is intentional: handles both 'Foo (Bar)' and 'Foo(Bar)'.
    s = re.sub(r"\s*\([^)]*\)", "", s)
    # Normalise separators
    s = re.sub(r"\s*&\s*", "-", s)
    s = re.sub(r"\s*-\s*", "-", s)
    # Lowercase before state stripping so codes match regardless of input case
    s = s.lower()
    # Strip state codes iteratively (multi-state markets need several passes)
    for _ in range(5):
        prev = s
        # " tx" / " ca" before hyphen or at string end
        s = re.sub(
            r" ([a-z]{2})(?=-|$)",
            lambda m: "" if m.group(1) in _US_STATE_CODES else m.group(0),
            s,
        )
        # ", tx" at end or before hyphen
        s = re.sub(
            r",\s*([a-z]{2})(?=-|$)",
            lambda m: "" if m.group(1) in _US_STATE_CODES else m.group(0),
            s,
        )
        # "-tx" at string end (residual from multi-state stripping)
        s = re.sub(
            r"-([a-z]{2})$",
            lambda m: "" if m.group(1) in _US_STATE_CODES else m.group(0),
            s,
        )
        s = re.sub(r"-{2,}", "-", s).strip("-").strip()
        if s == prev:
            break
    # Clean up comma artifacts left after state stripping
    # e.g. "albany, ga" → " ga" stripped → "albany,"
    # e.g. "washington, dc-hagrstwn" → " dc" stripped → "washington,-hagrstwn"
    s = re.sub(r",\s*-", "-", s)
    s = s.strip(",").strip()
    # Expand abbreviations AFTER state stripping so 'mt' (Montana) is already
    # gone and only genuine 'Mt.' abbreviations (rare in DMA names) remain.
    # \b...\b with optional trailing period: "ft." → "fort", "ft " → "fort".
    s = re.sub(r"\bft\b\.?", "fort", s)
    s = re.sub(r"\bst\b\.?", "saint", s)
    s = re.sub(r"\bmt\b\.?", "mount", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


# Pytrends CA province name → crosswalk province column value
_CA_PROVINCE_ALIASES: dict[str, str] = {
    "quebec":          "Quebec",   # pytrends returns "Québec"; after accent-strip → "quebec"
    "yukon territory": "Yukon",
}


def _normalise_ca_province(name: str) -> str:
    key = _strip_accents(name).strip().lower()
    return _CA_PROVINCE_ALIASES.get(key, name.strip())


def _normalise_geo_scores(
    geo_scores: dict[str, dict[str, float]],
    normalise_fn,
) -> dict[str, dict[str, float]]:
    """Re-key geo_scores using normalise_fn; merge if two keys collapse."""
    out: dict[str, dict[str, float]] = {}
    for key, scores in geo_scores.items():
        nkey = normalise_fn(key)
        if nkey in out:
            # Average the two entries
            for q, v in scores.items():
                if q in out[nkey]:
                    out[nkey][q] = (out[nkey][q] + v) / 2
                else:
                    out[nkey][q] = v
        else:
            out[nkey] = dict(scores)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    teams        = pd.read_csv(TEAMS_CSV, dtype=str).fillna("")
    team_lookup  = _build_team_lookup(teams)

    anchor_query = teams[teams["team_id"] == ANCHOR_ID].iloc[0]["search_query"]

    us_crosswalk = pd.read_csv(US_CROSSWALK, dtype=str)
    ca_crosswalk = pd.read_csv(CA_CROSSWALK, dtype=str)

    # -----------------------------------------------------------------------
    # Aggregate normalized scores across all batches
    # geo_scores: {geo_name: {search_query: [score, ...]}}  (list for averaging)
    # -----------------------------------------------------------------------
    def aggregate_batches(geo: str) -> dict[str, dict[str, list[float]]]:
        batches = load_batches(geo)
        agg: dict[str, dict[str, list[float]]] = {}
        for batch in batches:
            norm_df = normalize_anchor(batch, anchor_query)
            if norm_df.empty:
                continue
            for geo_name, row in norm_df.iterrows():
                if geo_name not in agg:
                    agg[geo_name] = {}
                for query, val in row.items():
                    if pd.isna(val):
                        continue
                    agg[geo_name].setdefault(query, []).append(val)
        # Average across batches (handles any accidental duplicate team appearances)
        return {
            geo_name: {q: sum(vals) / len(vals) for q, vals in queries.items()}
            for geo_name, queries in agg.items()
        }

    log.info("Aggregating US batches …")
    us_geo_scores_raw = aggregate_batches("US")
    # Normalize pytrends geoNames (strip states, parens, expand Ft./St.)
    us_geo_scores = _normalise_geo_scores(us_geo_scores_raw, _normalize_us_dma)

    log.info("Aggregating CA batches …")
    ca_geo_scores_raw = aggregate_batches("CA")
    # Pytrends returns full province names; normalise accents/aliases
    ca_geo_scores = _normalise_geo_scores(ca_geo_scores_raw, _normalise_ca_province)

    # -----------------------------------------------------------------------
    # Normalise crosswalk join keys to match pytrends format
    # -----------------------------------------------------------------------
    us_crosswalk = us_crosswalk.copy()
    # Normalize crosswalk DMA names with the same function, then apply explicit
    # aliases for markets where names differ beyond what normalization handles.
    us_crosswalk["dma_name"] = (
        us_crosswalk["dma_name"]
        .apply(_normalize_us_dma)
        .apply(lambda x: _US_DMA_ALIASES.get(x, x))
    )

    # For Canada, join on the 'province' column (full English name, e.g. "Alberta")
    # rather than trends_geo ISO code, since that's what pytrends returns.
    ca_crosswalk = ca_crosswalk.copy()

    # -----------------------------------------------------------------------
    # Expand to county / division level
    # -----------------------------------------------------------------------
    log.info("Expanding US scores to county level …")
    us_long = expand_to_geography(
        us_geo_scores, us_crosswalk,
        geo_col="dma_name", id_col="county_fips", name_col="county_name",
    )

    log.info("Expanding CA scores to division level …")
    ca_long = expand_to_geography(
        ca_geo_scores, ca_crosswalk,
        geo_col="province", id_col="division_id", name_col="division_name",
    )

    # -----------------------------------------------------------------------
    # Pick winners
    # -----------------------------------------------------------------------
    log.info("Picking winners …")
    us_winners = pick_winners(us_long, "county_fips", "county_name", team_lookup)
    ca_winners = pick_winners(ca_long, "division_id", "division_name", team_lookup)

    # Normalise column names for unified output
    ca_winners = ca_winners.rename(columns={
        "division_id":   "county_fips_or_division_id",
        "division_name": "county_name",
    })
    us_winners = us_winners.rename(columns={
        "county_fips": "county_fips_or_division_id",
    })

    combined = pd.concat([us_winners, ca_winners], ignore_index=True)

    out_cols = [
        "county_fips_or_division_id", "county_name",
        "dominant_team", "dominant_league", "dominant_score",
        "runner_up_team", "runner_up_score", "margin",
    ]
    missing_cols = [c for c in out_cols if c not in combined.columns]
    if missing_cols:
        raise RuntimeError(
            f"Output DataFrame missing columns {missing_cols}. "
            f"Present: {combined.columns.tolist()}"
        )
    combined[out_cols].to_csv(SCORES_OUTPUT, index=False)

    log.info(
        f"Wrote {len(combined)} rows "
        f"({len(us_winners)} US counties, {len(ca_winners)} CA divisions) "
        f"→ {SCORES_OUTPUT.name}"
    )

    # -----------------------------------------------------------------------
    # Balanced (location-quotient) scoring
    # Divide each team's DMA score by its national mean so the winner is the
    # team most CONCENTRATED here relative to its own nationwide baseline.
    # -----------------------------------------------------------------------
    log.info("Computing balanced (location-quotient) scores …")
    us_geo_lq = location_quotient(us_geo_scores)
    ca_geo_lq = location_quotient(ca_geo_scores)

    log.info("Expanding balanced US scores to county level …")
    us_long_bal = expand_to_geography(
        us_geo_lq, us_crosswalk,
        geo_col="dma_name", id_col="county_fips", name_col="county_name",
    )
    log.info("Expanding balanced CA scores to division level …")
    ca_long_bal = expand_to_geography(
        ca_geo_lq, ca_crosswalk,
        geo_col="province", id_col="division_id", name_col="division_name",
    )

    log.info("Picking balanced winners …")
    us_winners_bal = pick_winners(us_long_bal, "county_fips", "county_name", team_lookup)
    ca_winners_bal = pick_winners(ca_long_bal, "division_id", "division_name", team_lookup)

    ca_winners_bal = ca_winners_bal.rename(columns={
        "division_id":   "county_fips_or_division_id",
        "division_name": "county_name",
    })
    us_winners_bal = us_winners_bal.rename(columns={
        "county_fips": "county_fips_or_division_id",
    })

    combined_bal = pd.concat([us_winners_bal, ca_winners_bal], ignore_index=True)
    combined_bal[out_cols].to_csv(SCORES_BALANCED_OUTPUT, index=False)

    log.info(
        f"Wrote {len(combined_bal)} rows "
        f"({len(us_winners_bal)} US counties, {len(ca_winners_bal)} CA divisions) "
        f"→ {SCORES_BALANCED_OUTPUT.name}"
    )


if __name__ == "__main__":
    main()
