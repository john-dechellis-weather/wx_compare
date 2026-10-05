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
               radar_index: int | None = None, loop_ms: int = 500) -> str:
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
</style></head><body>
<div id="m"></div><div class="tip" id="tip"></div>
<div id="ac"></div>
<div class="bar">
  <button id="pp">Pause</button><button id="pv">&#9664;</button><button id="nx">&#9654;</button>
  <input id="sl" type="range" min="0" max="{max(len(frames) - 1, 0)}" value="{max(len(frames) - 1, 0)}">
  <span id="t"></span><span id="n"></span>
  <button id="sp" title="loop speed">{loop_ms} ms</button>
  <button id="rv" title="back to the opening view">Reset view</button>
</div>
<script>
const DJ = {json.dumps(dj)};
const FRAMES = {json.dumps(frames)};
const LABELS = {json.dumps(labels)};
const STYLE = {json.dumps(style_url)};
const TIP = {json.dumps(tip)};
const RADAR_AT = {radar_index};
const KEY = 'bm_scope_view_' + {json.dumps(icao)};
const SPEEDS = [250, 500, 900];
let ms = {loop_ms}, i = FRAMES.length - 1, playing = FRAMES.length > 1, tick = null;
const $ = id => document.getElementById(id);

// pydeck's JSON -> deck.gl layers, exactly as the Streamlit component does it
const converter = new deck.JSONConverter({{configuration: new deck.JSONConfiguration({{classes: Object.assign({{}}, deck)}})}});
function baseLayers() {{ return converter.convert({{layers: DJ.layers}}).layers; }}
function radarLayers() {{
  return FRAMES.map((f, k) => new deck.BitmapLayer({{id: 'l3_' + k, image: f.url, bounds: f.bounds,
    opacity: k === i ? 1 : 0, visible: true, parameters: {{depthTest: false}}}}));
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
function allLayers() {{
  const b = baseLayers();
  return b.slice(0, RADAR_AT).concat(radarLayers(), routeLayers(), b.slice(RADAR_AT));
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
  onViewStateChange: ({{viewState}}) => {{
    try {{ sessionStorage.setItem(KEY, JSON.stringify({{longitude: viewState.longitude, latitude: viewState.latitude, zoom: viewState.zoom}})); }} catch (e) {{}}
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
  $('t').textContent = FRAMES.length ? LABELS[i] : 'no radar';
  $('n').textContent = FRAMES.length ? 'frame ' + (i + 1) + '/' + FRAMES.length + (i === FRAMES.length - 1 ? ' (latest)' : '') : '';
  dk.setProps({{layers: allLayers()}});
}}
function step() {{ show((i + 1) % FRAMES.length); tick = setTimeout(step, i === FRAMES.length - 1 ? ms * 3 : ms); }}
function play(on) {{ playing = on; $('pp').textContent = on ? 'Pause' : 'Play'; $('pp').classList.toggle('on', on); clearTimeout(tick); if (on && FRAMES.length > 1) tick = setTimeout(step, ms); }}
$('pp').onclick = () => play(!playing);
$('pv').onclick = () => {{ play(false); show(i - 1); }};
$('nx').onclick = () => {{ play(false); show(i + 1); }};
$('sl').oninput = e => {{ play(false); show(+e.target.value); }};
$('sp').onclick = () => {{ ms = SPEEDS[(SPEEDS.indexOf(ms) + 1) % SPEEDS.length]; $('sp').textContent = ms + ' ms'; if (playing) play(true); }};
$('rv').onclick = () => {{ try {{ sessionStorage.removeItem(KEY); }} catch (e) {{}} dk.setProps({{initialViewState: Object.assign({{}}, vs0, {{transitionDuration: 300}})}}); }};
document.addEventListener('keydown', e => {{ if (e.key === 'ArrowLeft') $('pv').onclick(); if (e.key === 'ArrowRight') $('nx').onclick(); if (e.key === ' ') {{ e.preventDefault(); play(!playing); }} }});
// Preload every frame so the first loop is already smooth.
FRAMES.forEach(f => {{ const im = new Image(); im.src = f.url; }});
show(i); play(playing);
</script></body></html>"""
