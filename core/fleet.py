"""JetBlue fleet positions: one sweep, one store, for every page.

Target path: core/fleet.py

Moved out of pages/3_JBU_Weather_Map.py (21 Sep). A page module cannot
be imported, so while the sweep lived there nothing else could read
it - the login map had no aircraft - and it only ran while someone
had page 3 open. After a quiet spell the first viewer saw positions
from the last visit, trails had gaps, and holds nobody was watching
were never detected.

Now:
  * sweep() is the ADS-B tile sweep, unchanged from page 3 apart from
    the ground-speed fix noted in it.
  * STATE / TRACKS / TURN are plain module globals. A core module is
    imported once per process and stays in sys.modules, so these live
    for the process - the job st.cache_resource did inside the page.
  * ensure_fleet_warmer() runs the sweep every FLEET_SWEEP_S whether
    or not anyone is looking. Same request rate as one viewer leaving
    page 3 open. JBU_FLEET_WARMER=off stops it.
  * login_aircraft() is the public view for the login map: JetBlue
    positions and short trails, NO flight numbers.

Page 3 reads the same objects through thin wrappers, so its holding
detection, trails and map are unchanged.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

_persist = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persist if _persist.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

# Sweep cadence, in seconds. Also the bucket width: no number of
# viewers can push the sweep faster than this.
FLEET_SWEEP_S = int(os.environ.get("JBU_FLEET_SWEEP_S", "120"))
# How long the FIRST render of a process may wait for positions.
FIRST_WAIT_S = float(os.environ.get("JBU_FLEET_FIRST_WAIT", "20"))
# 30 fixes = one hour at the 2-minute sweep. What is STORED; how much
# is DRAWN is each page's choice.
TRAIL_MAX = int(os.environ.get("JBU_TRAIL_POINTS", "30"))
TRAIL_TTL_S = float(os.environ.get("JBU_TRAIL_TTL_S", "7200"))
# Straight flight this long resets the lap counter (holding).
HOLD_CLEAR_S = float(os.environ.get("JBU_HOLD_CLEAR_S", "480"))
# Whether the sweep keeps other airlines' aircraft (page 3's "Other
# airlines" layer). Airline callsigns only: three letters, digits.
KEEP_OTHERS = os.environ.get("JBU_KEEP_OTHERS", "on").lower() != "off"
_AIRLINE_CS = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]?$")

# ---------------------------------------------------------------------------
# Process-lifetime state. Mutate in place; never reassign.
# ---------------------------------------------------------------------------
STATE: dict = {"res": None, "bucket": None, "busy": False,
               "err": None, "tb": None, "at": 0.0}
#: {callsign: [(lon, lat, ts), ...]} - position history, oldest first.
TRACKS: dict = {}
#: {callsign: {"acc": signed degrees, "brg": last bearing, ...}}
TURN: dict = {}
TRACKS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Routes (adsbdb), off the render path
# ---------------------------------------------------------------------------
_route_cache: dict = {}     # cs -> (dest_icao_or_"", expiry_ts)
# v2 schema: {cs: {"d": icao, "oll": [lat,lon], "dll": [lat,lon],
# "exp": ts}}. Renamed from route_cache.json so v1 entries (which
# carried no origin) are simply ignored rather than mis-parsed.
_ROUTE_CACHE_PATH = CACHE_ROOT / "route_cache_v2.json"
try:
    import json as _json_rc
    for _k, _v in _json_rc.loads(
            _ROUTE_CACHE_PATH.read_text()).items():
        if isinstance(_v, dict):
            _route_cache[_k] = _v
except Exception:
    pass


def _route_plausible(alat, alon, trk, oll, dll):
    """Is this aircraft actually flying the route adsbdb claims?

    adsbdb returns the SCHEDULED route for a callsign from
    historical data, not today's flight. Observed 8/17: JBU759
    came back BOS-PHL while the aircraft was en route to Florida.
    That is not a cosmetic error - destination drives the red
    thunderstorm flag, so a wrong route plus a real hazard renders
    a confident, wrong warning on an ops display.

    Two independent checks against the claimed origin-destination
    great circle:
      * cross-track distance - how far off the route line the
        aircraft sits. Generous (120 nm) so reroutes, weather
        deviations and vectoring do not trip it.
      * along-track fraction - >1.25 means well past the claimed
        destination and still going.
    Falls back to bearing-vs-track when origin is unknown.

    Returns (ok, reason). Deliberately fails OPEN on bad input:
    a missing coordinate should not silently strip destinations
    from the whole fleet.
    """
    import math as _m
    R = 3440.065  # nm

    def _rad(d):
        return _m.radians(d)

    def _bearing(la1, lo1, la2, lo2):
        y = _m.sin(_rad(lo2 - lo1)) * _m.cos(_rad(la2))
        x = (_m.cos(_rad(la1)) * _m.sin(_rad(la2))
             - _m.sin(_rad(la1)) * _m.cos(_rad(la2))
             * _m.cos(_rad(lo2 - lo1)))
        return (_m.degrees(_m.atan2(y, x)) + 360.0) % 360.0

    def _dist(la1, lo1, la2, lo2):
        p1, p2 = _rad(la1), _rad(la2)
        dp, dl = _rad(la2 - la1), _rad(lo2 - lo1)
        a = (_m.sin(dp / 2) ** 2
             + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2)
        return 2 * R * _m.asin(min(1.0, _m.sqrt(a)))

    try:
        if not dll:
            return True, ""
        dla, dlo = float(dll[0]), float(dll[1])
        d_to_dest = _dist(alat, alon, dla, dlo)
        if d_to_dest < 150:
            return True, ""      # close in: vectors dominate
        if oll:
            ola, olo = float(oll[0]), float(oll[1])
            d12 = _dist(ola, olo, dla, dlo)
            if d12 < 50:
                return True, ""
            d13 = _dist(ola, olo, alat, alon)
            t13 = _rad(_bearing(ola, olo, alat, alon))
            t12 = _rad(_bearing(ola, olo, dla, dlo))
            xtd = _m.asin(_m.sin(d13 / R) * _m.sin(t13 - t12)) * R
            if abs(xtd) > 120:
                return False, f"{abs(xtd):.0f} nm off route line"
            catd = _m.cos(d13 / R) / max(1e-9, _m.cos(xtd / R))
            atd = _m.acos(max(-1.0, min(1.0, catd))) * R
            if atd / d12 > 1.25:
                return False, "past claimed destination"
            return True, ""
        # no origin: fall back to heading sanity
        if trk is None:
            return True, ""
        brg = _bearing(alat, alon, dla, dlo)
        diff = abs((brg - float(trk) + 180.0) % 360.0 - 180.0)
        if diff > 90:
            return False, f"track {diff:.0f}deg off bearing"
        return True, ""
    except Exception:
        return True, ""          # fail open


def _save_route_cache():
    try:
        import json as _json_rc
        import time as _t_rc
        now = _t_rc.time()
        keep = {k: v for k, v in _route_cache.items()
                if v.get("exp", 0) > now}
        _ROUTE_CACHE_PATH.write_text(_json_rc.dumps(keep))
    except Exception:
        pass


# Route resolution runs OFF the render path. Measured 8/17: the
# adsbdb lookups were sequential inside cached_fleet - up to 70
# callsigns x (latency + 0.05s sleep) = 18-38s that the map sat
# waiting on, every cold start and again whenever the 90s cache
# expired with new callsigns airborne. The tile sweep itself is
# only ~3s.
#
# Destinations only drive the hazard COLOUR, never aircraft
# position, so they do not belong on the critical path. The
# renderer now reads whatever routes are already cached and
# returns immediately; a daemon thread fills the rest in parallel
# and the colours appear on the next 120s beat.
_route_lock = threading.Lock()
_route_busy = {"on": False}


def _resolve_routes_bg(callsigns):
    """Fetch missing routes in the background. Never blocks a
    render; never raises into one."""
    def _work():
        import time as _t
        import requests as _r
        from concurrent.futures import ThreadPoolExecutor
        HDRS = {"User-Agent": "bluemet.org ops dashboard"}
        now_ts = _t.time()

        def _one(cs):
            try:
                r = _r.get(f"https://api.adsbdb.com/v0/callsign/{cs}",
                           headers=HDRS, timeout=5)
            except Exception:
                return cs, None
            d_icao = ""
            oll = dll = None
            if r.status_code == 200:
                try:
                    fr = (r.json().get("response") or {})
                    if isinstance(fr, dict):
                        fr = fr.get("flightroute") or {}
                        de = fr.get("destination") or {}
                        d_icao = (de.get("icao_code") or "").upper()
                        if de.get("latitude") is not None:
                            dll = [float(de["latitude"]),
                                   float(de["longitude"])]
                        og = fr.get("origin") or {}
                        if og.get("latitude") is not None:
                            oll = [float(og["latitude"]),
                                   float(og["longitude"])]
                except Exception:
                    pass
            return cs, {"d": d_icao, "oll": oll, "dll": dll,
                        "exp": now_ts + (6 * 3600 if d_icao else 3600)}
        try:
            # 8 workers: adsbdb is a lookup API, not the rate-limited
            # position feeds, so it tolerates modest concurrency.
            with ThreadPoolExecutor(max_workers=8) as ex:
                for cs, rec in ex.map(_one, callsigns[:120]):
                    if rec is not None:
                        _route_cache[cs] = rec
            _save_route_cache()
        except Exception:
            pass
        finally:
            with _route_lock:
                _route_busy["on"] = False

    with _route_lock:
        if _route_busy["on"] or not callsigns:
            return
        _route_busy["on"] = True
    t = threading.Thread(target=_work, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------
_FLEET_TILES = [
    # PROVEN by route-sampling: 31 JBU great circles
    # sampled pointwise + a 0.4-deg heartland lattice all
    # covered. Base rows + seam rows (mid-country diagonal
    # holes that ate Kansas cruisers) + north tier
    # (Seattle was never covered before) + Bahamas + GA
    # offshore + CA Central Valley + Ontario seam.
    (26.5, -81.5), (26.5, -90.5), (26.5, -99.5),
    (34.0, -118.0), (34.0, -109.0), (34.0, -100.0),
    (34.0, -91.0), (34.0, -82.0), (34.0, -76.0),
    (41.5, -122.0), (41.5, -112.0), (41.5, -102.0),
    (41.5, -92.0), (41.5, -82.0), (41.5, -73.0),
    (30.3, -86.0), (30.3, -95.0), (30.3, -104.0),
    (37.8, -113.5), (37.8, -104.5), (37.8, -95.5),
    (37.8, -86.5), (37.8, -78.5), (44.4, -107.0),
    (44.4, -97.0), (44.4, -87.0), (46.9, -121.5),
    (46.9, -111.5), (46.9, -101.0), (46.9, -90.5),
    (45.8, -69.5), (25.0, -76.5), (30.8, -79.2),
    (37.3, -120.5), (44.8, -77.5),
]


# ---------------------------------------------------------------------------
# Position history
# ---------------------------------------------------------------------------
def note_positions(rows) -> None:
    """Append current positions and drop stale aircraft."""
    with TRACKS_LOCK:
        _note_positions_locked(rows)


def _note_positions_locked(rows):
    """Append current positions and drop stale aircraft."""
    import math
    import time as _t

    now = _t.time()
    for r in rows or []:
        # Fleet rows key the callsign as "callsign"; the OTHER-traffic
        # rows use "cs". This read only "cs", so every fleet row was
        # skipped and TRACKS stayed permanently empty — which is why
        # no track line or dots ever appeared, however many fixes had
        # been collected.
        cs = (r.get("callsign") or r.get("cs") or "").strip().upper()
        la, lo = r.get("lat"), r.get("lon")
        if not cs or la is None or lo is None:
            continue
        h = TRACKS.setdefault(cs, [])
        # Only record real movement. Without this, a parked aircraft
        # accumulates a dozen dots in one spot and reads as a smudge.
        if h and abs(h[-1][0] - lo) < 0.002 and abs(h[-1][1] - la) < 0.002:
            h[-1] = (lo, la, now)
            continue
        # BREAK THE TRACK ON AN IMPOSSIBLE JUMP. A flight number is
        # reused: JBU1534 lands in Boston and a few hours later a
        # different aircraft departs San Francisco as JBU1534. Within
        # the TTL the tracker joined them with one straight line
        # across the country. Nothing flies 200 nm between two
        # sweeps, so a jump that large is a new flight: start over.
        if h:
            _plo, _pla, _pts = h[-1]
            _dx = (lo - _plo) * math.cos(math.radians((la + _pla) / 2))
            _nm = 60.0 * math.hypot(la - _pla, _dx)
            _dt_min = max((now - _pts) / 60.0, 1.0)
            # 200 nm floor, or whatever 700 kt would cover in the
            # elapsed time — generous for a jet, impossible for a
            # callsign that is really two flights.
            if _nm > max(200.0, 700.0 * _dt_min / 60.0):
                del h[:]
                TURN.pop(cs, None)      # new flight: lap count restarts
        # Cumulative turn for the lap counter. Bearing from the last
        # fix to this one; the signed difference from the previous
        # bearing is this leg's turn.
        if h:
            _plo, _pla, _ = h[-1]
            _kk = math.cos(math.radians((la + _pla) / 2))
            _brg = math.degrees(math.atan2((lo - _plo) * _kk, la - _pla))
            _ts = TURN.setdefault(cs, {"acc": 0.0, "brg": None})
            if _ts["brg"] is not None:
                _d = (_brg - _ts["brg"] + 180.0) % 360.0 - 180.0
                # A long straight leg (< 10 deg of turn) after less
                # than half a lap means the turning was a course
                # change, not a hold: forget it. Once past half a
                # lap the count is kept until the flight ends.
                if abs(_d) < 10.0 and abs(_ts["acc"]) < 180.0:
                    _ts["acc"] = 0.0
                else:
                    _ts["acc"] += _d
                if abs(_d) >= 10.0:
                    _ts["last_turn_ts"] = now
                elif now - _ts.get("last_turn_ts", 0.0) > HOLD_CLEAR_S:
                    # Straight for long enough: the hold is over.
                    # Reset so the next one counts from zero.
                    _ts["acc"] = 0.0
            _ts["brg"] = _brg
        h.append((lo, la, now))
        if len(h) > TRAIL_MAX:
            del h[:-TRAIL_MAX]
    # An aircraft that lands or leaves coverage should not leave a
    # trail hanging on the map forever.
    for cs in [k for k, v in TRACKS.items()
               if not v or now - v[-1][2] > TRAIL_TTL_S]:
        TRACKS.pop(cs, None)
        TURN.pop(cs, None)


def trail_paths(max_age_s: float = 1800.0, steps: int = 8):
    """One SMOOTH polyline per aircraft through its recorded fixes.

    Fixes land every ~2 minutes, so a straight polyline through them
    turns a holding racetrack into a diamond and a gentle arc into a
    dogleg. A Catmull-Rom spline passes through every fix exactly and
    draws the plausible curved path between them — which, for an
    aircraft, is much closer to what actually happened than a set of
    corners. Nothing is invented at the fixes themselves; only the
    shape between them is inferred.
    """
    import time as _t

    cutoff = _t.time() - max_age_s
    out = []
    with TRACKS_LOCK:
        items = [(cs, list(h)) for cs, h in TRACKS.items()]
    for cs, h in items:
        # Window by TIME, not by count, so "30 minutes" means thirty
        # minutes even if a sweep was late or missed.
        pts = [(lo, la) for lo, la, ts in h if ts >= cutoff]
        # SHORT WINDOWS. Fixes land once per sweep (~2 min), so a
        # 2-minute window often holds a single fix and would draw
        # nothing - trails flickering on and off each beat. Reach
        # back to the one fix just before the window so there is
        # always a segment from the previous position to now.
        if len(pts) < 2:
            older = [(lo, la) for lo, la, ts in h if ts < cutoff]
            if older and pts:
                pts = older[-1:] + pts
        if len(pts) < 2:
            continue
        if len(pts) == 2:
            out.append({"path": [[x, y] for x, y in pts], "cs": cs})
            continue
        # Pad the ends so the first and last segments get a tangent.
        P = [pts[0]] + pts + [pts[-1]]
        path = [list(pts[0])]
        for i in range(1, len(P) - 2):
            p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
            for k in range(1, steps + 1):
                t = k / steps
                t2, t3 = t * t, t * t * t
                x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                           + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                           + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
                y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                           + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                           + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
                path.append([x, y])
        out.append({"path": path, "cs": cs})
    return out


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------
def sweep():
    """All airborne JBU over CONUS.

    Field-measured reality (8/13 verdicts): airplanes.live 403s
    every request from Render - dropped entirely. adsb.lol and
    adsb.fi both 429 under burst load - so each gets ONE paced
    sequential lane (~0.45s between calls, backoff-retry on 429),
    with cross-host retry for stragglers. Slower (~5s cold, 90s
    cached) but built to finish 15/15."""
    import threading
    import time as _time

    import requests as _rq

    HDRS = {"User-Agent": "bluemet.org ops dashboard"}

    def _url(host, la, lo):
        if host == "adsb.lol":
            return (f"https://api.adsb.lol/v2/point/"
                    f"{la:.2f}/{lo:.2f}/246")
        return (f"https://opendata.adsb.fi/api/v2/lat/"
                f"{la:.2f}/lon/{lo:.2f}/dist/246")

    tile_stats: list = []

    def _call(host, tile):
        la, lo = tile
        try:
            r = _rq.get(_url(host, la, lo), headers=HDRS,
                        timeout=5)
        except Exception as e:
            return None, f"{host}:{type(e).__name__}"
        if r.status_code != 200:
            return None, f"{host}:HTTP{r.status_code}"
        j = r.json()
        # adsb.fi proved capable of HTTP 200 with a different (or
        # empty) payload shape - accept both common keys
        ac = j.get("ac") or j.get("aircraft") or []
        out = []
        for p in ac:
            # Strip AND upper-case. ADS-B callsigns are space
            # padded to eight characters and hosts differ on whether
            # they trim them, so "JBU2582 " and "JBU2582" arrive from
            # different tiles as different strings that display
            # identically. Normalising at the source is what stops
            # the same aircraft being counted twice everywhere
            # downstream.
            cs = (p.get("flight") or "").strip().upper()
            # Other operators are kept now rather than dropped. The
            # sweep already paid for them — they arrive in the same
            # payload — so carrying them costs nothing but a tag, and
            # a JBU aircraft is far easier to read when you can see
            # the traffic it is flying among.
            mine = cs.upper().startswith("JBU")
            if p.get("lat") is None:
                continue
            if not mine:
                if not KEEP_OTHERS:
                    continue
                # Airline callsign shape: three letters then digits.
                # Drops N-numbers, military and most GA at the
                # cheapest possible point.
                if not _AIRLINE_CS.match(cs):
                    continue
            alt = p.get("alt_baro")
            trk = p.get("track")
            gs = p.get("gs")
            out.append((cs, float(p["lat"]), float(p["lon"]),
                        alt if isinstance(alt, (int, float))
                        else None,
                        float(trk) if isinstance(
                            trk, (int, float)) else 0.0,
                        float(gs) if isinstance(
                            gs, (int, float)) else None,
                        mine))
        tile_stats.append(
            f"({la:.0f},{lo:.0f}) {host}: {len(ac)} ac, "
            f"{sum(1 for r in out if r[6])} JBU"
        )
        return out, ("EMPTY200" if not ac else None)

    def _lane(host, tiles, results, leftovers, empties):
        for tile in tiles:
            res, err = _call(host, tile)
            if res is None and "429" in (err or ""):
                # 4 s, not 1.3. A rate limiter that just refused is
                # still refusing a second later; the short retry
                # mostly burned a second request against the same
                # limit and then failed over to the other host,
                # which was busy with its own lane.
                _time.sleep(4.0)
                res, err = _call(host, tile)
            if res is None:
                leftovers.append((tile, err))
            else:
                results.append(res)
                if err == "EMPTY200":
                    empties.append(tile)
            _time.sleep(0.25)

    hosts = ("adsb.lol", "adsb.fi")
    lanes = {h: [t for i, t in enumerate(_FLEET_TILES)
                 if i % 2 == k] for k, h in enumerate(hosts)}
    results: list = []
    leftovers: dict = {h: [] for h in hosts}
    empties: dict = {h: [] for h in hosts}
    # Two paced sub-lanes per host (4 workers total): each worker
    # keeps the 0.25s inter-call pacing, so per-host request rate
    # stays modest while wall time halves
    threads = [
        threading.Thread(target=_lane,
                         args=(h, lanes[h][k::2], results,
                               leftovers[h], empties[h]))
        for h in hosts for k in (0, 1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Self-healing: a host answering 200-but-zero-aircraft on 3+
    # tiles is defective (measured 8/13: adsb.fi did this on ALL
    # 17 of its tiles - half the country silently blank). Its
    # empty tiles re-run on the other host, paced.
    for h in hosts:
        if len(empties[h]) >= 3:
            other = hosts[1] if h == hosts[0] else hosts[0]
            for tile in empties[h][:6]:   # bounded: a fully
                # defective host used to add 17 serial retries
                # (~11s) to every cold start
                _time.sleep(0.25)
                res, err = _call(other, tile)
                if res is not None and err != "EMPTY200":
                    results.append(res)

    # Stragglers: one paced retry on the OTHER host
    fails = []
    for h in hosts:
        other = hosts[1] if h == hosts[0] else hosts[0]
        for tile, err1 in leftovers[h][:6]:
            _time.sleep(0.25)
            res, err2 = _call(other, tile)
            if res is None:
                fails.append(
                    f"({tile[0]:.0f},{tile[1]:.0f}) "
                    f"{err1} -> {err2}"
                )
            else:
                results.append(res)

    # Ground speed is carried per aircraft now. Page 3 stored only
    # (lat, lon, alt, trk) here and then wrote "gs": gs from the loop
    # variable, so every aircraft got the LAST row's ground speed.
    seen = {}
    others = {}
    for res in results:
        for cs, la, lo, alt, trk, gs, mine in res:
            tgt = seen if mine else others
            if cs not in tgt and cs not in seen:
                tgt[cs] = (la, lo, alt, trk, gs)
    # Route lookups and the hazard ladder run on JBU only: `seen` is
    # the fleet. `others` is context and never drives a colour, an
    # alert or an API call — it is drawn and nothing more. That keeps
    # the adsbdb load unchanged and stops a hundred foreign callsigns
    # entering the route cache.

    # Destinations: one routeset POST for the whole fleet. The
    # parser is shape-defensive (public schema: _airports list of
    # {icao,...}; fallback: "airport_codes" like "JFK-MCO", IATA
    # mapped K+code for CONUS). A flight that can't resolve simply
    # has no dest - never an error.
    dests = {}
    rs_diag = []
    now_ts = _time.time()
    new_cs = [cs for cs in seen
              if cs not in _route_cache
              or _route_cache[cs].get("exp", 0) < now_ts]
    # Non-blocking: read what is cached, kick the rest to a
    # background thread, return now.
    _resolve_routes_bg(new_cs)
    _suppressed = []
    for cs in seen:
        cached = _route_cache.get(cs) or {}
        if not cached.get("d"):
            continue
        _la, _lo, _alt, _trk, _gs = seen[cs]
        ok, why = _route_plausible(_la, _lo, _trk,
                                   cached.get("oll"),
                                   cached.get("dll"))
        if ok:
            dests[cs] = cached["d"]
        else:
            # Drop the destination entirely rather than colour on
            # it. No flag beats a confidently wrong flag.
            _suppressed.append(f"{cs}->{cached['d']} ({why})")
    if _suppressed:
        rs_diag.append(
            "route sanity: dropped "
            + "; ".join(_suppressed[:8])
            + (f" (+{len(_suppressed) - 8} more)"
               if len(_suppressed) > 8 else ""))
    rs_diag.append(
        f"adsbdb: {len(new_cs)} routes resolving in background, "
        f"cache holds "
        f"{sum(1 for v in _route_cache.values() if v.get('d'))} "
        f"routes (destinations appear on the next beat)"
    )

    out = []
    for cs, (la, lo, alt, trk, gs) in seen.items():
        alt_s = (f"FL{int(alt // 100):03d}"
                 if alt and alt >= 18000
                 else (f"{int(alt):,} ft" if alt else "alt n/a"))
        out.append({
            "callsign": cs, "lat": la, "lon": lo,
            "dest": dests.get(cs, ""),
            # deck.gl IconLayer angle is CCW; heading is CW from N
            "angle": (360.0 - trk) % 360.0,
            "gs": gs,
            "alt": alt,
            "tip": f"{cs} | {alt_s}",
        })
    ok = len(_FLEET_TILES) - len(fails)
    out_other = [{"cs": cs, "lat": v[0], "lon": v[1],
                  "alt": v[2], "trk": v[3]}
                 for cs, v in others.items()]
    return (out, ok, len(_FLEET_TILES), fails, tile_stats, rs_diag,
            out_other)


# ---------------------------------------------------------------------------
# Refresh, readers, warmer
# ---------------------------------------------------------------------------
def bucket() -> str:
    """Key that changes once per FLEET_SWEEP_S."""
    from datetime import datetime, timezone

    n = datetime.now(timezone.utc)
    slot = (n.hour * 3600 + n.minute * 60 + n.second) // FLEET_SWEEP_S
    return n.strftime("%Y%m%d") + f"-{slot:04d}"


_refresh_lock = threading.Lock()


def refresh(b: str | None = None) -> None:
    """One sweep for bucket b, unless that bucket is done or running."""
    b = b or bucket()
    with _refresh_lock:
        if STATE["busy"] or STATE["bucket"] == b:
            return
        STATE["busy"] = True
    try:
        res = sweep()
        STATE.update({"res": res, "bucket": b, "err": None, "tb": None,
                      "at": time.time()})
        try:
            note_positions(res[0] if res else [])
        except Exception:
            pass          # trails are decoration; never break the fleet
    except Exception as exc:
        import traceback

        STATE.update({"err": f"{type(exc).__name__}: {exc}",
                      "tb": traceback.format_exc()})
    finally:
        STATE["busy"] = False


def kick(b: str | None = None) -> None:
    """Start a refresh in the background if this bucket needs one."""
    b = b or bucket()
    if STATE["bucket"] != b and not STATE["busy"]:
        threading.Thread(target=refresh, args=(b,), daemon=True,
                         name="fleet-sweep").start()


def fleet_now(b: str | None = None, first_wait_s: float = FIRST_WAIT_S):
    """(result, err, tb). Returns the last good sweep at once and kicks
    a refresh if it is stale. Waits only when there is no result yet
    in this process, and then at most first_wait_s."""
    kick(b)
    if STATE["res"] is None:
        deadline = time.time() + first_wait_s
        while (STATE["res"] is None and STATE["err"] is None
               and time.time() < deadline):
            time.sleep(0.25)
    return STATE["res"], STATE["err"], STATE["tb"]


_warm = {"started": False}
_warm_lock = threading.Lock()


def _warmer_loop() -> None:
    while True:
        try:
            refresh(bucket())
        except Exception:
            pass
        # Checks often, sweeps once per bucket: refresh() is a no-op
        # until the bucket turns over.
        time.sleep(15)


def ensure_fleet_warmer() -> bool:
    """Idempotent. Keeps the fleet current with nobody watching.
    JBU_FLEET_WARMER=off disables it without a deploy."""
    if os.environ.get("JBU_FLEET_WARMER", "on").lower() == "off":
        return False
    with _warm_lock:
        if not _warm["started"]:
            threading.Thread(target=_warmer_loop, daemon=True,
                             name="fleet-warmer").start()
            _warm["started"] = True
    return True


def login_aircraft(trail_s: float = 120.0,
                   max_age_s: float = 600.0) -> list:
    """JetBlue aircraft for the public login map.

    [{"lon", "lat", "angle", "trail": [[lon, lat], ...]}, ...]. No
    callsign, destination or altitude leaves this function: the login
    page is public. Empty when the last sweep is older than max_age_s,
    so a stalled sweep shows no aircraft rather than old ones.
    """
    res = STATE.get("res")
    if not res or time.time() - STATE.get("at", 0.0) > max_age_s:
        return []
    rows = res[0] or []
    trails = {t["cs"]: t["path"] for t in trail_paths(max_age_s=trail_s)}
    out = []
    for r in rows:
        cs = (r.get("callsign") or "").strip().upper()
        try:
            lon, lat = float(r["lon"]), float(r["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"lon": lon, "lat": lat,
                    "angle": float(r.get("angle") or 0.0),
                    "trail": trails.get(cs, [])})
    return out
