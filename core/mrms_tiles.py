"""MRMS reflectivity as a Web Mercator tile pyramid.

A single CONUS raster is correct at one zoom and stretched at every
other. At Florida width a 7000 px image is scaled 5x; at a terminal
area, 15x, and the 1 km cells become visible squares. Tiles keep
pixels-per-screen constant at every zoom, which is what makes a
radar display look the same zoomed in as zoomed out.

WHAT THE NAIVE VERSION GETS WRONG

Building each zoom level as one image and slicing it up. Measured:
the z8 level over CONUS is 12743 x 8321 — 106 megapixels, 424 MB of
RGBA — and it OOM-killed the process before a single tile was
written.

Tiles are cut STRAIGHT FROM THE SOURCE ARRAY instead. Nothing larger
than one 256 x 256 tile is ever materialised, so memory is flat
regardless of how deep the pyramid goes.

THE PROJECTION SHORTCUT

Source is a regular 0.01 degree lat/lon grid; tiles are Web
Mercator. Longitude is LINEAR in both, so the x mapping is a plain
arange. Only y needs the Mercator transform, and that is one
inverse per tile row — 256 values, not 65536. This is what keeps
per-tile cost at single-digit milliseconds.

EMPTY TILES ARE SKIPPED

Roughly 94% of CONUS has no echo at any moment. A tile with nothing
in it is never written, and deck.gl treats the resulting 404 as
"nothing here" — which is correct and free. On a typical scan this
cuts the pyramid by an order of magnitude.

WHERE THE DETAIL ACTUALLY STOPS

MRMS is a 0.01 degree grid. Zoom 7 is native; z8 is already
upsampling. Building past z8 writes exponentially more tiles that
carry no additional information — every product on the market
interpolates beyond this point, including the ones that look
smoother. Z_MAX is 8 for that reason, and the map smooth-scales
above it.
"""

from __future__ import annotations

import io
import math
import os
import shutil
from pathlib import Path

TILE = 256
Z_MIN = int(os.environ.get("MRMS_TILE_ZMIN", "3"))
Z_MAX = int(os.environ.get("MRMS_TILE_ZMAX", "8"))
WEBP_Q = int(os.environ.get("MRMS_TILE_Q", "80"))
# Source grid: MRMS CONUS, 0.01 deg, north-to-south rows.
SRC_W, SRC_E = -130.0, -60.0
SRC_N, SRC_S = 55.0, 20.0


def _merc_y(lat: float) -> float:
    """Latitude -> Mercator y in [0, 1], 0 at the north pole."""
    lat = max(-85.05112878, min(85.05112878, lat))
    r = math.radians(lat)
    return (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r))
            / math.pi) / 2.0


def _inv_merc_y(y: float) -> float:
    """Mercator y in [0, 1] -> latitude."""
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))


def tile_range(z: int):
    """(x0, x1, y0, y1) tile indices covering the source extent."""
    n = 2 ** z
    x0 = int((SRC_W + 180.0) / 360.0 * n)
    x1 = min(n - 1, int((SRC_E + 180.0) / 360.0 * n))
    y0 = int(_merc_y(SRC_N) * n)
    y1 = min(n - 1, int(_merc_y(SRC_S) * n))
    return x0, x1, y0, y1


def _row_index(z: int, ty: int, src_rows: int):
    """Source row for each of the 256 pixel rows in this tile.

    The only place the Mercator transform is needed. 256 inverses
    per tile row rather than one per pixel is what keeps this fast.
    """
    import numpy as np

    n = 2 ** z
    ys = (ty * TILE + np.arange(TILE) + 0.5) / (n * TILE)
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * ys))))
    # Source rows run NORTH to SOUTH.
    frac = (SRC_N - lat) / (SRC_N - SRC_S)
    idx = np.floor(frac * src_rows).astype("int64")
    return np.clip(idx, 0, src_rows - 1), lat


def _col_index(z: int, tx: int, src_cols: int):
    """Source column for each of the 256 pixel columns. Linear."""
    import numpy as np

    n = 2 ** z
    xs = (tx * TILE + np.arange(TILE) + 0.5) / (n * TILE)
    lon = xs * 360.0 - 180.0
    frac = (lon - SRC_W) / (SRC_E - SRC_W)
    idx = np.floor(frac * src_cols).astype("int64")
    return np.clip(idx, 0, src_cols - 1), lon


def build_pyramid(band, lut, outdir, stamp: str,
                  z_min: int = None, z_max: int = None,
                  progress=None) -> dict:
    """Write the pyramid for one scan.

    `band` is the palette-index array (uint8, 0 = transparent) at
    source resolution; `lut` maps index to RGBA. Passing indices
    rather than RGBA keeps the gather 4x smaller.
    """
    import numpy as np
    from PIL import Image

    z_min = Z_MIN if z_min is None else z_min
    z_max = Z_MAX if z_max is None else z_max
    rows, cols = band.shape
    root = Path(outdir) / stamp
    written = skipped = 0
    nbytes = 0

    for z in range(z_min, z_max + 1):
        x0, x1, y0, y1 = tile_range(z)
        for ty in range(y0, y1 + 1):
            ri, lat = _row_index(z, ty, rows)
            # A tile row entirely outside the source latitude band
            # has nothing to draw.
            if lat.max() < SRC_S or lat.min() > SRC_N:
                continue
            for tx in range(x0, x1 + 1):
                ci, lon = _col_index(z, tx, cols)
                if lon.max() < SRC_W or lon.min() > SRC_E:
                    continue
                # Fancy-index the source directly: no intermediate
                # level image, so memory is one tile regardless of z.
                sub = band[np.ix_(ri, ci)]
                if not sub.any():
                    skipped += 1
                    continue
                im = Image.fromarray(lut[sub], mode="RGBA")
                d = root / str(z) / str(tx)
                d.mkdir(parents=True, exist_ok=True)
                buf = io.BytesIO()
                im.save(buf, "WEBP", quality=WEBP_Q, method=0)
                data = buf.getvalue()
                (d / f"{ty}.webp").write_bytes(data)
                written += 1
                nbytes += len(data)
        if progress:
            progress(z, written, skipped)
    return {"stamp": stamp, "written": written, "skipped": skipped,
            "bytes": nbytes, "z_min": z_min, "z_max": z_max}


def prune(outdir, keep: int = 2):
    """Keep the newest `keep` scans. A pyramid is thousands of small
    files, so stale ones are removed by directory, not by glob."""
    root = Path(outdir)
    if not root.exists():
        return 0
    scans = sorted([p for p in root.iterdir() if p.is_dir()])
    dropped = 0
    for old in scans[:-keep] if keep else scans:
        try:
            shutil.rmtree(old)
            dropped += 1
        except OSError:
            pass
    return dropped


def newest(outdir):
    """Stamp of the newest complete pyramid, or None."""
    root = Path(outdir)
    if not root.exists():
        return None
    scans = sorted([p.name for p in root.iterdir()
                    if p.is_dir() and (p / "done").exists()])
    return scans[-1] if scans else None


def mark_done(outdir, stamp: str):
    """Marker written LAST. Without it the page can serve a pyramid
    that is still being written and show a half-drawn scan."""
    try:
        (Path(outdir) / stamp / "done").write_text("1")
    except OSError:
        pass
