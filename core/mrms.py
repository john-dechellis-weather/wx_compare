"""Native-resolution MRMS composite reflectivity.

NOAA's ArcGIS export tops out usefully around 4880 px across CONUS —
1.1 km per pixel against a 1 km source — and returns PNG32, which is
several megabytes per fetch. This goes to the source instead: the
MRMS GRIB2 on mrms.ncep.noaa.gov, decoded and coloured here at the
grid's own 0.01 degree spacing.

The result is HIGHER resolution and SMALLER than what the export
gives, because we control the encoding: a discrete 15-colour AWIPS
palette compresses far better as WebP than a PNG32 does.

Two deliberate choices worth stating.

NO MATPLOTLIB. Colouring a grid is an array lookup, not a plot — map
dBZ to palette indices with np.digitize and index an RGBA table. A
7000 x 3500 matplotlib figure would need a 70-inch canvas and a
hundred megabytes of buffer to do the same job an order of magnitude
slower. This matters on a box that also serves the page.

WARMED, NEVER ON THE RENDER PATH. MRMS publishes every ~2 minutes.
A background loop keeps the newest frame on disk; the page reads
whatever is there. Putting a GRIB2 fetch and decode in a page render
is how the CONUS map came to take a minute.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = ("https://mrms.ncep.noaa.gov/data/2D/"
        "MergedReflectivityQCComposite")
PRODUCT = "MRMS_MergedReflectivityQCComposite_00.50"

# The MRMS CONUS grid: 0.01 deg, 3500 x 7000, corners below. These
# are the grid EDGES, which is what a BitmapLayer wants — using cell
# centres would offset the image by half a cell.
BOUNDS = [-130.0, 20.0, -60.0, 55.0]

# Same AWIPS ramp as the radar mosaic and the CAM overlay, so an
# observation and a forecast read identically.
LEVELS = list(range(5, 80, 5))
COLORS = [
    (0x04, 0xE9, 0xE7), (0x01, 0x9F, 0xF4), (0x03, 0x00, 0xF4),
    (0x02, 0xFD, 0x02), (0x01, 0xC5, 0x01), (0x00, 0x8E, 0x00),
    (0xFD, 0xF8, 0x02), (0xE5, 0xBC, 0x00), (0xFD, 0x95, 0x00),
    (0xFD, 0x00, 0x00), (0xD4, 0x00, 0x00), (0xBC, 0x00, 0x00),
    (0xF8, 0x00, 0xFD), (0x98, 0x54, 0xC6), (0xFD, 0xFD, 0xFD),
]
DBZ_MIN = float(os.environ.get("MRMS_DBZ_MIN", "5"))
# 1 keeps the native 0.01 deg grid. 2 halves it to ~2 km, which is
# still finer than the ArcGIS export and a quarter of the pixels —
# worth having as an escape hatch if the box is tight.
DECIMATE = int(os.environ.get("MRMS_DECIMATE", "1"))
WEBP_Q = int(os.environ.get("MRMS_WEBP_Q", "80"))


def latest_file(timeout=15):
    """Newest GRIB2 filename on the server, or None.

    The directory listing is plain HTML, so the filenames are pulled
    out by pattern rather than parsed — the format has been stable
    for years and a regex fails loudly if it changes.
    """
    import requests

    r = requests.get(f"{BASE}/", timeout=timeout,
                     headers={"User-Agent": "bluemet.org"})
    r.raise_for_status()
    names = re.findall(rf"{PRODUCT}_(\d{{8}}-\d{{6}})\.grib2\.gz",
                       r.text)
    if not names:
        return None
    stamp = max(names)
    return f"{PRODUCT}_{stamp}.grib2.gz", stamp


def decode(raw_gz):
    """GRIB2 bytes -> (values, stamp). Values are dBZ, NaN for none."""
    import gzip
    import tempfile

    import numpy as np
    import xarray as xr

    with tempfile.NamedTemporaryFile(suffix=".grib2",
                                     delete=False) as fh:
        fh.write(gzip.decompress(raw_gz))
        path = fh.name
    try:
        ds = xr.open_dataset(path, engine="cfgrib",
                             backend_kwargs={"indexpath": ""})
        var = list(ds.data_vars)[0]
        vals = np.asarray(ds[var].values, dtype="float32")
        ds.close()
    finally:
        for p in (path, path + ".idx"):
            try:
                os.unlink(p)
            except OSError:
                pass
    # MRMS uses -999 for no-coverage and -99 for no-echo; both mean
    # "draw nothing", and leaving them in would paint the whole
    # continent the bottom colour.
    vals = np.where(vals < -90, np.nan, vals)
    return vals


def colorize(vals):
    """dBZ grid -> RGBA array, via a palette lookup.

    np.digitize gives the band index for every cell in one pass; the
    RGBA table is then indexed directly. No plotting library, no
    figure, no dpi — this is the whole render.
    """
    import numpy as np

    if DECIMATE > 1:
        vals = vals[::DECIMATE, ::DECIMATE]
    lut = np.zeros((len(LEVELS) + 1, 4), dtype="uint8")
    for i, (r, g, b) in enumerate(COLORS):
        lut[i + 1] = (r, g, b, 255)
    idx = np.digitize(np.nan_to_num(vals, nan=-999.0),
                      LEVELS).astype("uint8")
    idx[~np.isfinite(vals)] = 0
    idx[vals < DBZ_MIN] = 0          # below the floor = transparent
    rgba = lut[idx]
    # MRMS rows run north to south; image rows run top to bottom, so
    # they already agree. Flipping here would put Florida in Canada.
    return rgba


def render(vals, dest: Path) -> Path:
    from PIL import Image

    rgba = colorize(vals)
    im = Image.fromarray(rgba, mode="RGBA")
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "WEBP", quality=WEBP_Q, method=4)
    return dest


def build(outdir, stamp=None):
    """Fetch, decode and render the newest frame. (name, note)."""
    import requests

    got = latest_file()
    if not got:
        return None, "no files in the MRMS listing"
    fname, stamp = got
    name = f"mrmsnat_{stamp}.webp"
    dest = Path(outdir) / name
    if dest.exists():
        return name, "cached"
    r = requests.get(f"{BASE}/{fname}", timeout=90,
                     headers={"User-Agent": "bluemet.org"})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} fetching {fname}"
    t0 = time.time()
    vals = decode(r.content)
    render(vals, dest)
    kb = dest.stat().st_size / 1024
    ny, nx = vals.shape
    return name, (f"{nx}x{ny} native, {kb:.0f} KB WebP, "
                  f"{time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_started = False
SLEEP_S = int(os.environ.get("MRMS_SLEEP_S", "150"))
KEEP = int(os.environ.get("MRMS_KEEP", "3"))


def _log(outdir, msg):
    try:
        with open(Path(outdir) / "mrms_warmer.log", "a") as fh:
            fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                     f"{msg}\n")
    except OSError:
        pass


def _daemon(outdir):
    time.sleep(float(os.environ.get("MRMS_DELAY_S", "45")))
    _log(outdir, "MRMS warmer started")
    while True:
        try:
            name, note = build(outdir)
            if name:
                if note != "cached":
                    _log(outdir, f"{name}: {note}")
                for old in sorted(Path(outdir).glob("mrmsnat_*.webp"))[:-KEEP]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            else:
                _log(outdir, f"FAILED: {note}")
        except Exception as exc:
            _log(outdir, f"FAILED: {type(exc).__name__}: {exc}")
        time.sleep(SLEEP_S)


def ensure_mrms_warmer(outdir) -> None:
    """Idempotent. MRMS_WARMER=on enables; off by default."""
    if os.environ.get("MRMS_WARMER", "off").lower() != "on":
        return
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_daemon, args=(outdir,), daemon=True,
                         name="mrms-warmer").start()
        _started = True


def newest(outdir):
    """(name, stamp) of the newest rendered frame, or (None, None)."""
    hits = sorted(Path(outdir).glob("mrmsnat_*.webp"))
    if not hits:
        return None, None
    n = hits[-1].name
    m = re.search(r"mrmsnat_(\d{8}-\d{6})", n)
    return n, (m.group(1) if m else None)
