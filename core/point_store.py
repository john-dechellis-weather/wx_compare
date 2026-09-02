"""Pre-warmed point forecasts, sampled from the CAM warmer's arrays.

The MOS page's RRFS and HRRR tables fetched a small GRIB window per
station per hour on demand — 144 requests on a station's first open.
But the CAM warmer already fetches every one of those fields for the
Northeast and Florida regions on every cycle, and holds the decoded
array in memory while it renders the image. Sampling every airport
inside the array at that moment costs nothing: no fetch, no decode,
one nearest-cell lookup per station.

So this is not a second warmer. It is a hook in the existing one.

LAYOUT

    cache/point_warm/{model}/{icao}.json

        {"cycle": iso, "max_fhr": int, "updated": iso,
         "rows": {"1": {"CEIL": m, "VIS": m, "GUST": m/s,
                        "UGRD10": m/s, "VGRD10": m/s}, ...}}

One file per model per station. A cycle change REPLACES the file
rather than merging into it, because mixing hours from two runs
would produce a table that is internally inconsistent without
looking it.

Values are stored in the GRIB's own units and converted at display
time, so the store cannot drift from the renderer.

COVERAGE

29 of the 51 JBU stations fall inside the two warm regions. For the
other 22 the page still fetches live; that path is unchanged.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# Products the warmer samples for the point tables. UGRD10/VGRD10
# are SAMPLE-ONLY: the warmer fetches them for this store but never
# renders them as images, since wind components are not a map
# product anyone views.
POINT_PRODUCTS = ("CEIL", "VIS", "GUST", "UGRD10", "VGRD10")
SAMPLE_ONLY = ("UGRD10", "VGRD10")

_lock = threading.Lock()


def _root(cache_root) -> Path:
    return Path(cache_root) / "point_warm"


def _path(cache_root, model: str, icao: str) -> Path:
    return _root(cache_root) / model / f"{icao.upper()}.json"


def _nearest(vals, lats, lons, lat: float, lon: float):
    import numpy as np

    la = np.asarray(lats, dtype="float64")
    lo = np.asarray(lons, dtype="float64")
    if la.ndim == 1:
        lo, la = np.meshgrid(lo, la)
    lo = np.where(lo > 180.0, lo - 360.0, lo)
    # Bounds check first: a station outside this array must not be
    # assigned the value of the nearest EDGE cell, which could be
    # hundreds of kilometres away.
    if not (la.min() <= lat <= la.max() and lo.min() <= lon <= lo.max()):
        return None
    k = math.cos(math.radians(lat))
    d2 = (la - lat) ** 2 + ((lo - lon) * k) ** 2
    i = int(np.nanargmin(d2))
    v = float(np.asarray(vals, dtype="float64").ravel()[i])
    return None if not math.isfinite(v) else v


def record(cache_root, model: str, product: str, cycle_iso: str,
           fhr: int, vals, lats, lons, stations: dict) -> int:
    """Sample every station inside the array and persist. Returns
    the number of stations written.

    Called from inside the warmer's render loop, so it must be
    cheap and must never raise into the warmer.
    """
    if product not in POINT_PRODUCTS:
        return 0
    n = 0
    try:
        for icao, (slat, slon) in stations.items():
            v = _nearest(vals, lats, lons, slat, slon)
            if v is None:
                continue
            _write(cache_root, model, icao, cycle_iso, fhr, product, v)
            n += 1
    except Exception:
        return n
    return n


def _write(cache_root, model: str, icao: str, cycle_iso: str,
           fhr: int, product: str, value: float) -> None:
    p = _path(cache_root, model, icao)
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {"cycle": cycle_iso, "max_fhr": 0, "rows": {}}
        if p.exists():
            try:
                old = json.loads(p.read_text())
                # Same cycle: extend. New cycle: start over. Never
                # merge two runs into one table.
                if old.get("cycle") == cycle_iso:
                    doc = old
            except Exception:
                pass
        row = doc["rows"].setdefault(str(fhr), {})
        row[product] = value
        doc["max_fhr"] = max(int(doc.get("max_fhr", 0)), int(fhr))
        doc["updated"] = datetime.now(timezone.utc).isoformat()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, p)


def read(cache_root, model: str, icao: str):
    """(cycle_iso, {fhr: {product: value}}) or (None, {})."""
    p = _path(cache_root, model, icao)
    if not p.exists():
        return None, {}
    try:
        doc = json.loads(p.read_text())
        rows = {int(k): v for k, v in doc.get("rows", {}).items()}
        return doc.get("cycle"), rows
    except Exception:
        return None, {}


def to_display_rows(rows: dict) -> dict:
    """GRIB units -> the dict shape build_det_point_table expects.

    Same conversions as core.refs_point.sample_deterministic, kept
    in one place so a stored value and a live one render identically.
    """
    import math as _m

    MS_TO_KT = 1.943844
    M_TO_SM = 1.0 / 1609.344
    M_TO_HFT = 3.28084 / 100.0
    out = {}
    for h, r in rows.items():
        row = {"cig_hft": None, "vis_sm": None, "wdr_deg": None,
               "wsp_kt": None, "gst_kt": None}
        c = r.get("CEIL")
        if c is not None:
            hft = c * M_TO_HFT
            row["cig_hft"] = None if hft > 300 else int(round(hft))
        v = r.get("VIS")
        if v is not None:
            row["vis_sm"] = round(min(10.0, v * M_TO_SM), 2)
        g = r.get("GUST")
        if g is not None:
            row["gst_kt"] = int(round(g * MS_TO_KT))
        u, vv = r.get("UGRD10"), r.get("VGRD10")
        if u is not None and vv is not None:
            row["wsp_kt"] = int(round(_m.hypot(u, vv) * MS_TO_KT))
            row["wdr_deg"] = int(round(
                (270.0 - _m.degrees(_m.atan2(vv, u))) % 360.0))
        out[int(h)] = row
    return out
