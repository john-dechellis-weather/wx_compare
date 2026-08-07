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
    min_interval_s: int = 120,
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
