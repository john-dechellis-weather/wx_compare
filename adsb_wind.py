"""Winds and temperatures aloft, derived from ADS-B aircraft state.

Every airliner is an anemometer that nobody reads. An aircraft
broadcasting Mode S enhanced surveillance reports both what it is
doing relative to the AIR (true airspeed, true heading) and relative
to the GROUND (groundspeed, track). The difference between those two
vectors IS the wind at that aircraft's altitude:

    wind = ground_vector - air_vector

Add `oat` — outside air temperature, measured by the aircraft — and
each report becomes a single-level sounding. A hundred aircraft over
a terminal area at varying altitudes is an upper-air network
observing continuously, at a density no radiosonde program can match.

This is not a novel idea in meteorology — AMDAR and MDCRS have done
it operationally for decades — but that data is not freely available
in real time, and the ADS-B feed already being polled for the fleet
map carries the raw ingredients for the same calculation.

WHAT LIMITS IT, stated plainly:

  * `tas`, `true_heading` and `oat` come from Mode S EHS/MRAR
    downlinks, NOT from standard ADS-B position broadcasts. Coverage
    is a subset of aircraft and depends on interrogation. Expect a
    useful minority, not every target.
  * A turning aircraft gives a bad wind: heading and track are
    changing and the two vectors are not contemporaneous. Rejected
    on `roll` where available.
  * `alt_baro` is PRESSURE altitude, not true altitude. Fine for
    binning by flight level, wrong if treated as height MSL.
  * A single report is noisy. The value is in the aggregate.
"""

from __future__ import annotations

import math

# Quality gates. Each one exists because of a specific way the
# calculation goes wrong, noted alongside.
MIN_TAS_KT = 80.0        # below this the vectors are small and the
                         # difference is dominated by sensor error
MIN_GS_KT = 60.0
MAX_ROLL_DEG = 5.0       # turning: heading and track disagree for
                         # reasons that are not wind
MAX_WIND_KT = 250.0      # beyond a strong jet core = bad data
MIN_ALT_FT = 1000.0      # below this, ground effect and manoeuvring


def wind_from_state(gs_kt, track_deg, tas_kt, heading_deg):
    """One aircraft's wind vector, or None.

    Meteorological convention: direction is where the wind is FROM.

    Vectors are built in compass space (0 = north, clockwise), so
    east = sin and north = cos — the reverse of the mathematical
    convention, and an easy sign error.
    """
    if None in (gs_kt, track_deg, tas_kt, heading_deg):
        return None
    if tas_kt < MIN_TAS_KT or gs_kt < MIN_GS_KT:
        return None
    trk = math.radians(track_deg)
    hdg = math.radians(heading_deg)
    # ground vector minus air vector
    u = gs_kt * math.sin(trk) - tas_kt * math.sin(hdg)   # eastward
    v = gs_kt * math.cos(trk) - tas_kt * math.cos(hdg)   # northward
    spd = math.hypot(u, v)
    if spd > MAX_WIND_KT:
        return None
    # FROM direction: reverse the flow vector
    frm = (math.degrees(math.atan2(-u, -v))) % 360.0
    return {"u": u, "v": v, "speed_kt": spd, "dir_from_deg": frm}


def observations(rows):
    """Turn raw aircraft rows into wind/temperature observations.

    `rows` are the dicts the fleet fetcher already produces, plus the
    Mode S fields when present. Returns (obs, stats) — stats says how
    many aircraft were usable and why the rest were not, because a
    silent 5% yield looks identical to a broken calculation.
    """
    obs = []
    stats = {"seen": 0, "no_modes": 0, "turning": 0, "too_slow": 0,
             "too_low": 0, "bad_wind": 0, "used": 0}
    for r in rows or []:
        stats["seen"] += 1
        tas = r.get("tas")
        hdg = r.get("true_heading")
        if hdg is None:
            hdg = r.get("mag_heading")
        if tas is None or hdg is None:
            stats["no_modes"] += 1
            continue
        alt = r.get("alt")
        if alt is None or alt < MIN_ALT_FT:
            stats["too_low"] += 1
            continue
        roll = r.get("roll")
        if roll is not None and abs(roll) > MAX_ROLL_DEG:
            stats["turning"] += 1
            continue
        gs = r.get("gs")
        trk = r.get("trk", r.get("track"))
        w = wind_from_state(gs, trk, tas, hdg)
        if w is None:
            if (gs or 0) < MIN_GS_KT or tas < MIN_TAS_KT:
                stats["too_slow"] += 1
            else:
                stats["bad_wind"] += 1
            continue
        obs.append({
            "lat": r.get("lat"), "lon": r.get("lon"),
            "alt_ft": float(alt),
            "fl": int(round(float(alt) / 100.0)),
            "speed_kt": w["speed_kt"],
            "dir_from_deg": w["dir_from_deg"],
            "u": w["u"], "v": w["v"],
            "oat_c": r.get("oat"),
            "cs": r.get("cs") or r.get("flight"),
        })
        stats["used"] += 1
    return obs, stats


def profile(obs, bin_ft=2000, min_n=2):
    """Aggregate into a vertical profile.

    Averaging is done on the U/V COMPONENTS, not on speed and
    direction. Averaging directions is wrong whenever the sample
    straddles north — 350 deg and 010 deg average to 180, the exact
    opposite of the truth.
    """
    bins = {}
    for o in obs:
        k = int(o["alt_ft"] // bin_ft)
        b = bins.setdefault(k, {"u": [], "v": [], "t": [], "n": 0})
        b["u"].append(o["u"])
        b["v"].append(o["v"])
        if o.get("oat_c") is not None:
            b["t"].append(float(o["oat_c"]))
        b["n"] += 1
    out = []
    for k in sorted(bins, reverse=True):
        b = bins[k]
        if b["n"] < min_n:
            continue
        u = sum(b["u"]) / b["n"]
        v = sum(b["v"]) / b["n"]
        out.append({
            "fl_lo": int(k * bin_ft / 100),
            "fl_hi": int((k + 1) * bin_ft / 100),
            "speed_kt": math.hypot(u, v),
            "dir_from_deg": math.degrees(math.atan2(-u, -v)) % 360.0,
            "oat_c": (sum(b["t"]) / len(b["t"])) if b["t"] else None,
            "n": b["n"],
        })
    return out
