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
            "latitude", "longitude", "heading", "distance", "duration", "plannedDuration")
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
