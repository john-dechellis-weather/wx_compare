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

# The full JBU destination network. Coordinates are resolved at daemon
# start via the station resolver, then destinations are greedily
# clustered into wide sweep circles - each poll cycle queries ~18-20
# circles that together cover every terminal area, catching all
# low-altitude JBU network-wide for about the cost of the old
# five-airport design. Airports whose coordinates fail to resolve are
# skipped (and logged) rather than fatal.
JBU_AIRPORT_ICAOS = [
    "KJFK", "KEWR", "KLGA", "KHPN", "KISP", "KPHL", "KBOS", "KORH",
    "KBDL", "KPVD", "KPWM", "KPQI", "KACK", "KHYA", "KMVY", "KALB",
    "KSYR", "KROC", "KBUF", "KPIT",
    "KDCA", "KBWI", "KRIC", "KORF", "KILM", "KRDU", "KCLT", "KCHS",
    "KSAV",
    "KJAX", "KVPS", "KVRB", "KMCO", "KDAB", "KTPA", "KSRQ", "KRSW",
    "KDJT", "KFLL", "KEYW",
    "KORD", "KMKE", "KTVC", "KDTW", "KCLE", "KBNA", "KATL", "KMSY",
    "KDFW", "KAUS", "KIAH", "KABQ", "KPHX",
    "KBUR", "KLAX", "KSAN", "KONT", "KLAS",
    "KSFO", "KRNO", "KSMF", "KSLC", "KBZN", "KDEN", "KHDN", "KSEA",
    "KPDX", "CYVR",
    "TXKF", "MYNN", "MBPV", "TJSJ", "TJPS", "TJBQ", "TIST", "TISX",
    "TNCM", "TKPK", "TAPA", "TLPL", "TVSA", "TBPB", "TGPY", "TTPP",
    "SYCJ", "MDST", "MDSD", "MDPP", "MDPC",
    "TNCA", "TNCC", "TNCB", "MKJP", "MKJS", "MWCR",
]

ICAO_ALIASES = {"KPBI": "KDJT"}


def is_sampled(icao: str) -> bool:
    code = icao.upper()
    code = ICAO_ALIASES.get(code, code)
    return code in JBU_AIRPORT_ICAOS


POLL_INTERVAL_S = 180
SWEEP_RADIUS_DEG = 3.5        # per sweep circle (~210 nm)
ATTRIB_RADIUS_DEG = 0.35      # obs -> nearest airport within ~35 km
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


_resolved: dict = {}      # icao -> (lat, lon), built once
_sweep_centers: list = []  # [(lat, lon), ...]
_cache_root_holder: list = [Path("/tmp/wx_compare_cache")]


def _prepare_network(log) -> None:
    """Resolve airport coordinates and cluster into sweep circles.
    Runs once at daemon start; safe to re-run."""
    from core.stations import StationResolver

    if _sweep_centers:
        return
    resolver = StationResolver(cache_dir=_cache_root_holder[0]
                               / "stations")
    misses = []
    for icao in JBU_AIRPORT_ICAOS:
        try:
            stn = resolver.resolve(icao)
        except Exception:
            stn = None
        if stn is None:
            misses.append(icao)
            continue
        _resolved[icao] = (float(stn.lat), float(stn.lon))
    if misses:
        log(f"unresolved (skipped): {', '.join(misses)}")

    # Greedy clustering: each airport joins an existing center within
    # SWEEP_RADIUS - ATTRIB margin, else seeds a new one.
    for lat, lon in _resolved.values():
        for i, (cla, clo) in enumerate(_sweep_centers):
            if (abs(lat - cla) <= SWEEP_RADIUS_DEG - 0.5
                    and abs(lon - clo) <= SWEEP_RADIUS_DEG - 0.5):
                break
        else:
            _sweep_centers.append((lat, lon))
    log(f"network: {len(_resolved)} airports in "
        f"{len(_sweep_centers)} sweep circles")


def _nearest_airport(lat: float, lon: float):
    best, best_d = None, ATTRIB_RADIUS_DEG
    for icao, (ala, alo) in _resolved.items():
        d = max(abs(lat - ala), abs(lon - alo))
        if d < best_d:
            best, best_d = icao, d
    return best


def _sample_once(cache_root: Path, log) -> None:
    from core.flights import fetch_positions_near

    _prepare_network(log)
    n_total = 0
    for cla, clo in _sweep_centers:
        try:
            planes = fetch_positions_near(
                cla, clo, radius_deg=SWEEP_RADIUS_DEG
            )
        except Exception:
            continue
        for p in planes:
            if p.alt_ft is not None and p.alt_ft > MAX_OBS_ALT_FT:
                continue
            icao = _nearest_airport(p.lat, p.lon)
            if icao is None:
                continue
            _record(cache_root, icao, p.callsign, p.alt_ft,
                    p.lat, p.lon)
            n_total += 1
        time.sleep(0.3)   # gentle pacing between sweep queries
    if n_total:
        log(f"sweep: {n_total} obs across network")


def _daemon(cache_root: Path) -> None:
    _cache_root_holder[0] = cache_root
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
    icao = ICAO_ALIASES.get(icao, icao)
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
