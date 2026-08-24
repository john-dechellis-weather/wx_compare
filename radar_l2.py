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
    # TWO SITES. KOKX and KDIX are the only radars with the N90
    # terminal area inside their 52 nm circle — everything else is
    # coverage, not resolution. Starting here keeps the build fast
    # and the merge legible: one overlap region, one pair to reason
    # about. TDWR and the northern 88Ds are in other regions when
    # they are wanted.
    "N90 merged (S+C band)": ["KOKX", "KDIX"],
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
    "FLL-MIA merged (S+C)": ["KAMX", "TMIA", "TFLL", "TDJT"],
    # The whole peninsula, four TDWRs plus four 88Ds.
    #
    # KTBW and KMLB are NOT optional padding. Measured over this box:
    # the four TDWRs plus KAMX leave 38% of it with no coverage at
    # all and only 21% seen by two or more radars, because a TDWR is
    # a 90 km instrument and Tampa/Orlando sit 200+ km from the
    # Miami group. Worse, TTPA overlaps NOTHING in that set, so the
    # calibration solver could never estimate its bias and it would
    # float in the mosaic at whatever offset it happens to carry.
    # Adding the two S-band sites takes no-coverage to 3% and
    # calibratable area to 63%, and gives TTPA and TMCO each an
    # S-band neighbour to be anchored to.
    # TDWR ONLY, no S-band. Central and south Florida.
    #
    # Note the geometry this exposes: TMIA-TFLL are 33 km apart and
    # TFLL-TDJT about 60 km, so the south Florida three overlap
    # heavily and blend. TTPA and TMCO are 200+ km from that group
    # and from each other, so they render as isolated 90 km discs
    # with nothing to blend against. That separation IS the result —
    # it shows what TDWR alone can and cannot cover.
    #
    # Run it as: L2_TDWR_PRODUCT=TZ0 (lowest tilt only),
    # L2_TOP_M=6000 with LEVELS=1 (one deep layer, so the climbing
    # beam lands in it whole rather than slicing into range rings),
    # L2_DBZ_MIN=10, and L2_CBAND_CORRECT=off for genuinely raw.
    # Two-site S+C merge test bed. Better geometry than the NY pair
    # for exactly this: KLIX and TMSY are 68 km apart (vs 33 km for
    # TJFK/TEWR), so they view the terminal area from 147 deg apart
    # over the field, 170 deg over Lake Pontchartrain and 117 deg
    # over downtown. Nearly opposed views mean nearly independent
    # attenuation paths — a core that blinds the C-band is broadside
    # to the S-band. 43% of the box is seen by BOTH, which is the
    # area that actually exercises the merge algorithms.
    "MSY / New Orleans (S+C)": ["KLIX", "TMSY"],
    "FL TDWR only (raw)": ["TMCO", "TTPA", "TDJT", "TFLL", "TMIA"],
    "Florida Peninsula (S+C)": ["KAMX", "KTBW", "KMLB",
                                "TMIA", "TFLL", "TDJT", "TMCO",
                                "TTPA"],
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
    # Enlarged to make the two new sites worth their fetch time:
    # -77.17..-71.43, 39.22..43.18. 0.6% uncovered, 82% multi-radar.
    # JFK-centred, +-250 km (270 x 270 nm). Sized so the grid stays
    # at the full 250 m: 2000 x 2000 = 4.0M cells, under the 4.5M
    # ceiling. The 400 nm box needed 8.8M and had to be coarsened to
    # 350 m to render at all, which threw away the resolution the
    # whole exercise is for.
    "N90 merged (S+C band)": (40.6398, -73.7789, 250.0, 250.0),
    "NY Metro TDWR pair": (40.62, -74.07, 90, 80),
    # Union of two 30 nm circles 33 km apart, plus margin.
    "NY Metro 1.0deg proto": (40.59, -74.07, 80, 65),
    "FLL-MIA TDWR pair": (25.95, -80.30, 80, 70),
    "FLL-MIA merged (S+C)": (26.10, -80.35, 150, 140),
    "MSY / New Orleans (S+C)": (30.10, -90.15, 120, 120),
    "FL TDWR only (raw)": (27.05, -81.55, 195, 235),
    "Florida Peninsula (S+C)": (26.90, -81.30, 210, 195),
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
    "TTPA": "TDWR at TPA",
    "TMCO": "TDWR at MCO",
    "KLIX": "Slidell LA — the New Orleans WSR-88D (NOT KHDC, "
            "which is Hammond Northshore airport)",
    "TMSY": "TDWR at MSY, 14 km from the field",
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
    "TMSY": "TDWR at MSY",
    "TMIA": "TDWR at MIA", "TFLL": "TDWR at FLL",
    "TDJT": "TDWR at DJT (was PBI)",
    "TPBI": "TDWR at DJT (was PBI)", "TMCO": "TDWR at MCO",
    "TTPA": "TDWR at TPA",
}
N90_TDWR = ["TJFK", "TEWR"]

# Two-way C-band specific attenuation, k = A * Z**B dB/km. Measured
# consequence at these coefficients: a TDWR looking through a 55 dBZ
# core for 10 km reads about 10.6 dB LOW on everything behind it, and
# 21 dB low through 20 km. S-band over the same path loses under
# 0.5 dB. That difference is not noise — it is the single reason
# TDWR cannot simply be dropped into the blend.
# Set L2_CBAND_CORRECT=off to see TDWR exactly as it comes off the
# wire — attenuated, uncorrected. Useful for judging how bad the
# C-band problem actually is on a given day before deciding how much
# to trust the correction.
CBAND_CORRECT = os.environ.get("L2_CBAND_CORRECT", "on").lower() != "off"
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
# Sites must also be sampling at a comparable ELEVATION ANGLE. Live
# run 8/20 produced pair differences of +7.45, +7.82 and +8.50 dB and
# solved offsets of +4.47 and -6.00 — implausible as calibration,
# which is 1-3 dB. The cause is that equal beam HEIGHT is not the
# same as equal beam GEOMETRY: a 0.3 deg beam at long range and a
# 0.5 deg beam at short range can pass through the same altitude
# while sampling completely different volumes of a storm, and the
# difference between them is structure, not bias.
CAL_MAX_DTILT = float(os.environ.get("L2_CAL_MAX_DTILT", "0.35"))
# And a solved offset this large is a failed solve, not a miscalibrated
# radar. Report it, do not apply it.
CAL_SANITY_DB = float(os.environ.get("L2_CAL_SANITY_DB", "4.0"))

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
# Values are CANDIDATE ids, tried in order. West Palm Beach is the
# reason: the airport is now DJT, but a radar-site id and an airport
# code do not have to move together and Unidata's catalog may still
# carry the old PBI. Trying both costs one extra listing on a miss
# and removes the guess.
TDWR_ID3 = {"TJFK": ["JFK"], "TEWR": ["EWR"], "TPHL": ["PHL"],
            "TBOS": ["BOS"], "TDCA": ["DCA"], "TBWI": ["BWI"],
            "TIAD": ["IAD"], "TMSY": ["MSY"],
            "TMIA": ["MIA"], "TFLL": ["FLL"],
            "TDJT": ["DJT", "PBI"], "TPBI": ["DJT", "PBI"],
            "TMCO": ["MCO"], "TTPA": ["TPA"]}
# Base reflectivity tilts 1-3. TZL is the long-range product but it is
# resampled to 300 m, which throws away the resolution that made TDWR
# worth having.
TDWR_PRODUCTS = ["TZ0", "TZ1", "TZ2"]


def _nids_sweep(raw):
    """Parse one NIDS file into raw arrays. No pyart object yet."""
    import numpy as np
    from metpy.io import Level3File

    f = Level3File(io.BytesIO(raw))
    blk = f.sym_block[0][0]
    data = np.asarray(f.map_data(blk["data"]), dtype="float32")
    nrays, ngates = data.shape
    # Volume time from the NIDS header. Without it the assembled
    # radar keeps pyart's default epoch and datetime_from_radar
    # reports 1970 — which showed up as scan_spread_s and
    # detail_age_s of 1,187,652,841 and killed motion estimation
    # with "unusable interval".
    vt = None
    for k_ in ("vol_time", "prod_time", "valid_time"):
        v_ = f.metadata.get(k_)
        if hasattr(v_, "year"):
            vt = v_
            break
    if vt is not None and vt.tzinfo is None:
        vt = vt.replace(tzinfo=timezone.utc)
    return {
        "data": data,
        "time": vt,
        "az": np.asarray(blk["start_az"][:nrays], dtype="float32"),
        "el": float(f.metadata.get("el_angle", 0.5)),
        "gate_m": float(f.max_range) * 1000.0 / ngates,
        "lat": float(f.lat), "lon": float(f.lon),
        "alt": float(getattr(f, "height", 0.0)),
    }


def _radar_from_sweeps(sweeps):
    """Several NIDS sweeps -> ONE pyart Radar, assembled by hand.

    pyart.util.join_radar was the obvious route and it produced a
    radar whose field arrays had 1080 rays while its ray-indexed
    metadata still had 360 — the gridder then raised "boolean index
    did not match indexed array, size 1080 vs 360". Building the
    object directly means the shapes cannot disagree.

    Sweeps are truncated to a common ray and gate count. TDWR
    products are 360 x 600 at 150 m, but a mismatch must not throw.
    """
    import numpy as np

    import pyart

    nrays = min(s_["data"].shape[0] for s_ in sweeps)
    ngates = min(s_["data"].shape[1] for s_ in sweeps)
    n = len(sweeps)
    radar = pyart.testing.make_empty_ppi_radar(ngates, nrays, n)
    radar.range["data"] = ((np.arange(ngates) + 0.5)
                           * sweeps[0]["gate_m"]).astype("float32")
    radar.azimuth["data"] = np.concatenate(
        [s_["az"][:nrays] for s_ in sweeps]).astype("float32")
    radar.elevation["data"] = np.concatenate(
        [np.full(nrays, s_["el"], dtype="float32") for s_ in sweeps])
    radar.fixed_angle["data"] = np.array(
        [s_["el"] for s_ in sweeps], dtype="float32")
    radar.latitude["data"] = np.array([sweeps[0]["lat"]])
    radar.longitude["data"] = np.array([sweeps[0]["lon"]])
    radar.altitude["data"] = np.array([sweeps[0]["alt"]])
    radar.time["data"] = np.zeros(nrays * n, dtype="float64")
    stack = np.vstack([s_["data"][:nrays, :ngates] for s_ in sweeps])
    radar.add_field(
        "reflectivity",
        {"data": np.ma.masked_invalid(stack), "units": "dBZ",
         "_FillValue": -9999.0, "standard_name": "reflectivity"},
        replace_existing=True)
    return radar


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

    cands = TDWR_ID3.get(site.upper(), [site.upper()[-3:]])
    if isinstance(cands, str):
        cands = [cands]
    s3 = cands[0]
    tried = []
    sweeps, used = [], []
    now = datetime.now(timezone.utc)
    wanted = ([TDWR_PRODUCT_ONLY] if TDWR_PRODUCT_ONLY
              else TDWR_PRODUCTS[:max(1, min(TILTS,
                                             len(TDWR_PRODUCTS)))])
    for prod in wanted:
        ds = None
        for cand in cands:
          for day in (now, now - _td(days=1)):
            for url in _tdwr_catalog_urls(prod, cand, day):
                try:
                    from siphon.catalog import TDSCatalog

                    cat = TDSCatalog(url)
                    names = sorted(cat.datasets.keys(), reverse=True)
                    if not names:
                        tried.append(f"{cand}/{prod}: empty")
                        continue
                    ds = cat.datasets[names[0]]
                    s3 = cand
                    tried.append(f"{cand}/{prod}: {len(names)} datasets")
                    break
                except Exception as exc:
                    tried.append(f"{cand}/{prod}: "
                                 f"{type(exc).__name__}")
            if ds is not None:
                break
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
            _sw = _nids_sweep(raw)
            sweeps.append(_sw)
            used.append(f"{prod}@{_sw['el']:.1f}deg")
        except Exception as exc:
            tried.append(f"{prod} download/parse: "
                         f"{type(exc).__name__}: {exc}")
    diag.setdefault("tdwr_probe", {})[site] = tried[:8]
    if not sweeps:
        raise RuntimeError(
            f"no Level III tilts for {site} ({s3}); tried "
            + " | ".join(tried[:4]))
    radar = _radar_from_sweeps(sweeps)
    vt = next((sw.get("time") for sw in sweeps if sw.get("time")), None)
    diag.setdefault("tdwr", {})[site] = (
        f"{s3} tilts {'+'.join(used)}"
        + (f" @ {vt:%H:%M:%SZ}" if vt else " (no volume time)"))
    return radar, vt


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
# Ceiling on grid cells per level. A box and a resolution are chosen
# independently, and their product is what actually has to fit in
# memory — the 400x400 nm N90 box at 250 m is 8.8M cells, which
# OOM-killed the renderer. Rather than fail, the grid coarsens to fit
# and says so, because a slightly softer picture beats a 502.
MAX_CELLS = float(os.environ.get("L2_MAX_CELLS", "4.5e6"))
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
# RAW mode. One switch, so a single flag guarantees no smoothing is
# hiding anywhere: nearest-neighbour gridding, ROI floor at the grid
# cell, no render Gaussian, no seam matching. Intended as the base
# for a custom TDWR/WSR combiner — you cannot judge a blending
# algorithm through three layers of blur applied after it.
# DEFAULT ON. Raw is the base state now: a custom TDWR/WSR combiner
# is being developed against this output, and a blending algorithm
# cannot be judged through three layers of blur applied after it.
# Set L2_RAW=off to restore Barnes2 + Gaussian + seam matching.
RAW_MODE = os.environ.get("L2_RAW", "on").lower() != "off"
WEIGHT_FN = ("nearest" if RAW_MODE
             else os.environ.get("L2_WEIGHT", "Barnes2"))
# Radius-of-influence floor. This is the smoothing/peak trade: a
# planted 58 dBZ core survived intact at 500 and 1000 m but lost
# 2.6 dB at 2000 m. 1000 m smooths the blocks without eating cores.
# Radius-of-influence FLOOR, per band. This was one shared 1000 m
# value and it was the single biggest destroyer of resolution in the
# pipeline. Barnes2 averages every gate inside the ROI, so a 1000 m
# floor makes each cell a mean over a 1 km ball — at 20 km a TDWR
# resolves 349 m and we were averaging over 1000, throwing away 3x
# the detail before the render smoother even ran. Compared against a
# real TDWR display the difference was stark: fine radial structure
# and individual cells there, smooth blobs here.
#
# pyart's dist_beam ROI already grows with range on its own. The
# floor only needs to stop it collapsing below the grid cell, so it
# should be near RES_M, not far above it.
MIN_RADIUS_BAND = {
    "C": float(os.environ.get("L2_MIN_RADIUS_C_M", "150")),
    "S": float(os.environ.get("L2_MIN_RADIUS_S_M", "250")),
}
# In RAW mode _grid_one uses RES_M directly instead of these — the
# floor becomes one grid cell, which is the smallest value that still
# stops the ROI collapsing to nothing.
MIN_RADIUS_M = MIN_RADIUS_BAND["S"]
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
# 0.4, not 1.0: at a 250 m grid a sigma of 1.0 is another 250 m of
# blur on top of the gridder, which on TDWR data is most of what
# separates a cell from a blob. Enough to take the edge off gate
# wedges, not enough to erase structure.
SMOOTH_SIGMA = (0.0 if RAW_MODE
                else float(os.environ.get("L2_SMOOTH_SIGMA", "0.4")))

# NWS/AWIPS reflectivity table — the one every US radar display uses,
# 5 dBZ steps from 5 to 75. Anchors are the standard hex values, not
# an approximation of them, so a 45 dBZ cell here is the same orange
# a controller or a dispatcher already reads as 45.
AWIPS_LEVELS = list(range(5, 80, 5))
AWIPS_COLORS = [
    "#04E9E7",  #  5-10  light cyan
    "#019FF4",  # 10-15  blue
    "#0300F4",  # 15-20  dark blue
    "#02FD02",  # 20-25  green
    "#01C501",  # 25-30
    "#008E00",  # 30-35  dark green
    "#FDF802",  # 35-40  yellow
    "#E5BC00",  # 40-45
    "#FD9500",  # 45-50  orange
    "#FD0000",  # 50-55  red
    "#D40000",  # 55-60
    "#BC0000",  # 60-65  dark red
    "#F800FD",  # 65-70  magenta
    "#9854C6",  # 70-75  purple
    "#FDFDFD",  # 75+    white
]
# "steps" is authentic AWIPS: hard 5 dBZ bands. "smooth" keeps the
# same anchor colours but interpolates between them, which suits the
# 250 m grid better — at this resolution hard bands read as terracing
# on a gradient rather than as information.
# AWIPS steps, not the interpolated ramp — banding is honest here.
RAMP_MODE = os.environ.get("L2_RAMP", "steps").lower()

# ---------------------------------------------------------------------------
# RAW BY DEFAULT — every smoothing stage is off
# ---------------------------------------------------------------------------
# Defaults as of 8/19: SMOOTH_SIGMA 0, RES_MATCH off, WEIGHT_FN
# nearest, ROI floors at the grid cell. Nothing averages, nothing
# blurs, nothing matches texture across a seam. What you see is what
# the radars reported, resampled onto a common grid and nothing more.
#
# It will look bad: gate wedges, hard coverage edges, calibration
# steps between sites, speckle. That is the point — every artifact is
# now visible and attributable instead of hidden under a blur.
#
# The old machinery is still here and still tested; it is just not
# wired on. To bring any of it back:
#     L2_WEIGHT=Barnes2       gridding-stage averaging
#     L2_SMOOTH_SIGMA=0.4     Gaussian on the finished field
#     L2_RES_MATCH=on         gradient-keyed seam matching
#
# ---------------------------------------------------------------------------
# CUSTOM COMBINER HOOK
# ---------------------------------------------------------------------------
# Set COMBINE_FN to your own callable and build_mosaic will use it
# instead of the built-in weighted blend. Signature:
#
#     COMBINE_FN(staged, diag) -> ndarray (levels, ny, nx) in dBZ
#
# `staged` is a list, one entry per radar that loaded, of:
#     (field, rlat, rlon, tilt_deg, scan_time, site, site_alt_m)
# where `field` is that site's data ALREADY GRIDDED onto the common
# grid, dBZ, NaN where it saw nothing. Everything is on the same grid
# so the arrays line up cell for cell.
#
# Helpers available, all documented at their definitions:
#     _site_band(site)                 -> "C" (TDWR) or "S" (WSR-88D)
#     _site_weight(lat, lon, shape, tilt, age_s, site)
#                                      -> range/height/time weights
#     _beam_height_map(lat, lon, tilt, alt, shape)
#                                      -> sampling height MSL per cell
#     SITE_RANGE_M / SITE_BEAMWIDTH    -> per-band geometry
#     _cband_attenuation(radar)        -> PIA, already applied on load
#
# Return NaN where nothing should be drawn. Return dBZ, not linear Z —
# the built-in blend converts internally because averaging logarithms
# is wrong, and any combiner that averages should do the same.
COMBINE_FN = None
# Range scale for blend weights. Default is the 52 nm crossover
# where beam spreading makes L2 coarser than MRMS = 96 km.
BLEND_M = float(os.environ.get("L2_BLEND_M", "96000"))
# Per-band range scale. One shared 96 km scale was wrong: measured at
# FLL, the TDWR 21 km away contributed only 40% of the value while
# sites at 49, 57 and 70 km supplied the rest, so the effective sample
# width came out 394 m instead of the 202 m TFLL actually resolves.
# The blend was averaging away the advantage the whole design exists
# to capture. A TDWR is only better inside ~50 km, so its vote should
# fade on that scale, not on the S-band's.
BLEND_BAND_M = {
    "C": float(os.environ.get("L2_BLEND_C_M", "45000")),
    "S": float(os.environ.get("L2_BLEND_S_M", "96000")),
}
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

# --- seam control ------------------------------------------------------
# Each radar's usable range, and where its vote starts fading. Without
# this a TDWR still carries weight 0.415 at 90 km — and then its data
# simply stops, so the blend denominator falls off a cliff and draws a
# ring at the edge of coverage. Calibration cannot fix that; only
# tapering the weight to zero BEFORE the data ends can.
# S-band raised to 459 km (248 nm), the full Level II reflectivity
# range. Worth knowing what that buys: at 248 nm the 0.5 deg beam is
# 16.4 km AGL — above almost all weather — so it is not more coverage
# of a storm, it is a much higher slice of one. 100 nm is already
# 3.6 km and missing surface returns. The range weight fades hard
# with distance, so far-field echo arrives at low weight and fills
# box corners rather than replacing anything nearer.
SITE_RANGE_M = {"C": 90000.0,
                "S": float(os.environ.get("L2_S_RANGE_M", "459000"))}
# Fraction of max range where the taper begins.
EDGE_TAPER_FROM = float(os.environ.get("L2_EDGE_TAPER_FROM", "0.72"))

# Beamwidths, for the resolution-matching pass.
SITE_BEAMWIDTH = {"C": 0.55, "S": 0.50}
# Match texture across the seam. A TDWR resolves ~200 m at 20 km; an
# 88D covering the same ground from 150 km resolves ~1300 m. Even with
# identical dBZ the FINE STRUCTURE stops where TDWR coverage ends, and
# the eye reads that as an edge. Smoothing each cell by its own
# effective sample width makes texture continuous. off = disable.
RES_MATCH = (False if RAW_MODE
             else os.environ.get("L2_RES_MATCH", "on").lower() != "off")
# How far to look when deciding what "local" resolution means, and the
# hardest blur allowed. Both keep the match to the seam rather than
# letting it flatten the whole field.
# Scale must be SMALL relative to a coverage region: at 250 m cells a
# TDWR disc is ~360 cells across, so a 12-cell scale confines the
# blur to a ~3 km band at the handover and leaves the interior alone.
RES_MATCH_SCALE_CELLS = float(
    os.environ.get("L2_RES_MATCH_SCALE", "12"))
RES_MATCH_GAIN = float(os.environ.get("L2_RES_MATCH_GAIN", "6.0"))
RES_MATCH_MAX_SIGMA = float(os.environ.get("L2_RES_MATCH_MAX", "2.0"))
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
    scan_t = None
    src = ""
    notes = []

    # --- TDWR: Level III, not Level II -------------------------------
    if site.upper() in TDWR_SITES:
        try:
            radar, _tdwr_t = _load_tdwr(site, diag)
            scan_t = _tdwr_t
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
            # scan_time may be naive depending on which tier of the
            # chain answered; normalise before subtracting or this
            # raises AFTER radar is already loaded, which silently
            # blanked the source label.
            _st = scan.scan_time
            if _st.tzinfo is None:
                _st = _st.replace(tzinfo=timezone.utc)
            age = (now - _st).total_seconds() / 60.0
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
    if site.upper() in TDWR_SITES and CBAND_CORRECT:
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
    return radar, scan_t


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
def _fit_resolution(diag=None):
    """Grid spacing that keeps the cell count under MAX_CELLS.

    Returns RES_M unchanged when it already fits. When it does not,
    coarsens to the nearest 50 m that does and RECORDS it — silently
    rendering at a different resolution than asked for would be worse
    than the failure it prevents.

    This exists because box size and resolution are chosen
    independently and their PRODUCT is what has to fit in memory. The
    400x400 nm N90 box at 250 m is 8.8M cells per level, which
    OOM-killed the renderer and returned 502 from Render.
    """
    import math

    want = (2 * HALF_X_M / RES_M) * (2 * HALF_Y_M / RES_M)
    if want <= MAX_CELLS:
        return RES_M
    scale = math.sqrt(want / MAX_CELLS)
    res = math.ceil(RES_M * scale / 50.0) * 50.0
    if diag is not None:
        n2 = (2 * HALF_X_M / res) * (2 * HALF_Y_M / res)
        diag["resolution_capped"] = (
            f"{RES_M:.0f} m would be {want / 1e6:.1f}M cells, over the "
            f"{MAX_CELLS / 1e6:.1f}M ceiling; using {res:.0f} m "
            f"({n2 / 1e6:.1f}M)")
    return res


def _grid_one(radar, diag=None, site=None):
    """Grid one radar onto the shared target grid; return float32."""
    # RAW: floor at the grid cell itself — any smaller is meaningless
    # and pyart rejects zero. Otherwise the per-band floor.
    _roi_floor = (RES_M if RAW_MODE
                  else MIN_RADIUS_BAND[_site_band(site)])
    import numpy as np
    import pyart

    res = _fit_resolution(diag)
    nx = int(2 * HALF_X_M / res)
    ny = int(2 * HALF_Y_M / res)
    g = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(LEVELS, ny, nx),
        grid_limits=((BASE_M,
                      TOP_M if TOP_M else
                      BASE_M + 1000.0 * max(LEVELS - 1, 1)),
                     (-HALF_Y_M, HALF_Y_M), (-HALF_X_M, HALF_X_M)),
        fields=["reflectivity"],
        weighting_function=WEIGHT_FN,
        min_radius=_roi_floor,
        gatefilters=(gatefilter(radar, diag, site),),
        grid_origin=GRID_CENTER,
    )
    arr = np.ma.filled(
        g.fields["reflectivity"]["data"], np.nan).astype("float32")
    del g
    return arr, (float(radar.latitude["data"][0]),
                 float(radar.longitude["data"][0]))


def _site_band(site):
    return "C" if str(site).upper() in TDWR_SITES else "S"


def _site_weight(rlat, rlon, shape, tilt_deg=0.5, age_s=0.0,
                 site=None):
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
    _d0 = BLEND_BAND_M[_site_band(site)]
    w = np.exp(-d2 / (_d0 * _d0)).astype("float32")

    # Cosine taper to zero at the site's usable range, so its vote is
    # already gone by the time its data ends.
    rmax = SITE_RANGE_M[_site_band(site)]
    r0 = rmax * EDGE_TAPER_FROM
    d1 = np.sqrt(d2, dtype="float32")
    taper = np.ones_like(w)
    edge = (d1 >= r0) & (d1 < rmax)
    taper[edge] = 0.5 * (1.0 + np.cos(
        np.pi * (d1[edge] - r0) / (rmax - r0)))
    taper[d1 >= rmax] = 0.0
    w *= taper

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
                age_s=0.0, site=None, res=None):
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
    w = _site_weight(rlat, rlon, arr.shape, tilt_deg, age_s,
                     site) * ok
    z = np.zeros_like(arr)
    np.power(10.0, arr / 10.0, out=z, where=ok)
    if num is None:
        num = np.zeros_like(arr)
        den = np.zeros_like(arr)
    num += (z * w).astype("float32")
    den += w.astype("float32")
    if res is not None:
        # Effective sample width of THIS site at each cell, carried
        # as a weighted sum so the finished map says how well
        # resolved each cell actually is.
        import math as _m

        lat0, lon0 = GRID_CENTER
        rx = (rlon - lon0) * 111320.0 * _m.cos(_m.radians(lat0))
        ry = (rlat - lat0) * 111320.0
        ny, nx = arr.shape[-2], arr.shape[-1]
        ax = np.linspace(-HALF_X_M, HALF_X_M, nx, dtype="float32")
        ay = np.linspace(-HALF_Y_M, HALF_Y_M, ny, dtype="float32")
        d = np.sqrt(((ax - rx) ** 2)[None, :] + ((ay - ry) ** 2)[:, None])
        bw = _m.radians(SITE_BEAMWIDTH[_site_band(site)])
        # Floor at a gate length: d*bw goes to zero AT the radar,
        # which reported an absurd "best 4 m" resolution.
        _samp = np.maximum(d * bw, 150.0).astype("float32")
        res[0] += (np.broadcast_to(_samp, arr.shape) * w).sum(axis=0)
        res[1] += w.sum(axis=0)
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
        if abs(staged[ia][3] - staged[ib][3]) > CAL_MAX_DTILT:
            diag.setdefault("cal_skipped", {})[
                f"{names[ia]}-{names[ib]}"] = (
                f"tilts {staged[ia][3]:.1f} vs {staged[ib][3]:.1f} deg "
                f"differ by more than {CAL_MAX_DTILT} deg")
            continue
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
    rejected = {}
    for i, n_ in enumerate(names):
        b = float(sol[i])
        if abs(b) > CAL_SANITY_DB:
            rejected[n_] = f"{b:+.2f} dB"
            continue
        b = float(np.clip(b, -CAL_MAX_DB, CAL_MAX_DB))
        if abs(b) > 0.05:
            out[n_] = b
    if rejected:
        diag["cal_rejected"] = (
            ", ".join(f"{k} {v}" for k, v in rejected.items())
            + f" — beyond {CAL_SANITY_DB:.0f} dB, treated as a failed "
              "solve and NOT applied")
    diag["calibration"] = ", ".join(
        f"{n_} {b:+.2f} dB" for n_, b in out.items()) or "all within 0.05 dB"
    return out


# Last resolution map, handed to the renderer without changing the
# build_mosaic signature that pages already call.
_RES_MAP = []




# ---------------------------------------------------------------------------
# Custom combiner hook
# ---------------------------------------------------------------------------
# Set COMBINER to your own function and it replaces the built-in
# weighted blend entirely. Signature:
#
#     combiner(fields, meta) -> ndarray (nz, ny, nx) of dBZ, NaN where
#                               nothing was observed
#
#   fields  list of (nz, ny, nx) float32 dBZ arrays, one per site,
#           already gridded onto the SAME grid, NaN where that site
#           saw nothing. Nothing has been smoothed or averaged
#           across sites.
#   meta    list of dicts, same order, each with:
#             site      "TMIA" / "KAMX"
#             band      "C" or "S"
#             lat, lon  radar position
#             tilt      lowest elevation angle, degrees
#             age_s     seconds behind the newest volume in the set
#             alt_m     site altitude MSL
#             range_m   usable range of that radar
#             beamwidth degrees
#
# The grid itself is described by GRID_CENTER, HALF_X_M, HALF_Y_M,
# RES_M, BASE_M/TOP_M and LEVELS, all module globals at call time —
# so per-cell range and beam height are reconstructible with
# _beam_height_map() and _site_weight() if you want them.
#
# Return dBZ, not linear Z. The built-in blend converts internally
# because averaging in log space is wrong, but a combiner that never
# averages does not need to care.
COMBINER = None


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
    res_acc = [None, None]      # [sum(res*w), sum(w)] per cell
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
                if t is None:
                    try:
                        import pyart
                        t = pyart.util.datetime_from_radar(radar)
                        if t.year < 2000:
                            t = None   # pyart's default epoch
                        elif t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
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

    if staged and COMBINER is not None:
        # Custom combiner: hand over the raw per-site grids and use
        # whatever it returns. The built-in weighting, linear-Z
        # averaging and column max below are all bypassed.
        newest = max((x[4] for x in staged if x[4]), default=None)
        _fields = [x[0] for x in staged]
        _meta = [{
            "site": x[5], "band": _site_band(x[5]),
            "lat": x[1], "lon": x[2], "tilt": x[3],
            "alt_m": x[6],
            "age_s": ((newest - x[4]).total_seconds()
                      if (newest and x[4]) else 0.0),
            "range_m": SITE_RANGE_M[_site_band(x[5])],
            "beamwidth": SITE_BEAMWIDTH[_site_band(x[5])],
        } for x in staged]
        try:
            blended = COMBINER(_fields, _meta)
            diag["combiner"] = getattr(COMBINER, "__name__", "custom")
        except Exception as exc:
            diag["combiner"] = (f"FAILED {type(exc).__name__}: {exc}"
                                " — fell back to the built-in blend")
            blended = None
        if blended is not None:
            staged.clear()
            gc.collect()
            comp = np.nanmax(blended, axis=0)
            del blended
            diag["cells"] = int(comp.size)
            diag["wall_s"] = round(time.time() - t_all, 1)
            diag["coverage_pct"] = round(
                100.0 * float(np.isfinite(comp).sum()) / comp.size, 1)
            return comp, diag

    if staged and COMBINE_FN is not None:
        # Custom combiner takes the whole blending step.
        try:
            blended = COMBINE_FN(staged, diag)
            diag["blend"] = f"custom: {getattr(COMBINE_FN, '__name__', '?')}"
            comp = np.nanmax(blended, axis=0)
            del blended
            staged.clear()
            gc.collect()
            if times:
                diag["scan_spread_s"] = int(
                    (max(times) - min(times)).total_seconds())
                diag["valid"] = max(times).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            diag["cells"] = int(comp.size)
            diag["wall_s"] = round(time.time() - t_all, 1)
            diag["coverage_pct"] = round(
                100.0 * float(np.isfinite(comp).sum()) / comp.size, 1)
            return comp, diag
        except Exception as exc:
            diag["combine_error"] = (f"{type(exc).__name__}: {exc} "
                                     "- fell back to built-in blend")

    if staged:
        newest = max((x[4] for x in staged if x[4]), default=None)
        bias = _solve_biases(staged, diag)
        for arr, rla, rlo, tilt, t, site, _alt in staged:
            b = bias.get(site)
            if b:
                arr = arr - b        # bring this site onto the S-band scale
            age = ((newest - t).total_seconds()
                   if (newest and t) else 0.0)
            if res_acc[0] is None:
                res_acc[0] = np.zeros(arr.shape[-2:], dtype="float32")
                res_acc[1] = np.zeros(arr.shape[-2:], dtype="float32")
            num, den = _accumulate(num, den, arr, rla, rlo, tilt, age,
                                   site, res_acc)
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
    if res_acc[0] is not None:
        with np.errstate(invalid="ignore", divide="ignore"):
            res_map = np.where(res_acc[1] > 0,
                               res_acc[0] / res_acc[1], np.nan)
        diag["resolution_m"] = (
            f"best {np.nanmin(res_map):.0f} m, "
            f"median {np.nanmedian(res_map):.0f} m, "
            f"worst {np.nanpercentile(res_map, 95):.0f} m")
        _RES_MAP.clear()
        _RES_MAP.append(res_map)
    del blended
    gc.collect()
    if RAW_MODE:
        diag["raw_mode"] = ("ON — nearest-neighbour grid, ROI = one "
                            "cell, no render smoothing, no seam "
                            "matching. Blend weights still apply.")
    diag["blend"] = (f"range D0={BLEND_M / 1000:.0f} km, "
                     f"vert {BLEND_VERT_M:.0f} m, "
                     f"time {BLEND_TIME_S:.0f} s, "
                     f"sharpness {BLEND_SHARPNESS:g} | "
                     f"grid {WEIGHT_FN} roi>=C{MIN_RADIUS_BAND['C']:.0f}"
                     f"/S{MIN_RADIUS_BAND['S']:.0f} m | "
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
def _resolution_match(field, res_m, base_sigma):
    """Smooth each cell by its OWN effective sample width.

    The seam this fixes is textural, not numerical. Inside 90 km a
    TDWR resolves 200-900 m; the 88D covering the same ground from
    150 km resolves ~1300 m. Values can agree perfectly and the eye
    still sees an edge, because fine structure stops where TDWR
    coverage ends.

    Implemented as a blur stack: smooth the whole field at a few
    fixed sigmas, then pick per cell by interpolating between the two
    bracketing levels. A true per-pixel variable Gaussian is far more
    expensive and indistinguishable at these scales.
    """
    import numpy as np

    if not RES_MATCH or not np.isfinite(res_m).any():
        return _nan_smooth(field, base_sigma)
    # Target sigma in CELLS: enough to blur a cell to the coarsest
    # resolution present, so everything ends up equally soft.
    # Smooth where resolution CHANGES, not where it is coarse.
    #
    # First attempt matched the local resolution LEVEL: every cell was
    # blurred up toward its neighbours' coarseness. Measured on a fine
    # core inside a coarse field, that drove texture inside the core
    # to std 0.00 while the coarse area kept 0.96 — it sanded the
    # TDWR detail flat, which is the opposite of the goal.
    #
    # The seam is a DISCONTINUITY, so the fix keys on the gradient. In
    # a uniform area — fine or coarse — resolution is locally constant
    # and nothing is blurred. Only the band where a TDWR hands over to
    # an 88D gets softened, and only as much as the handover is abrupt.
    from scipy.ndimage import gaussian_filter as _gf

    r = np.nan_to_num(res_m, nan=float(np.nanmedian(res_m)))
    local = _gf(r, RES_MATCH_SCALE_CELLS, mode="nearest")
    disc = np.abs(r - local) / np.maximum(local, 1.0)
    tgt = np.clip(disc * RES_MATCH_GAIN, 0.0, RES_MATCH_MAX_SIGMA)
    tgt = np.nan_to_num(tgt, nan=0.0) + base_sigma
    stack_sigmas = [base_sigma, 1.5, 2.5, 4.0, 6.5]
    stack = [_nan_smooth(field, sg) for sg in stack_sigmas]
    out = np.array(stack[0], copy=True)
    for lo, hi, a_, b_ in zip(stack_sigmas[:-1], stack_sigmas[1:],
                              stack[:-1], stack[1:]):
        m = (tgt > lo) & (tgt <= hi)
        if m.any():
            f = ((tgt[m] - lo) / (hi - lo)).astype("float32")
            out[m] = a_[m] * (1 - f) + b_[m] * f
    m = tgt > stack_sigmas[-1]
    if m.any():
        out[m] = stack[-1][m]
    return out


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

    if POSTFILTER is not None:
        # Post-filter first: it works on structure, and running it
        # after a Gaussian would mean deciding max-vs-mean from
        # texture the blur already removed.
        comp = POSTFILTER(comp[None])[0] if comp.ndim == 2 else \
            POSTFILTER(comp)
    comp = (_resolution_match(comp, _RES_MAP[0], SMOOTH_SIGMA)
            if _RES_MAP else _nan_smooth(comp, SMOOTH_SIGMA))
    if RAMP_MODE == "smooth":
        # Same AWIPS anchors, interpolated to 1 dBZ.
        from matplotlib.colors import LinearSegmentedColormap
        lev = np.arange(AWIPS_LEVELS[0], 75.001, 1.0)
        base = LinearSegmentedColormap.from_list(
            "awips", AWIPS_COLORS, N=256)
        cmap = ListedColormap(base(np.linspace(0, 1, len(lev) - 1)))
    else:
        lev = np.array(AWIPS_LEVELS, dtype=float)
        cmap = ListedColormap(AWIPS_COLORS[:len(lev) - 1])
    cmap.set_over(AWIPS_COLORS[-1])
    cmap.set_bad(alpha=0.0)
    norm = BoundaryNorm(lev, cmap.N)

    ny, nx = comp.shape
    fig = plt.figure(figsize=(nx / 200.0, ny / 200.0), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    # imshow, NOT contourf. Measured 8/22: contourf took 15.8 s on a
    # 1840x1840 grid and was OOM-KILLED on 2963x2963 — which is what
    # the 400x400 nm N90 box became at 250 m, and what returned 502
    # from Render. imshow is 5.5x faster and a fraction of the memory
    # because it does not build contour polygons. With a discrete
    # 5 dBZ palette the two are visually identical anyway: contourf
    # was computing boundaries for a field that is already banded.
    ax.imshow(np.ma.masked_invalid(comp), cmap=cmap, norm=norm,
              origin="lower", interpolation="nearest", aspect="auto")
    fig.savefig(path, format="png", transparent=True,
                bbox_inches=None, pad_inches=0)
    plt.close(fig)
    if diag is not None:
        diag["png_bytes"] = os.path.getsize(path)
        diag["render"] = (f"AWIPS {RAMP_MODE}, {len(lev) - 1} bands, "
                          f"smooth sigma={SMOOTH_SIGMA}")
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
    """Render frames. Returns (frames, diag), oldest first.

    `site` may be a single id or a LIST of ids, and the two take
    different paths for a reason worth stating:

      one frame from a list  -> a true multi-radar MOSAIC of the
                                current volumes, via build_mosaic.
      several frames         -> a single-radar loop from the first
                                site's archive history.

    The split exists because history and mosaicking pull against
    each other. build_mosaic assembles whatever each radar has
    RIGHT NOW; the archive chain can walk back through one radar's
    past volumes, but there is no equivalent for TDWR Level III, and
    even with S-band alone the sites do not scan in step, so a
    "frame at 22:15" would mean seven different moments.

    So a loop is one radar over time, a mosaic is many radars at one
    time, and asking for both at once quietly gets you the loop. The
    diagnostic says which you received.

    Old frames for this tag are pruned to twice the requested depth
    so the disk does not grow without bound.
    """
    from pathlib import Path as _PP

    diag = diag if diag is not None else {}
    diag.setdefault("frames", {})
    sites = [site] if isinstance(site, str) else list(site)

    if len(sites) > 1 and n_frames <= 1:
        from pathlib import Path as _PP

        if progress:
            progress(0.2, f"mosaic of {len(sites)} sites...")
        comp, diag = build_mosaic(sites=sites, diag=diag)
        if comp is None:
            return [], diag
        out = _PP(outdir)
        out.mkdir(parents=True, exist_ok=True)
        # Name it in the SAME shape a volume filename has —
        # SITE + YYYYMMDD_HHMMSS — so warm_frames parses the valid
        # time out of it. The first version wrote MOSAIC20260823_...
        # and warm_frames, which reads the second-to-last underscore
        # field, produced "MO:SA:ICZ" for every frame.
        stamp = (diag.get("valid") or "").replace(":", "").replace(
            "-", "").replace("T", "_").replace("Z", "") or "now"
        name = f"l2loop_{tag}_MOSAIC{stamp}_V06.png"
        render_png(comp, out / name, diag)
        # Prune to a rolling window. The warmer runs continuously,
        # so without this the disk grows by ~2.7 MB every pass.
        keep_n = int(os.environ.get("L2_KEEP_MOSAICS", "12"))
        olds = sorted(out.glob(f"l2loop_{tag}_MOSAIC*.png"))
        for q in olds[:-keep_n]:
            try:
                q.unlink()
            except OSError:
                pass
        diag["mode"] = f"mosaic of {len(sites)} sites"
        return [{"name": name,
                 "valid": (diag.get("valid", "")[-9:-1] + "Z")
                 if diag.get("valid") else "now"}], diag

    if len(sites) > 1:
        diag["mode"] = (f"single-radar loop from {sites[0]} — a "
                        f"multi-frame request cannot be a mosaic; "
                        f"ask for 1 frame to get all "
                        f"{len(sites)} sites")
    else:
        diag["mode"] = f"single-radar loop from {sites[0]}"
    site = sites[0]
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
# Warmed as MOSAICS, one frame each: the region's full site list at
# one frame is the mosaic path in build_loop. Keeping this in step
# with REGIONS is the point — three separate site lists (page, lab,
# warmer) is how the pages came to disagree about what "N90" meant.
ROTATION = {
    "N90": (REGIONS["N90 merged (S+C band)"], "n90"),
    "MCO": (REGIONS["MCO / Orlando"][:2], "mco"),
}
WARM_FRAMES = int(os.environ.get("L2_WARM_FRAMES", "6"))
# 120 s against a 4-6 min volume cadence: every new scan is picked up
# within about two minutes of appearing, and a pass that finds nothing
# new costs one fetch rather than a render. Two sites at ~40 s is a
# ~33% duty cycle, which leaves room for the CAM and overlay warmers.
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


def _mem_mb():
    """Resident set size of this process, MB."""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0


def _warm_daemon(outdir):
    # STAGGERED START. All three warmers are launched together from
    # Homepage, which runs on every page view. Starting them at the
    # same instant put a 2000x2000 mosaic build, a CAM render and an
    # HRRR fetch on the box simultaneously and OOM-killed the
    # service — which surfaces as a 502, not as a warmer error.
    time.sleep(float(os.environ.get("L2_WARM_DELAY_S", "240")))
    _warm_log(outdir, f"radar warmer started (RSS {_mem_mb():.0f} MB)")
    ceiling = float(os.environ.get("L2_WARM_MEM_CEILING_MB", "2200"))
    while True:
        for name, (sites, tag) in ROTATION.items():
            # Skip a pass rather than push the box over. A stale
            # frame is recoverable; an OOM restart loses every
            # warmer's progress at once.
            if _mem_mb() > ceiling:
                _warm_log(outdir, f"{name}: SKIPPED, RSS "
                                  f"{_mem_mb():.0f} MB over "
                                  f"{ceiling:.0f} MB ceiling")
                continue
            try:
                t0 = time.time()
                diag = {}
                # Region geometry too, not just the site list.
                for _rn, _rv in REGION_VIEW.items():
                    if REGIONS.get(_rn) == sites:
                        global GRID_CENTER, HALF_X_M, HALF_Y_M
                        GRID_CENTER = (_rv[0], _rv[1])
                        HALF_X_M, HALF_Y_M = _rv[2] * 1000.0, _rv[3] * 1000.0
                        break
                frames, diag = build_loop(
                    sites, 1, outdir, tag=tag, diag=diag)
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
    # Default OFF: this is the heaviest of the three warmers and
    # starting it alongside the others OOM-killed the box.
    if os.environ.get("L2_WARMER", "off").lower() != "on":
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
    import re

    out = []
    for f in files:
        # Look for HHMMSS immediately after an 8-digit date, rather
        # than trusting a fixed field position — mosaic and
        # single-site names have different shapes.
        m = re.search(r"\d{8}_(\d{6})", f.stem)
        if m:
            h = m.group(1)
            valid = f"{h[:2]}:{h[2:4]}:{h[4:6]}Z"
        else:
            valid = "?"
        out.append({"name": f.name, "valid": valid})
    return out

# ---------------------------------------------------------------------------
# Merge algorithms
# ---------------------------------------------------------------------------
# A menu rather than one answer. Each takes the SAME inputs — every
# site already gridded onto the common grid — and differs only in how
# it decides a cell's value. Switching between them on live weather is
# the fastest way to see what each actually costs.
#
# Contract, same as COMBINE_FN:
#     fn(staged, diag) -> ndarray (levels, ny, nx) dBZ, NaN = nothing
# staged entries are (field, rlat, rlon, tilt, scan_time, site, alt).


def _stack_weights(staged):
    """Per-site weight cubes and their gridded fields."""
    import numpy as np

    newest = max((x[4] for x in staged if x[4]), default=None)
    fields, weights = [], []
    for arr, rla, rlo, tilt, t, site, alt in staged:
        age = ((newest - t).total_seconds()
               if (newest and t) else 0.0)
        w = _site_weight(rla, rlo, arr.shape, tilt, age, site)
        w = np.where(np.isfinite(arr), w, 0.0).astype("float32")
        fields.append(arr)
        weights.append(w)
    return np.stack(fields), np.stack(weights)


def combine_max(staged, diag):
    """Element-wise max. The v1 behaviour, kept as the baseline.

    Sharpest possible — no averaging at all — but a site reading 6 dB
    hot wins everywhere it reaches, so calibration differences appear
    as hard cliffs at coverage edges.
    """
    import numpy as np

    f, _ = _stack_weights(staged)
    with np.errstate(invalid="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmax(f, axis=0)


def combine_nearest(staged, diag):
    """Winner-take-all: each cell takes the single best-weighted site.

    No averaging, so detail and peak values survive intact. Seams fall
    on the equidistant lines between radars rather than at coverage
    edges — a Voronoi tessellation. Often the honest choice when scan
    times differ, because averaging two views of a moving cell smears
    it into a double image while this picks one.
    """
    import numpy as np

    f, w = _stack_weights(staged)
    idx = np.argmax(w, axis=0)
    out = np.take_along_axis(f, idx[None], axis=0)[0]
    return np.where(w.max(axis=0) > 0, out, np.nan)


def combine_quality(staged, diag):
    """Best-resolution-wins: the site with the finest sample volume.

    Range-weighting asks "who is closest"; this asks "who resolved
    this cell best", which is not the same question once a C-band
    150 m radar and an S-band 250 m radar are both in range. Keeps
    TDWR detail out to its full range instead of fading it by
    distance.
    """
    import math

    import numpy as np

    f, w = _stack_weights(staged)
    res = []
    for (arr, rla, rlo, tilt, t, site, alt), wi in zip(staged, w):
        lat0, lon0 = GRID_CENTER
        rx = (rlo - lon0) * 111320.0 * math.cos(math.radians(lat0))
        ry = (rla - lat0) * 111320.0
        ny, nx = arr.shape[-2], arr.shape[-1]
        ax = np.linspace(-HALF_X_M, HALF_X_M, nx, dtype="float32")
        ay = np.linspace(-HALF_Y_M, HALF_Y_M, ny, dtype="float32")
        d = np.sqrt(((ax - rx) ** 2)[None, :] + ((ay - ry) ** 2)[:, None])
        bw = math.radians(SITE_BEAMWIDTH[_site_band(site)])
        r = np.maximum(d * bw, 150.0).astype("float32")
        r = np.broadcast_to(r, arr.shape).copy()
        r[wi <= 0] = np.inf          # no data = infinitely bad
        res.append(r)
    res = np.stack(res)
    idx = np.argmin(res, axis=0)
    out = np.take_along_axis(f, idx[None], axis=0)[0]
    return np.where(np.isfinite(res).any(axis=0), out, np.nan)


def combine_weighted(staged, diag):
    """Weighted mean in linear Z — the built-in blend, as a callable.

    Smoothest transitions of the four. Averaging is also what costs
    it peak values and fine structure, and what smears a cell seen at
    two different times.
    """
    import numpy as np

    f, w = _stack_weights(staged)
    z = np.zeros_like(f)
    ok = np.isfinite(f)
    np.power(10.0, f / 10.0, out=z, where=ok)
    num = (z * w).sum(axis=0)
    den = w.sum(axis=0)
    out = np.full(num.shape, np.nan, dtype="float32")
    good = den > 0
    np.divide(num, den, out=out, where=good)
    np.log10(out, out=out, where=good & (out > 0))
    out *= 10.0
    out[~good] = np.nan
    return out


def combine_weighted_sharp(staged, diag):
    """Weighted mean, then unsharp mask to put the edges back.

    The blend's smoothness is bought by averaging, which rounds off
    gradients. An unsharp pass — subtract a blurred copy, add the
    difference back — restores edge contrast without reintroducing
    the seams, because the seams are low-frequency and the detail is
    high-frequency. L2_UNSHARP sets the amount.
    """
    import numpy as np
    from scipy.ndimage import gaussian_filter

    base = combine_weighted(staged, diag)
    amt = float(os.environ.get("L2_UNSHARP", "0.6"))
    out = np.array(base, copy=True)
    for k in range(out.shape[0]):
        lay = out[k]
        m = np.isfinite(lay)
        if not m.any():
            continue
        filled = np.where(m, lay, 0.0).astype("float32")
        norm = gaussian_filter(m.astype("float32"), 2.0,
                               mode="nearest")
        blur = gaussian_filter(filled, 2.0, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            blur = np.where(norm > 1e-3, blur / norm, lay)
        out[k] = np.where(m, lay + amt * (lay - blur), np.nan)
    return out


COMBINERS = {
    "built-in weighted": None,       # uses the full path incl. bias
    "weighted (linear Z)": combine_weighted,
    "weighted + unsharp": combine_weighted_sharp,
    "nearest site wins": combine_nearest,
    "best resolution wins": combine_quality,
    "element-wise max": combine_max,
}

# ---------------------------------------------------------------------------
# Time alignment
# ---------------------------------------------------------------------------
# The problem: radars do not scan together. A TDWR volume is ~1 min,
# an 88D 4-6, and nothing synchronises them. At 40 kt a cell moves
# 2 nm in 3 minutes, so blending two views of it puts the SAME storm
# in two places and the mean renders it twice, faintly. No weighting
# scheme fixes that — a weighted mean of two displaced copies is
# still two displaced copies.
#
# The fix is to move each field to a common valid time before
# blending. Motion comes from the data itself: phase correlation
# between consecutive grids of the SAME radar gives the domain
# displacement, no external wind field required.

_MOTION_PREV = {}          # site -> (field2d, datetime)
_MOTION_CACHE = {"v": (0.0, 0.0), "at": None}


def _phase_shift(a, b):
    """Displacement in CELLS that best maps a onto b.

    Phase correlation: the cross-power spectrum of two shifted copies
    of the same scene has a phase ramp whose inverse transform is a
    delta at the shift. Robust to intensity changes in a way that
    template matching is not, which matters because a growing cell is
    not just a translated one.
    """
    import numpy as np

    fa = np.nan_to_num(a, nan=0.0).astype("float32")
    fb = np.nan_to_num(b, nan=0.0).astype("float32")
    if fa.std() < 1e-3 or fb.std() < 1e-3:
        return 0.0, 0.0
    fa = fa - fa.mean()
    fb = fb - fb.mean()
    win = (np.hanning(fa.shape[0])[:, None]
           * np.hanning(fa.shape[1])[None, :]).astype("float32")
    A = np.fft.rfft2(fa * win)
    B = np.fft.rfft2(fb * win)
    # conj(A)*B, not A*conj(B): the latter yields the shift that maps
    # b onto a, i.e. the negative of what we want. Verified against
    # planted shifts — magnitudes were exact and every sign inverted.
    cross = np.conj(A) * B
    mag = np.abs(cross)
    cross = np.where(mag > 1e-6, cross / np.maximum(mag, 1e-6), 0)
    r = np.fft.irfft2(cross, s=fa.shape)
    peak = np.unravel_index(np.argmax(r), r.shape)
    dy = peak[0] if peak[0] <= fa.shape[0] // 2 else peak[0] - fa.shape[0]
    dx = peak[1] if peak[1] <= fa.shape[1] // 2 else peak[1] - fa.shape[1]
    return float(dy), float(dx)


def estimate_motion(staged, diag):
    """Domain motion in m/s, from the site with the most echo.

    Returns (vy, vx). Cached: consecutive builds are usually minutes
    apart, which is exactly the interval phase correlation wants.
    """
    import numpy as np

    best, best_cov = None, 0.0
    import warnings
    for arr, rla, rlo, tilt, t, site, alt in staged:
        if t is None:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            lay = np.nanmax(arr, axis=0)
        cov = float(np.isfinite(lay).mean())
        if cov > best_cov:
            best, best_cov = (site, lay, t), cov
    if best is None or best_cov < 0.02:
        diag["motion"] = "not enough echo to estimate"
        return 0.0, 0.0
    site, lay, t = best
    prev = _MOTION_PREV.get(site)
    _MOTION_PREV[site] = (lay, t)
    if prev is None:
        diag["motion"] = f"first frame for {site}, no motion yet"
        return _MOTION_CACHE["v"]
    p_lay, p_t = prev
    dt = (t - p_t).total_seconds()
    if dt < 30 or dt > 1800 or p_lay.shape != lay.shape:
        diag["motion"] = f"unusable interval ({dt:.0f}s)"
        return _MOTION_CACHE["v"]
    dy, dx = _phase_shift(p_lay, lay)
    vy = dy * RES_M / dt
    vx = dx * RES_M / dt
    spd = (vx * vx + vy * vy) ** 0.5
    if spd > 45.0:          # >87 kt is a correlation failure, not weather
        diag["motion"] = f"rejected {spd:.0f} m/s as implausible"
        return _MOTION_CACHE["v"]
    _MOTION_CACHE["v"] = (vy, vx)
    diag["motion"] = (f"{spd * 1.944:.0f} kt toward "
                      f"{(np.degrees(np.arctan2(vx, vy))) % 360:.0f} deg "
                      f"(from {site}, {dt:.0f}s apart)")
    return vy, vx


def _advect(field, vy, vx, dt_s):
    """Shift a field to a different valid time. Sub-cell accurate."""
    import numpy as np
    from scipy.ndimage import shift as _shift

    if abs(dt_s) < 1 or (abs(vy) < 0.05 and abs(vx) < 0.05):
        return field
    sy = vy * dt_s / RES_M
    sx = vx * dt_s / RES_M
    out = np.empty_like(field)
    for k in range(field.shape[0]):
        out[k] = _shift(field[k], (sy, sx), order=1, mode="constant",
                        cval=np.nan, prefilter=False)
    return out


# ---------------------------------------------------------------------------
# Multi-scale fusion
# ---------------------------------------------------------------------------


def combine_fused(staged, diag):
    """Seamless low frequencies, sharpest available high frequencies.

    The insight that makes this work: a SEAM is low-frequency — a
    broad step between two calibrations — while DETAIL is
    high-frequency. They live in different parts of the spectrum, so
    they can be sourced separately.

        low  <- weighted mean of every site   (smooth, no seams,
                                               calibration averaged)
        high <- the best-RESOLVED site per cell (full TDWR structure
                                                 near the airports)

    That is why this beats picking one site per cell, which gives
    detail plus seams, and beats a plain weighted mean, which gives
    neither seams nor detail. Near JFK the high-frequency content
    comes from TJFK at 150 m; fifty miles out it comes from KOKX; the
    low-frequency backbone is continuous across both, so the
    handover is invisible.

    Time alignment runs first — fusing displaced copies would sharpen
    a double image.
    """
    import math

    import numpy as np
    from scipy.ndimage import gaussian_filter

    vy, vx = estimate_motion(staged, diag)
    newest = max((x[4] for x in staged if x[4]), default=None)

    aligned = []
    for arr, rla, rlo, tilt, t, site, alt in staged:
        dt = (newest - t).total_seconds() if (newest and t) else 0.0
        aligned.append((_advect(arr, vy, vx, dt) if dt else arr,
                        rla, rlo, tilt, newest, site, alt))
    diag["aligned_to"] = (newest.strftime("%H:%M:%SZ")
                          if newest else "n/a")

    low = combine_weighted(aligned, diag)
    f, w = _stack_weights(aligned)

    # Effective sample width per site, to pick the sharpest source.
    res = []
    for (arr, rla, rlo, tilt, t, site, alt), wi in zip(aligned, w):
        lat0, lon0 = GRID_CENTER
        rx = (rlo - lon0) * 111320.0 * math.cos(math.radians(lat0))
        ry = (rla - lat0) * 111320.0
        ny, nx = arr.shape[-2], arr.shape[-1]
        ax = np.linspace(-HALF_X_M, HALF_X_M, nx, dtype="float32")
        ay = np.linspace(-HALF_Y_M, HALF_Y_M, ny, dtype="float32")
        d = np.sqrt(((ax - rx) ** 2)[None, :] + ((ay - ry) ** 2)[:, None])
        bw = math.radians(SITE_BEAMWIDTH[_site_band(site)])
        r = np.broadcast_to(np.maximum(d * bw, 150.0)
                            .astype("float32"), arr.shape).copy()
        r[wi <= 0] = np.inf
        res.append(r)
    res = np.stack(res)
    pick = np.argmin(res, axis=0)
    sharp = np.take_along_axis(f, pick[None], axis=0)[0]

    cut = float(os.environ.get("L2_FUSE_SIGMA", "2.0"))
    gain = float(os.environ.get("L2_FUSE_GAIN", "1.0"))
    out = np.array(low, copy=True)
    for k in range(out.shape[0]):
        s_lay, l_lay = sharp[k], low[k]
        m = np.isfinite(s_lay) & np.isfinite(l_lay)
        if not m.any():
            continue
        filled = np.where(m, s_lay, 0.0).astype("float32")
        norm = gaussian_filter(m.astype("float32"), cut, mode="nearest")
        blur = gaussian_filter(filled, cut, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            blur = np.where(norm > 1e-3, blur / norm, s_lay)
        detail = np.where(m, s_lay - blur, 0.0)   # high-pass only
        cand = l_lay + gain * detail
        # CLAMP to what the radars actually saw. Adding a high-pass
        # band to a low-pass base can overshoot at edges — measured
        # 66.1 dBZ against a 50 dBZ truth before this. A fused value
        # must never exceed the strongest observation contributing to
        # that cell, nor fall below the weakest; the fusion is
        # allowed to redistribute detail, not to invent intensity.
        with np.errstate(invalid="ignore"):
            hi = np.nanmax(f[:, k, :, :], axis=0)
            lo_env = np.nanmin(f[:, k, :, :], axis=0)
        out[k] = np.where(m, np.clip(cand, lo_env, hi), l_lay)
    return out


COMBINERS["fused (low blend + high detail)"] = combine_fused
COMBINERS["fused, no time align"] = lambda st, dg: combine_fused(
    [(a, b, c, d, None, f_, g) for a, b, c, d, e, f_, g in st], dg)

# ---------------------------------------------------------------------------
# Rapid update: 88D backbone, TDWR-cadence detail
# ---------------------------------------------------------------------------
# The cadences are mismatched in a way that is an OPPORTUNITY, not a
# problem. A WSR-88D volume takes 4-6 minutes; a TDWR is about 1. If
# the two are fused every time the 88D finishes, the display runs at
# 88D speed and four TDWR scans are thrown away.
#
# Split the update rate the way the fusion already splits the
# spectrum. The low-frequency backbone comes from the 88D and is
# reused between its volumes. The high-frequency detail comes from
# TDWR and is re-injected on every TDWR scan. Inside TDWR coverage
# the picture refreshes each minute; outside it, the 88D field is
# what it always was.
#
# One thing this MUST do to be honest: advect the cached backbone
# forward. It is minutes old by the time the third TDWR scan arrives,
# and pasting fresh detail onto a stale base would put sharp edges in
# the wrong place — worse than not updating at all.
#
# The output is a hybrid-age product and the diagnostics say so:
# base_age_s and detail_age_s are reported separately, never averaged
# into one "valid time" that would be true of neither half.

_BASE_CACHE = {"low": None, "t": None, "key": None}


def _grid_key():
    return (round(GRID_CENTER[0], 4), round(GRID_CENTER[1], 4),
            HALF_X_M, HALF_Y_M, RES_M, LEVELS, BASE_M, TOP_M)


def combine_rapid(staged, diag):
    """Reuse the 88D backbone; refresh detail at TDWR cadence.

    With S-band present the backbone is rebuilt and cached. With only
    TDWR in `staged` — a between-volumes update — the cached backbone
    is advected forward and fresh C-band detail injected into it.
    """
    import math

    import numpy as np
    from scipy.ndimage import gaussian_filter

    sband = [x for x in staged if _site_band(x[5]) == "S"]
    cband = [x for x in staged if _site_band(x[5]) == "C"]
    vy, vx = estimate_motion(staged, diag)
    key = _grid_key()

    now_t = max((x[4] for x in staged if x[4]), default=None)

    if sband:
        aligned_s = []
        newest_s = max((x[4] for x in sband if x[4]), default=None)
        for arr, rla, rlo, tilt, t, site, alt in sband:
            dt = ((newest_s - t).total_seconds()
                  if (newest_s and t) else 0.0)
            aligned_s.append((_advect(arr, vy, vx, dt) if dt else arr,
                              rla, rlo, tilt, newest_s, site, alt))
        low = combine_weighted(aligned_s, diag)
        _BASE_CACHE.update({"low": low, "t": newest_s, "key": key})
        diag["base"] = f"rebuilt from {len(sband)} S-band site(s)"
    elif _BASE_CACHE["low"] is not None and _BASE_CACHE["key"] == key:
        base_dt = ((now_t - _BASE_CACHE["t"]).total_seconds()
                   if (now_t and _BASE_CACHE["t"]) else 0.0)
        # Move the stale backbone to the detail's valid time before
        # anything is pasted onto it.
        low = _advect(_BASE_CACHE["low"], vy, vx, base_dt)
        diag["base"] = (f"cached 88D backbone, advected "
                        f"{base_dt:.0f}s forward")
        diag["base_age_s"] = int(base_dt)
    else:
        diag["base"] = "no backbone available; C-band only"
        low = combine_weighted(cband, diag) if cband else None
    if low is None:
        return combine_weighted(staged, diag)

    if not cband:
        diag["detail"] = "no TDWR this cycle; backbone only"
        return low

    newest_c = max((x[4] for x in cband if x[4]), default=None)
    aligned_c = []
    for arr, rla, rlo, tilt, t, site, alt in cband:
        dt = ((newest_c - t).total_seconds()
              if (newest_c and t) else 0.0)
        aligned_c.append((_advect(arr, vy, vx, dt) if dt else arr,
                          rla, rlo, tilt, newest_c, site, alt))
    f, w = _stack_weights(aligned_c)
    idx = np.argmax(w, axis=0)
    sharp = np.take_along_axis(f, idx[None], axis=0)[0]
    sharp = np.where(w.max(axis=0) > 0, sharp, np.nan)

    cut = float(os.environ.get("L2_FUSE_SIGMA", "2.0"))
    gain = float(os.environ.get("L2_FUSE_GAIN", "1.0"))
    out = np.array(low, copy=True)
    for k in range(out.shape[0]):
        s_lay, l_lay = sharp[k], low[k]
        m = np.isfinite(s_lay) & np.isfinite(l_lay)
        if not m.any():
            continue
        filled = np.where(m, s_lay, 0.0).astype("float32")
        norm = gaussian_filter(m.astype("float32"), cut, mode="nearest")
        blur = gaussian_filter(filled, cut, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            blur = np.where(norm > 1e-3, blur / norm, s_lay)
        cand = l_lay + gain * np.where(m, s_lay - blur, 0.0)
        with np.errstate(invalid="ignore"):
            hi = np.nanmax(f[:, k, :, :], axis=0)
            lo_env = np.nanmin(f[:, k, :, :], axis=0)
        # Only where TDWR actually sees. Everywhere else the 88D
        # field passes through untouched.
        out[k] = np.where(m, np.clip(cand, lo_env, hi), l_lay)
    if newest_c and now_t:
        diag["detail_age_s"] = int((now_t - newest_c).total_seconds())
    diag["detail"] = (f"injected from {len(cband)} TDWR site(s) at "
                      + (newest_c.strftime("%H:%M:%SZ")
                         if newest_c else "?"))
    return out


COMBINERS["rapid (88D base + TDWR detail)"] = combine_rapid

# ---------------------------------------------------------------------------
# Post-filters: keep fine detail, kill noise
# ---------------------------------------------------------------------------
# The problem a plain Gaussian cannot solve. TDWR at 150 m carries
# both real structure (gradients, core edges, radial fine detail) and
# junk (speckle, dropouts, the black voids visible on any raw TDWR
# display). A uniform blur cannot tell them apart, so it removes both
# and the result is smooth and flat.
#
# What every filter here has in common: the decision is made PER CELL
# from what surrounds it, not applied globally.

_POST_WIN = int(os.environ.get("L2_POST_WIN", "3"))


def post_none(a):
    return a


def post_adaptive(a):
    """Blend local max and local mean by how structured the area is.

    Where the neighbourhood is smooth — stratiform, or noise on a
    flat field — the mean wins and speckle disappears. Where it is
    structured, the max wins, which both preserves the peak and
    fills the single-cell dropouts that pepper raw TDWR.

    The weight is the local standard deviation put through a
    smoothstep, so the transition between the two behaviours is
    continuous. A hard threshold would draw its own edges.
    """
    import numpy as np
    from scipy.ndimage import maximum_filter, uniform_filter

    lo = float(os.environ.get("L2_POST_LO", "2.0"))
    hi = float(os.environ.get("L2_POST_HI", "8.0"))
    out = np.array(a, copy=True)
    for k in range(out.shape[0]):
        lay = out[k]
        m = np.isfinite(lay)
        if not m.any():
            continue
        f = np.where(m, lay, 0.0).astype("float32")
        w = m.astype("float32")
        cnt = uniform_filter(w, _POST_WIN, mode="nearest")
        mean = uniform_filter(f, _POST_WIN, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(cnt > 1e-3, mean / cnt, lay)
        sq = uniform_filter(f * f, _POST_WIN, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            var = np.where(cnt > 1e-3, sq / cnt - mean * mean, 0.0)
        sd = np.sqrt(np.maximum(var, 0.0))
        mx = maximum_filter(np.where(m, lay, -999.0), _POST_WIN,
                            mode="nearest")
        t = np.clip((sd - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
        alpha = t * t * (3.0 - 2.0 * t)          # smoothstep
        blended = alpha * mx + (1.0 - alpha) * mean
        # Fill dropouts from their surroundings. A raw TDWR field is
        # peppered with single-cell voids — the black speckle on any
        # scope — and they are not "no echo", they are "no return
        # this pulse". A cell whose neighbourhood is mostly valid
        # gets the same blended value; one surrounded by genuine
        # emptiness stays empty, which is what FILL_MIN enforces.
        fill_min = float(os.environ.get("L2_POST_FILL", "0.55"))
        fillable = (~m) & (cnt >= fill_min)
        out[k] = np.where(m | fillable, blended, np.nan)
    return out


def post_bilateral(a):
    """Average only the neighbours that look like me.

    The standard answer to "smooth without crossing edges": each
    neighbour is weighted by BOTH its distance and how close its
    value is. Across a gradient the far-valued neighbours get almost
    no weight, so the edge survives while noise inside a uniform area
    is averaged away. Unlike the adaptive filter it never takes a
    max, so it cannot dilate a core or fill a dropout — it is the
    conservative choice.
    """
    import numpy as np
    from scipy.ndimage import uniform_filter

    srange = float(os.environ.get("L2_POST_SIGMA_R", "4.0"))
    out = np.array(a, copy=True)
    r = _POST_WIN // 2
    for k in range(out.shape[0]):
        lay = out[k]
        m = np.isfinite(lay)
        if not m.any():
            continue
        base = np.where(m, lay, 0.0).astype("float32")
        acc = np.zeros_like(base)
        wsum = np.zeros_like(base)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                sh = np.roll(np.roll(base, dy, 0), dx, 1)
                shm = np.roll(np.roll(m, dy, 0), dx, 1)
                dv = sh - base
                w = np.exp(-(dv * dv) / (2.0 * srange * srange))
                w = np.where(shm, w, 0.0)
                acc += w * sh
                wsum += w
        with np.errstate(invalid="ignore", divide="ignore"):
            res = np.where(wsum > 1e-6, acc / wsum, lay)
        out[k] = np.where(m, res, np.nan)
    return out


def post_median(a):
    """Plain median. Removes isolated speckle, keeps edges, but also
    erases genuine single-cell peaks — listed for comparison."""
    import numpy as np
    from scipy.ndimage import median_filter

    out = np.array(a, copy=True)
    for k in range(out.shape[0]):
        lay = out[k]
        m = np.isfinite(lay)
        if not m.any():
            continue
        f = median_filter(np.where(m, lay, np.nanmedian(lay)),
                          _POST_WIN, mode="nearest")
        out[k] = np.where(m, f, np.nan)
    return out


POSTFILTERS = {
    "none (raw)": post_none,
    "adaptive max/mean": post_adaptive,
    "bilateral (edge-preserving)": post_bilateral,
    "median": post_median,
}
POSTFILTER = None          # set by the page; None = no post-filter

