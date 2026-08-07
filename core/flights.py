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