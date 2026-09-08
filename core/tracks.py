"""Aircraft position history: trails and holding-pattern detection.

Ported from bluemet.org's page 3, with one structural change. That
page reruns on a 120 s fragment beat and feeds the tracker from each
rerun; this page reruns only when someone interacts with it, so a
tracker fed from reruns would have gaps whenever nobody was looking.
Here the tracker is a daemon thread that polls JetBlue positions
itself every TRACK_BEAT_S, and the page reads the history. Reruns
ALSO feed it, for free, so a busy viewer only makes the trail denser.

History is module-level, shared across every session — everyone sees
the same trails — and starts EMPTY after a restart. A short trail in
the first half hour after a deploy is not a fault; there is no
position history to invent.

THE THREE DETECTOR TESTS, all from the same history:
  * path flown over the window >= HOLD_MIN_PATH_NM, so a slow or
    stationary aircraft never trips it
  * path >= HOLD_RATIO x the spread of its positions — flying a long
    way while going nowhere
  * net turn >= HOLD_MIN_TURN_DEG in ONE direction — the test that
    keeps an S-turn weather deviation from reading as a hold, which
    it otherwise would
"""

from __future__ import annotations

import math
import os
import threading
import time

# 30 fixes at one per 2-minute beat is one hour — the longest window
# the page offers (15 / 30 / 45 min / 1 h). History is always kept to
# the maximum so switching windows never waits for new fixes.
# 60 fixes at one per minute is one hour, the longest window the page
# offers (15 / 30 / 45 min / 1 h). History is always kept to the
# maximum so switching windows never waits for new fixes.
TRAIL_MAX = int(os.environ.get("TRACK_POINTS", "60"))
TRAIL_TTL_S = float(os.environ.get("TRACK_TTL_S", "7200"))
# ONE FIX A MINUTE. A standard hold laps in four minutes; at two-minute
# fixes that is two samples a lap and the direction of turn is
# ambiguous. At one a minute it is four, and a hold reads as a hold
# after two laps. One adsb.lol request a minute.
TRACK_BEAT_S = int(os.environ.get("TRACK_BEAT_S", "60"))
# Catmull-Rom subdivisions per fix-to-fix segment. Fixes are two
# minutes apart, so a hold drawn fix-to-fix is a hexagon; six points
# per segment makes it a racetrack. Display only — the hold detector
# reads the raw fixes.
# 4, not 6. At six the trails were ~2 MB of JSON per rerun for 300
# aircraft, and the browser parsed all of it before it could fetch the
# radar textures. Four points per 2-minute segment is still a curve;
# the eye cannot tell at map size.
SMOOTH_N = int(os.environ.get("TRACK_SMOOTH", "4"))
# How close the newest fix and the icon must be for the fix to be
# replaced by the icon rather than the icon appended after it. At
# 450 kt an aircraft covers 7.5 nm in a minute; 10 nm is generous.
ANCHOR_NM = 10.0

# THE DETECTOR READS A TIME WINDOW, not a fixed count of fixes, so a
# faster tracker makes it better rather than worse. The thresholds
# were swept against simulated holds — 0.5 to 2-minute legs, 230 to
# 300 kt, left and right, eight minutes in — and against the things
# that are not holds: a downwind-base-final turn, a STAR arc, S-turn
# vectors. This set catches every hold after two laps and none of
# the others.
HOLD_WINDOW_S = float(os.environ.get("HOLD_WINDOW_S", "480"))
HOLD_FIXES = int(os.environ.get("HOLD_FIXES", "5"))            # within the window
HOLD_MIN_PATH_NM = float(os.environ.get("HOLD_MIN_PATH_NM", "8"))
HOLD_RATIO = float(os.environ.get("HOLD_RATIO", "1.8"))         # path / spread
HOLD_MIN_TURN_DEG = float(os.environ.get("HOLD_MIN_TURN", "200"))  # same direction
HOLD_CLOSE_NM = float(os.environ.get("HOLD_CLOSE_NM", "1.5"))       # loop closure
# Chord path, not flown path: a two-minute circle sampled at three
# fixes measures as two diameters, 4.7 nm for 7.3 flown. 4 is the
# floor that admits a standard-rate 360 and rejects a wobble.
HOLD_CLOSE_PATH_NM = float(os.environ.get("HOLD_CLOSE_PATH_NM", "4"))

_TRACKS: dict = {}      # hex -> [(lon, lat, ts), ...]
_CS: dict = {}          # hex -> latest callsign, for display
_LOCK = threading.Lock()
_started = False

import re as _re

# ICAO airline designator + flight number: DAL182, JBU1923A, EJA411.
# Excludes N-numbers, generic callsigns like VFR, and blanks. "VFR"
# was a lesson: dozens of GA aircraft squawk it at once, and tracks
# keyed by callsign stitched them into one 720-degree hold.
_COMMERCIAL = _re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]?$")


def is_commercial(cs: str) -> bool:
    return bool(_COMMERCIAL.match((cs or "").strip().upper()))


def note_positions(rows):
    """Append current positions; drop airframes not seen in TTL.

    KEYED BY HEX, the 24-bit ICAO address, which is unique per
    airframe. A callsign is not: flight numbers are reused, and the
    generic ones — VFR, blank — are shared by many aircraft at once.
    The callsign is kept alongside for display only."""
    now = time.time()
    with _LOCK:
        for r in rows or []:
            hx = (r.get("hex") or "").strip().lower()
            cs = (r.get("cs") or r.get("callsign") or "").strip().upper()
            la, lo = r.get("lat"), r.get("lon")
            if not hx or la is None or lo is None:
                continue
            if cs:
                _CS[hx] = cs
            h = _TRACKS.setdefault(hx, [])
            if h and abs(h[-1][0] - lo) < 0.002 and abs(h[-1][1] - la) < 0.002:
                h[-1] = (lo, la, now)
                continue
            if h:
                plo, pla, pts = h[-1]
                dx = (lo - plo) * math.cos(math.radians((la + pla) / 2))
                d = 60.0 * math.hypot(la - pla, dx)
                dt_min = max((now - pts) / 60.0, 1.0)
                if d > max(200.0, 700.0 * dt_min / 60.0):
                    del h[:]
            h.append((lo, la, now))
            if len(h) > TRAIL_MAX:
                del h[:-TRAIL_MAX]
        for hx in [k for k, v in _TRACKS.items()
                   if not v or now - v[-1][2] > TRAIL_TTL_S]:
            _TRACKS.pop(hx, None)
            _CS.pop(hx, None)


def _smooth(pts, n: int = SMOOTH_N):
    """Catmull-Rom spline through the fixes, `n` points per segment.

    Passes THROUGH every fix — a smoothing that moved the points would
    draw the aircraft somewhere it was not. Endpoints are duplicated
    so the first and last segments are curved too."""
    if n <= 1 or len(pts) < 3:
        return list(pts)
    p = [pts[0]] + list(pts) + [pts[-1]]
    out = [pts[0]]
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        for k in range(1, n + 1):
            t = k / n
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    return out


def trail_paths(max_age_s: float = None, anchors: dict = None):
    """SOLID smoothed trails, one path per aircraft, ENDING AT THE
    ICON. `max_age_s` trims each to fixes newer than that.

    `anchors` is {hex: (lon, lat)} of the icons the page is drawing.
    The tracker's newest fix and the page's icon come from different
    fetches up to a minute apart, so one is always slightly ahead of
    the other; a trail that ran to its own newest fix would poke out
    past the icon, or fall short of it, and read as backwards. So the
    trail's last point IS the icon: the newest fix is replaced by the
    anchor when the two are within ANCHOR_NM, and the curve is fitted
    through that.
    """
    cutoff = (time.time() - max_age_s) if max_age_s else None
    with _LOCK:
        snap = {cs: [f for f in h if cutoff is None or f[2] >= cutoff]
                for cs, h in _TRACKS.items()}
    out = []
    for hx, h in snap.items():
        if len(h) < 2:
            continue
        raw = [(lo, la) for lo, la, _ts in h]
        a = (anchors or {}).get(hx)
        if a:
            lo, la = raw[-1]
            k = math.cos(math.radians(la))
            if math.hypot((a[0] - lo) * 60 * k, (a[1] - la) * 60) <= ANCHOR_NM:
                raw[-1] = (a[0], a[1])
            else:
                raw.append((a[0], a[1]))
        pts = _smooth(raw)
        # Four decimals is 11 m — below anything a 1.6 px line can
        # show — and a third shorter than full floats on the wire.
        out.append({"hex": hx, "cs": _CS.get(hx, ""),
                    "path": [[round(x, 4), round(y, 4)] for x, y in pts]})
    return out


def hold_candidates():
    """[(hex, path_nm, disp_nm, turn_deg)] that look to be holding."""
    # COMMERCIAL CALLSIGNS ONLY. A GA aircraft doing pattern work at
    # a county field is a real racetrack and a false alarm here.
    cutoff = time.time() - HOLD_WINDOW_S
    with _LOCK:
        snap = {hx: ([f for f in h if f[2] >= cutoff], _CS.get(hx, ""))
                for hx, h in _TRACKS.items() if is_commercial(_CS.get(hx, ""))}
    out = []
    for hx, (h, cs) in snap.items():
        if len(h) < HOLD_FIXES:
            continue
        pts = [(lo, la) for lo, la, _ts in h]
        k = math.cos(math.radians(pts[0][1]))

        def _nm(a, b):
            return 60.0 * math.hypot(b[1] - a[1], (b[0] - a[0]) * k)

        path = sum(_nm(a, b) for a, b in zip(pts, pts[1:]))
        disp = _nm(pts[0], pts[-1])
        spread = max(_nm(a, b) for a in pts for b in pts)
        turn, prev = 0.0, None
        for a, b in zip(pts, pts[1:]):
            brg = math.degrees(math.atan2((b[0] - a[0]) * k, b[1] - a[1]))
            if prev is not None:
                turn += (brg - prev + 180.0) % 360.0 - 180.0
            prev = brg
        racetrack = (path >= HOLD_MIN_PATH_NM
                     and path >= HOLD_RATIO * max(spread, 1.0)
                     and abs(turn) >= HOLD_MIN_TURN_DEG)
        # LOOP CLOSURE. A single 360 for spacing does not fold the
        # path up, and at one fix a minute a two-minute circle is two
        # chords whose direction cannot be resolved. But an aircraft
        # that comes back to within HOLD_CLOSE_NM of where it was,
        # having flown HOLD_MIN_PATH_NM to get there, went round
        # something — a circle or a hold — whatever the sampling.
        closed = False
        if not racetrack:
            cum = [0.0]
            for a, b in zip(pts, pts[1:]):
                cum.append(cum[-1] + _nm(a, b))
            for i in range(len(pts)):
                for j in range(i + 2, len(pts)):
                    if (cum[j] - cum[i] >= HOLD_CLOSE_PATH_NM
                            and _nm(pts[i], pts[j]) <= HOLD_CLOSE_NM):
                        closed = True
                        break
                if closed:
                    break
        if racetrack or closed:
            out.append((hx, path, disp, abs(turn)))
    return out


# ---------------------------------------------------------------------------
# Hold state: entering, holding, exited
# ---------------------------------------------------------------------------
# hold_candidates() is a snapshot: who looks to be holding NOW. An
# exit is a TRANSITION, so something has to remember who was holding.
#
# HYSTERESIS ON THE WAY OUT. The detector reads the last six fixes,
# so an aircraft one lap into its departure still scores as a hold
# for a beat or two, and at the threshold it can flicker. An exit is
# declared only after HOLD_EXIT_S with no detection — long enough to
# be sure, short enough that the alert arrives while it is useful.
HOLD_EXIT_S = float(os.environ.get("HOLD_EXIT_S", "300"))
HOLD_EXIT_SHOW_S = float(os.environ.get("HOLD_EXIT_SHOW_S", "1200"))
_HOLDING: dict = {}     # cs -> {"since": ts, "last": ts, "path": nm, "turn": deg}
_EXITED: dict = {}      # cs -> {"since": ts, "exited": ts, "held_s": s}


def update_holds():
    """Advance the state machine. Called every beat by the tracker
    and on every page rerun; cheap either way."""
    now = time.time()
    seen = {}
    for cs, path, _disp, turn in hold_candidates():
        seen[cs] = (path, turn)
    with _LOCK:
        for cs, (path, turn) in seen.items():
            st = _HOLDING.get(cs)
            if st is None:
                _HOLDING[cs] = {"since": now, "last": now,
                                "path": path, "turn": turn}
                _EXITED.pop(cs, None)      # re-entered: clear the exit
            else:
                st.update(last=now, path=path, turn=turn)
        for cs in list(_HOLDING):
            if cs in seen:
                continue
            st = _HOLDING[cs]
            if now - st["last"] >= HOLD_EXIT_S:
                _EXITED[cs] = {"since": st["since"], "exited": st["last"],
                               "held_s": st["last"] - st["since"]}
                del _HOLDING[cs]
        for cs in [k for k, v in _EXITED.items()
                   if now - v["exited"] > HOLD_EXIT_SHOW_S]:
            del _EXITED[cs]


def hold_events():
    """(holding, exited) for the page.

    holding: [(callsign, held_min, path_nm, turn_deg, hex)]
    exited:  [(callsign, held_min, exited_min_ago, hex)]
    """
    now = time.time()
    with _LOCK:
        holding = [(_CS.get(hx, hx), (now - v["since"]) / 60.0,
                    v["path"], v["turn"], hx)
                   for hx, v in _HOLDING.items()]
        exited = [(_CS.get(hx, hx), v["held_s"] / 60.0,
                   (now - v["exited"]) / 60.0, hx)
                  for hx, v in _EXITED.items()]
    holding.sort(key=lambda x: -x[1])
    exited.sort(key=lambda x: x[2])
    return holding, exited


def snapshot() -> dict:
    """{hex: [(lon, lat, ts), ...]} — a copy, for consumers that walk
    the history (core/flow.py counts hull crossings from it)."""
    with _LOCK:
        return {hx: list(h) for hx, h in _TRACKS.items()}


def track_count() -> int:
    with _LOCK:
        return sum(1 for v in _TRACKS.values() if len(v) >= 2)


def track_summary() -> dict:
    """Counts by operator prefix and tracker health, for the caption.
    The question "why no Delta trails" has three answers — never
    tracked, tracked with one fix, or tracked and not drawn — and
    this is the row that separates them."""
    import threading as _th

    with _LOCK:
        by = {}
        for hx, v in _TRACKS.items():
            k = (_CS.get(hx) or "???")[:3]
            by.setdefault(k, [0, 0])
            by[k][0] += 1
            if len(v) >= 2:
                by[k][1] += 1
        newest = max((v[-1][2] for v in _TRACKS.values() if v),
                     default=None)
    alive = any(t.name == "track-warmer" for t in _th.enumerate())
    return {"by_operator": by, "tracked": len(_TRACKS),
            "with_trail": sum(b[1] for b in by.values()),
            "thread_alive": alive, "started": _started,
            "newest_fix_age_s": (time.time() - newest) if newest else None}


# ---------------------------------------------------------------------------
# Background feed
# ---------------------------------------------------------------------------
def _fetch_all():
    """Every airborne aircraft with a callsign within 250 nm. Tracks
    are kept for all of them — a Delta in a hold at CAMRN matters as
    much as a JetBlue — and the page decides which trails to DRAW."""
    import requests

    r = requests.get("https://api.adsb.lol/v2/point/40.64/-73.78/250",
                     timeout=10, headers={"User-Agent": "n90 airspace"})
    if r.status_code != 200:
        return []
    out = []
    for p in (r.json() or {}).get("ac") or []:
        cs = (p.get("flight") or "").strip().upper()
        alt = p.get("alt_baro")
        if alt in ("ground", None) or not is_commercial(cs):
            continue
        try:
            out.append({"hex": (p.get("hex") or "").lower(), "cs": cs,
                        "lat": float(p["lat"]), "lon": float(p["lon"])})
        except (TypeError, ValueError, KeyError):
            continue
    return out


_SURFACE_DIR = None


def _daemon():
    time.sleep(float(os.environ.get("TRACK_DELAY_S", "30")))
    # The JFK surface from OpenStreetMap: fetched once, refreshed
    # weekly, on this thread so the page never waits on Overpass.
    if _SURFACE_DIR:
        try:
            from core import surface as _SF

            _SF.refresh_if_stale(_SURFACE_DIR)
        except Exception:
            pass
    _last_surface = time.time()
    while True:
        if _SURFACE_DIR and time.time() - _last_surface > 86400:
            try:
                from core import surface as _SF

                _SF.refresh_if_stale(_SURFACE_DIR)
            except Exception:
                pass
            _last_surface = time.time()
        try:
            note_positions(_fetch_all())
            update_holds()
        except Exception:
            pass
        time.sleep(TRACK_BEAT_S)


def ensure_tracker(surface_dir=None) -> str:
    """Idempotent. TRACKS=off disables. Returns what it did."""
    if os.environ.get("TRACKS", "on").lower() == "off":
        return "DISABLED by TRACKS=off in the environment"
    global _started, _SURFACE_DIR
    if surface_dir:
        _SURFACE_DIR = surface_dir
    with _LOCK:
        if _started:
            return "already running"
        threading.Thread(target=_daemon, daemon=True,
                         name="track-warmer").start()
        _started = True
        return "started"
