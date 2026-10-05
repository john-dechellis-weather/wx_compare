"""GOES visible imagery for the airport scope, straight from NASA GIBS
(5 Oct). No fetch on our side: the browser pulls Web Mercator tiles
from GIBS (CORS open, no key), one deck.gl TileLayer per frame.

GIBS publishes GOES-East ABI GeoColor (true colour by day, IR-based
night blend after dark) and the Band 2 red visible at 10-minute
steps, about 45-60 minutes behind real time, to zoom 7 (~1.2 km/px
at this latitude - the scope overzooms it beyond that). The newest
time comes from the "layer-time-actual" header of one HEAD request,
cached 5 minutes.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

GIBS = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
LAYERS = {
    "geocolor": ("GOES-East_ABI_GeoColor", "GeoColor"),
    "vis": ("GOES-East_ABI_Band2_Red_Visible_1km", "Band 2 visible"),
}
STEP_MIN = 10
_latest: dict = {}      # layer -> (time.time(), datetime | None)


def latest_time(layer: str = "geocolor") -> datetime | None:
    """Newest available time for the layer, from GIBS's header."""
    import requests

    ident = LAYERS[layer][0]
    hit = _latest.get(ident)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    t = None
    try:
        r = requests.head(f"{GIBS}/{ident}/default/default/GoogleMapsCompatible_Level7/5/12/9.png",
                          timeout=10, allow_redirects=True)
        v = r.headers.get("layer-time-actual", "")
        if v:
            t = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        t = None
    _latest[ident] = (time.time(), t)
    return t


def frames(layer: str = "geocolor", n: int = 6, opacity: float = 0.85) -> list:
    """[{tiles, maxZoom, label, opacity}] oldest first for the scope's
    frame loop; [] when GIBS cannot be reached."""
    ident, _label = LAYERS[layer]
    t = latest_time(layer)
    if t is None:
        return []
    out = []
    for k in range(n - 1, -1, -1):
        tk = t - timedelta(minutes=STEP_MIN * k)
        out.append({
            "tiles": f"{GIBS}/{ident}/default/{tk:%Y-%m-%dT%H:%M:%S}Z/"
                     "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png",
            "maxZoom": 7, "label": f"{tk:%H:%M}Z", "opacity": opacity,
        })
    return out
