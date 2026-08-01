"""METAR observation fetching from AWC.

Fetches METARs from aviationweather.gov's API and extracts fields for
overlay onto forecast plots as ground truth.

Design decisions:
- Fetch only from cycle time forward (past observations for verification)
- Include both routine METARs and SPECIs (specials)
- Extract vis, ceiling, wind speed, wind dir, wind gust
- Skip stations without METAR data silently
- Cache per station+cycle for 5 minutes (METARs update hourly + SPECIs anytime)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import pandas as pd


AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
USER_AGENT = "wx_compare/1.0 (aviation forecast comparison; github.com/john-dechellis-weather/wx_compare)"


@dataclass
class MetarObs:
    """One METAR observation, normalized to same units as ForecastRecord."""
    station_id: str
    obs_time: datetime          # tz-aware UTC
    vsby_sm: Optional[float]
    ceiling_ft: Optional[float]
    ceiling_unlimited: bool
    wind_speed_kt: Optional[float]
    wind_dir_deg: Optional[float]
    wind_gust_kt: Optional[float]
    raw_text: str


def fetch_metars(
    icaos: list[str],
    hours_back: int = 48,
    timeout: int = 30,
) -> dict[str, list[MetarObs]]:
    """Fetch recent METARs for the given stations.

    Returns dict of ICAO → list of MetarObs sorted by obs_time ascending.
    Stations with no METAR are omitted from the dict.
    """
    if not icaos:
        return {}
    params = {
        "ids": ",".join(icaos),
        "hours": hours_back,
        "format": "json",
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(AWC_METAR_URL, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        # Fail soft — a METAR fetch failure shouldn't break the whole page
        print(f"[METAR] fetch failed: {e}")
        return {}

    result: dict[str, list[MetarObs]] = {}
    for record in data:
        obs = _parse_metar_record(record)
        if obs is None:
            continue
        result.setdefault(obs.station_id, []).append(obs)

    # Sort each station's list by time
    for station in result.values():
        station.sort(key=lambda o: o.obs_time)

    return result


def filter_since(
    metars_by_station: dict[str, list[MetarObs]],
    since: datetime,
) -> dict[str, list[MetarObs]]:
    """Return only observations at or after `since` (typically the cycle time)."""
    return {
        icao: [o for o in obs if o.obs_time >= since]
        for icao, obs in metars_by_station.items()
    }


def metars_to_df(metars_by_station: dict[str, list[MetarObs]]) -> pd.DataFrame:
    """Convert nested dict to a flat DataFrame compatible with plot overlays."""
    rows = []
    for icao, obs_list in metars_by_station.items():
        for o in obs_list:
            rows.append({
                "station_id": o.station_id,
                "obs_time": o.obs_time,
                "vsby_sm": o.vsby_sm,
                "ceiling_ft": o.ceiling_ft,
                "ceiling_unlimited": o.ceiling_unlimited,
                "wind_speed_kt": o.wind_speed_kt,
                "wind_dir_deg": o.wind_dir_deg,
                "wind_gust_kt": o.wind_gust_kt,
                "raw_text": o.raw_text,
            })
    if not rows:
        return pd.DataFrame(columns=[
            "station_id", "obs_time", "vsby_sm", "ceiling_ft",
            "ceiling_unlimited", "wind_speed_kt", "wind_dir_deg",
            "wind_gust_kt", "raw_text",
        ])
    df = pd.DataFrame(rows)
    df["obs_time"] = pd.to_datetime(df["obs_time"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Parsing one METAR record from AWC's JSON response
# ---------------------------------------------------------------------------
def _parse_metar_record(record: dict) -> Optional[MetarObs]:
    """AWC returns METARs with these decoded fields already (partial):
      icaoId, reportTime, obsTime, temp, dewp, wdir, wspd, wgst, visib,
      altim, slp, wxString, rawOb, clouds[]
    We extract what we need, tolerating missing fields.
    """
    icao = record.get("icaoId")
    if not icao:
        return None

    # Observation time — AWC returns Unix epoch (seconds)
    obs_epoch = record.get("obsTime")
    if obs_epoch is None:
        return None
    try:
        obs_time = datetime.fromtimestamp(int(obs_epoch), tz=timezone.utc)
    except (TypeError, ValueError):
        return None

    # Visibility. AWC returns as a string like "10+" (unlimited), "1/2", "3",
    # or numeric. Normalize to statute miles.
    vsby_sm = _parse_visibility(record.get("visib"))

    # Ceiling from clouds array. AWC's "clouds" is a list of dicts:
    #   [{cover: "SCT", base: 25}, {cover: "BKN", base: 80}, ...]
    # Base is in hundreds of feet AGL. Ceiling = lowest BKN/OVC/VV base.
    ceiling_ft, ceiling_unlimited = _parse_ceiling(record.get("clouds", []))

    # Wind. AWC returns wdir as int (degrees) or "VRB" string. wspd/wgst in knots.
    wind_dir_deg = _parse_wind_direction(record.get("wdir"))
    wind_speed_kt = _to_float(record.get("wspd"))
    wind_gust_kt = _to_float(record.get("wgst"))

    return MetarObs(
        station_id=icao.upper(),
        obs_time=obs_time,
        vsby_sm=vsby_sm,
        ceiling_ft=ceiling_ft,
        ceiling_unlimited=ceiling_unlimited,
        wind_speed_kt=wind_speed_kt,
        wind_dir_deg=wind_dir_deg,
        wind_gust_kt=wind_gust_kt,
        raw_text=record.get("rawOb", ""),
    )


def _parse_visibility(v) -> Optional[float]:
    """Normalize vis to statute miles. AWC gives strings like '10+', '3', '1/2'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    if s.endswith("+"):
        # "10+" means "10 or greater" — for plot purposes clamp to 10
        try:
            return float(s[:-1])
        except ValueError:
            return None
    # Handle fractions like "1/2" or "1 1/2"
    if "/" in s:
        try:
            if " " in s:
                whole, frac = s.split(" ", 1)
                num, den = frac.split("/")
                return float(whole) + float(num) / float(den)
            num, den = s.split("/")
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_ceiling(clouds: list) -> tuple[Optional[float], bool]:
    """Return (ceiling_ft, unlimited_flag) from the clouds array.

    Ceiling = lowest broken/overcast/vertical-visibility layer, AGL.
    If no clouds or only FEW/SCT layers → treat as unlimited.
    """
    if not clouds:
        return None, True  # no cloud data → assume unlimited
    ceiling_bases = []
    for c in clouds:
        cover = (c.get("cover") or "").upper()
        base = c.get("base")
        if cover in {"BKN", "OVC", "VV"} and base is not None:
            try:
                ceiling_bases.append(int(base))
            except (TypeError, ValueError):
                continue
    if not ceiling_bases:
        return None, True  # all layers are FEW/SCT/etc → unlimited ceiling
     # AWC's JSON API returns base in feet directly (not hundreds like raw METAR)
    return float(min(ceiling_bases)), False


def _parse_wind_direction(w) -> Optional[float]:
    """Wind direction — 'VRB' or int. VRB → None (variable/unknown)."""
    if w is None:
        return None
    if isinstance(w, (int, float)):
        return float(w)
    s = str(w).strip().upper()
    if s == "VRB" or not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_float(x) -> Optional[float]:
    """Best-effort float conversion. None/empty → None."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
