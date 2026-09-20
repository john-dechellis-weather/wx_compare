"""JFK airport surface from OpenStreetMap: gates, taxiways, runways.

WHERE THE DATA COMES FROM. The FAA publishes runway endpoints in open
GIS but not taxiways or gates. OpenStreetMap has JFK mapped to the
gate — `aeroway=runway`, `aeroway=taxiway` with `ref` identifiers,
`aeroway=apron` polygons, `aeroway=terminal` outlines and
`aeroway=gate` nodes carrying their numbers. It is the source every
open airport-diagram app draws from. The Overpass API serves a
bounding box of it as JSON on request.

FETCHED ONCE, CACHED TO DISK. The geometry changes when a taxiway is
renamed, not every minute, so it is written to static/ on first use
and refreshed after SURFACE_MAX_AGE_S. A failed refresh keeps the old
file. The page never blocks on Overpass: if the cache is missing, the
warmer fetches it and the page shows no surface until it lands.

CLASSIFICATION of an aircraft on the ground, in priority order:
  runway   — within RUNWAY_M of a runway centreline
  gate     — within GATE_M of a gate node (reports the gate ref)
  taxiway  — within TAXI_M of a taxiway centreline (reports the ref)
  apron    — inside an apron polygon
  ground   — none of the above
Runway first because an aircraft on a runway is the one state that
matters most, and taxiway lines cross runways.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

# The three airports the surface layers cover, each with a margin
# for the approach ends. s, w, n, e.
AIRPORTS = {
    "JFK": (40.615, -73.840, 40.670, -73.740),
    "LGA": (40.760, -73.900, 40.795, -73.850),
    "EWR": (40.665, -74.205, 40.720, -74.150),
}
BBOX = AIRPORTS["JFK"]


def register(code: str, lat: float, lon: float, half_nm: float = 3.5):
    """Add an airport by its centre. A 7 nm box covers the runways and
    aprons of any US commercial field; the approach ends past that are
    drawn by the scope from the runway geometry, not from OSM."""
    code = (code or "").upper()
    if not code or code in AIRPORTS:
        return
    dlat = half_nm / 60.0
    dlon = half_nm / (60.0 * math.cos(math.radians(lat)))
    AIRPORTS[code] = (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


_FETCHING = set()


def fetch_async(outdir, code: str) -> None:
    """Fetch and cache one airport on a background thread.

    The page must never block on Overpass (a cold query is 5-30 s), so
    a missing cache kicks this and the page draws without a surface
    until the file lands. One thread per airport at a time."""
    code = (code or "").upper()
    with _LOCK:
        if code in _FETCHING or code not in AIRPORTS:
            return
        _FETCHING.add(code)

    def _run():
        try:
            d = fetch(code=code)
            if d.get("runways"):
                p = _path(outdir, code)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(d))
        except Exception:
            pass
        finally:
            with _LOCK:
                _FETCHING.discard(code)

    threading.Thread(target=_run, daemon=True).start()


def fetching(code: str) -> bool:
    with _LOCK:
        return (code or "").upper() in _FETCHING
OVERPASS = "https://overpass-api.de/api/interpreter"
SURFACE_MAX_AGE_S = 7 * 86400
RUNWAY_M, GATE_M, TAXI_M = 45.0, 60.0, 30.0

_LOCK = threading.Lock()
_DATA = {"loaded": None, "surface": None}

_QUERY_T = """
[out:json][timeout:90];
(
  way["aeroway"~"^(runway|taxiway|apron|terminal)$"]({s},{w},{n},{e});
  node["aeroway"="gate"]({s},{w},{n},{e});
  node["aeroway"="holding_position"]({s},{w},{n},{e});
);
out body;
>;
out skel qt;
"""


def _query(code: str) -> str:
    s, w, n, e = AIRPORTS[code]
    return _QUERY_T.format(s=s, w=w, n=n, e=e)


QUERY = _query("JFK")


def parse_overpass(payload: dict) -> dict:
    """Overpass JSON -> {"runways", "taxiways", "aprons", "terminals",
    "gates", "holds"}. Ways are resolved to coordinate lists through
    the node table Overpass returns alongside them."""
    nodes = {}
    for el in payload.get("elements", []):
        if el.get("type") == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
    out = {"runways": [], "taxiways": [], "aprons": [], "terminals": [],
           "gates": [], "holds": []}
    for el in payload.get("elements", []):
        t = el.get("type")
        tags = el.get("tags") or {}
        aw = tags.get("aeroway")
        if t == "node":
            if aw == "gate":
                out["gates"].append({"ref": tags.get("ref") or "",
                                     "lon": el["lon"], "lat": el["lat"]})
            elif aw == "holding_position":
                out["holds"].append({"ref": tags.get("ref") or "",
                                     "lon": el["lon"], "lat": el["lat"]})
            continue
        if t != "way":
            continue
        coords = [nodes[n] for n in el.get("nodes", []) if n in nodes]
        if len(coords) < 2:
            continue
        rec = {"ref": tags.get("ref") or tags.get("name") or "",
               "path": [[x, y] for x, y in coords]}
        if aw == "runway":
            out["runways"].append(rec)
        elif aw == "taxiway":
            out["taxiways"].append(rec)
        elif aw == "apron":
            out["aprons"].append({"name": tags.get("name") or "",
                                  "polygon": rec["path"]})
        elif aw == "terminal":
            out["terminals"].append({"name": tags.get("name") or "",
                                     "polygon": rec["path"]})
    return out


def fetch(timeout: int = 100, code: str = "JFK") -> dict:
    import requests

    r = requests.post(OVERPASS, data={"data": _query(code)}, timeout=timeout,
                      headers={"User-Agent": "bluemet.org ops"})
    r.raise_for_status()
    d = parse_overpass(r.json())
    d["apt"] = code
    return d


def _path(outdir, code: str = "JFK") -> Path:
    # JFK keeps its original file name so a cached copy still loads.
    return Path(outdir) / ("jfk_surface.json" if code == "JFK"
                           else f"{code.lower()}_surface.json")


_CACHE = {}       # code -> (mtime, surface)


def load(outdir, code: str = "JFK") -> dict:
    """The cached surface for one airport, or an empty dict. Never
    fetches."""
    p = _path(outdir, code)
    if not p.exists():
        return {}
    mt = p.stat().st_mtime
    with _LOCK:
        hit = _CACHE.get(code)
        if hit and hit[0] == mt:
            return hit[1]
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {}
    d.setdefault("apt", code)
    with _LOCK:
        _CACHE[code] = (mt, d)
    return d


def load_all(outdir) -> dict:
    """Every cached airport merged into one surface dict, each
    feature tagged with its airport in `apt`."""
    out = {"runways": [], "taxiways": [], "aprons": [], "terminals": [],
           "gates": [], "holds": [], "apts": []}
    for code in AIRPORTS:
        d = load(outdir, code)
        if not d:
            continue
        out["apts"].append(code)
        for k in ("runways", "taxiways", "aprons", "terminals", "gates", "holds"):
            for f in d.get(k) or []:
                out[k].append(dict(f, apt=code))
    return out


def refresh_if_stale(outdir) -> str:
    """Fetch and write any airport whose cache is missing or old. Called
    by the warmer, never by the page; one Overpass call per airport,
    politely spaced. Returns a one-line note."""
    notes = []
    for code in AIRPORTS:
        p = _path(outdir, code)
        if p.exists() and time.time() - p.stat().st_mtime < SURFACE_MAX_AGE_S:
            notes.append(f"{code} cached")
            continue
        try:
            d = fetch(code=code)
            if not d.get("runways"):
                notes.append(f"{code}: Overpass returned no runways; kept old")
                continue
            Path(outdir).mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d))
            notes.append(f"{code}: {len(d['runways'])} runways, "
                         f"{len(d['taxiways'])} taxiways, {len(d['gates'])} gates")
            time.sleep(5)
        except Exception as exc:
            notes.append(f"{code}: {type(exc).__name__}")
    return "; ".join(notes)


def _m_per_deg(lat):
    return 111320.0 * math.cos(math.radians(lat)), 111320.0


def _dist_to_path_m(lon, lat, path):
    kx, ky = _m_per_deg(lat)
    best = float("inf")
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        ax, ay = (x0 - lon) * kx, (y0 - lat) * ky
        bx, by = (x1 - lon) * kx, (y1 - lat) * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best:
            best = d
    return best


def _pip(lon, lat, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > lat) != (y1 > lat):
            x = x0 + (lat - y0) * (x1 - x0) / (y1 - y0)
            if x > lon:
                inside = not inside
    return inside


def classify(surface: dict, lon: float, lat: float) -> tuple:
    """(kind, ref). kind in runway / gate / taxiway / apron / ground."""
    if not surface:
        return "ground", ""
    best = (float("inf"), "")
    for r in surface.get("runways", []):
        d = _dist_to_path_m(lon, lat, r["path"])
        if d < best[0]:
            best = (d, r["ref"])
    if best[0] <= RUNWAY_M:
        return "runway", best[1]
    kx, ky = _m_per_deg(lat)
    bg = (float("inf"), "")
    for g in surface.get("gates", []):
        d = math.hypot((g["lon"] - lon) * kx, (g["lat"] - lat) * ky)
        if d < bg[0]:
            bg = (d, g["ref"])
    if bg[0] <= GATE_M:
        return "gate", bg[1]
    bt = (float("inf"), "")
    for t in surface.get("taxiways", []):
        d = _dist_to_path_m(lon, lat, t["path"])
        if d < bt[0]:
            bt = (d, t["ref"])
    if bt[0] <= TAXI_M:
        return "taxiway", bt[1]
    for a in surface.get("aprons", []):
        if len(a["polygon"]) >= 3 and _pip(lon, lat, a["polygon"]):
            return "apron", a.get("name") or ""
    return "ground", ""
