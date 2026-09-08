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

import io
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
# AWIPS reflectivity ramp, with the three lowest bands recoloured
# and a per-band alpha ramp.
#
# THE PROBLEM: the JetBlue icon is #005ADC. Measured colour distance
# from it to the standard AWIPS low end:
#
#     5-10 dBZ  #04E9E7   143
#    10-15 dBZ  #019FF4    73   <- nearly identical to the icon
#    15-20 dBZ  #0300F4    93
#    20-25 dBZ  #02FD02   272   (green: no conflict)
#
# So drizzle and light rain — the least important returns on the
# map — were erasing the most important symbols on it.
#
# THE FIX, two parts. The low bands move to desaturated grey-teal,
# which stays legible as "something is there" without competing with
# a saturated blue aircraft. And ALPHA RISES WITH INTENSITY, so light
# returns are faint and cores are solid. That is the honest
# weighting anyway: a 55 dBZ core should dominate the picture and a
# 12 dBZ return should not.
#
# Everything from 20 dBZ up is the unmodified AWIPS ramp, so the
# scheme still reads the way a forecaster expects.
COLORS = [
    (0x9E, 0xC8, 0xC8),   #  5-10  desaturated grey-teal
    (0x74, 0xB0, 0xBE),   # 10-15
    (0x4E, 0x94, 0xB4),   # 15-20
    (0x02, 0xFD, 0x02),   # 20-25  AWIPS green from here on
    (0x01, 0xC5, 0x01),   # 25-30
    (0x00, 0x8E, 0x00),   # 30-35
    (0xFD, 0xF8, 0x02),   # 35-40
    (0xE5, 0xBC, 0x00),   # 40-45
    (0xFD, 0x95, 0x00),   # 45-50
    (0xFD, 0x00, 0x00),   # 50-55
    (0xD4, 0x00, 0x00),   # 55-60
    (0xBC, 0x00, 0x00),   # 60-65
    (0xF8, 0x00, 0xFD),   # 65-70
    (0x98, 0x54, 0xC6),   # 70-75
    (0xFD, 0xFD, 0xFD),   # 75+
]
# Alpha per band, low to high. Light returns recede; cores read
# solidly. Scaled by MRMS_ALPHA so the existing control still works.
# Runs past 1.0 at the top and is clamped, so a 55 dBZ core is
# nearly solid while drizzle sits near 20%. A flat alpha made
# everything equally translucent, which flattered nothing: light
# rain still hid aircraft and cores looked weak.
ALPHA_RAMP = [0.30, 0.40, 0.50, 0.65, 0.78, 0.90,
              1.00, 1.10, 1.20, 1.32, 1.38, 1.42,
              1.45, 1.45, 1.45]
DBZ_MIN = float(os.environ.get("MRMS_DBZ_MIN", "5"))
# Alpha baked into the PALETTE, not left to the layer's opacity prop.
#
# BitmapLayer opacity was set to 0.5 and the radar still drew fully
# saturated — city labels vanished under it. Rather than keep
# guessing at how deck.gl handles the prop through pydeck's JSON,
# the transparency is put where it cannot be ignored: in the pixels.
# The layer prop stays at 1.0 so the two do not multiply.
# Raised back to 170 now that ALPHA_RAMP recedes the low bands.
# A flat 102 made everything faint; the ramp lets weak echo drop to
# ~22% while a core sits near 67%, which is the distribution that
# actually helps — faint drizzle, solid cores.
ALPHA = int(os.environ.get("MRMS_ALPHA", "170"))
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
        a = int(round(ALPHA * ALPHA_RAMP[min(i, len(ALPHA_RAMP) - 1)]))
        lut[i + 1] = (r, g, b, max(0, min(255, a)))
    return lut


SMOOTH = float(os.environ.get("MRMS_SMOOTH", "0.8"))
# Interpolate the field onto a finer grid BEFORE quantising.
#
# A blur alone softens the edges but the underlying grid is still
# 1 km, so zoomed in you see soft SQUARES instead of sharp ones.
# Upsampling interpolates BETWEEN cells, so the colour bands follow
# a smooth surface and the map holds up to roughly 20x zoom instead
# of 10x.
#
# Bilinear on the FIELD, not on the image: interpolating colours
# after quantising would produce shades that correspond to no dBZ
# value at all.
#
# Measured at 2x: 14000x7000, 200 px/degree, 13 s, ~4.6 MB across 8
# chunks. Set MRMS_UPSAMPLE=1 to disable.
UPSAMPLE = int(os.environ.get("MRMS_UPSAMPLE", "4"))
# Blur applied AFTER upsampling, in fine-grid pixels.
# Measured 8/30 on a synthetic convective field, peak 60 dBZ:
#
#     upsample  blur   peak kept   loss
#         2x    1.6        58      1.8   <- edges still stepped
#         2x    4.0        54      6.0
#         2x    6.0        49     10.7   <- cores erased
#         4x    4.0        58      2.4   <- smooth AND intact
#
# Blur is measured in FINE pixels, so the same sigma erodes less at
# higher upsample. That is the whole reason to raise resolution
# rather than raise the blur: heavy smoothing at 2x flattened a
# 60 dBZ core to 49, which on a radar display is not a cosmetic
# trade — it erases the cell someone needed to see.
FINE_SMOOTH = float(os.environ.get("MRMS_FINE_SMOOTH", "4.0"))


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
    if UPSAMPLE > 1:
        from scipy import ndimage as ndi

        # order=1 is bilinear. Higher orders overshoot at sharp
        # gradients — a spline through a reflectivity edge invents
        # values above the peak, which on a radar display means
        # inventing intensity that was never observed.
        vals = ndi.zoom(vals, UPSAMPLE, order=1)
        # A second, gentle pass on the FINE grid. Interpolation
        # alone still leaves the colour-band boundaries following
        # the coarse cell structure; blurring after the upsample
        # turns those boundaries into curves, which is what removes
        # the last of the square edges at high zoom.
        if FINE_SMOOTH:
            ok2 = np.isfinite(vals)
            num2 = ndi.gaussian_filter(
                np.where(ok2, vals, 0.0).astype("float32"),
                FINE_SMOOTH)
            den2 = ndi.gaussian_filter(ok2.astype("float32"),
                                       FINE_SMOOTH)
            with np.errstate(invalid="ignore", divide="ignore"):
                vals = np.where(den2 > 0.05,
                                num2 / np.maximum(den2, 1e-6), np.nan)
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
# 4x2 at UPSAMPLE=2 gives 3500x3500 pieces — under the 4096 cap
# that a 2x2 split would blow through at 7000x3500.
# 8x4 at UPSAMPLE=4: source slices are 875x875, output 3500x3500 —
# under the 4096 WebGL cap. Fewer chunks would blow through it.
CHUNKS_X = int(os.environ.get("MRMS_CHUNKS_X", "8"))
CHUNKS_Y = int(os.environ.get("MRMS_CHUNKS_Y", "4"))


def render(vals, dest: Path) -> Path:
    """Whole-CONUS single image. Kept for non-map uses."""
    from PIL import Image

    im = Image.fromarray(colorize(vals), mode="RGBA")
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "WEBP", quality=WEBP_Q, method=4)
    return dest


def render_chunks(vals, outdir, stamp: str) -> list:
    """Grid of tiles. Returns [{name, bounds}, ...].

    Upsamples and colourises PER CHUNK, never globally. Doing the
    whole CONUS grid at once peaked at 2.7 GB — above the warmer's
    own memory ceiling, so it would have skipped every pass. One
    chunk at a time holds peak to about a tenth of that.

    Slices carry a small MARGIN of source cells that is trimmed
    after interpolation. Without it, ndi.zoom has no neighbours at a
    slice edge and clamps, leaving a visible seam every time two
    chunks meet.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    lut = palette()
    rows, cols = vals.shape
    w, s_, e, n = BOUNDS
    # Margin in SOURCE cells, sized to cover the blur radius so a
    # chunk edge has real neighbours to smooth against. Too small
    # and every seam shows a faint line.
    margin = int(max(4, FINE_SMOOTH)) if UPSAMPLE > 1 else 0
    out = []
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    for iy in range(CHUNKS_Y):
        y0 = rows * iy // CHUNKS_Y
        y1 = rows * (iy + 1) // CHUNKS_Y
        for ix in range(CHUNKS_X):
            x0 = cols * ix // CHUNKS_X
            x1 = cols * (ix + 1) // CHUNKS_X
            my0, my1 = max(0, y0 - margin), min(rows, y1 + margin)
            mx0, mx1 = max(0, x0 - margin), min(cols, x1 + margin)
            sub = vals[my0:my1, mx0:mx1]
            if not np.isfinite(sub).any():
                continue
            if not (np.nan_to_num(sub, nan=-999.0) >= DBZ_MIN).any():
                continue

            band = band_index(sub)
            if UPSAMPLE > 1:
                # Trim the margin AFTER interpolation, in upsampled
                # pixels.
                t0 = (y0 - my0) * UPSAMPLE
                t1 = band.shape[0] - (my1 - y1) * UPSAMPLE
                l0 = (x0 - mx0) * UPSAMPLE
                l1 = band.shape[1] - (mx1 - x1) * UPSAMPLE
                band = band[t0:t1, l0:l1]
            if not band.any():
                continue

            name = f"mrmsc_{stamp}_{ix}{iy}.webp"
            Image.fromarray(lut[band], mode="RGBA").save(
                outdir / name, "WEBP", quality=WEBP_Q, method=4)
            del band
            # Image rows run north to south, so y0 is the NORTH edge.
            out.append({
                "name": name,
                "bounds": [w + (e - w) * x0 / cols,
                           n - (n - s_) * y1 / rows,
                           w + (e - w) * x1 / cols,
                           n - (n - s_) * y0 / rows],
            })
    return out



# Bump when the rendered output changes in any visible way.
RENDER_STYLE = int(os.environ.get("MRMS_RENDER_STYLE", "8"))


def _style_ok(outdir, stamp: str) -> bool:
    """True if chunks on disk were made by the current style."""
    import json as _json

    try:
        man = _json.loads(
            (Path(outdir) / f"mrmsc_{stamp}.json").read_text())
        return (isinstance(man, dict)
                and man.get("style") == RENDER_STYLE)
    except Exception:
        return False


def frame_name(stamp: str) -> str:
    return f"mrms_{stamp}.webp"


# ---------------------------------------------------------------------------
# Loop frames
# ---------------------------------------------------------------------------
# A SECOND, SMALLER RENDER, written from the same decoded array as the
# full-resolution chunks.
#
# Why not just scrub the chunks? A full frame is ~15 textures at
# 3500x3500. Stepping through 60 of those makes the browser upload
# ~900 large textures, and no amount of extra frames fixes that — the
# bottleneck is weight per frame, not frame count. One small image per
# scan is one texture per scrub step.
#
# LOOP_DECIMATE 2 gives 3500x1750 from the 7000x3500 source: still a
# SINGLE texture, comfortably under the 4096 cap, roughly 90 KB. Sixty
# frames is ~5.5 MB for two hours, which a browser caches without
# complaint.
#
# 2 IS THE FLOOR. Decimate 1 would be 7000 px wide and exceed
# MAX_TEXTURE_SIZE on much integrated graphics — the texture then
# fails to upload silently and the layer draws NOTHING, with no error
# anywhere. That is the same trap that made the full-resolution
# mosaic chunked in the first place.
#
# Raise to 4 (1750x875, ~27 KB) if the loop feels heavy; the live
# frame is full resolution either way, so this only sets how sharp a
# REWOUND frame looks.
LOOP_DECIMATE = int(os.environ.get("MRMS_LOOP_DECIMATE", "1"))
# LOOP FRAMES ARE NATIVE. A 2x upsample here peaked at 1.7 GB per
# frame — measured — and backfill renders six per pass outside the
# heavy lock. On a 2 GB instance that was an out-of-memory kill on
# every pass: no frames written, the service restarting, and every
# other warmer starving on the lock. Native is 0.44 GB and was what
# worked. MRMS_LOOP_UPSAMPLE=2 is still honoured if the instance ever
# grows; do not set it on a 2 GB box.
LOOP_UPSAMPLE = int(os.environ.get("MRMS_LOOP_UPSAMPLE", "1"))
LOOP_SMOOTH = float(os.environ.get("MRMS_LOOP_SMOOTH", "0.6"))
LOOP_Q = int(os.environ.get("MRMS_LOOP_Q", "72"))


def loop_name(stamp: str) -> str:
    return f"mrmsl_{stamp}.webp"


def render_loop_frame(vals, outdir, stamp: str):
    """Native-resolution loop frames, split into halves. Returns a
    list of {"name", "bounds"} or None.

    NO DECIMATION ANY MORE. The 4x-decimated loop frame drew 4 km
    squares at any zoom past ~z7 and read as broken next to the live
    frame. Native is 7000 px wide, over the 4096 texture cap, so the
    frame is written as two 3500-px halves — two textures per scrub
    step instead of one, which is still instant after preload and is
    the same resolution the live frame is built from.

    Still no 4x upsample: that is what makes a live frame ~50 s and
    fifteen chunks. A loop is read for motion; native 1.1 km with a
    light coverage-aware blur is the honest middle.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    d = max(1, LOOP_DECIMATE)
    rows, cols = vals.shape
    ry, rx = rows // d * d, cols // d * d
    v = vals[:ry, :rx]
    if d > 1:
        with np.errstate(invalid="ignore"):
            v = np.nanmax(v.reshape(ry // d, d, rx // d, d), axis=(1, 3))

    good = np.isfinite(v) & (v >= LEVELS[0])
    if not good.any():
        return None
    # UPSAMPLE THE FIELD AND ITS COVERAGE TOGETHER, then divide: the
    # mask is honoured through the interpolation instead of masked
    # cells dragging their neighbours toward zero.
    u = max(1, LOOP_UPSAMPLE)
    f = np.where(good, v, 0.0).astype("float32")
    c = good.astype("float32")
    if u > 1:
        f = ndi.zoom(f, u, order=1)
        c = ndi.zoom(c, u, order=1)
    if LOOP_SMOOTH > 0:
        f = ndi.gaussian_filter(f, LOOP_SMOOTH, mode="nearest")
        c = ndi.gaussian_filter(c, LOOP_SMOOTH, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        v = np.where(c > 0.05, f / np.maximum(c, 1e-6), np.nan)
    good = np.isfinite(v) & (v >= LEVELS[0])
    idx = np.zeros(v.shape, dtype="uint8")
    idx[good] = np.clip(np.digitize(v[good], LEVELS), 1,
                        len(LEVELS)).astype("uint8")
    if not idx.any():
        return None

    lut = palette()
    w, s_, e, n = BOUNDS
    h, wdt = idx.shape
    # Split into a GRID so every tile stays under the 4096 texture
    # cap in BOTH dimensions — at 2x upsample the frame is 14000 x
    # 7000, which is 4 x 2 tiles of 3500. Empty tiles are skipped.
    nx = max(1, -(-wdt // 3500))
    ny = max(1, -(-h // 3500))
    out = []
    for j in range(ny):
        y0, y1 = h * j // ny, h * (j + 1) // ny
        for k in range(nx):
            x0, x1 = wdt * k // nx, wdt * (k + 1) // nx
            sub = idx[y0:y1, x0:x1]
            if not sub.any():
                continue
            name = f"mrmsl_{stamp}_{j}{k}.webp"
            buf = io.BytesIO()
            Image.fromarray(lut[sub], mode="RGBA").save(
                buf, "WEBP", quality=LOOP_Q, method=0)
            (Path(outdir) / name).write_bytes(buf.getvalue())
            # Rows run north to south: row 0 is the top of the grid.
            out.append({"name": name,
                        "bounds": [w + (e - w) * x0 / wdt,
                                   n - (n - s_) * y1 / h,
                                   w + (e - w) * x1 / wdt,
                                   n - (n - s_) * y0 / h]})
    return out or None


def recent_files(n: int = 80, timeout: int = 15):
    """The newest `n` GRIB2 files, oldest first.

    Same listing `latest_file` reads; MRMS keeps well over two hours
    of scans in it, which is what makes backfilling possible instead
    of waiting two hours for the loop to fill itself.
    """
    import requests

    r = requests.get(f"{BASE}/", timeout=timeout,
                     headers={"User-Agent": "bluemet.org"})
    r.raise_for_status()
    stamps = sorted(set(re.findall(
        rf"{PRODUCT}_(\d{{8}}-\d{{6}})\.grib2\.gz", r.text)))
    return [(f"{PRODUCT}_{s}.grib2.gz", s) for s in stamps[-n:]]


def build_past(outdir, fname: str, stamp: str) -> tuple:
    """Render a FULL chunk set for one past scan, so the scrubber
    draws it at the same resolution as the live frame.

    A manifest that already has chunks means done. One that exists
    with no chunks — a loop-only manifest from the old backfill — is
    rebuilt, so the loop upgrades itself after a deploy without
    waiting for those scans to age out.
    """
    import json as _json

    import requests

    man_p = Path(outdir) / f"mrmsc_{stamp}.json"
    if man_p.exists():
        try:
            _m = _json.loads(man_p.read_text())
            if isinstance(_m, dict) and _m.get("chunks") \
                    and _m.get("style") == RENDER_STYLE:
                return None, "exists"
            if isinstance(_m, dict) and _m.get("no_echo"):
                return None, "exists"
        except Exception:
            pass
    r = requests.get(f"{BASE}/{fname}", timeout=90,
                     headers={"User-Agent": "bluemet.org"})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    vals = decode(r.content)
    try:
        chunks = render_chunks(vals, outdir, stamp)
        loop = render_loop_frame(vals, outdir, stamp) if LOOP_FRAMES else None
    finally:
        del vals
    if not chunks:
        # No echo anywhere. Still write the manifest so the backfill
        # does not retry this scan on every pass.
        man_p.write_text(_json.dumps(
            {"style": RENDER_STYLE, "chunks": [], "loop": None,
             "no_echo": True}))
        return None, "no echo"
    man_p.write_text(_json.dumps(
        {"style": RENDER_STYLE, "chunks": chunks, "loop": loop}))
    return chunks[0]["name"], "ok"


def backfill(outdir, hours: float = 2.0, budget: int = 2):
    """Fill in missing loop frames, newest gaps first.

    `budget` caps how many scans one call will render. The warmer
    calls this every pass, so the loop fills in over several minutes
    instead of blocking the live frame behind a 30 minute catch-up —
    the current scan must never wait on history.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        files = recent_files()
    except Exception as exc:
        return 0, f"listing failed: {type(exc).__name__}"

    # EVERY BACKFILL_STRIDE-th SCAN. At stride 2 the history is
    # four-minute frames, sixteen to the hour instead of thirty-two,
    # and a restart fills the hour in half the passes. The live
    # warmer still adds every scan at its own pace, so the loop is
    # dense at the recent end and sparser further back — the right
    # way round. The stride is anchored to the scan minute, not the
    # listing order, so two restarts pick the same scans.
    todo = []
    for fname, stamp in files:
        try:
            when = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            continue
        if BACKFILL_STRIDE > 1 and (when.minute // 2) % BACKFILL_STRIDE:
            continue
        if not (Path(outdir) / f"mrmsc_{stamp}.json").exists():
            todo.append((fname, stamp))
    if not todo:
        return 0, ""

    # Newest gaps first: the frames nearest now are the ones someone
    # scrubbing is most likely to reach for.
    # FAILURES ARE COUNTED AND NAMED. A bare "except: continue" here
    # meant a backfill where every scan errored returned zero frames
    # and logged nothing, which is indistinguishable from a backfill
    # with nothing to do.
    done, fails, first_err = 0, 0, ""
    for fname, stamp in reversed(todo[-budget * 4:]):
        if done >= budget:
            break
        try:
            # UNDER THE HEAVY LOCK. Each of these is a full GRIB decode
            # and a loop render — the same memory as a live build —
            # and they ran unserialised, overlapping the live build,
            # echo tops and the Level II child. Serialised, the peak
            # is one build at a time, which is what the box can hold.
            with _heavy_lock():
                _name, note = build_past(outdir, fname, stamp)
            if note in ("ok", "no echo"):
                done += 1
            elif note != "exists":
                fails += 1
                first_err = first_err or f"{stamp}: {note}"
        except Exception as exc:
            fails += 1
            first_err = first_err or (f"{stamp}: "
                                      f"{type(exc).__name__}: {exc}")
    note = f"{len(todo) - done} still missing"
    if fails:
        note += f"; {fails} failed, first {first_err}"[:180]
    return done, note


def build(outdir) -> tuple:
    """Fetch, decode and render the newest scan. (name, note).

    Writes CHUNKS for the map and a single whole-CONUS frame for any
    non-map use. The chunk manifest is written last, so `newest`
    cannot return a set that is still being produced.
    """
    import json as _json

    import requests

    got = latest_file()
    if not got:
        return None, "no files in the MRMS listing"
    fname, stamp = got
    name = frame_name(stamp)
    dest = Path(outdir) / name
    # STYLE-KEYED cache check. Without the style in the key, chunks
    # rendered by an older palette or resolution are served forever
    # for a stamp already on disk — which is why blocky, fully
    # opaque frames survived the switch to upsampling and baked
    # alpha. Bump RENDER_STYLE whenever the output changes.
    # A loop-only manifest from backfill is NOT a finished scan. If
    # this check accepted one, the newest scan would keep its 27 KB
    # loop frame and never render chunks, and the live view would sit
    # at loop resolution forever.
    _man_p = Path(outdir) / f"mrmsc_{stamp}.json"
    if _man_p.exists() and _style_ok(outdir, stamp):
        try:
            _prev = _json.loads(_man_p.read_text())
        except Exception:
            _prev = {}
        if isinstance(_prev, dict) and _prev.get("chunks"):
            return name, "cached"
    r = requests.get(f"{BASE}/{fname}", timeout=90,
                     headers={"User-Agent": "bluemet.org"})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} fetching {fname}"
    t0 = time.time()
    vals = decode(r.content)
    ny, nx = vals.shape
    chunks = render_chunks(vals, outdir, stamp)
    # Same decoded array, second render. Doing it here rather than in
    # its own pass avoids a second fetch and a second GRIB decode,
    # which is the expensive half of a scan.
    try:
        loop = render_loop_frame(vals, outdir, stamp) if LOOP_FRAMES else None
    except Exception:
        loop = None          # a loop frame is never worth a failed scan
    del vals
    if not chunks:
        return name, f"{nx}x{ny}, no echo anywhere"
    _cb = sum((Path(outdir) / c["name"]).stat().st_size
              for c in chunks)
    # Manifest LAST: `newest` reads it, so writing it earlier could
    # hand the page a set of chunks still being written.
    (Path(outdir) / f"mrmsc_{stamp}.json").write_text(
        _json.dumps({"style": RENDER_STYLE, "chunks": chunks,
                     "loop": loop}))
    _lb = (sum((Path(outdir) / c["name"]).stat().st_size
               for c in loop) / 1024.0 if loop else 0)
    return name, (f"{nx * UPSAMPLE}x{ny * UPSAMPLE}, "
                  f"{len(chunks)} chunks, {_cb / 1024:.0f} KB"
                  + (f", loop {_lb:.0f} KB" if loop else ", no loop")
                  + f", {time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_started = False
# MRMS publishes every 2 minutes, so 60 frames in two hours is the
# physical ceiling and needs a pass every ~120 s. A scan takes ~50 s
# on a small instance, so sleeping 70 gives a ~120 s cadence — but
# that is roughly 40% duty rather than 25%. Raise this if the box
# struggles; the loop just gets coarser.
SLEEP_S = int(os.environ.get("MRMS_SLEEP_S", "70"))
# TWO-TIER RETENTION.
#
# Full-resolution chunks are only ever drawn for the LIVE frame, so
# keeping 60 scans of them would be ~900 files and ~20 MB for no
# benefit. Loop frames are ~27 KB each and are what the scrubber
# actually draws, so those are kept deep and the chunks shallow.
#
# KEEP_LOOP IS WHAT BOUNDS THE SCRUBBER. history(hours=2) cannot
# return frames the warmer has already deleted, so this number, not
# the hours argument, decides how far back the loop reaches.
# How far back backfill reaches. Matches the page's LOOP_HOURS; the
# scrubber cannot show more than this regardless of KEEP_LOOP.
# Backfill every other scan (see backfill) for an hour.
BACKFILL_STRIDE = int(os.environ.get("MRMS_BACKFILL_STRIDE", "2"))
# Backfill an hour of full chunk sets: matches KEEP at the ~2 min cadence.
LOOP_HOURS_BF = float(os.environ.get("MRMS_LOOP_HOURS", "1.1"))
# Echo-top tags are rebuilt every ECHO_EVERY radar passes (~2 min
# each). 2 is every four minutes, which is the product's own cadence.
ECHO_EVERY = int(os.environ.get("MRMS_ECHO_EVERY", "2"))
# FULL RESOLUTION FOR THE WHOLE LOOP. A chunk set is ~300 KB and one
# build; keeping an hour of them (32 scans at ~2 min) costs ~10-40 MB
# of disk and nothing at draw time. Scrubbed frames used to fall back
# to a single native-resolution loop image, four times coarser than
# the live frame — that is what looked blocky when rewound.
KEEP = int(os.environ.get("MRMS_KEEP", "32"))
KEEP_LOOP = int(os.environ.get("MRMS_KEEP_LOOP", "65"))
# Loop frames are off: every frame the scrubber shows is a chunk set.
# MRMS_LOOP_FRAMES=1 turns the old lightweight frames back on.
LOOP_FRAMES = os.environ.get("MRMS_LOOP_FRAMES", "0") == "1"


def _log(outdir, msg):
    try:
        with open(Path(outdir) / "mrms_warmer.log", "a") as fh:
            fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                     f"{msg}\n")
    except OSError:
        pass


_LOCAL_LOCK = threading.Lock()


def _heavy_lock():
    """The process-wide heavy-build lock if core/warmlock.py exists,
    else a no-op context — MRMS must not hard-depend on that file.
    Named, so a diagnostics page can say "MRMS has held it 40 s"."""
    try:
        from core.warmlock import holding
        return holding("MRMS")
    except Exception:
        import contextlib
        return contextlib.nullcontext(True)


def _daemon(outdir):
    # FIRST LINE BEFORE THE DELAY. The page's "log does not exist"
    # diagnostic means "the thread never ran"; with the first write
    # here, that is exactly and only what a missing file means.
    _log(outdir, f"MRMS warmer thread up, waiting "
                 f"{os.environ.get('MRMS_DELAY_S', '45')}s before the first build")
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
                _log(outdir, f"build starting (RSS {_rss_mb():.0f} MB)")
                with _heavy_lock():
                    name, note = build(outdir)
                # ECHO TOPS ride the same pass, every ECHO_EVERY passes,
                # under the same lock: a second full-grid decode, so it
                # is not free, but it is a list of points and no
                # chunks. FL tags above ECHO_TAG_MIN_FL only.
                _pass_n = globals().get("_ECHO_N", 0) + 1
                globals()["_ECHO_N"] = _pass_n
                if _pass_n % ECHO_EVERY == 1 or ECHO_EVERY == 1:
                    try:
                        from core import echotops as _ET

                        with _heavy_lock():
                            _en = _ET.build(outdir, decode, BOUNDS)
                        if _en != "cached":
                            _log(outdir, _en)
                    except Exception as _eexc:
                        _log(outdir, f"echo tops: {type(_eexc).__name__}: {_eexc}")
                if name:
                    if note != "cached":
                        _log(outdir, f"{name}: {note}")
                    # Chunks first, shallow. A stamp losing its
                    # chunks keeps its manifest and loop frame, so
                    # the scrubber can still draw it.
                    _mans = sorted(Path(outdir).glob("mrmsc_*.json"))
                    for old in _mans[:-KEEP]:
                        st_ = re.search(r"(\d{8}-\d{6})", old.name)
                        if not st_:
                            continue
                        for c in Path(outdir).glob(
                                f"mrmsc_{st_.group(1)}_*.webp"):
                            try:
                                c.unlink()
                            except OSError:
                                pass
                    # Then the deep tier: manifest, loop frame and
                    # the standalone whole-CONUS frame together, so a
                    # manifest never outlives everything it points at.
                    for old in _mans[:-KEEP_LOOP]:
                        st_ = re.search(r"(\d{8}-\d{6})", old.name)
                        if st_:
                            for c in Path(outdir).glob(
                                    f"mrmsl_{st_.group(1)}*.webp"):
                                try:
                                    c.unlink()
                                except OSError:
                                    pass
                        try:
                            old.unlink()
                        except OSError:
                            pass
                    for old in sorted(
                            Path(outdir).glob("mrms_*.webp"))[:-KEEP]:
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
                # HISTORY AFTER THE LIVE FRAME, always. Backfilling
                # first would leave the current scan waiting behind a
                # catch-up that can take half an hour on a cold disk.
                try:
                    _n, _bnote = backfill(outdir, hours=LOOP_HOURS_BF)
                    # Logged even when nothing was built: "0 frames,
                    # 12 still missing, 12 failed" is the line that
                    # tells you backfill is broken rather than idle.
                    if _n or _bnote:
                        _log(outdir,
                             f"backfill +{_n} frame(s); {_bnote}")
                except Exception as _bexc:
                    _log(outdir, f"backfill failed: "
                                 f"{type(_bexc).__name__}: {_bexc}")
        except Exception as exc:
            _log(outdir, f"FAILED: {type(exc).__name__}: {exc}")
        time.sleep(SLEEP_S)


def ensure_mrms_warmer(outdir) -> str:
    """Idempotent. MRMS_WARMER=off disables without a deploy.

    RETURNS WHAT IT DID, so the caller can report honestly. The old
    None return let Homepage print "warmer started" while the kill
    switch had silently returned here — and that is exactly how a
    MRMS_WARMER=off left over from an unrelated debugging session
    read as "MRMS is broken" for days.
    """
    if os.environ.get("MRMS_WARMER", "on").lower() == "off":
        return "DISABLED by MRMS_WARMER=off in the environment"
    global _started
    with _lock:
        if _started:
            return "already running"
        threading.Thread(target=_daemon, args=(outdir,), daemon=True,
                         name="mrms-warmer").start()
        _started = True
        return "started"


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
        man = _json.loads(hits[-1].read_text())
        # Old manifests were a bare list; new ones are a dict with a
        # style. Accept both so a deploy does not blank the radar.
        chunks = man.get("chunks") if isinstance(man, dict) else man
        if isinstance(man, dict) and man.get("style") != RENDER_STYLE:
            return None, None      # stale style: warmer will replace
        return chunks, (m.group(1) if m else None)
    except Exception:
        return None, None


def history(outdir, hours: float = 2.0, limit: int = 60):
    """Scans available for a loop, oldest first.

    Returns [(stamp, chunks, loop), ...] where `loop` is the small
    single-image frame for scrubbing, or None for scans rendered
    before loop frames existed. Built from the manifests already
    on disk — nothing is re-rendered, and a stamp with a stale style
    is dropped rather than mixed in, so a palette change cannot show
    two different colour schemes in one loop.

    KEEP MUST BE LARGE ENOUGH. The warmer prunes to MRMS_KEEP scans;
    at roughly 200 s a pass that is ~36 scans for two hours. Leave
    KEEP at its default of 3 and this returns 3 frames no matter what
    `hours` says — the retention is the real limit, not this argument.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    root = Path(outdir)
    if not root.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for p in sorted(root.glob("mrmsc_*.json")):
        m = re.search(r"mrmsc_(\d{8}-\d{6})\.json", p.name)
        if not m:
            continue
        stamp = m.group(1)
        try:
            when = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            continue
        try:
            man = _json.loads(p.read_text())
        except Exception:
            continue
        if isinstance(man, dict):
            if man.get("style") != RENDER_STYLE:
                continue
            chunks = man.get("chunks")
        else:
            chunks = man
        # Backfilled frames carry a loop image and no chunks. Only
        # a manifest with neither is useless.
        if not chunks and not (isinstance(man, dict)
                               and man.get("loop")):
            continue
        chunks = chunks or []
        # A manifest is written after its chunks, but a scan pruned
        # mid-read can leave a manifest pointing at files that are
        # gone. Serving a frame with missing chunks draws a hole in
        # the mosaic that looks like clear air.
        loop = man.get("loop") if isinstance(man, dict) else None
        if isinstance(loop, dict):          # pre-split manifests
            loop = [loop]
        if loop and not all((root / c["name"]).exists() for c in loop):
            loop = None
        # Chunks are pruned long before loop frames, so most frames in
        # the window have only a loop image. Either is enough to draw.
        if not all((root / c["name"]).exists() for c in chunks):
            chunks = None
        if chunks or loop:
            out.append((stamp, chunks, loop))
    return out[-limit:]
