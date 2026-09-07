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
def _find(cols, *patterns):
    """First column whose upper-cased name matches all the given
    substrings. Returns the ORIGINAL header text."""
    up = {c.upper().strip(): c for c in cols}
    for pats in patterns:
        if isinstance(pats, str):
            pats = (pats,)
        for u, orig in up.items():
            if all(p in u for p in pats):
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
        if t not in NAV_TYPES:
            continue
        la, lo = _num(r.get(c_lat)), _num(r.get(c_lon))
        if la is None or lo is None:
            continue
        out.append({"id": (r.get(c_id) or "").strip().upper(),
                    "type": t, "name": (r.get(c_name) or "").strip(),
                    "lat": la, "lon": lo})
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


# ---------------------------------------------------------------------------
# Build and load
# ---------------------------------------------------------------------------
def build(cache_root, cycle: date = None, log=None) -> dict:
    """Download the three groups for a cycle, parse, cache, return."""
    import requests

    cycle = cycle or current_cycle()
    log = log or (lambda m: None)
    got = {}
    for grp in ("NAV", "FIX", "HPF"):
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
    doc = {"cycle": f"{cycle:%Y-%m-%d}",
           "built": datetime.now(timezone.utc).isoformat(),
           "navaids": navs, "holds": holds,
           "counts": {"navaids": len(navs), "fixes": len(fixes),
                      "holds": len(holds)}}
    p = _cache_path(cache_root, cycle)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)
    log(f"built {cycle}: {doc['counts']}")
    return doc


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


def ensure(cache_root) -> None:
    """Build the current cycle in the background if it is missing.
    Idempotent per cycle per process."""
    cycle = current_cycle()
    if _cache_path(cache_root, cycle).exists():
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
