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
    # DCA-BUF-PWM needs eight sites, not four. Deselect on the page
    # to trade coverage for build time — each site is ~20 s.
    "N90 / New York": ["KOKX", "KDIX", "KENX", "KBOX",
                       "KLWX", "KBUF", "KTYX", "KGYX"],
    "ATL / Atlanta": ["KFFC", "KJGX", "KBMX", "KGSP"],
    "FLL-MIA / South Florida": ["KAMX", "KBYX", "KMLB", "KTBW"],
    "BOS / New England": ["KBOX", "KENX", "KGYX", "KOKX"],
    "MSP / Minneapolis": ["KMPX", "KARX", "KFSD", "KDLH"],
    "MCO / Orlando": ["KMLB", "KTBW", "KJAX", "KAMX"],
}
# Explicit view per region: (centre_lat, centre_lon, half_x_km,
# half_y_km). Centring on the first radar that loaded put MCO's box
# 73 km off-centre — its west edge landed on Leesburg (-81.876) and
# clipped the storms, while 184 km of grid sat over the Atlantic.
# Boxes are RECTANGULAR because the areas are: N90 spans DCA to BUF
# to PWM, which is 8.4 deg of longitude against 4.8 of latitude.
REGION_VIEW = {
    "N90 / New York": (41.25, -74.52, 355, 270),
    "ATL / Atlanta": (33.64, -84.43, 200, 200),
    "FLL-MIA / South Florida": (26.30, -80.60, 200, 220),
    "BOS / New England": (42.36, -71.01, 230, 200),
    "MSP / Minneapolis": (44.88, -93.22, 220, 220),
    "MCO / Orlando": (28.43, -81.31, 200, 180),
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
    "KLWX": "Sterling VA — the DCA/BWI/IAD radar",
    "KBUF": "Buffalo NY — western end",
    "KTYX": "Fort Drum NY — fills the Adirondack gap",
    "KGYX": "Gray ME — the PWM radar",
    "KMLB": "Melbourne FL — ~35 nm east of MCO, the primary "
            "Orlando-area radar and well inside the useful radius",
    "KTBW": "Ruskin FL — Tampa Bay, ~65 nm southwest",
    "KJAX": "Jacksonville FL — ~110 nm north",
    "KAMX": "Miami FL — ~180 nm south, edge of usefulness for MCO",
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
HALF_X_M = float(os.environ.get("L2_HALF_X_M", "200000"))
HALF_Y_M = float(os.environ.get("L2_HALF_Y_M", "200000"))
# Back-compat alias; setting HALF_M sets both axes.
HALF_M = HALF_X_M
BASE_M = float(os.environ.get("L2_BASE_M", "500"))
# Optional explicit grid top. Without it the vertical span is derived
# from LEVELS (1 km apart), which is right for a multi-tilt composite
# but wrong for a SINGLE sweep: the 0.5 deg beam climbs from 180 m at
# 10 nm to ~5,100 m at 125 nm, so a thin layer intersects it only in a
# narrow range ring. For a base-reflectivity plan view set LEVELS=1
# and TOP_M above the beam at max range, and every gate lands in the
# one level.
TOP_M = os.environ.get("L2_TOP_M")
TOP_M = float(TOP_M) if TOP_M else None
GRID_WORKERS = int(os.environ.get("L2_WORKERS", "2"))
CC_MIN = float(os.environ.get("L2_CC_MIN", "0.80"))
# Contiguous regions smaller than this are dropped as speckle.
SPECKLE = int(os.environ.get("L2_SPECKLE", "10"))
# Gridding weight. "nearest" gives each gate a wedge of cells, so past
# ~20 nm — where the 0.5 deg beam is wider than a 250 m cell — one
# sample paints a visible block and echo edges come out stair-stepped.
# Barnes2 weights several gates into every cell and dissolves that.
# Measured 8/18 on a realistic volume: Barnes2 costs ~20% more wall
# time than nearest (11.5 s vs 9.4 s) with identical peak memory —
# far cheaper than expected.
WEIGHT_FN = os.environ.get("L2_WEIGHT", "Barnes2")
# Radius-of-influence floor. This is the smoothing/peak trade: a
# planted 58 dBZ core survived intact at 500 and 1000 m but lost
# 2.6 dB at 2000 m. 1000 m smooths the blocks without eating cores.
MIN_RADIUS_M = float(os.environ.get("L2_MIN_RADIUS_M", "1000"))
# Render-time smoothing, in grid cells. Applied to the finished
# composite rather than by widening the gridding ROI, because the two
# have very different costs: measured 8/18 on a varying field
# quantised into gate-sized blocks, sigma=1.0 cut visible edges (
# neighbour steps >0.5 dB) from 4.6% of pairs to 0.1% with ZERO peak
# loss on a planted 58 dBZ core, while getting the same smoothing
# from the grid ROI (2000 m) cost 2.6 dB of that core. Above sigma=2
# peaks start to go (3.7 dB at sigma=3), so 1.0 is the sweet spot.
# At 250 m cells that is a 250 m smoothing length — well below the
# beam width it is hiding.
SMOOTH_SIGMA = float(os.environ.get("L2_SMOOTH_SIGMA", "1.0"))
# Range scale for blend weights. Default is the 52 nm crossover
# where beam spreading makes L2 coarser than MRMS = 96 km.
BLEND_M = float(os.environ.get("L2_BLEND_M", "96000"))
# Floor for BOTH the QC gate filter and the colour ramp — one knob, so
# what is gridded is what is shown. 15 dBZ is the operationally
# meaningful threshold for a terminal area: below it is drizzle,
# insects, chaff and ground return, which is also most of what the
# speckle was. Raising this floor does more for legibility than
# despeckle alone, and it makes the grid sparser and cheaper.
# 10 rather than 15: at 15 the light stratiform that MRMS draws pale
# blue vanished entirely — comparing a KMLB/KTBW mosaic against an
# MRMS blend, a whole swath from Tampa northeast past Orlando was
# missing. Despeckle handles the clutter that motivated the higher
# floor. Lower to 5 for even weaker echo.
DBZ_MIN = float(os.environ.get("L2_DBZ_MIN", "10"))

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
def gatefilter(radar, diag=None, site=None):
    """Dual-pol QC + despeckle.

    Two stages, and the second is the one that matters for how the
    mosaic reads:

    1. Correlation coefficient. Birds, insects, chaff and ground
       returns all decorrelate, so CC below ~0.8 is almost never
       precipitation. This is most of what MRMS's QC buys over a raw
       mosaic. NOT always available: a partial chunk-feed volume may
       carry only the surveillance moments, so we record whether it
       was actually applied instead of assuming.

    2. Despeckle. Removes contiguous regions smaller than
       L2_SPECKLE gates. Verified 8/18 against a synthetic field with
       coherent echo plus 2% scattered noise: it removed 8,047 of
       8,560 planted speckle gates and left the coherent block
       untouched. Without it the real KOKX mosaic showed isolated
       dots from New Jersey to Rhode Island, far from any echo.
       Size is insensitive above ~5 — 5, 10, 20 and 40 all removed
       the same gates — so the default is deliberately modest.
    """
    import pyart

    gf = pyart.filters.GateFilter(radar)
    gf.exclude_transition()
    gf.exclude_masked("reflectivity")
    gf.exclude_below("reflectivity", DBZ_MIN)
    have_cc = "cross_correlation_ratio" in radar.fields
    if have_cc:
        gf.exclude_below("cross_correlation_ratio", CC_MIN)
    kept_pre = int((~gf.gate_excluded).sum())
    try:
        gf = pyart.correct.despeckle_field(
            radar, "reflectivity", gatefilter=gf, size=SPECKLE)
    except Exception:
        pass
    kept = int((~gf.gate_excluded).sum())
    if diag is not None and site:
        tot = radar.gate_longitude["data"].size
        diag.setdefault("qc", {})[site] = (
            f"fields={sorted(radar.fields)} | "
            f"CC filter {'applied' if have_cc else 'UNAVAILABLE'} | "
            f"kept {kept}/{tot} gates ({100.0 * kept / max(tot, 1):.1f}%)"
            f", despeckle removed {kept_pre - kept}")
    return gf


# ---------------------------------------------------------------------------
# Grid + merge
# ---------------------------------------------------------------------------
def _grid_one(radar, diag=None, site=None):
    """Grid one radar onto the shared target grid; return float32."""
    import numpy as np
    import pyart

    nx = int(2 * HALF_X_M / RES_M)
    ny = int(2 * HALF_Y_M / RES_M)
    g = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(LEVELS, ny, nx),
        grid_limits=((BASE_M,
                      TOP_M if TOP_M else
                      BASE_M + 1000.0 * max(LEVELS - 1, 1)),
                     (-HALF_Y_M, HALF_Y_M), (-HALF_X_M, HALF_X_M)),
        fields=["reflectivity"],
        weighting_function=WEIGHT_FN,
        min_radius=MIN_RADIUS_M,
        gatefilters=(gatefilter(radar, diag, site),),
        grid_origin=GRID_CENTER,
    )
    arr = np.ma.filled(
        g.fields["reflectivity"]["data"], np.nan).astype("float32")
    del g
    return arr, (float(radar.latitude["data"][0]),
                 float(radar.longitude["data"][0]))


def _site_weight(rlat, rlon, shape):
    """Per-cell confidence in one radar, from horizontal range.

    Replaces the v1 element-wise max, which treated a gate 10 nm from
    KOKX at 600 ft as equal to one 90 nm from KENX sampling 11,000 ft
    through the same column. That produced hard seams wherever a
    site's coverage ended — clearly visible in the first four-site
    synthetic mosaic.

    Weight is a Gaussian in range, w = exp(-(d/D0)^2), with D0
    defaulting to the 52 nm crossover where 0.5 deg beam spreading
    makes Level II coarser than MRMS. That is not an arbitrary
    constant: it is the distance at which this data stops being
    better than the alternative, so it is the right place for a
    site's vote to fall away.

    Height weighting is deliberately folded into range rather than
    computed separately. For a fixed elevation angle, sampling height
    and range are monotonically related (0.5 deg gives 600 ft at
    10 nm, 4,300 ft at 50 nm), so a range term already penalises
    high-sampling cells. True per-gate height weighting needs the
    height field, which gridding discards.
    """
    import math

    import numpy as np

    lat0, lon0 = GRID_CENTER
    # Equirectangular offset of the radar from the grid origin. Py-ART
    # grids in azimuthal-equidistant; over a few hundred km the
    # difference is sub-kilometre, which is far below what matters for
    # a smoothly varying weight.
    rx = (rlon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    ry = (rlat - lat0) * 111320.0
    ny, nx = shape[-2], shape[-1]
    ax = np.linspace(-HALF_X_M, HALF_X_M, nx, dtype="float32")
    ay = np.linspace(-HALF_Y_M, HALF_Y_M, ny, dtype="float32")
    d2 = ((ax - rx) ** 2)[None, :] + ((ay - ry) ** 2)[:, None]
    d0 = BLEND_M
    return np.exp(-d2 / (d0 * d0)).astype("float32")


def _accumulate(num, den, arr, rlat, rlon):
    """Fold one site into weighted sums, in LINEAR Z not dBZ.

    dBZ is logarithmic, so averaging it is not averaging power — a
    30 and a 40 dBZ sample average to 35 dBZ in log space but to
    37.4 in linear, and the linear answer is the physical one.
    Convert, accumulate weighted, convert back at the end.
    """
    import numpy as np

    w2d = _site_weight(rlat, rlon, arr.shape)
    ok = np.isfinite(arr)
    if not ok.any():
        return num, den
    z = np.zeros_like(arr)
    np.power(10.0, arr / 10.0, out=z, where=ok)
    w = np.broadcast_to(w2d, arr.shape) * ok
    if num is None:
        num = np.zeros_like(arr)
        den = np.zeros_like(arr)
    num += (z * w).astype("float32")
    den += w.astype("float32")
    return num, den


def _finalize(num, den):
    """Weighted mean back to dBZ; NaN where no site voted."""
    import numpy as np

    out = np.full(num.shape, np.nan, dtype="float32")
    good = den > 0
    np.divide(num, den, out=out, where=good)
    np.log10(out, out=out, where=good & (out > 0))
    out *= 10.0
    out[~good] = np.nan
    return out


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
                      f"+-{HALF_X_M / 1000:.0f} x "
                      f"+-{HALF_Y_M / 1000:.0f} km")
    t_all = time.time()
    fs = None   # volume access lives in load_site
    num = den = None       # weighted-sum accumulators, linear Z
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
                if num is None and not diag.get("center_fixed"):
                    # Only fall back to the radar when no region
                    # centre was supplied; see REGION_VIEW.
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
                arr, (rla, rlo) = _grid_one(radar, diag, site)
                num, den = _accumulate(num, den, arr, rla, rlo)
                if t:
                    times.append(t)
                del arr
            except Exception as exc:
                diag["sites"][site] = (
                    f"grid failed — {type(exc).__name__}: {exc}")
            finally:
                del radar
                gc.collect()

    if num is None:
        diag["error"] = "no sites produced a grid"
        return None, diag

    # Column max AFTER blending, so each level is a properly weighted
    # multi-radar estimate before the vertical max is taken.
    blended = _finalize(num, den)
    del num, den
    comp = np.nanmax(blended, axis=0)
    del blended
    gc.collect()
    diag["blend"] = (f"range-weighted D0={BLEND_M / 1000:.0f} km | "
                     f"grid {WEIGHT_FN} roi>={MIN_RADIUS_M:.0f} m | "
                     f"floor {DBZ_MIN:.0f} dBZ")
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
def _nan_smooth(a, sigma):
    """Gaussian smooth that ignores NaN instead of spreading it.

    A plain gaussian_filter treats NaN as poison — one missing cell
    contaminates its whole neighbourhood and the echo edge dissolves.
    Normalised convolution smooths the data and the valid-mask
    separately then divides, so edges stay put and only real values
    contribute.
    """
    import numpy as np
    from scipy.ndimage import gaussian_filter

    if sigma <= 0:
        return a
    m = np.isfinite(a).astype("float32")
    d = np.where(np.isfinite(a), a, 0.0).astype("float32")
    ds = gaussian_filter(d, sigma, mode="nearest")
    ms = gaussian_filter(m, sigma, mode="nearest")
    out = np.full_like(a, np.nan)
    good = ms > 1e-3
    out[good] = ds[good] / ms[good]
    # Do not invent echo where there was none: drop cells that only
    # got a value because a neighbour bled into them.
    out[(~np.isfinite(a)) & (ms < 0.5)] = np.nan
    return out


def render_png(comp, path, diag=None):
    """Filled-contour render — the smoothing half of the problem.

    Three things together make this look continuous rather than
    pixelated: Barnes2 gridding, a light Gaussian on the finished
    field, and fine contour steps. A discrete-class raster (what an
    ArcGIS export returns) gives hard pixel edges; 1 dBZ filled
    contours over a smoothed field do not.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap

    comp = _nan_smooth(comp, SMOOTH_SIGMA)
    # 1.0 dBZ steps, not 2.5: at 2.5 the bands themselves are visible
    # as contour terracing on a smooth gradient.
    lev = np.arange(DBZ_MIN, 80.001, 1.0)
    cols = plt.get_cmap("gist_ncar")(np.linspace(0.08, 0.95, len(lev) - 1))
    cmap = ListedColormap(cols)
    cmap.set_bad(alpha=0.0)
    norm = BoundaryNorm(lev, cmap.N)

    ny, nx = comp.shape
    fig = plt.figure(figsize=(nx / 200.0, ny / 200.0), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.contourf(np.ma.masked_invalid(comp), levels=lev,
                cmap=cmap, norm=norm, extend="max", antialiased=True)
    ax.set_xlim(0, nx - 1)
    ax.set_ylim(0, ny - 1)
    fig.savefig(path, format="png", transparent=True,
                bbox_inches=None, pad_inches=0)
    plt.close(fig)
    if diag is not None:
        diag["png_bytes"] = os.path.getsize(path)
        diag["render"] = (f"smooth sigma={SMOOTH_SIGMA}, "
                          f"{len(lev) - 1} contour bands")
    return path


def bounds():
    """(west, south, east, north) for a deck.gl BitmapLayer."""
    import math

    lat0, lon0 = GRID_CENTER
    dlat = HALF_Y_M / 111320.0
    dlon = HALF_X_M / (111320.0 * math.cos(math.radians(lat0)))
    return (lon0 - dlon, lat0 - dlat, lon0 + dlon, lat0 + dlat)

# ---------------------------------------------------------------------------
# Frame loops
# ---------------------------------------------------------------------------
# A loop is expensive: every frame is a full fetch + QC + grid + render,
# ~10 s each, so 24 frames is four minutes. Two things make that
# tolerable. Frames are keyed by the VOLUME FILENAME, so a rebuild
# only renders scans it has not seen — asking for 24 frames a second
# time costs one new frame, not twenty-four. And the loop is driven
# by the archive chain rather than the chunk feed, because the chunk
# feed only ever holds the volume currently being scanned.


def recent_scans(site, n, lookback_min=240):
    """The n most recent complete volumes, oldest first."""
    from datetime import datetime, timedelta, timezone

    from core import radar as _r

    now = datetime.now(timezone.utc)
    scans = _r._find_scans(site, now - timedelta(minutes=lookback_min),
                           now)
    if not scans:
        return []
    return sorted(scans, key=lambda x: x.scan_time)[-n:]


def render_scan(site, scan, outdir, tag, diag=None):
    """One frame. Returns (filename, note). Skips work if it exists."""
    import tempfile

    from pathlib import Path as _PP

    diag = diag if diag is not None else {}
    name = f"l2loop_{tag}_{scan.filename}.png"
    dest = _PP(outdir) / name
    if dest.exists():
        return name, "cached"
    from core import radar as _r

    global GRID_CENTER
    try:
        with tempfile.TemporaryDirectory() as td:
            path = _r._download_volume(scan, f"{td}/{scan.filename}")
            with open(path, "rb") as fh:
                radar = _read_volume_bytes(fh.read())
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    try:
        have = radar.nsweeps
        k = min(TILTS, have)
        if k < have:
            radar = radar.extract_sweeps(list(range(k)))
        gf_diag = {}
        arr, (rla, rlo) = _grid_one(radar, gf_diag, site)
        num, den = _accumulate(None, None, arr, rla, rlo)
        del arr
        import numpy as np

        comp = np.nanmax(_finalize(num, den), axis=0)
        del num, den
        render_png(comp, dest, diag)
        del comp
        return name, f"{k}/{have} tilts"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        del radar
        gc.collect()


def build_loop(site, n_frames, outdir, tag="cmax", progress=None,
               diag=None):
    """Render up to n_frames volumes. Returns (frames, diag).

    frames is oldest-first: [{"name", "valid"}]. Old frames for this
    tag are pruned to twice the requested depth so the disk does not
    grow without bound.
    """
    from pathlib import Path as _PP

    diag = diag if diag is not None else {}
    diag.setdefault("frames", {})
    scans = recent_scans(site, n_frames)
    if not scans:
        diag["error"] = f"no {site} volumes in the last 4 h"
        return [], diag
    out = _PP(outdir)
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, sc in enumerate(scans, 1):
        if progress:
            progress(i / len(scans),
                     f"frame {i}/{len(scans)} — {sc.filename}")
        name, note = render_scan(site, sc, out, tag, diag)
        diag["frames"][sc.filename] = note
        if name:
            frames.append({
                "name": name,
                "valid": sc.scan_time.strftime("%H:%M:%SZ"),
            })
    keep = {f["name"] for f in frames}
    for old in sorted(out.glob(f"l2loop_{tag}_*.png")):
        if old.name not in keep and len(list(
                out.glob(f"l2loop_{tag}_*.png"))) > 2 * n_frames:
            try:
                old.unlink()
            except OSError:
                pass
    diag["n_frames"] = len(frames)
    return frames, diag

