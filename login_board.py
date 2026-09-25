"""BlueMet login - the network board.

Target path: login_board.py (repo root, beside auth.py)

auth.py owns the password check and the st.stop(); this module only
draws. Layout: wordmark and clock, twelve station chips, the colour
key, then sign-in on the left with the fixed-view map on the right.

The map does NOT pan or zoom. It shows the lower 48 with MRMS
reflectivity and the network stations as small blue dots. Everything
it draws is either already on disk (the MRMS chunks the warmer wrote)
or a constant, so the login page starts no fetches of its own except
the twelve METARs behind a five-minute cache.

Fleet: live JetBlue aircraft from core.fleet - the same sweep page 3
draws, kept running by the fleet warmer - with 2-minute trails and NO
flight numbers, labels or hover. The login page is public.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import pydeck as pdk
import streamlit as st

import dark_theme as T

try:
    from core import station_status as S
except ImportError:          # repo-root copy, if it ever moves back
    import station_status as S

# ---------------------------------------------------------------- view

# Fixed extent, matched to the CONUS map's usual framing. Nothing here
# is user-adjustable by design.
VIEW_LAT = float(os.environ.get("BLUEMET_LOGIN_LAT", "36.8"))
VIEW_LON = float(os.environ.get("BLUEMET_LOGIN_LON", "-95.5"))
# 3.3 fills the larger map with the lower 48. Raise it on a very large
# display, lower it if the coasts are clipped.
VIEW_ZOOM = float(os.environ.get("BLUEMET_LOGIN_ZOOM", "3.3"))

# Map height: 75% of the window height left below the board, never
# below MAP_MIN_PX. MAP_TOP_PX is roughly what sits above the map
# (wordmark, chips, rules, page padding).
MAP_FILL = float(os.environ.get("BLUEMET_LOGIN_MAP_FILL", "0.75"))
MAP_TOP_PX = int(os.environ.get("BLUEMET_LOGIN_MAP_TOP_PX", "560"))
MAP_MIN_PX = int(os.environ.get("BLUEMET_LOGIN_MAP_MIN_PX", "380"))

# SCALE (23 Sep). The layout as drawn at SCALE_REF_PX wide is the
# minimum; on a wider window every size grows in proportion to the
# window width. _s(px) is that rule for one size: px at or below the
# reference width, px * width / SCALE_REF_PX above it. Font sizes are
# passed as --fs:<size> and applied by one rule in render().
SCALE_REF_PX = int(os.environ.get("BLUEMET_LOGIN_SCALE_REF_PX", "1440"))


def _s(px: float) -> str:
    return f"max({px:g}px,{px * 100.0 / SCALE_REF_PX:.4f}vw)"


MAP_STYLE = os.environ.get(
    "BLUEMET_MAP_STYLE",
    "https://basemaps.cartocdn.com/gl/"
    "dark-matter-nolabels-gl-style/style.json")

# FLEET. 2-minute trails, the same default as the CONUS map.
TRAIL_S = float(os.environ.get("BLUEMET_LOGIN_TRAIL_S", "120"))

def _rgb(h: str) -> list[int]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


# ------------------------------------------------------------- data

@st.cache_data(ttl=300, show_spinner=False)
def _metars() -> dict:
    """Latest raw METAR per board station. Best effort: on any failure
    the chips render grey, which reads as 'no data', not as 'fine'."""
    icaos = [f"K{c}" for c in S.LOGIN_STATIONS]
    try:
        from core.metar import fetch_metars
        by_station = fetch_metars(icaos, hours_back=2)
    except Exception:
        return {}
    out = {}
    for icao, obs in (by_station or {}).items():
        if not obs:
            continue
        raw = getattr(obs[-1], "raw_text", None)
        if raw:
            # Key by the 3-letter code the board displays.
            out[icao.upper()[-3:]] = raw
    return out


def _origin() -> str:
    """The origin the browser is actually on.

    Same reasoning as the CONUS map: a chunk served from a different
    origin is cross-origin, and WebGL refuses a cross-origin image as
    a texture - the layer draws nothing, with no error.
    """
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


def _mrms_layers() -> tuple[list, str]:
    """BitmapLayer per warmed MRMS chunk, newest complete scan."""
    base = _origin()
    if not base:
        return [], ""
    try:
        from core import mrms as _MR
        static_dir = Path(__file__).resolve().parent / "static"
        chunks, stamp = _MR.newest(static_dir, "REFL")
    except Exception:
        return [], ""
    if not chunks:
        return [], ""

    st.markdown("".join(
        f"<link rel='preload' as='image' "
        f"href='{base}/app/static/{c['name']}'>" for c in chunks),
        unsafe_allow_html=True)

    return [pdk.Layer("BitmapLayer", data=None,
                      image=f"{base}/app/static/{c['name']}",
                      bounds=c["bounds"], opacity=1.0)
            for c in chunks], (stamp or "")


def _station_layers(statuses) -> list:
    """Small blue dots with small labels, and a hover that says why
    the station is the colour it is on the board above.

    SIZING: get_radius / get_size are in METRES with a pixel clamp,
    not radius_units="pixels". The units props are ignored by the
    pydeck build running here - that is what drew continent-sized
    dots and labels - and the clamp is how pages/3 already does it.
    """
    blue = _rgb(T.JBU_BLUE)
    why = {s.icao: s for s in statuses}
    pts = []
    for code, (lat, lon) in S.STATION_LATLON.items():
        if code not in S.LOGIN_STATIONS:
            continue
        st_ = why.get(code)
        pts.append({
            "position": [lon, lat],
            "name": code,
            "reason": (st_.reason if st_ else "no observation"),
            "level": (st_.name if st_ else "none"),
        })
    return [
        pdk.Layer("ScatterplotLayer", pts, get_position="position",
                  get_fill_color=blue + [255],
                  get_line_color=[0, 0, 0, 255], stroked=True,
                  line_width_min_pixels=1,
                  get_radius=9000,
                  radius_min_pixels=3, radius_max_pixels=5,
                  pickable=True, auto_highlight=True),
        pdk.Layer("TextLayer", pts, get_position="position",
                  get_text="name", get_color=blue + [255],
                  get_size=2600, size_min_pixels=0, size_max_pixels=11,
                  get_pixel_offset=[10, -7],
                  get_text_anchor='"start"',
                  get_alignment_baseline='"center"',
                  pickable=False),
    ]


def _plane_uri(fill: str) -> str:
    """The CONUS map's A320 silhouette, as a data URI."""
    import urllib.parse
    body = ("M0,-10 L0.35,-9.6 L0.55,-8.8 L0.6,-6 L0.6,-1.6 "
            "L9.2,3.2 L9.6,3.4 L9.6,4 L9.1,4.1 L2.6,3.3 "
            "L0.6,3.1 L0.6,6.4 L3.3,8.2 L3.3,9 L0.5,8.5 "
            "L0.45,9.4 L0,9.7 L-0.45,9.4 L-0.5,8.5 L-3.3,9 "
            "L-3.3,8.2 L-0.6,6.4 L-0.6,3.1 L-2.6,3.3 "
            "L-9.1,4.1 L-9.6,4 L-9.6,3.4 L-9.2,3.2 L-0.6,-1.6 "
            "L-0.6,-6 L-0.55,-8.8 L-0.35,-9.6 Z")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" '
           'height="64" viewBox="-11 -11 22 22">'
           f'<path d="{body}" fill="{fill}" stroke="#000000" '
           'stroke-width="0.6"/></svg>')
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)


def _fleet_layers() -> list:
    """JetBlue aircraft and their trails. Positions only: no callsign
    is read, drawn or put in a tooltip."""
    try:
        from core import fleet as F
        fleet = F.login_aircraft(trail_s=TRAIL_S)
    except Exception:
        return []
    if not fleet:
        return []
    blue = _rgb(T.JBU_BLUE)
    icon = {"url": _plane_uri(T.JBU_BLUE), "width": 64, "height": 64,
            "anchorX": 32, "anchorY": 32, "mask": False}
    trails = [{"path": a["trail"]} for a in fleet
              if len(a.get("trail") or ()) > 1]
    planes = [{"position": [a["lon"], a["lat"]], "angle": a["angle"],
               "icon": icon} for a in fleet]
    out = []
    if trails:
        # Metres with a pixel clamp; width_units is ignored here.
        out.append(pdk.Layer("PathLayer", trails, get_path="path",
                             get_color=blue + [170], get_width=4000,
                             width_min_pixels=1.5, width_max_pixels=2,
                             joint_rounded=True, cap_rounded=True,
                             pickable=False))
    out.append(pdk.Layer("IconLayer", planes, get_position="position",
                         get_icon="icon", get_angle="angle",
                         get_size=14, size_min_pixels=10,
                         size_max_pixels=16, pickable=False))
    return out

# ARTCC boundaries and jet routes: bundled files in static/, read once
# per process. Nothing here fetches.
_STATIC = Path(__file__).resolve().parent / "static"
_AIRSPACE: dict = {}


def _airspace():
    """{"artcc": [{"id", "path"}], "labels": [{"id","lon","lat"}],
    "routes": [{"id","path"}]} from static/artcc_high.json and
    static/map_routes.geojson."""
    if _AIRSPACE:
        return _AIRSPACE
    import json
    out = {"artcc": [], "labels": [], "routes": []}
    try:
        a = json.loads((_STATIC / "artcc_high.json").read_text())
        best = {}
        for c in a.get("centers", []):
            poly = c.get("polygon") or []
            if len(poly) < 3:
                continue
            path = [[float(x), float(y)] for x, y in poly]
            if path[0] != path[-1]:
                path.append(path[0])
            out["artcc"].append({"id": c["ident"], "path": path})
            # Label on the largest piece of each centre, at the middle
            # of its bounding box (ZNY has two pieces).
            if c["ident"] not in best or len(poly) > best[c["ident"]][2]:
                xs = [p[0] for p in path]
                ys = [p[1] for p in path]
                best[c["ident"]] = ((min(xs) + max(xs)) / 2,
                                    (min(ys) + max(ys)) / 2, len(poly))
        out["labels"] = [{"id": k, "position": [v[0], v[1]]}
                         for k, v in best.items()]
    except Exception:
        pass
    try:
        r = json.loads((_STATIC / "map_routes.geojson").read_text())
        for f in r.get("features", []):
            g = f.get("geometry") or {}
            if g.get("type") == "LineString" and len(g["coordinates"]) > 1:
                out["routes"].append(
                    {"id": (f.get("properties") or {}).get("ident", ""),
                     "path": [[float(x), float(y)]
                              for x, y in g["coordinates"]]})
    except Exception:
        pass
    _AIRSPACE.update(out)
    return _AIRSPACE


# SPC Day 1 categorical outlook, as a fill per ARTCC. SPC's own
# colours; a centre is filled with the highest risk that covers at
# least SPC_MIN_FRACTION of its area. Fetched from SPC every 10 min
# (a 6-100 KB GeoJSON); the area sums are a few shapely intersections.
SPC_URL = "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.lyr.geojson"
SPC_MIN_FRACTION = float(os.environ.get("BLUEMET_SPC_MIN_FRACTION", "0.20"))
SPC_RANK = {"TSTM": 1, "MRGL": 2, "SLGT": 3, "ENH": 4, "MDT": 5, "HIGH": 6}
SPC_COLOR = {"TSTM": "#C1E9C1", "MRGL": "#66A366", "SLGT": "#FFE066",
             "ENH": "#FFA366", "MDT": "#E06666", "HIGH": "#EE99EE"}
# Legend, SPC's own: number in a coloured square, thunderstorms unnumbered.
SPC_LEGEND = [("HIGH", "5", "HIGH"), ("MDT", "4", "MDT"),
              ("ENH", "3", "ENH"), ("SLGT", "2", "Slight"),
              ("MRGL", "1", "MRGL"), ("TSTM", "", "TSTM")]
SPC_EDGE = {"TSTM": "#5BA85B", "MRGL": "#2F6B2F", "SLGT": "#C9A300",
            "ENH": "#C26A1B", "MDT": "#A31F1F", "HIGH": "#B84AB8"}


def _spc_badge_uri(label: str) -> str:
    """SPC's legend entry as one icon: the coloured square with the
    risk number in it (none for TSTM) and the word beside it. Drawn
    at 2x for crispness; 100 x 26 units."""
    import urllib.parse
    num = dict((k, n) for k, n, _ in SPC_LEGEND)[label]
    word = dict((k, w) for k, _, w in SPC_LEGEND)[label]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="52" '
           'viewBox="0 0 100 26">'
           f'<rect x="1.5" y="2.5" width="21" height="21" rx="2.5" '
           f'fill="{SPC_COLOR[label]}" stroke="{SPC_EDGE[label]}" '
           'stroke-width="2"/>'
           + (f'<text x="12" y="18.5" text-anchor="middle" font-size="14" '
              'font-family="Arial, Helvetica, sans-serif" font-weight="700" '
              f'fill="#000000">{num}</text>' if num else "")
           + f'<text x="28" y="18.5" font-size="13.5" font-weight="700" '
             'font-family="Arial, Helvetica, sans-serif" fill="#FFFFFF" '
             'stroke="#000000" stroke-width="1.6" paint-order="stroke">'
             f'{word}</text></svg>')
    return "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)


def spc_legend_html() -> str:
    """The SPC categorical legend, one row under the map: squares
    with the risk number and the word, TSTM first as SPC lays it out."""
    cells = []
    for key, num, word in reversed(SPC_LEGEND):
        cells.append(
            f'<span style="display:inline-flex;align-items:center;gap:6px;'
            f'margin-right:18px"><span style="display:inline-flex;'
            f'align-items:center;justify-content:center;width:18px;'
            f'height:18px;border-radius:3px;background:{SPC_COLOR[key]};'
            f'border:1.5px solid {SPC_EDGE[key]};color:#000;font:700 11px '
            f'Arial,Helvetica,sans-serif">{num}</span>'
            f'<span style="color:{T.TEXT};font-size:12px;font-weight:700">'
            f'{word}</span></span>')
    return (f'<div style="margin:6px 0 0 2px;color:{T.TEXT_2};font-size:12px;'
            f'font-weight:700">SPC Day 1 outlook, filled where a risk covers '
            f'&ge; {SPC_MIN_FRACTION:.0%} of a centre:&nbsp; '
            + "".join(cells) + "</div>")


@st.cache_data(ttl=600, show_spinner=False)
def _spc_by_artcc(bucket: str) -> dict:
    """{ARTCC ident: (risk label, fraction)} for centres whose highest
    qualifying risk covers >= SPC_MIN_FRACTION of their area."""
    import requests
    from shapely.geometry import shape, Polygon
    from shapely.ops import unary_union

    r = requests.get(SPC_URL, timeout=15, headers={"User-Agent": "bluemet.org"})
    r.raise_for_status()
    feats = r.json().get("features", [])
    # One geometry per risk label (SPC draws each category as its own
    # polygon set; higher risks sit inside lower ones).
    risk_geom = {}
    for f in feats:
        lab = (f.get("properties") or {}).get("LABEL")
        if lab not in SPC_RANK:
            continue
        g = shape(f["geometry"]).buffer(0)
        risk_geom[lab] = unary_union([risk_geom[lab], g]) if lab in risk_geom else g
    if not risk_geom:
        return {}
    out = {}
    A = _airspace()
    by_id = {}
    for c in A["artcc"]:
        poly = Polygon(c["path"]).buffer(0)
        by_id[c["id"]] = unary_union([by_id[c["id"]], poly]) if c["id"] in by_id else poly
    for ident, poly in by_id.items():
        area = poly.area
        if area <= 0:
            continue
        best = None
        for lab in sorted(risk_geom, key=lambda k: -SPC_RANK[k]):
            frac = poly.intersection(risk_geom[lab]).area / area
            if frac >= SPC_MIN_FRACTION:
                best = (lab, round(frac, 2))
                break
        if best:
            out[ident] = best
    return out


def _spc_fill_layers() -> list:
    """Filled ARTCC polygons coloured by SPC risk, under the outlines."""
    from datetime import datetime, timezone
    try:
        risks = _spc_by_artcc(datetime.now(timezone.utc).strftime("%Y%m%d%H%M")[:-1])
    except Exception:
        return []
    if not risks:
        return []
    A = _airspace()
    rows = []
    for c in A["artcc"]:
        hit = risks.get(c["id"])
        if not hit:
            continue
        rgb = _rgb(SPC_COLOR[hit[0]])
        rows.append({"polygon": c["path"], "fill": rgb + [95],
                     "tip": f"{c['id']}: SPC Day 1 {hit[0]} over "
                            f"{hit[1]:.0%} of the centre"})
    if not rows:
        return []
    out = [pdk.Layer("PolygonLayer", rows, get_polygon="polygon",
                     get_fill_color="fill", stroked=False, filled=True,
                     pickable=False)]
    # The words are drawn by _airspace_layers in the ident layer (one
    # TextLayer, two lines) - a second TextLayer on this deck did not
    # draw on the pydeck build in use.
    out.append(risks)
    return out


def _airspace_layers() -> list:
    """SPC risk fills, then thin grey ARTCC outlines with a small bold
    label at the centre of each - under the stations."""
    A = _airspace()
    _spc = _spc_fill_layers()
    layers = _spc[:1]                      # the fill, under everything
    risks = _spc[1] if len(_spc) > 1 else {}
    # One label per centre: the ident, and under it the SPC risk word
    # in bold white when the centre is filled.
    words = dict((k, w) for k, _, w in SPC_LEGEND)
    labels = []
    for lb in A["labels"]:
        hit = risks.get(lb["id"])
        labels.append({"position": lb["position"],
                       "text": (f"{lb['id']}\n{words[hit[0]]}" if hit
                                else lb["id"]),
                       "color": ([255, 255, 255, 255] if hit
                                 else [175, 185, 200, 230])})
    if A["artcc"]:
        layers.append(pdk.Layer(
            "PathLayer", A["artcc"], get_path="path",
            get_color=[120, 130, 145, 190], get_width=2500,
            width_min_pixels=1, width_max_pixels=1.2,
            pickable=False))
        layers.append(pdk.Layer(
            "TextLayer", labels, get_position="position",
            # Pinned at 12 px: with a zero floor this build let the
            # text shrink to specks at the CONUS zoom.
            get_text="text", get_size=2600, size_min_pixels=12,
            size_max_pixels=12, get_color="color", font_weight=700,
            get_text_anchor='"middle"',
            get_alignment_baseline='"center"',
            pickable=False))
    return layers


def _deck(layers) -> pdk.Deck:
    # controller=False freezes the view. min_zoom == max_zoom is the
    # second line of defence: if a pydeck version ignores the view's
    # controller flag, the view state still cannot change scale.
    return pdk.Deck(
        layers=layers,
        views=[pdk.View(type="MapView", controller=False)],
        initial_view_state=pdk.ViewState(
            latitude=VIEW_LAT, longitude=VIEW_LON, zoom=VIEW_ZOOM,
            min_zoom=VIEW_ZOOM, max_zoom=VIEW_ZOOM,
            bearing=0, pitch=0),
        map_style=MAP_STYLE,
        tooltip={
            "html": "<b>{name}</b><br/>{reason}",
            "style": {"backgroundColor": "#0A0A0A",
                      "color": "#FFFFFF",
                      "border": "1px solid #333333",
                      "fontSize": "12px",
                      "fontFamily": "DejaVu Sans Mono, monospace"},
        },
        parameters={"clearColor": [0, 0, 0, 1]},
    )


# -------------------------------------------------------------- chrome

def _chips(statuses) -> str:
    """Identifier, colour bar, and the ONE value that set the colour.
    The full reason is still on hover. Sizes follow the window (_s)."""
    cells = "".join(
        f'<div title="{s.icao}: {s.reason}" '
        f'style="flex:0 0 auto;min-width:{_s(92)};background:{T.PANEL};'
        f'border:1px solid {T.RULE};border-radius:3px;'
        f'padding:{_s(10)} {_s(12)};cursor:help">'
        f'<div style="color:{T.TEXT};--fs:{_s(17)};font-weight:700;'
        f'letter-spacing:.5px">{s.icao}</div>'
        f'<div style="height:{_s(6)};margin:{_s(10)} 0 {_s(6)} 0;'
        f'background:{s.color}"></div>'
        f'<div style="color:{s.color};-webkit-text-fill-color:{s.color};'
        f'--fs:{_s(12)};font-weight:700;white-space:nowrap">'
        f'{getattr(s, "label", "") or s.name.upper()}</div>'
        f'</div>'
        for s in statuses)
    return (f'<div style="display:flex;gap:{_s(9)};flex-wrap:wrap">'
            f'{cells}</div>')


def render() -> str | None:
    """Draw the board. Returns the typed password, or None."""
    T.apply_dark_theme()
    st.markdown(
        "<style>"
        '[data-testid="stSidebar"]{display:none}'
        # The retro stylesheet pins every div/span to 13px Times with
        # !important; the board's sizes are inline !important so they
        # win, and the face is Roboto like the rest of Ops Black.
        '.stApp p,.stApp div,.stApp span,.stApp label,.stApp input{'
        'font-family:Roboto,Arial,sans-serif !important}'
        # Sizes ride in a --fs custom property (inline !important is
        # dropped by the markdown renderer); this rule applies them
        # and outranks the retro 13px rule.
        '.stApp [style*="--fs"]{font-size:var(--fs) !important}'
        # ~16 characters wide, as the old login form was.
        f'[data-testid="stTextInput"]{{max-width:{_s(200)} !important;}}'
        f'[data-testid="stTextInput"] label p{{font-size:{_s(13)} !important;}}'
        f'[data-testid="stTextInput"] input{{font-size:{_s(16)} !important;'
        f'height:{_s(40)} !important;}}'
        '[data-testid="InputInstructions"]{display:none !important;}'
        # The eye toggle draws as the word "visibility" without the
        # icon font, which looks like stray text in the box.
        '[data-testid="stTextInput"] button{display:none !important;}'
        # MAP SIZE. pydeck_chart only takes a fixed pixel height, so
        # the chart and everything inside it are sized from the window
        # instead: 75% of the height left below the board, with a
        # floor. deck.gl follows its container's size.
        '[data-testid="stDeckGlJsonChart"],'
        '[data-testid="stDeckGlJsonChart"] > div,'
        '[data-testid="stDeckGlJsonChart"] #deckgl-wrapper,'
        '[data-testid="stDeckGlJsonChart"] .mapboxgl-map,'
        '[data-testid="stDeckGlJsonChart"] .maplibregl-map{'
        f"height:max({MAP_MIN_PX}px,calc((100vh - {_s(MAP_TOP_PX)}) * "
        f"{MAP_FILL})) !important;}}"
        "</style>", unsafe_allow_html=True)

    # Keeps the fleet current for the aircraft, when they are on.
    if os.environ.get("BLUEMET_LOGIN_FLEET", "off").lower() == "on":
        try:
            from core import fleet as _F
            _F.ensure_fleet_warmer()
            _F.kick()
        except Exception:
            pass

    now = _dt.datetime.now(_dt.timezone.utc)

    # "Login Page", centred at the top in big bold white (23 Sep).
    st.markdown(
        f'<div style="text-align:center;--fs:{_s(32)};font-weight:700;'
        f'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;line-height:1.1;'
        f'margin:0 0 {_s(10)} 0">Login Page</div>',
        unsafe_allow_html=True)

    st.markdown(
        f'<div style="--fs:{_s(56)};font-weight:700;color:{T.TEXT};'
        f'letter-spacing:2px;line-height:1.0">BLUEMET</div>'
        f'<div style="--fs:{_s(13)};color:{T.TEXT_2};margin-top:{_s(6)}">'
        f'JetBlue System Operations weather</div>',
        unsafe_allow_html=True)
    # Date and time, top right, twice the size of the small clock
    # every other page carries (24 Sep); that one is hidden here.
    try:
        from zoneinfo import ZoneInfo as _ZI
        _et = now.astimezone(_ZI("America/New_York"))
        _et_txt = f" &middot; {_et:%-I:%M %p} {_et:%Z}"
    except Exception:
        _et_txt = ""
    _d = now.day
    _sfx = ("th" if 11 <= _d % 100 <= 13
            else {1: "st", 2: "nd", 3: "rd"}.get(_d % 10, "th"))
    st.markdown(
        '<style>.stApp div[style*="999998"]{display:none !important}'
        '</style>'
        f'<div style="position:fixed;top:52px;right:22px;z-index:999999;'
        f'text-align:right;--fs:{_s(26)};font-weight:700;line-height:1.25;'
        f'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;white-space:nowrap">'
        f'{now:%H:%M}Z{_et_txt}<br>{now:%B} {_d}{_sfx}, {now:%Y}</div>',
        unsafe_allow_html=True)

    st.markdown(f'<hr style="border-color:{T.RULE};'
                f'margin:{_s(18)} 0 {_s(20)} 0">', unsafe_allow_html=True)

    statuses = S.board(S.LOGIN_STATIONS, _metars())
    st.markdown(
        f'<div style="color:{T.TEXT_2};--fs:{_s(12)};font-weight:700;'
        f'margin-bottom:{_s(8)}">Network conditions</div>' + _chips(statuses),
        unsafe_allow_html=True)

    st.markdown(f'<hr style="border-color:{T.RULE};'
                f'margin:{_s(24)} 0 {_s(8)} 0">',
                unsafe_allow_html=True)

    # Narrow sign-in column; the map takes the rest of the width.
    left, right = st.columns([1, 3.2], gap="large")

    with left:
        st.markdown(
            f'<div style="--fs:{_s(22)};font-weight:700;color:{T.TEXT};'
            f'margin-top:{_s(16)}">Sign in to continue</div>'
            f'<div style="--fs:{_s(13)};color:{T.TEXT_2};'
            f'margin:{_s(8)} 0 {_s(14)} 0">This site is for authorized users '
            f'only.</div>', unsafe_allow_html=True)
        pw = st.text_input("Password", type="password",
                           key="password_input")
        st.markdown(
            f'<div style="color:{T.MUTED};--fs:{_s(12)};'
            f'margin-top:{_s(18)}">bluemet.org</div>',
            unsafe_allow_html=True)

    with right:
        # MRMS and the live fleet are OFF on the login map (21 Sep):
        # it shows the airspace - ARTCC boundaries and jet routes -
        # and the station conditions. BLUEMET_LOGIN_MRMS=on and
        # BLUEMET_LOGIN_FLEET=on turn them back on.
        layers = _airspace_layers()
        note = "ARTCC boundaries \u00b7 fill: SPC Day 1 convective outlook"
        try:
            import datetime as _dtm
            _rk = _spc_by_artcc(_dtm.datetime.now(_dtm.timezone.utc)
                                .strftime("%Y%m%d%H%M")[:-1])
            if _rk:
                _top = max(set(v[0] for v in _rk.values()), key=SPC_RANK.get)
                note += f" (up to {_top}, {len(_rk)} centres)"
            else:
                note += " (no risk covers 20% of any centre)"
        except Exception:
            pass
        if os.environ.get("BLUEMET_LOGIN_MRMS", "off").lower() == "on":
            mrms, stamp = _mrms_layers()
            layers = mrms + layers
            note += (f" \u00b7 MRMS {stamp[9:11]}:{stamp[11:13]}Z"
                     if stamp else " \u00b7 MRMS: no current scan")
        # Stations are OFF the login map (23 Sep): the map is the SPC
        # Day 1 outlook by centre and nothing else. The chips above
        # carry the station conditions. BLUEMET_LOGIN_STATIONS=on
        # restores the blue dots.
        if os.environ.get("BLUEMET_LOGIN_STATIONS", "off").lower() == "on":
            layers += _station_layers(statuses)
        if os.environ.get("BLUEMET_LOGIN_FLEET", "off").lower() == "on":
            layers += _fleet_layers()
        st.markdown('<div style="margin-top:16px"></div>',
                    unsafe_allow_html=True)
        st.pydeck_chart(_deck(layers), use_container_width=True,
                        height=MAP_MIN_PX)

    return pw or None
