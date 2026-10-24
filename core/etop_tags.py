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
    thresh = (MIN_FL - (30 if BLEND else 0)) * KM_PER_FL   # candidates a bit lower; the blend decides
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

# BLEND WITH THE RADAR'S OWN ECHO TOPS (24 Oct). MRMS EchoTop_18 samples
# height in 1 km steps above 9 km and reports the highest cell, so its
# peaks read 2-5 kft above what CoSPA and the 88D Enhanced Echo Tops
# (EET, product 135, 1 km) show for the same storm. Each tag is now the
# mean of the MRMS peak and the nearest radar's EET at that point,
# floored to 10 FL; with no EET in reach the MRMS value is taken half
# a vertical level down. ETOP_TAGS_BLEND=off restores MRMS alone.
BLEND = os.environ.get("ETOP_TAGS_BLEND", "on").lower() != "off"
EET_URL = ("https://tgftp.nws.noaa.gov/SL.us008001/DF.of/DC.radar/"
           "DS.135et/SI.{site}/sn.last")
EET_MAX_KM = 230.0           # EET reaches 346 km, but it thins out past this
_eet_cache: dict = {}        # site -> (fetched_at, Level3File or None)


def _eet_file(site: str):
    """Level3File of the site's latest EET, cached for one pass."""
    import io
    import requests
    from metpy.io import Level3File
    now = time.time()
    hit = _eet_cache.get(site)
    if hit and now - hit[0] < 120:
        return hit[1]
    f = None
    try:
        r = requests.get(EET_URL.format(site=site.lower()), timeout=20,
                         headers={"User-Agent": "bluemet.org ops"})
        if r.status_code == 200 and len(r.content) > 200:
            f = Level3File(io.BytesIO(r.content))
    except Exception:
        f = None
    _eet_cache[site] = (now, f)
    return f


def _eet_kft(lat: float, lon: float):
    """Nearest 88D's EET (kft MSL) within ~3 km of a point, or None."""
    import numpy as np
    try:
        from core import nexrad_sites as NS
        site, km = NS.nearest_site(lat, lon)
    except Exception:
        return None
    if not site or km > EET_MAX_KM:
        return None
    f = _eet_file(site)
    if f is None:
        return None
    try:
        blk = f.sym_block[0][0]
        data = np.asarray(blk["data"])
        vals = np.asarray(f.map_data(data))
        if vals.ndim == 3:                 # MetPy's EET mapper: (tops kft, topped flag)
            vals = vals[0]
        vals = np.where(np.isnan(vals), 0.0, vals)
        rlat, rlon = f.prod_desc.lat / 1000.0, f.prod_desc.lon / 1000.0
        k = 111.32
        dx = (lon - rlon) * k * math.cos(math.radians(rlat))
        dy = (lat - rlat) * k
        rng_km = math.hypot(dx, dy)
        brg = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
        gate_km = 1.0                      # product 135: 1 km x 1 deg radials, 346 gates
        gi = int(round(rng_km / gate_km))
        if gi >= vals.shape[1] - 2:
            return None
        start = np.asarray(blk["start_az"]) % 360.0
        ai = int(np.argmin(np.abs((start - brg + 180.0) % 360.0 - 180.0)))
        rows = [(ai + d) % vals.shape[0] for d in (-2, -1, 0, 1, 2)]
        g0, g1 = max(0, gi - 3), min(vals.shape[1], gi + 4)
        v = float(np.max(vals[rows][:, g0:g1]))
        return v if v > 0 else None
    except Exception:
        return None


def _blend_fl(km: float, lat: float, lon: float) -> tuple:
    """(fl, mrms_fl, eet_fl|None) for one peak."""
    mrms_fl = km / KM_PER_FL
    if not BLEND:
        return int(round(mrms_fl / 10.0)) * 10, int(round(mrms_fl)), None
    eet = _eet_kft(lat, lon)
    if eet is not None:
        # mean of the two, but never more than 5,000 ft above what the
        # radar itself reports (an MRMS peak far above EET is a mosaic
        # artefact - seen 24 Oct as 17 km "tops" over New Mexico
        # against a 28 kft EET)
        # Measured 24 Oct across 20 tags: MRMS ran 6-10 kft above EET
        # everywhere, and EET matched CoSPA. So two-thirds EET, one-
        # third MRMS, and never more than 3,000 ft above the radar.
        fl = min((2.0 * eet * 10.0 + mrms_fl) / 3.0, eet * 10.0 + 30.0)
    else:
        # beyond EET reach (far range, Bahamas/Caribbean) the lowest beam
        # is already high and MRMS inflates most: take 1.5 km off
        fl = (km - (1.5 if km >= 9.0 else 0.5)) / KM_PER_FL
    return int(math.floor(fl / 10.0)) * 10, int(round(mrms_fl)), (int(round(eet * 10)) if eet is not None else None)


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
    rows = []
    n_eet = 0
    for km, la, lo in tags:
        fl, mfl, efl = _blend_fl(km, la, lo)
        if fl < MIN_FL:
            continue
        if efl is not None:
            n_eet += 1
        rows.append({"lat": round(la, 3), "lon": round(lo, 3), "fl": fl,
                     "km": round(km, 1), "mrms_fl": mfl, "eet_fl": efl})
    out = {"stamp": stamp, "n": len(rows), "min_fl": MIN_FL,
           "sep_nm": MIN_SEP_NM, "decoder": how,
           "blend": BLEND, "eet_used": n_eet, "tags": rows}
    p = Path(static_dir) / "etop_tags.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(p)
    _log(f"{len(cands)} peaks, {len(rows)} tagged >= FL{MIN_FL} "
         f"({n_eet} blended with 88D EET, {how})")
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
