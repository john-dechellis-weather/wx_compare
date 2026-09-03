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

# Palettes mirror core.hrrr_cam EXACTLY, including the unit
# conversions and masking, because a fast frame and a matplotlib
# frame must be indistinguishable — they are served from the same
# store and a user cannot tell which path produced one.
#
# Each entry:
#   bounds      band edges in DISPLAY units (after scale)
#   colors      one per band; the last also covers "above top"
#   metpy       colortable name to use instead of `colors`
#   scale       multiply raw values by this to reach display units
#   below       values under this draw nothing
#   above       values over this draw nothing
#
# Visibility and ceiling are INVERTED relative to reflectivity: low
# values are the significant ones, nothing is masked at the bottom,
# and ceiling masks the TOP (no ceiling = nothing to draw).
PALETTES = {
    "REFD": {"bounds": list(range(5, 80, 5)), "metpy": "NWSReflectivity",
             "below": 5.0},
    "REFC": {"bounds": list(range(5, 80, 5)), "metpy": "NWSReflectivity",
             "below": 5.0},
    "RETOP": {"bounds": [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50,
                         55, 60, 70],
              "colors": ["#C8C8C8", "#9BD4F5", "#4FA8E8", "#2E6FDB",
                         "#22B14C", "#7CD934", "#FFF200", "#FFC90E",
                         "#FF7F27", "#ED1C24", "#B21E28", "#A349A4",
                         "#6F2DA8"],
              "scale": 1.0 / 304.8, "below": 0.0},
    "VIS": {"bounds": [0, 0.5, 1, 2, 3, 5, 7, 10],
            "colors": ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                       "#B0E000", "#60C060", "#E8E8E8"],
            "scale": 1.0 / 1609.34},
    "CEIL": {"bounds": [0, 2, 4, 10, 20, 30, 50, 100, 300],
             "colors": ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                        "#B0E000", "#60C060", "#A8D8A8", "#E8E8E8"],
             "scale": 3.28084 / 100.0, "above": 300.0},
    "GUST": {"bounds": [0, 10, 15, 20, 25, 30, 35, 40, 50, 65],
             "colors": ["#E8E8E8", "#B0E0FF", "#60B0E0", "#FFFF00",
                        "#FFC90E", "#FF9900", "#FF4040", "#B21E28",
                        "#A349A4"],
             "scale": 1.943844},
}
# REFS probability fields share one ramp.
for _pk in ("PROB_CIG1000", "PROB_VIS1", "PROB_REFC40", "PROB"):
    PALETTES[_pk] = {
        "bounds": [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
        "colors": ["#d1e9f7", "#8fcbe8", "#54a6d6", "#4bb84b",
                   "#a4d64b", "#f5e642", "#f5a742", "#ec5f27",
                   "#c81e1e", "#8b0f5e"],
        "below": 5.0,
    }

# Coverage-aware blur controls; see the comment inside render_fast.
# MIN: coverage below this is blank. FLOOR: coverage is not restored
# below this, so isolated cells fade at their own size instead of
# being inflated to full-strength discs.
# Neighbours per target pixel. 1 = nearest, and that is the RIGHT
# default for reflectivity. Inverse-distance interpolation (4) was
# tried and rendered: it draws a lone cell as a LARGER blob with a
# graded halo, because every pixel whose neighbours include the
# cell takes a share of its value. Correct for a continuous field,
# wrong for a field that is mostly isolated single cells. Nearest
# plus the coverage floor below draws each cell at its own size in
# its own colour. Left switchable for smooth products.
REGRID_K = int(os.environ.get("CAM_REGRID_K", "1"))
# SPECKLE FILTER, ON by default. Connected echo regions smaller than
# SPECKLE_MIN_CELLS source cells AND with no pixel reaching
# SPECKLE_MAX_DBZ are dropped before rendering.
#
# Set from a real RRFS field on 3 Sep: the clutter was not single
# 5 dBZ cells but clumps of 2-6 cells at 25-35 dBZ with nothing
# inside them, so a "2 cells / 20 dBZ" filter removed none of it.
# 10 cells is ~30 km across; 35 dBZ is the floor of a real
# convective core. A region fails only if it is BOTH small and
# weak — a lone 45 dBZ cell is kept however small, and a broad
# 10 dBZ shield is kept however weak. CAM_SPECKLE_MIN_CELLS=0
# switches it off.
SPECKLE_MIN_CELLS = int(os.environ.get("CAM_SPECKLE_MIN_CELLS", "10"))
SPECKLE_MAX_DBZ = float(os.environ.get("CAM_SPECKLE_MAX_DBZ", "35"))
COVERAGE_MIN = float(os.environ.get("CAM_COVERAGE_MIN", "0.15"))
COVERAGE_FLOOR = float(os.environ.get("CAM_COVERAGE_FLOOR", "0.45"))

_LUT_CACHE = {}

_INDEX_CACHE = {}      # (key) -> flat index array
_BASEMAP_MEM = {}      # (key) -> RGBA Image


def _lut_for(product: str):
    """RGBA lookup table, index 0 = transparent.

    Reflectivity uses metpy's NWSReflectivity table — the SAME one
    core.hrrr_cam uses — rather than a hardcoded copy, so the two
    renderers cannot drift apart. Falls back to an equivalent hex
    ramp if metpy is unavailable.
    """
    import numpy as np

    hit = _LUT_CACHE.get(product)
    if hit is not None:
        return hit
    spec = PALETTES[product]
    cols = spec.get("colors")
    if spec.get("metpy"):
        try:
            from metpy.plots import colortables

            _n, cmap = colortables.get_with_steps(spec["metpy"], 5, 5)
            cols = [
                "#%02X%02X%02X" % tuple(
                    int(round(c * 255)) for c in cmap(i)[:3])
                for i in range(cmap.N)
            ]
        except Exception:
            cols = ["#04E9E7", "#019FF4", "#0300F4", "#02FD02",
                    "#01C501", "#008E00", "#FDF802", "#E5BC00",
                    "#FD9500", "#FD0000", "#D40000", "#BC0000",
                    "#F800FD", "#9854C6"]
    lut = np.zeros((len(cols) + 2, 4), dtype="uint8")
    for i, h in enumerate(cols):
        h = h.lstrip("#")
        lut[i + 1] = (int(h[0:2], 16), int(h[2:4], 16),
                      int(h[4:6], 16), 255)
    lut[-1] = lut[-2]
    _LUT_CACHE[product] = lut
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
    # REGRID_K neighbours with inverse-distance weights. Default 1
    # (nearest); see the constant for why interpolation was rejected
    # for reflectivity. The k=4 path is kept for smooth fields.
    dist, idx = tree.query(np.column_stack([GX.ravel(), GY.ravel()]),
                           k=REGRID_K, workers=-1)
    if REGRID_K == 1:
        idx = idx.reshape(-1, 1)
        wts = np.ones_like(idx, dtype="float32")
    else:
        # Inverse distance squared; a pixel sitting on a cell centre
        # gets that cell almost entirely.
        wts = 1.0 / np.maximum(dist, 1e-9) ** 2
        wts = (wts / wts.sum(axis=1, keepdims=True)).astype("float32")
    _INDEX_CACHE[key] = ((idx, wts), (width, height))
    return idx, wts


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
    spec = PALETTES[product]
    bounds = spec["bounds"]
    lut = _lut_for(product)

    extent = (center_lon - zoom_deg, center_lat - zoom_deg,
              center_lon + zoom_deg, center_lat + zoom_deg)
    width = height = int(2 * zoom_deg * ppd)
    idx, wts = regrid_index(grid_key, lats, lons, extent, width, height)

    src = np.asarray(vals, dtype="float32").ravel()
    nb = src[idx]                                  # (N, k)
    ok = np.isfinite(nb)
    # A missing neighbour drops out and the others take its weight,
    # so an echo next to a no-data cell is not pulled toward zero.
    wk = np.where(ok, wts, 0.0)
    wsum = wk.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        g = (np.where(ok, nb, 0.0) * wk).sum(axis=1) / wsum
    g = np.where(wsum > 0, g, np.nan).reshape(height, width)
    # Convert to DISPLAY units before anything else: the bounds and
    # the masks are both expressed in them (kft, statute miles,
    # hundreds of feet, knots).
    scale = float(spec.get("scale", 1.0))
    if scale != 1.0:
        g = g * scale

    # Build the "draw nothing" mask BEFORE smoothing, or the filter
    # drags masked values into neighbouring pixels — a ceiling of
    # 30,000 ft bleeding into a 200 ft cell would be a dangerous
    # artefact, not a cosmetic one.
    blank = ~np.isfinite(g)
    if "below" in spec:
        blank |= g < float(spec["below"])
    if "above" in spec:
        blank |= g > float(spec["above"])

    if SPECKLE_MIN_CELLS > 0 and product in ("REFD", "REFC"):
        # Label connected echo regions on the TARGET grid; a source
        # cell is ~(ppd*0.027)^2 target pixels, so convert the cell
        # count to a pixel count once.
        px_per_cell = max(1.0, (ppd * 0.027) ** 2)
        lab, nlab = ndi.label(~blank)
        if nlab:
            sizes = ndi.sum(~blank, lab, index=np.arange(1, nlab + 1))
            peaks = ndi.maximum(np.nan_to_num(g, nan=-1e9), lab,
                                index=np.arange(1, nlab + 1))
            drop = ((sizes < SPECKLE_MIN_CELLS * px_per_cell)
                    & (peaks < SPECKLE_MAX_DBZ))
            if drop.any():
                blank |= np.isin(lab, np.nonzero(drop)[0] + 1)

    if smooth:
        filled = np.where(blank, np.nan, g)
        # Smooth the field and the coverage together, then divide:
        # this is a nan-aware blur, so masked cells contribute
        # nothing instead of pulling values toward zero.
        w0 = (~blank).astype("float32")
        num = ndi.gaussian_filter(np.nan_to_num(filled, nan=0.0),
                                  smooth)
        den = ndi.gaussian_filter(w0, smooth)
        # COVERAGE FLOOR. Dividing by the smoothed coverage restores
        # full intensity at the EDGE of a real echo, which is what
        # keeps the rim from being eaten by the blur. But with a
        # floor of 1e-6 and a threshold of 0.02, it restored a lone
        # 3 km cell to full strength everywhere its gaussian tail
        # exceeded 2% — one 6 dBZ speck became a round 6 dBZ disc
        # larger than the cell. That was the field of dots on the
        # RRFS 1 km reflectivity.
        #
        # A floor of 0.45 means coverage below 45% is NOT fully
        # restored: interior and edges of real echoes (coverage near
        # 1) are untouched, while an isolated cell (peak coverage
        # ~0.3) keeps most of its value at its centre and fades at
        # its edge — drawn at its own size, not inflated. Measured:
        # big-echo peak identical, speck footprint down ~20%, no
        # cell removed.
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.where(den > COVERAGE_MIN,
                         num / np.maximum(den, COVERAGE_FLOOR), np.nan)
        blank = ~np.isfinite(g) | (den <= COVERAGE_MIN)

    band = np.digitize(np.nan_to_num(g, nan=-1e9),
                       bounds).astype("uint8")
    band[blank] = 0
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
