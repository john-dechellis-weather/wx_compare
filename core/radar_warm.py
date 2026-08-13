"""Radar pre-warmer: hub L3 loops rendered ahead of the click.

The slow part of the Quick View radar deck (fetch + decode + map
render) runs in a background thread for the hub airports, saving
aircraft-free frames plus their pixel<->degree geometry to the
persistent disk. At click time the page grabs the finished PNGs and
STAMPS current JBU triangles onto them in milliseconds - prewarmed
speed with live aircraft.

Scope: hub airports at the page's default zoom, REF + ET, newest 6
frames each. Cycle every 4 minutes; only frames not already in the
store are rendered, so steady-state work is 1-2 renders per
hub/product per cycle. Env kill switch: RADAR_WARMER=off.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RADAR_WARM_HUBS = {
    "KJFK": (40.6413, -73.7781),
    "KMCO": (28.4312, -81.3081),
    "KFLL": (26.0726, -80.1527),
    "KDCA": (38.8512, -77.0402),
    "KDJT": (26.6832, -80.0956),
}
# Bump when radar RENDERING changes (thresholds, colormaps): a
# mismatch forces immediate re-warm instead of stale frames aging
# out over half an hour.
STYLE_V = 7

WARM_ZOOM = 1.5
WARM_PRODUCTS = ["REF", "ET"]
WARM_N_FRAMES = 6
CYCLE_S = 240
FRESH_S = 900          # serve warm frames only if newer than this

_started = False
_lock = threading.Lock()


def _hub_dir(cache_root: Path, icao: str, product: str) -> Path:
    d = cache_root / "radar_warm" / icao.upper() / product
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_"
                   for c in name)


def _manifest(cache_root: Path, icao: str, product: str) -> dict:
    p = _hub_dir(cache_root, icao, product) / "manifest.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {}


def _write_manifest(cache_root: Path, icao: str, product: str,
                    man: dict) -> None:
    p = _hub_dir(cache_root, icao, product) / "manifest.json"
    p.write_text(json.dumps(man))


def _warm_hub_product(cache_root: Path, icao: str, product: str,
                      site: str, lat: float, lon: float, log) -> None:
    from core.radar3 import fetch_recent, parse_l3, render_l3

    files = fetch_recent(product, site, n=WARM_N_FRAMES)
    if not files:
        return
    man = _manifest(cache_root, icao, product)
    known = man.get("frames", {})
    if man.get("style") != STYLE_V:
        known = {}   # rendering changed: rebuild everything
    d = _hub_dir(cache_root, icao, product)

    new_frames = {}
    n_rendered = 0
    for raw, name in files:
        key = _safe(name)
        if key in known and (d / f"{key}.png").exists():
            new_frames[key] = known[key]
            continue
        try:
            parsed = parse_l3(raw)
            png, geom = render_l3(
                parsed, product, lat, lon, WARM_ZOOM, site,
                title_note=name, return_geometry=True,
                mark_center=True,
            )
        except Exception:
            continue
        (d / f"{key}.png").write_bytes(png)
        new_frames[key] = {"name": name, "geom": geom}
        n_rendered += 1
        import gc
        gc.collect()
        time.sleep(0.5)

    # Prune frames that fell out of the window
    for key in set(known) - set(new_frames):
        try:
            (d / f"{key}.png").unlink()
        except OSError:
            pass

    _write_manifest(cache_root, icao, product, {
        "site": site,
        "style": STYLE_V,
        "order": [_safe(n) for _r, n in files],
        "frames": new_frames,
        "updated": time.time(),
    })
    if n_rendered:
        log(f"{icao}/{product}: {n_rendered} new frames "
            f"({len(new_frames)} total)")


def _daemon(cache_root: Path) -> None:
    log_path = cache_root / "radar_warm" / "warmer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        try:
            with open(log_path, "a") as fh:
                fh.write(
                    f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                    f"{msg}\n"
                )
        except OSError:
            pass

    # Resolve nearest radar site per hub once
    from core.nexrad_sites import nearest_site
    sites = {}
    for icao, (lat, lon) in RADAR_WARM_HUBS.items():
        try:
            sites[icao], _ = nearest_site(lat, lon)
        except Exception:
            continue
    log(f"radar warmer started: {sites}")

    while True:
        for icao, (lat, lon) in RADAR_WARM_HUBS.items():
            site = sites.get(icao)
            if not site:
                continue
            for product in WARM_PRODUCTS:
                try:
                    _warm_hub_product(
                        cache_root, icao, product, site, lat, lon,
                        log,
                    )
                except Exception:
                    log(f"{icao}/{product} failed:\n"
                        f"{traceback.format_exc()}")
        time.sleep(CYCLE_S)


def ensure_radar_warmer_started(cache_root: Path) -> None:
    """Idempotent per-process start. RADAR_WARMER=off disables."""
    global _started
    if os.environ.get("RADAR_WARMER", "on").lower() == "off":
        return
    with _lock:
        if _started:
            return
        threading.Thread(
            target=_daemon, args=(cache_root,), daemon=True,
            name="radar-hub-warmer",
        ).start()
        _started = True


def warm_get_loop(cache_root: Path, icao: str, product: str,
                  zoom: float) -> Optional[list]:
    """Prewarmed frames [(png_bytes, name, geom), ...] oldest-first,
    or None when not applicable (non-hub, wrong zoom, stale)."""
    icao = icao.upper()
    if icao == "KPBI":
        icao = "KDJT"
    if icao not in RADAR_WARM_HUBS or abs(zoom - WARM_ZOOM) > 0.01:
        return None
    man = _manifest(cache_root, icao, product)
    if not man or time.time() - man.get("updated", 0) > FRESH_S:
        return None
    if man.get("style") != STYLE_V:
        return None   # stale styling: live path until re-warmed
    d = _hub_dir(cache_root, icao, product)
    out = []
    for key in man.get("order", []):
        info = man.get("frames", {}).get(key)
        if not info:
            continue
        try:
            png = (d / f"{key}.png").read_bytes()
        except OSError:
            continue
        out.append((png, info["name"], info["geom"]))
    return out if len(out) >= 2 else None


def stamp_aircraft(png_bytes: bytes, geom: dict,
                   planes: list) -> bytes:
    """Draw current JBU triangles + callsigns onto a prewarmed frame.
    Milliseconds of PIL work - this is what lets prewarmed frames
    carry live aircraft."""
    if not planes:
        return png_bytes
    import io
    import math
    from PIL import Image, ImageDraw

    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    dr = ImageDraw.Draw(im)
    x0, x1 = geom["x0"], geom["x1"]
    yt, yb = geom["y_top"], geom["y_bot"]
    lon0, lon1 = geom["lon0"], geom["lon1"]
    lat0, lat1 = geom["lat0"], geom["lat1"]

    def to_px(lat, lon):
        fx = (lon - lon0) / (lon1 - lon0)
        fy = (lat1 - lat) / (lat1 - lat0)
        return x0 + fx * (x1 - x0), yt + fy * (yb - yt)

    for p in planes:
        if not (lat0 <= p.lat <= lat1 and lon0 <= p.lon <= lon1):
            continue
        x, y = to_px(p.lat, p.lon)
        hdg = math.radians(p.heading_deg or 0)
        size = 11.0
        pts = []
        for ang, r in ((0, size), (2.5, size * 0.7),
                       (math.pi, size * 0.35), (-2.5, size * 0.7)):
            a = hdg + ang
            pts.append((x + r * math.sin(a), y - r * math.cos(a)))
        dr.polygon(pts, fill="#00BFFF", outline="#FFFFFF")
        dr.text((x + 9, y + 6), p.callsign, fill="#00BFFF")

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def stamp_scene(png_bytes: bytes, geom: dict, target=None,
                others=(), trail=()) -> bytes:
    """Full-scene stamp for the Tracker's fast loop: trail polyline,
    fleet triangles (blue), and the tracked aircraft highlighted
    (red, larger). Milliseconds of PIL work per frame."""
    import io
    import math
    from PIL import Image, ImageDraw

    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    dr = ImageDraw.Draw(im)
    x0, x1 = geom["x0"], geom["x1"]
    yt, yb = geom["y_top"], geom["y_bot"]
    lon0, lon1 = geom["lon0"], geom["lon1"]
    lat0, lat1 = geom["lat0"], geom["lat1"]

    def to_px(lat, lon):
        fx = (lon - lon0) / (lon1 - lon0)
        fy = (lat1 - lat) / (lat1 - lat0)
        return x0 + fx * (x1 - x0), yt + fy * (yb - yt)

    def in_frame(lat, lon):
        return lat0 <= lat <= lat1 and lon0 <= lon <= lon1

    # Trail polyline (cyan, thin)
    pts = [to_px(la, lo) for la, lo in trail if in_frame(la, lo)]
    if len(pts) >= 2:
        dr.line(pts, fill="#00E5FF", width=2)

    def triangle(p, color, size):
        hdg = math.radians(p.heading_deg or 0)
        x, y = to_px(p.lat, p.lon)
        poly = []
        for ang, r in ((0, size), (2.5, size * 0.7),
                       (math.pi, size * 0.35),
                       (-2.5, size * 0.7)):
            a = hdg + ang
            poly.append((x + r * math.sin(a),
                         y - r * math.cos(a)))
        dr.polygon(poly, fill=color, outline="#FFFFFF")
        dr.text((x + 9, y + 6), p.callsign, fill=color)

    for p in others:
        if in_frame(p.lat, p.lon):
            triangle(p, "#00BFFF", 10.0)
    if target is not None and in_frame(target.lat, target.lon):
        triangle(target, "#FF3333", 14.0)

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
