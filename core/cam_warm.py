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
    "KDCA": (38.8512, -77.0402),
}
WARM_ZOOM = 2.5
WARM_PRODUCT = "REFC"
WARM_HOURS = list(range(0, 13))
# HRRR only: its hub fetches are small filter subregions. The idx
# models decode full-CONUS grids, and warming several concurrently
# inside the web process can OOM a small instance (-> 502 crash
# loop). Re-expand only with instance headroom.
WARM_MODELS = ["hrrr"]
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


def _frame_path(cache_root: Path, model: str, cycle_iso: str,
                icao: str, fhr: int) -> Path:
    safe_cycle = cycle_iso.replace(":", "").replace("+", "")
    d = _warm_dir(cache_root) / model / safe_cycle
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{icao}_{WARM_PRODUCT}_f{fhr:02d}.png"


def warm_get(cache_root: Path, model: str, icao: str,
             fhr: int) -> Optional[tuple[bytes, str]]:
    """Pre-rendered frame if one exists for the model's warmed cycle.
    Returns (png_bytes, cycle_iso) or None."""
    man = _read_manifest(cache_root, model)
    cycle_iso = man.get("cycle")
    if not cycle_iso or icao.upper() not in HUBS:
        return None
    if fhr not in WARM_HOURS:
        return None
    p = _frame_path(cache_root, model, cycle_iso, icao.upper(), fhr)
    if not p.exists():
        return None
    try:
        return p.read_bytes(), cycle_iso
    except OSError:
        return None


def warm_status(cache_root: Path) -> dict:
    """{model: cycle_iso or None} for the page caption."""
    return {
        m: _read_manifest(cache_root, m).get("cycle")
        for m in WARM_MODELS
    }


def _warm_model(cache_root: Path, model: str, log) -> None:
    """Warm one model for its newest cycle if not already done."""
    from core.hrrr_cam import (
        MODELS, latest_cycle, parallel_fetch_decode, render_field,
    )

    max_h = min(max(WARM_HOURS), MODELS[model]["max_fhr"])
    hours = [h for h in WARM_HOURS if h <= max_h]
    cyc = latest_cycle(model, max_h)
    if cyc is None:
        return
    cycle_iso = cyc.isoformat()
    man = _read_manifest(cache_root, model)
    if man.get("cycle") == cycle_iso and man.get("complete"):
        return

    log(f"warming {model} cycle {cycle_iso}")
    tasks = []
    for icao, (lat, lon) in HUBS.items():
        for h in hours:
            tasks.append({
                "key": (icao, h),
                "model": model, "product": WARM_PRODUCT,
                "cycle": cyc, "fhr": h,
                "lat": lat, "lon": lon, "zoom_deg": WARM_ZOOM,
            })
    data = parallel_fetch_decode(tasks, max_workers=2)

    n_ok = 0
    for icao, (lat, lon) in HUBS.items():
        for h in hours:
            res = data.get((icao, h))
            if isinstance(res, Exception) or res is None:
                continue
            vals, lats, lons = res
            valid = cyc + timedelta(hours=h)
            title = (
                f"{MODELS[model]['label']} {cyc:%m/%d %H}Z  "
                f"f{h:02d}  valid {valid:%m/%d %H}Z  [prewarmed]"
            )
            try:
                png = render_field(
                    WARM_PRODUCT, vals, lats, lons, lat, lon,
                    WARM_ZOOM, title,
                )
            except Exception:
                continue
            _frame_path(cache_root, model, cycle_iso, icao,
                        h).write_bytes(png)
            n_ok += 1
            del png, vals, lats, lons
            import gc
            gc.collect()
            time.sleep(0.25)   # stay polite to user requests

    _manifest_path(cache_root, model).write_text(json.dumps({
        "cycle": cycle_iso,
        "complete": True,
        "frames": n_ok,
        "warmed_at": datetime.now(timezone.utc).isoformat(),
    }))
    data.clear()
    log(f"warmed {model}: {n_ok} frames")

    # Prune older cycle dirs for this model (keep the newest 2)
    mdir = _warm_dir(cache_root) / model
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
        for model in WARM_MODELS:
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