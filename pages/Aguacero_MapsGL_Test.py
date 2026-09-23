"""
Aguacero MapsGL test page — MRMS, GOES satellite, and single-site NEXRAD.

Loads @aguacerowx/mapsgl + maplibre-gl from jsDelivr as ES modules, so there is
no build step. Runs inside st.components.v1.html.

Env var (Render dashboard):
    AGUACERO_VIZ_KEY   the Visualization key (bare hex, NOT an ag_live_ key)

NOTE ON NEXRAD: the SDK decodes radar volumes in a Web Worker backed by WASM.
Under a CDN ESM shim those assets may resolve against the jsDelivr bundle URL
and 404. If MRMS works and NEXRAD does not, that is the cause, and the fix is a
real `npm install` with a bundled component rather than anything in this file.
The HUD reports worker/WASM failures explicitly so you can tell them apart.
"""

import json
import os

import streamlit as st
from streamlit.components.v1 import html as st_html

st.set_page_config(page_title="Aguacero MapsGL Test", layout="wide")

st.markdown(
    """
    <style>
      .stApp { background:#000; }
      html, body, [class*="css"], label, .stMarkdown {
          font-family:'Roboto',sans-serif !important; font-weight:700 !important; color:#fff !important; }
      .agua-h { color:#FFD700; font-size:16pt; font-weight:700; }
    </style>
    """,
    unsafe_allow_html=True,
)

SDK = "https://cdn.jsdelivr.net/npm/@aguacerowx/mapsgl@1.0.5/+esm"
MAPLIBRE = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm"
MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css"

# JetBlue-network radars, KOKX and KDIX first.
NEXRAD_SITES = {
    "KOKX — Upton NY (JFK/LGA/EWR)": [40.866, -72.864],
    "KDIX — Mt Holly NJ (PHL)": [39.947, -74.411],
    "KBOX — Taunton MA (BOS)": [41.956, -71.137],
    "KENX — Albany NY": [42.586, -74.064],
    "KGYX — Portland ME": [43.891, -70.256],
    "KCXX — Burlington VT": [44.511, -73.166],
    "KBGM — Binghamton NY": [42.200, -75.985],
    "KCCX — State College PA": [40.923, -78.004],
    "KDOX — Dover DE": [38.826, -75.440],
    "KLWX — Sterling VA (DCA/IAD)": [38.976, -77.478],
    "KAKQ — Wakefield VA (ORF)": [36.984, -77.007],
    "KRAX — Raleigh NC (RDU)": [35.665, -78.490],
    "KCLX — Charleston SC (CHS)": [32.656, -81.042],
    "KJAX — Jacksonville FL (JAX)": [30.485, -81.702],
    "KMLB — Melbourne FL (MCO)": [28.113, -80.654],
    "KTBW — Tampa FL (TPA)": [27.706, -82.402],
    "KAMX — Miami FL (MIA/FLL)": [25.611, -80.413],
    "KBYX — Key West FL (EYW)": [24.598, -81.703],
    "KTLX — Oklahoma City (docs default)": [35.333, -97.278],
}

NEXRAD_PRODUCTS = {
    "REF — reflectivity (L2)": "REF",
    "VEL — velocity (L2)": "VEL",
    "ZDR — differential reflectivity (L2)": "ZDR",
    "RHO — correlation coefficient (L2)": "RHO",
    "SW — spectrum width (L2)": "SW",
    "EET — echo tops (L3)": "EET",
    "DVL — VIL (L3)": "DVL",
    "N0H — hydrometeor classification (L3)": "N0H",
    "KDP — specific differential phase (L3)": "KDP",
    "DAA — 1-hour precip (L3)": "DAA",
    "DTA — storm-total precip (L3)": "DTA",
}

st.markdown('<div class="agua-h">AGUACERO MAPSGL TEST</div>', unsafe_allow_html=True)

c1, c2 = st.columns([3, 2])
with c1:
    api_key = st.text_input(
        "Visualization API key", value=os.environ.get("AGUACERO_VIZ_KEY", ""), type="password"
    )
with c2:
    mode = st.radio("Mode", ["mrms", "satellite", "nexrad"], horizontal=True)

variable, sat_id, sector = "", "GOES19-EAST", "conus"
site, product, tilt, storm_rel, show_picker = "KOKX", "REF", "auto", False, True

if mode == "mrms":
    variable = st.text_input("MRMS product id", "MergedReflectivityQCComposite_00.50")

elif mode == "satellite":
    s1, s2, s3 = st.columns(3)
    with s1:
        sat_id = st.selectbox("Spacecraft", ["GOES19-EAST", "GOES18-WEST", "HIMAWARI9"])
    with s2:
        sector = st.selectbox("Sector", ["conus", "full_disk", "mesoscale_1", "mesoscale_2"])
    with s3:
        variable = st.text_input("Channel / product", "C07")

else:
    n1, n2 = st.columns(2)
    with n1:
        site_label = st.selectbox("Radar site", list(NEXRAD_SITES.keys()))
        site = site_label.split(" —")[0]
    with n2:
        product = NEXRAD_PRODUCTS[st.selectbox("Product", list(NEXRAD_PRODUCTS.keys()))]
    n3, n4, n5 = st.columns(3)
    with n3:
        tilt = st.selectbox("Tilt (deg)", ["auto", "0.5", "0.9", "1.3", "1.8", "2.4", "3.1"])
    with n4:
        storm_rel = st.checkbox("Storm-relative velocity", value=False,
                                help="Only meaningful when product is VEL.")
    with n5:
        show_picker = st.checkbox("Show site picker overlay", value=True)

c3, c4, c5 = st.columns(3)
with c3:
    opacity = st.slider("Layer opacity", 0.0, 1.0, 0.85, 0.05)
with c4:
    duration = st.slider("Timeline window (hours)", 1, 12, 1)
with c5:
    auto_refresh = st.checkbox("Auto-refresh (60 s)", value=False)

style_url = st.text_input("MapLibre style JSON", "https://tiles.openfreemap.org/styles/liberty")
below_id = st.text_input("Anchor layer id (belowID)", "label_other")

if not api_key:
    st.warning("Enter the Visualization key to load the map.")
    st.stop()

center = NEXRAD_SITES[site_label] if mode == "nexrad" else [39.5, -96.0]
zoom = 7.0 if mode == "nexrad" else 3.6

cfg = {
    "apiKey": api_key,
    "userId": "bluemet-test",
    "mode": mode,
    "variable": variable,
    "satelliteId": sat_id,
    "satelliteSector": sector,
    "nexradSite": site,
    "nexradProduct": product,
    "nexradTilt": None if tilt == "auto" else float(tilt),
    "nexradStormRelative": bool(storm_rel),
    "nexradShowSitesPicker": bool(show_picker),
    "opacity": opacity,
    "duration": duration,
    "autoRefresh": auto_refresh,
    "styleURL": style_url,
    "belowID": below_id,
    "center": [center[1], center[0]],
    "zoom": zoom,
    "sdk": SDK,
    "maplibre": MAPLIBRE,
}

HTML = """
<link href="__CSS__" rel="stylesheet" />
<style>
  html,body { margin:0; padding:0; background:#000; }
  #map { position:absolute; inset:0 0 104px 0; }
  #hud { position:absolute; left:0; right:0; bottom:0; height:104px;
         background:#0a0a0a; color:#fff; font:700 12px/1.55 Roboto,Arial,sans-serif;
         padding:8px 12px; box-sizing:border-box; border-top:1px solid #222; overflow:auto; }
  .k { color:#7c8cff; }
  .ok { color:#3ddc84; } .bad { color:#ff5c8a; } .warn { color:#FFD700; }
</style>
<div id="map"></div>
<div id="hud">
  <div><span class="k">status</span> <span id="status" class="warn">loading SDK…</span>
       &nbsp;|&nbsp; <span class="k">origin sent</span> <span id="origin"></span></div>
  <div><span class="k">billable</span> <span id="calls" class="ok">0</span>
       &nbsp;|&nbsp; <span class="k">not billed</span> <span id="free">0</span>
       &nbsp;|&nbsp; <span class="k">failed</span> <span id="fail" class="bad">0</span>
       &nbsp;|&nbsp; <span class="k">bytes</span> <span id="bytes">0</span></div>
  <div><span class="k">frame</span> <span id="frame">—</span>
       &nbsp;|&nbsp; <span class="k">tilts</span> <span id="tilts">—</span>
       &nbsp;|&nbsp; <span class="k">sweeps</span> <span id="sweeps">—</span></div>
  <div><span class="k">worker/wasm</span> <span id="worker" class="ok">no errors</span></div>
</div>
<script type="module">
const CFG = __CFG__;
const $ = (id) => document.getElementById(id);
$('origin').textContent = window.location.origin || 'null (sandboxed iframe)';

// Surface worker / WASM failures, the expected NEXRAD failure mode on a CDN shim.
window.addEventListener('error', (e) => {
  const m = String(e.message || '');
  if (/worker|wasm|importScripts|Failed to fetch/i.test(m)) {
    $('worker').textContent = m.slice(0, 160);
    $('worker').className = 'bad';
  }
}, true);
window.addEventListener('unhandledrejection', (e) => {
  const m = String((e.reason && e.reason.message) || e.reason || '');
  if (/worker|wasm|importScripts|Failed to fetch/i.test(m)) {
    $('worker').textContent = m.slice(0, 160);
    $('worker').className = 'bad';
  }
});

let billable = 0, free = 0, failed = 0, bytes = 0;
const NOT_BILLED = /\\/status\\.json|\\/listings\\//;
const origFetch = window.fetch;
window.fetch = async function (...args) {
  const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
  const isData = url.includes('data.aguacerowx.com');
  try {
    const res = await origFetch.apply(this, args);
    if (isData) {
      if (!res.ok) { failed++; $('fail').textContent = failed + ' (last ' + res.status + ')'; }
      else if (NOT_BILLED.test(url)) { free++; $('free').textContent = free; }
      else {
        billable++; $('calls').textContent = billable;
        const len = Number(res.headers.get('content-length') || 0);
        if (len) { bytes += len; $('bytes').textContent = (bytes/1048576).toFixed(2) + ' MB'; }
      }
    }
    return res;
  } catch (e) {
    if (isData) { failed++; $('fail').textContent = failed; }
    throw e;
  }
};

try {
  const maplibregl = (await import(CFG.maplibre)).default;
  const { MapManager, WeatherLayerManager } = await import(CFG.sdk);
  $('status').textContent = 'SDK loaded, building map…';

  const mapManager = new MapManager('map', {
    mapLibrary: 'maplibre',
    maplibregl,
    styleURL: CFG.styleURL,
    belowID: CFG.belowID,
    theme: 'dark',
    mapOptions: { center: CFG.center, zoom: CFG.zoom },
  });

  const map = mapManager.getMap();
  const weatherManager = new WeatherLayerManager(map, {
    apiKey: CFG.apiKey,
    userId: CFG.userId,
    layerId: 'aguacero-weather',
    autoRefresh: CFG.autoRefresh,
    autoRefreshInterval: 60,
    layerOptions: { nexradShowSitesPicker: CFG.nexradShowSitesPicker },
  });

  weatherManager.on('loading:change', ({ isLoading, source, reason }) => {
    $('status').textContent = isLoading ? `receiving ${source}… (${reason||''})` : 'idle';
    $('status').className = isLoading ? 'warn' : 'ok';
  });

  weatherManager.on('state:change', (s) => {
    const ts = s.mrmsTimestamp || s.satelliteTimestamp || s.nexradTimestamp;
    $('frame').textContent = ts
      ? new Date(ts * 1000).toISOString().replace('T',' ').slice(0,19) + 'Z'
      : (s.dataType || '—');
    const tilts = s.availableNexradTilts;
    $('tilts').textContent = (tilts && tilts.length) ? tilts.join(', ') : '—';
    const times = s.availableNexradTimestamps;
    $('sweeps').textContent = (times && times.length) ? times.length : '—';
  });

  map.on('load', async () => {
    try {
      await weatherManager.initialize();
      if (CFG.mode === 'mrms') {
        await weatherManager.switchMode({ mode: 'mrms', variable: CFG.variable });
        await weatherManager.setMRMSDurationValue(String(CFG.duration));
      } else if (CFG.mode === 'satellite') {
        await weatherManager.switchMode({
          mode: 'satellite',
          satelliteId: CFG.satelliteId,
          satelliteSector: CFG.satelliteSector,
          satelliteProduct: CFG.variable,
        });
        await weatherManager.setSatelliteDurationValue(CFG.duration);
      } else {
        const args = {
          mode: 'nexrad',
          nexradSite: CFG.nexradSite,
          nexradProduct: CFG.nexradProduct,
        };
        if (CFG.nexradTilt !== null) args.nexradTilt = CFG.nexradTilt;
        await weatherManager.switchMode(args);
        if (CFG.nexradStormRelative) weatherManager.setNexradStormRelative(true);
        await weatherManager.setNexradDurationValue(CFG.duration);
      }
      weatherManager.setOpacity(CFG.opacity);
      $('status').textContent = 'layer active';
      $('status').className = 'ok';
    } catch (err) {
      $('status').textContent = 'switchMode failed: ' + err.message;
      $('status').className = 'bad';
      console.error(err);
    }
  });

  window.__wm = weatherManager;   // console access for poking at state
} catch (err) {
  $('status').textContent = 'init failed: ' + err.message;
  $('status').className = 'bad';
  console.error(err);
}
</script>
"""

st_html(HTML.replace("__CFG__", json.dumps(cfg)).replace("__CSS__", MAPLIBRE_CSS), height=740)

st.caption(
    "Site picker overlay is built into the SDK (nexradSitesDefault.json) — clicking a site "
    "on the map calls setNexradSite directly. The dropdown just sets the starting site."
)
