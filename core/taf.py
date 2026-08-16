"""TAF fetching from AWC and alert-analysis logic.

Fetches raw TAFs from aviationweather.gov's API, parses them with avwx-engine,
and produces "airport at risk" alert tables for visibility, ceiling, and TSRA.

Design decisions:
- One row per station in each alert table (not one per alert period)
- Show the WORST value across all periods within the user's time window
- Skip stations with no TAF (log them separately)
- Include TS/TSRA/+TSRA and VCTS
- Fetch latest TAF regardless of whether it's an amendment
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import avwx


AWC_TAF_URL = "https://aviationweather.gov/api/data/taf"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_tafs(icaos: list[str], timeout: int = 30) -> dict[str, str]:
    """Fetch latest TAF for each station from AWC.

    Returns dict mapping ICAO → raw TAF text. Missing stations are omitted.
    A single API call handles all stations via comma-separated ids.
    """
    if not icaos:
        return {}
    params = {
        "ids": ",".join(icaos),
        "format": "json",
    }
    # AWC blocks the default python-requests User-Agent with 403. Provide a
    # descriptive UA identifying the tool + a way to contact if there's abuse.
    headers = {
        "User-Agent": "wx_compare/1.0 (aviation forecast comparison; github.com/john-dechellis-weather/wx_compare)",
    }
    try:
        r = requests.get(AWC_TAF_URL, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        # AWC occasionally returns odd errors; surface a clean message
        raise RuntimeError(f"AWC TAF fetch failed: {e}") from e

    # AWC returns a list of TAF records; each has icaoId and rawTAF
    result: dict[str, str] = {}
    for record in data:
        icao = record.get("icaoId")
        raw = record.get("rawTAF")
        if icao and raw:
            result[icao.upper()] = raw
    return result


# ---------------------------------------------------------------------------
# Analysis result types
# ---------------------------------------------------------------------------
@dataclass
class VisAlert:
    icao: str
    min_vis_sm: float          # lowest visibility in the window
    worst_period_label: str    # e.g. "TEMPO 04-06Z"

@dataclass
class CeilingAlert:
    icao: str
    min_ceiling_ft: int        # lowest ceiling in the window
    worst_period_label: str

@dataclass
class TsraAlert:
    icao: str
    weather_code: str          # e.g. "TSRA", "+TSRA"
    period_label: str          # e.g. "PROB30 20-24Z"


@dataclass
class WindAlert:
    icao: str
    max_wind_kt: int           # highest sustained-or-gust in the window
    wind_str: str              # e.g. "35G45" or "38"
    worst_period_label: str = ""


@dataclass
class AlertResults:
    vis_alerts: list[VisAlert] = field(default_factory=list)
    ceiling_alerts: list[CeilingAlert] = field(default_factory=list)
    tsra_alerts: list[TsraAlert] = field(default_factory=list)
    wind_alerts: list[WindAlert] = field(default_factory=list)
    unavailable_icaos: list[str] = field(default_factory=list)
    parse_errors: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------
# TSRA-family codes we care about, including VCTS (vicinity).
_TSRA_CODES = {"TS", "TSRA", "+TSRA", "-TSRA", "TSSN", "+TSSN",
               "VCTS"}

# Cloud "layer" types that count as a ceiling
_CEILING_LAYER_TYPES = {"BKN", "OVC", "VV"}


def analyze_tafs(
    icaos: list[str],
    window_start: datetime,
    window_end: datetime,
    vis_threshold_sm: float,
    ceiling_threshold_ft: int,
    tsra_enabled: bool = True,
    wind_threshold_kt: int = 35,
) -> AlertResults:
    """Fetch, parse, and analyze TAFs for all provided stations.

    Returns an AlertResults with three sorted alert lists plus a list of
    stations for which TAFs were unavailable.
    """
    results = AlertResults()

    # Dedupe and uppercase
    icaos_clean = list(dict.fromkeys(s.strip().upper() for s in icaos if s.strip()))

    # Fetch all TAFs in one API call
    raw_tafs = fetch_tafs(icaos_clean)

    # Track which stations we got TAFs for
    results.unavailable_icaos = [i for i in icaos_clean if i not in raw_tafs]

    # Analyze each station
    for icao in icaos_clean:
        if icao not in raw_tafs:
            continue
        try:
            _analyze_one(
                icao, raw_tafs[icao],
                window_start, window_end,
                vis_threshold_sm, ceiling_threshold_ft, tsra_enabled,
                wind_threshold_kt, results,
            )
        except Exception as e:
            results.parse_errors[icao] = str(e)

    # Sort each alert list by severity
    results.vis_alerts.sort(key=lambda a: a.min_vis_sm)
    results.ceiling_alerts.sort(key=lambda a: a.min_ceiling_ft)
    results.tsra_alerts.sort(key=lambda a: a.icao)
    results.wind_alerts.sort(key=lambda a: -a.max_wind_kt)

    return results


def _analyze_one(
    icao: str,
    raw_taf: str,
    window_start: datetime,
    window_end: datetime,
    vis_threshold_sm: float,
    ceiling_threshold_ft: int,
    tsra_enabled: bool,
    wind_threshold_kt: int,
    results: AlertResults,
) -> None:
    """Analyze one station's TAF and append any alerts to results."""
    taf = avwx.Taf.from_report(raw_taf)
    if not taf.data or not taf.data.forecast:
        return

    # Track running minima for vis and ceiling across all overlapping periods
    min_vis: Optional[float] = None
    min_vis_label = ""
    min_ceiling: Optional[int] = None
    min_ceiling_label = ""
    first_tsra_code: Optional[str] = None
    first_tsra_label = ""
    max_wind: Optional[int] = None
    max_wind_str = ""
    max_wind_label = ""

    for period in taf.data.forecast:
        # Skip periods that don't overlap the user's window
        p_start = period.start_time.dt if period.start_time else None
        p_end = period.end_time.dt if period.end_time else None
        if p_start is None or p_end is None:
            continue
        if p_end <= window_start or p_start >= window_end:
            continue  # no overlap

        period_label = _format_period_label(period)

        # --- Visibility ---
        vis_sm = _period_visibility_sm(period)
        if vis_sm is not None and vis_sm < vis_threshold_sm:
            if min_vis is None or vis_sm < min_vis:
                min_vis = vis_sm
                min_vis_label = period_label

        # --- Ceiling ---
        ceil_ft = _period_ceiling_ft(period)
        if ceil_ft is not None and ceil_ft < ceiling_threshold_ft:
            if min_ceiling is None or ceil_ft < min_ceiling:
                min_ceiling = ceil_ft
                min_ceiling_label = period_label

        # --- Wind ---
        wind_kt, wind_str = _period_wind_kt(period)
        if wind_kt is not None and wind_kt >= wind_threshold_kt:
            if max_wind is None or wind_kt > max_wind:
                max_wind = wind_kt
                max_wind_str = wind_str
                max_wind_label = period_label

        # --- TSRA ---
        if tsra_enabled and first_tsra_code is None:
            tsra_code = _period_tsra_code(period)
            if tsra_code is not None:
                first_tsra_code = tsra_code
                first_tsra_label = period_label

    if min_vis is not None:
        results.vis_alerts.append(VisAlert(
            icao=icao,
            min_vis_sm=min_vis,
            worst_period_label=min_vis_label,
        ))
    if min_ceiling is not None:
        results.ceiling_alerts.append(CeilingAlert(
            icao=icao,
            min_ceiling_ft=min_ceiling,
            worst_period_label=min_ceiling_label,
        ))
    if max_wind is not None:
        results.wind_alerts.append(WindAlert(
            icao=icao,
            max_wind_kt=max_wind,
            wind_str=max_wind_str,
            worst_period_label=max_wind_label,
        ))
    if first_tsra_code is not None:
        results.tsra_alerts.append(TsraAlert(
            icao=icao,
            weather_code=first_tsra_code,
            period_label=first_tsra_label,
        ))


# ---------------------------------------------------------------------------
# Per-period field extraction
# ---------------------------------------------------------------------------
def _period_visibility_sm(period) -> Optional[float]:
    """Return visibility in statute miles, or None if unknown / >6."""
    v = period.visibility
    if v is None:
        return None
    # avwx encodes "P6SM" (greater than 6) with repr='P6' and value=None
    # We treat that as "unlimited for alerting" — no alert
    if v.value is None:
        return None
    try:
        val = float(v.value)
    except (TypeError, ValueError):
        return None
    # ICAO-format TAFs (Caribbean, Mexico, S. America) report
    # visibility in METERS (e.g. 0800, 4000, 9999). No real SM
    # value exceeds ~50 and no meter value sits below 50, so the
    # magnitude discriminates units cleanly. 9999 (and CAVOK's
    # encoding) means unlimited.
    if val >= 9999:
        return None
    if val >= 50:
        return val / 1609.34
    return val


def _period_ceiling_ft(period) -> Optional[int]:
    """Return ceiling in feet AGL (lowest BKN/OVC/VV base), or None if none."""
    if not period.clouds:
        return None
    ceiling_bases = [
        c.base for c in period.clouds
        if c.type in _CEILING_LAYER_TYPES and c.base is not None
    ]
    if not ceiling_bases:
        return None
    return int(min(ceiling_bases) * 100)


def _period_wind_kt(period):
    """Return (max sustained-or-gust kt, display string) or (None, "")."""
    spd = None
    gst = None
    try:
        if period.wind_speed is not None and period.wind_speed.value is not None:
            spd = int(period.wind_speed.value)
    except (TypeError, ValueError):
        pass
    try:
        if period.wind_gust is not None and period.wind_gust.value is not None:
            gst = int(period.wind_gust.value)
    except (TypeError, ValueError):
        pass
    if spd is None and gst is None:
        return None, ""
    max_kt = max(x for x in (spd, gst) if x is not None)
    if spd is not None and gst is not None:
        return max_kt, f"{spd:02d}G{gst:02d}"
    if gst is not None:
        return max_kt, f"G{gst:02d}"
    return max_kt, f"{spd:02d}"


def _period_tsra_code(period) -> Optional[str]:
    """Return the TSRA-family code present in this period, or None."""
    if not period.wx_codes:
        return None
    for wx in period.wx_codes:
        code = wx.repr.upper() if wx.repr else ""
        if code in _TSRA_CODES:
            return code
    return None


def _format_period_label(period) -> str:
    """Human-readable label like 'TEMPO 04-06Z' or 'PROB30 08-12Z'.

    For FROM periods, we say 'FM 08Z onward' since the end time is often just
    the end of the base forecast.
    """
    p_start = period.start_time.dt if period.start_time else None
    p_end = period.end_time.dt if period.end_time else None
    if p_start is None:
        return period.type or "?"

    start_str = p_start.strftime("%HZ")
    end_str = p_end.strftime("%HZ") if p_end else "?"
    ptype = period.type or ""

    if ptype in {"FROM", "FM"}:
        if period.probability is not None:
            prob = period.probability.value
            return f"PROB{prob} {start_str}-{end_str}"
        return f"FM {start_str} onward"
    if ptype == "BECMG":
        return f"BECMG {start_str}-{end_str}"
    if ptype == "TEMPO":
        # Include probability if present (avwx sometimes gives PROB30 TEMPO ...)
        if period.probability is not None:
            prob = period.probability.value
            return f"PROB{prob} TEMPO {start_str}-{end_str}"
        return f"TEMPO {start_str}-{end_str}"
    if period.probability is not None:
        prob = period.probability.value
        return f"PROB{prob} {start_str}-{end_str}"
    return f"{ptype} {start_str}-{end_str}"