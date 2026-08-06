"""NBE — NBM extended text bulletin (12-hourly, forecast hours ~17-185).

Format differs from NBH/NBS in three ways this parser handles:
  1. Day-group '|' separators packed into data rows -> strip pipes, then
     whitespace-split (fixed-width slicing would misalign).
  2. Trailing CLIMO columns on some rows -> truncate to the FHR count.
  3. An explicit FHR row -> valid times computed directly from it
     (no UTC-walking across many midnights).

NBE does NOT produce VIS or CIG. Records carry wind only
(WDR / WSP / GST); visibility and ceiling fields are left None.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

from models.base import ModelSource
from models.nbm import Nbm
from core import units
from core.schema import ForecastRecord, records_to_df, empty_df

# "KJFK   NBM V5.0 NBE GUIDANCE    8/06/2026  1900 UTC"
_NBE_HEADER_RE = re.compile(
    r"^([A-Z0-9]{3,4})\s+NBM(?:\s+V\d+\.\d+)?\s+NBE\s+GUIDANCE\s+"
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{4})\s+UTC",
    re.MULTILINE,
)


class Nbe(ModelSource):
    """NBM extended-range (NBE) bulletin as a standalone model."""

    name = "NBE"

    def __init__(self, cache_dir: Path):
        super().__init__(cache_dir)
        self._nbm = Nbm(cache_dir=cache_dir)  # reuse URL/cache conventions

    def available_cycles(self, date: datetime) -> list[datetime]:
        return self._nbm.available_cycles(date)

    def is_cycle_complete(self, cycle: datetime) -> bool:
        try:
            r = requests.head(
                self._nbm._url(cycle, "nbe"), timeout=15, allow_redirects=True
            )
            return r.status_code == 200
        except Exception:
            return False

    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        path = self._nbm._cache_path(cycle, "nbe")
        if path.exists():
            return path.parent
        url = self._nbm._url(cycle, "nbe")
        try:
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(r.text)
            return path.parent
        except Exception as e:
            print(f"[{self.name}] fetch failed: {url} — {e}")
            return None

    def parse(
        self,
        path: Path,
        stations: Iterable[str],
        cycle: datetime,
    ) -> pd.DataFrame:
        station_set = {s.upper() for s in stations}
        p = self._nbm._cache_path(cycle, "nbe")
        if not p.exists():
            return empty_df()

        raw = p.read_text()
        text = "\n".join(
            line[1:] if line.startswith(" ") else line
            for line in raw.splitlines()
        )

        all_records: list[ForecastRecord] = []
        for block in re.split(r"\n\s*\n", text):
            if "NBE GUIDANCE" not in block:
                continue
            m = _NBE_HEADER_RE.search(block)
            if not m:
                continue
            station_id = m.group(1)
            if station_id not in station_set:
                continue
            try:
                recs = _parse_nbe_block(block, m, station_id, p.name)
                all_records.extend(recs)
            except Exception as e:
                print(f"[{self.name}] parse error for {station_id}: {e}")

        if not all_records:
            return empty_df()

        df = records_to_df(all_records)
        df["model"] = self.name
        return df


def _row_values(block: str, label: str) -> Optional[list[str]]:
    """Find row by 3-char label; strip pipes; whitespace-split the rest."""
    for line in block.splitlines():
        stripped = line[1:] if line.startswith(" ") else line
        if stripped.startswith(label + " ") or stripped == label:
            data = stripped[len(label):].replace("|", " ")
            return data.split()
    return None


def _to_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _parse_nbe_block(
    block: str, header_match, station_id: str, source_file: str
) -> list[ForecastRecord]:
    month, day, year, hhmm = header_match.groups()[1:]
    header_dt = datetime(
        int(year), int(month), int(day), int(hhmm[:2]), tzinfo=timezone.utc
    )

    fhr_vals = _row_values(block, "FHR")
    if not fhr_vals:
        return []
    fhrs = [h for h in (_to_int(v) for v in fhr_vals) if h is not None]
    n = len(fhrs)
    if n == 0:
        return []

    def take(label: str) -> list[Optional[int]]:
        vals = _row_values(block, label) or []
        out = [_to_int(v) for v in vals[:n]]
        out += [None] * (n - len(out))
        return out

    wdr = take("WDR")
    wsp = take("WSP")
    gst = take("GST")

    records: list[ForecastRecord] = []
    for i, fh in enumerate(fhrs):
        vt = header_dt + timedelta(hours=fh)
        records.append(ForecastRecord(
            station_id=station_id,
            model="NBE",
            cycle=header_dt,
            valid_time=vt,
            forecast_hour=fh,
            vsby_sm=None,
            vsby_category=units.vsby_sm_to_category(None),
            ceiling_ft=None,
            ceiling_category=units.ceiling_ft_to_category(None, unlimited=False),
            ceiling_unlimited=False,
            wind_dir_deg=float(wdr[i]) * 10.0 if wdr[i] is not None else None,
            wind_speed_kt=float(wsp[i]) if wsp[i] is not None else None,
            wind_gust_kt=float(gst[i]) if gst[i] is not None else None,
            source_file=source_file,
        ))
    return records