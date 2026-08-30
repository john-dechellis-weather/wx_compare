"""Common forecast record schema.

Every model source (MOS, HRRR, NBM, ...) must produce DataFrames matching
this schema. This is the only schema decision that matters — get it right
and adding new models becomes trivial.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import pandas as pd


# Canonical column order. Use this when constructing DataFrames so every
# model's output looks identical and joins/concats are painless.
COLUMNS = [
    "station_id",        # ICAO, e.g. "KJFK"
    "model",             # "GFS_MOS", "HRRR", "NBM", ...
    "cycle",             # model run time (UTC, tz-aware)
    "valid_time",        # forecast valid time (UTC, tz-aware)
    "forecast_hour",     # int, valid_time - cycle in hours
    "vsby_sm",           # statute miles, float, NaN if missing
    "vsby_category",     # MOS-style 1-7 code if applicable, else pd.NA
    "ceiling_ft",        # feet AGL, float, NaN if missing or unlimited
    "ceiling_category",  # MOS-style 1-8 code if applicable, else pd.NA
    "ceiling_unlimited", # bool — True when model reports clear/unlimited
    "wind_speed_kt",     # knots, float, NaN if missing
    "wind_dir_deg",      # degrees true (0-360), float, NaN if missing or variable
    "wind_gust_kt",      # knots, float, NaN if no gust reported
    "temp_f",            # degrees F, float, NaN if missing
    "dewpoint_f",        # degrees F, float, NaN if missing
    "source_file",       # provenance: filename or URL fragment
]


@dataclass
class ForecastRecord:
    """One forecast point for one station, one valid time, one model.

    Build records via this dataclass when emitting from a parser, then call
    records_to_df() to get a properly-typed DataFrame.
    """
    station_id: str
    model: str
    cycle: datetime
    valid_time: datetime
    forecast_hour: int
    vsby_sm: Optional[float] = None
    vsby_category: Optional[int] = None
    ceiling_ft: Optional[float] = None
    ceiling_category: Optional[int] = None
    ceiling_unlimited: bool = False
    wind_speed_kt: Optional[float] = None
    wind_dir_deg: Optional[float] = None
    wind_gust_kt: Optional[float] = None
    # Temperature and dewpoint in FAHRENHEIT, which is what the MOS
    # bulletins carry. Storing the bulletin's own units avoids a
    # conversion that would then be undone for display.
    temp_f: Optional[float] = None
    dewpoint_f: Optional[float] = None
    source_file: str = ""


def records_to_df(records: list[ForecastRecord]) -> pd.DataFrame:
    """Convert a list of ForecastRecord into a canonical DataFrame."""
    if not records:
        return empty_df()
    df = pd.DataFrame([asdict(r) for r in records], columns=COLUMNS)
    return _enforce_dtypes(df)


def empty_df() -> pd.DataFrame:
    """Return a correctly-typed empty DataFrame. Useful for failed fetches."""
    df = pd.DataFrame({c: pd.Series(dtype=object) for c in COLUMNS})
    return _enforce_dtypes(df)


def _enforce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Apply canonical dtypes. Keeps joins and comparisons predictable."""
    if len(df) == 0:
        # Set dtypes on the empty frame too so downstream code doesn't choke.
        return df.astype({
            "station_id": "string",
            "model": "string",
            "forecast_hour": "Int64",
            "vsby_sm": "float64",
            "vsby_category": "Int64",
            "ceiling_ft": "float64",
            "ceiling_category": "Int64",
            "ceiling_unlimited": "boolean",
            "wind_speed_kt": "float64",
            "wind_dir_deg": "float64",
            "wind_gust_kt": "float64",
            "temp_f": "float64",
            "dewpoint_f": "float64",
            "source_file": "string",
        })
    df = df.copy()
    df["station_id"] = df["station_id"].astype("string")
    df["model"] = df["model"].astype("string")
    df["cycle"] = pd.to_datetime(df["cycle"], utc=True)
    df["valid_time"] = pd.to_datetime(df["valid_time"], utc=True)
    df["forecast_hour"] = df["forecast_hour"].astype("Int64")
    df["vsby_sm"] = pd.to_numeric(df["vsby_sm"], errors="coerce")
    df["vsby_category"] = df["vsby_category"].astype("Int64")
    df["ceiling_ft"] = pd.to_numeric(df["ceiling_ft"], errors="coerce")
    df["ceiling_category"] = df["ceiling_category"].astype("Int64")
    df["ceiling_unlimited"] = df["ceiling_unlimited"].astype("boolean")
    df["wind_speed_kt"] = pd.to_numeric(df["wind_speed_kt"], errors="coerce")
    df["wind_dir_deg"] = pd.to_numeric(df["wind_dir_deg"], errors="coerce")
    df["wind_gust_kt"] = pd.to_numeric(df["wind_gust_kt"], errors="coerce")
    df["temp_f"] = pd.to_numeric(df["temp_f"], errors="coerce")
    df["dewpoint_f"] = pd.to_numeric(df["dewpoint_f"],
                                     errors="coerce")
    df["source_file"] = df["source_file"].astype("string")
    return df
