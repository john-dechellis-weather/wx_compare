"""NOAA model layers for the Weather Mapping page.

Target path: core/model_tiles.py

Forecast model fields drawn on the same MapLibre map as the tomorrow.io
tiles: each frame is one Web-Mercator WebP of the model's own domain,
built by a warmer from the same GRIB fetch/decode the Hi-Res CAMs page
uses (core.hrrr_cam.fetch_and_decode) and the same palettes
(core.cam_fast.PALETTES), resampled onto Mercator rows so the image
sits exactly on the map.

Demand-driven like the tomorrow.io layers: the viewer reports which
model layers are on and where the time slider is (core.tio_map's
bridge; model layer keys are "<model>:<code>", e.g. "refs:PMMN"), and
this warmer builds the frames for those valid times - the slider's
hour first, its neighbours next, then the next PREFETCH_H hours of
every active layer. A frame is keyed by VALID time; for each valid
time the newest cycle that has published that hour is used, so the
slider always shows the freshest run.

Models come in one at a time: REFS first (MDL_MODELS env lists the
enabled ones). RRFS, HRRR, NAM nest and GFS plug into the same table.

Files (static/): mdl_<model>_<code>_<cycleYYYYMMDDHH>_f<ff>.webp and
mdl_manifest.json {model: {code: {valid: entry}}}; mdl_warmer.log.
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- layers

#: model key -> list of (code, source model in core.hrrr_cam.MODELS,
#: product in that source, label). Codes carry no underscores: the
#: viewer splits layer ids on "_".
LAYERS = {
    "refs": [
        ("PMMN", "refs_pmmn", "REFC", "REFS PMMN composite reflectivity"),
        ("PREFC40", "refs_prob", "PROB_REFC40", "REFS P(REFC ≥ 40 dBZ)"),
        ("PREFC50", "refs_prob", "PROB_REFC50", "REFS P(REFC ≥ 50 dBZ)"),
        ("PCIG1000", "refs_prob", "PROB_CIG1000", "REFS P(ceiling < 1000 ft)"),
        ("PCIG500", "refs_prob", "PROB_CIG500", "REFS P(ceiling < 500 ft)"),
        ("PVIS1", "refs_prob", "PROB_VIS1", "REFS P(visibility < 1 sm)"),
        ("PVIS3", "refs_prob", "PROB_VIS3", "REFS P(visibility < 3 sm)"),
        ("PRETOP35", "refs_prob", "PROB_RETOP35", "REFS P(echo tops > FL350)"),
    ],
    "rrfs": [
        ("REFC", "rrfs", "REFC", "RRFS composite reflectivity"),
        ("CEIL", "rrfs", "CEIL", "RRFS ceiling"),
        ("VIS", "rrfs", "VIS", "RRFS visibility"),
        ("GUST", "rrfs", "GUST", "RRFS 10 m gust"),
        ("LTNG", "rrfs", "LTNG", "RRFS lightning threat"),
    ],
    "gfs": [
        ("REFC", "gfs", "REFC", "GFS composite reflectivity"),
        ("CEIL", "gfs", "CEIL", "GFS ceiling"),
        ("VIS", "gfs", "VIS", "GFS visibility"),
        ("GUST", "gfs", "GUST", "GFS 10 m gust"),
        ("CAPE", "gfs", "CAPE", "GFS surface CAPE"),
        ("CIN", "gfs", "CIN", "GFS surface CIN"),
    ],
    "hrrr": [
        ("REFC", "hrrr", "REFC", "HRRR composite reflectivity"),
        ("CEIL", "hrrr", "CEIL", "HRRR ceiling"),
        ("VIS", "hrrr", "VIS", "HRRR visibility"),
        ("GUST", "hrrr", "GUST", "HRRR 10 m gust"),
        ("LTNG", "hrrr", "LTNG", "HRRR lightning threat"),
    ],
}
MODEL_LABEL = {"refs": "REFS ensemble", "rrfs": "RRFS", "hrrr": "HRRR",
               "nam_nest": "NAM nest", "gfs": "GFS"}
# REFS proved on 1 Oct (first real frames + readout); RRFS and HRRR
# verified the same day, so all three are on by default.
MODELS_ON = [m.strip() for m in os.environ.get("MDL_MODELS", "refs,rrfs,hrrr,gfs").split(",")
             if m.strip() and m.strip() in LAYERS]

PREFETCH_H = int(os.environ.get("MDL_PREFETCH_H", "6"))
ACTIVE_MIN = int(os.environ.get("MDL_ACTIVE_MIN", "120"))
REFRESH_MIN = int(os.environ.get("MDL_REFRESH_MIN", "60"))   # look for a newer cycle
WIDTH_PX = int(os.environ.get("MDL_WIDTH_PX", "1400"))
KEEP_PAST_H = 3
MAX_PER_TICK = 2

STATUS: dict = {"started": False, "last": None, "err": None, "busy": ""}
_lock = threading.Lock()
_started = False
_probe_cache: dict = {}


def field_keys() -> list:
    return [f"{m}:{c}" for m in MODELS_ON for (c, _s, _p, _l) in LAYERS[m]]


def field_label(key: str) -> str:
    m, c = key.split(":", 1)
    for code, _s, _p, lab in LAYERS.get(m, []):
        if code == c:
            return lab
    return key


def _spec(key: str):
    m, c = key.split(":", 1)
    for code, src, prod, _lab in LAYERS[m]:
        if code == c:
            return src, prod
    raise KeyError(key)


# ---------------------------------------------------------------- files

def _log(outdir, msg):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%MZ} {msg}"
    try:
        p = Path(outdir) / "mdl_warmer.log"
        with open(p, "a") as f:
            f.write(line + "\n")
        if p.stat().st_size > 200_000:
            p.write_text("\n".join(p.read_text().splitlines()[-400:]) + "\n")
    except Exception:
        pass


def log_tail(outdir, n=6) -> list:
    try:
        return (Path(outdir) / "mdl_warmer.log").read_text().splitlines()[-n:]
    except Exception:
        return []


def _man_path(outdir):
    return Path(outdir) / "mdl_manifest.json"


def manifest(outdir) -> dict:
    try:
        return json.loads(_man_path(outdir).read_text())
    except Exception:
        return {}


def _save_manifest(outdir, man):
    p = _man_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(man))
    os.replace(tmp, p)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


# ---------------------------------------------------------------- cycles

def _idx_exists(src: str, cyc: datetime, fhr: int) -> bool:
    """Does this run have this hour published? (HEAD on the .idx,
    cached 10 min; the hrrr_cam candidate list is honoured.)"""
    import requests
    from core.hrrr_cam import MODELS, _HEADERS

    k = (src, cyc.strftime("%Y%m%d%H"), fhr)
    hit = _probe_cache.get(k)
    if hit and time.time() - hit[1] < 600:
        return hit[0]
    cfg = MODELS[src]
    cands = ([cfg["_idx_resolved"]] if cfg.get("_idx_resolved")
             else (cfg.get("probe_candidates") or cfg.get("idx_candidates") or [cfg["idx"]]))
    ok = False
    for tmpl in cands:
        url = tmpl.format(ymd=cyc.strftime("%Y%m%d"), cc=cyc.hour, ff=fhr)
        try:
            r = requests.head(url, headers=_HEADERS, timeout=8)
            if r.status_code in (403, 405):
                r = requests.get(url, headers={**_HEADERS, "Range": "bytes=0-0"}, timeout=8)
            if r.status_code in (200, 206):
                ok = True
                if tmpl.endswith(".idx"):
                    cfg["_idx_resolved"] = tmpl
                break
        except Exception:
            continue
    _probe_cache[k] = (ok, time.time())
    return ok


def cycle_for_valid(src: str, valid: datetime):
    """(cycle, fhr) of the newest run that has published this valid
    time, or None. Walks back through the model's cycles."""
    from core.hrrr_cam import MODELS
    cfg = MODELS[src]
    max_fhr = int(cfg.get("max_fhr", 60))
    min_fhr = int(cfg.get("min_fhr", 1))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in range(0, 40):
        cyc = now - timedelta(hours=back)
        if cyc.hour not in cfg["cycles"]:
            continue
        fhr = int(round((valid - cyc).total_seconds() / 3600))
        if fhr < min_fhr or fhr > max_fhr:
            continue
        # GFS: hourly files to +120 h, 3-hourly after
        if fhr > int(cfg.get("hourly_to", 10 ** 6)) and fhr % 3:
            continue
        if _idx_exists(src, cyc, fhr):
            return cyc, fhr
    return None


# ---------------------------------------------------------------- render

def _merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _inv_merc(y):
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


_INDEX: dict = {}


def render_mercator(product: str, vals, lats, lons, bounds, width: int,
                    grid_key: str, smooth: float = 1.2) -> bytes:
    """vals/lats/lons (model grid, cropped) -> BAND-INDEX PNG (8-bit
    grey, 0 = nothing, k = k-th palette band) in Web Mercator over
    `bounds` (w, s, e, n), `width` px wide. The browser colours it
    (core.tio_map viewer), which is what makes the legend adjustable
    without re-rendering. Unit scaling, masks and the coverage-aware
    blur follow core.cam_fast."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage as ndi
    from scipy.spatial import cKDTree

    from core.cam_fast import (COVERAGE_FLOOR, COVERAGE_MIN, EDGE_MULT,
                               PALETTES, SPECKLE_MAX_DBZ, SPECKLE_MIN_CELLS,
                               _lut_for)

    spec = PALETTES[product]
    lut = _lut_for(product)
    w, s, e, n = bounds
    y0, y1 = _merc_y(s), _merc_y(n)
    height = max(2, int(round(width * (y1 - y0) / math.radians(e - w))))
    hit = _INDEX.get(grid_key)
    if hit is None or hit[0] != (width, height, bounds):
        la = np.asarray(lats, dtype="float64").ravel()
        lo = np.asarray(lons, dtype="float64").ravel()
        lo = np.where(lo > 180.0, lo - 360.0, lo)
        gx = np.linspace(w, e, width)
        gy = np.array([_inv_merc(y) for y in np.linspace(y1, y0, height)])
        GX, GY = np.meshgrid(gx, gy)
        pts = np.column_stack([lo, la])
        tree = cKDTree(pts)
        dist, idx = tree.query(np.column_stack([GX.ravel(), GY.ravel()]), k=1, workers=-1)
        rng = np.random.default_rng(0)
        pick = rng.choice(len(pts), size=min(500, len(pts)), replace=False)
        sd, _ = tree.query(pts[pick], k=2, workers=-1)
        spacing = float(np.median(sd[:, 1]))
        hit = ((width, height, bounds), idx.astype("int32"), dist.astype("float32"), spacing)
        _INDEX[grid_key] = hit
    _k, idx, near, spacing = hit
    g = np.asarray(vals, dtype="float32").ravel()[idx].reshape(height, width)
    scale = float(spec.get("scale", 1.0))
    if scale != 1.0:
        g = g * scale
    blank = ~np.isfinite(g)
    if "below" in spec:
        blank |= g < float(spec["below"])
    if "above" in spec:
        blank |= g > float(spec["above"])
    if EDGE_MULT > 0 and np.isfinite(spacing):
        blank |= (near > spacing * EDGE_MULT).reshape(height, width)
    if SPECKLE_MIN_CELLS > 0 and product in ("REFD", "REFC"):
        lab, nlab = ndi.label(~blank)
        if nlab:
            sizes = ndi.sum(~blank, lab, index=np.arange(1, nlab + 1))
            peaks = ndi.maximum(np.nan_to_num(g, nan=-1e9), lab, index=np.arange(1, nlab + 1))
            drop = (sizes < SPECKLE_MIN_CELLS) & (peaks < SPECKLE_MAX_DBZ)
            if drop.any():
                blank |= np.isin(lab, np.nonzero(drop)[0] + 1)
    if smooth:
        w0 = (~blank).astype("float32")
        num = ndi.gaussian_filter(np.where(blank, 0.0, g).astype("float32"), smooth)
        den = ndi.gaussian_filter(w0, smooth)
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.where(den > COVERAGE_MIN, num / np.maximum(den, COVERAGE_FLOOR), np.nan)
        blank = ~np.isfinite(g) | (den <= COVERAGE_MIN)
    band = np.digitize(np.nan_to_num(g, nan=-1e9), spec["bounds"]).astype("uint8")
    band[blank] = 0
    im = Image.fromarray(band, mode="L")
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# Decoded fields, kept for the point readout: (src, prod, cycleYYYYMMDDHH,
# fhr) -> (vals, lats, lons) at the decimated CONUS resolution (~2 MB
# each). LRU; MDL_FIELD_CACHE entries.
_FIELDS: dict = {}
FIELD_CACHE = int(os.environ.get("MDL_FIELD_CACHE", "24"))
_fields_lock = threading.Lock()


def _field(src: str, prod: str, cyc: datetime, fhr: int):
    import numpy as np
    from core.cam_warm import CONUS_CENTER, CONUS_ZOOM
    from core.hrrr_cam import fetch_and_decode

    k = (src, prod, cyc.strftime("%Y%m%d%H"), fhr)
    with _fields_lock:
        hit = _FIELDS.pop(k, None)
        if hit is not None:
            _FIELDS[k] = hit
            return hit
    la, lo = CONUS_CENTER
    # The Lambert grids end where they end; the global GFS is cropped
    # to the pad, so widen it to reach Maine and the Maritimes.
    zoom = float(CONUS_ZOOM) + (6.0 if src == "gfs" else 0.0)
    vals, lats, lons = fetch_and_decode(src, prod, cyc, fhr, la, lo, zoom)
    lons = np.where(lons > 180, lons - 360, lons).astype("float32")
    hit = (np.asarray(vals, dtype="float32"), np.asarray(lats, dtype="float32"), lons)
    with _fields_lock:
        _FIELDS[k] = hit
        while len(_FIELDS) > FIELD_CACHE:
            _FIELDS.pop(next(iter(_FIELDS)))
    return hit


# Display units for the readout, per cam_fast product (values are in
# the palette's display units after `scale`).
_FMT = {"REFC": ("{:.0f} dBZ", 1.0), "REFD": ("{:.0f} dBZ", 1.0),
        "RETOP": ("FL{:03.0f}", 10.0), "VIS": ("{:.1f} sm", 1.0),
        "CEIL": ("{:.0f}00 ft", 1.0), "GUST": ("{:.0f} kt", 1.0),
        "LTNG": ("{:.1f}", 1.0), "CAPE": ("{:.0f} J/kg", 1.0),
        "CIN": ("{:.0f} J/kg", 1.0)}


def _fmt(prod: str, v) -> str:
    import math as _m
    if v is None or not _m.isfinite(v):
        return "—"
    if prod.startswith("PROB"):
        return f"{v:.0f} %"
    f, mult = _FMT.get(prod, ("{:.2f}", 1.0))
    return f.format(v * mult)


def point_values(model: str, lat: float, lon: float, valid: datetime) -> dict:
    """Every layer of `model` at (lat, lon) for `valid`: the newest run
    with that hour, nearest grid cell. {"rows": [{layer, value, raw,
    cycle, fhr}], "model": ...}. Slow the first time a layer's field is
    not cached (one GRIB fetch each)."""
    import numpy as np
    from core.cam_fast import PALETTES

    rows = []
    for code, src, prod, label in LAYERS[model]:
        cf = cycle_for_valid(src, valid)
        if not cf:
            rows.append({"layer": label, "code": code, "value": "no run", "raw": None})
            continue
        cyc, fhr = cf
        try:
            vals, lats, lons = _field(src, prod, cyc, fhr)
            d2 = (lats - lat) ** 2 + ((lons - lon) * math.cos(math.radians(lat))) ** 2
            i = int(np.nanargmin(d2))
            raw = float(vals.ravel()[i])
            spec = PALETTES.get(prod, {})
            v = raw * float(spec.get("scale", 1.0))
            if not math.isfinite(raw):
                v = None
            rows.append({"layer": label, "code": code, "value": _fmt(prod, v),
                         "raw": None if v is None else round(v, 3),
                         "cycle": _iso(cyc), "fhr": fhr})
        except Exception as exc:
            rows.append({"layer": label, "code": code,
                         "value": f"error: {type(exc).__name__}", "raw": None})
    return {"model": model, "label": MODEL_LABEL.get(model, model), "rows": rows,
            "lat": round(lat, 3), "lon": round(lon, 3), "valid": _iso(valid)}


def _req_path(outdir):
    return Path(outdir) / "mdl_point_req.json"


def point_request(outdir, req: dict) -> None:
    """From the page: a click. The warmer answers into
    static/mdl_point_<id>.json (the viewer polls for it)."""
    try:
        p = _req_path(outdir)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({**req, "t": time.time()}))
        os.replace(tmp, p)
    except Exception:
        pass


def _answer_point(outdir) -> bool:
    """Serve a pending click, if any. Returns True when one was."""
    outdir = Path(outdir)
    p = _req_path(outdir)
    if not p.exists():
        return False
    try:
        req = json.loads(p.read_text())
    except Exception:
        return False
    try:
        p.unlink()
    except Exception:
        pass
    if time.time() - float(req.get("t", 0)) > 120:
        return False
    rid = str(req.get("id", ""))[:40]
    if not rid:
        return False
    now_h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    valid = now_h + timedelta(hours=int(req.get("step") or 0))
    models = [m for m in (req.get("models") or []) if m in MODELS_ON] or MODELS_ON[:1]
    STATUS["busy"] = f"point readout {rid}"
    t0 = time.time()
    out = {"id": rid, "valid": _iso(valid), "lat": req.get("lat"), "lon": req.get("lon"),
           "models": [point_values(m, float(req["lat"]), float(req["lon"]), valid) for m in models]}
    q = outdir / f"mdl_point_{rid}.json"
    tmp = q.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    os.replace(tmp, q)
    STATUS["busy"] = ""
    _log(outdir, f"point {rid} {req.get('lat')},{req.get('lon')} +{req.get('step')}h "
                 f"{', '.join(models)} {time.time() - t0:.1f}s")
    # old answers
    for old in outdir.glob("mdl_point_*.json"):
        try:
            if time.time() - old.stat().st_mtime > 600:
                old.unlink()
        except Exception:
            pass
    return True


def build_frame(outdir, key: str, valid: datetime) -> dict | None:
    """Fetch, render and file one frame for a layer at a valid time.
    None when no run has that hour yet."""
    import numpy as np
    from core.cam_warm import CONUS_CENTER, CONUS_ZOOM
    from core.hrrr_cam import fetch_and_decode

    src, prod = _spec(key)
    cf = cycle_for_valid(src, valid)
    if not cf:
        return None
    cyc, fhr = cf
    outdir = Path(outdir)
    model, code = key.split(":", 1)
    stamp = cyc.strftime("%Y%m%d%H")
    name = f"mdl_{model}_{code}_{stamp}_f{fhr:02d}.png"
    man = manifest(outdir)
    ent = (man.get(model) or {}).get(code, {}).get(_iso(valid))
    if ent and ent.get("name") == name and (outdir / name).exists():
        return ent                      # already have this run's frame
    t0 = time.time()
    STATUS["busy"] = f"{key} {_iso(valid)} ({cyc:%d/%H}Z f{fhr:02d})"
    la, lo = CONUS_CENTER
    vals, lats, lons = _field(src, prod, cyc, fhr)
    box = (float(np.nanmin(lons)), float(np.nanmin(lats)),
           float(np.nanmax(lons)), float(np.nanmax(lats)))
    # Clip to the map's working area (CONUS plus a margin); the frame
    # edge inside the box is masked by EDGE_MULT.
    bounds = (max(box[0], -126.0), max(box[1], 20.0), min(box[2], -62.0), min(box[3], 54.0))
    webp = render_mercator(prod, vals, lats, lons, bounds, WIDTH_PX,
                           grid_key=f"{src}|{vals.shape}")
    tmp = outdir / f".{name}.tmp"
    tmp.write_bytes(webp)
    os.replace(tmp, outdir / name)
    ent = {"name": name, "model": model, "code": code, "valid": _iso(valid),
           "cycle": _iso(cyc), "fhr": fhr, "built": _iso(datetime.now(timezone.utc)),
           "bounds": list(bounds), "idx": True, "product": prod}
    man = manifest(outdir)
    man.setdefault(model, {}).setdefault(code, {})[_iso(valid)] = ent
    _save_manifest(outdir, man)
    STATUS["busy"] = ""
    _log(outdir, f"{key} valid {_iso(valid)} <- {cyc:%d/%H}Z f{fhr:02d} "
                 f"{time.time() - t0:.1f}s {len(webp) // 1024} KB")
    return ent


def prune(outdir):
    """Drop frames whose valid time is more than KEEP_PAST_H ago, and
    files the manifest no longer names."""
    outdir = Path(outdir)
    man = manifest(outdir)
    now = datetime.now(timezone.utc)
    keep = set()
    changed = False
    for model, codes in list(man.items()):
        for code, ents in list(codes.items()):
            for v, e in list(ents.items()):
                try:
                    vt = datetime.strptime(v, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    vt = now
                if now - vt > timedelta(hours=KEEP_PAST_H):
                    del ents[v]
                    changed = True
                else:
                    keep.add(e["name"])
    if changed:
        _save_manifest(outdir, man)
    for p in list(outdir.glob("mdl_*_f[0-9][0-9].webp")) + list(outdir.glob("mdl_*_f[0-9][0-9].png")):
        if p.name not in keep:
            try:
                p.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------- warmer

def _wanted(outdir) -> list:
    """[(key, valid)] in priority order from the viewer's demand."""
    from core import tio_map as T

    now_h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    dm = T.demand(outdir)
    active = [f for f in T.active_fields_all(outdir) if ":" in f and f in field_keys()]
    out = []
    if dm and not dm.get("playing"):
        step = int(dm.get("step") or 0)
        on = [f for f in (dm.get("fields") or []) if ":" in f and f in field_keys()]
        for st_ in [step] + [step + d for d in (1, 2, 3)] + [step - 1]:
            if st_ < 0:
                continue
            for f in on:
                out.append((f, now_h + timedelta(hours=st_)))
    for h in range(0, PREFETCH_H + 1):
        for f in active:
            out.append((f, now_h + timedelta(hours=h)))
    seen, uniq = set(), []
    for k in out:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _is_fresh(outdir, key: str, valid: datetime) -> bool:
    model, code = key.split(":", 1)
    ent = (manifest(outdir).get(model) or {}).get(code, {}).get(_iso(valid))
    if not ent or not (Path(outdir) / ent["name"]).exists():
        return False
    try:
        built = datetime.strptime(ent["built"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    # re-check for a newer run once an hour
    return datetime.now(timezone.utc) - built < timedelta(minutes=REFRESH_MIN)


def _loop(outdir):
    from core import tio_map as T

    outdir = Path(outdir)
    time.sleep(12)
    _log(outdir, f"warmer start: models={MODELS_ON} layers={len(field_keys())} "
                 f"prefetch +{PREFETCH_H} h, {WIDTH_PX} px frames")
    last_prune = 0.0
    while True:
        try:
            if time.time() - last_prune > 900:
                prune(outdir)
                last_prune = time.time()
            if T.is_idle(outdir):
                time.sleep(15)
                continue
            if _answer_point(outdir):
                continue
            built = 0
            for key, valid in _wanted(outdir):
                if built >= MAX_PER_TICK:
                    break
                if _is_fresh(outdir, key, valid):
                    continue
                try:
                    if build_frame(outdir, key, valid) is not None:
                        built += 1
                        STATUS["last"] = time.time()
                        STATUS["err"] = None
                    else:
                        # no run has this hour yet: mark so we do not
                        # probe it every tick (probe cache covers 10 min)
                        pass
                except Exception as exc:
                    STATUS["err"] = f"{key}: {type(exc).__name__}: {exc}"
                    STATUS["busy"] = ""
                    _log(outdir, f"FAILED {key} {_iso(valid)}: {STATUS['err'][:160]}")
            time.sleep(1 if built else 2)
        except Exception as exc:
            _log(outdir, f"loop error: {type(exc).__name__}: {exc}")
            time.sleep(10)


def ensure_model_warmer(outdir) -> bool:
    if os.environ.get("MDL_WARMER", "on").lower() == "off" or not MODELS_ON:
        return False
    global _started
    with _lock:
        if _started:
            return True
        threading.Thread(target=_loop, args=(outdir,), daemon=True,
                         name="model-warmer").start()
        _started = True
        STATUS["started"] = True
    return True


# ---------------------------------------------------------------- legend

def palette_json() -> dict:
    """{layer key: {"bounds": [...], "colors": ["#RRGGBB", ...], "unit"}}
    for the viewer, which colours the band-index frames itself."""
    from core.cam_fast import PALETTES, _lut_for
    out = {}
    for key in field_keys():
        _src, prod = _spec(key)
        spec = PALETTES[prod]
        lut = _lut_for(prod)
        unit = ("%" if prod.startswith("PROB") else
                {"REFC": "dBZ", "REFD": "dBZ", "RETOP": "kft", "VIS": "sm",
                 "CEIL": "x100 ft", "GUST": "kt", "LTNG": "fl/km\u00b2",
                 "CAPE": "J/kg", "CIN": "J/kg"}.get(prod, ""))
        out[key] = {"bounds": [float(b) for b in spec["bounds"]],
                    "colors": ["#%02X%02X%02X" % tuple(int(c) for c in lut[i + 1][:3])
                               for i in range(len(spec["bounds"]))],
                    "unit": unit, "label": field_label(key)}
    return out
