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

# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
# Each entry is one MRMS 2D field: where it lives, how to scale it,
# and how to colour it. "REFL" is the merged reflectivity composite
# this module was built around; "ETOP" is 18 dBZ echo tops. Both are
# warmed on the same cadence and served through the same chunk and
# manifest scheme, distinguished by the product key in every
# filename.
_AWIPS = [
    (0x9E, 0xC8, 0xC8), (0x74, 0xB0, 0xBE), (0x4E, 0x94, 0xB4),
    (0x02, 0xFD, 0x02), (0x01, 0xC5, 0x01), (0x00, 0x8E, 0x00),
    (0xFD, 0xF8, 0x02), (0xE5, 0xBC, 0x00), (0xFD, 0x95, 0x00),
    (0xFD, 0x00, 0x00), (0xD4, 0x00, 0x00), (0xBC, 0x00, 0x00),
    (0xF8, 0x00, 0xFD), (0x98, 0x54, 0xC6), (0xFD, 0xFD, 0xFD),
]
# Echo tops: cool to hot with height, 10 kft steps to 60.
_ETOP = [
    (0x9E, 0xC8, 0xC8), (0x4E, 0x94, 0xB4), (0x02, 0xFD, 0x02),
    (0x00, 0x8E, 0x00), (0xFD, 0xF8, 0x02), (0xFD, 0x95, 0x00),
    (0xFD, 0x00, 0x00), (0xBC, 0x00, 0x00), (0xF8, 0x00, 0xFD),
    (0x98, 0x54, 0xC6), (0xFD, 0xFD, 0xFD),
]
PRODUCTS = {
    "REFL": {
        "label": "Reflectivity",
        "base": "https://mrms.ncep.noaa.gov/data/2D/"
                "MergedReflectivityQCComposite",
        "file": "MRMS_MergedReflectivityQCComposite_00.50",
        "levels": list(range(5, 80, 5)),      # dBZ
        "colors": _AWIPS,
        "scale": 1.0,                          # GRIB dBZ -> dBZ
        "floor": 5.0,                          # below this: blank
        "alpha": [0.30, 0.40, 0.50, 0.65, 0.78, 0.90,
                  1.00, 1.10, 1.20, 1.32, 1.38, 1.42, 1.45, 1.45, 1.45],
        "units": "dBZ",
    },
    "ETOP": {
        "label": "Echo tops",
        "base": "https://mrms.ncep.noaa.gov/data/2D/EchoTop_18",
        "file": "MRMS_EchoTop_18_00.50",
        "levels": list(range(10, 70, 5)),     # kft
        "colors": _ETOP,
        "scale": 3.28084,                      # GRIB km -> kft
        "floor": 10.0,
        # Tops are a height, not an intensity: draw every band at
        # the same weight so a 25 kft top and a 55 kft top read as
        # equally present and only the colour differs.
        "alpha": [1.0] * 11,
        "units": "kft",
    },
}
DEFAULT_PRODUCT = "REFL"

# Kept for callers that import these by name.
BASE = PRODUCTS["REFL"]["base"]
PRODUCT = PRODUCTS["REFL"]["file"]
LEVELS = PRODUCTS["REFL"]["levels"]
COLORS = PRODUCTS["REFL"]["colors"]

# The MRMS CONUS grid: 0.01 deg, 3500 x 7000. These are the grid
# EDGES, which is what a BitmapLayer wants — cell centres would
# offset the image by half a cell.
BOUNDS = [-130.0, 20.0, -60.0, 55.0]

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


def latest_file(timeout: int = 15, product: str = DEFAULT_PRODUCT):
    """(filename, stamp) for the newest GRIB2 of a product, or None.

    The directory listing is plain HTML, so names are pulled out by
    pattern. The format has been stable for years and the regex
    fails loudly rather than silently if it changes.
    """
    import requests

    spec = PRODUCTS[product]
    r = requests.get(f"{spec['base']}/", timeout=timeout,
                     headers={"User-Agent": "bluemet.org"})
    r.raise_for_status()
    stamps = re.findall(
        rf"{spec['file']}_(\d{{8}}-\d{{6}})\.grib2\.gz", r.text)
    if not stamps:
        return None
    stamp = max(stamps)
    return f"{spec['file']}_{stamp}.grib2.gz", stamp


import contextlib


@contextlib.contextmanager
def _quiet_stderr():
    """Silence the C-level ecCodes log for the duration of a decode.

    MRMS stamps its scan time with non-zero seconds and ecCodes
    truncates to the minute, logging "ECCODES ERROR: Truncating
    time: non-zero seconds ignored" — twice per scan, in red, every
    few minutes. The value is correct; only the label is alarming.
    It is written to file descriptor 2 by the C library, so Python
    logging cannot filter it; the descriptor has to be pointed at
    /dev/null. Real failures still raise as exceptions and are
    caught by the caller.
    """
    try:
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        try:
            os.dup2(saved, 2)
            os.close(saved)
        except OSError:
            pass


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
        with _quiet_stderr():
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


def palette(product: str = DEFAULT_PRODUCT):
    """RGBA lookup table; index 0 is transparent (no echo)."""
    import numpy as np

    spec = PRODUCTS[product]
    ramp = spec["alpha"]
    lut = np.zeros((len(spec["levels"]) + 1, 4), dtype="uint8")
    for i, (r, g, b) in enumerate(spec["colors"]):
        a = int(round(ALPHA * ramp[min(i, len(ramp) - 1)]))
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


def band_index(vals, product: str = DEFAULT_PRODUCT):
    """Field grid -> palette INDEX array (uint8).

    Kept separate from the RGBA expansion because the tiler wants
    indices: gathering one byte per pixel instead of four makes the
    per-tile crop four times cheaper, and the expansion happens on
    the 256x256 tile rather than on 24.5M source cells.
    """
    import numpy as np

    spec = PRODUCTS[product]
    levels = spec["levels"]
    floor = spec["floor"]
    if spec["scale"] != 1.0:
        vals = vals * spec["scale"]        # GRIB units -> display units
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

        ok = np.isfinite(vals) & (vals >= floor)
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
                      levels).astype("uint8")
    idx[~np.isfinite(vals)] = 0
    idx[vals < floor] = 0
    # MRMS rows run north to south and image rows run top to bottom,
    # so they already agree. Flipping would put Florida in Canada.
    return idx


def colorize(vals, product: str = DEFAULT_PRODUCT):
    """Field grid -> RGBA array."""
    return palette(product)[band_index(vals, product)]


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


def render_chunks(vals, outdir, stamp: str,
                  product: str = DEFAULT_PRODUCT) -> list:
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

    lut = palette(product)
    spec = PRODUCTS[product]
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
            # Empty-chunk test in the product's own units and floor.
            if not (np.nan_to_num(sub, nan=-999.0) * spec["scale"]
                    >= spec["floor"]).any():
                continue

            band = band_index(sub, product)
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

            name = f"mrmsc_{product}_{stamp}_{ix}{iy}.webp"
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
RENDER_STYLE = int(os.environ.get("MRMS_RENDER_STYLE", "6"))


def _manifest(outdir, product: str, stamp: str) -> Path:
    return Path(outdir) / f"mrmsc_{product}_{stamp}.json"


def _style_ok(outdir, stamp: str, product: str = DEFAULT_PRODUCT) -> bool:
    """True if chunks on disk were made by the current style."""
    import json as _json

    try:
        man = _json.loads(_manifest(outdir, product, stamp).read_text())
        return (isinstance(man, dict)
                and man.get("style") == RENDER_STYLE)
    except Exception:
        return False


def frame_name(stamp: str, product: str = DEFAULT_PRODUCT) -> str:
    return f"mrms_{product}_{stamp}.webp"


def build(outdir, product: str = DEFAULT_PRODUCT) -> tuple:
    """Fetch, decode and render the newest scan of one product.
    Returns (name, note).

    The chunk manifest is written last, so `newest` cannot return a
    set that is still being produced.
    """
    import json as _json

    import requests

    spec = PRODUCTS[product]
    got = latest_file(product=product)
    if not got:
        return None, f"no files in the {product} listing"
    fname, stamp = got
    name = frame_name(stamp, product)
    # STYLE-KEYED cache check. Without the style in the key, chunks
    # rendered by an older palette or resolution are served forever
    # for a stamp already on disk. Bump RENDER_STYLE whenever the
    # output changes.
    if (_manifest(outdir, product, stamp).exists()
            and _style_ok(outdir, stamp, product)):
        return name, "cached"
    r = requests.get(f"{spec['base']}/{fname}", timeout=90,
                     headers={"User-Agent": "bluemet.org"})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code} fetching {fname}"
    t0 = time.time()
    vals = decode(r.content)
    ny, nx = vals.shape
    chunks = render_chunks(vals, outdir, stamp, product)
    del vals
    if not chunks:
        # Still write a manifest: an empty scan is a real result and
        # the slider should be able to land on it rather than skip.
        _manifest(outdir, product, stamp).write_text(
            _json.dumps({"style": RENDER_STYLE, "chunks": []}))
        return name, f"{product} {nx}x{ny}, no echo anywhere"
    _cb = sum((Path(outdir) / c["name"]).stat().st_size
              for c in chunks)
    # Manifest LAST: `newest` reads it, so writing it earlier could
    # hand the page a set of chunks still being written.
    _manifest(outdir, product, stamp).write_text(
        _json.dumps({"style": RENDER_STYLE, "chunks": chunks}))
    return name, (f"{product} {nx * UPSAMPLE}x{ny * UPSAMPLE}, "
                  f"{len(chunks)} chunks, {_cb / 1024:.0f} KB, "
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


# Products the warmer keeps warm. MRMS_PRODUCTS trims it.
WARM_PRODUCTS = [x.strip().upper() for x in os.environ.get(
    "MRMS_PRODUCTS", "REFL,ETOP").split(",")
    if x.strip().upper() in PRODUCTS]


def _prune(outdir, product: str, keep: int) -> int:
    """Keep the newest `keep` scans of one product; drop the rest,
    chunks and manifest together. Returns scans removed."""
    mans = sorted(Path(outdir).glob(f"mrmsc_{product}_*.json"))
    dropped = 0
    for old in mans[:-keep]:
        m = re.search(r"(\d{8}-\d{6})", old.name)
        if m:
            for c in Path(outdir).glob(
                    f"mrmsc_{product}_{m.group(1)}_*.webp"):
                try:
                    c.unlink()
                except OSError:
                    pass
        try:
            old.unlink()
            dropped += 1
        except OSError:
            pass
    return dropped


def _daemon(outdir):
    # Staggered start: the CONUS map is the priority page and this
    # must not compete with its first load after a restart.
    time.sleep(float(os.environ.get("MRMS_DELAY_S", "45")))
    _log(outdir, f"MRMS warmer started (RSS {_rss_mb():.0f} MB), "
                 f"products {WARM_PRODUCTS}, keeping {KEEP} scans")
    while True:
        try:
            if _rss_mb() > MEM_CEILING_MB:
                _log(outdir, f"SKIPPED, RSS {_rss_mb():.0f} MB over "
                             f"{MEM_CEILING_MB:.0f} MB")
            else:
                for prod in WARM_PRODUCTS:
                    try:
                        name, note = build(outdir, prod)
                        if name:
                            if note != "cached":
                                _log(outdir, f"{name}: {note}")
                            _prune(outdir, prod, KEEP)
                        else:
                            _log(outdir, f"FAILED {prod}: {note}")
                    except Exception as exc:
                        _log(outdir, f"FAILED {prod}: "
                                     f"{type(exc).__name__}: {exc}")
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


def scans(outdir, product: str = DEFAULT_PRODUCT) -> list:
    """Stamps of every complete, current-style scan of a product,
    oldest first. This is the time slider's axis."""
    import json as _json

    out = []
    for p in sorted(Path(outdir).glob(f"mrmsc_{product}_*.json")):
        m = re.search(rf"mrmsc_{product}_(\d{{8}}-\d{{6}})\.json", p.name)
        if not m:
            continue
        try:
            man = _json.loads(p.read_text())
            if isinstance(man, dict) and man.get("style") == RENDER_STYLE:
                out.append(m.group(1))
        except Exception:
            continue
    return out


def chunks_for(outdir, stamp: str, product: str = DEFAULT_PRODUCT):
    """[{name, bounds}, ...] for one scan, or None if it is missing
    or was rendered under an older style."""
    import json as _json

    try:
        man = _json.loads(_manifest(outdir, product, stamp).read_text())
        if not isinstance(man, dict) or man.get("style") != RENDER_STYLE:
            return None
        return man.get("chunks", [])
    except Exception:
        return None


def newest(outdir, product: str = DEFAULT_PRODUCT):
    """(chunks, stamp) for the newest scan, or (None, None)."""
    st = scans(outdir, product)
    if not st:
        return None, None
    return chunks_for(outdir, st[-1], product), st[-1]
