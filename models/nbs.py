"""NBS — NBM short-range text bulletin (3-hourly, forecast hours 6-72),
exposed as its OWN model rather than merged into the blended "NBM".

Reuses everything from models.nbm: the URL scheme, the cache files (same
blendnbstx download the Nbm class already fetches — zero extra bandwidth
when both are used in one request), and the block parser.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

from models.base import ModelSource
from models.nbm import (
    Nbm,
    _split_station_blocks,
    _extract_station_id,
    _parse_block,
)
from core.schema import ForecastRecord, records_to_df, empty_df


class Nbs(ModelSource):
    """NBM short-range (NBS) bulletin as a standalone model."""

    name = "NBS"

    def __init__(self, cache_dir: Path):
        super().__init__(cache_dir)
        # Delegate URL/cache-path logic to Nbm so both share downloads.
        self._nbm = Nbm(cache_dir=cache_dir)

    def available_cycles(self, date: datetime) -> list[datetime]:
        return self._nbm.available_cycles(date)

    def is_cycle_complete(self, cycle: datetime) -> bool:
        try:
            r = requests.head(
                self._nbm._url(cycle, "nbs"), timeout=15, allow_redirects=True
            )
            return r.status_code == 200
        except Exception:
            return False

    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        """Download just the NBS bulletin (skips if Nbm already cached it)."""
        path = self._nbm._cache_path(cycle, "nbs")
        if path.exists():
            return path.parent
        url = self._nbm._url(cycle, "nbs")
        try:
            r = requests.get(url, timeout=60)
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
        p = self._nbm._cache_path(cycle, "nbs")
        if not p.exists():
            return empty_df()

        raw = p.read_text()
        text = "\n".join(
            line[1:] if line.startswith(" ") else line
            for line in raw.splitlines()
        )

        all_records: list[ForecastRecord] = []
        for block in _split_station_blocks(text, "NBS"):
            station_id = _extract_station_id(block, "NBS")
            if station_id is None or station_id not in station_set:
                continue
            try:
                recs = _parse_block(block, "NBS", station_id, cycle, p.name)
                all_records.extend(recs)
            except Exception as e:
                print(f"[{self.name}] parse error for {station_id}: {e}")

        if not all_records:
            return empty_df()

        df = records_to_df(all_records)
        df["model"] = self.name  # relabel NBM_NBS -> NBS
        return df