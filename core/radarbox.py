"""AirNav RadarBox On Demand API (11 Oct): JetBlue arrivals for one
station - what is scheduled in, and where the airborne ones are.

Pricing is PER RESULT, not per call (one JetBlue schedules call for
JFK over 9 h returned 25 flights = 25 credits; a live-flights call
costs one credit per aircraft returned). The month's allowance is
small (10,000 on the starter plan), so this module is deliberately
stingy:

  * one station is fetched only when someone is looking at it, and
    the answer is cached for TTL_MIN (3 h) for every viewer;
  * a local counter and the free /billing/status endpoint keep the
    month under RBX_MONTH_CAP; past the cap the cached answer (or a
    note) is served and nothing is spent;
  * the token comes from mac/.env (RADARBOX_TOKEN); without it the
    pod says so and spends nothing.

Two calls per station:
  GET  /flights/schedules?toAirport=K...&airline=JBU&arrival window
       -> every JetBlue arrival in the window, scheduled times only
  POST /flights/live {airlines:[JBU], toAirports:[K...]}
       -> the ones already airborne, with estimated arrival
The pod merges them by flight number: scheduled -> airborne (ETA,
early/late vs schedule) -> landed.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.airnavradar.com/v2"
STATIC = Path(__file__).resolve().parent.parent / "static"
CACHE = STATIC / "rbx"
TTL_MIN = int(os.environ.get("RBX_TTL_MIN", "180"))
MONTH_CAP = int(os.environ.get("RBX_MONTH_CAP", "9000"))
HOURS_BACK = int(os.environ.get("RBX_HOURS_BACK", "1"))
HOURS_AHEAD = int(os.environ.get("RBX_HOURS_AHEAD", "10"))
LIVE_ON = os.environ.get("RBX_LIVE", "on").lower() != "off"
AIRLINE = os.environ.get("RBX_AIRLINE", "JBU")
# A JFK window costs ~25 (schedules) + ~15 (live); the guard refuses a
# fetch that could not fit under the cap.
EST_COST = int(os.environ.get("RBX_EST_COST", "60"))


def token() -> str:
    return (os.environ.get("RADARBOX_TOKEN")
            or os.environ.get("RADARBOX_API_KEY") or "").strip()


# ------------------------------------------------------------- usage
def _usage_path() -> Path:
    return STATIC / "rbx_usage.json"


def usage() -> dict:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        d = json.loads(_usage_path().read_text())
        if d.get("month") == month:
            return d
    except Exception:
        pass
    return {"month": month, "used": 0, "calls": 0, "by": {}}


def _add_usage(n: int, icao: str) -> None:
    u = usage()
    u["used"] = int(u.get("used", 0)) + int(n)
    u["calls"] = int(u.get("calls", 0)) + 1
    u["by"][icao] = int(u["by"].get(icao, 0)) + int(n)
    p = _usage_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(u))
    os.replace(tmp, p)


def budget() -> dict:
    u = usage()
    return {"used": u["used"], "cap": MONTH_CAP, "left": MONTH_CAP - u["used"],
            "calls": u["calls"], "by": u["by"], "month": u["month"]}


# ----------------------------------------------------------- requests
def _get(path: str, params: dict = None, timeout: int = 20) -> dict:
    import requests

    r = requests.get(API + path, params=params or {}, timeout=timeout,
                     headers={"Authorization": f"Bearer {token()}"})
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict, timeout: int = 20) -> dict:
    import requests

    r = requests.post(API + path, json=body, timeout=timeout,
                      headers={"Authorization": f"Bearer {token()}",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def billing_status() -> dict | None:
    """Free call: the account's own view of the month."""
    try:
        return _get("/billing/status", timeout=10)
    except Exception:
        return None


# ------------------------------------------------------------- fetch
def _cache_path(icao: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"arrivals_{icao}.json"


def cached(icao: str) -> dict | None:
    try:
        return json.loads(_cache_path(icao).read_text())
    except Exception:
        return None


def age_min(doc: dict) -> float:
    try:
        t = datetime.fromisoformat(doc["fetched_iso"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 60
    except Exception:
        return 1e9


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _slim(f: dict) -> dict:
    keys = ("callsign", "flightNumberIata", "flightNumberIcao", "aircraftRegistration",
            "aircraftType", "depAirportIcao", "depAirportIata", "depAirportCity",
            "scheduledDeparture", "estimatedDeparture", "actualDeparture", "actualTakeoff",
            "scheduledArrival", "estimatedArrival", "actualLanding", "status",
            "latitude", "longitude", "heading", "distance", "duration", "plannedDuration",
            "depAirportLatitude", "depAirportLongitude", "arrAirportLatitude", "arrAirportLongitude")
    return {k: f.get(k) for k in keys if f.get(k) is not None}


def fetch(icao: str, force: bool = False) -> tuple[dict | None, str]:
    """(doc, note). doc = {fetched_iso, icao, cost, flights:[...]} where
    each flight is the schedules record overlaid with the live one.
    Serves the cache inside TTL_MIN, or past the cap, or on error."""
    icao = icao.upper()
    doc = cached(icao)
    if doc and not force and age_min(doc) < TTL_MIN:
        return doc, ""
    if not token():
        return doc, "RADARBOX_TOKEN is not set"
    b = budget()
    if b["left"] < EST_COST:
        return doc, (f"RadarBox month cap reached ({b['used']:,} / {b['cap']:,} credits);"
                     " showing the last fetch")
    now = datetime.now(timezone.utc)
    cost = 0
    try:
        sched = _get("/flights/schedules", {
            "toAirport": icao, "airline": AIRLINE,
            "arrivalFromDate": _iso(now - timedelta(hours=HOURS_BACK)),
            "arrivalToDate": _iso(now + timedelta(hours=HOURS_AHEAD))})
        cost += int(sched.get("cost") or 0)
        flights = {}
        for f in sched.get("flights") or []:
            key = f.get("flightNumberIata") or f.get("callsign")
            if key:
                flights[key] = _slim(f)
        if LIVE_ON:
            live = _post("/flights/live", {"airlines": [AIRLINE], "toAirports": [icao]})
            cost += int(live.get("cost") or 0)
            for f in live.get("flights") or []:
                key = f.get("flightNumberIata") or f.get("callsign")
                if not key:
                    continue
                s = _slim(f)
                s["live"] = True
                flights[key] = {**flights.get(key, {}), **s}
    except Exception as exc:
        return doc, f"RadarBox fetch failed: {type(exc).__name__}: {exc}"
    _add_usage(cost, icao)
    doc = {"fetched_iso": _iso(now), "icao": icao, "cost": cost,
           "window_h": [HOURS_BACK, HOURS_AHEAD],
           "flights": sorted(flights.values(),
                             key=lambda f: f.get("estimatedArrival") or f.get("scheduledArrival") or "")}
    p = _cache_path(icao)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)
    return doc, ""


# ------------------------------------------------------------ display
def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def rows(doc: dict) -> list[dict]:
    """Display rows: flight, from, STA, ETA, delta minutes, phase."""
    out = []
    now = datetime.now(timezone.utc)
    for f in doc.get("flights") or []:
        sta = _dt(f.get("scheduledArrival"))
        eta = _dt(f.get("estimatedArrival")) or sta
        landed = _dt(f.get("actualLanding"))
        airborne = bool(f.get("actualTakeoff")) and not landed
        if landed:
            phase = "landed"
        elif airborne or f.get("status") == "IN_FLIGHT":
            phase = "airborne"
        elif f.get("actualDeparture"):
            phase = "taxiing"
        else:
            phase = "scheduled"
        delta = None
        if sta and eta:
            delta = round((eta - sta).total_seconds() / 60)
        when = landed or eta
        out.append({
            "flight": f.get("flightNumberIata") or f.get("callsign") or "",
            "callsign": f.get("callsign") or "",
            "from": f.get("depAirportIcao") or f.get("depAirportIata") or "",
            "from_city": f.get("depAirportCity") or "",
            "type": f.get("aircraftType") or "",
            "reg": f.get("aircraftRegistration") or "",
            "sta": sta, "eta": eta, "landed": landed, "when": when,
            "delta_min": delta, "phase": phase,
            "mins_out": (round((eta - now).total_seconds() / 60) if eta and phase != "landed" else None),
            "lat": f.get("latitude"), "lon": f.get("longitude"),
        })
    out.sort(key=lambda r: (r["when"] or datetime.max.replace(tzinfo=timezone.utc)))
    return out


# ------------------------------------------------- one aircraft (card)
CARD_TTL_MIN = int(os.environ.get("RBX_CARD_TTL_MIN", "10"))
_card_cache: dict = {}      # callsign -> (time.time(), record | None)


def flight_card(callsign: str) -> tuple[dict | None, str]:
    """The live record for one callsign (JBUxxxx), 1 credit, cached
    CARD_TTL_MIN. (record, note)."""
    cs = (callsign or "").strip().upper()
    if not cs:
        return None, ""
    hit = _card_cache.get(cs)
    if hit and time.time() - hit[0] < CARD_TTL_MIN * 60:
        return hit[1], ""
    if not token():
        return None, "RADARBOX_TOKEN is not set"
    b = budget()
    if b["left"] < 5:
        return None, f"RadarBox month cap reached ({b['used']:,} / {b['cap']:,})"
    ids = [cs]
    if cs.startswith("JBU"):
        ids.append("B6" + cs[3:])
    try:
        live = _post("/flights/live", {"flightIds": ids})
    except Exception as exc:
        return None, f"RadarBox: {type(exc).__name__}: {exc}"
    _add_usage(int(live.get("cost") or 0), "card")
    rec = None
    for f in live.get("flights") or []:
        if (f.get("callsign") or "").upper() == cs or (f.get("flightNumberIata") or "").upper() in ids:
            rec = _slim(f)
            rec["airlineName"] = f.get("airlineName")
            rec["depAirportName"] = f.get("depAirportName")
            rec["arrAirportName"] = f.get("arrAirportName")
            rec["arrAirportIcao"] = f.get("arrAirportIcao")
            rec["arrAirportCity"] = f.get("arrAirportCity")
            break
    _card_cache[cs] = (time.time(), rec)
    return rec, ("" if rec else f"{cs}: no live record at RadarBox")


def gc_points(lat1, lon1, lat2, lon2, step_nm: float = 25.0) -> list:
    """Points along the great circle, every step_nm, as [lon, lat]."""
    import math
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d < 1e-9:
        return [[lon1, lat1]]
    n = max(2, int(d * 3440.065 / step_nm))
    out = []
    for i in range(n + 1):
        f = i / n
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        out.append([math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.sqrt(x * x + y * y)))])
    return out


def card_text(callsign: str, rec: dict | None, note: str = "", alt=None, gs=None,
              wx: str = "") -> str:
    """Plain-text card (15 Oct) for a deck.gl TextLayer drawn NEXT TO
    the aircraft on the CONUS map, tethered by a line. Monospace rows,
    same content as card_html."""
    cs = (callsign or "").upper()
    iata = ("B6" + cs[3:]) if cs.startswith("JBU") else ""

    def _t(s):
        d = _dt(s)
        return d.strftime("%H:%MZ") if d else "--"

    def _fl(a):
        if a is None:
            return "n/a"
        a = float(a)
        return f"FL{int(round(a / 100)):03d}" if a >= 18000 else f"{int(round(a)):,} ft"

    spd = (str(int(round(float(gs)))) + " kt") if gs not in (None, "") else "n/a"
    W = 34
    head = cs + (f" / {iata}" if iata else "")
    if wx:
        head = head + " " * max(1, W - len(head) - len(wx)) + wx
    lines = [head, "-" * W]
    if rec:
        status = (rec.get("status") or "").replace("_", " ").title()
        eta, sta = _dt(rec.get("estimatedArrival")), _dt(rec.get("scheduledArrival"))
        delta = round((eta - sta).total_seconds() / 60) if eta and sta else None
        dtxt = f" ({delta:+d} min)" if delta is not None and abs(delta) > 5 else ""
        typ = (rec.get("aircraftType") or "") + (
            (" " + rec["aircraftRegistration"]) if rec.get("aircraftRegistration") else "")
        lines += [
            f"{typ:<{W - len(_fl(alt)) - len(spd) - 3}}{_fl(alt)} / {spd}"[:W + 4],
            f"STD {_t(rec.get('scheduledDeparture'))}"
            + (f"  off {_t(rec.get('actualTakeoff'))}" if rec.get("actualTakeoff") else "")
            + f"  {status}",
            f"STA {_t(rec.get('scheduledArrival'))}  ETA {_t(rec.get('estimatedArrival'))}{dtxt}"
            + (f"  landed {_t(rec.get('actualLanding'))}" if rec.get("actualLanding") else ""),
            f"ORIGIN:      {rec.get('depAirportIcao') or ''}  {rec.get('depAirportCity') or ''}",
            f"DESTINATION: {rec.get('arrAirportIcao') or ''}  {rec.get('arrAirportCity') or ''}",
        ]
    else:
        lines.append(f"Alt / spd  {_fl(alt)} / {spd}")
        if note:
            lines.append(note)
    return "\n".join(lines)


def card_html(callsign: str, rec: dict | None, note: str = "", alt=None, gs=None,
              wx: str = "", color=None) -> str:
    """Compact aircraft card for the map's bottom-right corner. wx =
    the driving METAR condition at the destination (e.g. "TSRA in
    METAR"); when given, the card is tinted with the aircraft's colour
    and the condition sits top right."""
    from html import escape

    cs = escape((callsign or "").upper())
    iata = ("B6" + cs[3:]) if cs.startswith("JBU") else ""

    def _t(s):
        d = _dt(s)
        return d.strftime("%H:%MZ") if d else "--"

    def _fl(a):
        if a is None:
            return "n/a"
        a = float(a)
        return f"FL{int(round(a / 100)):03d}" if a >= 18000 else f"{int(round(a)):,} ft"

    spd = (str(int(round(float(gs)))) + " kt") if gs not in (None, "") else "n/a"
    r, g, b = (color or [0, 90, 220])[:3]
    bg = f"rgba({r},{g},{b},0.22)" if wx else "#0A0A0A"
    edge = f"rgb({r},{g},{b})" if wx else "#2D3957"
    wx_html = ('<span class="wx">' + escape(wx) + '</span>') if wx else ""
    rows = f'<div class="row"><span class="k">Alt / spd</span><span class="v">{_fl(alt)} &middot; {spd}</span></div>'
    legs = ""
    if rec:
        status = (rec.get("status") or "").replace("_", " ").title()
        eta, sta = _dt(rec.get("estimatedArrival")), _dt(rec.get("scheduledArrival"))
        delta = round((eta - sta).total_seconds() / 60) if eta and sta else None
        dtxt = (f' <span style="color:#FF8A00">+{delta}</span>' if delta is not None and delta > 5
                else f' <span style="color:#00FF7F">{delta:+d}</span>' if delta is not None and delta < -5
                else f' <span style="color:#9AA0A6">{delta:+d}</span>' if delta is not None else "")
        rows = (
            f'<div class="row"><span class="k">{escape(rec.get("aircraftType") or "")}'
            f'{(" &middot; " + escape(rec["aircraftRegistration"])) if rec.get("aircraftRegistration") else ""}</span>'
            f'<span class="v">{_fl(alt)} &middot; {spd}</span></div>'
            f'<div class="row"><span class="k">STD {_t(rec.get("scheduledDeparture"))}'
            f'{(" &middot; off " + _t(rec.get("actualTakeoff"))) if rec.get("actualTakeoff") else ""}</span>'
            f'<span class="v">{escape(status)}</span></div>'
            f'<div class="row"><span class="k">STA {_t(rec.get("scheduledArrival"))}</span>'
            f'<span class="v"><b>ETA {_t(rec.get("estimatedArrival"))}</b>{dtxt}'
            f'{(" &middot; landed " + _t(rec.get("actualLanding"))) if rec.get("actualLanding") else ""}</span></div>')
        legs = (
            f'<div class="leg orig"><span class="nm">ORIGIN:</span> <b>{escape(rec.get("depAirportIcao") or "")}</b>'
            f' <span class="nm">{escape(rec.get("depAirportCity") or "")}</span></div>'
            f'<div class="leg dest"><span class="nm">DESTINATION:</span> <b>{escape(rec.get("arrAirportIcao") or "")}</b>'
            f' <span class="nm">{escape(rec.get("arrAirportCity") or "")}</span></div>')
    elif note:
        legs = f'<div class="ft">{escape(note)}</div>'
    return (
        '<style>.bm-ac{font-family:DejaVu Sans Mono,Courier New,monospace;border-radius:6px;padding:7px 9px;'
        'color:#FFFFFF;font-size:11px;width:300px;line-height:1.35;backdrop-filter:blur(2px)}'
        '.bm-ac .hd{display:flex;gap:6px;align-items:baseline;border-bottom:1px solid rgba(255,255,255,.15);padding-bottom:4px;margin-bottom:4px}'
        '.bm-ac .hd b{font-size:14px}.bm-ac .hd span{color:#C8CDD3;font-size:10px}'
        '.bm-ac .hd .wx{margin-left:auto;color:#FFFFFF;font-weight:700;font-size:11px;letter-spacing:.3px}'
        '.bm-ac .row{display:flex;justify-content:space-between;gap:8px;padding:1px 0}'
        '.bm-ac .k{color:#C8CDD3}.bm-ac .v{text-align:right;white-space:nowrap}'
        '.bm-ac .leg{border-left:3px solid #4DA3FF;padding:2px 6px;margin-top:4px;background:rgba(0,0,0,.35)}'
        '.bm-ac .leg.dest{border-left-color:#00E5FF}.bm-ac .leg .nm{color:#C8CDD3;font-size:10px}'
        '.bm-ac .ft{color:#9AA0A6;font-size:10px;margin-top:4px}</style>'
        f'<div class="bm-ac" style="background:{bg};border:1px solid {edge}">'
        f'<div class="hd"><b>{cs}</b>{("<span>/ " + iata + "</span>") if iata else ""}'
        f'{wx_html}</div>'
        f'{rows}{legs}</div>')
