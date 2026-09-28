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

28 SEP: seven fields, an hourly timeline to +24 h then 3-hourly to
+72 h (40 forecast steps), layers that stack, and ONE model - NextGen
(see MODELS). Budget against the 9,000/day cap, CONUS at zoom 4 (8
tiles):

    forecast pass   8 tiles x 40 steps x 7 fields = 2,240,
                    every TIO_FCST_MIN (480) min             -> 6,720/day
    "now" frame     8 tiles x 7 fields,
                    every TIO_NOW_MIN (60) min               -> 1,344/day
    station points  one /v4/timelines call per station (~79),
                    every TIO_PT_MIN (180) min               ->   632/day
                                                                ---------
                                                                 8,696/day

HIGH-RESOLUTION SECTOR: one sector at a time (a page control, like
the model), "now" frames at zoom TIO_HIRES_ZOOM (6 - a tile pixel is
~2.4 km there, which already resolves NextGen's grid; higher zooms
are tomorrow.io interpolating, and we interpolate for free) for the
fields in TIO_HIRES_FIELDS, every TIO_HIRES_MIN (15) min. The
stitched image is upscaled 2x bicubic and lightly blurred before it
is published, and the viewer shows it over the CONUS frame past zoom
5.5. Northeast at zoom 6 is 9 tiles: one field every 15 min is ~860
requests/day; all seven ~6,000, which needs the cap raised. No sector
selected = no spend. TIO_HIRES=off disables regardless.

NOAA POINT SERIES (no tomorrow.io spend): for the 14 compare-warmer
hubs the meteogram can overlay NBM ceiling / visibility / wind from
the Station Forecast frames and REFS P(CIG < 1000 ft) / P(VIS < 3 sm)
from core.refs_point, built into static/tio_noaa.json by this
warmer as each cycle lands.

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
    TIO_HIRES / TIO_SECTORS ("key:label:w,s,e,n|...") / TIO_HIRES_ZOOM /
    TIO_HIRES_FIELDS / TIO_HIRES_MIN
    TIO_NOAA=off         skip the NBM / REFS point series
    TIO_DEMO=on          synthetic tiles and station series, no tomorrow.io
                         calls, nothing charged - to exercise the page while
                         the plan is exhausted or without a key. Frames are
                         watermarked DEMO.
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
    "precipitationReflectivity,precipitationIntensity,thunderstormProbability,"
    "cloudCeiling,visibility,windSpeed,windGust"
).split(",") if f.strip()]
FIELD_LABEL = {
    "precipitationReflectivity": "Precipitation reflectivity",
    "precipitationIntensity": "Precipitation intensity",
    "thunderstormProbability": "Thunderstorm probability",
    "windDirection": "Wind direction",
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
FCST_MIN = int(os.environ.get("TIO_FCST_MIN", "480"))
NOW_ZOOM = int(os.environ.get("TIO_NOW_ZOOM", "4"))
FCST_ZOOM = int(os.environ.get("TIO_FCST_ZOOM", "4"))
DAILY_CAP = int(os.environ.get("TIO_DAILY_CAP", "9000"))
KEEP_FRAMES = 2          # per model/field/step, newest kept
PT_MIN = int(os.environ.get("TIO_PT_MIN", "180"))
POINTS_ON = os.environ.get("TIO_POINTS", "on").lower() != "off"
# Tile layer -> timelines field. The reflectivity tile has no point
# equivalent, so the meteogram shows precipitation intensity for it.
POINT_FIELD = {"precipitationReflectivity": "precipitationIntensity"}
# Extra point-only fields for the meteogram menu (no tile).
POINT_EXTRA = ["windDirection"]
PT_MAX_H = int(os.environ.get("TIO_FCST_MAX", "72"))


def point_fields() -> list:
    out = []
    for f in [POINT_FIELD.get(f, f) for f in FIELDS] + POINT_EXTRA:
        if f not in out:
            out.append(f)
    return out


# High-resolution sector "now" frames. "key:label:w,s,e,n|...".
HIRES_ON = os.environ.get("TIO_HIRES", "on").lower() != "off"
HIRES_ZOOM = int(os.environ.get("TIO_HIRES_ZOOM", "6"))
HIRES_MIN = int(os.environ.get("TIO_HIRES_MIN", "15"))
HIRES_FIELDS = [f.strip() for f in os.environ.get(
    "TIO_HIRES_FIELDS", "precipitationReflectivity").split(",") if f.strip()]


def _parse_sectors() -> dict:
    raw = os.environ.get(
        "TIO_SECTORS",
        "NE:Northeast:-80.5,38,-69,45.5|MA:Mid-Atlantic:-80,35.5,-72,40.5|"
        "FL:Florida:-84,24.2,-79,31|TX:Texas:-100,26,-93,33|"
        "WC:West Coast:-124.5,32.5,-116,41")
    out = {}
    for part in raw.split("|"):
        bits = part.split(":", 2)
        if len(bits) != 3:
            continue
        k, label, box = (b.strip() for b in bits)
        try:
            w, s_, e, n = [float(x) for x in box.split(",")]
            out[k] = (label, (w, s_, e, n))
        except ValueError:
            continue
    return out


SECTORS = _parse_sectors()
HIRES_HUBS = {k: v[1] for k, v in SECTORS.items()}


def _sector_path(outdir):
    return Path(outdir) / "tio_sector.txt"


def active_sector(outdir) -> str:
    """The one sector being warmed at high resolution, or ''."""
    try:
        k = _sector_path(outdir).read_text().strip()
        return k if k in SECTORS else ""
    except Exception:
        return ""


def set_sector(outdir, key: str) -> None:
    if key and key not in SECTORS:
        raise ValueError(key)
    p = _sector_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(key or "")
    os.replace(tmp, p)
    _persist(p)
NOAA_ON = os.environ.get("TIO_NOAA", "on").lower() != "off"
NOAA_MIN = int(os.environ.get("TIO_NOAA_MIN", "20"))

# PERSISTENCE (28 Sep, after a 429). static/ is inside the repo
# checkout, which Render replaces on every deploy - so every deploy
# restarted the warmer from nothing, re-fetched a full pass and reset
# the usage counter that was supposed to stop exactly that. Ten
# deploys in a day spent the plan. Everything the warmer writes is
# now mirrored under the persistent disk and restored into static/
# at start; the counter lives there too, so the cap holds across
# deploys and a restart continues from what it has.
_PERSIST_PARENT = Path(os.environ.get("TIO_PERSIST_PARENT",
                                      "/opt/render/project/src/cache"))
PERSIST = (_PERSIST_PARENT / "tio") if _PERSIST_PARENT.exists() else None
# After a 429 (rate limit or plan exhausted) every pass waits this
# long before trying tomorrow.io again, doubling on each repeat up
# to an hour. Nothing fetched during the wait is charged.
BACKOFF_S = int(os.environ.get("TIO_BACKOFF_S", "600"))
_backoff = {"until": 0.0, "n": 0}


class RateLimited(RuntimeError):
    pass


DEMO = os.environ.get("TIO_DEMO", "off").lower() == "on"


def _demo_tile(z, x, y, field, tstr) -> bytes:
    """A 256 px PNG that looks like a weather tile: a few soft blobs
    of the field's colour drifting east with the forecast hour, on a
    transparent background. Deterministic per tile and hour."""
    import hashlib
    import io as _io

    from PIL import Image, ImageDraw, ImageFilter

    try:
        hh = 0 if tstr == "now" else int(tstr[11:13]) + 24 * int(tstr[8:10])
    except ValueError:
        hh = 0
    col = {"precipitationReflectivity": (40, 220, 60), "precipitationIntensity": (30, 140, 255),
           "thunderstormProbability": (255, 200, 40), "cloudCeiling": (255, 80, 200),
           "visibility": (255, 140, 0), "windSpeed": (120, 200, 255),
           "windGust": (255, 60, 60)}.get(field, (200, 200, 200))
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # world-pixel origin of this tile, so blobs continue across tiles
    ox, oy = x * 256, y * 256
    rng = int(hashlib.md5(f"{field}{z}".encode()).hexdigest()[:8], 16)
    n_blobs = 14 * (4 ** max(0, z - 4))          # denser at higher zoom
    for i in range(n_blobs):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        bx = (rng % (256 * (2 ** z)))
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        by = (rng % (256 * (2 ** z)))
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        r = 18 + rng % 60
        bx = (bx + hh * 6) % (256 * (2 ** z))    # drift east
        px, py = bx - ox, by - oy
        if -r <= px <= 256 + r and -r <= py <= 256 + r:
            a = 90 + (i * 37) % 120
            d.ellipse([px - r, py - r, px + r, py + r], fill=col + (a,))
    im = im.filter(ImageFilter.GaussianBlur(radius=6))
    d = ImageDraw.Draw(im)
    d.text((6, 240), f"DEMO {field[:6]} {tstr[:13]}", fill=(255, 255, 255, 140))
    buf = _io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _demo_point(icao, lat, lon, fields) -> dict:
    """Plausible hourly series for the meteogram, deterministic per
    station, with a dip in ceiling and visibility mid-period."""
    import hashlib
    import math as _m

    seed = int(hashlib.md5(icao.encode()).hexdigest()[:6], 16)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    n = PT_MAX_H + 1
    t = [(now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in range(n)]
    out = {"t": t}
    dip = 8 + seed % 20
    for f in fields:
        vals = []
        for h in range(n):
            w = _m.exp(-((h - dip) / 5.0) ** 2)
            if f == "cloudCeiling":
                v = max(0.1, 3.0 - 2.85 * w); v = None if v > 2.9 else v
            elif f == "visibility":
                v = max(0.4, 16.0 - 15.0 * w)
            elif f == "windSpeed":
                v = 4 + 8 * w + 2 * _m.sin(h / 3.0)
            elif f == "windGust":
                v = 6 + 12 * w + 3 * _m.sin(h / 3.0)
            elif f == "windDirection":
                v = (40 + h * 5 + seed % 90) % 360
            elif f == "thunderstormProbability":
                v = max(0.0, 70 * w - 10)
            else:
                v = 4.0 * w if w > 0.15 else 0.0
            vals.append(None if v is None else round(v, 2))
        out[f] = vals
    return out


def _persist(path) -> None:
    """Mirror one file from static/ to the persistent disk."""
    if PERSIST is None:
        return
    try:
        import shutil
        PERSIST.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, PERSIST / Path(path).name)
    except Exception:
        pass


def _restore(outdir) -> int:
    """Copy anything on the persistent disk that static/ lacks (or
    has older) back into static/. Returns the count."""
    if PERSIST is None or not PERSIST.exists():
        return 0
    import shutil
    n = 0
    for p in PERSIST.glob("tio_*"):
        q = Path(outdir) / p.name
        try:
            if not q.exists() or q.stat().st_mtime < p.stat().st_mtime - 1:
                shutil.copy2(p, q)
                n += 1
        except Exception:
            pass
    return n


def _rate_limited(outdir, where: str) -> None:
    _backoff["n"] += 1
    wait = min(3600, BACKOFF_S * (2 ** (_backoff["n"] - 1)))
    _backoff["until"] = time.time() + wait
    STATUS["err"] = f"429 from tomorrow.io ({where}); waiting {wait // 60} min"
    _log(outdir, f"RATE LIMITED at {where}: backing off {wait // 60} min "
                 f"(day {usage(outdir)['count']}/{DAILY_CAP})")


def in_backoff() -> float:
    """Seconds left in the 429 back-off, 0 when clear."""
    return max(0.0, _backoff["until"] - time.time())


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
    _persist(p)


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


def tile_grid(z: int, box=None):
    """(x0, x1, y0, y1, bounds) for the domain (or `box` = w, s, e, n)
    at zoom z. bounds is [w, s, e, n] of the whole tile block - the
    image corners."""
    w, s_, e, n = box or (W, S, E, N)
    x0, x1 = _lon2x(w, z), _lon2x(e - 1e-9, z)
    y0, y1 = _lat2y(n, z), _lat2y(s_ + 1e-9, z)
    bounds = [_x2lon(x0, z), _y2lat(y1 + 1, z), _x2lon(x1 + 1, z), _y2lat(y0, z)]
    return x0, x1, y0, y1, bounds


def tile_count(z: int, box=None) -> int:
    x0, x1, y0, y1, _ = tile_grid(z, box)
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


def sector_estimate(key: str) -> int:
    """Requests/day one sector adds at the current settings."""
    if not (HIRES_ON and key in SECTORS):
        return 0
    return (tile_count(HIRES_ZOOM, SECTORS[key][1]) * len(HIRES_FIELDS)
            * (1440 // max(1, HIRES_MIN)))


def _fetch_tile(z, x, y, field, tstr, key, query=""):
    if DEMO:
        return _demo_tile(z, x, y, field, tstr)
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
            if r.status_code == 429:
                raise RateLimited(f"tile {z}/{x}/{y}: 429 {r.text[:80]}")
            if r.status_code in (400, 401, 403):
                break
        except RateLimited:
            raise
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
    _persist(p)
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
    _persist(p)


def build_frame(outdir, model: str, field: str, step: int, z: int,
                hub: str = None) -> dict:
    """Fetch and stitch one frame. step 0 = now; otherwise hours
    ahead, on the hour. `hub` = a HIRES_HUBS key: that box instead of
    CONUS, filed under manifest[model]["hires:<hub>"][field]. Returns
    the manifest entry."""
    from PIL import Image

    key = api_key() or ("demo" if DEMO else "")
    if not key:
        raise RuntimeError("no TOMORROWIO_API_KEY")
    outdir = Path(outdir)
    _label, query = MODELS[model]
    x0, x1, y0, y1, bounds = tile_grid(z, HIRES_HUBS[hub] if hub else None)
    now = datetime.now(timezone.utc)
    if step == 0:
        tstr, valid = "now", now
    else:
        valid = (now.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=step))
        tstr = valid.strftime("%Y-%m-%dT%H:%M:%SZ")
    n = (x1 - x0 + 1) * (y1 - y0 + 1)
    u = usage(outdir)
    if not DEMO:
        if u["count"] + n > DAILY_CAP:
            raise RuntimeError(f"daily cap: {u['count']}+{n} > {DAILY_CAP}")
        _add_usage(outdir, n)      # charged up front; a failed tile still counts
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
    if hub:
        # Past the tiles' native scale the browser would just upscale
        # the pixels; do it here once, bicubic 2x with a light blur,
        # so the close-up view is smooth rather than blocky.
        from PIL import ImageFilter
        img = img.resize((img.width * 2, img.height * 2), Image.BICUBIC)
        img = img.filter(ImageFilter.GaussianBlur(radius=1.2))
    stamp = now.strftime("%Y%m%d-%H%M")
    tag = f"{model}_{field}" if not hub else f"{model}_h{hub}_{field}"
    name = f"tio_{tag}_{step:02d}_{stamp}.webp"
    tmp = outdir / f".{name}.tmp"
    img.save(tmp, "WEBP", quality=88, method=4)
    os.replace(tmp, outdir / name)
    _persist(outdir / name)
    ent = {"name": name, "model": model, "field": field, "step": step,
           "zoom": z, "hub": hub or "",
           "valid": valid.strftime("%Y-%m-%dT%H:%MZ"),
           "built": now.strftime("%Y-%m-%dT%H:%MZ"),
           "bounds": bounds, "px": [img.width, img.height]}
    man = manifest(outdir)
    if hub:
        man.setdefault(model, {}).setdefault(f"hires:{hub}", {}).setdefault(field, {})[str(step)] = ent
    else:
        man.setdefault(model, {}).setdefault(field, {})[str(step)] = ent
    _save_manifest(outdir, man)
    # prune older frames of this model/field/step
    olds = sorted(outdir.glob(f"tio_{tag}_{step:02d}_*.webp"))
    for p in olds[:-KEEP_FRAMES]:
        try:
            p.unlink()
            if PERSIST is not None:
                (PERSIST / p.name).unlink(missing_ok=True)
        except Exception:
            pass
    _log(outdir, f"{model} {(hub + ' ') if hub else ''}{field} +{step:02d}h z{z} {n} tiles "
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
    if DEMO:
        return _demo_point(icao, lat, lon, fields)
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
    if r.status_code == 429:
        raise RateLimited(f"{icao}: 429 {r.text[:100]}")
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
    key = api_key() or ("demo" if DEMO else "")
    if not key:
        raise RuntimeError("no TOMORROWIO_API_KEY")
    outdir = Path(outdir)
    _label, query = MODELS[model]
    fields = point_fields()
    pts = _points(outdir)
    u = usage(outdir)
    if not DEMO:
        if u["count"] + len(pts) > DAILY_CAP:
            raise RuntimeError(f"daily cap: {u['count']}+{len(pts)} > {DAILY_CAP}")
        _add_usage(outdir, len(pts))
    t0 = time.time()
    got, bad = {}, []

    limited = []

    def one(p):
        if limited:
            return
        try:
            got[p[0]] = _fetch_point(p[0], p[1], p[2], fields, key, query)
        except RateLimited as exc:
            limited.append(str(exc))
        except Exception as exc:
            bad.append(f"{type(exc).__name__}: {exc}"[:100])
        time.sleep(0.25)          # timelines is the stricter endpoint
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(one, pts))
    if limited:
        _rate_limited(outdir, "points")
        if not got:
            raise RateLimited(limited[0])
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
    _persist(p)
    _log(outdir, f"{model} points {len(got)}/{len(pts)} stations "
                 f"{time.time() - t0:.1f}s"
                 + (f", {len(bad)} failed ({bad[0]})" if bad else "")
                 + f", day {usage(outdir)['count']}/{DAILY_CAP}")
    return len(got)


def _hires_pass(outdir, model):
    sec = active_sector(outdir)
    if not sec:
        return
    for hub in [sec]:
        for field in HIRES_FIELDS:
            try:
                build_frame(outdir, model, field, 0, HIRES_ZOOM, hub=hub)
            except RateLimited:
                _rate_limited(outdir, f"hires {hub} {field}")
                return
            except Exception as exc:
                STATUS["err"] = f"{type(exc).__name__}: {exc}"
                _log(outdir, f"FAILED {model} hires {hub} {field}: {STATUS['err']}")
                if "daily cap" in str(exc):
                    return


# ------------------------------------------------------------ NOAA points

def _noaa_path(outdir):
    return Path(outdir) / "tio_noaa.json"


def noaa(outdir) -> dict:
    try:
        return json.loads(_noaa_path(outdir).read_text())
    except Exception:
        return {}


def build_noaa(outdir) -> str:
    """NBM (from the compare-warmer frames) and REFS probabilities
    (core.refs_point) for the compare hubs, into tio_noaa.json. No
    tomorrow.io spend. Returns a one-line summary."""
    import math as _m

    from core import compare_warm as CW
    from core.hrrr_cam import latest_cycle
    from core import refs_point as RP

    cache_root = Path("/opt/render/project/src/cache")
    if not cache_root.exists():
        cache_root = Path("/tmp/wx_compare_cache")
    cyc = CW.latest_cycle(cache_root)
    doc = noaa(outdir)
    out = {"nbm_cycle": cyc, "refs_cycle": doc.get("refs_cycle"),
           "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "stations": doc.get("stations", {})}
    n_nbm = 0
    if cyc and cyc != doc.get("nbm_cycle"):
        for icao in CW.HUBS:
            got = CW.read_frame(cache_root, icao, cyc)
            if not got:
                continue
            df, _ = got
            dm = df[(df["model"] == "NBM") & (df["station_id"] == icao)].sort_values("valid_time")
            if not len(dm):
                continue
            sid = icao[1:] if icao[0] in "KCT" and len(icao) == 4 else icao

            def col(name):
                if name not in dm.columns:
                    return [None] * len(dm)
                return [None if (v is None or (isinstance(v, float) and _m.isnan(v))) else float(v)
                        for v in dm[name].tolist()]
            unl = [bool(u) for u in (dm["ceiling_unlimited"].tolist()
                                     if "ceiling_unlimited" in dm.columns else [False] * len(dm))]
            cig = [None if u else c for c, u in zip(col("ceiling_ft"), unl)]
            out["stations"].setdefault(sid, {})["nbm"] = {
                "t": [pd_ts.strftime("%Y-%m-%dT%H:%M:%SZ") for pd_ts in dm["valid_time"]],
                "cloudCeiling_ft": cig, "visibility_sm": col("vsby_sm"),
                "windSpeed_kt": col("wind_speed_kt"), "windGust_kt": col("wind_gust_kt"),
                "windDirection": col("wind_dir_deg")}
            n_nbm += 1
        out["nbm_cycle"] = cyc
    n_refs = 0
    try:
        rc = latest_cycle("refs_prob", 12)
    except Exception:
        rc = None
    if rc and rc.isoformat() != doc.get("refs_cycle"):
        hours = list(range(1, 37))
        for icao, la, lo in _points(outdir):
            full = ("K" + icao) if len(icao) == 3 else icao
            if full not in CW.HUBS:
                continue
            try:
                got = RP.sample(la, lo, rc, hours,
                                products=["PROB_CIG1000", "PROB_VIS3"], max_workers=6)
            except Exception:
                continue
            ts = [(rc + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in hours]
            out["stations"].setdefault(icao, {})["refs"] = {
                "t": ts, "p_cig1000": [got["PROB_CIG1000"].get(h) for h in hours],
                "p_vis3": [got["PROB_VIS3"].get(h) for h in hours]}
            n_refs += 1
        out["refs_cycle"] = rc.isoformat()
    p = _noaa_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    os.replace(tmp, p)
    _persist(p)
    msg = f"noaa: nbm {n_nbm} stations ({cyc}), refs {n_refs} stations ({out['refs_cycle']})"
    _log(outdir, msg)
    return msg


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
                _backoff["n"] = 0
            except RateLimited:
                _rate_limited(outdir, f"{field} +{step:02d}h")
                return
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
    restored = _restore(outdir)
    _log(outdir, f"warmer start: fields={FIELDS} steps={len(FCST_HOURS)} "
                 f"(to +{FCST_HOURS[-1] if FCST_HOURS else 0}h) models={list(MODELS)} "
                 f"now z{NOW_ZOOM}/{NOW_MIN}min fcst z{FCST_ZOOM}/{FCST_MIN}min "
                 f"~{daily_estimate()}/day, cap {DAILY_CAP}/day, "
                 f"{restored} files restored from {PERSIST or 'no persistent disk'}, "
                 f"day so far {usage(outdir)['count']}")
    next_now = next_fc = next_pt = next_hr = next_noaa = 0.0
    last_model = active_model(outdir)
    last_sector = None

    def _age_min(built: str) -> float:
        try:
            return (datetime.now(timezone.utc)
                    - datetime.strptime(built, "%Y-%m-%dT%H:%MZ").replace(
                        tzinfo=timezone.utc)).total_seconds() / 60.0
        except Exception:
            return 1e9

    # Resume: if the restored frames are younger than their cadence,
    # schedule the next pass from their age instead of refetching.
    man = manifest(outdir).get(last_model) or {}
    fc_ages = [_age_min(e["built"]) for f in FIELDS
               for st_, e in (man.get(f) or {}).items() if st_ != "0"]
    if fc_ages and len(fc_ages) >= len(FIELDS) * len(FCST_HOURS) * 0.9:
        next_fc = time.time() + max(0.0, FCST_MIN - max(fc_ages)) * 60
    now_ages = [_age_min(((man.get(f) or {}).get("0") or {}).get("built", ""))
                for f in FIELDS]
    if now_ages and max(now_ages) < NOW_MIN:
        next_now = time.time() + (NOW_MIN - max(now_ages)) * 60
    pt_age = _age_min(points(outdir, last_model).get("built", ""))
    if pt_age < PT_MIN:
        next_pt = time.time() + (PT_MIN - pt_age) * 60
    if next_fc or next_now or next_pt:
        _log(outdir, "resuming: next forecast pass in "
                     f"{max(0, next_fc - time.time()) / 60:.0f} min, now-frames in "
                     f"{max(0, next_now - time.time()) / 60:.0f}, points in "
                     f"{max(0, next_pt - time.time()) / 60:.0f}")

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
        if in_backoff():
            time.sleep(20)
            continue
        model = active_model(outdir)
        STATUS["model"] = model
        if model != last_model:
            # first pass, or the page switched model: full re-warm
            _log(outdir, f"model -> {model}: full pass")
            last_model = model
            next_fc = next_pt = next_hr = 0.0
        if t >= next_pt:
            # points first: 79 small calls, and the meteogram is
            # usable before the first forecast frames land
            _pts(model)
            next_pt = time.time() + PT_MIN * 60
        if NOAA_ON and t >= next_noaa:
            try:
                build_noaa(outdir)
            except Exception as exc:
                _log(outdir, f"FAILED noaa: {type(exc).__name__}: {exc}")
            next_noaa = time.time() + NOAA_MIN * 60
        sec = active_sector(outdir)
        if sec != last_sector:
            last_sector = sec
            next_hr = 0.0
        if HIRES_ON and sec and t >= next_hr:
            _hires_pass(outdir, model)
            next_hr = time.time() + HIRES_MIN * 60
        if t >= next_fc:
            _pass(outdir, model, [0] + FCST_HOURS)
            next_now = time.time() + NOW_MIN * 60
            # a 429 mid-pass: come back after the back-off, not in 8 h
            next_fc = time.time() + (FCST_MIN * 60 if not in_backoff() else 0)
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
             model: str = None, pt_built: str = "", noaa_built: str = "",
             sector: str = "") -> str:
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
                               "built": e["built"], "b": e["bounds"], "hub": ""})
    # High-resolution hub frames ("now" only): same field, drawn over
    # the CONUS frame once the map is zoomed in.
    for k, v in mman.items():
        if not k.startswith("hires:") or k[6:] != sector:
            continue
        for field, steps in v.items():
            for step, e in steps.items():
                frames.append({"url": f"{base}/app/static/{e['name']}?v={e['built']}",
                               "field": field, "step": int(step), "valid": e["valid"],
                               "built": e["built"], "b": e["bounds"], "hub": k[6:]})
    style = os.environ.get(
        "BLUEMET_MAP_STYLE",
        "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json")
    labels = {f: FIELD_LABEL.get(f, f) for f in FIELDS}
    pt_url = f"{base}/app/static/tio_pt_{model}.json?v={pt_built}" if pt_built else ""
    noaa_url = f"{base}/app/static/tio_noaa.json?v={noaa_built}" if noaa_built else ""
    # GENERAL WEATHER menu: one row per layer, checkbox + opacity.
    # Precipitation reflectivity on by default.
    rows = "".join(
        f'<div class="ly"><label><input type="checkbox" data-f="{f}"'
        f'{" checked" if i == 0 else ""}> {labels[f]}</label>'
        f'<input type="range" data-f="{f}" min="10" max="100" value="{85 if i == 0 else 70}"></div>'
        for i, f in enumerate(FIELDS))
    # METEOGRAM menu: the point fields, up to four at once, none by
    # default - the panel is empty until an element is picked.
    pf = point_fields()
    plabels = {f: FIELD_LABEL.get(f, f) for f in pf}
    mrows = "".join(
        f'<div class="ly"><label><input type="checkbox" class="mgf" data-f="{f}"> {plabels[f]}</label></div>'
        for f in pf)
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
 .dd{{position:relative;display:inline-block}}
 .dd > summary{{list-style:none;cursor:pointer;font:bold 12px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:6px;padding:5px 10px}}
 .dd > summary::-webkit-details-marker{{display:none}}
 .dd[open] > summary{{border-color:#00E5FF}}
 .dd .menu{{position:absolute;top:34px;left:0;z-index:20;background:#0A0A0A;border:1px solid #333;border-radius:8px;padding:6px;min-width:290px;display:flex;flex-direction:column;gap:4px;box-shadow:0 8px 24px rgba(0,0,0,.7)}}
 .ly{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:3px 8px;border:1px solid #222;border-radius:6px;background:#0A0A0A}}
 .ly input[type=range]{{width:70px}} .ly label{{color:#fff;cursor:pointer}}
 .ly.off label{{color:#6E6E6E}} .ly.none label{{color:#FF00C8}}
 #mgh .dd .menu{{min-width:240px}}
 .nb{{color:#B388FF}} .rf{{color:#00E5FF}}
 #tm{{flex:1;min-width:160px}}
 #valid{{color:#FFD400;min-width:250px}} #built{{color:#B8B8B8}} #mdl{{color:#00E5FF}}
 label{{color:#B8B8B8}} .sp{{flex:1}}
 .lg{{position:absolute;left:10px;bottom:24px;background:rgba(0,0,0,.85);border:1px solid #333;border-radius:6px;padding:8px 10px;font-size:11px;z-index:5}}
 .lg .d{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;border:1px solid #000}}
</style></head><body>
<div class="bar">
  <details class="dd" id="ddw"><summary id="ddws">General Weather &#9662;</summary><div class="menu">{rows}</div></details>
  <span class="sp"></span><span id="mdl">{model_label(model)}{" &middot; DEMO DATA" if DEMO else ""}</span>
  <button id="bst" class="on">Stations</button><button id="balt" class="on">CA alternates</button>
  <button id="bfit">Fit</button><button id="b2" title="Map + meteogram of the layers that are on, for a station you click">2-panel</button>
  <span style="color:#333">|</span>
  <button class="ov" data-o="routes" title="FAA ATS routes (J and Q) around ZNY / ZBW / ZOB / ZDC">Jet routes</button>
  <button class="ov" data-o="n90" title="N90 TRACON lateral boundary (FAA 2012 map; the Newark area went to PHL in 2024)">N90</button>
  <button class="ov" data-o="stars" title="JFK arrivals: CAMRN, LENDY/IGN, PARCH/ROBER, PWL, coloured by gate">JFK STARs</button>
  <button class="ov" data-o="fixes" title="N90 arrival / departure gates and coordination fixes">Fixes</button>
</div>
<div class="bar">
  <label>TIME</label><button id="bprev">&#9664;</button><button id="bplay">Play</button><button id="bnext">&#9654;</button>
  <input id="tm" type="range" min="0" max="{len(steps_all) - 1}" value="0" step="1">
  <span id="valid"></span><span id="built"></span>
</div>
<div id="wrap">
<div id="m"><div class="lg" id="lg"><span class="d" style="background:#4DA3FF"></span>JBU station &nbsp;
 <span class="d" style="background:#9AA0A6"></span>Canadian alternate<br>
 <span style="color:#6E6E6E">tomorrow.io {model_label(model)} tiles · CONUS{(" · " + SECTORS[sector][0] + " hi-res past zoom 5.5") if sector in SECTORS else ""} · hourly to +{int(os.environ.get("TIO_FCST_HOURLY_TO", "24"))} h, 3-hourly to +{FCST_HOURS[-1] if FCST_HOURS else 0} h</span></div></div>
<div id="mg">
  <div id="mgh"><label>STATION</label><select id="mgsel"><option value="">click a dot…</option></select>
    <details class="dd" id="ddm"><summary id="ddms">Elements &#9662;</summary><div class="menu">{mrows}
      <div style="color:#6E6E6E;font-weight:normal;padding:2px 8px">up to four &middot; NBM (purple) and REFS (cyan) join for ceiling, visibility and wind at the compare hubs</div></div></details>
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
const NOAA_URL = {json.dumps(noaa_url)};
const LABELS = {json.dumps({**labels, **plabels})};
const PT_FIELD = {json.dumps({f: POINT_FIELD.get(f, f) for f in FIELDS})};
const PFIELDS = {json.dumps(pf)};
const HIRES_MINZOOM = 5.5;
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
function key(f) {{ return 'tio_' + (f.hub ? f.hub + '_' : '') + f.field + '_' + f.step; }}
function frame(field, step) {{ return F.find(f => f.field === field && f.step === step && !f.hub); }}
function hubFrames(field, step) {{ return F.filter(f => f.field === field && f.step === step && f.hub); }}
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
  // 'wx-top' is an empty anchor layer added at load: weather sits under
  // it, the airspace overlays and station dots above it
  const before = map.getLayer('wx-top') ? 'wx-top' : (map.getLayer('st-dot') ? 'st-dot' : undefined);
  map.addLayer({{id:key(f), type:'raster', source:key(f), minzoom: f.hub ? HIRES_MINZOOM : 0,
    paint:{{'raster-opacity':0, 'raster-fade-duration':0, 'raster-resampling':'linear'}}}}, before);
  // re-order: later fields in FIELDS go UNDER earlier ones
  const shown = [...added].filter(k => map.getLayer(k));
  const fi = k => {{ const p = k.split('_'); const hub = p.length > 3; const f = p[hub ? 2 : 1]; return FIELDS.indexOf(f) * 2 + (hub ? 0 : 1); }};
  shown.sort((a, b) => fi(a) - fi(b));
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
    if (ready) {{ ensure(f); visible.add(key(f)); hubFrames(field, step).forEach(h => {{ ensure(h); visible.add(key(h)); }}); }}
    if (!valid) {{ valid = f.valid; built = f.built; }}
  }});
  if (ready) added.forEach(k => {{ if (map.getLayer(k)) {{ const p = k.split('_'); const fld = p[p.length > 3 ? 2 : 1]; map.setPaintProperty(k, 'raster-opacity', visible.has(k) ? op[fld] : 0); }} }});
  const nOn = FIELDS.filter(f => on[f]).length;
  $('ddws').innerHTML = 'General Weather' + (nOn ? ' <span style="color:#00E5FF">' + nOn + '</span>' : '') + ' &#9662;';
  $('valid').textContent = (step ? '+' + step + ' h' : 'now') + (valid ? '  valid ' + valid : '  (no frame yet)');
  $('built').textContent = built ? ' · built ' + built : '';
  if (two && PT && sel) drawMg();
}}
// ---- 2-panel meteogram -------------------------------------------------
// Point forecasts for every station (one tomorrow.io timelines call
// each, warmed server-side) drawn with plotly: one panel per layer
// that is switched on, in the map's layer order, with the TIME
// slider's hour as a cursor. Click a dot or pick from the list.
let PT = null, NOAA = null, two = false, sel = '';
const mgOn = {{}};
document.querySelectorAll('.mgf').forEach(c => c.onchange = () => {{
  const n = PFIELDS.filter(f => mgOn[f]).length;
  if (c.checked && n >= 4) {{ c.checked = false; return; }}
  mgOn[c.dataset.f] = c.checked;
  const m = PFIELDS.filter(f => mgOn[f]).length;
  $('ddms').innerHTML = 'Elements' + (m ? ' <span style="color:#00E5FF">' + m + '</span>' : '') + ' &#9662;';
  drawMg();
}});
const NOAA_FIELDS = new Set(['cloudCeiling', 'visibility', 'windSpeed', 'windDirection']);
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
// Panels for the elements picked in the meteogram menu (its own set,
// not the map's). Wind speed and gust share one. Where the station is
// a compare hub, NBM (purple) rides along on ceiling / visibility /
// wind, and REFS P(CIG<1000) / P(VIS<3) (cyan, right axis) on ceiling
// and visibility. A trace is [name, y, colour, mode, x?, opts?].
function panels(st) {{
  const nb = (NOAA && NOAA.stations[sel] && NOAA.stations[sel].nbm) || null;
  const rf = (NOAA && NOAA.stations[sel] && NOAA.stations[sel].refs) || null;
  const out = [], wind = [];
  PFIELDS.forEach(pf => {{
    if (!mgOn[pf]) return;
    const v = st[pf]; if (!v) return;
    if (pf === 'windSpeed' || pf === 'windGust') {{
      wind.push([pf, v.map(x => x == null ? null : x * KT)]);
      if (nb && pf === 'windSpeed') wind.push(['NBM wind', nb.windSpeed_kt, '#B388FF', nb.t]);
      if (nb && pf === 'windGust') wind.push(['NBM gust', nb.windGust_kt, '#B388FF', nb.t, 'dot']);
      return;
    }}
    if (pf === 'cloudCeiling') {{
      const tr = [[LABELS[pf], v.map(x => x == null ? null : Math.min(25000, x * FT)), '#4FA8E8', 'lines+markers']];
      if (nb) tr.push(['NBM ceiling', nb.cloudCeiling_ft, '#B388FF', 'lines', nb.t]);
      if (rf) tr.push(['REFS P(CIG<1000)', rf.p_cig1000, '#00E5FF', 'lines', rf.t, {{y2:true, dash:'dot'}}]);
      out.push({{title:'ceiling (ft)', log:true, traces:tr, y2: rf ? 'P %' : null}});
    }} else if (pf === 'visibility') {{
      const tr = [[LABELS[pf], v.map(x => x == null ? null : Math.min(10, x * SM)), '#FFFFFF', 'lines+markers']];
      if (nb) tr.push(['NBM vis', nb.visibility_sm, '#B388FF', 'lines', nb.t]);
      if (rf) tr.push(['REFS P(VIS<3)', rf.p_vis3, '#00E5FF', 'lines', rf.t, {{y2:true, dash:'dot'}}]);
      out.push({{title:'vis (sm)', range:[0, 10.5], traces:tr, y2: rf ? 'P %' : null}});
    }} else if (pf === 'windDirection') {{
      const tr = [[LABELS[pf], v, '#FFFFFF', 'markers']];
      if (nb) tr.push(['NBM dir', nb.windDirection, '#B388FF', 'markers', nb.t]);
      out.push({{title:'wind dir (\u00b0)', range:[0, 360], tick:90, traces:tr}});
    }} else if (pf === 'thunderstormProbability') {{
      out.push({{title:'tstorm (%)', range:[0, 100], traces:[[LABELS[pf], v, '#FFD400', 'lines+markers']]}});
    }} else {{
      out.push({{title: pf === 'precipitationIntensity' ? 'precip (mm/h)' : LABELS[pf], bar:true, traces:[[LABELS[pf], v, '#00E5FF', 'bar']]}});
    }}
  }});
  if (wind.length) {{
    const cols = {{windSpeed:'#FFFFFF', windGust:'#FF8A00'}};
    out.push({{title:'wind (kt)', rangemode:'tozero', traces: wind.map(w => w.length > 2 ? [w[0], w[1], w[2], 'lines', w[3], w[4] ? {{dash:w[4]}} : null] : [LABELS[w[0]], w[1], cols[w[0]], 'lines+markers'])}});
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
  if (!ps.length) {{ note.textContent = 'pick up to four elements from the Elements menu'; box.innerHTML = ''; return; }}
  note.textContent = '';
  $('mgt').textContent = sel; $('mgs').textContent = PT.model + ' · built ' + PT.built + 'Z';
  const t = st.t.map(x => x.replace('Z', ''));
  const cig = st.cloudCeiling, vis = st.visibility;
  const haveCat = cig && vis;
  const n = ps.length + (haveCat ? 1 : 0);
  const data = [], layout = {{paper_bgcolor:'#0A0A0A', plot_bgcolor:'#05070B', margin:{{l:52, r:14, t:8, b:34}},
    font:{{color:'#B8B8B8', size:11, family:'DejaVu Sans Mono, monospace'}}, showlegend:true, legend:{{orientation:'h', y:-0.06, font:{{size:9}}}}, hovermode:'x unified',
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
    p.traces.forEach(([name, y, color, mode, xs, o]) => {{
      const x = xs ? xs.map(v => v.replace('Z', '')) : t; o = o || {{}};
      if (mode === 'bar') data.push({{type:'bar', name, x, y, marker:{{color}}, xaxis:xax(i), yaxis:yax(i), width:3600e3}});
      else data.push({{type:'scatter', name, x, y, mode, line:{{color, width: name.startsWith('NBM') || name.startsWith('REFS') ? 1.5 : 2, dash:o.dash || 'solid'}},
        marker:{{size: mode === 'markers' ? 5 : 4}}, connectgaps:false, xaxis:xax(i), yaxis: o.y2 ? 'y' + (i + 50) : yax(i)}});
    }});
    const h = haveCat ? (1 - 0.07) / ps.length : 1 / ps.length;
    const top = (haveCat ? 1 - 0.07 : 1) - k * h;
    const ya = {{domain:[top - h + 0.035, top], anchor:xax(i), gridcolor:'#1A2233', zerolinecolor:'#1A2233', title:{{text:p.title, standoff:6}}}};
    if (p.log) ya.type = 'log'; if (p.range) ya.range = p.range; if (p.rangemode) ya.rangemode = p.rangemode; if (p.tick) ya.dtick = p.tick;
    layout['yaxis' + (i === 1 ? '' : i)] = ya;
    if (p.y2) layout['yaxis' + (i + 50)] = {{domain: ya.domain, anchor:xax(i), overlaying: yax(i), side:'right', range:[0, 100], showgrid:false, title:{{text:p.y2, standoff:4}}, tickfont:{{color:'#00E5FF'}}}};
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
  if (two && !NOAA && NOAA_URL) fetch(NOAA_URL).then(r => r.json()).then(j => {{ NOAA = j; drawMg(); }}).catch(() => {{}});
  drawMg();
}};
// ---- airspace overlays -------------------------------------------------
// Static JSON under /app/static (jet routes, N90 boundary, JFK STARs,
// N90 fixes), fetched the first time a button is pressed and kept.
// Drawn under the station dots, above the weather.
const OV_BASE = {json.dumps(base + "/app/static/")};
const ovOn = {{}}, ovLoaded = {{}};
function lineFC(items) {{ return {{type:'FeatureCollection', features: items.map(([coords, props]) => ({{type:'Feature', properties:props, geometry:{{type:'LineString', coordinates:coords}}}}))}}; }}
function ptFC(items) {{ return {{type:'FeatureCollection', features: items.map(([lon, lat, props]) => ({{type:'Feature', properties:props, geometry:{{type:'Point', coordinates:[lon, lat]}}}}))}}; }}
const OV = {{
  routes: {{ file:'n90_routes.json', build: j => {{
      map.addSource('ov-routes', {{type:'geojson', data: lineFC(j.routes.map(r => [r.path, {{ident:r.ident, rnav:r.type === 'RNAV'}}]))}});
      map.addLayer({{id:'ov-routes', type:'line', source:'ov-routes', paint:{{'line-color':['case', ['get','rnav'], '#22D3EE', '#9AA0A6'], 'line-width':1.1, 'line-opacity':0.85}}}}, 'st-dot');
      map.addLayer({{id:'ov-routes-lab', type:'symbol', source:'ov-routes', layout:{{'symbol-placement':'line', 'text-field':['get','ident'], 'text-size':10, 'text-font':['Open Sans Bold'], 'symbol-spacing':320}},
        paint:{{'text-color':['case', ['get','rnav'], '#22D3EE', '#C8CCD2'], 'text-halo-color':'#000', 'text-halo-width':1.2}}}}, 'st-dot');
      return ['ov-routes', 'ov-routes-lab']; }} }},
  n90: {{ file:'n90_boundary.json', build: j => {{
      const ring = j.polygon.concat([j.polygon[0]]);
      map.addSource('ov-n90', {{type:'geojson', data: lineFC([[ring, {{name:'N90'}}]])}});
      map.addLayer({{id:'ov-n90', type:'line', source:'ov-n90', paint:{{'line-color':'#FF2A2A', 'line-width':2, 'line-dasharray':[3, 2]}}}}, 'st-dot');
      return ['ov-n90']; }} }},
  stars: {{ file:'jfk_stars.json', build: j => {{
      const legs = [];
      j.stars.forEach(s => s.legs.forEach(l => legs.push([l.path, {{star:s.star, col:'rgb(' + s.col.join(',') + ')', dashed:!!l.dashed}}])));
      map.addSource('ov-stars', {{type:'geojson', data: lineFC(legs)}});
      map.addLayer({{id:'ov-stars', type:'line', source:'ov-stars', filter:['!', ['get','dashed']], paint:{{'line-color':['get','col'], 'line-width':2}}}}, 'st-dot');
      map.addLayer({{id:'ov-stars-d', type:'line', source:'ov-stars', filter:['get','dashed'], paint:{{'line-color':['get','col'], 'line-width':1.5, 'line-dasharray':[2, 2]}}}}, 'st-dot');
      map.addLayer({{id:'ov-stars-lab', type:'symbol', source:'ov-stars', layout:{{'symbol-placement':'line', 'text-field':['get','star'], 'text-size':10, 'text-font':['Open Sans Bold'], 'symbol-spacing':260}},
        paint:{{'text-color':['get','col'], 'text-halo-color':'#000', 'text-halo-width':1.2}}}}, 'st-dot');
      const fx = Object.entries(j.fixes || {{}}).map(([n, p]) => [p.lon, p.lat, {{name:n}}]);
      map.addSource('ov-stars-fx', {{type:'geojson', data: ptFC(fx)}});
      map.addLayer({{id:'ov-stars-fx', type:'symbol', source:'ov-stars-fx', minzoom:6, layout:{{'text-field':['get','name'], 'text-size':9, 'text-font':['Open Sans Bold'], 'text-offset':[0, -0.9]}},
        paint:{{'text-color':'#E8E8E8', 'text-halo-color':'#000', 'text-halo-width':1}}}}, 'st-dot');
      return ['ov-stars', 'ov-stars-d', 'ov-stars-lab', 'ov-stars-fx']; }} }},
  fixes: {{ file:'n90_fixes.json', build: j => {{
      const pts = j.fixes.map(f => [f.lon, f.lat, {{name:f.name, role:f.role}}]);
      map.addSource('ov-fixes', {{type:'geojson', data: ptFC(pts)}});
      map.addLayer({{id:'ov-fixes', type:'circle', source:'ov-fixes', paint:{{'circle-radius':3,
        'circle-color':['match', ['get','role'], 'dep', '#3DDC84', 'both', '#FFD400', '#9AA0A6'], 'circle-stroke-color':'#000', 'circle-stroke-width':1}}}}, 'st-dot');
      map.addLayer({{id:'ov-fixes-lab', type:'symbol', source:'ov-fixes', minzoom:5.5, layout:{{'text-field':['get','name'], 'text-size':9, 'text-font':['Open Sans Bold'], 'text-offset':[0.6, -0.5], 'text-anchor':'bottom-left'}},
        paint:{{'text-color':['match', ['get','role'], 'dep', '#3DDC84', 'both', '#FFD400', '#B8B8B8'], 'text-halo-color':'#000', 'text-halo-width':1}}}}, 'st-dot');
      return ['ov-fixes', 'ov-fixes-lab']; }} }},
}};
function ovSet(k, show) {{
  (ovLoaded[k] || []).forEach(id => map.getLayer(id) && map.setLayoutProperty(id, 'visibility', show ? 'visible' : 'none'));
}}
document.querySelectorAll('.ov').forEach(b => b.onclick = () => {{
  const k = b.dataset.o; ovOn[k] = !ovOn[k]; b.classList.toggle('on', ovOn[k]);
  if (!ready) return;
  if (ovLoaded[k]) {{ ovSet(k, ovOn[k]); return; }}
  if (!ovOn[k]) return;
  fetch(OV_BASE + OV[k].file).then(r => r.json()).then(j => {{ ovLoaded[k] = OV[k].build(j); ovSet(k, ovOn[k]); }})
    .catch(() => {{ b.classList.remove('on'); ovOn[k] = false; }});
}});
map.on('load', () => {{
  map.addSource('wx-top', {{type:'geojson', data:{{type:'FeatureCollection', features:[]}}}});
  map.addLayer({{id:'wx-top', type:'line', source:'wx-top'}});
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
document.addEventListener('click', e => {{ document.querySelectorAll('details.dd[open]').forEach(d => {{ if (!d.contains(e.target)) d.removeAttribute('open'); }}); }});
draw();
</script></body></html>"""
