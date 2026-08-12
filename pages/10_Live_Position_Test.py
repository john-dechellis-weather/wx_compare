"""Live Position Test - disposable sandbox page.

Purpose: prove (or disprove) FlightRadar-style high-frequency JBU
position updates in the browser, in isolation from the real pages.
One static Level III radar frame + a canvas overlay; the BROWSER
polls community ADS-B APIs directly every few seconds and redraws
triangles. A diagnostic panel reports every poll: which source
answered, HTTP status, aircraft count, and any JavaScript error -
so a failure here is a lesson, not a mystery.

Delete this page when the experiment concludes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1

st.set_page_config(
    page_title="BlueMet - Live Position Test",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()

# Same-origin proxy for browser polling (aggregators block CORS -
# proven by this page's own diagnostics on 8/9)
try:
    from core.live_api import ensure_live_api
    _PROXY_OK = ensure_live_api()
except Exception:
    _PROXY_OK = False

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() \
    else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

st.title("Live Position Test")
st.caption(
    "Experimental: one radar frame, browser-side JBU triangles at "
    "high frequency, and a diagnostic readout of every poll."
)


@st.cache_data(ttl=600, show_spinner=False, max_entries=4)
def cached_station_coords(icao: str):
    from core.stations import StationResolver
    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    stn = resolver.resolve(icao)
    if stn is None:
        return None
    return float(stn.lat), float(stn.lon)


@st.cache_data(ttl=600, show_spinner=False, max_entries=4)
def cached_base_frame(icao: str, lat: float, lon: float,
                      zoom: float, bucket: str):
    """One aircraft-free L3 REF frame with pixel geometry."""
    from core.nexrad_sites import nearest_site
    from core.radar3 import fetch_recent, parse_l3, render_l3
    site, _ = nearest_site(lat, lon)
    files = fetch_recent("REF", site, n=1)
    if not files:
        raise RuntimeError(f"no L3 frames from {site}")
    raw, name = files[-1]
    parsed = parse_l3(raw)
    png, geom = render_l3(
        parsed, "REF", lat, lon, zoom, site,
        title_note=name, return_geometry=True,
    )
    return png, geom, site, name


def live_test_html(png_bytes: bytes, geom: dict, lat: float,
                   lon: float, radius_nm: int,
                   interval_ms: int) -> str:
    import base64
    import json as _json

    b64 = base64.b64encode(png_bytes).decode()
    g = _json.dumps(geom)
    return f"""
<style>
.lt {{font: 12px monospace; color: #0f0;}}
.lt .wrap {{position: relative; border: 1px solid #888;}}
.lt img {{width: 100%; display: block;}}
.lt canvas {{position: absolute; left: 0; top: 0;
             pointer-events: none;}}
.lt .diag {{background: #000; border: 1px solid #0f0;
            padding: 6px 8px; margin-top: 6px;
            white-space: pre-wrap; min-height: 120px;}}
</style>
<div class="lt">
  <div class="wrap">
    <img id="bg" src="data:image/png;base64,{b64}">
    <canvas id="cv"></canvas>
  </div>
  <div class="diag" id="diag">starting...</div>
</div>
<script>
(function() {{
  const G = {g};
  const LAT = {lat}, LON = {lon}, RAD = {radius_nm};
  const INTERVAL = {interval_ms};
  const SOURCES = [
    ["proxy", "/jbu_pos"],
    ["adsb.lol", "https://api.adsb.lol/v2/point/"],
    ["adsb.fi", "https://opendata.adsb.fi/api/v2/lat/"],
    ["airplanes.live", "https://api.airplanes.live/v2/point/"]
  ];
  const img = document.getElementById("bg");
  const cv = document.getElementById("cv");
  const diag = document.getElementById("diag");
  let log = [];
  let pollN = 0;

  function say(line) {{
    log.unshift(line);
    log = log.slice(0, 8);
    diag.textContent = log.join("\\n");
  }}

  window.addEventListener("error", function(e) {{
    say("JS ERROR: " + e.message + " @" + e.lineno);
  }});

  function draw(planes, tag) {{
    const w = img.clientWidth;
    if (!w) return;
    const k = w / 1200.0;
    cv.width = w;
    cv.height = img.clientHeight;
    const c = cv.getContext("2d");
    c.clearRect(0, 0, cv.width, cv.height);
    let shown = 0;
    for (const p of planes) {{
      const la = p[0], lo = p[1];
      const hd = (p[2] || 0) * Math.PI / 180;
      if (la < G.lat0 || la > G.lat1 ||
          lo < G.lon0 || lo > G.lon1) continue;
      const fx = (lo - G.lon0) / (G.lon1 - G.lon0);
      const fy = (G.lat1 - la) / (G.lat1 - G.lat0);
      const x = (G.x0 + fx * (G.x1 - G.x0)) * k;
      const y = (G.y_top + fy * (G.y_bot - G.y_top)) * k;
      const S = 10;
      c.beginPath();
      c.moveTo(x + S * Math.sin(hd), y - S * Math.cos(hd));
      c.lineTo(x + S * 0.7 * Math.sin(hd + 2.5),
               y - S * 0.7 * Math.cos(hd + 2.5));
      c.lineTo(x + S * 0.35 * Math.sin(hd + Math.PI),
               y - S * 0.35 * Math.cos(hd + Math.PI));
      c.lineTo(x + S * 0.7 * Math.sin(hd - 2.5),
               y - S * 0.7 * Math.cos(hd - 2.5));
      c.closePath();
      c.fillStyle = "#00BFFF";
      c.fill();
      c.strokeStyle = "#FFF";
      c.stroke();
      c.font = "11px monospace";
      c.fillStyle = "#00BFFF";
      c.fillText(p[3], x + 9, y + 12);
      shown++;
    }}
    return shown;
  }}

  function parseAc(j) {{
    return (j.ac || []).filter(function(a) {{
      return ((a.flight || "").trim().toUpperCase()
              .indexOf("JBU") === 0);
    }}).map(function(a) {{
      return [a.lat, a.lon, a.track || 0,
              (a.flight || "").trim()];
    }});
  }}

  async function trySource(idx) {{
    const name = SOURCES[idx][0];
    let url;
    if (name === "proxy") {{
      url = SOURCES[idx][1] + "?lat=" + LAT + "&lon=" + LON +
            "&r=" + (RAD / 60.0).toFixed(2);
    }} else if (name === "adsb.fi") {{
      url = SOURCES[idx][1] + LAT + "/lon/" + LON +
            "/dist/" + RAD;
    }} else {{
      url = SOURCES[idx][1] + LAT + "/" + LON + "/" + RAD;
    }}
    const t0 = performance.now();
    const r = await fetch(url);
    const ms = Math.round(performance.now() - t0);
    if (!r.ok) throw new Error(name + " HTTP " + r.status);
    const j = await r.json();
    if (name === "proxy") {{
      if (!j.ok) throw new Error("proxy err " + (j.err || "?"));
      return {{name: name, planes: j.planes || [], ms: ms,
               total: (j.planes || []).length}};
    }}
    const planes = parseAc(j);
    return {{name: name, planes: planes, ms: ms,
             total: (j.ac || []).length}};
  }}

  async function poll() {{
    pollN++;
    const ts = new Date().toISOString().substr(11, 8) + "Z";
    for (let i = 0; i < SOURCES.length; i++) {{
      try {{
        const res = await trySource(i);
        const shown = draw(res.planes, res.name);
        say("#" + pollN + " " + ts + " " + res.name + " OK " +
            res.ms + "ms | " + res.total + " ac total, " +
            res.planes.length + " JBU, " + shown + " in frame");
        return;
      }} catch (e) {{
        say("#" + pollN + " " + ts + " " + SOURCES[i][0] +
            " FAIL: " + e.message);
      }}
    }}
    say("#" + pollN + " " + ts + " all sources failed");
  }}

  poll();
  setInterval(poll, INTERVAL);
}})();
</script>
"""


def _embed(html: str, height: int) -> None:
    fn = getattr(st, "iframe", None)
    if fn is not None:
        try:
            fn(html, height=height)
            return
        except TypeError:
            pass
    st.components.v1.html(html, height=height)


with st.sidebar:
    st.header("Test setup")
    icao = st.text_input("Airport ICAO", value="KJFK",
                         max_chars=4).strip().upper()
    zoom = st.slider("Zoom (degrees)", 0.5, 3.0, 1.5, 0.5)
    interval = st.selectbox(
        "Poll interval", [2, 5, 10], index=0,
        format_func=lambda s: f"{s} seconds",
    )
    go = st.button("Run test", type="primary",
                   use_container_width=True)

if go:
    st.session_state["lt_icao"] = icao

if st.session_state.get("lt_icao"):
    icao = st.session_state["lt_icao"]
    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve {icao}.")
        st.stop()
    lat, lon = coords
    bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")[:11]
    with st.spinner("Rendering base radar frame..."):
        try:
            png, geom, site, name = cached_base_frame(
                icao, lat, lon, zoom, bucket
            )
        except Exception as e:
            st.error(f"Base frame failed: {e}")
            st.stop()
    st.caption(
        ("Proxy endpoint registered - expect 'proxy OK' lines. "
         if _PROXY_OK else
         "PROXY REGISTRATION FAILED - externals will CORS-fail; "
         "tell Claude. ")
        + f"Base: L3 REF from {site} ({name}), static. Triangles: "
        f"browser-drawn, polling every {interval}s. Watch the "
        f"diagnostic box - it reports each poll's source, latency, "
        f"and any error verbatim."
    )
    _embed(
        live_test_html(png, geom, lat, lon,
                       radius_nm=int(zoom * 60),
                       interval_ms=interval * 1000),
        height=920,
    )
else:
    st.info("Pick an airport and click **Run test**.")
