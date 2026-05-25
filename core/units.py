"""Unit conversions and MOS category decoding.

Single source of truth for converting between:
  - MOS VIS/CIG single-digit categories (per official MAV card)
  - Continuous values (statute miles, feet AGL)
  - GRIB native units (meters)

Category boundaries are taken from the NWS MDL MAV card. When HRRR or
another continuous-output model needs to be compared "apples-to-apples"
with MOS, use vsby_sm_to_category() / ceiling_ft_to_category() to bucket
the continuous value into the same MOS bin.
"""
from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# Visibility (statute miles)
# ---------------------------------------------------------------------------
# Per the official MAV card:
#   1: < 1/2 sm
#   2: 1/2 to < 1 sm
#   3: 1 to < 2 sm
#   4: 2 to < 3 sm
#   5: 3 to < 5 sm
#   6: 5 to <= 6 sm
#   7: > 6 sm
# Represented as (lower_inclusive, upper_exclusive). 7's upper is +inf.
VIS_CATEGORY_RANGES_SM: dict[int, tuple[float, float]] = {
    1: (0.0, 0.5),
    2: (0.5, 1.0),
    3: (1.0, 2.0),
    4: (2.0, 3.0),
    5: (3.0, 5.0),
    6: (5.0, 6.0001),  # tiny epsilon so 6.0 itself falls in cat 6
    7: (6.0001, math.inf),
}

# A representative scalar for plotting / numeric comparison when only the
# category is known. Midpoints; cat 1 and 7 use the open-ended boundary.
VIS_CATEGORY_MIDPOINTS_SM: dict[int, float] = {
    1: 0.25,
    2: 0.75,
    3: 1.5,
    4: 2.5,
    5: 4.0,
    6: 5.5,
    7: 7.0,
}

# ---------------------------------------------------------------------------
# Ceiling (feet AGL)
# ---------------------------------------------------------------------------
# Per the official MAV card:
#   1: < 200 ft
#   2: 200-400 ft
#   3: 500-900 ft
#   4: 1000-1900 ft
#   5: 2000-3000 ft
#   6: 3100-6500 ft
#   7: 6600-12000 ft
#   8: > 12000 ft or unlimited
CIG_CATEGORY_RANGES_FT: dict[int, tuple[float, float]] = {
    1: (0, 200),
    2: (200, 500),
    3: (500, 1000),
    4: (1000, 2000),
    5: (2000, 3100),
    6: (3100, 6600),
    7: (6600, 12001),
    8: (12001, math.inf),
}

CIG_CATEGORY_MIDPOINTS_FT: dict[int, float] = {
    1: 100,
    2: 300,
    3: 700,
    4: 1500,
    5: 2500,
    6: 4800,
    7: 9300,
    8: 15000,  # nominal — treat as "unlimited" downstream when needed
}


# ---------------------------------------------------------------------------
# MOS code -> numeric (midpoint)
# ---------------------------------------------------------------------------
def vis_category_to_sm(code: Optional[int]) -> Optional[float]:
    """MOS VIS code (1-7) to representative statute miles. None if invalid."""
    if code is None:
        return None
    return VIS_CATEGORY_MIDPOINTS_SM.get(int(code))


def ceiling_category_to_ft(code: Optional[int]) -> Optional[float]:
    """MOS CIG code (1-8) to representative feet AGL. None if invalid."""
    if code is None:
        return None
    return CIG_CATEGORY_MIDPOINTS_FT.get(int(code))


# ---------------------------------------------------------------------------
# Numeric -> MOS code (bucketing continuous values for comparison)
# ---------------------------------------------------------------------------
def vsby_sm_to_category(sm: Optional[float]) -> Optional[int]:
    """Bucket a continuous visibility (statute miles) into MOS VIS 1-7."""
    if sm is None or (isinstance(sm, float) and math.isnan(sm)):
        return None
    for code, (lo, hi) in VIS_CATEGORY_RANGES_SM.items():
        if lo <= sm < hi:
            return code
    return 7  # anything beyond cat 6 falls into 7


def ceiling_ft_to_category(ft: Optional[float], unlimited: bool = False) -> Optional[int]:
    """Bucket a ceiling (feet AGL) into MOS CIG 1-8. unlimited=True -> 8."""
    if unlimited:
        return 8
    if ft is None or (isinstance(ft, float) and math.isnan(ft)):
        return None
    for code, (lo, hi) in CIG_CATEGORY_RANGES_FT.items():
        if lo <= ft < hi:
            return code
    return 8


# ---------------------------------------------------------------------------
# GRIB / SI unit conversions
# ---------------------------------------------------------------------------
METERS_PER_MILE = 1609.344
METERS_PER_FOOT = 0.3048


def meters_to_statute_miles(m: float) -> float:
    return m / METERS_PER_MILE


def meters_to_feet(m: float) -> float:
    return m / METERS_PER_FOOT


# HRRR visibility convention: very large values (often ~24000 m, sometimes
# clipped at the model's max) represent "unlimited" / clear conditions. We
# cap at this threshold when reporting numeric vsby to avoid skewing plots.
HRRR_VIS_UNLIMITED_THRESHOLD_M = 20000.0  # ~12.4 sm


def hrrr_vis_meters_to_sm(m: Optional[float]) -> Optional[float]:
    """Convert HRRR surface visibility (m) to statute miles, clamping unlimited."""
    if m is None or (isinstance(m, float) and math.isnan(m)):
        return None
    if m >= HRRR_VIS_UNLIMITED_THRESHOLD_M:
        return meters_to_statute_miles(HRRR_VIS_UNLIMITED_THRESHOLD_M)
    return meters_to_statute_miles(m)
