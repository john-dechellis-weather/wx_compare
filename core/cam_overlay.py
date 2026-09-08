"""Transparent model-field overlays for the airspace maps.

SEPARATE from core.hrrr_cam's render_field on purpose. That one draws
a complete figure — axes, gridlines, coastlines, state borders, a
colorbar and a title — because it is a standalone panel meant to be
looked at on its own. Overlaying that on a slippy map gives you the
white page margin, a second set of coastlines, and a colorbar sitting
in the middle of the ocean.

This renders the FIELD AND NOTHING ELSE onto a transparent canvas, in
plain lat/lon, so a deck.gl BitmapLayer can place it by corners and
the basemap shows through. It shares hrrr_cam's fetch and decode, so
there is no second download path and no second set of model quirks to
maintain — only the drawing differs.

Design notes that matter for speed:

  * ONE fixed domain, not per-hub. The overlay covers the whole
    Mid-Atlantic and Northeast, so panning and zooming inside it
    never triggers a new render — the browser is just moving an
    image it already has. Per-hub frames would mean a fetch every
    time the view moved.
  * Rendered at a resolution matched to HRRR's own 3 km grid. Going
    finer invents detail the model does not have and costs bytes.
  * PNG8 with a fixed palette. The AWIPS-style ramp is discrete
    anyway, so quantising is lossless in practice and roughly
    quarters the file.
"""

from __future__ import annotations

import io
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# PIT to RDU to PWM, which is the operating area rather than a
# political boundary: west of Pittsburgh, south of Raleigh-Durham,
# north-east of Portland. 12.5 x 10 degrees.
DOMAIN = {
    "west": float(os.environ.get("OVL_WEST", "-81.5")),
    "east": float(os.environ.get("OVL_EAST", "-69.0")),
    "south": float(os.environ.get("OVL_SOUTH", "35.0")),
    "north": float(os.environ.get("OVL_NORTH", "45.0")),
}
# HRRR is 3 km (~0.027 deg). 0.01 deg oversamples it ~2.7x, which is
# interpolation for display smoothness rather than invented detail —
# and it is what stops the field looking blocky when you zoom into a
# terminal area. Measured: 1250x1000 px, 26 KB after quantisation,
# ~2 s to render. Going to 0.0075 costs 40% more bytes for detail the
# model does not have.
STEP_DEG = float(os.environ.get("OVL_STEP_DEG", "0.01"))
MAX_FHR = int(os.environ.get("OVL_MAX_FHR", "18"))

# SHORT VS LONG RUNS.
#
# HRRR runs every hour to f18, but the 00/06/12/18Z cycles reach f48.
# RRFS runs every 3 hours, and its synoptic cycles reach f84. So each
# model has two useful depths, and a page wants both: the freshest
# run for the next few hours, and the newest deep run for planning.
#
# Warming the long run is expensive — f48 is 49 frames per product,
# f84 is 85 — so it is done only for cycles that actually go that
# deep, and only on the synoptic hours.
# REFS runs only four times a day and every run goes to f60, so its
# "short" run IS the whole run; there is no separate long run to add.
SHORT_FHR = {"hrrr": 18, "rrfs": 18, "refs_pmmn": 60, "refs_mean": 60,
             "refs_prob": 60}
LONG_FHR = {"hrrr": int(os.environ.get("OVL_HRRR_LONG", "48")),
            "rrfs": int(os.environ.get("OVL_RRFS_LONG", "84"))}
LONG_CYCLES = (0, 6, 12, 18)

# Products warmed for every model, in this order. Reflectivity first
# because it is the default view and the one worth having soonest if
# a pass is cut short by the memory ceiling.
WARM_PRODUCTS = [p.strip() for p in os.environ.get(
    "OVL_PRODUCTS", "REFD,VIS,CEIL,GUST,RETOP").split(",") if p.strip()]
PRODUCT = os.environ.get("OVL_PRODUCT", "REFD")

# ---------------------------------------------------------------------------
# Product scales
# ---------------------------------------------------------------------------
# One ramp per field. Reflectivity was the only product this module
# rendered, so its dBZ scale was hard-coded; visibility, ceiling,
# gusts and echo tops each need their own units, thresholds and
# colours, and getting the UNITS wrong is silent — a ceiling plotted
# in metres against a feet scale simply shows everything as low IFR.
#
# GRIB units, which is where the conversions come from:
#   VIS    metres        -> statute miles
#   CEIL   metres AGL    -> feet
#   GUST   m/s           -> knots
#   RETOP  metres        -> kilofeet
#   REFD   dBZ           -> unchanged
#
# VIS and CEIL use FLIGHT CATEGORY colours — magenta LIFR, red IFR,
# blue MVFR, green VFR — because that is the scale a dispatcher
# already reads, and inventing a third colour language for the same
# question would be worse than any aesthetic gain. The categories are
# drawn LOW-VALUE-FIRST: the interesting end of a visibility field is
# the small numbers, the opposite of reflectivity.
_FLIGHT_CAT = ["#FF00FF", "#FF0000", "#0080FF", "#00C000", "#00C000"]

SCALES = {
    "REFD": {
        "label": "1 km simulated reflectivity",
        "units": "dBZ",
        "convert": None,
        "levels": list(range(5, 80, 5)),
        "colors": ["#04E9E7", "#019FF4", "#0300F4", "#02FD02",
                   "#01C501", "#008E00", "#FDF802", "#E5BC00",
                   "#FD9500", "#FD0000", "#D40000", "#BC0000",
                   "#F800FD", "#9854C6", "#FDFDFD"],
        # Values BELOW this are transparent.
        "mask_below": 5.0,
        "invert": False,
    },
    "REFC": {
        "label": "composite reflectivity",
        "units": "dBZ",
        "convert": None,
        "levels": list(range(5, 80, 5)),
        "colors": ["#04E9E7", "#019FF4", "#0300F4", "#02FD02",
                   "#01C501", "#008E00", "#FDF802", "#E5BC00",
                   "#FD9500", "#FD0000", "#D40000", "#BC0000",
                   "#F800FD", "#9854C6", "#FDFDFD"],
        "mask_below": 5.0,
        "invert": False,
    },
    "VIS": {
        "label": "surface visibility",
        "units": "sm",
        "convert": lambda v: v / 1609.344,
        # LIFR < 1, IFR 1-3, MVFR 3-5, VFR 5+. Anything at or above
        # the top level is transparent: a map painted green
        # everywhere hides the three places that matter.
        "levels": [0.5, 1.0, 3.0, 5.0],
        "colors": ["#FF00FF", "#FF0000", "#FF8000", "#0080FF"],
        "mask_above": 5.0,
        "invert": True,
    },
    "CEIL": {
        "label": "ceiling",
        "units": "ft AGL",
        "convert": lambda v: v * 3.280839895,
        "levels": [200.0, 500.0, 1000.0, 3000.0],
        "colors": ["#FF00FF", "#FF0000", "#FF8000", "#0080FF"],
        "mask_above": 3000.0,
        "invert": True,
    },
    "GUST": {
        "label": "10 m wind gust",
        "units": "kt",
        "convert": lambda v: v * 1.9438445,
        # Below 20 kt is not worth painting over an airspace map.
        "levels": [20, 25, 30, 35, 40, 45, 50, 60],
        "colors": ["#C8E6C9", "#FFF176", "#FFB300", "#FB8C00",
                   "#E53935", "#B71C1C", "#8E24AA", "#4A148C"],
        "mask_below": 20.0,
        "invert": False,
    },
    "RETOP": {
        "label": "echo tops",
        "units": "kft",
        "convert": lambda v: v * 3.280839895 / 1000.0,
        "levels": [10, 15, 20, 25, 30, 35, 40, 45, 50],
        "colors": ["#B3E5FC", "#4FC3F7", "#039BE5", "#00C853",
                   "#AEEA00", "#FFD600", "#FF9100", "#DD2C00",
                   "#B71C1C"],
        "mask_below": 10.0,
        "invert": False,
    },
}


# ENSEMBLE PROBABILITY. One scale for every REFS exceedance field:
# percent of members, 0-100 in 10% bands, nothing drawn under 5% so
# a count of zero draws nothing. The label carries the threshold.
PROB_LABELS = {
    "PROB_REFC40": "probability of reflectivity > 40 dBZ",
    "PROB_CIG500": "probability of ceiling < 500 ft",
    "PROB_CIG1000": "probability of ceiling < 1,000 ft",
    "PROB_CIG2000": "probability of ceiling < 2,000 ft",
    "PROB_VIS05": "probability of visibility < 1/2 sm",
    "PROB_VIS1": "probability of visibility < 1 sm",
    "PROB_VIS3": "probability of visibility < 3 sm",
    "PROB_RETOP30": "probability of echo tops > FL300",
    "PROB_RETOP35": "probability of echo tops > FL350",
}
_PROB_SCALE = {
    "units": "%",
    "convert": None,
    "levels": [5, 10, 20, 30, 40, 50, 60, 70, 80, 90],
    "colors": ["#DDEEFF", "#B3D4F5", "#7FB6EB", "#4C97E0", "#2F7FD6",
               "#FFF275", "#FFC94D", "#FF9E3D", "#F5602E", "#C81E1E"],
    "mask_below": 5.0,      # a member count of zero draws nothing
    "invert": False,
    # An ensemble statistic is already a smoothing; do not blur it
    # like reflectivity.
    "smooth": 0.5,
}


def scale_for(product: str) -> dict:
    """Scale definition for a product.

    Raises rather than guessing for an unknown product: rendering a
    field against the wrong scale produces a plausible-looking image
    that is entirely wrong, which is the worst failure mode here.
    """
    if product in PROB_LABELS:
        return dict(_PROB_SCALE, label=PROB_LABELS[product])
    if product not in SCALES:
        raise RuntimeError(f"no display scale defined for {product}")
    return SCALES[product]


# WHICH PRODUCTS EACH MODEL WARMS. HRRR and RRFS: the deterministic
# set from OVL_PRODUCTS. REFS PMMN: composite reflectivity only, the
# one field it publishes that maps to a radar look. REFS prob: the
# exceedance fields in OVL_REFS_PROBS — three by default, because
# each is sixty frames a cycle.
REFS_PROB_PRODUCTS = [p.strip().upper() for p in os.environ.get(
    "OVL_REFS_PROBS", "PROB_CIG1000,PROB_VIS1,PROB_REFC40").split(",")
    if p.strip()]


def products_for(model: str) -> list:
    if model == "refs_pmmn" or model == "refs_mean":
        return ["REFC"]
    if model == "refs_prob":
        return list(REFS_PROB_PRODUCTS)
    return list(WARM_PRODUCTS)


def legend_rows(product: str):
    """[(swatch_hex, text)] for a page-side legend, low to high."""
    sc = scale_for(product)
    lv, co, out = sc["levels"], sc["colors"], []
    if sc["invert"]:
        # Bands read as "below the first level", then between.
        out.append((co[0], f"< {lv[0]:g} {sc['units']}"))
        for i in range(1, len(lv)):
            out.append((co[i], f"{lv[i - 1]:g}\u2013{lv[i]:g} "
                               f"{sc['units']}"))
    else:
        for i, v in enumerate(lv):
            hi = lv[i + 1] if i + 1 < len(lv) else None
            out.append((co[i], f"{v:g}\u2013{hi:g} {sc['units']}"
                        if hi else f"{v:g}+ {sc['units']}"))
    return out


def bounds():
    """deck.gl BitmapLayer order: [west, south, east, north]."""
    return [DOMAIN["west"], DOMAIN["south"],
            DOMAIN["east"], DOMAIN["north"]]


def _center_zoom():
    """hrrr_cam fetches by centre + half-width, so convert."""
    lat = (DOMAIN["south"] + DOMAIN["north"]) / 2.0
    lon = (DOMAIN["west"] + DOMAIN["east"]) / 2.0
    # Half-width in DEGREES OF LATITUDE, which is what zoom_deg means
    # to fetch_field. Longitude is wider in degrees at this latitude,
    # so take the larger of the two or the east and west edges get
    # clipped.
    half_lat = (DOMAIN["north"] - DOMAIN["south"]) / 2.0
    half_lon = (DOMAIN["east"] - DOMAIN["west"]) / 2.0
    return lat, lon, max(half_lat, half_lon)


# ---------------------------------------------------------------------------
# Fast frame path
# ---------------------------------------------------------------------------
# Profiling on the BlueMet warmer found essentially all render time in
# one call: contourf on an upsampled grid at 6.74 s, against 0.46 s
# for a cached basemap plus a LUT-coloured data layer — 14.6x. This is
# that approach, minus the basemap.
#
# THIS OVERLAY HAS NO MAP FURNITURE TO COMPOSITE. Coastlines, borders
# and gridlines come from the deck.gl basemap underneath, so the whole
# cached-basemap half of the original is unnecessary here and the
# frame is just the coloured field on transparency. That makes this
# path cheaper than the one it was ported from.
#
# What stays expensive is building the source-to-target index, a
# cKDTree query at ~2.7 s. But THE GRID GEOMETRY NEVER CHANGES —
# the domain is fixed and a model's native grid is constant — so it
# is built once per model and reused for every hour of every cycle
# of every product.
_INDEX_CACHE = {}
_LUT_CACHE = {}
FAST = os.environ.get("OVL_FAST", "on").lower() != "off"


def _lut_for(product: str):
    """RGBA table for a product. Index 0 is transparent.

    Built from the SAME `SCALES` entry the matplotlib path uses, so
    the two renderers cannot drift — a frame produced by either must
    be indistinguishable, because they are served from one store and
    nothing records which path made it.
    """
    import numpy as np

    hit = _LUT_CACHE.get(product)
    if hit is not None:
        return hit
    sc = scale_for(product)
    cols = list(sc["colors"])
    lut = np.zeros((len(cols) + 2, 4), dtype="uint8")
    for i, h in enumerate(cols):
        h = h.lstrip("#")
        lut[i + 1] = (int(h[0:2], 16), int(h[2:4], 16),
                      int(h[4:6], 16), 255)
    # digitize can return len(bounds); that band shares the top
    # colour rather than falling off the end of the table.
    lut[-1] = lut[-2]
    _LUT_CACHE[product] = lut
    return lut


def _regrid_index(key: str, lats, lons, width: int, height: int):
    """Source cell index for every output pixel, cached by `key`.

    `key` must identify the MODEL GRID. Two models on different
    native grids need different index maps even for the same domain,
    and reusing one silently plots a field through the wrong
    geometry — an image that looks entirely plausible and is wrong
    everywhere.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    hit = _INDEX_CACHE.get(key)
    if hit is not None and hit[1] == (width, height):
        return hit[0]
    la = np.asarray(lats, dtype="float64")
    lo = np.asarray(lons, dtype="float64")
    if la.ndim == 1:
        lo, la = np.meshgrid(lo, la)
    lo = np.where(lo > 180.0, lo - 360.0, lo)
    gx = np.linspace(DOMAIN["west"], DOMAIN["east"], width)
    # Image rows run NORTH to SOUTH; the BitmapLayer places by
    # corners and expects the same order.
    gy = np.linspace(DOMAIN["north"], DOMAIN["south"], height)
    GX, GY = np.meshgrid(gx, gy)
    tree = cKDTree(np.column_stack([lo.ravel(), la.ravel()]))
    _d, idx = tree.query(np.column_stack([GX.ravel(), GY.ravel()]),
                         k=1, workers=-1)
    _INDEX_CACHE[key] = (idx, (width, height))
    return idx


def render_overlay_fast(vals, lats, lons, dest: Path, product: str,
                        grid_key: str, smooth: float = 1.2) -> Path:
    """Field only, transparent, no matplotlib. Writes a PNG."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    sc = scale_for(product)
    # A scale may carry its own blur (the probability fields ask for
    # less); otherwise the caller's default applies.
    smooth = float(sc.get("smooth", smooth))
    width = int((DOMAIN["east"] - DOMAIN["west"]) / STEP_DEG)
    height = int((DOMAIN["north"] - DOMAIN["south"]) / STEP_DEG)
    idx = _regrid_index(grid_key, lats, lons, width, height)

    g = np.asarray(vals, dtype="float32").ravel()[idx].reshape(
        height, width)
    # DISPLAY UNITS FIRST. Levels and masks are both expressed in
    # them; converting after masking compares metres to a threshold
    # in feet and paints the whole domain.
    if sc["convert"] is not None:
        g = sc["convert"](g)

    # MASK BEFORE SMOOTHING. On visibility and ceiling the masked
    # region is the SAFE region, and a blur applied first drags
    # masked values into neighbours — painting hazard where there is
    # none. That is a dangerous artefact, not a cosmetic one.
    blank = ~np.isfinite(g)
    if sc.get("mask_below") is not None:
        blank |= g < float(sc["mask_below"])
    if sc.get("mask_above") is not None:
        blank |= g > float(sc["mask_above"])

    if smooth:
        # nan-aware: blur the field and the coverage together, then
        # divide, so masked cells contribute nothing rather than
        # pulling values toward zero.
        num = ndi.gaussian_filter(
            np.where(blank, 0.0, g).astype("float32"), smooth)
        den = ndi.gaussian_filter(
            (~blank).astype("float32"), smooth)
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.where(den > 0.02, num / np.maximum(den, 1e-6),
                         np.nan)
        blank = ~np.isfinite(g) | (den <= 0.02)

    # Smooth the FIELD, quantise after. Blurring a quantised image
    # invents colours corresponding to no value.
    band = np.digitize(np.nan_to_num(g, nan=-1e9),
                       np.array(sc["levels"], dtype=float)
                       ).astype("uint8")
    if sc["invert"]:
        # Bands read as "below the first level" upward, so digitize's
        # 0 is a real band rather than "nothing".
        band = band + 1
    band[blank] = 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_lut_for(product)[band], mode="RGBA").save(
        dest, "PNG", optimize=True)
    del g, band, blank
    return dest


def render_overlay(vals, lats, lons, dest: Path,
                   product: str = "REFD") -> Path:
    """Field only, transparent background, plain lat/lon.

    No axes, no frame, no colorbar, no map features. The figure is
    sized so one pixel is one grid step, and the axes fill it edge to
    edge — anything else would put a margin in the image and the
    BitmapLayer would place the data slightly wrong.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from scipy.interpolate import griddata

    nx = int((DOMAIN["east"] - DOMAIN["west"]) / STEP_DEG)
    ny = int((DOMAIN["north"] - DOMAIN["south"]) / STEP_DEG)
    gx = np.linspace(DOMAIN["west"], DOMAIN["east"], nx)
    gy = np.linspace(DOMAIN["south"], DOMAIN["north"], ny)
    GX, GY = np.meshgrid(gx, gy)

    lons_w = np.where(lons > 180, lons - 360, lons)
    pts = np.column_stack([lons_w.ravel(), lats.ravel()])
    grid = griddata(pts, np.asarray(vals, dtype="float32").ravel(),
                    (GX, GY), method="linear")
    # UNITS FIRST, then masking. Converting after the mask would
    # compare metres against a threshold in feet and paint the whole
    # domain — a failure that looks like a plausible field.
    sc = scale_for(product)
    if sc["convert"] is not None:
        grid = sc["convert"](grid)
    lv = np.array(sc["levels"], dtype=float)

    if sc.get("mask_below") is not None:
        grid = np.where(np.isfinite(grid)
                        & (grid >= sc["mask_below"]), grid, np.nan)
    if sc.get("mask_above") is not None:
        # Visibility and ceiling: the interesting end is the SMALL
        # numbers, so everything comfortably VFR is transparent. A
        # map painted green edge to edge hides the three places that
        # matter.
        grid = np.where(np.isfinite(grid)
                        & (grid <= sc["mask_above"]), grid, np.nan)

    cols = sc["colors"]
    if sc["invert"]:
        # Bands are "below the first level" then between levels, so
        # there is one more colour than gaps between boundaries.
        cmap = ListedColormap(cols[1:len(lv)])
        cmap.set_under(cols[0])
    else:
        cmap = ListedColormap(cols[:len(lv) - 1])
        cmap.set_over(cols[-1])
    cmap.set_bad(alpha=0.0)
    norm = BoundaryNorm(lv, cmap.N)

    fig = plt.figure(figsize=(nx / 100.0, ny / 100.0), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.pcolormesh(gx, gy, np.ma.masked_invalid(grid),
                  cmap=cmap, norm=norm, shading="auto")
    ax.set_xlim(DOMAIN["west"], DOMAIN["east"])
    ax.set_ylim(DOMAIN["south"], DOMAIN["north"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest, format="png", transparent=True,
                bbox_inches=None, pad_inches=0)
    plt.close(fig)
    _quantise(dest)
    return dest


def _quantise(path: Path):
    """PNG8 with an adaptive palette. The ramp has 15 colours, so
    this is visually lossless and roughly quarters the file — which
    matters because every byte crosses the wire on each pan."""
    try:
        from PIL import Image

        im = Image.open(path).convert("RGBA")
        alpha = im.getchannel("A")
        q = im.convert("RGB").quantize(colors=32,
                                       method=Image.MEDIANCUT)
        q = q.convert("RGBA")
        q.putalpha(alpha)
        q.save(path, format="PNG", optimize=True)
    except Exception:
        pass          # an unquantised PNG is still correct


# PRODUCT PER MODEL. refs_* publish composite reflectivity (REFC)
# and no REFD, so a single module-wide product makes REFS fail with
# "does not provide REFD". REFD (1 km AGL) is preferred where it
# exists because it is closer to what a radar mosaic shows.
def product_for(model: str) -> str:
    from core import hrrr_cam as HC

    want = os.environ.get("OVL_PRODUCT", "REFD")
    have = HC.MODELS.get(model, {}).get("products", ())
    for p in (want, "REFD", "REFC"):
        if p in have:
            return p
    raise RuntimeError(f"{model} publishes no reflectivity product")


def frame_name(model: str, cycle, fhr: int,
               product: str = None) -> str:
    """PRODUCT IS PART OF THE NAME. Without it, reflectivity,
    visibility and gusts for the same hour all write to one file and
    silently overwrite each other — whichever rendered last wins and
    the page shows it under whatever label was selected."""
    c = cycle.strftime("%Y%m%d%H") if hasattr(cycle, "strftime") \
        else str(cycle)
    p = product or PRODUCT
    return f"ovl_{model}_{p}_{c}_f{fhr:02d}.png"


def build_frame(model: str, cycle, fhr: int, outdir: Path,
                product: str = None):
    """One overlay frame. Returns (path, note); skips if it exists."""
    from core import hrrr_cam as HC

    dest = Path(outdir) / frame_name(model, cycle, fhr,
                                     product or product_for(model))
    if dest.exists():
        return dest, "cached"
    lat, lon, zoom = _center_zoom()
    prod = product or product_for(model)
    vals, lats, lons = HC.fetch_and_decode(
        model, prod, cycle, fhr, lat, lon, zoom)
    # GATE IMPACT rides on the decode: while the REFS array is in
    # hand, the maximum within 10 nm of every arrival gate is written
    # to the cycle's impact file. No extra fetch. Reflectivity from
    # PMMN, confidence from the 40 dBZ probability.
    # REFS: PMMN composite plus the 40 dBZ probability. RRFS: its 1 km
    # reflectivity, a deterministic second opinion out to f84.
    if (prod in ("REFC", "PROB_REFC40") and model.startswith("refs")) \
            or (prod == "REFD" and model == "rrfs"):
        try:
            from core import gate_impact as _GIMP

            _GIMP.sample(vals, lats, lons, prod, cycle, fhr, outdir, outdir,
                         source="rrfs" if model == "rrfs" else "refs")
        except Exception:
            pass
    # FAST PATH BY DEFAULT. matplotlib is kept as a fallback rather
    # than deleted: if the LUT path ever produces something wrong,
    # OVL_FAST=off restores a known-good renderer without a deploy.
    if FAST:
        try:
            render_overlay_fast(vals, lats, lons, dest, prod,
                                grid_key=model)
            return dest, f"fast {dest.stat().st_size // 1024} KB"
        except Exception as exc:
            _log(outdir, f"fast render failed, falling back: "
                         f"{type(exc).__name__}: {exc}")
    render_overlay(vals, lats, lons, dest, prod)
    return dest, f"built {dest.stat().st_size // 1024} KB"


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_started = False

# ---------------------------------------------------------------------------
# Yield to page loads
# ---------------------------------------------------------------------------
# The warmer is a daemon thread in the SAME process as the request
# handler, so a render holding the GIL blocks a page load. Scheduling
# cannot fix that; yielding can.
#
# Every page calls note_request() as its first action. Before each
# frame the warmer checks that timestamp and waits while the site was
# used in the last QUIET_S, backing off up to MAX_BACKOFF_S. Someone
# scrubbing the forecast page holds the warmer off; the moment they
# stop, it resumes.
#
# This matters far less than it used to. A 0.13 s GIL hold between
# yields is invisible in a page load; the 3.5 s matplotlib one was
# the entire problem. Kept because cold starts still queue hundreds
# of frames back to back.
_LAST_REQUEST = [0.0]
QUIET_S = float(os.environ.get("OVL_QUIET_S", "4"))
MAX_BACKOFF_S = float(os.environ.get("OVL_MAX_BACKOFF_S", "25"))
YIELD_S = float(os.environ.get("OVL_YIELD_S", "0.05"))


def note_request() -> None:
    """Called by pages on render. Cheap enough for every rerun."""
    _LAST_REQUEST[0] = time.time()


def _wait_for_quiet() -> float:
    """Block while the site is in use. Returns seconds waited."""
    waited = 0.0
    while waited < MAX_BACKOFF_S:
        if time.time() - _LAST_REQUEST[0] >= QUIET_S:
            return waited
        time.sleep(0.5)
        waited += 0.5
    return waited
WARM_MODELS = [m.strip() for m in
               os.environ.get("OVL_MODELS", "hrrr").split(",") if m.strip()]
SLEEP_S = int(os.environ.get("OVL_SLEEP_S", "300"))


def _log(outdir, msg):
    try:
        with open(Path(outdir) / "overlay_warmer.log", "a") as fh:
            fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                     f"{msg}\n")
    except OSError:
        pass


def _heavy():
    """The process-wide heavy-build lock if core/warmlock.py exists,
    else a no-op — this module must not hard-depend on that file."""
    try:
        from core.warmlock import holding
        return holding("CAM overlay")
    except Exception:
        import contextlib
        return contextlib.nullcontext(True)


def _daemon(outdir):
    from core import hrrr_cam as HC

    # Staggered: the CAM warmer goes first (it has the longest job),
    # this one second, the radar warmer last. Simultaneous starts
    # OOM-killed the service.
    time.sleep(float(os.environ.get("OVL_DELAY_S", "120")))
    ceiling = float(os.environ.get("OVL_MEM_CEILING_MB", "2200"))

    def _rss():
        try:
            import resource
            return resource.getrusage(
                resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            return 0.0

    _log(outdir, f"overlay warmer started, models={WARM_MODELS}, "
                 f"products={ {m: products_for(m) for m in WARM_MODELS} }")

    def _warm(model, cycle, lo, hi, tag):
        """Render every product across a run. Returns (built, note).

        PRODUCT-MAJOR, NOT HOUR-MAJOR: all of REFD before any of VIS.
        The memory ceiling can cut a pass short at any point, and a
        complete reflectivity run with no visibility is far more
        useful than five products that all stop at f06.
        """
        built = 0
        # Probability fields are window statistics with no f00; the
        # model definition carries its own floor.
        lo = max(lo, int(HC.MODELS[model].get("min_fhr", 0)))
        for prod in products_for(model):
            if prod not in HC.MODELS[model].get("products", ()):
                continue
            for fhr in range(lo, hi + 1):
                if _rss() > ceiling:
                    return built, (f"{tag}: stopped at {prod} "
                                   f"f{fhr:02d}, RSS {_rss():.0f} MB")
                try:
                    _wait_for_quiet()
                    # UNDER THE HEAVY LOCK: a GRIB decode of one CAM
                    # forecast hour is a few hundred MB, and these ran
                    # unserialised alongside the MRMS build. One heavy
                    # thing at a time is what a 2 GB box can hold.
                    with _heavy():
                        _, note = build_frame(model, cycle, fhr, outdir,
                                              prod)
                    if note != "cached":
                        built += 1
                        if YIELD_S:
                            time.sleep(YIELD_S)
                except Exception as exc:
                    _log(outdir, f"{model} {prod} f{fhr:02d} {tag}: "
                                 f"{type(exc).__name__}: {exc}")
        return built, ""

    while True:
        # TWO SWEEPS, NOT ONE. Every model's short run first, then
        # every model's long run. Model-by-model, HRRR's f48 and
        # RRFS's f84 — 480 frames — came before REFS had its first
        # turn, and a REFS 36-hour outlook is worth more than RRFS
        # hour 84. Short runs are the freshest data for every model
        # and now all land within the first pass.
        cycles = {}
        for phase in ("short", "long"):
            for model in WARM_MODELS:
                if _rss() > ceiling:
                    _log(outdir, f"{model}: SKIPPED, RSS {_rss():.0f} MB "
                                 f"over {ceiling:.0f} MB")
                    continue
                short = SHORT_FHR.get(model, MAX_FHR)
                long_h = LONG_FHR.get(model)
                cycle, lc = cycles.get(model, (None, None))
                try:
                    if phase == "short":
                        # COMPLETE runs only — newest_complete asks for
                        # the LAST hour, so a run that has only reached
                        # f02 does not replace a finished one with two
                        # useful frames.
                        cycle = HC.newest_complete(model, short)
                        if cycle is None:
                            _log(outdir, f"{model}: no cycle complete to "
                                         f"f{short:02d} yet")
                        else:
                            t0 = time.time()
                            n, note = _warm(model, cycle, 0, short, "short")
                            if n or note:
                                _log(outdir,
                                     f"{model} short {cycle:%Y%m%d%H}: "
                                     f"{n} frame(s), {time.time() - t0:.0f}s"
                                     + (f" \u2014 {note}" if note else ""))
                    elif long_h and long_h > short:
                        # LONG RUN. Only the synoptic cycles go deep.
                        lc = HC.newest_complete(model, long_h)
                        if lc is not None and lc.hour in LONG_CYCLES:
                            t0 = time.time()
                            # From f00: the early hours were warmed as
                            # the short run and build_frame skips what
                            # exists, so this costs nothing when they
                            # are present and completes the run when
                            # they are not.
                            n, note = _warm(model, lc, 0, long_h, "long")
                            if n or note:
                                _log(outdir,
                                     f"{model} long {lc:%Y%m%d%H} "
                                     f"f{short + 1:02d}-f{long_h:02d}: "
                                     f"{n} frame(s), "
                                     f"{time.time() - t0:.0f}s"
                                     + (f" \u2014 {note}" if note else ""))
                    cycles[model] = (cycle, lc)
                except Exception as exc:
                    _log(outdir, f"{model} {phase}: {type(exc).__name__}: {exc}")
        # PRUNE, once both sweeps are done. Keep the newest short run
        # and the newest long run for every product; everything older
        # goes. Frames are small, but five products across f84 is 425
        # of them per cycle, so this is not optional.
        for model in WARM_MODELS:
            cycle, lc = cycles.get(model, (None, None))
            short = SHORT_FHR.get(model, MAX_FHR)
            long_h = LONG_FHR.get(model)
            keep = set()
            for prod in products_for(model):
                for c, hi in ((cycle, short), (lc if long_h else None, long_h)):
                    if c is None:
                        continue
                    keep |= {frame_name(model, c, f, prod)
                             for f in range(0, (hi or short) + 1)}
            if not keep:
                continue
            for old in Path(outdir).glob(f"ovl_{model}_*.png"):
                if old.name not in keep:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        time.sleep(SLEEP_S)


def ensure_overlay_warmer(outdir) -> str:
    """Idempotent. OVL_WARMER=off disables without a deploy.

    RETURNS WHAT IT DID so the caller can report honestly. A None
    return let Homepage print "started" while the kill switch had
    silently returned here — the exact failure that hid a disabled
    MRMS warmer for a day.
    """
    # DEFAULT ON, and this is the ONLY place OVL_WARMER is read. The
    # previous build had the call site defaulting to "on" and this
    # function defaulting to "off", so an unset variable meant the
    # warmer never ran while the page reported it started.
    if os.environ.get("OVL_WARMER", "on").lower() == "off":
        return "DISABLED by OVL_WARMER=off in the environment"
    global _started
    with _lock:
        if _started:
            return "already running"
        threading.Thread(target=_daemon, args=(outdir,), daemon=True,
                         name="cam-overlay-warmer").start()
        _started = True
        return "started"


# FILENAMES ARE PARSED BY REGEX, NOT BY SPLITTING ON "_".
#
# frame_name builds ovl_{model}_{cycle}_f{hh}.png, and a model key can
# itself contain an underscore — refs_mean, refs_pmmn, nam_nest,
# hiresw_fv3. Splitting on "_" and taking field 2 reads the cycle as
# "mean" for every REFS frame, so the forecast-hour list comes back
# empty and the page reports no frames while they sit on disk.
def _scan(model: str, outdir, product: str = None):
    """[(cycle, fhr)] for every frame of `model`/`product` on disk."""
    product = product or PRODUCT
    pat = re.compile(
        rf"^ovl_{re.escape(model)}_{re.escape(product)}_"
        r"(\d{10})_f(\d{2,3})\.png$")
    out = []
    for h in Path(outdir).glob(f"ovl_{model}_{product}_*.png"):
        m = pat.match(h.name)
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def cycles_on_disk(model: str, outdir, product: str = None) -> list:
    """Cycles present, newest first. Each is a YYYYMMDDHH string."""
    return sorted({c for c, _f in _scan(model, outdir, product)},
                  reverse=True)


def available(model: str, outdir, cycle: str = None,
              product: str = None) -> list:
    """Forecast hours on disk for a cycle (newest if unspecified)."""
    rows = _scan(model, outdir, product)
    if not rows:
        return []
    want = cycle or max(c for c, _f in rows)
    return sorted(f for c, f in rows if c == want)


def cycle_on_disk(model: str, outdir, product: str = None):
    cyc = cycles_on_disk(model, outdir, product)
    return cyc[0] if cyc else None


def long_cycles(model: str, outdir, product: str = None) -> list:
    """Cycles on disk that reached past the short-run ceiling.

    HRRR runs hourly to f18 but the 00/06/12/18Z cycles go to f48;
    RRFS runs 3-hourly with the synoptic cycles reaching f84. A page
    offering "latest long run" needs to know which cycles those are,
    and the only honest test is how deep the frames actually go.
    """
    rows = _scan(model, outdir, product)
    if not rows:
        return []
    short = SHORT_FHR.get(model, MAX_FHR)
    deep = {}
    for c, f in rows:
        deep[c] = max(deep.get(c, -1), f)
    return sorted([c for c, mx in deep.items() if mx > short],
                  reverse=True)
