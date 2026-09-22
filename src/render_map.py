"""
Step 6 – Render the fandom map as an interactive HTML file.

Reads output/map.geojson, simplifies geometries for browser performance,
and writes output/map.html — a self-contained Leaflet choropleth coloured
by dominant league with per-county tooltips.
"""

import logging
from pathlib import Path

import folium
from folium.features import GeoJsonTooltip
import geopandas as gpd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROOT       = Path(__file__).parent.parent
OUTPUT_DIR = ROOT / "output"

MAP_GEOJSON = OUTPUT_DIR / "map.geojson"
MAP_HTML    = OUTPUT_DIR / "map.html"

# One colour per league.  Unscored / unknown → grey.
LEAGUE_COLORS: dict[str, str] = {
    "NFL":                "#003f88",   # navy
    "College Football":   "#c8102e",   # crimson
    "College Basketball": "#e87722",   # orange
    "NHL":                "#006847",   # dark green
}
NO_DATA_COLOR = "#cccccc"


def main() -> None:
    if not MAP_GEOJSON.exists():
        raise FileNotFoundError(f"{MAP_GEOJSON} not found — run build_map.py first.")

    log.info("Reading map.geojson …")
    import os
    os.environ["OGR_GEOJSON_MAX_OBJ_SIZE"] = "0"   # remove size limit
    gdf = gpd.read_file(MAP_GEOJSON)
    log.info(f"  {len(gdf)} features loaded")

    # Simplify to ~5 km tolerance for reasonable HTML size.
    # Reproject to an equal-area CRS so tolerance is in metres, then back to WGS84.
    log.info("Simplifying geometries …")
    gdf["geometry"] = (
        gdf.to_crs(epsg=5070)
           .simplify(tolerance=5000, preserve_topology=True)
           .to_crs(epsg=4326)
    )

    log.info("Building map …")
    m = folium.Map(
        location=[47, -96],
        zoom_start=4,
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        prefer_canvas=True,
    )

    def _style(feature: dict) -> dict:
        league = (feature["properties"].get("dominant_league") or "").strip()
        return {
            "fillColor":   LEAGUE_COLORS.get(league, NO_DATA_COLOR),
            "color":       "#ffffff",
            "weight":      0.4,
            "fillOpacity": 0.75,
        }

    def _highlight(feature: dict) -> dict:
        return {"weight": 1.5, "color": "#333333", "fillOpacity": 0.9}

    folium.GeoJson(
        gdf.__geo_interface__,
        style_function=_style,
        highlight_function=_highlight,
        tooltip=GeoJsonTooltip(
            fields=[
                "geo_name", "dominant_team", "dominant_league",
                "dominant_score", "runner_up_team", "runner_up_score", "margin",
            ],
            aliases=[
                "Area", "Dominant team", "League",
                "Score", "Runner-up", "Runner-up score", "Margin",
            ],
            localize=True,
            sticky=True,
            style=(
                "background-color: white; color: #333; font-family: sans-serif; "
                "font-size: 12px; padding: 8px 10px; border-radius: 6px; "
                "box-shadow: 0 2px 6px rgba(0,0,0,.25);"
            ),
        ),
    ).add_to(m)

    # ── Legend ──────────────────────────────────────────────────────────────
    legend_items = "".join(
        f'<div style="margin:4px 0">'
        f'<span style="display:inline-block;width:14px;height:14px;'
        f'background:{color};border-radius:3px;margin-right:6px;'
        f'vertical-align:middle"></span>{league}</div>'
        for league, color in LEAGUE_COLORS.items()
    )
    legend_items += (
        f'<div style="margin:4px 0">'
        f'<span style="display:inline-block;width:14px;height:14px;'
        f'background:{NO_DATA_COLOR};border-radius:3px;margin-right:6px;'
        f'vertical-align:middle"></span>No data</div>'
    )
    legend_html = f"""
    <div style="position:fixed;bottom:30px;left:30px;z-index:1000;
                background:white;padding:12px 16px;border-radius:8px;
                box-shadow:0 2px 8px rgba(0,0,0,.3);
                font-family:sans-serif;font-size:13px;line-height:1.6">
      <b style="font-size:14px">Dominant league</b><br>
      {legend_items}
      <div style="margin-top:8px;font-size:11px;color:#777">
        Hover a county/division for details
      </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    # ── Title ────────────────────────────────────────────────────────────────
    title_html = """
    <div style="position:fixed;top:16px;left:50%;transform:translateX(-50%);
                z-index:1000;background:white;padding:8px 20px;border-radius:8px;
                box-shadow:0 2px 8px rgba(0,0,0,.25);
                font-family:sans-serif;font-size:16px;font-weight:600;
                white-space:nowrap">
      US + Canada Sports Fandom Map
      <span style="font-weight:400;font-size:12px;color:#777;margin-left:8px">
        Google Trends · 5-year interest · unbalanced scoring
      </span>
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))

    m.save(str(MAP_HTML))
    log.info(f"Wrote → {MAP_HTML}")
    log.info(f"HTML size: {MAP_HTML.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
