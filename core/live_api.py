"""Same-origin live-position proxy.

Browsers can't poll the ADS-B aggregators directly (no CORS headers -
proven by the Live Position Test page), so this module registers a
tiny JSON endpoint on Streamlit's underlying Tornado server:

    GET /jbu_pos?lat=40.64&lon=-73.78&r=1.5

    -> {"ok": true, "ts": "...", "planes": [[lat, lon, hdg, callsign],
        ...]}

The browser polls THIS (same origin, no CORS), and the server relays
to adsb.lol - where reachability is long proven. The upstream fetch
runs in a worker thread so a poll never blocks the Tornado loop.

Registration walks live Tornado Application objects (version-agnostic
for our pinned Streamlit); failures are swallowed - pages must treat
the endpoint as optional.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

_registered = False
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2)


def _fetch_planes(lat: float, lon: float, radius_deg: float):
    from core.flights import fetch_positions_near
    planes = fetch_positions_near(lat, lon, radius_deg=radius_deg)
    return [
        [p.lat, p.lon, p.heading_deg or 0, p.callsign]
        for p in planes
    ]


def ensure_live_api() -> bool:
    """Register /jbu_pos on the running Tornado app (idempotent).
    Returns True if the route is (now) registered."""
    global _registered
    with _lock:
        if _registered:
            return True
        try:
            import gc

            import tornado.web

            class JbuPosHandler(tornado.web.RequestHandler):
                async def get(self):
                    try:
                        lat = float(self.get_argument("lat"))
                        lon = float(self.get_argument("lon"))
                        r = min(float(
                            self.get_argument("r", "1.5")
                        ), 4.0)
                    except (ValueError, tornado.web.MissingArgumentError):
                        self.set_status(400)
                        self.write({"ok": False,
                                    "err": "bad params"})
                        return
                    loop = tornado.ioloop.IOLoop.current()
                    try:
                        planes = await loop.run_in_executor(
                            _pool, _fetch_planes, lat, lon, r
                        )
                    except Exception as e:
                        self.set_status(502)
                        self.write({"ok": False,
                                    "err": type(e).__name__})
                        return
                    self.set_header("Cache-Control", "no-store")
                    self.write(json.dumps({
                        "ok": True,
                        "ts": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "planes": planes,
                    }))

            import tornado.ioloop
            apps = [
                o for o in gc.get_objects()
                if isinstance(o, tornado.web.Application)
            ]
            if not apps:
                return False
            ok = False
            for app in apps:
                # Streamlit's route group ends in a catch-all that
                # serves index.html for any path, so a rule APPENDED
                # after it never matches (proven: /jbu_pos returned
                # the SPA's HTML). Insert our URLSpec at the FRONT of
                # the wildcard router so it wins the ordering.
                router = getattr(app, "wildcard_router", None)
                if router is None or not hasattr(router, "rules"):
                    continue
                already = any(
                    getattr(getattr(r, "matcher", None),
                            "regex", None) is not None
                    and r.matcher.regex.pattern.startswith("/jbu_pos")
                    for r in router.rules
                )
                if not already:
                    router.rules.insert(
                        0,
                        tornado.web.url(r"/jbu_pos", JbuPosHandler),
                    )
                ok = True
            if not ok:
                return False
            _registered = True
            return True
        except Exception:
            return False
