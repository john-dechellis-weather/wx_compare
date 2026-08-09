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

# Airport identifier renames. FAA retired KPBI on 2026-07-09 (Palm
# Beach became President Donald J. Trump Intl, KDJT); the OpenFlights
# dataset may lag, so we normalize input AND inject the new code from
# the old entry's coordinates when the dataset lacks it.
ICAO_RENAMES = {"KPBI": "KDJT"}


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
        code = icao.upper()
        code = ICAO_RENAMES.get(code, code)
        return self._table.get(code)

    def resolve_many(self, icaos):
        self._ensure_loaded()
        found, missing = [], []
        for code in icaos:
            code = code.upper()
            code = ICAO_RENAMES.get(code, code)
            stn = self._table.get(code)
            if stn is not None:
                found.append(stn)
            else:
                missing.append(code.upper())
        return found, missing

    def __contains__(self, icao: str) -> bool:
        self._ensure_loaded()
        code = icao.upper()
        return ICAO_RENAMES.get(code, code) in self._table

    def _ensure_loaded(self):
        if self._table is not None:
            return
        cached = self.cache_dir / "airports.dat"
        if not cached.exists() or cached.stat().st_size == 0:
            r = requests.get(OPENFLIGHTS_URL, timeout=60)
            r.raise_for_status()
            cached.write_text(r.text)
        self._table = self._parse(cached)
        # Inject renamed identifiers the dataset doesn't know yet,
        # cloning coordinates from the retired code's entry.
        for old_code, new_code in ICAO_RENAMES.items():
            if new_code not in self._table and old_code in self._table:
                e = self._table[old_code]
                self._table[new_code] = Station(
                    icao=new_code, lat=e.lat, lon=e.lon,
                    elev_ft=e.elev_ft, name=e.name,
                )

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
