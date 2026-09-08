"""N90 — New York TRACON airspace.

The live picture of the New York terminal area: navigation fixes with
role colouring, an approximate N90 extent, Class B shelves, ARTCC
boundaries, reference airports, live traffic, and winds aloft derived
from the aircraft themselves.

WEATHER IS NOT HERE YET, deliberately. Radar and model overlays are
being rebuilt from scratch rather than carried over, so this page is
mapping and traffic only. Nothing on it imports numpy, matplotlib,
cartopy or pyart, and that is the property to protect: the whole page
is a few hundred milliseconds of Streamlit, pydeck and one HTTP call.
Adding radar back should be a deliberate decision about what that
costs, not a drift.

DELIBERATELY NOT BUILT YET, and why — each needs a source decision
before any code is worth writing:

  * Commonly used routes. Needs a source for the actual route
    strings (preferred routes / CDR database, or JetBlue's own
    filed-route history), plus fix-by-fix expansion. The fix
    coordinates to draw them are already here.
  * CWSU / SWAP / TMI. There is no single documented CWSU API. The
    candidates are ATCSCC advisories, NAS Status, and the ZNY CWSU
    OIS page — different formats, different refresh rates, and
    scraping an OIS page is a fragile dependency for an ops tool.
    Worth an explicit decision rather than a guess.
"""

from pathlib import Path

import streamlit as st

st.set_page_config(page_title="N90 Airspace", layout="wide")

# ORDER MATTERS, and it is the same on every page: set_page_config
# first (Streamlit requires it before any other st call), then the
# theme, then auth. Pages 12 and 13 previously ran check_password
# BEFORE set_page_config and never applied the theme at all, so
# navigating here from a themed page visibly switched styling and
# the sidebar lost its nav group captions.
from retro_theme import apply_retro_theme

apply_retro_theme()

# FIRST ACTION ON EVERY RERUN. The overlay warmer checks this
# timestamp before each frame and stands down while the site is in
# use, so a person scrubbing holds the warmer off rather than
# competing with it for the GIL.
try:
    from core.cam_overlay import note_request as _note_req

    _note_req()
except Exception:
    pass

from auth import check_password

check_password()



# N90 centroid — between JFK and the KOKX radar, so the terminal area
# sits mid-frame rather than at an edge.
N90_CENTER = (40.90, -73.60)
_STATIC = Path(__file__).resolve().parent.parent / "static"

# How far back the radar scrubber can reach. Bounded by MRMS_KEEP in
# core/mrms.py, not by this: the warmer prunes older scans off disk,
# so raising this alone changes nothing.
LOOP_HOURS = 2.0

# Airport reference points, for orientation only. Majors plus the N90
# satellites plus the ring of fields that define the 300 nm view.
AIRPORTS = {
    "KJFK": (40.6398, -73.7789, "core"),
    "KLGA": (40.7772, -73.8726, "core"),
    "KEWR": (40.6925, -74.1687, "core"),
    "KTEB": (40.8501, -74.0608, "sat"),
    "KHPN": (41.0670, -73.7076, "sat"),
    "KISP": (40.7952, -73.1002, "sat"),
    "KFRG": (40.7288, -73.4134, "sat"),
    "KSWF": (41.5041, -74.1048, "sat"),
    "KPHL": (39.8721, -75.2411, "ring"),
    "KBDL": (41.9389, -72.6832, "ring"),
    "KPVD": (41.7240, -71.4283, "ring"),
    "KBOS": (42.3630, -71.0064, "ring"),
    "KALB": (42.7483, -73.8017, "ring"),
    "KBUF": (42.9405, -78.7322, "ring"),
    "KPWM": (43.6462, -70.3093, "ring"),
    "KDCA": (38.8521, -77.0377, "ring"),
    "KBWI": (39.1754, -76.6683, "ring"),
    "KIAD": (38.9445, -77.4558, "ring"),
    "KACY": (39.4576, -74.5772, "ring"),
    "KMDT": (40.1935, -76.7634, "ring"),
    "KRDU": (35.8776, -78.7875, "ring"),
    "KORF": (36.8946, -76.2012, "ring"),
}

# WHAT THE MAP DRAWS is a different, shorter list. AIRPORTS above is
# every field the ramp-declutter needs to know about; trimming it
# would let parked aircraft at PHL and TEB back onto the map. The
# drawn set is JetBlue's stations in the region, and nothing else —
# a dot at PWM or MDT is basemap noise on a JetBlue display.
JBU_CITIES = ("KJFK", "KBOS", "KDCA", "KALB", "KBUF", "KBDL", "KRDU",
              "KORF")


# The exact A320 icon page 3 uses — same path data, same #005ADC
# blue, same 64x64 centre anchor. Copied rather than imported because
# page filenames start with digits and are not importable as modules;
# if this ever diverges from page 3 the two maps will disagree about
# what a JetBlue aircraft looks like, so keep them in step.
# Icons, operator colours and names live in core/icons.py so every map
# draws the same aircraft. See that module for the silhouettes and the
# per-airline style table.
from core.icons import (  # noqa: E402
    _WIDE_QUAD, _body_class, _OPERATOR_STYLE, _OPERATOR_FILL,
    _OPERATOR_STROKE, _OTHER_FILL, _tcol, _who, _icon,
    atc_icon, atc_color, has_block, data_block,
    set_static_dir as _set_icon_dir,
)

_set_icon_dir(_STATIC)


# Everyone else: outline only, no fill. Reads as a silhouette
# rather than a solid, so a few hundred of them do not compete
# with the fleet or hide the basemap. stroke_w is in viewBox
# units: the box is 22 units rendered at 64 px, so 0.9 units
# is about 2.6 px on screen.
# JETBLUE IS ONE BLUE. The green/red crossing fills were retired
# once the other majors got brand colours: with Delta red and United
# navy on the map, a red JetBlue leaving N90 stopped being
# unambiguous. Crossing state lives in the tooltip ("entering N90
# ~6min") and the caption counts; the fill says operator, only.

# ICON SIZING LIVES HERE, IN ONE PLACE, AS A RATIO.
#
# Do not adjust get_size in the layer block. Sizing has been changed
# by three separate instructions now — "double the fleet", "halve the
# others", "make JetBlue 1.5x larger" — and the first two compounded
# to a 7x ratio before anyone noticed. Every one of them was really a
# statement about the RATIO between the two layers, so that is what
# is written down: change FLEET_RATIO, not the pixel numbers.
#
# Other traffic is the baseline because it is the crowded layer: at
# 250 nm there are a few thousand of them, and subordinate has to
# mean SMALL, not merely paler.
OTHER_SIZE = 10
OTHER_PX = (6, 13)
FLEET_RATIO_DEFAULT = 1.0  # fixed; the slider is gone
_FLEET_RATIO_UNUSED = 3.0    # was 2.0, then x1.5 applied once, here


def fleet_sizing(ratio):
    """(size, min_px, max_px) for the fleet layer at a given ratio."""
    return (OTHER_SIZE * ratio,
            round(OTHER_PX[0] * ratio),
            round(OTHER_PX[1] * ratio))

# How far ahead to look when deciding "entering" or "leaving". Six
# minutes at typical terminal groundspeeds is 20-35 nm, far enough to
# be useful and short enough that a turn does not make it a lie.
CROSS_LOOKAHEAD_MIN = 6

# The three primaries only. JetBlue's operation is JFK-centred; LGA
# and EWR are included because a controller's picture of "arriving
# the metro" does not stop at one airport's fence.
NY_METRO = {"JFK", "LGA", "EWR", "KJFK", "KLGA", "KEWR"}

# Live-state fallback, used when the route lookup has nothing. An
# aircraft counts as inbound if it is inside ARR_MAX_NM, below
# ARR_MAX_ALT_FT, and its track points within ARR_CONE_DEG of the
# bearing to the metro. This is inference, but from what the aircraft
# is doing NOW, so it stays right during diversions and reroutes —
# exactly when the historical route lookup is wrong.
ARR_MAX_NM = 250.0
ARR_MAX_ALT_FT = 26000
ARR_CONE_DEG = 55.0


# ---------------------------------------------------------------------------
# JetBlue traffic in the terminal area
# ---------------------------------------------------------------------------
# A single bounded point query rather than page 3's 17-tile CONUS
# sweep: 40 nm is one small circle, so one call covers it. That also
# keeps this page independent of page 3 — nothing is imported across
# pages, so a change to the fleet map cannot break the airspace map.
# Same two hosts and the same User-Agent as page 3, so we stay one
# well-behaved client rather than two.
# The traffic query FOLLOWS THE VIEW. It used to be a fixed 40 nm
# while the map opened at 300, so anything beyond 40 nm was never
# requested — not filtered, not decluttered, simply never asked for.
# On a display whose whole point is the terminal area that read as
# "JetBlue aircraft are missing".
#
# /v2/point is capped at 250 nm by the API, so a 300 or 400 nm view
# still tops out there; the caption says so rather than pretending
# the edge of the map is covered.
TRAFFIC_MAX_NM = 250
# Ramp declutter. Every airport carries a permanent pile of parked
# aircraft — a hundred at JFK alone — which swamps the terminal area
# and hides the traffic that matters. Rule: inside DECLUTTER_SM of an
# airport, if more than DECLUTTER_MIN aircraft are present, drop the
# ones that are not moving.
#
# Only the STATIONARY ones. Suppressing everything inside 10 sm would
# also delete aircraft on final and on climbout, which is exactly the
# traffic an airspace page exists to show — a parked A320 and one at
# 800 ft on approach are both "within 10 sm of JFK" and only one of
# them is clutter.
DECLUTTER_SM = 10.0
DECLUTTER_MIN = 10
DECLUTTER_ALT_FT = 1200      # at or below this, and slow, = parked
DECLUTTER_GS_KT = 40


def _declutter(rows):
    """Drop stationary aircraft in crowded airport circles.

    Returns (kept, n_hidden). Airborne traffic is never touched.
    """
    import math

    if not rows:
        return rows, 0
    sm = DECLUTTER_SM * 1609.34
    hidden = set()
    for icao, (ala, alo, _k) in AIRPORTS.items():
        near = []
        for i, r in enumerate(rows):
            dy = (r["lat"] - ala) * 111320.0
            dx = ((r["lon"] - alo) * 111320.0
                  * math.cos(math.radians(ala)))
            if math.hypot(dx, dy) <= sm:
                near.append(i)
        if len(near) <= DECLUTTER_MIN:
            continue
        for i in near:
            r = rows[i]
            alt = r.get("alt")
            parked = ((alt is None or alt <= DECLUTTER_ALT_FT)
                      and (r.get("gs") or 0) <= DECLUTTER_GS_KT)
            if parked:
                hidden.add(i)
    if not hidden:
        return rows, 0
    return [r for i, r in enumerate(rows) if i not in hidden], len(hidden)
# ICAO three-letter operator designators -> plain name, for tooltips.
#
# A STATIC TABLE ON PURPOSE. The alternative is another network call
# per page load to resolve something that is already sitting in the
# callsign, and this map already leans on three external feeds.
#
# It will drift: airlines merge, codes get reassigned, regionals lose
# contracts. That is handled by DEGRADING, not by guessing — an
# unknown prefix falls back to showing the prefix itself, so a wrong
# entry is the only way to mislead, and an absent entry never is.
# Scoped to what actually shows up in New York airspace rather than
# every carrier worldwide.
from core.icons import _OPERATORS  # noqa: E402


def _operator(cs: str) -> str:
    """Plain-language operator for a callsign, or a useful fallback.

    Never returns an empty string on a non-empty callsign: an unknown
    airline prefix reads as 'ICAO XXX' so the tooltip is still worth
    hovering, and an N-number reads as general aviation rather than
    being mistaken for a missing lookup.
    """
    cs = (cs or "").strip().upper()
    if not cs:
        return ""
    pre = cs[:3]
    if pre in _OPERATORS:
        return _OPERATORS[pre]
    # N-numbers: general aviation, business jets, anything not filing
    # under an airline designator. N123AB, not a three-letter code.
    if cs.startswith("N") and any(c.isdigit() for c in cs[1:3]):
        return "general aviation"
    if len(cs) >= 3 and pre.isalpha():
        return f"ICAO {pre}"
    return ""


def _is_ga(cs: str) -> bool:
    """True for aircraft that should not be on this display.

    N-numbered traffic and targets with no callsign at all. Both are
    unidentifiable on an airspace picture: a bare registration says
    nothing about who is flying or where, and at 250 nm they are the
    bulk of the returns. Airline prefixes NOT in _OPERATORS are KEPT —
    an unrecognised regional is a gap in the table, not general
    aviation, and dropping it would quietly hide real airline traffic.
    """
    cs = (cs or "").strip().upper()
    if not cs:
        return True
    if cs[:3] in _OPERATORS:
        return False
    return cs.startswith("N") and any(c.isdigit() for c in cs[1:3])


def _fl(alt):
    """Altitude the way it is spoken.

    FL only at or above the transition altitude. Writing FL045 for an
    aircraft at 4,500 ft would be wrong, and most traffic on a
    terminal display is below FL180.
    """
    if alt is None:
        return "GND"
    try:
        a = int(alt)
    except (TypeError, ValueError):
        return "?"
    return f"FL{round(a / 100):03d}" if a >= 18000 else f"{a:,} ft"


def _mk_row(p, cs, mine):
    """One aircraft dict, shared by the local query and the regional
    sweep so the two can never drift apart. Returns None if the
    position is unusable."""
    try:
        alat, alon = float(p["lat"]), float(p["lon"])
    except Exception:
        return None
    alt = p.get("alt_baro")
    alt = None if alt in ("ground", None) else alt
    try:
        gs = float(p.get("gs") or 0.0)
    except Exception:
        gs = 0.0
    _body = _body_class(p.get("t"))
    _wide = _body != "narrow"
    return {
        "lat": alat, "lon": alon,
        # ICAO 24-bit address: the only stable key for deduping a
        # local row against the same aircraft in the regional sweep.
        "hex": (p.get("hex") or "").lower(),
        # Real callsign, not the IATA form. A controller reads
        # JBU1234 off the strip, not B61234.
        "cs": cs,
        # Mode S enhanced surveillance, for wind derivation. Present
        # on a subset of aircraft only — core.adsb_wind counts what it
        # could and could not use rather than letting a low yield look
        # like a broken calculation.
        "tas": p.get("tas"),
        "true_heading": p.get("true_heading"),
        "mag_heading": p.get("mag_heading"),
        "roll": p.get("roll"),
        "oat": p.get("oat"),
        "trk": p.get("track"),
        # ICAO type designator, e.g. B738, A20N.
        "typ": (p.get("t") or "").strip().upper(),
        # JetBlue and the majors are SOLID in their brand colour with
        # the same white edge; everyone else is a small black outline.
        "icon": (_icon(_OPERATOR_STYLE[cs[:3]][1], _OPERATOR_STYLE[cs[:3]][2],
                       _OPERATOR_STYLE[cs[:3]][3], _body,
                       _OPERATOR_STYLE[cs[:3]][0])
                 if cs[:3] in _OPERATOR_STYLE else
                 _icon(_OTHER_FILL, "#FFFFFF", 0.5, _body, "A")),
        # Wide twins draw 30% larger, quads 40%, on top of the broader
        # outline. IconLayer takes size per row, so this rides the
        # existing sizing without a second layer.
        "isize": (1.4 if (p.get("t") or "").strip().upper() in _WIDE_QUAD
                  else 1.3 if _body == "wide" else 1.0),
        "wide": _wide,
        "body": _body,
        "alt": alt,
        "gs": gs,
        # deck.gl IconLayer angle is CCW; heading is CW from north, so
        # it has to be flipped.
        "angle": (360.0 - float(p.get("track") or 0.0)) % 360.0,
        # Pipe-separated, fixed order: who | how high | how fast |
        # what. Fields that are absent are dropped rather than
        # rendered as a placeholder, so a thin tooltip means thin
        # data and not a broken lookup.
        # Plain text only: "American 605 | 8,050 ft | 286 kt | B738".
        # Operator name and flight number rather than the raw
        # callsign, since the prefix is now carried by colour.
        "tip": " | ".join(x for x in (
            _who(cs),
            _fl(alt),
            f"{gs:.0f} kt",
            (p.get("t") or "").strip().upper(),
        ) if x),
        "tcol": _tcol(cs)[0],
        "ttxt": _tcol(cs)[1],
        # STARS: a tinted square, a leader and a two-line data block
        # for JetBlue and the majors, the square alone for the rest.
        "atc": atc_icon(has_block(cs)),
        "acol": atc_color(cs),
        "block": (data_block(cs, alt, p.get("gs")) if has_block(cs) else ""),
        # Emergency squawks. 7500 unlawful interference, 7600 radio
        # failure, 7700 emergency; the feed also carries the Mode S
        # emergency state as a word.
        # Vertical rate, ft/min. Sign is the whole story for flow:
        # descending in N90 is an arrival, climbing is a departure.
        "vr": (float(p.get("baro_rate")) if p.get("baro_rate")
               is not None else None),
        "sq": (str(p.get("squawk") or "").strip()),
        "emerg": (str(p.get("emergency") or "none").strip().lower()),
        "_alt": alt if alt is not None else 0,
        "_gs": gs,
        # Kept for _inbound(); "trk" above is the Mode S field
        # consumed by adsb_wind.
        "_trk": p.get("track"),
        # Default label colour; mark_arrivals() overrides it.
        "lcolor": [0, 40, 120],
    }


@st.cache_data(ttl=300, show_spinner=False)
def route_dests(planes: tuple, _bucket: str) -> dict:
    """callsign -> {"orig", "dest"} from adsb.lol's route data.

    IMPORTANT: these routes are PLAUSIBLE, not filed — inferred from
    what a flight number historically flies. Right on a normal day,
    wrong during exactly the irregular operations a New York terminal
    display exists for. Treat as a placeholder for an authoritative
    internal feed; _inbound() and the near-and-low rule cover the
    gaps from live state.

    Now returns BOTH ends, and is called for every callsign on the
    screen rather than JetBlue only, so it is chunked: several
    hundred callsigns in one POST is more than the endpoint should
    be asked to take at once. Never raises.
    """
    if not planes:
        return {}
    import requests

    out = {}
    CHUNK = 150
    for k in range(0, len(planes), CHUNK):
        part = planes[k:k + CHUNK]
        body = {"planes": [{"callsign": c, "lat": la, "lng": lo}
                           for c, la, lo in part]}
        try:
            r = requests.post("https://api.adsb.lol/api/0/routeset",
                              json=body, timeout=8,
                              headers={"User-Agent": "n90 airspace"})
            if r.status_code != 200:
                continue
            payload = r.json()
        except Exception:
            continue
        rows = (payload if isinstance(payload, list)
                else (payload or {}).get("planes") or [])
        for i, item in enumerate(rows):
            if not isinstance(item, dict):
                continue
            cs = (item.get("callsign") or "").strip().upper()
            if not cs and i < len(part):
                cs = part[i][0]
            orig = dest = None
            pair = (item.get("_airport_codes_iata")
                    or item.get("airport_codes"))
            if isinstance(pair, str) and "-" in pair:
                bits = [b.strip().upper() for b in pair.split("-")]
                orig, dest = bits[0], bits[-1]
            if dest is None:
                aps = item.get("_airports") or item.get("airports")
                if isinstance(aps, list) and aps:
                    def _code(a):
                        return ((a.get("iata") or a.get("icao") or "")
                                .strip().upper() or None) \
                            if isinstance(a, dict) else None
                    orig, dest = _code(aps[0]), _code(aps[-1])
            if cs and (orig or dest):
                out[cs] = {"orig": orig, "dest": dest}
    return out


def _inbound(row) -> bool:
    """Live-state guess at 'arriving the New York metro'.

    Inside ARR_MAX_NM, below ARR_MAX_ALT_FT, and tracking within
    ARR_CONE_DEG of the bearing to the metro centre. Deliberately
    independent of the route lookup, so a diversion still reads
    correctly.
    """
    import math

    alt = row.get("_alt") or 0
    trk = row.get("_trk")
    if alt <= 0 or alt > ARR_MAX_ALT_FT or trk is None:
        return False
    la, lo = N90_CENTER
    dx = (row["lon"] - lo) * 60.0 * math.cos(math.radians(la))
    dy = (row["lat"] - la) * 60.0
    dist = math.hypot(dx, dy)
    if dist > ARR_MAX_NM or dist < 1.0:
        return False
    # Bearing FROM the aircraft TO the metro centre.
    brg = math.degrees(math.atan2(-dx, -dy)) % 360.0
    diff = abs((float(trk) - brg + 180.0) % 360.0 - 180.0)
    return diff <= ARR_CONE_DEG


def _pip(lon, lat, poly):
    """Point in polygon, ray casting. Plain lon/lat is fine at this
    scale — the hull spans about 3 degrees."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > lat) != (yj > lat)) and \
                (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _ahead(row, minutes):
    """Where this aircraft will be in `minutes`, on its current track.

    Distance scales with groundspeed and is clamped: a stationary
    aircraft would otherwise project nowhere and a fast one would
    project across two centres.
    """
    import math

    gs = float(row.get("_gs") or 0.0)
    trk = row.get("_trk")
    if trk is None:
        return None
    d = max(5.0, min(60.0, gs * minutes / 60.0))
    b = math.radians(float(trk))
    dlat = d / 60.0 * math.cos(b)
    clat = math.cos(math.radians(row["lat"])) or 1e-9
    dlon = d / 60.0 * math.sin(b) / clat
    return row["lon"] + dlon, row["lat"] + dlat


def mark_crossings(jbu, hull, minutes=CROSS_LOOKAHEAD_MIN):
    """Outline JetBlue aircraft that are about to cross the N90 edge.

    Green entering, red exiting, white otherwise. Decided by testing
    the aircraft's position now and its projected position against the
    hull, so it works on the hull's real shape rather than a distance
    from one centre point.

    THE HULL IS APPROXIMATE. It is the convex hull of the coordination
    fixes, not the delegated TRACON boundary — that geometry is not
    published in FAA open GIS. An aircraft near the edge may be called
    wrong. The page caption says the same thing about the orange
    outline, and this colouring inherits every bit of that caveat.
    """
    n_in = n_out = 0
    if not hull:
        return 0, 0
    for r in jbu:
        nxt = _ahead(r, minutes)
        if nxt is None:
            continue
        now_in = _pip(r["lon"], r["lat"], hull)
        then_in = _pip(nxt[0], nxt[1], hull)
        if then_in and not now_in:
            pass   # icon unchanged: JetBlue is one blue now
            r["tip"] += f" | entering N90 ~{minutes}min"
            n_in += 1
        elif now_in and not then_in:
            pass   # icon unchanged: JetBlue is one blue now
            r["tip"] += f" | leaving N90 ~{minutes}min"
            n_out += 1
    return n_in, n_out


# The four airports whose traffic belongs on an N90 display. Anyone
# else's overflight at FL350 through ZNY is noise here.
METRO_FOUR = {"JFK", "LGA", "EWR", "TEB", "KJFK", "KLGA", "KEWR", "KTEB"}
METRO_NEAR_NM = 60.0
METRO_LOW_FT = 20000


def metro_only(rows, pairs):
    """Keep only aircraft that touch JFK/LGA/EWR/TEB.

    Two tests, either passes. The route lookup says so — origin OR
    destination in the four. Or the aircraft is inside METRO_NEAR_NM
    and below METRO_LOW_FT: at that range and height there is nothing
    airborne but arrivals and departures, which covers the aircraft
    the lookup has never heard of. Returns (kept, n_dropped).
    """
    import math

    la0, lo0 = N90_CENTER
    kept = []
    for r in rows:
        pr = pairs.get(r["cs"]) or {}
        if isinstance(pr, dict) and (pr.get("orig") in METRO_FOUR
                                     or pr.get("dest") in METRO_FOUR):
            kept.append(r)
            continue
        d = math.hypot((r["lon"] - lo0) * 60.0
                       * math.cos(math.radians(la0)),
                       (r["lat"] - la0) * 60.0)
        alt = r.get("_alt") or 0
        if d <= METRO_NEAR_NM and 0 < alt < METRO_LOW_FT:
            kept.append(r)
    return kept, len(rows) - len(kept)


def mark_arrivals(jbu, dests):
    """Recolour JetBlue rows bound for a NY metro primary.

    Route lookup first; live state as the fallback when the lookup
    returns nothing for that callsign. Sets the LABEL colour only —
    the icon outline is reserved for N90 crossing state, which is a
    different question. Records WHICH source decided, so the tooltip
    can be honest about it.
    """
    n = 0
    for r in jbu:
        _pr = dests.get(r["cs"]) or {}
        d = _pr.get("dest") if isinstance(_pr, dict) else _pr
        if d in NY_METRO:
            why = "route"
        elif d is None and _inbound(r):
            why = "descending"
        else:
            continue
        r["lcolor"] = [20, 90, 45]
        r["tip"] += f" | NY-bound ({why})"
        n += 1
    return n

@st.cache_data(ttl=60, show_spinner=False)
def area_traffic(bucket: str, radius_nm: int):
    """All aircraft within radius_nm of the N90 centre.

    Returns (jbu_rows, other_rows, note). Never raises: the airspace
    layers must still draw if the feed is down.
    """
    import requests

    hdrs = {"User-Agent": "bluemet.org ops dashboard"}
    la, lo = N90_CENTER
    urls = [
        f"https://api.adsb.lol/v2/point/{la:.2f}/{lo:.2f}/{radius_nm}",
        f"https://opendata.adsb.fi/api/v2/lat/{la:.2f}/lon/"
        f"{lo:.2f}/dist/{radius_nm}",
    ]
    last = "no hosts tried"
    for u in urls:
        try:
            r = requests.get(u, headers=hdrs, timeout=6)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            continue
        try:
            ac = (r.json() or {}).get("ac") or []
        except Exception as exc:
            last = f"bad JSON: {exc}"
            continue
        jbu, other, n_ga = [], [], 0
        for p in ac:
            cs = (p.get("flight") or "").strip().upper()
            if _is_ga(cs):
                n_ga += 1
                continue
            mine = cs.startswith("JBU")
            row = _mk_row(p, cs, mine)
            if row is None:
                continue
            (jbu if mine else other).append(row)
        # Decluttering happens ONCE, in _declutter() at layer
        # assembly. This used to run _drop_ramp_clusters here as
        # well, so every aircraft faced two different parked tests —
        # this one dropped anything below 3000 ft AND under 100 kt
        # near any of 21 airports, which is airborne traffic.
        note = f"{len(jbu) + len(other)} of {len(ac)}"
        if n_ga:
            note += f"; {n_ga} GA/unidentified hidden"
        return jbu, other, note
    return [], [], last


from core import airspace as _AS
from core import tracks as _TK

# THE SHARED LOADER, not a private copy. This page carried its own
# _json() and load_airspace(), identical to core/airspace.py except
# that every new asset had to be added in two places — and the
# oceanic routes were added in one. One loader for both pages; the
# cache decorator stays here where Streamlit is.
_json = _AS._json


# ---------------------------------------------------------------------------
# Fleet sweep
# ---------------------------------------------------------------------------
# ONE sweep for everything beyond the terminal query: ZNY and its
# neighbours south to the Florida line, and the oceanic L-routes. See
# core/fleet.py for the tile set, the tiered cadence and the host
# rotation. Cached per tile inside the module, so this wrapper only
# hands it the row builder and the route geometry.
def fleet_sweep(zones=(), all_on_lroutes=False):
    from core import fleet as _FL

    return _FL.sweep(load_airspace().get("oceanic", []), _mk_row,
                     zones=zones, all_on_lroutes=all_on_lroutes,
                     is_commercial=_TK.is_commercial)


# ---------------------------------------------------------------------------
# PIREPs
# ---------------------------------------------------------------------------
# Aviation Weather Center, https://aviationweather.gov/api/data/pirep
# Public, no key. AWC asks callers to set a custom User-Agent so their
# automated filtering does not block valid traffic, so we do.
#
# WHAT IS SHOWN: every PIREP EXCEPT those whose icing is negative,
# trace, light or moderate. Severe icing stays. A report with no icing
# group at all — turbulence, sky cover, weather, remarks — is kept,
# because the filter is about icing noise, not about icing being the
# only thing worth reading.
#
# THE EDGE CASE, decided rather than left to chance: a report carrying
# MODERATE ICING and SEVERE TURBULENCE is KEPT. A strict "drop
# non-severe icing" rule would bin a severe turbulence report because
# of the icing group sitting next to it, which is exactly backwards.
# Set KEEP_IF_TURBULENCE = False for the strict reading.
PIREP_HOURS = 3
PIREP_TTL = 300
KEEP_IF_TURBULENCE = True
# Intensity tokens that count as severe. MOD-SEV is included: a pilot
# who writes MOD-SEV met severe icing.
SEVERE_TOKENS = ("SEV", "SVR", "HVY", "EXTRM")
_ICE_FILL = "#B5179E"       # severe icing
_PIREP_FILL = "#3A6EA5"     # everything else


def _pirep_icon_uri(fill):
    """One symbol for every PIREP. Shape carries "pilot report";
    colour carries "severe icing or not". Using a different shape per
    hazard would mean learning a legend to read the map."""
    import urllib.parse

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" '
        'height="64" viewBox="-16 -16 32 32">'
        f'<circle cx="0" cy="0" r="12.5" fill="{fill}" '
        'stroke="#FFFFFF" stroke-width="1.8"/>'
        '<path d="M-5.6,-1.6 a2.8,2.8 0 1,1 5.6,0 a2.8,2.8 0 1,0 5.6,0" '
        'fill="none" stroke="#FFFFFF" stroke-width="2.0" '
        'stroke-linecap="round"/>'
        '<path d="M0,4.2 L0,8.4" stroke="#FFFFFF" stroke-width="2.0" '
        'stroke-linecap="round"/>'
        '</svg>'
    )
    return ("data:image/svg+xml;charset=utf-8,"
            + urllib.parse.quote(svg))


_PIREP_ICON = {"url": _pirep_icon_uri(_PIREP_FILL), "width": 64,
               "height": 64, "anchorX": 32, "anchorY": 32,
               "mask": False}
_PIREP_ICON_SEV = {"url": _pirep_icon_uri(_ICE_FILL), "width": 64,
                   "height": 64, "anchorX": 32, "anchorY": 32,
                   "mask": False}


def _group(raw, tag):
    """The text of one PIREP group, e.g. /IC or /TB. Returns None if
    the group is absent — which is different from present-but-empty."""
    if tag not in raw:
        return None
    return raw.split(tag, 1)[1].split("/")[0]


def _icing(p):
    """(has_icing, is_severe).

    Two paths on purpose. The AWC JSON schema changed in September
    2025 and the icing fields moved, so the structured read tries
    several spellings and the raw report text is the backstop.
    """
    conds = (p.get("icingCond") or p.get("icing")
             or p.get("icgCond") or [])
    if isinstance(conds, dict):
        conds = [conds]
    found = False
    for c in conds:
        if not isinstance(c, dict):
            continue
        for k in ("icgInt", "iceInt", "intensity", "int"):
            v = str(c.get(k) or "").upper()
            if v:
                found = True
                if any(t in v for t in SEVERE_TOKENS):
                    return True, True
    raw = str(p.get("rawOb") or p.get("raw") or "").upper()
    seg = _group(raw, "/IC")
    if seg is None:
        return found, False
    return True, any(t in seg for t in SEVERE_TOKENS)


def _has_turbulence(p):
    conds = p.get("turbCond") or p.get("turbulence") or []
    if isinstance(conds, dict):
        conds = [conds]
    if any(isinstance(c, dict) and any(c.get(k) for k in
           ("turbInt", "intensity", "int")) for c in conds):
        return True
    raw = str(p.get("rawOb") or p.get("raw") or "").upper()
    return _group(raw, "/TB") is not None


@st.cache_data(ttl=PIREP_TTL, show_spinner=False)
def pireps(_bucket: str, radius_nm: int):
    """PIREPs around the N90 centre, non-severe icing filtered out.

    Returns (rows, note). Never raises — a weather overlay must not
    take the airspace map down with it.
    """
    import math
    import requests

    la, lo = N90_CENTER
    dlat = radius_nm / 60.0
    dlon = dlat / max(math.cos(math.radians(la)), 1e-6)
    params = {
        "format": "json",
        "age": PIREP_HOURS,
        "minLat": round(la - dlat, 3), "maxLat": round(la + dlat, 3),
        "minLon": round(lo - dlon, 3), "maxLon": round(lo + dlon, 3),
    }
    try:
        r = requests.get("https://aviationweather.gov/api/data/pirep",
                         params=params, timeout=8,
                         headers={"User-Agent": "n90-airspace/1.0"})
        if r.status_code != 200:
            return [], f"PIREPs unavailable (HTTP {r.status_code})"
        payload = r.json()
    except Exception as exc:
        return [], f"PIREPs unavailable ({type(exc).__name__})"

    items = payload if isinstance(payload, list) else (
        payload.get("features") or payload.get("data") or [])
    rows, total, dropped, n_sev = [], 0, 0, 0
    for it in items:
        p = (it.get("properties")
             if isinstance(it, dict) and "properties" in it else it)
        if not isinstance(p, dict):
            continue
        total += 1
        has_ice, severe = _icing(p)
        if has_ice and not severe:
            if not (KEEP_IF_TURBULENCE and _has_turbulence(p)):
                dropped += 1
                continue
        try:
            plat, plon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError):
            continue
        alt = p.get("altFt") or p.get("fltLvl") or p.get("altitude")
        raw = str(p.get("rawOb") or p.get("raw") or "").strip()
        if severe:
            n_sev += 1
        rows.append({
            "lat": plat, "lon": plon,
            "icon": _PIREP_ICON_SEV if severe else _PIREP_ICON,
            "tip": (("<b>SEVERE ICING</b> &mdash; " if severe
                     else "<b>PIREP</b> &mdash; ")
                    + (f"{alt} ft" if alt else "level unknown")
                    + (f"<br>{raw[:180]}" if raw else "")),
        })

    note = (f"{len(rows)} PIREP{'' if len(rows) == 1 else 's'} in the "
            f"last {PIREP_HOURS} h")
    if n_sev:
        note += f", {n_sev} severe icing"
    if dropped:
        note += f"; {dropped} light/moderate icing hidden"
    if not rows and not total:
        note = f"no PIREPs reported in the last {PIREP_HOURS} h"
    return rows, note


# ---------------------------------------------------------------------------
# Airport Arrival Demand Chart (AADC)
# ---------------------------------------------------------------------------
# https://www.fly.faa.gov/aadc/ is a JavaScript application — its HTML
# is the word "Loading..." and nothing else — so there is nothing to
# scrape and an iframe would import a whole page's styling into a
# Times New Roman site. It calls a JSON endpoint, so the chart is
# rebuilt here from the same data the FAA's own page draws from.
#
# GET /aadc/api/airports/{LID} returns, per 15-minute bucket, arrival
# counts split by STATUS, by CENTER and by arrival FIX, plus a per
# bucket AAR in `rates` and the GDP state in `control`.
#
# The Referer header is required — the endpoint is meant to be called
# by its own page and returns nothing useful without it.
#
# NO NEW DEPENDENCIES. Rendered with st.vega_lite_chart, which ships
# inside Streamlit, and the data goes inline in the spec so pandas is
# never imported. Adding a plotting library to draw one chart would
# undo the point of stripping this repo down.
AADC_URL = "https://www.fly.faa.gov/aadc/api/airports/{}"
AADC_TTL = 300
AADC_AIRPORT = "JFK"

# Colours and stack order copied from the FAA's own AADC chart, so
# the two read the same at a glance. Order runs from "has not moved
# yet" at the bottom to "arrived" at the top — reverse the list to
# flip the stack.
#
# THE RATE LINE IS BLACK and no status may use black. Flight Active
# owns red on the real chart, which is exactly why the rate line
# cannot: it was red here and read as part of the data.
AADC_STATUS = [
    ("Past Dept Time", "#006400"),
    ("Departing", "#00A63C"),
    ("EDCT Issued", "#800080"),
    ("Irregular", "#FFD400"),
    ("Flight Active", "#FF0000"),
    ("Arrived", "#4D4D4D"),
]
AADC_RATE_COLOR = "#000000"


# The FAA page offers the same three. Fetch once at 15 min and roll
# up locally: switching interval is a re-render, not a re-fetch.
AADC_INTERVALS = {"15 min": 15, "30 min": 30, "60 min": 60}


@st.cache_data(ttl=AADC_TTL, show_spinner=False)
def aadc(_bucket: str, code: str = AADC_AIRPORT):
    """Raw 15-minute arrival demand. Returns (raw, meta, err).

    Timestamps are REBUILT from day + HHMM rather than kept as text:
    an ordinal axis cannot be panned or zoomed, and a window crossing
    midnight sorts wrongly as a string.
    """
    from datetime import datetime, timezone
    import requests

    try:
        r = requests.get(
            AADC_URL.format(code.upper()), timeout=8,
            headers={"User-Agent": "n90-airspace/1.0",
                     "Referer": "https://www.fly.faa.gov/aadc/"})
        if r.status_code != 200:
            return [], {}, f"HTTP {r.status_code}"
        d = r.json() or {}
    except Exception as exc:
        return [], {}, f"{type(exc).__name__}"

    if str(d.get("name", "")).upper() != code.upper():
        return [], {}, "airport mismatch in response"

    buckets = d.get("timeBuckets") or []
    rates = d.get("rates") or []
    wanted = {n for n, _ in AADC_STATUS}
    try:
        yr, mo = int(d.get("year")), int(d.get("month"))
    except (TypeError, ValueError):
        now = datetime.now(timezone.utc)
        yr, mo = now.year, now.month

    raw, prev_day, month_off = [], None, 0
    for i, b in enumerate(buckets):
        t = str(b.get("time") or "").zfill(4)
        try:
            day = int(b.get("day"))
        except (TypeError, ValueError):
            continue
        # The month offset must PERSIST once tripped. Detecting only
        # the transition bucket puts 0000 in the new month and 0015
        # back in the old one.
        if prev_day is not None and day < prev_day - 15:
            month_off += 1
        prev_day = day
        y2, m2 = yr, mo + month_off
        while m2 > 12:
            m2 -= 12
            y2 += 1
        try:
            t0 = datetime(y2, m2, day, int(t[:2]), int(t[2:]),
                          tzinfo=timezone.utc)
        except ValueError:
            continue
        counts = {}
        for c in (b.get("counts") or []):
            if c.get("type") == "STATUS" and c.get("name") in wanted:
                try:
                    counts[c["name"]] = int(c.get("count") or 0)
                except (TypeError, ValueError):
                    pass
        try:
            rate = float(rates[i])
        except (IndexError, TypeError, ValueError):
            rate = None
        raw.append({"t": t0.isoformat(), "counts": counts,
                    "rate": rate})

    meta = {
        "control": d.get("control") or "",
        "aar": d.get("defaultAarRate") or "",
        "total": d.get("totalFlightCount") or "",
        "cancelled": d.get("cancelledFlightCount") or 0,
        "when": d.get("dateTime") or "",
        "buckets": len(raw),
    }
    return raw, meta, None


def aadc_rollup(raw, step):
    """Bin raw 15-minute rows to `step` minutes.

    Returns (bars, cap, totals).

    THE RATE IS PER HOUR. `rates` is an hourly AAR, so a bin's
    capacity is rate x step/60. Comparing a 15-minute count against
    an hourly rate would make every bucket look comfortably under.

    Groups anchor to the hour, so a 30 minute bin starts at :00 and
    :30 the way the FAA page reads, rather than at whatever minute
    the window happens to open on.
    """
    from datetime import datetime, timedelta

    step = max(15, int(step))
    groups = {}
    for row in raw:
        try:
            t0 = datetime.fromisoformat(row["t"])
        except (TypeError, ValueError):
            continue
        anchor = t0.replace(
            minute=(t0.minute // step) * step if step < 60 else 0)
        g = groups.setdefault(anchor, {"counts": {}, "rates": []})
        for k, v in (row.get("counts") or {}).items():
            g["counts"][k] = g["counts"].get(k, 0) + v
        if row.get("rate") is not None:
            g["rates"].append(row["rate"])

    bars, cap, totals = [], [], []
    for anchor in sorted(groups):
        g = groups[anchor]
        iso0 = anchor.isoformat()
        iso1 = (anchor + timedelta(minutes=step)).isoformat()
        # STACK COMPUTED HERE, NOT IN VEGA. A bar given both x and x2
        # is a ranged mark, and Vega-Lite silently ignores
        # "stack": "zero" on ranged marks — every segment then draws
        # at its own value with no base and the chart reads as a
        # scatter of floating dashes. Explicit y0/y1 is the fix.
        n_tot = 0
        for nm, _c in AADC_STATUS:
            v = g["counts"].get(nm, 0)
            if v:
                bars.append({"t0": iso0, "t1": iso1, "status": nm,
                             "n": v, "y0": n_tot, "y1": n_tot + v})
            n_tot += v
        rate_hr = (sum(g["rates"]) / len(g["rates"])
                   if g["rates"] else None)
        binned = round(rate_hr * step / 60.0) if rate_hr else None
        if binned is not None:
            cap.append({"t0": iso0, "t1": iso1, "cap": binned})
        lbl = anchor.strftime("%H%M")
        over = (n_tot - binned) if binned is not None else 0
        totals.append({
            "t0": iso0, "t1": iso1, "demand": n_tot,
            "cap": binned if binned is not None else 0,
            "over": over, "label": lbl,
            "readout": (f"{lbl}  demand {n_tot}"
                        + (f"  cap {binned}  {over:+d}"
                           if binned is not None else "")),
        })
    return bars, cap, totals


def aadc_spec(bars, cap, totals, height=320):
    """Layered Vega-Lite: stacked demand, capacity line, hover readout.

    THE ZOOM PARAM LIVES IN A UNIT SPEC, NOT AT THE TOP LEVEL. Vega-
    Lite only permits selection parameters inside unit specs. Putting
    one on a layered spec makes the whole spec invalid, and an invalid
    spec renders as BLANK SPACE rather than an error — which is
    exactly how this failed the first time: the caption populated
    from live data while the chart area stayed empty.
    """
    return {
        "width": "container",
        "height": height,
        # The app theme is dark, so Vega inherits a dark canvas and
        # the chart floats black on a white retro page. Forced light
        # here rather than changing config.toml, which the map needs.
        "background": "#FFFFFF",
        "config": {
            "font": "Times New Roman, Times, serif",
            "axis": {"labelColor": "#000000", "titleColor": "#000000",
                     "gridColor": "#D0D0D0", "domainColor": "#000000",
                     "tickColor": "#000000"},
            "legend": {"labelColor": "#000000",
                       "titleColor": "#000000"},
            "view": {"stroke": "#000000"},
        },
        "layer": [
            {
                "data": {"values": bars},
                # Hairline outline: yellow has almost no edge on white
                # and the two greens run together without it.
                "mark": {"type": "bar", "stroke": "#000000",
                         "strokeWidth": 0.4},
                "params": [
                    {"name": "zoom",
                     "select": {"type": "interval", "encodings": ["x"]},
                     "bind": "scales"},
                ],
                "encoding": {
                    "x": {"field": "t0", "type": "temporal",
                          "title": None,
                          "axis": {"format": "%H%M",
                                   "labelAngle": -50}},
                    "x2": {"field": "t1"},
                    "y": {"field": "y0", "type": "quantitative",
                          "title": "arrivals",
                          "scale": {"domainMin": 0}},
                    "y2": {"field": "y1"},
                    "color": {"field": "status", "type": "nominal",
                              "title": None,
                              "scale": {
                                  "domain": [n for n, _ in AADC_STATUS],
                                  "range": [c for _, c in AADC_STATUS]},
                              "legend": {"orient": "bottom",
                                         "columns": 3}},
                    "order": {"field": "status", "type": "nominal"},
                    "tooltip": [{"field": "status", "title": "status"},
                                {"field": "n", "title": "flights"}],
                },
            },
            {
                "data": {"values": cap},
                "mark": {"type": "line",
                         "color": AADC_RATE_COLOR,
                         "strokeWidth": 2,
                         "interpolate": "step-after"},
                "encoding": {
                    "x": {"field": "t0", "type": "temporal"},
                    "y": {"field": "cap", "type": "quantitative"},
                },
            },
            {
                # Invisible point layer owns the selection. "nearest"
                # builds a Voronoi over point marks; a rule spanning
                # the full height is not one, so hover resolution was
                # unreliable when the param lived on the rule.
                "data": {"values": totals},
                "mark": {"type": "point", "opacity": 0, "size": 200},
                "params": [
                    {"name": "hover",
                     "select": {"type": "point", "encodings": ["x"],
                                "on": "pointerover", "nearest": True,
                                "clear": "pointerout",
                                "empty": False}},
                ],
                "encoding": {
                    "x": {"field": "t0", "type": "temporal"},
                    "y": {"field": "demand", "type": "quantitative"},
                    "tooltip": [{"field": "label", "title": "time"},
                                {"field": "demand", "title": "demand"},
                                {"field": "cap", "title": "capacity"},
                                {"field": "over", "title": "over/under"}],
                },
            },
            {
                "data": {"values": totals},
                # Dashed, so a vertical black rule is never mistaken
                # for part of the solid black rate line.
                "mark": {"type": "rule", "color": "#000000",
                         "strokeWidth": 1, "strokeDash": [3, 3]},
                "encoding": {
                    "x": {"field": "t0", "type": "temporal"},
                    # "empty" MUST BE SET ON THE CONDITION, not only
                    # on the selection. They are separate flags and
                    # both default to true, so a condition without it
                    # matches every row while nothing is hovered —
                    # which painted all forty readouts at once.
                    "opacity": {
                        "condition": {"param": "hover", "empty": False,
                                      "value": 0.55},
                        "value": 0},
                    "tooltip": [{"field": "label", "title": "time"},
                                {"field": "demand", "title": "demand"},
                                {"field": "cap", "title": "capacity"},
                                {"field": "over", "title": "over/under"}],
                },
            },
            {
                "data": {"values": totals},
                "mark": {"type": "text", "align": "left", "dx": 5,
                         "dy": -8, "fontSize": 12,
                         "fontWeight": "bold", "color": "#000000"},
                "encoding": {
                    "x": {"field": "t0", "type": "temporal"},
                    "y": {"field": "demand", "type": "quantitative"},
                    "text": {"field": "readout", "type": "nominal"},
                    "opacity": {
                        "condition": {"param": "hover", "empty": False,
                                      "value": 1},
                        "value": 0},
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# ZNY CWSU SWAP forecast
# ---------------------------------------------------------------------------
# The New York CWSU publishes its SWAP products at FIXED URLs that are
# overwritten in place. No scraping: the graphic is just an image and
# Streamlit can render it from the URL directly.
#
# THE TRAP IN "OVERWRITTEN IN PLACE": the filename never changes, so a
# stale graphic is indistinguishable from a current one by URL alone.
# On an operations display that is genuinely dangerous — yesterday's
# SWAP looks exactly like today's. The only way to tell is the HTTP
# Last-Modified header, so it is read on every refresh and the age is
# shown. Past SWAP_STALE_MIN the panel says so rather than quietly
# presenting old guidance as current.
#
# The SWAP Statement is the CWSU's forecast of the PROBABILITY that
# the FAA implements a SWAP — a planning tool, not an observation. It
# is a PDF, which would need a parser this repo does not carry, so it
# is linked rather than inlined until that trade is worth making.
SWAP_IMG = "https://www.weather.gov/images/zny/SWAP_1.gif"
SWAP_STMT = "https://www.weather.gov/media/zny/ZNY_SWAP.pdf"
SWAP_PAGE = "https://www.weather.gov/zny/SWAP_1"
SWAP_TTL = 600
SWAP_STALE_MIN = 360          # 6 h; SWAP products are issued daily


@st.cache_data(ttl=SWAP_TTL, show_spinner=False)
def swap_age(_bucket: str):
    """(age_minutes, last_modified_text, err) for the SWAP graphic.

    HEAD only — the image itself is rendered by the browser from the
    URL, so there is no reason to pull the bytes through this box.
    Returns age None when the server gives no Last-Modified, which is
    reported as unknown rather than assumed fresh.
    """
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    import requests

    try:
        r = requests.head(SWAP_IMG, timeout=6, allow_redirects=True,
                          headers={"User-Agent": "n90-airspace/1.0"})
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}"
        lm = r.headers.get("Last-Modified")
        if not lm:
            return None, None, None
        when = parsedate_to_datetime(lm)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - when).total_seconds() / 60.0
        return age, when.strftime("%d %b %H%MZ"), None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}"


# ---------------------------------------------------------------------------
# FAA NAS status — ground stops, delays, closures, runway config
# ---------------------------------------------------------------------------
# https://nasstatus.faa.gov/api/airport-events — one GET, JSON, no key.
#
# THIS REPLACES SCRAPING fly.faa.gov/ois. The OIS page is a legacy
# frameset that renders nothing without following its inner frames,
# and the FAA publishes the same ATCSCC information here as structured
# data. A scrape of OIS would break on any layout change and give
# nothing the feed does not already provide.
#
# The feed returns EVERY airport in one payload, so asking about more
# airports costs no extra requests — only more parsing.
NAS_URL = "https://nasstatus.faa.gov/api/airport-events"
NAS_TTL = 120
NAS_AIRPORT = "JFK"


@st.cache_data(ttl=NAS_TTL, show_spinner=False)
def nas_status(_bucket: str, code: str = NAS_AIRPORT):
    """Active traffic management initiatives at one airport.

    Returns (lines, updated, err). `lines` is ordered by operational
    weight: a ground stop first, config and rate last. Empty `lines`
    with no `err` means the airport is clear, which is a real answer
    and must not be shown the same way as a failed fetch.
    """
    import requests

    try:
        r = requests.get(NAS_URL, timeout=8,
                         headers={"User-Agent": "n90-airspace/1.0"})
        if r.status_code != 200:
            return [], None, f"HTTP {r.status_code}"
        data = r.json()
    except Exception as exc:
        return [], None, f"{type(exc).__name__}"

    ap = None
    for a in (data or []):
        if isinstance(a, dict) and a.get("airportId") == code:
            ap = a
            break
    if ap is None:
        # The endpoint is airport-EVENTS: an airport with nothing
        # happening is simply absent. That is "clear", not "broken",
        # and reporting it as an error trains people to ignore the
        # warning that matters.
        return [], None, None

    out, updated = [], None

    def _t(v):
        return str(v) if v not in (None, "") else "?"

    gs = ap.get("groundStop")
    if gs:
        updated = updated or gs.get("updatedAt")
        line = (f"**GROUND STOP** until {_t(gs.get('endTime'))} — "
                f"{_t(gs.get('impactingCondition'))}")
        if gs.get("probabilityOfExtension"):
            line += (f" (extension probability "
                     f"{gs['probabilityOfExtension']})")
        if gs.get("includedFlights"):
            line += f"\n  Scope: {gs['includedFlights']}"
        out.append(line)

    gd = ap.get("groundDelay")
    if gd:
        updated = updated or gd.get("updatedAt")
        out.append(
            f"**GROUND DELAY PROGRAM** {_t(gd.get('startTime'))}–"
            f"{_t(gd.get('endTime'))} — avg {_t(gd.get('avgDelay'))}, "
            f"max {_t(gd.get('maxDelay'))} — "
            f"{_t(gd.get('impactingCondition'))}"
            + (f"\n  Scope: {gd['departureScope']}"
               if gd.get("departureScope") else ""))

    # CLOSURES ARE IN TWO PLACES. The feed does not reliably populate
    # airportClosure; a closure often arrives as free-form text with
    # CLSD in it instead. Checking only the structured field means
    # missing real closures.
    cl = ap.get("airportClosure")
    ff = ap.get("freeForm")
    if cl:
        updated = updated or cl.get("updatedAt")
        out.append(f"**AIRPORT CLOSED** {_t(cl.get('startTime'))}–"
                   f"{_t(cl.get('endTime'))} — "
                   f"{_t(cl.get('simpleText'))}")
    elif ff and "CLSD" in str(ff.get("simpleText") or "").upper():
        updated = updated or ff.get("updatedAt")
        out.append(f"**CLOSURE** {_t(ff.get('startTime'))}–"
                   f"{_t(ff.get('endTime'))} — "
                   f"{_t(ff.get('simpleText'))}")

    for key, label in (("arrivalDelay", "ARRIVAL DELAY"),
                       ("departureDelay", "DEPARTURE DELAY")):
        d = ap.get(key)
        if not d:
            continue
        updated = updated or d.get("updateTime")
        ad = d.get("arrivalDeparture") or {}
        span = ""
        if ad.get("min") or ad.get("max"):
            span = f"{_t(ad.get('min'))}–{_t(ad.get('max'))}"
        elif d.get("averageDelay"):
            span = f"avg {d['averageDelay']}"
        out.append(f"**{label}** {span}"
                   + (f", {ad['trend']}" if ad.get("trend") else "")
                   + f" — {_t(d.get('reason'))}")

    if ap.get("deicing"):
        out.append("**DEICING** in progress")

    # Runway configuration and arrival rate. Not a delay, but it is
    # the single most useful line here for a terminal display and the
    # handoff lists runway config as a gap — so it goes in even on a
    # clear day.
    cfg = ap.get("airportConfig") or {}
    if cfg:
        bits = []
        if cfg.get("arrivalRunwayConfig"):
            bits.append(f"arr {cfg['arrivalRunwayConfig']}")
        if cfg.get("departureRunwayConfig"):
            bits.append(f"dep {cfg['departureRunwayConfig']}")
        if cfg.get("arrivalRate"):
            bits.append(f"AAR {cfg['arrivalRate']}")
        if bits:
            out.append("Config: " + ", ".join(bits))
            updated = updated or cfg.get("sourceTimeStamp")

    return out, updated, None


@st.cache_data(ttl=86400, show_spinner=False)
def load_airspace():
    return _AS.load_airspace()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("N90 — New York TRACON")
st.caption(
    "Terminal airspace reference. Opens at a 300 nm radius, which "
    "reaches DCA, BUF and PWM."
)

data = load_airspace()
for e in data.get("errors", []):
    st.warning(f"Asset unavailable — {e}")

c = st.columns([1, 1, 1, 1, 1, 2])
with c[0]:
    show_fix = st.checkbox("Fixes", value=True)
with c[1]:
    show_hull = st.checkbox("N90 extent", value=True)
with c[2]:
    show_cb = st.checkbox(
        "Class B", value=True,
        help="Draws the New York Class B rings and shelves, and limits "
             "the aircraft shown to those inside the Class B lateral "
             "boundary. Off, every aircraft in the view draws.")
with c[3]:
    show_ar = st.checkbox("ARTCC", value=True)
    show_radar = st.checkbox(
        "Radar", value=True,
        help="MRMS national mosaic, refreshed every couple of "
             "minutes by a background warmer. 1.1 km data upsampled "
             "4x and smoothed on the field before quantising, so it "
             "holds up at terminal zoom without inventing colours. "
             "The slider reaches back two hours.")
    show_l2 = False
    show_tops = st.checkbox(
        "Echo tops", value=False,
        help="MRMS 18 dBZ echo-top height, tagged COSPA-style on each "
             "cell's peak — only tops above FL380. Rebuilt every few "
             "minutes by the radar warmer.")
    show_ice = st.checkbox(
        "PIREPs", value=False,
        help=f"Pilot reports from the Aviation Weather Center, last "
             f"{PIREP_HOURS} hours. Everything EXCEPT negative, "
             f"trace, light and moderate icing — those are filtered "
             f"out and counted in the caption. Severe icing draws "
             f"magenta, everything else blue.")
    show_oc = st.checkbox(
        "Oceanic", value=True,
        help="WATRS / oceanic L-routes off the coast, from the same "
             "FAA ATS Route dataset as the domestic routes. Slate "
             "blue so they read as a separate family. JetBlue on "
             "them draws with the routes.")
    oc_all = st.checkbox(
        "All airlines on L-routes", value=False,
        help="Every commercial aircraft within 40 nm of an L-route, "
             "not just JetBlue. Same tiles, more rows.")
    # STACKED, NOT SUB-COLUMNS. Three columns inside this already
    # narrow one truncated every label to "Z", and the three zones
    # were literally indistinguishable in the controls.
    zone_ztl = st.checkbox("ZTL Atlanta", value=False,
                           help="JetBlue across Atlanta Center.")
    zone_zjx = st.checkbox("ZJX Jacksonville", value=False,
                           help="JetBlue across Jacksonville Center.")
    zone_zma = st.checkbox("ZMA Miami", value=False,
                           help="JetBlue across Miami Center, including "
                                "the Bahamas.")
    _zones = tuple(z for z, on in (("ZTL", zone_ztl), ("ZJX", zone_zjx),
                                   ("ZMA", zone_zma)) if on)
    show_rt = st.checkbox(
        "Routes", value=True,
        help="Charted ATS routes through ZNY and its neighbouring "
             "centres. GREEN eastbound, RED westbound, AMBER where "
             "the FAA publishes no direction. These are CHARTED "
             "airways, not what traffic is flying today.")
with c[4]:
    show_ap = st.checkbox("JBU stations", value=True)
    scope = st.toggle(
        "Scope view", value=False,
        help="Draw the map the way a STARS scope does: black background, "
             "a thin video map instead of a basemap, fixes as triangles, "
             "history dots instead of trails, range rings from JFK, muted "
             "weather. The data is the same; only the drawing changes.")
    track_win = st.selectbox(
        "Tracks", ["Off", "15 min", "30 min", "45 min", "1 hr"], index=2,
        help="Trail behind every aircraft on the map showing where it "
             "has been. History builds at one fix per two minutes from a "
             "background tracker and is empty right after a deploy.")
    metro_filter = st.checkbox(
        "Metro traffic only", value=True,
        help="Other operators are shown only if they depart or arrive "
             "JFK, LGA, EWR or TEB — by route lookup, or by being low "
             "and inside 60 nm, which nothing but an arrival or a "
             "departure is. Overflights through ZNY are dropped. "
             "JetBlue is never filtered.")
    jbu_only = st.checkbox(
        "JBU Flights Only", value=False,
        help="Hide everyone else. The airspace, routes and fixes stay; "
             "only the other-operator traffic layer goes. Winds aloft "
             "still use every aircraft reporting, because a wind is a "
             "wind regardless of who is flying through it.")
    show_fleet = st.checkbox(
        "JBU region", value=False,
        help="Every JetBlue aircraft across ZNY and its neighbouring "
             "centres (ZBW, ZOB, ZDC), south to the Florida line. "
             "Shares one sweep with the oceanic routes: near tiles "
             "refresh every 3 min, far ones every 5, spread across "
             "three feeds. Most of these sit outside the default "
             "view; zoom out to see them.")
    show_ac = st.checkbox(
        "Traffic", value=True,
        help="All aircraft within the view radius set on the right, "
             f"capped at {TRAFFIC_MAX_NM} nm by the feed. JetBlue "
             "solid blue with flight numbers, GREEN if bound for a "
             "NY metro primary; everyone else an outline only, "
             "tooltip only. Refreshes every 60 s.")
with c[5]:
    radius_nm = st.select_slider(
        "Initial radius (nm)", [100, 150, 200, 250, 300, 350, 400],
        value=300)

# ---------------------------------------------------------------------------
# Map sizing — TEMPORARY tuning controls
# ---------------------------------------------------------------------------
# Here to find the right default by eye, not to stay. Once the numbers
# settle, hardcode MAP_H and MAP_W_PCT below and delete this expander.
# Width is a percentage rather than pixels because Streamlit sizes a
# chart to its container: the only way to make it narrower is to put
# it in a column and leave the rest empty, so the slider drives a
# column ratio.
MAP_H_DEFAULT = 760
MAP_W_DEFAULT = 100
# ---------------------------------------------------------------------------
# Radar time
# ---------------------------------------------------------------------------
# BUILT HERE, WITH THE OTHER CONTROLS, not down in layer assembly.
_hist, _pick, _hist_err = [], None, ""
if show_radar:
    try:
        from core import mrms as _MR

        # NEVER SWALLOW THIS. history() is newer than newest(), so a
        # core/mrms.py that predates the scrubber raises here — and a
        # bare "except: _hist = []" turns that into no radar AND no
        # slider with nothing said.
        if not hasattr(_MR, "history"):
            raise AttributeError(
                "core/mrms.py has no history() — upload the version "
                "with the loop scrubber")
        _hist = _MR.history(_STATIC, hours=LOOP_HOURS, limit=80)
    except Exception as _hexc:
        _hist_err = f"{type(_hexc).__name__}: {_hexc}"
        _hist = []
if _hist_err:
    st.error(f"Radar loop unavailable — {_hist_err}")
if len(_hist) > 1:
    _labels = [f"{h[0][9:11]}:{h[0][11:13]}Z" for h in _hist]
    _pick = st.select_slider(
        f"Radar time \u2014 {len(_hist)} frames over the last "
        f"{LOOP_HOURS:g} h",
        options=list(range(len(_hist))), value=len(_hist) - 1,
        format_func=lambda i: _labels[i],
        help="Drag to loop the mosaic. The latest frame draws at "
             "full resolution; earlier ones use lighter frames so "
             "scrubbing stays smooth.")
    if _pick != len(_hist) - 1:
        st.warning(
            f"Radar rewound to {_labels[_pick]} \u2014 latest scan is "
            f"{_labels[-1]}. Aircraft, trails and PIREPs are FROZEN at "
            f"their last live positions; only the radar changes.")
elif show_radar and len(_hist) == 1:
    st.caption("Radar loop: 1 frame so far. Backfill fills the last "
               "two hours over the next ~20 minutes.")

with st.expander("Map size (tuning)"):
    z1, z2 = st.columns([2, 2])
    with z1:
        map_h = st.slider("Height (px)", 380, 1400, MAP_H_DEFAULT, 20)
    with z2:
        map_w = st.slider("Width (% of page)", 40, 100,
                          MAP_W_DEFAULT, 5)
    st.caption(f"Currently **{map_w}% x {map_h}px**. When this looks "
               f"right, set MAP_H_DEFAULT = {map_h} and MAP_W_DEFAULT = "
               f"{map_w} in the source and remove this expander.")

# JETBLUE AT 1.0 — the same size as everyone else. Scope targets are
# one square for every operator; the slider that scaled JetBlue's
# silhouette is gone. The ratio still feeds the parked-aircraft
# silhouettes at JFK, which is the one place a silhouette remains.
fleet_ratio = 1.0
_fs, _fmin, _fmax = fleet_sizing(fleet_ratio)


import math

import pydeck as pdk


def _zoom_for(radius_nm, px=1400.0):
    """deck.gl zoom that fits a radius. World is 512*2**z px wide, and
    a radius in nm converts to degrees of LONGITUDE via cos(lat) —
    which is why a 300 nm radius spans ~13 deg here, not 10."""
    deg = 2.0 * radius_nm / 60.0 / math.cos(math.radians(N90_CENTER[0]))
    return round(math.log2(px * 360.0 / (512.0 * deg)), 2)


layers = []

# Order matters: ARTCC underneath as a reference grid, then Class B,
# then the N90 extent, then fixes and airports on top.
# ---------------------------------------------------------------------------
# Scrub freeze
# ---------------------------------------------------------------------------
# When the radar slider is off the live frame, NOTHING ELSE MAY MOVE.
# The point of scrubbing is to read the radar trend; aircraft jumping
# to new positions, PIREPs refreshing and trails redrawing under it
# defeat that and read as the map being rewound, which it is not.
#
# So every live input on the layer path goes through _frozen(): at the
# live position it fetches and stores; scrubbed, it returns what was
# stored. The radar layer is the only thing built from the picked
# frame. Nothing here is a cache with a TTL — it is a snapshot that
# lasts exactly as long as the slider is off the end.
_scrubbed = bool(_hist) and _pick is not None and _pick != len(_hist) - 1


def _frozen(key, fetch):
    """Live: fetch, store, return. Scrubbed: return the stored value
    if there is one, else fetch (first-ever render while scrubbed)."""
    _sk = f"_freeze_{key}"
    if _scrubbed and _sk in st.session_state:
        return st.session_state[_sk]
    val = fetch()
    if not _scrubbed:
        st.session_state[_sk] = val
    return val


_flow = None      # filled once traffic is built; drawn after the map
_surf = {}
# HOLDING-PATTERN ALERTS, above the map. Two states, two colours:
# an aircraft holding now is red and should interrupt whoever is
# looking; one that has just left a hold is green, because "the
# hold is releasing" is the next thing an SOC wants to know and it
# is invisible on a map that only shows the present.
_holding, _exited, _hold_cs = [], [], set()
if show_ac:
    try:
        _holding, _exited = _frozen("holds", _TK.hold_events)
        _hold_cs = {hx for *_, hx in _holding}
    except Exception:
        pass
if _holding or _exited:
    from html import escape as _esc

    _h = "".join(
        f"<div>\u26a0 <b>{_esc(_who(c))}</b> in holding pattern "
        f"\u2014 {m:.0f} min, {p:.0f} nm flown, {t:.0f}&deg; of turn"
        f"</div>" for c, m, p, t, _hx in _holding)
    _x = "".join(
        f"<div>\u2713 <b>{_esc(_who(c))}</b> left holding "
        f"{a:.0f} min ago after {m:.0f} min in the hold</div>"
        for c, m, a, _hx in _exited)
    # UNMISSABLE ON PURPOSE. These are the only two things on the
    # page that should stop whoever is looking at it.
    if _h:
        st.markdown(
            "<div style='background:#B00020;border:4px solid #5A0010;"
            "border-radius:8px;padding:14px 18px;margin:8px 0;"
            "color:#FFFFFF;'>"
            "<div style='font-size:26px;font-weight:800;"
            "letter-spacing:1px;margin-bottom:6px;'>\u26a0 HOLDING "
            "PATTERN DETECTED</div>"
            "<div style='font-size:19px;'>" + _h + "</div></div>",
            unsafe_allow_html=True)
    if _x:
        st.markdown(
            "<div style='background:#1C8244;border:4px solid #0F4A24;"
            "border-radius:8px;padding:12px 18px;margin:8px 0;"
            "color:#FFFFFF;'>"
            "<div style='font-size:22px;font-weight:800;"
            "letter-spacing:1px;margin-bottom:4px;'>\u2713 HOLD "
            "RELEASED</div>"
            "<div style='font-size:17px;'>" + _x + "</div></div>",
            unsafe_allow_html=True)

# EMERGENCY SQUAWKS, above the map. Commercial only, from the rows
# already on screen — the tooltip says where; the banner says who and
# what. Built here, before the layers, so it appears at the top.
_EMERG_SQ = {"7500": "unlawful interference", "7600": "radio failure",
             "7700": "emergency"}
_EMERG_WORDS = {"general": "general emergency", "minima": "minimum fuel",
                "nordo": "radio failure", "unlawful": "unlawful "
                "interference", "downed": "downed aircraft"}
_emerg_rows = []
# RADAR GOES DOWN FIRST, under every vector layer. Reflectivity is
# the backdrop a controller reads the rest against; an airway or a
# Class B edge buried under a storm cell is a line nobody sees.
# RADAR GOES DOWN FIRST, under every vector layer.
_radar_note = ""
_l2_note = ""
if show_radar:
    try:
        import os

        _rbase = (os.environ.get("RENDER_EXTERNAL_URL")
                  or os.environ.get("PUBLIC_BASE_URL")
                  or "").rstrip("/")
        _chunks = _loop = _rstamp = None
        if _hist:
            _i = len(_hist) - 1 if _pick is None else _pick
            _rstamp, _chunks, _loop = _hist[_i]
            # TWO RESOLUTIONS. The live frame draws its full chunks;
            # any earlier frame draws its single light image, so
            # scrubbing is one texture per step rather than fifteen.
            if isinstance(_loop, dict):      # pre-split manifests
                _loop = [_loop]
            # Chunks whenever the frame has them — every loop frame
            # is a full chunk set now. The loop image is only a
            # fallback for a frame whose chunks were pruned first.
            if not _chunks and _loop:
                _chunks = _loop

        if _chunks and _rbase:
            for _c in _chunks:
                layers.append(pdk.Layer(
                    "BitmapLayer", data=None,
                    # A REAL https URL, not a data URI. deck.gl parses
                    # a data URI as a JS expression and dies on the
                    # colon; frames are written to static/ and served
                    # via enableStaticServing instead.
                    image=f"{_rbase}/app/static/{_c['name']}",
                    bounds=_c["bounds"], opacity=0.5 if scope else 1.0,
                    # Explicit GL filtering: trilinear minification,
                    # linear magnification. deck.gl's default is the
                    # same, but stated here so a future default change
                    # cannot quietly turn the mosaic into blocks.
                    # Keys are WebGL enums as strings (JSON).
                    texture_parameters={"10241": 9987, "10240": 9729,
                                        "10242": 33071, "10243": 33071}))
            _radar_note = (f" Radar: {_rstamp}, {len(_hist)} frame(s) "
                           f"over {LOOP_HOURS:g} h.")
        elif not _rbase:
            _radar_note = " Radar: RENDER_EXTERNAL_URL unset."
        else:
            # HIGHLY VISIBLE ON PURPOSE. An empty radar layer is
            # indistinguishable from clear skies.
            st.markdown(
                "<div style='background:#FFF3B0;"
                "border:2px solid #B38600;border-radius:6px;"
                "padding:10px 14px;margin:6px 0;text-align:center;"
                "font-size:20px;font-weight:700;color:#5A4300;'>"
                "MRMS radar rendering\u2026 "
                "<span style='font-size:15px;font-weight:400'>"
                "first national scan takes a few minutes after a "
                "restart. The map is NOT showing radar right now."
                "</span></div>", unsafe_allow_html=True)
            _radar_note = (" Radar: warming, first scan appears "
                           "within a few minutes.")
    except Exception as _rexc:
        _radar_note = f" Radar unavailable ({type(_rexc).__name__})."

# ECHO-TOP TAGS, above the radar and under the vector layers. A dark
# chip with the flight level, on the peak of every cell above FL380.
# The number is what a controller reads off COSPA; the position is
# the local maximum, not the cell centroid.
_tops_note = ""
if show_tops:
    try:
        from core import echotops as _ET

        _tops = _frozen("tops", lambda: _ET.load(_STATIC))
    except Exception:
        _tops = {}
    _tag_rows = [{"lon": t["lon"], "lat": t["lat"], "txt": f"{t['fl']}",
                  "tip": f"Echo top FL{t['fl']} (18 dBZ, MRMS "
                         f"{_tops.get('stamp', '')})"}
                 for t in (_tops.get("tags") or [])]
    if _tag_rows:
        layers.append(pdk.Layer(
            "TextLayer", data=_tag_rows, get_position="[lon, lat]",
            get_text="txt", get_size=13, get_color=[255, 255, 255],
            font_weight=700, background=True,
            get_background_color=[40, 40, 40, 235],
            background_padding=[5, 2, 5, 2], pickable=True))
        _tops_note = (f" Echo tops: {len(_tag_rows)} cell(s) above "
                      f"FL{_tops.get('min_fl', 380)}, MRMS "
                      f"{_tops.get('stamp', '?')}.")
    elif _tops:
        _tops_note = f" Echo tops: none above FL{_tops.get('min_fl', 380)}."
    else:
        _tops_note = " Echo tops: warming."

# Routes under the boundaries: they are a reference grid, and an
# airway drawn over a Class B shelf edge makes the shelf harder to
# read, which is the wrong trade on a terminal display.
if show_rt and data.get("routes"):
    layers.append(pdk.Layer(
        "PathLayer", data=data["routes"], get_path="path",
        get_color=[62, 74, 86, 220] if scope else "color",
        width_min_pixels=1, get_width=1 if scope else 2,
        width_units='"pixels"', pickable=True))
    # Labels repeat every ~75 nm along each run and rotate to follow
    # the line, so a route stays identifiable wherever it is on
    # screen without one label per vertex burying the map. The scope
    # has none: an airway is a line on a video map, and its name is
    # in the tooltip.
    if data.get("route_labels") and not scope:
        layers.append(pdk.Layer(
            "TextLayer", data=data["route_labels"],
            get_position="[lon, lat]", get_text="ident",
            get_size=10, get_color="color", get_angle="angle",
            get_text_anchor='"middle"',
            get_alignment_baseline='"center"',
            background=True,
            get_background_color=[255, 255, 255, 205],
            background_padding=[3, 1, 3, 1]))

if show_oc and data.get("oceanic"):
    layers.append(pdk.Layer(
        "PathLayer", data=data["oceanic"], get_path="path",
        get_color=[62, 74, 86, 220] if scope else "color",
        width_min_pixels=1, get_width=1 if scope else 2,
        width_units='"pixels"', pickable=True))
    if data.get("oceanic_labels") and not scope:
        layers.append(pdk.Layer(
            "TextLayer", data=data["oceanic_labels"],
            get_position="[lon, lat]", get_text="ident",
            get_size=10, get_color="color", get_angle="angle",
            get_text_anchor='"middle"',
            get_alignment_baseline='"center"',
            background=True,
            get_background_color=[255, 255, 255, 205],
            background_padding=[3, 1, 3, 1]))

# ZONE EXTENTS for the southern centres, whenever one is ticked. These
# are the boxes core/fleet.py actually sweeps and tags by — approximate
# ARTCC extents, not the published boundaries (see artcc_high.json for
# those). Without them, ticking ZMA put aircraft over Florida with no
# line saying where ZMA begins, and the three zones were
# indistinguishable. Dashed on purpose: approximate looks approximate.
if _zones:
    from core import fleet as _FLZ

    _zrows, _zlbl = [], []
    for _z in _zones:
        _b = _FLZ.ZONES[_z]
        _poly = [[_b["w"], _b["s"]], [_b["e"], _b["s"]],
                 [_b["e"], _b["n"]], [_b["w"], _b["n"]], [_b["w"], _b["s"]]]
        _zrows.append({"polygon": _poly, "tip": f"{_z} sweep extent "
                                                f"(approximate)"})
        _zlbl.append({"lon": (_b["w"] + _b["e"]) / 2,
                      "lat": (_b["s"] + _b["n"]) / 2, "txt": _z})
    layers.append(pdk.Layer(
        "PolygonLayer", data=_zrows, get_polygon="polygon",
        stroked=True, filled=True,
        get_fill_color=[120, 60, 160, 8 if scope else 18],
        get_line_color=[90, 70, 120, 160] if scope else [120, 60, 160, 200],
        line_width_min_pixels=1 if scope else 2,
        get_line_width=3, pickable=True))
    layers.append(pdk.Layer(
        "TextLayer", data=_zlbl, get_position="[lon, lat]",
        get_text="txt", get_size=28, get_color=[120, 60, 160, 160],
        font_weight=800, background=False))

if show_ar and data.get("artcc"):
    layers.append(pdk.Layer(
        "PolygonLayer", data=data["artcc"], get_polygon="polygon",
        filled=False, stroked=True,
        get_line_color=[62, 74, 86, 200] if scope else [110, 122, 128, 185],
        line_width_min_pixels=1, get_line_width=1, pickable=True))
if show_cb and data.get("classb"):
    layers.append(pdk.Layer(
        "PolygonLayer", data=data["classb"], get_polygon="polygon",
        filled=False, stroked=True,
        get_line_color=[78, 92, 108, 220] if scope else [0, 90, 200, 200],
        line_width_min_pixels=1, get_line_width=1, pickable=True))
if show_hull and data.get("hull"):
    layers.append(pdk.Layer(
        "PolygonLayer", data=data["hull"], get_polygon="polygon",
        filled=False, stroked=True,
        get_line_color=[140, 156, 172, 230] if scope else [230, 120, 30, 190],
        line_width_min_pixels=1 if scope else 5,
        get_line_width=1 if scope else 5, pickable=True))
# RANGE RINGS from JFK at 10 / 20 / 30 / 40 nm, faint: how a controller
# judges distance without a scale bar. Scope only.
if scope:
    import math as _m

    _rings = []
    for _nm in (10, 20, 30, 40):
        _k = _m.cos(_m.radians(N90_CENTER[0]))
        _rings.append({"path": [[N90_CENTER[1] + _nm / 60 * _m.sin(_m.radians(a)) / _k,
                                 N90_CENTER[0] + _nm / 60 * _m.cos(_m.radians(a))]
                                for a in range(0, 361, 5)],
                       "tip": f"{_nm} nm from JFK"})
    layers.append(pdk.Layer(
        "PathLayer", data=_rings, get_path="path",
        get_color=[43, 52, 64, 255], get_width=1, width_units='"pixels"',
        width_min_pixels=1, width_max_pixels=1, pickable=False))
    layers.append(pdk.Layer(
        "TextLayer", data=[{"lon": N90_CENTER[1], "lat": N90_CENTER[0] + _nm / 60,
                            "txt": str(_nm)} for _nm in (20, 30, 40)],
        get_position="[lon, lat]", get_text="txt", get_size=9,
        get_color=[74, 87, 102], font_family="monospace",
        get_text_anchor='"start"', get_pixel_offset=[3, 0]))
if show_ap:
    rows = [{"lon": AIRPORTS[ic][1], "lat": AIRPORTS[ic][0],
             "name": ic[1:], "tip": f"{ic} \u2014 JetBlue station"}
            for ic in JBU_CITIES if ic in AIRPORTS]
    # Small, blue, uniform. The old three-tier red/orange/grey dots
    # at 40-90 px radius were the loudest thing on the map after the
    # radar; a station marker should be found when looked for, not
    # seen from across the room.
    layers.append(pdk.Layer(
        "ScatterplotLayer", data=rows, get_position="[lon, lat]",
        get_fill_color=[184, 196, 208, 230] if scope else [0, 90, 220, 230],
        get_line_color=[5, 7, 12, 230] if scope else [255, 255, 255, 230],
        stroked=True,
        line_width_min_pixels=1, get_radius=1200,
        radius_units='"meters"', radius_min_pixels=3,
        radius_max_pixels=6, pickable=True))
    layers.append(pdk.Layer(
        "TextLayer", data=rows, get_position="[lon, lat]",
        get_text="name", get_size=1600, size_units='"meters"',
        size_min_pixels=8, size_max_pixels=11,
        get_color=[0, 40, 120],
        get_text_anchor='"start"', get_pixel_offset=[7, 7],
        background=True, get_background_color=[255, 255, 255, 225],
        background_padding=[3, 1, 3, 1]))
if show_ac:
    from datetime import datetime, timezone
    _tb = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    _tr_nm = min(int(radius_nm), TRAFFIC_MAX_NM)
    _ac, _other, _acnote = _frozen(
        "traffic", lambda: area_traffic(_tb, _tr_nm))
    # NO GENERAL AVIATION. Only ICAO airline-format callsigns —
    # DAL182, EJA411 — are kept; N-numbers, generic callsigns like
    # VFR, and blanks are dropped before anything else sees them.
    # This is the class that produced a 720-degree "hold" out of a
    # dozen unrelated aircraft all squawking VFR.
    _nga = sum(1 for r in _other if not _TK.is_commercial(r["cs"]))
    _other = [r for r in _other if _TK.is_commercial(r["cs"])]
    # CLASS B ON = CLASS B TRAFFIC ONLY. With the rings drawn, the
    # aircraft shown are the ones inside the Class B lateral boundary
    # — any of its shelves, floor ignored — which is the terminal
    # picture the rings exist to frame. Applied here, before parked,
    # fleet and trails, so everything downstream follows.
    _ncb = 0
    if show_cb and data.get("classb"):
        from core.flow import pip as _pip_cb

        _cb_polys = [c["polygon"] for c in data["classb"] if c.get("polygon")]

        def _in_cb(r):
            return any(_pip_cb(r["lon"], r["lat"], _p) for _p in _cb_polys)

        _before = len(_ac) + len(_other)
        _ac = [r for r in _ac if _in_cb(r)]
        _other = [r for r in _other if _in_cb(r)]
        _ncb = _before - len(_ac) - len(_other)
    # PARKED JETBLUE AT JFK go in their own layer, sized in real
    # metres, so they are sub-pixel at 300 nm and full size on the
    # ramp — visible only when you have zoomed in to look at the
    # ramp. They are pulled out BEFORE the declutter so the ramp
    # rule (which would hide them) never sees them.
    _jla, _jlo, _ = AIRPORTS["KJFK"]
    _parked, _ac = [], list(_ac)
    for _r in list(_ac):
        _alt = _r.get("alt")
        _isparked = ((_alt is None or _alt <= DECLUTTER_ALT_FT)
                     and (_r.get("gs") or 0) <= DECLUTTER_GS_KT)
        _djfk = math.hypot((_r["lon"] - _jlo) * 60.0
                           * math.cos(math.radians(_jla)),
                           (_r["lat"] - _jla) * 60.0)
        if _isparked and _djfk <= 3.0:
            _parked.append(_r)
            _ac.remove(_r)
    # SURFACE STATUS for everyone on the ground at JFK — JetBlue in
    # _parked, the others still in _other. Gate, taxiway or runway,
    # from the OpenStreetMap surface the warmer cached to static/.
    try:
        from core import surface as _SF

        _surf = _SF.load(_STATIC)
    except Exception:
        _surf = {}
    if _surf:
        for _r in _parked + [o for o in _other
                             if (o.get("alt") is None or o.get("alt", 0)
                                 <= DECLUTTER_ALT_FT)
                             and (o.get("gs") or 0) <= DECLUTTER_GS_KT]:
            _kind, _ref = _SF.classify(_surf, _r["lon"], _r["lat"])
            _moving = (_r.get("gs") or 0) > 4
            _r["surface"] = _kind
            _lbl = {"runway": f"ON RUNWAY {_ref}" if _ref else "ON RUNWAY",
                    "gate": f"at gate {_ref}" if _ref else "at gate",
                    "taxiway": (f"taxiing on {_ref}" if _ref else "taxiing")
                    if _moving else (f"holding on {_ref}" if _ref
                                     else "stopped on taxiway"),
                    "apron": "on the ramp", "ground": "on the ground"}[_kind]
            _r["tip"] = _r["tip"] + " | " + _lbl
    _ac, _h1 = _declutter(_ac)
    _other, _h2 = _declutter(_other)
    # Regional fleet layered on top. Local rows win on a hex clash:
    # they are a minute old at most, the sweep can be two.
    _regnote = _ocnote = ""
    _ac_local = list(_ac)
    if show_fleet or _zones or (show_oc and data.get("oceanic")):
        _flrows, _flnote = _frozen(
            "fleet", lambda: fleet_sweep(_zones, oc_all and show_oc))
        _have = ({r["hex"] for r in _ac if r["hex"]}
                 | {r["hex"] for r in _other if r["hex"]})
        _keep = [r for r in _flrows if r["hex"] not in _have
                 and ((show_fleet and r.get("in_region"))
                      or (show_oc and r.get("on_lroute"))
                      or r.get("zone") in _zones)]
        # JetBlue joins the fleet list; other airlines from the
        # L-routes join the OTHER list so they get their own icons.
        _ac = _ac + [r for r in _keep if r["cs"].startswith("JBU")]
        _other = _other + [r for r in _keep if not r["cs"].startswith("JBU")]
        _regnote = _flnote
    # Destination colouring, JetBlue only, AFTER the merge so the
    # regional aircraft are included. One batched call for the whole
    # fleet rather than one per aircraft.
    _dbkt = _tb[:-1]          # 10 min bucket, matches the 300 s TTL
    # Everyone on screen, not JetBlue only: the metro filter below
    # needs the other operators' routes too.
    _dests = _frozen("routes", lambda: route_dests(
        tuple((r["cs"], round(r["lat"], 2), round(r["lon"], 2))
              for r in (_ac + _other) if r["cs"]), _dbkt))
    _nmetro = 0
    if metro_filter and _other:
        _other, _nmetro = metro_only(_other, _dests)
    _narr = mark_arrivals(_ac, _dests)
    # Feed the tracker. It also polls on its own every 2 min from a
    # background thread, so history builds whether or not anyone is
    # looking; a rerun just adds a fix.
    # THE PAGE NO LONGER FEEDS THE TRACKER. Its positions come from a
    # cache up to 60 s old; the background thread records fresh ones
    # every 120 s. Feeding both appended a stale fix after a fresh one
    # on every rerun and every trail grew a backward hook at its end.
    # The thread is the single source of fixes; the page only reads.
    if not _scrubbed:
        try:
            _TK.update_holds()
        except Exception:
            pass
    # N90 FLOW: who is inside, which way, and the live rates. Snapshot
    # from the rows on screen; rates from hull crossings in the track
    # history. Frozen with everything else while scrubbed.
    try:
        from core import flow as _FW

        _hullp = (data.get("hull") or [{}])[0].get("polygon") or []
        def _aar_now():
            try:
                _rw, _up, _er = aadc(_tb[:-1])
                if _rw and not _er:
                    _c60 = aadc_rollup(_rw, 60)[1]
                    return (_c60[0].get("cap") or None) if _c60 else None
            except Exception:
                return None
            return None

        _comm = [r for r in _ac + _other if _TK.is_commercial(r.get("cs"))]
        _flow = _frozen("flow", lambda: {
            "snap": _FW.classify(_comm, _hullp, _dests),
            "x": _FW.crossings(_TK.snapshot(), _hullp),
            "aar": _aar_now(),
            "press": _FW.project_arrivals(_comm, _hullp, _aar_now(),
                                          dests=_dests),
        })
    except Exception as _fexc:
        _flow = {"err": f"{type(_fexc).__name__}: {_fexc}"}
    for _r in _ac + _other:
        if not _TK.is_commercial(_r.get("cs")):
            continue
        _why = _EMERG_SQ.get(_r.get("sq")) or _EMERG_WORDS.get(_r.get("emerg"))
        if _why:
            _r["emerg_why"] = _why
            _emerg_rows.append(_r)
    # ARRIVAL PRESSURE. Amber at the rate, red exceeding; nothing when
    # under, because a banner that is always there is not an alert.
    # The hold detector is the check on this: exceeding with nobody
    # holding means the model is early, holding while "at rate" means
    # it is late.
    _pr = (_flow or {}).get("press") or {}
    if _pr.get("state") in ("at_rate", "exceeding"):
        from html import escape as _esc2

        _red = _pr["state"] == "exceeding"
        _hold_n = len(_hold_cs)
        st.markdown(
            "<div style='background:" + ("#B00020" if _red else "#B36B00")
            + ";border:4px solid " + ("#5A0010" if _red else "#5A3600")
            + ";border-radius:8px;padding:12px 18px;margin:8px 0;"
            "color:#FFFFFF;'>"
            "<div style='font-size:22px;font-weight:800;letter-spacing:1px;"
            "margin-bottom:4px;'>"
            + ("\u26a0 N90 ARRIVAL TRAFFIC IS EXCEEDING THE LANDING RATE"
               if _red else "\u25b2 N90 ARRIVAL TRAFFIC AT THE LANDING RATE")
            + "</div><div style='font-size:16px;'>" + _esc2(_pr["text"])
            + (f" {_hold_n} aircraft currently holding." if _hold_n else
               " No holds detected yet.")
            + "</div></div>", unsafe_allow_html=True)
    if _emerg_rows:
        from html import escape as _esc

        _el = "".join(
            f"<div>\u26a0 <b>{_esc(_who(r['cs']))}</b> squawking "
            f"<b>{_esc(r.get('sq') or r.get('emerg', ''))}</b> \u2014 "
            f"{_esc(r['emerg_why'])} \u2014 {_esc(_fl(r.get('alt')))}, "
            f"{r.get('gs', 0):.0f} kt"
            + (f", {_esc(r['tip'].split(' | ')[-1])}" if ' | ' in r['tip'] else "")
            + "</div>" for r in _emerg_rows)
        st.markdown(
            "<div style='background:#7B0060;border:4px solid #3D0030;"
            "border-radius:8px;padding:14px 18px;margin:8px 0;"
            "color:#FFFFFF;'>"
            "<div style='font-size:26px;font-weight:800;"
            "letter-spacing:1px;margin-bottom:6px;'>\u26a0 EMERGENCY "
            "SQUAWK</div>"
            "<div style='font-size:19px;'>" + _el + "</div></div>",
            unsafe_allow_html=True)
    # Outline: which way across the N90 edge. Runs after the merge so
    # regional aircraft are included, and after mark_arrivals because
    # the two write different fields.
    _nin, _nout = mark_crossings(
        _ac, (data.get("hull") or [{}])[0].get("polygon") or [])
    if _h1 + _h2:
        _acnote += (f"; {_h1 + _h2} parked hidden inside "
                    f"{DECLUTTER_SM:.0f} sm of airports")
    # Other traffic first, so JetBlue draws on top of it. Deliberately
    # UNLABELLED: 40 nm around New York holds a few hundred aircraft
    # and labelling them all would bury the airspace underneath. The
    # callsign is still in the tooltip.
    if _other and not jbu_only:
        # TWO TIERS. The majors — Delta, United, American, Southwest —
        # draw at three-quarters of JetBlue; everyone else (business
        # jets, cargo, GA that survived the filters) stays at the
        # small baseline. Pixel clamps are per LAYER in deck.gl, so
        # this has to be two layers, not a per-row multiplier.
        _majors = [r for r in _other
                   if r["cs"][:3] in _OPERATOR_STROKE]
        _minor = [r for r in _other
                  if r["cs"][:3] not in _OPERATOR_STROKE]
        # SCOPE TARGETS. One square for everyone, tinted per operator
        # through the icon mask, never rotated — a STARS target does
        # not turn with the aircraft; the trail carries the heading.
        # The JetBlue size slider still scales JetBlue and the majors.
        if _minor:
            layers.append(pdk.Layer(
                "IconLayer", data=_minor, get_position="[lon, lat]",
                get_icon="atc", get_color="acol", get_size=26,
                size_min_pixels=18, size_max_pixels=32, pickable=True))
        if _majors:
            layers.append(pdk.Layer(
                "IconLayer", data=_majors, get_position="[lon, lat]",
                get_icon="atc", get_color="acol", get_size=26,
                size_min_pixels=18, size_max_pixels=32, pickable=True))
            layers.append(pdk.Layer(
                "TextLayer", data=_majors, get_position="[lon, lat]",
                get_text="block", get_size=11, get_color="acol",
                font_family="monospace", font_weight=700,
                get_text_anchor='"start"', get_alignment_baseline='"bottom"',
                line_height=1.1,
                get_pixel_offset=[13, -11],
                background=True, get_background_color=[255, 255, 255, 215],
                background_padding=[3, 1, 3, 1]))
    # SOLID TRAILS under the icons, in a lighter shade of each
    # airline's colour, for every aircraft ON THE MAP. The banners
    # above the map carry every alert; a trail only says where. History is kept for everyone within 250 nm; only aircraft
    # that survived the filters get drawn.
    _win_s = {"15 min": 900, "30 min": 1800, "45 min": 2700,
              "1 hr": 3600}.get(track_win)
    try:
        _anchors = {r["hex"]: (r["lon"], r["lat"])
                    for r in _ac + _other if r.get("hex")}
        _trail = _frozen(
            "trail", lambda: _TK.trail_paths(max_age_s=_win_s,
                                             anchors=_anchors)
            if _win_s else [])
    except Exception:
        _trail = []
    _shown = {r["hex"] for r in _ac if r["hex"]} | {r["hex"] for r in _other if r["hex"]}
    _trail = [d for d in _trail if d["hex"] in _shown]
    if _trail:
        def _light(hexcol, mix=0.55):
            """The airline colour mixed toward white: the trail reads
            as 'that airline' without competing with the icon."""
            h = hexcol.lstrip("#")
            return [int(int(h[i:i + 2], 16) + (255 - int(h[i:i + 2], 16)) * mix)
                    for i in (0, 2, 4)] + [200]
        _tcache = {}
        for _d in _trail:
            _pre = (_d.get("cs") or "")[:3]
            if _pre not in _tcache:
                _base = ("#005ADC" if _pre == "JBU" else
                         _OPERATOR_FILL.get(_pre, _OTHER_FILL))
                _tcache[_pre] = _light(_base)
            _d["color"] = _tcache[_pre]
        # 1.6 px with rounded joints and caps, so the spline reads as
        # one continuous stroke rather than a chain of segments. The
        # smoothing itself is a Catmull-Rom through every fix — see
        # core/tracks.py — so nothing is moved where the data is.
        # TRAILS ARE THE HISTORY on the scope too: the smoothed solid
        # trail in a lighter operator colour, not STARS history dots.
        layers.append(pdk.Layer(
            "PathLayer", data=_trail, get_path="path",
            get_color="color", get_width=1.6,
            width_units='"pixels"', width_min_pixels=1,
            width_max_pixels=2, joint_rounded=True, cap_rounded=True,
            pickable=False))
    # AIRPORT SURFACE at ramp zoom: runways, taxiways, aprons, gate
    # labels. Line widths are in METRES, so the layer is sub-pixel at
    # 300 nm and a diagram at z13 — same trick as the parked icons.
    if _surf and show_ap:
        if _surf.get("aprons"):
            layers.append(pdk.Layer(
                "PolygonLayer", data=_surf["aprons"],
                get_polygon="polygon", filled=True, stroked=False,
                get_fill_color=[150, 150, 150, 60], pickable=False))
        if _surf.get("taxiways"):
            layers.append(pdk.Layer(
                "PathLayer", data=_surf["taxiways"], get_path="path",
                get_color=[200, 170, 40, 190], get_width=18,
                width_units='"meters"', width_min_pixels=0,
                width_max_pixels=6, pickable=False))
        if _surf.get("runways"):
            layers.append(pdk.Layer(
                "PathLayer", data=_surf["runways"], get_path="path",
                get_color=[60, 60, 60, 230], get_width=45,
                width_units='"meters"', width_min_pixels=0,
                width_max_pixels=12, pickable=False))
        if _surf.get("gates"):
            _gl = [g for g in _surf["gates"] if g.get("ref")]
            layers.append(pdk.Layer(
                "TextLayer", data=_gl, get_position="[lon, lat]",
                get_text="ref", get_size=22, size_units='"meters"',
                size_min_pixels=0, size_max_pixels=11,
                get_color=[40, 40, 40], background=True,
                get_background_color=[255, 255, 255, 200],
                background_padding=[2, 1, 2, 1]))
    if _parked:
        # Real-world size, zero pixel floor. At z9 this is 1.3 px and
        # invisible; at z12 it is 10 px; on the ramp at z13-14 it is
        # a full icon. Nothing about the data changes with zoom —
        # only what the GPU draws it at.
        layers.append(pdk.Layer(
            "IconLayer", data=_parked, get_position="[lon, lat]",
            get_icon="icon", get_size="isize * 300",
            size_units='"meters"',
            size_min_pixels=0, size_max_pixels=28,
            get_angle="angle", pickable=True))
        layers.append(pdk.Layer(
            "TextLayer", data=_parked, get_position="[lon, lat]",
            get_text="cs", get_size=140, size_units='"meters"',
            size_min_pixels=0, size_max_pixels=11,
            get_color="lcolor", get_text_anchor='"start"',
            get_pixel_offset=[10, -10], background=True,
            get_background_color=[255, 255, 255, 225],
            background_padding=[3, 1, 3, 1]))
    if _ac:
        layers.append(pdk.Layer(
            "IconLayer", data=_ac, get_position="[lon, lat]",
            get_icon="atc", get_color="acol", get_size=26,
            size_min_pixels=18, size_max_pixels=32, pickable=True))
        layers.append(pdk.Layer(
            "TextLayer", data=_ac, get_position="[lon, lat]",
            get_text="block", get_size=11, get_color="acol",
            font_family="monospace", font_weight=700,
            get_text_anchor='"start"', get_alignment_baseline='"bottom"',
            line_height=1.1,
            get_pixel_offset=[13, -11],
            background=True, get_background_color=[255, 255, 255, 215],
            background_padding=[3, 1, 3, 1]))

# PIREPs draw LAST, over everything. A pilot report under an airway
# line is a report nobody sees.
_icenote = ""
if show_ice:
    _ipb = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y%m%d%H%M")[:-1]
    _ice, _icenote = _frozen("pireps", lambda: pireps(_ipb, _tr_nm if show_ac
                            else int(radius_nm)))
    if _ice:
        layers.append(pdk.Layer(
            "IconLayer", data=_ice, get_position="[lon, lat]",
            get_icon="icon", get_size=16, size_min_pixels=14,
            size_max_pixels=30, pickable=True))

if show_fix and data.get("fixes"):
    # Triangles ALWAYS, for every fix. The marker is cheap and never
    # collides; it is the chips that pile up.
    # SIZED IN METRES, CLAMPED IN PIXELS. deck.gl computes the screen
    # size per frame, so the triangles shrink as you zoom out and grow
    # as you zoom in with no rerun — 5 px at 300 nm, 14 px at terminal
    # zoom. A fixed 14 px was the right size at z9 and clutter at z6.
    # CHART GLYPHS: the waypoint star for RNAV fixes, the VOR/DME
    # symbol for navaids, tinted by role (green arrival, red
    # departure) — or video-map grey on the scope. Metre-sized with a
    # pixel clamp, as the triangles were.
    layers.append(pdk.Layer(
        "IconLayer", data=data["fixes"], get_position="[lon, lat]",
        get_icon="nav", get_size="nsize", size_units='"meters"',
        size_min_pixels=10, size_max_pixels=26,
        get_color=[138, 152, 168] if scope else "tcolor",
        pickable=True))
    # LABELS THIN OUT AS THE VIEW WIDENS. Chips draw at a fixed
    # SCREEN size while the fixes converge, so the busiest part of the
    # map becomes the least readable part — at 300 nm the gates around
    # the core render as one solid unreadable block.
    #
    # NOT filtered by role: every one of the 67 fixes is a gate
    # (36 dep, 24 coord, 7 both), so "gates only" removes nothing.
    # Thinned by SPACING instead — walk the list and keep a label only
    # if nothing already kept sits within LABEL_SEP nm, which scales
    # with the view. Every fix keeps its triangle and its tooltip
    # either way; only the chip is dropped.
    LABEL_SEP = radius_nm / 22.0        # ~14 nm at 300, ~4.5 at 100
    _lab, _kept = [], []
    # The operational nine first and unconditionally; the rest thin.
    for _f in sorted(data["fixes"], key=lambda f: not f.get("key")):
        _clat = math.cos(math.radians(_f["lat"]))
        if _f.get("key") or all(
                math.hypot((_f["lon"] - k[1]) * 60.0 * _clat,
                           (_f["lat"] - k[0]) * 60.0) >= LABEL_SEP
                for k in _kept):
            _kept.append((_f["lat"], _f["lon"]))
            _lab.append(_f)
    _nhid = len(data["fixes"]) - len(_lab)
    layers.append(pdk.Layer(
        "TextLayer", data=_lab, get_position="[lon, lat]",
        get_text="name", get_size=1800, size_units='"meters"',
        size_min_pixels=7, size_max_pixels=11,
        get_color=[138, 152, 168] if scope else "lcolor",
        font_family="monospace" if scope else "Helvetica",
        get_text_anchor='"start"', get_pixel_offset=[7, -7],
        background=not scope, get_background_color="chip",
        # No border: deck.gl TextLayer has no chip stroke, which is
        # exactly what scheme K wants.
        background_padding=[5, 2, 5, 2]))

if _emerg_rows:
    layers.append(pdk.Layer(
        "ScatterplotLayer", data=_emerg_rows, get_position="[lon, lat]",
        get_radius=1, radius_min_pixels=22, radius_max_pixels=22,
        stroked=True, filled=False, get_line_color=[123, 0, 96, 255],
        line_width_min_pixels=4, pickable=False))

# ---------------------------------------------------------------------------
# N90 flow tiles
# ---------------------------------------------------------------------------
if show_ac and _flow and "err" not in _flow:
    _sn, _xs = _flow["snap"], _flow["x"]
    _tile = ("<div style='flex:1;border:2px solid #000;background:#fff;"
             "padding:6px 10px;text-align:center;font-family:Times New "
             "Roman,serif'><div style='font-size:11px;letter-spacing:1px;"
             "color:#444'>{lbl}</div><div style='font-size:30px;"
             "font-weight:700;color:{col};line-height:1.05'>{val}</div>"
             "<div style='font-size:11px;color:#666'>{sub}</div></div>")
    _ba = _sn["by_airport"]
    _per = " &middot; ".join(
        f"{k} {v['arr']}/{v['dep']}" for k, v in _ba.items()
        if v["arr"] or v["dep"]) or "&mdash;"
    st.markdown(
        "<div style='display:flex;gap:8px;margin:6px 0'>"
        + _tile.format(lbl="IN N90 NOW", val=_sn["inside"], col="#000",
                       sub="commercial, inside the hull")
        + _tile.format(lbl="ARRIVING", val=len(_sn["arriving"]),
                       col="#1C8244", sub="descending in N90")
        + _tile.format(lbl="DEPARTING", val=len(_sn["departing"]),
                       col="#C62828", sub="climbing in N90")
        + _tile.format(lbl="ARRIVALS / HR", val=_xs["arr_last_hr"],
                       col="#1C8244", sub="crossed INTO N90, last 60 min")
        + _tile.format(lbl="DEPARTURES / HR", val=_xs["dep_last_hr"],
                       col="#C62828", sub="crossed OUT of N90, last 60 min")
        + "</div>"
        + f"<div style='font-family:Times New Roman,serif;font-size:12px;"
          f"color:#444;margin:-2px 0 6px 2px'>arr/dep by nearest airport: "
          f"{_per} &nbsp;&middot;&nbsp; {len(_sn['level'])} level "
          f"overflight(s). Vertical state from ADS-B baro_rate; rates from "
          f"hull crossings in the track history, so they need history to "
          f"exist \u2014 after a restart they start at zero.</div>",
        unsafe_allow_html=True)
elif show_ac and _flow and "err" in _flow:
    st.caption(f"N90 flow unavailable \u2014 {_flow['err']}")

_deck = pdk.Deck(
    layers=layers,
    initial_view_state=pdk.ViewState(
        latitude=N90_CENTER[0], longitude=N90_CENTER[1],
        zoom=_zoom_for(radius_nm), min_zoom=4, max_zoom=12),
    # Same basemap as the CONUS fleet map, so the two pages read as
    # one product. Every colour below is tuned for it.
    map_style="dark_no_labels" if scope else "light",
    # THE BOX IS THE AIRLINE'S COLOUR, white text, black border. The
    # markup lives in the template; {tcol} is a per-row hex on
    # aircraft. Rows without it — fixes, routes, PIREPs — substitute
    # an invalid value, and CSS keeps the EARLIER valid background:
    # two background declarations, dark grey first, so the fallback
    # costs no data changes. Outer style is made transparent so the
    # inner box is the only thing drawn.
    tooltip={
        "html": '<div style="background:#333333;background:{tcol};'
                'color:#FFFFFF;color:{ttxt};border:2px solid #000000;'
                'padding:6px 10px;font-weight:bold;'
                'font-family:Times New Roman,serif;">{tip}</div>',
        "style": {"backgroundColor": "transparent", "padding": "0",
                  "border": "none", "boxShadow": "none"},
    },
)

# A chart fills its container, so width is controlled by rendering
# into a column of the requested fraction and leaving the remainder
# empty.

# ---------------------------------------------------------------------------
# Arrival demand, under the map
# ---------------------------------------------------------------------------
# Rebuilt from the FAA's own AADC feed rather than iframed. Sits in
# the same column as the map so it tracks the width slider, and
# vega-lite sizes itself to that column, so it is responsive without
# any breakpoint handling.
def _render_aadc():
    from datetime import datetime, timezone

    _ab = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")[:-1]
    _raw, _meta, _err = aadc(_ab)
    _h1, _h2 = st.columns([3, 1])
    with _h1:
        st.markdown(f"**{AADC_AIRPORT} arrival demand** &mdash; FAA AADC")
    with _h2:
        _iv = st.selectbox("Interval", list(AADC_INTERVALS),
                           index=0, label_visibility="collapsed")
    if _err:
        st.warning(f"Arrival demand unavailable — {_err}")
        return
    if not _raw:
        st.caption("No arrival demand data returned.")
        return

    _mins = AADC_INTERVALS[_iv]
    _bars, _cap, _tot = aadc_rollup(_raw, _mins)
    st.vega_lite_chart(spec=aadc_spec(_bars, _cap, _tot),
                       width="stretch")

    _peak = max((t["over"] for t in _tot), default=0)
    _bits = []
    if _meta.get("control"):
        _bits.append(f"**{_meta['control']}**")
    if _meta.get("aar"):
        _bits.append(f"AAR {_meta['aar']}/hr")
    if _meta.get("total"):
        _bits.append(f"{_meta['total']} flights")
    if _meta.get("cancelled"):
        _bits.append(f"{_meta['cancelled']} cancelled")
    if _peak > 0:
        _bits.append(f"peak +{_peak:g} over capacity")
    st.caption(
        " &middot; ".join(_bits)
        + f" &middot; scroll to zoom, drag to pan, double-click to "
          f"reset; hover for the readout. Red step line is capacity "
          f"for a {_mins} min bin — the hourly AAR scaled to the bin, "
          f"not the raw rate. Source fly.faa.gov/aadc, updated "
          f"{_meta.get('when', '?')}."
    )


def _status_line():
    """STATUS LINE under the map on the scope: time, radar age, target
    count, the rate, holds. The banners above still carry the alerts;
    this is the glance a controller takes at the bottom of the display."""
    if not scope:
        return
    from datetime import datetime, timezone

    _g = globals()
    _st_ac = (len(_g.get("_ac", [])) + len(_g.get("_other", []))) if show_ac else 0
    _rs = _g.get("_rstamp")
    _st_rad = f"MRMS {_rs[9:11]}:{_rs[11:13]}Z" if _rs else "MRMS --"
    _hl = _g.get("_holding") or []
    _st_hold = (f"HOLD: {', '.join(_who(c) for c, *_ in _hl)}"
                if _hl else "HOLD: NONE")
    _flow = _g.get("_flow")
    _st_aar = ""
    try:
        if _flow and _flow.get("aar"):
            _st_aar = f"   AAR {int(_flow['aar'])}"
    except Exception:
        pass
    st.markdown(
        "<div style='background:#0B0F16;color:#8A98A8;font-family:monospace;"
        "font-size:12px;padding:4px 10px;letter-spacing:1px;margin-top:-6px'>"
        f"N90 SCOPE   {_st_rad}   {_st_ac} TGTS{_st_aar}   {_st_hold}   "
        f"{datetime.now(timezone.utc):%H%MZ}</div>", unsafe_allow_html=True)


if map_w >= 100:
    st.pydeck_chart(_deck, height=map_h)
    _status_line()
    _render_aadc()
else:
    _mc = st.columns([map_w, max(1, 100 - map_w)])
    with _mc[0]:
        st.pydeck_chart(_deck, height=map_h)
        _status_line()
        _render_aadc()

# ---------------------------------------------------------------------------
# N90 flow, last two hours
# ---------------------------------------------------------------------------
# Arrivals above the axis, departures below, per 15 min, from hull
# crossings in the track history. The dashed line is JFK's programmed
# arrival rate from the AADC, scaled to the bin — live arrivals into
# ALL of N90 against JFK's rate alone is not apples to apples, but it
# is the only published capacity number and it is the right order of
# magnitude to read the bars against.
if show_ac and _flow and "err" not in _flow:
    _aar = None
    try:
        _rw, _up, _er = aadc(datetime.now(timezone.utc).strftime("%Y%m%d%H%M")[:-1])
        if _rw and not _er:
            _c60 = aadc_rollup(_rw, 60)[1]
            _aar = (_c60[0].get("cap") or None) if _c60 else None
    except Exception:
        _aar = None
    st.markdown("**N90 flow** &mdash; arrivals into and departures out "
                "of the hull, per 15 min, last 2 h"
                + (f" &middot; dashed: JFK AAR {_aar}/h" if _aar else ""))
    st.vega_lite_chart(
        spec=_FW.flow_spec(_flow["x"]["bins"], _aar,
                           projected=(_flow.get("press") or {}).get("bins")),
        width="stretch")
    _pp = _flow.get("press") or {}
    if _pp.get("cap30"):
        st.caption(
            f"Right of the grey line: JFK arrivals PROJECTED from "
            f"aircraft now inbound, by ETA. Next 30 min: "
            f"{_pp['jfk_next30']} against {_pp['cap30']:.0f} the rate "
            f"allows ({_pp['ratio']:.0%}); peak queue "
            f"{_pp['backlog']:.0f}, est. delay {_pp['delay_min']} min.")

    # The gate rose moved to its own page — N90 Arrival Gate Status.
    st.caption("Arrival gates by direction, with projected load per "
               "gate: see **N90 Arrival Gate Status** in the sidebar.")

# ---------------------------------------------------------------------------
# Radar diagnostics
# ---------------------------------------------------------------------------
# ON THIS PAGE, because this is the page that gets screenshotted when
# radar is missing. Every question that distinguishes "warmer never
# ran" from "frames exist but the browser cannot fetch them" is
# answered here, in one box, with the frame URL as a link to click.
with st.expander("Radar diagnostics", expanded=not bool(_hist)):
    import os as _dos
    import json as _djs

    _ext = _dos.environ.get("RENDER_EXTERNAL_URL") or "MISSING"
    _kill = _dos.environ.get("MRMS_WARMER") or "(unset = on)"
    _pub = _dos.environ.get("PUBLIC_BASE_URL") or "(unset)"
    _sty_env = _dos.environ.get("MRMS_RENDER_STYLE") or "(unset)"
    try:
        from core import mrms as _DM

        _sty_code = _DM.RENDER_STYLE
        _has_hist = hasattr(_DM, "history")
        _has_bf = hasattr(_DM, "backfill")
        _res = (f"UPSAMPLE {_DM.UPSAMPLE} x DECIMATE {_DM.DECIMATE} "
                f"= {100 * _DM.UPSAMPLE // max(1, _DM.DECIMATE)} px/deg live, "
                f"LOOP_UPSAMPLE {_DM.LOOP_UPSAMPLE} = "
                f"{100 * _DM.LOOP_UPSAMPLE} px/deg loop")
        _res_env = ", ".join(f"{k}={_dos.environ[k]}" for k in (
            "MRMS_UPSAMPLE", "MRMS_DECIMATE", "MRMS_FINE_SMOOTH",
            "MRMS_SMOOTH", "MRMS_CHUNKS_X", "MRMS_CHUNKS_Y",
            "MRMS_LOOP_UPSAMPLE") if k in _dos.environ) or "(none set)"
        _res_bad = _DM.DECIMATE > 1 or _DM.UPSAMPLE < 4
    except Exception as _dexc:
        _sty_code, _has_hist, _has_bf = f"IMPORT FAILED: {_dexc}", False, False
        _res, _res_env, _res_bad = "?", "?", False
    # What is actually on screen right now: full chunks or a loop frame
    _drawn = "nothing"
    if _hist:
        _i = len(_hist) - 1 if _pick is None else _pick
        _st, _ch, _lp = _hist[_i]
        _live = _i == len(_hist) - 1
        if _live and _ch:
            _drawn = f"LIVE frame {_st}, {len(_ch)} full-res chunks"
        elif _lp:
            _drawn = (f"{'LIVE position but ' if _live else 'scrubbed to '}"
                      f"{_st}: LOOP frame ({len(_lp) if isinstance(_lp, list) else 1} "
                      f"tiles, lower resolution)"
                      + (" — newest manifest has NO chunks" if _live else ""))

    _mans = sorted(_STATIC.glob("mrmsc_*.json"))
    _loops = sorted(_STATIC.glob("mrmsl_*.webp"))
    _chunks_on_disk = sorted(_STATIC.glob("mrmsc_*.webp"))
    _newest_style = "n/a"
    if _mans:
        try:
            _newest_style = _djs.loads(_mans[-1].read_text()).get("style")
        except Exception as _mexc:
            _newest_style = f"unreadable ({type(_mexc).__name__})"

    import threading as _th
    import time as _tm
    _alive = ", ".join(t.name for t in _th.enumerate()
                       if "warmer" in t.name)
    try:
        import psutil as _ps
        _up = f"{(_tm.time() - _ps.Process().create_time()) / 60:.1f} min"
    except Exception:
        _up = "(psutil unavailable)"
    st.markdown(f"""
| | |
|---|---|
| warmer thread alive in this process | {"**yes** — " + _alive if _alive else "**NO** — never started, or died"} |
| process uptime | {_up} |
| `MRMS_WARMER` | `{_kill}`{" **← the warmer is OFF**" if _kill.lower() == "off" else ""} |
| `RENDER_EXTERNAL_URL` | `{_ext}` |
| `PUBLIC_BASE_URL` | `{_pub}` |
| **build** | {__import__("core.version", fromlist=["BUILD"]).BUILD} |
| `MRMS_RENDER_STYLE` env / code | `{_sty_env}` / `{_sty_code}` |
| **resolution in effect** | {_res}{" **← LOW: delete the MRMS_DECIMATE / MRMS_UPSAMPLE env vars**" if _res_bad else ""} |
| resolution env overrides | `{_res_env}` |
| **drawn on the map now** | {_drawn} |
| mrms.py has `history()` / `backfill()` | {_has_hist} / {_has_bf} |
| manifests on disk | {len(_mans)}{f", newest `{_mans[-1].name}` style {_newest_style}" if _mans else ""} |
| chunk files / loop frames on disk | {len(_chunks_on_disk)} / {len(_loops)} |
| frames `history()` returned | {len(_hist)}{f" — `{_hist_err}`" if _hist_err else ""} |
| static dir | `{_STATIC}` |
""")
    if _hist and _ext != "MISSING":
        _lp = _hist[-1][2]
        _lp = [_lp] if isinstance(_lp, dict) else (_lp or [])
        _fn = ((_hist[-1][1] or _lp) or [{"name": "?"}])[0]["name"]
        st.markdown(f"Newest frame, as the browser will request it: "
                    f"[{_fn}]({_ext}/app/static/{_fn}) \u2014 "
                    f"**open this link.** An image means the pipeline "
                    f"works and the layer is the problem; a 404 means "
                    f"static serving is not reaching `static/`.")
    _lg = _STATIC / "mrms_warmer.log"
    if _lg.exists():
        try:
            _ll = _lg.read_text().splitlines()
            st.caption(f"mrms_warmer.log \u2014 last 12 of {len(_ll)} lines")
            st.code("\n".join(_ll[-12:]) or "(empty)")
        except OSError as _lexc:
            st.caption(f"log unreadable: {_lexc}")
    else:
        if _kill.lower() == "off":
            st.error("MRMS_WARMER is set to off in the environment, so "
                     "the warmer never starts. Delete that variable in "
                     "Render (or set it to on) and the service will "
                     "restart with radar.")
        else:
            st.error("mrms_warmer.log does not exist \u2014 the warmer "
                     "thread never reached its first log line. Either "
                     "the process is restarting before MRMS_DELAY_S "
                     "(45 s) elapses, or ensure_mrms_warmer() is not "
                     "being called.")

# ---------------------------------------------------------------------------
# ZNY SWAP forecast
# ---------------------------------------------------------------------------
with st.expander("ZNY SWAP forecast (CWSU)", expanded=False):
    _sw_age, _sw_when, _sw_err = swap_age(
        __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y%m%d%H"))
    if _sw_err:
        st.warning(f"SWAP graphic unavailable — {_sw_err}")
    else:
        # AGE FIRST, IMAGE SECOND. The file is overwritten in place,
        # so the picture cannot tell you how old it is.
        if _sw_age is None:
            st.caption("Issued time not reported by the server — "
                       "check the CWSU page before relying on this.")
        elif _sw_age > SWAP_STALE_MIN:
            st.error(
                f"STALE — issued {_sw_when}, "
                f"{_sw_age / 60:.1f} h ago. SWAP products are issued "
                f"daily; this one is old.")
        else:
            st.caption(f"Issued {_sw_when} "
                       f"({_sw_age / 60:.1f} h ago).")
        st.image(SWAP_IMG, width="stretch")
    st.caption(
        f"NWS New York CWSU &middot; [SWAP statement (PDF)]({SWAP_STMT})"
        f" &middot; [full page]({SWAP_PAGE}) &middot; the statement is a "
        f"forecast of the PROBABILITY of a SWAP, not an observation."
    )

# ---------------------------------------------------------------------------
# JFK operational status
# ---------------------------------------------------------------------------
# A TEXT BOX, not a map layer: a ground stop has no position. It sits
# under the map because it answers "what is happening to the
# operation", which is the question you ask after looking at the
# picture.
_nas_lines, _nas_upd, _nas_err = nas_status(
    __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y%m%d%H%M")[:-1])
with st.container(border=True):
    st.markdown(f"**{NAS_AIRPORT} — FAA NAS status**")
    if _nas_err:
        st.warning(f"NAS status unavailable — {_nas_err}")
    elif not _nas_lines:
        # CLEAR IS AN ANSWER. It must not look like a failed fetch,
        # which is why it says so rather than rendering nothing.
        st.caption("No ground stop, delay programme or closure "
                   "reported.")
    else:
        for _l in _nas_lines:
            st.markdown("- " + _l)
    st.caption(
        "Source: FAA ATCSCC via nasstatus.faa.gov"
        + (f" &middot; updated {_nas_upd}" if _nas_upd else "")
        + " &middot; advisory only, not an operational briefing."
    )

st.caption(
    (f"{_nhid} of {len(data['fixes'])} fix labels hidden at this "
     f"zoom to stop them overlapping — every fix keeps its triangle "
     f"and tooltip. Zoom in for the rest. "
     if show_fix and data.get("fixes") and _nhid else "")
    + "Fix chips: GREEN arrival gate, ROSE departure gate, SAND other "
    "nav aid. Orange outline is an APPROXIMATE N90 "
    "extent (hull of the fixes), NOT the delegated TRACON boundary — "
    "that geometry is not published in FAA open GIS. Blue: FAA New "
    "York Class B shelves. Grey: ARTCC high-sector boundaries. "
    f"Fix data extracted {data.get('fix_vintage', '?')}."
    + _radar_note
    + _tops_note
    + _l2_note
    + (f" {_icenote}." if _icenote else "")
    + (" Other operators hidden. " if (show_ac and jbu_only) else "")
    + (f" {_nmetro} overflights hidden (not JFK/LGA/EWR/TEB). "
       if (show_ac and metro_filter and _nmetro) else "")
    + (f" {_nga} general aviation hidden. " if (show_ac and _nga) else "")
    + (f" {_ncb} outside Class B hidden (Class B on). " if (show_ac and _ncb) else "")
    + (f" Traffic: {_acnote} within {_tr_nm} nm — "
       + (f" {len(_parked)} JetBlue parked at JFK, shown only when "
          f"zoomed to the ramp." if _parked else "")
       + f" JetBlue solid blue; GREEN entering N90 "
       f"({_nin}), RED leaving ({_nout}); GREEN label if bound for "
       f"JFK/LGA/EWR ({_narr} now); all others outline only. "
       f"Destinations are inferred, not filed."
       + (f" {_regnote}." if _regnote else "")
       + ((lambda _ts: (
              f" Trails: {_ts['with_trail']} of {_ts['tracked']} tracked "
              f"({', '.join(f'{k} {b[1]}/{b[0]}' for k, b in sorted(_ts['by_operator'].items(), key=lambda kv: -kv[1][0])[:6])})"
              f"; tracker thread {'alive' if _ts['thread_alive'] else 'NOT RUNNING'}"
              + (f", newest fix {_ts['newest_fix_age_s'] / 60:.0f} min ago" if _ts['newest_fix_age_s'] is not None else ", no fixes yet")
              + f"; {len(_trail)} trails drawn."))(_TK.track_summary())
          if show_ac else "")
       if show_ac else "")
)

# ---------------------------------------------------------------------------
# Winds and temperatures aloft, from the traffic already fetched
# ---------------------------------------------------------------------------
if show_ac and (_ac or _other):
    try:
        from core import adsb_wind as _AW

        # TERMINAL AIRCRAFT ONLY — _ac_local, never the merged _ac.
        # profile() averages wind components inside a flight-level
        # bin. Aircraft are only comparable if they are in the same
        # air; fold in the regional sweep and an FL300 bin averages
        # Cleveland against Norfolk and returns a confident number
        # that describes nowhere. It would not error, which is what
        # makes it dangerous on an aviation field.
        _wobs, _wstats = _AW.observations(
            list(_ac_local) + list(_other))
        _wprof = _AW.profile(_wobs, bin_ft=4000, min_n=2)
    except Exception as _wexc:
        _wobs, _wstats, _wprof = [], {"error": str(_wexc)}, []
    with st.expander(
            f"Winds aloft from aircraft within "
            f"{_tr_nm} nm "
            f"({_wstats.get('used', 0)} of {_wstats.get('seen', 0)} "
            f"reporting)"):
        if _wprof:
            st.dataframe(
                [{"Layer": f"FL{p_['fl_lo']:03d}-{p_['fl_hi']:03d}",
                  "Wind": f"{p_['dir_from_deg']:.0f}\u00b0 / "
                          f"{p_['speed_kt']:.0f} kt",
                  "OAT": (f"{p_['oat_c']:.0f} \u00b0C"
                          if p_['oat_c'] is not None else "\u2014"),
                  "Aircraft": p_["n"]} for p_ in _wprof],
                width="stretch", hide_index=True)
            st.caption(
                "Derived from each aircraft's true airspeed and "
                "heading against its groundspeed and track — the "
                "difference between the two vectors IS the wind. "
                "Components are averaged, not directions: 350\u00b0 "
                "and 010\u00b0 average to 180\u00b0, the opposite "
                "of the truth. Temperature is the aircraft's own "
                "OAT probe."
            )
        else:
            st.caption(
                f"No usable reports. {_wstats.get('no_modes', 0)} "
                f"aircraft lacked Mode S airspeed/heading, "
                f"{_wstats.get('turning', 0)} were turning, "
                f"{_wstats.get('too_low', 0)} too low. Those fields "
                f"come from enhanced surveillance downlinks, not "
                f"standard ADS-B, so coverage is a subset."
            )

with st.expander("Planned additions"):
    st.markdown(
        "**Commonly used routes.** Needs a source for the route "
        "strings — the FAA preferred-route database and CDRs, or "
        "JetBlue's own filed-route history, which would be more "
        "representative of what actually gets flown. The fix "
        "coordinates needed to draw them are already loaded.\n\n"
        "**Live aircraft, all operators.** Page 3's fleet fetcher "
        "already sweeps ADS-B tiles over this area and then filters "
        "to JetBlue callsigns. Showing everyone is a filter change, "
        "not a new feed — but expect a large jump in aircraft count "
        "inside this box, so thinning and a callsign filter matter.\n\n"
        "**CWSU / SWAP / TMI.** No single documented CWSU API "
        "exists. Candidates: ATCSCC advisories, NAS Status, and the "
        "ZNY CWSU OIS page — different formats, different refresh "
        "rates, and an OIS scrape is a fragile dependency for an "
        "ops tool. Worth deciding the source before writing code, "
        "and worth checking whether JetBlue already ingests TMI "
        "data internally."
    )
