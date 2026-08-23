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
PRODUCT = os.environ.get("OVL_PRODUCT", "REFD")

# Same AWIPS ramp the radar mosaic uses, so a forecast frame and an
# observation frame read identically. 5 dBZ steps from 5 to 75.
LEVELS = list(range(5, 80, 5))
COLORS = [
    "#04E9E7", "#019FF4", "#0300F4", "#02FD02", "#01C501",
    "#008E00", "#FDF802", "#E5BC00", "#FD9500", "#FD0000",
    "#D40000", "#BC0000", "#F800FD", "#9854C6", "#FDFDFD",
]
DBZ_MIN = float(os.environ.get("OVL_DBZ_MIN", "5"))


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


def render_overlay(vals, lats, lons, dest: Path) -> Path:
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
    grid = np.where(np.isfinite(grid) & (grid >= DBZ_MIN),
                    grid, np.nan)

    cmap = ListedColormap(COLORS[:len(LEVELS) - 1])
    cmap.set_over(COLORS[-1])
    cmap.set_bad(alpha=0.0)
    norm = BoundaryNorm(np.array(LEVELS, dtype=float), cmap.N)

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


def frame_name(model: str, cycle, fhr: int) -> str:
    c = cycle.strftime("%Y%m%d%H") if hasattr(cycle, "strftime") \
        else str(cycle)
    return f"ovl_{model}_{c}_f{fhr:02d}.png"


def build_frame(model: str, cycle, fhr: int, outdir: Path):
    """One overlay frame. Returns (path, note); skips if it exists."""
    from core import hrrr_cam as HC

    dest = Path(outdir) / frame_name(model, cycle, fhr)
    if dest.exists():
        return dest, "cached"
    lat, lon, zoom = _center_zoom()
    vals, lats, lons = HC.fetch_and_decode(
        model, PRODUCT, cycle, fhr, lat, lon, zoom)
    render_overlay(vals, lats, lons, dest)
    return dest, f"built {dest.stat().st_size // 1024} KB"


# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_started = False
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


def _daemon(outdir):
    from core import hrrr_cam as HC

    _log(outdir, f"overlay warmer started, models={WARM_MODELS}, "
                 f"f00-f{MAX_FHR:02d}")
    while True:
        for model in WARM_MODELS:
            try:
                cycle = HC.latest_cycle(model)
                t0 = time.time()
                built = 0
                for fhr in range(0, MAX_FHR + 1):
                    try:
                        _, note = build_frame(model, cycle, fhr, outdir)
                        if note != "cached":
                            built += 1
                    except Exception as exc:
                        _log(outdir, f"{model} f{fhr:02d}: "
                                     f"{type(exc).__name__}: {exc}")
                # Prune anything not from the current cycle.
                keep = {frame_name(model, cycle, f)
                        for f in range(0, MAX_FHR + 1)}
                for old in Path(outdir).glob(f"ovl_{model}_*.png"):
                    if old.name not in keep:
                        try:
                            old.unlink()
                        except OSError:
                            pass
                _log(outdir, f"{model} {cycle:%Y-%m-%d %HZ}: "
                             f"{built} new in {time.time() - t0:.0f}s")
            except Exception as exc:
                _log(outdir, f"{model} FAILED: "
                             f"{type(exc).__name__}: {exc}")
        time.sleep(SLEEP_S)


def ensure_overlay_warmer(outdir) -> None:
    """Idempotent. OVL_WARMER=off disables without a deploy."""
    if os.environ.get("OVL_WARMER", "on").lower() == "off":
        return
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_daemon, args=(outdir,), daemon=True,
                         name="cam-overlay-warmer").start()
        _started = True


def available(model: str, outdir) -> list:
    """Forecast hours already on disk, newest cycle only."""
    out = Path(outdir)
    hits = sorted(out.glob(f"ovl_{model}_*.png"))
    if not hits:
        return []
    newest = max(h.name.split("_")[2] for h in hits)
    return sorted(int(h.name.split("_f")[1][:2]) for h in hits
                  if h.name.split("_")[2] == newest)


def cycle_on_disk(model: str, outdir):
    hits = sorted(Path(outdir).glob(f"ovl_{model}_*.png"))
    if not hits:
        return None
    return max(h.name.split("_")[2] for h in hits)
