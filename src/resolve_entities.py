"""
Step 2 – Resolve Google entity IDs for every team in teams.csv.

For each row with a blank google_entity_id, calls pytrends suggestions
and takes the top result whose type looks like a sports team or org.
Unresolved teams are logged to data/unresolved_entities.csv — they are
NOT guessed.  Resolved IDs are written back into teams.csv in place.

Re-running skips rows that already have an entity ID.
Uses the same backoff helper that fetch_trends.py imports.
"""

import logging
import time
from pathlib import Path

import pandas as pd
from pytrends.request import TrendReq

from _backoff import with_backoff

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT       = Path(__file__).parent.parent
TEAMS_CSV  = ROOT / "data" / "teams.csv"
UNRESOLVED = ROOT / "data" / "unresolved_entities.csv"

# pytrends suggestions return a 'type' string; accept any result whose type
# contains one of these substrings.
ACCEPTED_TYPES = {
    "football team",
    "basketball team",
    "baseball team",
    "hockey team",
    "ice hockey",
    "sports team",
    "sports club",
    "american football",
    "baseball",       # pytrends returns this for MLB teams
    "college",
    "university",
    "organization",
}


def _type_matches(suggestion_type: str) -> bool:
    t = suggestion_type.lower()
    # "baseball" must be an exact type match; "baseball cap", "baseball park" etc.
    # are merchandise/venues and should not match.
    if t == "baseball":
        return True
    return any(kw in t for kw in ACCEPTED_TYPES if kw != "baseball")


def _suggestions(pytrends: TrendReq, keyword: str) -> list[dict]:
    def _call() -> list[dict]:
        return pytrends.suggestions(keyword=keyword)
    return with_backoff(_call)


def resolve_team(
    pytrends: TrendReq, row: pd.Series
) -> tuple[str | None, list[dict]]:
    """
    Return (entity_id_or_None, raw_suggestions_list).

    First tries the full search_query (e.g. "Dallas Cowboys NFL").
    If nothing matches on type, retries with the team_name only
    (e.g. "Dallas Cowboys") — league suffixes sometimes push event
    results ahead of the team entity.
    """
    full_query = row["search_query"]
    results = _suggestions(pytrends, full_query)
    for r in results:
        if _type_matches(r.get("type", "")):
            return r.get("mid", ""), results

    # Fallback: try team name without league suffix
    name_only = row["team_name"]
    if name_only != full_query:
        fallback = _suggestions(pytrends, name_only)
        for r in fallback:
            if _type_matches(r.get("type", "")):
                return r.get("mid", ""), fallback
            # Also accept a "Topic" result whose title exactly matches the team
            # name — pytrends sometimes classifies unambiguous team entities as Topic.
            if (
                r.get("type", "").lower() == "topic"
                and r.get("title", "").strip().lower() == name_only.strip().lower()
            ):
                return r.get("mid", ""), fallback
        results = fallback

    return None, results


def main(pytrends: TrendReq) -> None:
    teams = pd.read_csv(TEAMS_CSV, dtype=str).fillna("")

    unresolved_rows: list[dict] = []
    changed = False

    for idx, row in teams.iterrows():
        if row["google_entity_id"].strip():
            continue  # already resolved

        log.info(f"Resolving {row['team_id']} {row['team_name']} …")
        entity_id, raw = resolve_team(pytrends, row)

        if entity_id:
            teams.at[idx, "google_entity_id"] = entity_id
            log.info(f"  → {entity_id}")
            changed = True
        else:
            log.warning(f"  → no confident match for {row['search_query']!r}")
            unresolved_rows.append({
                "team_id":          row["team_id"],
                "team_name":        row["team_name"],
                "search_query":     row["search_query"],
                "raw_suggestions":  str(raw),
            })

        time.sleep(1)  # light throttle between calls

    if changed:
        teams.to_csv(TEAMS_CSV, index=False)
        log.info(f"Updated {TEAMS_CSV.name}")

    if unresolved_rows:
        df_unres = pd.DataFrame(unresolved_rows)
        mode   = "a" if UNRESOLVED.exists() else "w"
        header = not UNRESOLVED.exists()
        df_unres.to_csv(UNRESOLVED, mode=mode, header=header, index=False)
        log.warning(f"{len(unresolved_rows)} teams unresolved → {UNRESOLVED.name}")

    log.info("Done.")


if __name__ == "__main__":
    pt = TrendReq(hl="en-US", tz=360, retries=3, backoff_factor=2)
    main(pt)
