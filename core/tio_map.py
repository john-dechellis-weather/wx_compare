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
(see MODELS). Budget against the 9,000/day cap, CONUS at zoom 4 (8 tiles). DEMAND
MODE (28 Sep): the warmer no longer walks the whole forecast for
every layer. It keeps the layers people have switched on warm (the
first field's now-frame every TIO_NOW_FAST_MIN, other active layers
every TIO_NOW_SLOW_MIN, the next TIO_PREFETCH_H hours every
TIO_FCST_MIN) and fetches any other (layer, hour) the moment the
slider asks for it - 8 tiles, about a second, then cached. Station
meteograms are fetched for the clicked station only; the hi-res
sector only while the map is zoomed into it. A heavy day with two
layers in use and forty frames scrubbed to is ~1,700 requests; a
quiet one a few hundred.

BUDGET RULES: nothing runs while nobody has the page open
(TIO_IDLE_MIN; the first field's now-frame drops to every
TIO_NOW_IDLE_MIN); every non-now request must leave TIO_RESERVE plus
the rest of the day's now-frames untouched; no clock hour may spend
more than TIO_HOURLY_CAP on non-now work; frames and the counter
persist outside the checkout so a restart resumes rather than
refetches. ONE running instance may hold the API key - two (Render
and the Mac) double every number above.

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
    three_to = int(os.environ.get("TIO_FCST_3H_TO", "36"))
    step = max(1, int(os.environ.get("TIO_FCST_STEP", "6")))
    mx = int(os.environ.get("TIO_FCST_MAX", "72"))
    out = list(range(1, min(hourly_to, mx) + 1))
    h = hourly_to + 3
    while h <= min(three_to, mx):
        out.append(h)
        h += 3
    h = (out[-1] if out else 0) + step
    while h <= mx:
        out.append(h)
        h += step
    return out


# Fields whose forecast is worth fewer steps (BUDGET, 28 Sep): these
# get 3-hourly to +24 and 6-hourly beyond, the rest the full list.
# Now-frames are unaffected - every field is current every hour.
FCST_LIGHT = set(f.strip() for f in os.environ.get(
    "TIO_FCST_LIGHT", "thunderstormProbability,windSpeed").split(",") if f.strip())


def steps_for(field: str) -> list:
    """Forecast steps for one field (see FCST_LIGHT)."""
    if field not in FCST_LIGHT:
        return FCST_HOURS
    return [h for h in FCST_HOURS if (h <= 24 and h % 3 == 0) or h % 6 == 0]


FCST_HOURS = _steps()
NOW_MIN = int(os.environ.get("TIO_NOW_MIN", "60"))
FCST_MIN = int(os.environ.get("TIO_FCST_MIN", "360"))
NOW_ZOOM = int(os.environ.get("TIO_NOW_ZOOM", "4"))
FCST_ZOOM = int(os.environ.get("TIO_FCST_ZOOM", "4"))
# The plan: 10,000 requests/day, 9,999/hour, 50/s. This app's own
# ceiling is TIO_CAP_PCT of the daily plan (85 % by 1 Oct request) so
# anything else on the same key - other scripts, notebooks, the
# dashboard's own test calls - has the rest. TIO_DAILY_CAP overrides.
PLAN_DAY = int(os.environ.get("TIO_PLAN_DAY", "10000"))
CAP_PCT = float(os.environ.get("TIO_CAP_PCT", "85"))
DAILY_CAP = int(os.environ.get("TIO_DAILY_CAP", str(int(PLAN_DAY * CAP_PCT / 100))))

# BUDGET (28 Sep, after the plan was spent by noon). Three rules on
# top of the daily cap, all in _charge():
#   RESERVE       requests kept back for the rest of the day for
#                 anything that is not a now-frame: a restart, a model
#                 switch, a sector someone opens at 11 pm. A pass that
#                 would eat into it is deferred, not run.
#   mandatory     the hourly now-frames for every remaining hour of
#                 the UTC day are reserved too, so a burst early in
#                 the day cannot leave the evening with a stale map.
#   HOURLY_CAP    smoothing: no more than this in any one clock hour
#                 for non-mandatory work. A restart or model switch
#                 spreads over hours instead of landing at once.
# Priority 0 = now-frames (only the hard cap applies), 1 = points and
# forecast to +24 h, 2 = forecast beyond +24 h and hi-res sectors.
RESERVE = int(os.environ.get("TIO_RESERVE", "1000"))
# tomorrow.io's daily bucket resets at 00Z (a 429 at 06:58Z came back
# with Retry-After 1022 min = 00:00Z). The usage day here starts at the
# same hour so the counter and the plan reset together;
# TIO_DAY_RESET_UTC_HOUR moves it if the plan ever changes.
DAY_RESET_UTC_HOUR = int(os.environ.get("TIO_DAY_RESET_UTC_HOUR", "0"))
# TIO_PAUSE_UNTIL="2026-09-29T04:00Z": no tomorrow.io request of any
# kind before then - the warmer idles and the page says so. Lets a
# spent plan sit untouched until it resets, whatever restarts happen.
PAUSE_UNTIL = os.environ.get("TIO_PAUSE_UNTIL", "").strip()


def pause_left_s() -> float:
    """Seconds until TIO_PAUSE_UNTIL, 0 when unset or past."""
    if not PAUSE_UNTIL:
        return 0.0
    try:
        t = datetime.strptime(PAUSE_UNTIL.replace("Z", ""), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            t = datetime.strptime(PAUSE_UNTIL.replace("Z", ""), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
    return max(0.0, (t - datetime.now(timezone.utc)).total_seconds())
HOURLY_CAP = int(os.environ.get("TIO_HOURLY_CAP", "1500"))
# VIEWER GATING: the page writes a heartbeat while it is open
# (note_view). With nobody looking for IDLE_MIN, forecast passes,
# points and the hi-res sector stop, and now-frames slow to
# NOW_IDLE_MIN so the map still opens on something recent. The first
# view after an idle spell runs whatever is overdue at once.
# Forecast frames beyond +24 h are refreshed only when older than
# FAR_MIN; a forecast pass every FCST_MIN otherwise stops at +24 h.
FAR_MIN = int(os.environ.get("TIO_FAR_MIN", "720"))
# DEMAND MODE (28 Sep): the viewer reports which layers are on, the
# slider's step, the map zoom and the station picked (via the bridge
# component on the page -> set_demand). The warmer keeps only the
# ACTIVE layers warm (any layer someone switched on in the last
# ACTIVE_MIN; the first field when nobody has), fetches any other
# (layer, step) the moment it is asked for, and prefetches the next
# PREFETCH_H hours of the active layers. Now-frames: the first field
# every NOW_FAST_MIN, other active fields every NOW_SLOW_MIN.
ACTIVE_MIN = int(os.environ.get("TIO_ACTIVE_MIN", "120"))
PREFETCH_H = int(os.environ.get("TIO_PREFETCH_H", "6"))
NOW_FAST_MIN = int(os.environ.get("TIO_NOW_FAST_MIN", "30"))
NOW_SLOW_MIN = int(os.environ.get("TIO_NOW_SLOW_MIN", "180"))
PT_STALE_MIN = int(os.environ.get("TIO_PT_STALE_MIN", "60"))
# Points: "demand" = the clicked station only; "all" = every station
# every PT_MIN (the old way); "off".
POINTS_MODE = os.environ.get("TIO_POINTS", "demand").lower()
HIRES_ZOOM_GATE = float(os.environ.get("TIO_HIRES_ZOOM_GATE", "5.5"))
IDLE_MIN = int(os.environ.get("TIO_IDLE_MIN", "120"))
NOW_IDLE_MIN = int(os.environ.get("TIO_NOW_IDLE_MIN", "180"))
KEEP_FRAMES = 2          # per model/field/step, newest kept
PT_MIN = int(os.environ.get("TIO_PT_MIN", "360"))
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
HIRES_MIN = int(os.environ.get("TIO_HIRES_MIN", "30"))
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
if not _PERSIST_PARENT.exists():
    # Not on Render: keep the mirror outside the git checkout so a
    # checkout, pull or reclone never resets the counter or the frames.
    _PERSIST_PARENT = Path.home() / ".bluemet_cache"
    try:
        _PERSIST_PARENT.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
PERSIST = (_PERSIST_PARENT / "tio") if _PERSIST_PARENT.exists() else None
# After a 429 (rate limit or plan exhausted) every pass waits this
# long before trying tomorrow.io again, doubling on each repeat up
# to an hour. Nothing fetched during the wait is charged.
BACKOFF_S = int(os.environ.get("TIO_BACKOFF_S", "600"))
_backoff = {"until": 0.0, "n": 0}


class RateLimited(RuntimeError):
    """A 429. retry_after: seconds until the bucket refills, from
    the response's Retry-After header when tomorrow.io sends one (it
    does: the daily bucket says exactly when it resets)."""

    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def _retry_after(r):
    try:
        return int(float(r.headers.get("retry-after") or
                         r.headers.get("ratelimit-reset") or 0)) or None
    except (TypeError, ValueError):
        return None


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


def _rate_limited(outdir, where: str, retry_after=None) -> None:
    _backoff["n"] += 1
    if retry_after:
        # the API said when: sleep to the reset (+1 min), not a guess
        wait = min(26 * 3600, int(retry_after) + 60)
    else:
        wait = min(3600, BACKOFF_S * (2 ** (_backoff["n"] - 1)))
    _backoff["until"] = time.time() + wait
    STATUS["err"] = (f"429 from tomorrow.io ({where}); the daily quota resets in "
                     f"{wait // 3600} h {(wait % 3600) // 60} min" if retry_after and wait > 3600
                     else f"429 from tomorrow.io ({where}); waiting {wait // 60} min")
    _log(outdir, f"RATE LIMITED at {where}: waiting {wait // 60} min"
                 + (" (Retry-After)" if retry_after else "")
                 + f" (day {usage(outdir)['count']}/{DAILY_CAP})")


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


def daily_estimate(n_active: int = 2, demanded_steps: int = 40) -> int:
    """Requests/day for one model with `n_active` layers in use all
    day and `demanded_steps` (layer, step) frames scrubbed to."""
    t = tile_count(NOW_ZOOM)
    now = t * ((1440 // NOW_FAST_MIN) + (n_active - 1) * (1440 // NOW_SLOW_MIN))
    pre = t * n_active * PREFETCH_H * (1440 // FCST_MIN)
    dem = t * demanded_steps
    pts = 40 if POINTS_MODE == "demand" else (79 * (1440 // PT_MIN) if POINTS_MODE == "all" else 0)
    return now + pre + dem + pts


def _daily_estimate_old() -> int:
    nf = len(FIELDS)
    near = sum(len([h for h in steps_for(f) if h <= 24]) for f in FIELDS)
    far = sum(len([h for h in steps_for(f) if h > 24]) for f in FIELDS)
    fc = tile_count(FCST_ZOOM) * (near * (1440 // max(1, FCST_MIN))
                                  + far * (1440 // max(1, FAR_MIN)))
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
    if pause_left_s() > 0:
        raise Deferred(f"paused until {PAUSE_UNTIL}")
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
                raise RateLimited(f"tile {z}/{x}/{y}: 429 {r.text[:80]}", _retry_after(r))
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
    """{"day", "count", "hour", "hcount"}: the UTC day's spend and the
    current clock hour's."""
    try:
        d = json.loads(_usage_path(outdir).read_text())
    except Exception:
        d = {}
    now = datetime.now(timezone.utc)
    today = (now - timedelta(hours=DAY_RESET_UTC_HOUR)).strftime("%Y-%m-%d")
    hour = now.strftime("%Y-%m-%dT%H")
    same_day = d.get("day") == today
    return {"day": today,
            "count": int(d.get("count", 0)) if same_day else 0,
            "hour": hour,
            "hcount": int(d.get("hcount", 0)) if d.get("hour") == hour else 0,
            # who spent it: {"now": n, "forecast": n, "hires": n,
            # "points": n, "noaa": n} for the day (1 Oct)
            "by": dict(d.get("by") or {}) if same_day else {}}


def _category(what: str) -> str:
    w = (what or "").lower()
    if w.startswith("point"):
        return "points"
    if w.startswith("noaa"):
        return "noaa"
    if "+00h" in w:
        return "hires" if len(w.split()) > 2 else "now"
    return "hires" if len(w.split()) > 2 else "forecast"


def _add_usage(outdir, n, what: str = ""):
    u = usage(outdir)
    u["count"] += n
    u["hcount"] += n
    c = _category(what)
    u["by"][c] = int(u["by"].get(c, 0)) + n
    p = _usage_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(u))
    os.replace(tmp, p)
    _persist(p)
    return u["count"]


class Deferred(RuntimeError):
    """The budget said not now (not a failure: the pass retries on
    its next tick)."""


def mandatory_left() -> int:
    """Now-frame requests still owed for the rest of the UTC day."""
    now = datetime.now(timezone.utc) - timedelta(hours=DAY_RESET_UTC_HOUR)
    hours_left = 24 - now.hour
    return tile_count(NOW_ZOOM) * len(FIELDS) * max(1, hours_left * 60 // max(1, NOW_MIN))


def budget(outdir) -> dict:
    """What is left today, for the page and the log."""
    u = usage(outdir)
    left = DAILY_CAP - u["count"]
    return {"used": u["count"], "cap": DAILY_CAP, "left": left, "by": u["by"],
            "mandatory_left": mandatory_left(), "reserve": RESERVE,
            "free": max(0, left - mandatory_left() - RESERVE),
            "hour_used": u["hcount"], "hour_cap": HOURLY_CAP,
            "idle_min": idle_min(outdir)}


def _charge(outdir, n: int, prio: int, what: str = "") -> int:
    """Check the budget and charge `n` requests, or raise. Priority 0
    only respects the hard cap; 1 and 2 also keep the reserve, the
    mandatory now-frames and the hourly cap."""
    if DEMO:
        return 0
    if pause_left_s() > 0:
        raise Deferred(f"paused until {PAUSE_UNTIL} (TIO_PAUSE_UNTIL)")
    u = usage(outdir)
    if u["count"] + n > DAILY_CAP:
        raise RuntimeError(f"daily cap: {u['count']}+{n} > {DAILY_CAP}")
    if prio >= 1:
        keep = mandatory_left() + RESERVE
        if u["count"] + n > DAILY_CAP - keep:
            raise Deferred(f"{what}: {n} would cut into the reserve "
                           f"({u['count']}+{n} > {DAILY_CAP}-{keep})")
        if u["hcount"] + n > HOURLY_CAP:
            raise Deferred(f"{what}: hourly cap ({u['hcount']}+{n} > {HOURLY_CAP})")
    return _add_usage(outdir, n, what)


def _view_path(outdir):
    return Path(outdir) / "tio_last_view.txt"


_last_note = {"t": 0.0}


def note_view(outdir) -> None:
    """Called by the page on every render (its fragment reruns every
    60 s while open): the warmer's proof that someone is looking."""
    t = time.time()
    if t - _last_note["t"] < 30:
        return
    _last_note["t"] = t
    try:
        _view_path(outdir).write_text(str(int(t)))
    except Exception:
        pass


def idle_min(outdir) -> float:
    """Minutes since the page was last open; large when never."""
    try:
        return (time.time() - float(_view_path(outdir).read_text().strip())) / 60.0
    except Exception:
        return 1e6


def is_idle(outdir) -> bool:
    return idle_min(outdir) > IDLE_MIN


def _demand_path(outdir):
    return Path(outdir) / "tio_demand.json"


def _active_path(outdir):
    return Path(outdir) / "tio_active.json"


def set_demand(outdir, d: dict) -> None:
    """From the page: what the viewer is looking at right now."""
    if not isinstance(d, dict):
        return
    now = time.time()
    fields = [f for f in (d.get("fields") or [])
              if f in FIELDS or (":" in f and len(f) < 40)]   # "refs:PMMN" etc. (core.model_tiles)
    doc = {"fields": fields, "step": int(d.get("step") or 0),
           "zoom": float(d.get("zoom") or 0), "station": str(d.get("station") or ""),
           "playing": bool(d.get("playing")), "t": now}
    try:
        p = _demand_path(outdir)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        os.replace(tmp, p)
        # remember every layer anyone switched on, with when
        try:
            act = json.loads(_active_path(outdir).read_text())
        except Exception:
            act = {}
        for f in fields:
            act[f] = now
        act = {k: v for k, v in act.items() if now - v < 86400}
        tmp = _active_path(outdir).with_suffix(".tmp")
        tmp.write_text(json.dumps(act))
        os.replace(tmp, _active_path(outdir))
    except Exception:
        pass
    note_view(outdir)


def demand(outdir, max_age_s: int = 900) -> dict:
    """The latest demand, or {} when it is older than max_age_s."""
    try:
        d = json.loads(_demand_path(outdir).read_text())
        if time.time() - float(d.get("t", 0)) > max_age_s:
            return {}
        return d
    except Exception:
        return {}


def active_fields_all(outdir) -> list:
    """Every layer key (tomorrow.io and model) anyone switched on in
    the last ACTIVE_MIN, in the order first seen."""
    try:
        act = json.loads(_active_path(outdir).read_text())
    except Exception:
        act = {}
    now = time.time()
    return [f for f, t in act.items() if now - float(t) < ACTIVE_MIN * 60]


def active_fields(outdir) -> list:
    """Layers switched on by anyone in the last ACTIVE_MIN, in FIELDS
    order; the first field when nobody has asked for anything."""
    try:
        act = json.loads(_active_path(outdir).read_text())
    except Exception:
        act = {}
    now = time.time()
    out = [f for f in FIELDS if now - float(act.get(f, 0)) < ACTIVE_MIN * 60]
    return out or FIELDS[:1]


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
    prio = 2 if (hub or step > 24) else (1 if step else 0)
    # charged up front; a failed tile still counts
    _charge(outdir, n, prio, f"{field} +{step:02d}h" + (f" {hub}" if hub else ""))
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
    if pause_left_s() > 0:
        raise Deferred(f"paused until {PAUSE_UNTIL}")
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
        raise RateLimited(f"{icao}: 429 {r.text[:100]}", _retry_after(r))
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
    _charge(outdir, len(pts), 1, "points")
    t0 = time.time()
    got, bad = {}, []

    limited = []

    def one(p):
        if limited:
            return
        try:
            got[p[0]] = _fetch_point(p[0], p[1], p[2], fields, key, query)
        except RateLimited as exc:
            limited.append(exc)
        except Exception as exc:
            bad.append(f"{type(exc).__name__}: {exc}"[:100])
        time.sleep(0.25)          # timelines is the stricter endpoint
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(one, pts))
    if limited:
        _rate_limited(outdir, "points", limited[0].retry_after)
        if not got:
            raise limited[0]
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


def build_point_one(outdir, model, icao: str) -> bool:
    """One station's timeline, merged into the points file with its
    own built time. Charged one request."""
    key = api_key() or ("demo" if DEMO else "")
    if not key:
        raise RuntimeError("no TOMORROWIO_API_KEY")
    pts = {p[0]: p for p in _points(outdir)}
    if icao not in pts:
        return False
    _label, query = MODELS[model]
    fields = point_fields()
    _charge(outdir, 1, 1, f"point {icao}")
    _ic, lat, lon = pts[icao]
    got = _fetch_point(icao, lat, lon, fields, key, query)
    doc = points(outdir, model) or {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    doc.update({"model": model, "fields": fields,
                "units": {"windSpeed": "m/s", "windGust": "m/s", "visibility": "km",
                          "cloudCeiling": "km", "precipitationIntensity": "mm/h"}})
    doc.setdefault("stations", {})[icao] = got
    doc.setdefault("built_by", {})[icao] = now
    doc["built"] = now
    p = _pt_path(outdir, model)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)
    _persist(p)
    _log(outdir, f"{model} point {icao} on demand, day {usage(outdir)['count']}/{DAILY_CAP}")
    return True


def _point_age_min(outdir, model, icao) -> float:
    try:
        b = points(outdir, model)["built_by"][icao]
        return (datetime.now(timezone.utc)
                - datetime.strptime(b, "%Y-%m-%dT%H:%MZ").replace(
                    tzinfo=timezone.utc)).total_seconds() / 60.0
    except Exception:
        return 1e9


def _hires_age_min(outdir, model, hub, field) -> float:
    try:
        e = manifest(outdir)[model][f"hires:{hub}"][field]["0"]
        return (datetime.now(timezone.utc)
                - datetime.strptime(e["built"], "%Y-%m-%dT%H:%MZ").replace(
                    tzinfo=timezone.utc)).total_seconds() / 60.0
    except Exception:
        return 1e9


def _hires_pass(outdir, model):
    sec = active_sector(outdir)
    if not sec:
        return
    act = set(active_fields(outdir))
    for hub in [sec]:
        for field in [f for f in HIRES_FIELDS if f in act] or HIRES_FIELDS[:1]:
            if _hires_age_min(outdir, model, hub, field) < HIRES_MIN:
                continue
            try:
                build_frame(outdir, model, field, 0, HIRES_ZOOM, hub=hub)
            except RateLimited as rl:
                _rate_limited(outdir, f"hires {hub} {field}", rl.retry_after)
                return
            except Deferred as d:
                _log(outdir, f"DEFERRED hires {hub}: {d}")
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
    deferred = 0
    for step in steps:
        for field in FIELDS:
            if step and step not in steps_for(field):
                continue
            if step > 24 and _frame_age_min(outdir, model, field, step) < FAR_MIN:
                continue
            try:
                build_frame(outdir, model, field, step,
                            NOW_ZOOM if step == 0 else FCST_ZOOM)
                STATUS["last"] = time.time()
                STATUS["err"] = None
                _backoff["n"] = 0
            except RateLimited as rl:
                _rate_limited(outdir, f"{field} +{step:02d}h", rl.retry_after)
                return
            except Deferred as d:
                # budget says wait: the near hours already landed, the
                # rest comes on a later tick; log once per pass
                if not deferred:
                    _log(outdir, f"DEFERRED {model}: {d}")
                deferred += 1
                STATUS["err"] = None
                return False
            except Exception as exc:
                STATUS["err"] = f"{type(exc).__name__}: {exc}"
                _log(outdir, f"FAILED {model} {field} +{step:02d}h: {STATUS['err']}")
                if "daily cap" in str(exc) or "no TOMORROWIO" in str(exc):
                    return False
    return True


def _frame_age_min(outdir, model, field, step) -> float:
    """Minutes since this frame was built; huge when it does not exist."""
    try:
        e = manifest(outdir)[model][field][str(step)]
        return (datetime.now(timezone.utc)
                - datetime.strptime(e["built"], "%Y-%m-%dT%H:%MZ").replace(
                    tzinfo=timezone.utc)).total_seconds() / 60.0
    except Exception:
        return 1e9


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
            return True
        try:
            build_points(outdir, model)
        except Deferred as d:
            _log(outdir, f"DEFERRED {model} points: {d}")
            return False
        except Exception as exc:
            STATUS["err"] = f"{type(exc).__name__}: {exc}"
            _log(outdir, f"FAILED {model} points: {STATUS['err']}")
        return True

    _defer_log = {"t": 0.0}

    def _try(fn, what, *a) -> bool:
        """Run one build; True when it ran. Deferred/failed are logged
        (deferrals at most once per 10 min) and never raise."""
        try:
            fn(*a)
            STATUS["last"] = time.time()
            STATUS["err"] = None
            _backoff["n"] = 0
            return True
        except RateLimited as rl:
            _rate_limited(outdir, what, rl.retry_after)
        except Deferred as d:
            if time.time() - _defer_log["t"] > 600:
                _log(outdir, f"DEFERRED {what}: {d}")
                _defer_log["t"] = time.time()
        except Exception as exc:
            STATUS["err"] = f"{type(exc).__name__}: {exc}"
            _log(outdir, f"FAILED {what}: {STATUS['err']}")
        return False

    def _stale(model, field, step) -> bool:
        age = _frame_age_min(outdir, model, field, step)
        if step == 0:
            return age >= (NOW_FAST_MIN if field == FIELDS[0] else NOW_SLOW_MIN)
        return age >= (FCST_MIN if step <= 24 else FAR_MIN)

    def _frame(model, field, step) -> bool:
        return _try(build_frame, f"{model} {field} +{step:02d}h", outdir, model, field, step,
                    NOW_ZOOM if step == 0 else FCST_ZOOM)

    _noaa_next = 0.0
    while True:
        t = time.time()
        if in_backoff():
            time.sleep(10)
            continue
        if pause_left_s() > 0:
            if STATUS.get("paused") != PAUSE_UNTIL:
                STATUS["paused"] = PAUSE_UNTIL
                STATUS["err"] = f"tomorrow.io paused until {PAUSE_UNTIL} (TIO_PAUSE_UNTIL)"
                _log(outdir, f"PAUSED: no tomorrow.io requests until {PAUSE_UNTIL} "
                             f"({pause_left_s() / 3600:.1f} h)")
            time.sleep(30)
            continue
        if STATUS.get("paused"):
            STATUS["paused"] = None
            STATUS["err"] = None
            _log(outdir, "pause over: resuming")
        if not api_key():
            # No key on this instance (the Mac deliberately leaves it to
            # Render): say so once, then check again every minute instead
            # of logging a failure per tick.
            if STATUS.get("err") != "no TOMORROWIO_API_KEY":
                STATUS["err"] = "no TOMORROWIO_API_KEY"
                _log(outdir, "no TOMORROWIO_API_KEY on this instance: tomorrow.io "
                             "layers idle (model layers unaffected)")
            time.sleep(60)
            continue
        model = active_model(outdir)
        STATUS["model"] = model
        if model != last_model:
            _log(outdir, f"model -> {model}: warming on demand")
            last_model = model
        idle = is_idle(outdir)
        if idle != STATUS.get("idle"):
            STATUS["idle"] = idle
            _log(outdir, "nobody viewing for %d min: on-demand and prefetch paused, "
                         "%s now-frame every %d min" % (IDLE_MIN, FIELDS[0], NOW_IDLE_MIN)
                 if idle else "viewer back: resuming")
        if NOAA_ON and t >= _noaa_next:
            try:
                build_noaa(outdir)
            except Exception as exc:
                _log(outdir, f"FAILED noaa: {type(exc).__name__}: {exc}")
            _noaa_next = time.time() + NOAA_MIN * 60
        built = 0
        if idle:
            # keep the map openable: the first field's now-frame, slowly
            if _frame_age_min(outdir, model, FIELDS[0], 0) >= NOW_IDLE_MIN:
                _frame(model, FIELDS[0], 0)
            time.sleep(15)
            continue

        dm = demand(outdir)
        act = active_fields(outdir)
        # 1. what the viewer is looking at RIGHT NOW: the slider's step
        #    for every layer that is on, then the neighbouring steps
        #    (scrubbing), unless Play is running through the loop
        if dm and not dm.get("playing"):
            step = dm.get("step", 0)
            want = [step] + [h for h in FCST_HOURS if 0 < h - step <= 3] + \
                   [h for h in [0] + FCST_HOURS if 0 < step - h <= 1]
            for st_ in want:
                for f in dm.get("fields") or []:
                    if st_ and st_ not in steps_for(f):
                        continue
                    if _stale(model, f, st_):
                        built += _frame(model, f, st_)
                        if built >= 4:
                            break
                if built >= 4:
                    break
        # 2. the station someone clicked
        if dm.get("station") and POINTS_MODE == "demand" and built < 4:
            ic = dm["station"]
            if _point_age_min(outdir, model, ic) >= PT_STALE_MIN:
                built += _try(build_point_one, f"{model} point {ic}", outdir, model, ic)
        # 3. hi-res sector, only while the map is zoomed into it
        sec = active_sector(outdir)
        if (HIRES_ON and sec and dm and dm.get("zoom", 0) >= HIRES_ZOOM_GATE
                and built < 4):
            _hires_pass(outdir, model)
        # 4. keep-warm for the active layers: now-frames on their
        #    cadence, then the next PREFETCH_H hours
        if built < 4:
            for f in act:
                if _stale(model, f, 0):
                    built += _frame(model, f, 0)
                    if built >= 4:
                        break
        if built < 4:
            for h in [h for h in FCST_HOURS if h <= PREFETCH_H]:
                for f in act:
                    if h in steps_for(f) and _stale(model, f, h):
                        built += _frame(model, f, h)
                        if built >= 4:
                            break
                if built >= 4:
                    break
        # 5. the old warm-everything points pass, if asked for
        if POINTS_MODE == "all" and built < 4:
            if _point_age_min(outdir, model, "__all__") >= PT_MIN:
                if _try(build_points, f"{model} points", outdir, model):
                    doc = points(outdir, model)
                    doc.setdefault("built_by", {})["__all__"] = doc.get("built", "")
                    p = _pt_path(outdir, model)
                    p.write_text(json.dumps(doc))
        time.sleep(2 if built else 5)


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
    pt_url = f"{base}/app/static/tio_pt_{model}.json"
    noaa_url = f"{base}/app/static/tio_noaa.json?v={noaa_built}" if noaa_built else ""
    # GENERAL WEATHER menu: one row per layer, checkbox + opacity.
    # Precipitation reflectivity on by default.
    rows = "".join(
        f'<div class="ly"><label><input type="checkbox" data-f="{f}"'
        f'{" checked" if i == 0 else ""}> {labels[f]}</label>'
        f'<input type="range" data-f="{f}" min="10" max="100" value="{85 if i == 0 else 70}"></div>'
        for i, f in enumerate(FIELDS))
    # METEOGRAM menu: the point fields, up to four at once. Ceiling,
    # visibility and wind speed / gust start ticked (7 Oct: an empty
    # panel read as "no data"); untick to swap in others.
    pf = point_fields()
    plabels = {f: FIELD_LABEL.get(f, f) for f in pf}
    mg_default = [f for f in ("cloudCeiling", "visibility", "windSpeed", "windGust") if f in pf][:4]
    mrows = "".join(
        f'<div class="ly"><label><input type="checkbox" class="mgf" data-f="{f}"'
        f'{" checked" if f in mg_default else ""}> {plabels[f]}</label></div>'
        for f in pf)
    steps_all = [0] + FCST_HOURS
    # NOAA MODEL layers (core/model_tiles): one checkbox + opacity per
    # layer, grouped by model, drawn under the tomorrow.io layers.
    try:
        from core import model_tiles as _MT
        mkeys = _MT.field_keys()
        _by_model = {}
        for k in mkeys:
            _by_model.setdefault(k.split(":")[0], []).append(k)
        nrows = "".join(
            f'<div style="color:#6E6E6E;font-size:10px;padding:4px 8px 0;letter-spacing:1px">{_MT.MODEL_LABEL.get(m, m).upper()}</div>'
            + "".join(
                f'<div class="ly"><label><input type="checkbox" data-f="{k}"> {_MT.field_label(k).split(" ", 1)[1]}</label>'
                f'<input type="range" data-f="{k}" min="10" max="100" value="80"></div>'
                for k in ks)
            for m, ks in _by_model.items())
        mpal = _MT.palette_json()
        mlabels = {k: _MT.field_label(k) for k in mkeys}
    except Exception:
        mkeys, nrows, mpal, mlabels = [], "", {}, {}
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
 .mcb{{margin:0 0 6px 0;min-width:300px}} .mcb .t{{font-size:10px;color:#fff}}
 .mcb .cells{{display:flex;gap:1px;margin-top:2px}} .mcb .cells div{{flex:1;height:9px}}
 .mcb .labs{{display:flex;gap:1px}} .mcb .labs div{{flex:1;font-size:9px;color:#B8B8B8}}
 .mcb .ctl{{display:flex;align-items:center;gap:6px;margin-top:3px;font-size:10px;color:#B8B8B8}}
 .mcb .ctl button{{font:bold 10px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:4px;padding:1px 6px;cursor:pointer}}
 .mcb .ctl select{{font:bold 10px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:4px;padding:1px 4px}}
 .maplibregl-popup-content{{background:#0A0A0A !important;color:#fff;border:1px solid #333;border-radius:6px;font:12px "DejaVu Sans Mono",monospace;padding:8px 10px;max-width:360px}}
 .maplibregl-popup-close-button{{color:#fff;font-size:16px}} .maplibregl-popup-tip{{border-top-color:#333 !important}}
 .rd table{{border-collapse:collapse}} .rd td{{padding:1px 8px 1px 0;white-space:nowrap}} .rd td.v{{color:#FFD400;text-align:right}}
 .rd .h{{color:#00E5FF;font-weight:700;margin:4px 0 2px}} .rd .s{{color:#6E6E6E;font-size:10px}}
</style></head><body>
<div class="bar">
  <details class="dd" id="ddw"><summary id="ddws">General Weather &#9662;</summary><div class="menu">{rows}</div></details>
  <details class="dd" id="ddn"><summary id="ddns">NOAA Models &#9662;</summary><div class="menu">{nrows or '<div style="color:#6E6E6E;padding:6px 8px">no model layers enabled (MDL_MODELS)</div>'}</div></details>
  <span class="sp"></span><span id="mdl">{model_label(model)}{" &middot; DEMO DATA" if DEMO else ""}</span>
  <button id="bst" class="on">Stations</button><button id="balt" class="on">CA alternates</button>
  <button id="bfit">Fit</button><button id="b2" title="Map + meteogram of the layers that are on, for a station you click">2-panel</button>
  <span style="color:#333">|</span>
  <button class="ov" data-o="routes" title="The 58 ATS routes the JBU CONUS map draws: 42 domestic J and Q, 16 oceanic L">Jet routes</button>
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
<div id="m"><div class="lg" id="lg"><div id="mlg"></div><span class="d" style="background:#4DA3FF"></span>JBU station &nbsp;
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
const TFIELDS = {json.dumps(FIELDS)};
const MFIELDS = {json.dumps(mkeys)};
const FIELDS = TFIELDS.concat(MFIELDS);
const MDL_URL = {json.dumps(base + "/app/static/mdl_manifest.json")};
const PAL = {json.dumps(mpal)};
const STEPS = {json.dumps(steps_all)};
const PT_URL = {json.dumps(pt_url)};
const MAN_URL = {json.dumps(base + "/app/static/tio_manifest.json")};
const IMG_BASE = {json.dumps(base + "/app/static/")};
const MODEL = {json.dumps(model)}, SECTOR = {json.dumps(sector)};
const FCST_STEPS = {json.dumps(FCST_HOURS)};
const NOAA_URL = {json.dumps(noaa_url)};
const LABELS = {json.dumps({**labels, **plabels, **mlabels})};
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
// ---- band-index frames (NOAA model layers) --------------------------
// The file holds a palette band per pixel; the colours come from PAL
// and the per-layer legend settings (floor and shift), applied here on
// a canvas, so moving the legend never touches the server.
const IDX = {{}};            // frame key -> {{w, h, bands:Uint8Array, url(blob)}}
const LG = {{}};             // field -> {{min:0, shift:0}}
MFIELDS.forEach(f => LG[f] = {{min: 0, shift: 0}});
function colorOf(field, band) {{
  // band 1..n (0 = none). Shift moves the colours: band b takes the
  // colour of band b - shift; below the floor or below 1 -> nothing.
  const p = PAL[field], g = LG[field];
  if (!p || !band || band <= g.min) return null;
  const c = band - g.shift;
  if (c < 1) return null;
  return p.colors[Math.min(c, p.colors.length) - 1];
}}
function hex2rgb(h) {{ return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }}
function paint(fk, field) {{
  const d = IDX[fk]; if (!d) return null;
  const cv = document.createElement('canvas'); cv.width = d.w; cv.height = d.h;
  const cx = cv.getContext('2d'); const im = cx.createImageData(d.w, d.h); const px = im.data;
  const n = PAL[field] ? PAL[field].colors.length : 0; const lut = new Array(n + 2).fill(null);
  for (let b = 1; b <= n + 1; b++) {{ const c = colorOf(field, b); lut[b] = c ? hex2rgb(c) : null; }}
  for (let i = 0, j = 0; i < d.bands.length; i++, j += 4) {{
    const c = lut[d.bands[i]]; if (c) {{ px[j] = c[0]; px[j + 1] = c[1]; px[j + 2] = c[2]; px[j + 3] = 255; }}
  }}
  cx.putImageData(im, 0, 0);
  return cv.toDataURL('image/png');
}}
function decodeIdx(f, done) {{
  const im = new Image(); im.crossOrigin = 'anonymous';
  im.onload = () => {{
    const cv = document.createElement('canvas'); cv.width = im.width; cv.height = im.height;
    const cx = cv.getContext('2d'); cx.drawImage(im, 0, 0);
    const px = cx.getImageData(0, 0, im.width, im.height).data; const bands = new Uint8Array(im.width * im.height);
    for (let i = 0, j = 0; i < bands.length; i++, j += 4) bands[i] = px[j];
    IDX[key(f)] = {{w: im.width, h: im.height, bands: bands, src: f.url}};
    done();
  }};
  im.onerror = () => {{}};
  im.src = f.url;
}}
function recolorField(field) {{
  F.filter(f => f.field === field && IDX[key(f)]).forEach(f => {{
    const src = map.getSource(key(f)); const url = paint(key(f), field);
    if (src && url) src.updateImage({{url: url, coordinates: [[f.b[0], f.b[3]], [f.b[2], f.b[3]], [f.b[2], f.b[1]], [f.b[0], f.b[1]]]}});
  }});
  renderLegends();
}}
function ensure(f) {{
  if (added.has(key(f))) return;
  if (f.idx) {{
    if (!IDX[key(f)]) {{ if (!f._loading) {{ f._loading = true; decodeIdx(f, () => {{ f._loading = false; draw(); }}); }} return; }}
    added.add(key(f));
    map.addSource(key(f), {{type:'image', url: paint(key(f), f.field),
      coordinates:[[f.b[0], f.b[3]], [f.b[2], f.b[3]], [f.b[2], f.b[1]], [f.b[0], f.b[1]]]}});
  }} else {{
  added.add(key(f));
  map.addSource(key(f), {{type:'image', url:f.url,
    coordinates:[[f.b[0], f.b[3]], [f.b[2], f.b[3]], [f.b[2], f.b[1]], [f.b[0], f.b[1]]]}});
  }}
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
// ---- demand: tell the warmer what is being looked at ------------------
// Posted to the page's bridge component (a sibling iframe, same
// origin), which hands it to Streamlit -> core.tio_map.set_demand.
let demandTimer = null, lastDemand = '';
function sendDemand(now) {{
  clearTimeout(demandTimer);
  demandTimer = setTimeout(() => {{
    const v = {{fields: FIELDS.filter(f => on[f]), step: STEPS[ti], zoom: +map.getZoom().toFixed(2),
               station: sel || '', playing: !!timer}};
    const js = JSON.stringify(v);
    if (js === lastDemand && !now) return;
    lastDemand = js;
    try {{ const P = window.parent; for (let i = 0; i < P.frames.length; i++) {{ try {{ P.frames[i].postMessage({{tio:'demand', value:v}}, '*'); }} catch (e) {{}} }} }} catch (e) {{}}
  }}, now ? 0 : 600);
}}
setInterval(() => sendDemand(true), 45000);          // heartbeat while open
// ---- frames: (re)built from the manifest, polled while the page is open
function entriesFrom(man) {{
  const mm = (man && man[MODEL]) || {{}}, out = [];
  FIELDS.forEach(field => {{ const d = mm[field] || {{}}; Object.keys(d).forEach(st => {{ const e = d[st];
    out.push({{url: IMG_BASE + e.name + '?v=' + e.built, field: field, step: +st, valid: e.valid, built: e.built, b: e.bounds, hub: ''}}); }}); }});
  const hr = SECTOR ? (mm['hires:' + SECTOR] || {{}}) : {{}};
  Object.keys(hr).forEach(field => {{ Object.keys(hr[field]).forEach(st => {{ const e = hr[field][st];
    out.push({{url: IMG_BASE + e.name + '?v=' + e.built, field: field, step: +st, valid: e.valid, built: e.built, b: e.bounds, hub: SECTOR}}); }}); }});
  return out;
}}
function mergeFrames(list) {{
  let changed = false;
  list.forEach(n => {{
    const i = F.findIndex(f => f.field === n.field && f.step === n.step && f.hub === n.hub);
    if (i < 0) {{ F.push(n); changed = true; return; }}
    if (F[i].url !== n.url) {{
      F[i] = n; changed = true;
      if (n.idx) {{ delete IDX[key(n)]; decodeIdx(n, () => {{ const s = map.getSource(key(n)); const u = paint(key(n), n.field); if (s && u) s.updateImage({{url: u, coordinates: [[n.b[0], n.b[3]], [n.b[2], n.b[3]], [n.b[2], n.b[1]], [n.b[0], n.b[1]]]}}); }}); return; }}
      const src = ready && map.getSource(key(n));
      if (src && src.updateImage) src.updateImage({{url: n.url, coordinates: [[n.b[0], n.b[3]], [n.b[2], n.b[3]], [n.b[2], n.b[1]], [n.b[0], n.b[1]]]}});
    }}
  }});
  if (changed) draw();
}}
// ---- adjustable legends ---------------------------------------------
function renderLegends() {{
  const box = $('mlg'); if (!box) return;
  const onF = MFIELDS.filter(f => on[f] && PAL[f]);
  box.innerHTML = onF.map(f => {{
    const p = PAL[f], g = LG[f];
    const cells = p.bounds.map((b, i) => {{ const c = colorOf(f, i + 1); return '<div style="background:' + (c || 'transparent') + ';' + (c ? '' : 'border:1px dashed #333;') + '"></div>'; }}).join('');
    const labs = p.bounds.map((b, i) => '<div style="color:' + (colorOf(f, i + 1) ? '#B8B8B8' : '#444') + '">' + b + '</div>').join('');
    const opts = ['<option value="0"' + (g.min === 0 ? ' selected' : '') + '>all</option>'].concat(
      p.bounds.map((b, i) => '<option value="' + (i + 1) + '"' + (g.min === i + 1 ? ' selected' : '') + '>&ge; ' + (p.bounds[i + 1] != null ? p.bounds[i + 1] : b) + '</option>')).join('');
    return '<div class="mcb" data-f="' + f + '"><div class="t">' + p.label + (p.unit ? ' (' + p.unit + ')' : '') + '</div>' +
      '<div class="cells">' + cells + '</div><div class="labs">' + labs + '</div>' +
      '<div class="ctl">show <select class="lgmin" data-f="' + f + '">' + opts + '</select>' +
      ' &nbsp;shift <button class="lgsh" data-f="' + f + '" data-d="-1">&#9664;</button><span>' + (g.shift > 0 ? '+' : '') + g.shift + '</span><button class="lgsh" data-f="' + f + '" data-d="1">&#9654;</button>' +
      ' <button class="lgrs" data-f="' + f + '">reset</button></div></div>';
  }}).join('');
  box.querySelectorAll('.lgmin').forEach(el => el.onchange = e => {{ LG[el.dataset.f].min = +e.target.value; recolorField(el.dataset.f); }});
  box.querySelectorAll('.lgsh').forEach(el => el.onclick = () => {{ const g = LG[el.dataset.f]; g.shift = Math.max(-(PAL[el.dataset.f].colors.length - 1), Math.min(PAL[el.dataset.f].colors.length - 1, g.shift + (+el.dataset.d))); recolorField(el.dataset.f); }});
  box.querySelectorAll('.lgrs').forEach(el => el.onclick = () => {{ LG[el.dataset.f] = {{min: 0, shift: 0}}; recolorField(el.dataset.f); }});
}}
// ---- click readout --------------------------------------------------
// A click anywhere asks the server for EVERY layer of the models that
// have a layer switched on (or the first model), at the slider's hour,
// nearest grid cell. Answer arrives as a small JSON the page polls for.
let rdPopup = null, rdPoll = null;
function readout(lngLat) {{
  const models = [...new Set(MFIELDS.filter(f => on[f]).map(f => f.split(':')[0]))];
  if (!MFIELDS.length) return;
  const id = Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
  const req = {{kind: 'point', id: id, lat: +lngLat.lat.toFixed(4), lon: +lngLat.lng.toFixed(4), step: STEPS[ti], models: models}};
  try {{ const P = window.parent; for (let i = 0; i < P.frames.length; i++) {{ try {{ P.frames[i].postMessage({{tio:'demand', value:req}}, '*'); }} catch (e) {{}} }} }} catch (e) {{}}
  if (rdPopup) rdPopup.remove();
  rdPopup = new maplibregl.Popup({{closeOnClick: false, maxWidth: '380px'}}).setLngLat(lngLat)
    .setHTML('<div class="rd"><div class="h">' + req.lat + ', ' + req.lon + ' &middot; +' + req.step + ' h</div><div class="s">reading ' + (models.length ? models.join(', ').toUpperCase() : 'model') + '&hellip;</div></div>').addTo(map);
  clearInterval(rdPoll); let tries = 0;
  rdPoll = setInterval(() => {{
    tries++;
    fetch(IMG_BASE + 'mdl_point_' + id + '.json?_=' + Date.now(), {{cache:'no-store'}}).then(r => r.ok ? r.json() : null).then(j => {{
      if (!j) {{ if (tries > 45) {{ clearInterval(rdPoll); if (rdPopup) rdPopup.setHTML('<div class="rd"><div class="s">no answer - is the model warmer running?</div></div>'); }} return; }}
      clearInterval(rdPoll);
      const html = '<div class="rd"><div class="h">' + j.lat + ', ' + j.lon + ' &middot; valid ' + j.valid + '</div>' +
        j.models.map(m => '<div class="h">' + m.label + '</div><table>' + m.rows.map(r => '<tr><td>' + r.layer.replace(m.label.split(' ')[0] + ' ', '') + '</td><td class="v">' + r.value + '</td><td class="s">' + (r.cycle ? r.cycle.slice(8, 10) + '/' + r.cycle.slice(11, 13) + 'Z f' + String(r.fhr).padStart(2, '0') : '') + '</td></tr>').join('') + '</table>').join('') + '</div>';
      if (rdPopup) rdPopup.setHTML(html);
    }}).catch(() => {{}});
  }}, 1000);
}}
function stepOfValid(v) {{
  const nowH = Math.floor(Date.now() / 3600000) * 3600000;
  return Math.round((Date.parse(v.replace('Z', ':00Z')) - nowH) / 3600000);
}}
function modelEntries(man) {{
  const out = [];
  MFIELDS.forEach(key => {{
    const [m, c] = key.split(':');
    const d = (man[m] || {{}})[c] || {{}};
    Object.keys(d).forEach(v => {{ const e = d[v]; const st = stepOfValid(v);
      if (STEPS.indexOf(st) < 0) return;
      out.push({{url: IMG_BASE + e.name + '?v=' + e.built, field: key, step: st, valid: e.valid + ' (' + m.toUpperCase() + ' ' + e.cycle.slice(8, 10) + '/' + e.cycle.slice(11, 13) + 'Z f' + String(e.fhr).padStart(2, '0') + ')', built: e.built, b: e.bounds, hub: '', idx: !!e.idx}}); }});
  }});
  return out;
}}
function pollModels() {{
  if (!MFIELDS.length) return;
  fetch(MDL_URL + '?_=' + Date.now(), {{cache:'no-store'}}).then(r => r.ok ? r.json() : {{}}).then(j => mergeFrames(modelEntries(j))).catch(() => {{}});
}}
setInterval(pollModels, 8000); pollModels();
function pollManifest() {{
  fetch(MAN_URL + '?_=' + Date.now(), {{cache:'no-store'}}).then(r => r.json()).then(j => mergeFrames(entriesFrom(j))).catch(() => {{}});
}}
setInterval(pollManifest, 8000);
function hasFrame(i) {{ return FIELDS.some(f => on[f] && frame(f, STEPS[i])); }}
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
  const nT = TFIELDS.filter(f => on[f]).length, nM = MFIELDS.filter(f => on[f]).length;
  $('ddws').innerHTML = 'General Weather' + (nT ? ' <span style="color:#00E5FF">' + nT + '</span>' : '') + ' &#9662;';
  if ($('ddns')) $('ddns').innerHTML = 'NOAA Models' + (nM ? ' <span style="color:#00E5FF">' + nM + '</span>' : '') + ' &#9662;';
  renderLegends();
  $('valid').textContent = (step ? '+' + step + ' h' : 'now') + (valid ? '  valid ' + valid : (nOn ? '  fetching…' : '  (no layer on)'));
  sendDemand(false);
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
// Elements ticked in the HTML start on (7 Oct).
document.querySelectorAll('.mgf').forEach(c => {{ if (c.checked) mgOn[c.dataset.f] = true; }});
{{ const m0 = PFIELDS.filter(f => mgOn[f]).length;
  if (m0) $('ddms').innerHTML = 'Elements <span style="color:#00E5FF">' + m0 + '</span> &#9662;'; }}
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
  if (!PT) {{ note.textContent = 'loading station forecasts…'; box.innerHTML = ''; return; }}
  const st = PT.stations[sel];
  if (!st) {{ note.textContent = sel ? 'fetching ' + sel + ' from tomorrow.io…' : 'click a station dot on the map'; box.innerHTML = ''; $('mgt').textContent = sel || ''; return; }}
  const ps = panels(st);
  if (!ps.length) {{ note.textContent = 'pick up to four elements from the Elements menu'; box.innerHTML = ''; return; }}
  note.textContent = '';
  $('mgt').textContent = sel; $('mgs').textContent = PT.model + ' · built ' + ((PT.built_by && PT.built_by[sel]) || PT.built) + 'Z';
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
let ptPoll = null;
function pick(icao) {{
  sel = icao; $('mgsel').value = icao; sendDemand(true); drawMg();
  clearInterval(ptPoll);
  if (icao && !(PT && PT.stations && PT.stations[icao])) {{
    let tries = 0;
    ptPoll = setInterval(() => {{
      tries++;
      fetch(PT_URL + '?_=' + Date.now(), {{cache:'no-store'}}).then(r => r.json()).then(j => {{ PT = j; fillSel(); if (PT.stations && PT.stations[sel]) {{ clearInterval(ptPoll); }} drawMg(); }}).catch(() => {{}});
      if (tries > 20) clearInterval(ptPoll);
    }}, 4000);
  }}
}}
function fillSel() {{
  const ids = ST.features.map(f => f.properties.id).sort();
  $('mgsel').innerHTML = '<option value="">station…</option>' + ids.map(i => `<option value="${{i}}">${{i}}</option>`).join('');
  if (sel) $('mgsel').value = sel;
}}
$('mgsel').onchange = e => pick(e.target.value);
$('b2').onclick = () => {{
  two = !two; $('b2').classList.toggle('on', two); document.body.classList.toggle('two', two);
  setTimeout(() => map.resize(), 30);
  if (two && !PT && PT_URL) fetch(PT_URL + '?_=' + Date.now(), {{cache:'no-store'}}).then(r => r.ok ? r.json() : {{stations:{{}}}}).then(j => {{ PT = j; fillSel(); drawMg(); }}).catch(() => {{ PT = {{stations:{{}}}}; fillSel(); drawMg(); }});
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
  routes: {{ file:'map_routes.geojson', build: j => {{
      // the same 58 routes the JBU CONUS map draws (28 Sep)
      map.addSource('ov-routes', {{type:'geojson', data: lineFC(j.features.map(f => [f.geometry.coordinates, {{ident:f.properties.ident, rnav:f.properties.type === 'RNAV'}}]))}});
      map.addLayer({{id:'ov-routes', type:'line', source:'ov-routes', paint:{{'line-color':['case', ['get','rnav'], '#22D3EE', '#9AA0A6'], 'line-width':1.1, 'line-opacity':0.85}}}}, 'st-dot');
      map.addLayer({{id:'ov-routes-lab', type:'symbol', source:'ov-routes', layout:{{'symbol-placement':'line', 'text-field':['get','ident'], 'text-size':10, 'text-font':['Open Sans Bold'], 'symbol-spacing':900}},
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
  map.on('click', e => {{ if (map.queryRenderedFeatures(e.point, {{layers: ['st-dot']}}).length) return; readout(e.lngLat); }});
  map.on('mouseenter', 'st-dot', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'st-dot', () => map.getCanvas().style.cursor = '');
  map.on('zoomend', () => sendDemand(false));
  ready = true; draw();
}});
$('tm').oninput = e => {{ ti = +e.target.value; draw(); }};
function stepBy(d) {{ ti = Math.max(0, Math.min(STEPS.length - 1, ti + d)); $('tm').value = ti; draw(); }}
$('bprev').onclick = () => stepBy(-1);
$('bnext').onclick = () => stepBy(1);
$('bplay').onclick = () => {{
  if (timer) {{ clearInterval(timer); timer = null; $('bplay').textContent = 'Play'; $('bplay').classList.remove('on'); return; }}
  $('bplay').textContent = 'Pause'; $('bplay').classList.add('on');
  timer = setInterval(() => {{
    let n = ti, guard = 0;
    do {{ n = n >= STEPS.length - 1 ? 0 : n + 1; guard++; }} while (!hasFrame(n) && guard < STEPS.length);
    ti = n; $('tm').value = ti; draw();
  }}, 700);
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
