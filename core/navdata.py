"""FAA NASR navaids, fixes and published holding patterns.

Holds happen over fixes. Drawing the published ones on the map is
the complement to geometric hold detection: an aircraft circling
over a charted holding fix is holding whatever the track geometry
says, and a dispatcher can see which fix it is.

SOURCE

The FAA's 28-day NASR subscription, CSV form. Three small zips per
cycle, one per group:

    https://nfdc.faa.gov/webContent/28DaySub/extra/
        {DD}_{Mon}_{YYYY}_NAV_CSV.zip     navaids (VOR, VORTAC, NDB ...)
        {DD}_{Mon}_{YYYY}_FIX_CSV.zip     fixes / waypoints
        {DD}_{Mon}_{YYYY}_HPF_CSV.zip     published holding patterns

Cycles are every 28 days; 2026-08-06 and 2026-09-03 are confirmed
effective dates and the series is projected from there. The files
are posted 28 days ahead, so the current cycle always exists.

COLUMN DETECTION

Column names are found by pattern rather than assumed — the CSV
layout was revised on the 3 Sep 2026 cycle and the mapping document
was not yet reissued when this was written. Every load logs which
columns it matched so a schema surprise is visible in the log rather
than silently producing nothing.

CACHE

Parsed output is one JSON file per cycle under
cache/navdata/{cycle}.json. Loading is one read; the download and
parse happen once per cycle in a background thread, so the map never
waits on the FAA.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

NASR_BASE = "https://nfdc.faa.gov/webContent/28DaySub/extra"
# A confirmed 28-day effective date; the series is projected from it.
_EPOCH = date(2026, 8, 6)
_CYCLE_DAYS = 28

# Navaid types worth drawing on an airline map. NDBs are omitted by
# default — hundreds of low-level beacons that no jet holds over.
NAV_TYPES = set(x.strip().upper() for x in os.environ.get(
    "NAVDATA_TYPES", "VOR,VORTAC,VOR/DME,TACAN,DME").split(","))

# ARTCC boundaries to keep. Default: every CONUS centre. The ARB
# file also carries Anchorage, Honolulu, Guam and the oceanic FIRs,
# which stretch a CONUS map to nothing; those are the ones left out.
# NAVDATA_ARTCC overrides; empty string means all of them.
_CONUS_ARTCC = ("ZAB,ZAU,ZBW,ZDC,ZDV,ZFW,ZHU,ZID,ZJX,ZKC,ZLA,ZLC,"
                "ZMA,ZME,ZMP,ZNY,ZOA,ZOB,ZSE,ZTL")
ARTCC_IDS = set(x.strip().upper() for x in os.environ.get(
    "NAVDATA_ARTCC", _CONUS_ARTCC).split(",") if x.strip())

# Bump whenever the parsed output gains or changes a section. The
# cache for a cycle is rebuilt when its stored version is older —
# without this, a deploy that adds routes or more centres appears to
# do nothing until the next 28-day cycle.
DATA_VERSION = 6

_lock = threading.Lock()
_started = set()


def current_cycle(today: date = None) -> date:
    """Effective date of the cycle in force on `today`."""
    today = today or datetime.now(timezone.utc).date()
    n = (today - _EPOCH).days // _CYCLE_DAYS
    return _EPOCH + timedelta(days=n * _CYCLE_DAYS)


def _zip_url(cycle: date, group: str) -> str:
    return f"{NASR_BASE}/{cycle:%d_%b_%Y}_{group}_CSV.zip"


def _cache_path(cache_root, cycle: date) -> Path:
    return Path(cache_root) / "navdata" / f"{cycle:%Y-%m-%d}.json"


# ---------------------------------------------------------------------------
# Column matching
# ---------------------------------------------------------------------------
def _find(cols, *patterns, exclude=()):
    """First column whose upper-cased name matches all the given
    substrings and none of `exclude`. Returns the ORIGINAL header.

    `exclude` exists because a loose fallback pattern will happily
    match the wrong column: "POINT" matched POINT_SEQ before it ever
    reached FROM_POINT, so every airway point id became a sequence
    number and no route could be placed. Seen live 7 Sep."""
    up = {c.upper().strip(): c for c in cols}
    for pats in patterns:
        if isinstance(pats, str):
            pats = (pats,)
        for u, orig in up.items():
            if all(p in u for p in pats) and not any(x in u for x in exclude):
                return orig
    return None


def _num(v):
    try:
        f = float(str(v).strip())
        return f if f == f else None
    except Exception:
        return None


def _read_csv(zbytes: bytes, name_hint: str):
    """Rows of the first CSV in the zip whose name contains the hint."""
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        pick = next((n for n in names if name_hint.lower() in n.lower()),
                    names[0] if names else None)
        if not pick:
            return [], []
        with z.open(pick) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
            rdr = csv.DictReader(text)
            rows = list(rdr)
            return rdr.fieldnames or [], rows


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def parse_nav(zbytes: bytes, log=None) -> list:
    """[{id, type, name, lat, lon}] for drawable navaids."""
    cols, rows = _read_csv(zbytes, "NAV_BASE")
    c_id = _find(cols, "NAV_ID", ("NAV", "ID"))
    c_type = _find(cols, "NAV_TYPE", ("NAV", "TYPE"))
    c_name = _find(cols, "NAME")
    c_lat = _find(cols, ("LAT", "DECIMAL"), ("LATITUDE", "DECIMAL"))
    c_lon = _find(cols, ("LONG", "DECIMAL"), ("LON", "DECIMAL"))
    if log:
        log(f"NAV columns: id={c_id} type={c_type} lat={c_lat} lon={c_lon}")
    out = []
    for r in rows:
        t = (r.get(c_type) or "").strip().upper()
        la, lo = _num(r.get(c_lat)), _num(r.get(c_lon))
        if la is None or lo is None:
            continue
        out.append({"id": (r.get(c_id) or "").strip().upper(),
                    "type": t, "name": (r.get(c_name) or "").strip(),
                    "lat": la, "lon": lo,
                    # drawn on the map, or kept only for joining
                    # airways and holds to a position
                    "draw": t in NAV_TYPES})
    return out


def parse_fix(zbytes: bytes, log=None) -> dict:
    """{FIX_ID: (lat, lon)} for every fix — used to place holds."""
    cols, rows = _read_csv(zbytes, "FIX_BASE")
    c_id = _find(cols, "FIX_ID", ("FIX", "ID"))
    c_lat = _find(cols, ("LAT", "DECIMAL"), ("LATITUDE", "DECIMAL"))
    c_lon = _find(cols, ("LONG", "DECIMAL"), ("LON", "DECIMAL"))
    if log:
        log(f"FIX columns: id={c_id} lat={c_lat} lon={c_lon}")
    out = {}
    for r in rows:
        fid = (r.get(c_id) or "").strip().upper()
        la, lo = _num(r.get(c_lat)), _num(r.get(c_lon))
        if fid and la is not None and lo is not None:
            out.setdefault(fid, (la, lo))
    return out


def parse_hpf(zbytes: bytes, fixes: dict, navs: dict, log=None) -> list:
    """[{id, name, lat, lon, course, turn}] — one per published hold,
    placed at its fix or navaid."""
    cols, rows = _read_csv(zbytes, "HPF_BASE")
    c_name = _find(cols, "HP_NAME", ("HOLD", "NAME"), "NAME")
    c_fix = _find(cols, "FIX_ID", ("FIX", "ID"), "NAV_ID", ("NAV", "ID"))
    c_crs = _find(cols, ("INBOUND", "COURSE"), "INBD_CRS", "COURSE")
    c_turn = _find(cols, ("TURN", "DIR"), "TURN")
    c_lat = _find(cols, ("LAT", "DECIMAL"))
    c_lon = _find(cols, ("LONG", "DECIMAL"), ("LON", "DECIMAL"))
    if log:
        log(f"HPF columns: name={c_name} fix={c_fix} crs={c_crs} "
            f"turn={c_turn} lat={c_lat} lon={c_lon}")
    out = []
    seen = set()
    for r in rows:
        name = (r.get(c_name) or "").strip()
        fid = (r.get(c_fix) or "").strip().upper()
        # Some HP names are "FIXNAME*STATE"; the fix id is the part
        # before the star. Fall back to that when the fix column is
        # empty.
        if not fid and "*" in name:
            fid = name.split("*")[0].strip().upper()
        la = _num(r.get(c_lat)) if c_lat else None
        lo = _num(r.get(c_lon)) if c_lon else None
        if la is None or lo is None:
            pos = fixes.get(fid) or navs.get(fid)
            if not pos:
                continue
            la, lo = pos
        key = (fid, round(la, 3), round(lo, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": fid, "name": name,
                    "lat": la, "lon": lo,
                    "course": _num(r.get(c_crs)) if c_crs else None,
                    "turn": (r.get(c_turn) or "").strip() if c_turn else ""})
    return out


def parse_arb(zbytes: bytes, want: set = None, log=None) -> list:
    """[{id, structure, path: [[lon, lat], ...]}] per ARTCC boundary.

    NASR ARB_SEG.csv is one row per boundary POINT, keyed by the
    ARTCC location id and an altitude structure (HIGH / LOW / BDRY),
    ordered by a sequence number. HIGH is what an airline map wants
    — jets are in it — with LOW as a fallback for a centre that
    publishes only one.

    Some boundaries (ZMA, ZNY, ZAN, ZOA) are more than one closed
    shape. The legacy format marks the seam with the phrase
    "TO POINT OF BEGINNING" in the point description; the same text
    survives in the CSV description column, and a new shape starts
    at the next point after it.
    """
    cols, rows = _read_csv(zbytes, "ARB_SEG")
    c_id = _find(cols, "LOCATION_ID", ("LOC", "ID"), "ARTCC_ID", "ID")
    c_str = _find(cols, ("ALT", "STRUCT"), "STRUCTURE", ("ALTITUDE",))
    c_seq = _find(cols, ("POINT", "SEQ"), "SEQ")
    c_lat = _find(cols, ("LAT", "DECIMAL"))
    c_lon = _find(cols, ("LONG", "DECIMAL"), ("LON", "DECIMAL"))
    c_desc = _find(cols, ("POINT", "DESC"), "DESCRIPTION", "DESC")
    if log:
        log(f"ARB columns: id={c_id} struct={c_str} seq={c_seq} "
            f"lat={c_lat} lon={c_lon} desc={c_desc}")
    groups = {}
    for r in rows:
        aid = (r.get(c_id) or "").strip().upper()
        if not aid or (want and aid not in want):
            continue
        st_ = (r.get(c_str) or "").strip().upper() if c_str else ""
        la, lo = _num(r.get(c_lat)), _num(r.get(c_lon))
        if la is None or lo is None:
            continue
        seq = _num(r.get(c_seq)) if c_seq else None
        desc = (r.get(c_desc) or "").upper() if c_desc else ""
        groups.setdefault((aid, st_), []).append(
            (seq if seq is not None else len(groups[(aid, st_)]),
             lo, la, "BEGINNING" in desc))
    out = []
    for (aid, st_), pts in groups.items():
        pts.sort(key=lambda t: t[0])
        shapes, cur = [], []
        for _, lo, la, seam in pts:
            cur.append([lo, la])
            if seam and len(cur) > 2:
                shapes.append(cur)
                cur = []
        if len(cur) > 2:
            shapes.append(cur)
        for sh in shapes:
            if sh[0] != sh[-1]:
                sh.append(sh[0])          # close the ring
            out.append({"id": aid, "structure": st_, "path": sh})
    return out


def parse_awy(zbytes: bytes, fixes: dict, navs: dict, want=None,
              log=None) -> list:
    """[{id, type, path: [[lon, lat], ...], fixes: [id, ...]}] — one
    per airway, full length, from NASR AWY_SEG.csv.

    An airway is a sequence of points, each a fix or a navaid id, in
    order. Positions come from joining to FIX_BASE and NAV_BASE. A
    point that cannot be placed is skipped rather than breaking the
    route; a route with fewer than two placed points is dropped.

    `want` limits to a set of idents (J60, Q409 ...) or a set of
    prefixes ("J", "Q"). None keeps every airway in the file.
    """
    cols, rows = _read_csv(zbytes, "AWY_SEG")
    c_id = _find(cols, "AWY_ID", ("AWY", "ID"), "AIRWAY")
    c_seq = _find(cols, ("POINT", "SEQ"), "SEQ")
    _no = ("SEQ", "TYPE", "NAME", "CITY", "COURSE", "DIST")
    c_pt = _find(cols, "FROM_POINT", ("FROM", "PT"), ("POINT", "ID"),
                 ("NAV", "ID"), "FIX_ID", ("FIX", "ID"), "POINT",
                 exclude=_no)
    c_to = _find(cols, "TO_POINT", ("TO", "PT"), exclude=_no)
    c_typ = _find(cols, "AWY_TYPE", ("AWY", "TYPE"))
    if log:
        log(f"AWY columns: id={c_id} seq={c_seq} point={c_pt} "
            f"to={c_to} type={c_typ} | all: {list(cols)}")
    want = set(x.upper() for x in want) if want else None
    groups = {}
    for r in rows:
        aid = (r.get(c_id) or "").strip().upper()
        if not aid:
            continue
        if want and aid not in want and not any(
                aid.startswith(w) and len(w) == 1 for w in want):
            continue
        seq = _num(r.get(c_seq)) if c_seq else None
        g = groups.setdefault(aid, {"type": (r.get(c_typ) or "").strip()
                                    if c_typ else "", "pts": [],
                                    "unplaced": 0})
        pid = (r.get(c_pt) or "").strip().upper()
        pos = fixes.get(pid) or navs.get(pid)
        if pos:
            g["pts"].append((seq if seq is not None else len(g["pts"]),
                             pid, pos[1], pos[0]))
        else:
            g["unplaced"] += 1
        # Segment files list FROM and TO per row; the final TO point
        # is not any row's FROM, so add it with a half-step sequence
        # so it sorts after its own FROM.
        if c_to:
            tid = (r.get(c_to) or "").strip().upper()
            tpos = fixes.get(tid) or navs.get(tid)
            if tpos:
                g["pts"].append(((seq if seq is not None
                                  else len(g["pts"])) + 0.5,
                                 tid, tpos[1], tpos[0]))
    out = []
    unplaced = 0
    for aid, g in groups.items():
        g["pts"].sort(key=lambda t: t[0])
        # TO-points duplicate the next row's FROM-point; drop
        # consecutive repeats so the path has no zero-length legs.
        pts = []
        for t in g["pts"]:
            if not pts or pts[-1][1] != t[1]:
                pts.append(t)
        unplaced += g["unplaced"]
        if len(pts) < 2:
            continue
        out.append({"id": aid, "type": g["type"],
                    "path": [[lo, la] for _, _, lo, la in pts],
                    "fixes": [pid for _, pid, _, _ in pts]})
    if log:
        log(f"AWY: {len(out)} routes placed from {len(groups)} ids; "
            f"{unplaced} points had no fix/navaid match")
        if not out and rows:
            log(f"AWY sample row: {dict(list(rows[0].items())[:8])}")
    return out


def pick_boundaries(arbs: list, prefer=("HIGH", "LOW", "BDRY", "")) -> list:
    """One structure per ARTCC, preferring HIGH."""
    by = {}
    for b in arbs:
        by.setdefault(b["id"], {}).setdefault(b["structure"], []).append(b)
    out = []
    for aid, structs in by.items():
        for p in prefer:
            if p in structs:
                out.extend(structs[p])
                break
    return out


# ---------------------------------------------------------------------------
# Build and load
# ---------------------------------------------------------------------------
def build(cache_root, cycle: date = None, log=None) -> dict:
    """Download the three groups for a cycle, parse, cache, return."""
    import requests

    cycle = cycle or current_cycle()
    log = log or (lambda m: None)
    got = {}
    for grp in ("NAV", "FIX", "HPF", "ARB", "AWY"):
        url = _zip_url(cycle, grp)
        r = requests.get(url, timeout=120,
                         headers={"User-Agent": "bluemet.org"})
        if r.status_code != 200:
            log(f"{grp}: HTTP {r.status_code} {url}")
            raise RuntimeError(f"{grp} HTTP {r.status_code}")
        got[grp] = r.content
        log(f"{grp}: {len(r.content) / 1024:.0f} KB")
    navs = parse_nav(got["NAV"], log)
    fixes = parse_fix(got["FIX"], log)
    nav_pos = {n["id"]: (n["lat"], n["lon"]) for n in navs}
    holds = parse_hpf(got["HPF"], fixes, nav_pos, log)
    bounds = pick_boundaries(parse_arb(got["ARB"], ARTCC_IDS or None, log))
    # THE 58 ROUTES, AT FULL LENGTH. The bundled export defines
    # WHICH routes; the full geometry comes from source:
    #   domestic J/Q  -> NASR AWY, joined to fix positions
    #   oceanic L     -> FAA AIS ATS Route layer, by ident
    # Anything the sources cannot supply keeps its exported (clipped)
    # geometry, so a network failure degrades to the previous map
    # rather than to a blank one. The log says which happened.
    bundled = bundled_routes(log)
    want_dom = {r["id"] for r in bundled if r["type"] != "OCEAN"}
    want_oc = {r["id"] for r in bundled if r["type"] == "OCEAN"}
    full_dom = {r["id"]: r for r in parse_awy(
        got["AWY"], fixes, nav_pos, want_dom, log)}
    try:
        full_oc = ais_routes(want_oc, log)
    except Exception as exc:
        log(f"AIS ATS failed: {type(exc).__name__}: {exc}")
        full_oc = {}
    routes, seen = [], set()
    n_full = 0
    for r in bundled:
        if r["id"] in seen:
            continue                 # export split some routes in two
        seen.add(r["id"])
        if r["type"] == "OCEAN" and r["id"] in full_oc:
            routes.append({**r, "path": full_oc[r["id"]]}); n_full += 1
        elif r["type"] != "OCEAN" and r["id"] in full_dom:
            routes.append({**r, "path": full_dom[r["id"]]["path"],
                           "fixes": full_dom[r["id"]]["fixes"]})
            n_full += 1
        else:
            # keep every exported piece of a route that could not be
            # extended, so nothing that was drawn before vanishes
            routes.extend(x for x in bundled if x["id"] == r["id"])
    log(f"routes: {len(seen)} idents, {n_full} at full length, "
        f"{len(seen) - n_full} kept as exported")
    if ROUTE_PREFIXES:
        routes += parse_awy(got["AWY"], fixes, nav_pos, ROUTE_PREFIXES,
                            log)
    # Fixes that sit on a drawn route, with positions, for the
    # middle density setting on the map.
    on_route = {}
    for rt in routes:
        for pid, (lo, la) in zip(rt.get("fixes", []), rt["path"]):
            if pid and pid not in on_route:
                on_route[pid] = (la, lo)
    route_fixes = [{"id": k, "lat": v[0], "lon": v[1]}
                   for k, v in on_route.items()]
    navs = [n for n in navs if n.get("draw")]
    doc = {"cycle": f"{cycle:%Y-%m-%d}",
           "version": DATA_VERSION,
           "built": datetime.now(timezone.utc).isoformat(),
           "navaids": navs, "holds": holds, "artcc": bounds,
           "routes": routes, "route_fixes": route_fixes,
           "counts": {"navaids": len(navs), "fixes": len(fixes),
                      "holds": len(holds), "artcc": len(bounds),
                      "routes": len(routes),
                      "route_fixes": len(route_fixes)}}
    p = _cache_path(cache_root, cycle)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)
    log(f"built {cycle}: {doc['counts']}")
    return doc


# Airway prefixes to ALSO draw from NASR's AWY group at full length.
# EMPTY by default: the routes on the map are the 58 in the bundled
# GeoJSON, exactly as exported, and nothing else. NAVDATA_ROUTES=J,Q
# adds every high-altitude airway in the country on top of them.
ROUTE_PREFIXES = set(x.strip().upper() for x in os.environ.get(
    "NAVDATA_ROUTES", "").split(",") if x.strip())

# The 58 routes the map draws — 42 domestic J and Q routes and 16
# oceanic L-routes — from the FAA AIS Open Data ATS Route layer,
# exported to GeoJSON and bundled with the repo. Drawn EXACTLY as
# exported: the export was clipped to a Northeast box, so long
# routes end at that box rather than at their true ends. That is the
# geometry the map is meant to show.
ROUTES_GEOJSON = os.environ.get(
    "NAVDATA_ROUTES_GEOJSON",
    str(Path(__file__).resolve().parent.parent / "static"
        / "map_routes.geojson"))


# FAA AIS Open Data, ATS Route feature layer. This is what the
# bundled export was cut from; querying it by ident returns the
# same routes at FULL length. Env-overridable in case the service
# name changes; a failure falls back to the bundled geometry.
AIS_ATS_URL = os.environ.get(
    "NAVDATA_AIS_ATS_URL",
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/"
    "services/ATS_Route/FeatureServer/0/query")


def ais_routes(idents, log=None) -> dict:
    """{ident: [[lon, lat], ...]} from the AIS ATS Route layer, full
    length. Missing idents are simply absent from the result."""
    import requests

    idents = sorted(set(i for i in idents if i))
    if not idents:
        return {}
    out = {}
    # Batches of 25 keep the WHERE clause short enough for a GET.
    for i in range(0, len(idents), 25):
        batch = idents[i:i + 25]
        where = "IDENT IN (" + ",".join(f"'{x}'" for x in batch) + ")"
        try:
            r = requests.get(AIS_ATS_URL, params={
                "where": where, "outFields": "IDENT,TYPE_CODE",
                "outSR": "4326", "f": "geojson", "resultRecordCount": 2000,
            }, timeout=60, headers={"User-Agent": "bluemet.org"})
            if r.status_code != 200:
                if log:
                    log(f"AIS ATS: HTTP {r.status_code} for {batch[:3]}...")
                continue
            g = r.json()
        except Exception as exc:
            if log:
                log(f"AIS ATS: {type(exc).__name__}: {exc}")
            continue
        for f in g.get("features", []):
            ident = (f.get("properties", {}).get("IDENT") or "").upper()
            geom = f.get("geometry") or {}
            coords = []
            if geom.get("type") == "LineString":
                coords = geom["coordinates"]
            elif geom.get("type") == "MultiLineString":
                # Join the parts end to end; AIS splits long routes
                # into pieces that are contiguous in order.
                for part in geom["coordinates"]:
                    coords.extend(part)
            if ident and len(coords) > 1:
                pts = [[float(x), float(y)] for x, y in coords]
                # A route can come back as several features (one per
                # segment); keep the longest, extend if contiguous.
                if ident in out and len(out[ident]) >= len(pts):
                    continue
                out[ident] = pts
    if log:
        log(f"AIS ATS: {len(out)} of {len(idents)} idents at full length")
    return out


def bundled_routes(log=None) -> list:
    """Every LineString in the bundled GeoJSON, domestic and oceanic."""
    try:
        g = json.loads(Path(ROUTES_GEOJSON).read_text())
    except Exception as exc:
        if log:
            log(f"bundled routes: {type(exc).__name__}: {exc}")
        return []
    out = []
    for f in g.get("features", []):
        pr = f.get("properties", {})
        geom = f.get("geometry", {})
        if geom.get("type") != "LineString":
            continue
        fam = (pr.get("family") or "").lower()
        out.append({"id": pr.get("ident", ""),
                    "type": "OCEAN" if fam == "oceanic"
                            else (pr.get("type") or "").upper(),
                    "path": [[float(x), float(y)]
                             for x, y in geom["coordinates"]],
                    "fixes": []})
    if log:
        n_oc = sum(1 for r in out if r["type"] == "OCEAN")
        log(f"bundled routes: {len(out)} ({len(out) - n_oc} domestic, "
            f"{n_oc} oceanic)")
    return out


def _log_to(cache_root):
    def _l(msg):
        try:
            p = Path(cache_root) / "navdata" / "navdata.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as fh:
                fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                         f"{msg}\n")
        except OSError:
            pass
    return _l


def _cached_version(path: Path) -> int:
    try:
        return int(json.loads(path.read_text()).get("version", 0))
    except Exception:
        return 0


def ensure(cache_root) -> None:
    """Build the current cycle in the background if it is missing OR
    was built by an older version of this module. Idempotent per
    cycle per process."""
    cycle = current_cycle()
    p = _cache_path(cache_root, cycle)
    if p.exists() and _cached_version(p) >= DATA_VERSION:
        return
    with _lock:
        if cycle in _started:
            return
        _started.add(cycle)

    def _run():
        try:
            build(cache_root, cycle, _log_to(cache_root))
        except Exception as exc:
            _log_to(cache_root)(f"FAILED {cycle}: "
                                f"{type(exc).__name__}: {exc}")

    threading.Thread(target=_run, daemon=True).start()


def load(cache_root) -> dict | None:
    """Newest cached cycle on disk, or None. Never blocks."""
    d = Path(cache_root) / "navdata"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return None
