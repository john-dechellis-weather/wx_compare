"""A 30 x 20 nm scope of any airport.

Target path: core/airport_scope.py

The field drawn from OpenStreetMap, range rings, final approach
courses with mile ticks off the runways in use, and live traffic
with tags. Rendered as pydeck layers, because that is what the rest
of BlueMet draws with.

Generalised from jfk_detail.py, which was JFK-only: a fixed centre,
a fixed bounding box and eight named runway ends. Here the centre is
whatever airport was asked for, the box is derived from it, and the
runway ends come out of the OSM geometry.

    scope(static_dir, icao, lat, lon)   everything, as one dict of
                                        pydeck layers + metadata

Network: Overpass for the surface (cached to disk for a week) and
one adsb.lol/adsb.fi point query for traffic. Both are best effort -
each returns empty rather than raising, because a missing surface
should cost the range rings, not the page.
"""

from __future__ import annotations

import math
from pathlib import Path
import os
import re

NM_PER_DEG = 60.0

# The scope: 30 nm across, 20 nm tall.
SCOPE_W_NM = float(os.environ.get("BLUEMET_SCOPE_W_NM", "30"))
SCOPE_H_NM = float(os.environ.get("BLUEMET_SCOPE_H_NM", "20"))
RING_NMS = (10.0, 20.0, 30.0)
FINAL_NM = 15.0

# Colours, matching the Ops Black palette.
C_RUNWAY = [235, 235, 235, 255]
C_TAXIWAY = [120, 120, 128, 200]
C_APRON = [38, 42, 50, 255]
C_TERMINAL = [205, 205, 212, 235]
C_RING = [150, 150, 160, 150]
C_FINAL = [0, 255, 127, 210]
C_FINAL_IDLE = [90, 96, 100, 120]
C_DEP = [255, 138, 0, 230]


# ---------------------------------------------------------------- math

def _k(lat: float) -> float:
    return math.cos(math.radians(lat))


def offset(lat, lon, d_nm, brg_deg):
    """Point d_nm from (lat, lon) on a true bearing. Flat-earth, which
    is right to well under a metre at scope range."""
    b = math.radians(brg_deg)
    return (lat + d_nm * math.cos(b) / NM_PER_DEG,
            lon + d_nm * math.sin(b) / (NM_PER_DEG * _k(lat)))


def bearing(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * _k(lat1) * NM_PER_DEG
    dy = (lat2 - lat1) * NM_PER_DEG
    return math.degrees(math.atan2(dx, dy)) % 360.0


def distance_nm(lat1, lon1, lat2, lon2) -> float:
    dx = (lon2 - lon1) * _k(lat1) * NM_PER_DEG
    dy = (lat2 - lat1) * NM_PER_DEG
    return math.hypot(dx, dy)


def zoom_for(lat: float, width_nm: float = SCOPE_W_NM,
             width_px: int = 1000) -> float:
    """Web-mercator zoom that puts width_nm across width_px pixels."""
    m_per_px = (width_nm * 1852.0) / max(width_px, 1)
    return math.log2(156543.03392 * _k(lat) / m_per_px)


# ------------------------------------------------------- the surface

def _code(icao: str) -> str:
    """KJFK -> JFK, the key core/surface.py and core/runways.py use.
    Delegates to core/airports so the KPBI -> KDJT alias holds."""
    from core import airports as _AP
    return _AP.code(icao)


def surface(static_dir, icao: str, lat: float, lon: float) -> dict:
    """The field from OpenStreetMap, via core/surface.py.

    NEVER BLOCKS. A cached surface returns at once; a missing one
    starts a background Overpass fetch and returns {} - the scope
    draws rings and traffic, and the field appears on a later rerun.
    """
    from core import surface as _SF
    code = _code(icao)
    _SF.register(code, lat, lon)
    d = _SF.load(static_dir, code)
    if not d:
        _SF.fetch_async(static_dir, code)
        return {}
    # runways.runway_ends() attributes each runway by its "apt" key and
    # defaults to JFK; set it so BOS runways are BOS runways.
    for rw in d.get("runways", []):
        rw.setdefault("apt", code)
    return d


def surface_pending(icao: str) -> bool:
    from core import surface as _SF
    return _SF.fetching(_code(icao))


# ------------------------------------------------------ runway ends

def best_runway_ends(icao: str, sf: dict) -> tuple:
    """(ends, source). The runway table first, OSM second.

    The table (core/airports.py) knows where each LANDING threshold is,
    displaced thresholds included; OSM knows only where the pavement
    ends. So OSM is used only for a field the table has no geometry
    for (TVSA today).
    """
    from core import airports as _AP
    ends = _AP.runway_ends(icao)
    if ends:
        return ends, "runway table"
    ends = runway_ends(sf) if sf else []
    return ends, ("OpenStreetMap" if ends else "")


def runway_ends(sf: dict) -> list:
    """[{apt, end, thr, far, hdg}] for every named runway end.

    Delegates to core/runways.py rather than repeating its geometry:
    that module already binds each designator to the right physical
    end (the designator is the approximate magnetic heading, and the
    two candidates are 180 deg apart, so variation cannot flip it),
    and the finals module expects exactly its output shape.
    """
    from core import runways as _RW
    return _RW.runway_ends(sf)


# ---------------------------------------------------- finals + rings

def finals(ends, active=None, out_nm: float = FINAL_NM,
           major_tick_nm: float = 3.0, minor_tick_nm: float = 1.0):
    """Extended centreline off each end with distance ticks.

    active: the runway ends in use. Those draw lit; the rest draw
    dim, so the picture still shows the field's geometry without
    claiming every runway is active.
    """
    from core import atis as _AT
    # ATIS says "4R", runway ends are "04R" - normalise before matching
    # or a whole configuration silently fails to light up.
    act = {_AT.norm(a) for a in (active or [])}
    lines, ticks, labels = [], [], []
    for e in ends:
        lit = (not act) or (_AT.norm(e["end"]) in act)
        col = C_FINAL if (lit and act) else C_FINAL_IDLE
        tlon, tlat = e["thr"]
        back = (e["hdg"] + 180.0) % 360.0
        far = offset(tlat, tlon, out_nm, back)
        lines.append({"path": [[tlon, tlat], [far[1], far[0]]],
                      "color": col, "end": e["end"],
                      "width": 2 if lit and act else 1})
        d = minor_tick_nm
        while d <= out_nm + 1e-6:
            major = abs((d / major_tick_nm)
                        - round(d / major_tick_nm)) < 1e-6
            half = 0.45 if major else 0.22
            c = offset(tlat, tlon, d, back)
            p1 = offset(c[0], c[1], half, (e["hdg"] + 90) % 360)
            p2 = offset(c[0], c[1], half, (e["hdg"] + 270) % 360)
            ticks.append({"path": [[p1[1], p1[0]], [p2[1], p2[0]]],
                          "color": col,
                          "width": 2 if major else 1})
            d += minor_tick_nm
        if lit and act:
            lab = offset(tlat, tlon, out_nm + 1.0, back)
            labels.append({"position": [lab[1], lab[0]],
                           # Not "ILS": the scope knows the runway, not
                           # the approach type - the ATIS line says that.
                           "text": f"RWY {e['end'].lstrip('0')}",
                           "color": C_FINAL})
    return lines, ticks, labels


def departure_marks(ends, departing=None):
    """A label at each departure end, as 'DEP 22R'."""
    from core import atis as _AT
    dep = {_AT.norm(d) for d in (departing or [])}
    out = []
    for e in ends:
        if _AT.norm(e["end"]) in dep:
            tlon, tlat = e["thr"]
            p = offset(tlat, tlon, 0.8, (e["hdg"] + 180.0) % 360.0)
            out.append({"position": [p[1], p[0]],
                        "text": f"DEP {e['end']}", "color": C_DEP})
    return out


def rings(lat, lon, nms=RING_NMS, steps: int = 180):
    """Range rings, plus a label on each at due east."""
    circles, labels = [], []
    for nm in nms:
        path = []
        for i in range(steps + 1):
            p = offset(lat, lon, nm, 360.0 * i / steps)
            path.append([p[1], p[0]])
        circles.append({"path": path, "nm": nm})
        lp = offset(lat, lon, nm, 90.0)
        labels.append({"position": [lp[1], lp[0]], "text": f"{nm:g}"})
    return circles, labels


# ----------------------------------------------------------- traffic

_CALLSIGN_RE = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]?$")


def traffic(lat: float, lon: float, radius_nm: float = 30.0) -> list:
    """Every aircraft within radius_nm, from the community ADS-B
    point endpoints. One request, one fallback host.

    Point-and-radius, so this is a single small query rather than the
    tiled sweep the CONUS map runs - it does not add to that rate.
    """
    import requests
    hdrs = {"User-Agent": "bluemet.org ops dashboard"}
    urls = (f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/"
            f"{int(radius_nm)}",
            f"https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/"
            f"lon/{lon:.4f}/dist/{int(radius_nm)}")
    rows = []
    for u in urls:
        try:
            r = requests.get(u, timeout=8, headers=hdrs)
            if r.status_code != 200:
                continue
            rows = (r.json() or {}).get("ac") or []
            if rows:
                break
        except Exception:
            continue

    # Feeds overlap and repeat; dedupe on hex, callsign and rounded
    # position at the source rather than per layer.
    seen, out = set(), []
    for a in rows:
        cs = (a.get("flight") or "").strip().upper()
        hexid = (a.get("hex") or "").strip().lower()
        try:
            alat, alon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (hexid, cs, round(alat, 3), round(alon, 3))
        if key in seen:
            continue
        seen.add(key)
        if not cs:
            continue
        jbu = cs.startswith("JBU")
        out.append({
            "position": [alon, alat],
            "callsign": cs,
            "hdg": float(a.get("track") or 0.0),
            "alt": a.get("alt_baro"),
            "gs": a.get("gs"),
            "type": (a.get("t") or "").strip().upper(),
            "jbu": jbu,
            # commercial = an airline callsign (three letters + flight
            # number), which drops N-numbers and most GA and military
            "airline": _CALLSIGN_RE.match(cs) is not None,
            "label": cs,
        })
    return out


# --------------------------------------------------------------- ATIS

def atis(icao: str) -> dict:
    """Runway configuration from the FAA D-ATIS relay.

    core/atis.py owns the fetch, the cache and the parsing. This
    wrapper only reshapes it for the scope and never raises.
    """
    from core import atis as _AT
    cfg = _AT.config(icao)
    return {"raw": cfg.get("raw", ""), "code": cfg.get("code", ""),
            "arriving": cfg.get("arr") or [],
            "departing": cfg.get("dep") or [],
            "err": cfg.get("err", ""),
            "describe": _AT.describe(cfg)}


# ------------------------------------------------------ pydeck layers

def layers(sf: dict, ends: list, lat: float, lon: float,
           arriving=None, departing=None, ac=None,
           show_traffic: bool = True) -> list:
    """Every layer of the scope, bottom to top.

    SIZING RULE: every width, radius and text size is in METRES with a
    min/max pixel clamp. Never width_units / radius_units / size_units
    = "pixels": the pydeck build on Render ignores those props, which
    is what drew the rings and finals as bands miles wide.
    """
    import pydeck as pdk
    out = []

    if sf.get("aprons"):
        out.append(pdk.Layer(
            "SolidPolygonLayer",
            [{"polygon": a["polygon"]} for a in sf["aprons"]
             if len(a.get("polygon") or []) >= 3],
            get_polygon="polygon", get_fill_color=C_APRON,
            pickable=False))
    if sf.get("terminals"):
        out.append(pdk.Layer(
            "SolidPolygonLayer",
            [{"polygon": t["polygon"]} for t in sf["terminals"]
             if len(t.get("polygon") or []) >= 3],
            get_polygon="polygon", get_fill_color=C_TERMINAL,
            pickable=False))
    if sf.get("taxiways"):
        out.append(pdk.Layer(
            "PathLayer",
            [{"path": t["path"]} for t in sf["taxiways"]],
            get_path="path", get_color=C_TAXIWAY, get_width=20,
            width_min_pixels=1, width_max_pixels=3, pickable=False))
    if sf.get("runways"):
        out.append(pdk.Layer(
            "PathLayer",
            [{"path": r["path"]} for r in sf["runways"]],
            get_path="path", get_color=C_RUNWAY, get_width=55,
            width_min_pixels=2, width_max_pixels=7, pickable=False))

    circles, ring_labels = rings(lat, lon)
    out.append(pdk.Layer(
        "PathLayer", circles, get_path="path", get_color=C_RING,
        get_width=40, width_min_pixels=1, width_max_pixels=1,
        pickable=False))
    out.append(pdk.Layer(
        "TextLayer", ring_labels, get_position="position", get_text="text",
        get_color=C_RING, get_size=2000, size_min_pixels=0,
        size_max_pixels=11, get_pixel_offset=[8, 0],
        get_text_anchor='"start"', pickable=False))

    fin, ticks, fin_labels = finals(ends, active=arriving)
    if ticks:
        out.append(pdk.Layer(
            "PathLayer", ticks, get_path="path", get_color="color",
            get_width=40, width_min_pixels=1, width_max_pixels=1.5,
            pickable=False))
    if fin:
        out.append(pdk.Layer(
            "PathLayer", fin, get_path="path", get_color="color",
            get_width=60, width_min_pixels=1, width_max_pixels=2,
            pickable=False))
    if fin_labels:
        out.append(pdk.Layer(
            "TextLayer", fin_labels, get_position="position",
            get_text="text", get_color="color", get_size=2400,
            size_min_pixels=0, size_max_pixels=12,
            get_text_anchor='"middle"', pickable=False))

    dep = departure_marks(ends, departing)
    if dep:
        out.append(pdk.Layer(
            "TextLayer", dep, get_position="position", get_text="text",
            get_color="color", get_size=2400, size_min_pixels=0,
            size_max_pixels=12, get_text_anchor='"middle"',
            pickable=False))

    if show_traffic and ac:
        out.extend(aircraft_layers(ac))
    return out


def aircraft_layers(ac: list) -> list:
    """The twin-podded icon in the carrier's colour, rotated to track,
    and the r202 data card beside it.

    Both are IconLayers sized in METRES with min == max pixel clamps,
    which pins them to an exact pixel size at any zoom - the pattern
    pages/3 has shipped every icon through. A layer's clamp is one
    value, so cards are grouped by their pixel height: three layers
    at most (plain, mark-on-top, compact).
    """
    import pydeck as pdk
    from core import cards as _C

    planes, by_h = [], {}
    for a in ac:
        cs = a.get("callsign", "")
        planes.append({"position": a["position"],
                       "icon": _C.plane_icon(_C.colour(cs)),
                       # IconLayer turns counter-clockwise; track is
                       # clockwise from north
                       "angle": (360.0 - float(a.get("hdg") or 0)) % 360.0,
                       "callsign": cs, "alt": a.get("alt"),
                       "gs": a.get("gs"), "type": a.get("type")})
        icon = _C.card(a)
        h = icon.pop("px_h")
        by_h.setdefault(h, []).append({"position": a["position"],
                                       "icon": icon})
    out = []
    for h, rows in sorted(by_h.items()):
        out.append(pdk.Layer(
            "IconLayer", rows, get_position="position", get_icon="icon",
            get_size=5000, size_min_pixels=h, size_max_pixels=h,
            pickable=False))
    # aircraft drawn last so they sit on top of every leader line
    out.append(pdk.Layer(
        "IconLayer", planes, get_position="position", get_icon="icon",
        get_angle="angle", get_size=5000,
        size_min_pixels=22, size_max_pixels=22,
        pickable=True, auto_highlight=False))
    return out


def coast_layer(icao: str):
    """Natural Earth coastline and lake shore for the station, as a
    PathLayer. The FALLBACK basemap: coarse (about a kilometre) but
    drawn entirely by this app, so it cannot fail to load."""
    import json as _json
    import pydeck as pdk
    from core import airports as _AP
    p = (Path(__file__).resolve().parent.parent / "static"
         / "jbu_coast.json")
    try:
        segs = _json.loads(p.read_text()).get(_AP.canonical(icao)) or []
    except Exception:
        segs = []
    if not segs:
        return None
    return pdk.Layer(
        "PathLayer", [{"path": sg} for sg in segs], get_path="path",
        get_color=[59, 74, 99, 255], get_width=40,
        width_min_pixels=1, width_max_pixels=1.2, pickable=False)


def view(lat: float, lon: float, width_px: int = 1000):
    """Fixed view: SCOPE_W_NM across, centred on the field."""
    import pydeck as pdk
    z = zoom_for(lat, SCOPE_W_NM, width_px)
    return pdk.ViewState(latitude=lat, longitude=lon, zoom=z,
                         min_zoom=z - 2, max_zoom=z + 3,
                         bearing=0, pitch=0)


def height_px(width_px: int = 1000) -> int:
    """Pixel height that makes the scope SCOPE_H_NM tall."""
    return int(round(width_px * (SCOPE_H_NM / SCOPE_W_NM)))
