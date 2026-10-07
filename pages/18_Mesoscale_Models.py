"""Mesoscale Models (6 Oct) - Hi-Res CAMs and REFS Ensemble in one page.

Opens with a pop-up: a sector (nine views that tile CONUS), how many
plots (1, 2 or 4), and for each plot a model (HRRR, RRFS, REFS) and
one of that model's products. Continue turns green once every plot
has a product. The maps share one run choice and one forecast-hour
slider. Frames come from the warm store for the warmed regions and
render on demand elsewhere (core/cam_fast draws the basemap: coasts,
states, every JetBlue station with 5 and 20 mile white rings, other
commercial airports in green).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="BlueMet - Mesoscale Models", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

try:
    from core.cam_warm import note_request as _note_req
    _note_req()
except Exception:
    pass

from auth import check_password

check_password()

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

from core.cam_warm import (
    SECTOR_LABELS, SECTORS, ensure_warmer_started, hub_geom as _hub_geom,
    warm_cycle, warm_get, warm_hours, warm_status,
)
from core.hrrr_cam import MODELS

ensure_warmer_started(CACHE_ROOT)

st.title("Mesoscale Models")
st.caption("HRRR (hourly, 18 h; 48 h at 00/06/12/18Z), RRFS (3-hourly, 84 h) "
           "and the REFS ensemble (hourly, 60 h at 00/12Z). One sector, up to "
           "four maps, one run, one forecast hour.")

# ---------------------------------------------------------------- choices
MODEL_LABEL = {"hrrr": "HRRR", "rrfs": "RRFS", "refs": "REFS"}
_DET = {"REFD": "1 km reflectivity", "REFC": "Composite reflectivity",
        "RETOP": "Echo tops", "VIS": "Visibility", "CEIL": "Ceiling",
        "GUST": "10 m wind gust", "LTNG": "Lightning threat"}
# model key -> {product label: (source model in hrrr_cam.MODELS, product)}
PRODUCTS = {
    "hrrr": {lab: ("hrrr", p) for p, lab in _DET.items() if p in MODELS["hrrr"]["products"]},
    "rrfs": {lab: ("rrfs", p) for p, lab in _DET.items() if p in MODELS["rrfs"]["products"]},
    "refs": {
        "PMMN composite reflectivity": ("refs_pmmn", "REFC"),
        "Probability REFC ≥ 40 dBZ": ("refs_prob", "PROB_REFC40"),
        "Probability REFC ≥ 50 dBZ": ("refs_prob", "PROB_REFC50"),
        "Probability ceiling < 1000 ft": ("refs_prob", "PROB_CIG1000"),
        "Probability ceiling < 500 ft": ("refs_prob", "PROB_CIG500"),
        "Probability visibility < 1 sm": ("refs_prob", "PROB_VIS1"),
        "Probability visibility < 3 sm": ("refs_prob", "PROB_VIS3"),
        "Probability echo tops > FL350": ("refs_prob", "PROB_RETOP35"),
    },
}
_NONE = "— choose a product —"
_RUNS = {"Latest run": {"hrrr": 1, "rrfs": 1, "refs": 1},
         "Latest long run (HRRR 48 h / RRFS 84 h / REFS 60 h)": {"hrrr": 48, "rrfs": 84, "refs": 60}}

st.markdown(
    "<style>"
    "[data-testid='stDialog'] [data-testid='stButton'] button[kind='primary']{"
    "background:#00C853 !important;color:#000 !important;"
    "-webkit-text-fill-color:#000 !important;border:none !important;"
    "font-weight:700 !important;font-size:15px !important;width:100%;padding:10px 0}"
    "[data-testid='stDialog'] [data-testid='stButton'] button[kind='primary']:disabled{"
    "background:#2A2A2A !important;color:#6E6E6E !important;"
    "-webkit-text-fill-color:#6E6E6E !important}"
    # Plot selection boxes (7 Oct): white text was sitting on a white
    # field; the model/product dropdowns are a semi-dark blue instead.
    # Every layer of the BaseWeb select gets the blue field, and every
    # piece of text in it white (the inner value div was painting white
    # on white, 7 Oct).
    "[data-testid='stDialog'] [data-baseweb='select'],"
    "[data-testid='stDialog'] [data-baseweb='select'] > div,"
    "[data-testid='stDialog'] [data-baseweb='select'] > div > div,"
    "[data-testid='stDialog'] [data-baseweb='select'] div[value],"
    "[data-testid='stDialog'] [data-baseweb='select'] input{"
    "background:#1B2A4A !important;background-color:#1B2A4A !important;"
    "color:#FFFFFF !important;-webkit-text-fill-color:#FFFFFF !important;"
    "border-color:#2D3957 !important}"
    "[data-testid='stDialog'] [data-baseweb='select'] *{"
    "color:#FFFFFF !important;-webkit-text-fill-color:#FFFFFF !important}"
    "[data-testid='stDialog'] [data-baseweb='select'] svg{fill:#FFFFFF !important;color:#FFFFFF !important}"
    "div[data-baseweb='popover'] ul, div[data-baseweb='popover'] li{"
    "background:#1B2A4A !important;color:#FFFFFF !important}"
    "div[data-baseweb='popover'] li:hover, div[data-baseweb='popover'] li[aria-selected='true']{"
    "background:#2D3957 !important}"
    "</style>", unsafe_allow_html=True)


@st.dialog("Mesoscale Models - choose what to show", width="large")
def _setup():
    st.markdown("**1. Sector**")
    sector = st.radio("Sector", list(SECTORS), index=list(SECTORS).index(
        st.session_state.get("mm_sector", "NE")),
        format_func=lambda k: SECTOR_LABELS[k], horizontal=True,
        key="mm_dlg_sector", label_visibility="collapsed")
    st.markdown("**2. Plots**")
    nplots = st.radio("Plots", [1, 2, 4], index=[1, 2, 4].index(
        int(st.session_state.get("mm_nplots", 2))), horizontal=True,
        key="mm_dlg_nplots", label_visibility="collapsed")
    st.markdown("**3. Model and product for each plot** - any mix of HRRR, RRFS and REFS")
    prev = st.session_state.get("mm_slots", [])
    slots = []
    for i in range(nplots):
        c0, c1, c2 = st.columns([0.5, 1.1, 2.4])
        pm, pp = (prev[i] if i < len(prev) else ("hrrr", None))
        with c0:
            st.markdown(f'<div style="margin-top:10px;font-weight:700;color:#B8B8B8">Plot {i + 1}</div>',
                        unsafe_allow_html=True)
        with c1:
            m = st.selectbox("Model", list(MODEL_LABEL), index=list(MODEL_LABEL).index(pm),
                             format_func=lambda k: MODEL_LABEL[k], key=f"mm_dlg_m{i}",
                             label_visibility="collapsed")
        with c2:
            opts = [_NONE] + list(PRODUCTS[m])
            idx = opts.index(pp) if (pp in opts and m == pm) else 0
            p = st.selectbox("Product", opts, index=idx, key=f"mm_dlg_p{i}_{m}",
                             label_visibility="collapsed")
        slots.append((m, None if p == _NONE else p))
    st.markdown("**4. Run**")
    run = st.radio("Run", list(_RUNS), index=list(_RUNS).index(
        st.session_state.get("mm_run_choice", "Latest run")),
        horizontal=True, key="mm_dlg_run", label_visibility="collapsed")
    n_ok = sum(1 for _m, p in slots if p)
    if n_ok == nplots:
        st.markdown(f'<div style="color:#00C853;font-weight:700">{n_ok} of {nplots} plots set</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<div style="color:#FFD400;font-weight:700">{n_ok} of {nplots} plots set</div>',
                    unsafe_allow_html=True)
    if st.button("Continue", type="primary", disabled=(n_ok != nplots), key="mm_dlg_go"):
        st.session_state["mm_sector"] = sector
        st.session_state["mm_nplots"] = nplots
        st.session_state["mm_slots"] = slots
        st.session_state["mm_run_choice"] = run
        st.session_state["mm_setup_done"] = True
        st.rerun()


if not st.session_state.get("mm_setup_done"):
    _setup()
    st.info("Choose a sector, the number of plots and a product for each in the pop-up.")
    st.stop()

_sector = st.session_state["mm_sector"]
_slots = st.session_state["mm_slots"]
_nplots = int(st.session_state["mm_nplots"])

_PANEL, _EDGE, _INK, _INK2 = "#0A0A0A", "#333333", "#FFFFFF", "#B8B8B8"
st.markdown(
    "<style>[data-testid='stVerticalBlockBorderWrapper']{"
    f"background:{_PANEL};border:1px solid {_EDGE} !important;"
    "border-radius:12px;padding:6px 10px}</style>", unsafe_allow_html=True)

now = datetime.now(timezone.utc)
bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

_h0, _h1, _h2 = st.columns([1.2, 2.6, 1.4])
with _h0:
    if st.button("Change selection", key="mm_change"):
        st.session_state["mm_setup_done"] = False
        st.rerun()
with _h1:
    run_choice = st.radio("Run", list(_RUNS), horizontal=True, key="mm_run",
                          index=list(_RUNS).index(st.session_state.get("mm_run_choice", "Latest run")),
                          label_visibility="collapsed")
    st.session_state["mm_run_choice"] = run_choice
with _h2:
    pod_px = st.slider("Map size (px)", 300, 1000, 640 if _nplots <= 2 else 520, 10, key="mm_pod_px")


@st.cache_data(ttl=600, show_spinner=False, max_entries=48)
def _cycle_for(model: str, need_fhr: int, bucket: str):
    """Newest cycle of a model that has reached need_fhr."""
    src = {"hrrr": "hrrr", "rrfs": "rrfs", "refs": "refs_prob"}[model]
    try:
        if model in ("hrrr", "rrfs") and need_fhr <= max(warm_hours(model)):
            wc = warm_status(CACHE_ROOT).get(model)
            if wc:
                return wc
        if model == "refs" and need_fhr <= 1:
            wc = warm_cycle(CACHE_ROOT, "refs_prob@PROB_REFC40")
            if wc:
                return wc
    except Exception:
        pass
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(src, need_fhr)
    return cyc.isoformat() if cyc else None


# Label overlay for the viewer (constant screen size): one list per sector.
_la0, _lo0, _hw0 = _hub_geom(_sector)
from core.cam_fast import marks_json as _marks_json
_marks = _marks_json((_lo0 - _hw0, _la0 - _hw0, _lo0 + _hw0, _la0 + _hw0))

_models_used = sorted({m for m, _p in _slots})
_cycles = {m: _cycle_for(m, _RUNS[run_choice][m], bucket10) for m in _models_used}


def _max_fhr(model: str, cycle_iso: str) -> int:
    h = datetime.fromisoformat(cycle_iso).hour
    if model == "hrrr":
        return 48 if h in (0, 6, 12, 18) else 18
    if model == "refs":
        return 60 if h in (0, 12) else 48
    return MODELS["rrfs"]["max_fhr"]


_maxes = [_max_fhr(m, c) for m, c in _cycles.items() if c]
max_fhr = max(_maxes) if _maxes else 18
fhr = max(1, min(int(st.session_state.get("mm_fhr", 1) or 1), max_fhr))


@st.cache_data(ttl=10800, show_spinner=False, max_entries=800)
def cached_frame(src: str, field: str, cycle_iso: str, h: int, la: float, lo: float, zm: float):
    from core.hrrr_cam import fetch_and_decode, render_frame
    _c = datetime.fromisoformat(cycle_iso)
    vals, lats, lons = fetch_and_decode(src, field, _c, h, la, lo, zm)
    return render_frame(field, vals, lats, lons, la, lo, zm, "",
                        grid_key=f"{la:.2f},{lo:.2f},{zm:.2f}|{src}",
                        cache_root=CACHE_ROOT / "cam_warm")


def _frame(sector: str, src: str, field: str, h: int, cycle_iso: str):
    try:
        got = warm_get(CACHE_ROOT, f"{src}@{field}", sector, h)
        if got and got[1] == cycle_iso:
            return got[0], "warm"
    except Exception:
        pass
    clat, clon, zm = _hub_geom(sector)
    return cached_frame(src, field, cycle_iso, h, round(clat, 2), round(clon, 2), zm), "live"


def _viewer(img: bytes, height: int, marks: list) -> None:
    """Pan/zoom image viewer. Labels (JBU stations, other airports,
    N90 fixes) are an HTML overlay positioned from the map transform,
    so they stay a constant size on screen while the map zooms
    (7 Oct) - the raster carries dots, rings and lines only."""
    import base64 as _b64
    import json as _json
    import streamlit.components.v1 as _components
    mime = "image/webp" if img[:4] == b"RIFF" else "image/png"
    uri = f"data:{mime};base64," + _b64.b64encode(img).decode("ascii")
    _components.html(f"""
<style>
 .lb{{position:absolute;transform:translate(-50%,-100%);white-space:nowrap;pointer-events:none;
      font-family:"DejaVu Sans",Arial,sans-serif;font-weight:700;line-height:1}}
 .lb.jbu{{color:#4DA3FF;font-size:13px;text-shadow:0 0 3px #000,0 0 2px #000,1px 1px 0 #000}}
 .lb.apt{{color:#19C37D;font-size:9px;text-shadow:0 0 2px #000,1px 1px 0 #000}}
 .lb.fix{{color:#FFD400;font-size:8px;font-weight:600;text-shadow:0 0 2px #000,1px 1px 0 #000}}
</style>
<div id="w" style="width:100%;max-width:{height}px;height:{height}px;aspect-ratio:1/1;margin:0 auto;
     overflow:hidden;background:#0b0c0e;border-radius:8px;cursor:grab;position:relative">
 <img id="m" src="{uri}" draggable="false" style="position:absolute;left:0;top:0;width:100%;height:100%;transform-origin:0 0;user-select:none">
 <div id="ov" style="position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none"></div>
</div>
<script>(function(){{
  const MARKS = {_json.dumps(marks)};
  const OFF = {{jbu: 9, apt: 5, fix: 5}};   // px above the mark
  const w=document.getElementById('w'), m=document.getElementById('m'), ov=document.getElementById('ov');
  let s=1, tx=0, ty=0, drag=null;
  const els = MARKS.map(k => {{ const d=document.createElement('div'); d.className='lb '+k.k; d.textContent=k.t; ov.appendChild(d); return d; }});
  function place(){{
    const W=w.clientWidth, H=w.clientHeight;
    for (let i=0;i<MARKS.length;i++) {{
      const k=MARKS[i], x=tx+k.x*W*s, y=ty+k.y*H*s;
      const e=els[i];
      if (x<-40||x>W+40||y<-20||y>H+20) {{ e.style.display='none'; continue; }}
      e.style.display=''; e.style.left=x+'px'; e.style.top=(y-OFF[k.k])+'px';
      // de-clutter: fixes and other airports only once zoomed in a little
      if (k.k==='fix' && s<1.6) e.style.display='none';
      if (k.k==='apt' && s<1.2) e.style.display='none';
    }}
  }}
  function apply(){{ m.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`; place(); }}
  w.addEventListener('wheel', e=>{{ e.preventDefault(); const r=w.getBoundingClientRect(), x=e.clientX-r.left, y=e.clientY-r.top;
    // Zoom cap (7 Oct): up to twice the frame's native pixel scale, so the
    // raster never shows more than 2x upscaled (it blurred past that).
    const nat = (m.naturalWidth || 1950) / Math.max(1, w.clientWidth);
    const SMAX = Math.max(2, 2 * nat);
    const k=Math.exp(-e.deltaY*0.0015), ns=Math.min(SMAX, Math.max(1, s*k)); tx = x-(x-tx)*(ns/s); ty = y-(y-ty)*(ns/s); s=ns; if(s===1){{tx=0;ty=0;}} apply(); }}, {{passive:false}});
  w.addEventListener('mousedown', e=>{{ drag={{x:e.clientX-tx, y:e.clientY-ty}}; w.style.cursor='grabbing'; }});
  window.addEventListener('mousemove', e=>{{ if(!drag) return; tx=e.clientX-drag.x; ty=e.clientY-drag.y; apply(); }});
  window.addEventListener('mouseup', ()=>{{ drag=null; w.style.cursor='grab'; }});
  w.addEventListener('dblclick', ()=>{{ s=1; tx=0; ty=0; apply(); }});
  window.addEventListener('resize', place);
  apply();
}})();</script>""", height=height + 4)


def _colorbar(field: str) -> str:
    try:
        from core.cam_fast import PALETTES, _lut_for
        spec = PALETTES[field]
        bounds = spec["bounds"]
        lut = _lut_for(field)
        cols = ["#%02x%02x%02x" % tuple(int(v) for v in c[:3]) for c in lut[1:1 + len(bounds)]]
        unit = ("%" if field.startswith("PROB") else
                {"REFD": "dBZ", "REFC": "dBZ", "RETOP": "kft", "VIS": "sm",
                 "CEIL": "x100 ft", "GUST": "kt", "LTNG": "fl/km²"}.get(field, ""))
        cells = "".join(f'<div style="flex:1;text-align:center"><div style="height:8px;background:{c}"></div>{bounds[i]:g}</div>'
                        for i, c in enumerate(cols) if i < len(bounds))
        return (f'<div style="display:flex;font:11px DejaVu Sans Mono,monospace;color:{_INK2};'
                f'margin-top:4px">{cells}<div style="padding:8px 0 0 6px">{unit}</div></div>')
    except Exception:
        return ""


def _pod(i: int):
    model, plabel = _slots[i]
    src, field = PRODUCTS[model][plabel]
    with st.container(border=True):
        cycle_iso = _cycles.get(model)
        if not cycle_iso:
            st.warning(f"{MODEL_LABEL[model]}: no cycle found for that run.")
            return
        cyc = datetime.fromisoformat(cycle_iso)
        h, note = fhr, ""
        mx = _max_fhr(model, cycle_iso)
        if h > mx:
            h, note = mx, f" (run ends at f{mx:02d})"
        if model == "rrfs" and h % 3:
            h, note = (h // 3) * 3 or 3, " (RRFS is 3-hourly)"
        valid = cyc + timedelta(hours=h)
        st.markdown(
            f'<div style="display:flex;justify-content:space-between;font:bold 12px DejaVu Sans Mono,monospace;'
            f'color:{_INK};margin:2px 0 4px"><span>{MODEL_LABEL[model]} &middot; {plabel}</span>'
            f'<span style="color:{_INK2}">{cyc:%H}Z/{cyc:%d} &middot; f{h:02d} &middot; valid {valid:%H}Z/{valid:%d}{note}</span></div>',
            unsafe_allow_html=True)
        try:
            with st.spinner(""):
                img, how = _frame(_sector, src, field, h, cycle_iso)
            _viewer(img, pod_px, _marks)
            st.markdown(_colorbar(field), unsafe_allow_html=True)
            if how == "live":
                st.caption("rendered on demand")
        except Exception as exc:
            st.warning(f"{MODEL_LABEL[model]} {plabel}: {type(exc).__name__}: {str(exc)[:160]}")


# ------------------------------------------------------------- hour slider
_sa, _sb = st.columns([5, 1.2])
with _sa:
    st.slider("Forecast hour", 1, max_fhr, min(fhr, max_fhr), key="mm_fhr", label_visibility="collapsed")
    _ticks = sorted({(_max_fhr(m, c), MODEL_LABEL[m]) for m, c in _cycles.items() if c})
    _marks = "".join(
        f'<div style="position:absolute;left:calc(8px + (100% - 16px) * {(h - 1) / max(1, max_fhr - 1):.4f});'
        f'transform:translateX(-50%);text-align:center;color:{_INK2};font:bold 10px DejaVu Sans Mono,monospace">'
        f'<div style="width:2px;height:8px;background:#00E5FF;margin:0 auto 2px"></div>{lab} f{h:02d}</div>'
        for h, lab in _ticks)
    st.markdown(f'<div style="position:relative;height:30px;margin-top:-14px">{_marks}</div>', unsafe_allow_html=True)
with _sb:
    st.markdown(f'<div style="font:bold 13px DejaVu Sans Mono,monospace;color:{_INK2};margin-top:10px">f{fhr:02d}</div>',
                unsafe_allow_html=True)

st.markdown('<div style="font:700 15px DejaVu Sans Mono,monospace;color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
            f'letter-spacing:.5px;margin:10px 0 6px 2px;border-bottom:1px solid #333;padding-bottom:4px">'
            f'{SECTOR_LABELS[_sector].upper()}</div>', unsafe_allow_html=True)

# Layout: 1 -> one map; 2 -> side by side; 4 -> 2 x 2.
if _nplots == 1:
    _c = st.columns([1, 2, 1])
    with _c[1]:
        _pod(0)
elif _nplots == 2:
    _c = st.columns(2, gap="small")
    for _i in range(2):
        with _c[_i]:
            _pod(_i)
else:
    for _row in (0, 2):
        _c = st.columns(2, gap="small")
        for _j in range(2):
            with _c[_j]:
                _pod(_row + _j)

st.caption("One run and one forecast hour for every map; RRFS maps snap to the nearest 3-hourly frame. "
           "Blue: JetBlue stations with 5 and 20 mile rings. Green: other airports with major-carrier service. "
           "Red outline: N90.")
