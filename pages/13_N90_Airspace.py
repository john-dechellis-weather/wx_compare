"""N90 — New York TRACON airspace.

A dedicated airspace page rather than more layers bolted onto the
CONUS fleet map. Page 3 answers "where is my fleet and what is the
weather"; this answers "what does the New York terminal airspace look
like right now". Different question, different default view, different
layer set — and keeping them apart means neither page pays for the
other's load time.

Opens at a 300 nm radius on the N90 centroid, which reaches DCA to
BUF to PWM.

BUILT NOW: navigation fixes with role colouring, approximate N90
extent, Class B shelves, ARTCC boundaries, reference airports, and
an optional KOKX Level II radar loop.

The radar is opt-in and manual for a reason: every frame is a full
fetch, QC, grid and render at ~10 s, so a 24-frame loop is four
minutes of work. Frames are keyed by volume filename and cached on
disk, so a second build only renders scans it has not seen.

DELIBERATELY NOT BUILT YET, and why — each needs a source decision
before any code is worth writing:

  * Commonly used routes. Needs a source for the actual route
    strings (preferred routes / CDR database, or JetBlue's own
    filed-route history), plus fix-by-fix expansion. The fix
    coordinates to draw them are already here.
  * Live aircraft from other airlines. Page 3's fleet fetcher is
    JetBlue-filtered by callsign; the same tile sweep returns
    everything, so this is a filter change rather than a new feed.
  * CWSU / SWAP / TMI. There is no single documented CWSU API. The
    candidates are ATCSCC advisories, NAS Status, and the ZNY CWSU
    OIS page — different formats, different refresh rates, and
    scraping an OIS page is a fragile dependency for an ops tool.
    Worth an explicit decision rather than a guess.
"""

import os
from pathlib import Path

import streamlit as st

from auth import check_password

check_password()

st.set_page_config(page_title="N90 Airspace", layout="wide")

# N90 centroid — between JFK and the KOKX radar, so the terminal area
# sits mid-frame rather than at an edge.
N90_CENTER = (40.90, -73.60)
_STATIC = Path(__file__).resolve().parent.parent / "static"

# Airport reference points, for orientation only. Majors plus the N90
# satellites plus the ring of fields that define the 300 nm view.
AIRPORTS = {
    "KJFK": (40.6398, -73.7789, "core"),
    "KLGA": (40.7772, -73.8726, "core"),
    "KEWR": (40.6925, -74.1687, "core"),
    "KTEB": (40.8501, -74.0608, "sat"),
    "KHPN": (41.0670, -73.7076, "sat"),
    "KISP": (40.7952, -73.1002, "sat"),
    "KFRG": (40.7288, -73.4134, "sat"),
    "KSWF": (41.5041, -74.1048, "sat"),
    "KPHL": (39.8721, -75.2411, "ring"),
    "KBDL": (41.9389, -72.6832, "ring"),
    "KPVD": (41.7240, -71.4283, "ring"),
    "KBOS": (42.3630, -71.0064, "ring"),
    "KALB": (42.7483, -73.8017, "ring"),
    "KBUF": (42.9405, -78.7322, "ring"),
    "KPWM": (43.6462, -70.3093, "ring"),
    "KDCA": (38.8521, -77.0377, "ring"),
    "KBWI": (39.1754, -76.6683, "ring"),
    "KIAD": (38.9445, -77.4558, "ring"),
    "KACY": (39.4576, -74.5772, "ring"),
    "KMDT": (40.1935, -76.7634, "ring"),
}


# ---------------------------------------------------------------------------
# JetBlue traffic in the terminal area
# ---------------------------------------------------------------------------
# A single bounded point query rather than page 3's 17-tile CONUS
# sweep: 40 nm is one small circle, so one call covers it. That also
# keeps this page independent of page 3 — nothing is imported across
# pages, so a change to the fleet map cannot break the airspace map.
# Same two hosts and the same User-Agent as page 3, so we stay one
# well-behaved client rather than two.
TRAFFIC_RADIUS_NM = 40


@st.cache_data(ttl=60, show_spinner=False)
def jbu_traffic(bucket: str, radius_nm: int):
    """JetBlue aircraft within radius_nm of the N90 centre.

    Returns (rows, note). Never raises: the airspace layers must
    still draw if the feed is down.
    """
    import requests

    hdrs = {"User-Agent": "bluemet.org ops dashboard"}
    la, lo = N90_CENTER
    urls = [
        f"https://api.adsb.lol/v2/point/{la:.2f}/{lo:.2f}/{radius_nm}",
        f"https://opendata.adsb.fi/api/v2/lat/{la:.2f}/lon/"
        f"{lo:.2f}/dist/{radius_nm}",
    ]
    last = "no hosts tried"
    for u in urls:
        try:
            r = requests.get(u, headers=hdrs, timeout=6)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            continue
        try:
            ac = (r.json() or {}).get("ac") or []
        except Exception as exc:
            last = f"bad JSON: {exc}"
            continue
        rows = []
        for p in ac:
            cs = (p.get("flight") or "").strip().upper()
            if not cs.startswith("JBU"):
                continue
            try:
                alat, alon = float(p["lat"]), float(p["lon"])
            except Exception:
                continue
            alt = p.get("alt_baro")
            alt = None if alt in ("ground", None) else alt
            rows.append({
                "lat": alat, "lon": alon,
                "cs": cs.replace("JBU", "B6"),
                "trk": float(p.get("track") or 0.0),
                "tip": (f"{cs} &mdash; "
                        f"{'on ground' if alt is None else f'{alt:,} ft'}"
                        f", {p.get('gs') or 0:.0f} kt"),
            })
        return rows, f"{len(rows)} JetBlue of {len(ac)} aircraft"
    return [], last


def _json(name):
    """Load a static asset. Returns (data, error) — never raises into
    the page, because one missing asset should not take the map down."""
    import json

    try:
        return json.loads((_STATIC / name).read_text()), None
    except Exception as exc:
        return None, f"{name}: {type(exc).__name__}: {exc}"


@st.cache_data(ttl=86400, show_spinner=False)
def load_airspace():
    """All static airspace assets in one cached call."""
    out = {"errors": []}
    fx, err = _json("n90_fixes.json")
    if err:
        out["errors"].append(err)
    else:
        # GREEN departure gate | YELLOW arrival AND departure |
        # WHITE coordination fix. Data-driven: vice lists gates under
        # airspace_awareness and boundary crossings under
        # coordination_fixes; fixes in both work traffic both ways.
        # Two colour sets. The triangle keeps the bright role colour
        # so it reads against the basemap; the LABEL uses a darkened
        # version, because a light plate needs dark text. Bright
        # green or white on light grey is unreadable, so switching
        # the plate without darkening the text would have traded one
        # legibility problem for another.
        col = {"dep": [40, 190, 70], "both": [250, 210, 40]}
        tcol = {"dep": [16, 96, 40], "both": [128, 88, 0]}
        lbl = {"dep": "departure gate", "both": "arrival + departure"}
        out["fixes"] = [{
            "name": f["name"], "lat": f["lat"], "lon": f["lon"],
            "tcolor": col.get(f.get("role"), [255, 255, 255]),
            "lcolor": tcol.get(f.get("role"), [45, 45, 45]),
            "tip": (f"{f['name']} &mdash; "
                    + lbl.get(f.get("role"), "coordination fix")
                    + (f", from {f['from']}" if f.get("from") else "")
                    + f" ({f.get('dist_nm', '?')} nm)"),
        } for f in fx.get("fixes", [])]
        out["hull"] = [{"polygon": fx.get("hull", []),
                        "tip": "N90 approximate extent (hull of "
                               "coordination fixes) &mdash; NOT the "
                               "delegated TRACON boundary"}]
        out["fix_vintage"] = fx.get("extracted", "?")

    cb, err = _json("ny_class_b.json")
    if err:
        out["errors"].append(err)
    else:
        out["classb"] = [{
            "polygon": a["polygon"],
            "tip": f"NY Class B {a['name']} &mdash; "
                   f"{a['low']} to {a['high']} ft",
        } for a in cb.get("areas", [])]

    ar, err = _json("artcc_high.json")
    if err:
        out["errors"].append(err)
    else:
        out["artcc"] = [{
            "polygon": c["polygon"],
            "tip": f"{c['ident']} &mdash; {c['name']} Center (high)",
        } for c in ar.get("centers", [])]
    return out


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("N90 — New York TRACON")
st.caption(
    "Terminal airspace reference. Opens at a 300 nm radius, which "
    "reaches DCA, BUF and PWM."
)

data = load_airspace()
for e in data.get("errors", []):
    st.warning(f"Asset unavailable — {e}")

c = st.columns([1, 1, 1, 1, 1, 2])
with c[0]:
    show_fix = st.checkbox("Fixes", value=True)
with c[1]:
    show_hull = st.checkbox("N90 extent", value=True)
with c[2]:
    show_cb = st.checkbox("Class B", value=True)
with c[3]:
    show_ar = st.checkbox("ARTCC", value=False)
with c[4]:
    show_ap = st.checkbox("Airports", value=True)
    show_ac = st.checkbox("JBU traffic", value=True,
                          help=f"JetBlue aircraft within "
                               f"{TRAFFIC_RADIUS_NM} nm of the N90 "
                               f"centre. Refreshes every 60 s.")
with c[5]:
    radius_nm = st.select_slider(
        "Initial radius (nm)", [100, 150, 200, 300, 400], value=300)

# ---------------------------------------------------------------------------
# Radar (opt-in)
# ---------------------------------------------------------------------------
st.divider()
r1, r2, r3, r4 = st.columns([1, 2, 2, 2])
with r1:
    radar_on = st.checkbox("Radar", value=False,
                           help="KOKX Level II. Off by default — a "
                                "loop build is minutes of work.")
product = "Composite (all reflectivity levels)"
n_frames = 6
if radar_on:
    with r2:
        product = st.selectbox(
            "Product",
            ["Composite (all reflectivity levels)",
             "Base reflectivity (0.5 deg)"],
            help="Composite maxes every gridded level. Base is the "
                 "lowest sweep only, projected as a plan view.",
        )
    with r3:
        n_frames = st.slider("Frames", 1, 24, 6,
                             help="~10 s per NEW frame. Cached "
                                  "frames are free, so growing an "
                                  "existing loop is cheap.")
    with r4:
        st.write("")
        build = st.button("Build radar loop", type="primary")
else:
    build = False

_L2_TAG = "cmax" if product.startswith("Composite") else "base"

if build:
    try:
        from core import radar_l2 as L2

        # Composite: several levels, max over them. Base: ONE deep
        # level so the 0.5 deg sweep lands in it whole — a thin
        # level would intersect the climbing beam only in a narrow
        # range ring rather than giving a plan view.
        if _L2_TAG == "cmax":
            L2.TILTS, L2.LEVELS, L2.TOP_M, L2.BASE_M = 6, 5, None, 500.0
        else:
            L2.TILTS, L2.LEVELS, L2.TOP_M, L2.BASE_M = 1, 1, 6000.0, 0.0
        L2.RES_M = 250.0
        L2.HALF_X_M = L2.HALF_Y_M = 230000.0
        L2.GRID_CENTER = (40.8656, -72.8639)      # KOKX
        bar = st.progress(0.0, "Starting...")
        diag = {}
        frames, diag = L2.build_loop(
            "KOKX", int(n_frames), _STATIC, tag=_L2_TAG,
            progress=lambda f, m: bar.progress(min(f, 1.0), m),
            diag=diag)
        bar.empty()
        st.session_state["_n90_radar"] = {
            "frames": frames, "diag": diag,
            "bounds": list(L2.bounds()), "tag": _L2_TAG,
            "product": product,
        }
        if not frames:
            st.error(f"No frames built — {diag.get('error', 'unknown')}")
    except Exception as exc:
        import traceback
        st.error(f"Radar build failed — {type(exc).__name__}: {exc}")
        st.code(traceback.format_exc(), language="text")

_rs = st.session_state.get("_n90_radar") if radar_on else None
_frame_url = None
if _rs and _rs.get("frames"):
    fr = _rs["frames"]
    idx = st.slider("Frame", 1, len(fr), len(fr),
                    help="Oldest to newest.") - 1 if len(fr) > 1 else 0
    base = (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if base:
        _frame_url = f"{base}/app/static/{fr[idx]['name']}"
        st.caption(f"{_rs['product']} — frame {idx + 1} of {len(fr)}, "
                   f"valid {fr[idx]['valid']}")
    else:
        st.warning("RENDER_EXTERNAL_URL not set — cannot build an "
                   "absolute URL for the radar layer.")

import math

import pydeck as pdk


def _zoom_for(radius_nm, px=1400.0):
    """deck.gl zoom that fits a radius. World is 512*2**z px wide, and
    a radius in nm converts to degrees of LONGITUDE via cos(lat) —
    which is why a 300 nm radius spans ~13 deg here, not 10."""
    deg = 2.0 * radius_nm / 60.0 / math.cos(math.radians(N90_CENTER[0]))
    return round(math.log2(px * 360.0 / (512.0 * deg)), 2)


layers = []

# Radar goes down first so fixes, airports and boundaries stay legible
# on top of it.
if _frame_url:
    layers.append(pdk.Layer(
        "BitmapLayer", data=None, image=_frame_url,
        bounds=_rs["bounds"], opacity=0.8))

# Order matters: ARTCC underneath as a reference grid, then Class B,
# then the N90 extent, then fixes and airports on top.
if show_ar and data.get("artcc"):
    layers.append(pdk.Layer(
        "PolygonLayer", data=data["artcc"], get_polygon="polygon",
        filled=False, stroked=True, get_line_color=[150, 150, 150, 170],
        line_width_min_pixels=1, get_line_width=1, pickable=True))
if show_cb and data.get("classb"):
    layers.append(pdk.Layer(
        "PolygonLayer", data=data["classb"], get_polygon="polygon",
        filled=False, stroked=True, get_line_color=[0, 90, 200, 200],
        line_width_min_pixels=1, get_line_width=1, pickable=True))
if show_hull and data.get("hull"):
    layers.append(pdk.Layer(
        "PolygonLayer", data=data["hull"], get_polygon="polygon",
        filled=False, stroked=True, get_line_color=[230, 120, 30, 190],
        line_width_min_pixels=5, get_line_width=5, pickable=True))
if show_ap:
    rows = [{"lon": lo, "lat": la, "name": ic[1:],
             "acolor": {"core": [220, 30, 30], "sat": [200, 110, 30]}
                       .get(k, [110, 110, 110]),
             "asize": {"core": 90, "sat": 60}.get(k, 40),
             "tip": ic}
            for ic, (la, lo, k) in AIRPORTS.items()]
    layers.append(pdk.Layer(
        "ScatterplotLayer", data=rows, get_position="[lon, lat]",
        get_fill_color="acolor", get_radius="asize",
        radius_units='"pixels"', radius_min_pixels=3,
        radius_max_pixels=7, pickable=True))
    layers.append(pdk.Layer(
        "TextLayer", data=rows, get_position="[lon, lat]",
        get_text="name", get_size=11, get_color=[30, 30, 30],
        get_text_anchor='"start"', get_pixel_offset=[8, 8],
        background=True, get_background_color=[255, 255, 255, 210],
        background_padding=[3, 1, 3, 1]))
if show_ac:
    from datetime import datetime, timezone
    _tb = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    _ac, _acnote = jbu_traffic(_tb, TRAFFIC_RADIUS_NM)
    if _ac:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=_ac, get_position="[lon, lat]",
            get_fill_color=[0, 60, 160, 230], get_radius=110,
            radius_units='"pixels"', radius_min_pixels=4,
            radius_max_pixels=9, pickable=True))
        layers.append(pdk.Layer(
            "TextLayer", data=_ac, get_position="[lon, lat]",
            get_text="cs", get_size=10, get_color=[0, 40, 120],
            get_text_anchor='"start"', get_pixel_offset=[9, -9],
            background=True,
            get_background_color=[255, 255, 255, 225],
            background_padding=[3, 1, 3, 1]))

if show_fix and data.get("fixes"):
    layers.append(pdk.Layer(
        "TextLayer", data=data["fixes"], get_position="[lon, lat]",
        get_text='"▲"', get_size=14, get_color="tcolor",
        pickable=True))
    layers.append(pdk.Layer(
        "TextLayer", data=data["fixes"], get_position="[lon, lat]",
        get_text="name", get_size=10, get_color="lcolor",
        get_text_anchor='"start"', get_pixel_offset=[7, -7],
        background=True, get_background_color=[228, 228, 230, 235],
        background_padding=[4, 2, 4, 2]))

st.pydeck_chart(pdk.Deck(
    layers=layers,
    initial_view_state=pdk.ViewState(
        latitude=N90_CENTER[0], longitude=N90_CENTER[1],
        zoom=_zoom_for(radius_nm), min_zoom=4, max_zoom=12),
    map_style="light",
    tooltip={"html": "<b>{tip}</b>"},
), height=760)

st.caption(
    "Fix triangles: GREEN departure gate, YELLOW arrival + departure, "
    "WHITE coordination fix. Orange outline is an APPROXIMATE N90 "
    "extent (hull of the fixes), NOT the delegated TRACON boundary — "
    "that geometry is not published in FAA open GIS. Blue: FAA New "
    "York Class B shelves. Grey: ARTCC high-sector boundaries. "
    f"Fix data extracted {data.get('fix_vintage', '?')}."
    + (f" Blue dots: {_acnote} within {TRAFFIC_RADIUS_NM} nm."
       if show_ac else "")
)

if _rs and _rs.get("diag"):
    with st.expander("Radar diagnostics"):
        st.json(_rs["diag"])

with st.expander("Planned additions"):
    st.markdown(
        "**Commonly used routes.** Needs a source for the route "
        "strings — the FAA preferred-route database and CDRs, or "
        "JetBlue's own filed-route history, which would be more "
        "representative of what actually gets flown. The fix "
        "coordinates needed to draw them are already loaded.\n\n"
        "**Live aircraft, all operators.** Page 3's fleet fetcher "
        "already sweeps ADS-B tiles over this area and then filters "
        "to JetBlue callsigns. Showing everyone is a filter change, "
        "not a new feed — but expect a large jump in aircraft count "
        "inside this box, so thinning and a callsign filter matter.\n\n"
        "**CWSU / SWAP / TMI.** No single documented CWSU API "
        "exists. Candidates: ATCSCC advisories, NAS Status, and the "
        "ZNY CWSU OIS page — different formats, different refresh "
        "rates, and an OIS scrape is a fragile dependency for an "
        "ops tool. Worth deciding the source before writing code, "
        "and worth checking whether JetBlue already ingests TMI "
        "data internally."
    )
