"""Live aircraft positions from community ADS-B aggregators.

Primary: adsb.lol (keyless, point+radius). Fallback: OpenSky (keyless,
bounding box, tighter rate limits). Both are community services — treat
failures as normal and degrade gracefully.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import requests

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}


@dataclass
class AircraftPos:
    callsign: str
    lat: float
    lon: float
    alt_ft: Optional[float]      # barometric, feet (None on ground/unknown)
    heading_deg: Optional[float]
    hex: Optional[str] = None    # ICAO24 transponder hex (for track lookups)


def fetch_positions_near(
    lat: float,
    lon: float,
    radius_deg: float,
    callsign_prefix: str = "JBU",
) -> list[AircraftPos]:
    """Live positions within radius_deg of (lat, lon) whose callsign
    starts with the prefix. Empty list on total failure."""
    radius_nm = max(10, int(radius_deg * 60))  # 1 deg lat ~ 60 nm

    out = _try_adsb_lol(lat, lon, radius_nm, callsign_prefix)
    if out is not None:
        return out
    out = _try_opensky(lat, lon, radius_deg, callsign_prefix)
    return out if out is not None else []


def _try_adsb_lol(lat, lon, radius_nm, prefix) -> Optional[list[AircraftPos]]:
    try:
        r = requests.get(
            f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius_nm}",
            headers=_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        planes = payload.get("ac", []) or []
        out = []
        for p in planes:
            cs = (p.get("flight") or "").strip().upper()
            if not cs.startswith(prefix):
                continue
            plat, plon = p.get("lat"), p.get("lon")
            if plat is None or plon is None:
                continue
            alt = p.get("alt_baro")
            alt_ft = float(alt) if isinstance(alt, (int, float)) else None
            trk = p.get("track")
            out.append(AircraftPos(
                callsign=cs,
                lat=float(plat),
                lon=float(plon),
                alt_ft=alt_ft,
                heading_deg=float(trk) if trk is not None else None,
                hex=(p.get("hex") or "").strip().lower() or None,
            ))
        return out
    except Exception:
        return None


def _try_opensky(lat, lon, radius_deg, prefix) -> Optional[list[AircraftPos]]:
    try:
        r = requests.get(
            "https://opensky-network.org/api/states/all",
            params={
                "lamin": lat - radius_deg,
                "lamax": lat + radius_deg,
                "lomin": lon - radius_deg,
                "lomax": lon + radius_deg,
            },
            headers=_HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        states = payload.get("states") or []
        out = []
        for s in states:
            # state vector: [icao24, callsign, origin_country, time_position,
            #   last_contact, lon, lat, baro_alt_m, on_ground, velocity,
            #   true_track, ...]
            cs = (s[1] or "").strip().upper()
            if not cs.startswith(prefix):
                continue
            plon, plat = s[5], s[6]
            if plat is None or plon is None:
                continue
            alt_m = s[7]
            out.append(AircraftPos(
                callsign=cs,
                lat=float(plat),
                lon=float(plon),
                alt_ft=float(alt_m) * 3.28084 if alt_m is not None else None,
                heading_deg=float(s[10]) if s[10] is not None else None,
            ))
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Historical positions (OpenSky authenticated "time travel", last ~1 hour)
# ---------------------------------------------------------------------------
import os
import time as _time

_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
_token_cache: dict = {"token": None, "expires": 0.0}
_last_error: dict = {"msg": None}


def last_error():
    """Most recent OpenSky failure detail, or None."""
    return _last_error["msg"]


def _opensky_token() -> Optional[str]:
    """OAuth2 client-credentials token, cached until near expiry.
    Requires OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET env vars."""
    cid = os.environ.get("OPENSKY_CLIENT_ID")
    secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    if _token_cache["token"] and _time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    try:
        r = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            },
            headers=_HEADERS,
            timeout=20,
        )
        if r.status_code != 200:
            _last_error["msg"] = (
                f"token HTTP {r.status_code}: {r.text[:120]}"
            )
            return None
        payload = r.json()
        _token_cache["token"] = payload["access_token"]
        _last_error["msg"] = None
        _token_cache["expires"] = _time.time() + int(
            payload.get("expires_in", 1800)
        )
        return _token_cache["token"]
    except Exception as e:
        _last_error["msg"] = f"token {type(e).__name__}: {e}"
        return None


def historical_positions_available() -> bool:
    """True when OpenSky credentials are configured."""
    return bool(
        os.environ.get("OPENSKY_CLIENT_ID")
        and os.environ.get("OPENSKY_CLIENT_SECRET")
    )


def fetch_positions_at(
    lat: float,
    lon: float,
    radius_deg: float,
    when_unix: int,
    callsign_prefix: str = "JBU",
) -> list[AircraftPos]:
    """Positions at a specific past moment (OpenSky supports roughly the
    last hour for authenticated users). Empty list on failure or no creds.
    Note: creds present but token/permission failure also yields empty
    lists — which renders as NO planes (distinct from the static mode,
    which shows frozen planes)."""
    token = _opensky_token()
    if token is None:
        if _last_error["msg"] is None:
            _last_error["msg"] = "no credentials"
        return []
    try:
        r = requests.get(
            "https://opensky-network.org/api/states/all",
            params={
                "time": int(when_unix),
                "lamin": lat - radius_deg,
                "lamax": lat + radius_deg,
                "lomin": lon - radius_deg,
                "lomax": lon + radius_deg,
            },
            headers={**_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=20,
        )
        if r.status_code != 200:
            _last_error["msg"] = (
                f"states HTTP {r.status_code}: {r.text[:120]}"
            )
            return []
        payload = r.json()
        states = payload.get("states") or []
        out = []
        for s in states:
            cs = (s[1] or "").strip().upper()
            if not cs.startswith(callsign_prefix):
                continue
            plon, plat = s[5], s[6]
            if plat is None or plon is None:
                continue
            alt_m = s[7]
            out.append(AircraftPos(
                callsign=cs,
                lat=float(plat),
                lon=float(plon),
                alt_ft=float(alt_m) * 3.28084 if alt_m is not None else None,
                heading_deg=float(s[10]) if s[10] is not None else None,
            ))
        return out
    except Exception as e:
        _last_error["msg"] = f"states {type(e).__name__}: {e}"
        return []


# ---------------------------------------------------------------------------
# Self-recorded history: append live snapshots to disk; the radar loop
# matches each frame to the snapshot nearest its scan time. Accumulates
# naturally during active use — no external history API required.
# ---------------------------------------------------------------------------
import json
from pathlib import Path


def record_snapshot(
    history_dir: Path,
    icao: str,
    positions: list[AircraftPos],
    min_interval_s: int = 45,
    max_age_s: int = 7200,
) -> None:
    """Append a timestamped snapshot for this airport (throttled), and
    prune entries older than max_age_s."""
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / f"{icao.upper()}.jsonl"
    now = _time.time()

    lines: list[str] = []
    last_ts = 0.0
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if now - rec["ts"] <= max_age_s:
                lines.append(line)
                last_ts = max(last_ts, rec["ts"])

    if now - last_ts >= min_interval_s:
        lines.append(json.dumps({
            "ts": now,
            "ac": [
                {
                    "cs": p.callsign, "lat": p.lat, "lon": p.lon,
                    "alt": p.alt_ft, "trk": p.heading_deg,
                }
                for p in positions
            ],
        }))
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def positions_at_time(
    history_dir: Path,
    icao: str,
    when_unix: float,
    tolerance_s: int = 300,
) -> Optional[list[AircraftPos]]:
    """Snapshot nearest when_unix within tolerance, or None."""
    path = history_dir / f"{icao.upper()}.jsonl"
    if not path.exists():
        return None
    best = None
    best_dt = tolerance_s + 1
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        dt = abs(rec["ts"] - when_unix)
        if dt < best_dt:
            best_dt = dt
            best = rec
    if best is None:
        return None
    return [
        AircraftPos(
            callsign=a["cs"], lat=a["lat"], lon=a["lon"],
            alt_ft=a.get("alt"), heading_deg=a.get("trk"),
        )
        for a in best["ac"]
    ]


def _load_history(history_dir: Path, icao: str, max_age_s: int = 7200):
    path = history_dir / f"{icao.upper()}.jsonl"
    if not path.exists():
        return []
    now = _time.time()
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if now - rec["ts"] <= max_age_s:
            out.append(rec)
    out.sort(key=lambda r: r["ts"])
    return out


def _rec_to_positions(rec) -> list[AircraftPos]:
    return [
        AircraftPos(
            callsign=a["cs"], lat=a["lat"], lon=a["lon"],
            alt_ft=a.get("alt"), heading_deg=a.get("trk"),
        )
        for a in rec["ac"]
    ]


def interpolate_at(
    history_dir: Path,
    icao: str,
    when_unix: float,
    tolerance_s: int = 240,
) -> Optional[list[AircraftPos]]:
    """Aircraft positions at an arbitrary moment.

    Linear interpolation per callsign between the snapshots bracketing
    when_unix; aircraft present on only one side are shown at that
    snapshot's position when it's within tolerance. Returns None when
    no snapshot is anywhere near the requested time."""
    hist = _load_history(history_dir, icao)
    if not hist:
        return None

    before = None
    after = None
    for rec in hist:
        if rec["ts"] <= when_unix:
            before = rec
        elif after is None:
            after = rec
            break

    if before is None and after is None:
        return None
    if before is None:
        return (_rec_to_positions(after)
                if after["ts"] - when_unix <= tolerance_s else None)
    if after is None:
        return (_rec_to_positions(before)
                if when_unix - before["ts"] <= tolerance_s else None)

    span = after["ts"] - before["ts"]
    if span <= 0 or span > 2 * tolerance_s:
        # Bracketing gap too wide to trust a lerp; use the nearer side.
        nearer = before if (when_unix - before["ts"]) <= (
            after["ts"] - when_unix) else after
        return (_rec_to_positions(nearer)
                if abs(nearer["ts"] - when_unix) <= tolerance_s else None)

    frac = (when_unix - before["ts"]) / span
    b = {a["cs"]: a for a in before["ac"]}
    aft = {a["cs"]: a for a in after["ac"]}
    out: list[AircraftPos] = []
    for cs in set(b) | set(aft):
        if cs in b and cs in aft:
            pa, pb = b[cs], aft[cs]
            lat = pa["lat"] + (pb["lat"] - pa["lat"]) * frac
            lon = pa["lon"] + (pb["lon"] - pa["lon"]) * frac
            alt_a, alt_b = pa.get("alt"), pb.get("alt")
            alt = (alt_a + (alt_b - alt_a) * frac
                   if alt_a is not None and alt_b is not None else alt_a)
            out.append(AircraftPos(cs, lat, lon, alt, pa.get("trk")))
        elif cs in b and frac < 0.5:
            a = b[cs]
            out.append(AircraftPos(cs, a["lat"], a["lon"],
                                   a.get("alt"), a.get("trk")))
        elif cs in aft and frac >= 0.5:
            a = aft[cs]
            out.append(AircraftPos(cs, a["lat"], a["lon"],
                                   a.get("alt"), a.get("trk")))
    return out


# Community ADS-B aggregators sharing the same /v2 API schema. If the
# primary's callsign lookup fails or comes back empty, the mirrors are
# asked the same question - a flight visible to any of them is found.
_ADSB_SOURCES = [
    ("adsb.lol", "https://api.adsb.lol/v2/callsign/{cs}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/callsign/{cs}"),
    ("airplanes.live", "https://api.airplanes.live/v2/callsign/{cs}"),
]

_callsign_diag: dict = {"msg": None}


def last_callsign_diag():
    return _callsign_diag["msg"]


def fetch_callsign(callsign: str) -> Optional[AircraftPos]:
    """Live position of a specific flight by exact callsign, tried
    across multiple ADS-B aggregators. Returns None only when every
    source says not-found/unreachable; last_callsign_diag() then holds
    a per-source summary for display."""
    cs = callsign.strip().upper()
    diags = []
    for src_name, url_t in _ADSB_SOURCES:
        pos = _try_callsign_source(cs, url_t, diags, src_name)
        if pos is not None:
            _callsign_diag["msg"] = None
            return pos
    _callsign_diag["msg"] = "; ".join(diags)
    return None


def _try_callsign_source(
    cs: str, url_t: str, diags: list, src_name: str
) -> Optional[AircraftPos]:
    try:
        r = requests.get(
            url_t.format(cs=cs),
            headers=_HEADERS,
            timeout=12,
        )
        if r.status_code != 200:
            diags.append(f"{src_name}: HTTP {r.status_code}")
            return None
        planes = r.json().get("ac", []) or []
        for p in planes:
            plat, plon = p.get("lat"), p.get("lon")
            if plat is None or plon is None:
                continue
            alt = p.get("alt_baro")
            trk = p.get("track")
            return AircraftPos(
                callsign=(p.get("flight") or cs).strip().upper(),
                lat=float(plat),
                lon=float(plon),
                alt_ft=float(alt) if isinstance(alt, (int, float)) else None,
                heading_deg=float(trk) if trk is not None else None,
                hex=(p.get("hex") or "").strip().lower() or None,
            )
        diags.append(f"{src_name}: no aircraft with that callsign")
        return None
    except Exception as e:
        diags.append(f"{src_name}: {type(e).__name__}")
        return None


# OpenSky reachability memory: the host is confirmed blocked from
# some deployment environments (ConnectTimeout). After any connection
# failure we skip OpenSky entirely for a cooldown window rather than
# paying a timeout on every call.
_opensky_dead_until: list[float] = [0.0]
_OPENSKY_COOLDOWN_S = 1800


def _opensky_available() -> bool:
    return _time.time() >= _opensky_dead_until[0]


def _mark_opensky_dead() -> None:
    _opensky_dead_until[0] = _time.time() + _OPENSKY_COOLDOWN_S


def fetch_track_opensky(icao24: str) -> Optional[list[tuple[float, float]]]:
    """Waypoints of the aircraft's current flight via OpenSky's tracks
    endpoint (anonymous, experimental). None on failure — including the
    host being unreachable from the server — so callers can fall back."""
    if not _opensky_available():
        return None
    try:
        r = requests.get(
            "https://opensky-network.org/api/tracks/all",
            params={"icao24": icao24.lower(), "time": 0},
            headers=_HEADERS,
            timeout=6,
        )
        if r.status_code != 200:
            return None
        path = r.json().get("path") or []
        pts = []
        for wp in path:
            # [time, lat, lon, baro_alt, true_track, on_ground]
            if wp[1] is not None and wp[2] is not None:
                pts.append((float(wp[1]), float(wp[2])))
        return pts if pts else None
    except requests.exceptions.ConnectionError:
        _mark_opensky_dead()
        return None
    except Exception:
        return None


def record_track_point(
    track_dir: Path,
    callsign: str,
    lat: float,
    lon: float,
    min_interval_s: int = 60,
    max_age_s: int = 10800,
) -> None:
    """Append the target's position to its own trail file (throttled,
    pruned) — the self-recorded fallback when OpenSky is unreachable."""
    track_dir.mkdir(parents=True, exist_ok=True)
    path = track_dir / f"{callsign.upper()}.jsonl"
    now = _time.time()
    lines: list[str] = []
    last_ts = 0.0
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if now - rec["ts"] <= max_age_s:
                lines.append(line)
                last_ts = max(last_ts, rec["ts"])
    if now - last_ts >= min_interval_s:
        lines.append(json.dumps(
            {"ts": now, "lat": round(lat, 4), "lon": round(lon, 4)}
        ))
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def load_track(track_dir: Path, callsign: str) -> list[tuple[float, float]]:
    path = track_dir / f"{callsign.upper()}.jsonl"
    if not path.exists():
        return []
    pts = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
            pts.append((rec["lat"], rec["lon"]))
        except (ValueError, KeyError):
            continue
    return pts


_route_error: dict = {"msg": None}


def last_route_error():
    return _route_error["msg"]


def fetch_routes(planes: list[AircraftPos]) -> dict:
    """Origin/destination for a set of live callsigns via adsb.lol's
    routeset API (community route DB). Returns
    {callsign: {"label": "JFK-FLL", "orig": (lat, lon),
                "dest": (lat, lon)}} — only entries with both airports
    resolved. Empty dict on failure."""
    if not planes:
        return {}
    body = {
        "planes": [
            {"callsign": p.callsign, "lat": p.lat, "lng": p.lon}
            for p in planes
        ]
    }
    _route_error["msg"] = None
    try:
        r = requests.post(
            "https://api.adsb.lol/api/0/routeset",
            json=body,
            headers={**_HEADERS, "Content-Type": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            _route_error["msg"] = (
                f"HTTP {r.status_code}: {r.text[:150]}"
            )
            return {}
        payload = r.json()
        if not payload:
            _route_error["msg"] = "empty response (no callsigns matched)"
        elif not isinstance(payload, list):
            _route_error["msg"] = f"unexpected shape: {str(payload)[:150]}"
        out = {}
        for item in (payload if isinstance(payload, list) else []):
            cs = (item.get("callsign") or "").strip().upper()
            airports = item.get("_airports") or []
            if len(airports) < 2 or not cs:
                continue
            o, d = airports[0], airports[-1]
            if None in (o.get("lat"), o.get("lon"),
                        d.get("lat"), d.get("lon")):
                continue
            label = (
                f"{o.get('iata') or o.get('icao') or '?'}"
                f"-{d.get('iata') or d.get('icao') or '?'}"
            )
            out[cs] = {
                "label": label,
                "orig": (float(o["lat"]), float(o["lon"])),
                "dest": (float(d["lat"]), float(d["lon"])),
            }
        if not out and _route_error["msg"] is None:
            sample = str((payload or [None])[0])[:200]
            _route_error["msg"] = (
                f"{len(payload or [])} items, none parsed — "
                f"first item: {sample}"
            )
        return out
    except Exception as e:
        _route_error["msg"] = f"{type(e).__name__}: {e}"
        return {}


# Globe trace endpoints: full recorded path of an airframe since 00Z,
# served per-hex by the aggregators' map backends. Directory sharding
# is by the LAST TWO hex characters.
_TRACE_SOURCES = [
    ("adsb.lol trace",
     "https://globe.adsb.lol/data/traces/{shard}/trace_full_{hex}.json"),
    ("adsb.fi trace",
     "https://globe.adsb.fi/data/traces/{shard}/trace_full_{hex}.json"),
    ("airplanes.live trace",
     "https://globe.airplanes.live/data/traces/{shard}/"
     "trace_full_{hex}.json"),
]


def fetch_track_trace(
    icao24: str,
) -> tuple[Optional[list[tuple[float, float]]], Optional[str]]:
    """Complete current-flight path from a globe trace file.

    Returns (points, source_name) or (None, None). The trace covers
    the whole day, so we slice to the CURRENT airborne segment: points
    after the last ground contact / >20 min gap.
    """
    hx = icao24.lower().strip()
    if len(hx) < 2:
        return None, None
    shard = hx[-2:]
    for src_name, url_t in _TRACE_SOURCES:
        try:
            r = requests.get(
                url_t.format(shard=shard, hex=hx),
                headers=_HEADERS,
                timeout=10,
            )
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        base_ts = data.get("timestamp")
        trace = data.get("trace") or []
        if not trace or base_ts is None:
            continue
        # Walk the day's trace; restart the segment at ground contact
        # or a long silence gap - what remains is the current flight.
        seg: list[tuple[float, float]] = []
        prev_t = None
        for p in trace:
            try:
                t_off, lat, lon, alt = p[0], p[1], p[2], p[3]
            except (IndexError, TypeError):
                continue
            if lat is None or lon is None:
                continue
            on_ground = (alt == "ground")
            gap = (prev_t is not None
                   and (t_off - prev_t) > 1200)
            if on_ground or gap:
                seg = []
                prev_t = t_off
                if on_ground:
                    continue
            prev_t = t_off
            seg.append((round(float(lat), 4), round(float(lon), 4)))
        if len(seg) >= 2:
            # Thin very dense traces for rendering sanity
            if len(seg) > 600:
                step = len(seg) // 600 + 1
                seg = seg[::step] + [seg[-1]]
            return seg, src_name
    return None, None


def fetch_fleet_trails(
    planes: list[AircraftPos],
    track_dir: Path,
) -> tuple[dict, str]:
    """Trails for every aircraft: OpenSky tracks in parallel (with a
    circuit breaker - if the first probes fail, OpenSky is skipped for
    the rest), self-recorded trail as per-aircraft fallback. Always
    banks current positions so fallback trails grow regardless.

    Returns ({callsign: tuple((lat, lon), ...)}, summary_string).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Bank everyone's current fix first (cheap, powers the fallback)
    for p in planes:
        try:
            record_track_point(track_dir, p.callsign, p.lat, p.lon)
        except Exception:
            pass

    trails: dict = {}
    n_trace = n_self = 0

    # Primary: globe trace files (complete takeoff-to-now path),
    # fetched in parallel across the fleet.
    probeable = [p for p in planes if p.hex]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {
            ex.submit(fetch_track_trace, p.hex): p
            for p in probeable
        }
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                pts, _src = fut.result()
            except Exception:
                pts = None
            if pts and len(pts) >= 2:
                trails[p.callsign] = tuple(pts)
                n_trace += 1

    # Self-recorded fallback for everyone still trail-less
    for p in planes:
        if p.callsign in trails:
            continue
        own = load_track(track_dir, p.callsign)
        if len(own) >= 2:
            trails[p.callsign] = tuple(own)
            n_self += 1

    if n_trace and n_self:
        summary = (f"{n_trace} full traces, "
                   f"{n_self} self-recorded")
    elif n_trace:
        summary = f"{n_trace} full traces (takeoff to now)"
    elif n_self:
        summary = (f"{n_self} self-recorded (trace endpoints "
                   "unreachable; trails grow while tracking)")
    else:
        summary = "none yet (trails build as the page refreshes)"
    return trails, summary


_airport_flights_error: dict = {"msg": None}


def last_airport_flights_error():
    return _airport_flights_error["msg"]


def fetch_airport_flights(
    icao: str,
    begin_unix: int,
    end_unix: int,
    airline_prefix: str = "JBU",
) -> list[dict]:
    """Arrivals and departures at an airport over [begin, end] via
    OpenSky's flights endpoints (anonymous; batch-processed upstream,
    so expect an hour-plus lag behind real time). Filters callsigns by
    airline_prefix. Tries renamed-airport aliases (e.g. KDJT also
    queries KPBI) and merges.

    Returns [{"callsign", "kind" (ARR/DEP), "other" (airport or "?"),
              "time_unix"}], newest first. Empty list on failure with
    the reason in last_airport_flights_error().
    """
    _airport_flights_error["msg"] = None
    if not _opensky_available():
        _airport_flights_error["msg"] = (
            "OpenSky is unreachable from this server (host blocked); "
            "skipping until cooldown expires"
        )
        return []
    codes = [icao.upper()]
    # Rename transition: OpenSky's aerodrome DB may know either code
    renames = {"KDJT": "KPBI", "KPBI": "KDJT"}
    if icao.upper() in renames:
        codes.append(renames[icao.upper()])

    rows: list[dict] = []
    seen = set()
    any_ok = False
    last_err = None
    for code in codes:
        for kind, endpoint, other_key, t_key in (
            ("ARR", "arrival", "estDepartureAirport", "lastSeen"),
            ("DEP", "departure", "estArrivalAirport", "firstSeen"),
        ):
            try:
                r = requests.get(
                    f"https://opensky-network.org/api/flights/{endpoint}",
                    params={"airport": code, "begin": begin_unix,
                            "end": end_unix},
                    headers=_HEADERS,
                    timeout=15,
                )
            except requests.exceptions.ConnectionError as e:
                last_err = f"{type(e).__name__}: host unreachable"
                _mark_opensky_dead()
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 404:
                # OpenSky returns 404 for "no flights found" - not an
                # error for our purposes
                any_ok = True
                continue
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code} on {endpoint}"
                continue
            any_ok = True
            try:
                payload = r.json() or []
            except ValueError:
                continue
            for f in payload:
                cs = (f.get("callsign") or "").strip().upper()
                if not cs.startswith(airline_prefix.upper()):
                    continue
                t = f.get(t_key) or 0
                dedup = (cs, kind, t)
                if dedup in seen:
                    continue
                seen.add(dedup)
                rows.append({
                    "callsign": cs,
                    "kind": kind,
                    "other": (f.get(other_key) or "?").upper(),
                    "time_unix": int(t),
                })
    if not any_ok:
        _airport_flights_error["msg"] = (
            last_err or "no response from OpenSky"
        )
    rows.sort(key=lambda x: -x["time_unix"])
    return rows
