"""Single-site NEXRAD Level II: base reflectivity and echo tops.

ONE SITE, ONE TEXTURE. A radar's own sweep is already a picture —
720 azimuths by 1,832 gates of 250 m for the 0.5 degree tilt — so
rendering it is a polar-to-cartesian lookup, not a gridding problem.
+-230 km at 250 m is 1,841 x 1,841 pixels: one texture per frame,
under the 4096 cap. That is what makes a one-hour loop tractable and
what the earlier three-site mosaic never was.

DECODER: MetPy's Level2File. Pure Python, super-res aware, a few
seconds and a few hundred MB per volume. No pyart.

SOURCE: the AWS real-time chunk feed (core/radar_l2rt.py), so the
0.5 degree tilt is available within a minute of being scanned. The
archive bucket only has a volume after it completes, by which time
that tilt is five minutes old — too old to draw next to live aircraft.

PRODUCTS
  BR  base reflectivity — the lowest tilt with REF, through the MRMS
      palette and the same smooth-then-quantise recipe, so the two
      radar products look like one family.
  ET  echo tops — every tilt's beam height from the 4/3-Earth formula,
      then per column the highest tilt still at or above ET_DBZ. A
      height raster; the FL tags come from the same field.

FRAMES are written as WebP with a manifest per site; one hour kept.
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from pathlib import Path

SITES = {
    # lat, lon, ground elevation m (from the volume header when it
    # arrives; these are fallbacks for the bounds)
    "KOKX": (40.8655, -72.8639, 26.0),
    "KDIX": (39.9470, -74.4108, 45.0),
}
HALF_KM = float(os.environ.get("L2_HALF_KM", "230"))
RES_M = float(os.environ.get("L2_RES_M", "250"))
ET_RES_M = float(os.environ.get("L2_ET_RES_M", "500"))
ET_DBZ = float(os.environ.get("L2_ET_DBZ", "18"))
ET_MASK_KM = float(os.environ.get("L2_ET_MASK_KM", "8"))
KEEP_S = float(os.environ.get("L2_KEEP_S", "3600"))
SLEEP_S = float(os.environ.get("L2_SLEEP_S", "60"))
DELAY_S = float(os.environ.get("L2_DELAY_S", "150"))
BR_SMOOTH_PX = float(os.environ.get("L2_BR_SMOOTH", "1.2"))
# Parse + render run in a child process. A hang or an OOM kills the
# child and is logged; the service keeps going. 240 s is generous —
# the sample volume takes 8 s here.
CHILD_TIMEOUT_S = float(os.environ.get("L2_CHILD_TIMEOUT_S", "240"))
LOCK_WAIT_S = float(os.environ.get("L2_LOCK_WAIT_S", "180"))
# Total time allowed for one pass's chunk downloads. 58 chunks took
# 9 s once and never finished another time; this makes the difference
# a logged number instead of silence.
DL_BUDGET_S = float(os.environ.get("L2_DL_BUDGET_S", "120"))
RE_KM = 8494.0          # 4/3 Earth radius, the standard refraction model
FT_PER_KM = 3280.84

_LOCK = threading.Lock()
_started = False


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def decode(raw: bytes):
    from metpy.io import Level2File

    return Level2File(io.BytesIO(raw))


def site_position(f, default):
    vc = f.sweeps[0][0][1]
    lat, lon = float(vc.lat), float(vc.lon)
    alt = float(getattr(vc, "site_amsl", 0) or 0) + float(
        getattr(vc, "feedhorn_agl", 0) or 0)
    return (lat, lon, alt if alt else default[2])


def sweeps_with_ref(f):
    """[(el_deg, az[n], rng_km[m], ref[n, m])] for every tilt that
    carries REF, in file order. Ranges are gate CENTRES in km."""
    import numpy as np

    out = []
    for sw in f.sweeps:
        m = sw[0][4]
        if b"REF" not in m:
            continue
        rh = m[b"REF"][0]
        n = rh.num_gates
        rng = rh.first_gate + rh.gate_width * np.arange(n)
        az = np.array([r[0].az_angle for r in sw], dtype="float32")
        ref = np.full((len(sw), n), np.nan, dtype="float32")
        for i, r in enumerate(sw):
            d = r[4][b"REF"][1]
            k = min(n, len(d))
            ref[i, :k] = d[:k]
        out.append((float(sw[0][0].el_angle), az, rng.astype("float32"), ref))
    return out


def volume_stamp(f) -> str:
    """YYYYMMDD-HHMMSS of the volume start, from the first radial."""
    from datetime import datetime, timedelta, timezone

    h = f.sweeps[0][0][0]
    t = (datetime(1970, 1, 1, tzinfo=timezone.utc)
         + timedelta(days=int(h.date) - 1, milliseconds=int(h.time_ms)))
    return t.strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Polar -> cartesian
# ---------------------------------------------------------------------------
def _grid(res_m: float):
    """Cartesian grid: km east / north of the site, rows N->S."""
    import numpy as np

    n = int(round(2 * HALF_KM * 1000.0 / res_m))
    axis = (np.arange(n) + 0.5) * (res_m / 1000.0) - HALF_KM
    x = axis[None, :]
    y = -axis[:, None]
    return n, x, y


def polar_to_cart(az, rng_km, field, res_m: float):
    """Nearest-neighbour sample of a polar field onto the grid.
    Cells beyond the last gate are NaN."""
    import numpy as np

    n, x, y = _grid(res_m)
    r = np.hypot(x, y)
    a = (np.degrees(np.arctan2(x, y)) + 360.0) % 360.0
    order = np.argsort(az)
    az_s = az[order]
    # nearest radial: searchsorted on the sorted azimuths, with wrap
    j = np.searchsorted(az_s, a) % len(az_s)
    jm = (j - 1) % len(az_s)
    pick = np.where(np.abs(((az_s[j] - a) + 180) % 360 - 180)
                    <= np.abs(((az_s[jm] - a) + 180) % 360 - 180), j, jm)
    ridx = order[pick]
    gw = float(rng_km[1] - rng_km[0])
    g = np.rint((r - rng_km[0]) / gw).astype("int64")
    valid = (g >= 0) & (g < len(rng_km))
    g = np.clip(g, 0, len(rng_km) - 1)
    out = field[ridx, g]
    out = np.where(valid, out, np.nan).astype("float32")
    return out


def bounds(site_lat, site_lon):
    """[W, S, E, N] of the grid in degrees."""
    dlat = HALF_KM / 111.32
    dlon = HALF_KM / (111.32 * math.cos(math.radians(site_lat)))
    return [site_lon - dlon, site_lat - dlat, site_lon + dlon, site_lat + dlat]


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
def base_reflectivity(f):
    """Lowest tilt with REF, on the 250 m grid, as dBZ (NaN = none)."""
    tilts = sweeps_with_ref(f)
    if not tilts:
        return None
    el, az, rng, ref = min(tilts, key=lambda t: t[0])
    return polar_to_cart(az, rng, ref, RES_M)


def echo_tops(f, site_alt_m: float):
    """Height (km MSL) of the highest tilt at or above ET_DBZ, on the
    ET grid. NaN where no tilt reaches the threshold.

    Each tilt is resampled onto its own polar lookup and the beam
    height added; the column maximum across tilts is the top. Where
    the TOP tilt is still above threshold the true top is higher than
    reported — the same limitation every echo-top product has."""
    import numpy as np

    tilts = sweeps_with_ref(f)
    if not tilts:
        return None
    n, x, y = _grid(ET_RES_M)
    r_km = np.hypot(x, y).astype("float32")
    top = np.full((n, n), np.nan, dtype="float32")
    for el, az, rng, ref in tilts:
        z = polar_to_cart(az, rng, ref, ET_RES_M)
        s = math.sin(math.radians(el))
        h = (np.sqrt(r_km * r_km + RE_KM * RE_KM + 2.0 * r_km * RE_KM * s)
             - RE_KM + site_alt_m / 1000.0)
        hit = np.isfinite(z) & (z >= ET_DBZ)
        top = np.where(hit & (~np.isfinite(top) | (h > top)), h, top)
    # CLEAN-UP. Tilts are discrete, so the raw field has quantised
    # rings near the radar and radial speckle where the coarse upper
    # tilts catch anvil. A 3x3 median smooths the rings and removes
    # single-pixel speckle without moving a real top; inside ET_MASK_KM
    # the geometry is all cone-of-silence and is masked outright.
    from scipy import ndimage as ndi

    filled = np.nan_to_num(top, nan=0.0)
    med = ndi.median_filter(filled, size=3, mode="nearest")
    top = np.where(med > 0, med, np.nan).astype("float32")
    top[r_km < ET_MASK_KM] = np.nan
    return top


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# THE MRMS PALETTE, COPIED. The same 15 dBZ thresholds and RGBA table
# core/mrms.py uses, so the two radar products look like one family.
# Copied rather than imported because the child interpreter that
# renders Level II could import core.l2 but not core.mrms on the
# deployed box — an environment quirk not worth a dependency. If the
# mosaic palette ever changes, change this too.
LEVELS = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75]
_PALETTE = [
    (0, 0, 0, 0),
    (158, 200, 200, 51), (116, 176, 190, 68), (78, 148, 180, 85),
    (2, 253, 2, 110), (1, 197, 1, 133), (0, 142, 0, 153),
    (253, 248, 2, 170), (229, 188, 0, 187), (253, 149, 0, 204),
    (253, 0, 0, 224), (212, 0, 0, 235), (188, 0, 0, 241),
    (248, 0, 253, 246), (152, 84, 198, 246), (253, 253, 253, 246),
]


def palette():
    import numpy as np

    return np.array(_PALETTE, dtype="uint8")


def render_br(field, outpath: Path):
    """dBZ grid -> WebP through the MRMS palette, smooth then quantise."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi

    v = field.astype("float32")
    good = np.isfinite(v) & (v >= LEVELS[0])
    if BR_SMOOTH_PX > 0:
        fld = np.where(good, v, 0.0)
        cov = good.astype("float32")
        fb = ndi.gaussian_filter(fld, BR_SMOOTH_PX, mode="nearest")
        cb = ndi.gaussian_filter(cov, BR_SMOOTH_PX, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            v = np.where(cb > 0.05, fb / np.maximum(cb, 1e-6), np.nan)
        good = np.isfinite(v) & (v >= LEVELS[0])
    idx = np.zeros(v.shape, dtype="uint8")
    lv = np.asarray(LEVELS, dtype="float32")
    idx[good] = np.clip(np.searchsorted(lv, v[good], side="right"), 1,
                        len(lv)).astype("uint8")
    lut = palette()
    Image.fromarray(lut[idx], mode="RGBA").save(outpath, "WEBP", quality=88,
                                                method=0)
    return int(good.sum())


# Echo-top palette: kft, from a cool low top to a hot high one.
ET_KFT = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]
ET_RGB = [(120, 170, 220), (80, 140, 220), (40, 110, 210), (30, 160, 120),
          (60, 190, 60), (170, 210, 40), (240, 220, 30), (250, 170, 30),
          (240, 110, 30), (220, 50, 40), (190, 30, 90), (150, 30, 150)]


def render_et(top_km, outpath: Path):
    import numpy as np
    from PIL import Image

    kft = top_km * FT_PER_KM / 1000.0
    good = np.isfinite(kft) & (kft >= ET_KFT[0])
    idx = np.zeros(kft.shape, dtype="uint8")
    lv = np.asarray(ET_KFT, dtype="float32")
    idx[good] = np.clip(np.searchsorted(lv, kft[good], side="right"), 1,
                        len(lv)).astype("uint8")
    lut = np.zeros((len(ET_KFT) + 1, 4), dtype="uint8")
    for i, rgb in enumerate(ET_RGB, start=1):
        lut[i] = (*rgb, 215)
    Image.fromarray(lut[idx], mode="RGBA").save(outpath, "WEBP", quality=88,
                                                method=0)
    return int(good.sum())


def et_tags(top_km, site_lat, site_lon, min_fl: int = 380):
    """COSPA-style tags for the ET grid: local maxima above min_fl."""
    import numpy as np
    from scipy import ndimage as ndi

    kft = np.nan_to_num(top_km * FT_PER_KM / 1000.0, nan=0.0)
    thr = min_fl / 10.0
    high = kft >= thr
    if not high.any():
        return []
    win = max(3, int(25000 / ET_RES_M) | 1)
    mx = ndi.maximum_filter(kft, size=win, mode="nearest")
    n = kft.shape[0]
    W, S, E, N = bounds(site_lat, site_lon)
    out = []
    for r, c in np.argwhere(high & (kft == mx)):
        out.append({"lat": round(N - (r + 0.5) * (N - S) / n, 4),
                    "lon": round(W + (c + 0.5) * (E - W) / n, 4),
                    "fl": int(round(kft[r, c] * 10 / 10.0) * 10)})
    out.sort(key=lambda t: -t["fl"])
    kept = []
    for t in out:
        k = math.cos(math.radians(t["lat"]))
        if all(math.hypot((t["lon"] - u["lon"]) * 60 * k,
                          (t["lat"] - u["lat"]) * 60) >= 12 for u in kept):
            kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# Manifest and warmer
# ---------------------------------------------------------------------------
def _manifest_path(outdir, site):
    return Path(outdir) / f"l2_{site}.json"


def load_manifest(outdir, site) -> dict:
    p = _manifest_path(outdir, site)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_manifest(outdir, site, man):
    _manifest_path(outdir, site).write_text(json.dumps(man))


def build_site(outdir, site: str, f, complete: bool, log) -> str:
    """Render one PARSED volume: BR (always) and ET (when the volume
    is complete). Returns a one-line note. `f` is a MetPy Level2File;
    parsing is the caller's job so it happens exactly once."""
    outdir = Path(outdir)
    t0 = time.time()
    stamp = volume_stamp(f)
    lat, lon, alt = site_position(f, SITES[site])
    man = load_manifest(outdir, site)
    man.setdefault("site", site)
    man["bounds"] = bounds(lat, lon)
    man["res_m"] = RES_M
    frames = {fr["stamp"]: fr for fr in man.get("frames", [])}
    fr = frames.get(stamp, {"stamp": stamp})
    notes = []
    if "br" not in fr:
        field = base_reflectivity(f)
        if field is not None:
            name = f"l2_{site}_BR_{stamp}.webp"
            n = render_br(field, outdir / name)
            fr["br"] = name
            notes.append(f"BR {n} cells")
            del field
    if complete and "et" not in fr:
        top = echo_tops(f, alt)
        if top is not None:
            name = f"l2_{site}_ET_{stamp}.webp"
            n = render_et(top, outdir / name)
            fr["et"] = name
            fr["tags"] = et_tags(top, lat, lon)
            notes.append(f"ET {n} cells, {len(fr['tags'])} tag(s)")
            del top
    fr["complete"] = bool(complete)
    frames[stamp] = fr
    # Retention: one hour of volumes.
    cutoff = time.strftime("%Y%m%d-%H%M%S",
                           time.gmtime(time.time() - KEEP_S))
    keep = sorted(frames.values(), key=lambda x: x["stamp"])
    for old in [x for x in keep if x["stamp"] < cutoff]:
        for k in ("br", "et"):
            if old.get(k):
                try:
                    (outdir / old[k]).unlink()
                except OSError:
                    pass
    man["frames"] = [x for x in keep if x["stamp"] >= cutoff]
    _write_manifest(outdir, site, man)
    return (f"{site} {stamp}: {', '.join(notes) or 'nothing new'} "
            f"in {time.time() - t0:.0f}s ({len(man['frames'])} kept)")


def parse_volume(parts, log=None, max_drop: int = 3):
    """Level2File from concatenated chunks. A chunk is a whole S3
    object, so every chunk but possibly the newest is complete; try
    the full set, then drop up to `max_drop` trailing chunks. NOT one
    attempt per prefix — a full parse is seconds each, and a hundred
    of them was half an hour of silence."""
    from metpy.io import Level2File

    last = None
    for drop in range(0, min(max_drop, len(parts) - 1) + 1):
        upto = len(parts) - drop
        t0 = time.time()
        try:
            f = Level2File(io.BytesIO(b"".join(parts[:upto])))
            if getattr(f, "sweeps", None):
                if log:
                    log(f"  parsed {upto}/{len(parts)} chunks, "
                        f"{len(f.sweeps)} sweep(s), {time.time() - t0:.0f}s")
                return f, upto
            last = RuntimeError("no sweeps")
        except Exception as exc:
            last = exc
            if log:
                log(f"  parse of {upto} chunks failed in {time.time() - t0:.0f}s: "
                    f"{type(exc).__name__}: {str(exc)[:80]}")
    raise RuntimeError(f"no parseable prefix within {max_drop} chunks "
                       f"(last: {type(last).__name__}: {last})")


def _child_render(outdir, site, chunk_path, complete, result_path):
    """Runs in a CHILD PROCESS: parse the concatenated chunks, render,
    write the manifest, and leave a one-line note in result_path. A
    hang or an out-of-memory kill takes the child, not the service."""
    import traceback

    try:
        parts = _split_chunks(Path(chunk_path).read_bytes())
        notes = []
        f, upto = parse_volume(parts, notes.append)
        note = build_site(outdir, site, f, complete, None)
        Path(result_path).write_text("\n".join(notes + [note]))
    except Exception:
        import sys as _s

        try:
            import core as _c

            _pkg = f"core at {getattr(_c, '__file__', None)} path={list(getattr(_c, '__path__', []))}"
        except Exception as _ce:
            _pkg = f"core not importable: {_ce}"
        Path(result_path).write_text(
            "FAILED: " + traceback.format_exc()[-600:]
            + f"\n  child sys.path[:4]={_s.path[:4]}\n  {_pkg}")


def _join_chunks(parts):
    """Length-prefixed concatenation so the child can split it back."""
    import struct

    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def _split_chunks(blob):
    import struct

    out, i = [], 0
    while i + 4 <= len(blob):
        n = struct.unpack(">I", blob[i:i + 4])[0]
        out.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    return out


def _child_main(argv):
    """Entry point for the child interpreter: python -c 'from core.l2
    import _child_main; _child_main(sys.argv)' outdir site chunks
    complete result."""
    outdir, site, chunk_path, complete, result_path = argv[1:6]
    _child_render(outdir, site, chunk_path, complete == "1", result_path)


def render_isolated(outdir, site, parts, complete, log,
                    timeout_s: float = None) -> str:
    """Parse and render in a FRESH INTERPRETER with a hard timeout.
    Returns the child's note, or a TIMED OUT / KILLED line.

    A plain subprocess, not multiprocessing: spawn re-imports the
    parent's __main__, which under Streamlit is the server's entry
    point, and fork copies a process full of threads and locks.
    A new interpreter with the repo on its path has neither problem,
    and its memory is returned to the OS the moment it exits."""
    import subprocess
    import sys
    import tempfile

    timeout_s = timeout_s or CHILD_TIMEOUT_S
    outdir = str(outdir)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".chunks",
                                     dir=outdir) as fh:
        fh.write(_join_chunks(parts))
        chunk_path = fh.name
    result_path = chunk_path + ".result"
    root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-c",
           "import sys; from core.l2 import _child_main; _child_main(sys.argv)",
           outdir, site, chunk_path, "1" if complete else "0", result_path]
    t0 = time.time()
    try:
        try:
            proc = subprocess.run(cmd, cwd=root, env=env, timeout=timeout_s,
                                  capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return (f"{site}: parse/render TIMED OUT after {timeout_s:.0f}s "
                    f"and was killed")
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            return (f"{site}: child exited with code {proc.returncode} after "
                    f"{time.time() - t0:.0f}s"
                    + (" (killed \u2014 almost certainly out of memory)"
                       if proc.returncode < 0 else "")
                    + (" | " + " / ".join(tail) if tail else ""))
        p = Path(result_path)
        return p.read_text() if p.exists() else f"{site}: child left no result"
    finally:
        for pth in (chunk_path, result_path):
            try:
                os.unlink(pth)
            except OSError:
                pass


def _download(keys, RT, log, workers: int = 6, budget_s: float = None):
    """Fetch chunk objects in parallel with a total time budget.
    Returns ({key: bytes}, n_failed). Logs progress every ten chunks
    so a slow feed shows its rate instead of going quiet."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import requests

    budget_s = budget_s or DL_BUDGET_S
    out, failed, t0 = {}, 0, time.time()
    if not keys:
        return out, 0
    sess = requests.Session()
    sess.headers.update(RT._HEADERS)

    def _get(k):
        r = sess.get(f"{RT.BUCKET}/{k}", timeout=(10, 30))
        r.raise_for_status()
        return k, r.content

    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(_get, k): k for k in keys}
    done = 0
    try:
        for fut in as_completed(futs, timeout=budget_s):
            try:
                k, b = fut.result()
                out[k] = b
            except Exception as exc:
                failed += 1
                log(f"  chunk {futs[fut].rsplit('/', 1)[-1]} failed: "
                    f"{type(exc).__name__}")
            done += 1
            if done % 10 == 0:
                log(f"  {done}/{len(keys)} chunks, {time.time() - t0:.0f}s")
    except TimeoutError:
        log(f"  download budget of {budget_s:.0f}s exhausted with "
            f"{done}/{len(keys)} chunks \u2014 continuing with what arrived")
        failed += len(keys) - done
    finally:
        # Do NOT wait for stalled workers: that is exactly the hang
        # this exists to avoid. Cancel the queue and let the running
        # ones die with their sockets.
        ex.shutdown(wait=False, cancel_futures=True)
    return out, failed


def _state(outdir, step: str, **extra):
    """Write the daemon's current step to l2_state.json. The page shows
    it with its age, so a silent thread still says where it is."""
    try:
        d = {"step": step, "since": time.time(), **extra}
        (Path(outdir) / "l2_state.json").write_text(json.dumps(d))
    except OSError:
        pass


def _log(outdir, msg):
    try:
        with open(Path(outdir) / "l2_warmer.log", "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%SZ ", time.gmtime())
                     + msg + "\n")
    except OSError:
        pass


def _daemon(outdir, sites):
    _state(outdir, f"startup delay {DELAY_S:.0f}s")
    time.sleep(DELAY_S)
    _log(outdir, f"Level II warmer started for {sites}")
    _state(outdir, "idle")
    # Per-site chunk cache: {vol: {key: bytes}}. Chunks land every
    # 10-20 s and a volume is 10-15 MB; refetching all of it every
    # minute would be most of the bandwidth for nothing. Only chunks
    # not yet held are downloaded; older volumes are dropped.
    cache = {site: {} for site in sites}
    seen = {}
    while True:
        for site in sites:
            try:
                from core import radar_l2rt as RT

                t_pass = time.time()
                _state(outdir, f"{site}: finding live volume")
                vol, _t0 = RT.find_live_volume(site)
                _state(outdir, f"{site}: listing vol {vol}")
                keys, _ = RT._list(f"{site}/{vol}/")
                keys = sorted(keys)
                if not keys:
                    continue
                newest = keys[-1].rsplit("/", 1)[-1]
                # Chunk names end -S (start), -I (intermediate), -E
                # (end). An -E chunk means the volume is complete and
                # every tilt is present: that is when echo tops build.
                complete = newest.endswith("-E")
                if seen.get(site) == (vol, len(keys), complete):
                    continue
                held = cache[site].setdefault(vol, {})
                new_keys = [k for k in keys if k not in held]
                _log(outdir, f"{site} vol {vol}: {len(keys)} chunks "
                             f"({len(new_keys)} new), newest {newest[:15]}"
                             f"{', COMPLETE' if complete else ''}, found in "
                             f"{time.time() - t_pass:.0f}s")
                t_dl = time.time()
                _state(outdir, f"{site}: downloading {len(new_keys)} chunks")
                got, failed = _download(new_keys, RT, lambda m: _log(outdir, m))
                held.update(got)
                for old in [v for v in cache[site] if v != vol]:
                    del cache[site][old]
                # Chunks are ordered; use the longest complete prefix
                # we hold, so one missing object costs the tail, not
                # the pass. A missing chunk means the volume is not
                # complete for ET purposes either.
                keys_have = []
                for k in keys:
                    if k in held:
                        keys_have.append(k)
                    else:
                        complete = False
                        break
                parts = [held[k] for k in keys_have]
                _log(outdir, f"  downloaded {len(got)} of {len(new_keys)} new "
                             f"chunk(s){f', {failed} failed' if failed else ''}, "
                             f"using {len(parts)}/{len(keys)}, "
                             f"{sum(len(p) for p in parts) // 1024} KB, "
                             f"{time.time() - t_dl:.0f}s")
                if not parts:
                    continue
                try:
                    from core.warmlock import held_by, holding
                except Exception:
                    import contextlib

                    held_by = lambda: (None, 0.0)  # noqa: E731
                    holding = lambda name, timeout=None: contextlib.nullcontext(True)  # noqa: E731
                _h, _hs = held_by()
                _state(outdir, f"{site}: waiting for heavy lock",
                       holder=_h, holder_s=round(_hs))
                if _h:
                    _log(outdir, f"  heavy lock held by {_h} for {_hs:.0f}s; "
                                 f"waiting up to {LOCK_WAIT_S:.0f}s")
                t_lock = time.time()
                with holding("Level II", timeout=LOCK_WAIT_S) as got:
                    if not got:
                        _h, _hs = held_by()
                        _log(outdir, f"  heavy lock not free after "
                                     f"{LOCK_WAIT_S:.0f}s (held by {_h} for "
                                     f"{_hs:.0f}s) \u2014 skipping this pass")
                        _state(outdir, "idle")
                        continue
                    if time.time() - t_lock > 5:
                        _log(outdir, f"  waited {time.time() - t_lock:.0f}s "
                                     f"for the heavy lock")
                    _log(outdir, f"  parsing {len(parts)} chunks in a child "
                                 f"process (timeout {CHILD_TIMEOUT_S:.0f}s)")
                    _state(outdir, f"{site}: parsing + rendering in child",
                           chunks=len(parts))
                    note = render_isolated(outdir, site, parts, complete, _log)
                _state(outdir, "idle")
                for line in note.splitlines():
                    _log(outdir, line if line.startswith(site) else "  " + line)
                if "TIMED OUT" not in note and "FAILED" not in note \
                        and "exited" not in note:
                    seen[site] = (vol, len(keys), complete)
                _log(outdir, f"  pass {time.time() - t_pass:.0f}s [vol {vol}, "
                             f"{len(keys)} chunks{', complete' if complete else ''}]")
            except Exception as exc:
                _log(outdir, f"{site} FAILED: {type(exc).__name__}: {exc}")
        time.sleep(SLEEP_S)


def ensure_l2_warmer(outdir, sites=("KOKX",)) -> str:
    if os.environ.get("L2_WARMER", "on").lower() == "off":
        return "DISABLED by L2_WARMER=off in the environment"
    global _started
    with _LOCK:
        if _started:
            return "already running"
        threading.Thread(target=_daemon, args=(outdir, tuple(sites)),
                         daemon=True, name="l2-warmer").start()
        _started = True
        return f"started for {', '.join(sites)}"
