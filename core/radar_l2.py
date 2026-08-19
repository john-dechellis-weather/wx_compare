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
    # DCA to BOS. Eight sites is ~2.7 min a pass at 20 s each, which
    # fits inside the 4-6 min volume cadence — the largest domain
    # that can stay current. The Northeast has the densest WSR-88D
    # coverage in the country, so nearly all of this box sits inside
    # the 52 nm radius of at least one site, which is the condition
    # for beating MRMS rather than just duplicating it.
    "NE Corridor / DCA-BOS": ["KLWX", "KDOX", "KDIX", "KOKX",
                              "KBOX", "KENX", "KCCX", "KBGM"],
    # S-band + C-band merged. The two 88Ds carry the area and set the
    # calibration; the three TDWRs sit AT the airports with 150 m
    # gates and win inside ~20 nm, which is the approach and
    # departure environment. C-band attenuation is what makes this
    # non-trivial and is handled per-gate — see _cband_attenuation.
    "N90 merged (S+C band)": ["KOKX", "KDIX", "TJFK", "TEWR", "TPHL"],
    # The two NY-metro TDWRs alone. Same ~1 min cadence, so scan
    # spread is near zero and there is no advection smearing to
    # correct. 33 km apart, which means a cell over Newark Bay is
    # viewed 121 deg apart and Manhattan 70 deg apart — nearly
    # independent attenuation paths, so a core that blinds one is
    # broadside to the other. That is what makes the pair recover
    # shadowed cores neither could see alone.
    #
    # LOW LEVEL ONLY. Level III gives three tilts (0.6/1.0/2.0 deg)
    # and nothing higher, so within 30 km every beam is under 1 km
    # AGL. Raising TILTS does not help; TZ0/TZ1/TZ2 is all the SPG
    # distributes. For anything aloft use the S-band sites.
    "NY Metro TDWR pair": ["TJFK", "TEWR"],
    # Single-tilt prototype: 1.0 deg only, 30 nm range each, no
    # S-band. The point is a controlled comparison — TJFK and TEWR
    # share exactly one elevation angle, so any residual difference
    # is calibration, attenuation or radome wetting rather than a
    # tilt mismatch. Set L2_TDWR_PRODUCT=TZ1 and
    # L2_MAX_RANGE_M=55560 to run it as intended.
    "NY Metro 1.0deg proto": ["TJFK", "TEWR"],
    # South Florida: TMIA and TFLL are 33 km apart, the same spacing
    # as the NY pair, with KAMX as an S-band anchor for calibration —
    # which the NY pair lacks. Better test bed, and Florida actually
    # has convection.
    "FLL-MIA TDWR pair": ["TMIA", "TFLL"],
    "FLL-MIA merged (S+C)": ["KAMX", "TMIA", "TFLL", "TPBI"],
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
    "NE Corridor / DCA-BOS": (40.60, -74.00, 300, 250),
    "N90 merged (S+C band)": (40.75, -74.05, 190, 160),
    "NY Metro TDWR pair": (40.62, -74.07, 90, 80),
    # Union of two 30 nm circles 33 km apart, plus margin.
    "NY Metro 1.0deg proto": (40.59, -74.07, 80, 65),
    "FLL-MIA TDWR pair": (25.95, -80.30, 80, 70),
    "FLL-MIA merged (S+C)": (26.10, -80.35, 150, 140),
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
    "KLWX": "Sterling VA — DCA/BWI/IAD",
    "KDOX": "Dover DE — the Delmarva gap",
    "KCCX": "State College PA — western edge",
    "KBGM": "Binghamton NY — northwest",
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
# TDWR. C-band, 150 m gates, 0.55 deg beam, sited AT the airports —
# better than an 88D inside ~20 nm, which is precisely the approach
# and departure environment. Two things make it hard to blend, and
# both are handled in _cband_attenuation below.
TDWR_SITES = {
    "TJFK": "TDWR at JFK", "TEWR": "TDWR at EWR",
    "TPHL": "TDWR at PHL", "TBOS": "TDWR at BOS",
    "TDCA": "TDWR at DCA", "TBWI": "TDWR at BWI",
    "TIAD": "TDWR at IAD",
    "TMIA": "TDWR at MIA", "TFLL": "TDWR at FLL",
    "TPBI": "TDWR at PBI", "TMCO": "TDWR at MCO",
    "TTPA": "TDWR at TPA",
}
N90_TDWR = ["TJFK", "TEWR"]

# Two-way C-band specific attenuation, k = A * Z**B dB/km. Measured
# consequence at these coefficients: a TDWR looking through a 55 dBZ
# core for 10 km reads about 10.6 dB LOW on everything behind it, and
# 21 dB low through 20 km. S-band over the same path loses under
# 0.5 dB. That difference is not noise — it is the single reason
# TDWR cannot simply be dropped into the blend.
CBAND_A = float(os.environ.get("L2_CBAND_A", "1.6e-4"))
CBAND_B = float(os.environ.get("L2_CBAND_B", "0.64"))
# Above this much accumulated two-way attenuation the correction is
# guesswork — the gate is downweighted rather than trusted.
PIA_TRUST_DB = float(os.environ.get("L2_PIA_TRUST_DB", "6.0"))

# --- inter-radar bias calibration -------------------------------------
# Only compare cells with real echo: weak returns are noisy and their
# differences are dominated by sampling, not calibration.
CAL_MIN_DBZ = float(os.environ.get("L2_CAL_MIN_DBZ", "20"))
# A pair needs this many overlapping cells before its difference is
# believed. In dry weather no pair will reach it and calibration
# correctly becomes a no-op.
CAL_MIN_CELLS = int(os.environ.get("L2_CAL_MIN_CELLS", "300"))
# Hard cap. A real inter-radar bias is 1-3 dB; anything larger is a
# bad estimate, not a calibration problem, and must not be applied.
CAL_MAX_DB = float(os.environ.get("L2_CAL_MAX_DB", "6.0"))
# Calibration samples must come from cells where BOTH radars are
# sampling nearly the same altitude. Measured for the TJFK/TEWR pair
# at a shared 1.0 deg tilt: over the midpoint the two beams are 11 m
# apart, but over JFK they are 638 m apart and at the east end 739 m,
# because the cell is at very different RANGES from the two sites.
# Comparing those would charge a real vertical reflectivity gradient
# to instrument bias. Only near the perpendicular bisector are the
# two genuinely measuring the same air.
CAL_MAX_DZ_M = float(os.environ.get("L2_CAL_MAX_DZ_M", "150"))

# --- single-tilt prototype knobs --------------------------------------
# TDWR tilts are not identical between sites: TJFK runs 0.5/1.0/2.8 and
# TEWR 0.3/1.0/2.7. Only 1.0 deg is common to both, so a clean
# two-site experiment uses that product alone and leaves the mismatched
# base and upper tilts out until there is an adjustment for them.
TDWR_PRODUCT_ONLY = os.environ.get("L2_TDWR_PRODUCT")   # e.g. "TZ1"
# Hard range cut, applied per site before gridding.
MAX_RANGE_M = os.environ.get("L2_MAX_RANGE_M")
MAX_RANGE_M = float(MAX_RANGE_M) if MAX_RANGE_M else None


# TDWR arrives as LEVEL III, not Level II — the FAA's SPG generates
# derived products and the NWS distributes those. So it does not come
# down the chunk feed at all; it comes from the same THREDDS
# /terminal/level3/ tree that core.radar3 already talks to for NEXRAD
# Level III, just under a different catalog root.
#
# Site ids are THREE letters there (JFK, EWR, PHL), not the four-letter
# T-prefixed forms used on displays.
TDWR_ID3 = {"TJFK": "JFK", "TEWR": "EWR", "TPHL": "PHL",
            "TBOS": "BOS", "TDCA": "DCA", "TBWI": "BWI",
            "TIAD": "IAD", "TMIA": "MIA", "TFLL": "FLL",
            "TPBI": "PBI", "TMCO": "MCO", "TTPA": "TPA"}
# Base reflectivity tilts 1-3. TZL is the long-range product but it is
# resampled to 300 m, which throws away the resolution that made TDWR
# worth having.
TDWR_PRODUCTS = ["TZ0", "TZ1", "TZ2"]


def _radar_from_nids(raw):
    """NIDS bytes -> pyart Radar, via metpy.

    pyart.io.read_nexrad_level3 refuses TDWR: measured live 8/19,
    "Level3 product with code 180 is not supported" — its reader
    covers WSR-88D product codes only. metpy's Level3File parses the
    NIDS radial format generically and does not care which radar
    produced it, which is why core.radar3 uses it for Level III
    everywhere. So decode with metpy and assemble the pyart Radar
    by hand.

    Elevation comes from the file's own metadata rather than being
    assumed from the product name — TJFK runs 0.5/1.0/2.8 and TEWR
    0.3/1.0/2.7, so the tilt has to be read, not inferred.
    """
    import numpy as np
    from metpy.io import Level3File

    import pyart

    f = Level3File(io.BytesIO(raw))
    blk = f.sym_block[0][0]
    data = np.asarray(f.map_data(blk["data"]), dtype="float32")
    nrays, ngates = data.shape
    az = np.asarray(blk["start_az"][:nrays], dtype="float32")
    el = float(f.metadata.get("el_angle", 0.5))
    gate_m = float(f.max_range) * 1000.0 / ngates

    radar = pyart.testing.make_empty_ppi_radar(ngates, nrays, 1)
    radar.range["data"] = ((np.arange(ngates) + 0.5)
                           * gate_m).astype("float32")
    radar.azimuth["data"] = az
    radar.elevation["data"] = np.full(nrays, el, dtype="float32")
    radar.fixed_angle["data"] = np.array([el], dtype="float32")
    radar.latitude["data"] = np.array([float(f.lat)])
    radar.longitude["data"] = np.array([float(f.lon)])
    radar.altitude["data"] = np.array([float(getattr(f, "height", 0.0))])
    radar.time["data"] = np.zeros(nrays, dtype="float64")
    radar.add_field(
        "reflectivity",
        {"data": np.ma.masked_invalid(data), "units": "dBZ",
         "_FillValue": -9999.0, "standard_name": "reflectivity"},
        replace_existing=True)
    return radar, el


def _tdwr_catalog_urls(prod, s3, day):
    """Candidate catalog URLs for one TDWR product.

    The layout is NOT certain, which is why several are tried and the
    outcome of each is recorded. Unidata serves NEXRAD Level III at
    /nexrad/level3/{PROD}/{SITE}/{YYYYMMDD}/ — core.radar3 uses that
    and it works — and TDWR lives under a sibling root. Observed in
    the wild without a date folder too, hence both forms.
    """
    base = "https://thredds.ucar.edu/thredds/catalog"
    return [
        f"{base}/terminal/level3/{prod}/{s3}/{day:%Y%m%d}/catalog.xml",
        f"{base}/terminal/level3/{prod}/{s3}/catalog.xml",
        f"{base}/nexrad/level3/{prod}/{s3}/{day:%Y%m%d}/catalog.xml",
        f"{base}/nexrad/level3/{prod}/{s3}/catalog.xml",
    ]


def _load_tdwr(site, diag):
    """One TDWR volume assembled from Level III tilts.

    Each product file is ONE sweep, so a volume is built by reading
    several and merging.

    Every URL tried is recorded with its outcome. The first version
    swallowed catalog errors in a bare `except: continue` and
    reported only "no Level III tilts", which says nothing about
    whether the host was wrong, the path was wrong, or the site
    simply had no recent data.
    """
    import requests
    from datetime import timedelta as _td

    import pyart

    s3 = TDWR_ID3.get(site.upper(), site.upper()[-3:])
    tried = []
    sweeps, used = [], []
    now = datetime.now(timezone.utc)
    wanted = ([TDWR_PRODUCT_ONLY] if TDWR_PRODUCT_ONLY
              else TDWR_PRODUCTS[:max(1, min(TILTS,
                                             len(TDWR_PRODUCTS)))])
    for prod in wanted:
        ds = None
        for day in (now, now - _td(days=1)):
            for url in _tdwr_catalog_urls(prod, s3, day):
                try:
                    from siphon.catalog import TDSCatalog

                    cat = TDSCatalog(url)
                    names = sorted(cat.datasets.keys(), reverse=True)
                    if not names:
                        tried.append(f"{url.split('/catalog')[0]}: "
                                     f"empty")
                        continue
                    ds = cat.datasets[names[0]]
                    tried.append(f"{url.split('/catalog')[0]}: "
                                 f"{len(names)} datasets")
                    break
                except Exception as exc:
                    tried.append(f"{url.split('/catalog')[0]}: "
                                 f"{type(exc).__name__}")
            if ds is not None:
                break
        if ds is None:
            continue
        try:
            u = (ds.access_urls.get("HTTPServer")
                 or ds.access_urls.get("httpserver"))
            raw = requests.get(
                u, headers={"User-Agent": "BlueMet/1.0"},
                timeout=60).content
            _r, _el = _radar_from_nids(raw)
            sweeps.append(_r)
            used.append(f"{prod}@{_el:.1f}deg")
        except Exception as exc:
            tried.append(f"{prod} download/parse: "
                         f"{type(exc).__name__}: {exc}")
    diag.setdefault("tdwr_probe", {})[site] = tried[:8]
    if not sweeps:
        raise RuntimeError(
            f"no Level III tilts for {site} ({s3}); tried "
            + " | ".join(tried[:4]))
    radar = sweeps[0]
    for extra in sweeps[1:]:
        try:
            radar = pyart.util.join_radar(radar, extra)
        except Exception:
            break
    diag.setdefault("tdwr", {})[site] = f"{s3} tilts {'+'.join(used)}"
    return radar


def _cband_attenuation(radar, field="reflectivity"):
    """Correct C-band reflectivity for rain attenuation, and report
    how much correction each gate needed.

    Works in POLAR space, before gridding, because attenuation is a
    path integral along the ray — once gridded, the ray geometry is
    gone and the correction is impossible to compute.

    Returns (pia_2way_db, corrected_dbz). The PIA array is the more
    important half: it is not just a correction, it is a confidence
    map. A gate needing 2 dB of correction is trustworthy; one
    needing 15 dB is a guess, because the estimate compounds its own
    errors along the ray and a fully attenuated ray gives no signal
    to correct at all.
    """
    import numpy as np

    z = radar.fields[field]["data"]
    dbz = np.ma.filled(z, np.nan).astype("float32")
    rng = radar.range["data"].astype("float32")
    dr_km = float(np.diff(rng).mean()) / 1000.0 if rng.size > 1 else 0.15

    lin = np.power(10.0, np.clip(dbz, -30, 70) / 10.0,
                   where=np.isfinite(dbz),
                   out=np.zeros_like(dbz))
    k = CBAND_A * np.power(lin, CBAND_B)          # dB/km one-way
    k[~np.isfinite(dbz)] = 0.0
    # Two-way PIA to the NEAR edge of each gate: the cumulative sum
    # is shifted so a gate is not attenuated by itself.
    pia = 2.0 * np.cumsum(k, axis=1) * dr_km
    pia[:, 1:] = pia[:, :-1]
    pia[:, 0] = 0.0
    return pia.astype("float32"), (dbz + pia).astype("float32")


def _cband_weight(pia):
    """Confidence multiplier from accumulated attenuation.

    exp(-(pia/PIA_TRUST_DB)^2): full weight where the ray is clean,
    fading as the correction grows. At the 6 dB default a gate behind
    5 km of 55 dBZ core keeps ~40% weight, and one behind 20 km of it
    keeps essentially none — which is right, because there is no
    signal left to correct.
    """
    import numpy as np

    return np.exp(-(pia / PIA_TRUST_DB) ** 2).astype("float32")

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
# Vertical proximity scale. Range weighting alone cannot tell that two
# sites equidistant from a cell may be sampling completely different
# altitudes — at a 1500 m grid level a 0.5 deg beam is 1300 m below it
# at 20 km and 1100 m above it at 150 km. This term prefers the site
# whose beam actually passes through the level being gridded.
BLEND_VERT_M = float(os.environ.get("L2_BLEND_VERT_M", "1500"))
# Time scale. Volumes are not synchronised: at 40 kt a 3-minute
# offset displaces a cell 2 nm, so an older scan should carry less
# weight. Set 0 to disable.
BLEND_TIME_S = float(os.environ.get("L2_BLEND_TIME_S", "300"))
# Winner-take-most exponent on the combined weight. Measured 8/18
# with a 30 dBZ site west and a 45 dBZ site east: at 1.0 the value
# 90 km INTO the western site's territory was already pulled to
# 34 dBZ by the far site; at 2.0 it reads 30.4 and at 6.0 it is a
# clean 30.0, while the midpoint stays ~42 either way. So this
# controls how fast the blend hands over — 1.0 lets a distant site
# contaminate a near one, higher values keep each area honest while
# still crossing over smoothly in the middle.
#
# It does NOT fix time-offset smearing. Tested directly: a cell seen
# 3 min apart by two sites spans 19 km instead of 16 at every
# sharpness value, because a weighted mean gives a value wherever
# EITHER site has data, so the footprint is always the union. Only
# advection correction fixes that.
BLEND_SHARPNESS = float(os.environ.get("L2_BLEND_SHARPNESS", "2.0"))
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

    # --- TDWR: Level III, not Level II -------------------------------
    if site.upper() in TDWR_SITES:
        try:
            radar = _load_tdwr(site, diag)
            src = f"TDWR Level III ({diag.get('tdwr', {}).get(site, '')})"
        except Exception as exc:
            diag["sites"][site] = (f"TDWR fetch: "
                                   f"{type(exc).__name__}: {exc}")
            return None, None

    # --- path 1: live chunk feed (S-band only) -----------------------
    if radar is None:
        try:
            from core import radar_l2rt

            raw, info = radar_l2rt.fetch_live_volume_bytes(site)
            radar = _read_volume_bytes(raw)
            src = (f"chunk feed vol {info.get('volume')} "
                   f"{info.get('n_used')}/{info.get('n_chunks')} "
                   f"chunks, {info.get('age_s')}s old")
        except Exception as exc:
            notes.append(f"chunk feed: {type(exc).__name__}: {exc}")

    # --- path 2: complete volume via the AWS/GCS/THREDDS chain -------
    # WSR-88D only; that chain has no TDWR in it.
    if radar is None and site.upper() not in TDWR_SITES:
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

    # C-band sites get attenuation-corrected in polar space, before
    # gridding. The PIA array rides along as a field so the gridder
    # carries it onto the grid and the blend can use it as a
    # confidence map — see _cband_weight.
    if site.upper() in TDWR_SITES:
        try:
            pia, corr = _cband_attenuation(radar)
            import numpy as _np
            radar.fields["reflectivity"]["data"] = _np.ma.masked_invalid(
                corr)
            radar.add_field(
                "pia_2way",
                {"data": _np.ma.masked_invalid(pia),
                 "units": "dB", "_FillValue": -9999.0},
                replace_existing=True)
            notes.append(f"C-band corrected, max PIA "
                         f"{float(_np.nanmax(pia)):.1f} dB")
        except Exception as exc:
            notes.append(f"C-band correction failed: "
                         f"{type(exc).__name__}: {exc}")

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
    import numpy as np

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
    if MAX_RANGE_M:
        # Range cut before anything else. Past the cut the beam is
        # both higher and wider, so including it would dilute exactly
        # the close-in advantage the experiment is testing.
        gf.exclude_gates(
            np.broadcast_to(radar.range["data"][None, :],
                            radar.fields["reflectivity"]["data"].shape)
            > MAX_RANGE_M)
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


def _site_weight(rlat, rlon, shape, tilt_deg=0.5, age_s=0.0):
    """Per-cell confidence in one radar. Three independent terms.

    Replaces the v1 element-wise max, which treated a gate 10 nm from
    KOKX at 600 ft as equal to one 90 nm from KENX sampling 11,000 ft
    through the same column, and produced hard seams wherever a
    site's coverage ended.

    RANGE   exp(-(d/D0)^2), D0 = the 52 nm crossover where 0.5 deg
            beam spreading makes Level II coarser than MRMS. Not an
            arbitrary constant: it is where this data stops being
            better than the alternative, so it is the right place
            for a site's vote to fall away.

    VERTICAL exp(-((z - h_beam(d))/H0)^2). h_beam is where the
            lowest tilt actually is at that range, under 4/3-earth
            refraction. This is the term range weighting cannot
            supply — two sites the same distance away can be looking
            at totally different heights.

    TIME    exp(-(age/T0)^2), age measured from the newest volume in
            the mosaic. At 40 kt a 3-minute offset is 2 nm of
            displacement, so a stale site should not win ties.

    The product is then raised to BLEND_SHARPNESS. At 1.0 this is a
    plain weighted mean; higher values approach nearest-site-wins,
    which trades smooth transitions for less smearing of fast cells.
    """
    import math

    import numpy as np

    lat0, lon0 = GRID_CENTER
    # Equirectangular offset of the radar from the grid origin.
    # Py-ART grids in azimuthal-equidistant; over a few hundred km
    # the difference is sub-kilometre, far below what matters for a
    # smoothly varying weight.
    rx = (rlon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    ry = (rlat - lat0) * 111320.0
    nz, ny, nx = shape
    ax = np.linspace(-HALF_X_M, HALF_X_M, nx, dtype="float32")
    ay = np.linspace(-HALF_Y_M, HALF_Y_M, ny, dtype="float32")
    d2 = ((ax - rx) ** 2)[None, :] + ((ay - ry) ** 2)[:, None]
    w = np.exp(-d2 / (BLEND_M * BLEND_M)).astype("float32")

    if BLEND_TIME_S > 0 and age_s:
        w *= float(math.exp(-(age_s / BLEND_TIME_S) ** 2))

    if BLEND_VERT_M > 0:
        # Beam height at each cell's range, 4/3 earth.
        d = np.sqrt(d2, dtype="float32")
        bh = (d * math.sin(math.radians(tilt_deg))
              + d * d / (2.0 * 1.333 * 6371000.0)).astype("float32")
        zs = np.linspace(
            BASE_M,
            TOP_M if TOP_M else BASE_M + 1000.0 * max(nz - 1, 1),
            nz, dtype="float32")
        out = np.empty(shape, dtype="float32")
        for k, z in enumerate(zs):
            out[k] = w * np.exp(
                -((z - bh) / BLEND_VERT_M) ** 2).astype("float32")
        w3 = out
    else:
        w3 = np.broadcast_to(w, shape).astype("float32")

    if BLEND_SHARPNESS != 1.0:
        w3 = np.power(w3, BLEND_SHARPNESS, dtype="float32")
    return w3


def _accumulate(num, den, arr, rlat, rlon, tilt_deg=0.5,
                age_s=0.0):
    """Fold one site into weighted sums, in LINEAR Z not dBZ.

    dBZ is logarithmic, so averaging it is not averaging power — a
    30 and a 40 dBZ sample average to 35 dBZ in log space but to
    37.4 in linear, and the linear answer is the physical one.
    Convert, accumulate weighted, convert back at the end.
    """
    import numpy as np

    ok = np.isfinite(arr)
    if not ok.any():
        return num, den
    w = _site_weight(rlat, rlon, arr.shape, tilt_deg, age_s) * ok
    z = np.zeros_like(arr)
    np.power(10.0, arr / 10.0, out=z, where=ok)
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


def _beam_height_map(rlat, rlon, tilt_deg, site_alt_m, shape):
    """Height MSL that this site's lowest tilt samples, per cell.

    Used to keep calibration honest: two radars only measure the same
    air where their beams are at the same altitude.
    """
    import math

    import numpy as np

    lat0, lon0 = GRID_CENTER
    rx = (rlon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    ry = (rlat - lat0) * 111320.0
    ny, nx = shape[-2], shape[-1]
    ax = np.linspace(-HALF_X_M, HALF_X_M, nx, dtype="float32")
    ay = np.linspace(-HALF_Y_M, HALF_Y_M, ny, dtype="float32")
    d = np.sqrt(((ax - rx) ** 2)[None, :] + ((ay - ry) ** 2)[:, None])
    return (d * math.sin(math.radians(tilt_deg))
            + d * d / (2.0 * 1.333 * 6371000.0)
            + site_alt_m).astype("float32")


def _solve_biases(staged, diag):
    """Per-site dBZ offsets from overlap, solved as a graph.

    Every cell seen by two radars is a direct comparison of what they
    say about the same air at nearly the same time. Measured over the
    merged N90 box, 73% of the area is seen by two or more sites and
    30% by three or more — plenty of samples.

    Method: median difference per pair (median, not mean, because a
    single convective core in one site's view and not the other's
    would drag a mean), then least squares over the pair graph for
    per-site offsets.

    ANCHORED TO THE S-BAND SITES, not to the global mean. The 88Ds
    are the better-calibrated instruments and do not suffer C-band
    attenuation, so the physically right move is to pull TDWR toward
    them rather than meet in the middle. Anchoring to the mean would
    let two attenuating C-band sites drag a good 88D down.

    Returns {site: offset_db}; empty dict when there is not enough
    overlap, which is the normal case in dry weather.
    """
    import itertools

    import numpy as np

    names = [x[5] for x in staged]
    if len(names) < 2:
        return {}
    hmaps = [_beam_height_map(x[1], x[2], x[3], x[6], x[0].shape)
             for x in staged]
    pairs, obs = [], []
    for (ia, a), (ib, b) in itertools.combinations(
            list(enumerate(staged)), 2):
        fa, fb = a[0], b[0]
        dz = np.abs(hmaps[ia] - hmaps[ib])
        m = (np.isfinite(fa) & np.isfinite(fb)
             & (fa > CAL_MIN_DBZ) & (fb > CAL_MIN_DBZ)
             & (dz[None, :, :] <= CAL_MAX_DZ_M))
        n = int(m.sum())
        if n < CAL_MIN_CELLS:
            continue
        d = float(np.median(fa[m] - fb[m]))
        pairs.append((ia, ib))
        obs.append(d)
        diag.setdefault("cal_pairs", {})[
            f"{names[ia]}-{names[ib]}"] = (
                f"{d:+.2f} dB, {n} cells within "
                f"{CAL_MAX_DZ_M:.0f} m of equal beam height")
    if not pairs:
        diag["calibration"] = ("no pair reached "
                               f"{CAL_MIN_CELLS} overlapping cells "
                               f"above {CAL_MIN_DBZ:.0f} dBZ — "
                               "offsets not applied")
        return {}
    k = len(names)
    A = np.zeros((len(pairs) + 1, k), dtype="float64")
    y = np.zeros(len(pairs) + 1, dtype="float64")
    for r, ((ia, ib), d) in enumerate(zip(pairs, obs)):
        A[r, ia], A[r, ib], y[r] = 1.0, -1.0, d
    # Anchor row: mean offset of the S-band sites is zero.
    sband = [i for i, n_ in enumerate(names)
             if n_.upper() not in TDWR_SITES]
    anchor = sband or list(range(k))
    for i in anchor:
        A[-1, i] = 1.0 / len(anchor)
    sol, *_ = np.linalg.lstsq(A, y, rcond=None)
    out = {}
    for i, n_ in enumerate(names):
        b = float(np.clip(sol[i], -CAL_MAX_DB, CAL_MAX_DB))
        if abs(b) > 0.05:
            out[n_] = b
    diag["calibration"] = ", ".join(
        f"{n_} {b:+.2f} dB" for n_, b in out.items()) or "all within 0.05 dB"
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
    # Gridded fields are held until every site is in, because the
    # TIME weight is measured against the NEWEST volume and that is
    # not known until the last site has loaded. Each staged field is
    # ~31 MB; the radar volumes themselves are still freed as we go,
    # which is what actually drives peak memory.
    staged = []
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
                _tilt = 0.5
                try:
                    _tilt = float(radar.fixed_angle["data"][0])
                except Exception:
                    pass
                arr, (rla, rlo) = _grid_one(radar, diag, site)
                try:
                    _alt = float(radar.altitude["data"][0])
                except Exception:
                    _alt = 0.0
                staged.append((arr, rla, rlo, _tilt, t, site, _alt))
                if t:
                    times.append(t)
            except Exception as exc:
                diag["sites"][site] = (
                    f"grid failed — {type(exc).__name__}: {exc}")
            finally:
                del radar
                gc.collect()

    # How high did each site actually sample inside the box? This is
    # the check that says whether an "aloft" question is answerable
    # at all: a TDWR pair maxes out under 1 km within 30 km, so a
    # storm building aloft over the metro is invisible to it however
    # the tilts are combined.
    try:
        import math as _m
        for _a, _rla, _rlo, _tlt, _t, _sname, _salt in staged:
            _reach = max(HALF_X_M, HALF_Y_M)
            _hi = (_reach * _m.sin(_m.radians(_tlt))
                   + _reach ** 2 / (2 * 1.333 * 6371000.0))
            diag.setdefault("vertical_reach_m", {})[_sname] = (
                f"lowest tilt {_tlt:.1f} deg -> {_hi:,.0f} m at "
                f"{_reach / 1000:.0f} km")
    except Exception:
        pass

    if staged:
        newest = max((x[4] for x in staged if x[4]), default=None)
        bias = _solve_biases(staged, diag)
        for arr, rla, rlo, tilt, t, site, _alt in staged:
            b = bias.get(site)
            if b:
                arr = arr - b        # bring this site onto the S-band scale
            age = ((newest - t).total_seconds()
                   if (newest and t) else 0.0)
            num, den = _accumulate(num, den, arr, rla, rlo, tilt, age)
            del arr
        staged.clear()
        gc.collect()

    if num is None:
        diag["error"] = "no sites produced a grid"
        return None, diag

    # Column max AFTER blending, so each level is a properly weighted
    # multi-radar estimate before the vertical max is taken.
    blended = _finalize(num, den)
    del num, den
    # ALOFT: max over levels ABOVE aloft_base_m, so a cell that is
    # strong at altitude while base reflectivity is still weak shows
    # up. Only meaningful when the sites actually sample that high —
    # see vertical_reach_m.
    _ab = float(os.environ.get("L2_ALOFT_BASE_M", "3000"))
    _zs = np.linspace(
        BASE_M, TOP_M if TOP_M else BASE_M + 1000.0 * max(LEVELS - 1, 1),
        blended.shape[0])
    _hi_idx = [i for i, z in enumerate(_zs) if z >= _ab]
    if _hi_idx:
        diag["aloft"] = (f"max above {_ab:,.0f} m from levels "
                         f"{_hi_idx[0]}-{_hi_idx[-1]}")
    else:
        diag["aloft"] = (f"no grid level reaches {_ab:,.0f} m — "
                         f"aloft product unavailable at this "
                         f"LEVELS/TOP_M")
    comp = np.nanmax(blended, axis=0)
    del blended
    gc.collect()
    diag["blend"] = (f"range D0={BLEND_M / 1000:.0f} km, "
                     f"vert {BLEND_VERT_M:.0f} m, "
                     f"time {BLEND_TIME_S:.0f} s, "
                     f"sharpness {BLEND_SHARPNESS:g} | "
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

# ---------------------------------------------------------------------------
# Warmer
# ---------------------------------------------------------------------------
# Deliberately a SEPARATE daemon from core.cam_warm, not a job added to
# WARM_JOBS. Three reasons: the CAM warmer is keyed by model/cycle and
# writes frames into a manifest store, which does not fit a rolling
# radar loop; a radar cycle is minutes where a CAM cycle is hours, so
# they want different sleep intervals; and a failure here must not be
# able to stall the CAM warmer that pages 9 and 11 depend on. Same
# shape as cam_warm.ensure_warmer_started though — idempotent, daemon
# thread, env kill switch — so it behaves the way the rest of the app
# already does.
#
# What it does: for each region in ROTATION, keep the newest N frames
# rendered on disk. Frames are keyed by volume filename, so a pass
# that finds nothing new costs a listing and no renders.

import threading as _th

_warm_lock = _th.Lock()
_warm_started = False

# Region -> (sites, tag). Kept short on purpose: each site is ~20 s,
# so a four-site region is over a minute and the whole rotation has to
# finish inside the volume cadence to stay current.
ROTATION = {
    "N90": (["KOKX", "KDIX"], "n90"),
    "MCO": (["KMLB", "KTBW"], "mco"),
}
WARM_FRAMES = int(os.environ.get("L2_WARM_FRAMES", "6"))
WARM_SLEEP_S = int(os.environ.get("L2_WARM_SLEEP_S", "120"))


def _warm_log(outdir, msg):
    from datetime import datetime, timezone
    from pathlib import Path as _PP

    try:
        with open(_PP(outdir) / "radar_warmer.log", "a") as fh:
            fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                     f"{msg}\n")
    except OSError:
        pass


def _warm_daemon(outdir):
    _warm_log(outdir, "radar warmer started")
    while True:
        for name, (sites, tag) in ROTATION.items():
            try:
                t0 = time.time()
                diag = {}
                frames, diag = build_loop(
                    sites[0], WARM_FRAMES, outdir, tag=tag, diag=diag)
                built = sum(1 for v in diag.get("frames", {}).values()
                            if v != "cached")
                _warm_log(outdir,
                          f"{name}: {len(frames)} frames "
                          f"({built} new) in {time.time() - t0:.0f}s")
            except Exception as exc:
                _warm_log(outdir, f"{name} FAILED: "
                                  f"{type(exc).__name__}: {exc}")
        time.sleep(WARM_SLEEP_S)


def ensure_radar_warmer(outdir) -> None:
    """Idempotent; starts one background thread per process.

    L2_WARMER=off is the kill switch — this does real work every two
    minutes and must be stoppable without a deploy.
    """
    if os.environ.get("L2_WARMER", "on").lower() == "off":
        return
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return
        _th.Thread(target=_warm_daemon, args=(outdir,), daemon=True,
                   name="l2-radar-warmer").start()
        _warm_started = True


def warm_frames(outdir, tag, n=None):
    """Frames already on disk for a tag, oldest first.

    Reads the filesystem rather than any manifest, so a page can show
    whatever the warmer has produced without coordinating with it.
    Volume filenames sort chronologically, which is what makes this
    work: KOKX20260818_193112_V06 sorts after ..._192612_V06.
    """
    from pathlib import Path as _PP

    files = sorted(_PP(outdir).glob(f"l2loop_{tag}_*.png"))
    if n:
        files = files[-n:]
    out = []
    for f in files:
        stem = f.stem.split("_")
        hhmm = stem[-2][:6] if len(stem) >= 3 else "?"
        out.append({"name": f.name,
                    "valid": f"{hhmm[:2]}:{hhmm[2:4]}:{hhmm[4:6]}Z"})
    return out

