"""TAF generator from tomorrow.io Timelines data (core module, 15 Oct).

Algorithm and criteria developed by George Delanjian (taf_maker.py,
12 Oct build); carried into BlueMet unchanged apart from:
  * no interactive prompts / hard-coded key - the key comes from the
    site's tomorrow.io config (core.tio_map.api_key) and every call
    is charged to the site's daily budget;
  * airport lookup tries the site's own station tables first, then
    the OurAirports CSV (cached under the cache root);
  * generate() is the page entry point (several ICAOs, one call each).

One TAF costs 27 Timelines requests (station + 8-point rings at 5,
10 and 25 mi).
"""

from __future__ import annotations

import os

# -*- coding: utf-8 -*-
"""
taf_maker.py

Interactive TAF generator using Tomorrow.io Timelines data and “practically perfect” TAF criteria.

Windows CMD quick start:
  1) Install dependency:
       python -m pip install requests
  2) Set your API key for THIS terminal session:
       set TOMORROW_API_KEY=YOUR_KEY_HERE
  3) Run:
       python taf_maker.py

What this script does (as agreed):
    pass
- Station unit selection:
    * IMPERIAL for: US, Canada, Mexico, and Puerto Rico (ICAO starts with TJ)
    * METRIC everywhere else

- Cloud base rounding (feet):
    * <1000 ft: nearest 100 ft
    * >=1000 ft: nearest 500 ft

- METRIC visibility formatting (meters):
    * >= 9000 m -> 9999
    * 5000–8999 m -> round to nearest 1000 m (half-up)
    * <5000 m -> round in SM bins, convert back to meters, then nearest 100 m:
         - <800 m => nearest 0.25SM
         - 800–4999 m => nearest 0.5SM
      floor: 0400 (never output 0000)

- IMPERIAL visibility formatting:
    * >= 6SM -> P6SM, else use common SM fractions

- BR/FG logic (applies to BOTH station types):
    * If visibility is restricted (metric: not 9999; imperial: not P6SM):
         - <= 800 m => FG
         - else => BR
    * BR/FG can appear alongside precip and convective tokens

- Precipitation inclusion:
    * Include precip if it occurs within 5 miles of station (station + 8-point ring at 5mi)

- Convective (thunder) distance logic:
    * If thunder <= 5 miles:
         - use -TSRA / TSRA / +TSRA based on precip intensity (mm/hr) within 5 miles
    * If thunder > 5 miles and <= 25 miles:
         - add VCTS (placed LAST in wx list, after BR/FG)

- CB attachment:
    * If TSRA appears on a line:
         - append "CB" to the end of the last cloud group token
         - if sky would be SKC, replace with SCT###CB using the LOWEST cloud base within 5 miles

- Precip line merge:
    * If precip repeats within 2 hours with a no-precip FM between AND wind/vis/sky identical, merge.

PATCH: Flight-category ceiling fix
- Category logic now uses ceiling derived from the SAME sky group you print:
    * If printed cover is BKN/OVC -> ceiling = printed (rounded) base
    * Else -> no ceiling (infinite)
This makes category-based FM breaks align with what you actually see in the TAF text.
"""

import csv
import datetime as dt
import math
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

OURAIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
TOMORROW_TIMELINES_URL = "https://api.tomorrow.io/v4/timelines"

RADIUS_MILES_5 = 5.0
RADIUS_MILES_25 = 25.0
RADIUS_MILES_10 = 10.0
RADIUS_KM_5 = RADIUS_MILES_5 * 1.609344
RADIUS_KM_25 = RADIUS_MILES_25 * 1.609344
RADIUS_KM_10 = RADIUS_MILES_10 * 1.609344


# ----------------------------
# Time utilities
# ----------------------------

def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_iso_z(s: str) -> dt.datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return dt.datetime.fromisoformat(s)

def ceil_to_hour(t: dt.datetime) -> dt.datetime:
    if t.minute == 0 and t.second == 0 and t.microsecond == 0:
        return t
    return (t.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1))


def round_to_hour(t: dt.datetime) -> dt.datetime:
    """Round to nearest hour (half up).
    - HH00–HH29 -> same hour (floor)
    - HH30–HH59 -> next hour (ceil)
    """
    base = t.replace(minute=0, second=0, microsecond=0)
    return base if t.minute < 30 else (base + dt.timedelta(hours=1))

def ddhhmm(t: dt.datetime) -> str:
    return f"{t.day:02d}{t.hour:02d}{t.minute:02d}"

def ddhh(t: dt.datetime) -> str:
    return f"{t.day:02d}{t.hour:02d}"

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ----------------------------
# Unit helpers
# ----------------------------

def km_to_m(km: float) -> float:
    return km * 1000.0

def km_to_sm(km: float) -> float:
    return km * 0.621371

def km_to_ft(km: float) -> float:
    return km * 3280.84

def sm_to_m(sm: float) -> float:
    return sm * 1609.344

def m_to_sm(m: float) -> float:
    return m / 1609.344

def mph_to_kt(x: float) -> float:
    return x * 0.868976

def ms_to_kt(x: float) -> float:
    return x * 1.943844


# ----------------------------
# Geo helpers (offset points)
# ----------------------------

def offset_latlon_km(lat: float, lon: float, north_km: float, east_km: float) -> Tuple[float, float]:
    dlat = north_km / 110.574
    dlon = east_km / (111.320 * max(0.1, math.cos(math.radians(lat))))
    return lat + dlat, lon + dlon

def ring_points(lat: float, lon: float, radius_km: float) -> List[Tuple[float, float]]:
    r = radius_km
    offsets = [
        ( r,  0),
        ( r/math.sqrt(2),  r/math.sqrt(2)),
        ( 0,  r),
        (-r/math.sqrt(2),  r/math.sqrt(2)),
        (-r,  0),
        (-r/math.sqrt(2), -r/math.sqrt(2)),
        ( 0, -r),
        ( r/math.sqrt(2), -r/math.sqrt(2)),
    ]
    pts = [(lat, lon)]
    for nk, ek in offsets:
        pts.append(offset_latlon_km(lat, lon, nk, ek))
    return pts


# ----------------------------
# Formatting helpers
# ----------------------------

def round_dir_to_10(deg: float) -> int:
    d = int(round(deg / 10.0) * 10) % 360
    return 360 if d == 0 else d

def round_half_up_step(value: float, step: int) -> int:
    """True half-up rounding to nearest step."""
    return int(value / step + 0.5) * step

def round_cloud_base_ft(ft: float) -> int:
    if ft < 1000:
        return max(0, round_half_up_step(ft, 100))
    return max(0, round_half_up_step(ft, 500))

def ft_to_hundreds(ft: float) -> str:
    hh = int(round(ft / 100.0))
    return f"{hh:03d}"

def intensity_prefix_from_mmph(mmph: float) -> Optional[str]:
    """
    Map precip intensity (mm/hr) to TAF intensity prefix.

    Rule of thumb:
      - Suppress precip if < 0.01 mm/hr
      - "-" for light
      - ""  for moderate
      - "+" for heavy
    """
    if mmph is None:
        return None

    mmph = float(mmph)

    # --- SIMPLE, MEMORABLE THRESHOLDS ---
    if mmph < 0.0:
        return None        # too weak to report

    if mmph < 0.5:
        return "-"         # light

    if mmph >= 2.5:
        return "+"         # heavy

    return ""              # moderate

def cover_from_fraction(cloud_cover_pct: Optional[float]) -> Optional[str]:
    if cloud_cover_pct is None:
        return None
    cc = clamp(cloud_cover_pct, 0.0, 100.0)
    if cc < 12.5:
        return None
    if cc < 25:
        return "FEW"
    if cc < 50:
        return "SCT"
    if cc < 88:
        return "BKN"
    return "OVC"

def format_vis_us(vis_sm: float) -> str:
    if vis_sm >= 6:
        return "P6SM"
    candidates = [5.0, 4.0, 3.0, 2.0, 1.0, 0.75, 0.5, 0.25]
    best = min(candidates, key=lambda x: abs(x - vis_sm))
    if best >= 1:
        return f"{int(best)}SM"
    if math.isclose(best, 0.75):
        return "3/4SM"
    if math.isclose(best, 0.5):
        return "1/2SM"
    if math.isclose(best, 0.25):
        return "1/4SM"
    return "1/2SM"

def format_vis_metric_meters(meters: float) -> str:
    """
    METRIC station visibility output in meters:
      >= 9000 m        -> 9999
      5000–8999 m      -> nearest 1000 m (half-up)
      4000–4999 m      -> nearest 1000 m (half-up)
      < 4000 m         -> SM binning:
                           <800 m  -> 0.25 SM
                           >=800 m -> 0.5 SM
                         convert back to meters,
                         nearest 100 m (half-up),
                         floor 0400
    """
    meters = float(meters)

    # Very good visibility
    if meters >= 9000:
        return "9999"

    # 4000 m and above: always round to nearest 1000
    if meters >= 4000:
        out = round_half_up_step(meters, 1000)
        out = max(4000, min(8000, out))
        return f"{out:04d}"

    # Below 4000 m: use SM-style binning
    vis_sm = m_to_sm(meters)
    increment = 0.25 if meters < 800 else 0.5
    rounded_sm = round(vis_sm / increment) * increment

    out_m = sm_to_m(rounded_sm)
    out = round_half_up_step(out_m, 100)

    out = max(400, out)
    return f"{out:04d}"


    vis_sm = m_to_sm(meters)
    increment = 0.25 if meters < 800 else 0.5
    rounded_sm = round(vis_sm / increment) * increment

    out_m = sm_to_m(rounded_sm)
    out = round_half_up_step(out_m, 100)

    # If we're very close to 5000 m, round up to 5000 to avoid odd tokens like 4800.
    if out >= 4500:
        out = 5000

    out = max(400, min(8900, out))
    return f"{out:04d}"

def tsra_token_from_intensity(mmph: float) -> str:
    sign = intensity_prefix_from_mmph(mmph)
    # If below the precip threshold, we still emit TSRA (thunder present),
    # but without a +/- qualifier.
    if sign is None:
        return "TSRA"
    return f"{sign}TSRA" if sign else "TSRA"


# ----------------------------
# Airport lookup
# ----------------------------

def load_airports_csv(cache_path: str) -> None:
    if os.path.exists(cache_path):
        return
    with urllib.request.urlopen(OURAIRPORTS_CSV_URL, timeout=30) as r:
        data = r.read()
    with open(cache_path, "wb") as f:
        f.write(data)

def _country_from_prefix(icao: str) -> str:
    """ISO country for the unit rule when no table has it: the ICAO
    prefixes that print IMPERIAL (US incl. Alaska/Hawaii, Canada,
    Mexico, Puerto Rico)."""
    p = icao.upper()
    if p.startswith(("K", "PA", "PH", "PG", "PJ", "PW")):
        return "US"
    if p.startswith("C"):
        return "CA"
    if p.startswith("MM"):
        return "MX"
    if p.startswith("TJ"):
        return "US"
    return ""


def icao_to_airport_info(icao: str, cache_path: str) -> Tuple[float, float, str, str]:
    """lat, lon, name, iso_country. BlueMet's own station tables
    first (no network), then the OurAirports CSV (one download,
    cached at cache_path)."""
    icao = icao.strip().upper()
    try:
        from core.stations import StationResolver
        r = StationResolver(cache_dir=os.path.join(os.path.dirname(cache_path), "stations"))
        stn = r.resolve(icao)
        if stn is not None:
            # OurAirports gives the country when it is already cached;
            # otherwise the prefix rule decides the units.
            country = ""
            try:
                if os.path.exists(cache_path):
                    country = _ourairports_row(icao, cache_path)[3]
            except Exception:
                country = ""
            return float(stn.lat), float(stn.lon), stn.name or icao, country or _country_from_prefix(icao)
    except Exception:
        pass
    load_airports_csv(cache_path)
    return _ourairports_row(icao, cache_path)


def _ourairports_row(icao: str, cache_path: str) -> Tuple[float, float, str, str]:
    with open(cache_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ident = (row.get("ident") or "").strip().upper()
            if ident == icao:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
                name = (row.get("name") or icao).strip()
                iso_country = (row.get("iso_country") or "").strip().upper()
                return lat, lon, name, iso_country
    raise ValueError(f"ICAO '{icao}' not found in OurAirports database.")


def is_imperial_station(icao: str, iso_country: str) -> bool:
    icao = icao.upper()
    if iso_country in {"US", "CA", "MX"}:
        return True
    if icao.startswith("TJ"):  # Puerto Rico
        return True
    return False


# ----------------------------
# Tomorrow.io Timelines client
# ----------------------------

FIELDS = [
    "windSpeed",
    "windDirection",
    "windGust",
    "visibility",
    "cloudCover",
    "cloudBase",
    "cloudCeiling",
    "precipitationType",
    "precipitationIntensity",
    "weatherCode",
    "lightningFlashRateDensity"
]

def fetch_timelines_one_location(
    api_key: str,
    lat: float,
    lon: float,
    start: dt.datetime,
    end: dt.datetime,
    timestep: str,
    units: str,
) -> List[Dict[str, Any]]:
    body = {
        "location": f"{lat},{lon}",
        "fields": FIELDS,
        "timesteps": [timestep],
        "units": units,
        "startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime": end.isoformat().replace("+00:00", "Z"),
    }

    r = requests.post(
        TOMORROW_TIMELINES_URL,
        params={"apikey": api_key},
        json=body,
        headers={"accept": "application/json", "content-type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    timelines = payload.get("data", {}).get("timelines", [])
    if not timelines:
        return []
    return timelines[0].get("intervals", [])

def fetch_timelines_for_points(
    api_key: str,
    points: List[Tuple[float, float]],
    start: dt.datetime,
    end: dt.datetime,
    timestep: str,
    units: str,
) -> List[List[Dict[str, Any]]]:
    out: List[List[Dict[str, Any]]] = []
    for plat, plon in points:
        out.append(fetch_timelines_one_location(api_key, plat, plon, start, end, timestep, units))
    return out


# ----------------------------
# Wx interpretation helpers
# ----------------------------

def is_thunder_code(weather_code: Optional[int]) -> bool:
    """
    Adjust as needed to match your Tomorrow weatherCode mapping.
    """
    if weather_code is None:
        return False
    return weather_code in {8000, 8001, 8002, 8003}

def precip_rank(ptype: int) -> int:
    order = {3: 0, 2: 1, 1: 2, 4: 3}  # FZRA, SN, RA, PL
    return order.get(ptype, 99)


# ----------------------------
# Precip merge signature logic
# ----------------------------

PRECIP_CODES = {"DZ", "RA", "SN", "SG", "PL", "GR", "GS", "FZRA", "FZDZ"}

def precip_signature(wx: str) -> Set[str]:
    if not wx:
        return set()
    tokens = re.split(r"\s+", wx.strip())
    out: Set[str] = set()
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        tok = tok.lstrip("+-")
        if tok in PRECIP_CODES:
            out.add(tok)
            continue
        if tok.endswith("RA"):
            out.add("FZRA" if tok == "FZRA" else "RA")
        elif tok.endswith("SN"):
            out.add("SN")
        elif tok.endswith("DZ"):
            out.add("FZDZ" if tok == "FZDZ" else "DZ")
        elif tok.endswith("PL"):
            out.add("PL")
    return out


# ----------------------------
# Aggregation (5mi precip + 5/25mi thunder logic)
# ----------------------------



# Lightning flash-rate-density threshold (flashes/km^2/hr) for thunder/VCTS detection
THUNDER_LFDR_THRESHOLD = 0.05

def interval_has_thunder(values: dict) -> bool:
    """Thunder detection using lightningFlashRateDensity primarily, with weatherCode fallback."""
    lfdr = values.get("lightningFlashRateDensity")
    try:
        if lfdr is not None and float(lfdr) >= THUNDER_LFDR_THRESHOLD:
            return True
    except Exception:
        pass

    code = values.get("weatherCode")
    try:
        return int(code) in THUNDER_WEATHER_CODES
    except Exception:
        return False

def aggregate_interval(
    points_5: List[Dict[str, Any]],
    points_10: List[Dict[str, Any]],
    points_25: List[Dict[str, Any]],
) -> Dict[str, Any]:
    pass

    base = points_5[0]
    out = {"startTime": base["startTime"], "values": dict(base.get("values", {}))}

    best_type: Optional[int] = None
    best_rank = 999

    sum_precip_intensity_5 = 0.0
    n_points_5 = 0
    max_precip_intensity_5 = 0.0

    has_precip_any_5 = False
    min_cb_km_5: Optional[float] = None

    # ---- Thunder flags ----
    ts_in_5 = False
    ts_in_25 = False

    for it in points_5:
        v = it.get("values", {})
        lfdr = v.get("lightningFlashRateDensity")
        try:
            if lfdr is not None and float(lfdr) >= THUNDER_LFDR_THRESHOLD:
                ts_in_5 = True
                break
        except Exception:
            pass

    if not ts_in_5:
        for it in points_25:
            v = it.get("values", {})
            lfdr = v.get("lightningFlashRateDensity")
            try:
                if lfdr is not None and float(lfdr) >= THUNDER_LFDR_THRESHOLD:
                    ts_in_25 = True
                    break
            except Exception:
                pass

    # ---- 5mi precip + cloud base scan ----
    for it in points_5:
        v = it.get("values", {})
        n_points_5 += 1

        ptype = v.get("precipitationType")
        pint = v.get("precipitationIntensity")

        if isinstance(ptype, (int, float)) and isinstance(pint, (int, float)) and pint > 0:
            has_precip_any_5 = True
            r = precip_rank(int(ptype))
            if r < best_rank:
                best_rank = r
                best_type = int(ptype)
            sum_precip_intensity_5 += float(pint)
            max_precip_intensity_5 = max(max_precip_intensity_5, float(pint))

        cb = v.get("cloudBase")
        if isinstance(cb, (int, float)):
            if min_cb_km_5 is None or cb < min_cb_km_5:
                min_cb_km_5 = cb

    avg_precip_intensity_5 = (
        sum_precip_intensity_5 / n_points_5 if n_points_5 > 0 else 0.0
    )

    if best_type is not None and avg_precip_intensity_5 > 0:
        out["values"]["precipitationType"] = best_type
        out["values"]["precipitationIntensity"] = avg_precip_intensity_5
    else:
        out["values"]["precipitationIntensity"] = 0.0

    # ---- VCSH: >5mi to <=10mi only, and ONLY if no precip in 5mi ----
    sh_in_10 = False
    if not has_precip_any_5:
        for it in points_10:
            v = it.get("values", {})
            pint = v.get("precipitationIntensity")
            if isinstance(pint, (int, float)) and pint > 0:
                sh_in_10 = True
                break

    # ---- write derived flags ----
    out["values"]["_ts_in_5mi"] = ts_in_5
    out["values"]["_ts_in_25mi"] = ts_in_25
    out["values"]["_ts_intensity_mmph"] = avg_precip_intensity_5 if ts_in_5 else 0.0
    out["values"]["_minCloudBaseKm_5mi"] = min_cb_km_5
    out["values"]["_sh_in_10mi"] = sh_in_10

    return out

    return out


# ----------------------------
# Data model
# ----------------------------

@dataclass
class Step:
    t: dt.datetime
    wind_dir: Optional[float]
    wind_spd_kt: Optional[float]
    wind_gust_kt: Optional[float]
    vis_sm: Optional[float]
    vis_m: Optional[float]
    cloud_cover_pct: Optional[float]
    cloud_base_ft: Optional[float]
    ceiling_ft_raw: Optional[float]  # from cloudCeiling; kept for fallback only
    precip_type: Optional[int]
    precip_intensity_mmph: Optional[float]
    ts_in_5mi: bool
    ts_in_25mi: bool
    sh_in_10mi: bool
    ts_intensity_mmph: float
    min_cb_ft_5mi: Optional[float]
    output_style: str  # "US" or "METRIC"
    station_lat: float
    category_override: Optional[str] = None  # used to suppress 1-hour category changes


    def vis_token(self) -> str:
        if self.output_style == "US":
            if self.vis_sm is None:
                return "P6SM"
            return format_vis_us(self.vis_sm)
        if self.vis_m is None:
            return "9999"
        return format_vis_metric_meters(self.vis_m)

    def wind_token(self) -> str:
        if self.wind_spd_kt is None or self.wind_dir is None:
            return "00000KT"
        spd = int(round(self.wind_spd_kt))
        if spd <= 0:
            return "00000KT"
        ddd = round_dir_to_10(self.wind_dir)
        if spd <= 3:
            return f"VRB{max(3, spd):02d}KT"
        gust = ""
        if self.wind_gust_kt is not None:
            g = int(round(self.wind_gust_kt))
            if g - spd >= 10:
                gust = f"G{g:02d}"
        return f"{ddd:03d}{spd:02d}{gust}KT"

    # --- PATCHED SKY/CEILING: use same base for printing & flight categories ---

    def _sky_cover_and_base_rounded_ft(self) -> Tuple[Optional[str], Optional[int]]:
        """
        Returns (cover, base_ft_rounded).
        Rounding rules:
          - < 10,000 ft: use round_cloud_base_ft() (100ft under 1000; 500ft otherwise)
          - >= 10,000 ft and < 20,000 ft: nearest 1,000 ft (half-up)
          - >= 20,000 ft: nearest 5,000 ft (half-up)
          - cap at 30,000 ft (FL300)
        """
        cover = cover_from_fraction(self.cloud_cover_pct)
        if cover is None:
            return None, None

        # Prefer cloud base; fall back to raw ceiling when base is missing.
        base_ft = self.cloud_base_ft
        if base_ft is None:
            if self.ceiling_ft_raw is not None:
                base_ft = self.ceiling_ft_raw
            else:
                return cover, None

        if base_ft >= 20000:
            rounded = round_half_up_step(base_ft, 5000)
        elif base_ft >= 10000:
            rounded = round_half_up_step(base_ft, 1000)
        else:
            rounded = round_cloud_base_ft(base_ft)

        rounded = int(min(30000, rounded))
        return cover, rounded

    def sky_tokens(self) -> List[str]:
        cover, base_ft = self._sky_cover_and_base_rounded_ft()
        if cover is None:
            return ["SKC"]
        if base_ft is None:
            return [cover + "///"]
        return [f"{cover}{ft_to_hundreds(base_ft)}"]

    def category_ceiling_ft(self) -> Optional[float]:
        """
        Ceiling for flight categories is derived from what we PRINT:
        BKN/OVC base => ceiling; FEW/SCT => no ceiling.
        """
        cover, base_ft = self._sky_cover_and_base_rounded_ft()
        if cover in ("BKN", "OVC") and base_ft is not None:
            return float(base_ft)
        return None

    # --- Weather tokens ---

    def precip_tokens(self) -> List[str]:
        toks: List[str] = []
        mmph = self.precip_intensity_mmph or 0.0
        ptype = self.precip_type
        if ptype and mmph > 0.0:
            prefix = intensity_prefix_from_mmph(mmph)
            # Below the "emit precip" threshold, suppress precip tokens entirely.
            if prefix is None:
                return []

            if ptype == 1:
                toks.append(f"{prefix}RA" if prefix else "RA")
            elif ptype == 2:
                toks.append(f"{prefix}SN" if prefix else "SN")
            elif ptype == 3:
                toks.append(f"{prefix}FZRA" if prefix else "FZRA")
            elif ptype == 4:
                toks.append(f"{prefix}PL" if prefix else "PL")
        return toks

    def br_fg_tokens(self) -> List[str]:
        """Return BR/FG token based on RESTRICTED visibility, using the printed visibility token.

        This avoids cases where raw visibility (e.g., 804m) formats to 0800 but would otherwise be
        classified as BR due to the unrounded value.
        """
        vt = self.vis_token()

        # Unrestricted => no BR/FG
        if (vt == "P6SM") if self.output_style == "US" else (vt == "9999"):
            return []

        # Determine restriction severity using the rendered token
        if self.output_style == "US":
            # Parse common SM formats: "3SM", "1/2SM", "3/4SM", "1SM"
            t = vt.replace("SM", "")
            try:
                if "/" in t:
                    num, den = t.split("/", 1)
                    v_sm = float(num) / float(den)
                else:
                    v_sm = float(t)
            except Exception:
                return ["BR"]
            return ["FG"] if v_sm <= 0.5 else ["BR"]

        # METRIC: vt is meters like 0400, 0800, 1600, ... 9999
        try:
            v_m = int(vt)
        except Exception:
            return ["BR"]
        return ["FG"] if v_m <= 800 else ["BR"]

    def wx_tokens(self) -> List[str]:
        toks: List[str] = []

        # Station thunder (<=5mi) => TSRA family (no separate RA token)
        if self.ts_in_5mi:
            toks.append(tsra_token_from_intensity(self.ts_intensity_mmph))
        else:
            # Station precip within 5mi (when not TS in 5mi)
            precip_toks = self.precip_tokens()
            toks.extend(precip_toks)

            # BR/FG (visibility restriction) — should appear BEFORE vicinity tokens
            toks.extend(self.br_fg_tokens())

            # Vicinity tokens — never both; VCTS always wins over VCSH
            if self.ts_in_25mi:
                toks.append("VCTS")
            elif (not precip_toks) and self.sh_in_10mi:
                if abs(self.station_lat) <= 25.0:
                    toks.append("VCSH")

        # If TS in 5mi, still allow BR/FG (and keep it before any vicinity, though vicinity won't exist here)
        if self.ts_in_5mi:
            toks.extend(self.br_fg_tokens())

        # de-dupe preserving order
        out: List[str] = []
        for w in toks:
            if w not in out:
                out.append(w)
        return out


@dataclass
class TafGroup:
    kind: str  # "INIT" | "FM" | "TEMPO"
    start: dt.datetime
    end: Optional[dt.datetime]
    wind: str
    vis: str
    wx: str
    sky: str

    def render(self) -> str:
        # Build tokens and join once to avoid double-spaces.
        wx_tokens = (self.wx or "").split()
        sky_tokens = (self.sky or "").split()

        if self.kind == "INIT":
            parts = [self.wind, self.vis] + wx_tokens + sky_tokens
            return " ".join(p for p in parts if p)

        if self.kind == "TEMPO":
            # TEMPO uses DDHH/DDHH, and may omit end if missing (shouldn't).
            tempo_range = f"{ddhh(self.start)}/{ddhh(self.end or self.start)}"
            parts = ["TEMPO", tempo_range, self.wind, self.vis] + wx_tokens + sky_tokens
            return "    " + " ".join(p for p in parts if p)

        # Default FM
        parts = [f"FM{ddhhmm(self.start)}", self.wind, self.vis] + wx_tokens + sky_tokens
        return "  " + " ".join(p for p in parts if p)



# ----------------------------
# Group building + CB logic
# ----------------------------

def append_cb_to_last_cloud_group(sky_tokens: List[str]) -> List[str]:
    if not sky_tokens:
        return ["SCT050CB"]
    if sky_tokens == ["SKC"]:
        return ["SCT050CB"]
    out = sky_tokens[:]
    if not out[-1].endswith("CB"):
        out[-1] = out[-1] + "CB"
    return out

def sky_tokens_for_skc_tsra(step: Step) -> List[str]:
    base_ft = step.min_cb_ft_5mi if step.min_cb_ft_5mi is not None else 5000.0
    rounded_ft = round_cloud_base_ft(base_ft)
    return [f"SCT{ft_to_hundreds(rounded_ft)}CB"]

def flight_category(step: Step) -> str:
    """Return flight category (VFR/MVFR/IFR/LIFR).

    IMPORTANT: Visibility categorization is derived from the SAME visibility token we PRINT
    (step.vis_token()), so category breaks and smoothing align with what the user sees.

    Rule: overall category is the WORSE (more restrictive) of visibility-based and ceiling-based categories.
    - Visibility thresholds (SM):
        VFR  >= 3
        MVFR >= 2 and < 3
        IFR  >= 1 and < 2
        LIFR <  1
    - Ceiling thresholds (ft) using step.category_ceiling_ft() (derived from printed BKN/OVC base):
        VFR  >= 2000
        MVFR >= 1000 and < 2000
        IFR  >= 400  and < 1000
        LIFR <  400
      If ceiling is None (no BKN/OVC), it does not restrict (treated as VFR for ceiling component).
      If visibility token is unrestricted (METRIC 9999 / US P6SM), it does not restrict (treated as VFR for vis).
    """
    if getattr(step, "category_override", None):
        return step.category_override  # type: ignore[attr-defined]

    # --- visibility category (SM) derived from printed token ---
    vt = step.vis_token()
    if step.output_style == "US":
        if vt == "P6SM":
            v_sm = float("inf")
        else:
            # Parse common SM formats: "3SM", "1/2SM", "3/4SM", "1SM"
            t = vt.replace("SM", "")
            if "/" in t:
                num, den = t.split("/", 1)
                v_sm = float(num) / float(den)
            else:
                v_sm = float(t)
    else:
        # METRIC token is meters (e.g., 1600, 2400, 4000, 5000, 6000, 8000, 9999)
        if vt == "9999":
            v_sm = float("inf")
        else:
            try:
                v_m = float(int(vt))
            except Exception:
                v_m = float("inf")
            v_sm = m_to_sm(v_m)

    if v_sm < 1.0:
        vis_cat = "LIFR"
    elif v_sm < 2.0:
        vis_cat = "IFR"
    elif v_sm < 3.0:
        vis_cat = "MVFR"
    else:
        vis_cat = "VFR"

    # --- ceiling category (ft) ---
    c = step.category_ceiling_ft()
    if c is None:
        ceil_cat = "VFR"
    else:
        if c < 400.0:
            ceil_cat = "LIFR"
        elif c < 1000.0:
            ceil_cat = "IFR"
        elif c < 2000.0:
            ceil_cat = "MVFR"
        else:
            ceil_cat = "VFR"

    order = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
    return vis_cat if order[vis_cat] >= order[ceil_cat] else ceil_cat

def enforce_min_2hr_rules(steps: List[Step]) -> List[Step]:
    """
    Enforce a minimum 2-hour persistence rule on the actual TAF elements
    that drive the prevailing group:
      - Precipitation
      - Visibility (BR/FG follows automatically because it is visibility-driven)
      - Sky condition / cloud layer

    IMPORTANT:
    We smooth the underlying Step fields, not only the derived flight-category
    label. This prevents contradictory states such as ``1600 BR BKN015`` being
    re-labelled VFR and then allowed to persist after group merging.

    For a 1-hour visibility or sky run, replace that hour with the neighboring
    regime that has the longer duration. At the beginning/end of the TAF, use
    the only available neighboring regime. Ties prefer the left/earlier regime.

    Mutates Step objects in-place.
    """
    if not steps:
        return steps

    # Clear any legacy category overrides. Category should be derived from the
    # actual smoothed visibility/sky fields, never forced independently.
    for s in steps:
        s.category_override = None

    # --- helper: run-length encode indices by a key ---
    def runs(values: List[str]) -> List[Tuple[int, int, str]]:
        # returns list of (start_idx, end_idx_exclusive, value)
        out: List[Tuple[int, int, str]] = []
        i = 0
        n = len(values)
        while i < n:
            j = i + 1
            while j < n and values[j] == values[i]:
                j += 1
            out.append((i, j, values[i]))
            i = j
        return out

    def replacement_index(run_list: List[Tuple[int, int, str]], r_idx: int) -> Optional[int]:
        """Return a representative step index from the preferred neighbor run."""
        left = run_list[r_idx - 1] if r_idx > 0 else None
        right = run_list[r_idx + 1] if (r_idx + 1) < len(run_list) else None

        if left is None and right is None:
            return None
        if left is None:
            return right[0]  # first step of right run
        if right is None:
            return left[1] - 1  # last step of left run

        left_len = left[1] - left[0]
        right_len = right[1] - right[0]
        if left_len >= right_len:
            return left[1] - 1
        return right[0]

    # --- 1) PRECIP smoothing: remove 1-hour precip runs ---
    precip_present: List[str] = []
    for s in steps:
        has_p = False
        if s.precip_type is not None and (s.precip_intensity_mmph or 0.0) > 0.0:
            has_p = True
        # TSRA implies precip-like operational line as well (keep it)
        if s.ts_in_5mi:
            has_p = True
        precip_present.append("P" if has_p else "N")

    for i0, i1, v in runs(precip_present):
        if v == "P" and (i1 - i0) < 2:
            for k in range(i0, i1):
                steps[k].precip_type = None
                steps[k].precip_intensity_mmph = 0.0
                # NOTE: thunder/vicinity flags are unchanged here.

    # --- 2) VISIBILITY smoothing: remove 1-hour rendered visibility regimes ---
    # BR/FG is derived from vis_token(), so replacing visibility also removes an
    # isolated 1-hour BR/FG restriction when the neighboring regime is unrestricted.
    vis_values = [s.vis_token() for s in steps]
    vis_runs = runs(vis_values)
    vis_replacements: List[Tuple[int, int]] = []
    for r_idx, (i0, i1, _tok) in enumerate(vis_runs):
        if (i1 - i0) >= 2:
            continue
        src_idx = replacement_index(vis_runs, r_idx)
        if src_idx is None:
            continue
        for k in range(i0, i1):
            vis_replacements.append((k, src_idx))

    # Apply after determining all runs so earlier replacements cannot affect
    # neighbor selection for later runs.
    for dst_idx, src_idx in vis_replacements:
        steps[dst_idx].vis_sm = steps[src_idx].vis_sm
        steps[dst_idx].vis_m = steps[src_idx].vis_m

    # --- 3) SKY smoothing: remove 1-hour rendered sky regimes ---
    # Copy the underlying cloud fields so both the printed sky and the derived
    # ceiling/category remain internally consistent.
    sky_values = [" ".join(s.sky_tokens()) for s in steps]
    sky_runs = runs(sky_values)
    sky_replacements: List[Tuple[int, int]] = []
    for r_idx, (i0, i1, _sky) in enumerate(sky_runs):
        if (i1 - i0) >= 2:
            continue
        src_idx = replacement_index(sky_runs, r_idx)
        if src_idx is None:
            continue
        for k in range(i0, i1):
            sky_replacements.append((k, src_idx))

    for dst_idx, src_idx in sky_replacements:
        steps[dst_idx].cloud_cover_pct = steps[src_idx].cloud_cover_pct
        steps[dst_idx].cloud_base_ft = steps[src_idx].cloud_base_ft
        steps[dst_idx].ceiling_ft_raw = steps[src_idx].ceiling_ft_raw
        steps[dst_idx].min_cb_ft_5mi = steps[src_idx].min_cb_ft_5mi

    return steps



    # ... rest of your category logic (your custom thresholds) ...

    """
    Updated thresholds (your spec):

    VISIBILITY (SM):
      - VFR:  >= 3
      - MVFR: >= 2 and < 3
      - IFR:  >= 1 and < 2
      - LIFR: <  1

    CEILING (ft) (using step.category_ceiling_ft()):
      - VFR:  >= 2000
      - MVFR: >= 1000 and < 2000
      - IFR:  >= 400  and < 1000
      - LIFR: <  400

    Rule: overall category is the WORSE (more restrictive) of vis-based and ceiling-based categories.
    If ceiling is missing (None), it does not restrict the category (vis alone governs).
    If visibility is missing, treat as unrestricted for categorization.
    """
    # --- visibility category ---
    v = step.vis_sm if step.vis_sm is not None else float("inf")
    if v < 1.0:
        vis_cat = "LIFR"
    elif v < 2.0:
        vis_cat = "IFR"
    elif v < 3.0:
        vis_cat = "MVFR"
    else:
        vis_cat = "VFR"

    # --- ceiling category ---
    c = step.category_ceiling_ft()
    if c is None:
        ceil_cat = "VFR"  # no ceiling restriction if no BKN/OVC ceiling present
    else:
        if c < 400.0:
            ceil_cat = "LIFR"
        elif c < 1000.0:
            ceil_cat = "IFR"
        elif c < 2000.0:
            ceil_cat = "MVFR"
        else:
            ceil_cat = "VFR"

    # pick the worse of the two
    order = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
    return vis_cat if order[vis_cat] >= order[ceil_cat] else ceil_cat

def group_has_precip(g: TafGroup) -> bool:
    """
    Treat any precip token in the group's wx as 'precip present'.
    """
    wx = (g.wx or "").strip()
    if not wx:
        return False
    tokens = wx.split()
    # normalize +/- and look at core precip tokens
    base = [t.lstrip("+-") for t in tokens]
    precip_set = {"RA", "SN", "FZRA", "DZ", "FZDZ", "PL", "SG", "GR", "GS"}
    return any(t in precip_set for t in base)

def parse_wind_token(w: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Parse dddffKT or VRBffKT. Returns (dir_deg, spd_kt, gust_kt). None if can't parse.
    """
    w = w.strip()
    m = re.match(r"^(VRB|\d{3})(\d{2})(G\d{2})?KT$", w)
    if not m:
        return None, None, None
    d = m.group(1)
    spd = int(m.group(2))
    gust = m.group(3)
    gust_kt = int(gust[1:]) if gust else None
    ddeg = None if d == "VRB" else int(d)
    return ddeg, spd, gust_kt


def wind_tokens_similar(w1: str, w2: str,
                        dir_tol_deg: int = 10,
                        spd_tol_kt: int = 2,
                        gust_tol_kt: int = 5) -> bool:
    """Return True if two rendered wind tokens are 'similar enough' to treat as the same regime.

    Intended for 1-hour FM blip removal (e.g., 05009G21KT followed by 05010G20KT).
    - VRB or unparseable directions: require both to be VRB/unparseable to be similar.
    - Direction tolerance is circular.
    - Gust tolerance compares explicit gusts if present; if missing, falls back to sustained.
    """
    d1, s1, g1 = parse_wind_token(w1)
    d2, s2, g2 = parse_wind_token(w2)
    if s1 is None or s2 is None:
        return False

    # If one has a fixed direction and the other doesn't, don't call it similar.
    if (d1 is None) != (d2 is None):
        return False

    if d1 is not None and d2 is not None:
        diff = abs((d2 - d1 + 180) % 360 - 180)
        if diff > dir_tol_deg:
            return False

    if abs(s2 - s1) > spd_tol_kt:
        return False

    gg1 = g1 if g1 is not None else s1
    gg2 = g2 if g2 is not None else s2
    if abs(gg2 - gg1) > gust_tol_kt:
        return False

    return True



def wind_break_from_group_tokens(g1: TafGroup, g2: TafGroup) -> bool:
    """
    Notable wind change rule (same as your step-level break thresholds):
      - direction change >= 30° AND (new) speed >= 5 kt
      - speed change >= 10 kt
      - gust change >= 10 kt (using gust value if present, otherwise sustained speed)
    """
    d1, s1, g1k = parse_wind_token(g1.wind)
    d2, s2, g2k = parse_wind_token(g2.wind)
    if s1 is None or s2 is None:
        return False

    # speed change
    if abs(s2 - s1) >= 10:
        return True

    # gust change (treat missing gust as sustained speed)
    prev_g = g1k if g1k is not None else s1
    cur_g  = g2k if g2k is not None else s2
    if abs(cur_g - prev_g) >= 10:
        return True

    # direction change (only if both dirs present)
    if d1 is not None and d2 is not None:
        ddir = abs((d2 - d1 + 180) % 360 - 180)
        if ddir >= 30 and s2 >= 5:
            return True

    return False



def sustained_group_gust_at_or_above(steps: List[Step], idx: int, threshold: int, hours: int = 3) -> bool:
    """Return True if gusts are >= threshold for `hours` consecutive steps starting at idx."""
    if idx + hours > len(steps):
        return False
    for j in range(idx, idx + hours):
        g = steps[j].wind_gust_kt
        if g is None or int(round(g)) < threshold:
            return False
    return True


def sustained_group_gust_at_or_below(steps: List[Step], idx: int, threshold: int, hours: int = 3) -> bool:
    """Return True if gusts are <= threshold for `hours` consecutive steps starting at idx.
    Missing gusts count as relaxed / below-threshold.
    """
    if idx + hours > len(steps):
        return False
    for j in range(idx, idx + hours):
        g = steps[j].wind_gust_kt
        if g is not None and int(round(g)) > threshold:
            return False
    return True


def gust_regime_break_between_group_winds(w1: str, w2: str, gust_delta_kt: int = 5) -> bool:
    """Return True if the rendered wind tokens imply a meaningful gust-regime change.

    This protects gust-driven FM groups from being merged away when the only important
    difference is the gust regime itself. Missing gust is treated as no gust regime.
    """
    _, s1, g1 = parse_wind_token(w1)
    _, s2, g2 = parse_wind_token(w2)
    if s1 is None or s2 is None:
        return False

    # Treat missing gust as no gust regime rather than sustained wind.
    a = g1 if g1 is not None else 0
    b = g2 if g2 is not None else 0

    # Gust regime starts/stops.
    if (a == 0) != (b == 0):
        return True

    # Meaningful gust change between adjacent groups.
    if abs(b - a) >= gust_delta_kt:
        return True

    return False

def merge_adjacent_same_category_groups(
    groups: List[TafGroup],
    steps_by_time: Dict[dt.datetime, Step],
) -> List[TafGroup]:
    """
    Merge adjacent groups if:
      - same flight category at group start time
      - AND no precip in either group
      - AND no notable wind change between groups
      - AND no gust-regime change between groups

    The merged group keeps the first group's kind/start; the second group is dropped.
    """
    if not groups:
        return groups

    def cat_for_group(g: TafGroup) -> str:
        s = steps_by_time.get(g.start)
        return flight_category(s) if s is not None else "VFR"

    out: List[TafGroup] = [groups[0]]
    for g in groups[1:]:
        prev = out[-1]

        same_cat = cat_for_group(prev) == cat_for_group(g)
        if (
            same_cat
            and (not group_has_precip(prev))
            and (not group_has_precip(g))
            and (not wind_break_from_group_tokens(prev, g))
            and (not gust_regime_break_between_group_winds(prev.wind, g.wind))
        ):
            # Merge by dropping g (prev implicitly extends until g's end)
            continue

        out.append(g)

    return out




def _is_unrestricted_vis_token(step: Step) -> bool:
    tok = step.vis_token()
    return (tok == "P6SM") if step.output_style == "US" else (tok == "9999")


def _group_duration_hours(groups: List[TafGroup], idx: int, valid_to: dt.datetime) -> int:
    """Return integer duration in hours for group idx."""
    start = groups[idx].start
    end = groups[idx + 1].start if (idx + 1) < len(groups) else valid_to
    return max(1, int(round((end - start).total_seconds() / 3600.0)))


def _parse_metric_vis_token(tok: str) -> int:
    """Best-effort numeric visibility in meters from rendered token."""
    tok = (tok or "").strip().upper()
    if tok == "9999":
        return 10000
    try:
        return int(tok)
    except Exception:
        return 10000


def _group_has_sig_wx(g: TafGroup) -> bool:
    """True if group contains precip/TS/vicinity tokens (excluding BR/FG)."""
    if not g.wx:
        return False
    toks = [t for t in g.wx.split() if t not in ("BR", "FG")]
    if not toks:
        return False
    # Any precip family or thunder/vicinity counts as significant
    for t in toks:
        core = t.lstrip("+-")
        if core in {"RA","SN","DZ","FZRA","FZDZ","PL","TSRA","VCTS","VCSH","SHRA","SHSN"}:
            return True
        if "TS" in core:  # catches VCTS etc
            return True
    return False


def _group_has_precip_or_vicinity(g: TafGroup) -> bool:
    """True if group contains precip, thunder/convective, or vicinity wx.

    BR/FG alone do NOT count.
    """
    if not g.wx:
        return False
    toks = [t for t in g.wx.split() if t not in ("BR", "FG")]
    if not toks:
        return False
    for t in toks:
        core = t.lstrip("+-")
        # precip families
        if core in {"RA","SN","DZ","FZRA","FZDZ","PL","SG","GR","GS"}:
            return True
        # showers / thunder / vicinity
        if core in {"TSRA","VCTS","VCSH","SHRA","SHSN"}:
            return True
        if "TS" in core or core.startswith("VC") or core.startswith("SH"):
            return True
    return False


def prune_transient_and_trend_fm_groups(
    groups: List[TafGroup],
    steps_by_time: Dict[dt.datetime, Step],
    valid_to: dt.datetime,
) -> List[TafGroup]:
    """Post-process FM groups to reduce noise during trends and remove 1-hour 'blips'.

    Implements two rules (only for FM groups; INIT/TEMPO are preserved):

    Rule 1 (trend pruning):
      If three consecutive FM groups (prev, mid, next) are:
        - each duration == 1 hour,
        - no significant wx (precip/TS/vicinity) in any of the three (BR/FG allowed),
        - wind token identical across the three,
        - and visibility + ceiling are monotonic (both trending up or both trending down),
      then drop the middle FM group.

    Rule 2 (transient extreme pruning):
      If a middle FM group is a 1-hour local extreme (visibility worse than BOTH neighbors,
      or ceiling lower than BOTH neighbors), with no significant wx, and winds match,
      then drop the middle FM group. (This removes unnecessary 1-hour dips like 1600 at 11Z
      when conditions improve and stay improved afterward.)
    """
    if len(groups) < 4:
        return groups

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        g = groups[i]
        if g.kind != "FM" or i == 0 or i == len(groups) - 1:
            out.append(g)
            i += 1
            continue

        # Need a prev and next FM
        prev = out[-1] if out else None
        nxt = groups[i + 1] if (i + 1) < len(groups) else None
        if prev is None or nxt is None or prev.kind != "FM" or nxt.kind != "FM":
            out.append(g)
            i += 1
            continue

        # Durations (hours)
        dur_prev = _group_duration_hours(groups, i - 1, valid_to)
        dur_mid = _group_duration_hours(groups, i, valid_to)
        dur_next = _group_duration_hours(groups, i + 1, valid_to)

        # Only prune 1-hour middle groups
        if dur_mid != 1:
            out.append(g)
            i += 1
            continue

        # Special-case wind-threshold exception:
        # If the FOLLOWING FM group exactly 1 hour later has similar winds,
        # drop this 1-hour FM even if it represents a wind-regime onset.
        # (Example: FM050200 05009G21KT followed by FM050300 05010G20KT.)
        if (nxt.start - g.start) == dt.timedelta(hours=1):
            if (not _group_has_sig_wx(g)) and wind_tokens_similar(g.wind, nxt.wind):
                i += 1
                continue

        # For any pruning we require middle->next wind to be stable.
        # (Prev may differ if a precip/wx regime ends.)
        if not wind_tokens_similar(g.wind, nxt.wind):
            out.append(g)
            i += 1
            continue

        # For pruning, the middle group must be "visibility-only" (BR/FG allowed, but no precip/TS/vicinity).
        if _group_has_sig_wx(g):
            out.append(g)
            i += 1
            continue

        # Gather numeric vis + ceiling from steps at group start
        s_prev = steps_by_time.get(prev.start)
        s_mid = steps_by_time.get(g.start)
        s_next = steps_by_time.get(nxt.start)

        if s_prev is None or s_mid is None or s_next is None:
            out.append(g)
            i += 1
            continue

        # Visibility numeric (use rendered tokens to match what users see)
        v0 = _parse_metric_vis_token(prev.vis) if s_prev.output_style == "METRIC" else (7 if prev.vis == "P6SM" else 0)
        v1 = _parse_metric_vis_token(g.vis)    if s_mid.output_style  == "METRIC" else (7 if g.vis == "P6SM" else 0)
        v2 = _parse_metric_vis_token(nxt.vis)  if s_next.output_style == "METRIC" else (7 if nxt.vis == "P6SM" else 0)

        # Ceiling numeric: derived from printed BKN/OVC base; None -> treat as very high (no restriction)
        c0 = s_prev.category_ceiling_ft()
        c1 = s_mid.category_ceiling_ft()
        c2 = s_next.category_ceiling_ft()
        C0 = c0 if c0 is not None else 1e9
        C1 = c1 if c1 is not None else 1e9
        C2 = c2 if c2 is not None else 1e9

        # Rule 1: trend pruning (prefer only when all three are short: 1h each)
        trend_ok = (dur_prev == 1 and dur_next == 1)
        if trend_ok:
            if _group_has_sig_wx(prev) or _group_has_sig_wx(nxt):
                out.append(g)
                i += 1
                continue
            if not wind_tokens_similar(prev.wind, g.wind):
                out.append(g)
                i += 1
                continue
            down = (v0 >= v1 >= v2) and (C0 >= C1 >= C2) and ((v0 > v1) or (v1 > v2) or (C0 > C1) or (C1 > C2))
            up   = (v0 <= v1 <= v2) and (C0 <= C1 <= C2) and ((v0 < v1) or (v1 < v2) or (C0 < C1) or (C1 < C2))
            if down or up:
                # Drop the middle group by skipping append
                i += 1
                continue

        # Rule 2: transient extreme pruning. Require next to persist a bit so the "signal" is clear.
        # (Your example: 12Z persists 3 hours.)
        extreme = ((v1 < v0 and v1 < v2) or (C1 < C0 and C1 < C2))
        if extreme and (dur_next >= 3):
            i += 1
            continue

        out.append(g)
        i += 1

    return out


def significant_change(a: Step, b: Step) -> bool:
    ca = flight_category(a)
    cb = flight_category(b)

    def wind_break(x: Step, y: Step) -> bool:
        if x.wind_dir is None or y.wind_dir is None or x.wind_spd_kt is None or y.wind_spd_kt is None:
            return False

        # speed change
        if abs(y.wind_spd_kt - x.wind_spd_kt) >= 10:
            return True

        # gust change (treat missing gust as sustained speed)
        prev_g = (x.wind_gust_kt if x.wind_gust_kt is not None else x.wind_spd_kt)
        cur_g  = (y.wind_gust_kt if y.wind_gust_kt is not None else y.wind_spd_kt)
        if abs(cur_g - prev_g) >= 10:
            return True

        # direction change (only if both dirs present, and winds are at least 5 kt)
        ddir = abs((y.wind_dir - x.wind_dir + 180) % 360 - 180)
        if ddir >= 30 and y.wind_spd_kt >= 5:
            return True

        return False

    # category change always breaks
    if ca != cb:
        return True

    # meaningful wind change breaks
    if wind_break(a, b):
        return True

    # any wx token change breaks (including -RA vs RA, BR/FG, VCTS, TSRA)
    if a.wx_tokens() != b.wx_tokens():
        return True

    # VFR/MVFR compression: do NOT break for sky/vis wiggles
    if ca in ("VFR", "MVFR"):
        return False

    # IFR/LIFR: keep stricter (sky changes matter)
    if a.sky_tokens() != b.sky_tokens():
        return True

    return False

    def has_precip(step: Step) -> bool:
        # Any precip descriptor present keeps lines separate (your rule)
        return len(precip_signature(" ".join(step.wx_tokens()))) > 0

    def strip_vis_wx(tokens: List[str]) -> List[str]:
        # BR/FG are visibility-driven; ignore them for VFR vis-compression grouping
        return [t for t in tokens if t not in ("BR", "FG")]

    # --- VFR VISIBILITY COMPRESSION ---
    # If BOTH are VFR, we merge VFR visibility changes (and BR/FG changes)
    # UNLESS precip is present in either line.
    if ca == "VFR" and cb == "VFR":
        # Still break for meaningful wind shifts
        if wind_break(a, b):
            return True

        # If precip exists in either hour, do NOT compress vis changes
        if has_precip(a) or has_precip(b):
            # With precip around, respect wx/sky changes normally
            if a.wx_tokens() != b.wx_tokens():
                return True
            if a.sky_tokens() != b.sky_tokens():
                return True
            # (vis token differences are implicitly handled via BR/FG / wx logic already)
            return False

        # No precip in either: ignore BR/FG and ignore vis/sky wiggles inside VFR
        wa = strip_vis_wx(a.wx_tokens())
        wb = strip_vis_wx(b.wx_tokens())

        # Still break for non-vis wx changes (e.g., VCTS starts/stops)
        if wa != wb:
            return True

        # Otherwise keep in same group
        return False

    # --- NON-VFR: keep original behavior ---
    if ca != cb:
        return True

    if wind_break(a, b):
        return True

    if a.wx_tokens() != b.wx_tokens():
        return True

    if a.sky_tokens() != b.sky_tokens():
        return True

    return False

    # --- NON-VFR: keep original behavior ---
    # Break when category changes (MVFR/IFR/LIFR etc.)
    if ca != cb:
        return True

    # Wind thresholds
    if a.wind_dir is not None and b.wind_dir is not None and a.wind_spd_kt is not None and b.wind_spd_kt is not None:
        ddir = abs((b.wind_dir - a.wind_dir + 180) % 360 - 180)
        if ddir >= 40 and b.wind_spd_kt >= 8:
            return True
        if abs(b.wind_spd_kt - a.wind_spd_kt) >= 10:
            return True

    # Wx and sky changes (non-VFR)
    if a.wx_tokens() != b.wx_tokens():
        return True

    if a.sky_tokens() != b.sky_tokens():
        return True

    return False


def merge_close_precip_groups(groups: List[TafGroup], max_gap: dt.timedelta = dt.timedelta(hours=2)) -> List[TafGroup]:
    if len(groups) < 3:
        return groups

    def same_nonwx(a: TafGroup, b: TafGroup) -> bool:
        return (a.wind == b.wind) and (a.vis == b.vis) and (a.sky == b.sky)

    merged: List[TafGroup] = []
    i = 0
    while i < len(groups):
        if i + 2 < len(groups):
            g1, g2, g3 = groups[i], groups[i + 1], groups[i + 2]
            if g1.kind in ("INIT", "FM") and g2.kind == "FM" and g3.kind == "FM":
                p1 = precip_signature(g1.wx)
                p2 = precip_signature(g2.wx)
                p3 = precip_signature(g3.wx)
                if p1 and (not p2) and (p1 == p3):
                    gap = g3.start - g2.start
                    if gap <= max_gap and same_nonwx(g1, g2) and same_nonwx(g2, g3):
                        merged.append(g1)
                        i += 3
                        continue
        merged.append(groups[i])
        i += 1
    return merged


def build_groups(steps: List[Step]) -> List[TafGroup]:
    if not steps:
        return []

    starts = [0]
    last_start_idx = 0
    gust_anchor_kt: Optional[int] = None

    for i in range(1, len(steps)):
        prev = steps[i - 1]
        cur = steps[i]
        anchor = steps[last_start_idx]

        forced_break = False

        # Boundary logic (hour-to-hour changes) OR accumulated wind changes since last FM.
        if significant_change(prev, cur):
            forced_break = True

        # Wind speed accumulated change: if sustained speed differs by >= 10 kt from the last FM line, break.
        if (not forced_break) and (anchor.wind_spd_kt is not None) and (cur.wind_spd_kt is not None):
            if abs(cur.wind_spd_kt - anchor.wind_spd_kt) >= 10.0:
                forced_break = True

        cur_g = int(round(cur.wind_gust_kt)) if cur.wind_gust_kt is not None else None
        prev_g = int(round(prev.wind_gust_kt)) if prev.wind_gust_kt is not None else None

        # Gust onset: first sustained 20+ kt gust regime for at least 3 hours.
        if (not forced_break) and (cur_g is not None) and (cur_g >= 20):
            if sustained_group_gust_at_or_above(steps, i, threshold=20, hours=3):
                if gust_anchor_kt is None and (prev_g is None or prev_g < 20):
                    forced_break = True

        # Gust escalation: once a gust-bearing FM exists, include a new FM at +5 kt or more.
        if (not forced_break) and (gust_anchor_kt is not None) and (cur_g is not None):
            if cur_g >= gust_anchor_kt + 5:
                forced_break = True

        # Gust relaxation: require a drop of 5 kt or more from the gust anchor,
        # sustained for at least 3 hours. Also covers gusts ending entirely.
        if (not forced_break) and (gust_anchor_kt is not None) and (gust_anchor_kt >= 20):
            relax_threshold = gust_anchor_kt - 5
            if cur_g is None:
                if i + 2 < len(steps):
                    future_none = True
                    for j in range(i, i + 3):
                        if steps[j].wind_gust_kt is not None:
                            future_none = False
                            break
                    if future_none:
                        forced_break = True
            elif cur_g <= relax_threshold:
                if sustained_group_gust_at_or_below(steps, i, threshold=relax_threshold, hours=3):
                    forced_break = True

        # Option B: if visibility clears to unrestricted for >= 2 consecutive hours,
        # force an FM at the first unrestricted hour so low vis/BR does not persist.
        if not forced_break:
            prev_unres = (prev.vis_token() == "P6SM") if prev.output_style == "US" else (prev.vis_token() == "9999")
            cur_unres  = (cur.vis_token()  == "P6SM") if cur.output_style  == "US" else (cur.vis_token()  == "9999")
            if (not prev_unres) and cur_unres and (i + 1) < len(steps):
                nxt = steps[i + 1]
                nxt_unres = (nxt.vis_token() == "P6SM") if nxt.output_style == "US" else (nxt.vis_token() == "9999")
                if nxt_unres:
                    forced_break = True

        if forced_break:
            starts.append(i)
            last_start_idx = i
            gust_anchor_kt = cur_g if cur_g is not None else None
            continue

    starts.append(len(steps))

    groups: List[TafGroup] = []

    for si in range(len(starts) - 1):
        i0 = starts[si]
        i1 = starts[si + 1]
        s0 = steps[i0]

        wx_list = s0.wx_tokens()
        sky_toks = s0.sky_tokens()

        # Any TS mention (TSRA, -TSRA, +TSRA, VCTS, etc.) => CB logic
        has_ts = any("TS" in w for w in wx_list)
        if has_ts:
            if sky_toks == ["SKC"]:
                sky_toks = sky_tokens_for_skc_tsra(s0)
            else:
                sky_toks = append_cb_to_last_cloud_group(sky_toks)

        wx = " ".join(wx_list)
        sky = " ".join(sky_toks)

        groups.append(
            TafGroup(
                kind="INIT" if si == 0 else "FM",
                start=s0.t,
                end=steps[i1 - 1].t if i1 - 1 >= i0 else s0.t,
                wind=s0.wind_token(),
                vis=s0.vis_token(),
                wx=(wx + " ") if wx else "",
                sky=(" " + sky) if sky else "",
            )
        )

    groups = merge_close_precip_groups(groups, max_gap=dt.timedelta(hours=2))
    return groups



# ----------------------------
# Zero ceiling guard (BKN000/OVC000)
# ----------------------------
def fix_zero_ceiling_layers(groups: List[TafGroup], placeholder: str = 'SCT015') -> List[TafGroup]:
    """Replace any BKN000/OVC000 tokens with the previous FM/INIT ceiling token.
    If none exists, use a placeholder (default SCT015).
    """
    def is_bad(token: str) -> bool:
        t = token.strip()
        if len(t) < 6:
            return False
        if not (t.startswith('BKN') or t.startswith('OVC')):
            return False
        core = t[3:6]
        return core == '000'

    def parse_hundreds(token: str) -> Optional[int]:
        # e.g. BKN007, OVC015, BKN050CB
        m = re.match(r'^(BKN|OVC)(\d{3})', token)
        if not m:
            return None
        try:
            return int(m.group(2))
        except Exception:
            return None

    prev_ceiling_token: Optional[str] = None
    fixed: List[TafGroup] = []
    for g in groups:
        sky_tokens = (g.sky or '').split()
        if sky_tokens:
            new_sky: List[str] = []
            for tok in sky_tokens:
                if is_bad(tok):
                    had_cb = tok.endswith('CB')
                    repl = prev_ceiling_token or placeholder
                    if had_cb and not repl.endswith('CB'):
                        repl = repl + 'CB'
                    new_sky.append(repl)
                else:
                    new_sky.append(tok)
            g = TafGroup(kind=g.kind, start=g.start, end=g.end, wind=g.wind, vis=g.vis, wx=g.wx, sky=' '.join(new_sky))

        # update prev ceiling token from (possibly fixed) group
        toks2 = (g.sky or '').split()
        candidates = []
        for t in toks2:
            if t.startswith('BKN') or t.startswith('OVC'):
                h = parse_hundreds(t)
                if h is not None and h > 0:
                    candidates.append((h, t))
        if candidates:
            candidates.sort(key=lambda x: x[0])  # lowest ceiling wins
            prev_ceiling_token = candidates[0][1]

        fixed.append(g)
    return fixed

def format_taf(icao: str, issue: dt.datetime, valid_from: dt.datetime, valid_to: dt.datetime, groups: List[TafGroup]) -> str:
    header = f"TAF {icao.upper()} {ddhhmm(issue)}Z {ddhh(valid_from)}/{ddhh(valid_to)}"
    if not groups:
        return header

    init = groups[0] if groups and groups[0].kind == "INIT" else None
    rest = groups[1:] if init else groups

    first_line = header + (" " + init.render() if init else "")
    body = "\n".join(g.render() for g in rest)
    return first_line + ("\n" + body if body else "")
# ----------------------------
# Steps from aggregated intervals
# ----------------------------

def to_steps_from_aggregated(
    aggregated_intervals: List[Dict[str, Any]],
    api_units: str,
    output_style: str,
    station_lat: float,
) -> List[Step]:
    steps: List[Step] = []
    for it in aggregated_intervals:
        t = parse_iso_z(it["startTime"])
        v = it.get("values", {})

        # Visibility
        vis = v.get("visibility")
        vis_sm: Optional[float] = None
        vis_m: Optional[float] = None
        if isinstance(vis, (int, float)):
            vis_val = float(vis)
            if api_units == "metric":
                # Timelines metric visibility is typically km
                vis_m = km_to_m(vis_val)
                vis_sm = km_to_sm(vis_val)
            else:
                # Timelines imperial visibility might come as miles (but sometimes km-ish).
                sm = km_to_sm(vis_val) if vis_val > 20 else vis_val
                vis_sm = sm
                vis_m = sm_to_m(sm)

        # Clouds (km -> ft)
        cb = v.get("cloudBase")
        cc = v.get("cloudCeiling")
        cloud_base_ft = km_to_ft(float(cb)) if isinstance(cb, (int, float)) else None
        ceiling_ft_raw = km_to_ft(float(cc)) if isinstance(cc, (int, float)) else None

        # Wind
        ws = v.get("windSpeed")
        wg = v.get("windGust")
        wind_spd_kt = (ms_to_kt(float(ws)) if api_units == "metric" else mph_to_kt(float(ws))) if isinstance(ws, (int, float)) else None
        wind_gust_kt = (ms_to_kt(float(wg)) if api_units == "metric" else mph_to_kt(float(wg))) if isinstance(wg, (int, float)) else None

        ts_in_5mi = bool(v.get("_ts_in_5mi", False))
        ts_in_25mi = bool(v.get("_ts_in_25mi", False))
        sh_in_10mi = bool(v.get("_sh_in_10mi", False))
        ts_intensity = float(v.get("_ts_intensity_mmph", 0.0) or 0.0)

        min_cb_ft_5mi = None
        min_cb_km = v.get("_minCloudBaseKm_5mi")
        if isinstance(min_cb_km, (int, float)):
            min_cb_ft_5mi = km_to_ft(float(min_cb_km))

        steps.append(
            Step(
                t=t,
                wind_dir=float(v["windDirection"]) if isinstance(v.get("windDirection"), (int, float)) else None,
                wind_spd_kt=wind_spd_kt,
                wind_gust_kt=wind_gust_kt,
                vis_sm=vis_sm,
                vis_m=vis_m,
                cloud_cover_pct=float(v["cloudCover"]) if isinstance(v.get("cloudCover"), (int, float)) else None,
                cloud_base_ft=cloud_base_ft,
                ceiling_ft_raw=ceiling_ft_raw,
                precip_type=int(v["precipitationType"]) if isinstance(v.get("precipitationType"), (int, float)) else None,
                precip_intensity_mmph=float(v["precipitationIntensity"]) if isinstance(v.get("precipitationIntensity"), (int, float)) else None,
                ts_in_5mi=ts_in_5mi,
                ts_in_25mi=ts_in_25mi,
                sh_in_10mi=sh_in_10mi,
                ts_intensity_mmph=ts_intensity,
                min_cb_ft_5mi=min_cb_ft_5mi,
                output_style=output_style,
                station_lat=station_lat,
            )
        )
    return steps



# ----------------------------
# Collapse identical FM wind/vis/sky groups (noise reduction)
# ----------------------------

def _wx_has_precip_or_ts(wx: str) -> bool:
    if not wx:
        return False
    toks = set(wx.split())
    precip = {"RA", "-RA", "+RA", "SN", "-SN", "+SN", "DZ", "-DZ", "+DZ", "FZRA", "-FZRA", "+FZRA", "PL"}
    thunder = {"TSRA", "-TSRA", "+TSRA", "VCTS"}
    return bool(toks & precip) or bool(toks & thunder)


def collapse_identical_fm_wind_vis_sky(groups: List[TafGroup]) -> List[TafGroup]:
    """Collapse adjacent FM groups when they are effectively identical.

    This is a *noise reduction* pass. It will NOT collapse if either group has precip/TS,
    but it *will* treat SCT/FEW/SKC/NSC-only skies as equivalent so small SCT height
    wiggles don't force extra FM lines.
    """
    if not groups:
        return groups

    def _is_sct_few_skc_only(sky: str) -> bool:
        toks = (sky or "").split()
        if not toks:
            return True
        for t in toks:
            t = t.strip().upper()
            if t in {"SKC", "NSC"}:
                continue
            if t.startswith("SCT") or t.startswith("FEW"):
                continue
            # anything else (BKN/OVC/CLR/VV/??) is not in this bucket
            return False
        return True

    out: List[TafGroup] = []
    for g in groups:
        if not out:
            out.append(g)
            continue

        prev = out[-1]

        if prev.kind == "FM" and g.kind == "FM":
            if (prev.wind == g.wind and
                prev.vis == g.vis and
                not _wx_has_precip_or_ts(prev.wx) and
                not _wx_has_precip_or_ts(g.wx)):
                same_sky = ((prev.sky or "") == (g.sky or ""))
                sct_equiv = _is_sct_few_skc_only(prev.sky or "") and _is_sct_few_skc_only(g.sky or "")
                if same_sky or sct_equiv:
                    continue

        out.append(g)

    return out


# ----------------------------
# Block smoothing: 3–6 consecutive FM groups where
#  - majority contain precipitation
#  - flight categories are within 1 step across the block
# Action:
    pass
#  - Prevailing FM becomes the most optimistic conditions in the block
#  - Add a TEMPO summarizing the lowest category that occurs >=2 hours within the block
#  - Precip intensity token uses the majority (mode) token across the block (e.g., -RA beats lone RA)
# ----------------------------

_FCAT_ORDER = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}



def compress_consecutive_vfr_fm_groups(
    groups: List[TafGroup],
    steps_by_time: Dict[dt.datetime, Step],
) -> List[TafGroup]:
    """Reduce runs of consecutive VFR FM groups to a single FM.

    Keep the FIRST FM in any consecutive run where:
      - all groups are kind == "FM"
      - each group's start-time flight category is VFR
      - no group in the run contains precip/TS/vicinity (BR/FG allowed)
      - and there is NO drastic wind change between consecutive groups

    Break the run (do NOT compress across) if:
      - precip/TS/vicinity appears, OR
      - wind is a drastic change, OR
      - any group is not VFR, OR
      - group kind is not FM
    """
    if not groups:
        return groups

    def cat(g: TafGroup) -> str:
        st = steps_by_time.get(g.start)
        return flight_category(st) if st is not None else "VFR"

    def has_sig_wx(g: TafGroup) -> bool:
        # Reuse earlier definition if present, otherwise fall back
        try:
            return _group_has_sig_wx(g)  # type: ignore[name-defined]
        except Exception:
            return _wx_has_precip_or_ts(g.wx)

    def brfg_state(g: TafGroup) -> str:
        """Return 'FG', 'BR', or 'NONE' based on wx tokens."""
        toks = (g.wx or "").split()
        if "FG" in toks:
            return "FG"
        if "BR" in toks:
            return "BR"
        return "NONE"

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        g0 = groups[i]
        if g0.kind != "FM" or cat(g0) != "VFR" or has_sig_wx(g0):
            out.append(g0)
            i += 1
            continue

        # Start a compressible VFR run at i
        run_start = i
        run_brfg = brfg_state(g0)
        j = i + 1
        while j < len(groups):
            gj = groups[j]
            if gj.kind != "FM":
                break
            if cat(gj) != "VFR":
                break
            if has_sig_wx(gj):
                break
            # Break the run when BR/FG first appears/disappears so we keep the first FM introducing it.
            if brfg_state(gj) != run_brfg:
                break
            # Break if drastic wind change between previous FM and this one
            prev_g = groups[j - 1]
            try:
                if wind_is_drastic_change(gj.wind, prev_g.wind):  # type: ignore[name-defined]
                    break
            except Exception:
                # fallback to token-based break
                if wind_break_from_group_tokens(prev_g, gj):  # type: ignore[name-defined]
                    break
            j += 1

        # If run length >=2, keep only first; else keep as-is
        out.append(g0)
        i = j

    return out


def _wx_has_any_precip_or_ts(wx: str) -> bool:
    if not wx:
        return False
    toks = set(wx.split())
    precip = {
        "RA","-RA","+RA","SN","-SN","+SN","DZ","-DZ","+DZ",
        "FZRA","-FZRA","+FZRA","PL","-PL","+PL",
        "SHRA","-SHRA","+SHRA","SHSN","-SHSN","+SHSN",
        "TSRA","-TSRA","+TSRA","VCTS"
    }
    return bool(toks & precip)

def _wx_has_any_precip(wx: str) -> bool:
    """True if wx contains an actual precip token (not VCTS-only)."""
    if not wx:
        return False
    toks = set(wx.split())
    precip = {
        "RA","-RA","+RA","SN","-SN","+SN","DZ","-DZ","+DZ",
        "FZRA","-FZRA","+FZRA","PL","-PL","+PL",
        "SHRA","-SHRA","+SHRA","SHSN","-SHSN","+SHSN",
        "TSRA","-TSRA","+TSRA"
    }
    return bool(toks & precip)

def _mode_precip_intensity_token(wx_list: List[str]) -> Optional[str]:
    from collections import Counter
    candidates = {"-RA","RA","+RA","-SN","SN","+SN","-TSRA","TSRA","+TSRA","-SHRA","SHRA","+SHRA","-SHSN","SHSN","+SHSN"}
    toks: List[str] = []
    for wx in wx_list:
        for t in (wx or "").split():
            if t in candidates:
                toks.append(t)
    if not toks:
        return None
    c = Counter(toks)
    top_n = c.most_common(1)[0][1]
    tied = [t for t, n in c.items() if n == top_n]
    # Tie-break: prefer lighter (more optimistic)
    rank = {
        "-RA":0,"RA":1,"+RA":2,
        "-SN":0,"SN":1,"+SN":2,
        "-TSRA":0,"TSRA":1,"+TSRA":2,
        "-SHRA":0,"SHRA":1,"+SHRA":2,
        "-SHSN":0,"SHSN":1,"+SHSN":2,
    }
    tied.sort(key=lambda t: rank.get(t, 1))
    return tied[0]

def _replace_family_token(wx: str, new_tok: str) -> str:
    if not wx or not new_tok:
        return wx
    parts = wx.split()
    if new_tok.endswith("TSRA"):
        fam = {"-TSRA","TSRA","+TSRA"}
    elif new_tok.endswith("SHRA"):
        fam = {"-SHRA","SHRA","+SHRA"}
    elif new_tok.endswith("SHSN"):
        fam = {"-SHSN","SHSN","+SHSN"}
    elif new_tok.endswith("RA"):
        fam = {"-RA","RA","+RA"}
    elif new_tok.endswith("SN"):
        fam = {"-SN","SN","+SN"}
    else:
        fam = set()

    for i, p in enumerate(parts):
        if p in fam:
            parts[i] = new_tok
            return " ".join(parts)
    return " ".join(parts)

def smooth_precip_blocks_to_prevailing_plus_tempo(groups: List[TafGroup], steps_by_time: Dict[dt.datetime, Step], valid_to: dt.datetime) -> List[TafGroup]:
    """Build prevailing+TEMPO precipitation blocks using HOURLY Step timing.

    Key rules:
      - TEMPO duration is based on actual hourly steps, never compressed FM spacing.
      - Each TEMPO window is capped at 6 elapsed hours.
      - A single dry hour may be bridged only when significant wx resumes immediately
        afterward in a compatible family.
      - Two consecutive dry hours always split an episode.
      - TEMPO ends one hour after the last qualifying weather step in its segment.
      - Existing FM groups are only used as presentation anchors; they do not define
        precipitation duration.
    """
    if not groups or not steps_by_time:
        return groups

    # -------- hourly weather classification --------
    def step_family(st: Step) -> str:
        if st.ts_in_5mi:
            return "TS"
        if st.precip_tokens():
            return "PRECIP"
        if st.ts_in_25mi:
            return "VCTS"
        if st.sh_in_10mi:
            return "VCSH"
        return "DRY"

    def significant_family(fam: str) -> bool:
        return fam in {"TS", "PRECIP", "VCTS", "VCSH"}

    def compatible(a: str, b: str) -> bool:
        if a == b:
            return True
        # Treat station precip and station thunder as the same wet/convective episode.
        if {a, b} <= {"TS", "PRECIP"}:
            return True
        # Vicinity signals can connect to station convection, but not to an unrelated
        # dry/showery regime by themselves.
        if a in {"TS", "PRECIP"} and b in {"VCTS", "VCSH"}:
            return True
        if b in {"TS", "PRECIP"} and a in {"VCTS", "VCSH"}:
            return True
        return False

    ordered_steps = [steps_by_time[t] for t in sorted(steps_by_time) if t < valid_to]
    if not ordered_steps:
        return groups

    # -------- find weather episodes on hourly data --------
    # Each episode is a list of qualifying/bridged Step objects. We retain a single
    # bridged dry hour only when significant wx resumes next hour in a compatible family.
    episodes: List[List[Step]] = []
    i = 0
    nsteps = len(ordered_steps)
    while i < nsteps:
        fam = step_family(ordered_steps[i])
        if not significant_family(fam):
            i += 1
            continue

        episode: List[Step] = [ordered_steps[i]]
        last_sig_family = fam
        j = i + 1

        while j < nsteps:
            st = ordered_steps[j]
            fam_j = step_family(st)

            if significant_family(fam_j):
                # Natural one-hour progression only; a missing timestamp breaks.
                if st.t - episode[-1].t != dt.timedelta(hours=1):
                    break
                if compatible(last_sig_family, fam_j):
                    episode.append(st)
                    last_sig_family = fam_j
                    j += 1
                    continue
                break

            # Dry hour: bridge only exactly one hour when the NEXT hour resumes
            # significant weather in a compatible family.
            if j + 1 < nsteps:
                nxt = ordered_steps[j + 1]
                fam_next = step_family(nxt)
                contiguous = (
                    st.t - episode[-1].t == dt.timedelta(hours=1)
                    and nxt.t - st.t == dt.timedelta(hours=1)
                )
                if contiguous and significant_family(fam_next) and compatible(last_sig_family, fam_next):
                    episode.append(st)   # bridged dry hour
                    episode.append(nxt)
                    last_sig_family = fam_next
                    j += 2
                    continue

            break

        episodes.append(episode)
        i = max(j, i + 1)

    if not episodes:
        return groups

    # -------- split each episode into <=6-hour elapsed TEMPO segments --------
    segments: List[Tuple[dt.datetime, dt.datetime, List[Step]]] = []
    for ep in episodes:
        if not ep:
            continue
        seg_start_idx = 0
        while seg_start_idx < len(ep):
            seg_start = ep[seg_start_idx].t
            seg_steps: List[Step] = []
            k = seg_start_idx
            while k < len(ep):
                st = ep[k]
                # Including hour st means the TEMPO ends at st.t + 1h.
                proposed_end = min(st.t + dt.timedelta(hours=1), valid_to)
                if proposed_end - seg_start > dt.timedelta(hours=6):
                    break
                seg_steps.append(st)
                k += 1

            if not seg_steps:
                # Safety: always advance at least one step.
                seg_steps = [ep[seg_start_idx]]
                k = seg_start_idx + 1

            # Do not let a bridged DRY hour define the TEMPO endpoint. Find the last
            # actually significant weather hour inside the segment.
            sig_steps = [st for st in seg_steps if significant_family(step_family(st))]
            if sig_steps:
                seg_end = min(sig_steps[-1].t + dt.timedelta(hours=1), valid_to)
                segments.append((seg_start, seg_end, seg_steps))

            seg_start_idx = k

    if not segments:
        return groups

    # -------- helper to choose representative group/conditions --------
    def group_for_time(t: dt.datetime) -> Optional[TafGroup]:
        # Choose the latest INIT/FM group whose start is <= t.
        candidates = [g for g in groups if g.kind in {"INIT", "FM"} and g.start <= t]
        return max(candidates, key=lambda g: g.start) if candidates else None

    def step_precip_token(st: Step) -> Optional[str]:
        toks = st.wx_tokens()
        priority = [
            "-TSRA", "TSRA", "+TSRA",
            "-SHRA", "SHRA", "+SHRA",
            "-SHSN", "SHSN", "+SHSN",
            "-RA", "RA", "+RA",
            "-SN", "SN", "+SN",
            "-DZ", "DZ", "+DZ",
            "FZRA", "-FZRA", "+FZRA",
            "PL", "-PL", "+PL",
        ]
        for tok in priority:
            if tok in toks:
                return tok
        return None

    from collections import Counter

    tempo_groups: List[TafGroup] = []
    segment_ranges: List[Tuple[dt.datetime, dt.datetime]] = []

    for seg_start, seg_end, seg_steps in segments:
        if seg_end <= seg_start:
            continue

        sig_steps = [st for st in seg_steps if significant_family(step_family(st))]
        if not sig_steps:
            continue

        # Representative wx: mode of actual station precip/TS tokens. If the segment
        # contains only vicinity weather, do not synthesize a precip TEMPO here.
        precip_tokens = [step_precip_token(st) for st in sig_steps]
        precip_tokens = [t for t in precip_tokens if t]
        if not precip_tokens:
            continue

        counts = Counter(precip_tokens)
        top_n = max(counts.values())
        tied = [t for t, c in counts.items() if c == top_n]
        # Prefer thunder over plain precip on a tie; within a family prefer lighter.
        def wx_rank(tok: str) -> Tuple[int, int]:
            core = tok.lstrip("+-")
            family_rank = 0 if core == "TSRA" else 1
            intensity_rank = 0 if tok.startswith("-") else (2 if tok.startswith("+") else 1)
            return (family_rank, intensity_rank)
        tied.sort(key=wx_rank)
        tempo_tok = tied[0]

        # Worst vis/ceiling within the segment for TEMPO depiction.
        def severity_key(st: Step) -> Tuple[float, float]:
            vis_m = float(st.vis_m) if st.vis_m is not None else float("inf")
            ceil = float(st.category_ceiling_ft()) if st.category_ceiling_ft() is not None else float("inf")
            return (vis_m, ceil)

        worst_step = min(sig_steps, key=severity_key)
        anchor = group_for_time(seg_start)
        wind = worst_step.wind_token()
        vis = worst_step.vis_token()
        sky_toks = worst_step.sky_tokens()
        if "TS" in tempo_tok:
            if sky_toks == ["SKC"]:
                sky_toks = sky_tokens_for_skc_tsra(worst_step)
            else:
                sky_toks = append_cb_to_last_cloud_group(sky_toks)
        sky = " ".join(sky_toks)

        tempo_groups.append(TafGroup(
            kind="TEMPO",
            start=seg_start,
            end=seg_end,
            wind=wind if anchor is not None else wind,
            vis=vis,
            wx=tempo_tok,
            sky=sky,
        ))
        segment_ranges.append((seg_start, seg_end))

    if not tempo_groups:
        return groups

    # -------- remove FM precip groups fully covered by a generated TEMPO --------
    # Preserve dry INIT/FM anchors and any FM outside the TEMPO windows.
    def covered_by_tempo(g: TafGroup) -> bool:
        if g.kind != "FM":
            return False
        if not _group_has_precip_or_vicinity(g):
            return False
        return any(start <= g.start < end for start, end in segment_ranges)

    base_groups = [g for g in groups if not covered_by_tempo(g)]

    # If a long precipitation FM was removed and no dry FM exists at the actual end of
    # the episode, create a dry FM anchor from the hourly step at that endpoint so the
    # TEMPO/precip regime cannot implicitly persist into a long dry tail.
    existing_starts = {g.start for g in base_groups if g.kind in {"INIT", "FM"}}
    for _start, end in segment_ranges:
        st = steps_by_time.get(end)
        if st is None or end >= valid_to or end in existing_starts:
            continue
        if significant_family(step_family(st)):
            continue
        base_groups.append(TafGroup(
            kind="FM",
            start=end,
            end=None,
            wind=st.wind_token(),
            vis=st.vis_token(),
            wx=" ".join(st.wx_tokens()),
            sky=" ".join(st.sky_tokens()),
        ))
        existing_starts.add(end)

    # Insert TEMPO groups immediately after the prevailing INIT/FM that begins before
    # them. Sorting by time and kind keeps INIT/FM before TEMPO at the same timestamp.
    all_groups = base_groups + tempo_groups
    kind_order = {"INIT": 0, "FM": 1, "TEMPO": 2}
    all_groups.sort(key=lambda g: (g.start, kind_order.get(g.kind, 9)))

    return all_groups



# ----------------------------
# TEMPO omissions: drop wind unless drastic vs preceding FM, and drop vis/sky
# when TEMPO flight category matches preceding FM flight category.
# ----------------------------


def apply_tempo_omissions(groups: List[TafGroup], steps_by_time: Dict[dt.datetime, Step]) -> List[TafGroup]:
    """
    TEMPO omissions:
      - Wind: include only if *drastically* different from preceding FM wind; otherwise omit.
      - Vis/Sky: if TEMPO flight category matches preceding FM flight category, omit both vis and sky.
      - Wx: if predominant precip token (e.g., -RA/RA/+RA, -SN/SN/+SN, -TSRA/TSRA/+TSRA) matches preceding FM,
            omit that precip token from TEMPO (keep BR/FG etc).
      - VCSH/VCTS: always omit from TEMPO.
    """
    def _norm_wind(w: Optional[str]) -> Optional[str]:
        if not w:
            return None
        return w.strip().upper()

    def _pred_precip_token(wx: Optional[str]) -> Optional[str]:
        if not wx:
            return None
        toks = wx.split()
        priority = [
            "-TSRA","TSRA","+TSRA",
            "-SHRA","SHRA","+SHRA",
            "-SHSN","SHSN","+SHSN",
            "-RA","RA","+RA",
            "-SN","SN","+SN",
            "-DZ","DZ","+DZ",
            "FZRA","-FZRA","+FZRA",
            "PL","-PL","+PL",
            "VCTS","VCSH",
        ]
        for p in priority:
            if p in toks:
                return p
        return None

    def _strip_token(wx: Optional[str], tok: Optional[str]) -> Optional[str]:
        if not wx:
            return None
        if not tok:
            return wx
        parts = [p for p in wx.split() if p != tok]
        return " ".join(parts) if parts else None

    out: List[TafGroup] = []
    last_fm: Optional[TafGroup] = None

    for g in groups:
        if g.kind in ("FM", "INIT"):
            last_fm = g
            out.append(g)
            continue

        if g.kind != "TEMPO":
            out.append(g)
            continue

        wind = _norm_wind(g.wind)
        vis = g.vis
        sky = g.sky
        wx = g.wx

        prev_wind = _norm_wind(last_fm.wind) if last_fm is not None else None

        # Wind: keep only if drastic vs preceding FM; otherwise omit
        if wind and prev_wind:
            try:
                if not wind_is_drastic_change(wind, prev_wind):
                    wind = None
            except Exception:
                wind = None

        # Vis/Sky: if TEMPO category == preceding FM category, omit both
        if last_fm is not None:
            st_tempo = steps_by_time.get(g.start)
            st_prev = steps_by_time.get(last_fm.start)
            if st_tempo is not None and st_prev is not None:
                try:
                    if flight_category(st_tempo) == flight_category(st_prev):
                        vis = None
                        sky = None
                except Exception:
                    pass

        # Wx: remove tokens that match preceding FM.
        # - If predominant precip token matches, remove it.
        # - Also remove BR/FG if they match preceding FM (may cause TEMPO to disappear).
        if last_fm is not None:
            wx_prev = last_fm.wx or ""
            tok_prev = _pred_precip_token(wx_prev)
            tok_temp = _pred_precip_token(wx)

            if tok_prev and tok_temp and tok_prev == tok_temp:
                wx = _strip_token(wx, tok_temp)

            # Remove BR/FG if also present in preceding FM
            for restr in ("BR", "FG"):
                if restr in (wx or "").split() and restr in wx_prev.split():
                    wx = _strip_token(wx, restr)

        # Never include vicinity showers/thunderstorms in TEMPO (prevents "TEMPO ... VCSH/VCTS").
        for vic in ("VCSH", "VCTS"):
            if vic in (wx or "").split():
                wx = _strip_token(wx, vic)

        # If wx is only BR/FG and there is no vis/sky included, drop it (avoid TEMPO 'BR' alone).
        if (vis is None) and (sky is None):
            wx_toks = (wx or "").split()
            if wx_toks and all(t in ("BR", "FG") for t in wx_toks):
                wx = None

        # If TEMPO has nothing meaningful left (no wind/vis/sky/wx), drop it entirely.
        if (wind is None) and (vis is None) and (sky is None) and (not (wx or "").strip()):
            continue

        out.append(replace(g, wind=wind, vis=vis, wx=wx, sky=sky))

    return out


def _precip_intensity_signature(wx: str) -> str:
    """Normalized precip signature INCLUDING intensity (+/-) for comparisons."""
    if not wx:
        return ""
    sig = []
    for tok in re.split(r"\s+", wx.strip()):
        if not tok:
            continue
        core = tok.lstrip("+-")
        if core in {"RA","SN","DZ","FZRA","FZDZ","PL","TSRA","SHRA","SHSN"}:
            sig.append(tok)  # keep sign
        elif core in {"VCSH","VCTS"}:
            sig.append(core)
    if not sig:
        return ""
    return "|".join(sorted(sig))

def _extract_ceiling_token_and_ft(sky: str) -> Tuple[str, Optional[int], int]:
    """Return (token, feet, rank) where rank favors more restrictive coverage (OVC<BKN<others)."""
    best_tok = ""
    best_ft: Optional[int] = None
    best_rank = 2
    for tok in (sky or "").split():
        if len(tok) < 6:
            continue
        cov = tok[:3]
        if cov not in {"OVC","BKN","SCT","FEW"}:
            continue
        try:
            h = int(tok[3:6]) * 100
        except Exception:
            continue
        rank = 0 if cov == "OVC" else (1 if cov == "BKN" else 2)
        if best_ft is None or h < best_ft or (h == best_ft and rank < best_rank):
            best_tok, best_ft, best_rank = tok, h, rank
    return best_tok, best_ft, best_rank

def _vis_sm_from_token(tok: str) -> float:
    tok = (tok or "").strip().upper()
    if tok.startswith("P6"):
        return 99.0
    tok = tok.replace("SM", "")
    try:
        if " " in tok:
            whole, frac = tok.split(" ", 1)
            num, den = frac.split("/", 1)
            return float(whole) + float(num) / float(den)
        if "/" in tok:
            num, den = tok.split("/", 1)
            return float(num) / float(den)
        return float(tok)
    except Exception:
        return 99.0

def _vis_m_from_token(tok: str) -> int:
    tok = (tok or "").strip().upper()
    if tok == "9999":
        return 10000
    try:
        return int(tok)
    except Exception:
        return 10000

def _lower_vis_token(a: str, b: str) -> str:
    a_u = (a or "").upper()
    b_u = (b or "").upper()
    us = ("SM" in a_u) or ("SM" in b_u) or a_u.startswith("P6") or b_u.startswith("P6")
    if us:
        return a if _vis_sm_from_token(a_u) <= _vis_sm_from_token(b_u) else b
    return a if _vis_m_from_token(a_u) <= _vis_m_from_token(b_u) else b

def _higher_vis_token(a: str, b: str) -> str:
    """Return the higher/better visibility token (opposite of _lower_vis_token)."""
    a_u = (a or "").upper()
    b_u = (b or "").upper()
    us = ("SM" in a_u) or ("SM" in b_u) or a_u.startswith("P6") or b_u.startswith("P6")
    if us:
        return a if _vis_sm_from_token(a_u) >= _vis_sm_from_token(b_u) else b
    return a if _vis_m_from_token(a_u) >= _vis_m_from_token(b_u) else b

def _block_merge_due_to_wind(g1: TafGroup, g3: TafGroup) -> bool:
        """Return True if we should NOT merge g1 and g3 due to a meaningful wind shift.

        Rule:
          - If either direction is VRB/unavailable -> never block (merge allowed).
          - Otherwise, if direction difference >= 30 degrees AND sustained winds >= 5 kt in BOTH groups -> block.
        """
        d1, sp1, _ = parse_wind_token(g1.wind)
        d3, sp3, _ = parse_wind_token(g3.wind)
        if d1 is None or d3 is None:
            return False  # variable/unparseable direction -> allow merge
        if sp1 is None or sp3 is None:
            return False
        if sp1 < 5 or sp3 < 5:
            return False
        diff = abs((d3 - d1 + 180) % 360 - 180)
        return diff >= 30
def smooth_3hr_precip_same_intensity_blip(groups: List[TafGroup], valid_to: dt.datetime) -> List[TafGroup]:
    """3-hour precip-consistency smoother.

    If 3 consecutive hourly groups (INIT+FM+FM or FM+FM+FM) share the same precip intensity signature, and the
    1st and 3rd share the same visibility and/or ceiling while the middle differs, drop
    the middle and merge outer two by selecting lowest ceiling + lowest visibility.

    Block if dramatic wind shift (>=30°) with sustained winds >=10 kt between either adjacent pair.
    """
    if len(groups) < 3:
        return groups

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        if i + 2 < len(groups):
            g1, g2, g3 = groups[i], groups[i + 1], groups[i + 2]
            if g1.kind in {"INIT","FM"} and g2.kind == "FM" and g3.kind == "FM":
                if (g2.start - g1.start) == dt.timedelta(hours=1) and (g3.start - g2.start) == dt.timedelta(hours=1):
                    s1 = _precip_intensity_signature(g1.wx)
                    s2 = _precip_intensity_signature(g2.wx)
                    s3 = _precip_intensity_signature(g3.wx)
                    if s1 and (s1 == s2 == s3):
                        if _block_merge_due_to_wind(g1, g3):
                                out.append(g1)
                                i += 1
                                continue

                        _, c1_ft, _ = _extract_ceiling_token_and_ft(g1.sky)
                        _, c2_ft, _ = _extract_ceiling_token_and_ft(g2.sky)
                        _, c3_ft, _ = _extract_ceiling_token_and_ft(g3.sky)

                        vis_match = (g1.vis == g3.vis)
                        ceil_match = (c1_ft is not None and c3_ft is not None and c1_ft == c3_ft)

                        middle_diff = False
                        if vis_match and (g2.vis != g1.vis):
                            middle_diff = True
                        if ceil_match and (c2_ft != c1_ft):
                            middle_diff = True

                        if (vis_match or ceil_match) and middle_diff:
                            # Merge into g1; drop g2 & g3
                            g1.vis = _lower_vis_token(g1.vis, g3.vis)

                            # Select lower ceiling by comparing parsed feet; fallback to whichever has a ceiling.
                            if c1_ft is None and c3_ft is not None:
                                g1.sky = g3.sky
                            elif c3_ft is None and c1_ft is not None:
                                pass
                            elif c1_ft is not None and c3_ft is not None:
                                if c3_ft < c1_ft:
                                    g1.sky = g3.sky
                                elif c3_ft == c1_ft:
                                    # If equal, prefer OVC over BKN if one is more restrictive at that height.
                                    if "OVC" in g3.sky and "OVC" not in g1.sky:
                                        g1.sky = g3.sky

                            out.append(g1)
                            i += 3
                            continue
        out.append(groups[i])
        i += 1

    return out

def remove_1hr_dry_blips_between_precip(groups: List[TafGroup], valid_to: dt.datetime) -> List[TafGroup]:
    """Remove 1-hour 'dry' FM groups that sit between precip/vicinity hours.

    Rule:
      - Candidate must be an FM group with duration == 1 hour (to the next group's start).
      - Candidate must NOT contain precip/vicinity (no RA/SN/etc, no VCSH/VCTS/TS).
      - The immediately preceding group (INIT or FM) must contain precip/vicinity.
      - The immediately following group (FM) must contain precip/vicinity.
      - Block removal if merging the surrounding groups would be prevented by wind:
          wind dir diff between surrounding groups >=30° AND sustained >=5 kt in BOTH,
          unless either direction is VRB/unparseable (then do NOT block).
    Effect:
      - Drop the dry FM group. The prior group implicitly extends to the next start.
    """
    if len(groups) < 3:
        return groups

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        g = groups[i]
        if g.kind == "FM":
            dur = _group_duration_hours(groups, i, valid_to)
            if dur == 1 and (not _group_has_precip_or_vicinity(g)):
                prev = out[-1] if out else None
                nxt = groups[i + 1] if (i + 1) < len(groups) else None
                if prev is not None and nxt is not None and nxt.kind == "FM":
                    if _group_has_precip_or_vicinity(prev) and _group_has_precip_or_vicinity(nxt):
                        # Only block on wind if surrounding groups are close in time (prevents blocking long-gap INIT->FM cases)
                        time_gap_ok = (nxt.start - prev.start) <= dt.timedelta(hours=3)
                        if (not time_gap_ok) or (not _block_merge_due_to_wind(prev, nxt)):
                            # Drop this 1-hour dry blip
                            i += 1
                            continue
        out.append(g)
        i += 1

    return out

def _flight_cat_rank_from_group(g: TafGroup) -> Tuple[int, int]:
    """Return (ceil_rank, vis_rank) where higher is better: LIFR=0, IFR=1, MVFR=2, VFR=3.

    Uses g.sky for ceiling and g.vis for visibility.
    """
    # Ceiling rank
    _, c_ft, _ = _extract_ceiling_token_and_ft(g.sky)
    if c_ft is None:
        ceil_rank = 3
    else:
        if c_ft < 500:
            ceil_rank = 0
        elif c_ft < 1000:
            ceil_rank = 1
        elif c_ft < 2000:
            ceil_rank = 2
        else:
            ceil_rank = 3

    # Visibility rank (use your thresholds)
    vtok = (g.vis or "").strip().upper()
    is_us = ("SM" in vtok) or vtok.startswith("P6")
    if is_us:
        v_sm = _vis_sm_from_token(vtok)
    else:
        # metric: token is meters; convert to SM
        v_m = _vis_m_from_token(vtok)
        v_sm = m_to_sm(float(v_m))
    if v_sm < 1.0:
        vis_rank = 0
    elif v_sm < 2.0:
        vis_rank = 1
    elif v_sm < 3.0:
        vis_rank = 2
    else:
        vis_rank = 3

    return ceil_rank, vis_rank

def smooth_init_first_fm_promote_if_one_cat(groups: List[TafGroup]) -> List[TafGroup]:
    """If INIT and the first FM exactly 1 hour later share the same precip descriptor,
    and either ceiling or visibility differs by exactly ONE flight category (adjacent),
    promote INIT's ceiling+visibility to the FM's values and drop that FM group.

    Example intent: INIT IFR, FM MVFR (or MVFR->IFR) is allowed; IFR->VFR is NOT.
    """
    if len(groups) < 2:
        return groups
    if groups[0].kind != "INIT":
        return groups
    g0 = groups[0]
    g1 = groups[1]
    if g1.kind != "FM":
        return groups
    if (g1.start - g0.start) != dt.timedelta(hours=1):
        return groups

    s0 = _precip_intensity_signature(g0.wx)
    s1 = _precip_intensity_signature(g1.wx)
    if not s0 or s0 != s1:
        return groups

    c0, v0 = _flight_cat_rank_from_group(g0)
    c1, v1 = _flight_cat_rank_from_group(g1)

    one_cat_ceil = abs(c1 - c0) == 1
    one_cat_vis = abs(v1 - v0) == 1

    if one_cat_ceil or one_cat_vis:
        # Promote INIT sky/vis to FM's, keep INIT wind and wx.
        g0.sky = g1.sky
        g0.vis = g1.vis
        # Drop g1
        return [g0] + groups[2:]

    return groups


def remove_1hr_wx_blip_fm_groups(groups: List[TafGroup], valid_to: dt.datetime) -> List[TafGroup]:
    """Drop FM groups that contain precip/VCSH/VCTS for only 1 hour."""
    if not groups:
        return groups
    out: List[TafGroup] = []
    for i, g in enumerate(groups):
        if g.kind != "FM":
            out.append(g)
            continue
        dur = _group_duration_hours(groups, i, valid_to)
        if dur == 1 and _group_has_precip_or_vicinity(g):
            continue
        out.append(g)
    return out

def _overall_flight_cat_rank(g: TafGroup) -> int:
    """Overall flight category rank for smoothing decisions: worst of ceiling/visibility.
    Rank: LIFR=0, IFR=1, MVFR=2, VFR=3
    """
    ceil_rank, vis_rank = _flight_cat_rank_from_group(g)
    return min(ceil_rank, vis_rank)

def remove_1hr_dry_improvement_blips(groups: List[TafGroup], valid_to: dt.datetime) -> List[TafGroup]:
    """Remove 1-hour *dry* FM groups that are a one-category improvement 'blip' between two groups
    with the same overall flight category.

    Example: IFR (prev) -> MVFR (candidate, 1hr, dry) -> IFR (next) => drop candidate.

    Conditions:
      - candidate is FM with duration == 1 hour
      - candidate is dry (no precip tokens and no VCSH/VCTS/TS)
      - prev exists (INIT or FM), next exists (FM)
      - prev and next have the SAME overall flight category (worst of ceil/vis)
      - candidate overall category differs by exactly ONE category AND is BETTER (higher rank)
      - Block removal if wind between prev and next should block merging (>=30° and >=5 kt in both),
        unless either direction is VRB/unparseable (then do not block).
    """
    if len(groups) < 3:
        return groups

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        g = groups[i]
        if g.kind == "FM":
            dur = _group_duration_hours(groups, i, valid_to)
            if dur == 1 and (not _group_has_precip_or_vicinity(g)):
                prev = out[-1] if out else None
                nxt = groups[i + 1] if (i + 1) < len(groups) else None
                if prev is not None and nxt is not None and nxt.kind == "FM":
                    prev_cat = _overall_flight_cat_rank(prev)
                    nxt_cat = _overall_flight_cat_rank(nxt)
                    if prev_cat == nxt_cat:
                        cand_cat = _overall_flight_cat_rank(g)
                        if (cand_cat - prev_cat) == 1:  # one-category improvement
                            if not _block_merge_due_to_wind(prev, nxt):
                                # Drop the 1-hour dry improvement blip
                                i += 1
                                continue
        out.append(g)
        i += 1
    return out

def _vis_category_rank(g: TafGroup) -> int:
    """Visibility category rank using your thresholds: LIFR(0)<IFR(1)<MVFR(2)<VFR(3)."""
    vtok = (g.vis or "").strip().upper()
    is_us = ("SM" in vtok) or vtok.startswith("P6")
    if is_us:
        v_sm = _vis_sm_from_token(vtok)
    else:
        v_m = _vis_m_from_token(vtok)
        v_sm = m_to_sm(float(v_m))
    if v_sm < 1.0:
        return 0
    if v_sm < 2.0:
        return 1
    if v_sm < 3.0:
        return 2
    return 3

def merge_adjacent_dry_bkn_ovc_same_height(groups: List[TafGroup], valid_to: dt.datetime) -> List[TafGroup]:
    """Merge adjacent *dry* groups when ceiling height is identical and the only sky difference
    is BKN vs OVC at that same height.

    Requirements (both groups):
      - kind in {INIT, FM} for left; FM for right (standard adjacency)
      - dry: no precip and no vicinity (no RA/SN/etc, no VCSH/VCTS/TS)
      - same visibility CATEGORY (not necessarily identical token)
      - lowest ceiling height (ft) is identical and coverage is BKN/OVC in both
      - wind merge still respects _block_merge_due_to_wind (>=30° & >=5kt in both, unless VRB)
    Result:
      - merge into the left group; sky becomes the MORE RESTRICTIVE of the two at that height (OVC preferred)
      - keep the lower (more restrictive) of the two visibility tokens
    """
    if len(groups) < 2:
        return groups

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        g1 = groups[i]
        if i + 1 < len(groups):
            g2 = groups[i + 1]
            if (g2.kind == "FM") and (g1.kind in {"INIT","FM"}):
                if (not _group_has_precip_or_vicinity(g1)) and (not _group_has_precip_or_vicinity(g2)):
                    # vis category match
                    if _vis_category_rank(g1) == _vis_category_rank(g2):
                        # ceiling height match and only BKN/OVC difference
                        t1, c1_ft, _ = _extract_ceiling_token_and_ft(g1.sky)
                        t2, c2_ft, _ = _extract_ceiling_token_and_ft(g2.sky)
                        if c1_ft is not None and c2_ft is not None and c1_ft == c2_ft:
                            cov1 = t1[:3] if t1 else ""
                            cov2 = t2[:3] if t2 else ""
                            if cov1 in {"BKN","OVC"} and cov2 in {"BKN","OVC"}:
                                # wind block check between the two to-be-merged groups
                                if not _block_merge_due_to_wind(g1, g2):
                                    # merge g2 into g1
                                    # Option A: keep the BETTER (more optimistic) visibility when only sky cover toggles BKN/OVC at the same height.
                                    g1.vis = _higher_vis_token(g1.vis, g2.vis)
                                    # choose more restrictive coverage at same height (OVC over BKN)
                                    if cov2 == "OVC" and cov1 != "OVC":
                                        g1.sky = g2.sky
                                    # else keep g1.sky
                                    out.append(g1)
                                    i += 2
                                    continue
        out.append(g1)
        i += 1
    return out


# ----------------------------
# FINAL SIMILARITY MERGE (last-pass smoothing)
# ----------------------------

def final_merge_similar_fm_groups(
    groups: List[TafGroup],
    valid_to: dt.datetime,
    dir_tol_deg: int = 30,
    spd_tol_kt: int = 2,
    gust_tol_kt: int = 5,
    ceil_tol_ft: int = 500,
) -> List[TafGroup]:
    """Final pass to merge *adjacent* FM groups that are operationally similar.

    Intended to remove extra FM lines caused by small wiggles in:
      - wind direction/speed,
      - SCT-only cloud base changes,
      - small ceiling (BKN/OVC) height changes within a narrow tolerance.

    Guardrails (conservative):
      - By default, avoids merging precip/TS/vicinity; BUT will merge steady precip when wx strings match exactly (e.g., -RA to -RA) and wx is not convective/vicinity/showery. (BR/FG allowed).
      - Requires same visibility category (LIFR/IFR/MVFR/VFR).
      - Requires winds to be 'similar' (tolerances).
      - If either group has a ceiling (BKN/OVC), only merge if both have ceilings and
        the ceiling bases are within ceil_tol_ft.

    When merging:
      - Keep the *first* group's wind (don't backdate wind shifts).
      - Keep the *lower/more restrictive* visibility token.
      - Keep the *more restrictive* sky depiction (lower ceiling; OVC preferred over BKN at same height).
    """
    if len(groups) < 2:
        return groups

    def _has_sig_wx(g: TafGroup) -> bool:
        # Significant wx = precip/TS/vicinity. BR/FG alone is not significant.
        return _group_has_precip_or_vicinity(g)


    def _wx_ok_to_merge_when_equal(wx: str) -> bool:
        """Return True if wx tokens are safe to merge when wx strings are identical.

        We allow steady precip like RA/SN/DZ (including +/-), and BR/FG.
        We DO NOT merge if wx contains thunder, vicinity, or showers, to avoid
        hiding convective or spatially-variable regimes.
        """
        s = (wx or "").strip().upper()
        if not s:
            return True
        toks = s.split()
        for t in toks:
            # vicinity / thunder
            if t.startswith("VC") or "TS" in t:
                return False
            # showers are more spatially variable
            if t.startswith("SH"):
                return False
        return True

    def _ceiling_ft(g: TafGroup) -> Optional[int]:
        tok, ft, _ = _extract_ceiling_token_and_ft(g.sky or "")
        if tok.startswith(("BKN", "OVC")) and ft is not None:
            return int(ft)
        return None

    def _has_ceiling(g: TafGroup) -> bool:
        return _ceiling_ft(g) is not None

    def _is_non_ceiling_sky(g: TafGroup) -> bool:
        # True if there is no BKN/OVC/VV ceiling token. (FEW/SCT/SKC/NSC only)
        toks = (g.sky or "").split()
        for t in toks:
            t = t.strip().upper()
            if t.startswith("BKN") or t.startswith("OVC") or t.startswith("VV"):
                return False
        return True

    def _sky_more_restrictive(a: TafGroup, b: TafGroup) -> str:
        # If either has a ceiling, choose lower ceiling; if equal, prefer OVC over BKN.
        tok_a, ft_a, rank_a = _extract_ceiling_token_and_ft(a.sky or "")
        tok_b, ft_b, rank_b = _extract_ceiling_token_and_ft(b.sky or "")
        # ranks: OVC=0, BKN=1, others=2 (lower is more restrictive)
        if ft_a is None and ft_b is None:
            # No ceiling in either: prefer higher cover, then lower base.
            # Cover rank: SKC/NSC best, FEW, SCT, then (shouldn't include BKN/OVC here)
            def cover_rank(tok: str) -> int:
                t = tok.strip().upper()
                if t in {"SKC", "NSC"}:
                    return 0
                if t.startswith("FEW"):
                    return 1
                if t.startswith("SCT"):
                    return 2
                if t.startswith("BKN"):
                    return 3
                if t.startswith("OVC"):
                    return 4
                return 0

            toks_a = (a.sky or "").split()
            toks_b = (b.sky or "").split()
            # pick the worst token in each
            def worst_token(toks):
                if not toks:
                    return ("SKC", 999999)
                best = None
                best_key = None
                for t in toks:
                    base = 999999
                    m = re.match(r'^[A-Z]{3}(\d{3})', t.strip().upper())
                    if m:
                        base = int(m.group(1)) * 100
                    key = (cover_rank(t), -base)  # higher cover_rank worse; lower base worse but we invert for max()
                    if best_key is None or key > best_key:
                        best_key = key
                        best = t
                return best, best_key[1] if best_key else 999999

            wa, _ = worst_token(toks_a)
            wb, _ = worst_token(toks_b)
            # Choose worse cover; if tie, choose the one with lower base.
            ra = cover_rank(wa)
            rb = cover_rank(wb)
            if ra > rb:
                return a.sky
            if rb > ra:
                return b.sky
            # same cover: pick lower base (more restrictive)
            def base_ft(tok: str) -> int:
                m = re.match(r'^[A-Z]{3}(\d{3})', tok.strip().upper())
                return int(m.group(1)) * 100 if m else 999999
            return a.sky if base_ft(wa) <= base_ft(wb) else b.sky

        # If only one has a ceiling, keep that one (more restrictive)
        if ft_a is None and ft_b is not None:
            return b.sky
        if ft_b is None and ft_a is not None:
            return a.sky

        # Both have ceilings:
        assert ft_a is not None and ft_b is not None
        if ft_a < ft_b:
            return a.sky
        if ft_b < ft_a:
            return b.sky
        # Same height: prefer more restrictive coverage (OVC over BKN)
        if rank_a < rank_b:
            return a.sky
        if rank_b < rank_a:
            return b.sky
        # otherwise keep left
        return a.sky

    def _vis_cat_rank(g: TafGroup) -> int:
        return _vis_category_rank(g)

    out: List[TafGroup] = []
    i = 0
    while i < len(groups):
        g = groups[i]
        if not out:
            out.append(g)
            i += 1
            continue

        prev = out[-1]

        # Only merge FM<-FM (or INIT->FM) blocks? We'll allow INIT/FM on the left and FM on the right.
        if (prev.kind in {"INIT", "FM"}) and (g.kind == "FM"):
            # Allow merging even with precip ONLY when wx strings match exactly and are safe to merge.
            if _wx_ok_to_merge_when_equal(prev.wx or "") and _wx_ok_to_merge_when_equal(g.wx or ""):
                # Wx must match exactly (keeps BR/FG starts/stops)
                if (prev.wx or "").strip() == (g.wx or "").strip():
                    # Vis category must match
                    if _vis_cat_rank(prev) == _vis_cat_rank(g):
                        # Winds must be similar
                        if wind_tokens_similar(prev.wind, g.wind, dir_tol_deg=dir_tol_deg, spd_tol_kt=spd_tol_kt, gust_tol_kt=gust_tol_kt):
                            # Sky similarity:
                            c_prev = _ceiling_ft(prev)
                            c_g = _ceiling_ft(g)

                            sky_ok = False
                            if c_prev is None and c_g is None:
                                # no ceilings in either (SCT/FEW/SKC only) => merge
                                sky_ok = True
                            elif (c_prev is not None) and (c_g is not None):
                                if abs(int(c_g) - int(c_prev)) <= int(ceil_tol_ft):
                                    sky_ok = True

                            if sky_ok:
                                # Merge g into prev (keep prev start/kind)
                                prev.vis = _lower_vis_token(prev.vis, g.vis)
                                prev.sky = _sky_more_restrictive(prev, g)
                                # keep prev.wind as-is (don't backdate)
                                # prev.wx unchanged (already equal)
                                i += 1
                                continue

        out.append(g)
        i += 1

    return out




# ----------------------------
# Orchestration
# ----------------------------

def run_for_icao(icao: str, api_key: str, hours: int, cache_path: str, taf_start_time: Optional[dt.datetime] = None) -> str:
    lat, lon, name, iso_country = icao_to_airport_info(icao, cache_path)

    imperial = is_imperial_station(icao, iso_country)
    api_units = "imperial" if imperial else "metric"
    output_style = "US" if imperial else "METRIC"

    issue = taf_start_time if taf_start_time is not None else utcnow()
    valid_from = round_to_hour(issue)
    valid_to = valid_from + dt.timedelta(hours=hours)

    pts5 = ring_points(lat, lon, RADIUS_KM_5)
    pts10 = ring_points(lat, lon, RADIUS_KM_10)
    pts25 = ring_points(lat, lon, RADIUS_KM_25)

    series5 = fetch_timelines_for_points(api_key, pts5, valid_from, valid_to, "1h", api_units)
    series10 = fetch_timelines_for_points(api_key, pts10, valid_from, valid_to, "1h", api_units)
    series25 = fetch_timelines_for_points(api_key, pts25, valid_from, valid_to, "1h", api_units)

    n = min(min(len(s) for s in series5), min(len(s) for s in series10), min(len(s) for s in series25))
    if n == 0:
        raise RuntimeError("No timeline intervals returned (check API key/plan/fields).")

    aggregated: List[Dict[str, Any]] = []
    for i in range(n):
        aggregated.append(aggregate_interval([s[i] for s in series5], [s[i] for s in series10], [s[i] for s in series25]))

    steps_raw = to_steps_from_aggregated(aggregated, api_units=api_units, output_style=output_style, station_lat=lat)
    # Do not start an FM group at the final valid hour; exclude the step exactly at valid_to.
    steps_raw = [s for s in steps_raw if s.t < valid_to]
    steps = enforce_min_2hr_rules([replace(s) for s in steps_raw])
    steps_by_time = {s.t: s for s in steps}


    groups = build_groups(steps)

    groups = merge_adjacent_same_category_groups(groups, steps_by_time)

    groups = smooth_precip_blocks_to_prevailing_plus_tempo(groups, steps_by_time, valid_to)

    groups = apply_tempo_omissions(groups, steps_by_time)

    groups = prune_transient_and_trend_fm_groups(groups, steps_by_time, valid_to)

    groups = compress_consecutive_vfr_fm_groups(groups, steps_by_time)

    groups = collapse_identical_fm_wind_vis_sky(groups)

    groups = fix_zero_ceiling_layers(groups, placeholder='SCT015')

    groups = remove_1hr_wx_blip_fm_groups(groups, valid_to)

    groups = smooth_init_first_fm_promote_if_one_cat(groups)

    groups = smooth_3hr_precip_same_intensity_blip(groups, valid_to)

    groups = remove_1hr_dry_blips_between_precip(groups, valid_to)

    groups = remove_1hr_dry_improvement_blips(groups, valid_to)

    groups = merge_adjacent_dry_bkn_ovc_same_height(groups, valid_to)

    groups = merge_adjacent_same_category_groups(groups, steps_by_time)

    groups = merge_close_precip_groups(groups)

    groups = final_merge_similar_fm_groups(groups, valid_to)

    taf = format_taf(icao, issue, valid_from, valid_to, groups)

    banner = (
        f"# {icao} — {name} ({lat:.4f},{lon:.4f}) country={iso_country} "
        f"api_units={api_units} output={output_style} precip_radius={RADIUS_MILES_5}mi thunder_radius={RADIUS_MILES_25}mi"
    )
    taf = taf.rstrip("\n") + "\n  RMK TIO/"
    return banner + "\n" + taf



# ----------------------------
# BlueMet entry point
# ----------------------------

REQUESTS_PER_TAF = 27   # 9 points x 3 rings, one Timelines call each


def generate(icao: str, hours: int = 24, start: Optional[dt.datetime] = None,
             cache_root: Optional[str] = None) -> Dict[str, Any]:
    """Build one TAF. Returns {"icao", "taf", "banner", "issued",
    "valid_from", "valid_to"}; raises on a bad station, a missing key
    or a tomorrow.io error. The 27 requests are charged to the site's
    tomorrow.io budget first (core.tio_map._charge) so a run of TAFs
    cannot starve the map warmer."""
    from core import tio_map as _TIO
    key = _TIO.api_key()
    if os.environ.get("BLUEMET_TAF_FAKE"):
        # layout testing without a key or a charge: synthetic timelines
        global fetch_timelines_one_location
        fetch_timelines_one_location = _fake_timelines
        key = "fake"
    elif not key:
        raise RuntimeError("tomorrow.io data is disabled on this site (TIO_ENABLED is not on)"
                           if not getattr(_TIO, "TIO_ENABLED", True)
                           else "tomorrow.io key is not configured on this instance")
    root = cache_root or os.environ.get("BLUEMET_CACHE", "/tmp/wx_compare_cache")
    os.makedirs(root, exist_ok=True)
    cache_path = os.path.join(root, "airports.csv")
    static = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
    try:
        if key != "fake":
            _TIO._charge(static, REQUESTS_PER_TAF, 1, f"taf {icao}")
    except Exception as exc:
        raise RuntimeError(f"tomorrow.io budget: {exc}") from exc
    text = run_for_icao(icao.strip().upper(), key, max(6, min(30, int(hours))), cache_path, start)
    banner, _, taf = text.partition("\n")
    issue = start if start is not None else utcnow()
    vf = round_to_hour(issue)
    return {"icao": icao.strip().upper(), "taf": taf, "banner": banner.lstrip("# "),
            "issued": issue, "valid_from": vf, "valid_to": vf + dt.timedelta(hours=int(hours))}


def _fake_timelines(api_key, lat, lon, start, end, timestep, units):
    """BLUEMET_TAF_FAKE=1: a plausible 24 h (fog + showers + a wind
    shift) so the page can be laid out without spending requests."""
    out, t = [], start
    while t <= end:
        h = (t - start).total_seconds() / 3600
        imp = units == "imperial"
        v = {"windSpeed": 12 + 6 * (h > 8), "windDirection": 220 + 40 * (h > 8),
             "windGust": 24 if 8 < h < 14 else 0,
             "visibility": (2 if imp else 3.2) if 3 <= h <= 6 else (10 if imp else 16),
             "cloudCover": 90 if 2 <= h <= 9 else 20,
             "cloudBase": (800 if imp else 0.25) if 2 <= h <= 9 else (4000 if imp else 1.2),
             "cloudCeiling": (800 if imp else 0.25) if 2 <= h <= 9 else None,
             "precipitationType": 1 if 3 <= h <= 6 else 0,
             "precipitationIntensity": 0.05 if 3 <= h <= 6 else 0,
             "weatherCode": 8000 if 4 <= h <= 5 else 4000 if 3 <= h <= 6 else 1000,
             "lightningFlashRateDensity": 0}
        out.append({"startTime": t.isoformat().replace("+00:00", "Z"), "values": v})
        t += dt.timedelta(hours=1)
    return out
