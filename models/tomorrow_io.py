"""Tomorrow.io weather forecast source.

Tomorrow.io provides a commercial blended forecast (their proprietary
post-processing across multiple inputs). Different in character from the
NOAA models — included on the plot as one extra perspective.

API:
  GET https://api.tomorrow.io/v4/weather/forecast
      ?location=LAT,LON
      &apikey=...
      &units=imperial

Response (relevant parts):
  timelines.hourly[i].time   ISO8601 UTC
  timelines.hourly[i].values.visibility    statute miles (with units=imperial)
  timelines.hourly[i].values.cloudCeiling  kilofeet (or None for unlimited)

Authentication:
  API key is read from the environment variable TOMORROWIO_API_KEY.
  Do NOT hardcode the key — leak risk if committed to GitHub.
  In Colab:
      import os
      from google.colab import userdata
      os.environ["TOMORROWIO_API_KEY"] = userdata.get("TOMORROWIO_API_KEY")
  Or just os.environ["TOMORROWIO_API_KEY"] = "..."  in a cell you won't push.

"Cycle":
  Tomorrow.io has no operational cycle concept — it's continuously updated.
  We tag records with the wall-clock time we made the API request.
  This is honest about the data but means cycle won't align with other
  models' HH:00 cycle times on the plot.

Rate limits (free tier):
  ~25 calls/hour, ~500/day. Each station = one call.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

from core.schema import ForecastRecord, records_to_df, empty_df
from core import units
from core.stations import StationResolver, Station
from .base import ModelSource


API_BASE = "https://api.tomorrow.io/v4/weather/forecast"
ENV_KEY = "TOMORROWIO_API_KEY"


class TomorrowIO(ModelSource):
    """Commercial weather forecast via the Tomorrow.io v4 API."""

    name = "TOMORROW_IO"

    def __init__(self, cache_dir: Path, station_resolver: Optional[StationResolver] = None):
        super().__init__(cache_dir)
        # Need a resolver to convert ICAO -> lat/lon. The notebook usually
        # already has one in compare_icaos; we let it be passed in or
        # lazily create one.
        self._resolver = station_resolver

    def _get_resolver(self) -> StationResolver:
        if self._resolver is None:
            self._resolver = StationResolver(cache_dir=self.cache_dir.parent / "stations")
        return self._resolver

    # --- API key handling --------------------------------------------------
    def _api_key(self) -> Optional[str]:
        key = os.environ.get(ENV_KEY)
        if not key:
            print(f"[{self.name}] Set environment variable {ENV_KEY} to enable this source.")
            return None
        return key

    # --- ModelSource interface ---------------------------------------------
    def available_cycles(self, date: datetime) -> list[datetime]:
        # Tomorrow.io has no cycle. Return a single "cycle" at this moment.
        # In practice the cycle-detect probes won't reach this method.
        return [datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)]

    def is_cycle_complete(self, cycle: datetime) -> bool:
        """Tomorrow.io is always 'complete' — it's a continuous service.
        Return True if we have an API key, False otherwise (so the cycle
        selector knows to skip this source when unauthenticated).
        """
        return self._api_key() is not None

    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        """Fetch raw JSON for each station, cache one file per station per cycle."""
        api_key = self._api_key()
        if api_key is None:
            return None

        resolver = self._get_resolver()
        cycle_dir = self.cache_dir / f"tomorrowio.{cycle:%Y%m%d.%H}z"
        cycle_dir.mkdir(parents=True, exist_ok=True)

        ok_any = False
        for icao in stations:
            station = resolver.resolve(icao)
            if station is None:
                print(f"[{self.name}] {icao}: unknown ICAO, skipping")
                continue
            out_path = cycle_dir / f"{station.icao}.json"
            if out_path.exists() and out_path.stat().st_size > 0:
                ok_any = True
                continue
            params = {
                "location": f"{station.lat},{station.lon}",
                "apikey": api_key,
                "units": "imperial",
            }
            try:
                r = requests.get(API_BASE, params=params, timeout=30)
                if r.status_code == 429:
                    print(f"[{self.name}] {station.icao}: rate limit hit (HTTP 429)")
                    continue
                r.raise_for_status()
                out_path.write_text(r.text)
                ok_any = True
            except requests.RequestException as e:
                print(f"[{self.name}] {station.icao}: request failed — {e}")
        return cycle_dir if ok_any else None

    def parse(
        self,
        path: Path,
        stations: Iterable[str],
        cycle: datetime,
    ) -> pd.DataFrame:
        cycle_dir = Path(path)
        # Tag with the actual fetch time (per your earlier choice — "honest").
        # The 'cycle' arg may be the canonical cycle datetime; we override
        # with the API call timestamp inferred from the cache directory name
        # (the cache dir is named tomorrowio.YYYYMMDD.HHz so the cycle there
        # reflects when the data was fetched).
        # In practice cycle == when we made the request when called fresh.
        tag_cycle = cycle

        records: list[ForecastRecord] = []
        for icao in stations:
            f = cycle_dir / f"{icao.upper()}.json"
            if not f.exists():
                continue
            try:
                payload = pd.read_json(f, typ="series").to_dict()
            except Exception:
                # Fallback: read raw and json.loads
                import json
                payload = json.loads(f.read_text())

            timelines = payload.get("timelines", {})
            hourly = timelines.get("hourly", [])
            if not hourly:
                continue

            for entry in hourly:
                try:
                    rec = _parse_hourly_entry(entry, icao.upper(), tag_cycle, f.name)
                    if rec is not None:
                        records.append(rec)
                except Exception as e:
                    print(f"[{self.name}] parse error for {icao}: {e}")
                    continue

        return records_to_df(records) if records else empty_df()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_hourly_entry(
    entry: dict,
    station_id: str,
    cycle: datetime,
    source_file: str,
) -> Optional[ForecastRecord]:
    """Convert one hourly entry into a ForecastRecord."""
    time_str = entry.get("time")
    values = entry.get("values", {}) or {}
    if not time_str:
        return None

    valid_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    if valid_time.tzinfo is None:
        valid_time = valid_time.replace(tzinfo=timezone.utc)

    # forecast_hour relative to the API-call cycle. Can be negative if the
    # API returned past hours (it sometimes does).
    delta = valid_time - cycle
    fhour = int(round(delta.total_seconds() / 3600))

    # Drop the f+0 ("nowcast") hour. Tomorrow.io anchors this to observations
    # in a way that often diverges from both the actual METAR and the
    # subsequent forecast hours. Skip it so the plot starts at f+1.
    if fhour <= 0:
        return None

    # Visibility — already in statute miles via units=imperial. Clamp to 10
    # to match our existing convention (cat 7 == "unlimited" displayed at 10).
    vis = values.get("visibility")
    vsby_sm: Optional[float] = None
    if vis is not None:
        try:
            vsby_sm = min(10.0, float(vis))
        except (TypeError, ValueError):
            vsby_sm = None

    # Ceiling — given in kilofeet via units=imperial. None means unlimited.
    cig_kft = values.get("cloudCeiling")
    ceiling_ft: Optional[float] = None
    ceiling_unlimited = False
    if cig_kft is None:
        ceiling_unlimited = True
    else:
        try:
            ceiling_ft = float(cig_kft) * 1000.0
        except (TypeError, ValueError):
            ceiling_unlimited = True

    return ForecastRecord(
        station_id=station_id,
        model=TomorrowIO.name,
        cycle=cycle,
        valid_time=valid_time,
        forecast_hour=fhour,
        vsby_sm=vsby_sm,
        vsby_category=units.vsby_sm_to_category(vsby_sm),
        ceiling_ft=ceiling_ft,
        ceiling_category=units.ceiling_ft_to_category(
            ceiling_ft, unlimited=ceiling_unlimited
        ),
        ceiling_unlimited=ceiling_unlimited,
        source_file=source_file,
    )
