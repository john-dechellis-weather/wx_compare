"""REFS ensemble probabilities sampled at a station.

The REFS pages render probability fields as regional maps. This
reads the same fields at ONE grid point — the cell nearest an
airport — for a set of forecast hours, so they can be laid out as a
table alongside the deterministic MOS: rows are thresholds, columns
are valid hours in Z.

WHY A SEPARATE PATH FROM THE WARM STORE

The warmer keeps rendered images. An image has already been
quantised to a palette, so reading a value back out of it would give
a band, not a number. The GRIB is the only place the probability
itself lives, so this fetches it — but only a tiny window around the
station, via the idx byte-range mechanism fetch_and_decode already
uses, so each request is a few kilobytes rather than the CONUS file.

COST

Six thresholds x 24 hours = 144 small fetches per station and cycle,
run through the existing parallel fetcher and then cached for the
life of the cycle. First open of a station is a few seconds behind a
notice; every later open of that station on that cycle is instant.
"""

from __future__ import annotations

import math
from datetime import datetime

# Row order and labels. These are the six thresholds the REFS fetch
# already understands; the label is what the table prints.
THRESHOLDS = [
    ("PROB_CIG500", "P(CIG < 500 ft)"),
    ("PROB_CIG1000", "P(CIG < 1000 ft)"),
    ("PROB_CIG2000", "P(CIG < 2000 ft)"),
    ("PROB_VIS05", "P(VIS < 1/2 sm)"),
    ("PROB_VIS1", "P(VIS < 1 sm)"),
    ("PROB_VIS3", "P(VIS < 3 sm)"),
]

MODEL = "refs_prob"
# Half-width of the fetch window in degrees. 0.35 is ~40 km — enough
# that the nearest-cell lookup always lands inside the window even
# at the edge of a subsetting tile, and small enough that the GRIB
# subset is trivially sized.
WINDOW_DEG = 0.35


def _nearest(vals, lats, lons, lat, lon):
    """Value at the grid cell nearest (lat, lon), or None."""
    import numpy as np

    la = np.asarray(lats, dtype="float64")
    lo = np.asarray(lons, dtype="float64")
    if la.ndim == 1:
        lo, la = np.meshgrid(lo, la)
    lo = np.where(lo > 180.0, lo - 360.0, lo)
    # Scale longitude by cos(lat) so the search is roughly isotropic
    # in distance rather than in degrees.
    k = math.cos(math.radians(lat))
    d2 = (la - lat) ** 2 + ((lo - lon) * k) ** 2
    i = int(np.nanargmin(d2))
    v = np.asarray(vals, dtype="float64").ravel()[i]
    return None if not np.isfinite(v) else float(v)


def sample(lat: float, lon: float, cycle: datetime, hours,
           products=None, max_workers: int = 6) -> dict:
    """{product: {fhr: percent}} at the station.

    Missing hours are simply absent from the inner dict — a fetch
    that fails for one hour must not blank the whole row.
    """
    from core.hrrr_cam import parallel_fetch_decode

    products = products or [p for p, _ in THRESHOLDS]
    tasks = [{
        "key": (p, h), "model": MODEL, "product": p,
        "cycle": cycle, "fhr": h,
        "lat": lat, "lon": lon, "zoom_deg": WINDOW_DEG,
    } for p in products for h in hours]
    got = parallel_fetch_decode(tasks, max_workers=max_workers)
    out = {p: {} for p in products}
    for (p, h), res in got.items():
        if isinstance(res, Exception) or res is None:
            continue
        try:
            vals, lats, lons = res
            v = _nearest(vals, lats, lons, lat, lon)
            if v is not None:
                # GRIB probabilities are 0-100 already; clamp in case
                # of encoding noise at the edges.
                out[p][h] = int(round(max(0.0, min(100.0, v))))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Deterministic point sampling — RRFS (or HRRR) as a MOS-style table
# ---------------------------------------------------------------------------
# Same machinery, different fields. Wind is DERIVED from U and V at
# the station rather than fetched as speed and direction, so both
# come from the same cell at the same instant.
DET_PRODUCTS = ("CEIL", "VIS", "GUST", "UGRD10", "VGRD10")

MS_TO_KT = 1.943844
M_TO_SM = 1.0 / 1609.344
M_TO_HFT = 3.28084 / 100.0     # metres -> hundreds of feet


def sample_deterministic(model: str, lat: float, lon: float,
                         cycle: datetime, hours,
                         max_workers: int = 6) -> dict:
    """{fhr: {cig_hft, vis_sm, wdr_deg, wsp_kt, gst_kt}} at the station.

    Units match what the NBM table shows, so the two tables read the
    same way row for row. Ceiling in hundreds of feet with None for
    unlimited; visibility in statute miles; wind in knots, direction
    in degrees true.
    """
    import math as _m

    from core.hrrr_cam import parallel_fetch_decode

    tasks = [{
        "key": (p, h), "model": model, "product": p,
        "cycle": cycle, "fhr": h,
        "lat": lat, "lon": lon, "zoom_deg": WINDOW_DEG,
    } for p in DET_PRODUCTS for h in hours]
    got = parallel_fetch_decode(tasks, max_workers=max_workers)
    raw = {}
    for (p, h), res in got.items():
        if isinstance(res, Exception) or res is None:
            continue
        try:
            vals, lats, lons = res
            raw.setdefault(h, {})[p] = _nearest(vals, lats, lons,
                                                 lat, lon)
        except Exception:
            continue

    out = {}
    for h in hours:
        r = raw.get(h, {})
        row = {"cig_hft": None, "vis_sm": None, "wdr_deg": None,
               "wsp_kt": None, "gst_kt": None}
        c = r.get("CEIL")
        if c is not None:
            hft = c * M_TO_HFT
            # The model encodes "no ceiling" as a very large height.
            # Above 30,000 ft it is unlimited for every purpose this
            # table serves; leaving the number in would print 300+
            # and colour it as if it were a ceiling.
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
            # Meteorological convention: direction the wind is FROM.
            row["wdr_deg"] = int(round(
                (270.0 - _m.degrees(_m.atan2(vv, u))) % 360.0))
        out[h] = row
    return out


def cell_style(p) -> tuple | None:
    """(background, text) for a probability, or None for blank.

    By probability, not by severity: reading down a column shows the
    SHAPE of the distribution — high for <2000 ft and low for <500 ft
    says stratus is likely but how low is uncertain.
    """
    if p is None:
        return None
    if p >= 70:
        return ("#FF4040", "#FFFFFF")
    if p >= 50:
        return ("#FF9900", "#000000")
    if p >= 30:
        return ("#FFFF00", "#000000")
    if p >= 10:
        return ("#FFF7B0", "#000000")
    return None
