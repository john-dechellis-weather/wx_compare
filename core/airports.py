"""JetBlue stations: centre, elevation and runway ends.

Target path: core/airports.py   (data: static/jbu_airports.json)

WHY. OpenStreetMap draws a field well but rarely says where the
LANDING threshold is, and a final has to start there, not at the end
of the pavement. 104 runway ends across the network have a displaced
threshold; JFK 31L's is over 3,000 ft. This table carries the
displacement for every end, so the finals start in the right place.

SOURCE. OurAirports (ourairports.com), public domain: runways.csv
with both-end idents, coordinates, true headings and displaced
thresholds, worldwide. It is crowd-sourced - positions are good to
tens of metres at most fields, occasionally worse - so this is a
display source, not a navigation one. US stations can later be
overridden from the FAA NASR subscription core/navdata.py already
downloads; nothing downstream would change.

REGENERATE when stations are added: the table is built offline from
the two OurAirports CSVs (see build_table below) and committed, so
the app never downloads it.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

_TABLE = Path(__file__).resolve().parent.parent / "static" / "jbu_airports.json"
_LOCK = threading.Lock()
_DATA = {"stations": None}

# One airport, two identifiers: KPBI was renamed KDJT. Anything asked
# for under the old code resolves to the new one, and the surface is
# fetched once.
ALIASES = {"KPBI": "KDJT"}


def _load() -> dict:
    with _LOCK:
        if _DATA["stations"] is None:
            try:
                _DATA["stations"] = json.loads(_TABLE.read_text())["stations"]
            except Exception:
                _DATA["stations"] = {}
        return _DATA["stations"]


def canonical(icao: str) -> str:
    i = (icao or "").strip().upper()
    return ALIASES.get(i, i)


def stations() -> list:
    """Every station, aliases folded, in table order."""
    return [k for k in _load() if k not in ALIASES]


def get(icao: str) -> dict:
    return _load().get(canonical(icao)) or {}


def centre(icao: str):
    a = get(icao)
    return (a["lat"], a["lon"]) if a.get("lat") is not None else None


def elevation(icao: str):
    return get(icao).get("elev_ft")


def code(icao: str) -> str:
    """The key core/surface.py and core/runways.py use: 3 letters for
    US K-codes (JFK), the full ICAO everywhere else (EGLL)."""
    i = canonical(icao)
    return i[1:] if len(i) == 4 and i.startswith("K") else i


def _move(lat, lon, brg, ft):
    m = ft * 0.3048
    b = math.radians(brg)
    return (lat + m * math.cos(b) / 111195.0,
            lon + m * math.sin(b) / (111195.0 * math.cos(math.radians(lat))))


def _brg(lat1, lon1, lat2, lon2):
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2((lon2 - lon1) * k, lat2 - lat1)) % 360.0


def _pad(ident: str) -> str:
    """'4L' -> '04L', the form core/runways.py and core/atis.norm use."""
    s = (ident or "").strip().upper()
    num = "".join(c for c in s if c.isdigit())
    suf = "".join(c for c in s if c.isalpha())
    return f"{int(num):02d}{suf}" if num else s


def runway_ends(icao: str) -> list:
    """[{apt, end, thr, far, hdg, disp_ft}] in core/runways.py's shape.

    thr is the LANDING threshold: the physical end moved along the
    runway by its displacement. far is the opposite physical end. hdg
    is true, measured from the geometry rather than taken from the
    heading column, which is blank at some fields.
    """
    a = get(icao)
    apt = code(icao)
    out = []
    for r in a.get("runways") or []:
        le, he = r["le"], r["he"]
        if not (le.get("ident") and he.get("ident")):
            continue
        for this, other in ((le, he), (he, le)):
            hdg = _brg(this["lat"], this["lon"], other["lat"], other["lon"])
            tlat, tlon = _move(this["lat"], this["lon"], hdg,
                               this.get("disp_ft") or 0.0)
            out.append({"apt": apt, "end": _pad(this["ident"]),
                        "thr": (tlon, tlat),
                        "far": (other["lon"], other["lat"]),
                        "hdg": hdg, "disp_ft": this.get("disp_ft") or 0.0})
    return out


def register_all() -> int:
    """Teach core/runways.py every station's field elevation, so the
    traffic observer's arrival test is right everywhere, not only at
    the three N90 fields it shipped with."""
    from core import runways as _RW
    n = 0
    for icao in stations():
        e = elevation(icao)
        if e is not None:
            _RW.register_elevation(code(icao), e)
            n += 1
    return n
