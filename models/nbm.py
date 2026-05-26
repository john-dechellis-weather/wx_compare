"""National Blend of Models (NBM) text bulletins.

We fetch and merge two NBM products:
  - NBH (hourly, forecast hours 1-25)
  - NBS (3-hourly, forecast hours 6-72)

The merge strategy is "NBH for hours 1-25, NBS for hours >= 26". This gives
hourly resolution near-term and extends out to 3 days, all reported as a single
'NBM' line on the comparison plot.

URL pattern (per cycle CC and date YYYYMMDD):
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/
    blend.YYYYMMDD/CC/text/blend_nbhtx.tCCz
    blend.YYYYMMDD/CC/text/blend_nbstx.tCCz

Bulletin format (NBM v4.x, current as of mid-2026):
  - VIS row: visibility in 1/10ths of statute miles (100 = 10.0 sm)
  - CIG row: ceiling in hundreds of feet (25 = 2500 ft); -88 = unlimited
  - Older v3.x files used 888 as the unlimited sentinel; we accept both.
  - Field width is 3 chars, with NO padding between adjacent fields (a row of
    25 hourly values is exactly 75 characters of data after the row label).
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


NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"

# NBM runs every hour, but we only consider those for cycle-selection.
NBM_CYCLE_HOURS = tuple(range(24))

# Sentinel values meaning "unlimited" (>10 sm vis, or no ceiling layer)
UNLIMITED_SENTINELS = {888, -88, -888}


class Nbm(ModelSource):
    """NBM — hourly (NBH) + short-range (NBS) blended into one logical model."""

    name = "NBM"

    # Where NBH ends, NBS takes over. NBH typically covers f+1 .. f+25;
    # NBS starts at f+6. We pick 26 as the handoff so NBH owns the full hourly
    # range and there's no overlap ambiguity.
    NBH_NBS_HANDOFF = 26

    def __init__(self, cache_dir: Path):
        super().__init__(cache_dir)

    # --- cycle discovery ---------------------------------------------------
    def available_cycles(self, date: datetime) -> list[datetime]:
        d = date.astimezone(timezone.utc) if date.tzinfo else date.replace(tzinfo=timezone.utc)
        return [d.replace(hour=h, minute=0, second=0, microsecond=0)
                for h in NBM_CYCLE_HOURS]

    # --- URL helpers --------------------------------------------------------
    def _url(self, cycle: datetime, kind: str) -> str:
        """kind is 'nbh' or 'nbs'."""
        ymd = cycle.strftime("%Y%m%d")
        cc = f"{cycle.hour:02d}"
        filename = f"blend_{kind}tx.t{cc}z"
        return f"{NOMADS_BASE}/blend.{ymd}/{cc}/text/{filename}"

    def _cache_path(self, cycle: datetime, kind: str) -> Path:
        return self.cache_dir / (
            f"blend_{kind}tx.{cycle:%Y%m%d.%H}z.txt"
        )

    # --- cycle completeness probe ------------------------------------------
    def is_cycle_complete(self, cycle: datetime) -> bool:
        """Both NBH and NBS must exist for the cycle to count as complete."""
        for kind in ("nbh", "nbs"):
            try:
                r = requests.head(self._url(cycle, kind), timeout=15, allow_redirects=True)
                if r.status_code != 200:
                    return False
            except requests.RequestException:
                return False
        return True

    # --- fetch -------------------------------------------------------------
    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        """Fetch both NBH and NBS into the cache. Returns the cache directory
        rather than a single file (this matches how HRRR handles its per-hour
        files; parse() iterates).
        """
        ok_any = False
        for kind in ("nbh", "nbs"):
            cache_path = self._cache_path(cycle, kind)
            if cache_path.exists() and cache_path.stat().st_size > 0:
                ok_any = True
                continue
            url = self._url(cycle, kind)
            try:
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                cache_path.write_text(r.text)
                ok_any = True
            except requests.RequestException as e:
                print(f"[{self.name}] fetch failed: {url} — {e}")
        return self.cache_dir if ok_any else None

    # --- parse -------------------------------------------------------------
    def parse(
        self,
        path: Path,
        stations: Iterable[str],
        cycle: datetime,
    ) -> pd.DataFrame:
        """Parse NBH and NBS for the requested stations, merge at the handoff."""
        station_set = {s.upper() for s in stations}

        nbh_path = self._cache_path(cycle, "nbh")
        nbs_path = self._cache_path(cycle, "nbs")

        all_records: list[ForecastRecord] = []
        for kind, p in [("NBH", nbh_path), ("NBS", nbs_path)]:
            if not p.exists():
                continue
            try:
                raw = p.read_text()
                # NBM bulletins have a leading space on every line; strip them
                # before pattern-matching against headers and row labels.
                text = "\n".join(
                    line[1:] if line.startswith(" ") else line
                    for line in raw.splitlines()
                )
            except Exception as e:
                print(f"[{self.name}] cannot read {p}: {e}")
                continue
            for block in _split_station_blocks(text, kind):
                station_id = _extract_station_id(block, kind)
                if station_id is None or station_id not in station_set:
                    continue
                try:
                    recs = _parse_block(block, kind, station_id, cycle, p.name)
                    all_records.extend(recs)
                except Exception as e:
                    print(f"[{self.name}] {kind} parse error for {station_id}: {e}")

        if not all_records:
            return empty_df()

        # Merge: per (station, valid_time), prefer NBH if forecast_hour < handoff,
        # else NBS. Since NBH and NBS overlap at hours 6-24 (with 3-hour stride for
        # NBS), the dedup naturally drops the NBS overlap.
        df = records_to_df(all_records)
        df = _merge_nbh_nbs(df, handoff_fhour=self.NBH_NBS_HANDOFF)
        # All rows say model='NBM' regardless of source bulletin.
        df["model"] = self.name
        return df


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Header looks like: "KJFK   NBM V4.2 NBH GUIDANCE   5/25/2026  1200 UTC"
# Older v3.x: "KJFK   NBM NBH GUIDANCE   ..."
_HEADER_RE = re.compile(
    r"^([A-Z0-9]{3,4})\s+NBM(?:\s+V\d+\.\d+)?\s+(NBH|NBS)\s+GUIDANCE\s+"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{4})\s+UTC",
    re.MULTILINE,
)


def _split_station_blocks(text: str, kind: str) -> list[str]:
    """A block is the chunk of lines starting at a station header and ending
    before the next station header. We split by detecting consecutive blank
    lines between blocks.
    """
    blocks = re.split(r"\n\s*\n", text)
    return [b for b in blocks if f"{kind} GUIDANCE" in b]


def _extract_station_id(block: str, kind: str) -> Optional[str]:
    m = _HEADER_RE.search(block)
    if not m:
        return None
    if m.group(2) != kind:
        return None
    return m.group(1)


def _parse_header_datetime(block: str) -> Optional[datetime]:
    m = _HEADER_RE.search(block)
    if not m:
        return None
    _, _, month, day, year, hhmm = m.groups()
    return datetime(int(year), int(month), int(day),
                    int(hhmm[:2]), tzinfo=timezone.utc)


# Row label is exactly 3 chars (e.g., "UTC", "VIS", "CIG"), then a single space,
# then the data fields. Each data field is exactly 3 chars, no separators.
_ROW_LABEL_WIDTH = 3
_DATA_START = 4   # label (3) + space (1)
_FIELD_WIDTH = 3


def _extract_row(block: str, label: str) -> Optional[str]:
    """Return the full text of a row starting with the given 3-char label."""
    for line in block.splitlines():
        # Strip any leading space the bulletin may have (some products do, some don't).
        stripped = line[1:] if line.startswith(" ") else line
        if stripped.startswith(f"{label} ") or stripped.startswith(label) and len(stripped) >= _DATA_START:
            return stripped
    return None


def _slice_fields(line: str, n_expected: int) -> list[str]:
    """Slice line starting at _DATA_START into n_expected 3-char fields."""
    fields = []
    pos = _DATA_START
    for _ in range(n_expected):
        cell = line[pos:pos + _FIELD_WIDTH] if pos < len(line) else ""
        fields.append(cell.strip())
        pos += _FIELD_WIDTH
    return fields


def _parse_int_field(s: str) -> Optional[int]:
    """Parse a 3-char field that may be blank, a number, or a sentinel."""
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_block(
    block: str,
    kind: str,
    station_id: str,
    cycle: datetime,
    source_file: str,
) -> list[ForecastRecord]:
    """Parse one station's NBH or NBS block."""
    header_dt = _parse_header_datetime(block) or cycle

    utc_row = _extract_row(block, "UTC")
    if utc_row is None:
        return []

    # Determine number of forecast hours by counting non-blank entries in UTC row.
    # NBH typically has 25, NBS typically has 23.
    raw_hours = _slice_fields(utc_row, n_expected=30)
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

    # Walk forward, bumping day whenever hour wraps. Seed with cycle hour so
    # the first column can correctly cross midnight (same fix as MAV parser).
    valid_times: list[datetime] = []
    current = header_dt.replace(minute=0, second=0, microsecond=0)
    prev_hr = header_dt.hour
    for hr in hours:
        if hr <= prev_hr:
            current = current + timedelta(days=1)
        current = current.replace(hour=hr)
        valid_times.append(current)
        prev_hr = hr

    # Pull VIS and CIG rows
    vis_row = _extract_row(block, "VIS")
    cig_row = _extract_row(block, "CIG")
    vis_fields = _slice_fields(vis_row, n_hours) if vis_row else [""] * n_hours
    cig_fields = _slice_fields(cig_row, n_hours) if cig_row else [""] * n_hours

    records: list[ForecastRecord] = []
    for i, vt in enumerate(valid_times):
        fhour = int(round((vt - header_dt).total_seconds() / 3600))

        # VIS: 1/10ths of miles. So 100 -> 10.0 sm, 5 -> 0.5 sm.
        vis_raw = _parse_int_field(vis_fields[i]) if i < len(vis_fields) else None
        vsby_sm: Optional[float] = None
        if vis_raw is not None and vis_raw not in UNLIMITED_SENTINELS:
            vsby_sm = vis_raw / 10.0
        elif vis_raw in UNLIMITED_SENTINELS:
            vsby_sm = 10.0  # clamp to top of vis y-axis, same convention as MOS cat 7

        # CIG: 100s of feet. So 25 -> 2500 ft. -88/888 -> unlimited.
        cig_raw = _parse_int_field(cig_fields[i]) if i < len(cig_fields) else None
        ceiling_ft: Optional[float] = None
        ceiling_unlimited = False
        if cig_raw is not None:
            if cig_raw in UNLIMITED_SENTINELS:
                ceiling_unlimited = True
            elif cig_raw >= 0:
                ceiling_ft = float(cig_raw) * 100.0

        records.append(ForecastRecord(
            station_id=station_id,
            model=f"NBM_{kind}",   # tag with sub-product; merger relabels to NBM
            cycle=header_dt,
            valid_time=vt,
            forecast_hour=fhour,
            vsby_sm=vsby_sm,
            vsby_category=units.vsby_sm_to_category(vsby_sm),
            ceiling_ft=ceiling_ft,
            ceiling_category=units.ceiling_ft_to_category(
                ceiling_ft, unlimited=ceiling_unlimited
            ),
            ceiling_unlimited=ceiling_unlimited,
            source_file=source_file,
        ))
    return records


def _merge_nbh_nbs(df: pd.DataFrame, handoff_fhour: int) -> pd.DataFrame:
    """Keep NBH rows where forecast_hour < handoff_fhour, NBS where >=.

    NBH and NBS overlap at hours 6-25 (3-hour stride for NBS). With handoff=26,
    NBH owns everything 1-25 and NBS picks up at 27, 30, 33, ...
    """
    nbh = df[(df["model"] == "NBM_NBH") & (df["forecast_hour"] < handoff_fhour)]
    nbs = df[(df["model"] == "NBM_NBS") & (df["forecast_hour"] >= handoff_fhour)]
    merged = pd.concat([nbh, nbs], ignore_index=True)
    return merged.sort_values(["station_id", "valid_time"]).reset_index(drop=True)
