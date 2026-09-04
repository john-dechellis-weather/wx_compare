"""Convection-allowing model fields for the Hi-Res CAMs page.

Generic multi-model layer over the NOMADS grib_filter CGIs. Each model
is a config entry; fetch/decode/render are shared. All requests are
single-field subregion subsets (~0.5-3 MB GRIB2), decoded with cfgrib.

Models:
  hrrr        HRRR CONUS (hourly cycles, f00-f18 here)
  nam_nest    NAM 3 km CONUS nest (00/06/12/18Z)   [retires Oct 2026]
  hiresw_arw  Hi-Res Window ARW CONUS (00/12Z)     [retires Oct 2026]
  rrfs        RRFS 3km CONUS pre-ops (8 cycles/day, 84h) - AWS ops bucket idx feed
              announced for ~Aug 11 2026, operational Oct 6 2026.
              Until files appear the panel reports "no cycle found".
"""
from __future__ import annotations

import os

import io
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import requests

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}

NOMADS = "https://nomads.ncep.noaa.gov"

# product -> (var param, list of candidate level params). Different
# NOMADS filters name the same layer differently (e.g. HRRR's
# "entire_atmosphere" vs NAM's
# "entire_atmosphere_(considered_as_a_single_layer)"), so fetch_field
# tries candidates in order until one returns GRIB.
PRODUCT_PARAMS = {
    "REFD": ({"var_REFD": "on"}, [
        {"lev_1000_m_above_ground": "on"},
    ]),
    "REFC": ({"var_REFC": "on"}, [
        {"lev_entire_atmosphere": "on"},
        {"lev_entire_atmosphere_(considered_as_a_single_layer)": "on"},
    ]),
    "RETOP": ({"var_RETOP": "on"}, [
        {"lev_cloud_top": "on"},
        {"lev_entire_atmosphere": "on"},
    ]),
    "VIS": ({"var_VIS": "on"}, [{"lev_surface": "on"}]),
    "CEIL": ({"var_HGT": "on"}, [{"lev_cloud_ceiling": "on"}]),
    "GUST": ({"var_GUST": "on"}, [{"lev_surface": "on"}]),
    # 10 m wind components, for the RRFS point table. Speed and
    # direction are derived from U and V at the station rather than
    # fetched, so the two are guaranteed to come from the same cell
    # and the same instant.
    "UGRD10": ({"var_UGRD": "on"}, [{"lev_10_m_above_ground": "on"}]),
    "VGRD10": ({"var_VGRD": "on"}, [{"lev_10_m_above_ground": "on"}]),
}

PRODUCT_LABELS = {
    "REFD": "1 km AGL Reflectivity (dBZ)",
    "REFC": "Composite Reflectivity (dBZ)",
    "RETOP": "Echo Tops (kft)",
    "VIS": "Visibility (SM)",
    "CEIL": "Ceiling (hundreds ft)",
    "GUST": "10 m Wind Gust (kt)",
    "UGRD10": "10 m U wind (m/s)",
    "VGRD10": "10 m V wind (m/s)",
    "PROB_REFC40": "P(Composite Refl >= 40 dBZ)  %",
    "PROB_CIG500": "P(Ceiling < 500 ft)  %",
    "PROB_CIG1000": "P(Ceiling < 1000 ft)  %",
    "PROB_CIG2000": "P(Ceiling < 2000 ft)  %",
    "PROB_VIS05": "P(Visibility < 1/2 sm)  %",
    "PROB_VIS1": "P(Visibility < 1 sm)  %",
    "PROB_VIS3": "P(Visibility < 3 sm)  %",
    "PROB_RETOP30": "P(Echo Tops > 30 kft)  %",
    "PROB_RETOP35": "P(Echo Tops > 35 kft)  %",
}

# For idx-based fetching: (grib var name, level substring to match)
# Each product maps to one or more (var, level_substring)
# alternatives, tried in order - different products spell the same
# field differently (e.g. hourly-max 1 km reflectivity is MAXREF in
# NCEP indexes; NBM may use either spelling).
# Exceedance-probability products: idx lines carry a threshold
# qualifier after the fcst column, e.g.
#   "n:off:d=...:REFC:entire atmosphere:24 hour fcst:prob >40:"
# Each entry: (VAR, [required substrings anywhere in the line]).
# Exceedance probabilities are matched NUMERICALLY: the idx
# line's "prob <VALUE" / "prob >VALUE" number is parsed and
# compared within a tolerance, so decimal spelling ("<804" vs
# "<804.672") and prefix collisions ("<804" swallowing "<8045")
# cannot misroute a threshold. Aviation values are meters:
# 500ft=152.4  1000ft=304.8  2000ft=609.6
# 1/2sm=804.7  1sm=1609.3    3sm=4828
PROB_DEFS = {
    "PROB_REFC40": ("REFC", "", ">", 40.0, 1.0),
    "PROB_CIG500": ("HGT", "cloud ceiling", "<", 152.4, 3.0),
    "PROB_CIG1000": ("HGT", "cloud ceiling", "<", 304.8, 3.0),
    "PROB_CIG2000": ("HGT", "cloud ceiling", "<", 609.6, 3.0),
    "PROB_VIS05": ("VIS", "surface", "<", 804.7, 5.0),
    "PROB_VIS1": ("VIS", "surface", "<", 1609.3, 8.0),
    "PROB_VIS3": ("VIS", "surface", "<", 4828.0, 15.0),
    # Echo tops (meters): 30 kft = 9144, 35 kft = 10668
    "PROB_RETOP30": ("RETOP", "", ">", 9144.0, 30.0),
    "PROB_RETOP35": ("RETOP", "", ">", 10668.0, 30.0),
}

IDX_MATCHERS = {
    "REFD": [("REFD", "1000 m above ground"),
             ("MAXREF", "1000 m above ground"),
             ("MAXREF", "")],
    "REFC": [("REFC", "entire atmosphere")],
    "RETOP": [("RETOP", "")],
    "VIS": [("VIS", "surface")],
    "CEIL": [("HGT", "cloud ceiling")],
    "GUST": [("GUST", "surface")],
    "UGRD10": [("UGRD", "10 m above ground")],
    "VGRD10": [("VGRD", "10 m above ground")],
}

MODELS = {
    "hrrr": {
        "label": "HRRR",
        "filter": f"{NOMADS}/cgi-bin/filter_hrrr_2d.pl",
        "file": "hrrr.t{cc:02d}z.wrfsfcf{ff:02d}.grib2",
        "dir": "/hrrr.{ymd}/conus",
        "idx": (f"{NOMADS}/pub/data/nccf/com/hrrr/prod/"
                "hrrr.{ymd}/conus/hrrr.t{cc:02d}z.wrfsfcf{ff:02d}"
                ".grib2.idx"),
        "cycles": list(range(24)),
        "max_fhr": 18,
        "products": {"REFD", "REFC", "RETOP", "VIS", "CEIL", "GUST"},
        "note": "",
    },
    "rrfs": {
        "label": "RRFS",
        "mechanism": "idx",
        # CONUS surface/storm diagnostics (REFD/REFC/VIS/CEIL/
        # GUST) live in the 2dfld family - prslev is pure
        # upper-air (verified by inventory 8/17). Grid spelling
        # (3km vs 2p5km) probed both ways.
        "file": "rrfs.t{cc:02d}z.2dfld.3km.f{ff:03d}.conus.grib2",
        "dir": "/rrfs.{ymd}",
        "idx": ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
                "rrfs.{ymd}/{cc:02d}/rrfs.t{cc:02d}z.2dfld."
                "3km.f{ff:03d}.conus.grib2.idx"),
        "idx_candidates": [
            ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
             "rrfs.{ymd}/{cc:02d}/rrfs.t{cc:02d}z.2dfld."
             "3km.f{ff:03d}.conus.grib2.idx"),
            ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
             "rrfs.{ymd}/{cc:02d}/rrfs.t{cc:02d}z.2dfld."
             "2p5km.f{ff:03d}.conus.grib2.idx"),
        ],
        "cycles": [0, 3, 6, 9, 12, 15, 18, 21],
        "probe_back": 31,
        "max_fhr": 84,
        "products": {"REFD", "REFC", "RETOP", "VIS", "CEIL",
                     "GUST"},
        "note": ("pre-operational prototype; availability "
                 "follows the experimental schedule"),
    },
    "refs_mean": {
        "label": "REFS mean",
        "mechanism": "idx",
        # SCN 26-48 verbatim template:
        # refs.YYYYMMDD/CC/ensprod/
        #   refs.tCCz.${type}.fFF.${dom}.grib2
        # (type BEFORE hour, domain LAST - inverse of HREF)
        "file": "refs.t{cc:02d}z.mean.f{ff:02d}.conus.grib2",
        "dir": "/refs.{ymd}",
        "idx": ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
                "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
                "refs.t{cc:02d}z.mean.f{ff:02d}.conus"
                ".grib2.idx"),
        "idx_candidates": [
            ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
             "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
             "refs.t{cc:02d}z.mean.f{ff:02d}.conus"
             ".grib2.idx"),
            ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
             "refs.{ymd}/{cc:02d}/ensprod/refs.t{cc:02d}z."
             "mean.f{ff:02d}.conus.grib2.idx"),
        ],
        "cycles": [0, 6, 12, 18],
        "probe_back": 31,
        "max_fhr": 60,
        "products": {"REFC"},
        "note": "HREF successor (SCN 26-48), pre-implementation",
    },
    "refs_prob": {
        "label": "REFS prob",
        "mechanism": "idx",
        # SCN 26-48 verbatim template:
        # refs.YYYYMMDD/CC/ensprod/
        #   refs.tCCz.${type}.fFF.${dom}.grib2
        # (type BEFORE hour, domain LAST - inverse of HREF)
        "file": "refs.t{cc:02d}z.prob.f{ff:02d}.conus.grib2",
        "dir": "/refs.{ymd}",
        "idx": ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
                "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
                "refs.t{cc:02d}z.prob.f{ff:02d}.conus"
                ".grib2.idx"),
        "idx_candidates": [
            ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
             "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
             "refs.t{cc:02d}z.prob.f{ff:02d}.conus"
             ".grib2.idx"),
            ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
             "refs.{ymd}/{cc:02d}/ensprod/refs.t{cc:02d}z."
             "prob.f{ff:02d}.conus.grib2.idx"),
        ],
        "cycles": [0, 6, 12, 18],
        "probe_back": 31,
        # Probability fields are window statistics - no f00
        "min_fhr": 1,
        "max_fhr": 60,
        "products": {
            "PROB_REFC40", "PROB_CIG500", "PROB_CIG1000",
            "PROB_CIG2000", "PROB_VIS05", "PROB_VIS1",
            "PROB_VIS3", "PROB_RETOP30", "PROB_RETOP35"},
        "note": "HREF successor (SCN 26-48), pre-implementation",
    },
    "refs_pmmn": {
        "label": "REFS PMMN",
        "mechanism": "idx",
        # SCN 26-48 verbatim template:
        # refs.YYYYMMDD/CC/ensprod/
        #   refs.tCCz.${type}.fFF.${dom}.grib2
        # (type BEFORE hour, domain LAST - inverse of HREF)
        "file": "refs.t{cc:02d}z.pmmn.f{ff:02d}.conus.grib2",
        "dir": "/refs.{ymd}",
        "idx": ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
                "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
                "refs.t{cc:02d}z.pmmn.f{ff:02d}.conus"
                ".grib2.idx"),
        "idx_candidates": [
            ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
             "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
             "refs.t{cc:02d}z.pmmn.f{ff:02d}.conus"
             ".grib2.idx"),
            ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
             "refs.{ymd}/{cc:02d}/ensprod/refs.t{cc:02d}z."
             "pmmn.f{ff:02d}.conus.grib2.idx"),
        ],
        "cycles": [0, 6, 12, 18],
        "probe_back": 31,
        "max_fhr": 60,
        "products": {"REFC"},
        "note": "HREF successor (SCN 26-48), pre-implementation",
    },
    "refs_lpmm": {
        "label": "REFS LPMM",
        "mechanism": "idx",
        # SCN 26-48 verbatim template:
        # refs.YYYYMMDD/CC/ensprod/
        #   refs.tCCz.${type}.fFF.${dom}.grib2
        # (type BEFORE hour, domain LAST - inverse of HREF)
        "file": "refs.t{cc:02d}z.lpmm.f{ff:02d}.conus.grib2",
        "dir": "/refs.{ymd}",
        "idx": ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
                "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
                "refs.t{cc:02d}z.lpmm.f{ff:02d}.conus"
                ".grib2.idx"),
        "idx_candidates": [
            ("https://nomads.ncep.noaa.gov/pub/data/nccf/"
             "com/refs/para/refs.{ymd}/{cc:02d}/ensprod/"
             "refs.t{cc:02d}z.lpmm.f{ff:02d}.conus"
             ".grib2.idx"),
            ("https://noaa-rrfs-ops-pds.s3.amazonaws.com/"
             "refs.{ymd}/{cc:02d}/ensprod/refs.t{cc:02d}z."
             "lpmm.f{ff:02d}.conus.grib2.idx"),
        ],
        "cycles": [0, 6, 12, 18],
        "probe_back": 31,
        "max_fhr": 60,
        "products": {"REFC"},
        "note": "HREF successor (SCN 26-48), pre-implementation",
    },
    "nam_nest": {
        "label": "NAM 3km Nest",
        "mechanism": "idx",
        "file": "nam.t{cc:02d}z.conusnest.hiresf{ff:02d}.tm00.grib2",
        "dir": "/nam.{ymd}",
        "idx": (f"{NOMADS}/pub/data/nccf/com/nam/prod/"
                "nam.{ymd}/nam.t{cc:02d}z.conusnest.hiresf{ff:02d}"
                ".tm00.grib2.idx"),
        "cycles": [0, 6, 12, 18],
        "max_fhr": 60,
        "products": {"REFD", "REFC", "VIS", "CEIL", "GUST"},
        "note": "retires Oct 2026 (replaced by RRFS)",
    },
    "hiresw_fv3": {
        "label": "HRW FV3",
        "mechanism": "idx",
        "file": "hiresw.t{cc:02d}z.fv3_2p5km.f{ff:02d}.conus.grib2",
        "dir": "/hiresw.{ymd}",
        "idx_candidates": [
            (f"{NOMADS}/pub/data/nccf/com/hiresw/prod/"
             "hiresw.{ymd}/hiresw.t{cc:02d}z.fv3_2p5km.f{ff:02d}"
             ".conus.grib2.idx"),
            (f"{NOMADS}/pub/data/nccf/com/hiresw/prod/"
             "hiresw.{ymd}/hiresw.t{cc:02d}z.fv3_5km.f{ff:02d}"
             ".conus.grib2.idx"),
        ],
        "idx": (f"{NOMADS}/pub/data/nccf/com/hiresw/prod/"
                "hiresw.{ymd}/hiresw.t{cc:02d}z.fv3_2p5km.f{ff:02d}"
                ".conus.grib2.idx"),
        "cycles": [0, 12],
        "max_fhr": 48,
        "products": {"REFD", "REFC", "VIS", "CEIL", "GUST"},
        "note": "retires Oct 2026 (replaced by RRFS)",
    },
    "hiresw_arw": {
        "label": "HRW ARW",
        "mechanism": "idx",
        "file": "hiresw.t{cc:02d}z.arw_2p5km.f{ff:02d}.conus.grib2",
        "dir": "/hiresw.{ymd}",
        "idx": (f"{NOMADS}/pub/data/nccf/com/hiresw/prod/"
                "hiresw.{ymd}/hiresw.t{cc:02d}z.arw_2p5km.f{ff:02d}"
                ".conus.grib2.idx"),
        "cycles": [0, 12],
        "max_fhr": 48,
        "products": {"REFD", "REFC", "VIS", "CEIL", "GUST"},
        "note": "retires Oct 2026 (replaced by RRFS)",
    },
    "nbm": {
        "label": "NBM Blend",
        "mechanism": "idx",
        # NBM v5 core files carry hourly-max 1 km AGL simulated
        # reflectivity (new in v5, operational May 2026). AWS mirror
        # first - its .idx sidecars are reliable; NOMADS prod
        # trails.
        "idx_candidates": [
            ("https://noaa-nbm-grib2-pds.s3.amazonaws.com/"
             "blend.{ymd}/{cc:02d}/core/blend.t{cc:02d}z.core"
             ".f{ff:03d}.co.grib2.idx"),
            (f"{NOMADS}/pub/data/nccf/com/blend/prod/"
             "blend.{ymd}/{cc:02d}/core/blend.t{cc:02d}z.core"
             ".f{ff:03d}.co.grib2.idx"),
        ],
        "idx": (f"{NOMADS}/pub/data/nccf/com/blend/prod/"
                "blend.{ymd}/{cc:02d}/core/blend.t{cc:02d}z.core"
                ".f{ff:03d}.co.grib2.idx"),
        "cycles": list(range(24)),
        "max_fhr": 36,
        "products": {"REFD", "RETOP", "VIS", "CEIL", "GUST"},
        "note": ("NBM v5 blend, 2.5 km; reflectivity is the "
                 "HOURLY MAX at 1 km AGL (no composite)"),
    },
}

# Candidates that connection-error get dropped for the session so
# repeated timeouts don't tax every cycle probe.
_dead_candidates: set = set()


def latest_cycle(
    model: str, fhr: int, now: Optional[datetime] = None
) -> Optional[datetime]:
    """Newest cycle whose requested forecast hour exists (idx probe).
    Walks back up to 30 hours to cover sparse-cycle models."""
    cfg = MODELS[model]
    now = now or datetime.now(timezone.utc)
    candidates = (cfg.get("probe_candidates")
                  or cfg.get("idx_candidates")
                  or [cfg["idx"]])
    diag: dict = {}
    cfg["_probe_diag"] = diag
    if cfg.get("_idx_resolved"):
        candidates = [cfg["_idx_resolved"]]
    max_back = cfg.get(
        "probe_back", 31 if len(candidates) == 1 else 13)
    for back in range(1, max_back):
        cyc = (now - timedelta(hours=back)).replace(
            minute=0, second=0, microsecond=0
        )
        if cyc.hour not in cfg["cycles"]:
            continue
        for tmpl in candidates:
            if tmpl in _dead_candidates:
                continue
            url = tmpl.format(ymd=cyc.strftime("%Y%m%d"),
                              cc=cyc.hour, ff=fhr)
            try:
                r = requests.head(url, headers=_HEADERS, timeout=8)
            except requests.exceptions.ConnectionError as e:
                _dead_candidates.add(tmpl)
                diag[tmpl] = f"ConnectionError: {e}"[:120]
                continue
            except Exception as e:
                diag[tmpl] = f"{type(e).__name__}: {e}"[:120]
                continue
            # Some servers reject HEAD; retry as a zero-range GET
            if r.status_code in (403, 405):
                try:
                    r = requests.get(
                        url, headers={**_HEADERS,
                                      "Range": "bytes=0-0"},
                        timeout=8,
                    )
                except Exception as e:
                    diag[tmpl] = f"{type(e).__name__}"[:120]
                    continue
            diag[tmpl] = f"HTTP {r.status_code} @{cyc:%d/%H}z"
            if r.status_code in (200, 206):
                if tmpl.endswith(".idx"):
                    cfg["_idx_resolved"] = tmpl
                return cyc
    return None


def fetch_field(
    model: str,
    product: str,
    cycle: datetime,
    fhr: int,
    lat: float,
    lon: float,
    zoom_deg: float,
) -> bytes:
    """Small subregion GRIB2 for one field via the model's filter CGI."""
    cfg = MODELS[model]
    if product not in cfg["products"]:
        raise RuntimeError(f"{cfg['label']} does not provide {product}")
    if cfg.get("mechanism") == "idx" or cfg.get("_idx_resolved"):
        return _fetch_field_idx(cfg, product, cycle, fhr)
    var_p, lev_candidates = PRODUCT_PARAMS[product]
    pad = zoom_deg + 0.4
    base = {
        "file": cfg["file"].format(cc=cycle.hour, ff=fhr),
        "subregion": "",
        "leftlon": f"{(lon - pad) % 360:.2f}",
        "rightlon": f"{(lon + pad) % 360:.2f}",
        "toplat": f"{lat + pad:.2f}",
        "bottomlat": f"{lat - pad:.2f}",
        **var_p,
    }
    if cfg.get("ds"):
        base["ds"] = cfg["ds"]

    # Filter URL and dir may each have several plausible spellings
    # (RRFS is brand-new); the first working (filter, dir) pair is
    # memoized for the session.
    if cfg.get("_filter_resolved"):
        filter_specs = [cfg["_filter_resolved"][0]]
        dir_tmpls = [cfg["_filter_resolved"][1]]
    else:
        raw_specs = (cfg.get("filter_candidates")
                     or [cfg["filter"]])
        filter_specs = [
            s if isinstance(s, tuple) else (s, cfg.get("ds"))
            for s in raw_specs
        ]
        dir_tmpls = cfg.get("dir_candidates") or [cfg["dir"]]

    attempts: list = []
    for f_spec in filter_specs:
        f_url, f_ds = f_spec
        if (f_url, f_ds) in _dead_candidates:
            continue
        for d_tmpl in dir_tmpls:
            d = d_tmpl.format(ymd=cycle.strftime("%Y%m%d"),
                              cc=cycle.hour)
            params = {**base, "dir": d}
            if f_ds:
                params["ds"] = f_ds
            for lev_p in lev_candidates:
                try:
                    r = requests.get(
                        f_url, params={**params, **lev_p},
                        headers=_HEADERS, timeout=90,
                    )
                except requests.exceptions.ConnectionError as e:
                    _dead_candidates.add((f_url, f_ds))
                    attempts.append(
                        f"{f_url.rsplit('/', 1)[-1]}: ConnErr"
                    )
                    break
                except Exception as e:
                    attempts.append(f"{type(e).__name__}")
                    continue
                if (r.status_code == 200 and len(r.content) >= 500
                        and r.content[:4] == b"GRIB"):
                    cfg["_filter_resolved"] = (f_spec, d_tmpl)
                    return r.content
                tag = f_url.rsplit('/', 1)[-1]
                if f_ds:
                    tag += f"?ds={f_ds}"
                attempts.append(
                    f"{tag} dir={d_tmpl.split('/')[1][:9]}: "
                    f"HTTP {r.status_code} {len(r.content)}B"
                )
                # 404 on the script itself: skip its dir variants
                if r.status_code == 404 and len(r.content) < 500:
                    break
            else:
                continue
            break
    # Report EVERY distinct attempt - the informative failure is
    # often mid-list, not last.
    seen = []
    for a in attempts:
        if a not in seen:
            seen.append(a)
    raise RuntimeError(
        f"{cfg['label']} filter failed for {product} f{fhr:02d}. "
        f"Attempts: " + " | ".join(seen[:10])
    )


def _fetch_field_idx(cfg: dict, product: str, cycle, fhr: int) -> bytes:
    """Byte-range fetch of a single GRIB message using the .idx sidecar
    (the Herbie technique). Filter-independent: works for any NOMADS
    GRIB that publishes an index, so it survives interface migrations.
    Returns the full-domain field (~1-5 MB); the renderer crops to the
    requested extent."""
    tmpl = cfg.get("_idx_resolved") or cfg.get("idx")
    if not tmpl:
        raise RuntimeError(f"{cfg['label']}: no idx source resolved")
    idx_url = tmpl.format(ymd=cycle.strftime("%Y%m%d"),
                          cc=cycle.hour, ff=fhr)
    grib_url = idx_url[:-4]  # strip ".idx"
    r = requests.get(idx_url, headers=_HEADERS, timeout=30)
    r.raise_for_status()

    lines = r.text.splitlines()
    start = end = None
    if product in PROB_DEFS:
        import re as _re
        var_name, lev_sub, op, target, tol = PROB_DEFS[product]
        for i, line in enumerate(lines):
            parts = line.split(":")
            if len(parts) < 5 or parts[3] != var_name:
                continue
            if lev_sub and lev_sub not in parts[4].lower():
                continue
            m = _re.search(r"prob\s*([<>])\s*([\d.]+)",
                           line.lower())
            if (m and m.group(1) == op
                    and abs(float(m.group(2)) - target) <= tol):
                start = int(parts[1])
                for nxt in lines[i + 1:]:
                    p2 = nxt.split(":")
                    if len(p2) >= 2:
                        end = int(p2[1]) - 1
                        break
                break
        if start is None:
            have = [l for l in lines
                    if f":{var_name}:" in l][:8]
            raise RuntimeError(
                f"{cfg['label']}: no {product} line. "
                f"{var_name} lines present: "
                + " || ".join(have))
        matchers = []
    else:
        matchers = IDX_MATCHERS[product]
    for var_name, lev_sub in matchers:
        for i, line in enumerate(lines):
            # "n:offset:d=YYYYMMDDHH:VAR:LEVEL:fcst:..."
            parts = line.split(":")
            if len(parts) < 5:
                continue
            if (parts[3] == var_name
                    and lev_sub in parts[4].lower()):
                start = int(parts[1])
                for nxt in lines[i + 1:]:
                    p2 = nxt.split(":")
                    if len(p2) >= 2:
                        end = int(p2[1]) - 1
                        break
                break
        if start is not None:
            break
    if start is None:
        available = sorted({
            p[3] for p in (l.split(":") for l in lines) if len(p) > 3
        })
        wanted = "/".join(v for v, _l in matchers)
        raise RuntimeError(
            f"{cfg['label']}: {wanted} "
            f"not in index. Vars present: {', '.join(available[:25])}"
        )
    headers = {**_HEADERS,
               "Range": f"bytes={start}-{end}" if end else
                        f"bytes={start}-"}
    r2 = requests.get(grib_url, headers=headers, timeout=120)
    if r2.status_code not in (200, 206):
        raise RuntimeError(
            f"{cfg['label']}: range request HTTP {r2.status_code}"
        )
    if r2.content[:4] != b"GRIB":
        raise RuntimeError(
            f"{cfg['label']}: range response not GRIB "
            f"({len(r2.content)} bytes)"
        )
    return r2.content


def decode_field(raw: bytes):
    """(values_2d, lat_2d, lon_2d) from a single-message GRIB2."""
    import xarray as xr

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(raw)
        path = tf.name
    ds = xr.open_dataset(path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    var = list(ds.data_vars)[0]
    vals = np.asarray(ds[var].values, dtype=float)
    lats = np.asarray(ds["latitude"].values, dtype=float)
    lons = np.asarray(ds["longitude"].values, dtype=float)
    lons = np.where(lons > 180, lons - 360, lons)
    ds.close()
    return vals, lats, lons


def fetch_and_decode(
    model: str,
    product: str,
    cycle: datetime,
    fhr: int,
    lat: float,
    lon: float,
    zoom_deg: float,
):
    """fetch_field + decode_field + CROP in one call (thread-safe:
    pure I/O and numpy, no matplotlib).

    Cropping here is the memory diet: full grids (NBM CONUS is
    2345x1597 - ~90 MB of float64 per decode) shrink to the view
    window before anything else touches them, and everything is
    downcast to float32. Two concurrent full-grid decodes were
    enough to OOM the small instance; post-crop frames are a few
    hundred KB.
    """
    import gc

    import numpy as np

    raw = fetch_field(model, product, cycle, fhr, lat, lon, zoom_deg)
    vals, lats, lons = decode_field(raw)
    del raw

    pad = zoom_deg + 0.6
    lons_w = np.where(lons > 180, lons - 360, lons)
    mask = (
        (lats >= lat - pad) & (lats <= lat + pad)
        & (lons_w >= lon - pad) & (lons_w <= lon + pad)
    )
    if mask.any() and mask.ndim == 2:
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        r0, r1 = rows[0], rows[-1] + 1
        c0, c1 = cols[0], cols[-1] + 1
        vals = np.ascontiguousarray(
            vals[r0:r1, c0:c1], dtype=np.float32
        )
        lats = np.ascontiguousarray(
            lats[r0:r1, c0:c1], dtype=np.float32
        )
        lons = np.ascontiguousarray(
            lons_w[r0:r1, c0:c1], dtype=np.float32
        )
        # Wide (CONUS-class) requests: decimate 2x AT DECODE so
        # every downstream holder (result dicts, render queues)
        # carries ~5 MB frames instead of ~45 MB - the render-
        # side decimation alone still let the smooth pipeline
        # accumulate gigabytes before the first plot.
        if zoom_deg > 10 and vals.shape[0] > 800:
            # Decimation factor tunable WITHOUT redeploy via the
            # WIDE_DECIM env var (2 = 6 km effective, 4 = 12 km).
            # 4 quarters the quadmesh memory again - the dial to
            # prove/relieve single-render OOM on small instances.
            import os as _os
            _df = max(2, int(_os.environ.get("WIDE_DECIM", "2")))
            vals = np.ascontiguousarray(vals[::_df, ::_df])
            lats = np.ascontiguousarray(lats[::_df, ::_df])
            lons = np.ascontiguousarray(lons[::_df, ::_df])
    else:
        vals = vals.astype(np.float32, copy=False)
        lats = lats.astype(np.float32, copy=False)
        lons = lons_w.astype(np.float32, copy=False)
    del mask, lons_w
    gc.collect()
    return vals, lats, lons


def parallel_fetch_decode(tasks: list[dict], max_workers: int = 6):
    """Run many fetch_and_decode calls concurrently.

    tasks: [{"key": anything-hashable, "model", "product", "cycle",
             "fhr", "lat", "lon", "zoom_deg"}, ...]
    Returns {key: (vals, lats, lons) | Exception}. Downloads and
    decodes overlap across models and hours; rendering stays with the
    caller (matplotlib is not thread-safe).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(
                fetch_and_decode, t["model"], t["product"], t["cycle"],
                t["fhr"], t["lat"], t["lon"], t["zoom_deg"],
            ): t["key"]
            for t in tasks
        }
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                out[key] = e
    return out


# JetBlue stations, for in-frame markers. Same set the fleet map
# uses; coordinates are airport reference points.
JBU_STATIONS = {
    "KJFK": (40.6398, -73.7789), "KBOS": (42.3630, -71.0064),
    "KEWR": (40.6925, -74.1687), "KLGA": (40.7772, -73.8726),
    "KPHL": (39.8721, -75.2411), "KDCA": (38.8521, -77.0377),
    "KBWI": (39.1754, -76.6683), "KPVD": (41.7240, -71.4283),
    "KBDL": (41.9389, -72.6832), "KALB": (42.7483, -73.8017),
    "KSYR": (43.1112, -76.1063), "KBUF": (42.9405, -78.7322),
    "KROC": (43.1189, -77.6724), "KPIT": (40.4915, -80.2329),
    "KCLE": (41.4118, -81.8498), "KDTW": (42.2124, -83.3534),
    "KORD": (41.9786, -87.9048), "KMDW": (41.7860, -87.7524),
    "KCLT": (35.2140, -80.9431), "KRDU": (35.8776, -78.7875),
    "KRIC": (37.5052, -77.3197), "KORF": (36.8946, -76.2012),
    "KCHS": (32.8986, -80.0405), "KSAV": (32.1276, -81.2021),
    "KJAX": (30.4941, -81.6879), "KMCO": (28.4312, -81.3081),
    "KTPA": (27.9755, -82.5332), "KFLL": (26.0726, -80.1527),
    "KMIA": (25.7932, -80.2906), "KPBI": (26.6832, -80.0956),
    "KRSW": (26.5362, -81.7552), "KEYW": (24.5561, -81.7595),
    "KSRQ": (27.3954, -82.5544), "KATL": (33.6367, -84.4281),
    "KBNA": (36.1245, -86.6782), "KMSY": (29.9934, -90.2580),
    "KIAH": (29.9844, -95.3414), "KAUS": (30.1975, -97.6664),
    "KDFW": (32.8968, -97.0380), "KDEN": (39.8617, -104.6732),
    "KPHX": (33.4343, -112.0116), "KLAS": (36.0801, -115.1523),
    "KLAX": (33.9425, -118.4081), "KSAN": (32.7336, -117.1897),
    "KSFO": (37.6189, -122.3750), "KSJC": (37.3626, -121.9291),
    "KSMF": (38.6954, -121.5908), "KPDX": (45.5887, -122.5975),
    "KSEA": (47.4502, -122.3088), "KMSP": (44.8820, -93.2218),
    "KDTW2": (42.2124, -83.3534),
}


# Station mark sizes in points. Four times the first attempt, which
# was invisible at working zoom. CAM_STATION_DOT_PT / _FONT_PT tune
# them without a deploy.
STATION_DOT_PT = float(os.environ.get("CAM_STATION_DOT_PT", "13"))
STATION_FONT_PT = float(os.environ.get("CAM_STATION_FONT_PT", "26"))
# Range ring radius in NAUTICAL miles; 0 disables.
STATION_RING_NM = float(os.environ.get("CAM_STATION_RING_NM", "10"))


def draw_stations(ax, w: float, s: float, e: float, n: float,
                  skip=None, pad: float = 0.15) -> int:
    """JetBlue station dots and identifiers on a cartopy axis.

    Shared by the matplotlib renderer here and the fast composite
    renderer in cam_fast, so the warmed frames and the live-render
    fallback show the same marks in the same places. New York metro
    shows JFK only — LGA and EWR overlap it at every zoom these
    pages use.
    """
    import math

    import cartopy.crs as ccrs
    import matplotlib.patheffects as _pe
    import numpy as np

    if skip is None:
        import os as _os

        skip = set(x.strip().upper() for x in _os.environ.get(
            "CAM_STATION_SKIP", "KLGA,KEWR").split(",") if x.strip())
    drawn = 0
    for icao, (sla, slo) in JBU_STATIONS.items():
        if icao in skip:
            continue
        if not (w + pad <= slo <= e - pad and s + pad <= sla <= n - pad):
            continue
        # Sized to read on the SOC wall, not a laptop: a 13 pt dot
        # and 26 pt label on a ~2000 px frame. The first pass at
        # 3 pt / 6.5 pt was invisible at working zoom. Label offset
        # scales with the dot so it clears the edge at any size.
        # Range ring first, so the dot draws over its centre. A
        # true circle on the ground: radius in degrees of latitude,
        # stretched in longitude by 1/cos(lat) so it is not an
        # ellipse on the map. Thin and translucent — a reference
        # mark, not a symbol.
        if STATION_RING_NM > 0:
            r_lat = STATION_RING_NM / 60.0
            r_lon = r_lat / max(0.2, math.cos(math.radians(sla)))
            th = np.linspace(0.0, 2.0 * np.pi, 73)
            ax.plot(slo + r_lon * np.cos(th), sla + r_lat * np.sin(th),
                    color="#003B8E", linewidth=0.9, alpha=0.75,
                    transform=ccrs.PlateCarree(), zorder=6)
        ax.plot(slo, sla, marker="o", markersize=STATION_DOT_PT,
                markerfacecolor="#005ADC", markeredgecolor="white",
                markeredgewidth=1.4, linestyle="none",
                transform=ccrs.PlateCarree(), zorder=7)
        # Label just clear of the dot: offset is the dot radius plus
        # a small gap, in degrees, computed from the figure's own
        # points-per-degree so it stays tight at any size.
        _pt_per_deg = (ax.figure.get_size_inches()[1] * 72.0) / (n - s)
        _gap_deg = (STATION_DOT_PT / 2.0 + 3.0) / _pt_per_deg
        ax.text(slo, sla + _gap_deg,
                icao[1:] if icao.startswith("K") else icao,
                fontsize=STATION_FONT_PT, color="#003B8E",
                fontweight="bold", ha="center", va="bottom",
                transform=ccrs.PlateCarree(), zorder=7,
                path_effects=[_pe.withStroke(linewidth=2.5,
                                             foreground="white")])
        drawn += 1
    return drawn


def render_frame(product: str, vals, lats, lons, center_lat: float,
                 center_lon: float, zoom_deg: float, title: str = "",
                 grid_key: str = None, cache_root=None) -> bytes:
    """Render one frame the SAME way the warmer does.

    Live renders used to go through render_field — the legacy
    matplotlib figure with axes, a colorbar and its own small
    station labels — while warmed frames came from cam_fast on a
    full-bleed square. So the moment a page fell back to a live
    render the map changed size, gained a colorbar, and station
    marks doubled up. This routes live renders through cam_fast
    whenever it supports the product, so a live frame and a warmed
    frame are pixel-for-pixel the same layout.
    """
    try:
        from core import cam_fast as _CF

        if _CF.supports(product):
            return _CF.render_fast(
                product, vals, lats, lons, center_lat, center_lon,
                zoom_deg,
                grid_key=grid_key or f"{center_lat:.2f},{center_lon:.2f}"
                                     f"|{zoom_deg:.2f}",
                cache_dir=(str(cache_root) if cache_root else None),
            )
    except Exception:
        pass
    return render_field(product, vals, lats, lons, center_lat,
                        center_lon, zoom_deg, title)


def render_field(
    product: str,
    vals: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    center_lat: float,
    center_lon: float,
    zoom_deg: float,
    title: str,
    headline: str = "",
    aircraft=None,
    routes=None,
) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.patheffects as _pe
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    # Wide (CONUS-class) renders: decimate the 3km grid 2x
    # BEFORE data is derived, so C and X/Y stay married in
    # pcolormesh (~4x less contouring memory/time; a national
    # view cannot resolve 3km anyway).
    if zoom_deg > 10 and getattr(vals, "ndim", 0) == 2 \
            and vals.shape[0] > 800:
        vals = vals[::2, ::2]
        lats = lats[::2, ::2]
        lons = lons[::2, ::2]

    data = np.ma.masked_invalid(vals)

    if product.startswith("PROB"):
        from matplotlib.colors import BoundaryNorm, ListedColormap
        bounds = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        colors = ["#d1e9f7", "#8fcbe8", "#54a6d6", "#4bb84b",
                  "#a4d64b", "#f5e642", "#f5a742", "#ec5f27",
                  "#c81e1e", "#8b0f5e"]
        cmap = ListedColormap(colors)
        norm = BoundaryNorm(bounds, cmap.N)
        data = np.ma.masked_less(data, 5)
    elif product in ("REFC", "REFD"):
        from metpy.plots import colortables
        norm, cmap = colortables.get_with_steps("NWSReflectivity", 5, 5)
        # Standard CAM convention: mask < 5 dBZ so clear air stays clean
        data = np.ma.masked_less(data, 5)
    elif product == "RETOP":
        data = np.ma.masked_less(data, 0) / 304.8
        bounds = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70]
        colors = ["#C8C8C8", "#9BD4F5", "#4FA8E8", "#2E6FDB", "#22B14C",
                  "#7CD934", "#FFF200", "#FFC90E", "#FF7F27", "#ED1C24",
                  "#B21E28", "#A349A4", "#6F2DA8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "VIS":
        data = data / 1609.34
        bounds = [0, 0.5, 1, 2, 3, 5, 7, 10]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "CEIL":
        data = data * 3.28084 / 100.0
        data = np.ma.masked_greater(data, 300)
        bounds = [0, 2, 4, 10, 20, 30, 50, 100, 300]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#A8D8A8", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    else:  # GUST
        data = data * 1.94384
        bounds = [0, 10, 15, 20, 25, 30, 35, 40, 50, 65]
        colors = ["#E8E8E8", "#B0E0FF", "#60B0E0", "#FFFF00", "#FFC90E",
                  "#FF9900", "#FF4040", "#B21E28", "#A349A4"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)

    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
    ax.set_extent(
        [center_lon - zoom_deg, center_lon + zoom_deg,
         center_lat - zoom_deg, center_lat + zoom_deg],
        crs=ccrs.PlateCarree(),
    )
    # Contour-smoothed rendering (Pivotal/TT-style): upsample the
    # native grid 3x bilinearly, then filled contours at the
    # palette boundaries subdivided 2x - smooth organic edges
    # instead of 3km blocks. Falls back to the honest mesh if
    # scipy or contouring balks (e.g. degenerate crops).
    mesh = None
    try:
        from scipy import ndimage as _ndi

        _bg = float(norm.boundaries[0]) - 1.0
        _fill = np.asarray(
            np.ma.filled(data, _bg), dtype=np.float32)
        _vz = _ndi.zoom(_fill, 3, order=1)
        _la2 = (lats if getattr(lats, "ndim", 1) == 2
                else np.broadcast_to(
                    np.asarray(lats)[:, None],
                    (len(lats), len(lons))))
        _lo2 = (lons if getattr(lons, "ndim", 1) == 2
                else np.broadcast_to(
                    np.asarray(lons)[None, :],
                    (len(lats), len(lons))))
        _laz = _ndi.zoom(np.asarray(_la2, np.float64), 3, order=1)
        _loz = _ndi.zoom(np.asarray(_lo2, np.float64), 3, order=1)
        _b = np.asarray(norm.boundaries, dtype=float)
        _lv = np.sort(np.unique(np.concatenate(
            [_b, (_b[:-1] + _b[1:]) / 2.0])))
        mesh = ax.contourf(
            _loz, _laz, _vz, levels=_lv, cmap=cmap, norm=norm,
            extend="max", antialiased=True,
            transform=ccrs.PlateCarree(), zorder=2,
        )
        del _fill, _vz, _laz, _loz
    except Exception:
        mesh = None
    if mesh is None:
        mesh = ax.pcolormesh(
            lons, lats, data, cmap=cmap, norm=norm,
            shading="auto",
            transform=ccrs.PlateCarree(), zorder=2,
        )
    try:
        coast = cfeature.COASTLINE.with_scale("10m")
        states = cfeature.STATES.with_scale("10m")
        next(iter(coast.geometries()))
        ax.add_feature(coast, linewidth=0.8, zorder=3)
        ax.add_feature(states, linewidth=0.5, zorder=3)
    except Exception:
        pass
    if 4 < zoom_deg <= 10:
        # Regional (2x) frames are viewed pre-zoomed: put the
        # degree labels INLINE along the gridlines so the chart
        # stays labeled wherever the user zooms or pans - edge
        # labels vanish outside the cropped view (observed live)
        gl = ax.gridlines(draw_labels=True, linewidth=0.3,
                          linestyle=":", color="gray",
                          x_inline=True, y_inline=True)
        gl.xlabel_style = {"size": 7, "color": "#555"}
        gl.ylabel_style = {"size": 7, "color": "#555"}
    else:
        gl = ax.gridlines(draw_labels=True, linewidth=0.3,
                          linestyle=":", color="gray")
        gl.top_labels = False
        gl.right_labels = False
        gl.xlabel_style = {"size": 8}
    gl.ylabel_style = {"size": 8}

    # render_field draws its own small station labels further down;
    # the large marks live in draw_stations, used by cam_fast. Live
    # renders reach cam_fast via render_frame, so they are not
    # doubled here.

    # 10 nm range ring around the center site (white dashed with a
    # black understroke so it reads over any reflectivity), plus a
    # small center marker
    try:
        _r_nm = 10.0
        _rlat = _r_nm / 60.0
        _rlon = _rlat / np.cos(np.radians(center_lat))
        _t = np.linspace(0, 2 * np.pi, 181)
        _ring_lon = center_lon + _rlon * np.cos(_t)
        _ring_lat = center_lat + _rlat * np.sin(_t)
        for _lw, _col, _z in ((3.0, "#000000", 4.5),
                              (1.5, "#FFFFFF", 4.6)):
            ax.plot(_ring_lon, _ring_lat, color=_col,
                    linewidth=_lw, linestyle=(0, (4, 2)),
                    zorder=_z, transform=ccrs.PlateCarree())
        ax.scatter([center_lon], [center_lat], s=26,
                   color="#005ADC", edgecolors="white",
                   linewidths=1.0, zorder=4.7,
                   transform=ccrs.PlateCarree())
        ax.annotate(
            "10 nm", xy=(center_lon + _rlon * 0.72,
                         center_lat + _rlat * 0.72),
            fontsize=7, color="white", fontweight="bold",
            ha="left", zorder=4.7,
            path_effects=[__import__("matplotlib.patheffects",
                                     fromlist=["w"]).withStroke(
                linewidth=2.2, foreground="black")],
        )
    except Exception:
        pass

    routes = routes or {}
    for ac in aircraft or []:
        rt = routes.get(ac.callsign)
        if rt:
            (olat, olon), (dlat, dlon) = rt["orig"], rt["dest"]
            ax.plot(
                [olon, dlon], [olat, dlat],
                color="#0000CC", linewidth=1.0, linestyle="--",
                alpha=0.55, zorder=9, transform=ccrs.Geodetic(),
            )
        ax.scatter(ac.lon, ac.lat, s=70, marker="^", color="#0000CC",
                   edgecolors="white", linewidths=0.8, zorder=10,
                   transform=ccrs.PlateCarree())
        lbl = ac.callsign
        if ac.alt_ft is not None:
            lbl += f"\nFL{int(round(ac.alt_ft / 100)):03d}"
        if rt:
            lbl += f"\n{rt['label']}"
        ax.annotate(lbl, xy=(ac.lon, ac.lat), xytext=(4, 4),
                    textcoords="offset points", fontsize=6,
                    fontweight="bold", color="#0000CC", zorder=10)

    # --- JetBlue stations inside the frame -----------------------
    # Drawn from the same list the fleet map uses. A dot the size of
    # the centre hub's, with the 3-letter code in tiny text, so a
    # KJFK frame also shows BOS, PHL, DCA and anything else in view.
    # Purely geographic, so it belongs in the image rather than as a
    # map overlay.
    try:
        w, e, s_, n_ = ax.get_extent(crs=ccrs.PlateCarree())
        for _ic, (_sla, _slo) in JBU_STATIONS.items():
            if not (w <= _slo <= e and s_ <= _sla <= n_):
                continue
            ax.plot(_slo, _sla, marker="o", markersize=4.0,
                    color="#0033A0", markeredgecolor="white",
                    markeredgewidth=0.6, transform=ccrs.PlateCarree(),
                    zorder=9)
            ax.annotate(_ic[1:], xy=(_slo, _sla), xytext=(3.5, -6.5),
                        textcoords="offset points", fontsize=4.6,
                        fontweight="bold", color="#0033A0",
                        zorder=9,
                        path_effects=[_pe.withStroke(
                            linewidth=1.4, foreground="white")])
    except Exception:
        pass

    # --- headline, INSIDE the axes ------------------------------
    # Drawn in axes coordinates so it sits over the map itself. The
    # frames are viewed magnified and panned, and anything in the
    # matplotlib TITLE area scrolls out of view as soon as you zoom —
    # which is exactly when you most need to know which run you are
    # looking at.
    # Headline is drawn by the PAGE now, in an HTML bar above the
    # map, so it stays visible at any zoom. Baking it into the image
    # meant it scrolled away exactly when it was needed. Kept behind
    # a flag for anyone rendering a frame outside the scrubber.
    if headline and os.environ.get("CAM_INMAP_TITLE", "off") == "on":
        ax.text(0.5, 0.985, headline, transform=ax.transAxes,
                ha="center", va="top", fontsize=9, fontweight="bold",
                color="#101010", zorder=12,
                bbox=dict(boxstyle="round,pad=0.32", fc="#FFFFFF",
                          ec="#101010", lw=0.8, alpha=0.92))

    if zoom_deg > 4:
        # Wide (CONUS-class) frames are viewed magnified: keep
        # the map nearly edge-to-edge and park a slim horizontal
        # colorbar under it, so hub-zoomed views show map, not
        # a monster legend (observed live at 6x magnification)
        if os.environ.get("CAM_INMAP_TITLE", "off") == "on":
            ax.set_title(title, fontsize=8, pad=2)
        cb = plt.colorbar(
            mesh, ax=ax, orientation="horizontal",
            fraction=0.030, pad=0.015, aspect=55,
            label=PRODUCT_LABELS.get(product, product))
        cb.ax.tick_params(labelsize=6)
        cb.set_label(PRODUCT_LABELS.get(product, product),
                     fontsize=6)
        fig.subplots_adjust(left=0.015, right=0.985,
                            top=0.955, bottom=0.075)
    else:
        if os.environ.get("CAM_INMAP_TITLE", "off") == "on":
            ax.set_title(title, fontsize=10)
        plt.colorbar(mesh, ax=ax, pad=0.02, shrink=0.85,
                     label=PRODUCT_LABELS.get(product, product))
    # NOTE: no bbox_inches="tight" - crops the GeoAxes (see core.radar).
    buf = io.BytesIO()
    # Wide (CONUS-class) renders carry the pixels for deep
    # digital zoom-in; hub-scale renders stay light
    # Mid tier (±5 deg warm frames) is the instant-open payload:
    # ~202 frames base64'd into ONE embed. dpi is the direct
    # multiplier on that payload, so it is a page-load budget,
    # not just a quality dial. 180 -> 1440x1260, map ~1400 px
    # across 10 deg = 140 px/deg (vs 112 px/deg on the old
    # ±2.5/dpi-100 frames), so it is SHARPER per degree than
    # what shipped before while covering 4x the area, with
    # ~1.5x digital zoom-in headroom. Raise toward 230 only
    # after checking the open-page transfer size.
    # Resolution expressed as PIXELS PER DEGREE, not a dpi tier
    # keyed on zoom. The tiered version gave dpi 260 to anything
    # wider than 10 degrees, so the new 13-degree region would have
    # rendered LARGER than the two 10-degree frames it replaced —
    # exactly backwards from the reason for combining them.
    #
    # figsize is a fixed (8, 7) inches, so dpi is NOT px/degree:
    # width_px = 8 * dpi, and the frame spans 2 * zoom_deg degrees.
    # Solve for the dpi that yields the target density.
    _ppd = float(os.environ.get("CAM_WARM_PPD", "150"))
    _dpi = max(90.0, min(300.0, _ppd * 2.0 * float(zoom_deg) / 8.0))
    fig.savefig(buf, format="png", dpi=_dpi)
    plt.close(fig)
    buf.seek(0)
    raw = buf.getvalue()

    # WebP, not PNG. Measured 8/24 on a representative frame:
    # 533 KB PNG32 -> 190 KB WebP LOSSLESS, a 64% cut with no
    # quality loss whatsoever. Quality 90 is 140 KB and visually
    # identical on a reflectivity field, which is what the default
    # uses; set CAM_WEBP_Q=100 for mathematically lossless.
    #
    # This matters beyond speed: the CAM page ships ~77 MB per view
    # because frames are base64'd inline, and 25 GB of monthly
    # bandwidth disappears in about 330 views. This is the single
    # biggest per-image lever and costs one conversion per frame.
    #
    # CAM_IMG_FORMAT=png reverts. Every browser in use supports
    # WebP, but the escape hatch is cheap to keep.
    if os.environ.get("CAM_IMG_FORMAT", "webp").lower() != "webp":
        return raw
    try:
        import io as _io

        from PIL import Image as _Im

        q = int(os.environ.get("CAM_WEBP_Q", "90"))
        im = _Im.open(_io.BytesIO(raw)).convert("RGBA")
        out = _io.BytesIO()
        im.save(out, "WEBP", quality=q, lossless=(q >= 100),
                method=4)
        conv = out.getvalue()
        # Only if it actually helped — a tiny or already-sparse
        # frame can compress worse.
        return conv if len(conv) < len(raw) else raw
    except Exception:
        return raw

def complete_cycles(model: str, need_fhr: int, n: int = 3,
                    now=None) -> list:
    """The n newest cycles that have reached need_fhr, newest first.

    A model publishes forecast hours as it computes them, so the
    newest cycle is usually PARTIAL — an 18Z HRRR at 18:20Z may only
    have f00-f02, which is useless for planning and worse than
    useless if it silently replaces a complete 17Z run.

    latest_cycle already probes for a SPECIFIC hour, so asking it for
    the LAST hour of the run is exactly the completeness test: if the
    final hour exists, everything before it does too.

    Costs one HEAD request per candidate cycle, so it is cheap enough
    to call on a page load.
    """
    from datetime import datetime, timedelta, timezone

    cfg = MODELS[model]
    now = now or datetime.now(timezone.utc)
    out = []
    probe = now
    for _ in range(n):
        cyc = latest_cycle(model, need_fhr, now=probe)
        if cyc is None:
            break
        out.append(cyc)
        # Step back past this cycle and look for the one before it.
        probe = cyc - timedelta(minutes=1)
    return out


def newest_complete(model: str, need_fhr: int, now=None):
    """Newest cycle that has reached need_fhr, or None."""
    got = complete_cycles(model, need_fhr, n=1, now=now)
    return got[0] if got else None

