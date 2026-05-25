"""Abstract base class for model data sources.

To add a new model (NBM, RRFS, NAM, GFS, ...):
  1. Create models/<model>.py
  2. Subclass ModelSource
  3. Implement available_cycles(), fetch(), and parse()
  4. Register it in models/__init__.py

The pipeline (notebook) never needs to know the internals — it just calls
.get_forecast() on each registered source and concatenates the results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from core.schema import empty_df


class ModelSource(ABC):
    """One model's worth of forecast data.

    Subclasses set `name` (a stable string used as the 'model' column value)
    and implement the three abstract methods.
    """

    name: str = "OVERRIDE_ME"

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------
    @abstractmethod
    def available_cycles(self, date: datetime) -> list[datetime]:
        """List of cycle datetimes (UTC, tz-aware) this model runs on `date`."""
        ...

    @abstractmethod
    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        """Download (or load from cache) the file(s) for one cycle.

        Returns a Path to the local file, or None on failure. May download
        only the subset needed for the given stations (e.g. HRRR byte-range).
        """
        ...

    @abstractmethod
    def parse(
        self,
        path: Path,
        stations: Iterable[str],
        cycle: datetime,
    ) -> pd.DataFrame:
        """Parse the fetched file into the canonical schema (see core/schema.py)."""
        ...

    # ------------------------------------------------------------------
    # Convenience: one call to do the whole thing
    # ------------------------------------------------------------------
    def get_forecast(
        self,
        cycle: datetime,
        stations: Iterable[str],
    ) -> pd.DataFrame:
        """Fetch + parse for a single cycle. Returns empty df on failure."""
        # Normalize cycle to tz-aware UTC.
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=timezone.utc)
        path = self.fetch(cycle, stations)
        if path is None:
            return empty_df()
        return self.parse(path, stations, cycle)
