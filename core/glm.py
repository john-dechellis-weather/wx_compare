"""Live lightning from the GOES Geostationary Lightning Mapper (24 Oct).

Source: NOAA Open Data on AWS, product GLM-L2-LCFA - one small netCDF
every 20 s per satellite, public, no key. GOES-East (noaa-goes19)
covers the whole JetBlue network east of the Rockies and the
Caribbean; GOES-West (noaa-goes18) adds the Pacific coast. Where
both see a flash the split is by longitude (East east of SPLIT_LON).

A poller lists the current hour's prefix every POLL_S seconds, pulls
every file it has not seen, and keeps the flashes of the last KEEP_MIN
minutes in memory. It writes static/glm_flashes.json:
    {"t": epoch, "keep_min", "files", "flashes": [[lon, lat, epoch_s, energy_fJ], ...]}
restricted to the network box (BOX) so the file stays a few hundred KB
even on a big convective day. Flash = total lightning (IC + CG), ~8 km
footprint, about a minute behind real time.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLL_S = float(os.environ.get("GLM_POLL_S", "60"))
KEEP_MIN = float(os.environ.get("GLM_KEEP_MIN", "15"))
SATS = {"noaa-goes19": "G19", "noaa-goes18": "G18"}
SPLIT_LON = -105.0            # East satellite east of this, West west of it
BOX = (5.0, 62.0, -175.0, -50.0)   # lat min, lat max, lon min, lon max
STATUS: dict = {"started": False, "last": None, "err": None, "files": 0, "n": 0, "lag_s": None}
_lock = threading.Lock()
_started = False
_seen: set = set()
_flashes: list = []           # [lon, lat, epoch, energy_fJ]
_EPOCH_2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
_KEY_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})\d_")


def path(outdir) -> Path:
    return Path(outdir) / "glm_flashes.json"


def _list(bucket: str, when: datetime) -> list:
    import requests
    prefix = f"GLM-L2-LCFA/{when:%Y/%j/%H}/"
    url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&prefix={prefix}&max-keys=400"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return re.findall(r"<Key>([^<]+)</Key>", r.text)


def _key_time(key: str) -> float | None:
    m = _KEY_RE.search(key)
    if not m:
        return None
    y, j, hh, mm, ss = (int(v) for v in m.groups())
    return (datetime(y, 1, 1, tzinfo=timezone.utc) + timedelta(days=j - 1, hours=hh, minutes=mm, seconds=ss)).timestamp()


def _read(bucket: str, key: str) -> list:
    import io
    import requests
    import numpy as np
    import netCDF4
    r = requests.get(f"https://{bucket}.s3.amazonaws.com/{key}", timeout=30)
    r.raise_for_status()
    ds = netCDF4.Dataset("inmem", memory=r.content)
    try:
        lat = np.asarray(ds.variables["flash_lat"][:], dtype="float64")
        lon = np.asarray(ds.variables["flash_lon"][:], dtype="float64")
        if not len(lat):
            return []
        t0 = float(ds.variables["product_time"][:]) + _EPOCH_2000
        off = np.asarray(ds.variables["flash_time_offset_of_first_event"][:], dtype="float64")
        en = np.asarray(ds.variables["flash_energy"][:], dtype="float64") * 1e15   # J -> fJ
    finally:
        ds.close()
    east = SATS.get(bucket) == "G19"
    keep = (lat >= BOX[0]) & (lat <= BOX[1]) & (lon >= BOX[2]) & (lon <= BOX[3])
    keep &= (lon >= SPLIT_LON) if east else (lon < SPLIT_LON)
    return [[round(float(lo), 3), round(float(la), 3), round(t0 + float(o), 1), round(float(e), 1)]
            for lo, la, o, e in zip(lon[keep], lat[keep], off[keep], en[keep])]


def poll(outdir) -> str:
    global _flashes
    now = datetime.now(timezone.utc)
    n_new, n_files, newest = 0, 0, 0.0
    cutoff = time.time() - KEEP_MIN * 60
    for bucket in SATS:
        keys = []
        for when in (now - timedelta(hours=1), now):
            try:
                keys += _list(bucket, when)
            except Exception as exc:
                STATUS["err"] = f"{bucket} list: {type(exc).__name__}"
                continue
        for key in sorted(keys):
            kt = _key_time(key)
            if kt is None or kt < cutoff or key in _seen:
                continue
            try:
                fl = _read(bucket, key)
            except Exception as exc:
                STATUS["err"] = f"{bucket} read: {type(exc).__name__}"
                continue
            _seen.add(key)
            n_files += 1
            n_new += len(fl)
            newest = max(newest, kt)
            _flashes.extend(fl)
    # prune memory + the seen set
    _flashes = [f for f in _flashes if f[2] >= cutoff]
    for k in [k for k in _seen if (_key_time(k) or 0) < cutoff - 600]:
        _seen.discard(k)
    doc = {"t": time.time(), "keep_min": KEEP_MIN, "files": n_files, "n": len(_flashes),
           "newest": newest or None, "flashes": _flashes}
    p = path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")))
    os.replace(tmp, p)
    STATUS.update({"last": time.time(), "files": n_files, "n": len(_flashes),
                   "lag_s": round(time.time() - newest) if newest else None})
    return f"glm: {n_files} files, {n_new} new flashes, {len(_flashes)} in last {KEEP_MIN:.0f} min"


def _loop(outdir):
    time.sleep(5)
    while True:
        try:
            poll(outdir)
            STATUS["err"] = None
        except Exception as exc:
            STATUS["err"] = f"{type(exc).__name__}: {exc}"[:120]
        time.sleep(POLL_S)


def ensure_glm(outdir) -> bool:
    global _started
    with _lock:
        if _started:
            return True
        threading.Thread(target=_loop, args=(outdir,), daemon=True, name="glm").start()
        _started = True
        STATUS["started"] = True
    return True


def near(outdir, lat: float, lon: float, radius_nm: float = 30.0, minutes: float = 15.0) -> int:
    """Flashes within radius_nm of a point in the last `minutes`."""
    import math
    try:
        doc = json.loads(path(outdir).read_text())
    except Exception:
        return 0
    cut = time.time() - minutes * 60
    k = math.cos(math.radians(lat))
    r_deg = radius_nm / 60.0
    n = 0
    for lo, la, t, _e in doc.get("flashes", []):
        if t < cut:
            continue
        if math.hypot((lo - lon) * k, la - lat) <= r_deg:
            n += 1
    return n
