"""JBU movement sampler: BlueMet's own arrivals/departures log.

OpenSky's flights database is unreachable from the app server, so we
build the movement log ourselves from a source that IS reachable:
adsb.lol (already the app's live-position source, JBU-filtered at the
query). A background daemon polls each sampled airport every couple
of minutes for low-altitude JetBlue aircraft and appends compact
observations to the persistent disk. Movements are derived on read:
an aircraft that appears low and climbs out is a departure; one that
descends and disappears near the field is an arrival.

Honest properties:
  - JBU only, by construction (the position query is JBU-scoped).
  - Coverage starts at deploy time; no history before day one.
  - Fresh within ~2-4 minutes (vs OpenSky's hour-plus batch lag).
  - Overflights don't trigger: flat-altitude transits match neither
    the climb-out nor the descend-in rule.
Env kill switch: MOV_SAMPLER=off.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

SAMPLED_AIRPORTS = {
    "KJFK": (40.6413, -73.7781),
    "KMCO": (28.4312, -81.3081),
    "KFLL": (26.0726, -80.1527),
    "KDCA": (38.8512, -77.0402),
    "KDJT": (26.6832, -80.0956),   # ex-KPBI (renamed 2026-07-09)
}
POLL_INTERVAL_S = 120
RADIUS_DEG = 0.6
MAX_OBS_ALT_FT = 12000        # only record the terminal-area band
OBS_RETENTION_DAYS = 3

# Movement derivation thresholds
SESSION_GAP_S = 900           # >15 min silence splits sessions
LOW_ALT_FT = 4000             # "near the field" band
TREND_FT = 1500               # required climb/descend across session

_started = False
_lock = threading.Lock()


def _obs_dir(cache_root: Path, icao: str) -> Path:
    d = cache_root / "movements" / "obs" / icao.upper()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record(cache_root: Path, icao: str, callsign: str,
            alt_ft, lat: float, lon: float) -> None:
    now = datetime.now(timezone.utc)
    path = _obs_dir(cache_root, icao) / f"{now:%Y%m%d}.jsonl"
    rec = {
        "ts": int(now.timestamp()),
        "cs": callsign.upper(),
        "alt": int(alt_ft) if alt_ft is not None else None,
        "lat": round(lat, 3),
        "lon": round(lon, 3),
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _prune(cache_root: Path) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() \
        - OBS_RETENTION_DAYS * 86400
    base = cache_root / "movements" / "obs"
    if not base.exists():
        return
    for adir in base.iterdir():
        if not adir.is_dir():
            continue
        for f in adir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def _sample_once(cache_root: Path, log) -> None:
    from core.flights import fetch_positions_near

    for icao, (lat, lon) in SAMPLED_AIRPORTS.items():
        try:
            planes = fetch_positions_near(lat, lon,
                                          radius_deg=RADIUS_DEG)
        except Exception:
            continue
        n = 0
        for p in planes:
            if p.alt_ft is not None and p.alt_ft > MAX_OBS_ALT_FT:
                continue
            _record(cache_root, icao, p.callsign, p.alt_ft,
                    p.lat, p.lon)
            n += 1
        if n:
            log(f"{icao}: {n} obs")


def _daemon(cache_root: Path) -> None:
    log_path = cache_root / "movements" / "sampler.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        try:
            with open(log_path, "a") as fh:
                fh.write(
                    f"{datetime.now(timezone.utc):%m-%d %H:%M:%S} "
                    f"{msg}\n"
                )
        except OSError:
            pass

    log("movement sampler started")
    cycles = 0
    while True:
        try:
            _sample_once(cache_root, log)
        except Exception:
            log(f"sample failed:\n{traceback.format_exc()}")
        cycles += 1
        if cycles % 30 == 0:
            _prune(cache_root)
        time.sleep(POLL_INTERVAL_S)


def ensure_sampler_started(cache_root: Path) -> None:
    """Idempotent per-process start. MOV_SAMPLER=off disables."""
    global _started
    if os.environ.get("MOV_SAMPLER", "on").lower() == "off":
        return
    with _lock:
        if _started:
            return
        threading.Thread(
            target=_daemon, args=(cache_root,), daemon=True,
            name="jbu-movement-sampler",
        ).start()
        _started = True


def sampling_since(cache_root: Path, icao: str):
    """Earliest observation timestamp for an airport, or None."""
    d = cache_root / "movements" / "obs" / icao.upper()
    if not d.exists():
        return None
    earliest = None
    for f in sorted(d.glob("*.jsonl"))[:1]:
        for line in f.read_text().splitlines()[:1]:
            try:
                earliest = json.loads(line)["ts"]
            except (ValueError, KeyError):
                pass
    return earliest


def derive_movements(cache_root: Path, icao: str,
                     hours_back: int) -> list[dict]:
    """Arrivals/departures derived from the observation log.

    Returns [{"callsign", "kind" (ARR/DEP), "time_unix", "alt_from",
    "alt_to"}], newest first.
    """
    icao = icao.upper()
    # KDJT/KPBI are the same field; the sampler logs under KDJT
    if icao == "KPBI":
        icao = "KDJT"
    d = cache_root / "movements" / "obs" / icao
    if not d.exists():
        return []
    cutoff = time.time() - hours_back * 3600

    obs_by_cs: dict[str, list] = {}
    for f in sorted(d.glob("*.jsonl")):
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec["ts"] < cutoff - SESSION_GAP_S:
                continue
            obs_by_cs.setdefault(rec["cs"], []).append(rec)

    movements = []
    for cs, obs in obs_by_cs.items():
        obs.sort(key=lambda r: r["ts"])
        # Split into sessions at silence gaps
        sessions: list[list] = [[obs[0]]]
        for rec in obs[1:]:
            if rec["ts"] - sessions[-1][-1]["ts"] > SESSION_GAP_S:
                sessions.append([rec])
            else:
                sessions[-1].append(rec)
        for sess in sessions:
            alts = [r["alt"] for r in sess if r["alt"] is not None]
            if len(alts) < 2:
                continue
            first_a, last_a = alts[0], alts[-1]
            t_first, t_last = sess[0]["ts"], sess[-1]["ts"]
            if (first_a <= LOW_ALT_FT
                    and last_a >= first_a + TREND_FT):
                kind, t = "DEP", t_first
            elif (last_a <= LOW_ALT_FT
                    and first_a >= last_a + TREND_FT):
                kind, t = "ARR", t_last
            else:
                continue
            if t < cutoff:
                continue
            movements.append({
                "callsign": cs,
                "kind": kind,
                "time_unix": t,
                "alt_from": first_a,
                "alt_to": last_a,
            })
    movements.sort(key=lambda m: -m["time_unix"])
    return movements
