"""N90 Arrival Gate Status — the ZNY gate-forecast graphic, live.

The CWSU's "Gate Forecast for KNYC" draws four lobes on a map — NORTH,
EAST, SOUTH, WEST — with an hour grid per gate coloured green, yellow
or red. This page is that picture, but for what is happening NOW and
in the next hour, from aircraft that exist:

  * each lobe is coloured by its projected load in the next 30 min
    against the gate's share of JFK's landing rate
  * the number in the lobe is the inbound aircraft assigned to it
  * the grid beside each lobe is projected arrivals per 15 min for the
    next hour, one box each, coloured the same way
  * the aircraft themselves are drawn, so the count can be checked
    against the picture

WHAT A GATE IS HERE. Four angular sectors from the N90 centre, sized
and pointed like the CWSU's lobes. An inbound aircraft belongs to the
lobe its bearing falls in, or the nearest lobe if it is in a gap.
This is the same geometry the flow model uses; nothing is looked up.

WHAT THE COLOURS MEAN. JFK's AAR from the AADC, split four ways, is
the per-gate rate. A box is green under 100% of that, yellow to 150%,
red above. With no AAR published the split is against 36/h, which is
JFK on a normal day, and the caption says so.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from retro_theme import apply_retro_theme

apply_retro_theme()

from auth import check_password

check_password()

import pydeck as pdk

from core import airspace as AS
from core import flow as FW
from core import icons as IC
from core import tracks as TK

try:
    from core.cam_overlay import note_request as _note_req

    _note_req()
except Exception:
    pass

_STATIC = Path(__file__).resolve().parent.parent / "static"
IC.set_static_dir(_STATIC)
CENTER = AS.N90_CENTER
AC_RING_NM = 30.0        # aircraft drawn this far outside the N90 outline
AC_CEIL_FT = 18000       # and below this altitude

# The four lobes, TRACED FROM THE CWSU'S GATE FORECAST GRAPHIC and
# georeferenced from known points (NYC, Montauk, Cape May). They are
# not angular sectors: NORTH is a finger up the Hudson, EAST runs the
# length of the Sound, SOUTH is the Jersey shore, WEST fans over
# central New Jersey. Aircraft are assigned by the bearing range each
# lobe subtends from N90, nearest lobe if in a gap. The grid anchor is
# where the CWSU puts each gate's boxes: beyond the lobe on its side.
# EXTRACTED FROM THE CWSU GRAPHIC'S PIXELS, not traced by eye: the
# green and yellow regions were colour-segmented, the state-border
# lines drawn over them healed, each region's boundary walked and
# simplified to 2 px, then georeferenced by an affine fit through
# NYC, Montauk Point and Cape May. The shapes are the graphic's own.
# The lobes nearly meet at the apex, as they do in the original —
# the darker edge stroke is what separates them visually.
LOBES = {
    "NORTH": [
        [
            -75.1066,
            41.8987
        ],
        [
            -74.6764,
            41.5278
        ],
        [
            -74.5625,
            41.2787
        ],
        [
            -74.2929,
            41.1779
        ],
        [
            -74.0848,
            40.9262
        ],
        [
            -74.162,
            40.8914
        ],
        [
            -74.243,
            40.9871
        ],
        [
            -75.4047,
            41.3286
        ],
        [
            -75.1186,
            41.8843
        ]
    ],
    "EAST": [
        [
            -73.3849,
            41.9209
        ],
        [
            -73.135,
            41.7
        ],
        [
            -72.9939,
            41.6765
        ],
        [
            -72.88,
            41.4274
        ],
        [
            -73.1103,
            41.0351
        ],
        [
            -73.3926,
            40.7155
        ],
        [
            -73.5115,
            40.6498
        ],
        [
            -73.614,
            40.874
        ],
        [
            -74.0607,
            40.9551
        ],
        [
            -74.298,
            41.2297
        ],
        [
            -73.9658,
            41.3584
        ],
        [
            -73.7481,
            41.697
        ],
        [
            -73.3969,
            41.9064
        ]
    ],
    "SOUTH": [
        [
            -73.1527,
            40.9649
        ],
        [
            -72.7408,
            40.5526
        ],
        [
            -73.5825,
            39.5421
        ],
        [
            -74.1785,
            39.5017
        ],
        [
            -74.2247,
            39.562
        ],
        [
            -74.579,
            39.5224
        ],
        [
            -74.7606,
            39.5938
        ],
        [
            -74.9213,
            39.8639
        ],
        [
            -74.7195,
            39.9519
        ],
        [
            -74.4727,
            40.2675
        ],
        [
            -74.1861,
            40.1293
        ],
        [
            -73.6115,
            40.2983
        ],
        [
            -73.4944,
            40.6125
        ],
        [
            -73.3995,
            40.6493
        ],
        [
            -73.1647,
            40.9504
        ]
    ],
    "WEST": [
        [
            -75.423,
            41.2873
        ],
        [
            -74.2259,
            40.9497
        ],
        [
            -74.2215,
            40.8586
        ],
        [
            -74.548,
            40.7173
        ],
        [
            -74.5075,
            40.3029
        ],
        [
            -74.6467,
            40.0779
        ],
        [
            -74.8372,
            39.9649
        ],
        [
            -75.2681,
            39.9299
        ],
        [
            -75.5674,
            40.0143
        ],
        [
            -75.8622,
            40.3741
        ],
        [
            -76.1014,
            40.8973
        ],
        [
            -75.654,
            40.8556
        ],
        [
            -75.4351,
            41.2729
        ]
    ]
}
GATES = (
    # name, bearing a0, a1 (for assignment), grid anchor (lon, lat)
    ("NORTH", 305.0, 5.0, (-74.35, 42.75)),
    ("EAST", 25.0, 100.0, (-71.55, 41.15)),
    ("SOUTH", 140.0, 215.0, (-74.10, 38.55)),
    ("WEST", 220.0, 295.0, (-77.05, 40.90)),
)
GREEN, YELLOW, RED = [46, 204, 64], [255, 213, 0], [214, 40, 40]

st.title("N90 Arrival Gate Status")
st.caption("The ZNY gate forecast graphic, live: inbound aircraft by "
           "gate now, over the MRMS national radar mosaic.")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def _airspace():
    return AS.load_airspace()


@st.cache_data(ttl=60, show_spinner=False)
def _traffic(_bucket: str):
    """Every airborne aircraft within 250 nm, the fields the model
    and the map need. ALL of them — TEB is N-numbers and bizjet
    callsigns, and the arrival rate counts every IFR arrival.
    `commercial` is carried per row so the demand model can still
    tell them apart."""
    import requests

    la, lo = CENTER
    ac = []
    for attempt in range(2):
        try:
            r = requests.get(f"https://api.adsb.lol/v2/point/{la:.2f}/{lo:.2f}/250",
                             timeout=8, headers={"User-Agent": "n90 gates"})
            if r.status_code != 200:
                st.session_state["_gates_traffic_err"] = f"HTTP {r.status_code}"
                continue
            ac = (r.json() or {}).get("ac") or []
            st.session_state["_gates_traffic_err"] = ""
            # An empty 200 is adsb.lol overloaded, not an empty sky;
            # one retry before accepting it.
            if ac or attempt:
                break
        except Exception as exc:
            st.session_state["_gates_traffic_err"] = f"{type(exc).__name__}: {exc}"
    out = []
    for p in ac:
        cs = (p.get("flight") or "").strip().upper()
        alt = p.get("alt_baro")
        if alt in ("ground", None):
            continue
        try:
            out.append({
                "cs": cs or (p.get("r") or "").strip().upper() or "?",
                "hex": (p.get("hex") or "").lower(),
                "lat": float(p["lat"]), "lon": float(p["lon"]),
                "_alt": int(alt), "_gs": float(p.get("gs") or 0),
                "_trk": (float(p["track"]) if p.get("track") is not None
                         else None),
                "vr": (float(p["baro_rate"]) if p.get("baro_rate") is not None
                       else None),
                "typ": (p.get("t") or "").strip().upper(),
                "commercial": TK.is_commercial(cs),
                "sq": str(p.get("squawk") or "").strip(),
            })
        except (TypeError, ValueError, KeyError):
            continue
    return out


# The N90 airports whose arrivals this page counts, with a default
# hourly AAR for each in case the AADC has nothing. TEB and HPN have
# no AADC entry; their defaults are typical VMC figures.
N90_AIRPORTS = {"JFK": 36, "LGA": 36, "EWR": 40, "TEB": 14, "HPN": 12}


@st.cache_data(ttl=300, show_spinner=False)
def _aar_one(code: str, _bucket: str):
    """One airport's programmed arrival rate from the AADC, or None."""
    import requests

    try:
        r = requests.get(f"https://www.fly.faa.gov/aadc/api/airports/{code}",
                         timeout=8,
                         headers={"User-Agent": "n90 gates",
                                  "Referer": "https://www.fly.faa.gov/aadc/"})
        if r.status_code != 200:
            return None
        d = r.json() or {}
        for v in d.get("rates") or []:
            try:
                if float(v) > 0:
                    return float(v)
            except (TypeError, ValueError):
                continue
    except Exception:
        return None
    return None


def _aar_all(_bucket: str):
    """{code: (rate, source)} for every N90 airport."""
    out = {}
    for code, dflt in N90_AIRPORTS.items():
        v = _aar_one(code, _bucket) if code in ("JFK", "LGA", "EWR") else None
        out[code] = (v, "AADC") if v else (dflt, "default")
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _dests(planes: tuple, _bucket: str) -> dict:
    """callsign -> {"orig", "dest"} via adsb.lol routeset, chunked."""
    import requests

    out = {}
    for k in range(0, len(planes), 150):
        part = planes[k:k + 150]
        try:
            r = requests.post("https://api.adsb.lol/api/0/routeset",
                              json={"planes": [{"callsign": c, "lat": la, "lng": lo}
                                               for c, la, lo in part]},
                              timeout=8, headers={"User-Agent": "n90 gates"})
            if r.status_code != 200:
                continue
            rows = r.json()
            rows = rows if isinstance(rows, list) else (rows or {}).get("planes") or []
        except Exception:
            continue
        for i, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            cs = (item.get("callsign") or "").strip().upper() or (
                part[i][0] if i < len(part) else "")
            pair = item.get("_airport_codes_iata") or item.get("airport_codes")
            if isinstance(pair, str) and "-" in pair:
                b = [x.strip().upper() for x in pair.split("-")]
                out[cs] = {"orig": b[0], "dest": b[-1]}
    return out


data = _airspace()
# TWO OUTLINES. `hull` is the N90 outline around the four lobes — the
# orange line on every page now. `core` is the inner terminal circle:
# an aircraft inside it has already arrived as far as the gates are
# concerned and is not counted.
hull = (data.get("hull") or [{}])[0].get("polygon") or []
# The core is a CIRCLE, not the old fix hull. The convex hull of the
# fixes reaches HNK and HIDAL, a hundred miles out — larger than the
# lobes in places, so it swallowed aircraft standing in a gate. Inside
# CORE_NM an arrival is committed to an approach and is no longer "at
# a gate"; that is the line this page cares about.
CORE_NM = 22.0
core = None      # built after the geometry helpers below
_now = datetime.now(timezone.utc)
_tb = _now.strftime("%Y%m%d%H%M")
rows = _traffic(_tb)
dests = _dests(tuple((r["cs"], round(r["lat"], 2), round(r["lon"], 2))
                     for r in rows), _tb[:-1])
aars = _aar_all(_tb[:-1])
aar_total = sum(v for v, _src in aars.values())        # N90-wide arrivals per hour
aar = aars["JFK"][0]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
_k = math.cos(math.radians(CENTER[0]))


def _pt(brg, nm):
    return [CENTER[1] + nm / 60.0 * math.sin(math.radians(brg)) / _k,
            CENTER[0] + nm / 60.0 * math.cos(math.radians(brg))]


def _brg(lon, lat):
    return (math.degrees(math.atan2((lon - CENTER[1]) * _k,
                                    lat - CENTER[0])) + 360.0) % 360.0


def _in_arc(b, a0, a1):
    return (a0 <= b <= a1) if a0 <= a1 else (b >= a0 or b <= a1)


core = [_pt(a, CORE_NM) for a in range(0, 360, 10)]


def _gate_for(b):
    for name, a0, a1, *_ in GATES:
        if _in_arc(b, a0, a1):
            return name
    # In a gap: nearest lobe centre.
    best = None
    for name, a0, a1, *_ in GATES:
        c = (a0 + ((a1 - a0) % 360.0) / 2.0) % 360.0
        d = abs((b - c + 180.0) % 360.0 - 180.0)
        if best is None or d < best[0]:
            best = (d, name)
    return best[1]


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
# Inbound now, and where each will be in 15-minute steps, per gate.
inbound = []
for r in rows:
    if not r.get("commercial"):
        # Demand counts airline-format callsigns only, as before —
        # N-numbers in the descent cone are as likely to be a Cessna
        # going to Farmingdale as a Gulfstream going to Teterboro.
        # They are still DRAWN.
        continue
    lon, lat = r["lon"], r["lat"]
    if core and FW.pip(lon, lat, core):
        continue
    # INSIDE A LOBE IS THAT GATE, full stop — it is at the gate. The
    # heading test below is only for aircraft still outside them.
    _in_lobe = next((n for n, poly in LOBES.items() if FW.pip(lon, lat, poly)),
                    None)
    d = 60.0 * math.hypot((lon - CENTER[1]) * _k, lat - CENTER[0])
    if d > FW.ROSE_MAX_NM or r["_trk"] is None or r["_gs"] < 60:
        continue
    vr = r["vr"] or 0.0
    if _in_lobe is None:
        to_c = (math.degrees(math.atan2((CENTER[1] - lon) * _k,
                                        CENTER[0] - lat)) + 360.0) % 360.0
        if abs((r["_trk"] - to_c + 180.0) % 360.0 - 180.0) > FW.ROSE_CONE_DEG:
            continue
    if r["_alt"] > FW.TERMINAL_CEILING_FT and vr > FW.DESC_FPM:
        continue
    # DESTINATION FILTER. The route lookup knows where most airline
    # flights are going. If it says somewhere other than an N90
    # airport — Philadelphia, Boston, Hartford — the aircraft is an
    # overflight however inbound it looks, and is not demand here.
    # Unknown destinations keep the geometric test.
    _pr = dests.get(r["cs"]) or {}
    _dst = (_pr.get("dest") or "").upper().lstrip("K")
    if _dst and _dst not in N90_AIRPORTS:
        continue
    apt = FW._dest_airport(r, dests)
    ala, alo = FW.METRO[apt]
    dist_apt = 60.0 * math.hypot((lon - alo) * _k, lat - ala)
    eta_min = dist_apt / r["_gs"] * 60.0 + FW.TERMINAL_MIN
    r = dict(r, gate=_in_lobe or _gate_for(_brg(lon, lat)), apt=apt,
             eta_min=eta_min, bin=min(3, max(0, int(eta_min // 15))))
    inbound.append(r)

per_gate = {}
for name, *_ in GATES:
    g = [r for r in inbound if r["gate"] == name]
    bins = [sum(1 for r in g if r["bin"] == i) for i in range(4)]
    per_gate[name] = {"now": len(g), "bins": bins,
                      "jfk": sum(1 for r in g if r["apt"] == "JFK"),
                      "cs": [r["cs"] for r in g]}

# Capacity per gate per 15 minutes: the N90 total split four ways.
# With defaults that is 138/h -> 8.6 per gate per quarter hour. It
# was JFK's 36 alone — 2.25 — against ALL metro arrivals, which made
# every gate red on any normal afternoon.
cap15 = aar_total / 4.0 / 4.0


# TWO STATES. Demand at or above the gate's share of the landing rate
# is HIGH and draws red; anything under is normal or low and draws
# green. No amber: the question this page answers is "is this gate
# a problem", and a third colour only softens the answer.
HIGH_RATIO = 1.05


def _colour(n):
    if cap15 <= 0:
        return GREEN
    return RED if n / cap15 >= HIGH_RATIO else GREEN


def _status(name):
    b = per_gate[name]["bins"]
    return _colour((b[0] + b[1]) / 2.0)


# ---------------------------------------------------------------------------
# Radar controls — the MRMS national mosaic, as on the Airspace page
# ---------------------------------------------------------------------------
# Same frames, same warmer, same smoothing: the live scan draws its
# full-resolution chunks, earlier scans draw their lighter loop frame,
# and the slider walks the last hour. Level II stays in core/l2.py for
# later; its warmer still runs and its log is in the diagnostics.
# Imported by name so a missing or broken core/mrms.py is REPORTED on
# the page instead of taking the page down. importlib gives the real
# reason; a plain "from core import mrms" hides it behind "cannot
# import name".
import importlib

try:
    _MR = importlib.import_module("core.mrms")
    _MR_ERR = ""
except Exception as _mexc:
    _MR, _MR_ERR = None, f"{type(_mexc).__name__}: {_mexc}"
    _cp = Path(__file__).resolve().parent.parent / "core"
    _MR_ERR += (f" \u2014 core/mrms.py "
                f"{'present' if (_cp / 'mrms.py').exists() else 'MISSING'}, "
                f"core/__init__.py "
                f"{'present' if (_cp / '__init__.py').exists() else 'MISSING'}")

RADAR_HOURS = 1.0
_rc = st.columns([1, 1, 2, 1])
_radar_on = _rc[0].radio("Radar", ["Off", "MRMS mosaic"], index=1,
                         horizontal=True,
                         help="NOAA MRMS national reflectivity mosaic, "
                              "1 km, every ~2 min, rendered by the same "
                              "warmer as the Airspace page.")
_l2_alpha = _rc[1].slider("Radar opacity", 0.2, 1.0, 0.75, 0.05)
_show_ac = _rc[3].toggle("Aircraft", value=True,
                         help=f"Every aircraft inside N90 or within "
                              f"{AC_RING_NM:.0f} nm of it, below "
                              f"FL{AC_CEIL_FT // 100}.")
_hist, _pick, _hist_err = [], None, ""
if _radar_on != "Off":
    try:
        if _MR is None:
            raise ImportError(_MR_ERR)
        if not hasattr(_MR, "history"):
            raise AttributeError("core/mrms.py has no history()")
        _hist = _MR.history(_STATIC, hours=RADAR_HOURS, limit=40)
    except Exception as _hexc:
        _hist_err = f"{type(_hexc).__name__}: {_hexc}"
    if _hist_err:
        _rc[2].error(f"Radar loop unavailable \u2014 {_hist_err}")
    elif len(_hist) > 1:
        _lbls = [f"{h[0][9:11]}:{h[0][11:13]}Z" for h in _hist]
        _pick = _rc[2].select_slider(
            f"Scan \u2014 {len(_hist)} over the last {RADAR_HOURS:g} h",
            options=list(range(len(_hist))), value=len(_hist) - 1,
            format_func=lambda i: _lbls[i])
    elif len(_hist) == 1:
        _rc[2].caption("Radar loop: 1 scan so far; backfill fills the "
                       "hour over the next few minutes.")
    else:
        _rc[2].caption("MRMS radar warming \u2014 first national scan "
                       "appears within a few minutes of a restart.")

# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------
layers = []

# RADAR FIRST, so it draws under the lobes and every label. The live
# frame draws its full-resolution chunks; an earlier frame draws its
# single loop image so scrubbing is one texture per step. Explicit
# linear filtering, exactly as on the Airspace page.
_l2_note = ""
if _hist:
    _rbase = (os.environ.get("RENDER_EXTERNAL_URL")
              or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    _i = len(_hist) - 1 if _pick is None else _pick
    _rstamp, _chunks, _loop = _hist[_i]
    if isinstance(_loop, dict):
        _loop = [_loop]
    # Chunks whenever the frame has them — every loop frame is a full
    # chunk set now; the loop image is only a fallback.
    if not _chunks and _loop:
        _chunks = _loop
    if _chunks and _rbase:
        for _c in _chunks:
            layers.append(pdk.Layer(
                "BitmapLayer", data=None,
                image=f"{_rbase}/app/static/{_c['name']}",
                bounds=_c["bounds"], opacity=float(_l2_alpha),
                texture_parameters={"10241": 9987, "10240": 9729,
                                    "10242": 33071, "10243": 33071}))
        _l2_note = (f" Radar: MRMS {_rstamp[9:11]}:{_rstamp[11:13]}Z"
                    + ("" if _i == len(_hist) - 1 else " (rewound)")
                    + f", {len(_hist)} scan(s) in the last hour.")

# Lobes, coloured by the next 30 minutes.
lobes = []
for name, a0, a1, *_ in GATES:
    col = _status(name)
    lobes.append({"polygon": LOBES[name], "name": name,
                  "fill": col + [170],
                  "line": [max(0, c - 70) for c in col] + [255],
                  "tip": f"{name} gate \u2014 {per_gate[name]['now']} inbound "
                         f"now; next hour by 15 min: "
                         f"{' / '.join(str(x) for x in per_gate[name]['bins'])}"})
layers.append(pdk.Layer(
    "PolygonLayer", data=lobes, get_polygon="polygon", filled=True,
    stroked=True, get_fill_color="fill", get_line_color="line",
    line_width_min_pixels=2, get_line_width=3, pickable=True))

# The count, large, at each lobe's OUTER END — the vertex farthest
# from N90, pulled 30% back toward the centroid. The middle of a lobe
# is where its gates are, and the number was sitting on top of them.
counts = []
for name, a0, a1, *_ in GATES:
    _poly = LOBES[name]
    _cx = sum(p[0] for p in _poly) / len(_poly)
    _cy = sum(p[1] for p in _poly) / len(_poly)
    _far = max(_poly, key=lambda q: math.hypot((q[0] - CENTER[1]) * _k,
                                               q[1] - CENTER[0]))
    lon = _far[0] + (_cx - _far[0]) * 0.30
    lat = _far[1] + (_cy - _far[1]) * 0.30
    counts.append({"lon": lon, "lat": lat, "txt": str(per_gate[name]["now"]),
                   "tip": lobes[[g[0] for g in GATES].index(name)]["tip"]})
layers.append(pdk.Layer(
    "TextLayer", data=counts, get_position="[lon, lat]", get_text="txt",
    get_size=34, get_color=[0, 0, 0], font_weight=800,
    background=True, get_background_color=[255, 255, 255, 220],
    background_padding=[8, 3, 8, 3], pickable=True))

# (The projected-arrival grid per gate is off for now. The lobe colour
# already carries the next-30-minute verdict.)

# JET ROUTES INTO THE GATES, clipped to the N90 outline: each route is
# cut where it crosses the outline so the line runs in from outside
# and stops at the border, never inside the lobes. Identifiers sit at
# the outer end. Routes not in the file (J64, Q42, Q480) are listed
# in the caption rather than silently missing.
JET_ROUTES = ("Q818", "Q436", "J64", "J60", "Q42", "Q480", "J6", "J48",
              "Q75", "Q133", "Q109", "Q64", "Q481")


def _seg_x_poly(a, b, poly):
    """Where segment a->b first crosses the polygon edge, or None."""
    best = None
    for i in range(len(poly)):
        c, d = poly[i], poly[(i + 1) % len(poly)]
        r = (b[0] - a[0], b[1] - a[1])
        q = (d[0] - c[0], d[1] - c[1])
        den = r[0] * q[1] - r[1] * q[0]
        if abs(den) < 1e-12:
            continue
        t = ((c[0] - a[0]) * q[1] - (c[1] - a[1]) * q[0]) / den
        u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den
        if 0 <= t <= 1 and 0 <= u <= 1 and (best is None or t < best):
            best = t
    return None if best is None else [a[0] + r[0] * best, a[1] + r[1] * best]


def _clip_outside(path, poly):
    """Pieces of `path` that lie outside `poly`, each ending at the
    border where it crosses in."""
    pieces, cur = [], []
    for a, b in zip(path, path[1:]):
        ai, bi = FW.pip(a[0], a[1], poly), FW.pip(b[0], b[1], poly)
        if not ai and not bi:
            if not cur:
                cur.append(a)
            cur.append(b)
        elif not ai and bi:
            x = _seg_x_poly(a, b, poly) or b
            if not cur:
                cur.append(a)
            cur.append(x)
            pieces.append(cur)
            cur = []
        elif ai and not bi:
            x = _seg_x_poly(a, b, poly) or a
            cur = [x, b]
        # both inside: drop
    if len(cur) >= 2:
        pieces.append(cur)
    return [pc for pc in pieces if len(pc) >= 2]


_route_rows, _route_lbl, _routes_found = [], [], []
_by_id = {r["ident"]: r for r in data.get("routes", []) if r.get("ident")}
for _rid in JET_ROUTES:
    _r = _by_id.get(_rid)
    if not _r:
        continue
    _routes_found.append(_rid)
    for _pc in _clip_outside(_r["path"], hull) if hull else [_r["path"]]:
        _route_rows.append({"path": _pc, "ident": _rid,
                            "tip": f"{_rid} \u2014 to the N90 border"})
        # LABELS WHERE THEY CAN BE SEEN, but not on the border: the
        # first label sits 40 nm back from where the route meets the
        # outline, then every 50 nm beyond. The border itself has the
        # gate labels and the lobe edges; a route name there is clutter.
        _dist = lambda q: math.hypot((q[0] - CENTER[1]) * _k, q[1] - CENTER[0]) * 60
        _ordered = _pc if _dist(_pc[0]) > _dist(_pc[-1]) else list(reversed(_pc))
        # _ordered runs outer -> border; walk back from the border
        _acc, _last, _first_at = 0.0, None, 40.0
        for _q in reversed(_ordered):
            if _last is not None:
                _acc += math.hypot((_q[0] - _last[0]) * _k, _q[1] - _last[1]) * 60
            if _acc >= _first_at:
                _route_lbl.append({"lon": _q[0], "lat": _q[1], "ident": _rid})
                _acc, _first_at = 0.0, 50.0
            _last = _q
if _route_rows:
    layers.append(pdk.Layer(
        "PathLayer", data=_route_rows, get_path="path",
        get_color=[201, 122, 45, 200], get_width=2,
        width_units='"pixels"', width_min_pixels=2, width_max_pixels=3,
        cap_rounded=True, pickable=True))
    layers.append(pdk.Layer(
        "TextLayer", data=_route_lbl, get_position="[lon, lat]", get_text="ident",
        get_size=12, get_color=[120, 60, 0], font_weight=700,
        background=True, get_background_color=[255, 255, 255, 220],
        background_padding=[4, 2, 4, 2]))

# The N90 outline around the lobes, orange as on every page, and the
# inner core faintly — the line inside which an arrival is no longer
# "at a gate".
if hull:
    layers.append(pdk.Layer(
        "PolygonLayer", data=[{"polygon": hull, "tip": "N90 outline"}],
        get_polygon="polygon", stroked=True, filled=False,
        get_line_color=[201, 122, 45, 230], line_width_min_pixels=2,
        get_line_width=3, pickable=True))
if core:
    layers.append(pdk.Layer(
        "PolygonLayer", data=[{"polygon": core,
                               "tip": f"terminal core, {CORE_NM:.0f} nm"}],
        get_polygon="polygon", stroked=True, filled=False,
        get_line_color=[201, 122, 45, 110], line_width_min_pixels=1,
        get_line_width=1))

# THE ARRIVAL GATES, every one given a lobe. Only fixes the file marks
# as arrival gates (role coord or both) are drawn; departure fixes are
# not on this page. A gate inside a lobe belongs to it; a gate in a
# gap — LENDY, CCC, KORRY and fourteen others — goes to the NEAREST
# lobe by bearing, with a thin connector to the lobe's edge so the
# picture still matches the CWSU shapes. Every gate label is white on
# its lobe's status colour: red if that gate's demand is high, green
# if normal or low.
_gate_fix, _links = [], []
for f in data.get("fixes", []):
    if f.get("role") not in ("coord", "both") or f.get("name") == "JFK":
        continue
    _l = next((n for n, poly in LOBES.items()
               if FW.pip(f["lon"], f["lat"], poly)), None)
    _in = _l is not None
    if _l is None:
        # Nearest lobe by EDGE, not by bearing range: KORRY sits in the
        # SOUTH-WEST gap a few miles off SOUTH's edge and belongs there,
        # whatever its bearing says. Aircraft still assign by bearing.
        _best = None
        for _n, _poly in LOBES.items():
            for _q in _poly:
                _d = math.hypot((_q[0] - f["lon"]) * _k, _q[1] - f["lat"])
                if _best is None or _d < _best[0]:
                    _best = (_d, _n, _q)
        _l, _near = _best[1], _best[2]
        _links.append({"path": [[f["lon"], f["lat"]], _near],
                       "col": [max(0, c - 40) for c in _status(_l)] + [160]})
    _c = _status(_l)
    _gate_fix.append({"lon": f["lon"], "lat": f["lat"], "name": f["name"],
                      "nav": f.get("nav") or IC.nav_icon(IC.nav_kind(f["name"])),
                      "bg": _c + [235], "tri": [max(0, c - 90) for c in _c],
                      # black text reads on green; white on red
                      "fg": [0, 0, 0] if _c == GREEN else [255, 255, 255],
                      "size": 12 if _in else 10,
                      "tip": f"{f['name']} \u2014 {_l} gate"
                             + ("" if _in else " (assigned, in a gap)")
                             + f"; demand {'HIGH' if _c == RED else 'normal'}"})
if _links:
    layers.append(pdk.Layer(
        "PathLayer", data=_links, get_path="path", get_color="col",
        get_width=1, width_units='"pixels"', width_min_pixels=1,
        width_max_pixels=1, pickable=False))
if _gate_fix:
    layers.append(pdk.Layer(
        "IconLayer", data=_gate_fix, get_position="[lon, lat]",
        get_icon="nav", get_size=16, size_min_pixels=12, size_max_pixels=20,
        get_color="tri", pickable=True))
    # TextLayer cannot stroke its background box, so the black border
    # is a second, slightly larger text layer drawn first: same text,
    # black background, one pixel more padding all round.
    layers.append(pdk.Layer(
        "TextLayer", data=_gate_fix, get_position="[lon, lat]", get_text="name",
        get_size="size", get_color=[0, 0, 0, 0], font_weight=700,
        get_text_anchor='"start"', get_pixel_offset=[8, -8],
        background=True, get_background_color=[0, 0, 0, 255],
        background_padding=[6, 3, 6, 3], pickable=False))
    layers.append(pdk.Layer(
        "TextLayer", data=_gate_fix, get_position="[lon, lat]", get_text="name",
        get_size="size", get_color="fg", font_weight=700,
        get_text_anchor='"start"', get_pixel_offset=[8, -8],
        background=True, get_background_color="bg",
        background_padding=[5, 2, 5, 2], pickable=True))

# LIVE AIRCRAFT: everything inside the N90 outline or within
# AC_RING_NM of it, below AC_CEIL_FT. That is the gate-crossing
# population — CAMRN and LENDY are crossed at 11-14,000 — plus the
# low traffic, and it includes TEB's N-numbers, which the Airspace
# page drops. Same silhouettes, colours and tooltip boxes as the
# Airspace page, from core/icons.py.
def _dist_to_outline_nm(lon, lat):
    """Distance to the N90 outline edge; 0 if inside."""
    if not hull:
        return 999.0
    if FW.pip(lon, lat, hull):
        return 0.0
    best = 999.0
    for i in range(len(hull)):
        a, b = hull[i], hull[(i + 1) % len(hull)]
        ax, ay = (a[0] - lon) * 60 * _k, (a[1] - lat) * 60
        bx, by = (b[0] - lon) * 60 * _k, (b[1] - lat) * 60
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _ac_row(r):
    cs = r["cs"]
    box, txt = IC._tcol(cs)
    alt = r["_alt"]
    fl = f"FL{alt // 100:03d}" if alt >= 18000 else f"{alt:,} ft"
    # STARS: tinted square, leader + data block for JetBlue and majors.
    return {"lon": r["lon"], "lat": r["lat"],
            "atc": IC.atc_icon(IC.has_block(cs)), "acol": IC.atc_color(cs),
            "block": IC.data_block(cs, alt, r["_gs"]) if IC.has_block(cs) else "",
            "tcol": box, "ttxt": txt,
            "tip": f"{IC._who(cs)} | {fl} | {r['_gs']:.0f} kt"
                   + (f" | {r['typ']}" if r["typ"] else "")
                   + (f" | squawk {r['sq']}" if r["sq"] in ("7500", "7600", "7700") else "")}


_ac_rows = [_ac_row(r) for r in rows
            if r["_alt"] <= AC_CEIL_FT
            and _dist_to_outline_nm(r["lon"], r["lat"]) <= AC_RING_NM] \
    if _show_ac else []
if _ac_rows:
    layers.append(pdk.Layer(
        "IconLayer", data=_ac_rows, get_icon="atc", get_color="acol",
        get_position="[lon, lat]", get_size=26,
        size_min_pixels=18, size_max_pixels=32, pickable=True))
    _blk = [r for r in _ac_rows if r["block"]]
    if _blk:
        layers.append(pdk.Layer(
            "TextLayer", data=_blk, get_position="[lon, lat]",
            get_text="block", get_size=11, get_color="acol",
            font_family="monospace", font_weight=700,
            get_text_anchor='"start"', get_alignment_baseline='"bottom"',
            line_height=1.1, get_pixel_offset=[13, -11],
            background=True, get_background_color=[255, 255, 255, 215],
            background_padding=[3, 1, 3, 1]))

# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
_stamp = _now.strftime("%H%M UTC %d %b %Y")
_hd = st.columns([3, 1])
_hd[0].markdown(
    "**Gate Status for KNYC** &mdash; live &middot; N90 landing rate "
    f"{aar_total:.0f}/h ("
    + ", ".join(f"{c} {v:.0f}{'' if src == 'AADC' else '*'}" for c, (v, src) in aars.items())
    + f") &rarr; {cap15:.1f} per gate per 15 min"
    + (" &middot; *default, no AADC rate" if any(src != "AADC" for _v, src in aars.values()) else ""))
_hd[1].markdown(f"<div style='text-align:right'><b>{_stamp}</b></div>",
                unsafe_allow_html=True)

_tot = sum(g["now"] for g in per_gate.values())
_tiles = st.columns(4)
for i, (name, *_) in enumerate(GATES):
    g = per_gate[name]
    col = _status(name)
    _tiles[i].markdown(
        f"<div style='border:2px solid #000;background:rgb({col[0]},{col[1]},"
        f"{col[2]});padding:6px 10px;text-align:center;font-family:Times New "
        f"Roman,serif'><div style='font-size:12px;letter-spacing:1px'>{name}"
        f"</div><div style='font-size:30px;font-weight:700;line-height:1.05'>"
        f"{g['now']}</div><div style='font-size:11px'>"
        f"{'DEMAND HIGH' if col == RED else 'normal'} &middot; next 30 min "
        f"{g['bins'][0] + g['bins'][1]}</div></div>",
        unsafe_allow_html=True)

st.pydeck_chart(pdk.Deck(
    layers=layers,
    initial_view_state=pdk.ViewState(latitude=CENTER[0] + 0.15,
                                     longitude=CENTER[1] - 0.5, zoom=6.3,
                                     min_zoom=5, max_zoom=10),
    map_style="light",
    tooltip={
        "html": '<div style="background:#333333;background:{tcol};'
                'color:#FFFFFF;color:{ttxt};border:2px solid #000000;'
                'padding:6px 10px;font-weight:bold;'
                'font-family:Times New Roman,serif;">{tip}</div>',
        "style": {"backgroundColor": "transparent", "padding": "0",
                  "border": "none", "boxShadow": "none"},
    },
), height=1150)   # roughly square at full page width

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
with st.expander("Radar and traffic diagnostics", expanded=not _hist):
    import threading as _th

    _alive = any(t.name == "l2-warmer" for t in _th.enumerate())
    try:
        import metpy  # noqa: F401

        _mp = "importable"
    except Exception as _mexc:
        _mp = f"IMPORT FAILED: {type(_mexc).__name__}: {_mexc}"
    _man_p = _STATIC / "l2_KOKX.json"
    _log_p = _STATIC / "l2_warmer.log"
    # Where the daemon says it is, and for how long; and who holds
    # the heavy lock. A thread that is alive but silent is one of
    # these two things, and this is the row that says which.
    _stp = _STATIC / "l2_state.json"
    try:
        _sd = json.loads(_stp.read_text()) if _stp.exists() else {}
        _step = (f"{_sd.get('step', '?')} for "
                 f"{(datetime.now(timezone.utc).timestamp() - _sd.get('since', 0)) / 60:.1f} min"
                 + (f" (lock held by {_sd['holder']} {_sd.get('holder_s', 0)}s at that point)"
                    if _sd.get("holder") else ""))
    except Exception as _sexc:
        _step = f"unreadable: {_sexc}"
    try:
        from core.warmlock import held_by as _hb

        _hh, _hs = _hb()
        _lock = f"held by {_hh} for {_hs:.0f}s" if _hh else "free"
    except Exception:
        _lock = "warmlock not importable"
    _nlow = sum(1 for r in rows if r["_alt"] <= AC_CEIL_FT)
    _nring = sum(1 for r in rows if r["_alt"] <= AC_CEIL_FT
                 and _dist_to_outline_nm(r["lon"], r["lat"]) <= AC_RING_NM)
    _rows = [
        ("Level II (parked) warmer thread", "alive" if _alive else
         "NOT RUNNING \u2014 Homepage.py with ensure_l2_warmer() not deployed, "
         "or L2_WARMER=off"),
        ("warmer is at", _step),
        ("heavy lock", _lock),
        ("aircraft funnel", f"{len(rows)} within 250 nm \u2192 {_nlow} below "
                            f"FL{AC_CEIL_FT // 100} \u2192 {_nring} within "
                            f"{AC_RING_NM:.0f} nm of N90 \u2192 toggle "
                            f"{'ON' if _show_ac else 'OFF'} \u2192 "
                            f"{len(_ac_rows)} drawn; hull {len(hull)} vertices"),
        ("metpy", _mp),
        ("MRMS frames", f"{len(_hist)} in the last {RADAR_HOURS:g} h"
                        + (f" \u2014 {_hist_err}" if _hist_err else "")),
        ("Level II manifest", (f"{_man_p.name} present"
                               if _man_p.exists() else "not written yet")),
        ("static dir", str(_STATIC)),
        ("traffic fetch", st.session_state.get("_gates_traffic_err") or
         f"ok, {len(rows)} aircraft"),
    ]
    st.markdown("\n".join(f"| {k} | {v} |" for k, v in
                          [("**item**", "**status**")] + _rows).replace(
        "| **item** | **status** |", "| item | status |\n|---|---|"))
    if _log_p.exists():
        _tail = _log_p.read_text().splitlines()[-12:]
        st.code("\n".join(_tail) or "(empty)", language="text")
    else:
        st.caption("l2_warmer.log not written yet \u2014 the warmer logs its "
                   "first line 150 s after the service starts.")

st.caption(
    f"{_tot} inbound to N90 now across the four gates, from {len(rows)} "
    f"aircraft within 250 nm; {len(_ac_rows)} drawn (inside N90 or within "
    f"{AC_RING_NM:.0f} nm of it, below FL{AC_CEIL_FT // 100}, every "
    f"operator). DEMAND: commercial aircraft outside the "
    f"terminal core, within 200 nm, tracking toward N90, descending or "
    f"below FL180, and NOT bound elsewhere according to the route lookup "
    f"({sum(1 for r in inbound if (dests.get(r['cs']) or {}).get('dest'))} "
    f"of {_tot} have a known destination). CAPACITY: the N90 airports' "
    f"AARs summed ({aar_total:.0f}/h; JFK, LGA and EWR from the FAA AADC, "
    f"TEB and HPN defaults) and split four ways. A lobe and its gates are "
    f"RED when projected arrivals in the next 30 min reach {HIGH_RATIO:.0%} "
    f"of that share, GREEN under. Gates in a gap go to the nearest lobe "
    f"with a connector. Jet routes drawn to the N90 border: "
    + ", ".join(_routes_found)
    + (f"; not in the route file: {', '.join(r for r in JET_ROUTES if r not in _by_id)}" if any(r not in _by_id for r in JET_ROUTES) else "")
    + ". Advisory only."
    + _l2_note
)
