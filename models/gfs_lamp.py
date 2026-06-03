"""GFS LAMP (Localized Aviation MOS Program) — LAV text bulletin.

LAMP is the hourly statistical sibling of MAV. Same categorical VIS (1-7) and
CIG (1-8) codes, same fixed-width layout, but at 1-hour resolution out to
38 hours instead of 3-hour out to 72.

URL pattern (per cycle CC and date YYYYMMDD):
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/lmp/prod/
    lmp.YYYYMMDD/lmp.tCC30z.simpbull.f001-f038.txt

Key quirks:
  - LAMP runs at HH:30 past every hour. The filename has 'tCC30z', not 'tCCz'.
  - When find_latest_complete probes us with cycle=YYYY-MM-DD HH:00, we look
    for the LAMP file associated with that hour (i.e. 'tCC30z'), but tag the
    parsed records with cycle = HH:30 so forecast_hour math comes out right.
  - That HH:30 cycle propagates into the DataFrame. On the plot, LAMP points
    will appear 30 minutes shifted from MOS/HRRR/NBM points — which is honest:
    LAMP genuinely forecasts the half-hour-offset valid times.

Format: leading-space prefix on every line (same as MAV); fixed-width 3-char
fields after a 4-char row label. VIS/CIG decoded via the shared
core.units.vis_category_to_sm / ceiling_category_to_ft helpers.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

from core.schema import ForecastRecord, records_to_df, empty_df
from core import units
from .base import ModelSource


NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/lmp/prod"

# LAMP runs every hour. The cycle-detection probes pass hour-boundary
# datetimes (HH:00) and we translate internally to HH:30.
LAMP_CYCLE_HOURS = tuple(range(24))


class GfsLamp(ModelSource):
    """GFS LAMP (LAV) — hourly categorical VIS/CIG out to 38 hr."""

    name = "GFS_LAMP"

    # The number of forecast hours we extract. Bulletin covers f+1 .. f+38
    # for VIS/CIG; older params end at f+25. We only care about VIS/CIG.
    MAX_FHOUR = 38

    def __init__(self, cache_dir: Path):
        super().__init__(cache_dir)

    # --- cycle discovery ---------------------------------------------------
    def available_cycles(self, date: datetime) -> list[datetime]:
        d = date.astimezone(timezone.utc) if date.tzinfo else date.replace(tzinfo=timezone.utc)
        return [d.replace(hour=h, minute=0, second=0, microsecond=0)
                for h in LAMP_CYCLE_HOURS]

    # --- URL helpers --------------------------------------------------------
    def _cycle_30(self, cycle: datetime) -> datetime:
        """Caller passes the hour-boundary cycle (HH:00). LAMP's real cycle
        time is HH:30 — that's what gets stored on records."""
        return cycle.replace(minute=30)

    def _url(self, cycle: datetime) -> str:
        """Build the LAMP URL. cycle is the HH:00 datetime; URL uses HH30z.

        Filename quirk: the full LAV bulletin (25-hour hourly forecast) is only
        produced at HH:30. The HH:00, :15, :45 sub-hourly runs publish a
        different file with only ~3 hours of forecast.
        """
        ymd = cycle.strftime("%Y%m%d")
        cc = f"{cycle.hour:02d}"
        return f"{NOMADS_BASE}/lmp.{ymd}/lmp.t{cc}30z.lavtxt.ascii"

    def _cache_path(self, cycle: datetime) -> Path:
        return self.cache_dir / f"lmp.{cycle:%Y%m%d.%H}30z.lavtxt.ascii"

    # --- cycle completeness probe ------------------------------------------
    def is_cycle_complete(self, cycle: datetime) -> bool:
        """One HEAD on the bulletin URL is enough — the file is all-or-nothing."""
        try:
            r = requests.head(self._url(cycle), timeout=15, allow_redirects=True)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # --- fetch -------------------------------------------------------------
    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        cache_path = self._cache_path(cycle)
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path
        url = self._url(cycle)
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[{self.name}] fetch failed: {url} — {e}")
            return None
        cache_path.write_text(r.text)
        return cache_path

    # --- parse -------------------------------------------------------------
    def parse(
        self,
        path: Path,
        stations: Iterable[str],
        cycle: datetime,
    ) -> pd.DataFrame:
        raw = path.read_text()
        # Strip leading spaces (same pattern as MAV and NBM)
        text = "\n".join(
            line[1:] if line.startswith(" ") else line
            for line in raw.splitlines()
        )
        station_set = {s.upper() for s in stations}

        # Caller passed HH:00, but LAMP's actual cycle time is HH:30.
        lamp_cycle = self._cycle_30(cycle)

        records: list[ForecastRecord] = []
        for block in _split_station_blocks(text):
            station_id = _extract_station_id(block)
            if station_id is None or station_id not in station_set:
                continue
            try:
                records.extend(_parse_station_block(
                    block, station_id, lamp_cycle, path.name
                ))
            except Exception as e:
                print(f"[{self.name}] parse error for {station_id}: {e}")
        if not records:
            return empty_df()
        return records_to_df(records)


# ---------------------------------------------------------------------------
# Parsing helpers — closely mirror models/gfs_mos.py since the format is similar
# ---------------------------------------------------------------------------

# Header: "KJFK   GFS LAMP GUIDANCE   5/27/2026  1230 UTC"
_HEADER_RE = re.compile(
    r"^([A-Z0-9]{3,4})\s+GFS\s+LAMP\s+GUIDANCE\s+"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{4})\s+UTC",
    re.MULTILINE,
)


def _split_station_blocks(text: str) -> list[str]:
    blocks = re.split(r"\n\s*\n", text)
    return [b for b in blocks if "LAMP GUIDANCE" in b]


def _extract_station_id(block: str) -> Optional[str]:
    m = _HEADER_RE.search(block)
    return m.group(1) if m else None


def _parse_header_datetime(block: str) -> Optional[datetime]:
    """Header gives the cycle's date/time: MM/DD/YYYY HHMM UTC.
    LAMP cycles are at HH:30 so the HHMM in the header is e.g. '1230'.
    """
    m = _HEADER_RE.search(block)
    if not m:
        return None
    _, month, day, year, hhmm = m.groups()
    hour = int(hhmm[:2])
    minute = int(hhmm[2:])
    return datetime(int(year), int(month), int(day), hour, minute, tzinfo=timezone.utc)


# Row label is 3 chars, then a space, then 3-char fixed-width fields.
# Same layout as MAV — same constants.
_FIELD_WIDTH = 3
_DATA_START = 4


def _slice_fields(line: str, n_expected: int) -> list[str]:
    fields = []
    pos = _DATA_START
    for _ in range(n_expected):
        cell = line[pos:pos + _FIELD_WIDTH] if pos < len(line) else ""
        fields.append(cell.strip())
        pos += _FIELD_WIDTH
    return fields


def _extract_row(block: str, label: str) -> Optional[str]:
    """Find the row whose first non-space token is `label`."""
    for line in block.splitlines():
        if line.startswith(f"{label} ") or line.startswith(label + "  "):
            return line
    return None


def _parse_station_block(
    block: str,
    station_id: str,
    fallback_cycle: datetime,
    source_file: str,
) -> list[ForecastRecord]:
    """Parse one station's LAMP block. fallback_cycle is used if the header
    is unparseable; normally header_dt is preferred."""
    header_dt = _parse_header_datetime(block) or fallback_cycle

    utc_row = _extract_row(block, "UTC")
    if utc_row is None:
        return []

    # LAMP CIG/VIS rows go to f+38 — that's at most ~38 hourly columns. Slice
    # generously and discard blanks.
    raw_hours = _slice_fields(utc_row, n_expected=40)
    hours: list[int] = []
    for r in raw_hours:
        if not r:
            continue
        try:
            hours.append(int(r))
        except ValueError:
            break
    n_hours = len(hours)
    if n_hours == 0:
        return []

    # Walk the hours, bumping day when the value wraps. Seed with cycle hour
    # so we correctly cross midnight (same logic as MAV / NBM parsers).
    # IMPORTANT: LAMP's UTC row gives top-of-hour valid times (HH:00 sharp).
    # The cycle is HH:30 but the forecast is *for* HH:00. So we explicitly set
    # minute=0 on the valid_time, not whatever the cycle minute was.
    valid_times: list[datetime] = []
    current = header_dt.replace(minute=0, second=0, microsecond=0)
    prev_hr = header_dt.hour
    for hr in hours:
        if hr <= prev_hr:
            current = current + timedelta(days=1)
        current = current.replace(hour=hr, minute=0)
        valid_times.append(current)
        prev_hr = hr

    vis_row = _extract_row(block, "VIS")
    cig_row = _extract_row(block, "CIG")
    wdr_row = _extract_row(block, "WDR")
    wsp_row = _extract_row(block, "WSP")
    vis_fields = _slice_fields(vis_row, n_hours) if vis_row else [""] * n_hours
    cig_fields = _slice_fields(cig_row, n_hours) if cig_row else [""] * n_hours
    wdr_fields = _slice_fields(wdr_row, n_hours) if wdr_row else [""] * n_hours
    wsp_fields = _slice_fields(wsp_row, n_hours) if wsp_row else [""] * n_hours

    records: list[ForecastRecord] = []
    for i, vt in enumerate(valid_times):
        # LAMP valid times are HH:00 sharp; cycle is HH:30. The first
        # forecast valid time (13:00 when cycle is 12:30) is f+1 by LAMP
        # convention. Compute as ceiling of hours: a 30-min offset rounds up.
        delta = vt - header_dt
        fhour = int((delta.total_seconds() + 1800) / 3600)  # +1800s = +0.5hr

        # Decode VIS category
        vis_code: Optional[int] = None
        if i < len(vis_fields) and vis_fields[i]:
            try:
                vis_code = int(vis_fields[i])
            except ValueError:
                vis_code = None

        # Decode CIG category
        cig_code: Optional[int] = None
        if i < len(cig_fields) and cig_fields[i]:
            try:
                cig_code = int(cig_fields[i])
            except ValueError:
                cig_code = None

        # Decode wind direction (tens of degrees) and speed (knots)
        wdr_deg: Optional[float] = None
        if i < len(wdr_fields) and wdr_fields[i]:
            try:
                wdr_deg = float(int(wdr_fields[i])) * 10.0
            except ValueError:
                pass
        wsp_kt: Optional[float] = None
        if i < len(wsp_fields) and wsp_fields[i]:
            try:
                wsp_kt = float(int(wsp_fields[i]))
            except ValueError:
                pass

        records.append(ForecastRecord(
            station_id=station_id,
            model=GfsLamp.name,
            cycle=header_dt,
            valid_time=vt,
            forecast_hour=fhour,
            vsby_sm=units.vis_category_to_sm(vis_code),
            vsby_category=vis_code,
            ceiling_ft=units.ceiling_category_to_ft(cig_code),
            ceiling_category=cig_code,
            ceiling_unlimited=(cig_code == 8),
            wind_dir_deg=wdr_deg,
            wind_speed_kt=wsp_kt,
            wind_gust_kt=None,  # LAMP doesn't publish gust
            source_file=source_file,
        ))
    return records
