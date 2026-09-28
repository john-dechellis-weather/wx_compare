"""Large Scale Map: tomorrow.io map tiles stitched server-side.

Target path: core/tio_map.py

The page covers every JetBlue destination except Europe, plus the
Canadian alternates: 125W-50W, 52N-5S, Web Mercator. The tomorrow.io
Weather Maps API serves 256 px PNG tiles

    https://api.tomorrow.io/v4/map/tile/{z}/{x}/{y}/{field}/{time}.png?apikey=KEY

for the last 7 days and next 14 days. A browser could load them
directly, but the tile URL carries the API key (visible to every
viewer) and every pan, zoom and time step would spend the request
quota. So a warmer thread fetches a FIXED tile set for the domain,
stitches each set into one WebP under static/, and the page shows
those. The key never leaves the server and the spend is predictable.

Budget (plan: 10,000 requests/day, 9,999/hour, 50/s):

    "now" frame      zoom 5, 56 tiles, every TIO_NOW_MIN (15) min  ->  5,376/day
    forecast steps   zoom 4, 16 tiles x 4 steps, every TIO_FCST_MIN
                     (60) min                                        ->  1,536/day
                                                                        -------
                                                                         6,912/day

for one field (precipitationIntensity). TIO_DAILY_CAP (9,000) is a
hard stop: the warmer counts every request in static/tio_usage.json
by UTC day and skips a pass rather than cross it. Adding a second
field to TIO_FIELDS doubles the spend, so drop TIO_NOW_MIN to 30 or
the forecast cadence to 120 when you do.

Env
    TOMORROWIO_API_KEY   (or TOMORROW_API_KEY)  the key
    TIO_WARMER=off       stop the warmer without a deploy
    TIO_FIELDS           comma list, default precipitationIntensity
    TIO_FCST_HOURS       default 6,12,24,48
    TIO_NOW_MIN / TIO_FCST_MIN / TIO_NOW_ZOOM / TIO_FCST_ZOOM
    TIO_DAILY_CAP
"""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- domain

W, E, S, N = -125.0, -50.0, -5.0, 52.0
TILE = 256

FIELDS = [f.strip() for f in os.environ.get(
    "TIO_FIELDS", "precipitationIntensity").split(",") if f.strip()]
FIELD_LABEL = {
    "precipitationIntensity": "Precipitation intensity",
    "precipitationType": "Precipitation type",
    "cloudCover": "Cloud cover",
    "windSpeed": "Wind speed",
    "windGust": "Wind gust",
    "temperature": "Temperature",
    "visibility": "Visibility",
    "cloudBase": "Cloud base",
    "cloudCeiling": "Cloud ceiling",
    "pressureSeaLevel": "MSL pressure",
    "dewPoint": "Dew point",
}
FCST_HOURS = [int(h) for h in os.environ.get(
    "TIO_FCST_HOURS", "6,12,24,48").split(",") if h.strip()]
NOW_MIN = int(os.environ.get("TIO_NOW_MIN", "15"))
FCST_MIN = int(os.environ.get("TIO_FCST_MIN", "60"))
NOW_ZOOM = int(os.environ.get("TIO_NOW_ZOOM", "5"))
FCST_ZOOM = int(os.environ.get("TIO_FCST_ZOOM", "4"))
DAILY_CAP = int(os.environ.get("TIO_DAILY_CAP", "9000"))
KEEP_FRAMES = 3          # per field/step, newest kept

#: Canadian alternates, not in jbu_airports.json.
CAN_ALT = {
    "CYVR": (49.194, -123.184), "CYYC": (51.114, -114.020),
    "CYWG": (49.910, -97.240), "CYYZ": (43.677, -79.631),
    "CYOW": (45.323, -75.669), "CYUL": (45.470, -73.741),
    "CYQB": (46.791, -71.393), "CYHZ": (44.881, -63.509),
}
EU_COUNTRIES = {"GB", "IE", "FR", "NL", "ES", "IT"}
SKIP_STATIONS = {"SBAQ"}
#: Metro clusters: the dot stays, the label goes to the named field.
NO_LABEL = {"KLGA", "KEWR", "KHPN", "KISP", "KMDW", "KONT", "KBUR",
            "KFLL", "KPBI", "KDJT", "KHYA", "KMVY", "KACK", "KORH",
            "KPVD", "KBDL", "KBWI", "KPHL", "KSRQ", "KDAB", "KVRB",
            "KSJC", "KRSW", "MDST", "MDPP", "TJBQ", "TJPS", "TISX",
            "TNCB", "TNCC", "TIST"}

STATUS: dict = {"started": False, "last": None, "err": None}
_lock = threading.Lock()
_started = False


def api_key() -> str:
    return (os.environ.get("TOMORROWIO_API_KEY")
            or os.environ.get("TOMORROW_API_KEY") or "").strip()


# ---------------------------------------------------------------- tiles

def _lon2x(lon, z):
    return int(math.floor((lon + 180.0) / 360.0 * (1 << z)))


def _lat2y(lat, z):
    r = math.radians(lat)
    return int(math.floor(
        (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi)
        / 2.0 * (1 << z)))


def _x2lon(x, z):
    return x / (1 << z) * 360.0 - 180.0


def _y2lat(y, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


def tile_grid(z: int):
    """(x0, x1, y0, y1, bounds) for the domain at zoom z. bounds is
    [w, s, e, n] of the whole tile block - the image corners."""
    x0, x1 = _lon2x(W, z), _lon2x(E - 1e-9, z)
    y0, y1 = _lat2y(N, z), _lat2y(S + 1e-9, z)
    bounds = [_x2lon(x0, z), _y2lat(y1 + 1, z), _x2lon(x1 + 1, z), _y2lat(y0, z)]
    return x0, x1, y0, y1, bounds


def tile_count(z: int) -> int:
    x0, x1, y0, y1, _ = tile_grid(z)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def _fetch_tile(z, x, y, field, tstr, key):
    import requests

    url = (f"https://api.tomorrow.io/v4/map/tile/{z}/{x}/{y}/{field}/"
           f"{tstr}.png?apikey={key}")
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403):
                break
        except Exception as exc:
            last = f"{type(exc).__name__}"
        time.sleep(0.5)
    raise RuntimeError(f"tile {z}/{x}/{y} {tstr}: {last}")


# ---------------------------------------------------------------- usage

def _usage_path(outdir):
    return Path(outdir) / "tio_usage.json"


def usage(outdir) -> dict:
    try:
        d = json.loads(_usage_path(outdir).read_text())
    except Exception:
        d = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"day": today, "count": int(d.get("count", 0))
            if d.get("day") == today else 0}


def _add_usage(outdir, n):
    u = usage(outdir)
    u["count"] += n
    p = _usage_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(u))
    os.replace(tmp, p)
    return u["count"]


# ---------------------------------------------------------------- build

def _log(outdir, msg):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%MZ} {msg}"
    try:
        p = Path(outdir) / "tio_warmer.log"
        with open(p, "a") as f:
            f.write(line + "\n")
        if p.stat().st_size > 200_000:
            p.write_text("\n".join(p.read_text().splitlines()[-400:]) + "\n")
    except Exception:
        pass


def log_tail(outdir, n=6) -> list:
    try:
        return (Path(outdir) / "tio_warmer.log").read_text().splitlines()[-n:]
    except Exception:
        return []


def _manifest_path(outdir):
    return Path(outdir) / "tio_manifest.json"


def manifest(outdir) -> dict:
    try:
        return json.loads(_manifest_path(outdir).read_text())
    except Exception:
        return {}


def _save_manifest(outdir, man):
    p = _manifest_path(outdir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(man))
    os.replace(tmp, p)


def build_frame(outdir, field: str, step: int, z: int) -> dict:
    """Fetch and stitch one frame. step 0 = now; otherwise hours
    ahead, on the hour. Returns the manifest entry."""
    from PIL import Image

    key = api_key()
    if not key:
        raise RuntimeError("no TOMORROWIO_API_KEY")
    outdir = Path(outdir)
    x0, x1, y0, y1, bounds = tile_grid(z)
    now = datetime.now(timezone.utc)
    if step == 0:
        tstr, valid = "now", now
    else:
        valid = (now.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=step))
        tstr = valid.strftime("%Y-%m-%dT%H:%M:%SZ")
    n = (x1 - x0 + 1) * (y1 - y0 + 1)
    u = usage(outdir)
    if u["count"] + n > DAILY_CAP:
        raise RuntimeError(f"daily cap: {u['count']}+{n} > {DAILY_CAP}")
    _add_usage(outdir, n)          # charged up front; a failed tile still counts
    jobs = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        blobs = list(ex.map(lambda xy: _fetch_tile(z, xy[0], xy[1], field,
                                                   tstr, key), jobs))
    img = Image.new("RGBA", ((x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE),
                    (0, 0, 0, 0))
    for (x, y), b in zip(jobs, blobs):
        img.paste(Image.open(io.BytesIO(b)).convert("RGBA"),
                  ((x - x0) * TILE, (y - y0) * TILE))
    stamp = now.strftime("%Y%m%d-%H%M")
    name = f"tio_{field}_{step:02d}_{stamp}.webp"
    tmp = outdir / f".{name}.tmp"
    img.save(tmp, "WEBP", quality=88, method=4)
    os.replace(tmp, outdir / name)
    ent = {"name": name, "field": field, "step": step, "zoom": z,
           "valid": valid.strftime("%Y-%m-%dT%H:%MZ"),
           "built": now.strftime("%Y-%m-%dT%H:%MZ"),
           "bounds": bounds, "px": [img.width, img.height]}
    man = manifest(outdir)
    man.setdefault(field, {})[str(step)] = ent
    _save_manifest(outdir, man)
    # prune older frames of this field/step
    olds = sorted(outdir.glob(f"tio_{field}_{step:02d}_*.webp"))
    for p in olds[:-KEEP_FRAMES]:
        try:
            p.unlink()
        except Exception:
            pass
    _log(outdir, f"{field} +{step:02d}h z{z} {n} tiles {time.time() - t0:.1f}s "
                 f"-> {name} ({(outdir / name).stat().st_size // 1024} KB), "
                 f"day {usage(outdir)['count']}/{DAILY_CAP}")
    return ent


def _pass(outdir, steps):
    for field in FIELDS:
        for step in steps:
            try:
                build_frame(outdir, field, step, NOW_ZOOM if step == 0 else FCST_ZOOM)
                STATUS["last"] = time.time()
                STATUS["err"] = None
            except Exception as exc:
                STATUS["err"] = f"{type(exc).__name__}: {exc}"
                _log(outdir, f"FAILED {field} +{step:02d}h: {STATUS['err']}")
                if "daily cap" in str(exc) or "no TOMORROWIO" in str(exc):
                    return


def _loop(outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    time.sleep(8)          # let the page-3 warmers get in first
    _log(outdir, f"warmer start: fields={FIELDS} fcst={FCST_HOURS} "
                 f"now z{NOW_ZOOM}/{NOW_MIN}min fcst z{FCST_ZOOM}/{FCST_MIN}min "
                 f"cap {DAILY_CAP}/day")
    next_now = next_fc = 0.0
    while True:
        t = time.time()
        if t >= next_fc:
            _pass(outdir, [0] + FCST_HOURS)
            next_now = time.time() + NOW_MIN * 60
            next_fc = time.time() + FCST_MIN * 60
        elif t >= next_now:
            _pass(outdir, [0])
            next_now = time.time() + NOW_MIN * 60
        time.sleep(20)


def ensure_tio_warmer(outdir) -> bool:
    """Idempotent. TIO_WARMER=off disables without a deploy."""
    if os.environ.get("TIO_WARMER", "on").lower() == "off":
        return False
    global _started
    with _lock:
        if _started:
            return True
        threading.Thread(target=_loop, args=(outdir,), daemon=True,
                         name="tio-warmer").start()
        _started = True
        STATUS["started"] = True
    return True


# ---------------------------------------------------------------- page

def stations_geojson(static_dir) -> dict:
    feats = []
    try:
        st = json.loads((Path(static_dir) / "jbu_airports.json").read_text())["stations"]
    except Exception:
        st = {}
    for icao, v in st.items():
        if v.get("country") in EU_COUNTRIES or icao in SKIP_STATIONS:
            continue
        if not (W <= v["lon"] <= E and S <= v["lat"] <= N):
            continue
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [v["lon"], v["lat"]]},
                      "properties": {"id": icao[1:] if icao[0] in "KCT" and len(icao) == 4 else icao,
                                     "kind": "jbu",
                                     "label": "" if icao in NO_LABEL else
                                     (icao[1:] if icao[0] in "KCT" and len(icao) == 4 else icao)}})
    for icao, (la, lo) in CAN_ALT.items():
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [lo, la]},
                      "properties": {"id": icao[1:], "kind": "alt", "label": icao[1:]}})
    return {"type": "FeatureCollection", "features": feats}


def routes_geojson(cache_root=None) -> dict:
    """The SAME 58 ATS routes the JBU CONUS map draws (42 domestic J/Q,
    16 oceanic L), from core.navdata: the NASR/AIS full-length build
    when it is on disk, else the bundled static/map_routes.geojson,
    exactly as the CONUS page falls back. Colour by type as on the
    enroute chart and the CONUS map: J brown-red, Q olive, L slate."""
    rts = []
    try:
        from core import navdata as _ND

        if cache_root is not None:
            try:
                _ND.ensure(cache_root)
                rts = ((_ND.load(cache_root) or {}).get("routes") or [])
            except Exception:
                rts = []
        if not rts:
            rts = _ND.bundled_routes()
    except Exception:
        rts = []
    feats = []
    for r in rts:
        if len(r.get("path") or []) < 2:
            continue
        t = (r.get("type") or "").upper()
        rid = r.get("id") or ""
        if t == "OCEAN":
            col = "rgba(70,85,120,0.8)"
        elif t == "RNAV" or rid.startswith("Q"):
            col = "rgba(110,110,30,0.8)"
        else:
            col = "rgba(150,60,40,0.8)"
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": r["path"]},
                      "properties": {"id": rid, "color": col}})
    return {"type": "FeatureCollection", "features": feats}


def map_html(man: dict, base: str, stations: dict, routes: dict | None = None,
             height: int = 860) -> str:
    """MapLibre page: CARTO dark vector basemap, one image source per
    warmed frame, station dots and labels. Served from /app/static and
    embedded by URL (see radar_l3.loop_html for why not srcdoc)."""
    frames = []
    for field in FIELDS:
        for step in [0] + FCST_HOURS:
            e = (man.get(field) or {}).get(str(step))
            if e:
                frames.append({"url": f"{base}/app/static/{e['name']}?v={e['built']}",
                               "field": field, "step": step, "valid": e["valid"],
                               "built": e["built"], "b": e["bounds"]})
    style = os.environ.get(
        "BLUEMET_MAP_STYLE",
        "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json")
    labels = {f: FIELD_LABEL.get(f, f) for f in FIELDS}
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"></script>
<style>
 html,body{{margin:0;background:#000;height:100%;font:bold 12px "DejaVu Sans Mono","Courier New",monospace;color:#fff}}
 #m{{height:{height - 48}px;border:1px solid #333;border-radius:10px;overflow:hidden}}
 .bar{{display:flex;align-items:center;gap:10px;height:44px;padding:0 4px;flex-wrap:nowrap;white-space:nowrap}}
 .bar select,.bar button{{font:bold 12px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:6px;padding:5px 10px}}
 .bar button.on{{border-color:#00E5FF;background:#1C1C22}}
 .bar input[type=range]{{accent-color:#00E5FF;width:110px}}
 #valid{{color:#FFD400}} #built{{color:#B8B8B8}}
 label{{color:#B8B8B8}} .sp{{flex:1}}
 .lg{{position:absolute;left:10px;bottom:24px;background:rgba(0,0,0,.85);border:1px solid #333;border-radius:6px;padding:8px 10px;font-size:11px;z-index:5}}
 .lg .d{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;border:1px solid #000}}
</style></head><body>
<div class="bar">
  <label>FIELD</label><select id="fld">{"".join(f'<option value="{f}">{labels[f]}</option>' for f in FIELDS)}</select>
  <label>TIME</label><span id="steps"></span>
  <span id="valid"></span><span id="built"></span>
  <span class="sp"></span>
  <label>OPACITY</label><input id="op" type="range" min="20" max="100" value="85">
  <button id="bst" class="on">Stations</button><button id="balt" class="on">CA alternates</button><button id="brt" class="on">Routes</button>
  <button id="bfit">Fit</button>
</div>
<div id="m"><div class="lg" id="lg"><span class="d" style="background:#4DA3FF"></span>JBU station &nbsp;
 <span class="d" style="background:#9AA0A6"></span>Canadian alternate<br>
 <span style="color:#B44C34">&#9473;</span> J route &nbsp;<span style="color:#8C8C26">&#9473;</span> Q route &nbsp;<span style="color:#5A6E96">&#9473;</span> L oceanic<br>
 <span style="color:#6E6E6E">tomorrow.io tiles · 125W-50W 52N-5S</span></div></div>
<script>
const F = {json.dumps(frames)};
const ST = {json.dumps(stations)};
const RT = {json.dumps(routes or {"type": "FeatureCollection", "features": []})};
const BB = [[{W},{S}],[{E},{N}]];
const map = new maplibregl.Map({{container:'m', style:{json.dumps(style)},
  bounds:BB, fitBoundsOptions:{{padding:8}}, minZoom:2.2, maxZoom:9,
  maxBounds:[[{W - 25},{S - 15}],[{E + 25},{N + 8}]], attributionControl:true}});
map.addControl(new maplibregl.NavigationControl({{showCompass:false}}));
let field = F.length ? F[0].field : '', step = 0, ready = false;
const $ = id => document.getElementById(id);
function key(f) {{ return 'tio_' + f.field + '_' + f.step; }}
function cur() {{ return F.find(f => f.field === field && f.step === step) || F.find(f => f.field === field); }}
function draw() {{
  const c = cur();
  const op = (+$('op').value) / 100;
  if (ready) F.forEach(f => map.setPaintProperty(key(f), 'raster-opacity', c && key(f) === key(c) ? op : 0));
  $('valid').textContent = c ? 'valid ' + c.valid + (c.step ? ' (+' + c.step + ' h)' : ' (now)') : 'no frames yet';
  $('built').textContent = c ? ' · built ' + c.built : '';
  [...$('steps').querySelectorAll('button')].forEach(b => b.classList.toggle('on', +b.dataset.s === (c ? c.step : -1)));
}}
function buildSteps() {{
  const steps = [...new Set(F.filter(f => f.field === field).map(f => f.step))].sort((a, b) => a - b);
  $('steps').innerHTML = steps.map(s => `<button data-s="${{s}}">${{s ? '+' + s + ' h' : 'now'}}</button>`).join('');
  [...$('steps').querySelectorAll('button')].forEach(b => b.onclick = () => {{ step = +b.dataset.s; draw(); }});
}}
map.on('load', () => {{
  F.forEach(f => {{
    map.addSource(key(f), {{type:'image', url:f.url,
      coordinates:[[f.b[0], f.b[3]], [f.b[2], f.b[3]], [f.b[2], f.b[1]], [f.b[0], f.b[1]]]}});
    map.addLayer({{id:key(f), type:'raster', source:key(f),
      paint:{{'raster-opacity':0, 'raster-fade-duration':0, 'raster-resampling':'linear'}}}});
  }});
  map.addSource('rt', {{type:'geojson', data:RT}});
  map.addLayer({{id:'rt-line', type:'line', source:'rt',
    paint:{{'line-color':['get','color'], 'line-width':1.2}}}});
  map.addLayer({{id:'rt-lab', type:'symbol', source:'rt',
    layout:{{'symbol-placement':'line', 'symbol-spacing':220, 'text-field':['get','id'],
             'text-size':10, 'text-font':['Open Sans Bold'], 'text-rotation-alignment':'map',
             'text-keep-upright':true, 'text-allow-overlap':false, 'text-padding':2}},
    paint:{{'text-color':'#C8C8C8', 'text-halo-color':'#000', 'text-halo-width':1.5}}}});
  map.addSource('st', {{type:'geojson', data:ST}});
  map.addLayer({{id:'st-dot', type:'circle', source:'st',
    paint:{{'circle-radius':['case', ['==', ['get','kind'], 'jbu'], 4, 3.5],
            'circle-color':['case', ['==', ['get','kind'], 'jbu'], '#4DA3FF', '#9AA0A6'],
            'circle-stroke-color':'#000', 'circle-stroke-width':1}}}});
  map.addLayer({{id:'st-lab', type:'symbol', source:'st',
    layout:{{'text-field':['get','label'], 'text-size':11, 'text-offset':[0.7, -0.6],
             'text-anchor':'bottom-left', 'text-font':['Open Sans Bold'], 'text-allow-overlap':false}},
    paint:{{'text-color':['case', ['==', ['get','kind'], 'jbu'], '#FFFFFF', '#B8B8B8'],
            'text-halo-color':'#000', 'text-halo-width':1.4}}}});
  ready = true; draw();
}});
$('fld').onchange = e => {{ field = e.target.value; buildSteps(); draw(); }};
$('op').oninput = draw;
$('bst').onclick = () => {{ $('bst').classList.toggle('on'); applyFilter(); }};
$('balt').onclick = () => {{ $('balt').classList.toggle('on'); applyFilter(); }};
$('brt').onclick = () => {{ if (!ready) return; const on = !$('brt').classList.contains('on'); $('brt').classList.toggle('on', on);
  ['rt-line','rt-lab'].forEach(l => map.setLayoutProperty(l, 'visibility', on ? 'visible' : 'none')); }};
function applyFilter() {{
  if (!ready) return;
  const kinds = []; if ($('bst').classList.contains('on')) kinds.push('jbu'); if ($('balt').classList.contains('on')) kinds.push('alt');
  ['st-dot','st-lab'].forEach(l => {{ map.setFilter(l, ['in', ['get','kind'], ['literal', kinds]]);
    map.setLayoutProperty(l, 'visibility', kinds.length ? 'visible' : 'none'); }});
}}
$('bfit').onclick = () => map.fitBounds(BB, {{padding:8}});
buildSteps(); draw();
</script></body></html>"""
