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

# Same order as every other page: config, theme, auth. Page 11 was
# never themed, so arriving here from a themed page switched styling
# and dropped the sidebar nav captions.
from retro_theme import apply_retro_theme

apply_retro_theme()

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = (_persistent if _persistent.exists()
              else Path("/tmp/wx_compare_cache"))
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

# Announce activity so the warmer backs off while this page renders.
from core.cam_warm import note_request as _note_req

_note_req()

from core.cam_warm import (
    HUBS as REFS_HUBS, WARM_ZOOM, ensure_warmer_started,
    warm_cycle, warm_get, warm_hours,
)
from core.hrrr_cam import MODELS

ensure_warmer_started(CACHE_ROOT)

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



def _data_uri(b: bytes) -> str:
    """data: URI carrying the MIME the bytes actually are."""
    import base64 as _b64

    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64," + _b64.b64encode(b).decode()


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
                # MIME from the magic bytes: render_field returns
                # WebP now, and calling it PNG fails silently in
                # some browsers.
                _data_uri(png) if png else None
            )
        model_arrays[m] = arr
    labels = [f"f{h:02d}" for h in hour_axis]
    order = [m for m in order if m in model_arrays]
    names = {m: MODELS[m]["label"] for m in order}
    _cols = "1fr" if single else "1fr 1fr"
    html = (
        "<style>"
        # Square side, set per view: one big panel fills
        # the screen; two side by side each get less.
        ":root{--sq:980px}"
        ".camgrid.pair{--sq:980px}"
        ".camgrid{display:grid;grid-template-columns:"
        + _cols + ";gap:6px}"
        # Fit by HEIGHT, not aspect-ratio.
        #
        # aspect-ratio:1/1 was the previous attempt and it made the
        # clipping worse: the container is ~2300 px wide, so a square
        # wrapper became 2300 px TALL, far past the component height,
        # and the iframe cut it off. Pinning the wrapper to a fixed
        # height and letting object-fit:contain letterbox the square
        # horizontally shows the WHOLE 10x10 degree frame, every
        # time, at any container width.
        ".camgrid img{border:1px solid #888}"
        # SQUARE viewport, not full width. The frame is 10x10
        # degrees; stretching the wrapper across a 2300 px window
        # left enormous white margins either side and squeezed the
        # map into a short band. Sizing the wrapper to the frame's
        # own shape uses the space for map instead of padding.
        # min() so a narrow window still fits.
        ".camgrid img{border:1px solid #888}"
        ".zoomwrap{overflow:hidden;cursor:grab;"
        "width:min(var(--sq),100%);height:var(--sq);"
        "margin:0 auto;display:flex;align-items:center;"
        "justify-content:center;background:#fff}"
        ".zoomwrap img{max-width:100%;max-height:100%;"
        "width:auto;height:100%;object-fit:contain;"
        "transform-origin:center center;user-select:none;"
        "-webkit-user-drag:none}"
        ".camlbl{font:bold 13px monospace;margin:2px 0}"
        ".ctl{font:13px monospace;margin:8px 0}"
        "input[type=range]{width:70%}"
        "</style>"
        "<div class='ctl'>Forecast hour: "
        "<span id='hlbl'></span><br>"
        "<input type='range' id='hsl' min='0' max='"
        + str(len(hour_axis) - 1) + "' value='0' step='1'>"
        # `pair` narrows the square when two panels sit side by
        # side; a single panel keeps the full :root size.
        "</div><div class='camgrid"
        + ("" if single else " pair") + "'>"
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
        # 820 px of map + slider, label and padding.
        return html, 150 + 980
    rows = (len(order) + 1) // 2
    return html, 150 + rows * 1020


PRODUCTS = {
    "Ensemble mean": ("refs_mean", "REFC"),
    "Prob-matched mean (PMMN)": ("refs_pmmn", "REFC"),
    "Local prob-matched (LPMM)": ("refs_lpmm", "REFC"),
    "Probability REFC >= 40 dBZ": ("refs_prob", "PROB_REFC40"),
    "Prob ceiling < 500 ft": ("refs_prob", "PROB_CIG500"),
    "Prob ceiling < 1000 ft": ("refs_prob", "PROB_CIG1000"),
    "Prob ceiling < 2000 ft": ("refs_prob", "PROB_CIG2000"),
    "Prob visibility < 1/2 sm": ("refs_prob", "PROB_VIS05"),
    "Prob visibility < 1 sm": ("refs_prob", "PROB_VIS1"),
    "Prob visibility < 3 sm": ("refs_prob", "PROB_VIS3"),
    "Prob echo tops > 30 kft": ("refs_prob", "PROB_RETOP30"),
    "Prob echo tops > 35 kft": ("refs_prob", "PROB_RETOP35"),
}

with st.sidebar:
    st.header("Station")
    icao_input = st.text_input(
        "ICAO", value="", placeholder="e.g. KJFK",
        help="Any station; hub buttons below for one tap",
    ).strip().upper()

    st.header("Ensemble product")
    prod_label = st.selectbox(
        "Product", list(PRODUCTS.keys()), index=1,
        help="Mean smooths displaced cells into broad signal; "
             "PMMN/LPMM keep realistic reflectivity structure "
             "(the products HREF displays are built on). All "
             "render composite reflectivity.",
    )

    st.header("Hours")
    fhr_lo, fhr_hi = st.slider(
        "Preload hours", 0, 60, (0, 24),
        help="REFS runs 00/06/12/18Z to 60h. Full-run spans "
             "allowed; warmed hub products load instantly.",
    )
    # 2.5 deg matches WARM_ZOOM, which is what the warm store is
    # rendered at — moving off it silently disables instant open,
    # because the page tests `abs(zoom - WARM_ZOOM) < 0.01` before
    # reading the store. The frame itself covers +-5 deg
    # (RENDER_FACTOR 2), and the CSS above now shows all of it, so
    # the default already IS the whole map.
    zoom = st.slider("Zoom (degrees)", 1.0, 6.0, 2.5, 0.5,
                     help="2.5 uses the pre-warmed frames and opens "
                          "instantly. Other values render on demand.")
    # Display width as a percentage of the column. Default 70 rather
    # than full width: the ensemble opens as a grid of members, and
    # at 100% the first row alone fills the window so nothing else is
    # visible without scrolling. 70% fits the whole set on a laptop.
    refs_scale = st.slider(
        "Panel size (%)", 40, 100, 70, 5,
        help="Display size only — the underlying render is "
             "unchanged, so this costs nothing to move.")
    st.session_state["refs_scale_v"] = refs_scale

    st.divider()
    run_button = st.button("Render", type="primary",
                           width=int(760 * st.session_state.get(
                               "refs_scale_v", 70) / 100))

if run_button and icao_input:
    st.session_state["refs_icao"] = icao_input

active = st.session_state.get("refs_icao")

if not active:
    st.info("Pick a hub or enter an ICAO, then Render.")
    _c = st.columns(len(REFS_HUBS))
    for _i, _hk in enumerate(REFS_HUBS):
        if _c[_i].button(_hk[1:], key=f"w_{_hk}",
                         width=int(360 * st.session_state.get(
                             "refs_scale_v", 70) / 100)):
            st.session_state["refs_icao"] = _hk
            st.rerun()
else:
    _sw = st.columns(len(REFS_HUBS))
    for _i, _hk in enumerate(REFS_HUBS):
        _lbl = ("* " + _hk[1:]) if _hk == active else _hk[1:]
        if _sw[_i].button(_lbl, key=f"sw_{_hk}",
                          width=int(360 * st.session_state.get(
                              "refs_scale_v", 70) / 100)):
            st.session_state["refs_icao"] = _hk
            st.rerun()

    icao = active
    model, field = PRODUCTS[prod_label]
    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve coordinates for {icao}.")
        st.stop()
    clat, clon = coords
    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

    st.info(f"**{icao}** | {prod_label}")

    span = min(fhr_hi - fhr_lo, 60)
    _lo = MODELS[model].get("min_fhr", 0)
    hours = [h for h in range(fhr_lo, fhr_lo + span + 1)
             if _lo <= h <= MODELS[model]["max_fhr"]]
    if not hours:
        st.warning(f"{MODELS[model]['label']} starts at "
                   f"f{_lo:02d} - raise the hour range.")
        st.stop()

    _wk = f"{model}@{field}"
    _warm_ok = (icao in REFS_HUBS
                and abs(zoom - WARM_ZOOM) < 0.01)
    cycle_iso = None
    if _warm_ok:
        _wc = warm_cycle(CACHE_ROOT, _wk)
        # Use the warm store if it covers ANY of the requested hours,
        # not only if it covers the LAST one. The warmer is now
        # depth-capped (CAM_WARM_MAX_FHR, 24 by default) to leave CPU
        # for the CONUS map, so a request out to f60 would never see
        # a warm frame under the old test — the store would be full
        # and completely unused. Hours past the cap fall through to
        # an on-demand render, which is the intended trade.
        _wh = set(warm_hours(_wk))
        if _wc and (_wh & set(hours)):
            cycle_iso = _wc
            _n_warm_hrs = len(_wh & set(hours))
            if _n_warm_hrs < len(hours):
                st.caption(
                    f"{_n_warm_hrs} of {len(hours)} hours are "
                    f"pre-warmed (to f{max(_wh):02d}); later hours "
                    f"render on demand."
                )
    if cycle_iso is None:
        cycle_iso = cached_refs_cycle(model, hours[-1], bucket10)
    if cycle_iso is None:
        pd = MODELS[model].get("_probe_diag") or {}
        st.warning(
            f"{MODELS[model]['label']}: no cycle found"
            + ("; probes: " + "; ".join(list(pd.values())[:4])
               if pd else "")
        )
        st.stop()

    @st.cache_data(ttl=10800, show_spinner=False,
                   max_entries=600)
    def cached_refs_frame(model: str, field: str,
                          cycle_iso: str, h: int,
                          la: float, lo: float, zm: float):
        from core.hrrr_cam import fetch_and_decode, render_field
        cyc = datetime.fromisoformat(cycle_iso)
        vals, lats, lons = fetch_and_decode(
            model, field, cyc, h, la, lo, zm)
        valid = cyc + timedelta(hours=h)
        title = (f"{MODELS[model]['label']} "
                 f"{cyc:%m/%d %H}Z  f{h:02d}  "
                 f"valid {valid:%m/%d %H}Z")
        return render_field(field, vals, lats, lons,
                            la, lo, zm, title)

    prog = st.progress(0.0, text=f"Loading {len(hours)} "
                                 "ensemble frames...")
    frames = {model: {}}
    _errs = {}
    for i, h in enumerate(hours):
        prog.progress((i + 1) / len(hours),
                      text=f"Frame f{h:02d} ({i + 1}/"
                           f"{len(hours)})")
        try:
            got = (warm_get(CACHE_ROOT, _wk, icao, h)
                   if _warm_ok else None)
            if got and got[1] == cycle_iso:
                frames[model][h] = got[0]
            else:
                frames[model][h] = cached_refs_frame(
                    model, field, cycle_iso, h,
                    round(clat, 2), round(clon, 2), zoom)
        except Exception as _re:
            _errs.setdefault(
                model, f"f{h:02d}: {type(_re).__name__}: "
                       f"{_re}"[:220])
    prog.empty()

    if not frames[model]:
        for _m, _e in _errs.items():
            st.warning(f"{MODELS[_m]['label']}: {_e}")
        _pd = MODELS[model].get("_probe_diag") or {}
        _ir = MODELS[model].get("_idx_resolved")
        if _ir or _pd:
            st.caption(
                "resolved template: " + (_ir or "none")
                + ((" | probes: " + "; ".join(
                    list(_pd.values())[:4])) if _pd else "")
            )
        st.error("No REFS frames - see verdicts above.")
        st.stop()

    got_hours = sorted(frames[model].keys())
    html, hgt = build_scrub_html(frames, got_hours, [model],
                                 single=True)
    streamlit.components.v1.html(html, height=hgt)
    with st.expander("File inventory (what this REFS file "
                     "contains)"):
        try:
            import requests as _rq
            _tpl = (MODELS[model].get("_idx_resolved")
                    or MODELS[model]["idx"])
            _cyc = datetime.fromisoformat(cycle_iso)
            _iu = _tpl.format(ymd=_cyc.strftime("%Y%m%d"),
                              cc=_cyc.hour, ff=got_hours[-1])
            _it = _rq.get(_iu, timeout=15).text
            _sel = st.text_input("Filter lines", value="prob"
                                 if field.startswith("PROB")
                                 else "")
            _show = [l for l in _it.splitlines()
                     if _sel.lower() in l.lower()][:120]
            st.code("\n".join(_show) or "(no matching lines)")
        except Exception as _ie:
            st.caption(f"inventory unavailable: {_ie}")

    st.caption(
        f"{len(got_hours)} frames | wheel to zoom "
        "(cursor-anchored), drag to pan; view holds while "
        "scrubbing | hub views prewarm to disk each cycle "
        "(PMMN + CIG/VIS/REFC probs); everything else caches "
        "server-side for 3h"
    )
