"""Model-comparison warmer: every hub's frame, built as each cycle lands.

Target path: core/compare_warm.py

WHY. Station Forecast's plots and grids all read one DataFrame from
compare_icaos(): HRRR, NBM, GFS LAMP and GFS MOS for one station and
one cycle. Measured 21 Sep against live NOMADS: 14.7 s for the first
station of a new cycle (the HRRR subset download), 4.6 s for each
station after it, and still 4.4 s with every model file already on
disk - the parsing alone is most of it. st.cache_data hid that for
ten minutes at a time, so the first viewer after every expiry paid it.

WHAT. A thread probes for the latest complete cycle every few
minutes and, when it changes, runs compare_icaos() for every hub and
writes the finished frame to disk:

    CACHE_ROOT/compare/latest.json          {"cycle": "<iso>", ...}
    CACHE_ROOT/compare/KJFK_2026092112.pkl  the DataFrame

A frame for a given station and cycle never changes once the cycle is
complete, so each is built once. The page reads the pickle (a few
milliseconds) and falls back to computing only if the warmer has not
reached that station yet. About 75 s of work per 6-hour cycle for
14 hubs; I/O-bound apart from the parse.

COMPARE_WARMER=off disables it. COMPARE_HUBS overrides the station
list (comma-separated ICAOs).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

DEFAULT_HUBS = ["KJFK", "KBOS", "KFLL", "KMCO", "KEWR", "KLGA", "KDCA",
                "KLAX", "KSFO", "KTPA", "KDJT", "KBDL", "KHPN", "TJSJ"]
HUBS = [h.strip().upper() for h in
        os.environ.get("COMPARE_HUBS", ",".join(DEFAULT_HUBS)).split(",")
        if h.strip()]
PROBE_S = int(os.environ.get("COMPARE_PROBE_S", "300"))
START_DELAY_S = int(os.environ.get("COMPARE_DELAY_S", "60"))
# Frames older than this many cycles are deleted.
KEEP_CYCLES = 2

_LOCK = threading.Lock()
_STATE = {"thread": None, "log": [], "cycle": None, "done": set()}


def _log(msg: str) -> None:
    with _LOCK:
        _STATE["log"].append(time.strftime("%H:%M:%SZ ", time.gmtime()) + msg)
        del _STATE["log"][:-40]


def log_tail(n: int = 6) -> list:
    with _LOCK:
        return list(_STATE["log"][-n:])


# ------------------------------------------------------------- paths

def _dir(cache_root) -> Path:
    d = Path(cache_root) / "compare"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _frame_path(cache_root, icao: str, cycle: datetime) -> Path:
    return _dir(cache_root) / f"{icao.upper()}_{cycle:%Y%m%d%H}.pkl"


# ----------------------------------------------------------- readers
# Used by the page. Both return None when the warmer has nothing yet,
# and the page then does what it did before.

def latest_cycle(cache_root) -> str | None:
    """ISO cycle the warmer last found complete, or None."""
    try:
        return json.loads((_dir(cache_root) / "latest.json").read_text())["cycle"]
    except Exception:
        return None


def read_frame(cache_root, icao: str, cycle_iso: str):
    """(DataFrame, True) for a station and cycle the warmer has built,
    else None."""
    try:
        import pandas as pd
        cycle = datetime.fromisoformat(cycle_iso)
        p = _frame_path(cache_root, icao, cycle)
        if not p.exists():
            return None
        return pd.read_pickle(p), True
    except Exception:
        return None


# ------------------------------------------------------------ builder

def _probe_cycle(cache_root) -> datetime | None:
    """Latest cycle complete for every model, probed with one station
    (the answer does not depend on which)."""
    from core.stations import StationResolver
    from core.cycle_select import find_latest_complete
    from models import GfsMos, GfsLamp, Hrrr, Nbm

    root = Path(cache_root)
    resolved, _ = StationResolver(
        cache_dir=root / "stations").resolve_many([HUBS[0]])
    if not resolved:
        return None
    probes = [GfsMos(cache_dir=root / "gfs_mos"),
              GfsLamp(cache_dir=root / "gfs_lamp"),
              Hrrr(cache_dir=root / "hrrr", stations=resolved,
                   fhours=range(0, 19)),
              Nbm(cache_dir=root / "nbm")]
    return find_latest_complete(probes, verbose=False)


def build_one(cache_root, icao: str, cycle: datetime) -> bool:
    """compare_icaos for one station, saved. False if nothing came back."""
    from compare import compare_icaos

    p = _frame_path(cache_root, icao, cycle)
    if p.exists():
        return True
    df, resolved, _ = compare_icaos(icaos=[icao], cycle=cycle,
                                    cache_root=Path(cache_root))
    if not resolved or df is None or not len(df):
        return False
    tmp = p.with_suffix(".pkl.tmp")
    df.to_pickle(tmp)
    os.replace(tmp, p)
    return True


def run_pass(cache_root) -> str:
    cycle = _probe_cycle(cache_root)
    if cycle is None:
        return "no complete cycle found"
    ciso = cycle.isoformat()
    with _LOCK:
        new_cycle = _STATE["cycle"] != ciso
        if new_cycle:
            _STATE["cycle"] = ciso
            _STATE["done"] = set()
    built, failed = 0, []
    t0 = time.time()
    for icao in HUBS:
        with _LOCK:
            if icao in _STATE["done"]:
                continue
        try:
            ok = build_one(cache_root, icao, cycle)
        except Exception as exc:
            ok = False
            _log(f"{icao} {cycle:%d/%HZ} FAILED: "
                 f"{type(exc).__name__}: {exc}"[:160])
        if ok:
            built += 1
            with _LOCK:
                _STATE["done"].add(icao)
        else:
            failed.append(icao)
    # latest.json last, after the frames, so a page that reads the
    # cycle finds the frames that go with it.
    d = _dir(cache_root)
    tmp = d / "latest.json.tmp"
    tmp.write_text(json.dumps({"cycle": ciso, "hubs": HUBS,
                               "built": sorted(_STATE["done"]),
                               "at": time.time()}))
    os.replace(tmp, d / "latest.json")
    # Prune frames from older cycles.
    stamps = sorted({p.name.rsplit("_", 1)[1][:10]
                     for p in d.glob("*.pkl")}, reverse=True)
    for old in stamps[KEEP_CYCLES:]:
        for p in d.glob(f"*_{old}.pkl"):
            try:
                p.unlink()
            except OSError:
                pass
    if built or failed:
        return (f"cycle {cycle:%d/%HZ}: {built} built in "
                f"{time.time() - t0:.0f}s"
                + (f", no data for {','.join(failed)}" if failed else ""))
    return "cached"


def _loop(cache_root):
    time.sleep(START_DELAY_S)
    _log(f"compare warmer started, {len(HUBS)} hubs")
    while True:
        try:
            note = run_pass(cache_root)
            if note != "cached":
                _log(note)
        except Exception as exc:
            _log(f"pass FAILED: {type(exc).__name__}: {exc}"[:160])
        time.sleep(PROBE_S)


def ensure_compare_warmer(cache_root) -> bool:
    """Idempotent. COMPARE_WARMER=off disables without a deploy."""
    if os.environ.get("COMPARE_WARMER", "on").lower() == "off":
        return False
    with _LOCK:
        t = _STATE["thread"]
        if t is not None and t.is_alive():
            return True
        t = threading.Thread(target=_loop, args=(Path(cache_root),),
                             daemon=True, name="compare-warmer")
        t.start()
        _STATE["thread"] = t
    return True
