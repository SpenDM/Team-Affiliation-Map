# Sports Fandom Map — Pipeline Spec

## Goal

Build a US + Canada map showing the "dominant" sports team by county (US) /
census division (Canada), using Google Trends search interest as the fandom
proxy. This first pass uses **unbalanced** scoring (raw rescaled interest,
no adjustment for league size) — a league-balancing variant comes later as
a second scoring function, not a rewrite.

Input file `teams.csv` is provided separately (229 teams across NFL, NBA,
NHL, MLB, College Football, College Basketball). Columns:

```
team_id, team_name, league, market, search_query, country, google_entity_id
```

`google_entity_id` starts blank — the fetch step resolves and caches it.

---

## Repo structure

```
sports-fandom-map/
  data/
    teams.csv                    # provided, checked into repo
    dma_county_crosswalk.csv     # built in Step 1
    canada_division_market.csv   # built in Step 1
    raw/                         # cached Trends API responses, one JSON per batch
  src/
    resolve_entities.py          # Step 2
    fetch_trends.py              # Step 3
    score.py                     # Step 4
    build_map.py                 # Step 5
  output/
    scores_unbalanced.csv
    map.geojson
  README.md
```

Nothing downstream should ever force a re-fetch of already-cached data.
Every network-calling script must check `data/raw/` (or the relevant cache
file) before hitting the API, and must be safely re-runnable after a crash.

---

## Step 1 — Geography crosswalk

**US:** Every county belongs to exactly one Nielsen DMA. Find a public
county→DMA crosswalk (search for "Nielsen DMA county crosswalk csv" —
several are floating around from academic/journalism projects; there is no
single official free file, so document wherever you source it in the
README, including its as-of date). Output `dma_county_crosswalk.csv` with
columns: `county_fips, county_name, state, dma_name, dma_code`.

**Canada:** Google Trends geographic resolution for Canada is coarser
(province, and some city-level "metro" breakdowns). Build
`canada_division_market.csv` mapping each census division to its nearest
reported Trends geo (province code at minimum; metro where available).
Columns: `division_id, division_name, province, trends_geo`.

Flag any county/division you can't confidently map rather than guessing —
write it to `data/unmapped_geographies.csv` for manual review later. Don't
silently drop it.

---

## Step 2 — Resolve Google entity IDs

`resolve_entities.py`:

- For each row in `teams.csv` with a blank `google_entity_id`, call
  pytrends' `TrendReq().suggestions(keyword=search_query)`.
- Take the top result whose type matches (sports team / athlete /
  organization — pytrends returns a `type` field per suggestion).
- If no confident match, log it to `data/unresolved_entities.csv` with the
  raw suggestions returned, for manual resolution — **do not** guess an ID.
- Write resolved IDs back into `teams.csv` in place (this file becomes the
  cache; re-running the script should skip rows that already have an ID).
- Rate-limit these calls too (pytrends will 429 if hit too fast) — reuse
  the backoff helper from Step 3 rather than duplicating it.

This step runs once, not per scoring pass.

---

## Step 3 — Fetch Trends interest by geography

`fetch_trends.py`

**The core constraint:** `pytrends.build_payload()` compares at most 5
terms at once, and returns *relative* interest (0–100) scaled within that
batch only — values aren't comparable across separate API calls. To make
~229 teams comparable, every batch must include one shared **anchor team**
(pick something with broad, stable, non-seasonal interest — e.g. Dallas
Cowboys) alongside 4 target teams. After all batches are pulled, divide
every team's raw values by that batch's anchor value to get an
anchor-relative score, which *is* comparable across batches.

Batching:
- Group the 229 teams into batches of 4 + anchor = ~58 batches.
- For US: pull with `geo='US'`, `resolution='DMA'` (via
  `interest_by_region`), 5-year timeframe (`today 5-y`) to smooth
  championship-run spikes.
- For Canada: separate batch set with `geo='CA'`, province-level
  resolution (Trends doesn't offer DMA-equivalent granularity for Canada).
- Cache every batch's raw JSON response to `data/raw/batch_{n}.json`
  immediately after fetching, before any processing. If the script is
  re-run, skip batches whose cache file already exists.

Rate limiting: pytrends is unofficial and will throttle/block on
sustained hammering. Implement exponential backoff (start ~2s, cap
~60s, retry ~5x) on 429s, and add a flat ~1–2s sleep between batches
regardless. Expect this step to take a while wall-clock — that's expected
and fine given the caching.

Output: leave raw batches in `data/raw/`; no separate consolidated file
needed yet (that's Step 4's job).

---

## Step 4 — Score (unbalanced, v1)

`score.py`

- Load every cached batch from `data/raw/`.
- For each team, compute anchor-relative interest per geography (batch
  raw value ÷ that batch's anchor value).
- Where a team appears in multiple batches (shouldn't happen if batching
  is clean, but guard for it) average the anchor-relative values.
- Join to `dma_county_crosswalk.csv` / `canada_division_market.csv` to
  expand DMA/province-level scores out to every county/division that maps
  to it.
- For each county/division, pick the team with the highest anchor-relative
  score as the "dominant team." Also keep the full ranked list per
  county, not just the winner — the map step may want runner-up margin
  (e.g. to show confidence/how close the result was) and the next scoring
  variant will need to re-rank the same data.
- Write `output/scores_unbalanced.csv`:
  ```
  county_fips_or_division_id, county_name, dominant_team, dominant_league,
  dominant_score, runner_up_team, runner_up_score, margin
  ```

**Design note for the balancing variant coming later:** structure this so
the "divide by anchor" step and the "pick winner per county" step are
separate functions. The balanced version only changes what gets divided
by what (team score ÷ league national total, instead of raw anchor
scaling) — it should reuse the same county-expansion and winner-picking
logic, not duplicate it. Write `score.py` with that separation in mind now
even though only the unbalanced path is being built first.

---

## Step 5 — Build the map

`build_map.py`

- Load a US counties GeoJSON/TopoJSON (Census TIGER/Line cartographic
  boundary files, simplified resolution is fine) and a Canada census
  division boundary file (Statistics Canada).
- Join `scores_unbalanced.csv` onto the boundary geometries by FIPS/
  division ID.
- Output `output/map.geojson` with properties: `dominant_team`,
  `dominant_league`, `margin` (for opacity/confidence styling later),
  plus the raw scores for tooltip use.
- Leave actual rendering (colors per team/league, legend, interactivity)
  for a follow-up step — this script's job is just a clean joined
  GeoJSON ready to hand to a mapping library.

---

## README.md

Document: how to run each step in order, where the pytrends dependency
comes from and its known flakiness, the crosswalk source and its as-of
date, and a note that `data/raw/` is the source of truth — `score.py` and
`build_map.py` should be freely re-runnable/iterable without touching the
network.

---

## Explicitly out of scope for this pass

- League-balanced and sqrt-balanced scoring (next iteration, reusing
  `score.py`'s structure per the note in Step 4)
- Actual map rendering/styling
- MLS, and any college basketball program outside the 21 curated in
  `teams.csv`
