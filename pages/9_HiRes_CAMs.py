"""Hi-Res CAMs - convection-allowing model viewer, 2x2 model grid.

HRRR viewer (single-model for now) with a hub pre-warmer
serving JFK/MCO/FLL/DCA reflectivity instantly. Other CAM configs
stay dormant in core.hrrr_cam for later re-enable.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1

def _embed_html(html: str, height: int) -> None:
    """Render raw HTML: st.iframe on newer Streamlit, else the
    deprecated components.v1.html (removed after 2026-06)."""
    fn = getattr(st, "iframe", None)
    if fn is not None:
        try:
            fn(html, height=height)
            return
        except TypeError:
            pass
    st.components.v1.html(html, height=height)


st.set_page_config(
    page_title="BlueMet - Hi-Res CAMs",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

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

# Background hub pre-warmer: renders JFK/MCO/FLL/DCA reflectivity
# frames for each new model cycle so hub views serve instantly.
from core.cam_warm import (
    CONUS_CENTER, CONUS_KEY, CONUS_ZOOM,
    HUBS as WARM_HUBS, HUB_LABELS as _HUB_LABELS,
    WARM_HOURS, WARM_PRODUCT, WARM_ZOOM,
    ensure_warmer_started, warm_get, warm_hours, warm_report,
    warm_status,
)
ensure_warmer_started(CACHE_ROOT)


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False, max_entries=30)
def cached_station_coords(icao: str):
    from core.stations import StationResolver
    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved, _ = resolver.resolve_many([icao])
    if not resolved:
        return None
    stn = resolved[0]
    return float(stn.lat), float(stn.lon)


@st.cache_data(ttl=300, show_spinner=False, max_entries=20)
def cached_model_cycle(model: str, fhr: int, bucket: str):
    # The warmer's manifest already knows each model's latest
    # complete cycle - reading it costs a disk stat instead of
    # NOMADS round-trips, and the warmer refreshes it continuously.
    # Only fall back to live probing for models the warmer doesn't
    # track (or before its first fill), or hours beyond the warm
    # window where a newer partial cycle may exist.
    try:
        if fhr <= max(warm_hours(model)):
            wc = warm_status(CACHE_ROOT).get(model)
            if wc:
                return wc
    except Exception:
        pass
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(model, fhr)
    return cyc.isoformat() if cyc else None


@st.cache_data(ttl=1800, show_spinner=False, max_entries=48)
def cached_grid_frame(
    model_cycle_fhr: tuple,   # ((model, cycle_iso, fhr), ...)
    product: str,
    clat: float, clon: float, zoom: float,
    style_v: int = 2,
):
    """One forecast frame for MANY models: all fetch+decode run
    concurrently (the slow, parallelizable part), then panels render
    serially. Returns {model: png | error_string}."""
    from core.hrrr_cam import (
        MODELS, parallel_fetch_decode, render_field,
    )
    from datetime import timedelta as _td

    tasks = []
    for model, cycle_iso, fhr in model_cycle_fhr:
        tasks.append({
            "key": model,
            "model": model,
            "product": product,
            "cycle": datetime.fromisoformat(cycle_iso),
            "fhr": fhr,
            "lat": clat, "lon": clon, "zoom_deg": zoom,
        })
    data = parallel_fetch_decode(tasks, max_workers=2)

    out = {}
    for model, cycle_iso, fhr in model_cycle_fhr:
        res = data.get(model)
        if isinstance(res, Exception) or res is None:
            out[model] = f"error: {res}"
            continue
        vals, lats, lons = res
        cycle = datetime.fromisoformat(cycle_iso)
        valid = cycle + _td(hours=fhr)
        # Forecast hour dropped from the title: the scrubber shows
        # it, and the thing you cannot recover by looking at the
        # control is WHICH RUN this is.
        title = f"{MODELS[model]['label']}  valid {valid:%m/%d %H}Z"
        headline = (f"{MODELS[model]['label']} "
                    f"{cycle:%d %b %Y  %H}Z run")
        try:
            out[model] = render_field(
                product, vals, lats, lons, clat, clon, zoom, title,
                headline=headline,
            )
        except Exception as e:
            out[model] = f"error: {e}"
    return out


@st.cache_data(ttl=1800, show_spinner=False, max_entries=96)
def cached_panel(
    model: str, product: str, cycle_iso: str, fhr: int,
    clat: float, clon: float, zoom: float,
    style_v: int = 2,
) -> bytes:
    """One rendered panel for one model. Keyed on model+cycle+fhr+
    product+region, so new model cycles refresh naturally."""
    from core.hrrr_cam import (
        fetch_field, decode_field, render_field, MODELS,
    )
    cycle = datetime.fromisoformat(cycle_iso)
    raw = fetch_field(model, product, cycle, fhr, clat, clon, zoom)
    vals, lats, lons = decode_field(raw)
    valid = cycle + __import__("datetime").timedelta(hours=fhr)
    title = (
        f"{MODELS[model]['label']} {cycle:%m/%d %H}Z  f{fhr:02d}  "
        f"valid {valid:%m/%d %H}Z"
    )
    return render_field(
        product, vals, lats, lons, clat, clon, zoom, title,
    )


_AXCAL = dict(l=0.015, r=0.985, t=0.045, b=0.075)


def _render_chunk(pchunk, data, frames, errs, prog,
                  done, total, product, rlat, rlon, rzoom):
    from core.hrrr_cam import MODELS, render_field
    from datetime import datetime as _dt, timedelta as _td2
    for j, (m, cyc, h) in enumerate(pchunk):
        prog.progress(
            min(0.99, (done + j + 1) / max(total, 1)),
            text=f"Rendering {MODELS[m]['label']} f{h:02d} "
                 f"({done + j + 1}/{total})",
        )
        res = data.get((m, h))
        if isinstance(res, Exception) or res is None:
            if isinstance(res, Exception):
                errs.setdefault(
                    m, f"f{h:02d}: {type(res).__name__}: "
                       f"{res}"[:200])
            continue
        vals, lats, lons = res
        cycle = _dt.fromisoformat(cyc)
        valid = cycle + _td2(hours=h)
        title = (
            f"{MODELS[m]['label']} {cycle:%m/%d %H}Z  f{h:02d}  "
            f"valid {valid:%m/%d %H}Z"
        )
        try:
            png = render_field(
                product, vals, lats, lons,
                rlat, rlon, rzoom, title,
            )
        except Exception as _re:
            errs.setdefault(
                m, f"render f{h:02d}: "
                   f"{type(_re).__name__}: {_re}"[:200])
            continue
        frames.setdefault(m, {})[h] = png
        data[(m, h)] = None


def _data_uri(b: bytes) -> str:
    """data: URI with the MIME the bytes actually are."""
    import base64 as _b64

    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        mime = "image/webp"
    elif b[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    else:
        mime = "image/png"
    return f"data:{mime};base64," + _b64.b64encode(b).decode()


def build_scrub_html(frames: dict, hour_axis: list,
                     order: list, single: bool = False,
                     home=None, conus=None,
                     axcal=None, cycles: dict = None,
                     product_key: str = "", urls: dict = None) -> tuple:
    """Client-side shared-slider grid from {model: {fhr: png}}.
    Returns (html, height). Used by smooth-scrub mode AND the
    instant-open warm path."""
    import base64
    import json as _json

    from core.hrrr_cam import MODELS

    def names_run(m):
        return MODELS[m]["label"]

    # Field name for the second line. PRODUCT_LABELS is what the
    # colorbar uses, so the two agree.
    try:
        from core.hrrr_cam import PRODUCT_LABELS as _PL
        _prod_label = _PL.get(product_key, product_key or "")
    except Exception:
        _prod_label = ""

    model_arrays = {}
    for m in order:
        if m not in frames or not frames[m]:
            continue
        arr = []
        for h in hour_axis:
            png = frames[m].get(h)
            # A URL beats bytes every time: cacheable, not re-sent on
            # rerun, and no base64 tax. Bytes remain the fallback for
            # on-demand renders that were never written to disk.
            u = (urls or {}).get(m, {}).get(h)
            arr.append(
                u if u else (_data_uri(png) if png else None)
            )
        model_arrays[m] = arr
    labels = [f"f{h:02d}" for h in hour_axis]
    # Run and valid time per model per hour, for the pinned overlay.
    # The headline baked into each PNG sits at the top of the CANVAS,
    # so zooming scrolls it out of view — which is exactly when you
    # need it. An HTML overlay pinned to the viewer stays put at any
    # zoom, and can update the valid time as the slider moves, which
    # a baked-in label cannot.
    from datetime import datetime as _dt, timedelta as _tdd
    heads = {}
    # Fall back to the warm manifest if the caller did not pass a
    # cycle. Two call sites feed this and one of them was arriving
    # empty, which silently degraded the headline to "HRRR f10" —
    # the one piece of information the user cannot get anywhere else
    # on the page. Looking it up here removes the dependency on
    # whichever caller is in play.
    _fallback = {}
    try:
        from core.cam_warm import warm_status as _ws

        _fallback = _ws(CACHE_ROOT) or {}
    except Exception:
        pass
    for m in order:
        ci = (cycles or {}).get(m) or _fallback.get(m)
        row = []
        for h in hour_axis:
            c = None
            if ci:
                try:
                    c = _dt.fromisoformat(str(ci))
                except Exception:
                    c = None
            if c is not None:
                v = c + _tdd(hours=h)
                # "HRRR valid 23Z 8-25". Split on \x1f:
                # [prefix, bold valid, italic product].
                row.append(
                    f"{names_run(m)} valid "
                    f"\x1f{v:%H}Z {v.month}-{v.day}\x1f"
                    f"{_prod_label}")
            else:
                row.append(f"{names_run(m)}\x1ff{h:02d}\x1f"
                           f"{_prod_label}")
        heads[m] = row
    order = [m for m in order if m in model_arrays]
    names = {m: MODELS[m]["label"] for m in order}
    _cols = "1fr" if single else "1fr 1fr"
    html = (
        "<style>"
        # Square side, set per view: one big panel fills
        # the screen; two side by side each get less.
        ":root{--sq:980px}"
        ".camgrid.pair{--sq:820px}"
        ".camgrid{display:grid;grid-template-columns:"
        + _cols + ";gap:6px}"
        # Fit by HEIGHT. aspect-ratio:1/1 looked right but makes the
        # wrapper as TALL as the container is WIDE — 2300 px in a
        # wide window — which the fixed component height then clips.
        # A fixed height with object-fit:contain shows the whole
        # square frame at any container width, letterboxed sideways.
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
        # Pinned to the VIEWPORT of the zoom wrapper, not to the
        # image, so panning and zooming leave it in place.
        # A BAR above the map, not a box on it. Sitting outside the
        # zoom wrapper means it can never cover weather, and because
        # the wrapper scrolls underneath it the bar stays visible at
        # any zoom — which is the whole point.
        # Width follows the square so the bar and the map share an
        # edge instead of the bar running the whole column.
        ".hdr{display:block;width:min(var(--sq),100%);"
        "margin:0 auto;box-sizing:border-box;"
        "background:#F2F2EE;border:1px solid #111;border-bottom:none;"
        "border-radius:4px 4px 0 0;padding:6px 10px;text-align:center;"
        # 22px, not 12. The in-map title box it replaces rendered
        # far larger than a normal caption once the frame was scaled
        # to the panel, and shrinking it in the move made the run and
        # valid time harder to read than before.
        "font:normal 22px/1.3 monospace;color:#111}"
        ".hdr .vt{font-weight:bold}"
        ".hdr .sub{font-style:italic;font-size:17px;opacity:0.85;"
        "font-weight:normal}"
        "#fswrap.fs .hdr{font-size:28px;line-height:1.35}"
        "#fswrap.fs .hdr .sub{font-size:21px}"
        ".ctl{font:13px monospace;margin:8px 0}"
        "input[type=range]{width:70%}"
        "#fswrap.fs{background:#c0c0c0;overflow:auto;"
        "height:100vh;padding:6px 14px;box-sizing:border-box}"
        "#fswrap.fs .camgrid{grid-template-columns:1fr}"
        "#fswrap.fs .camgrid>div{display:none}"
        "#fswrap.fs .camgrid>div.solo{display:block}"
        "#fswrap.fs .camgrid>div.solo img{max-height:84vh;"
        "width:auto;max-width:100%;display:block;margin:0 auto}"
        "#fswrap.fs .fsb{display:none}"
        "</style>"
        "<div id='fswrap'>"
        "<div class='ctl'>"
        "<button id='fsx' style='display:none;position:fixed;"
        "top:10px;right:14px;z-index:99;font:bold 15px monospace;"
        "cursor:pointer;padding:4px 12px'>&#10005;</button>"
        "Forecast hour: "
        "<span id='hlbl'></span><br>"
        "<input type='range' id='hsl' min='0' max='"
        + str(len(hour_axis) - 1) + "' value='0' step='1'>"
        # `pair` narrows the square when two panels sit side by
        # side; a single panel keeps the full :root size.
        "</div><div class='camgrid"
        + ("" if single else " pair") + "'>"
    )
    for m in order:
        _wrap = " class='zoomwrap'"
        html += ("<div id='cell_" + m + "'>"
                 # The tiny label duplicated the model name and the
                 # forecast hour; the header bar below carries both
                 # properly, so this is now just a spacer.
                 "<div class='camlbl'>" + ""
                 + " <button class='fsb' data-m='" + m
                 + "' style='font:11px monospace;cursor:pointer;"
                 "margin-left:10px;padding:1px 8px'>&#x26F6; "
                 "full size</button>"
                 "</div><div class='hdr' id='hdr_" + m + "'></div>"
                 "<div" + _wrap + "><img id='img_"
                 + m + "'></div></div>")
    html += "</div></div><script>"
    html += "const D=" + _json.dumps(model_arrays) + ";"
    html += "const L=" + _json.dumps(labels) + ";"
    html += "const H=" + _json.dumps(heads) + ";"
    html += (
        "const sl=document.getElementById('hsl');"
        "function upd(){const i=+sl.value;"
        "document.getElementById('hlbl').textContent=L[i];"
        "for(const m in H){const hd="
        "document.getElementById('hdr_'+m);"
        "if(hd){const t=(H[m][i]||'').split('\\x1f');"
        "hd.innerHTML=(t[0]||'')"
        "+(t[1]?\"<span class='vt'>\"+t[1]+'</span>':'')"
        "+(t[2]?\"<br><span class='sub'>\"+t[2]+'</span>':'');}}"
        "for(const m in D){const el="
        "document.getElementById('img_'+m);"
        "if(D[m][i]){el.src=D[m][i];el.style.display='';"
        "el.style.opacity=1;el.title='';}"
        "else{let k=i;while(k>=0&&!D[m][k])k--;"
        "if(k>=0){el.src=D[m][k];el.style.display='';"
        "el.style.opacity=0.35;"
        "el.title='beyond this model\\'s last hour ('+L[k]+')';}"
        "else{el.style.display='none';}}}}"
        "sl.addEventListener('input',upd);upd();"
        "const fw=document.getElementById('fswrap'),"
        "fx=document.getElementById('fsx');"
        "document.querySelectorAll('.fsb').forEach(b=>{"
        "b.addEventListener('click',()=>{"
        "fw.querySelectorAll('.camgrid>div').forEach("
        "d=>d.classList.remove('solo'));"
        "document.getElementById('cell_'+b.dataset.m)"
        ".classList.add('solo');"
        "(fw.requestFullscreen||fw.webkitRequestFullscreen"
        "||function(){alert('Fullscreen not permitted in this "
        "embed');}).call(fw);});});"
        "fx.addEventListener('click',()=>{"
        "(document.exitFullscreen||"
        "document.webkitExitFullscreen||function(){})"
        ".call(document);});"
        "function fchg(){const on=!!(document.fullscreenElement"
        "||document.webkitFullscreenElement);"
        "fx.style.display=on?'':'none';"
        "fw.classList.toggle('fs',on);"
        "if(!on){fw.querySelectorAll('.camgrid>div')"
        ".forEach(d=>d.classList.remove('solo'));}}"
        "document.addEventListener('fullscreenchange',fchg);"
        "document.addEventListener('webkitfullscreenchange',"
        "fchg);"
    )
    _cal = axcal or _AXCAL
    _homejs = "null"
    if home and conus:
        _hla, _hlo, _hw = home
        _cla, _clo, _cz = conus
        fx = (_cal["l"] + (_cal["r"] - _cal["l"])
              * ((_hlo - (_clo - _cz)) / (2.0 * _cz)))
        fy = (_cal["t"] + (1.0 - _cal["t"] - _cal["b"])
              * (((_cla + _cz) - _hla) / (2.0 * _cz)))
        s0 = (_cal["r"] - _cal["l"]) * _cz / max(_hw, 0.5)
        _homejs = "{fx:%.4f,fy:%.4f,s:%.2f}" % (fx, fy, s0)
    # Plain statement into the ALREADY-OPEN main script - a
    # nested <script> tag's closer terminates the outer script
    # early and dumps the rest as page text (observed live)
    html += "const HOME=" + _homejs + ";"
    if True:
        # Wheel zoom (cursor-anchored) + drag pan on every
        # panel; transform persists across frame swaps because
        # the <img> element is reused
        html += (
            "document.querySelectorAll('.zoomwrap').forEach("
            "w=>{const im=w.querySelector('img');"
            "let s=1,tx=0,ty=0,dragging=false,lx=0,ly=0;"
            "function goHome(){if(!HOME){s=1;tx=0;ty=0;ap();"
            "return;}const r=w.getBoundingClientRect();"
            "s=HOME.s;tx=r.width/2-HOME.fx*r.width*s;"
            "ty=r.height/2-HOME.fy*r.height*s;ap();}"
            "w.addEventListener('dblclick',goHome);"
            "setTimeout(goHome,60);"
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
        # +70 for the enlarged two-line header bar above each panel.
        return html, 150 + 980
    rows = (len(order) + 1) // 2
    return html, 150 + rows * 860


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Hi-Res CAMs - 4 Panel ""(HRRR | RRFS | ARW | FV3)")
st.caption(
    "Convection-allowing model viewer - aviation products, "
    "hourly-updating, with prewarmed hub views."
)

with st.sidebar:
    st.header("Region")
    icao_input = st.text_input("Airport ICAO", value="KJFK",
                               max_chars=4).strip().upper()
    conus_view = st.checkbox(
        "Full CONUS view", value=False,
        help="Render every panel continent-wide (edge-to-edge "
             "map, slim bottom colorbar). Crisp at national "
             "scale; hub views stay on native crops. Not "
             "prewarmed - renders live, then caches 30 min.",
    )
    zoom = st.slider(
        "Zoom (degrees)", 1.0, 6.0, 2.5, 0.5,
        help="Geographic window for hub renders. 2.5 serves "
             "instantly from the warm store. Wheel/drag on any "
             "panel pans within the frame; double-click resets.",
    )
    with st.expander("Home-position calibration"):
        st.caption("If panels open off-center from the hub, "
                   "nudge the map-axes fractions until the "
                   "hub sits centered at load, then tell "
                   "Claude the four numbers to harden.")
        _c1 = st.number_input("axes left", 0.0, 0.4, 0.015,
                              0.005, key="axl")
        _c2 = st.number_input("axes right", 0.5, 1.0, 0.985,
                              0.005, key="axr")
        _c3 = st.number_input("axes top", 0.0, 0.4, 0.045,
                              0.005, key="axt")
        _c4 = st.number_input("axes bottom", 0.0, 0.4, 0.075,
                              0.005, key="axb")
        st.session_state["_axcal"] = dict(
            l=_c1, r=_c2, t=_c3, b=_c4)
    panel_scale = st.slider(
        "Panel size (%)", 50, 100, 85, 5,
        help="Display size of the CAM panels",
    )
    st.session_state["panel_scale_v"] = panel_scale

    # HRRR + NAM 3km nest (proven idx path, instantaneous fields
    # RRFS holds NAM's grid slot as of 8/17 (NAM retires Oct
    # 2026; its successor earned the seat early). NAM remains a
    # single-view choice for comparison until retirement.
    show_models = {"hrrr": True, "rrfs": True,
                   "hiresw_arw": True, "hiresw_fv3": True,
                   "nam_nest": True, "refs_mean": True}

    st.header("Product")
    product_label = st.selectbox(
        "Product",
        options=[
            "1km Reflectivity",
            "Composite Reflectivity",
            "Echo Tops",
            "Visibility",
            "Ceiling",
            "10 m Wind Gust",
        ],
        index=0,
    )
    PRODUCT_KEY = {
        "1km Reflectivity": "REFD",
        "Composite Reflectivity": "REFC",
        "Echo Tops": "RETOP",
        "Visibility": "VIS",
        "Ceiling": "CEIL",
        "10 m Wind Gust": "GUST",
    }

    with st.expander("Warm store status (debug)"):
        try:
            st.text(
                "view state: cam_icao="
                f"{st.session_state.get('cam_icao')!r} open_hub="
                f"{st.session_state.get('open_hub')!r}"
            )
            for _ph in WARM_HUBS:
                _hits = 0
                for _pm in ["hrrr", "rrfs",
                            "hiresw_arw", "hiresw_fv3"]:
                    if warm_get(CACHE_ROOT, _pm, _ph, 1):
                        _hits += 1
                st.text(f"live warm_get {_ph}: "
                        f"{_hits}/4 models answer at f01")
            for line in warm_report(CACHE_ROOT):
                st.text(line)
            from core.cam_warm import WARM_STYLE as _ws
            st.caption(
                f"style={_ws} + full frames = current-era "
                "renders everywhere from the warm store. Older "
                "style or partial frames = rebuild "
                "pending/stuck; paste this to Claude."
            )
        except Exception as e:
            st.text(f"report failed: {e}")

    smooth = st.checkbox(
        "Smooth scrub mode", value=True,
        help="Preloads every hour in the range below, then scrubbing "
             "is instant (frames swap in the browser, no reloading). "
             "Preload takes a while on first run; hours cache on the "
             "server, so rebuilding later is fast.",
    )
    if smooth:
        # Default 0-24, which is exactly what the warmer keeps on
        # disk (CAM_WARM_MAX_FHR). Opening on 0-84 meant most of the
        # requested range was NOT warm, so the page rendered on
        # demand and the store went unused — the slowest possible
        # default on a page built around pre-warming.
        fhr_lo, fhr_hi = st.slider(
            "Preload hours", 0, 84, (0, 24),
            help="0-24 is pre-warmed and loads from disk instantly. "
                 "Beyond 24 renders on demand, a few seconds per "
                 "frame.",
        )
    else:
        fhr_all = st.slider(
            "Forecast hour (all models)", 0, 84, 1,
            help="HRRR f18 hourly (f48 synoptic); NAM to f60 "
                 "on synoptics; HRW ARW/FV3 to f48 on 00/12Z. "
                 "Panels clamp to their own max.",
        )

    st.divider()
    run_button = st.button("Render", type="primary",
                           use_container_width=True)

if run_button and icao_input:
    st.session_state["cam_icao"] = icao_input

# Auto-render on first arrival. The page used to sit blank until
# someone pressed Render, which is the wrong default once the warm
# store is populated: the frames are already built, so the wait is
# a second or two, and making people click for it hides the whole
# point of warming. Only fires ONCE per session — after that the
# pinned hub in session state drives it, so a rerun from moving a
# slider does not re-trigger anything.
if not st.session_state.get("_cam_autorun") and icao_input:
    st.session_state["_cam_autorun"] = True
    st.session_state["cam_icao"] = icao_input

active = st.session_state.get("cam_icao")

# Placeholder claimed BEFORE the work starts, so the notice is
# painted while frames load rather than after. Cleared at the end of
# the render, which is why it is a placeholder and not st.info.
_render_notice = st.empty()
if active:
    _render_notice.markdown(
        "<p style='text-align:center; font-size:30px; "
        "font-weight:700; margin:18px 0 6px 0;'>"
        "Rendering models\u2026</p>"
        "<p style='text-align:center; font-size:15px; opacity:0.75; "
        "margin:0 0 14px 0;'>"
        "Warmed frames appear in a few seconds. Nothing to click."
        "</p>",
        unsafe_allow_html=True,
    )

if active:
    # Always-visible hub switcher: session state pins cam_icao
    # across opens, which once trapped the page on a single hub
    # (the welcome branch's switcher became unreachable). One tap
    # now swaps stations from anywhere.
    # Two regions now, so the buttons get real names and real
    # width. The old labels stripped the leading K off an ICAO,
    # which made sense for KJFK and not for a region.
    st.markdown(
        "<style>div[data-testid='stHorizontalBlock'] button{"
        "font-size:17px;font-weight:600;padding:0.6rem 1rem}"
        "</style>", unsafe_allow_html=True)
    _sw_cols = st.columns(len(WARM_HUBS))
    for _i, _hk in enumerate(WARM_HUBS):
        _lbl = _HUB_LABELS.get(_hk, _hk)
        if _hk == active:
            _lbl = "\u25cf  " + _lbl
        if _sw_cols[_i].button(_lbl, key=f"sw_{_hk}",
                               use_container_width=True):
            st.session_state["cam_icao"] = _hk
            st.rerun()

    icao = active
    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve coordinates for {icao}.")
        st.stop()
    clat, clon = coords
    from core.cam_warm import RENDER_FACTOR
    if conus_view:
        rlat, rlon = CONUS_CENTER
        rzoom = CONUS_ZOOM
    else:
        rlat, rlon = round(clat, 2), round(clon, 2)
        rzoom = zoom * RENDER_FACTOR

    product = PRODUCT_KEY[product_label]

    st.info(f"**{icao}** | {product_label}")

    from core.hrrr_cam import MODELS

    PRODUCT_LABELS_SHORT = {
        "REFD": "1km reflectivity",
        "REFC": "Composite reflectivity", "RETOP": "Echo tops",
        "VIS": "Visibility", "CEIL": "Ceiling", "GUST": "Gusts",
    }

    def _panel_specs():
        """(model, cycle_iso, clamped_fhr) for every renderable model,
        plus per-model skip reasons."""
        specs, notes = [], {}
        for m in GRID_ORDER:
            cfg = MODELS[m]
            if not show_models.get(m, m == _single_model):
                notes[m] = "(unchecked in sidebar)"
                continue
            if product not in cfg["products"]:
                notes[m] = (
                    f"{PRODUCT_LABELS_SHORT.get(product, product)} is "
                    f"not available in {cfg['label']}."
                )
                continue
            fh = min(fhr_all, _eff_max_fhr(m, cfg))
            cyc = cached_model_cycle(m, fh, bucket10)
            if cyc is None:
                msg = f"No complete {cfg['label']} cycle found."
                if cfg["note"]:
                    msg += f" ({cfg['note']})"
                pd = cfg.get("_probe_diag") or {}
                if pd:
                    msg += " Probe verdicts: " + "; ".join(
                        f"{u.split('/com/')[-1].split('/rrfs.')[0]}"
                        f" -> {v}"
                        for u, v in list(pd.items())[:4]
                    )
                notes[m] = msg
                continue
            specs.append((m, cyc, fh))
        return specs, notes

    # HRRR and RRFS only. The other models still exist in
    # core.hrrr_cam and can be brought back by adding a line here,
    # but they were not pre-warmed and nobody was scrubbing them, so
    # every selection was a cold render.
    _VIEW_LABELS = {
        "Both (HRRR + RRFS)": None,
        "HRRR": "hrrr", "RRFS": "rrfs",
    }
    view_choice = st.radio(
        "View", list(_VIEW_LABELS.keys()),
        index=0, horizontal=True, key="cam_view",
        help="Single-model view renders one large panel with "
             "mouse-wheel zoom and drag-pan.",
    )
    _single_model = _VIEW_LABELS[view_choice]
    hrrr_ext = st.checkbox(
        "HRRR long-range \u2014 last 00/06/12/18Z run, to f48",
        value=False, key="hrrr_ext",
        help="HRRR runs hourly to f18; the four synoptic cycles "
             "extend to f48. With this on, hours past 18 come from "
             "the newest long-range run, and those frames are "
             "PRE-WARMED, so f34 at 14Z loads from disk rather than "
             "rendering. "
             "and the whole scrub renders from that single run "
             "for consistency. Hours past 18 aren't prewarmed, "
             "so they download on demand.",
    )

    def _eff_max_fhr(m, cfg):
        if m == "hrrr" and hrrr_ext:
            return 48
        return cfg["max_fhr"]
    # Two panels, not four: HRRR and RRFS are the ones that are
    # pre-warmed and the ones actually used. The HiResW pair stayed
    # in the grid long after anyone scrubbed them, and every panel is
    # a render.
    GRID_ORDER = ([_single_model] if _single_model
                  else ["hrrr", "rrfs"])

    if smooth:
        span = min(fhr_hi - fhr_lo, 84)
        hours = list(range(fhr_lo, fhr_lo + span + 1))
        active_models = [
            m for m in GRID_ORDER
            if show_models.get(m, m == _single_model) and product in MODELS[m]["products"]
        ]
        frames = {}      # model -> {fhr: png}
        skipped = []
        # Phase 1: every (model, hour) fetch+decode in PARALLEL
        from core.hrrr_cam import parallel_fetch_decode, render_field
        from datetime import timedelta as _td

        plan = []      # (model, cycle_iso, hour)
        for m in active_models:
            cfg = MODELS[m]
            mh = [h for h in hours if h <= _eff_max_fhr(m, cfg)]
            if not mh:
                skipped.append(f"{cfg['label']}: range beyond its max")
                continue
            cycle_iso = cached_model_cycle(m, mh[-1], bucket10)
            if cycle_iso is None:
                pd = MODELS[m].get("_probe_diag") or {}
                extra = ("; probes: " + "; ".join(
                    list(pd.values())[:3]) if pd else "")
                skipped.append(
                    f"{cfg['label']}: no cycle found{extra}"
                )
                continue
            for h in mh:
                plan.append((m, cycle_iso, h))

        # Pull any prewarmed frames first; only the rest download.
        warm_ok_s = (
            (not conus_view) and icao in WARM_HUBS
            and product == WARM_PRODUCT
            and abs(zoom - WARM_ZOOM) < 0.01
        )
        if warm_ok_s:
            still_plan = []
            for m, cyc, h in plan:
                got = None
                try:
                    if h in warm_hours(m):
                        got = warm_get(CACHE_ROOT, m,
                                       icao, h)
                except Exception:
                    got = None
                if got:
                    frames.setdefault(m, {})[h] = got[0]
                else:
                    still_plan.append((m, cyc, h))
            if len(still_plan) < len(plan):
                st.caption(
                    f"{len(plan) - len(still_plan)} prewarmed frames "
                    f"loaded instantly from disk"
                )
            plan = still_plan

        prog = st.progress(
            0.0, text=f"Downloading {len(plan)} fields in parallel..."
        )
        tasks = [{
            "key": (m, h),
            "model": m, "product": product,
            "cycle": datetime.fromisoformat(cyc), "fhr": h,
            "lat": rlat, "lon": rlon,
            "zoom_deg": rzoom,
        } for m, cyc, h in plan]
        # Chunked pipeline: fetch a slice, render it, FREE it -
        # peak memory is bounded by the chunk, not the preload.
        # (Holding all decoded CONUS frames at once OOM-killed
        # the instance at ~45 MB x N; even decimated, unbounded
        # accumulation is the disease - chunking is the cure.)
        _CHUNK = 12
        _errs = {}
        _done = 0
        for _c0 in range(0, len(tasks), _CHUNK):
            _tchunk = tasks[_c0:_c0 + _CHUNK]
            data = parallel_fetch_decode(_tchunk, max_workers=2)
            _pchunk = plan[_c0:_c0 + _CHUNK]
            _render_chunk(_pchunk, data, frames, _errs,
                          prog, _done, len(plan), product,
                          rlat, rlon, rzoom)
            _done += len(_pchunk)
            data.clear()
            import gc as _gc
            _gc.collect()
        prog.empty()
        # Surface the first real exception for any model that
        # produced zero frames - no more silent failures
        for _m, _e in _errs.items():
            if not frames.get(_m):
                skipped.append(f"{MODELS[_m]['label']}: {_e}")
        if skipped:
            st.warning(" | ".join(skipped))
        frames = {m: f for m, f in frames.items() if f}
        if skipped:
            st.caption(" | ".join(skipped))
        if not frames:
            st.error("No frames preloaded - check model/product/range.")
        else:
            # Cycle per model, so the pinned overlay can show the
            # run AND the valid time of whichever frame is showing.
            _cyc_by_model = {}
            for _m, _ci, _h in plan:
                _cyc_by_model.setdefault(_m, _ci)
            html, hgt = build_scrub_html(
                frames, hours, GRID_ORDER,
                single=bool(_single_model),
                cycles=_cyc_by_model,
                product_key=product,
                # home=None -> HOME is null in the JS, so the
                # panel opens at scale 1 with the ENTIRE
                # rendered frame visible, and dblclick returns
                # there. The rendered frame is now the wide
                # view (rzoom), so "fully zoomed out" is the
                # default and the wheel goes IN from there.
                home=None,
                conus=(rlat, rlon, rzoom),
                axcal=st.session_state.get("_axcal"),
            )
            _sc = st.session_state.get("panel_scale_v", 85)
            if _sc >= 100:
                _embed_html(html, height=hgt)
            else:
                _wl, _wm, _wr = st.columns(
                    [(100 - _sc) / 2, float(_sc),
                     (100 - _sc) / 2])
                with _wm:
                    _embed_html(html, height=hgt)
            st.caption(
                f"{sum(len(v) for v in frames.values())} frames "
                f"preloaded across {len(frames)} model(s). Scrub away."
            )
    else:
        # 2x2 model grid - HRRR keeps the top-right quadrant. All
        # models' data fetches run in PARALLEL inside
        # cached_grid_frame; panels then display from the dict.
        specs, notes = _panel_specs()
        # Warm-store eligibility: hub airport, warm product/zoom,
        # in-range hour.
        warm_ok = (
            icao in WARM_HUBS and product == WARM_PRODUCT
            and abs(zoom - WARM_ZOOM) < 0.01
        )
        grid = {}
        warm_hits = []
        remaining = list(specs)
        if warm_ok:
            still = []
            for m, cyc, fh in remaining:
                got = warm_get(CACHE_ROOT, m, icao, fh)                     if fh in WARM_HOURS else None
                if got:
                    grid[m] = got[0]
                    warm_hits.append(m)
                else:
                    still.append((m, cyc, fh))
            remaining = still
        if warm_hits:
            st.caption(
                f"Prewarmed hub frames: {', '.join(warm_hits)} "
                f"(instant from disk)"
            )
        if remaining:
            with st.spinner(
                f"Fetching {len(remaining)} model(s) in parallel..."
            ):
                grid.update(cached_grid_frame(
                    tuple(remaining), product,
                    rlat, rlon, rzoom,
                ))

        spec_fhr = {m: fh for m, _c, fh in specs}
        model_cols = st.columns(len(GRID_ORDER))
        for m, col in zip(GRID_ORDER, model_cols):
            with col:
                cfg = MODELS[m]
                st.markdown(f"**{cfg['label']}**")
                if m in notes:
                    st.caption(notes[m])
                    continue
                res = grid.get(m)
                if isinstance(res, (bytes, bytearray)):
                    if spec_fhr.get(m, fhr_all) != fhr_all:
                        st.caption(
                            f"f{fhr_all:02d} beyond {cfg['label']} "
                            f"range; showing f{spec_fhr[m]:02d} "
                            f"(its max)."
                        )
                    if panel_scale >= 100:
                        st.image(res,
                                 use_container_width=True)
                    else:
                        st.image(
                            res,
                            width=int(430 * panel_scale / 100),
                        )
                else:
                    st.error(f"{cfg['label']}: {res}")
                if cfg.get("note"):
                    st.caption(cfg["note"])

    # Panels are on screen; take the notice down.
    _render_notice.empty()

else:
    # INSTANT-OPEN: before any click, serve the prewarmed default-hub
    # scrub straight from disk (sub-second). Custom anything = the
    # normal Render flow.
    _hub_keys = list(WARM_HUBS)
    _hub_cols = st.columns(len(_hub_keys))
    for _i, _hk in enumerate(_hub_keys):
        if _hub_cols[_i].button(_hk[1:], key=f"open_{_hk}",
                                use_container_width=True):
            st.session_state["open_hub"] = _hk
    _open_hub = st.session_state.get("open_hub", "NE")
    _OPEN_ORDER = ["hrrr", "rrfs"]
    _warm_frames: dict = {}
    # warm_get returns (png, cycle) and the cycle was being thrown
    # away at got[0]. It is what the pinned overlay needs to say
    # WHICH RUN a frame belongs to, so keep the first one seen per
    # model — every frame in a warm set shares a cycle by definition.
    _warm_cycles: dict = {}
    # URLs, not bytes. publish_frame copies the warm frame into
    # static/ and hands back a name; the browser then caches it and a
    # rerun re-sends nothing. Falls back to reading the bytes only if
    # publishing fails, so a missing static dir degrades to the old
    # behaviour rather than an empty page.
    _warm_urls: dict = {}
    _base_url = (os.environ.get("RENDER_EXTERNAL_URL")
                 or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    _static_dir = Path(__file__).resolve().parent.parent / "static"
    try:
        from core.cam_warm import publish_frame, prune_published

        for _m in _OPEN_ORDER:
            for _h in warm_hours(_m):
                _nm, _cy = (publish_frame(CACHE_ROOT, _static_dir,
                                          _m, _open_hub, _h)
                            if _base_url else (None, None))
                if _nm:
                    _warm_urls.setdefault(_m, {})[_h] = (
                        f"{_base_url}/app/static/{_nm}")
                    _warm_frames.setdefault(_m, {})[_h] = b""
                    _warm_cycles.setdefault(_m, _cy)
                    continue
                got = warm_get(CACHE_ROOT, _m, _open_hub, _h)
                if got:
                    _warm_frames.setdefault(_m, {})[_h] = got[0]
                    _warm_cycles.setdefault(_m, got[1])
        try:
            prune_published(_static_dir)
        except Exception:
            pass
    except Exception:
        _warm_frames = {}
        _warm_cycles = {}
        _warm_urls = {}
    # Counts URL-published frames too: those hold a b"" placeholder
    # so the model/hour structure the scrubber walks stays intact.
    _n_warm = sum(len(v) for v in _warm_frames.values())
    if _n_warm >= 6:
        st.info(
            f"**{_HUB_LABELS.get(_open_hub, _open_hub)}** | "
            f"1km Reflectivity | prewarmed - "
            f"scrub instantly, or set up a custom view in the "
            f"sidebar and click **Render**."
        )
        _axis = sorted({h for v in _warm_frames.values()
                        for h in v})
        from core.cam_warm import RENDER_FACTOR as _RF
        # HUBS entries are (lat, lon, half_width) now, not pairs.
        _hg = WARM_HUBS[_open_hub]
        _hla2, _hlo2 = _hg[0], _hg[1]
        # warm_get returns (png, cycle); _warm_cycles is populated
        # alongside _warm_frames when the store is read.
        _html, _hgt = build_scrub_html(
            _warm_frames, _axis, _OPEN_ORDER,
            cycles=_warm_cycles,
            urls=_warm_urls,
            product_key=WARM_PRODUCT,
            home=None,   # open on the whole ±5 deg frame
            # Per-region half-width, not the old shared constant.
            conus=(_hla2, _hlo2,
                   _hg[2] if len(_hg) > 2 else WARM_ZOOM * _RF),
            axcal=st.session_state.get("_axcal"),
        )
        _sc2 = st.session_state.get("panel_scale_v", 85)
        if _sc2 >= 100:
            _embed_html(_html, height=_hgt)
        else:
            _owl, _owm, _owr = st.columns(
                [(100 - _sc2) / 2, float(_sc2), (100 - _sc2) / 2])
            with _owm:
                _embed_html(_html, height=_hgt)
        st.caption(
            f"{_n_warm} prewarmed frames served from disk at page "
            f"open."
        )
    else:
        st.info(
            "Enter an airport in the sidebar and click **Render**. "
            "(Instant-open view appears once the hub warmer has "
            "built its first frames.)"
        )
    st.markdown(
        """
        ### What this page is

        The classic 4-panel CAM comparison - HRRR, RRFS
        (NAM's operational successor), and both HiRes Window
        members (ARW, FV3) - centered on your airport,
        aviation products only: 1km reflectivity, echo tops, visibility, ceiling, and gusts,
        with smooth scrubbing. Hub buttons serve all four
        panels prewarmed - reflectivity scrubs instantly.
        """
    )
