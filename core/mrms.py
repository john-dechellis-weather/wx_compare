"""MRMS merged reflectivity at native resolution.

The CONUS map previously pulled radar from NOAA's ArcGIS export
service. That had three problems, and all three are why it was
removed rather than fixed in place:

  * it was fetched ON THE RENDER PATH, so a 4.7 MB PNG32 download
    sat between the user and the map;
  * a browser-side failure was INVISIBLE — the layer existed, the
    deck rendered, nothing appeared, and there was nothing to read;
  * 4880 px across CONUS is 1.1 km per pixel against a 1 km source,
    so it was already throwing detail away, and asking for more
    pixels made the fetch slower still.

This goes to the source instead: the MRMS GRIB2 on
mrms.ncep.noaa.gov, decoded and coloured here at the grid's own
0.01 degree spacing. The result is HIGHER resolution and roughly
35x SMALLER than the export, because we control the encoding — a
discrete 15-colour palette over a mostly-transparent field is
something WebP compresses extremely well and a general-purpose
PNG32 cannot.

Measured on a full CONUS frame: 7000 x 3500 (24.5M cells), 1.1 s to
colourise, 4.2 s including the WebP encode, 131 KB out, 808 MB peak.

TWO DESIGN RULES, both learned the hard way on this page:

NO MATPLOTLIB. Colouring a grid is an array lookup — np.digitize for
the band index, then index an RGBA table. A 7000 x 3500 matplotlib
figure would need a 70-inch canvas and hundreds of megabytes to do
the same job an order of magnitude slower.

NEVER ON THE RENDER PATH. A background loop keeps the newest frame on
disk; the page reads whatever is there and never waits. Putting a
fetch and decode inside a page render is what made the CONUS map take
a minute.
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

# The MRMS CONUS grid: 0.01 deg, 3500 x 7000. These are the grid
# EDGES, which is what a BitmapLayer wants — cell centres would
# offset the image by half a cell.
BOUNDS = [-130.0, 20.0, -60.0, 55.0]

# Same AWIPS ramp as the CAM overlays, so a forecast frame and an
# observation frame read identically.
LEVELS = list(range(5, 80, 5))
COLORS = [
    (0x04, 0xE9, 0xE7), (0x01, 0x9F, 0xF4), (0x03, 0x00, 0xF4),
    (0x02, 0xFD, 0x02), (0x01, 0xC5, 0x01), (0x00, 0x8E, 0x00),
    (0xFD, 0xF8, 0x02), (0xE5, 0xBC, 0x00), (0xFD, 0x95, 0x00),
    (0xFD, 0x00, 0x00), (0xD4, 0x00, 0x00), (0xBC, 0x00, 0x00),
    (0xF8, 0x00, 0xFD), (0x98, 0x54, 0xC6), (0xFD, 0xFD, 0xFD),
]
DBZ_MIN = float(os.environ.get("MRMS_DBZ_MIN", "5"))
# 1 keeps the native 0.01 deg grid. 2 halves it to ~2 km — still
# finer than the ArcGIS export ever was, at a quarter the pixels.
DECIMATE = int(os.environ.get("MRMS_DECIMATE", "1"))
WEBP_Q = int(os.environ.get("MRMS_WEBP_Q", "80"))
# Peak RSS is ~800 MB while a full-resolution frame is in flight.
# Skip a pass rather than push the box over: a stale radar frame is
# recoverable, an OOM restart takes the whole site down.
MEM_CEILING_MB = float(os.environ.get("MRMS_MEM_CEILING_MB", "2400"))


def _rss_mb() -> float:
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0


def latest_file(timeout: int = 15):
    """(filename, stamp) for the newest GRIB2, or None.

    The directory listing is plain HTML, so names are pulled out by
    pattern. The format has been stable for years and the regex
    fails loudly rather than silently if it changes.
    """
    import requests

    r = requests.get(f"{BASE}/", timeout=timeout,
                     headers={"User-Agent": "bluemet.org"})
    r.raise_for_status()
    stamps = re.findall(rf"{PRODUCT}_(\d{{8}}-\d{{6}})\.grib2\.gz",
                        r.text)
    if not stamps:
        return None
    stamp = max(stamps)
    return f"{PRODUCT}_{stamp}.grib2.gz", stamp


def decode(raw_gz: bytes):
    """GRIB2 bytes -> dBZ array. NaN where there is no data."""
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
    # MRMS uses -999 for no coverage and -99 for no echo. Both mean
    # "draw nothing" — leaving them in paints the whole continent
    # the bottom colour.
    return np.where(vals < -90, np.nan, vals)


def palette():
    """RGBA lookup table; index 0 is transparent (no echo)."""
    import numpy as np

    lut = np.zeros((len(LEVELS) + 1, 4), dtype="uint8")
    for i, (r, g, b) in enumerate(COLORS):
        lut[i + 1] = (r, g, b, 255)
    return lut


SMOOTH = float(os.environ.get("MRMS_SMOOTH", "0.8"))


def band_index(vals):
    """dBZ grid -> palette INDEX array (uint8).

    Kept separate from the RGBA expansion because the tiler wants
    indices: gathering one byte per pixel instead of four makes the
    per-tile crop four times cheaper, and the expansion happens on
    the 256x256 tile rather than on 24.5M source cells.
    """
    import numpy as np

    if DECIMATE > 1:
        vals = vals[::DECIMATE, ::DECIMATE]
    # Light blur BEFORE quantising. One CONUS raster is scaled by
    # the browser, so at zoom past the data's own 1 km resolution
    # the cells would otherwise show as hard squares. Smoothing the
    # FIELD rather than the image keeps the colour bands honest —
    # blurring after quantising would invent intermediate colours
    # that correspond to no dBZ value.
    #
    # The blur is coverage-aware: masked cells contribute nothing
    # instead of dragging edges toward zero, which would eat away
    # the rim of every echo.
    if SMOOTH:
        from scipy import ndimage as ndi

        ok = np.isfinite(vals) & (vals >= DBZ_MIN)
        num = ndi.gaussian_filter(
            np.where(ok, vals, 0.0).astype("float32"), SMOOTH)
        den = ndi.gaussian_filter(ok.astype("float32"), SMOOTH)
        with np.errstate(invalid="ignore", divide="ignore"):
            vals = np.where(den > 0.05, num / np.maximum(den, 1e-6),
                            np.nan)
    idx = np.digitize(np.nan_to_num(vals, nan=-999.0),
                      LEVELS).astype("uint8")
    idx[~np.isfinite(vals)] = 0
    idx[vals < DBZ_MIN] = 0
    # MRMS rows run north to south and image rows run top to bottom,
    # so they already agree. Flipping would put Florida in Canada.
    return idx


def colorize(vals):
    """dBZ grid -> RGBA array."""
    return palette()[band_index(vals)]


# CONUS is 7000 px wide, and WebGL's MAX_TEXTURE_SIZE is 4096 on a
# lot of integrated graphics. Over that limit the texture silently
# fails to upload and the layer draws NOTHING — the map looks fine,
# the caption says the frame loaded, and there is no error anywhere.
#
# Splitting into a grid keeps every piece well under the cap while
# preserving full resolution. Each piece becomes its own BitmapLayer
# with its own bounds, which pydeck can express — unlike a TileLayer,
# which needs a JS callback it cannot serialise.
CHUNKS_X = int(os.environ.get("MRMS_CHUNKS_X", "2"))
CHUNKS_Y = int(os.environ.get("MRMS_CHUNKS_Y", "2"))


def render(vals, dest: Path) -> Path:
    """Whole-CONUS single image. Kept for non-map uses."""
    from PIL import Image

    im = Image.fromarray(colorize(vals), mode="RGBA")
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "WEBP", quality=WEBP_Q, method=4)
    return dest


def render_chunks(vals, outdir, stamp: str) -> list:
    """Grid of tiles. Returns [{name, bounds}, ...].

    Bounds are computed from the SLICE, not assumed, so an odd
    division that leaves a remainder row or column still lands in
    the right place on the map.
    """
    from PIL import Image

    rgba = colorize(vals)
    rows, cols = rgba.shape[0], rgba.shape[1]
    w, s, e, n = BOUNDS
    out = []
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for iy in range(CHUNKS_Y):
        y0 = rows * iy // CHUNKS_Y
        y1 = rows * (iy + 1) // CHUNKS_Y
        for ix in range(CHUNKS_X):
            x0 = cols * ix // CHUNKS_X
            x1 = cols * (ix + 1) // CHUNKS_X
            sub = rgba[y0:y1, x0:x1]
            if not sub[..., 3].any():
                continue          # nothing to draw in this piece
            name = f"mrmsc_{stamp}_{ix}{iy}.webp"
            Image.fromarray(sub, mode="RGBA").save(
                outdir / name, "WEBP", quality=WEBP_Q, method=4)
            # Image rows run north to south, so y0 is the NORTH edge.
            out.append({
                "name": name,
                "bounds": [w + (e - w) * x0 / cols,
                           n - (n - s) * y1 / rows,
                           w + (e - w) * x1 / cols,
                           n - (n - s) * y0 / rows],
            })
    return out


def frame_name(stamp: str) -> str:
    return f"mrms_{stamp}.webp"


def build(outdir) -> tuple:
    """Fetch, decode and render the newest scan. (name, note)."""
    import requests

    got = latest_file()
    if not got:
        return None, "no files in the MRMS listing"
    fname, stamp = got
    name = frame_name(stamp)
    dest = Path(outdir) / name
    if dest.exists():
        return name, "cached"
    r = requests.get(f"{BASE}/{fname}", timeout=90,
                     headers={"User-Agent": "bluemet.org"})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} fetching {fname}"
    t0 = time.time()
    vals = decode(r.content)
    ny, nx = vals.shape
    chunks = render_chunks(vals, outdir, stamp)
    import json as _json

    (Path(outdir) / f"mrmsc_{stamp}.json").write_text(
        _json.dumps(chunks))
    _cb = sum((Path(outdir) / c["name"]).stat().st_size
              for c in chunks)
    note = (f"{nx}x{ny} native, {len(chunks)} chunks, "
            f"{_cb / 1024:.0f} KB")

    # Tile pyramid. This is what makes zooming hold its resolution:
    # a single raster is correct at one zoom and stretched at every
    # other. Empty tiles are skipped, and since ~94% of CONUS has no
    # echo at any moment, a typical scan writes a few hundred tiles
    # rather than the 2,400 the grid would suggest.
    # Default OFF: the pyramid cannot be served through pydeck
    # (TileLayer needs a JS callback), so writing thousands of
    # tiles every scan buys nothing until a custom deck.gl
    # component exists. MRMS_TILES=on re-enables it.
    if os.environ.get("MRMS_TILES", "off").lower() == "on":
        try:
            from core import mrms_tiles as _T

            tdir = Path(outdir) / "mrmstiles"
            st = _T.build_pyramid(band_index(vals), palette(),
                                  tdir, stamp)
            _T.mark_done(tdir, stamp)
            _T.prune(tdir, keep=int(os.environ.get("MRMS_TILE_KEEP", "2")))
            note += (f", {st['written']} tiles "
                     f"({st['bytes'] / 1e6:.1f} MB)")
        except Exception as exc:
            note += f", TILES FAILED: {type(exc).__name__}: {exc}"
    del vals
    return name, note + f", {time.time() - t0:.0f}s"


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
    # Staggered start: the CONUS map is the priority page and this
    # must not compete with its first load after a restart.
    time.sleep(float(os.environ.get("MRMS_DELAY_S", "45")))
    _log(outdir, f"MRMS warmer started (RSS {_rss_mb():.0f} MB)")
    while True:
        try:
            if _rss_mb() > MEM_CEILING_MB:
                _log(outdir, f"SKIPPED, RSS {_rss_mb():.0f} MB over "
                             f"{MEM_CEILING_MB:.0f} MB")
            else:
                name, note = build(outdir)
                if name:
                    if note != "cached":
                        _log(outdir, f"{name}: {note}")
                    for pat, k in (("mrms_*.webp", KEEP),
                                   ("mrmsc_*.json", KEEP)):
                        for old in sorted(Path(outdir).glob(pat))[:-k]:
                            st_ = re.search(r"(\d{8}-\d{6})", old.name)
                            if st_:
                                for c in Path(outdir).glob(
                                        f"mrmsc_{st_.group(1)}_*.webp"):
                                    try:
                                        c.unlink()
                                    except OSError:
                                        pass
                            try:
                                old.unlink()
                            except OSError:
                                pass
                    olds = []
                    for old in olds[:-KEEP] if olds else []:
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
    """Idempotent. MRMS_WARMER=off disables without a deploy."""
    if os.environ.get("MRMS_WARMER", "on").lower() == "off":
        return
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_daemon, args=(outdir,), daemon=True,
                         name="mrms-warmer").start()
        _started = True


def newest(outdir):
    """(chunks, stamp) for the newest scan, or (None, None).

    `chunks` is [{name, bounds}, ...] — one BitmapLayer each.
    """
    import json as _json

    hits = sorted(Path(outdir).glob("mrmsc_*.json"))
    if not hits:
        return None, None
    m = re.search(r"mrmsc_(\d{8}-\d{6})\.json", hits[-1].name)
    try:
        return _json.loads(hits[-1].read_text()), (m.group(1)
                                                   if m else None)
    except Exception:
        return None, None
