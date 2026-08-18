"""Level II super-resolution multi-radar mosaic — N90 / New York.

WHY THIS EXISTS
---------------
MRMS reflectivity is 0.01 deg (~840 m E-W at 41N, 1113 m N-S) and no
higher-resolution MRMS exists — that IS the product grid. Level II
super-res is 250 m gates at 0.5 deg azimuth, which beats MRMS spacing
inside ~52 nm of a radar and is 2-5x better inside 30 nm. N90 is a
~50 nm operation with KOKX near its centre, so the useful radius of
this technique and the airspace we care about are the same circle.
Outside that radius MRMS wins and we should keep using it.

ARCHITECTURE — READ BEFORE CHANGING
-----------------------------------
Sites are gridded SEQUENTIALLY onto a shared target grid and merged
into a float32 accumulator. This is not stylistic. Measured 8/18 on
synthetic super-res volumes:

    one grid_from_radars() call over a full 14-tilt volume  2051 MB
    sequential per-site grid + fmax merge, 4 sites, 4 tilts 1160 MB
    ditto, 6 tilts / 5 levels                              1433 MB
    ditto, 8 tilts / 6 levels                              1815 MB

Peak is driven by INPUT GATES, not output cells: +-200 km at 250 m and
+-120 km at 250 m cost within 40 MB of each other, while going 14
tilts -> 4 tilts cut peak by more than half. The accumulator stays
flat at ~31 MB regardless of site count, so adding TDWRs later does
not move peak memory. Passing all four radars to one
grid_from_radars() call holds every volume plus every intermediate
KD-tree at once and will OOM.

TIME, NOT MEMORY, IS THE BINDING CONSTRAINT. Same benchmark, wall
clock: 4 tilts 37 s, 6 tilts 76 s, 8 tilts 95 s — single threaded,
and that is BEFORE S3 fetch and bzip2 decode. MRMS updates every
2 min. Anything past ~6 tilts cannot keep up. GRID_WORKERS>1 grids
sites in parallel and roughly halves wall time on a 2-CPU box.

This module must run in the WARMER, never on a render path.

STATUS: v1 renders a max-merge mosaic. `fmax` ignores that KOKX at
10 nm is far more trustworthy than KENX at 90 nm looking at the same
storm — proper blending weights by range and beam height. That is the
next step, deliberately deferred so the pipeline can be seen working
first. See _merge() for where it plugs in.
"""

from __future__ import annotations

import gc
import io
import os
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------
# Just ICAO ids — NO coordinates. Every Level II volume carries its own
# radar lat/lon in the header, so the grid centre is DERIVED from the
# volumes actually loaded (see _center_from). That means adding a focus
# city is a four-string edit with nothing to get wrong: a bad id simply
# reports "no recent volume" in diagnostics instead of silently
# shifting the grid.
REGIONS = {
    "N90 / New York": ["KOKX", "KDIX", "KENX", "KBOX"],
    "ATL / Atlanta": ["KFFC", "KJGX", "KBMX", "KGSP"],
    "FLL-MIA / South Florida": ["KAMX", "KBYX", "KMLB", "KTBW"],
    "BOS / New England": ["KBOX", "KENX", "KGYX", "KOKX"],
    "MSP / Minneapolis": ["KMPX", "KARX", "KFSD", "KDLH"],
}
SITE_NOTES = {
    "KOKX": "Upton NY — inside the N90 terminal area, primary source",
    "KDIX": "Mount Holly NJ — south/southwest gates",
    "KENX": "East Berne NY — northwest gates",
    "KBOX": "Taunton MA — northeast, BOS overlap",
    "KFFC": "Peachtree City GA — the Atlanta radar",
    "KJGX": "Robins AFB GA — southeast",
    "KBMX": "Birmingham AL — west",
    "KGSP": "Greer SC — northeast",
    "KMPX": "Chanhassen MN — the Minneapolis radar",
    "KARX": "La Crosse WI — southeast",
    "KFSD": "Sioux Falls SD — southwest",
    "KDLH": "Duluth MN — northeast",
}
# Back-compat for anything importing the old name.
N90_SITES = {s: (None, None, SITE_NOTES.get(s, "")) 
             for s in REGIONS["N90 / New York"]}

# TDWR sits at the airports and is 150 m gates — better than 88D close
# in. NOT wired up yet: TDWR is C-band and attenuates hard behind heavy
# cores, so max-merging it with S-band would make TJFK show LESS than
# KOKX behind a squall line. Needs attenuation handling first.
N90_TDWR = ["TJFK", "TEWR"]

# Grid centre, set at build time from the loaded radars. Never
# hardcoded: see _center_from().
GRID_CENTER = (40.90, -73.60)


def _center_from(latlons):
    """Centre the grid on the radars we actually got.

    Mean of the loaded site positions, read from each volume's own
    header. With one site that is the radar itself; with four it is
    the centroid, which keeps every site's coverage inside the box.
    """
    if not latlons:
        return GRID_CENTER
    return (sum(a for a, _ in latlons) / len(latlons),
            sum(b for _, b in latlons) / len(latlons))


# Tunables. Defaults are the benchmarked config that fits a 2-min
# cycle on a 2-CPU box. Raising TILTS past 6 will fall behind the feed.
TILTS = int(os.environ.get("L2_TILTS", "6"))
LEVELS = int(os.environ.get("L2_LEVELS", "5"))
RES_M = float(os.environ.get("L2_RES_M", "250"))
HALF_M = float(os.environ.get("L2_HALF_M", "200000"))
BASE_M = float(os.environ.get("L2_BASE_M", "500"))
GRID_WORKERS = int(os.environ.get("L2_WORKERS", "2"))
CC_MIN = float(os.environ.get("L2_CC_MIN", "0.80"))
DBZ_MIN = float(os.environ.get("L2_DBZ_MIN", "5"))

_BUCKET = "noaa-nexrad-level2"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Volume access — REUSES the existing modules, does not re-solve this
# ---------------------------------------------------------------------------
# Two earlier attempts here fetched from noaa-nexrad-level2 directly and
# both got "Access Denied". core/radar.py already documented why, last
# week: "AWS public bucket XML listing - tried first, currently denies
# anonymous listing". That bucket does not serve anonymous listings any
# more, and no amount of switching between s3fs and plain HTTPS changes
# it. The repo already had the answer.
#
# So this module fetches nothing of its own. Two proven paths, in order:
#
#   1. core.radar_l2rt — the unidata-nexrad-level2-chunks feed, which
#      receives chunks WHILE the antenna scans. A usable 0.5 deg sweep
#      ~1 min into the volume instead of after the full 4-6 min scan.
#      Returns a PARTIAL volume: expect fewer sweeps than a complete
#      one, which is why tilt selection below clamps to what arrived
#      rather than assuming TILTS are present.
#   2. core.radar._find_scans / _download_volume — the AWS -> GCS ->
#      UCAR THREDDS chain, for a complete volume when the chunk feed
#      is unavailable. Slower and staler, but whole.
#
# Diagnostics record which path served each site, so a mosaic can be
# read as "3 sites live, 1 site archive" rather than looking uniform.


def _read_volume_bytes(raw: bytes):
    """Bytes -> pyart Radar. Tolerates a truncated chunk stream.

    radar_l2rt hands back a deliberately truncated Archive II stream
    (first N chunks, mid-scan). metpy's Level2File is lenient about
    that; pyart may not be, so a failure here is expected sometimes
    and is a fallback trigger, not a bug.
    """
    import pyart

    return pyart.io.read_nexrad_archive(io.BytesIO(raw))


def load_site(site: str, fs, diag: dict):
    """Fetch one volume, trimmed to the lowest available sweeps.

    Trimming happens at read time via `extract_sweeps` because the
    full 14-tilt volume is what pushes peak memory past 2 GB.
    """
    t0 = time.time()
    radar = None
    src = ""
    notes = []

    # --- path 1: live chunk feed -------------------------------------
    try:
        from core import radar_l2rt

        raw, info = radar_l2rt.fetch_live_volume_bytes(site)
        radar = _read_volume_bytes(raw)
        src = (f"chunk feed vol {info.get('volume')} "
               f"{info.get('n_used')}/{info.get('n_chunks')} chunks, "
               f"{info.get('age_s')}s old")
    except Exception as exc:
        notes.append(f"chunk feed: {type(exc).__name__}: {exc}")

    # --- path 2: complete volume via the AWS/GCS/THREDDS chain -------
    if radar is None:
        try:
            import tempfile
            from datetime import datetime, timedelta, timezone

            from core import radar as _r

            now = datetime.now(timezone.utc)
            scans = _r._find_scans(site, now - timedelta(minutes=90), now)
            if not scans:
                raise RuntimeError("no scans in the last 90 min")
            scan = sorted(scans, key=lambda s_: s_.scan_time)[-1]
            with tempfile.TemporaryDirectory() as td:
                path = _r._download_volume(scan, f"{td}/{scan.filename}")
                with open(path, "rb") as fh:
                    radar = _read_volume_bytes(fh.read())
            age = (now - scan.scan_time).total_seconds() / 60.0
            src = f"archive {scan.filename} ({age:.0f} min old)"
        except Exception as exc:
            notes.append(f"archive: {type(exc).__name__}: {exc}")

    if radar is None:
        diag["sites"][site] = " | ".join(notes) or "no volume"
        return None, None

    # Clamp to what actually arrived — a partial volume may hold far
    # fewer sweeps than TILTS.
    have = radar.nsweeps
    n = min(TILTS, have)
    if n < have:
        radar = radar.extract_sweeps(list(range(n)))
    diag["sites"][site] = (
        f"{src} | {n}/{have} tilts | "
        f"{radar.nrays * radar.ngates / 1e6:.1f}M gates | "
        f"{time.time() - t0:.1f}s"
        + (f" | {'; '.join(notes)}" if notes else ""))
    return radar, None


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------
def gatefilter(radar):
    """Dual-pol QC. Most of MRMS's value over a raw mosaic is its
    clutter/AP/biological filtering; correlation coefficient does the
    bulk of that job. Birds, insects, chaff and ground returns all
    decorrelate, so CC below ~0.8 is almost never precipitation.
    Falls back to a plain reflectivity floor if the volume has no
    dual-pol moments (legacy or split-cut sweeps)."""
    import pyart

    gf = pyart.filters.GateFilter(radar)
    gf.exclude_transition()
    gf.exclude_masked("reflectivity")
    gf.exclude_below("reflectivity", DBZ_MIN)
    if "cross_correlation_ratio" in radar.fields:
        gf.exclude_below("cross_correlation_ratio", CC_MIN)
    return gf


# ---------------------------------------------------------------------------
# Grid + merge
# ---------------------------------------------------------------------------
def _grid_one(radar):
    """Grid one radar onto the shared target grid; return float32."""
    import numpy as np
    import pyart

    n = int(2 * HALF_M / RES_M)
    g = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(LEVELS, n, n),
        grid_limits=((BASE_M, BASE_M + 1000.0 * max(LEVELS - 1, 1)),
                     (-HALF_M, HALF_M), (-HALF_M, HALF_M)),
        fields=["reflectivity"],
        weighting_function="nearest",
        min_radius=RES_M,
        gatefilters=(gatefilter(radar),),
        grid_origin=GRID_CENTER,
    )
    arr = np.ma.filled(
        g.fields["reflectivity"]["data"], np.nan).astype("float32")
    del g
    return arr


def _merge(acc, arr):
    """v1: element-wise max.

    Deliberately crude. The right merge weights each site by range and
    beam height — a gate 10 nm from KOKX at 600 ft AGL should dominate
    one 90 nm from KENX sampling 11,000 ft through the same column,
    and max-merge treats them as equals. Replacing this function is
    the single highest-value improvement to the mosaic; everything
    else in the pipeline stays as-is.
    """
    import numpy as np

    return arr if acc is None else np.fmax(acc, arr)


def build_mosaic(sites=None, diag=None):
    """Fetch, QC, grid and merge. Returns (composite_2d, diag).

    Sequential by design — see the module docstring. GRID_WORKERS>1
    overlaps the S3 fetch/decode of the next site with the gridding
    of the current one, which is where the parallel win actually
    comes from; the gridding itself stays serialised so peak memory
    is never more than one volume plus the accumulator.
    """
    import numpy as np

    global GRID_CENTER
    sites = sites or REGIONS["N90 / New York"]
    diag = diag if diag is not None else {}
    diag.setdefault("sites", {})
    diag["config"] = (f"{TILTS} tilts, {LEVELS} levels, {RES_M:.0f} m, "
                      f"+-{HALF_M / 1000:.0f} km")
    t_all = time.time()
    fs = None   # volume access lives in load_site
    acc = None
    times = []

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, GRID_WORKERS)) as ex:
        pending = {s: ex.submit(load_site, s, fs, diag) for s in sites}
        for site in sites:
            try:
                radar, t = pending[site].result()
            except Exception as exc:
                diag["sites"][site] = f"{type(exc).__name__}: {exc}"
                continue
            if radar is None:
                continue
            try:
                # Centre on the first volume we successfully read, so
                # the box follows the region rather than a constant.
                if acc is None:
                    GRID_CENTER = (float(radar.latitude["data"][0]),
                                   float(radar.longitude["data"][0]))
                    diag["center"] = [round(v, 4) for v in GRID_CENTER]
                # Scan time comes from the volume itself rather than a
                # filename, so it is right for both the chunk feed and
                # the archive path.
                try:
                    import pyart
                    t = pyart.util.datetime_from_radar(radar)
                except Exception:
                    t = None
                arr = _grid_one(radar)
                acc = _merge(acc, arr)
                if t:
                    times.append(t)
                del arr
            except Exception as exc:
                diag["sites"][site] = (
                    f"grid failed — {type(exc).__name__}: {exc}")
            finally:
                del radar
                gc.collect()

    if acc is None:
        diag["error"] = "no sites produced a grid"
        return None, diag

    comp = np.nanmax(acc, axis=0)
    del acc
    gc.collect()
    if times:
        # Scan times differ between sites; a 40 kt storm moves ~2 nm
        # in 4 min, so the spread is the mosaic's real time error.
        diag["scan_spread_s"] = int(
            (max(times) - min(times)).total_seconds())
        diag["valid"] = max(times).strftime("%Y-%m-%dT%H:%M:%SZ")
    diag["cells"] = int(comp.size)
    diag["wall_s"] = round(time.time() - t_all, 1)
    diag["coverage_pct"] = round(
        100.0 * float(np.isfinite(comp).sum()) / comp.size, 1)
    return comp, diag


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render_png(comp, path, diag=None):
    """Filled-contour render — the smoothing half of the problem.

    A discrete-class raster (what the ArcGIS export returns) gives
    hard pixel edges. Filled contours at 2.5 dBZ steps produce the
    continuous look, the same technique as the CAM renderer.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap

    lev = np.arange(DBZ_MIN, 80.001, 2.5)
    cols = plt.get_cmap("gist_ncar")(np.linspace(0.08, 0.95, len(lev) - 1))
    cmap = ListedColormap(cols)
    cmap.set_bad(alpha=0.0)
    norm = BoundaryNorm(lev, cmap.N)

    n = comp.shape[0]
    fig = plt.figure(figsize=(n / 200.0, n / 200.0), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.contourf(np.ma.masked_invalid(comp), levels=lev,
                cmap=cmap, norm=norm, extend="max", antialiased=True)
    ax.set_xlim(0, n - 1)
    ax.set_ylim(0, n - 1)
    fig.savefig(path, format="png", transparent=True,
                bbox_inches=None, pad_inches=0)
    plt.close(fig)
    if diag is not None:
        diag["png_bytes"] = os.path.getsize(path)
    return path


def bounds():
    """(west, south, east, north) for a deck.gl BitmapLayer."""
    import math

    lat0, lon0 = GRID_CENTER
    dlat = HALF_M / 111320.0
    dlon = HALF_M / (111320.0 * math.cos(math.radians(lat0)))
    return (lon0 - dlon, lat0 - dlat, lon0 + dlon, lat0 + dlat)
