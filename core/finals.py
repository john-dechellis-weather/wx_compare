"""JFK ILS finals as a controller's video map would draw them.

WHAT THIS IS. One thin line per runway from the intermediate fix to
the landing threshold — the extended centreline — with a tick every
mile out from the threshold (the miles-in-trail reference), the FAF
ringed, the IF dotted, and the fix names and runway tag only when
zoomed in. Only the runways the traffic says are in use are drawn
(core/runways.py); when nothing has been observed for a while, all
seven, because a blank scope helps nobody.

WHERE THE GEOMETRY COMES FROM. The runway ends and true headings come
from the OpenStreetMap surfaces already cached for the airport diagram
(core/surface.py -> runways.runway_ends), so the finals sit exactly
on the runways that are drawn. The distances along the final come from
the FAA approach plates (AL-610, NE-2 22 FEB 2024): the FAF-to-MAP
distance from each plate's timing table, taken as FAF-to-threshold
(the LOC MAP is at or within a few tenths of the threshold), and the
other fixes by their published DME differences. The landing threshold
is the pavement end moved in by the displacement the plates imply
(runway length minus "Rwy Ldg"). 22R's localizer is offset 2.53 deg
from the runway (plate note) and the line follows the localizer.

This is DERIVED data, good to a few tenths of a mile, same standing as
core/stars.py. The FAA CIFP replaces `DEFAULT` wholesale when it is
loaded: exact fix coordinates then, no dead reckoning.

Least certain: 22R, 31L and 31R, where the DME arithmetic on the plate
does not close against the timing table (the DME transponders are not
at the far end of those runways); the timing table was used. Expect
up to half a mile on those three.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

NM_M = 1852.0
FT_M = 0.3048

# Runway -> final. `fixes` are (name, role, nm from the landing threshold).
# `ldg_ft` is the plate's "Rwy Ldg"; `len_ft` the runway length. `offset`
# is the localizer course minus the runway heading, degrees, signed.
DEFAULT = {
    "derived": True,
    "source": "FAA AL-610 approach plates, NE-2 22 FEB 2024 cycle; "
              "distances from the FAF-to-MAP timing tables and DME differences; "
              "runway geometry from OSM at run time",
    "finals": [
        {"rwy": "04L", "proc": "ILS 4L", "crs_mag": 44, "len_ft": 12079, "ldg_ft": 11010,
         "fixes": [["AROKE", "IF", 10.5], ["KRSTL", "FAF", 4.5]]},
        {"rwy": "04R", "proc": "ILS 4R", "crs_mag": 44, "len_ft": 8400, "ldg_ft": 8400,
         "fixes": [["ZETAL", "IF", 9.4], ["EBBEE", "FAF", 4.5]]},
        {"rwy": "13L", "proc": "ILS 13L", "crs_mag": 134, "len_ft": 10000, "ldg_ft": 9093,
         "fixes": [["TELEX", "IF", 6.5], ["CAXUN", "FAF", 4.5], ["UXHUB", "", 2.0]]},
        {"rwy": "22L", "proc": "ILS 22L", "crs_mag": 224, "len_ft": 8400, "ldg_ft": 8400,
         "fixes": [["ROSLY", "IF", 10.7], ["ZALPO", "FAF", 5.5]]},
        {"rwy": "22R", "proc": "ILS 22R", "crs_mag": 221, "len_ft": 12079, "ldg_ft": 7794,
         "offset": -2.53,
         "fixes": [["CORVT", "IF", 10.7], ["MATTR", "FAF", 5.7]]},
        {"rwy": "31L", "proc": "ILS 31L", "crs_mag": 314, "len_ft": 14511, "ldg_ft": 11247,
         "fixes": [["ZACHS", "IF", 12.1], ["MEALS", "FAF", 5.4]]},
        {"rwy": "31R", "proc": "ILS 31R", "crs_mag": 314, "len_ft": 10000, "ldg_ft": 8486,
         "fixes": [["CATOD", "IF", 12.0], ["MALDE", "", 9.4], ["ZULAB", "FAF", 5.7]]},
    ],
    # which side of the line the names go, so the L/R of a parallel pair
    # never collide: these label on the left, the rest on the right.
    "label_left": ["22R", "04L", "31R", "13L"],
}

TICK_NM = 0.3          # total tick length across the final
FALLBACK_ENDS = None   # none: without the OSM surface there is nothing to anchor to


def write(static_dir) -> Path:
    p = Path(static_dir) / "jfk_finals.json"
    p.write_text(json.dumps(DEFAULT))
    return p


def load(static_dir) -> dict:
    """The file if present (so the CIFP or a hand edit can replace it),
    else the embedded default, written out for inspection."""
    p = Path(static_dir) / "jfk_finals.json"
    if not p.exists():
        try:
            write(static_dir)
        except Exception:
            return DEFAULT
    try:
        d = json.loads(p.read_text())
        return d if d.get("finals") else DEFAULT
    except Exception:
        return DEFAULT


def _dest(lat, lon, brg_true, m):
    dlat = m * math.cos(math.radians(brg_true)) / 111195.0
    dlon = m * math.sin(math.radians(brg_true)) / (111195.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _norm_end(name: str) -> str:
    """'4L' / '04L' / '22l' -> '04L' / '22L' (runways.runway_ends form)."""
    s = (name or "").strip().upper()
    if not s:
        return s
    num = "".join(ch for ch in s if ch.isdigit())
    suf = "".join(ch for ch in s if ch.isalpha())
    return f"{int(num):02d}{suf}" if num else s


def active_runways(cfg: dict, apt: str = "JFK") -> set:
    """Arrival ends the traffic observer lists for the airport, '22L'
    form. Empty when nothing has been observed."""
    c = (cfg or {}).get(apt) or {}
    return {_norm_end(e) for e, n in (c.get("arr") or []) if n}


def geometry(ends: list, data: dict = None, active: set = None) -> dict:
    """Build the drawable rows.

    ends:   runways.runway_ends(surface) — [{apt, end, thr, far, hdg}]
    active: runway ends to draw ('22L' form); None/empty -> all.

    Returns {"lines", "ticks", "fixes", "tags", "drawn", "missing"}:
    lines/ticks are PathLayer rows [{path, tip}], fixes are points with
    role and label side, tags the runway name at the outer end.
    """
    data = data or DEFAULT
    left = {_norm_end(x) for x in data.get("label_left", [])}
    by_end = {}
    for e in ends or []:
        if (e.get("apt") or "JFK").upper() == "JFK":
            by_end[_norm_end(e["end"])] = e
    out = {"lines": [], "ticks": [], "fixes": [], "tags": [], "drawn": [], "missing": []}
    want = {_norm_end(x) for x in (active or set())}
    for f in data.get("finals", []):
        rwy = _norm_end(f["rwy"])
        if want and rwy not in want:
            continue
        e = by_end.get(rwy)
        if not e:
            out["missing"].append(rwy)
            continue
        thr_lon, thr_lat = e["thr"]
        hdg = float(e["hdg"])                       # true, threshold -> far end
        # landing threshold: pavement end moved in by the displacement
        disp_ft = max(0.0, float(f.get("len_ft", 0)) - float(f.get("ldg_ft", 0)))
        lat0, lon0 = _dest(thr_lat, thr_lon, hdg, disp_ft * FT_M)
        course = (hdg + float(f.get("offset", 0.0))) % 360.0   # final course, true
        back = (course + 180.0) % 360.0                        # outbound from threshold
        side = (course + 90.0) % 360.0                         # right of the inbound course
        fixes = sorted(f.get("fixes", []), key=lambda x: -float(x[2]))
        if not fixes:
            continue
        outer_nm = float(fixes[0][2])
        o_lat, o_lon = _dest(lat0, lon0, back, outer_nm * NM_M)
        tip = (f"{f.get('proc', rwy)} — final approach course {f.get('crs_mag', '?')}°, "
               f"{fixes[-1][0]} FAF {fixes[-1][2]} nm, {fixes[0][0]} IF {outer_nm} nm "
               f"(derived from the plate; CIFP replaces)")
        out["lines"].append({"path": [[o_lon, o_lat], [lon0, lat0]], "rwy": rwy, "tip": tip})
        # ticks every mile from the threshold to the outer fix
        for i in range(1, int(math.floor(outer_nm + 1e-6)) + 1):
            c_lat, c_lon = _dest(lat0, lon0, back, i * NM_M)
            a_lat, a_lon = _dest(c_lat, c_lon, side, TICK_NM / 2 * NM_M)
            b_lat, b_lon = _dest(c_lat, c_lon, (side + 180) % 360, TICK_NM / 2 * NM_M)
            out["ticks"].append({"path": [[a_lon, a_lat], [b_lon, b_lat]],
                                 "tip": f"{rwy} final — {i} nm from the threshold"})
        is_left = rwy in left
        for name, role, nm in fixes:
            p_lat, p_lon = _dest(lat0, lon0, back, float(nm) * NM_M)
            out["fixes"].append({
                "lon": p_lon, "lat": p_lat, "name": name, "role": role or "",
                "rwy": rwy, "nm": float(nm),
                "label": f"{name} {role}".strip(),
                "anchor": "end" if is_left else "start",
                "off": [-7, -3] if is_left else [7, -3],
                "tip": f"{name} — {rwy} final, {role or 'fix'}, {nm} nm from the threshold",
            })
        out["tags"].append({
            "lon": o_lon, "lat": o_lat, "text": rwy.lstrip("0"),
            "anchor": "end" if is_left else "start",
            "off": [-7, 9] if is_left else [7, 9],
        })
        out["drawn"].append(rwy)
    return out
