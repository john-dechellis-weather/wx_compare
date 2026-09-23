"""
Large Scale Map of North America — full-size CONUS map and the Aguacero test bench.

Everything interactive runs inside the map iframe, so switching MRMS products
does NOT rerun Streamlit and does NOT re-initialise the SDK. That matters for
billing: every Streamlit rerun would rebuild the map and re-download frames.

Env var (Render dashboard):
    AGUACERO_VIZ_KEY   Visualization key (bare 40-hex, NOT an ag_live_ key)

Billing notes baked into the UI:
  * The MRMS inventory (which products have frames, how fresh) comes from the
    listing endpoint, which is not billed.
  * Loading a product downloads every frame in the timeline window. The page
    opens with the window at "Latest frame", so one product = one call.
    A 1-hour window on a 2-minute product is ~30 calls per product switch.
  * "Test every product" loads each one at latest-frame only (~44 calls total).
"""

import json
import os

import streamlit as st
from streamlit.components.v1 import html as st_html

try:
    st.set_page_config(page_title="Large Scale Map of North America", layout="wide")
except Exception:
    pass  # already set by Homepage.py / st.navigation

st.markdown(
    """
    <style>
      .stApp { background:#000; }
      html, body, [class*="css"], label, .stMarkdown {
          font-family:'Roboto',sans-serif !important; font-weight:700 !important; color:#fff !important; }
      .lsm-title { color:#FFD700; font-size:16pt; font-weight:700; margin:0 0 4px 0; }
      .block-container { padding-top:1rem; padding-bottom:0; }
    </style>
    """,
    unsafe_allow_html=True,
)

SDK = "https://cdn.jsdelivr.net/npm/@aguacerowx/mapsgl@1.0.5/+esm"
MAPLIBRE = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm"
MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css"
STYLE_URL = "https://tiles.openfreemap.org/styles/dark"
BELOW_ID = "boundary_state"  # radar draws under state/country lines and city labels

# Every MRMS variable the SDK itself ships (MODEL_CONFIGS.mrms.vars in
# @aguacerowx/javascript-sdk). Note there is no MRMS echo-top product in it.
MRMS_GROUPS = [
    ("Reflectivity", [
        ("MergedReflectivityQCComposite_00.50", "Composite reflectivity"),
        ("SeamlessHSR_00.00", "Seamless hybrid-scan reflectivity"),
        ("CREF_1HR_MAX_00.50", "Composite reflectivity, 1-hr max"),
        ("ptypeRefl", "Reflectivity by precip type"),
    ]),
    ("Dual-pol", [
        ("MergedZdr_04.00", "ZDR, 4 km"),
        ("MergedRhoHV_04.00", "Correlation coefficient, 4 km"),
    ]),
    ("VIL", [
        ("VIL_00.50", "VIL"),
        ("VIL_Density_00.50", "VIL density"),
        ("LVL3_HighResVIL_00.50", "High-res VIL (Level III)"),
        ("VIL_Max_120min_00.50", "VIL max, 2 hr"),
        ("VIL_Max_1440min_00.50", "VIL max, 24 hr"),
    ]),
    ("Lightning", [
        ("LightningProbabilityNext30minGrid_scale_1", "Lightning probability, next 30 min"),
        ("LightningProbabilityNext60minGrid_scale_1", "Lightning probability, next 60 min"),
    ]),
    ("Hail", [
        ("MESH_00.50", "MESH, instantaneous"),
        ("MESH_Max_30min_00.50", "MESH max, 30 min"),
        ("MESH_Max_60min_00.50", "MESH max, 60 min"),
        ("MESH_Max_120min_00.50", "MESH max, 2 hr"),
        ("MESH_Max_240min_00.50", "MESH max, 4 hr"),
        ("MESH_Max_360min_00.50", "MESH max, 6 hr"),
        ("MESH_Max_1440min_00.50", "MESH max, 24 hr"),
    ]),
    ("Rotation", [
        ("MergedAzShear_0-2kmAGL_00.50", "Az shear, 0-2 km AGL"),
        ("MergedAzShear_3-6kmAGL_00.50", "Az shear, 3-6 km AGL"),
        ("RotationTrack30min_00.50", "Rotation track, 30 min"),
        ("RotationTrack60min_00.50", "Rotation track, 60 min"),
        ("RotationTrack120min_00.50", "Rotation track, 2 hr"),
        ("RotationTrack240min_00.50", "Rotation track, 4 hr"),
        ("RotationTrack360min_00.50", "Rotation track, 6 hr"),
        ("RotationTrack1440min_00.50", "Rotation track, 24 hr"),
        ("RotationTrackML30min_00.50", "Rotation track ML, 30 min"),
        ("RotationTrackML60min_00.50", "Rotation track ML, 60 min"),
        ("RotationTrackML120min_00.50", "Rotation track ML, 2 hr"),
        ("RotationTrackML240min_00.50", "Rotation track ML, 4 hr"),
        ("RotationTrackML360min_00.50", "Rotation track ML, 6 hr"),
        ("RotationTrackML1440min_00.50", "Rotation track ML, 24 hr"),
    ]),
    ("Precipitation", [
        ("MultiSensor_QPE_01H_Pass1_00.00", "QPE, 1 hr"),
        ("MultiSensor_QPE_03H_Pass1_00.00", "QPE, 3 hr"),
        ("MultiSensor_QPE_06H_Pass1_00.00", "QPE, 6 hr"),
        ("MultiSensor_QPE_12H_Pass1_00.00", "QPE, 12 hr"),
        ("MultiSensor_QPE_24H_Pass1_00.00", "QPE, 24 hr"),
        ("MultiSensor_QPE_48H_Pass1_00.00", "QPE, 48 hr"),
        ("MultiSensor_QPE_72H_Pass1_00.00", "QPE, 72 hr"),
    ]),
    ("Flash flood", [
        ("FLASH_QPE_ARI30M_00.00", "Flash flood ARI, 30 min"),
        ("FLASH_QPE_ARI01H_00.00", "Flash flood ARI, 1 hr"),
        ("FLASH_QPE_ARI03H_00.00", "Flash flood ARI, 3 hr"),
        ("FLASH_QPE_ARI06H_00.00", "Flash flood ARI, 6 hr"),
        ("FLASH_QPE_ARI12H_00.00", "Flash flood ARI, 12 hr"),
        ("FLASH_QPE_ARI24H_00.00", "Flash flood ARI, 24 hr"),
        ("FLASH_QPE_ARIMAX_00.00", "Flash flood ARI, max"),
    ]),
]

st.markdown('<div class="lsm-title">Large Scale Map of North America</div>', unsafe_allow_html=True)

api_key = os.environ.get("AGUACERO_VIZ_KEY", "").strip()
with st.expander("Test settings", expanded=not api_key):
    if not api_key:
        st.caption("AGUACERO_VIZ_KEY is not set on Render. Paste the Visualization key to test.")
    api_key = st.text_input("Visualization key", value=api_key, type="password").strip()
    map_height = st.slider("Map height (px)", 600, 1400, 900, 50)

if not api_key:
    st.stop()

cfg = {
    "apiKey": api_key,
    "userId": "bluemet-lsm",
    "groups": MRMS_GROUPS,
    "default": "MergedReflectivityQCComposite_00.50",
    "sdk": SDK,
    "maplibre": MAPLIBRE,
    "styleURL": STYLE_URL,
    "belowID": BELOW_ID,
}

HTML = r"""
<link href="__CSS__" rel="stylesheet" />
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@700&family=Roboto+Mono:wght@700&display=swap" rel="stylesheet">
<style>
  :root { --ink:#fff; --dim:#8a8f98; --line:#2a2d33; --cyan:#22d3ee; --gold:#FFD700;
          --green:#3ddc84; --yellow:#ffd23f; --orange:#ff8c1a; --pink:#ff4fa3; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; background:#000; color:var(--ink);
              font:700 12pt/1.35 Roboto, Arial, sans-serif; }
  #map { position:absolute; inset:0 0 34px 0; background:#000; }

  /* control panel, top left */
  #panel { position:absolute; top:10px; left:10px; width:340px; z-index:5;
           background:#000; border:1px solid var(--line); padding:10px 12px; }
  #panel label { display:block; color:var(--dim); font-size:12pt; margin:8px 0 3px; }
  #panel select, #panel button { font:700 12pt Roboto, Arial, sans-serif; color:#fff;
           background:#000; border:1px solid #444; padding:5px 6px; }
  #panel select { width:100%; }
  #panel button { cursor:pointer; }
  #panel button:hover { border-color:var(--cyan); }
  #panel button:focus-visible, #panel select:focus-visible { outline:2px solid var(--cyan); }
  .row { display:flex; gap:6px; align-items:center; }
  .row > * { flex:0 0 auto; }
  #frame { font-family:'Roboto Mono', monospace; margin-left:4px; }
  #est { color:var(--dim); font-size:12pt; margin-top:3px; }
  input[type=range] { width:100%; accent-color:var(--cyan); }

  /* inventory, right side */
  #inv { position:absolute; top:10px; right:10px; bottom:130px; width:470px; z-index:5;
         background:#000; border:1px solid var(--line); display:none; flex-direction:column; }
  #inv.open { display:flex; }
  #invhead { padding:8px 12px; border-bottom:1px solid var(--line);
             display:flex; justify-content:space-between; align-items:center; gap:8px; }
  #invhead span { color:var(--gold); }
  #invbody { overflow:auto; flex:1; }
  #invbody table { width:100%; border-collapse:collapse; font-size:12pt; }
  #invbody td { padding:4px 8px; border-bottom:1px solid #15171a; vertical-align:top; }
  #invbody tr.grp td { color:var(--gold); padding-top:10px; border-bottom:1px solid var(--line); }
  #invbody tr.p { cursor:pointer; }
  #invbody tr.p:hover td { background:#0d1418; }
  #invbody tr.sel td:first-child { box-shadow:inset 3px 0 0 var(--cyan); }
  .id { color:var(--dim); font-size:12pt; font-family:'Roboto Mono', monospace; word-break:break-all; }
  .num { text-align:right; font-family:'Roboto Mono', monospace; white-space:nowrap; }
  .fresh { color:var(--green); } .stale { color:var(--yellow); } .old { color:var(--orange); }
  .none { color:var(--pink); } .muted { color:var(--dim); }

  /* legend, bottom right */
  #legend { position:absolute; right:10px; bottom:44px; z-index:5; background:#000;
            border:1px solid var(--line); padding:8px 10px; min-width:320px; display:none; }
  #legend .t { margin-bottom:5px; }
  #lbar { display:flex; height:14px; }
  #lbar div { flex:1; }
  #lticks { display:flex; justify-content:space-between; font-family:'Roboto Mono', monospace;
            font-size:12pt; margin-top:3px; }

  /* status strip */
  #hud { position:absolute; left:0; right:0; bottom:0; height:34px; background:#000;
         border-top:1px solid var(--line); padding:6px 12px; white-space:nowrap; overflow:hidden;
         font-size:12pt; }
  #hud .k { color:var(--dim); margin-right:4px; }
  #hud .sep { color:#333; margin:0 10px; }
  .ok { color:var(--green); } .bad { color:var(--pink); } .warn { color:var(--yellow); }
</style>

<div id="map"></div>

<div id="panel">
  <label for="prod" style="margin-top:0">MRMS product</label>
  <select id="prod" disabled><option>Loading inventory…</option></select>

  <div class="row" style="margin-top:8px">
    <button id="back" title="Previous frame" aria-label="Previous frame">◀</button>
    <button id="play" title="Play loop">Play</button>
    <button id="fwd" title="Next frame" aria-label="Next frame">▶</button>
    <span id="frame">—</span>
  </div>

  <label for="win">Timeline window</label>
  <select id="win">
    <option value="0.01" selected>Latest frame only</option>
    <option value="0.5">30 minutes</option>
    <option value="1">1 hour</option>
    <option value="3">3 hours</option>
    <option value="6">6 hours</option>
    <option value="12">12 hours</option>
  </select>
  <div id="est"></div>

  <label for="op">Opacity</label>
  <input id="op" type="range" min="0" max="1" step="0.05" value="0.85">

  <div class="row" style="margin-top:10px; flex-wrap:wrap">
    <label style="margin:0; color:#fff; display:flex; gap:6px; align-items:center">
      <input id="auto" type="checkbox"> Auto-refresh (2 min)
    </label>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="invbtn">Show inventory</button>
    <button id="testall">Test every product</button>
  </div>
</div>

<div id="inv">
  <div id="invhead">
    <span>MRMS inventory</span>
    <div class="row">
      <button id="reinv" style="font:700 12pt Roboto;color:#fff;background:#000;border:1px solid #444;padding:4px 6px;cursor:pointer">Refresh</button>
      <button id="closeinv" style="font:700 12pt Roboto;color:#fff;background:#000;border:1px solid #444;padding:4px 6px;cursor:pointer">Close</button>
    </div>
  </div>
  <div id="invbody"><table><tbody id="invrows"></tbody></table></div>
</div>

<div id="legend"><div class="t" id="ltitle"></div><div id="lbar"></div><div id="lticks"></div></div>

<div id="hud">
  <span class="k">Status</span><span id="status" class="warn">loading SDK…</span>
  <span class="sep">|</span><span class="k">Billable calls</span><span id="calls" class="ok">0</span>
  <span class="sep">|</span><span class="k">Not billed</span><span id="free">0</span>
  <span class="sep">|</span><span class="k">Failed</span><span id="fail">0</span>
  <span class="sep">|</span><span class="k">Downloaded</span><span id="bytes">0 MB</span>
  <span class="sep">|</span><span class="k">Origin</span><span id="origin"></span>
</div>

<script type="module">
const CFG = __CFG__;
const $ = (id) => document.getElementById(id);
const setStatus = (t, cls) => { $('status').textContent = t; $('status').className = cls || ''; };
$('origin').textContent = window.location.origin || 'null';

const LABEL = {}, GROUP = {};
CFG.groups.forEach(([g, items]) => items.forEach(([id, lab]) => { LABEL[id] = lab; GROUP[id] = g; }));
const ALL = Object.keys(LABEL);

// ---- call meter -----------------------------------------------------------
let billable = 0, free = 0, failed = 0, bytes = 0, lastFailStatus = null;
const NOT_BILLED = /\/status\.json|\/listings\//;
const origFetch = window.fetch;
window.fetch = async function (...args) {
  const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
  const isData = url.includes('aguacerowx.com');
  try {
    const res = await origFetch.apply(this, args);
    if (isData) {
      if (!res.ok) { failed++; lastFailStatus = res.status;
        $('fail').textContent = failed + ' (HTTP ' + res.status + ')'; $('fail').className = 'bad'; }
      else if (NOT_BILLED.test(url)) { free++; $('free').textContent = free; }
      else {
        billable++; $('calls').textContent = billable;
        const len = Number(res.headers.get('content-length') || 0);
        if (len) { bytes += len; $('bytes').textContent = (bytes / 1048576).toFixed(2) + ' MB'; }
      }
    }
    return res;
  } catch (e) {
    if (isData) { failed++; lastFailStatus = 'network'; $('fail').textContent = failed + ' (network)'; $('fail').className = 'bad'; }
    throw e;
  }
};
window.addEventListener('unhandledrejection', (e) => {
  const m = String((e.reason && e.reason.message) || e.reason || '');
  if (m) setStatus('error: ' + m.slice(0, 140), 'bad');
});

// ---- helpers --------------------------------------------------------------
const zulu = (s) => new Date(s * 1000).toISOString().slice(11, 16) + 'Z';
const ageMin = (s) => Math.round((Date.now() / 1000 - s) / 60);
const ageClass = (m) => m == null ? 'none' : m <= 10 ? 'fresh' : m <= 30 ? 'stale' : 'old';
function framesFor(inv, id) {
  const raw = (inv && inv[id]) || [];
  return raw.map(Number).filter(Number.isFinite).sort((a, b) => a - b);
}
const tested = {};   // id -> {result:'ok'|'fail'|'empty'|'timeout', calls, note}

try {
  const maplibregl = (await import(CFG.maplibre)).default;
  const { MapManager, WeatherLayerManager } = await import(CFG.sdk);
  setStatus('SDK loaded, building map…', 'warn');

  const mapManager = new MapManager('map', {
    mapLibrary: 'maplibre',
    maplibregl,
    styleURL: CFG.styleURL,
    belowID: CFG.belowID,
    theme: 'dark',
    mapOptions: { center: [-96, 38.5], zoom: 3.55, minZoom: 2.5, maxZoom: 11, attributionControl: false },
  });
  const map = mapManager.getMap();

  const wm = new WeatherLayerManager(map, {
    apiKey: CFG.apiKey,
    userId: CFG.userId,
    layerId: 'aguacero-weather',
    autoRefresh: false,
    autoRefreshInterval: 120,
    layerOptions: { mrmsDurationValue: '0.01' },   // latest frame only until changed
  });
  window.__wm = wm;

  let busy = false, current = CFG.default, lastState = null;

  wm.on('loading:change', ({ isLoading }) => {
    if (!busy) setStatus(isLoading ? 'loading frames…' : 'layer active', isLoading ? 'warn' : 'ok');
  });

  wm.on('state:change', (s) => {
    lastState = s;
    $('frame').textContent = s.mrmsTimestamp ? zulu(s.mrmsTimestamp) + '  (' + ageMin(s.mrmsTimestamp) + ' min old)' : '—';
    $('play').textContent = s.isPlaying ? 'Pause' : 'Play';
    drawLegend(s);
    updateEstimate();
  });

  // ---- black-out the basemap so it matches Ops Black ------------------------
  function opsBlack() {
    const st = map.getStyle(); if (!st) return;
    for (const l of st.layers) {
      try {
        if (l.type === 'background') map.setPaintProperty(l.id, 'background-color', '#000');
        else if (l.type === 'fill' && /water/.test(l.id)) map.setPaintProperty(l.id, 'fill-color', '#06080c');
        else if (l.type === 'fill') map.setLayoutProperty(l.id, 'visibility', 'none');
        else if (l.type === 'line' && /boundary_state/.test(l.id)) { map.setPaintProperty(l.id, 'line-color', '#5a5f68'); }
        else if (l.type === 'line' && /boundary_country/.test(l.id)) { map.setPaintProperty(l.id, 'line-color', '#9aa0a8'); }
        else if (l.type === 'line' && /coast/.test(l.id)) { map.setPaintProperty(l.id, 'line-color', '#6b7280'); }
      } catch (_) {}
    }
  }

  // ---- legend ---------------------------------------------------------------
  function drawLegend(s) {
    const cm = s && s.colormap;
    if (!cm || cm.length < 4) { $('legend').style.display = 'none'; return; }
    const stops = [];
    for (let i = 0; i < cm.length; i += 2) stops.push([cm[i], cm[i + 1]]);
    $('ltitle').textContent = (LABEL[s.variable] || s.variable) + (s.colormapBaseUnit ? '  (' + s.colormapBaseUnit + ')' : '');
    $('lbar').innerHTML = stops.map(([, c]) => `<div style="background:${c}"></div>`).join('');
    const n = stops.length, want = Math.min(n, 7);
    const idx = Array.from({ length: want }, (_, k) => Math.round(k * (n - 1) / Math.max(1, want - 1)));
    const fmt = (v) => Math.abs(v) < 0.01 && v !== 0 ? v.toExponential(0) : (Math.round(v * 100) / 100);
    $('lticks').innerHTML = idx.map((i) => `<span>${fmt(stops[i][0])}</span>`).join('');
    $('legend').style.display = 'block';
  }

  // ---- inventory ------------------------------------------------------------
  function inventory() { return wm.core && wm.core.mrmsStatus ? wm.core.mrmsStatus : {}; }

  function fillSelect() {
    const inv = inventory();
    const sel = $('prod'); sel.innerHTML = '';
    CFG.groups.forEach(([g, items]) => {
      const og = document.createElement('optgroup'); og.label = g;
      items.forEach(([id, lab]) => {
        const f = framesFor(inv, id);
        const o = document.createElement('option'); o.value = id;
        o.textContent = f.length ? `${lab}  ·  ${ageMin(f[f.length - 1])} min` : `${lab}  ·  no frames`;
        if (!f.length) o.style.color = '#ff4fa3';
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    sel.value = current; sel.disabled = false;
  }

  function fillInventory() {
    const inv = inventory();
    const rows = [];
    let have = 0;
    CFG.groups.forEach(([g, items]) => {
      rows.push(`<tr class="grp"><td colspan="4">${g}</td></tr>`);
      items.forEach(([id, lab]) => {
        const f = framesFor(inv, id);
        const last = f.length ? f[f.length - 1] : null;
        const age = last ? ageMin(last) : null;
        if (f.length) have++;
        const t = tested[id];
        const test = !t ? '<span class="muted">not loaded</span>'
          : t.result === 'ok' ? `<span class="ok">draws (${t.calls} call${t.calls === 1 ? '' : 's'})</span>`
          : t.result === 'empty' ? '<span class="none">no frames</span>'
          : `<span class="bad">${t.result}${t.note ? ' ' + t.note : ''}</span>`;
        rows.push(`<tr class="p${id === current ? ' sel' : ''}" data-id="${id}">
          <td>${lab}<div class="id">${id}</div></td>
          <td class="num">${f.length || '<span class="none">0</span>'}</td>
          <td class="num ${ageClass(age)}">${last ? zulu(last) + '<br>' + age + ' min' : '—'}</td>
          <td>${test}</td></tr>`);
      });
    });
    $('invrows').innerHTML =
      `<tr><td class="muted">Product</td><td class="num muted">Frames</td><td class="num muted">Latest</td><td class="muted">Load test</td></tr>` +
      rows.join('');
    $('invhead').querySelector('span').textContent = `MRMS inventory: ${have} of ${ALL.length} have frames`;
    document.querySelectorAll('#invrows tr.p').forEach((tr) =>
      tr.addEventListener('click', () => loadProduct(tr.dataset.id)));
  }

  async function refreshInventory() {
    try { await wm.core.fetchMRMSStatus(true); } catch (e) { setStatus('inventory refresh failed: ' + e.message, 'bad'); }
    fillSelect(); fillInventory(); updateEstimate();
  }

  function updateEstimate() {
    const hours = Number($('win').value);
    const f = framesFor(inventory(), current);
    if (!f.length) { $('est').textContent = 'No frames listed for this product.'; return; }
    const cutoff = f[f.length - 1] - hours * 3600;
    const n = f.filter((t) => t >= cutoff).length;
    $('est').textContent = `Loading this product costs about ${n} call${n === 1 ? '' : 's'}.`;
  }

  // ---- loading & testing ----------------------------------------------------
  function waitIdle(ms) {
    return new Promise((resolve) => {
      let seenBusy = false;
      const h = ({ isLoading }) => {
        if (isLoading) seenBusy = true;
        else if (seenBusy) { done('idle'); }
      };
      const done = (r) => { clearTimeout(t); clearTimeout(t0); wm.off && wm.off('loading:change', h); resolve(r); };
      const t = setTimeout(() => done('timeout'), ms);
      // if the frame was cached and loading never toggles, settle after 2.5 s
      const t0 = setTimeout(() => { if (!seenBusy && !wm.isLoading) done('idle'); }, 2500);
      wm.on('loading:change', h);
    });
  }

  async function loadProduct(id, { quiet = false } = {}) {
    current = id; $('prod').value = id;
    const f = framesFor(inventory(), id);
    if (!f.length) {
      tested[id] = { result: 'empty' };
      if (!quiet) setStatus(`${LABEL[id]}: no frames in Aguacero's listing`, 'bad');
      fillInventory(); updateEstimate();
      return tested[id];
    }
    const b0 = billable, f0 = failed;
    if (!quiet) setStatus(`loading ${LABEL[id]}…`, 'warn');
    try {
      const idle = waitIdle(25000);
      await wm.switchMode({ mode: 'mrms', variable: id });
      await wm.setOpacity(Number($('op').value));
      const r = await idle;
      const calls = billable - b0, fails = failed - f0;
      tested[id] = fails > 0 ? { result: 'failed', note: `HTTP ${lastFailStatus}`, calls }
                 : r === 'timeout' ? { result: 'timeout', calls }
                 : { result: 'ok', calls };
    } catch (e) {
      tested[id] = { result: 'error', note: String(e.message || e).slice(0, 60), calls: billable - b0 };
    }
    if (!quiet) {
      const t = tested[id];
      setStatus(t.result === 'ok' ? `${LABEL[id]} drawn` : `${LABEL[id]}: ${t.result} ${t.note || ''}`,
                t.result === 'ok' ? 'ok' : 'bad');
    }
    fillInventory(); updateEstimate();
    return tested[id];
  }

  async function testAll() {
    if (busy) return;
    const prevWin = $('win').value;
    if (prevWin !== '0.01') { $('win').value = '0.01'; await wm.setMRMSDurationValue('0.01'); }
    busy = true; $('testall').disabled = true;
    $('inv').classList.add('open'); $('invbtn').textContent = 'Hide inventory';
    const keep = current;
    for (let i = 0; i < ALL.length; i++) {
      setStatus(`testing ${i + 1} of ${ALL.length}: ${LABEL[ALL[i]]}`, 'warn');
      await loadProduct(ALL[i], { quiet: true });
    }
    const ok = ALL.filter((id) => tested[id] && tested[id].result === 'ok').length;
    busy = false; $('testall').disabled = false;
    await loadProduct(keep, { quiet: true });
    if (prevWin !== '0.01') { $('win').value = prevWin; await wm.setMRMSDurationValue(prevWin); }
    setStatus(`test complete: ${ok} of ${ALL.length} products draw`, ok ? 'ok' : 'bad');
  }

  // ---- wire controls --------------------------------------------------------
  $('prod').addEventListener('change', (e) => loadProduct(e.target.value));
  $('win').addEventListener('change', async (e) => { await wm.setMRMSDurationValue(e.target.value); updateEstimate(); });
  $('op').addEventListener('input', (e) => wm.setOpacity(Number(e.target.value)));
  $('back').addEventListener('click', () => { wm.pause(); wm.step(-1); });
  $('fwd').addEventListener('click', () => { wm.pause(); wm.step(1); });
  $('play').addEventListener('click', () => wm.togglePlay());
  $('auto').addEventListener('change', (e) => wm.setAutoRefresh(e.target.checked, 120));
  $('invbtn').addEventListener('click', () => {
    const open = $('inv').classList.toggle('open');
    $('invbtn').textContent = open ? 'Hide inventory' : 'Show inventory';
    if (open) fillInventory();
  });
  $('closeinv').addEventListener('click', () => { $('inv').classList.remove('open'); $('invbtn').textContent = 'Show inventory'; });
  $('reinv').addEventListener('click', refreshInventory);
  $('testall').addEventListener('click', testAll);
  $('testall').textContent = `Test every product (~${ALL.length} calls)`;

  map.on('load', async () => {
    opsBlack();
    try {
      await wm.initialize();
      fillSelect();
      await loadProduct(CFG.default);
    } catch (err) {
      setStatus('initialize failed: ' + err.message, 'bad');
      console.error(err);
    }
  });
} catch (err) {
  setStatus('SDK failed to load: ' + err.message, 'bad');
  console.error(err);
}
</script>
"""

st_html(
    HTML.replace("__CFG__", json.dumps(cfg)).replace("__CSS__", MAPLIBRE_CSS),
    height=map_height,
)
