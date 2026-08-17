"""REFS Ensemble - dedicated page for the RRFS Ensemble
Forecast System (HREF's successor).

Standalone by design: shares core fetch/render machinery with the
CAMs page but owns its own layout, so ensemble-specific features
(probability products, member spreads) can grow here without
touching the deterministic grid.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1

st.set_page_config(
    page_title="BlueMet - REFS Ensemble",
    layout="wide",
    initial_sidebar_state="expanded",
)

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = (_persistent if _persistent.exists()
              else Path("/tmp/wx_compare_cache"))
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

from core.cam_warm import HUBS as REFS_HUBS
from core.hrrr_cam import MODELS

st.title("REFS Ensemble")
st.caption(
    "RRFS Ensemble Forecast System - control + 6 members "
    "(HRRR among them). Replaces HREF at implementation "
    "(Oct 2026). Pre-implementation feed; availability follows "
    "the experimental schedule."
)


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


def build_scrub_html(frames: dict, hour_axis: list,
                     order: list, single: bool = False) -> tuple:
    """Client-side shared-slider grid from {model: {fhr: png}}.
    Returns (html, height). Used by smooth-scrub mode AND the
    instant-open warm path."""
    import base64
    import json as _json

    from core.hrrr_cam import MODELS

    model_arrays = {}
    for m in order:
        if m not in frames or not frames[m]:
            continue
        arr = []
        for h in hour_axis:
            png = frames[m].get(h)
            arr.append(
                "data:image/png;base64,"
                + base64.b64encode(png).decode() if png else None
            )
        model_arrays[m] = arr
    labels = [f"f{h:02d}" for h in hour_axis]
    order = [m for m in order if m in model_arrays]
    names = {m: MODELS[m]["label"] for m in order}
    _cols = "1fr" if single else "1fr 1fr"
    html = (
        "<style>"
        ".camgrid{display:grid;grid-template-columns:"
        + _cols + ";gap:6px}"
        ".camgrid img{width:100%;border:1px solid #888}"
        ".zoomwrap{overflow:hidden;cursor:grab}"
        ".zoomwrap img{transform-origin:0 0;user-select:none;"
        "-webkit-user-drag:none}"
        ".camlbl{font:bold 13px monospace;margin:2px 0}"
        ".ctl{font:13px monospace;margin:8px 0}"
        "input[type=range]{width:70%}"
        "</style>"
        "<div class='ctl'>Forecast hour: "
        "<span id='hlbl'></span><br>"
        "<input type='range' id='hsl' min='0' max='"
        + str(len(hour_axis) - 1) + "' value='0' step='1'>"
        "</div><div class='camgrid'>"
    )
    for m in order:
        _wrap = " class='zoomwrap'" if single else ""
        html += ("<div><div class='camlbl'>" + names[m]
                 + "</div><div" + _wrap + "><img id='img_"
                 + m + "'></div></div>")
    html += "</div><script>"
    html += "const D=" + _json.dumps(model_arrays) + ";"
    html += "const L=" + _json.dumps(labels) + ";"
    html += (
        "const sl=document.getElementById('hsl');"
        "function upd(){const i=+sl.value;"
        "document.getElementById('hlbl').textContent=L[i];"
        "for(const m in D){const el="
        "document.getElementById('img_'+m);"
        "if(D[m][i]){el.src=D[m][i];el.style.display='';}"
        "else{el.style.display='none';}}}"
        "sl.addEventListener('input',upd);upd();"
    )
    if single:
        # Wheel zoom (cursor-anchored) + drag pan on the single
        # panel; transform persists across frame swaps because
        # the <img> element is reused
        html += (
            "document.querySelectorAll('.zoomwrap').forEach("
            "w=>{const im=w.querySelector('img');"
            "let s=1,tx=0,ty=0,dragging=false,lx=0,ly=0;"
            "function ap(){im.style.transform="
            "'translate('+tx+'px,'+ty+'px) scale('+s+')';}"
            "w.addEventListener('wheel',e=>{e.preventDefault();"
            "const r=w.getBoundingClientRect();"
            "const mx=e.clientX-r.left,my=e.clientY-r.top;"
            "const os=s;"
            "s=Math.min(6,Math.max(1,s*(e.deltaY<0?1.15:0.87)));"
            "tx=mx-(mx-tx)*s/os;ty=my-(my-ty)*s/os;"
            "if(s===1){tx=0;ty=0;}ap();},{passive:false});"
            "w.addEventListener('mousedown',e=>{dragging=true;"
            "lx=e.clientX;ly=e.clientY;"
            "w.style.cursor='grabbing';});"
            "window.addEventListener('mouseup',()=>{"
            "dragging=false;w.style.cursor='grab';});"
            "window.addEventListener('mousemove',e=>{"
            "if(!dragging)return;tx+=e.clientX-lx;"
            "ty+=e.clientY-ly;lx=e.clientX;ly=e.clientY;ap();});"
            "});"
        )
    html += "</script>"
    if single:
        return html, 140 + 720
    rows = (len(order) + 1) // 2
    return html, 140 + rows * 560


PRODUCTS = {
    "Ensemble mean": "refs_mean",
    "Prob-matched mean (PMMN)": "refs_pmmn",
    "Local prob-matched (LPMM)": "refs_lpmm",
}

with st.sidebar:
    st.header("Station")
    icao_input = st.text_input(
        "ICAO", value="", placeholder="e.g. KJFK",
        help="Any station; hub buttons below for one tap",
    ).strip().upper()

    st.header("Ensemble product")
    prod_label = st.radio(
        "Product", list(PRODUCTS.keys()), index=1,
        help="Mean smooths displaced cells into broad signal; "
             "PMMN/LPMM keep realistic reflectivity structure "
             "(the products HREF displays are built on). All "
             "render composite reflectivity.",
    )

    st.header("Hours")
    fhr_lo, fhr_hi = st.slider(
        "Preload hours", 0, 60, (0, 24),
        help="REFS runs 00/06/12/18Z to 60h. Span capped at 24.",
    )
    zoom = st.slider("Zoom (degrees)", 1.0, 6.0, 2.5, 0.5)

    st.divider()
    run_button = st.button("Render", type="primary",
                           use_container_width=True)

if run_button and icao_input:
    st.session_state["refs_icao"] = icao_input

active = st.session_state.get("refs_icao")

if not active:
    st.info("Pick a hub or enter an ICAO, then Render.")
    _c = st.columns(len(REFS_HUBS))
    for _i, _hk in enumerate(REFS_HUBS):
        if _c[_i].button(_hk[1:], key=f"w_{_hk}",
                         use_container_width=True):
            st.session_state["refs_icao"] = _hk
            st.rerun()
else:
    _sw = st.columns(len(REFS_HUBS))
    for _i, _hk in enumerate(REFS_HUBS):
        _lbl = ("* " + _hk[1:]) if _hk == active else _hk[1:]
        if _sw[_i].button(_lbl, key=f"sw_{_hk}",
                          use_container_width=True):
            st.session_state["refs_icao"] = _hk
            st.rerun()

    icao = active
    model = PRODUCTS[prod_label]
    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve coordinates for {icao}.")
        st.stop()
    clat, clon = coords
    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

    st.info(f"**{icao}** | {prod_label} | composite reflectivity")

    span = min(fhr_hi - fhr_lo, 24)
    hours = [h for h in range(fhr_lo, fhr_lo + span + 1)
             if h <= MODELS[model]["max_fhr"]]

    cycle_iso = cached_refs_cycle(model, hours[-1], bucket10)
    if cycle_iso is None:
        pd = MODELS[model].get("_probe_diag") or {}
        st.warning(
            f"{MODELS[model]['label']}: no cycle found"
            + ("; probes: " + "; ".join(list(pd.values())[:4])
               if pd else "")
        )
        st.stop()

    from core.hrrr_cam import parallel_fetch_decode, render_field

    prog = st.progress(0.0, text=f"Downloading {len(hours)} "
                                 "ensemble fields...")
    tasks = [{
        "key": (model, h),
        "model": model, "product": "REFC",
        "cycle": datetime.fromisoformat(cycle_iso), "fhr": h,
        "lat": round(clat, 2), "lon": round(clon, 2),
        "zoom_deg": zoom,
    } for h in hours]
    data = parallel_fetch_decode(tasks, max_workers=2)
    prog.progress(0.5, text="Rendering frames...")

    frames = {model: {}}
    _errs = {}
    cycle = datetime.fromisoformat(cycle_iso)
    for i, h in enumerate(hours):
        prog.progress(0.5 + 0.5 * (i + 1) / len(hours),
                      text=f"Rendering f{h:02d}")
        res = data.get((model, h))
        if isinstance(res, Exception) or res is None:
            if isinstance(res, Exception):
                _errs.setdefault(
                    model, f"f{h:02d}: {type(res).__name__}: "
                           f"{res}"[:200])
            continue
        vals, lats, lons = res
        valid = cycle + timedelta(hours=h)
        title = (f"{MODELS[model]['label']} "
                 f"{cycle:%m/%d %H}Z  f{h:02d}  "
                 f"valid {valid:%m/%d %H}Z")
        try:
            frames[model][h] = render_field(
                "REFC", vals, lats, lons,
                round(clat, 2), round(clon, 2), zoom, title,
            )
        except Exception as _re:
            _errs.setdefault(
                model, f"render f{h:02d}: "
                       f"{type(_re).__name__}: {_re}"[:200])
    prog.empty()

    if not frames[model]:
        for _m, _e in _errs.items():
            st.warning(f"{MODELS[_m]['label']}: {_e}")
        st.error("No REFS frames - see verdicts above.")
        st.stop()

    got_hours = sorted(frames[model].keys())
    html, hgt = build_scrub_html(frames, got_hours, [model],
                                 single=True)
    streamlit.components.v1.html(html, height=hgt)
    st.caption(
        f"{len(got_hours)} frames | wheel to zoom "
        "(cursor-anchored), drag to pan; view holds while "
        "scrubbing | probabilities land here next once a "
        "percent render path exists"
    )
