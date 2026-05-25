"""Station metadata resolver.

Given a 4-letter ICAO code like "KORD" or "EGLL", returns lat/lon/elevation.
Uses the OpenFlights airports.dat dataset (public, ~7000 airports worldwide).
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
    icao: str
    lat: float
    lon: float
    elev_ft: float
    name: str = ""


class StationResolver:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._table: Optional[dict] = None

    def resolve(self, icao: str) -> Optional[Station]:
        self._ensure_loaded()
        return self._table.get(icao.upper())

    def resolve_many(self, icaos):
        self._ensure_loaded()
        found, missing = [], []
        for code in icaos:
            stn = self._table.get(code.upper())
            if stn is not None:
                found.append(stn)
            else:
                missing.append(code.upper())
        return found, missing

    def __contains__(self, icao: str) -> bool:
        self._ensure_loaded()
        return icao.upper() in self._table

    def _ensure_loaded(self):
        if self._table is not None:
            return
        cached = self.cache_dir / "airports.dat"
        if not cached.exists() or cached.stat().st_size == 0:
            r = requests.get(OPENFLIGHTS_URL, timeout=60)
            r.raise_for_status()
            cached.write_text(r.text)
        self._table = self._parse(cached)

    def _parse(self, path: Path) -> dict:
        table = {}
        reader = csv.reader(io.StringIO(path.read_text()))
        for row in reader:
            if len(row) < 9:
                continue
            icao = row[5].strip().strip('"')
            if not icao or icao == "\\N" or len(icao) != 4:
                continue
            try:
                lat = float(row[6]); lon = float(row[7]); elev = float(row[8])
            except (ValueError, IndexError):
                continue
            name = row[1].strip().strip('"') if len(row) > 1 else ""
            table[icao.upper()] = Station(
                icao=icao.upper(), lat=lat, lon=lon, elev_ft=elev, name=name
            )
        return table
