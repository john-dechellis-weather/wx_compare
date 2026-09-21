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
}
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
def latest_key(site: str, product: str = PRODUCT, now=None):
    """Newest object key for site/product, or None. Keys look like
    OKX_N0B_2026_09_21_16_18_52 and sort by time. Lists today and,
    just after 00Z, yesterday too."""
    import requests

    now = now or datetime.now(timezone.utc)
    days = [now]
    if now.hour == 0 and now.minute < 20:
        days.insert(0, datetime.fromtimestamp(now.timestamp() - 86400,
                                              timezone.utc))
    best = None
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
            keys = re.findall(r"<Key>([^<]+)</Key>", r.text)
            if keys:
                best = max(keys + ([best] if best else []))
            m = re.search(r"<NextContinuationToken>([^<]+)<", r.text)
            if not m:
                break
            token = m.group(1)
    return best


def key_stamp(key: str) -> str:
    """OKX_N0B_2026_09_21_16_18_52 -> 20260921-161852."""
    p = key.split("_")
    return f"{p[2]}{p[3]}{p[4]}-{p[5]}{p[6]}{p[7]}"


def fetch(key: str) -> bytes:
    import requests

    r = requests.get(f"{BUCKET}/{key}", timeout=20, headers=_HDRS)
    r.raise_for_status()
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


def build(domain: str, outdir, wait: bool = True) -> tuple:
    """Fetch, decode and render the newest scan. (stamp, note).

    One build per domain at a time: the page can build on demand
    while the warmer is running, and whichever arrives second waits
    for the first (or, with wait=False, returns at once) and then
    finds the result cached."""
    lk = _lock_for(domain)
    if not lk.acquire(blocking=wait):
        return None, "busy"
    try:
        stamp, note = _build(domain, outdir)
        STATUS[domain] = {"ok": stamp is not None, "note": note,
                          "at": time.time()}
        return stamp, note
    except Exception as exc:
        STATUS[domain] = {"ok": False, "at": time.time(),
                          "note": f"{type(exc).__name__}: {exc}"}
        raise
    finally:
        lk.release()


def _build(domain: str, outdir) -> tuple:
    from PIL import Image

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    site = DOMAINS[domain][0]
    t0 = time.time()
    key = latest_key(site)
    if not key:
        return None, f"no {site} {PRODUCT} files listed"
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
            cc_note = f"MRMS-masked ({mstamp})"
    except Exception as exc:
        cc_note = f"no MRMS mask ({type(exc).__name__})"
    if mask is None and CC_FILTER:
        # Same volume, same timestamp in the key.
        try:
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
        "site": f"K{site}", "product": PRODUCT, "stamp": stamp,
        "elev": scan["elev"],
        "filter": ("mrms" if mask is not None
                   else "cc" if cc is not None else "none")}))
    return stamp, (f"{domain} K{site} {stamp}: list {t1 - t0:.2f}s, "
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


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
SLEEP_S = int(os.environ.get("L3_SLEEP_S", "60"))
KEEP = int(os.environ.get("L3_KEEP", "6"))
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
                stamp, note = build(dom, outdir)
                if note != "cached":
                    _log(outdir, note)
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
