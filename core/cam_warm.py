"""Hub pre-warmer for the Hi-Res CAMs page.

TropicalTidbits trick, BlueMet-sized: a background thread watches for
new model cycles and immediately renders a fixed menu - four JBU hubs
x composite reflectivity x f00-f12 - to PNGs on the persistent disk.
Page requests that match the menu are served from disk instantly; the
compute happened once per cycle instead of once per viewer.

v1 scope (deliberate):
  - Hubs: KJFK, KMCO, KFLL, KDCA at zoom 2.5 (the page default)
  - Product: REFC only
  - Hours: f00-f12
  - Frames contain NO aircraft overlay (pre-rendered images cannot
    hold live positions; the page serves warm frames only when the
    JBU overlay is off)
Per-model warming triggers only when that model's cycle changes, so
hourly background work is just HRRR's 4x13 frames.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# TWO REGIONS, not five hubs.
#
# The hub frames were +-5 degrees each and overlapped heavily: JFK,
# EWR, LGA, PHL, ALB and BDL all fell inside BOTH a DCA frame and a
# BOS frame, so two renders covered the same ground twice. One
# 13x13 box covers 169 square degrees against 200 for the pair, and
# holds every station from CLT at 35.2N to PWM at 43.6N.
#
# Measured cost, relative to one 10x10 frame:
#     FL(10) + MidAtl(10) + NE(10)   3.00 units   58% duty
#     FL(10) + combined(13)          2.69 units   52%
#     the same at 150 px/degree      1.87 units   36%
#
# This is the opposite conclusion from a single CONUS frame, and the
# difference is what the extra area buys. CONUS adds 1,500 square
# degrees of ocean and empty West; this adds 50 that were already
# being rendered twice.
#
# Each entry is (lat, lon, half_width_degrees).
HUBS = {
    "NE": (40.60, -74.00, 6.5),
    "FL": (27.25, -80.73, 5.0),
}
# Display names. The keys are short because they appear in every
# frame FILENAME on disk; the labels are what a user reads.
HUB_LABELS = {
    "NE": "Northeast and Mid-Atlantic",
    "FL": "Florida",
}
WARM_ZOOM = 2.5


def hub_geom(icao: str):
    """(lat, lon, half_width_deg) for a region."""
    v = HUBS[icao]
    return (v[0], v[1], v[2] if len(v) > 2 else WARM_ZOOM * RENDER_FACTOR)


# Pixels per degree for warm frames. 150, not 180: a 13x13 frame at
# 180 is 2340 px square, and the panel displays at ~900 px, so the
# extra resolution is never seen and costs 30% more bytes and render
# time. 150 px/deg still resolves finer than the display.
WARM_PPD = int(os.environ.get("CAM_WARM_PPD", "150"))
# Frames render at RENDER_FACTOR x the display zoom. The whole
# frame IS the default view now (no home pre-zoom): the user
# opens fully zoomed out and wheels IN for detail. So this
# factor sets the size of the map you see on page open.
# 2.5 x 2 = ±5 deg = a 10x10 degree box; from JFK that runs
# 35.6N-45.6N, 68.8W-78.8W (Cape Hatteras to Montreal,
# Cleveland to Nantucket) with model data across all of it.
RENDER_FACTOR = 2  # +-5 deg: 10x10 box, shown whole at open
# Design A: deterministic CAM jobs warm ONE CONUS frame set per
# model-hour (serves every hub via client-side transform) - the
# hub dimension collapses, 5x fewer frames at higher dpi. REFS
# jobs stay hub-cropped at WARM_ZOOM.
CONUS_CENTER = (39.5, -97.5)
CONUS_ZOOM = 28.0
CONUS_KEY = "CONUS"


def _job_geom(key: str):
    """(hub_list, coords_map, zoom) for a warm job. Hybrid
    verdict 8/17: hub-native frames are ~3x sharper than any
    browser-tenable CONUS frame, so ALL jobs warm hub crops;
    CONUS is an explicit render mode (never warmed)."""
    # Coordinates AND half-width per region: they are no longer the
    # same size. NE is 13x13 (it absorbed the old DCA and BOS
    # frames), FL is 10x10.
    return (list(HUBS),
            {k: (v[0], v[1]) for k, v in HUBS.items()},
            {k: hub_geom(k)[2] for k in HUBS})
# Bump when render styling changes so prewarmed frames rebuild
# (v2: 10 nm range ring; v3: fix hub-center leak - every frame
# had rendered centered on the LAST hub in the dict, Boston,
# regardless of which hub's path it was saved under)
# v6: two REGIONS (NE 13x13, FL 10x10) replacing five
# +-5 deg hub frames, and a fixed 150 px/degree instead of a
# dpi tier. Frame paths do not encode geometry, so without
# this bump every stale hub frame would be served as if it
# were the new region.
# v7: composite fast renderer (core.cam_fast) — cached
# basemap plus a LUT-coloured data layer instead of contourf.
# Visually close but not byte-identical, and the title is no
# longer baked in, so old frames must not be mixed with new.
WARM_STYLE = 12  # v12: 10 nm rings, tighter labels
# ^^ THIS MUST BE BUMPED WHENEVER RENDER_FACTOR / WARM_ZOOM /
# dpi CHANGE. Frame paths do NOT encode geometry and warm_get
# does NOT check style - it serves whatever bytes sit on disk
# for the manifest's cycle. A geometry change without a style
# bump leaves the manifest reading "current", so the warmer
# never rebuilds and the page serves frames at the OLD zoom
# forever. (Exactly what happened 8/17: RENDER_FACTOR moved
# but style stayed 4, so ±2.5 deg frames kept being served
# while every constant in the file claimed otherwise.)
WARM_PRODUCT = "REFD"
# Per-model warm depth: HRRR's hourly cycles top out at f18;
# NAM/HRW warm a full day. Raise these toward 48/60 for total
# coverage at ~2.5x the disk and fill time.
# Every model warmed to its full horizon. HRRR stays 18 by
# design: warming to 48 would pin its warm store to the four
# synoptic cycles, sacrificing the hourly freshness that is
# HRRR's whole identity (f19-48 stays live behind the extended
# toggle).
# Per-model depth, then a GLOBAL CAP. Warming every model to its own
# maximum is 1,255 frames per cycle set and a ~100% duty cycle, which
# leaves nothing for the page that matters. Capping at 24 h cuts it to
# 500 frames and ~58%, and hours past 24 are rendered on demand — a
# few seconds each, and rarely scrubbed.
#
# Raise CAM_WARM_MAX_FHR only after the warmer log shows headroom.
WARM_MAX = {"hrrr": 18, "rrfs": 84, "nam_nest": 60,
            "hiresw_arw": 48, "hiresw_fv3": 48}
# f24 for EVERY job, CAMs and REFS alike. Measured duty cycle at this
# cap, each model against its own cadence:
#
#     hrrr        1 h cycle   95 frames   26%   <- hourly, dominates
#     rrfs        3 h        125          12%
#     nam_nest    6 h        125           6%
#     hiresw_arw  6 h        125           6%
#     hiresw_fv3  6 h        125           6%
#     REFS x4     6 h        500          23%
#                                        ----
#                                         79%
#
# That leaves headroom for the CONUS map, which is the page that
# must never be slow. Uncapped REFS alone is 56% and pushes the
# total past 100%, at which point the warmer never catches up and
# the store stays empty — which is what had been happening.
#
# Hours past f24 render on demand at a few seconds each. Page 11 now
# USES partial coverage rather than ignoring the store when the last
# requested hour is not warm.
#
# Raise CAM_WARM_MAX_FHR only after the warmer log shows the store
# filling and page 3 still opening fast.
WARM_CAP_FHR = int(os.environ.get("CAM_WARM_MAX_FHR", "24"))

# Per-job depth overrides, and a SYNOPTIC-ONLY deep tier.
#
# HRRR runs hourly, so warming it to f48 costs 68% duty on its own —
# 245 frames every hour, forever. But the extension only EXISTS on
# 00/06/12/18Z. Warming f0-18 every cycle and the f19-48 tail only on
# those four runs costs ~17% instead of 68% and loses nothing, since
# there is no extended data to warm on the other twenty cycles.
#
# RRFS is 3-hourly, so its full f84 is only 39% and needs no split.
WARM_DEPTH = {"hrrr": 18, "rrfs": 84,
              "refs_pmmn": 60, "refs_prob": 60}
SYNOPTIC_DEPTH = {"hrrr": 48}
SYNOPTIC_HOURS = {0, 6, 12, 18}


def warm_hours(model: str, cycle_iso: str = None) -> list:
    """Hours to warm for this job, for this cycle.

    Deeper on synoptic runs where the extended forecast exists;
    WARM_CAP_FHR still bounds anything without an explicit depth.
    """
    # `model` may be a JOB KEY like "hrrr@VIS". Depth is a property
    # of the MODEL, not the product — without this split every
    # non-reflectivity job silently fell back to the 12-hour default.
    mkey = model.split("@", 1)[0] if "@" in str(model) else model
    base = WARM_DEPTH.get(mkey)
    if base is None:
        base = min(WARM_MAX.get(model, WARM_MAX.get(mkey, 12)),
                   WARM_CAP_FHR)
    deep = SYNOPTIC_DEPTH.get(mkey)
    if deep and cycle_iso:
        try:
            from datetime import datetime as _d

            if _d.fromisoformat(str(cycle_iso)).hour in SYNOPTIC_HOURS:
                # NOT clamped by WARM_MAX: that holds the model's
                # ROUTINE depth (HRRR 18), and the whole point of the
                # synoptic tier is that the run goes further than
                # routine. Clamping here silently disabled it.
                base = deep
        except Exception:
            pass
    return list(range(0, base + 1))


# Back-compat union (page-side gating uses per-model warm_hours)
WARM_HOURS = list(range(0, max(WARM_MAX.values()) + 1))
# All four panel models. Incremental by design: each model's
# manifest skips work until IT publishes a new cycle - HRRR churns
# hourly, NAM 6-hourly, the HRW pair only 00/12Z - so after the
# one-time fill (~10-15 min) steady-state cost is modest.
# HRRR and RRFS only. The HiResW pair and NAM were warmed too, which
# meant five models competing for the same CPU as the CONUS map for
# panels nobody was scrubbing. Two models at f0-24 is 38% duty
# instead of 55%, and it is what the 4-panel view actually needs.
#
# The others still RENDER on demand — they are in MODELS and remain
# selectable — they just are not pre-warmed.
WARM_MODELS = [m.strip() for m in
               os.environ.get("CAM_WARM_MODELS", "hrrr,rrfs").split(",")
               if m.strip()]
# Warm JOBS: a job is "model" (legacy, product=WARM_PRODUCT) or
# "model@PRODUCT". REFS jobs warm the flagship ensemble products
# so hub loads scrub instantly, same as the deterministic grid.
REFS_WARM_JOBS = [
    "refs_pmmn@REFC",
    "refs_prob@PROB_CIG1000",
    "refs_prob@PROB_VIS1",
    "refs_prob@PROB_REFC40",
]
# Every aviation product, not just reflectivity. This was one
# product because contourf cost ~10 s a frame and six products did
# not fit; core.cam_fast renders in ~0.5 s, so all six are ~11% duty
# including REFS. CAM_WARM_PRODUCTS trims it without a deploy.
CAM_PRODUCTS = [p.strip() for p in os.environ.get(
    "CAM_WARM_PRODUCTS",
    "REFD,REFC,RETOP,VIS,CEIL,GUST,UGRD10,VGRD10").split(",")
    if p.strip()]
# UGRD10/VGRD10 are fetched so the point store can sample wind at
# every airport, but they are never rendered — a wind component is
# not a map anyone views. The fetch is what costs; the render is
# what is skipped.
SAMPLE_ONLY_PRODUCTS = {"UGRD10", "VGRD10"}
WARM_JOBS = [f"{m}@{p}" for m in WARM_MODELS for p in CAM_PRODUCTS]
WARM_JOBS += REFS_WARM_JOBS
for _j in REFS_WARM_JOBS:
    WARM_MAX[_j] = 60   # full REFS run - hub scrubs instant to f60


def _job(key: str) -> tuple:
    """(model, product) for a warm job key."""
    if "@" in key:
        m, p = key.split("@", 1)
        return m, p
    return key, WARM_PRODUCT
CHECK_INTERVAL_S = 600

# ---------------------------------------------------------------------------
# Traffic-aware backoff
# ---------------------------------------------------------------------------
# A fixed yield between frames is blind: it pauses just as much at
# 03Z with nobody on the site as it does while someone is waiting for
# the CONUS map. This makes the warmer aware of actual requests.
#
# Pages call note_request() as they start rendering. Before each
# frame the warmer checks how long ago that was, and if the site is
# in use it waits — up to a bounded number of times, so a page on a
# 2-minute auto-refresh cannot starve the warmer forever.
#
# It cannot fix the underlying problem, which is that matplotlib
# holds the GIL and Python threads therefore cannot truly run in
# parallel. The real fix is a separate PROCESS. This gets most of
# the benefit for none of the memory.
_LAST_REQUEST = [0.0]
QUIET_S = float(os.environ.get("CAM_WARM_QUIET_S", "4"))
MAX_BACKOFF_S = float(os.environ.get("CAM_WARM_MAX_BACKOFF_S", "25"))


def note_request() -> None:
    """Called by pages on render. Cheap enough for every rerun."""
    _LAST_REQUEST[0] = time.time()


def _wait_for_quiet() -> float:
    """Block while the site is in use. Returns seconds waited."""
    waited = 0.0
    while waited < MAX_BACKOFF_S:
        since = time.time() - _LAST_REQUEST[0]
        if since >= QUIET_S:
            return waited
        time.sleep(0.5)
        waited += 0.5
    return waited

_started = False
_lock = threading.Lock()


def _warm_dir(cache_root: Path) -> Path:
    d = cache_root / "cam_warm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(cache_root: Path, model: str) -> Path:
    return _warm_dir(cache_root) / f"{model}.manifest.json"


def _read_manifest(cache_root: Path, model: str) -> dict:
    p = _manifest_path(cache_root, model)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {}


def _frame_path(cache_root: Path, key: str, cycle_iso: str,
                icao: str, fhr: int) -> Path:
    """Path to a warm frame.

    Returns an EXISTING file in either format if one is there, and
    otherwise the path for the format currently configured. That
    matters during the PNG-to-WebP switchover: without it, changing
    the format makes every frame already on disk invisible and the
    whole store silently rebuilds — hours of work thrown away for a
    file-extension change.
    """
    model, product = _job(key)
    safe_cycle = cycle_iso.replace(":", "").replace("+", "")
    d = _warm_dir(cache_root) / key.replace("@", "__") / safe_cycle
    d.mkdir(parents=True, exist_ok=True)
    for ext in ("webp", "png"):
        cand = d / f"{icao}_{product}_f{fhr:02d}.{ext}"
        if cand.exists():
            return cand
    ext = "webp" if os.environ.get(
        "CAM_IMG_FORMAT", "webp").lower() == "webp" else "png"
    return d / f"{icao}_{product}_f{fhr:02d}.{ext}"


def warm_cycle(cache_root: Path, key: str):
    """Warmed cycle iso for a job key, or None."""
    return _read_manifest(cache_root, key).get("cycle")


def _resolve_frame(cache_root: Path, key: str, fhr: int):
    """(cycle_iso, fhr) of the warm frame for `fhr` on the newest
    cycle, shifting into the deep cycle when the newest run does
    not reach that far. Preserves VALID TIME across the shift.

    One resolver, used by both warm_get and publish_frame, so the
    two cannot disagree about which frame represents an hour.
    """
    man = _read_manifest(cache_root, key)
    cycle_iso = man.get("cycle")
    if not cycle_iso:
        return None, None
    # STYLE CHECK. A manifest written under an older WARM_STYLE
    # describes frames rendered the old way — pre-stations basemap,
    # old palette, whatever changed. Serving them as current is why
    # a style bump appeared to do nothing on the REFS page for an
    # hour: REFS is the last job in the pass, and its stale frames
    # were handed out until the warmer finally reached it. Refuse
    # them; the page falls back to a live render that IS current.
    if man.get("style") != WARM_STYLE:
        return None, None
    if fhr in warm_hours(key, cycle_iso):
        return cycle_iso, fhr
    dc = man.get("deep_cycle")
    if not dc:
        return None, None
    try:
        from datetime import datetime as _d

        off = int(round((_d.fromisoformat(cycle_iso)
                         - _d.fromisoformat(dc)).total_seconds()
                        / 3600.0))
    except Exception:
        return None, None
    deep_fhr = fhr + off
    if 0 <= deep_fhr <= int(man.get("deep_max", 0)):
        return dc, deep_fhr
    return None, None


def warm_get(cache_root: Path, model: str, icao: str,
             fhr: int) -> Optional[tuple[bytes, str]]:
    """Pre-rendered frame if one exists for the model's warmed cycle.
    Returns (png_bytes, cycle_iso) or None."""
    # Callers may pass a bare model ("hrrr") or a full job key
    # ("hrrr@VIS"). Manifests are per JOB now that every product is
    # warmed, so default the product rather than make every call
    # site build the key.
    if "@" not in model:
        model = f"{model}@{WARM_PRODUCT}"
    if icao.upper() not in HUBS and icao.upper() != CONUS_KEY:
        return None
    cycle_iso, fhr = _resolve_frame(cache_root, model, fhr)
    if not cycle_iso:
        return None
    p = _frame_path(cache_root, model, cycle_iso, icao.upper(), fhr)
    if not p.exists():
        return None
    try:
        return p.read_bytes(), cycle_iso
    except OSError:
        return None


def warm_report(cache_root: Path) -> list:
    """One line per model for the debug expander: cycle, style
    era, and frame count on disk vs expected - the ground truth
    for 'why don't my rings show'."""
    from core.hrrr_cam import MODELS

    out = []
    for m in WARM_JOBS:
        _mm, _pp = _job(m)
        man = _read_manifest(cache_root, m)
        cyc = man.get("cycle") or "-"
        style = man.get("style", "pre-ring")
        mh = warm_hours(m, cyc if cyc != "-" else None)
        max_h = min(max(mh), MODELS[_mm]["max_fhr"])
        _lo2 = MODELS[_mm].get("min_fhr", 0)
        hours = [h for h in mh if _lo2 <= h <= max_h]
        _gi, _gc, _gz = _job_geom(m)
        have = 0
        if man.get("cycle"):
            have = sum(
                1 for icao in _gi for h in hours
                if _frame_path(cache_root, m, man["cycle"],
                               icao, h).exists()
            )
        out.append(
            f"{m}: cycle={cyc} style={style} "
            f"frames={have}/{len(_gi) * len(hours)}"
        )
    return out


def warm_status(cache_root: Path) -> dict:
    """{model: cycle_iso or None} for the page caption.

    Reports against the PRIMARY product's job — every product of a
    model warms from the same cycle, so one is representative.
    """
    return {
        m: _read_manifest(
            cache_root, f"{m}@{WARM_PRODUCT}").get("cycle")
        for m in WARM_MODELS
    }


def _warm_model(cache_root: Path, key: str, log) -> None:
    """Warm one job (model or model@product) for its newest cycle."""
    from core import cam_fast as _CF
    from core import point_store as _PS
    from core.hrrr_cam import (
        MODELS, latest_cycle, parallel_fetch_decode, render_field,
    )

    model, w_product = _job(key)
    # Depth depends on the cycle (synoptic runs go deeper) and the
    # cycle probe needs a depth to ask for. Resolve it in two steps:
    # find the newest cycle at the SHALLOW depth, then recompute the
    # hour list now that the cycle hour is known.
    mh = warm_hours(key)
    max_h = min(max(mh), MODELS[model]["max_fhr"])
    cyc = latest_cycle(model, max_h)
    if cyc is not None:
        mh = warm_hours(key, cyc.isoformat())
        deep_max = min(max(mh), MODELS[model]["max_fhr"])
        if deep_max > max_h:
            # Synoptic run: confirm the extension actually published
            # before committing to warming 30 more frames per hub.
            if latest_cycle(model, deep_max) == cyc:
                max_h = deep_max
            else:
                mh = warm_hours(key)
    lo = MODELS[model].get("min_fhr", 0)
    hours = [h for h in mh if lo <= h <= max_h]
    if cyc is None:
        return
    cycle_iso = cyc.isoformat()
    man = _read_manifest(cache_root, key)
    # Completeness is recomputed against the CURRENT hub set and
    # warm depth from the files themselves - a manifest's old
    # "complete" flag must not skip a newly added hub (KBOS once
    # sat cold for hours waiting on the HRW pair's next 12-hourly
    # cycle because of exactly that).
    _icaos, _coords, _zooms = _job_geom(key)
    missing = [
        (icao, h)
        for icao in _icaos
        for h in hours
        if not _frame_path(cache_root, key, cycle_iso,
                           icao, h).exists()
    ]
    if (man.get("cycle") == cycle_iso
            and man.get("product") == w_product
            and man.get("style") == WARM_STYLE
            and not missing):
        return

    same_cycle = (man.get("cycle") == cycle_iso
                  and man.get("style") == WARM_STYLE)
    build = missing if same_cycle else [
        (icao, h) for icao in _icaos for h in hours
    ]
    log(f"warming {key} cycle {cycle_iso} "
        f"({len(build)} frames{' - fill-in' if same_cycle else ''})")
    tasks = []
    for icao, h in build:
        lat, lon = _coords[icao]
        tasks.append({
            "key": (icao, h),
            "model": model, "product": w_product,
            "cycle": cyc, "fhr": h,
            # Per-region: the GRIB subset must match the frame it
            # will be rendered into, or NE gets a 10-degree crop
            # drawn onto a 13-degree canvas.
            "lat": lat, "lon": lon, "zoom_deg": _zooms[icao],
        })
    # INSTRUMENTED. max_workers=2 has been the setting since this was
    # written and nobody has measured whether it is the constraint.
    # Fetch is network-bound and parallel; RENDER is matplotlib with
    # cartopy coastlines and runs serially in this thread. If render
    # dominates, raising workers changes nothing — so measure the
    # split before touching either. L2 warmer note: the same question
    # applies there.
    _t_fetch = time.time()
    data = parallel_fetch_decode(
        tasks, max_workers=int(os.environ.get("CAM_WORKERS", "2")))
    _t_fetch = time.time() - _t_fetch
    _t_render = time.time()

    # Yield between frames. A matplotlib render holds the GIL for its
    # duration, so a tight loop starves the thread serving page 3 —
    # which is the page that must never be slow. A short sleep
    # between frames costs a few minutes across a full rebuild and
    # makes the site usable while it runs. 0 disables.
    _yield_s = float(os.environ.get("CAM_WARM_YIELD_S", "0.6"))
    n_ok = 0
    for icao, h in build:
            _hla, _hlo = _coords[icao]
            res = data.get((icao, h))
            if isinstance(res, Exception) or res is None:
                continue
            vals, lats, lons = res

            # POINT STORE. The decoded array is in hand; sampling
            # every airport inside it is one nearest-cell lookup per
            # station and no extra fetch. This is what makes the MOS
            # page's RRFS/HRRR tables instant for the 29 stations
            # inside the warm regions.
            if w_product in _PS.POINT_PRODUCTS:
                try:
                    from core.hrrr_cam import JBU_STATIONS as _JS

                    _PS.record(cache_root, model, w_product,
                               cyc.isoformat(), h, vals, lats, lons,
                               _JS)
                except Exception:
                    pass
            if w_product in SAMPLE_ONLY_PRODUCTS:
                # Nothing to draw. Count it and move on.
                n_ok += 1
                del vals, lats, lons
                continue

            valid = cyc + timedelta(hours=h)
            title = (f"{MODELS[model]['label']}  "
                     f"valid {valid:%m/%d %H}Z")
            headline = (f"{MODELS[model]['label']} "
                        f"{cyc:%d %b %Y  %H}Z run")
            try:
                # FAST PATH. core.cam_fast composites a colourised
                # data layer onto a cached basemap instead of
                # contouring: measured 6.74 s -> 0.46 s per frame on
                # a 13-degree region, 14.6x, with smoother edges.
                # Falls back to the matplotlib path for any product
                # it has no palette for, and CAM_FAST=off reverts
                # everything without a deploy.
                if _CF.supports(w_product):
                    png = _CF.render_fast(
                        w_product, vals, lats, lons,
                        _hla, _hlo, _zooms[icao],
                        grid_key=f"{icao}|{model}",
                        ppd=WARM_PPD,
                        cache_dir=str(_warm_dir(cache_root)),
                    )
                else:
                    png = render_field(
                        w_product, vals, lats, lons,
                        _hla, _hlo, _zooms[icao], title,
                        headline=headline,
                    )
            except Exception as _rexc:
                log(f"{key} {icao} f{h:02d} render failed: "
                    f"{type(_rexc).__name__}: {_rexc}")
                continue
            _frame_path(cache_root, key, cycle_iso, icao,
                        h).write_bytes(png)
            n_ok += 1
            # Yield to anyone actually using the site before starting
            # the next frame.
            _wait_for_quiet()
            if _yield_s:
                time.sleep(_yield_s)
            del png, vals, lats, lons
            import gc
            gc.collect()
            time.sleep(0.25)   # stay polite to user requests

    # Carry forward the last SYNOPTIC cycle that warmed extended
    # hours. Without it those frames become unreachable the moment
    # the next hourly run overwrites "cycle": warm_get would look in
    # the 13Z directory for f34, not find it, and re-render from
    # scratch while the 12Z frames sat on disk unused.
    _routine = max(warm_hours(key))
    _deep_cycle = man.get("deep_cycle")
    _deep_max = man.get("deep_max", 0)
    if max(hours) > _routine:
        _deep_cycle, _deep_max = cycle_iso, max(hours)
    _manifest_path(cache_root, key).write_text(json.dumps({
        "style": WARM_STYLE,
        "cycle": cycle_iso,
        "product": w_product,
        "complete": True,
        "frames": n_ok,
        "deep_cycle": _deep_cycle,
        "deep_max": _deep_max,
        "warmed_at": datetime.now(timezone.utc).isoformat(),
    }))
    data.clear()
    _t_render = time.time() - _t_render
    # The number that decides the next optimisation: if fetch
    # dominates, raise CAM_WORKERS; if render does, workers are
    # irrelevant and the lever is dpi, figure size, or moving the
    # draw off the main thread.
    _n = max(n_ok, 1)
    log(f"warmed {key}: {n_ok} frames | "
        f"fetch {_t_fetch:.0f}s ({_t_fetch / _n:.1f}s/frame, "
        f"{int(os.environ.get('CAM_WORKERS', '2'))} workers) | "
        f"render {_t_render:.0f}s ({_t_render / _n:.1f}s/frame) | "
        f"{'FETCH-bound' if _t_fetch > _t_render else 'RENDER-bound'}")

    # Prune older cycle dirs for this job: keep the newest 2, AND
    # the deep cycle.
    #
    # HRRR runs hourly but only reaches f48 on synoptic cycles. The
    # manifest records that run as deep_cycle so f19-48 stay
    # reachable — but the pruner was keeping only the newest two
    # directories, so the 12Z f48 run was deleted the moment 14Z
    # landed. The manifest still pointed at it; the files were gone;
    # every hour past f18 came back empty two hours after the deep
    # run finished. Protecting the deep directory is what makes "the
    # last full run is always warm" actually true.
    mdir = _warm_dir(cache_root) / key.replace("@", "__")
    if mdir.exists():
        dirs = sorted(d for d in mdir.iterdir() if d.is_dir())
        _protect = set()
        if _deep_cycle:
            # Same transform _frame_path uses for directory names.
            _protect.add(str(_deep_cycle).replace(":", "")
                         .replace("+", ""))
        for d in dirs[:-2]:
            if d.name in _protect:
                continue
            for f in d.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass


def _daemon(cache_root: Path) -> None:
    log_path = _warm_dir(cache_root) / "warmer.log"

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} {msg}\n"
        try:
            with open(log_path, "a") as fh:
                fh.write(line)
        except OSError:
            pass

    log("warmer daemon started")
    while True:
        for model in WARM_JOBS:
            try:
                _warm_model(cache_root, model, log)
            except Exception:
                log(f"warm {model} failed:\n{traceback.format_exc()}")
        time.sleep(CHECK_INTERVAL_S)


def ensure_warmer_started(cache_root: Path) -> None:
    """Idempotent: starts the background warmer thread once per
    process. Safe to call on every page run. Set env CAM_WARMER=off
    to disable entirely (kill switch)."""
    import os
    if os.environ.get("CAM_WARMER", "on").lower() == "off":
        return
    global _started
    with _lock:
        if _started:
            return
        t = threading.Thread(
            target=_daemon, args=(cache_root,), daemon=True,
            name="cam-hub-warmer",
        )
        t.start()
        _started = True

# ---------------------------------------------------------------------------
# Serving frames as URLs instead of inline bytes
# ---------------------------------------------------------------------------
# The CAM page base64s every frame into the HTML. That costs three
# ways at once: +33% from the encoding, the browser cannot cache any
# of it because inline data has no URL, and Streamlit re-ships the
# WHOLE payload on every rerun — every slider move, every checkbox.
# Measured at ~77 MB per page view before WebP, which exhausted a
# 25 GB monthly bandwidth allowance in about 330 views.
#
# Publishing each frame as a file under static/ turns them into
# cacheable resources: the HTML drops to kilobytes, a rerun re-sends
# nothing, and scrubbing back through hours already seen costs zero.
#
# The warm store stays authoritative and lives on the PERSISTENT
# disk. static/ is inside the app directory, which is wiped on every
# deploy — so these are disposable copies, recreated on demand from
# the store. That is the right way round: losing them costs a file
# copy, losing the store costs hours of rendering.


def publish_frame(cache_root: Path, static_dir, model: str, icao: str,
                  fhr: int):
    """Copy a warm frame into static/ and return (name, cycle_iso).

    Returns (None, None) when the frame is not warm. Idempotent —
    an existing copy is reused, so the cost after the first call is
    one stat().
    """
    import shutil

    if "@" not in model:
        model = f"{model}@{WARM_PRODUCT}"
    cycle_iso, fhr = _resolve_frame(cache_root, model, fhr)
    if not cycle_iso:
        return None, None
    src = _frame_path(cache_root, model, cycle_iso, icao.upper(), fhr)
    if not src.exists():
        return None, None
    tag = str(cycle_iso).replace(":", "").replace("-", "").replace(
        "+", "")[:13]
    # Cycle is IN the filename, so a new run publishes new URLs and
    # the browser never serves a stale frame from cache.
    name = (f"cam_{model}_{tag}_{icao.upper()}"
            f"_f{fhr:02d}{src.suffix}")
    dest = Path(static_dir) / name
    if not dest.exists():
        try:
            Path(static_dir).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except OSError:
            return None, None
    return name, cycle_iso


def prune_published(static_dir, keep_cycles: int = 2):
    """Drop published frames from older cycles.

    static/ is ephemeral but not unbounded — a busy day of hourly
    HRRR cycles would accumulate thousands of files between deploys.
    """
    from collections import defaultdict

    d = Path(static_dir)
    if not d.exists():
        return 0
    by_model = defaultdict(set)
    for f in d.glob("cam_*_*"):
        parts = f.name.split("_")
        if len(parts) >= 4:
            by_model[parts[1]].add(parts[2])
    dropped = 0
    for model, cycles in by_model.items():
        for old in sorted(cycles, reverse=True)[keep_cycles:]:
            for f in d.glob(f"cam_{model}_{old}_*"):
                try:
                    f.unlink()
                    dropped += 1
                except OSError:
                    pass
    return dropped

