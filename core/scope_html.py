"""Airport scope as a self-contained deck.gl page with a client-side
radar loop.

Target path: core/scope_html.py

The Station Forecast scope used to be an st.pydeck_chart re-run every
2 s by a fragment to advance the radar frame. Every re-run put
Streamlit's status widget up (the "Loading ..." banner), cost a server
round trip, and still only managed a 2-second step. This renders the
SAME deck (pydeck's JSON, converted in the browser by deck.gl's own
JSONConverter - what the Streamlit component does internally) inside
an iframe we control, with every Level III frame preloaded as a
BitmapLayer and a play/pause/slider that just flips which one is
visible. Smooth, no reruns, pan and zoom survive (the view is kept in
sessionStorage per station).

Served from /app/static and embedded by URL (see radar_l3.loop_html
for why not srcdoc).
"""

from __future__ import annotations

import json
import re

DECK_JS = "https://cdn.jsdelivr.net/npm/deck.gl@8.9.36/dist.min.js"
DECK_JSON_JS = "https://cdn.jsdelivr.net/npm/@deck.gl/json@8.9.36/dist.min.js"
MAPLIBRE_JS = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js"
MAPLIBRE_CSS = "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css"

# pydeck writes any bare-word string prop as an "@@=" expression; these
# props are plain enums, not accessors, so the marker comes off.
_ENUM_PROPS = {"sizeUnits", "widthUnits", "radiusUnits", "lineWidthUnits",
               "fontFamily", "fontWeight", "wordBreak", "textAnchor",
               "alignmentBaseline", "coordinateSystem", "billboard",
               "sizeScale", "iconAtlas"}


def _clean(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _ENUM_PROPS and isinstance(v, str) and v.startswith("@@="):
                v = v[3:]
            out[k] = _clean(v)
        return out
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    return obj


def scope_html(deck_json: dict, frames: list, style_url: str, icao: str,
               height: int = 560, tooltip: dict | None = None,
               radar_index: int | None = None, loop_ms: int = 500,
               live_url: str | None = None, declutter: bool = False,
               autoplay: bool = True) -> str:
    from core.airport_scope import AIRPORT_ZOOM as _AZ
    airport_zoom = _AZ
    """deck_json: json.loads(pdk.Deck(...).to_json()) WITHOUT the radar
    layer. frames: [{"url", "bounds":[w,s,e,n], "label"}] oldest first;
    they are inserted at layer index `radar_index` (default: after the
    leading BitmapLayers, i.e. above MRMS, under the vector overlays).
    style_url: the MapLibre style (scope_style.json) or "" for black."""
    dj = _clean(deck_json)
    layers = dj.get("layers", [])
    if radar_index is None:
        radar_index = 0
        while radar_index < len(layers) and layers[radar_index].get("@@type") == "BitmapLayer":
            radar_index += 1
    vs = dj.get("initialViewState", {})
    views = dj.get("views", [{"@@type": "MapView", "controller": True}])
    ctrl = views[0].get("controller", True) if views else True
    tip = tooltip or {}
    labels = [f.get("label", "") for f in frames]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{MAPLIBRE_CSS}">
<script src="{MAPLIBRE_JS}"></script>
<script src="{DECK_JS}"></script>
<script src="{DECK_JSON_JS}"></script>
<style>
 html,body{{margin:0;background:#000;height:100%;overflow:hidden;font:bold 12px "DejaVu Sans Mono","Courier New",monospace;color:#fff}}
 #m{{position:absolute;top:0;left:0;right:0;bottom:36px;background:#000}}
 .bar{{position:absolute;left:0;right:0;bottom:0;height:36px;display:flex;align-items:center;gap:8px;padding:0 8px;background:#0A0A0A;border-top:1px solid #333}}
 .bar button{{font:bold 12px "DejaVu Sans Mono",monospace;background:#0A0A0A;color:#fff;border:1px solid #333;border-radius:6px;padding:3px 9px;cursor:pointer}}
 .bar button.on{{border-color:#00E5FF;background:#1C1C22}}
 .bar input[type=range]{{flex:1;accent-color:#00E5FF}}
 #t{{color:#FFD400;min-width:60px}} #n{{color:#B8B8B8;min-width:120px}}
 .tip{{position:absolute;pointer-events:none;background:#0A0A0A;color:#fff;border:1px solid #333;font-size:12px;padding:5px 8px;border-radius:4px;z-index:9;display:none;white-space:nowrap}}
 /* aircraft panel (3 Oct): top right, like the tomorrow.io flight card */
 #ac{{position:absolute;top:10px;right:10px;width:300px;background:#0F1115;border:1px solid #2D3957;border-radius:6px;z-index:8;display:none;font:12px Roboto,Arial,sans-serif;color:#E8E8E8;box-shadow:0 4px 18px rgba(0,0,0,.6)}}
 #ac .hd{{padding:10px 12px 8px;border-bottom:1px solid #22283A;display:flex;align-items:center;gap:8px}}
 #ac .hd b{{font-size:16px;color:#fff;letter-spacing:.3px}} #ac .hd span{{color:#9AA0A6;font-size:11px}}
 #ac .x{{margin-left:auto;cursor:pointer;color:#9AA0A6;font-size:16px;line-height:1;padding:0 2px}} #ac .x:hover{{color:#fff}}
 #ac .row{{display:flex;justify-content:space-between;padding:6px 12px;border-bottom:1px solid #1A1F2B}} #ac .row .k{{color:#9AA0A6}} #ac .row .v{{color:#fff;font-weight:600}}
 #ac .leg{{margin:10px 10px;background:#161A22;border-radius:5px;padding:8px 10px;border-left:3px solid #4DA3FF}}
 #ac .leg .ap{{font-weight:700;color:#fff;font-size:13px}} #ac .leg .nm{{color:#9AA0A6;font-size:11px;margin-bottom:4px}}
 #ac .leg.dest{{border-left-color:#00E5FF}}
 #ac .ft{{padding:6px 12px 8px;color:#6E6E6E;font-size:10px}}
 /* reflectivity legend (23 Oct): top left, AWIPS steps + floor slider */
 #lg{{position:absolute;top:10px;left:10px;z-index:8;background:rgba(10,10,10,.82);border:1px solid #333;border-radius:6px;padding:6px 10px;display:none;font:bold 11px "DejaVu Sans Mono",monospace;color:#B8B8B8}}
 #lg .sw{{display:flex;gap:0;margin:4px 0 2px}} #lg .sw div{{width:22px;height:12px;position:relative}}
 #lg .sw div span{{position:absolute;left:-6px;top:13px;font-size:9px;color:#B8B8B8;width:22px;text-align:center}}
 #lg .sw div.off{{opacity:.18}}
 #lg .row{{display:flex;align-items:center;gap:8px;margin-top:16px}} #lg input[type=range]{{width:150px;accent-color:#00E5FF}}
 #lg b{{color:#FFD400}}
</style></head><body>
<div id="m"></div><div class="tip" id="tip"></div>
<div id="lg"><div>REFLECTIVITY dBZ</div><div class="sw" id="sw"></div>
  <div class="row"><span>show &ge;</span><input id="fl" type="range" min="5" max="40" step="1"><span><b id="flv"></b> dBZ</span></div>
  <div class="row" style="margin-top:6px"><label style="cursor:pointer"><input id="cf" type="checkbox" style="accent-color:#00E5FF;vertical-align:-2px"> clutter filter <span style="color:#6E6E6E;font-weight:400">(RHO + texture)</span></label></div></div>
<div id="ac"></div>
<div class="bar">
  <button id="pp">Pause</button><button id="pv">&#9664;</button><button id="nx">&#9654;</button>
  <input id="sl" type="range" min="0" max="{max(len(frames) - 1, 0)}" value="{max(len(frames) - 1, 0)}">
  <span id="t"></span><span id="n"></span>
  <button id="sp" title="loop speed">{loop_ms} ms</button>
  <span id="opl" title="radar opacity" style="color:#B8B8B8">radar</span><input id="op" type="range" min="10" max="100" value="85" style="flex:0 0 90px" title="radar opacity">
  <button id="rv" title="back to the opening view">Reset view</button>
</div>
<script>
const DJ = {json.dumps(dj)};
const FRAMES = {json.dumps(frames)};
const LABELS = {json.dumps(labels)};
const STYLE = {json.dumps(style_url)};
const TIP = {json.dumps(tip)};
const RADAR_AT = {radar_index};
// raw radar tiles (23 Oct): sample the texture nearest-neighbour so a
// gate stays a crisp block when magnified instead of a blurred blob
const RAWTEX = {{10240: 9728, 10241: 9728, 10242: 33071, 10243: 33071}};  // MAG/MIN_FILTER NEAREST, CLAMP_TO_EDGE
// ---- POLAR GATES (23 Oct): a Level II frame may be a gate file
// ({{bin_url, g0, gw, radar}}) instead of an image. Each gate is drawn
// as the range/azimuth rectangle it is, so nothing is resampled or
// blended; the legend's floor filters on the GPU.
// greens 20-30 dBZ at 75 % lightness (24 Oct, user pick): #01BD01 #009300 #006A00
const AWIPS = [[5,'#04E9E7'],[10,'#019FF4'],[15,'#0300F4'],[20,'#01BD01'],[25,'#009300'],[30,'#006A00'],[35,'#FDF802'],[40,'#E5BC00'],[45,'#FD9500'],[50,'#FD0000'],[55,'#D40000'],[60,'#BC0000'],[65,'#F800FD'],[70,'#9854C6'],[75,'#FDFDFD']];
const AWRGB = AWIPS.map(([v, h]) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]);
function awips(dbz) {{ let k = 0; for (let j = 0; j < AWIPS.length; j++) if (dbz >= AWIPS[j][0]) k = j; return AWRGB[k]; }}
let floorDbz = {json.dumps(float(__import__('os').environ.get('ARADAR_DBZ_MIN', '10')))};
try {{ const v = localStorage.getItem('bm_radar_floor'); if (v) floorDbz = Math.max(5, Math.min(40, +v)); }} catch (e) {{}}
let clutterOff = true;   // clutter filter on by default
try {{ const v = localStorage.getItem('bm_radar_clutter'); if (v != null) clutterOff = v === '1'; }} catch (e) {{}}
const GATES = {{}};   // frame index -> decoded buffers
// Decoded frames are big (tens of MB each at 250 mi), so only the frame
// on screen is decoded at first; stepping or playing decodes the rest
// on demand and the cache keeps at most MAX_DECODED, farthest from
// the current frame evicted first.
const MAX_DECODED = 6;
function evictGates() {{
  const have = Object.keys(GATES).filter(k => GATES[k].n).map(Number);
  if (have.length <= MAX_DECODED) return;
  have.sort((a, b) => Math.abs(b - i) - Math.abs(a - i));
  for (const k of have.slice(0, have.length - MAX_DECODED)) if (k !== i) delete GATES[k];
}}
function destPoint(lat, lon, brgDeg, distM) {{
  const R = 6371000, d = distM / R, b = brgDeg * Math.PI / 180, la1 = lat * Math.PI / 180, lo1 = lon * Math.PI / 180;
  const la2 = Math.asin(Math.sin(la1) * Math.cos(d) + Math.cos(la1) * Math.sin(d) * Math.cos(b));
  const lo2 = lo1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la1), Math.cos(d) - Math.sin(la1) * Math.sin(la2));
  return [lo2 * 180 / Math.PI, la2 * 180 / Math.PI];
}}
async function loadGates(k) {{
  const f = FRAMES[k];
  if (!f || !f.bin_url || GATES[k]) return;
  GATES[k] = {{loading: true}};
  try {{
    const r = await fetch(f.bin_url, {{cache: 'force-cache'}});
    const u16 = new Uint16Array(await r.arrayBuffer());
    const naz = u16[0], az = Array.from(u16.subarray(1, 1 + naz), v => v / 100);
    const body = u16.subarray(1 + naz), n = Math.floor(body.length / 3);
    const pos = new Float32Array(n * 8), col = new Uint8Array(n * 16), val = new Float32Array(n * 8);  // [dBZ, clutter] per vertex
    const start = new Uint32Array(n + 1);
    const [rlat, rlon] = f.radar, g0 = f.g0, gw = f.gw;
    // half the step to each neighbouring ray, so bins tile the sweep
    const half = az.map((a, j) => {{
      const p = az[(j - 1 + naz) % naz], q = az[(j + 1) % naz];
      const dp = ((a - p) % 360 + 360) % 360, dq = ((q - a) % 360 + 360) % 360;
      return [Math.min(dp, 2) / 2, Math.min(dq, 2) / 2];
    }});
    for (let g = 0; g < n; g++) {{
      const w = body[3 * g + 2], ri = body[3 * g], gi = body[3 * g + 1], dbz = (w & 0x7FFF) / 2, cl = (w & 0x8000) ? 1 : 0;
      const a0 = az[ri] - half[ri][0], a1 = az[ri] + half[ri][1];
      const r0 = Math.max(0, g0 + gi * gw - gw / 2), r1 = r0 + gw;
      const c = [destPoint(rlat, rlon, a0, r0), destPoint(rlat, rlon, a1, r0), destPoint(rlat, rlon, a1, r1), destPoint(rlat, rlon, a0, r1)];
      for (let v = 0; v < 4; v++) {{ pos[g * 8 + v * 2] = c[v][0]; pos[g * 8 + v * 2 + 1] = c[v][1]; val[g * 8 + v * 2] = dbz; val[g * 8 + v * 2 + 1] = cl; }}
      const rgb = awips(dbz);
      for (let v = 0; v < 4; v++) {{ col.set([rgb[0], rgb[1], rgb[2], 255], g * 16 + v * 4); }}
      start[g + 1] = (g + 1) * 4;
    }}
    // one data object per frame, built once: deck.gl compares `data` by
    // reference, so a fresh object on every redraw re-uploaded every
    // gate twice a second (the drag lag of 23 Oct)
    GATES[k] = {{n, data: {{length: n, startIndices: start, attributes: {{
      getPolygon: {{value: pos, size: 2}}, getFillColor: {{value: col, size: 4}},
      getFilterValue: {{value: val, size: 2}}}}}}}};
  }} catch (e) {{ GATES[k] = {{error: String(e)}}; }}
  evictGates();
  dk.setProps({{layers: allLayers()}});
}}
function gateLayers() {{
  return FRAMES.map((f, k) => {{
    const gd = GATES[k];
    if (!f.bin_url || !gd || !gd.n) return null;
    return new deck.SolidPolygonLayer({{id: 'l2g_' + k, visible: k === i, opacity: radarOp,
      data: gd.data,
      _normalize: false, positionFormat: 'XY', filled: true, extruded: false,
      extensions: [new deck.DataFilterExtension({{filterSize: 2}})], filterRange: [[floorDbz, 999], [0, clutterOff ? 0.5 : 1]],
      parameters: {{depthTest: false}}}});
  }}).filter(Boolean);
}}
function legend() {{
  if (!FRAMES.some(f => f.bin_url)) return;
  const sw = $('sw');
  sw.innerHTML = AWIPS.map(([v, h]) => `<div style="background:${{h}}" class="${{v < floorDbz ? 'off' : ''}}"><span>${{v}}</span></div>`).join('');
  $('fl').value = floorDbz; $('flv').textContent = floorDbz; $('cf').checked = clutterOff;
  $('lg').style.display = 'block';
}}

const LIVE = {json.dumps(live_url)};
const DECLUTTER = {json.dumps(bool(declutter))};
const KEY = 'bm_scope_view_' + {json.dumps(icao)};
const SPEEDS = [250, 500, 900];
let ms = {loop_ms}, i = FRAMES.length - 1, playing = FRAMES.length > 1 && {json.dumps(bool(autoplay))}, tick = null;
// radar opacity (23 Oct): slider in the bar, remembered per browser
let radarOp = 0.85;
try {{ const v = localStorage.getItem('bm_radar_op'); if (v) radarOp = Math.max(0.1, Math.min(1, +v)); }} catch (e) {{}}
const $ = id => document.getElementById(id);

// pydeck's JSON -> deck.gl layers, exactly as the Streamlit component does it
const converter = new deck.JSONConverter({{configuration: new deck.JSONConfiguration({{classes: Object.assign({{}}, deck)}})}});
let baseCache = null;
function baseLayers() {{ if (!baseCache) baseCache = converter.convert({{layers: DJ.layers}}).layers; return baseCache; }}
function radarLayers() {{
  // A frame is either one image ({{url, bounds}}: Level III) or a tile
  // template ({{tiles, maxZoom}}: GOES visible from NASA GIBS, 5 Oct).
  // Every frame stays mounted at opacity 0/1 so each has its tiles
  // before the loop reaches it. A frame may carry a finer inner tile
  // ({{inner_url, inner_bounds}}, 23 Oct) drawn over the wide one.
  const inner = FRAMES.map((f, k) => f.inner_url
    ? new deck.BitmapLayer({{id: 'l3i_' + k, image: f.inner_url, bounds: f.inner_bounds,
        opacity: k === i ? radarOp : 0, visible: true, parameters: {{depthTest: false}},
        textureParameters: RAWTEX}}) : null).filter(Boolean);
  return FRAMES.map((f, k) => f.bin_url ? null : f.tiles
    ? new deck.TileLayer({{id: 'sat_' + k, data: f.tiles, minZoom: 0, maxZoom: f.maxZoom || 7,
        tileSize: 256, opacity: k === i ? (f.opacity != null ? f.opacity : 1) * radarOp : 0, visible: true,
        maxRequests: 12, refinementStrategy: 'no-overlap',
        renderSubLayers: props => {{
          const {{west, south, east, north}} = props.tile.bbox;
          return new deck.BitmapLayer(props, {{data: null, image: props.data,
            bounds: [west, south, east, north], parameters: {{depthTest: false}}}});
        }}}})
    : new deck.BitmapLayer({{id: 'l3_' + k, image: f.url, bounds: f.bounds,
        opacity: k === i ? radarOp : 0, visible: true, parameters: {{depthTest: false}},
        textureParameters: f.raw ? RAWTEX : undefined}})).filter(Boolean).concat(inner, gateLayers());
}}
// ---- selected aircraft: panel + dotted track (3 Oct) ----
let sel = null;
function gc(a, b, n) {{   // great-circle points [lon,lat] from a to b, n steps
  const r = Math.PI / 180, la1 = a[1] * r, lo1 = a[0] * r, la2 = b[1] * r, lo2 = b[0] * r;
  const d = 2 * Math.asin(Math.sqrt(Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2));
  const out = [];
  if (d < 1e-9) return [a, b];
  for (let i = 0; i <= n; i++) {{
    const f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
    const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
    const z = A * Math.sin(la1) + B * Math.sin(la2);
    out.push([Math.atan2(y, x) / r, Math.atan2(z, Math.sqrt(x * x + y * y)) / r]);
  }}
  return out;
}}
function routeLayers() {{
  if (!sel) return [];
  const pos = sel.position, dots = [], segs = [];
  const o = sel.oll ? [sel.oll[1], sel.oll[0]] : null, d = sel.dll ? [sel.dll[1], sel.dll[0]] : null;
  // one dot about every 3 km, so the track reads as dotted at any zoom
  const km = (a, b) => 6371 * 2 * Math.asin(Math.sqrt(Math.sin((b[1] - a[1]) * Math.PI / 360) ** 2 + Math.cos(a[1] * Math.PI / 180) * Math.cos(b[1] * Math.PI / 180) * Math.sin((b[0] - a[0]) * Math.PI / 360) ** 2));
  const nseg = (a, b) => Math.max(40, Math.min(1500, Math.round(km(a, b) / 3)));
  if (o) {{ const p = gc(o, pos, nseg(o, pos)); segs.push({{path: p}}); p.forEach(q => dots.push({{position: q, flown: true}})); }}
  if (d) {{ const p = gc(pos, d, nseg(pos, d)); segs.push({{path: p}}); p.forEach(q => dots.push({{position: q, flown: false}})); }}
  if (!segs.length) return [];
  return [
    new deck.PathLayer({{id: 'sel_route', data: segs, getPath: x => x.path, getColor: [20, 30, 60, 220],
      widthMinPixels: 4, widthMaxPixels: 4, parameters: {{depthTest: false}}}}),
    new deck.ScatterplotLayer({{id: 'sel_dots', data: dots, getPosition: x => x.position,
      getFillColor: x => x.flown ? [120, 170, 255, 255] : [77, 163, 255, 255],
      radiusMinPixels: 2.2, radiusMaxPixels: 2.2, parameters: {{depthTest: false}}}}),
    new deck.ScatterplotLayer({{id: 'sel_ends', data: [o, d].filter(Boolean).map(p => ({{position: p}})),
      getPosition: x => x.position, getFillColor: [77, 163, 255, 255], getLineColor: [255, 255, 255, 255],
      stroked: true, lineWidthMinPixels: 1.5, radiusMinPixels: 5, radiusMaxPixels: 5, parameters: {{depthTest: false}}}})
  ];
}}
function fmtAlt(a) {{ if (a == null || a === '') return 'n/a'; a = +a; return a >= 18000 ? 'FL' + String(Math.round(a / 100)).padStart(3, '0') : Math.round(a).toLocaleString() + ' ft'; }}
function airline(cs) {{ const m = {{JBU: 'JetBlue Airways', DAL: 'Delta Air Lines', UAL: 'United Airlines', AAL: 'American Airlines', SWA: 'Southwest Airlines', NKS: 'Spirit Airlines', FFT: 'Frontier Airlines', ASA: 'Alaska Airlines', RPA: 'Republic Airways', EDV: 'Endeavor Air', SKW: 'SkyWest', ENY: 'Envoy Air', JIA: 'PSA Airlines', PDT: 'Piedmont', FDX: 'FedEx', UPS: 'UPS', ACA: 'Air Canada', BAW: 'British Airways', VIR: 'Virgin Atlantic', AFR: 'Air France', DLH: 'Lufthansa', AVA: 'Avianca', CMP: 'Copa Airlines', AMX: 'Aeromexico', WJA: 'WestJet'}}; return m[(cs || '').slice(0, 3)] || ''; }}
function showPanel(a) {{
  const el = $('ac');
  if (!a) {{ el.style.display = 'none'; return; }}
  const cs = a.callsign || '', iata = cs.startsWith('JBU') ? 'B6' + cs.slice(3) : '';
  const leg = (lab, ap, nm, cls) => ap ? `<div class="leg ${{cls}}"><div class="nm">${{lab}}</div><div class="ap">${{ap}}</div><div class="nm">${{nm || ''}}</div></div>` : '';
  el.innerHTML = `<div class="hd"><b>${{cs}}</b>${{iata ? '<span>/ ' + iata + '</span>' : ''}}<span>${{airline(cs)}}</span><span class="x" onclick="select(null)">&times;</span></div>
    <div class="row"><span class="k">Aircraft</span><span class="v">${{a.type || 'n/a'}}</span></div>
    <div class="row"><span class="k">Altitude / speed</span><span class="v">${{fmtAlt(a.alt)}} &middot; ${{a.gs != null && a.gs !== '' ? Math.round(+a.gs) + ' kt' : 'n/a'}}</span></div>
    ${{leg('ORIGIN', a.origin, a.origin_name, 'orig')}}${{leg('DESTINATION', a.dest, a.dest_name, 'dest')}}
    ${{!a.origin && !a.dest ? '<div class="ft">Route not resolved yet - it appears on the next refresh once adsbdb answers.</div>' : '<div class="ft">Dotted track: great circle origin → aircraft → destination (adsbdb route)</div>'}}`;
  el.style.display = 'block';
}}
function select(a) {{ sel = a; showPanel(a); dk.setProps({{layers: allLayers()}}); }}
// ---- LIVE AIRCRAFT (22 Oct): poll aradar_live_<ICAO>.json every few
// seconds and dead-reckon each airborne aircraft from its last fix
// (ground speed along track), so icons glide between fixes instead
// of jumping once a sweep. When live data is in hand the server-drawn
// ac_* and hold_* layers are hidden and these take their place.
let live = null, liveAt = 0;
async function pollLive() {{
  if (!LIVE) return;
  try {{
    const r = await fetch(LIVE + (LIVE.includes('?') ? '&' : '?') + 't=' + Date.now(), {{cache: 'no-store'}});
    if (r.ok) {{
      live = await r.json(); liveAt = Date.now();
      if (sel) {{ const s = (live.ac || []).find(a => a.callsign === sel.callsign); if (s) sel = Object.assign(sel, s); }}
    }}
  }} catch (e) {{}}
}}
function drPos(a) {{
  if (!live || a.ground || !a.gs || a.gs < 50) return a.position;
  // seconds since the fix: wall clock minus (server write time - feed age)
  const dt = Math.min(150, Math.max(0, Date.now() / 1000 - (live.t - (a.age || 0))));
  const r = Math.PI / 180, b = (a.hdg || 0) * r, d = a.gs * dt / 3600;   // nm
  return [a.position[0] + d / 60 * Math.sin(b) / Math.cos(a.position[1] * r), a.position[1] + d / 60 * Math.cos(b)];
}}
// Catmull-Rom through every fix (22 Oct): fixes come every ~10 s, so
// a straight polyline shows a turn as a chain of doglegs. The spline
// passes through each fix exactly and rounds the corners between
// them; the current (dead-reckoned) position is the last knot.
function smooth(pts, n) {{
  if (pts.length < 3) return pts;
  const out = [pts[0]];
  for (let i = 0; i < pts.length - 1; i++) {{
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    for (let k = 1; k <= n; k++) {{
      const t = k / n, t2 = t * t, t3 = t2 * t;
      out.push([0, 1].map(j => 0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t
        + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)));
    }}
  }}
  return out;
}}
function liveLayers() {{
  if (!live) return [];
  const z = curZoom == null ? (DJ.initialViewState || {{}}).zoom : curZoom;
  const close = z != null && z >= AIRPORT_ZOOM;
  const rows = (live.ac || []).filter(a => close || !a.ground)
    .map(a => Object.assign({{}}, a, {{position: drPos(a), angle: (360 - (a.hdg || 0)) % 360}}));
  const trails = rows.filter(a => !a.ground && a.trail && a.trail.length > 0)
    .map(a => ({{path: smooth(a.trail.concat([a.position]), 6), color: a.rgb.concat([140])}}));
  const holdSet = new Set(live.hold || []);
  const holds = rows.filter(a => holdSet.has(a.callsign));
  const P = {{depthTest: false}};
  return [
    new deck.PathLayer({{id: 'live_trails', data: trails, getPath: d => d.path, getColor: d => d.color,
      getWidth: 60, widthMinPixels: 1.5, widthMaxPixels: 2, pickable: false, parameters: P}}),
    new deck.ScatterplotLayer({{id: 'live_hold', data: holds, getPosition: d => d.position, getRadius: 2600,
      radiusMinPixels: 16, radiusMaxPixels: 26, filled: false, stroked: true, getLineColor: [255, 212, 0, 255],
      lineWidthMinPixels: 2.5, pickable: false, parameters: P}}),
    new deck.TextLayer({{id: 'live_hold_lab', data: holds, getPosition: d => d.position, getText: d => d.callsign + ' HOLDING',
      getColor: [255, 212, 0, 255], getSize: 12, sizeUnits: 'pixels', getPixelOffset: [0, -24],
      getTextAnchor: 'middle', fontFamily: live.font || 'monospace', fontWeight: 700, pickable: false, parameters: P}}),
    new deck.TextLayer({{id: 'live_tags', data: rows, getPosition: d => d.position, getText: d => d.callsign,
      getColor: d => d.rgb.concat([235]), getSize: live.tag_fs || 7, sizeUnits: 'pixels', getPixelOffset: [7, 8],
      getTextAnchor: 'start', getAlignmentBaseline: 'top', fontFamily: live.font || 'monospace', fontWeight: 700,
      pickable: false, parameters: P}}),
  ].concat(['bizjet', 'regional', 'narrow', 'wide'].map(g => {{
    const px = (live.icon_px || {{}})[g] || 22;
    return new deck.IconLayer({{id: 'live_planes_' + g, data: rows.filter(d => (d.glyph || 'narrow') === g),
      getPosition: d => d.position, getIcon: d => {{ const ic = live.icons || {{}}; return ic[d.ikey] || ic['narrow:' + d.colour] || ic[d.colour] || Object.values(ic)[0]; }},
      getAngle: d => d.angle, getSize: 5000, sizeMinPixels: px, sizeMaxPixels: px, pickable: true,
      autoHighlight: false, parameters: P}});
  }}));
}}
// DECLUTTER (20 Oct): ground aircraft come as two sets of layers -
// ac_gnd_agg* (one glyph + count per airport) for the wide view and
// ac_gnd_* (every aircraft) for the airport-scale view. Zoom decides.
const AIRPORT_ZOOM = {airport_zoom};
let curZoom = null, zoomMode = null, dragging = false;
function allLayers() {{
  const z = curZoom == null ? (DJ.initialViewState || {{}}).zoom : curZoom;
  const close = z != null && z >= AIRPORT_ZOOM;
  const b = baseLayers().map(l => {{
    const id = l.id || '';
    if (id.startsWith('ac_gnd_agg')) return l.clone({{visible: !close}});
    if (live && (id.startsWith('ac_') || id.startsWith('hold_'))) return l.clone({{visible: false}});
    if (id.startsWith('ac_gnd_')) return l.clone({{visible: close}});
    return l;
  }});
  return b.slice(0, RADAR_AT).concat(radarLayers(), routeLayers(), b.slice(RADAR_AT), liveLayers());
}}
let saved = null;
try {{ saved = JSON.parse(sessionStorage.getItem(KEY) || 'null'); }} catch (e) {{}}
const vs0 = Object.assign({{}}, DJ.initialViewState);
const vs = saved && saved.zoom ? Object.assign({{}}, vs0, saved) : vs0;
function fill(tpl, o) {{ return tpl.replace(/\\{{(\\w+)\\}}/g, (m, k) => (o && o[k] != null) ? o[k] : ''); }}
const dk = new deck.DeckGL({{
  container: 'm', map: STYLE ? maplibregl : null, mapStyle: STYLE || undefined,
  initialViewState: vs, controller: {json.dumps(ctrl)},
  // pydeck's clearColor [0,0,0,1] paints deck's canvas opaque black ON
  // TOP of the MapLibre canvas - the basemap was there all along, just
  // hidden (found 5 Oct). Transparent clear lets the map show through.
  parameters: Object.assign({{}}, DJ.parameters || {{}}, {{clearColor: [0, 0, 0, 0]}}),
  layers: allLayers(),
  onDragStart: () => {{ dragging = true; }}, onDragEnd: () => {{ dragging = false; }},
  onViewStateChange: ({{viewState}}) => {{
    try {{ sessionStorage.setItem(KEY, JSON.stringify({{longitude: viewState.longitude, latitude: viewState.latitude, zoom: viewState.zoom}})); }} catch (e) {{}}
    curZoom = viewState.zoom;
    const mode = curZoom >= AIRPORT_ZOOM;
    if (mode !== zoomMode) {{ zoomMode = mode; dk.setProps({{layers: allLayers()}}); }}
  }},
  getTooltip: ({{object}}) => object && TIP.html ? {{html: fill(TIP.html, object), style: TIP.style || {{}}}} : null,
  onClick: info => {{ select(info && info.object && info.object.callsign ? info.object : null); }}
}});
// Basemap tone (5 Oct): CARTO Dark Matter is near-black (#0e0e0e land);
// the tomorrow.io flight map is a lighter charcoal with grey roads and
// readable place names. Re-tone the loaded style the same way.
function retone() {{
  let m = null;
  try {{ m = dk.getMapboxMap && dk.getMapboxMap(); }} catch (e) {{}}
  if (!m || !m.getStyle) return;
  const apply = () => {{
    const st = m.getStyle(); if (!st || !st.layers) return;
    for (const l of st.layers) {{
      try {{
        // DECLUTTER (22 Oct, Airport Radar): minor roads, buildings,
        // rail, road names, POIs and small place names go; motorways,
        // trunks, water, boundaries, towns and cities stay.
        // (24 Oct) every road, tunnel, bridge and rail layer goes too -
        // an aviation scope wants water, coastlines, boundaries, place
        // names and runways, not the highway network
        if (DECLUTTER && /road|tunnel|bridge|rail|path|building|housenumber|poi_|place_hamlet|place_suburbs|place_villages|landuse|park_|watername_lake|waterway/.test(l.id)) {{
          m.setLayoutProperty(l.id, 'visibility', 'none'); continue;
        }}
        if (l.type === 'background') m.setPaintProperty(l.id, 'background-color', '#2A2C30');
        else if (l.type === 'fill' && /water/.test(l.id)) m.setPaintProperty(l.id, 'fill-color', '#15181D');
        else if (l.type === 'fill' && /land|park|wood|green|grass|cemetery|pitch|stadium/.test(l.id)) m.setPaintProperty(l.id, 'fill-color', '#2E3035');
        else if (l.type === 'fill' && /building/.test(l.id)) m.setPaintProperty(l.id, 'fill-color', '#34363B');
        else if (l.type === 'fill') m.setPaintProperty(l.id, 'fill-color', '#2C2E33');
        else if (l.type === 'line' && /road|street|highway|motorway|trunk|primary|secondary|tertiary|minor|path|rail/.test(l.id)) {{
          m.setPaintProperty(l.id, 'line-color', /motorway|trunk|highway/.test(l.id) ? '#6E7178' : '#4A4D53');
        }}
        else if (l.type === 'line' && /boundary|admin/.test(l.id)) m.setPaintProperty(l.id, 'line-color', '#7A7D85');
        else if (l.type === 'line' && /water|river/.test(l.id)) m.setPaintProperty(l.id, 'line-color', '#1E232B');
        else if (l.type === 'symbol') {{
          m.setPaintProperty(l.id, 'text-color', '#D8DADF');
          m.setPaintProperty(l.id, 'text-halo-color', '#1A1C20');
          m.setPaintProperty(l.id, 'text-halo-width', 1.2);
        }}
      }} catch (e) {{}}
    }}
  }};
  if (m.isStyleLoaded && m.isStyleLoaded()) apply(); else m.once('style.load', apply);
  m.once('load', apply);
}}
setTimeout(retone, 300); setTimeout(retone, 2500);
function show(k) {{
  i = Math.max(0, Math.min(FRAMES.length - 1, k)); $('sl').value = i;
  if (FRAMES[i] && FRAMES[i].bin_url && !GATES[i]) loadGates(i);
  $('t').textContent = FRAMES.length ? LABELS[i] : (DJ.noFramesText || 'no radar');
  $('n').textContent = FRAMES.length ? 'frame ' + (i + 1) + '/' + FRAMES.length + (i === FRAMES.length - 1 ? ' (latest)' : '') : '';
  dk.setProps({{layers: allLayers()}});
}}
function step() {{ show((i + 1) % FRAMES.length); tick = setTimeout(step, i === FRAMES.length - 1 ? ms * 3 : ms); }}
function play(on) {{
  playing = on; $('pp').textContent = on ? 'Pause' : 'Play'; $('pp').classList.toggle('on', on); clearTimeout(tick);
  if (on && FRAMES.length > 1) {{
    // decode the loop's frames (newest back) before it starts running
    (async () => {{ for (let k = FRAMES.length - 1; k >= Math.max(0, FRAMES.length - MAX_DECODED); k--) await loadGates(k); }})();
    tick = setTimeout(step, ms);
  }}
}}
$('pp').onclick = () => play(!playing);
$('pv').onclick = () => {{ play(false); show(i - 1); }};
$('nx').onclick = () => {{ play(false); show(i + 1); }};
$('sl').oninput = e => {{ play(false); show(+e.target.value); }};
$('sp').onclick = () => {{ ms = SPEEDS[(SPEEDS.indexOf(ms) + 1) % SPEEDS.length]; $('sp').textContent = ms + ' ms'; if (playing) play(true); }};
$('op').value = Math.round(radarOp * 100);
$('op').oninput = e => {{ radarOp = +e.target.value / 100; try {{ localStorage.setItem('bm_radar_op', radarOp); }} catch (e2) {{}} dk.setProps({{layers: allLayers()}}); }};
$('rv').onclick = () => {{ try {{ sessionStorage.removeItem(KEY); }} catch (e) {{}} dk.setProps({{initialViewState: Object.assign({{}}, vs0, {{transitionDuration: 300}})}}); }};
document.addEventListener('keydown', e => {{ if (e.key === 'ArrowLeft') $('pv').onclick(); if (e.key === 'ArrowRight') $('nx').onclick(); if (e.key === ' ') {{ e.preventDefault(); play(!playing); }} }});
// Preload every frame so the first loop is already smooth.
FRAMES.forEach((f, k) => {{ if (f.url) {{ const im = new Image(); im.src = f.url; }} if (f.inner_url) {{ const im2 = new Image(); im2.src = f.inner_url; }} }});
// gate files: the frame on screen now; the rest when stepped to / played
loadGates(i);
legend();
$('fl').oninput = e => {{ floorDbz = +e.target.value; try {{ localStorage.setItem('bm_radar_floor', floorDbz); }} catch (e2) {{}} legend(); dk.setProps({{layers: allLayers()}}); }};
$('cf').onchange = e => {{ clutterOff = e.target.checked; try {{ localStorage.setItem('bm_radar_clutter', clutterOff ? '1' : '0'); }} catch (e2) {{}} dk.setProps({{layers: allLayers()}}); }};
show(i); play(playing);
if (LIVE) {{
  pollLive(); setInterval(pollLive, 6000);
  // redraw at 2 Hz: positions are dead-reckoned, so this is movement
  // between fixes, not a server round trip
  setInterval(() => {{ if (live && !dragging) dk.setProps({{layers: allLayers()}}); }}, 500);
}}
</script></body></html>"""
