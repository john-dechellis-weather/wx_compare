"""Station metadata resolver.

Given a 4-letter ICAO code like "KORD" or "EGLL", returns lat/lon/elevation.
Uses the OpenFlights airports.dat dataset (public, ~7000 airports worldwide).
Downloads once, caches locally.

Data source:
  https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat

CSV columns (no header):
  0  airport_id
  1  name
  2  city
  3  country
  4  iata        (3-letter, may be empty/\\N)
  5  icao        (4-letter, may be empty/\\N)
  6  latitude    (decimal degrees, + north)
  7  longitude   (decimal degrees, + east)
  8  altitude    (feet)
  9  timezone_offset
  10 dst
  11 timezone_name
  12 type
  13 source
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


OPENFLIGHTS_URL = (
    "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
)


@dataclass(frozen=True)
class Station:
    """Lightweight station record. Matches models.hrrr.Station so existing
    code keeps working — this is the canonical version going forward."""
    icao: str
    lat: float
    lon: float
    elev_ft: float
    name: str = ""


class StationResolver:
    """Lazy-loaded ICAO -> Station lookup. Construct once, query many times."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._table: Optional[dict[str, Station]] = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def resolve(self, icao: str) -> Optional[Station]:
        """Return Station for the given ICAO, or None if unknown."""
        self._ensure_loaded()
        return self._table.get(icao.upper())

    def resolve_many(self, icaos: list[str]) -> tuple[list[Station], list[str]]:
        """Return (found, missing). Missing is the list of ICAOs that didn't resolve."""
        self._ensure_loaded()
        found, missing = [], []
        for code in icaos:
            stn = self._table.get(code.upper())
            (found if stn is not None else missing).append(stn if stn else code)
        # missing was appended with strings; found with Station objects.
        return found, [m for m in missing if isinstance(m, str)]

    def __contains__(self, icao: str) -> bool:
        self._ensure_loaded()
        return icao.upper() in self._table

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _ensure_loaded(self):
        if self._table is not None:
            return
        cached = self.cache_dir / "airports.dat"
        if not cached.exists() or cached.stat().st_size == 0:
            self._download(cached)
        self._table = self._parse(cached)

    def _download(self, dest: Path) -> None:
        r = requests.get(OPENFLIGHTS_URL, timeout=60)
        r.raise_for_status()
        dest.write_text(r.text)

    def _parse(self, path: Path) -> dict[str, Station]:
        table: dict[str, Station] = {}
        text = path.read_text()
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if len(row) < 9:
                continue
            icao = row[5].strip().strip('"')
            if not icao or icao == r"\N" or len(icao) != 4:
                continue
            try:
                lat = float(row[6])
                lon = float(row[7])
                elev = float(row[8])
            except (ValueError, IndexError):
                continue
            name = row[1].strip().strip('"') if len(row) > 1 else ""
            table[icao.upper()] = Station(
                icao=icao.upper(),
                lat=lat,
                lon=lon,
                elev_ft=elev,
                name=name,
            )
        return table
