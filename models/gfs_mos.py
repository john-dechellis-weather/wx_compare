"""GFS MOS (MAV bulletin) source.

URL pattern:
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs_mos/prod/
    gfs_mos.YYYYMMDD/mdl_gfsmav.tCCz

Cycles: 00, 06, 12, 18 UTC.
Format: fixed-width text, one block per station, ~1500 stations per file.

Per the MAV card, VIS uses codes 1-7 and CIG uses codes 1-8 (see core/units.py
for the full decode tables).
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


NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs_mos/prod"
CYCLE_HOURS = (0, 6, 12, 18)

# Map 3-letter month abbreviations in the MAV header to month numbers.
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1
)}


class GfsMos(ModelSource):
    """Short-range GFS MOS (MAV) — categorical VIS and CIG forecasts."""

    name = "GFS_MOS"

    # --- cycle discovery ---------------------------------------------------
    def available_cycles(self, date: datetime) -> list[datetime]:
        d = date.astimezone(timezone.utc) if date.tzinfo else date.replace(tzinfo=timezone.utc)
        return [d.replace(hour=h, minute=0, second=0, microsecond=0) for h in CYCLE_HOURS]

    # --- url helpers --------------------------------------------------------
    def _url(self, cycle: datetime) -> str:
        ymd = cycle.strftime("%Y%m%d")
        return f"{NOMADS_BASE}/gfs_mos.{ymd}/mdl_gfsmav.t{cycle:%H}z"

    def _cache_path(self, cycle: datetime) -> Path:
        return self.cache_dir / f"mdl_gfsmav.{cycle:%Y%m%d.%H}z.txt"

    # --- cycle completeness probe ------------------------------------------
    def is_cycle_complete(self, cycle: datetime) -> bool:
        """The MAV bulletin is one file containing all forecast hours for
        all stations, so 'complete' simply means 'the file exists.'"""
        url = self._url(cycle)
        try:
            r = requests.head(url, timeout=15, allow_redirects=True)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # --- fetch -------------------------------------------------------------
    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        # The MAV bulletin is small (~few MB) and contains all stations, so
        # caching the whole file is fine. Station filtering happens in parse().
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
        text = path.read_text()
        station_set = {s.upper() for s in stations}
        records: list[ForecastRecord] = []
        for block in _split_station_blocks(text):
            station_id = _extract_station_id(block)
            if station_id is None or station_id not in station_set:
                continue
            try:
                records.extend(_parse_station_block(
                    block, station_id, cycle, path.name
                ))
            except Exception as e:
                print(f"[{self.name}] parse error for {station_id}: {e}")
                continue
        if not records:
            return empty_df()
        return records_to_df(records)


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------
def _split_station_blocks(text: str) -> list[str]:
    """A station block starts with the station header line.

    The MAV file separates stations with blank lines. We split on runs of
    blank lines and keep non-empty blocks that contain a header.
    """
    blocks = re.split(r"\n\s*\n", text)
    return [b for b in blocks if "MOS GUIDANCE" in b or "GFS MOS GUIDANCE" in b]


_HEADER_RE = re.compile(
    r"^([A-Z0-9]{3,4})\s+(?:GFS\s+)?MOS\s+GUIDANCE\s+"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{4})\s+UTC",
    re.MULTILINE,
)


def _extract_station_id(block: str) -> Optional[str]:
    m = _HEADER_RE.search(block)
    return m.group(1) if m else None


def _parse_header_datetime(block: str) -> Optional[datetime]:
    """The header gives the cycle's date/time: MM/DD/YYYY HHMM UTC."""
    m = _HEADER_RE.search(block)
    if not m:
        return None
    _, month, day, year, hhmm = m.groups()
    hour = int(hhmm[:2])
    return datetime(int(year), int(month), int(day), hour, tzinfo=timezone.utc)


def _parse_station_block(
    block: str,
    station_id: str,
    cycle: datetime,
    source_file: str,
) -> list[ForecastRecord]:
    """Parse one station's MAV block.

    The MAV block has labeled rows. We need:
      - DT row (date row, e.g. "DT /MAY  25/MAY  26/...")
      - HR row (hours, fixed 3-char fields after the label)
      - CIG row (ceiling category 1-8)
      - VIS row (visibility category 1-7)

    Cycle is taken from the header rather than the caller, so we record the
    actual run time even if the caller passed something slightly off.
    """
    header_dt = _parse_header_datetime(block) or cycle
    lines = block.splitlines()
    rows = {}
    current_dt_row = None
    for line in lines:
        if not line.strip():
            continue
        # Row label is the first token (3 chars typically). Use first 4 chars
        # then strip — robust to "HR " vs "HR  ".
        label = line[:4].strip()
        if label in {"DT", "HR", "CIG", "VIS", "WDR", "WSP"}:
            rows[label] = line
            if label == "DT":
                current_dt_row = line

    if "HR" not in rows:
        return []

    hours = _extract_hr_fields(rows["HR"])
    if not hours:
        return []
    date_for_hour = _build_valid_times(rows.get("DT", ""), hours, header_dt)

    cigs = _extract_category_fields(rows.get("CIG", ""), n=len(hours))
    viss = _extract_category_fields(rows.get("VIS", ""), n=len(hours))
    # WDR in MOS is reported in tens of degrees (e.g. 27 = 270°)
    wdrs = _extract_category_fields(rows.get("WDR", ""), n=len(hours))
    # WSP in MOS is in knots
    wsps = _extract_category_fields(rows.get("WSP", ""), n=len(hours))

    records: list[ForecastRecord] = []
    for i, hr in enumerate(hours):
        valid_time = date_for_hour[i]
        if valid_time is None:
            continue
        fhour = int(round((valid_time - header_dt).total_seconds() / 3600))
        cig_code = cigs[i] if i < len(cigs) else None
        vis_code = viss[i] if i < len(viss) else None
        wdr_code = wdrs[i] if i < len(wdrs) else None
        wsp_code = wsps[i] if i < len(wsps) else None
        records.append(ForecastRecord(
            station_id=station_id,
            model=GfsMos.name,
            cycle=header_dt,
            valid_time=valid_time,
            forecast_hour=fhour,
            vsby_sm=units.vis_category_to_sm(vis_code),
            vsby_category=vis_code,
            ceiling_ft=units.ceiling_category_to_ft(cig_code),
            ceiling_category=cig_code,
            ceiling_unlimited=(cig_code == 8),
            wind_dir_deg=(float(wdr_code) * 10.0) if wdr_code is not None else None,
            wind_speed_kt=float(wsp_code) if wsp_code is not None else None,
            wind_gust_kt=None,  # MOS does not report gust
            source_file=source_file,
        ))
    return records


# The MAV layout is fixed-width: after the 4-char row label there's a leading
# space, then 3-char fields for each forecast hour. We slice rather than split
# on whitespace, because category cells can be blank (missing) and split would
# silently misalign columns.
_FIELD_WIDTH = 3
_DATA_START = 4  # position of first character after the row label


def _slice_fields(line: str, n_expected: int) -> list[str]:
    fields = []
    pos = _DATA_START
    for _ in range(n_expected):
        cell = line[pos:pos + _FIELD_WIDTH] if pos < len(line) else ""
        fields.append(cell.strip())
        pos += _FIELD_WIDTH
    return fields


def _extract_hr_fields(hr_line: str) -> list[int]:
    """Pull integer forecast hours from the HR row."""
    raw = _slice_fields(hr_line, n_expected=30)  # MAV has up to ~21 hourly cols
    hours: list[int] = []
    for r in raw:
        if not r:
            continue
        try:
            hours.append(int(r))
        except ValueError:
            break  # past the data columns
    return hours


def _extract_category_fields(line: str, n: int) -> list[Optional[int]]:
    if not line:
        return [None] * n
    raw = _slice_fields(line, n_expected=n)
    out: list[Optional[int]] = []
    for r in raw:
        if r == "":
            out.append(None)
        else:
            try:
                out.append(int(r))
            except ValueError:
                out.append(None)
    return out


def _build_valid_times(
    dt_line: str,
    hours: list[int],
    cycle: datetime,
) -> list[Optional[datetime]]:
    """Map each HR column to a UTC datetime.

    The DT row labels columns with a date (e.g. "MAY  25") that changes when
    the hour wraps past 00 UTC. Because date labels span multiple columns
    in a non-trivial way, the most robust approach is: start from the cycle
    time, walk forward through the HR sequence, and bump the date whenever
    the hour decreases (wrap from 23 -> 02, etc.).
    """
    if not hours:
        return []
    result: list[Optional[datetime]] = []
    current = cycle.replace(minute=0, second=0, microsecond=0)
    prev_hr: Optional[int] = None
    for hr in hours:
        if prev_hr is not None and hr <= prev_hr:
            current = current + timedelta(days=1)
        current = current.replace(hour=hr)
        result.append(current)
        prev_hr = hr
    return result
