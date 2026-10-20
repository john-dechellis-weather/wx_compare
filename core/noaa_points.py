"""NOAA point forecasts for the map viewer's meteogram (20 Oct).

Replaces the tomorrow.io point warmer. For every station the map
shows (JBU stations + Canadian alternates), the NBM hourly bulletin
(NBH, hours 1-25) and short-range bulletin (NBS, 3-hourly to +72 h)
give ceiling, visibility, wind, gust, direction, temperature,
dewpoint, sky cover, P(precip), QPF and P(thunder). At the compare
hubs the REFS ensemble adds P(ceiling < 1000 ft) and P(vis < 3 sm).

Written to static/tio_noaa.json (the name the viewer already reads):
  {"nbm_cycle", "refs_cycle", "built",
   "stations": {"JFK": {"nbm": {...}, "refs": {...}}, ...}}

One NBM download per cycle serves every station (the bulletins are
one file each); REFS is sampled per hub as before. Rebuilt every
NOAA_MIN minutes when a newer cycle exists.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOAA_MIN = int(os.environ.get("NOAA_POINTS_MIN", "20"))
STATUS: dict = {"started": False, "last": None, "err": None}
_lock = threading.Lock()
_started = False


def _path(outdir) -> Path:
    return Path(outdir) / "tio_noaa.json"


def _cache_root() -> Path:
    p = Path("/opt/render/project/src/cache")
    return p if p.exists() else Path("/tmp/wx_compare_cache")


def _log(outdir, msg):
    try:
        from core.tio_map import _log as _l
        _l(outdir, msg)
    except Exception:
        pass


def _latest_nbm_cycle(nbm) -> datetime | None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in range(1, 8):
        cyc = now - timedelta(hours=back)
        try:
            if nbm.is_cycle_complete(cyc):
                return cyc
        except Exception:
            continue
    return None


def _col(dm, name):
    if name not in dm.columns:
        return [None] * len(dm)
    return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)
            for v in dm[name].tolist()]


def build(outdir) -> str:
    from core import tio_map as T
    from core import compare_warm as CW
    from models.nbm import Nbm

    outdir = Path(outdir)
    try:
        doc = json.loads(_path(outdir).read_text())
    except Exception:
        doc = {}
    pts = T._points(outdir)                       # [(id3, lat, lon)]
    full = {(("K" + i) if len(i) == 3 else i): i for i, _, _ in pts}
    nbm = Nbm(cache_dir=_cache_root() / "nbm")
    cyc = _latest_nbm_cycle(nbm)
    out = {"nbm_cycle": cyc.isoformat() if cyc else doc.get("nbm_cycle"),
           "refs_cycle": doc.get("refs_cycle"),
           "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "stations": doc.get("stations", {})}
    n_nbm = 0
    if cyc and (cyc.isoformat() != doc.get("nbm_cycle")
                or not any("tmp_f" in (v.get("nbm") or {}) for v in out["stations"].values())):
        p = nbm.fetch(cyc, list(full))
        df = nbm.parse(p, list(full), cyc) if p is not None else None
        if df is not None and len(df):
            for icao, sid in full.items():
                dm = df[df["station_id"] == icao].sort_values("valid_time")
                if not len(dm):
                    continue
                unl = [bool(u) for u in (dm["ceiling_unlimited"].tolist()
                                         if "ceiling_unlimited" in dm.columns else [False] * len(dm))]
                cig = [None if u else c for c, u in zip(_col(dm, "ceiling_ft"), unl)]
                out["stations"].setdefault(sid, {})["nbm"] = {
                    "t": [ts.strftime("%Y-%m-%dT%H:%M:%SZ") for ts in dm["valid_time"]],
                    "cycle": cyc.strftime("%d/%HZ"),
                    "cloudCeiling_ft": cig, "visibility_sm": _col(dm, "vsby_sm"),
                    "windSpeed_kt": _col(dm, "wind_speed_kt"), "windGust_kt": _col(dm, "wind_gust_kt"),
                    "windDirection": _col(dm, "wind_dir_deg"),
                    "tmp_f": _col(dm, "temp_f"), "dpt_f": _col(dm, "dewpoint_f"),
                    "sky_pct": _col(dm, "sky_pct"), "pop1": _col(dm, "pop1"),
                    "qpf_in": _col(dm, "qpf_in"), "tstm_pct": _col(dm, "p_tstm"),
                    "lcb_ft": _col(dm, "lcb_ft"),
                    "p_cig_ifr": _col(dm, "p_cig_ifr"), "p_vis_ifr": _col(dm, "p_vis_ifr")}
                n_nbm += 1
            out["nbm_cycle"] = cyc.isoformat()
    # REFS probabilities at the compare hubs (unchanged from the old builder)
    n_refs = 0
    try:
        from core.hrrr_cam import latest_cycle as _lc
        from core import refs_point as RP
        rc = _lc("refs_prob", 12)
    except Exception:
        rc, RP = None, None
    if rc and rc.isoformat() != doc.get("refs_cycle"):
        hours = list(range(1, 37))
        for sid, la, lo in pts:
            icao = ("K" + sid) if len(sid) == 3 else sid
            if icao not in CW.HUBS:
                continue
            try:
                got = RP.sample(la, lo, rc, hours, products=["PROB_CIG1000", "PROB_VIS3"], max_workers=6)
            except Exception:
                continue
            ts = [(rc + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in hours]
            out["stations"].setdefault(sid, {})["refs"] = {
                "t": ts, "p_cig1000": [got["PROB_CIG1000"].get(h) for h in hours],
                "p_vis3": [got["PROB_VIS3"].get(h) for h in hours]}
            n_refs += 1
        out["refs_cycle"] = rc.isoformat()
    p = _path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    os.replace(tmp, p)
    msg = f"noaa points: nbm {n_nbm} stations ({out['nbm_cycle']}), refs {n_refs} ({out['refs_cycle']})"
    _log(outdir, msg)
    STATUS["last"] = time.time()
    return msg


def _loop(outdir):
    time.sleep(20)
    while True:
        try:
            build(outdir)
            STATUS["err"] = None
        except Exception as exc:
            STATUS["err"] = f"{type(exc).__name__}: {exc}"
            _log(outdir, f"noaa points FAILED: {STATUS['err'][:160]}")
        time.sleep(NOAA_MIN * 60)


def ensure_noaa_points(outdir) -> bool:
    global _started
    with _lock:
        if _started:
            return True
        threading.Thread(target=_loop, args=(outdir,), daemon=True, name="noaa-points").start()
        _started = True
        STATUS["started"] = True
    return True
