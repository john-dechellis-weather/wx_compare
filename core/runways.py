"""Runway configuration inferred from traffic.

There is no public feed that says which runways JFK is landing on.
But the aircraft say it: an arrival on final is aligned with a
runway's heading, on its extended centreline, low and descending, a
few miles from the threshold. A departure is aligned, low and
climbing, just past the far end. Tally those over the last fifteen
minutes and the configuration reads itself: "landing 22L (7), 22R
(3); departing 31L (5)".

GEOMETRY. Each runway way in the OpenStreetMap surface carries a ref
such as "13L/31R" and a path whose first and last points are its
ends. The end named first is the one an aircraft on that runway
heads AWAY from: landing 13L means tracking 130 and touching down at
the 13L threshold, which is the end from which a 130 heading runs
along the runway. So the threshold of "13L" is whichever endpoint
makes the along-runway bearing closest to 130.

FINAL: track within ALIGN_DEG of the runway heading, lateral offset
from the extended centreline within LAT_NM, between FINAL_MIN_NM and
FINAL_MAX_NM before the threshold, below FINAL_MAX_FT, not climbing.
DEPARTURE: aligned, within LAT_NM, between DEP_MIN_NM and DEP_MAX_NM
past the far end, below DEP_MAX_FT, climbing.

The tally lives in this module for the life of the process; the
page feeds it every rerun and reads the last WINDOW_S.
"""

from __future__ import annotations

import math
import re
import threading
import time

ALIGN_DEG = 15.0
LAT_NM = 0.7
FINAL_MIN_NM, FINAL_MAX_NM, FINAL_MAX_FT = 1.0, 12.0, 4500
DEP_MIN_NM, DEP_MAX_NM, DEP_MAX_FT = 0.3, 7.0, 6000
WINDOW_S = 15 * 60
_LOCK = threading.Lock()
_SEEN = {}     # (apt, end, kind) -> {hex: last_ts}
# HOURLY TALLY (r106). _SEEN is pruned at twice WINDOW_S (30 min), so
# it cannot answer "how many landed in the last hour". This keeps one
# timestamp per aircraft per airport for RATE_WIN_S instead: distinct
# hexes seen on final, which is an OBSERVED rate, not the AAR.
_ARR_LOG = {}  # apt -> {hex: first_seen_ts}
RATE_WIN_S = 3600
# EVENT QUEUE (r122). observe() records the fact that an aircraft was
# seen on final or climbing out; the collector wants the MOMENT it
# first happened so it can write one durable line per movement. Each
# first sighting is queued here and drained by whoever is recording.
# Bounded: if nothing drains it, it stops growing rather than eating
# the process.
_EVENTS = []
_EVENTS_MAX = 5000


def drain_events() -> list:
    """Every movement seen since the last call. [{apt,end,kind,hex,cs,ts}]"""
    global _EVENTS
    with _LOCK:
        out, _EVENTS = _EVENTS, []
    return out


def _queue(apt, end, kind, hx, cs, now):
    if len(_EVENTS) < _EVENTS_MAX:
        _EVENTS.append({"apt": apt, "end": end, "kind": kind,
                        "hex": hx, "cs": cs, "ts": now})
# Field elevation, ft. Used only to decide whether an aircraft is low
# enough to be on final or in climbout, so it is forgiving - being a
# few hundred feet out never flips a call. register_elevation() lets
# any airport be added at run time from whatever source the caller
# has; an unknown field falls back to sea level, which makes the
# arrival test slightly stricter rather than wrong.
_ELEV = {"JFK": 13, "LGA": 21, "EWR": 18}


def register_elevation(apt: str, elev_ft) -> None:
    """Teach the observer a field's elevation, so it works anywhere."""
    try:
        _ELEV[(apt or "").upper()] = float(elev_ft)
    except (TypeError, ValueError):
        pass


def _bearing(a, b):
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.degrees(math.atan2((b[0] - a[0]) * k, b[1] - a[1])) % 360.0


def _dang(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def runway_ends(surface: dict) -> list:
    """[{apt, end, thr (lon, lat), far (lon, lat), hdg}] for every
    named runway end in a surface (merged or single)."""
    out = []
    for rw in surface.get("runways") or []:
        ref = (rw.get("ref") or "").strip().upper()
        path = rw.get("path") or []
        if "/" not in ref or len(path) < 2:
            continue
        a, b = tuple(path[0]), tuple(path[-1])
        ends = []
        for e in ref.split("/"):
            m = re.match(r"^(\d{1,2})([LRC]?)$", e.strip())
            if m:
                ends.append(f"{int(m.group(1)):02d}{m.group(2)}")
        if len(ends) != 2:
            continue
        b_ab = _bearing(a, b)
        for name in ends:
            want = int(name[:2]) * 10.0
            # threshold is the end from which `want` runs along the runway
            if _dang(b_ab, want) <= 90:
                thr, far, hdg = a, b, b_ab
            else:
                thr, far, hdg = b, a, (b_ab + 180.0) % 360.0
            out.append({"apt": rw.get("apt", "JFK"), "end": name,
                        "thr": thr, "far": far, "hdg": hdg})
    return out


def _along_across(p, origin, hdg_deg, lat_ref):
    """Distance (nm) along the heading from `origin` and across it."""
    k = math.cos(math.radians(lat_ref))
    dx = (p[0] - origin[0]) * 60.0 * k
    dy = (p[1] - origin[1]) * 60.0
    h = math.radians(hdg_deg)
    along = dx * math.sin(h) + dy * math.cos(h)
    across = dx * math.cos(h) - dy * math.sin(h)
    return along, across


def observe(rows, ends: list, now: float = None) -> int:
    """Feed the live aircraft rows (lon, lat, _alt, _gs, _trk, vr,
    hex). Tags each as an arrival to or a departure from a runway end
    where the geometry says so. Returns the number tagged."""
    now = now or time.time()
    n = 0
    for r in rows:
        trk, alt, gs = r.get("_trk"), r.get("_alt"), r.get("_gs") or 0
        if trk is None or alt is None or gs < 60:
            continue
        vr = r.get("vr")
        hx = r.get("hex") or r.get("cs")
        for e in ends:
            if _dang(trk, e["hdg"]) > ALIGN_DEG:
                continue
            elev = _ELEV.get(e["apt"], 0)
            # arrival: before the threshold, aligned, low, not climbing
            along, across = _along_across((r["lon"], r["lat"]), e["thr"], e["hdg"], e["thr"][1])
            if (abs(across) <= LAT_NM and -FINAL_MAX_NM <= along <= -FINAL_MIN_NM
                    and alt - elev <= FINAL_MAX_FT and (vr is None or vr < 300)):
                with _LOCK:
                    _k = (e["apt"], e["end"], "arr")
                    _new = hx not in _SEEN.get(_k, {})
                    _SEEN.setdefault(_k, {})[hx] = now
                    # first sighting only, so one arrival counts once
                    _ARR_LOG.setdefault(e["apt"], {}).setdefault(hx, now)
                    if _new:
                        _queue(e["apt"], e["end"], "arr", hx, r.get("cs", ""), now)
                n += 1
                continue
            # departure: past the far end, aligned, low, climbing
            along2, across2 = _along_across((r["lon"], r["lat"]), e["far"], e["hdg"], e["far"][1])
            if (abs(across2) <= LAT_NM and DEP_MIN_NM <= along2 <= DEP_MAX_NM
                    and alt - elev <= DEP_MAX_FT and vr is not None and vr > 300):
                with _LOCK:
                    _kd = (e["apt"], e["end"], "dep")
                    _newd = hx not in _SEEN.get(_kd, {})
                    _SEEN.setdefault(_kd, {})[hx] = now
                    if _newd:
                        _queue(e["apt"], e["end"], "dep", hx, r.get("cs", ""), now)
                n += 1
    return n


def departed(apt: str = "JFK", now: float = None, window_s: float = None) -> dict:
    """{hex: (end, ts)} for aircraft the observer has seen DEPART `apt`
    recently — the same detection the configuration table is built on
    (past the far end of a runway, climbing), so the departure view and
    the runway caption can never disagree about who departed.

    The window is WINDOW_S by default, but the departure view wants a
    longer memory than the 15 minutes the configuration uses: an
    aircraft takes 15-25 minutes to cross N90, so at 15 minutes its
    track would be dropped from the store while it was still inside
    the boundary and still the thing you wanted to look at.
    """
    now = now or time.time()
    win = window_s or WINDOW_S
    out = {}
    with _LOCK:
        for (a, end, kind), seen in _SEEN.items():
            if a != apt or kind != "dep":
                continue
            for hx, t in seen.items():
                if now - t <= win and (hx not in out or t > out[hx][1]):
                    out[hx] = (end, t)
    return out


def arrival_rate(apt: str = "JFK", now: float = None, win_s: float = None) -> tuple:
    """(count, minutes_of_coverage) of distinct aircraft seen on final
    at `apt` in the last hour.

    The second number matters: after a deploy the observer has only
    been watching for a few minutes, and 3 arrivals in 6 minutes is
    not "3 an hour". The caller should say which it is.
    """
    now = now or time.time()
    win = win_s or RATE_WIN_S
    with _LOCK:
        seen = _ARR_LOG.setdefault(apt, {})
        for hx in [h for h, t in seen.items() if now - t > win]:
            del seen[hx]
        n = len(seen)
        oldest = min(seen.values()) if seen else now
    return n, int(max(0.0, min(win, now - oldest)) / 60.0)


def configuration(now: float = None) -> dict:
    """{apt: {"arr": [(end, count)...], "dep": [(end, count)...]}}
    over the last WINDOW_S, counts of distinct aircraft, sorted by
    count. Ends with no traffic are absent."""
    now = now or time.time()
    out = {}
    with _LOCK:
        for (apt, end, kind), seen in _SEEN.items():
            fresh = [h for h, t in seen.items() if now - t <= WINDOW_S]
            for h in [h for h, t in seen.items() if now - t > 2 * WINDOW_S]:
                del seen[h]
            if fresh:
                out.setdefault(apt, {"arr": [], "dep": []})[kind].append((end, len(fresh)))
    for apt in out:
        for kind in ("arr", "dep"):
            out[apt][kind].sort(key=lambda x: -x[1])
    return out


def describe(cfg: dict, apt: str) -> str:
    """'landing 22L (7), 22R (3) · departing 31L (5)' or 'no traffic
    observed on final or climbout in the last 15 min'."""
    c = cfg.get(apt)
    if not c or not (c["arr"] or c["dep"]):
        return "no traffic observed on final or climbout in the last 15 min"
    parts = []
    if c["arr"]:
        parts.append("landing " + ", ".join(f"{e} ({n})" for e, n in c["arr"]))
    if c["dep"]:
        parts.append("departing " + ", ".join(f"{e} ({n})" for e, n in c["dep"]))
    return " \u00b7 ".join(parts)
