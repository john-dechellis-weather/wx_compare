"""Airport Radar (22 Oct): live Level II for one JetBlue station.

For a station, the nearest WSR-88D (core.nexrad_sites) is read from
the AWS Level II CHUNK feed (core.radar_l2rt) - the 0.5 deg
reflectivity sweep is available about a minute into each volume -
and rasterised straight from the radials (metpy Level2File, no
pyart, no gridding) onto a Web-Mercator box of half-width BOX_KM
around the radar: nearest gate per pixel, 250 m gates kept as they
are. Rendered with the AWIPS ramp from core.radar_l2.render_png.

Where the airport has a TDWR (core.radar_l3.STATION_RADAR "T:xxx"),
the page can switch to it: TDWR base data is not on the public
Level II feed, so that option uses the Level III TZL loop the L3
warmer already builds for the station.

Files (static/): aradar_<SITE>_<stamp>.png + aradar_<SITE>.json
  {"site", "bounds": [w, s, e, n], "frames": [{name, stamp, valid, age_s}]}
Warmer: demand-driven. A page viewing a station writes
aradar_demand.json; the loop renders every demanded 88D whose live
volume has moved on, about once a minute, and keeps the last
KEEP frames per site.
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# 250 statute miles from the radar (24 Oct; was a 150 km box). Gates
# are kept out to RANGE_KM; BOX_KM is the square that contains them.
RANGE_KM = float(os.environ.get("ARADAR_RANGE_KM", str(round(250 * 1.609344, 1))))
BOX_KM = float(os.environ.get("ARADAR_BOX_KM", str(RANGE_KM)))
# Inner tile (23 Oct): a second raster centred on each airport looking
# at this radar, INNER_KM half-width at INNER_PX, so the picture keeps
# its detail at airport zoom (the wide tile is ~250 m/px; this is ~60).
INNER_KM = float(os.environ.get("ARADAR_INNER_KM", "36"))
INNER_PX = int(os.environ.get("ARADAR_INNER_PX", "1200"))
PX = int(os.environ.get("ARADAR_PX", "1200"))
KEEP = int(os.environ.get("ARADAR_KEEP", "12"))
DEMAND_S = int(os.environ.get("ARADAR_DEMAND_S", "900"))     # keep warming 15 min after the last view
PERIOD_S = int(os.environ.get("ARADAR_PERIOD_S", "45"))
# RAW (23 Oct): every gate of the 0.5 deg sweep is drawn, nearest gate
# per pixel, hard AWIPS 5 dBZ steps, no smoothing, no speckle filter,
# no correlation-coefficient filter. The one cut is the dBZ floor:
# gates of DBZ_MIN and below are left out. ARADAR_CC_MIN=0.9 and
# ARADAR_SPECKLE_MIN=5 switch the old filters back on.
DBZ_MIN = float(os.environ.get("ARADAR_DBZ_MIN", "10"))
CC_MIN = float(os.environ.get("ARADAR_CC_MIN", "0"))          # 0 = off
SPECKLE_MIN = int(os.environ.get("ARADAR_SPECKLE_MIN", "0"))  # 0 = off
STATUS: dict = {"started": False, "busy": "", "err": None, "last": None}
_lock = threading.Lock()
_started = False


# ---------------------------------------------------------------- stations / radars

def stations() -> list:
    """[(icao, name, lat, lon)] JetBlue stations with a position."""
    from core import airports as _AP
    out = []
    for k in _AP.stations():
        a = _AP.get(k)
        if a.get("lat") is not None:
            out.append((k, a.get("name") or k, float(a["lat"]), float(a["lon"])))
    return out


def radars_for(icao: str) -> dict:
    """{"l2": (site, km), "tdwr": "T:JFK" | None, "lat", "lon"}."""
    from core import airports as _AP
    from core import nexrad_sites as _NS
    c = _AP.centre(icao)
    if not c:
        return {}
    site, km = _NS.nearest_site(c[0], c[1])
    tdwr = None
    try:
        from core import radar_l3 as L3
        for cand in L3.STATION_RADAR.get(icao.upper(), []):
            if L3.is_tdwr(cand):
                tdwr = cand
                break
    except Exception:
        pass
    return {"l2": (site, km), "tdwr": tdwr, "lat": c[0], "lon": c[1]}


# ---------------------------------------------------------------- render

def _merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _inv_merc_y(y):
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def box_bounds(rlat: float, rlon: float, half_km: float = None) -> list:
    half_km = BOX_KM if half_km is None else half_km
    dlon = half_km / (111.32 * math.cos(math.radians(rlat)))
    dy = half_km / 6371.0 / math.cos(math.radians(rlat))   # Mercator y spacing ~ 1/cos(lat)
    y0 = _merc_y(rlat)
    return [rlon - dlon, _inv_merc_y(y0 - dy), rlon + dlon, _inv_merc_y(y0 + dy)]


class IncompleteSweep(RuntimeError):
    pass


def rasterise(f, rlat: float, rlon: float):
    """metpy Level2File -> (comp dBZ float32 [PX, PX], bounds). Sweep 0,
    REF moment, nearest gate per pixel."""
    polar = polar_sweep(f)
    bounds = box_bounds(rlat, rlon)
    return grid(polar, rlat, rlon, bounds, PX), bounds


def polar_sweep(f) -> dict:
    """Sweep 0 REF as a clutter-filtered polar array, sorted by azimuth."""
    import numpy as np
    sweep = f.sweeps[0]
    az = np.array([ray[0].az_angle for ray in sweep], dtype="float32")
    ref_hdr = sweep[0][4][b"REF"][0]
    gate_first = float(ref_hdr.first_gate) * 1000.0     # metpy reports km
    gate_w = float(ref_hdr.gate_width) * 1000.0
    ngates = int(ref_hdr.num_gates)
    ref = np.full((len(sweep), ngates), np.nan, dtype="float32")
    rho = np.full((len(sweep), ngates), np.nan, dtype="float32")
    for i, ray in enumerate(sweep):
        try:
            d = ray[4][b"REF"][1]
            n = min(len(d), ngates)
            ref[i, :n] = d[:n]
        except Exception:
            continue
        try:
            rh = ray[4][b"RHO"]
            rd = rh[1]
            # RHO gates may differ from REF gates: resample by range
            rf, rw = float(rh[0].first_gate) * 1000.0, float(rh[0].gate_width) * 1000.0
            idx = np.round((gate_first + np.arange(ngates) * gate_w - rf) / rw).astype("int32")
            okr = (idx >= 0) & (idx < len(rd))
            rho[i, okr] = np.asarray(rd, dtype="float32")[idx[okr]]
        except Exception:
            pass
    ref[~np.isfinite(ref)] = np.nan
    # Clutter / non-met: the L3 rule - drop gates with correlation
    # coefficient under CC_MIN unless the echo is strong (>= 40 dBZ)
    if CC_MIN > 0:
        rho_known = np.isfinite(rho)
        clutter = rho_known & (rho < CC_MIN) & (ref < 40.0)
        ref[clutter] = np.nan
    order = np.argsort(az)
    az, ref = az[order], ref[order]
    # A live volume arrives in chunks; sweep 0 is only usable once it
    # has gone all the way round. Fewer rays, or an azimuth gap wider
    # than a few degrees, means the radar is mid-sweep: say so (the
    # caller keeps the previous frame) rather than smear the rays
    # present across the gap (seen 22 Oct as bright arcs over Boca).
    gaps = np.diff(np.concatenate([az, [az[0] + 360.0]]))
    if len(az) < 300 or gaps.max() > 4.0:
        raise IncompleteSweep(f"{len(az)} rays, largest gap {gaps.max():.1f} deg")
    ref[ref < 5.0] = np.nan          # the display floor is the viewer's slider (>= DBZ_MIN default)
    # speckle in GATE space (23 Oct; was per pixel, which let single
    # gates through on the fine inner tile): drop gates with fewer
    # than SPECKLE_MIN valid neighbours in the 3x3 az/range window,
    # wrapping in azimuth
    if SPECKLE_MIN > 0:
        try:
            from scipy import ndimage as _ndi
            valid = np.isfinite(ref).astype("uint8")
            nb = _ndi.convolve(valid, np.ones((3, 3), dtype="uint8"), mode="wrap") - valid
            ref[(valid == 1) & (nb < SPECKLE_MIN)] = np.nan
        except Exception:
            pass
    return {"az": az, "ref": ref, "gate_first": gate_first, "gate_w": gate_w, "ngates": ngates}


def grid(polar: dict, rlat: float, rlon: float, bounds: list, px: int):
    """Nearest-gate sample of the polar sweep onto a px x px Mercator
    grid covering bounds [w, s, e, n]. Row 0 = SOUTH: radar_l2.render_png
    draws with origin="lower" (found 22 Oct - the first cut built
    north-first and the image came out mirrored)."""
    import numpy as np
    az, ref = polar["az"], polar["ref"]
    gate_first, gate_w, ngates = polar["gate_first"], polar["gate_w"], polar["ngates"]
    w, s, e, n = bounds
    xs = np.linspace(w, e, px, dtype="float64")
    ys = np.array([_inv_merc_y(v) for v in np.linspace(_merc_y(s), _merc_y(n), px)])
    lon_g, lat_g = np.meshgrid(xs, ys)
    k = 111320.0
    dx = (lon_g - rlon) * k * math.cos(math.radians(rlat))
    dy = (lat_g - rlat) * k
    rng = np.hypot(dx, dy)
    brg = (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0
    gi = np.round((rng - gate_first) / gate_w).astype("int32")
    ai = np.searchsorted(az, brg.ravel()).reshape(brg.shape)
    ai = np.where(ai >= len(az), 0, ai)
    # nearest of the two bracketing radials
    prev = (ai - 1) % len(az)
    d_next = np.abs((az[ai] - brg + 180.0) % 360.0 - 180.0)
    d_prev = np.abs((az[prev] - brg + 180.0) % 360.0 - 180.0)
    ai = np.where(d_prev < d_next, prev, ai)
    d_ray = np.minimum(d_prev, d_next)
    ok = (gi >= 0) & (gi < ngates) & (d_ray <= 1.5)
    comp = np.full(brg.shape, np.nan, dtype="float32")
    comp[ok] = ref[ai[ok], gi[ok]]
    return comp


def _png(comp, dest: Path) -> None:
    """Raw write: the AWIPS 5 dBZ steps (core.radar_l2 table), one
    colour per gate, nothing smoothed. Row 0 = south, so the array is
    flipped for the image (top row = north)."""
    import numpy as np
    from PIL import Image
    try:
        from core import radar_l2 as L2
        levels, hexes = list(L2.AWIPS_LEVELS), list(L2.AWIPS_COLORS)
    except Exception:
        levels = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75]
        hexes = ["#04E9E7", "#019FF4", "#0300F4", "#02FD02", "#01C501", "#008E00", "#FDF802",
                 "#E5BC00", "#FD9500", "#FD0000", "#D40000", "#BC0000", "#F800FD", "#9854C6", "#FDFDFD"]
    cols = [tuple(int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for h in hexes]
    rgba = np.zeros(comp.shape + (4,), dtype="uint8")
    for lo, c in zip(levels, cols):
        m = comp >= lo
        rgba[m, :3] = c
        rgba[m, 3] = 255
    Image.fromarray(rgba[::-1], "RGBA").save(dest, optimize=True)


def _man_path(outdir, site):
    return Path(outdir) / f"aradar_{site.upper()}.json"


def manifest(outdir, site: str) -> dict:
    try:
        return json.loads(_man_path(outdir, site).read_text())
    except Exception:
        return {"site": site.upper(), "bounds": None, "frames": []}


def gate_bin(polar: dict, rlat: float, rlon: float, half_km: float, floor_dbz: float = 5.0) -> tuple:
    """Pack every gate >= floor_dbz inside the box as a little-endian
    Uint16 stream the viewer turns into one rectangle per gate:
        [n_az, az_0*100 ... az_{n-1}*100, then per gate: ray, gate, dbz*2]
    Returns (bytes, n_gates). Each gate is drawn in the browser as the
    polar bin it is - range gate by azimuth step - so nothing is
    resampled, blended or smoothed (23 Oct)."""
    import numpy as np
    az, ref = polar["az"], polar["ref"]
    g0, gw, ng = polar["gate_first"], polar["gate_w"], polar["ngates"]
    rr, gg = np.nonzero(np.isfinite(ref) & (ref >= floor_dbz))
    if len(rr):
        # cull to the box: gate centre within half_km of the radar in
        # both axes (flat-earth is fine for a yes/no at this scale)
        rng = g0 + gg * gw
        keep = rng <= half_km * 1000.0          # a circle of half_km around the radar
        rr, gg = rr[keep], gg[keep]
    dbz2 = np.clip(np.round(ref[rr, gg] * 2.0), 0, 65535).astype("<u2")
    head = np.concatenate([np.array([len(az)], dtype="<u2"),
                           np.round(az * 100.0).astype("<u2")])
    body = np.stack([rr.astype("<u2"), gg.astype("<u2"), dbz2], axis=1).ravel()
    return head.tobytes() + body.tobytes(), int(len(rr))


def render_live(outdir, site: str) -> tuple:
    """Fetch the live volume for an 88D and write its gate file if new.
    Returns (stamp, note)."""
    from core import radar_l2rt as RT
    outdir = Path(outdir)
    site = site.upper()
    man = manifest(outdir, site)
    try:
        vol, start_t = RT.find_live_volume(site)
    except Exception as exc:
        return None, f"chunk feed: {type(exc).__name__}: {exc}"
    stamp = start_t.strftime("%Y%m%d-%H%M%S")
    if any(fr["stamp"] == stamp and fr.get("bin") for fr in man.get("frames", [])):
        return stamp, "cached"
    STATUS["busy"] = f"{site} {stamp}"
    raw, info = RT.fetch_live_volume_bytes(site)
    from metpy.io import Level2File
    f = Level2File(io.BytesIO(raw))
    rlat = float(f.sweeps[0][0][1].lat)
    rlon = float(f.sweeps[0][0][1].lon)
    try:
        polar = polar_sweep(f)
    except IncompleteSweep as exc:
        STATUS["busy"] = ""
        return stamp, f"waiting: sweep 0 incomplete ({exc})"
    bounds = box_bounds(rlat, rlon)
    data, n = gate_bin(polar, rlat, rlon, RANGE_KM)
    name = f"aradar_{site}_{stamp}.bin"
    tmp = outdir / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, outdir / name)
    frames = [fr for fr in man.get("frames", []) if fr["stamp"] != stamp]
    frames.append({"name": name, "bin": True, "stamp": stamp, "n": n,
                   "g0": polar["gate_first"], "gw": polar["gate_w"],
                   "valid": f"{stamp[9:11]}:{stamp[11:13]}Z", "age_s": info.get("age_s")})
    frames = sorted(frames, key=lambda x: x["stamp"])[-KEEP:]
    keep = {fr["name"] for fr in frames}
    for p in list(outdir.glob(f"aradar_{site}_*.png")) + list(outdir.glob(f"aradar_{site}_*.bin")):
        if p.name not in keep:
            try:
                p.unlink()
            except Exception:
                pass
    man = {"site": site, "bounds": bounds, "radar": [rlat, rlon],
           "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "frames": frames}
    mp = _man_path(outdir, site)
    tmpm = mp.with_suffix(".tmp")
    tmpm.write_text(json.dumps(man))
    os.replace(tmpm, mp)
    STATUS["busy"] = ""
    STATUS["last"] = time.time()
    return stamp, f"{n} gates ({info.get('n_used')} chunks, {info.get('age_s')}s old)"


# ---------------------------------------------------------------- demand + warmer

def _demand_path(outdir):
    return Path(outdir) / "aradar_demand.json"


def request(outdir, site: str, icao: str = None, lat: float = None, lon: float = None) -> None:
    """A page is looking at this 88D (from airport icao at lat/lon):
    keep it warm, and an inner tile centred on that airport."""
    p = _demand_path(outdir)
    try:
        d = json.loads(p.read_text())
    except Exception:
        d = {}
    cur = d.get(site.upper())
    cur = cur if isinstance(cur, dict) else {"inner": {}}
    cur["t"] = time.time()
    if icao and lat is not None:
        cur.setdefault("inner", {})[icao.upper()] = [float(lat), float(lon), time.time()]
    d[site.upper()] = cur
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, p)
    except Exception:
        pass


def demanded(outdir) -> list:
    try:
        d = json.loads(_demand_path(outdir).read_text())
    except Exception:
        return []
    now = time.time()
    return [s for s, v in d.items() if now - _dt(v) < DEMAND_S]


def _dt(v) -> float:
    return float(v["t"]) if isinstance(v, dict) else float(v)


def inner_centres(outdir, site: str) -> dict:
    """{ICAO: (lat, lon)} for airports that asked for this radar lately."""
    try:
        v = json.loads(_demand_path(outdir).read_text()).get(site.upper())
    except Exception:
        return {}
    if not isinstance(v, dict):
        return {}
    now = time.time()
    return {k: (a[0], a[1]) for k, a in (v.get("inner") or {}).items() if now - a[2] < DEMAND_S}


def _log(outdir, msg):
    try:
        p = Path(outdir) / "aradar.log"
        with p.open("a") as fh:
            fh.write(f"{datetime.now(timezone.utc):%m-%d %H:%MZ} {msg}\n")
    except Exception:
        pass


def log_tail(outdir, n=6) -> list:
    try:
        return (Path(outdir) / "aradar.log").read_text().splitlines()[-n:]
    except Exception:
        return []


def _loop(outdir):
    time.sleep(8)
    _log(outdir, f"airport radar warmer started (box {BOX_KM:.0f} km, {PX}px, keep {KEEP})")
    while True:
        try:
            for site in demanded(outdir):
                try:
                    stamp, note = render_live(outdir, site)
                    if note != "cached" and not note.startswith("waiting"):
                        _log(outdir, f"{site} {stamp}: {note}")
                    STATUS["err"] = None
                except Exception as exc:
                    STATUS["err"] = f"{site}: {type(exc).__name__}: {exc}"
                    STATUS["busy"] = ""
                    _log(outdir, f"{site} FAILED: {STATUS['err'][:160]}")
        except Exception as exc:
            _log(outdir, f"loop error: {type(exc).__name__}: {exc}")
        time.sleep(PERIOD_S)


def ensure_warmer(outdir) -> bool:
    global _started
    with _lock:
        if _started:
            return True
        threading.Thread(target=_loop, args=(outdir,), daemon=True, name="airport-radar").start()
        _started = True
        STATUS["started"] = True
    return True
