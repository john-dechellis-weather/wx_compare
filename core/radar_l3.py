"""Single-site super-res radar from Level III N0B - fast path.

Target path: core/radar_l3.py

WHY THIS AND NOT LEVEL II
-------------------------
core/radar_l2.py needs a full Level II volume (10-20 MB of bzip2, 14+
tilts) and pyart's grid_from_radars, which its own notes measure at
37-95 s per scan before download and decode. For a MAP we only draw
the 0.5 deg tilt, and NWS already ships exactly that as a Level III
product:

    N0B  super-resolution digital base reflectivity
         0.5 deg tilt, 0.5 deg azimuth x 0.25 km gates, 0.5 dBZ steps
         the same resolution as Level II at that tilt
         ~150 KB per scan on the public unidata-nexrad-level3 bucket

What is lost against Level II: the higher tilts. The one dual-pol
field that matters for a clean picture comes as its own Level III
product (N0C, correlation coefficient, same scan) and is used below.

CLUTTER. N0B is not quality-controlled the way MRMS is. Measured
21 Sep 16:23Z against the MRMS scan a minute earlier: KTLX N0B had
echo over 89% of a 250 x 250 km box where MRMS had 3.5% - clear-air
return, insects, birds, ground clutter. KOKX showed up to 38 dBZ
within 25 km of the radar on a day MRMS was dry.

Correlation coefficient (N0C) alone does not fix it: at CC 0.85 a
third of the clear-air return survived, and raising the threshold far
enough to remove it (0.95) also erased half the real rain.

So the primary filter is MRMS itself. The MRMS warmer saves an echo
mask with every scan (core.mrms.echo_mask); an N0B pixel is drawn
only where MRMS, dilated by MASK_GROW_KM (1 km) to cover the minute
between the scans, also sees weather. Measured on the same pair: all
of the echo MRMS confirms is kept, about 1% of the clear-air return
survives (a thin fringe along real echo), and what is drawn is at
Level III's own 250 m detail. A 3 km growth filled every small MRMS
speck with a visible diamond of clear-air return, so 1 km it is. MRMS decides WHERE there is
weather; N0B decides what it looks like.

If no MRMS mask is within MASK_MAX_SKEW_S (warmer off, or just after
a restart), the CC filter is used instead, and the manifest says so.

WHY IT IS FAST
--------------
A radar never moves. Which (radial, gate) lands on which map pixel is
fixed geometry, so it is computed ONCE per site and domain (the
lookup table, cached to disk) and every scan after that is one numpy
fancy-index plus a 256-entry palette lookup. No KD-tree, no gridding.

Pixels are laid out in Web Mercator rows, which is what a deck.gl
BitmapLayer stretches between its bounds, so the image is placed
correctly across the whole box rather than only near its middle.

Measured in the sandbox, KOKX, N90 box (250 x 250 km at 250 m):
see BENCH in the handoff notes; list + download + decode + render +
WebP encode is about a second, against 40-100 s for the Level II path.

NEVER ON A RENDER PATH. The warmer writes a WebP and a manifest per
scan; pages read the newest manifest and draw one BitmapLayer.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BUCKET = "https://unidata-nexrad-level3.s3.amazonaws.com"
_HDRS = {"User-Agent": "bluemet.org"}

# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------
# name -> (site, centre lat, centre lon, half-width km, half-height km,
#          pixel metres). Site is the 3-letter Level III id (KOKX -> OKX).
# The N90 box matches radar_l2's "N90 merged" view: 250 x 250 km on JFK.
DOMAINS = {
    "N90": ("OKX", 40.6398, -73.7789, 125.0, 125.0, 250.0),
    # Potomac: KLWX (Sterling VA) over a DCA-centred box, which also
    # takes in IAD and BWI.
    "DCA": ("LWX", 38.8512, -77.0377, 125.0, 125.0, 250.0),
}
# Label per domain, for pages.
DOMAIN_LABEL = {"N90": "N90 - KOKX", "DCA": "DC - KLWX"}

# STATION DOMAINS: one small box per hub on the Station Forecast page,
# from the station's nearest NEXRAD, so the airport scope opens on a
# 250 m picture that is already on disk, with an hour's loop.
#
# WHICH RADAR. A station uses the nearest radar within STATION_MAX_MI
# (130 mi) of the field that has a recent scan; only beyond that does
# the scope fall back to MRMS. Candidates are listed nearest first,
# so the airport's own TDWR (Terminal Doppler Weather Radar: a C-band
# radar a few miles off the field, its long-range reflectivity
# product TZL - 300 m gates, 1 deg radials, 416 km - posted to the
# same bucket every ~6 min) is tried first and the NEXRAD covers when
# the TDWR is down or absent. "T:JFK" is the TDWR filed under JFK,
# "OKX" the NEXRAD KOKX.
#
# Box: 150 km half-width so zooming the scope out still shows the
# radar, MRMS beyond it. 1200 x 1200 px, ~100-300 KB a frame.
STATION_MAX_MI = float(os.environ.get("L3_STATION_MAX_MI", "130"))
STATION_RADAR = {
    "KJFK": ["T:JFK", "OKX"], "KLGA": ["T:JFK", "OKX"],
    "KEWR": ["T:EWR", "T:JFK", "OKX"], "KHPN": ["T:JFK", "OKX"],
    "KBOS": ["T:BOS", "BOX"], "KBDL": ["T:BOS", "BOX"],
    "KDCA": ["T:DCA", "T:IAD", "LWX"], "KMCO": ["T:MCO", "MLB"],
    "KFLL": ["T:FLL", "T:MIA", "AMX"], "KDJT": ["T:PBI", "T:FLL", "AMX"],
    "KTPA": ["T:TPA", "TBW"], "KLAX": ["SOX"], "KSFO": ["MUX"],
    "TJSJ": ["T:SJU", "JUA"],
}
# Radar positions, for the 25-mile rule before any file is opened.
# TDWR positions are the ones each site's own files report.
RADAR_POS = {
    "T:JFK": (40.589, -73.880), "T:EWR": (40.593, -74.270),
    "T:BOS": (42.158, -70.933), "T:DCA": (38.759, -76.962),
    "T:IAD": (39.084, -77.529), "T:MCO": (28.343, -81.325),
    "T:FLL": (26.143, -80.344), "T:MIA": (25.758, -80.491),
    "T:PBI": (26.688, -80.273), "T:TPA": (27.860, -82.518),
    "T:SJU": (18.474, -66.179),
    "OKX": (40.865, -72.864), "BOX": (41.956, -71.137),
    "LWX": (38.976, -77.487), "MLB": (28.113, -80.654),
    "AMX": (25.611, -80.413), "TBW": (27.705, -82.402),
    "SOX": (33.818, -117.636), "MUX": (37.155, -121.898),
    "JUA": (18.116, -66.078),
}


def is_tdwr(site: str) -> bool:
    return site.startswith("T:")


def bucket_site(site: str) -> str:
    """The 3-letter id the bucket files it under."""
    return site[2:] if is_tdwr(site) else site


def product_for(site: str) -> str:
    return "TZL" if is_tdwr(site) else PRODUCT


def _mi(a, b) -> float:
    import math
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    dla, dlo = la2 - la1, math.radians(b[1] - a[1])
    h = (math.sin(dla / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2)
    return 2 * 3958.8 * math.asin(min(1.0, math.sqrt(h)))


def station_site(icao: str, now=None):
    """The radar a station uses now: the nearest candidate within
    STATION_MAX_MI with a scan in the last 20 min, or None (MRMS).
    One list call per candidate, cached per pass by recent_keys."""
    st_pos = STATION_LATLON.get(icao.upper())
    if not st_pos:
        return None
    for site in STATION_RADAR.get(icao.upper(), []):
        pos = RADAR_POS.get(site)
        if not pos or _mi(st_pos, pos) > STATION_MAX_MI:
            continue
        try:
            k = latest_key(bucket_site(site), product_for(site), now)
        except Exception:
            k = None
        if k and age_s(key_stamp(k)) < 1200:
            return site
    return None
STATION_LATLON = {
    "KJFK": (40.640, -73.779), "KLGA": (40.777, -73.872),
    "KEWR": (40.689, -74.175), "KHPN": (41.067, -73.708),
    "KBOS": (42.363, -71.006), "KBDL": (41.939, -72.683),
    "KDCA": (38.852, -77.038), "KMCO": (28.429, -81.309),
    "KFLL": (26.072, -80.152), "KTPA": (27.976, -82.533),
    "KDJT": (26.683, -80.096), "KLAX": (33.942, -118.408),
    "KSFO": (37.619, -122.375), "TJSJ": (18.439, -66.002),
}


def station_domain(icao: str) -> str:
    return f"STN_{icao.upper()}"


for _i, _cands in STATION_RADAR.items():
    _la, _lo = STATION_LATLON[_i]
    # The site slot is resolved per pass by station_site(); the entry
    # holds the first candidate as a placeholder.
    DOMAINS[station_domain(_i)] = (_cands[0], _la, _lo, 150.0, 150.0, 250.0)
    DOMAIN_LABEL[station_domain(_i)] = _i


def domain_site(domain: str) -> str | None:
    """Radar for a domain right now. Station domains apply the 25-mile
    rule; the others are fixed."""
    if domain.startswith("STN_"):
        return station_site(domain[4:])
    return DOMAINS[domain][0]


def loop_minutes(domain: str) -> int:
    """How far back a domain's loop reaches: an hour everywhere."""
    return LOOP_MINUTES
PRODUCT = "N0B"
CC_PRODUCT = "N0C"
CC_MIN = float(os.environ.get("L3_CC_MIN", "0.85"))
CC_KEEP_DBZ = float(os.environ.get("L3_CC_KEEP_DBZ", "40"))
CC_FILTER = os.environ.get("L3_CC_FILTER", "on").lower() != "off"
MASK_GROW_KM = float(os.environ.get("L3_MASK_GROW_KM", "1"))
MASK_MAX_SKEW_S = int(os.environ.get("L3_MASK_MAX_SKEW_S", "480"))
# Where the MRMS warmer writes its fields (static/, beside the chunks).
MRMS_DIR = Path(__file__).resolve().parent.parent / "static"

# Reflectivity floor for drawing. Below this the pixel is transparent.
DBZ_MIN = float(os.environ.get("L3_DBZ_MIN", "5"))
WEBP_Q = int(os.environ.get("L3_WEBP_Q", "85"))
# Bump when the rendered output changes; manifests carry it.
RENDER_STYLE = 3


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def recent_keys(site: str, n: int = 1, product: str = PRODUCT,
                now=None) -> list:
    """The newest n object keys for site/product, oldest first. Keys
    look like OKX_N0B_2026_09_21_16_18_52 and sort by time. Lists today
    and, in the first two hours after 00Z, yesterday too (a loop at
    00:30Z reaches back past midnight)."""
    import requests

    now = now or datetime.now(timezone.utc)
    days = [now]
    if now.hour < 2:
        days.insert(0, datetime.fromtimestamp(now.timestamp() - 86400,
                                              timezone.utc))
    found = []
    for d in days:
        prefix = f"{site}_{product}_{d:%Y_%m_%d}_"
        token = None
        while True:
            params = {"list-type": "2", "prefix": prefix,
                      "max-keys": "1000"}
            if token:
                params["continuation-token"] = token
            r = requests.get(f"{BUCKET}/", params=params, timeout=15,
                             headers=_HDRS)
            r.raise_for_status()
            found += re.findall(r"<Key>([^<]+)</Key>", r.text)
            m = re.search(r"<NextContinuationToken>([^<]+)<", r.text)
            if not m:
                break
            token = m.group(1)
    return sorted(set(found))[-n:] if found else []


def latest_key(site: str, product: str = PRODUCT, now=None):
    """Newest object key for site/product, or None."""
    k = recent_keys(site, 1, product, now)
    return k[-1] if k else None


def loop_keys(site: str, minutes: int = None, product: str = PRODUCT,
              now=None) -> list:
    """Keys of every scan in the last `minutes`, oldest first, capped
    at LOOP_MAX_FRAMES; minutes=0 means the newest scan only.
    Time-based, so the loop is one hour whether the radar is scanning
    every 4 min (precipitation) or every 10 (clear air)."""
    if minutes is None:
        minutes = LOOP_MINUTES
    now = now or datetime.now(timezone.utc)
    if minutes <= 0:
        k = latest_key(site, product, now)
        return [k] if k else []
    keys = recent_keys(site, LOOP_MAX_FRAMES, product, now)
    cut = now.timestamp() - minutes * 60
    return [k for k in keys if _stamp_s(key_stamp(k)) >= cut]


def age_s(stamp: str) -> float:
    """Seconds since a scan stamp (YYYYMMDD-HHMMSS)."""
    try:
        return time.time() - _stamp_s(stamp)
    except Exception:
        return 1e9


def _stamp_s(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
        tzinfo=timezone.utc).timestamp()


def key_stamp(key: str) -> str:
    """OKX_N0B_2026_09_21_16_18_52 -> 20260921-161852."""
    p = key.split("_")
    return f"{p[2]}{p[3]}{p[4]}-{p[5]}{p[6]}{p[7]}"


_dl: dict = {}
_dl_lock = threading.Lock()


def fetch(key: str) -> bytes:
    """One download per key per process, briefly cached: four New York
    stations share KOKX and would otherwise fetch the same 150 KB
    four times a scan."""
    import requests

    with _dl_lock:
        hit = _dl.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    r = requests.get(f"{BUCKET}/{key}", timeout=20, headers=_HDRS)
    r.raise_for_status()
    with _dl_lock:
        _dl[key] = (time.time(), r.content)
        for k in [k for k, v in _dl.items() if time.time() - v[0] > 600]:
            del _dl[k]
    return r.content


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def decode_cc(raw: bytes) -> dict:
    """N0C bytes -> correlation coefficient (float, NaN = no data) and
    geometry. 1 deg radials, 0.25 km gates to 300 km."""
    import numpy as np
    from metpy.io import Level3File

    f = Level3File(io.BytesIO(raw))
    blk = f.sym_block[0][0]
    data = np.asarray(blk["data"])
    return {"cc": np.asarray(f.map_data(data), dtype="float32"),
            "start_az": np.asarray(blk["start_az"], dtype="float64"),
            "max_range_km": float(f.max_range),
            "lat": float(f.lat), "lon": float(f.lon)}


def decode(raw: bytes) -> dict:
    """N0B bytes -> raw 8-bit codes plus the geometry to place them.

    Codes are kept as uint8 rather than converted to dBZ: the palette
    is indexed by code directly, which is both faster and exact."""
    import numpy as np
    from metpy.io import Level3File

    f = Level3File(io.BytesIO(raw))
    blk = f.sym_block[0][0]
    codes = np.asarray(blk["data"], dtype="uint8")        # (radials, gates)
    start_az = np.asarray(blk["start_az"], dtype="float64")
    # Code -> dBZ through the product's own thresholds: min, step and
    # number of levels live in the product header. Codes 0 and 1 are
    # below threshold / range folded.
    lo, inc = f.thresholds[0], f.thresholds[1]
    dbz_of_code = np.full(256, np.nan, dtype="float32")
    dbz_of_code[2:] = lo / 10.0 + (np.arange(2, 256) - 2) * inc / 10.0
    return {
        "codes": codes,
        "start_az": start_az,
        "max_range_km": float(f.max_range),
        "lat": float(f.lat), "lon": float(f.lon),
        "elev": float(f.metadata.get("el_angle", 0.5)),
        "time": f.metadata.get("prod_time"),
        "dbz_of_code": dbz_of_code,
    }


# ---------------------------------------------------------------------------
# Lookup table
# ---------------------------------------------------------------------------
def _merc_y(lat):
    import numpy as np
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))


def _inv_merc_y(y):
    import numpy as np
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def domain_grid(domain: str):
    """(lats (H,), lons (W,), bounds [W,S,E,N]) for a domain. Rows are
    evenly spaced in Web Mercator, north to south."""
    import numpy as np

    _site, clat, clon, hw_km, hh_km, px_m = DOMAINS[domain]
    dlat = hh_km / 111.32
    dlon = hw_km / (111.32 * math.cos(math.radians(clat)))
    w, e = clon - dlon, clon + dlon
    s, n = clat - dlat, clat + dlat
    ncol = int(round(2 * hw_km * 1000 / px_m))
    nrow = int(round(2 * hh_km * 1000 / px_m))
    lons = w + (np.arange(ncol) + 0.5) * (e - w) / ncol
    ys, yn = _merc_y(s), _merc_y(n)
    lats = _inv_merc_y(yn - (np.arange(nrow) + 0.5) * (yn - ys) / nrow)
    return lats, lons, [w, s, e, n]


def build_lut(domain: str, rlat: float, rlon: float, ngates: int,
              max_range_km: float, az_width: float = 0.5):
    """Per pixel: azimuth bin (az_width deg) and gate index; -1 = none.

    Ground range from the radar via pyproj's geodesic, which is the
    distance along the Earth; at the 0.5 deg tilt slant range differs
    from it by well under a gate inside 150 km."""
    import numpy as np
    from pyproj import Geod

    lats, lons, _ = domain_grid(domain)
    LON, LAT = np.meshgrid(lons, lats)
    az, _baz, dist = Geod(ellps="WGS84").inv(
        np.full(LON.shape, rlon), np.full(LAT.shape, rlat), LON, LAT)
    az = np.mod(az, 360.0)
    gate_km = max_range_km / ngates
    gate = np.floor(dist / 1000.0 / gate_km).astype("int32")
    nbin = int(round(360.0 / az_width))
    abin = np.floor(az / az_width).astype("int32") % nbin
    gate[(gate < 0) | (gate >= ngates)] = -1
    return abin.astype("int16"), gate.astype("int16")


_LUT_CACHE: dict = {}


def lut_for(domain: str, scan: dict, cache_dir: Path,
            field: str = "codes"):
    """LUT for this domain and radar geometry, from memory, disk, or
    built once. Keyed by domain, radar position, gate layout and
    azimuth width, so a change in any of them builds a new one."""
    import numpy as np

    nrad, ngates = scan[field].shape
    az_width = 360.0 / (720 if nrad > 400 else 360)
    key = (f"{domain}_{scan['lat']:.3f}_{scan['lon']:.3f}_{ngates}_"
           f"{scan['max_range_km']:.1f}_{az_width:g}")
    if key in _LUT_CACHE:
        return _LUT_CACHE[key]
    path = Path(cache_dir) / f"l3lut_{key}.npz"
    try:
        with np.load(path) as z:
            lut = (z["abin"], z["gate"])
    except Exception:
        lut = build_lut(domain, scan["lat"], scan["lon"], ngates,
                        scan["max_range_km"], az_width)
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            np.savez_compressed(path, abin=lut[0], gate=lut[1])
        except OSError:
            pass
    _LUT_CACHE[key] = lut
    return lut


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def code_palette(dbz_of_code):
    """256 x RGBA, indexed by N0B code. Same AWIPS colours and alpha
    ramp as the MRMS mosaic, so both radars read the same on a page."""
    import numpy as np

    from core import mrms as _MR

    lut = _MR.palette("REFL")                     # index 0 = clear
    levels = np.asarray(_MR.PRODUCTS["REFL"]["levels"], dtype="float32")
    pal = np.zeros((256, 4), dtype="uint8")
    ok = np.isfinite(dbz_of_code) & (dbz_of_code >= DBZ_MIN)
    band = np.digitize(np.nan_to_num(dbz_of_code, nan=-99.0), levels)
    pal[ok] = lut[np.clip(band[ok], 0, len(lut) - 1)]
    return pal


def _radials(start_az, nbin: int):
    """Azimuth bin -> radial index for THIS scan. Radials start at a
    slightly different azimuth every volume, so this is rebuilt per
    scan (360 or 720 entries, microseconds). A radial's centre picks
    its bin; a bin no radial claimed takes its neighbour's."""
    import numpy as np

    width = 360.0 / nbin
    centre = np.mod(start_az + width / 2, 360.0)
    rad_of_bin = np.full(nbin, -1, dtype="int32")
    rad_of_bin[(np.floor(centre / width).astype("int32")) % nbin] = \
        np.arange(len(start_az))
    if (rad_of_bin < 0).any():
        idx = np.where(rad_of_bin >= 0)[0]
        near = idx[np.searchsorted(idx, np.arange(nbin)) % len(idx)]
        rad_of_bin = np.where(rad_of_bin >= 0, rad_of_bin,
                              rad_of_bin[near])
    return rad_of_bin


def _sample(arr, start_az, abin, gate, fill):
    """Field -> image through a LUT."""
    import numpy as np

    nbin = 720 if arr.shape[0] > 400 else 360
    rad = _radials(start_az, nbin)[abin]
    valid = (gate >= 0) & (rad >= 0)
    out = np.full(gate.shape, fill, dtype=arr.dtype)
    out[valid] = arr[rad[valid], gate[valid]]
    return out


def mrms_mask(domain: str, mask, grow_km: float = MASK_GROW_KM):
    """MRMS echo mask (CONUS grid) -> the domain's pixels, dilated."""
    import numpy as np
    from scipy import ndimage as ndi

    from core import mrms as _MR

    lats, lons, _ = domain_grid(domain)
    w, s_, e, n = _MR.BOUNDS
    # Outside the MRMS CONUS grid (San Juan is south of it) there is
    # no mask to apply; the caller falls back to the CC filter.
    if lats.min() < s_ or lats.max() > n or lons.min() < w or lons.max() > e:
        return None
    rows, cols = mask.shape
    yi = np.clip(((n - lats) / (n - s_) * rows).astype(int), 0, rows - 1)
    xi = np.clip(((lons - w) / (e - w) * cols).astype(int), 0, cols - 1)
    # Crop, dilate on the 1 km grid, then sample - dilating the
    # domain-sized image would cost 16x the work for the same result.
    y0, y1 = yi.min(), yi.max() + 1
    x0, x1 = xi.min(), xi.max() + 1
    sub = mask[y0:y1, x0:x1]
    it = max(0, int(round(grow_km)))
    if it:
        sub = ndi.binary_dilation(sub, iterations=it)
    return sub[np.ix_(yi - y0, xi - x0)]


def render(scan: dict, domain: str, cache_dir: Path, cc: dict = None,
           mask=None):
    """Scan -> RGBA image (H, W, 4) for the domain.

    mask: domain-shaped bool from mrms_mask; pixels outside it are
    blanked. Without one, cc (if given) blanks non-meteorological echo
    under CC_KEEP_DBZ instead."""
    import numpy as np

    abin, gate = lut_for(domain, scan, cache_dir)
    img = _sample(scan["codes"], scan["start_az"], abin, gate, 0)
    if mask is not None:
        img[~mask] = 0
    elif cc is not None:
        cabin, cgate = lut_for(domain, cc, cache_dir, field="cc")
        ccimg = _sample(cc["cc"], cc["start_az"], cabin, cgate, np.nan)
        dbz = scan["dbz_of_code"][img]
        drop = (ccimg < CC_MIN) & ~(dbz >= CC_KEEP_DBZ)
        img[drop] = 0
    return code_palette(scan["dbz_of_code"])[img]


#: Last outcome per domain, for the page: {"ok": bool, "note": str,
#: "at": epoch}. Lets the page say WHY there is no image instead of
#: "warming" forever.
STATUS: dict = {}
_build_locks: dict = {}
_build_locks_guard = threading.Lock()


def _lock_for(domain: str) -> threading.Lock:
    with _build_locks_guard:
        return _build_locks.setdefault(domain, threading.Lock())


def build(domain: str, outdir, wait: bool = True, key: str = None,
          site: str = None) -> tuple:
    """Fetch, decode and render the newest scan. (stamp, note).

    One build per domain at a time: the page can build on demand
    while the warmer is running, and whichever arrives second waits
    for the first (or, with wait=False, returns at once) and then
    finds the result cached."""
    lk = _lock_for(domain)
    if not lk.acquire(blocking=wait):
        return None, "busy"
    try:
        stamp, note = _build(domain, outdir, key, site)
        STATUS[domain] = {"ok": stamp is not None, "note": note,
                          "at": time.time()}
        return stamp, note
    except Exception as exc:
        STATUS[domain] = {"ok": False, "at": time.time(),
                          "note": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
        lk.release()


def _build(domain: str, outdir, key: str = None, site: str = None) -> tuple:
    from PIL import Image

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    site = site or domain_site(domain)
    if site is None:
        return None, f"{domain}: no radar within {STATION_MAX_MI:.0f} mi"
    bsite, prod = bucket_site(site), product_for(site)
    t0 = time.time()
    key = key or latest_key(bsite, prod)
    if not key:
        return None, f"no {bsite} {prod} files listed"
    stamp = key_stamp(key)
    man = outdir / f"l3_{domain}_{stamp}.json"
    if man.exists():
        return stamp, "cached"
    t1 = time.time()
    raw = fetch(key)
    t2 = time.time()
    scan = decode(raw)
    cc = None
    mask = None
    cc_note = "unfiltered"
    try:
        from core import mrms as _MR
        grid, mstamp = _MR.echo_mask(MRMS_DIR, stamp, MASK_MAX_SKEW_S)
        if grid is not None:
            mask = mrms_mask(domain, grid)
            del grid
            cc_note = (f"MRMS-masked ({mstamp})" if mask is not None
                       else "outside MRMS grid")
    except Exception as exc:
        cc_note = f"no MRMS mask ({type(exc).__name__})"
    if mask is None and CC_FILTER:
        # Same volume, same timestamp in the key.
        # TDWRs are single-pol: no CC product, so MRMS or nothing.
        try:
            if is_tdwr(site):
                raise LookupError("TDWR")
            cc = decode_cc(fetch(key.replace(f"_{PRODUCT}_",
                                             f"_{CC_PRODUCT}_")))
            cc_note = "CC filtered (no MRMS mask)"
        except Exception as exc:
            cc_note = f"no CC ({type(exc).__name__}), unfiltered"
    t3 = time.time()
    rgba = render(scan, domain, outdir, cc, mask)
    t4 = time.time()
    name = f"l3_{domain}_{stamp}.webp"
    tmp = outdir / f".{name}.tmp"
    Image.fromarray(rgba, mode="RGBA").save(tmp, "WEBP",
                                            quality=WEBP_Q, method=4)
    os.replace(tmp, outdir / name)
    t5 = time.time()
    _l, _o, bounds = domain_grid(domain)
    # Manifest LAST, so a reader never finds a half-written image.
    man.write_text(json.dumps({
        "style": RENDER_STYLE, "name": name, "bounds": bounds,
        "site": (f"T{bsite}" if is_tdwr(site) else f"K{bsite}"),
        "product": prod, "stamp": stamp,
        "elev": scan["elev"],
        "radar": [scan["lon"], scan["lat"]],
        "filter": ("mrms" if mask is not None
                   else "cc" if cc is not None else "none")}))
    return stamp, (f"{domain} {'T' if is_tdwr(site) else 'K'}{bsite} "
                   f"{stamp}: list {t1 - t0:.2f}s, "
                   f"download {len(raw) // 1024} KB {t2 - t1:.2f}s, "
                   f"decode+filter {t3 - t2:.2f}s ({cc_note}), "
                   f"render {t4 - t3:.2f}s, "
                   f"webp {(outdir / name).stat().st_size // 1024} KB "
                   f"{t5 - t4:.2f}s")


def newest(outdir, domain: str = "N90"):
    """(manifest dict, stamp) for the newest current-style scan, or
    (None, None)."""
    for p in sorted(Path(outdir).glob(f"l3_{domain}_*.json"),
                    reverse=True):
        try:
            m = json.loads(p.read_text())
            if m.get("style") == RENDER_STYLE:
                return m, m.get("stamp")
        except Exception:
            continue
    return None, None


def frames(outdir, domain: str, minutes: int = None) -> list:
    """Manifests of the current-style frames from the last `minutes`,
    OLDEST first - the loop's order."""
    minutes = minutes or max(LOOP_MINUTES, 15)
    cut = time.time() - minutes * 60
    out = []
    for p in sorted(Path(outdir).glob(f"l3_{domain}_*.json")):
        try:
            m = json.loads(p.read_text())
            if (m.get("style") == RENDER_STYLE
                    and _stamp_s(m["stamp"]) >= cut):
                out.append(m)
        except Exception:
            continue
    return out[-LOOP_MAX_FRAMES:]


def backfill(domain: str, outdir) -> int:
    """Build every scan of the last LOOP_MINUTES not built yet, newest
    first so the current frame is never the one waiting. Returns
    frames built. Each is ~0.5 s once the lookup tables exist."""
    site = domain_site(domain)
    if site is None:
        return 0
    built = 0
    for key in reversed(loop_keys(bucket_site(site), loop_minutes(domain),
                                  product_for(site))):
        man = Path(outdir) / f"l3_{domain}_{key_stamp(key)}.json"
        if man.exists():
            continue
        stamp, note = build(domain, outdir, key=key, site=site)
        if stamp and note != "cached":
            built += 1
            _log(outdir, note)
    return built


_bg = {"busy": set()}


def backfill_bg(domain: str, outdir) -> None:
    """backfill() in a background thread, at most one per domain - for
    a page that should show what exists now and let the rest arrive."""
    if domain in _bg["busy"]:
        return

    def _run():
        try:
            backfill(domain, outdir)
            _prune(outdir, domain, KEEP)
        except Exception as exc:
            _log(outdir, f"backfill FAILED {domain}: "
                         f"{type(exc).__name__}: {exc}")
        finally:
            _bg["busy"].discard(domain)

    _bg["busy"].add(domain)
    threading.Thread(target=_run, daemon=True,
                     name=f"l3-backfill-{domain}").start()


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
SLEEP_S = int(os.environ.get("L3_SLEEP_S", "60"))
# Loop length: the last hour, whatever the scan rate. In precipitation
# (VCP 212, ~4.3 min) that is 13-14 frames; the cap only matters if a
# radar is scanning unusually fast.
LOOP_MINUTES = int(os.environ.get("L3_LOOP_MINUTES", "60"))
LOOP_MAX_FRAMES = int(os.environ.get("L3_LOOP_MAX_FRAMES", "20"))
KEEP = max(int(os.environ.get("L3_KEEP", "20")), LOOP_MAX_FRAMES + 2)
_warm = {"started": False}
_warm_lock = threading.Lock()


def _log(outdir, msg):
    try:
        with open(Path(outdir) / "l3_warmer.log", "a") as fh:
            fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                     f"{msg}\n")
    except OSError:
        pass


def _prune(outdir, domain: str, keep: int):
    for pat in (f"l3_{domain}_*.json", f"l3_{domain}_*.webp"):
        for old in sorted(Path(outdir).glob(pat))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass


def _loop(outdir):
    # Short start delay: the build is ~1 s. Import the heavy pieces
    # up front so the first scan does not pay for them (metpy's unit
    # registry alone is several seconds on a small instance).
    time.sleep(float(os.environ.get("L3_DELAY_S", "5")))
    t0 = time.time()
    try:
        import importlib
        for mod in ("metpy.io", "pyproj", "scipy.ndimage", "PIL.Image"):
            importlib.import_module(mod)
    except Exception as exc:
        _log(outdir, f"import FAILED: {type(exc).__name__}: {exc}")
    _log(outdir, f"L3 warmer started, domains {list(DOMAINS)}, "
                 f"imports {time.time() - t0:.1f}s")
    while True:
        for dom in DOMAINS:
            try:
                # Fills the loop on the first pass after a restart;
                # after that, builds just the new scan (one list call,
                # the rest are already on disk).
                backfill(dom, outdir)
                _prune(outdir, dom, KEEP)
            except Exception as exc:
                _log(outdir, f"FAILED {dom}: {type(exc).__name__}: {exc}")
        # Scans arrive every 2-6 min depending on VCP; polling every
        # minute costs one S3 list call and catches each within ~1 min.
        time.sleep(SLEEP_S)


def ensure_l3_warmer(outdir) -> bool:
    """Idempotent. L3_WARMER=off disables without a deploy."""
    if os.environ.get("L3_WARMER", "on").lower() == "off":
        return False
    with _warm_lock:
        if not _warm["started"]:
            threading.Thread(target=_loop, args=(outdir,), daemon=True,
                             name="l3-warmer").start()
            _warm["started"] = True
    return True


# ---------------------------------------------------------------------------
# Loop page (shared by the Level III page and the Station Forecast scope)
# ---------------------------------------------------------------------------
def loop_html(frames, base, clat, clon, zoom=7.3, height=720,
              min_zoom=4.0, rings_nm=(), center_label="") -> str:
    """MapLibre map + client-side loop. Every frame is an image source
    loaded once; the loop only changes which one is opaque.

    MapLibre with CARTO's vector dark style - the same basemap pydeck
    uses, and keyless (CARTO's raster tiles now stamp "API KEY
    REQUIRED" across the map). An image source is stretched between
    its corners in Web Mercator, which is how radar_l3 lays out its
    rows. Images come from this site's /app/static, the same origin
    as the page, so WebGL accepts them as textures.

    Served as a real file from /app/static and embedded by URL, not
    inlined with components.html: inside components.html's srcdoc
    frame MapLibre loads its style but never requests a tile, so the
    map stayed blank. As a normal same-origin page it works.
    Loop speed comes in the URL (?ms=), so one file serves every
    viewer's speed setting."""
    data = [{"url": f"{base}/app/static/{f['name']}",
             "t": f"{f['stamp'][9:11]}:{f['stamp'][11:13]}Z",
             "b": f["bounds"]} for f in frames]
    radar = frames[-1].get("radar") if frames else None
    style = os.environ.get(
        "BLUEMET_MAP_STYLE",
        "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json")
    return f"""
<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet"
 href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"></script>
<style>
 html,body{{margin:0;background:#000;height:100%;}}
 #m{{height:{height - 46}px;border:1px solid #333;border-radius:12px;
     overflow:hidden;}}
 .bar{{display:flex;align-items:center;gap:12px;height:42px;
      font:bold 12px "Courier New",Courier,monospace;color:#fff;}}
 .bar button{{font:bold 12px "Courier New",monospace;background:#0A0A0A;
      color:#fff;border:1px solid #333;border-radius:6px;padding:5px 12px;
      cursor:pointer;}}
 .bar input[type=range]{{flex:1;accent-color:#00E5FF;}}
 #t{{min-width:64px;}} #n{{color:#B8B8B8;min-width:150px;}}
</style></head><body>
<div class="bar">
  <button id="pp">Pause</button><button id="pv">&#9664;</button>
  <button id="nx">&#9654;</button>
  <input id="sl" type="range" min="0" max="{max(len(data) - 1, 0)}"
         value="{max(len(data) - 1, 0)}">
  <span id="t"></span><span id="n"></span>
</div>
<div id="m"></div>
<script>
const F = {json.dumps(data)};
const RINGS = {json.dumps(list(rings_nm))};
const CENTER = {json.dumps(bool(center_label))};
const MS = Math.max(100, +(new URLSearchParams(location.search).get('ms')) || 500);
const map = new maplibregl.Map({{container:'m', style:{json.dumps(style)},
  center:[{clon}, {clat}], zoom:{zoom}, minZoom:{min_zoom}, maxZoom:12, attributionControl:true}});
map.addControl(new maplibregl.NavigationControl({{showCompass:false}}));
let i = F.length - 1, playing = F.length > 1, tick = null, ready = false;
const sl = document.getElementById('sl'), t = document.getElementById('t'),
      n = document.getElementById('n'), pp = document.getElementById('pp');
function show(k) {{
  i = k; sl.value = k;
  t.textContent = F.length ? F[k].t : 'no frames';
  n.textContent = F.length ? `frame ${{k + 1}}/${{F.length}}` +
    (k === F.length - 1 ? ' (latest)' : '') : '';
  if (!ready) return;
  F.forEach((f, j) => map.setPaintProperty('r' + j, 'raster-opacity',
                                           j === k ? 0.9 : 0));
}}
function step() {{
  show((i + 1) % F.length);
  // Hold the latest frame three times as long.
  tick = setTimeout(step, i === F.length - 1 ? MS * 3 : MS);
}}
function play(on) {{
  playing = on; pp.textContent = on ? 'Pause' : 'Play';
  clearTimeout(tick);
  if (on && F.length > 1) tick = setTimeout(step, MS);
}}
map.on('load', () => {{
  // Radar under the place labels, over everything else.
  const firstSymbol = (map.getStyle().layers.find(l => l.type === 'symbol')
                       || {{}}).id;
  F.forEach((f, j) => {{
    const [w, s, e, nn] = f.b;
    map.addSource('s' + j, {{type:'image', url:f.url,
      coordinates:[[w, nn], [e, nn], [e, s], [w, s]]}});
    map.addLayer({{id:'r' + j, type:'raster', source:'s' + j,
      paint:{{'raster-opacity':0, 'raster-fade-duration':0,
              'raster-resampling':'nearest'}}}}, firstSymbol);
  }});
  {f"new maplibregl.Marker({{color:'#FFFFFF', scale:0.5}}).setLngLat([{radar[0]}, {radar[1]}]).addTo(map);" if radar else ""}
  RINGS.forEach((nm, k) => {{
    const pts = []; const R = nm * 1852.0;
    for (let a = 0; a <= 360; a += 3) {{
      const b = a * Math.PI / 180, dn = R * Math.cos(b), de = R * Math.sin(b);
      pts.push([{clon} + de / (111320 * Math.cos({clat} * Math.PI / 180)),
                {clat} + dn / 110540]);
    }}
    map.addSource('ring' + k, {{type:'geojson', data:{{type:'Feature',
      geometry:{{type:'LineString', coordinates:pts}}}}}});
    map.addLayer({{id:'ring' + k, type:'line', source:'ring' + k,
      paint:{{'line-color':'#8A9BB0', 'line-width':1, 'line-dasharray':[3,3]}}}});
  }});
  if (CENTER) {{
    new maplibregl.Marker({{color:'#00E5FF', scale:0.6}})
      .setLngLat([{clon}, {clat}]).addTo(map);
  }}
  ready = true; show(i); play(playing);
}});
pp.onclick = () => play(!playing);
document.getElementById('nx').onclick = () => {{ play(false); show((i + 1) % F.length); }};
document.getElementById('pv').onclick = () => {{ play(false); show((i - 1 + F.length) % F.length); }};
sl.oninput = () => {{ play(false); show(+sl.value); }};
if (F.length) show(i);
</script></body></html>"""
