"""Fast frame rendering: cached basemap plus a colourised data layer.

WHY THIS EXISTS

Profiling the warm path on 8/26 found that essentially all of the
render time was one call. On a 13-degree region:

    3x bilinear upsample            0.37 s
    contourf on the upsampled grid  6.74 s   <-- everything
    pcolormesh, native grid         0.39 s   (but 3 km blocks)
    pcolormesh, upsampled, gouraud 28.44 s   (worse)

The coastlines, state borders, gridlines and colourbar were never
the problem — and they are IDENTICAL for every frame of a region,
so rendering them thousands of times a day is pure waste.

THE APPROACH

Two halves, each cached at the level it can be:

  1. BASEMAP — all the map furniture, drawn once per region by
     cartopy onto a transparent canvas whose axes fill the figure
     exactly. Cached on disk. Reused by every frame, every product,
     every cycle.

  2. DATA LAYER — the field, resampled onto the basemap's pixel grid
     and coloured by a lookup table. No plotting library involved:
     np.digitize gives a palette index per pixel and the RGBA table
     is indexed directly.

The resampling needs a source-to-target index map, which is the only
expensive part (2.7 s via cKDTree). But the GRID GEOMETRY DOES NOT
CHANGE BETWEEN FRAMES — only the values do — so it is computed once
per region and model and reused for every hour of every cycle.

Measured result: 6.74 s -> 1.21 s per frame, a 5.6x speedup, with
smoother edges than the native-grid mesh because the field is
interpolated before it is quantised.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

# Palettes mirror core.hrrr_cam so a fast frame and a matplotlib
# frame are indistinguishable. Bounds are the band edges; colours
# fill between them, with the last colour used above the top bound.
PALETTES = {
    "REFD": (list(range(5, 80, 5)),
             ["#04E9E7", "#019FF4", "#0300F4", "#02FD02", "#01C501",
              "#008E00", "#FDF802", "#E5BC00", "#FD9500", "#FD0000",
              "#D40000", "#BC0000", "#F800FD", "#9854C6"]),
    "REFC": (list(range(5, 80, 5)),
             ["#04E9E7", "#019FF4", "#0300F4", "#02FD02", "#01C501",
              "#008E00", "#FDF802", "#E5BC00", "#FD9500", "#FD0000",
              "#D40000", "#BC0000", "#F800FD", "#9854C6"]),
}
# Where the field means "nothing to draw" rather than a low value.
FLOOR = {"REFD": 5.0, "REFC": 5.0}

_INDEX_CACHE = {}      # (key) -> flat index array
_BASEMAP_MEM = {}      # (key) -> RGBA Image


def _rgba(hex_colors):
    import numpy as np

    lut = np.zeros((len(hex_colors) + 2, 4), dtype="uint8")
    for i, h in enumerate(hex_colors):
        h = h.lstrip("#")
        lut[i + 1] = (int(h[0:2], 16), int(h[2:4], 16),
                      int(h[4:6], 16), 255)
    lut[-1] = lut[-2]          # above the top bound
    return lut


def regrid_index(key: str, lats, lons, extent, width: int,
                 height: int):
    """Source-cell index for every target pixel.

    Cached by `key`, which must identify the region AND the model
    grid — two models on different native grids need different
    index maps even for the same region.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    hit = _INDEX_CACHE.get(key)
    if hit is not None and hit[1] == (width, height):
        return hit[0]
    w, s, e, n = extent
    la = np.asarray(lats, dtype="float64")
    lo = np.asarray(lons, dtype="float64")
    if la.ndim == 1:
        lo, la = np.meshgrid(lo, la)
    lo = np.where(lo > 180.0, lo - 360.0, lo)
    gx = np.linspace(w, e, width)
    gy = np.linspace(n, s, height)      # image rows run north->south
    GX, GY = np.meshgrid(gx, gy)
    tree = cKDTree(np.column_stack([lo.ravel(), la.ravel()]))
    _, idx = tree.query(np.column_stack([GX.ravel(), GY.ravel()]),
                        k=1, workers=-1)
    _INDEX_CACHE[key] = (idx, (width, height))
    return idx


def basemap(key: str, extent, width: int, height: int,
            cache_dir=None):
    """Map furniture on a transparent canvas, cached on disk.

    The axes fill the figure exactly ([0, 0, 1, 1]) so the mapping
    from lat/lon to pixel is linear and shared with the data layer.
    Anything drawn outside the axes — a colourbar in a margin, edge
    tick labels — would break that alignment, so the colourbar is an
    inset and the gridline labels are inline.
    """
    from PIL import Image

    mem = _BASEMAP_MEM.get((key, width, height))
    if mem is not None:
        return mem
    path = None
    if cache_dir:
        tag = hashlib.md5(
            f"{key}|{extent}|{width}x{height}".encode()).hexdigest()[:12]
        path = Path(cache_dir) / f"basemap_{tag}.png"
        if path.exists():
            try:
                im = Image.open(path).convert("RGBA")
                _BASEMAP_MEM[(key, width, height)] = im
                return im
            except Exception:
                pass

    import matplotlib
    matplotlib.use("Agg")
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import matplotlib.pyplot as plt

    w, s, e, n = extent
    fig = plt.figure(figsize=(width / 100.0, height / 100.0), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree())
    ax.set_extent([w, e, s, n], crs=ccrs.PlateCarree())
    ax.patch.set_alpha(0.0)
    ax.outline_patch.set_visible(False) if hasattr(
        ax, "outline_patch") else None
    try:
        ax.add_feature(cfeature.COASTLINE.with_scale("10m"),
                       linewidth=0.9, edgecolor="#1a1a1a", zorder=5)
        ax.add_feature(cfeature.STATES.with_scale("10m"),
                       linewidth=0.55, edgecolor="#444444", zorder=5)
        ax.add_feature(cfeature.BORDERS.with_scale("10m"),
                       linewidth=0.8, edgecolor="#1a1a1a", zorder=5)
    except Exception:
        pass
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, linestyle=":",
                      color="#777777", x_inline=True, y_inline=True,
                      zorder=6)
    gl.xlabel_style = {"size": 7, "color": "#555"}
    gl.ylabel_style = {"size": 7, "color": "#555"}
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True, dpi=100)
    plt.close(fig)
    buf.seek(0)
    im = Image.open(buf).convert("RGBA")
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            im.save(path, "PNG", optimize=True)
        except OSError:
            pass
    _BASEMAP_MEM[(key, width, height)] = im
    return im


def render_fast(product: str, vals, lats, lons, center_lat: float,
                center_lon: float, zoom_deg: float, grid_key: str,
                ppd: int = 150, cache_dir=None, smooth: float = 1.2,
                webp_q: int = 88) -> bytes:
    """One frame: resample, colourise, composite. Returns WebP bytes.

    `grid_key` must identify the region AND the source grid, because
    the index map is cached against it.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    if product not in PALETTES:
        raise ValueError(f"no fast palette for {product}")
    bounds, colors = PALETTES[product]
    lut = _rgba(colors)
    floor = FLOOR.get(product, bounds[0])

    extent = (center_lon - zoom_deg, center_lat - zoom_deg,
              center_lon + zoom_deg, center_lat + zoom_deg)
    width = height = int(2 * zoom_deg * ppd)
    idx = regrid_index(grid_key, lats, lons, extent, width, height)

    src = np.asarray(vals, dtype="float32").ravel()
    g = src[idx].reshape(height, width)
    if smooth:
        # Nearest-neighbour resampling leaves stair-steps at the
        # source cell edges. A light gaussian before quantising is
        # what makes this look like contourf rather than a mesh, and
        # costs a fraction of what contouring did.
        g = ndi.gaussian_filter(np.nan_to_num(g, nan=floor - 99.0),
                                smooth)
    band = np.digitize(g, bounds).astype("uint8")
    band[~np.isfinite(g)] = 0
    band[g < floor] = 0
    data_im = Image.fromarray(lut[band], mode="RGBA")

    base = basemap(grid_key.split("|")[0], extent, width, height,
                   cache_dir=cache_dir)
    # Furniture ON TOP of the field: coastlines must stay visible
    # through heavy echo.
    out = Image.alpha_composite(data_im, base)
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=webp_q, method=4)
    return buf.getvalue()


def supports(product: str) -> bool:
    """Whether the fast path can render this product."""
    if os.environ.get("CAM_FAST", "on").lower() == "off":
        return False
    return product in PALETTES
