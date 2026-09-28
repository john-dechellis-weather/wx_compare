"""Echo-top tags for the CONUS map.

Target path: core/etop_tags.py
Started from Homepage.py beside the MRMS warmer. Kill: ETOP_TAGS=off.

Every pass: fetch MRMS EchoTop_18 (the 18 dBZ echo top, kilometres
MSL, 0.01 deg CONUS grid), find the local maxima at or above FL320,
rank them tallest first, keep a peak only if no kept peak is within
MIN_SEP_NM of it, and write static/etop_tags.json:

    {"stamp": "20260921-184400", "n": 14,
     "tags": [{"lat": 40.05, "lon": -75.55, "fl": 430, "km": 13.1}, ...]}

The page draws the file; it never touches the grid. This is the
same manifest pattern the MRMS chunks use, and for the same reason:
nothing on the run path decodes a GRIB.

DECODING. Tries pygrib, then eccodes, then cfgrib - whichever the
image has. The MRMS warmer already decodes this product, so at least
one of them is present; the log says which was used.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import threading
import time
from pathlib import Path

URL = ("https://mrms.ncep.noaa.gov/2D/EchoTop_18/"
       "MRMS_EchoTop_18.latest.grib2.gz")
PERIOD_S = float(os.environ.get("ETOP_TAGS_PERIOD_S", "150"))
MIN_FL = int(os.environ.get("ETOP_TAGS_MIN_FL", "320"))
MIN_SEP_NM = float(os.environ.get("ETOP_TAGS_SEP_NM", "30"))
MAX_TAGS = int(os.environ.get("ETOP_TAGS_MAX", "250"))
# A peak counts only if it sits in a contiguous area of at least this
# many 0.01 deg cells (~1 km2 each) at or above MIN_FL. Measured
# 21 Sep 21Z: 8,254 separate areas were at FL320+, and 6,424 of them
# were three cells or fewer - single-scan specks, not storms - so
# without this the file was 250 tags, most of them on nothing.
MIN_CELLS = int(os.environ.get("ETOP_TAGS_MIN_CELLS", "10"))
POOL = 5                       # 0.01 deg cells -> 0.05 deg blocks
KM_PER_FL = 0.03048            # one flight level (100 ft) in km
START_DELAY_S = 45

_LOCK = threading.Lock()
_STATE = {"thread": None, "log": []}


def _log(msg: str) -> None:
    with _LOCK:
        _STATE["log"].append(time.strftime("%H:%M:%SZ ", time.gmtime()) + msg)
        del _STATE["log"][:-40]


def log_tail(n: int = 8) -> list:
    with _LOCK:
        return list(_STATE["log"][-n:])


# ------------------------------------------------------------ decode

def _decode(raw: bytes):
    """(values 2-D km, lats 1-D, lons 1-D) from a GRIB2 message."""
    try:
        import pygrib
        with open("/tmp/_etop18.grib2", "wb") as f:
            f.write(raw)
        g = pygrib.open("/tmp/_etop18.grib2")[1]
        vals = g.values
        lats, lons = g.latlons()
        return vals, lats[:, 0], lons[0, :], "pygrib"
    except Exception:
        pass
    try:
        import eccodes as ec
        import numpy as np
        h = ec.codes_new_from_message(raw)
        ni, nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
        vals = ec.codes_get_values(h).reshape(nj, ni)
        la1, lo1 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees"), \
            ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
        dla, dlo = ec.codes_get(h, "jDirectionIncrementInDegrees"), \
            ec.codes_get(h, "iDirectionIncrementInDegrees")
        jscan = ec.codes_get(h, "jScansPositively")
        ec.codes_release(h)
        lats = la1 + (np.arange(nj) * dla if jscan else -np.arange(nj) * dla)
        lons = lo1 + np.arange(ni) * dlo
        return vals, lats, lons, "eccodes"
    except Exception:
        pass
    import xarray as xr                     # engine="cfgrib" below
    with open("/tmp/_etop18.grib2", "wb") as f:
        f.write(raw)
    ds = xr.open_dataset("/tmp/_etop18.grib2", engine="cfgrib")
    v = ds[list(ds.data_vars)[0]]
    return v.values, ds["latitude"].values, ds["longitude"].values, "cfgrib"


# ------------------------------------------------------------- peaks

def _peaks(vals, lats, lons) -> list:
    import numpy as np
    v = np.asarray(vals, dtype="float32")
    v[~np.isfinite(v)] = 0.0
    v[v < 0] = 0.0                       # -999 missing / no coverage
    nj, ni = v.shape
    nj2, ni2 = nj // POOL, ni // POOL
    blk = v[:nj2 * POOL, :ni2 * POOL].reshape(nj2, POOL, ni2, POOL)
    m = blk.max(axis=(1, 3))             # max-pool to 0.05 deg
    # where in each block the max sits, for a tag on the cell itself
    arg = blk.reshape(nj2, POOL, ni2, POOL).transpose(0, 2, 1, 3).reshape(nj2, ni2, -1).argmax(-1)
    thresh = MIN_FL * KM_PER_FL
    # local maximum: >= all 8 neighbours
    pad = np.pad(m, 1, mode="constant", constant_values=-1)
    is_max = np.ones_like(m, dtype=bool)
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            if dj == di == 0:
                continue
            is_max &= m >= pad[1 + dj:1 + dj + nj2, 1 + di:1 + di + ni2]
    is_max &= m >= thresh
    # Drop peaks in areas too small to be a storm (see MIN_CELLS).
    try:
        from scipy import ndimage as ndi
        lab, n = ndi.label(v >= thresh)
        if n:
            size = np.bincount(lab.ravel())
            size[0] = 0
            big = size >= MIN_CELLS
            # a block is kept if ANY cell of a big enough area is in it
            keep = big[lab[:nj2 * POOL, :ni2 * POOL]].reshape(
                nj2, POOL, ni2, POOL).any(axis=(1, 3))
            is_max &= keep
    except Exception:
        pass                 # no scipy: tag everything, as before
    jj, ii = np.nonzero(is_max)
    lat_step = float(lats[1] - lats[0]) if len(lats) > 1 else -0.01
    lon_step = float(lons[1] - lons[0]) if len(lons) > 1 else 0.01
    out = []
    for j, i in zip(jj, ii):
        sub_j, sub_i = divmod(int(arg[j, i]), POOL)
        gj, gi = j * POOL + sub_j, i * POOL + sub_i
        lat = float(lats[0] + gj * lat_step)
        lon = float(lons[0] + gi * lon_step)
        if lon > 180:
            lon -= 360.0
        out.append((float(m[j, i]), lat, lon))
    out.sort(key=lambda t: -t[0])
    return out


def _separate(cands: list) -> list:
    """Tallest first; a candidate is kept only if no kept tag is inside
    MIN_SEP_NM."""
    kept = []
    for km, lat, lon in cands:
        k = math.cos(math.radians(lat))
        ok = True
        for _, la, lo in kept:
            if math.hypot((lon - lo) * k * 60.0, (lat - la) * 60.0) < MIN_SEP_NM:
                ok = False
                break
        if ok:
            kept.append((km, lat, lon))
            if len(kept) >= MAX_TAGS:
                break
    return kept


# -------------------------------------------------------------- pass

def run_once(static_dir) -> dict:
    import requests
    r = requests.get(URL, timeout=30,
                     headers={"User-Agent": "bluemet.org ops"})
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    # The "latest" file carries no timestamp in its name; the server's
    # Last-Modified is the scan time to within a minute.
    try:
        from email.utils import parsedate_to_datetime
        stamp = parsedate_to_datetime(
            r.headers.get("Last-Modified", "")).strftime("%Y%m%d-%H%M%S")
    except Exception:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    vals, lats, lons, how = _decode(raw)
    cands = _peaks(vals, lats, lons)
    tags = _separate(cands)
    out = {"stamp": stamp, "n": len(tags), "min_fl": MIN_FL,
           "sep_nm": MIN_SEP_NM, "decoder": how,
           "tags": [{"lat": round(la, 3), "lon": round(lo, 3),
                     "fl": int(round(km / KM_PER_FL / 10.0)) * 10,
                     "km": round(km, 1)} for km, la, lo in tags]}
    p = Path(static_dir) / "etop_tags.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(p)
    _log(f"{len(cands)} peaks >= FL{MIN_FL}, {len(tags)} tagged ({how})")
    return out


def _loop(static_dir):
    time.sleep(START_DELAY_S)
    while True:
        try:
            run_once(static_dir)
        except Exception as exc:
            _log(f"pass failed: {type(exc).__name__}: {exc}"[:160])
        time.sleep(PERIOD_S)


def ensure_etop_tags_warmer(static_dir) -> bool:
    if os.environ.get("ETOP_TAGS", "on").lower() == "off":
        return False
    with _LOCK:
        t = _STATE["thread"]
        if t is not None and t.is_alive():
            return True
        t = threading.Thread(target=_loop, args=(Path(static_dir),),
                             daemon=True, name="etop-tags")
        t.start()
        _STATE["thread"] = t
    return True


def read(static_dir, max_age_s: float = 900.0) -> dict:
    """The current tag file, or {} if missing or stale."""
    p = Path(static_dir) / "etop_tags.json"
    try:
        if time.time() - p.stat().st_mtime > max_age_s:
            return {}
        return json.loads(p.read_text())
    except Exception:
        return {}
