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


# The exact A320 icon page 3 uses — same path data, same #005ADC
# blue, same 64x64 centre anchor. Copied rather than imported because
# page filenames start with digits and are not importable as modules;
# if this ever diverges from page 3 the two maps will disagree about
# what a JetBlue aircraft looks like, so keep them in step.
def _a320_icon_uri(fill="#005ADC", stroke="#FFFFFF",
                   stroke_w=0.5):
    import urllib.parse
    body = ("M0,-10 L0.35,-9.6 L0.55,-8.8 L0.6,-6 L0.6,-1.6 "
            "L9.2,3.2 L9.6,3.4 L9.6,4 L9.1,4.1 L2.6,3.3 "
            "L0.6,3.1 L0.6,6.4 L3.3,8.2 L3.3,9 L0.5,8.5 "
            "L0.45,9.4 L0,9.7 L-0.45,9.4 L-0.5,8.5 L-3.3,9 "
            "L-3.3,8.2 L-0.6,6.4 L-0.6,3.1 L-2.6,3.3 "
            "L-9.1,4.1 L-9.6,4 L-9.6,3.4 L-9.2,3.2 L-0.6,-1.6 "
            "L-0.6,-6 L-0.55,-8.8 L-0.35,-9.6 Z")
    eng_r = ("M2.6,-0.9 L3.35,-0.9 L3.45,-0.4 L3.45,1.6 "
             "L3.3,1.9 L2.75,1.9 L2.6,1.5 Z")
    eng_l = ("M-2.6,-0.9 L-3.35,-0.9 L-3.45,-0.4 L-3.45,1.6 "
             "L-3.3,1.9 L-2.75,1.9 L-2.6,1.5 Z")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" '
        'height="64" viewBox="-11 -11 22 22">'
        f'<g fill="{fill}" stroke="{stroke}" stroke-width="{stroke_w}">'
        f'<path d="{body}"/><path d="{eng_r}"/>'
        f'<path d="{eng_l}"/></g></svg>'
    )
    return ("data:image/svg+xml;charset=utf-8,"
            + urllib.parse.quote(svg))


_AC_ICON = {"url": _a320_icon_uri(), "width": 64, "height": 64,
            "anchorX": 32, "anchorY": 32, "mask": False}
# Everyone else: grey #7F817E body, black outline, drawn at half the
# previous size. Filled rather than hollow now — at this size an
# outline alone reads as a smudge, while a solid grey body still
# stays clearly subordinate to the blue fleet. The
# silhouette shape and heading still read, but an unfilled symbol
# stays visually subordinate to the solid blue fleet and lets the
# basemap show through, which matters when a few hundred aircraft
# are in the box. stroke_w is in viewBox units: the box is 22 units
# rendered at 64 px, so 0.9 units is about 2.6 px on screen.
_AC_ICON_OTHER = {"url": _a320_icon_uri("#7F817E", "#000000", 0.9),
                  "width": 64, "height": 64, "anchorX": 32,
                  "anchorY": 32, "mask": False}


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
# Ramp declutter. Every airport carries a permanent pile of parked
# aircraft — a hundred at JFK alone — which swamps the terminal area
# and hides the traffic that matters. Rule: inside DECLUTTER_SM of an
# airport, if more than DECLUTTER_MIN aircraft are present, drop the
# ones that are not moving.
#
# Only the STATIONARY ones. Suppressing everything inside 10 sm would
# also delete aircraft on final and on climbout, which is exactly the
# traffic an airspace page exists to show — a parked A320 and one at
# 800 ft on approach are both "within 10 sm of JFK" and only one of
# them is clutter.
DECLUTTER_SM = 10.0
DECLUTTER_MIN = 10
DECLUTTER_ALT_FT = 1200      # at or below this, and slow, = parked
DECLUTTER_GS_KT = 40


def _declutter(rows):
    """Drop stationary aircraft in crowded airport circles.

    Returns (kept, n_hidden). Airborne traffic is never touched.
    """
    import math

    if not rows:
        return rows, 0
    sm = DECLUTTER_SM * 1609.34
    hidden = set()
    for icao, (ala, alo, _k) in AIRPORTS.items():
        near = []
        for i, r in enumerate(rows):
            dy = (r["lat"] - ala) * 111320.0
            dx = ((r["lon"] - alo) * 111320.0
                  * math.cos(math.radians(ala)))
            if math.hypot(dx, dy) <= sm:
                near.append(i)
        if len(near) <= DECLUTTER_MIN:
            continue
        for i in near:
            r = rows[i]
            alt = r.get("alt")
            parked = ((alt is None or alt <= DECLUTTER_ALT_FT)
                      and (r.get("gs") or 0) <= DECLUTTER_GS_KT)
            if parked:
                hidden.add(i)
    if not hidden:
        return rows, 0
    return [r for i, r in enumerate(rows) if i not in hidden], len(hidden)
# Ramp-cluster suppression. Every airport carries a permanent pile of
# parked and taxiing aircraft — a hundred or more at JFK — which
# swamps the terminal area and hides the traffic that matters. When
# more than CLUSTER_MIN aircraft sit inside CLUSTER_SM statute miles
# of a field, that group is treated as ramp clutter and dropped.
#
# ONE EXEMPTION, and it matters: anything above CLUSTER_EXEMPT_FT or
# faster than CLUSTER_EXEMPT_KT is kept regardless. Without it the
# rule would also delete aircraft on short final into JFK, which is
# precisely what an ops map exists to show.
CLUSTER_SM = 10.0
CLUSTER_MIN = 10
CLUSTER_EXEMPT_FT = 3000
CLUSTER_EXEMPT_KT = 100


@st.cache_data(ttl=60, show_spinner=False)
def _drop_ramp_clusters(rows):
    """Remove parked/taxiing piles. Returns (kept, n_dropped).

    For each airport, count aircraft within CLUSTER_SM. If that group
    is bigger than CLUSTER_MIN it is ramp clutter and goes — except
    for anything airborne by altitude or speed, which is kept.
    """
    import math

    if not rows:
        return rows, 0
    doomed = set()
    for _, (ala, alo, _k) in AIRPORTS.items():
        near = []
        for i, r in enumerate(rows):
            dx = ((r["lon"] - alo) * 60.0
                  * math.cos(math.radians(ala)))
            dy = (r["lat"] - ala) * 60.0
            # 1 nm = 1.15078 sm
            if math.hypot(dx, dy) * 1.15078 <= CLUSTER_SM:
                near.append(i)
        if len(near) > CLUSTER_MIN:
            for i in near:
                r = rows[i]
                if (r.get("_alt", 0) >= CLUSTER_EXEMPT_FT
                        or r.get("_gs", 0) >= CLUSTER_EXEMPT_KT):
                    continue
                doomed.add(i)
    kept = [r for i, r in enumerate(rows) if i not in doomed]
    return kept, len(doomed)


def area_traffic(bucket: str, radius_nm: int):
    """All aircraft within radius_nm of the N90 centre.

    Returns (jbu_rows, other_rows, note). Never raises: the airspace
    layers must still draw if the feed is down.
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
        jbu, other = [], []
        for p in ac:
            cs = (p.get("flight") or "").strip().upper()
            try:
                alat, alon = float(p["lat"]), float(p["lon"])
            except Exception:
                continue
            alt = p.get("alt_baro")
            alt = None if alt in ("ground", None) else alt
            try:
                gs = float(p.get("gs") or 0.0)
            except Exception:
                gs = 0.0
            mine = cs.startswith("JBU")
            (jbu if mine else other).append({
                "lat": alat, "lon": alon,
                "cs": cs.replace("JBU", "B6") if mine else cs,
                "icon": _AC_ICON if mine else _AC_ICON_OTHER,
                "alt": alt,
                "gs": float(p.get("gs") or 0.0),
                # deck.gl IconLayer angle is CCW; heading is CW from
                # north, so it has to be flipped — same convention
                # page 3 uses.
                "angle": (360.0 - float(p.get("track") or 0.0)) % 360.0,
                "tip": (f"{cs} &mdash; "
                        f"{'on ground' if alt is None else f'{alt:,} ft'}"
                        f", {gs:.0f} kt"),
                "_alt": alt if alt is not None else 0,
                "_gs": gs,
            })
        jbu, n_j = _drop_ramp_clusters(jbu)
        other, n_o = _drop_ramp_clusters(other)
        return (jbu, other,
                f"{len(jbu)} JetBlue and {len(other)} other of "
                f"{len(ac)} aircraft; {n_j + n_o} hidden as ramp "
                f"clutter")
    return [], [], last


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
        # Scheme K. Three pieces per class: a muted TRIANGLE, a
        # pastel CHIP behind the label, and dark text on it. No chip
        # border — dropping it is what makes this read calm rather
        # than decorated, and the fills stay distinct enough without.
        #
        # Class mapping, inferred from the vice adaptation rather
        # than stated by it: ARRIVAL = coordination_fixes, which
        # carry traffic INTO N90 from a centre. DEPARTURE =
        # airspace_awareness only. OTHER = the seven in both lists.
        #
        # Text is dark enough to clear 4.5 contrast on its own chip —
        # measured 9.0 arrival, 8.4 departure, 8.8 other — while the
        # chips themselves sit at ~1.1 against the basemap, which is
        # the point: visible, not shouting.
        tri  = {"coord": [76, 139, 63], "dep": [180, 99, 90]}
        txt  = {"coord": [27, 67, 50], "dep": [107, 31, 31]}
        chip = {"coord": [216, 240, 192], "dep": [247, 214, 214]}
        lbl  = {"coord": "arrival gate", "dep": "departure gate"}
        out["fixes"] = [{
            "name": f["name"], "lat": f["lat"], "lon": f["lon"],
            "tcolor": tri.get(f.get("role"), [160, 139, 60]),
            "lcolor": txt.get(f.get("role"), [74, 59, 18]),
            "chip": chip.get(f.get("role"), [239, 231, 198]) + [238],
            "tip": (f"{f['name']} &mdash; "
                    + lbl.get(f.get("role"), "other nav aid")
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
    show_ac = st.checkbox("Traffic", value=True,
                          help=f"All aircraft within "
                               f"{TRAFFIC_RADIUS_NM} nm of the N90 "
                               f"centre. JetBlue solid blue with "
                               f"flight numbers; everyone else a "
                               f"black outline only, tooltip only. "
                               f"Refreshes every 60 s.")
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
smooth_on = True
algo = "built-in weighted"
post = "none (raw)"
if radar_on:
    # Start the background warmer the first time anyone opens the
    # page with radar on. Idempotent, so this is safe on every rerun;
    # L2_WARMER=off disables it without a deploy.
    try:
        from core import radar_l2 as _L2W
        _L2W.ensure_radar_warmer(_STATIC)
        _warm = _L2W.warm_frames(_STATIC, "n90", 24)
    except Exception:
        _warm = []
else:
    _warm = []
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
        try:
            from core import radar_l2 as _L2C
            _algos = list(_L2C.COMBINERS)
        except Exception:
            _algos = ["built-in weighted"]
        algo = st.selectbox(
            "Merge algorithm", _algos, index=0,
            help="How overlapping radars are combined. Weighted "
                 "blends smoothly but averages away peaks and fine "
                 "structure; nearest and best-resolution pick one "
                 "site per cell, keeping detail at the cost of "
                 "visible seams; max is the sharpest and the least "
                 "honest about calibration.")
        post = st.selectbox(
            "Detail filter", list(_L2C.POSTFILTERS), index=0,
            help="Applied per cell from its surroundings. Adaptive "
                 "blends local max and mean by how structured the "
                 "area is — mean where smooth, max where structured "
                 "— and fills single-cell dropouts. Bilateral "
                 "averages only similar-valued neighbours, so it "
                 "never dilates a core.")
        n_frames = st.slider("Frames", 1, 24, 1,
                             help="1 frame = a MOSAIC of every site "
                                  "in the region, current. More "
                                  "than 1 = a single-radar loop "
                                  "through history, because the "
                                  "sites do not scan in step and "
                                  "TDWR has no archive. ~10 s per "
                                  "new frame; cached ones are free.")
    with r4:
        smooth_on = st.toggle(
            "Smoothing", value=True,
            help="Gaussian smoothing plus seam matching at coverage "
                 "handovers. Applies to frames built with the button "
                 "below — WARMED frames are already rendered on disk "
                 "and keep whatever setting was active when they "
                 "were made.")
        build = st.button("Build radar loop", type="primary",
                          help="Only needed for a product or depth "
                               "the warmer is not already keeping "
                               "current.")
        if _warm:
            st.caption(f"{len(_warm)} warmed frames on disk")
else:
    build = False

# The page's radar is defined by a REGION in core.radar_l2, not by
# constants here. It used to hardcode sites=["KOKX"] and a fixed
# 230 km box, written before regions existed — so editing REGIONS
# changed the L2 Radar Lab and did nothing here, and the two pages
# quietly disagreed about what "N90" meant.
L2_REGION = "N90 merged (S+C band)"
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
        # Geometry and site list come from the region definition.
        _clat_r, _clon_r, _hx_r, _hy_r = L2.REGION_VIEW[L2_REGION]
        L2.GRID_CENTER = (_clat_r, _clon_r)
        # Without center_fixed, build_mosaic re-centres on whichever
        # radar loads first — TPHL won that race, so the box ran
        # Wilkes-Barre to Delaware and cropped KENX and KBGM out
        # entirely even though both had loaded fine.
        diag = {"center": [_clat_r, _clon_r], "center_fixed": True}
        L2.HALF_X_M = _hx_r * 1000.0
        L2.HALF_Y_M = _hy_r * 1000.0
        L2.SMOOTH_SIGMA = 1.0 if smooth_on else 0.0
        L2.RES_MATCH = bool(smooth_on)
        # None = the full built-in path, which also runs the
        # inter-radar bias solver. The named combiners skip that and
        # merge the gridded fields directly.
        L2.COMBINE_FN = L2.COMBINERS.get(algo)
        L2.POSTFILTER = (None if post.startswith("none")
                         else L2.POSTFILTERS[post])
        bar = st.progress(0.0, "Starting...")
        frames, diag = L2.build_loop(
            L2.REGIONS[L2_REGION], int(n_frames), _STATIC,
            tag=_L2_TAG,
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
        elif diag.get("mode"):
            st.caption(f"Mode: {diag['mode']}")
    except Exception as exc:
        import traceback
        st.error(f"Radar build failed — {type(exc).__name__}: {exc}")
        st.code(traceback.format_exc(), language="text")

# Warmed frames win over a manual build: they are already on disk and
# kept current every two minutes, so opening the page costs nothing.
# The Build button stays for products or depths the warmer does not
# cover.
if radar_on and _warm and not st.session_state.get("_n90_radar"):
    try:
        from core import radar_l2 as _L2B
        st.session_state["_n90_radar"] = {
            "frames": _warm[-int(n_frames):],
            "diag": {"source": f"warmer ({len(_warm)} on disk)"},
            "bounds": list(_L2B.bounds()), "tag": "n90",
            "product": "Composite (warmed)",
        }
    except Exception:
        pass

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

# ---------------------------------------------------------------------------
# Map sizing — TEMPORARY tuning controls
# ---------------------------------------------------------------------------
# Here to find the right default by eye, not to stay. Once the numbers
# settle, hardcode MAP_H and MAP_W_PCT below and delete this expander.
# Width is a percentage rather than pixels because Streamlit sizes a
# chart to its container: the only way to make it narrower is to put
# it in a column and leave the rest empty, so the slider drives a
# column ratio.
MAP_H_DEFAULT = 760
MAP_W_DEFAULT = 100
with st.expander("Map size (tuning)"):
    z1, z2, z3 = st.columns([2, 2, 3])
    with z1:
        map_h = st.slider("Height (px)", 380, 1400, MAP_H_DEFAULT, 20)
    with z2:
        map_w = st.slider("Width (% of page)", 40, 100,
                          MAP_W_DEFAULT, 5)
    with z3:
        st.caption(
            f"Currently **{map_w}% x {map_h}px**. When this looks "
            f"right, set MAP_H_DEFAULT = {map_h} and "
            f"MAP_W_DEFAULT = {map_w} in the source and remove this "
            f"expander."
        )

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
        filled=False, stroked=True, get_line_color=[110, 122, 128, 185],
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
             # The third tier used to be mid-grey and washed out
             # against the basemap; darkened so it reads.
             "acolor": {"core": [200, 24, 24], "sat": [190, 100, 20]}
                       .get(k, [72, 84, 90]),
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
        get_text="name", get_size=11, get_color=[26, 32, 36],
        get_text_anchor='"start"', get_pixel_offset=[8, 8],
        background=True, get_background_color=[255, 255, 255, 225],
        background_padding=[3, 1, 3, 1]))
if show_ac:
    from datetime import datetime, timezone
    _tb = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    _ac, _other, _acnote = area_traffic(_tb, TRAFFIC_RADIUS_NM)
    _ac, _h1 = _declutter(_ac)
    _other, _h2 = _declutter(_other)
    if _h1 + _h2:
        _acnote += (f"; {_h1 + _h2} parked hidden inside "
                    f"{DECLUTTER_SM:.0f} sm of airports")
    # Other traffic first, so JetBlue draws on top of it. Deliberately
    # UNLABELLED: 40 nm around New York holds a few hundred aircraft
    # and labelling them all would bury the airspace underneath. The
    # callsign is still in the tooltip.
    if _other:
        layers.append(pdk.Layer(
            "IconLayer", data=_other, get_position="[lon, lat]",
            # Half the previous size (19 -> 10, bounds scaled with
            # it), against a fleet drawn at double. A 7x size ratio
            # is deliberate: with a few hundred aircraft in a 40 nm
            # circle, subordinate has to mean SMALL, not just paler.
            get_icon="icon", get_size=10, size_min_pixels=6,
            size_max_pixels=13, get_angle="angle", pickable=True))
    if _ac:
        layers.append(pdk.Layer(
            # EXACTLY twice the other-traffic size: other is 10
            # (6-13 px), fleet is 20 (12-26 px). Two separate
            # instructions — "double the fleet" and "halve the
            # others" — compounded to a 7x ratio once, so this is
            # written as a ratio to the other layer rather than
            # adjusted on its own.
            "IconLayer", data=_ac, get_position="[lon, lat]",
            get_icon="icon", get_size=20, size_min_pixels=12,
            size_max_pixels=26, get_angle="angle", pickable=True))
        layers.append(pdk.Layer(
            "TextLayer", data=_ac, get_position="[lon, lat]",
            get_text="cs", get_size=10, get_color=[0, 40, 120],
            get_text_anchor='"start"', get_pixel_offset=[9, -9],
            background=True,
            get_background_color=[255, 255, 255, 228],
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
        background=True, get_background_color="chip",
        # No border: deck.gl TextLayer has no chip stroke, which is
        # exactly what scheme K wants.
        background_padding=[5, 2, 5, 2]))

_deck = pdk.Deck(
    layers=layers,
    initial_view_state=pdk.ViewState(
        latitude=N90_CENTER[0], longitude=N90_CENTER[1],
        zoom=_zoom_for(radius_nm), min_zoom=4, max_zoom=12),
    # Same basemap as the CONUS fleet map, so the two pages read as
    # one product. Every colour below is tuned for it.
    map_style="light",
    tooltip={"html": "<b>{tip}</b>"},
)

# A chart fills its container, so width is controlled by rendering
# into a column of the requested fraction and leaving the remainder
# empty.
if map_w >= 100:
    st.pydeck_chart(_deck, height=map_h)
else:
    _mc = st.columns([map_w, max(1, 100 - map_w)])
    with _mc[0]:
        st.pydeck_chart(_deck, height=map_h)

st.caption(
    "Fix chips: GREEN arrival gate, ROSE departure gate, SAND other "
    "nav aid. Orange outline is an APPROXIMATE N90 "
    "extent (hull of the fixes), NOT the delegated TRACON boundary — "
    "that geometry is not published in FAA open GIS. Blue: FAA New "
    "York Class B shelves. Grey: ARTCC high-sector boundaries. "
    f"Fix data extracted {data.get('fix_vintage', '?')}."
    + (f" Traffic: {_acnote} within {TRAFFIC_RADIUS_NM} nm — "
       f"JetBlue solid blue and labelled, all others "
       f"black outline."
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
