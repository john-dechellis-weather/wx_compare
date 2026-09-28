"""Large Scale Map: tomorrow.io map tiles stitched server-side.

Target path: core/tio_map.py

The page covers CONUS (125W-66W, 24N-50N; was the whole JetBlue
network to 5S until 28 Sep - tiles over ocean and South America were
most of the spend), Web Mercator. The tomorrow.io
Weather Maps API serves 256 px PNG tiles

    https://api.tomorrow.io/v4/map/tile/{z}/{x}/{y}/{field}/{time}.png?apikey=KEY

for the last 7 days and next 14 days. A browser could load them
directly, but the tile URL carries the API key (visible to every
viewer) and every pan, zoom and time step would spend the request
quota. So a warmer thread fetches a FIXED tile set for the domain,
stitches each set into one WebP under static/, and the page shows
those. The key never leaves the server and the spend is predictable.

28 SEP: five fields, an hourly timeline to +24 h then 3-hourly to
+72 h (40 forecast steps), layers that stack, and ONE model - NextGen
(see MODELS). Budget against the 9,000/day cap, CONUS at zoom 4 (8
tiles):

    forecast pass   8 tiles x 40 steps x 5 fields = 1,600,
                    every TIO_FCST_MIN (360) min             -> 6,400/day
    "now" frame     8 tiles x 5 fields,
                    every TIO_NOW_MIN (60) min               ->   960/day
    station points  one /v4/timelines call per station (~79),
                    every TIO_PT_MIN (180) min               ->   632/day
                                                                ---------
                                                                 7,992/day

The station points feed the 2-PANEL mode: click a station dot and the
right panel draws a meteogram of the layers that are switched on,
from the same tomorrow.io source as the tiles (hourly to +72 h, all
five fields in one call). Warmed like the tiles, so a click costs
nothing and the key stays on the server.

precipitationReflectivity is one of tomorrow.io's "advanced weather
layers" - served by the tile endpoint, not in the public field list.

Switching model re-warms everything for the new model (one forecast
pass plus a now pass, ~1,280 requests), so the switch is a page
control, not a per-viewer toggle, and the cap still governs it.
TIO_DAILY_CAP is a hard stop: the warmer counts every request in
static/tio_usage.json by UTC day and skips a pass rather than cross it.

Env
    TOMORROWIO_API_KEY   (or TOMORROW_API_KEY)  the key
    TIO_WARMER=off       stop the warmer without a deploy
    TIO_FIELDS           comma list; default the five below
    TIO_FCST_HOURLY_TO / TIO_FCST_STEP / TIO_FCST_MAX   24 / 3 / 72
    TIO_FCST_HOURS       explicit comma list, overrides the three above
    TIO_MODEL_QUERY      query appended to every tile URL to select the
                         model, e.g. "includedLayers=nextgen"; empty
                         (default) until the tile endpoint's spelling is
                         confirmed - the tiles are then whatever the key
                         serves by default.
    TIO_MODELS           several models, "key:label:query|key:label:query",
                         e.g. "focus:FOCUS:includedLayers=focus|nextgen:
                         NextGen:includedLayers=nextgen"; the page then
                         shows a switch. Overrides TIO_MODEL_QUERY.
    TIO_NOW_MIN / TIO_FCST_MIN / TIO_NOW_ZOOM / TIO_FCST_ZOOM
    TIO_PT_MIN           station-point refresh (180); TIO_POINTS=off skips
    TIO_DAILY_CAP
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- domain

W, E, S, N = -125.0, -66.0, 24.0, 50.0
TILE = 256

# Order matters twice: it is the layer menu order, and (reversed) the
# stacking order on the map - precipitation is listed first and drawn
# on top, the ceiling and visibility fills sit underneath.
FIELDS = [f.strip() for f in os.environ.get(
    "TIO_FIELDS",
    "precipitationReflectivity,cloudCeiling,visibility,windSpeed,windGust"
).split(",") if f.strip()]
FIELD_LABEL = {
    "precipitationReflectivity": "Precipitation reflectivity",
    "precipitationIntensity": "Precipitation intensity",
    "precipitationType": "Precipitation type",
    "cloudCover": "Cloud cover",
    "windSpeed": "Wind speed",
    "windGust": "Wind gust",
    "temperature": "Temperature",
    "visibility": "Visibility",
    "cloudBase": "Cloud base",
    "cloudCeiling": "Cloud ceiling",
    "pressureSeaLevel": "MSL pressure",
    "dewPoint": "Dew point",
}


def _steps() -> list:
    raw = os.environ.get("TIO_FCST_HOURS", "").strip()
    if raw:
        return sorted({int(h) for h in raw.split(",") if h.strip()})
    hourly_to = int(os.environ.get("TIO_FCST_HOURLY_TO", "24"))
    step = max(1, int(os.environ.get("TIO_FCST_STEP", "3")))
    mx = int(os.environ.get("TIO_FCST_MAX", "72"))
    out = list(range(1, min(hourly_to, mx) + 1))
    h = hourly_to + step
    while h <= mx:
        out.append(h)
        h += step
    return out


FCST_HOURS = _steps()
NOW_MIN = int(os.environ.get("TIO_NOW_MIN", "60"))
FCST_MIN = int(os.environ.get("TIO_FCST_MIN", "360"))
NOW_ZOOM = int(os.environ.get("TIO_NOW_ZOOM", "4"))
FCST_ZOOM = int(os.environ.get("TIO_FCST_ZOOM", "4"))
DAILY_CAP = int(os.environ.get("TIO_DAILY_CAP", "9000"))
KEEP_FRAMES = 2          # per model/field/step, newest kept
PT_MIN = int(os.environ.get("TIO_PT_MIN", "180"))
POINTS_ON = os.environ.get("TIO_POINTS", "on").lower() != "off"
# Tile layer -> timelines field. The reflectivity tile has no point
# equivalent, so the meteogram shows precipitation intensity for it.
POINT_FIELD = {"precipitationReflectivity": "precipitationIntensity"}
PT_MAX_H = int(os.environ.get("TIO_FCST_MAX", "72"))


def _parse_models() -> dict:
    """{key: (label, query)} in menu order; always at least one."""
    raw = os.environ.get("TIO_MODELS", "").strip()
    out = {}
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        bits = part.split(":", 2)
        key = bits[0].strip()
        label = bits[1].strip() if len(bits) > 1 and bits[1].strip() else key
        query = bits[2].strip().lstrip("?&") if len(bits) > 2 else ""
        if key:
            out[key] = (label, query)
    return out or {"nextgen": ("NextGen",
                               os.environ.get("TIO_MODEL_QUERY", "").strip().lstrip("?&"))}


MODELS = _parse_models()

#: Canadian alternates, not in jbu_airports.json.
CAN_ALT = {
    "CYVR": (49.194, -123.184), "CYYC": (51.114, -114.020),
    "CYWG": (49.910, -97.240), "CYYZ": (43.677, -79.631),
    "CYOW": (45.323, -75.669), "CYUL": (45.470, -73.741),
    "CYQB": (46.791, -71.393), "CYHZ": (44.881, -63.509),
}
EU_COUNTRIES = {"GB", "IE", "FR", "NL", "ES", "IT"}
SKIP_STATIONS = {"SBAQ"}
#: Metro clusters: the dot stays, the label goes to the named field.
NO_LABEL = {"KLGA", "KEWR", "KHPN", "KISP", "KMDW", "KONT", "KBUR",
            "KFLL", "KPBI", "KDJT", "KHYA", "KMVY", "KACK", "KORH",
            "KPVD", "KBDL", "KBWI", "KPHL", "KSRQ", "KDAB", "KVRB",
            "KSJC", "KRSW", "MDST", "MDPP", "TJBQ", "TJPS", "TISX",
            "TNCB", "TNCC", "TIST"}

STATUS: dict = {"started": False, "last": None, "err": None, "model": None}
_lock = threading.Lock()
_started = False


def api_key() -> str:
    return (os.environ.get("TOMORROWIO_API_KEY")
            or os.environ.get("TOMORROW_API_KEY") or "").strip()


# ---------------------------------------------------------------- model

def _model_path(outdir):
    return Path(outdir) / "tio_model.txt"


def active_model(outdir) -> str:
    """The one model being warmed. A page control writes it; the
    warmer reads it every pass."""
    try:
        k = _model_path(outdir).read_text().strip()
        if k in MODELS:
            return k
    except Exception:
        pass
    return next(iter(MODELS))


def set_model(outdir, key: str) -> None:
    if key not in MODELS:
        raise ValueError(key)
    p = _model_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(key)
    os.replace(tmp, p)


def model_label(key: str) -> str:
    return MODELS.get(key, (key, ""))[0]


# ---------------------------------------------------------------- tiles

def _lon2x(lon, z):
    return int(math.floor((lon + 180.0) / 360.0 * (1 << z)))


def _lat2y(lat, z):
    r = math.radians(lat)
    return int(math.floor(
        (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi)
        / 2.0 * (1 << z)))


def _x2lon(x, z):
    return x / (1 << z) * 360.0 - 180.0


def _y2lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def tile_grid(z: int):
    """(x0, x1, y0, y1, bounds) for the domain at zoom z. bounds is
    [w, s, e, n] of the whole tile block - the image corners."""
    x0, x1 = _lon2x(W, z), _lon2x(E - 1e-9, z)
    y0, y1 = _lat2y(N, z), _lat2y(S + 1e-9, z)
    bounds = [_x2lon(x0, z), _y2lat(y1 + 1, z), _x2lon(x1 + 1, z), _y2lat(y0, z)]
    return x0, x1, y0, y1, bounds


def tile_count(z: int) -> int:
    x0, x1, y0, y1, _ = tile_grid(z)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def daily_estimate() -> int:
    """Requests/day the current settings spend for one model."""
    nf = len(FIELDS)
    fc = tile_count(FCST_ZOOM) * len(FCST_HOURS) * nf * (1440 // max(1, FCST_MIN))
    nw = tile_count(NOW_ZOOM) * nf * (1440 // max(1, NOW_MIN))
    pt = 0
    if POINTS_ON:
        # station count without reading the file: 72 JBU + 7 alternates
        # in the box today; the log line has the real number
        pt = 79 * (1440 // max(1, PT_MIN))
    return fc + nw + pt


def _fetch_tile(z, x, y, field, tstr, key, query=""):
    import requests

    url = (f"https://api.tomorrow.io/v4/map/tile/{z}/{x}/{y}/{field}/"
           f"{tstr}.png?apikey={key}" + (f"&{query}" if query else ""))
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403):
                break
        except Exception as exc:
            last = f"{type(exc).__name__}"
        time.sleep(0.5)
    raise RuntimeError(f"tile {z}/{x}/{y} {tstr}: {last}")


# ---------------------------------------------------------------- usage

def _usage_path(outdir):
    return Path(outdir) / "tio_usage.json"


def usage(outdir) -> dict:
    try:
        d = json.loads(_usage_path(outdir).read_text())
    except Exception:
        d = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"day": today, "count": int(d.get("count", 0))
            if d.get("day") == today else 0}


def _add_usage(outdir, n):
    u = usage(outdir)
    u["count"] += n
    p = _usage_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(u))
    os.replace(tmp, p)
    return u["count"]


# ---------------------------------------------------------------- build

def _log(outdir, msg):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%MZ} {msg}"
    try:
        p = Path(outdir) / "tio_warmer.log"
        with open(p, "a") as f:
            f.write(line + "\n")
        if p.stat().st_size > 200_000:
            p.write_text("\n".join(p.read_text().splitlines()[-400:]) + "\n")
    except Exception:
        pass


def log_tail(outdir, n=6) -> list:
    try:
        return (Path(outdir) / "tio_warmer.log").read_text().splitlines()[-n:]
    except Exception:
        return []


# Manifest v2: {model: {field: {step: entry}}}. A new file name so the
# single-model v1 manifest (field -> step) is never misread.
def _manifest_path(outdir):
    return Path(outdir) / "tio_manifest2.json"


def manifest(outdir) -> dict:
    try:
        return json.loads(_manifest_path(outdir).read_text())
    except Exception:
        return {}


def _save_manifest(outdir, man):
    p = _manifest_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(man))
    os.replace(tmp, p)


def build_frame(outdir, model: str, field: str, step: int, z: int) -> dict:
    """Fetch and stitch one frame. step 0 = now; otherwise hours
    ahead, on the hour. Returns the manifest entry."""
    from PIL import Image

    key = api_key()
    if not key:
        raise RuntimeError("no TOMORROWIO_API_KEY")
    outdir = Path(outdir)
    _label, query = MODELS[model]
    x0, x1, y0, y1, bounds = tile_grid(z)
    now = datetime.now(timezone.utc)
    if step == 0:
        tstr, valid = "now", now
    else:
        valid = (now.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=step))
        tstr = valid.strftime("%Y-%m-%dT%H:%M:%SZ")
    n = (x1 - x0 + 1) * (y1 - y0 + 1)
    u = usage(outdir)
    if u["count"] + n > DAILY_CAP:
        raise RuntimeError(f"daily cap: {u['count']}+{n} > {DAILY_CAP}")
    _add_usage(outdir, n)          # charged up front; a failed tile still counts
    jobs = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        blobs = list(ex.map(lambda xy: _fetch_tile(z, xy[0], xy[1], field,
                                                   tstr, key, query), jobs))
    img = Image.new("RGBA", ((x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE),
                    (0, 0, 0, 0))
    for (x, y), b in zip(jobs, blobs):
        img.paste(Image.open(io.BytesIO(b)).convert("RGBA"),
                  ((x - x0) * TILE, (y - y0) * TILE))
    stamp = now.strftime("%Y%m%d-%H%M")
    name = f"tio_{model}_{field}_{step:02d}_{stamp}.webp"
    tmp = outdir / f".{name}.tmp"
    img.save(tmp, "WEBP", quality=88, method=4)
    os.replace(tmp, outdir / name)
    ent = {"name": name, "model": model, "field": field, "step": step,
           "zoom": z,
           "valid": valid.strftime("%Y-%m-%dT%H:%MZ"),
           "built": now.strftime("%Y-%m-%dT%H:%MZ"),
           "bounds": bounds, "px": [img.width, img.height]}
    man = manifest(outdir)
    man.setdefault(model, {}).setdefault(field, {})[str(step)] = ent
    _save_manifest(outdir, man)
    # prune older frames of this model/field/step
    olds = sorted(outdir.glob(f"tio_{model}_{field}_{step:02d}_*.webp"))
    for p in olds[:-KEEP_FRAMES]:
        try:
            p.unlink()
        except Exception:
            pass
    _log(outdir, f"{model} {field} +{step:02d}h z{z} {n} tiles "
                 f"{time.time() - t0:.1f}s -> {name} "
                 f"({(outdir / name).stat().st_size // 1024} KB), "
                 f"day {usage(outdir)['count']}/{DAILY_CAP}")
    return ent


# ------------------------------------------------------------- points

def _points(static_dir) -> list:
    """[(icao, lat, lon)] for every station the map shows."""
    out = []
    for f in stations_geojson(static_dir)["features"]:
        lo, la = f["geometry"]["coordinates"]
        out.append((f["properties"]["id"], la, lo))
    return out


def _pt_path(outdir, model):
    return Path(outdir) / f"tio_pt_{model}.json"


def points(outdir, model) -> dict:
    try:
        return json.loads(_pt_path(outdir, model).read_text())
    except Exception:
        return {}


def _fetch_point(icao, lat, lon, fields, key, query):
    """One /v4/timelines call: hourly, now to +PT_MAX_H h, metric.
    Returns {"t": [iso...], field: [values...]}."""
    import requests

    url = f"https://api.tomorrow.io/v4/timelines?apikey={key}"
    body = {"location": f"{lat:.4f},{lon:.4f}", "fields": fields,
            "timesteps": ["1h"], "units": "metric",
            "startTime": "now", "endTime": f"nowPlus{PT_MAX_H}h"}
    # The model selector rides in the body on timelines (the tile
    # query "includedLayers=x" becomes "includedLayers": ["x"]).
    if query:
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                body[k] = [v] if k == "includedLayers" else v
    r = requests.post(url, json=body, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"{icao}: HTTP {r.status_code} {r.text[:120]}")
    iv = r.json()["data"]["timelines"][0]["intervals"]
    out = {"t": [x["startTime"] for x in iv]}
    for f in fields:
        out[f] = [x["values"].get(f) for x in iv]
    return out


def build_points(outdir, model) -> int:
    """All stations' point forecasts for `model` into one JSON.
    Charged one request per station, up front, like the tiles."""
    key = api_key()
    if not key:
        raise RuntimeError("no TOMORROWIO_API_KEY")
    outdir = Path(outdir)
    _label, query = MODELS[model]
    fields = sorted({POINT_FIELD.get(f, f) for f in FIELDS})
    pts = _points(outdir)
    u = usage(outdir)
    if u["count"] + len(pts) > DAILY_CAP:
        raise RuntimeError(f"daily cap: {u['count']}+{len(pts)} > {DAILY_CAP}")
    _add_usage(outdir, len(pts))
    t0 = time.time()
    got, bad = {}, []

    def one(p):
        try:
            got[p[0]] = _fetch_point(p[0], p[1], p[2], fields, key, query)
        except Exception as exc:
            bad.append(f"{type(exc).__name__}: {exc}"[:100])
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, pts))
    if not got:
        raise RuntimeError("points: every station failed - "
                           + (bad[0] if bad else "?"))
    now = datetime.now(timezone.utc)
    doc = {"model": model, "built": now.strftime("%Y-%m-%dT%H:%MZ"),
           "fields": fields, "units": {"windSpeed": "m/s", "windGust": "m/s",
                                       "visibility": "km", "cloudCeiling": "km",
                                       "precipitationIntensity": "mm/h"},
           "stations": got}
    p = _pt_path(outdir, model)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)
    _log(outdir, f"{model} points {len(got)}/{len(pts)} stations "
                 f"{time.time() - t0:.1f}s"
                 + (f", {len(bad)} failed ({bad[0]})" if bad else "")
                 + f", day {usage(outdir)['count']}/{DAILY_CAP}")
    return len(got)


def _pass(outdir, model, steps):
    """Steps in TIME order across fields, so when the cap stops a
    pass mid-way the near hours exist for every layer rather than
    the whole run for one."""
    for step in steps:
        for field in FIELDS:
            try:
                build_frame(outdir, model, field, step,
                            NOW_ZOOM if step == 0 else FCST_ZOOM)
                STATUS["last"] = time.time()
                STATUS["err"] = None
            except Exception as exc:
                STATUS["err"] = f"{type(exc).__name__}: {exc}"
                _log(outdir, f"FAILED {model} {field} +{step:02d}h: {STATUS['err']}")
                if "daily cap" in str(exc) or "no TOMORROWIO" in str(exc):
                    return


def _prune_v1(outdir):
    """Frames from the single-model layout (tio_<field>_NN_*.webp) are
    unreachable now; drop them once."""
    for f in FIELD_LABEL:
        for p in Path(outdir).glob(f"tio_{f}_[0-9][0-9]_*.webp"):
            try:
                p.unlink()
            except Exception:
                pass


def _loop(outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    time.sleep(8)          # let the page-3 warmers get in first
    _prune_v1(outdir)
    _log(outdir, f"warmer start: fields={FIELDS} steps={len(FCST_HOURS)} "
                 f"(to +{FCST_HOURS[-1] if FCST_HOURS else 0}h) models={list(MODELS)} "
                 f"now z{NOW_ZOOM}/{NOW_MIN}min fcst z{FCST_ZOOM}/{FCST_MIN}min "
                 f"~{daily_estimate()}/day, cap {DAILY_CAP}/day")
    next_now = next_fc = next_pt = 0.0
    last_model = None

    def _pts(model):
        if not POINTS_ON:
            return
        try:
            build_points(outdir, model)
        except Exception as exc:
            STATUS["err"] = f"{type(exc).__name__}: {exc}"
            _log(outdir, f"FAILED {model} points: {STATUS['err']}")

    while True:
        t = time.time()
        model = active_model(outdir)
        STATUS["model"] = model
        if model != last_model:
            # first pass, or the page switched model: full re-warm
            _log(outdir, f"model -> {model}: full pass")
            last_model = model
            next_fc = next_pt = 0.0
        if t >= next_pt:
            # points first: 79 small calls, and the meteogram is
            # usable before the first forecast frames land
            _pts(model)
            next_pt = time.time() + PT_MIN * 60
        if t >= next_fc:
            _pass(outdir, model, [0] + FCST_HOURS)
            next_now = time.time() + NOW_MIN * 60
            next_fc = time.time() + FCST_MIN * 60
        elif t >= next_now:
            _pass(outdir, model, [0])
            next_now = time.time() + NOW_MIN * 60
        time.sleep(20)


def ensure_tio_warmer(outdir) -> bool:
    """Idempotent. TIO_WARMER=off disables without a deploy."""
    if os.environ.get("TIO_WARMER", "on").lower() == "off":
        return False
    global _started
    with _lock:
        if _started:
            return True
        threading.Thread(target=_loop, args=(outdir,), daemon=True,
                         name="tio-warmer").start()
        _started = True
        STATUS["started"] = True
    return True


# ---------------------------------------------------------------- page

def stations_geojson(static_dir) -> dict:
    feats = []
    try:
        st = json.loads((Path(static_dir) / "jbu_airports.json").read_text())["stations"]
    except Exception:
        st = {}
    for icao, v in st.items():
        if v.get("country") in EU_COUNTRIES or icao in SKIP_STATIONS:
            continue
        if not (W <= v["lon"] <= E and S <= v["lat"] <= N):
            continue
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [v["lon"], v["lat"]]},
                      "properties": {"id": icao[1:] if icao[0] in "KCT" and len(icao) == 4 else icao,
                                     "kind": "jbu",
                                     "label": "" if icao in NO_LABEL else
                                     (icao[1:] if icao[0] in "KCT" and len(icao) == 4 else icao)}})
    for icao, (la, lo) in CAN_ALT.items():
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [lo, la]},
                      "properties": {"id": icao[1:], "kind": "alt", "label": icao[1:]}})
    return {"type": "FeatureCollection", "features": feats}


def map_html(man: dict, base: str, stations: dict, height: int = 860,
             model: str = None, pt_built: str = "") -> str:
    """MapLibre page: CARTO dark vector basemap, the active model's
    warmed frames as image sources (added lazily, first time a layer
    and hour are shown), layer toggles with their own opacity, an
    hourly TIME slider, station dots and labels. Served from
    /app/static and embedded by URL (see radar_l3.loop_html for why
    not srcdoc)."""
    model = model or next(iter(MODELS))
    mman = man.get(model) or {}
    frames = []
    for field in FIELDS:
        for step in [0] + FCST_HOURS:
            e = (mman.get(field) or {}).get(str(step))
            if e:
                frames.append({"url": f"{base}/app/static/{e['name']}?v={e['built']}",
                               "field": field, "step": step, "valid": e["valid"],
                               "built": e["built"], "b": e["bounds"]})
    style = os.environ.get(
        "BLUEMET_MAP_STYLE",
        "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json")
    labels = {f: FIELD_LABEL.get(f, f) for f in FIELDS}
    pt_url = f"{base}/app/static/tio_pt_{model}.json?v={pt_built}" if pt_built else ""
    # Layer rows: checkbox + opacity. Precipitation on by default.
    rows = "".join(
        f'<span class="ly"><label><input type="checkbox" data-f="{f}"'
        f'{" checked" if i == 0 else ""}> {labels[f]}</label>'
        f'<input type="range" data-f="{f}" min="10" max="100" value="{85 if i == 0 else 70}"></span>'
        for i, f in enumerate(FIELDS))
    steps_all = [0] + FCST_HOURS
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.2/plotly.min.js"></script>
<style>
 html,body{{margin:0;background:#000;height:100%;font:bold 12px "DejaVu Sans Mono","Courier New",monospace;color:#fff}}
 #wrap{{display:flex;gap:8px;height:{height - 92}px}}
 #m{{flex:1 1 auto;min-width:0;height:100%;border:1px solid #333;border-radius:10px;overflow:hidden;position:relative}}
 #mg{{display:none;flex:0 0 46%;height:100%;border:1px solid #333;border-radius:10px;background:#0A0A0A;overflow:hidden;flex-direction:column}}
 body.two #mg{{display:flex}}
 #mgh{{display:flex;align-items:center;gap:10px;padding:6px 10px;border-bottom:1px solid #222}}
 #mgh select{{font:bold 12px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:6px;padding:4px 8px}}
 #mgt{{color:#FFD400}} #mgs{{color:#B8B8B8;font-weight:normal}}
 #mgc{{flex:1;min-height:0}}
 #mgn{{color:#B8B8B8;padding:20px;font-weight:normal}}
 .bar{{display:flex;align-items:center;gap:10px;height:44px;padding:0 4px;flex-wrap:nowrap;white-space:nowrap}}
 .bar button{{font:bold 12px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:6px;padding:5px 10px}}
 .bar button.on{{border-color:#00E5FF;background:#1C1C22}}
 .bar input[type=range]{{accent-color:#00E5FF}}
 .ly{{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;border:1px solid #333;border-radius:6px;background:#0A0A0A}}
 .ly input[type=range]{{width:56px}} .ly label{{color:#fff;cursor:pointer}}
 .ly.off label{{color:#6E6E6E}} .ly.none label{{color:#FF00C8}}
 #tm{{flex:1;min-width:160px}}
 #valid{{color:#FFD400;min-width:250px}} #built{{color:#B8B8B8}} #mdl{{color:#00E5FF}}
 label{{color:#B8B8B8}} .sp{{flex:1}}
 .lg{{position:absolute;left:10px;bottom:24px;background:rgba(0,0,0,.85);border:1px solid #333;border-radius:6px;padding:8px 10px;font-size:11px;z-index:5}}
 .lg .d{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;border:1px solid #000}}
</style></head><body>
<div class="bar">
  <label>LAYERS</label>{rows}
  <span class="sp"></span><span id="mdl">{model_label(model)}</span>
  <button id="bst" class="on">Stations</button><button id="balt" class="on">CA alternates</button>
  <button id="bfit">Fit</button><button id="b2" title="Map + meteogram of the layers that are on, for a station you click">2-panel</button>
</div>
<div class="bar">
  <label>TIME</label><button id="bprev">&#9664;</button><button id="bplay">Play</button><button id="bnext">&#9654;</button>
  <input id="tm" type="range" min="0" max="{len(steps_all) - 1}" value="0" step="1">
  <span id="valid"></span><span id="built"></span>
</div>
<div id="wrap">
<div id="m"><div class="lg" id="lg"><span class="d" style="background:#4DA3FF"></span>JBU station &nbsp;
 <span class="d" style="background:#9AA0A6"></span>Canadian alternate<br>
 <span style="color:#6E6E6E">tomorrow.io {model_label(model)} tiles · CONUS · hourly to +{int(os.environ.get("TIO_FCST_HOURLY_TO", "24"))} h, 3-hourly to +{FCST_HOURS[-1] if FCST_HOURS else 0} h</span></div></div>
<div id="mg">
  <div id="mgh"><label>STATION</label><select id="mgsel"><option value="">click a dot…</option></select>
    <span id="mgt"></span><span class="sp"></span><span id="mgs"></span></div>
  <div id="mgc"></div><div id="mgn"></div>
</div>
</div>
<script>
const F = {json.dumps(frames)};
const ST = {json.dumps(stations)};
const FIELDS = {json.dumps(FIELDS)};
const STEPS = {json.dumps(steps_all)};
const PT_URL = {json.dumps(pt_url)};
const LABELS = {json.dumps(labels)};
const PT_FIELD = {json.dumps({f: POINT_FIELD.get(f, f) for f in FIELDS})};
const BB = [[{W},{S}],[{E},{N}]];
const map = new maplibregl.Map({{container:'m', style:{json.dumps(style)},
  bounds:BB, fitBoundsOptions:{{padding:8}}, minZoom:2.2, maxZoom:9,
  maxBounds:[[{W - 25},{S - 15}],[{E + 25},{N + 8}]], attributionControl:true}});
map.addControl(new maplibregl.NavigationControl({{showCompass:false}}));
const $ = id => document.getElementById(id);
let ready = false, ti = 0, timer = null;
const on = {{}}, op = {{}};
document.querySelectorAll('.ly input[type=checkbox]').forEach(c => {{ on[c.dataset.f] = c.checked; c.onchange = () => {{ on[c.dataset.f] = c.checked; draw(); }}; }});
document.querySelectorAll('.ly input[type=range]').forEach(r => {{ op[r.dataset.f] = +r.value / 100; r.oninput = () => {{ op[r.dataset.f] = +r.value / 100; draw(); }}; }});
function key(f) {{ return 'tio_' + f.field + '_' + f.step; }}
function frame(field, step) {{ return F.find(f => f.field === field && f.step === step); }}
// Frames become map sources the first time they are shown, not all
// at page load: 5 fields x 57 steps is a lot of WebP to pull for a
// viewer who only looks at the next six hours.
const added = new Set();
function ensure(f) {{
  if (added.has(key(f))) return;
  added.add(key(f));
  map.addSource(key(f), {{type:'image', url:f.url,
    coordinates:[[f.b[0], f.b[3]], [f.b[2], f.b[3]], [f.b[2], f.b[1]], [f.b[0], f.b[1]]]}});
  // insert under the station layers so dots stay on top, and in field
  // order so precipitation (FIELDS[0]) ends up above the fills
  const before = map.getLayer('st-dot') ? 'st-dot' : undefined;
  map.addLayer({{id:key(f), type:'raster', source:key(f),
    paint:{{'raster-opacity':0, 'raster-fade-duration':0, 'raster-resampling':'linear'}}}}, before);
  // re-order: later fields in FIELDS go UNDER earlier ones
  const shown = [...added].filter(k => map.getLayer(k));
  shown.sort((a, b) => FIELDS.indexOf(a.split('_')[1]) - FIELDS.indexOf(b.split('_')[1]));
  // moveLayer(id, beforeId): walk from the bottom-most field up
  for (let i = shown.length - 1; i >= 0; i--) map.moveLayer(shown[i], before);
}}
function draw() {{
  const step = STEPS[ti];
  let valid = '', built = '';
  const visible = new Set();
  FIELDS.forEach(field => {{
    const box = document.querySelector('.ly input[data-f="' + field + '"]').parentElement.parentElement;
    const f = frame(field, step);
    box.classList.toggle('off', !on[field]);
    box.classList.toggle('none', on[field] && !f);
    if (!on[field] || !f) return;
    if (ready) {{ ensure(f); visible.add(key(f)); }}
    if (!valid) {{ valid = f.valid; built = f.built; }}
  }});
  if (ready) added.forEach(k => {{ if (map.getLayer(k)) map.setPaintProperty(k, 'raster-opacity', visible.has(k) ? op[k.split('_')[1]] : 0); }});
  $('valid').textContent = (step ? '+' + step + ' h' : 'now') + (valid ? '  valid ' + valid : '  (no frame yet)');
  $('built').textContent = built ? ' · built ' + built : '';
  if (two && PT && sel) drawMg();
}}
// ---- 2-panel meteogram -------------------------------------------------
// Point forecasts for every station (one tomorrow.io timelines call
// each, warmed server-side) drawn with plotly: one panel per layer
// that is switched on, in the map's layer order, with the TIME
// slider's hour as a cursor. Click a dot or pick from the list.
let PT = null, two = false, sel = '';
const KT = 1.943844, SM = 0.621371, FT = 3280.84;
const CAT = {{VFR:'#00FF7F', MVFR:'#FFD400', IFR:'#FF8A00', LIFR:'#FF00C8'}};
function cat(cig_ft, vis_sm) {{
  const c = cig_ft == null ? 1e9 : cig_ft, v = vis_sm == null ? 99 : vis_sm;
  if (c < 500 || v < 1) return 'LIFR'; if (c < 1000 || v < 3) return 'IFR';
  if (c <= 3000 || v <= 5) return 'MVFR'; return 'VFR';
}}
function cursorTime(st) {{
  const step = STEPS[ti]; const t = st.t;
  return t[Math.min(t.length - 1, step)] || t[0];
}}
// Panels for the layers that are on. Wind speed and gust share one.
function panels(st) {{
  const out = [], wind = [];
  FIELDS.forEach(f => {{
    if (!on[f]) return;
    const pf = PT_FIELD[f], v = st[pf]; if (!v) return;
    if (f === 'windSpeed' || f === 'windGust') {{ wind.push([f, v.map(x => x == null ? null : x * KT)]); return; }}
    if (f === 'cloudCeiling') out.push({{title:'ceiling (ft)', log:true, traces:[[LABELS[f], v.map(x => x == null ? null : Math.min(25000, x * FT)), '#4FA8E8', 'lines+markers']]}});
    else if (f === 'visibility') out.push({{title:'vis (sm)', range:[0, 10.5], traces:[[LABELS[f], v.map(x => x == null ? null : Math.min(10, x * SM)), '#B388FF', 'lines+markers']]}});
    else out.push({{title: pf === 'precipitationIntensity' ? 'precip (mm/h)' : LABELS[f], bar:true, traces:[[LABELS[f], v, '#00E5FF', 'bar']]}});
  }});
  if (wind.length) {{
    const cols = {{windSpeed:'#FFFFFF', windGust:'#FF8A00'}};
    out.push({{title:'wind (kt)', rangemode:'tozero', traces: wind.map(([f, v]) => [LABELS[f], v, cols[f], 'lines+markers'])}});
  }}
  return out;
}}
function drawMg() {{
  if (!two) return;
  const box = $('mgc'), note = $('mgn');
  if (!PT) {{ note.textContent = PT_URL ? 'loading station forecasts…' : 'no station forecasts yet - the warmer builds them on its first pass'; box.innerHTML = ''; return; }}
  const st = PT.stations[sel];
  if (!st) {{ note.textContent = 'click a station dot on the map'; box.innerHTML = ''; $('mgt').textContent = ''; return; }}
  const ps = panels(st);
  if (!ps.length) {{ note.textContent = 'switch a layer on to see it here'; box.innerHTML = ''; return; }}
  note.textContent = '';
  $('mgt').textContent = sel; $('mgs').textContent = PT.model + ' · built ' + PT.built + 'Z';
  const t = st.t.map(x => x.replace('Z', ''));
  const cig = st.cloudCeiling, vis = st.visibility;
  const haveCat = cig && vis;
  const n = ps.length + (haveCat ? 1 : 0);
  const data = [], layout = {{paper_bgcolor:'#0A0A0A', plot_bgcolor:'#05070B', margin:{{l:52, r:14, t:8, b:34}},
    font:{{color:'#B8B8B8', size:11, family:'DejaVu Sans Mono, monospace'}}, showlegend:false, hovermode:'x unified',
    shapes:[], annotations:[]}};
  let row = 1;
  const yax = (i) => 'y' + (i === 1 ? '' : i), xax = (i) => 'x' + (i === 1 ? '' : i);
  if (haveCat) {{
    const cats = t.map((_, k) => cat(cig[k] == null ? null : cig[k] * FT, vis[k] == null ? null : vis[k] * SM));
    data.push({{type:'bar', x:t, y:t.map(() => 1), marker:{{color:cats.map(c => CAT[c])}}, text:cats, hovertemplate:'%{{text}}<extra></extra>',
      xaxis:xax(row), yaxis:yax(row), width:3600e3}});
    layout['yaxis' + (row === 1 ? '' : row)] = {{domain:[1 - 0.055, 1], anchor:xax(row), showticklabels:false, fixedrange:true, title:{{text:'cat', standoff:4}}}};
    layout['xaxis' + (row === 1 ? '' : row)] = {{anchor:yax(row), showticklabels:false, matches:'x' + (n === 1 ? '' : n), showgrid:false}};
    row++;
  }}
  ps.forEach((p, k) => {{
    const i = row + k;
    p.traces.forEach(([name, y, color, mode]) => data.push(mode === 'bar'
      ? {{type:'bar', name, x:t, y, marker:{{color}}, xaxis:xax(i), yaxis:yax(i), width:3600e3}}
      : {{type:'scatter', name, x:t, y, mode, line:{{color, width:2}}, marker:{{size:4}}, connectgaps:false, xaxis:xax(i), yaxis:yax(i)}}));
    const h = haveCat ? (1 - 0.07) / ps.length : 1 / ps.length;
    const top = (haveCat ? 1 - 0.07 : 1) - k * h;
    const ya = {{domain:[top - h + 0.035, top], anchor:xax(i), gridcolor:'#1A2233', zerolinecolor:'#1A2233', title:{{text:p.title, standoff:6}}}};
    if (p.log) ya.type = 'log'; if (p.range) ya.range = p.range; if (p.rangemode) ya.rangemode = p.rangemode;
    layout['yaxis' + (i === 1 ? '' : i)] = ya;
    layout['xaxis' + (i === 1 ? '' : i)] = {{anchor:yax(i), gridcolor:'#1A2233', showticklabels: k === ps.length - 1, tickformat:'%HZ<br>%d', matches: k === ps.length - 1 ? undefined : xax(row + ps.length - 1)}};
    if (p.log) {{ [500, 1000, 3000].forEach(v => layout.shapes.push({{type:'line', xref:'paper', x0:0, x1:1, yref:yax(i), y0:v, y1:v, line:{{color:'#3A424C', width:1, dash:'dot'}}}})); }}
  }});
  const ct = cursorTime(st).replace('Z', '');
  layout.shapes.push({{type:'line', xref:'x' + (n === 1 ? '' : n), x0:ct, x1:ct, yref:'paper', y0:0, y1:1, line:{{color:'#FFD400', width:1.5}}}});
  Plotly.react(box, data, layout, {{displayModeBar:false, responsive:true}});
  box.removeAllListeners && box.removeAllListeners('plotly_click');
  box.on('plotly_click', ev => {{
    const k = ev.points[0].pointIndex; const h = Math.max(0, k);
    let best = 0; STEPS.forEach((s, i) => {{ if (Math.abs(s - h) < Math.abs(STEPS[best] - h)) best = i; }});
    ti = best; $('tm').value = ti; draw();
  }});
}}
function pick(icao) {{ sel = icao; $('mgsel').value = icao; drawMg(); }}
function fillSel() {{
  const ids = PT ? Object.keys(PT.stations).sort() : [];
  $('mgsel').innerHTML = '<option value="">station…</option>' + ids.map(i => `<option value="${{i}}">${{i}}</option>`).join('');
  if (sel) $('mgsel').value = sel;
}}
$('mgsel').onchange = e => pick(e.target.value);
$('b2').onclick = () => {{
  two = !two; $('b2').classList.toggle('on', two); document.body.classList.toggle('two', two);
  setTimeout(() => map.resize(), 30);
  if (two && !PT && PT_URL) fetch(PT_URL).then(r => r.json()).then(j => {{ PT = j; fillSel(); drawMg(); }}).catch(() => {{ $('mgn').textContent = 'station forecasts failed to load'; }});
  drawMg();
}};
map.on('load', () => {{
  map.addSource('st', {{type:'geojson', data:ST}});
  map.addLayer({{id:'st-dot', type:'circle', source:'st',
    paint:{{'circle-radius':['case', ['==', ['get','kind'], 'jbu'], 4, 3.5],
            'circle-color':['case', ['==', ['get','kind'], 'jbu'], '#4DA3FF', '#9AA0A6'],
            'circle-stroke-color':'#000', 'circle-stroke-width':1}}}});
  map.addLayer({{id:'st-lab', type:'symbol', source:'st',
    layout:{{'text-field':['get','label'], 'text-size':11, 'text-offset':[0.7, -0.6],
             'text-anchor':'bottom-left', 'text-font':['Open Sans Bold'], 'text-allow-overlap':false}},
    paint:{{'text-color':['case', ['==', ['get','kind'], 'jbu'], '#FFFFFF', '#B8B8B8'],
            'text-halo-color':'#000', 'text-halo-width':1.4}}}});
  map.on('click', 'st-dot', e => {{ const id = e.features[0].properties.id; if (!two) $('b2').onclick(); pick(id); }});
  map.on('mouseenter', 'st-dot', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'st-dot', () => map.getCanvas().style.cursor = '');
  ready = true; draw();
}});
$('tm').oninput = e => {{ ti = +e.target.value; draw(); }};
function stepBy(d) {{ ti = Math.max(0, Math.min(STEPS.length - 1, ti + d)); $('tm').value = ti; draw(); }}
$('bprev').onclick = () => stepBy(-1);
$('bnext').onclick = () => stepBy(1);
$('bplay').onclick = () => {{
  if (timer) {{ clearInterval(timer); timer = null; $('bplay').textContent = 'Play'; $('bplay').classList.remove('on'); return; }}
  $('bplay').textContent = 'Pause'; $('bplay').classList.add('on');
  timer = setInterval(() => {{ if (ti >= STEPS.length - 1) ti = -1; stepBy(1); }}, 700);
}};
document.addEventListener('keydown', e => {{ if (e.key === 'ArrowLeft') stepBy(-1); if (e.key === 'ArrowRight') stepBy(1); }});
$('bst').onclick = () => {{ $('bst').classList.toggle('on'); applyFilter(); }};
$('balt').onclick = () => {{ $('balt').classList.toggle('on'); applyFilter(); }};
function applyFilter() {{
  if (!ready) return;
  const kinds = []; if ($('bst').classList.contains('on')) kinds.push('jbu'); if ($('balt').classList.contains('on')) kinds.push('alt');
  ['st-dot','st-lab'].forEach(l => {{ map.setFilter(l, ['in', ['get','kind'], ['literal', kinds]]);
    map.setLayoutProperty(l, 'visibility', kinds.length ? 'visible' : 'none'); }});
}}
$('bfit').onclick = () => map.fitBounds(BB, {{padding:8}});
draw();
</script></body></html>"""
