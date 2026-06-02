"""HRRR (High-Resolution Rapid Refresh) source.

URL pattern (CONUS):
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/
    hrrr.YYYYMMDD/conus/hrrr.tCCz.wrfsfcfHH.grib2
  (companion .idx file at the same URL + ".idx")

Cycles: every hour (00-23 UTC).
Forecast hours: 0-48 for 00/06/12/18Z cycles, 0-18 otherwise.

Key optimization: full GRIB files are ~150 MB. We parse the .idx (a tiny
text file listing byte offsets per record), find just the records we need
(VIS at surface, HGT at cloud ceiling), and HTTP-Range-fetch only those
bytes — typically a few hundred KB per forecast hour instead of 150 MB.

Variable strings as they appear in HRRR .idx files:
  ":VIS:surface:"       — surface visibility (m)
  ":HGT:cloud ceiling:" — cloud ceiling height (m, MSL — needs station elev for AGL)
  ":CEIL:..."           — some products expose CEIL directly; fall back to HGT
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd
import requests

from core.schema import ForecastRecord, records_to_df, empty_df
from core import units
from .base import ModelSource


NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod"

# Records we want from the surface file. Each is a regex matched against
# the .idx line. Keep these conservative — too-loose patterns will grab
# extra records and bloat downloads.
WANTED_RECORDS = {
    "vsby": re.compile(r":VIS:surface:"),
    "ceiling_hgt": re.compile(r":HGT:cloud ceiling:"),
    # Wind at 10m AGL. The component values are in m/s; we compute speed and
    # direction in parse_point_subset(). Note: ":UGRD:10 m above ground:" matches
    # only that record, not other UGRD layers like 80m or top of atmosphere.
    "ugrd_10m": re.compile(r":UGRD:10 m above ground:"),
    "vgrd_10m": re.compile(r":VGRD:10 m above ground:"),
}


# Use the canonical Station from core.stations so all modules speak the
# same dataclass. Re-export here for backward compatibility with any code
# that imported Station from models.hrrr or models.
from core.stations import Station  # noqa: F401  (re-export)


class Hrrr(ModelSource):
    """HRRR surface forecasts, point-extracted at given stations."""

    name = "HRRR"

    def __init__(self, cache_dir: Path, stations: list[Station], fhours: Iterable[int] = range(0, 7)):
        super().__init__(cache_dir)
        # Index stations by ICAO for fast lookup during parsing.
        self.station_table: dict[str, Station] = {s.icao.upper(): s for s in stations}
        self.fhours = list(fhours)

    # --- cycle discovery ---------------------------------------------------
    def available_cycles(self, date: datetime) -> list[datetime]:
        d = date.astimezone(timezone.utc) if date.tzinfo else date.replace(tzinfo=timezone.utc)
        return [
            d.replace(hour=h, minute=0, second=0, microsecond=0)
            for h in range(24)
        ]

    # --- URL helpers -------------------------------------------------------
    def _grib_url(self, cycle: datetime, fhour: int) -> str:
        ymd = cycle.strftime("%Y%m%d")
        return (f"{NOMADS_BASE}/hrrr.{ymd}/conus/"
                f"hrrr.t{cycle:%H}z.wrfsfcf{fhour:02d}.grib2")

    def _idx_url(self, cycle: datetime, fhour: int) -> str:
        return self._grib_url(cycle, fhour) + ".idx"

    def _cache_path(self, cycle: datetime, fhour: int) -> Path:
        return self.cache_dir / (
            f"hrrr.{cycle:%Y%m%d.%H}z.f{fhour:02d}.subset.grib2"
        )

    # --- cycle completeness probe ------------------------------------------
    def is_cycle_complete(self, cycle: datetime) -> bool:
        """HRRR posts files incrementally as the run progresses. Probe the
        .idx of the highest forecast hour we care about — if that exists,
        every earlier hour exists too. Using .idx not the grib itself
        because idx files are tiny (~kilobytes) and one HEAD is enough.
        """
        last_fhour = max(self.fhours) if self.fhours else 0
        url = self._idx_url(cycle, last_fhour)
        try:
            r = requests.head(url, timeout=15, allow_redirects=True)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # --- fetch -------------------------------------------------------------
    def fetch(self, cycle: datetime, stations: Iterable[str]) -> Optional[Path]:
        """Fetch is per-cycle but HRRR has one file per forecast hour. We
        return the cache *directory* root and let parse() iterate. This is a
        deliberate departure from the simple 'one file per cycle' contract
        and is documented here so future maintainers don't get surprised.
        """
        cycle_dir = self.cache_dir / f"hrrr.{cycle:%Y%m%d.%H}z"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        ok_any = False
        for fhour in self.fhours:
            subset_path = cycle_dir / f"f{fhour:02d}.subset.grib2"
            if subset_path.exists() and subset_path.stat().st_size > 0:
                ok_any = True
                continue
            try:
                self._download_subset(cycle, fhour, subset_path)
                ok_any = True
            except Exception as e:
                print(f"[{self.name}] f{fhour:02d} failed: {e}")
        return cycle_dir if ok_any else None

    def _download_subset(self, cycle: datetime, fhour: int, out_path: Path) -> None:
        """Fetch the .idx, find wanted records, then HTTP-Range the bytes."""
        idx_url = self._idx_url(cycle, fhour)
        grib_url = self._grib_url(cycle, fhour)

        idx_text = _http_get_text(idx_url, timeout=30)
        ranges = _idx_byte_ranges(idx_text, WANTED_RECORDS.values())
        if not ranges:
            raise RuntimeError(f"no wanted records found in idx for f{fhour:02d}")

        # Each range is independent; download and concatenate. GRIB2 is a
        # sequence of self-contained messages, so concatenated subsets are
        # still a valid GRIB2 file.
        chunks: list[bytes] = []
        with requests.Session() as sess:
            for start, end in ranges:
                headers = {"Range": f"bytes={start}-{end}"}
                r = sess.get(grib_url, headers=headers, timeout=60)
                if r.status_code not in (200, 206):
                    raise RuntimeError(
                        f"range request {start}-{end} returned {r.status_code}"
                    )
                chunks.append(r.content)
        out_path.write_bytes(b"".join(chunks))

    # --- parse -------------------------------------------------------------
    def parse(
        self,
        path: Path,
        stations: Iterable[str],
        cycle: datetime,
    ) -> pd.DataFrame:
        """Point-extract VIS and ceiling at each station for each forecast hour."""
        # Lazy import — cfgrib pulls in eccodes, which is a heavy install we
        # only want to require when HRRR is actually used.
        try:
            import xarray as xr  # noqa: F401
        except ImportError:
            print(f"[{self.name}] xarray/cfgrib not installed — skipping parse")
            return empty_df()

        cycle_dir = Path(path)
        station_list = [self.station_table[s.upper()]
                        for s in stations if s.upper() in self.station_table]
        if not station_list:
            return empty_df()

        records: list[ForecastRecord] = []
        for fhour in self.fhours:
            subset_path = cycle_dir / f"f{fhour:02d}.subset.grib2"
            if not subset_path.exists():
                continue
            try:
                records.extend(self._parse_one_hour(
                    subset_path, station_list, cycle, fhour
                ))
            except Exception as e:
                print(f"[{self.name}] parse f{fhour:02d}: {e}")
                continue
        return records_to_df(records) if records else empty_df()

    def _parse_one_hour(
        self,
        subset_path: Path,
        stations: list[Station],
        cycle: datetime,
        fhour: int,
    ) -> list[ForecastRecord]:
        import xarray as xr
        # Open each record separately. cfgrib needs a filter_by_keys to pick
        # which message it loads. Surface VIS and cloud-ceiling HGT live in
        # different typeOfLevel groups.
        vis_ds = _open_grib(subset_path, filter_by_keys={"shortName": "vis"})
        hgt_ds = _open_grib(subset_path, filter_by_keys={
            "shortName": "gh", "typeOfLevel": "cloudCeiling",
        })
        # Wind U/V at 10m AGL — heightAboveGround=10 disambiguates from other layers
        u_ds = _open_grib(subset_path, filter_by_keys={
            "shortName": "u10", "typeOfLevel": "heightAboveGround",
        })
        v_ds = _open_grib(subset_path, filter_by_keys={
            "shortName": "v10", "typeOfLevel": "heightAboveGround",
        })

        valid_time = cycle + timedelta(hours=fhour)
        records: list[ForecastRecord] = []
        for stn in stations:
            vis_m = _nearest_point(vis_ds, "vis", stn.lat, stn.lon) if vis_ds is not None else np.nan
            hgt_m_msl = _nearest_point(hgt_ds, "gh", stn.lat, stn.lon) if hgt_ds is not None else np.nan
            u_ms = _nearest_point(u_ds, "u10", stn.lat, stn.lon) if u_ds is not None else np.nan
            v_ms = _nearest_point(v_ds, "v10", stn.lat, stn.lon) if v_ds is not None else np.nan

            vsby_sm = units.hrrr_vis_meters_to_sm(float(vis_m)) if np.isfinite(vis_m) else None
            # Convert MSL height to AGL using the station's field elevation.
            # HRRR cloud ceiling missing/unlimited is typically a fill value
            # like 20000 m or NaN; treat both as "unlimited".
            ceiling_ft = None
            ceiling_unlimited = False
            if np.isfinite(hgt_m_msl) and hgt_m_msl < 20000:
                agl_ft = units.meters_to_feet(float(hgt_m_msl)) - stn.elev_ft
                if agl_ft > 0:
                    ceiling_ft = agl_ft
                else:
                    ceiling_unlimited = True  # below ground -> treat as missing
            else:
                ceiling_unlimited = True

            # Wind speed = sqrt(u² + v²), m/s → knots × 1.94384
            # Direction (meteorological): atan2(-u, -v) gives the direction the
            # wind is COMING FROM, in radians, then convert to degrees [0, 360).
            wind_speed_kt = None
            wind_dir_deg = None
            if np.isfinite(u_ms) and np.isfinite(v_ms):
                speed_ms = float(np.hypot(u_ms, v_ms))
                wind_speed_kt = speed_ms * 1.94384
                # Calm winds: direction is undefined; leave as None
                if speed_ms > 0.1:
                    raw_deg = float(np.degrees(np.arctan2(-u_ms, -v_ms)))
                    wind_dir_deg = raw_deg % 360.0

            records.append(ForecastRecord(
                station_id=stn.icao,
                model=self.name,
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
                wind_speed_kt=wind_speed_kt,
                wind_dir_deg=wind_dir_deg,
                wind_gust_kt=None,  # HRRR provides GUST but at surface, separate record
                source_file=subset_path.name,
            ))
        return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _http_get_text(url: str, timeout: int = 30) -> str:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def _idx_byte_ranges(
    idx_text: str,
    patterns: Iterable[re.Pattern],
) -> list[tuple[int, int]]:
    """Parse a .idx file and return a list of (start, end) byte ranges
    matching any of the given patterns.

    .idx format: "<msgnum>:<byteoffset>:d=<date>:<var>:<level>:<...>"
    The end byte is (next message's offset - 1); the last message ends at EOF
    so we use a very large end value (HTTP servers cap at file size anyway).
    """
    patterns = list(patterns)
    lines = [l for l in idx_text.splitlines() if l.strip()]
    parsed = []
    for line in lines:
        parts = line.split(":")
        if len(parts) < 2:
            continue
        try:
            offset = int(parts[1])
        except ValueError:
            continue
        parsed.append((offset, line))

    ranges: list[tuple[int, int]] = []
    for i, (offset, line) in enumerate(parsed):
        if any(p.search(line) for p in patterns):
            next_offset = parsed[i + 1][0] - 1 if i + 1 < len(parsed) else offset + 50_000_000
            ranges.append((offset, next_offset))
    return ranges


def _open_grib(path: Path, filter_by_keys: dict):
    """Open a GRIB2 subset with cfgrib, returning None if the message isn't there."""
    import xarray as xr
    try:
        return xr.open_dataset(
            path, engine="cfgrib",
            backend_kwargs={"filter_by_keys": filter_by_keys, "indexpath": ""},
        )
    except Exception:
        return None


def _nearest_point(ds, varname: str, lat: float, lon: float) -> float:
    """Nearest-neighbor extraction of a 2D field at one (lat, lon)."""
    if ds is None or varname not in ds.variables:
        return float("nan")
    da = ds[varname]
    # HRRR longitudes are 0..360; allow either convention for the input.
    grid_lon = ds["longitude"].values
    grid_lat = ds["latitude"].values
    target_lon = lon % 360 if grid_lon.max() > 180 else lon
    # Flat-earth nearest neighbor is fine over CONUS for a 3-km grid; great-
    # circle distance is overkill here.
    dist2 = (grid_lat - lat) ** 2 + (grid_lon - target_lon) ** 2
    j, i = np.unravel_index(np.argmin(dist2), dist2.shape)
    return float(da.values[j, i])
