"""Live aircraft for one airport, written as JSON for the scope viewer
(22 Oct).

The fleet sweep (core.fleet) covers CONUS every two minutes. For an
airport-scale scope that is a jump every 120 s, not movement. This
runs ONE point query centred on the selected airport every LIVE_S
seconds while the page is open (demand expires EXPIRE_S after the
last call), dedupes it through core.airport_scope, keeps position
history for trails, and writes static/aradar_live_<ICAO>.json. The
viewer (core/scope_html.py, live_url=) polls that file and
dead-reckons each airborne aircraft between fixes, so icons move
every frame rather than every sweep.

One request per LIVE_S per watched airport is well inside what
adsb.lol / adsb.fi allow; the hosts alternate and a 429 backs the
loop off for a minute.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LIVE_S = float(os.environ.get("BLUEMET_LIVE_S", "10"))
EXPIRE_S = float(os.environ.get("BLUEMET_LIVE_EXPIRE_S", "300"))
TRAIL_S = float(os.environ.get("BLUEMET_LIVE_TRAIL_S", "3900"))   # an hour; the viewer trims
RADIUS_NM = 150
ROLL_KT = float(os.environ.get("BLUEMET_ROLL_KT", "30"))   # ground speed that counts as a takeoff / landing roll

STATUS: dict = {}            # icao -> {"last", "n", "err", "host"}
_demand: dict = {}           # icao -> last demand time
_threads: dict = {}
_lock = threading.Lock()
_turn = {"i": 0}


def path(outdir, icao: str) -> Path:
    return Path(outdir) / f"aradar_live_{icao.upper()}.json"


def demand(icao: str, lat: float, lon: float, outdir, radius_nm: float = RADIUS_NM) -> None:
    """Called by the page on every fragment run: keeps the poller for
    this airport alive, starting it if needed."""
    icao = icao.upper()
    with _lock:
        _demand[icao] = time.time()
        th = _threads.get(icao)
        if th is not None and th.is_alive():
            return
        th = threading.Thread(target=_loop, args=(icao, lat, lon, Path(outdir), radius_nm),
                              daemon=True, name=f"live-{icao}")
        _threads[icao] = th
        th.start()


def fresh(outdir, icao: str, max_age_s: float = 60.0) -> bool:
    try:
        return time.time() - path(outdir, icao).stat().st_mtime < max_age_s
    except Exception:
        return False


def _fetch(lat, lon, radius_nm) -> tuple:
    """(rows, host) from the community point endpoints, alternating."""
    import requests
    hdrs = {"User-Agent": "bluemet.org ops dashboard"}
    urls = [("adsb.lol", f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{int(radius_nm)}"),
            ("adsb.fi", f"https://opendata.adsb.fi/api/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{int(radius_nm)}")]
    k = _turn["i"] % 2
    _turn["i"] += 1
    last = None
    for host, u in (urls[k], urls[1 - k]):
        try:
            r = requests.get(u, timeout=8, headers=hdrs)
            if r.status_code == 429:
                last = f"{host} 429"
                continue
            r.raise_for_status()
            j = r.json() or {}
            rows = j.get("ac") or j.get("aircraft") or []
            return rows, host
        except Exception as exc:
            last = f"{host} {type(exc).__name__}"
            continue
    raise RuntimeError(last or "no host answered")


def build_rows(raw: list, lat: float, lon: float) -> list:
    """Raw feed rows -> viewer rows (same fields the scope's
    IconLayer data carries, plus ground flag, fix age and trail)."""
    from core import airport_scope as AS
    from core import cards as C
    out = AS._dedupe(raw)
    AS._remember(out)
    seen = {}
    for a in raw:
        cs = (a.get("flight") or "").strip().upper()
        if cs:
            try:
                seen[cs] = float(a.get("seen_pos") if a.get("seen_pos") is not None else a.get("seen") or 0)
            except (TypeError, ValueError):
                seen[cs] = 0.0
    try:
        from core import fleet as F
    except Exception:
        F = None
    rows = []
    for a in out:
        cs = a["callsign"]
        # same cut as the page: JetBlue anywhere in the box, other
        # airlines within 60 nm, no GA / N-numbers
        if not a.get("jbu"):
            if not a.get("airline"):
                continue
            if AS.distance_nm(lat, lon, a["position"][1], a["position"][0]) > 60:
                continue
        gs, alt = a.get("gs"), a.get("alt")
        try:
            gs = float(gs) if gs not in (None, "") else None
        except (TypeError, ValueError):
            gs = None
        try:
            alt = float(alt) if alt not in (None, "", "ground") else None
        except (TypeError, ValueError):
            alt = None
        ground = (gs < AS.GROUND_KT) if gs is not None else (alt is None or alt < 1500)
        # parked / taxiing (24 Oct): on the ground under ROLL_KT. Not
        # drawn on the scope; only aircraft rolling on a runway are.
        parked = bool(ground and (gs is None or gs < ROLL_KT))
        col = C.colour(cs)
        glyph = C.kind(cs, a.get("type", ""))
        r = {}
        if F is not None:
            try:
                r = F.route_for(cs) or {}
            except Exception:
                r = {}
        rows.append({"callsign": cs, "position": a["position"], "hdg": a.get("hdg") or 0.0,
                     "alt": alt if alt is not None else ("ground" if ground else None),
                     "gs": gs, "type": a.get("type", ""), "jbu": bool(a.get("jbu")),
                     "airline": bool(a.get("airline")), "ground": bool(ground), "parked": parked,
                     "age": seen.get(cs, 0.0),
                     "origin": r.get("o") or "", "dest": r.get("d") or "",
                     "origin_name": r.get("on") or "", "dest_name": r.get("dn") or "",
                     "oll": r.get("oll"), "dll": r.get("dll"),
                     "colour": col, "rgb": AS._rgb(C.muted(col)), "glyph": glyph, "ikey": f"{glyph}:{col}",
                     "trail": [] if ground else AS.trail_t(cs, TRAIL_S)})
    if F is not None:
        try:
            F.request_routes([r["callsign"] for r in rows if r["airline"]])
        except Exception:
            pass
    return rows


def write(outdir, icao: str, rows: list, host: str = "") -> None:
    from core import cards as C
    hold = []
    try:
        from core import tracks as TR
        holding, _ = TR.hold_events()
        hold = [h[0] for h in holding]
    except Exception:
        pass
    # one icon per colour, not per aircraft (the data URL is ~2 KB)
    icons = {k: C.plane_icon(k.split(":")[1], k.split(":")[0]) for k in {r["ikey"] for r in rows}}
    doc = {"icao": icao, "t": time.time(), "host": host, "ac": rows, "hold": hold,
           "icons": icons, "icon_px": C.ICON_PX, "tag_fs": C.TAG_FS, "font": C.FONT}
    p = path(outdir, icao)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, p)


def _loop(icao, lat, lon, outdir: Path, radius_nm):
    st = STATUS.setdefault(icao, {"last": 0, "n": 0, "err": "", "host": ""})
    while time.time() - _demand.get(icao, 0) < EXPIRE_S:
        wait = LIVE_S
        try:
            raw, host = _fetch(lat, lon, radius_nm)
            rows = build_rows(raw, lat, lon)
            write(outdir, icao, rows, host)
            st.update({"last": time.time(), "n": len(rows), "err": "", "host": host})
        except Exception as exc:
            st["err"] = f"{type(exc).__name__}: {exc}"[:120]
            wait = 60 if "429" in st["err"] else 20
        time.sleep(wait)
    with _lock:
        _threads.pop(icao, None)
