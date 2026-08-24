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

HUBS = {
    "KJFK": (40.6413, -73.7781),
    "KMCO": (28.4312, -81.3081),
    "KFLL": (26.0726, -80.1527),
    "KDCA": (38.8521, -77.0377),
    "KBOS": (42.3629, -71.0064),
}
WARM_ZOOM = 2.5
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
    return list(HUBS), dict(HUBS), WARM_ZOOM * RENDER_FACTOR
# Bump when render styling changes so prewarmed frames rebuild
# (v2: 10 nm range ring; v3: fix hub-center leak - every frame
# had rendered centered on the LAST hub in the dict, Boston,
# regardless of which hub's path it was saved under)
WARM_STYLE = 5   # v5: ±5 deg canvas, full-frame default view
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
WARM_CAP_FHR = int(os.environ.get("CAM_WARM_MAX_FHR", "24"))


def warm_hours(model: str) -> list:
    return list(range(0, min(WARM_MAX.get(model, 12),
                             WARM_CAP_FHR) + 1))


# Back-compat union (page-side gating uses per-model warm_hours)
WARM_HOURS = list(range(0, max(WARM_MAX.values()) + 1))
# All four panel models. Incremental by design: each model's
# manifest skips work until IT publishes a new cycle - HRRR churns
# hourly, NAM 6-hourly, the HRW pair only 00/12Z - so after the
# one-time fill (~10-15 min) steady-state cost is modest.
WARM_MODELS = ["hrrr", "rrfs", "nam_nest",
               "hiresw_arw", "hiresw_fv3"]
# Warm JOBS: a job is "model" (legacy, product=WARM_PRODUCT) or
# "model@PRODUCT". REFS jobs warm the flagship ensemble products
# so hub loads scrub instantly, same as the deterministic grid.
REFS_WARM_JOBS = [
    "refs_pmmn@REFC",
    "refs_prob@PROB_CIG1000",
    "refs_prob@PROB_VIS1",
    "refs_prob@PROB_REFC40",
]
WARM_JOBS = WARM_MODELS + REFS_WARM_JOBS
for _j in REFS_WARM_JOBS:
    WARM_MAX[_j] = 60   # full REFS run - hub scrubs instant to f60


def _job(key: str) -> tuple:
    """(model, product) for a warm job key."""
    if "@" in key:
        m, p = key.split("@", 1)
        return m, p
    return key, WARM_PRODUCT
CHECK_INTERVAL_S = 600

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


def warm_get(cache_root: Path, model: str, icao: str,
             fhr: int) -> Optional[tuple[bytes, str]]:
    """Pre-rendered frame if one exists for the model's warmed cycle.
    Returns (png_bytes, cycle_iso) or None."""
    man = _read_manifest(cache_root, model)
    cycle_iso = man.get("cycle")
    if not cycle_iso or (icao.upper() not in HUBS
                         and icao.upper() != CONUS_KEY):
        return None
    if fhr not in warm_hours(model):
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
        mh = warm_hours(m)
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
    """{model: cycle_iso or None} for the page caption."""
    return {
        m: _read_manifest(cache_root, m).get("cycle")
        for m in WARM_MODELS
    }


def _warm_model(cache_root: Path, key: str, log) -> None:
    """Warm one job (model or model@product) for its newest cycle."""
    from core.hrrr_cam import (
        MODELS, latest_cycle, parallel_fetch_decode, render_field,
    )

    model, w_product = _job(key)
    mh = warm_hours(key)
    max_h = min(max(mh), MODELS[model]["max_fhr"])
    lo = MODELS[model].get("min_fhr", 0)
    hours = [h for h in mh if lo <= h <= max_h]
    cyc = latest_cycle(model, max_h)
    if cyc is None:
        return
    cycle_iso = cyc.isoformat()
    man = _read_manifest(cache_root, key)
    # Completeness is recomputed against the CURRENT hub set and
    # warm depth from the files themselves - a manifest's old
    # "complete" flag must not skip a newly added hub (KBOS once
    # sat cold for hours waiting on the HRW pair's next 12-hourly
    # cycle because of exactly that).
    _icaos, _coords, _zoom = _job_geom(key)
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
            "lat": lat, "lon": lon, "zoom_deg": _zoom,
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
            valid = cyc + timedelta(hours=h)
            title = (f"{MODELS[model]['label']}  "
                     f"valid {valid:%m/%d %H}Z")
            headline = (f"{MODELS[model]['label']} "
                        f"{cyc:%d %b %Y  %H}Z run")
            try:
                png = render_field(
                    w_product, vals, lats, lons,
                    _hla, _hlo, _zoom, title,
                    headline=headline,
                )
            except Exception:
                continue
            _frame_path(cache_root, key, cycle_iso, icao,
                        h).write_bytes(png)
            n_ok += 1
            if _yield_s:
                time.sleep(_yield_s)
            del png, vals, lats, lons
            import gc
            gc.collect()
            time.sleep(0.25)   # stay polite to user requests

    _manifest_path(cache_root, key).write_text(json.dumps({ "style": WARM_STYLE,
        "cycle": cycle_iso,
        "product": w_product,
        "complete": True,
        "frames": n_ok,
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

    # Prune older cycle dirs for this model (keep the newest 2)
    mdir = _warm_dir(cache_root) / key.replace("@", "__")
    if mdir.exists():
        dirs = sorted(d for d in mdir.iterdir() if d.is_dir())
        for d in dirs[:-2]:
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

    man = _read_manifest(cache_root, model)
    cycle_iso = man.get("cycle")
    if not cycle_iso:
        return None, None
    if fhr not in warm_hours(model):
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

