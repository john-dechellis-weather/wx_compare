"""Convection Parameters: a 48 h tomorrow.io point forecast for one
station (pages/16_Convection_Parameters.py).

One /v4/timelines call per station returns every field at once, so
a station costs ONE request per refresh, charged through the same
budget as the map tiles (core.tio_map._charge, priority 1: it keeps
the reserve, the hourly now-frames and the hourly cap). The reply is
cached under tio_map.PERSIST (or static/) for TTL_MIN and served to
every viewer from there, so a busy day with ten stations in use is
~10 * 24 = 240 requests.

Fields (tomorrow.io names, verified 1 Oct against docs.tomorrow.io
data-layers-precipitation / expert-layers / core):
    thunderstormProbability   %       core, +14 d
    lightningFlashRateDensity fl/km2 per 5 min   Lightning layer, +90 h
    hailProbability           %       NextGen layer, +36 h
    lightningProbability      %       NextGen layer, +36 h
    cape                      J/kg    Expert layer, +8 d
    cin                       J/kg    Expert layer, +8 d
    vorticity500              1e-5/s  Expert layer, +8 d
    rainIntensity             mm/h    core
    precipitationProbability  %       core
    precipitationReflectivity dBZ     Advanced precipitation layer
A field the plan does not include comes back as HTTP 400 naming it;
fetch() then retries once without the fields the error names and
remembers them in `_dropped` so later calls skip them.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from core import tio_map as TIO

HOURS = int(os.environ.get("TIO_CONV_HOURS", "48"))
TTL_MIN = int(os.environ.get("TIO_CONV_TTL_MIN", "60"))

# (field, label, units, panel colour, kind) in page order. kind:
# "bar" for probabilities and rates (filled columns), "line" for the
# continuous parameters.
FIELDS = [
    ("thunderstormProbability",   "Thunderstorm probability",      "%",       "#FFD400", "bar"),
    ("lightningFlashRateDensity", "Lightning flash rate density",  "fl/km²/5 min", "#FF8A00", "bar"),
    ("hailProbability",           "Hail probability",              "%",       "#FF3B30", "bar"),
    ("lightningProbability",      "Lightning probability",         "%",       "#FF00C8", "bar"),
    ("cape",                      "CAPE",                          "J/kg",    "#00E5FF", "line"),
    ("cin",                       "CIN",                           "J/kg",    "#4DA3FF", "line"),
    ("vorticity500",              "500 hPa vorticity",             "10⁻⁵ s⁻¹", "#00FF7F", "line"),
    ("rainIntensity",             "Rain intensity",                "mm/h",    "#2A5BFF", "bar"),
    ("precipitationProbability",  "Precipitation probability",     "%",       "#9AA0A6", "bar"),
    ("precipitationReflectivity", "Precipitation reflectivity",    "dBZ",     "#00A3FF", "line"),
]
FIELD_KEYS = [f[0] for f in FIELDS]

_dropped: set = set()        # fields the plan rejected (per process)
_lock_t: dict = {}           # icao -> time of the fetch in flight


def _dir() -> Path:
    d = TIO.PERSIST if TIO.PERSIST is not None else (Path(__file__).resolve().parent.parent / "static")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(icao: str) -> Path:
    return _dir() / f"tio_conv_{icao.upper()}.json"


def cached(icao: str) -> dict | None:
    """The stored reply for icao, whatever its age, or None."""
    try:
        return json.loads(_path(icao).read_text())
    except Exception:
        return None


def age_min(doc: dict | None) -> float:
    if not doc:
        return 1e9
    return (time.time() - float(doc.get("fetched", 0))) / 60.0


def _call(lat: float, lon: float, fields: list, key: str) -> dict:
    import requests

    url = f"https://api.tomorrow.io/v4/timelines?apikey={key}"
    body = {"location": f"{lat:.4f},{lon:.4f}", "fields": fields,
            "timesteps": ["1h"], "units": "metric",
            "startTime": "now", "endTime": f"nowPlus{HOURS}h"}
    r = requests.post(url, json=body, timeout=25)
    if r.status_code == 429:
        raise TIO.RateLimited(f"429 {r.text[:100]}", TIO._retry_after(r))
    if r.status_code == 400:
        # "The field(s) X, Y ... not available" style message: pull out
        # any of our field names it mentions.
        bad = {f for f in fields if re.search(r"\b" + re.escape(f) + r"\b", r.text)}
        raise FieldsRejected(bad, r.text[:200])
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
    iv = r.json()["data"]["timelines"][0]["intervals"]
    out = {"t": [x["startTime"] for x in iv]}
    for f in fields:
        out[f] = [x["values"].get(f) for x in iv]
    return out


class FieldsRejected(RuntimeError):
    def __init__(self, fields: set, text: str):
        super().__init__(text)
        self.fields = fields


def fetch(icao: str, lat: float, lon: float, force: bool = False) -> tuple[dict | None, str]:
    """(doc, note). Serves the cache while younger than TTL_MIN (or
    while the budget says not now); otherwise one charged request.
    note is "" or a one-line reason the data is not fresh."""
    icao = icao.upper()
    doc = cached(icao)
    if doc and not force and age_min(doc) < TTL_MIN:
        return doc, ""
    key = TIO.api_key()
    if not key:
        return doc, "no TOMORROWIO_API_KEY on this instance"
    if TIO.in_backoff():
        return doc, f"tomorrow.io rate limit: retry in {TIO.in_backoff() / 60:.0f} min"
    if TIO.pause_left_s() > 0:
        return doc, f"tomorrow.io paused until {TIO.PAUSE_UNTIL}"
    # one fetch at a time per station across sessions
    t = _lock_t.get(icao, 0)
    if time.time() - t < 20:
        return doc, "refreshing..."
    _lock_t[icao] = time.time()
    outdir = Path(__file__).resolve().parent.parent / "static"
    fields = [f for f in FIELD_KEYS if f not in _dropped]
    try:
        TIO._charge(outdir, 1, 1, f"point {icao} convection")
    except TIO.Deferred as exc:
        return doc, f"budget: {exc}"
    except RuntimeError as exc:
        return doc, str(exc)
    try:
        try:
            data = _call(lat, lon, fields, key)
        except FieldsRejected as exc:
            if not exc.fields:
                raise
            _dropped.update(exc.fields)
            fields = [f for f in fields if f not in exc.fields]
            TIO._log(outdir, f"convection {icao}: plan rejects {sorted(exc.fields)}; "
                             "fetching without them")
            data = _call(lat, lon, fields, key)
    except TIO.RateLimited as exc:
        TIO._rate_limited(outdir, f"convection {icao}", exc.retry_after)
        return doc, f"tomorrow.io rate limit ({exc})"
    except Exception as exc:
        TIO._log(outdir, f"FAILED convection {icao}: {type(exc).__name__}: {exc}")
        return doc, f"fetch failed: {type(exc).__name__}: {exc}"
    finally:
        _lock_t.pop(icao, None)
    new = {"icao": icao, "lat": lat, "lon": lon, "fetched": time.time(),
           "fetched_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "fields": fields, "dropped": sorted(_dropped), "data": data}
    p = _path(icao)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(new))
    os.replace(tmp, p)
    return new, ""
