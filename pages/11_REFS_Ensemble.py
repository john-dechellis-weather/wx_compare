"""REFS Ensemble - six pods, three across (23 Sep).

REFS Ensemble - dedicated page for the RRFS Ensemble
Forecast System (HREF's successor).

Standalone by design: shares core fetch/render machinery with the
CAMs page but owns its own layout, so ensemble-specific features
(probability products, member spreads) can grow here without
touching the deterministic grid.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st


st.set_page_config(
    page_title="BlueMet - REFS Ensemble",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Same order as every other page: config, theme, auth. Page 11 was
# never themed, so arriving here from a themed page switched styling
# and dropped the sidebar nav captions.
from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = (_persistent if _persistent.exists()
              else Path("/tmp/wx_compare_cache"))
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

# Announce activity so the warmer backs off while this page renders.
from core.cam_warm import note_request as _note_req

_note_req()

from core.cam_warm import (
    HUBS as REFS_HUBS, HUB_LABELS as _HUB_LABELS,
    ensure_warmer_started,
    warm_cycle, warm_get, warm_hours,
)
from core.hrrr_cam import MODELS
from core.cam_warm import hub_geom as _hub_geom

ensure_warmer_started(CACHE_ROOT)

@st.cache_data(ttl=3600, show_spinner=False, max_entries=64)
def cached_station_coords(icao: str):
    from core.stations import StationResolver
    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    try:
        stn = resolver.resolve(icao)
        if stn is not None:
            return float(stn.lat), float(stn.lon)
    except Exception:
        pass
    return None


@st.cache_data(ttl=600, show_spinner=False, max_entries=24)
def cached_refs_cycle(model: str, fhr: int, bucket: str):
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(model, fhr)
    return cyc.isoformat() if cyc else None


# ---------------------------------------------------------------------------
# Six pods (23 Sep): three across, Northeast / Mid-Atlantic on top and
# Florida below, both shown at once, one run and one valid hour for all.
#
# Each pod is cropped tight to its region. The warm store still holds
# the same square hub frames (nothing in core/ or the warmer changed);
# the page crops them to CROP[region] before display. The frames are
# PlateCarree with the same pixels-per-degree on both axes, so the crop
# is exact and the station positions are a linear lookup.
#
# Station values: each JetBlue station the basemap draws gets a white
# box with the highest colour band found inside its 10 nm ring,
# written as the band floor ("40+ dBZ"). It is read from the frame's
# own pixels against the renderer's palette, so it works for warmed
# frames with no extra download, and it can never disagree with the
# colours on the map. Stations with nothing in the ring get no box.
# Map furniture is masked out first using the renderer's own cached
# basemap, so a blue station dot or white label never reads as echo.
# ---------------------------------------------------------------------------
PRODUCTS = {
    "PMMN composite reflectivity": ("refs_pmmn", "REFC"),
    "Probability REFC \u2265 40 dBZ": ("refs_prob", "PROB_REFC40"),
    "Probability REFC \u2265 50 dBZ": ("refs_prob", "PROB_REFC50"),
    "Probability ceiling < 1000 ft": ("refs_prob", "PROB_CIG1000"),
    "Probability ceiling < 500 ft": ("refs_prob", "PROB_CIG500"),
    "Probability visibility < 1 sm": ("refs_prob", "PROB_VIS1"),
    "Probability visibility < 3 sm": ("refs_prob", "PROB_VIS3"),
    "Probability echo tops > FL350": ("refs_prob", "PROB_RETOP35"),
}
# Same three products on both rows, left to right.
_POD_DEFAULTS = ["PMMN composite reflectivity",
                 "Probability REFC \u2265 40 dBZ",
                 "Probability ceiling < 1000 ft"]
_ROWS = [("NE", "Northeast / Mid-Atlantic"), ("FL", "Florida")]

# Display box per region: (west, south, east, north). Must sit inside
# the hub's square frame (core.cam_warm.HUBS: centre +- half width).
CROP = {
    "NE": (-80.3, 37.6, -69.6, 44.9),
    "FL": (-84.6, 24.35, -78.9, 30.9),
}
_RING_NM = 10.0          # matches the basemap's station ring
_COLOR_TOL = 22.0        # RGB distance for "this pixel is palette colour k"
_MIN_PX = 6              # pixels a band needs inside the ring to count
_POD_GUESS_PX = 540      # first-paint width before the viewer measures itself

# Run choice -> the forecast hour a run must have reached to count.
# REFS runs 00Z and 12Z to f60, 06Z and 18Z to f48.
_RUNS = {"Latest run": 1,
         "Latest 60-hr run (00Z/12Z)": 60,
         "Latest 48-hr run": 48}

_INK, _INK2, _GOLD = "#FFFFFF", "#B8B8B8", "#FFD700"
st.markdown(
    "<style>"
    ".refs-head{font:700 18px Roboto,sans-serif;color:%s;margin:0 0 2px 0}"
    ".refs-band{font:700 20px Roboto,sans-serif;color:#fff;margin:6px 0 4px 0}"
    ".refs-sep{height:6px;background:#9aa0a8;margin:14px 0 6px 0;border-radius:1px}"
    "div[data-testid='stColumn'] div[data-testid='stSelectbox']{margin-bottom:-10px}"
    "</style>" % _GOLD, unsafe_allow_html=True)

now = datetime.now(timezone.utc)
bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)


@st.cache_data(ttl=600, show_spinner=False, max_entries=24)
def _cycle_for(need_fhr: int, bucket: str):
    """The newest REFS cycle that has reached need_fhr, from the
    warm store when it has one (no probing), else by probing."""
    if need_fhr <= 1:
        _wc = warm_cycle(CACHE_ROOT, "refs_prob@PROB_REFC40")
        if _wc:
            return _wc
    return cached_refs_cycle("refs_prob", need_fhr, bucket)


# ---- run toggle + the one forecast-hour slider --------------------------
_h1, _h2 = st.columns([1.5, 2.5])
with _h1:
    run_choice = st.radio("Run", list(_RUNS), horizontal=True,
                          key="refs_run", label_visibility="collapsed")
need_fhr = _RUNS[run_choice]

cycle_iso = _cycle_for(need_fhr, bucket10)
if cycle_iso is None:
    st.warning("No REFS cycle found for that run length yet.")
    st.stop()
cyc = datetime.fromisoformat(cycle_iso)
max_fhr = 60 if cyc.hour in (0, 12) else 48
with _h2:
    fhr = st.slider("Forecast hour", 1, max_fhr,
                    max(1, min(int(st.session_state.get("refs_fhr", 1) or 1),
                               max_fhr)),
                    key="refs_fhr", label_visibility="collapsed")
valid = cyc + timedelta(hours=fhr)

st.markdown(
    f'<div class="refs-head">REFS Ensemble &nbsp; F{fhr:02d} &nbsp; '
    f'valid {valid:%HZ %a %d %b} &nbsp; run {cyc:%HZ}</div>',
    unsafe_allow_html=True)


@st.cache_data(ttl=10800, show_spinner=False, max_entries=800)
def cached_refs_frame(model: str, field: str, cycle_iso: str, h: int,
                      la: float, lo: float, zm: float):
    from core.hrrr_cam import fetch_and_decode, render_frame
    _c = datetime.fromisoformat(cycle_iso)
    vals, lats, lons = fetch_and_decode(model, field, _c, h, la, lo, zm)
    return render_frame(field, vals, lats, lons, la, lo, zm, "",
                        grid_key=f"{la:.2f},{lo:.2f},{zm:.2f}|{model}",
                        cache_root=CACHE_ROOT / "cam_warm")


def _frame(region: str, model: str, field: str, h: int):
    """(image bytes, 'warm'|'live') for one region, product and hour."""
    _wk = f"{model}@{field}"
    got = warm_get(CACHE_ROOT, _wk, region, h)
    if got and got[1] == cycle_iso:
        return got[0], "warm"
    clat, clon, zm = _hub_geom(region)
    return cached_refs_frame(model, field, cycle_iso, h,
                             round(clat, 2), round(clon, 2), zm), "live"


def _palette(field: str):
    """(bounds, [(r,g,b) per band 1..n], unit) from the renderer."""
    from core.cam_fast import PALETTES, _lut_for
    key = field if field in PALETTES else "PROB"
    bounds = PALETTES[key]["bounds"]
    lut = _lut_for(key)
    cols = [tuple(int(v) for v in lut[k][:3])
            for k in range(1, min(len(lut) - 1, len(bounds)) + 1)]
    return bounds, cols, ("dBZ" if field in ("REFC", "REFD") else "%")


@st.cache_data(ttl=3600, show_spinner=False, max_entries=240)
def _pod_view(region: str, model: str, field: str, cyc_iso: str, h: int):
    """Cropped frame + station values for one pod.

    Returns (webp bytes, width, height, labels, source) where labels
    is a list of (x_frac, y_frac, text).
    """
    import io
    import math

    import numpy as np
    from PIL import Image

    from core.hrrr_cam import JBU_STATIONS, STATION_DOT_PT

    raw, source = _frame(region, model, field, h)
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    clat, clon, half = _hub_geom(region)
    fw, fs, fe, fn = clon - half, clat - half, clon + half, clat + half
    ppd = im.width / (fe - fw)
    w, s, e, n = CROP[region]
    box = (int(round((w - fw) * ppd)), int(round((fn - n) * ppd)),
           int(round((e - fw) * ppd)), int(round((fn - s) * ppd)))
    crop = im.crop(box)
    arr = np.asarray(crop).astype("float32")
    H, W = arr.shape[:2]

    # Map furniture (station dot, ring, identifier, coast and state
    # lines, gridline labels) is drawn OVER the field and its blues and
    # whites sit close to palette colours. Mask it out using the same
    # cached basemap the renderer composited, grown by 2 px for the
    # WebP edge bleed, so only field pixels are read.
    from scipy import ndimage as _ndi
    from core.cam_fast import basemap as _basemap
    furn = np.zeros((H, W), dtype=bool)
    try:
        bm = _basemap(region, (fw, fs, fe, fn), im.width, im.height,
                      cache_dir=str(CACHE_ROOT / "cam_warm"))
        a = np.asarray(bm.crop(box))[:, :, 3] > 8
        furn = _ndi.binary_dilation(a, iterations=2)
    except Exception:
        pass

    bounds, cols, unit = _palette(field)
    pal = np.asarray(cols, dtype="float32")              # (k, 3)
    # metpy's reflectivity table ends in black, which is the ground
    # colour; a palette entry that close to the ground can never be
    # told apart from "no echo", so it is taken out of the match.
    from core.cam_fast import GROUND as _GROUND
    near_ground = np.sqrt(((pal - np.asarray(_GROUND[:3], "float32")) ** 2
                           ).sum(-1)) < 45.0
    pal[near_ground] = 1e4
    skip = {"KLGA", "KEWR"}
    dot_px = STATION_DOT_PT / 72.0 * 100.0 * (ppd / 150.0)  # basemap dot, frame px
    labels = []
    for icao, (sla, slo) in JBU_STATIONS.items():
        if icao in skip or not (w + 0.15 <= slo <= e - 0.15
                                and s + 0.15 <= sla <= n - 0.15):
            continue
        cx, cy = (slo - w) * ppd, (n - sla) * ppd
        ry = _RING_NM / 60.0 * ppd
        rx = ry / max(0.2, math.cos(math.radians(sla)))
        x0, x1 = max(0, int(cx - rx)), min(W, int(cx + rx) + 1)
        y0, y1 = max(0, int(cy - ry)), min(H, int(cy + ry) + 1)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        inside = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
        inside &= ~furn[y0:y1, x0:x1]
        px = arr[y0:y1, x0:x1][inside]                   # (m, 3)
        best = 0
        if px.size:
            d = np.sqrt(((px[:, None, :] - pal[None, :, :]) ** 2).sum(-1))
            k = d.argmin(1)
            hit = d[np.arange(len(k)), k] <= _COLOR_TOL
            if hit.any():
                # Highest band held by at least _MIN_PX pixels, so a
                # few blended edge pixels cannot promote the value.
                counts = np.bincount(k[hit], minlength=len(cols))
                ok = np.nonzero(counts >= _MIN_PX)[0]
                if ok.size:
                    best = int(ok.max()) + 1             # band 1..n
        if best:
            floor = bounds[best - 1]
            txt = f"{floor:g}+ {unit}" if unit == "dBZ" else f"{floor:g}+%"
            # just under the basemap dot, so the identifier above it
            # and the value below read as one mark
            labels.append((cx / W, (cy + dot_px / 2 + 3) / H, txt))

    buf = io.BytesIO()
    crop.save(buf, "WEBP", quality=88, method=4)
    return buf.getvalue(), W, H, labels, source


def _legend_html(field: str) -> str:
    bounds, cols, unit = _palette(field)
    cols = [c for c in cols if sum(c) > 60]         # drop the black tail
    cells = "".join(
        f'<div class="c"><i style="background:rgb{c}"></i>'
        f'<b>{bounds[i]:g}</b></div>' if i % 2 == 0 else
        f'<div class="c"><i style="background:rgb{c}"></i><b></b></div>'
        for i, c in enumerate(cols))
    return f'<div class="leg">{cells}<span class="u">{unit}</span></div>'


def _viewer(img: bytes, w: int, h: int, labels, field: str, key: str):
    """The pod: cropped map exactly the column's width, its own
    height from the map's aspect. Wheel zooms about the cursor, drag
    pans, double-click resets; value boxes stay a constant size."""
    import base64 as _b64
    import json as _json

    import streamlit.components.v1 as _components

    uri = "data:image/webp;base64," + _b64.b64encode(img).decode("ascii")
    boxes = "".join(
        f'<div class="v" style="left:{x * 100:.3f}%;top:{y * 100:.3f}%">{t}</div>'
        for x, y, t in labels)
    guess_h = int(_POD_GUESS_PX * h / w) + 2
    _components.html(f"""
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@700&display=swap" rel="stylesheet">
<style>
 html,body{{margin:0;padding:0;background:#000;overflow:hidden}}
 #w{{position:relative;width:100%;aspect-ratio:{w}/{h};overflow:hidden;
     background:#0b0c0e;cursor:grab;outline:1px solid #2a2d33;outline-offset:-1px}}
 #z{{position:absolute;inset:0;transform-origin:0 0;--inv:1}}
 #z img{{width:100%;height:100%;display:block;user-select:none}}
 .v{{position:absolute;transform:translate(-50%,0) scale(var(--inv));
     transform-origin:50% 0;background:#fff;color:#000;white-space:nowrap;
     font:700 12pt/1 Roboto,Arial,sans-serif;padding:2px 4px}}
 .leg{{position:absolute;left:6px;bottom:6px;display:flex;align-items:flex-end;
      background:rgba(0,0,0,.78);padding:4px 6px 3px;gap:0}}
 .leg .c{{display:flex;flex-direction:column;align-items:flex-start;width:22px}}
 .leg i{{display:block;width:22px;height:7px}}
 .leg b,.leg .u{{font:700 12pt/1.2 Roboto,Arial,sans-serif;color:#fff;margin-top:2px}}
 .leg .u{{margin-left:6px}}
</style>
<div id="w"><div id="z"><img src="{uri}" draggable="false" alt="">{boxes}</div>{_legend_html(field)}</div>
<script>
(function(){{
  const w=document.getElementById('w'), z=document.getElementById('z');
  let s=1, tx=0, ty=0, drag=null;
  function apply(){{ z.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`;
                    z.style.setProperty('--inv', 1/s); }}
  w.addEventListener('wheel', e=>{{
    e.preventDefault();
    const r=w.getBoundingClientRect(), x=e.clientX-r.left, y=e.clientY-r.top;
    const k=Math.exp(-e.deltaY*0.0015), ns=Math.min(12, Math.max(1, s*k));
    tx = x - (x - tx) * (ns / s); ty = y - (y - ty) * (ns / s); s = ns;
    if (s===1) {{ tx=0; ty=0; }}
    apply();
  }}, {{passive:false}});
  w.addEventListener('mousedown', e=>{{ drag={{x:e.clientX-tx, y:e.clientY-ty}}; w.style.cursor='grabbing'; }});
  window.addEventListener('mousemove', e=>{{ if(!drag) return; tx=e.clientX-drag.x; ty=e.clientY-drag.y; apply(); }});
  window.addEventListener('mouseup', ()=>{{ drag=null; w.style.cursor='grab'; }});
  w.addEventListener('dblclick', ()=>{{ s=1; tx=0; ty=0; apply(); }});
  // Size the iframe to the map: the pod is exactly the map, whatever
  // the column width turns out to be.
  function fit(){{
    try {{
      const fe = window.frameElement; if (!fe) return;
      const hh = Math.ceil(w.getBoundingClientRect().height);
      if (hh > 0) fe.style.height = hh + 'px';
    }} catch (_) {{}}
  }}
  new ResizeObserver(fit).observe(w); window.addEventListener('load', fit); fit();
}})();
</script>""", height=guess_h)


def _pod(region: str, col: int):
    i = 0 if region == "NE" else 3
    key = f"refs_pod{i + col}"
    label = st.selectbox(
        "Product", list(PRODUCTS),
        index=list(PRODUCTS).index(
            st.session_state.get(key, _POD_DEFAULTS[col])),
        key=key, label_visibility="collapsed")
    model, field = PRODUCTS[label]
    try:
        img, w, h, labels, src = _pod_view(region, model, field,
                                           cycle_iso, fhr)
        _viewer(img, w, h, labels, field, key=f"{key}_{fhr}_{field}")
        if src == "live":
            st.caption("rendered on demand (not yet warmed)")
    except Exception as exc:
        st.warning(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")


for r, (region, banner) in enumerate(_ROWS):
    if r:
        st.markdown('<div class="refs-sep"></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="refs-band">{banner}</div>',
                unsafe_allow_html=True)
    cols = st.columns(3, gap="small")
    for c in range(3):
        with cols[c]:
            _pod(region, c)

st.caption(
    "RRFS Ensemble Forecast System (HREF's successor, pre-implementation "
    "feed). One run and one valid hour for all six maps. White boxes: "
    "the highest colour band inside each station's 10 nm ring. Red "
    "outline: N90. Wheel to zoom, drag to pan, double-click to reset.")
