"""Network-condition colour for a station chip.

Target path: core/station_status.py

Pure functions, no Streamlit, no network. One station in, one colour out:
the WORST condition wins, and only that colour is shown.

Ladder, least to most severe
    GREEN   VFR, gusts <= 25 kt, no thunder. -RA and RA stay green.
    YELLOW  MVFR, or VCTS, or gusts 26-30 kt
    ORANGE  IFR, or +RA, or gusts 31-35 kt
    PINK    LIFR, or gusts >= 36 kt
    RED     any TS / TSRA / -TSRA / +TSRA (not VCTS)

RED sits above PINK because thunder drives the operation harder than a
low ceiling does. If you would rather fold thunder into PINK and keep a
four-colour board, set TS_LEVEL = PINK below - nothing else changes.

Thunder is read from the current METAR by default. Set TAF_TS_COUNTS to
True to let a TS group in the TAF colour the chip as well.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ---------------------------------------------------------------- levels

#: NONE is not on the severity ladder - it means "no observation".
#: A station whose METAR failed to arrive must not paint green, which
#: would read as "this field is fine".
NONE = -1

GREEN, YELLOW, ORANGE, PINK, RED = 0, 1, 2, 3, 4

COLORS = {
    NONE:   "#6E6E6E",
    GREEN:  "#00FF7F",
    YELLOW: "#FFD400",
    ORANGE: "#FF8A00",
    PINK:   "#FF00C8",
    RED:    "#FF3B30",
}

NAMES = {NONE: "none", GREEN: "green", YELLOW: "yellow",
         ORANGE: "orange", PINK: "pink", RED: "red"}

#: Level assigned to observed thunder. Set to PINK for a four-colour board.
TS_LEVEL = {"red": RED, "pink": PINK}.get(
    os.getenv("BLUEMET_TS_LEVEL", "red").lower(), RED)

#: When True a TS group in the TAF also colours the chip.
TAF_TS_COUNTS = os.getenv("BLUEMET_TAF_TS", "0") == "1"

# Gust breaks, in knots. <=25 green, 26-30 yellow, 31-35 orange, >=36 pink.
GUST_YELLOW, GUST_ORANGE, GUST_PINK = 26, 31, 36


@dataclass(frozen=True)
class Status:
    """Result for one station."""
    icao: str
    level: int
    reason: str          # why this colour, as a sentence, for the hover
    # The ONE value that set the colour, as printed on the chip:
    # "CIG 600", "VIS 2SM", "G38", "+RA", "TS", "VCTS", "VFR".
    label: str = ""

    @property
    def color(self) -> str:
        return COLORS[self.level]

    @property
    def name(self) -> str:
        return NAMES[self.level]


# ---------------------------------------------------------------- parsing

_GUST_RE = re.compile(r"\b(?:VRB|\d{3})\d{2}G(\d{2,3})(?:KT|MPS)\b")
_WIND_RE = re.compile(r"\b(?:VRB|\d{3})(\d{2})(?:KT|MPS)\b")
_VIS_RE = re.compile(r"\b(?:(\d{1,2})\s+)?(\d{1,2})(?:/(\d))?SM\b")
_LAYER_RE = re.compile(r"\b(?:BKN|OVC|VV)(\d{3})\b")
_CAVOK_RE = re.compile(r"\b(?:CAVOK|SKC|CLR|NSC)\b")

# Thunder. VCTS is deliberately matched first and separately.
_VCTS_RE = re.compile(r"\bVCTS\b")
_TS_RE = re.compile(r"(?<![A-Z])[-+]?(?:TS|TSRA|TSSN|TSGR|TSPL)(?![A-Z])")

# Heavy precipitation: a leading + on a precip group.
_HEAVY_RE = re.compile(r"\+(?:TS)?(?:SH)?(?:RA|SN|PL|GR|GS|DZ|UP)")


def parse_gust(metar: str) -> int | None:
    """Gust in knots, or None when the group carries no gust."""
    m = _GUST_RE.search(metar)
    return int(m.group(1)) if m else None


def parse_visibility_sm(metar: str) -> float | None:
    """Prevailing visibility in statute miles."""
    if _CAVOK_RE.search(metar):
        return 10.0
    for m in _VIS_RE.finditer(metar):
        whole, num, den = m.group(1), m.group(2), m.group(3)
        if den:                       # 3/4SM, or 1 1/2SM
            v = int(num) / int(den)
            if whole:
                v += int(whole)
        else:
            v = float(num)
        return v
    return None


def parse_ceiling_ft(metar: str) -> int | None:
    """Lowest BKN/OVC/VV layer in feet AGL, or None when none is reported."""
    hs = [int(m.group(1)) * 100 for m in _LAYER_RE.finditer(metar)]
    return min(hs) if hs else None


def flight_category(vis_sm: float | None, ceiling_ft: int | None) -> str:
    """LIFR / IFR / MVFR / VFR from visibility and ceiling."""
    v = 99.0 if vis_sm is None else vis_sm
    c = 99999 if ceiling_ft is None else ceiling_ft
    if v < 1 or c < 500:
        return "LIFR"
    if v < 3 or c < 1000:
        return "IFR"
    if v <= 5 or c <= 3000:
        return "MVFR"
    return "VFR"


# ---------------------------------------------------------------- rules

_CATEGORY_LEVEL = {"VFR": GREEN, "MVFR": YELLOW, "IFR": ORANGE, "LIFR": PINK}


def _gust_level(gust: int | None) -> tuple[int, str]:
    if gust is None:
        return GREEN, ""
    if gust >= GUST_PINK:
        return PINK, f"Gusting {gust} kt"
    if gust >= GUST_ORANGE:
        return ORANGE, f"Gusting {gust} kt"
    if gust >= GUST_YELLOW:
        return YELLOW, f"Gusting {gust} kt"
    return GREEN, ""


def _category_reason(cat: str, vis_sm, ceiling_ft) -> str:
    bits = []
    if vis_sm is not None:
        bits.append(f"vis {vis_sm:g} SM")
    bits.append("ceiling unlimited" if ceiling_ft is None
                else f"ceiling {ceiling_ft} ft")
    return f"{cat} \u2014 " + ", ".join(bits)


def _fmt_vis(v: float) -> str:
    """0.25 -> '1/4SM', 1.5 -> '1 1/2SM', 2.0 -> '2SM'."""
    whole, frac = int(v), round(v - int(v), 2)
    fr = {0.25: "1/4", 0.5: "1/2", 0.75: "3/4", 0.12: "1/8",
          0.13: "1/8", 0.62: "5/8", 0.63: "5/8", 0.38: "3/8"}.get(frac)
    if not frac:
        return f"{whole}SM"
    if fr:
        return f"{whole} {fr}SM" if whole else f"{fr}SM"
    return f"{v:g}SM"


def _category_label(cat: str, vis_sm, ceiling_ft) -> str:
    """The value that put the station in its category: visibility or
    ceiling, whichever is the worse on its own."""
    if cat == "VFR":
        return "VFR"
    by_vis = flight_category(vis_sm, None)
    by_cig = flight_category(None, ceiling_ft)
    order = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
    if ceiling_ft is not None and order[by_cig] >= order[by_vis]:
        return f"CIG {ceiling_ft}"
    if vis_sm is not None:
        return f"VIS {_fmt_vis(vis_sm)}"
    return cat


def _strip_remarks(metar: str) -> str:
    """Everything after RMK is commentary and must not drive a colour."""
    return metar.split(" RMK ", 1)[0]


def status_for(icao: str, metar: str | None, taf: str | None = None) -> Status:
    """Worst-condition colour for one station.

    metar: the raw current observation. taf: the raw forecast, used only
    for VCTS/TS when TAF_TS_COUNTS is on.
    """
    if not metar:
        return Status(icao, NONE, "no observation", "NO OBS")

    body = _strip_remarks(metar.upper())

    vis_sm = parse_visibility_sm(body)
    ceiling_ft = parse_ceiling_ft(body)
    cat = flight_category(vis_sm, ceiling_ft)

    # flight category. Green still carries its reason, so a hover on a
    # clear station explains the green rather than saying nothing.
    level = _CATEGORY_LEVEL[cat]
    reason = _category_reason(cat, vis_sm, ceiling_ft)
    label = _category_label(cat, vis_sm, ceiling_ft)

    # wind gusts
    gust = parse_gust(body)
    glevel, gtext = _gust_level(gust)
    if glevel > level:
        level, reason, label = glevel, gtext, f"G{gust}"

    # heavy precipitation
    heavy = _HEAVY_RE.search(body)
    if heavy and ORANGE > level:
        level, reason = ORANGE, "Heavy precipitation (+RA)"
        label = heavy.group(0)

    # thunder. VCTS is yellow; everything else that is thunder is TS_LEVEL.
    ts_sources = [body]
    if TAF_TS_COUNTS and taf:
        ts_sources.append(_strip_remarks(taf.upper()))

    for i, src in enumerate(ts_sources):
        where = ("TAF" if (i and TAF_TS_COUNTS) else "current METAR")
        without_vc = _VCTS_RE.sub(" ", src)
        if _TS_RE.search(without_vc):
            if TS_LEVEL > level:
                level, reason = TS_LEVEL, f"Thunderstorm in {where}"
                label = "TS" if not i else "TS (TAF)"
            break
        if _VCTS_RE.search(src) and YELLOW > level:
            level, reason = YELLOW, f"Thunderstorm in the vicinity ({where})"
            label = "VCTS" if not i else "VCTS (TAF)"

    return Status(icao, level, reason, label)


def board(stations, metars: dict, tafs: dict | None = None):
    """Status for each station, in the given order.

    stations: iterable of ICAO identifiers.
    metars / tafs: {icao: raw text}. A station with no observation is
    grey, not green.
    """
    tafs = tafs or {}
    return [status_for(s, metars.get(s), tafs.get(s)) for s in stations]


#: The 13 stations on the login board, in display order.
LOGIN_STATIONS = ["JFK", "LGA", "EWR", "HPN", "BOS", "BDL", "DCA",
                  "MCO", "FLL", "TPA", "DJT", "LAX"]

#: DJT took over the old KPBI location.
STATION_LATLON = {
    "JFK": (40.640, -73.779), "LGA": (40.777, -73.872),
    "EWR": (40.689, -74.175), "HPN": (41.067, -73.708),
    "BOS": (42.363, -71.006), "BDL": (41.939, -72.683),
    "DCA": (38.852, -77.038), "MCO": (28.429, -81.309),
    "FLL": (26.072, -80.152), "TPA": (27.976, -82.533),
    "DJT": (26.683, -80.096), "LAX": (33.942, -118.408),
}
