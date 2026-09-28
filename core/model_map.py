"""Model forecast view for the Large Scale Map page (28 Sep).

One CONUS frame of a forecast model, hour by hour on a slider: pick the
model (RRFS - NOAA's next-generation CONUS model - by default, HRRR,
NAM nest), pick a layer (reflectivity, ceiling, visibility, lightning)
and drag. Frames come from the same fetch -> decode -> render pipeline
as the Hi-Res CAMs page (core.hrrr_cam / core.cam_fast) at the CONUS
geometry core.cam_warm already defines; nothing is pre-warmed, every
frame renders on request and is kept for three hours. The data edge of
each frame is the model's own domain edge (cam_fast masks pixels past
the grid) and the frame is cropped to that domain, so the map boundary
IS the model's.

Env: BLUEMET_CONUS_PPD  pixels per degree for the frame (40).

Called from pages/15_Large_Scale_Map.py as core.model_map.render().
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from streamlit.components.v1 import html as st_html


# Model menu. Keys are core.hrrr_cam.MODELS entries; RRFS first because
# it is the next-generation CONUS model (replaces HRRR, NAM nest and the
# HRW runs in Oct 2026) and the one the page is built around.
FC_MODELS = [("rrfs", "RRFS 3 km (next-gen)"),
             ("hrrr", "HRRR 3 km"),
             ("nam_nest", "NAM 3 km nest")]
# Layer menu: label -> product key in core.hrrr_cam. LTNG is the
# model's composite lightning threat, added for this page (HRRR and
# RRFS carry it; the NAM nest does not).
FC_LAYERS = [("Precipitation reflectivity", "REFC"),
             ("Ceilings", "CEIL"),
             ("Visibility", "VIS"),
             ("Lightning", "LTNG")]
FC_UNITS = {"REFC": "dBZ", "CEIL": "x100 ft", "VIS": "sm",
            "LTNG": "flashes/km²/5 min"}
# The slider's ceiling. No model here reaches it yet - RRFS ends at
# f84 (3.5 days), HRRR at 18/48, the nest at 60 - so the slider stops
# at the model's own last hour and grows on its own when a longer run
# is wired into MODELS.
FC_SLIDER_MAX = 120
FC_PPD = int(os.environ.get("BLUEMET_CONUS_PPD", "40"))

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

_INK2 = "#B8B8B8"


def _run_max_fhr(model: str, cycle_iso: str) -> int:
    """Last hour of THIS run. HRRR goes to 48 at 00/06/12/18Z and 18
    otherwise; everything else runs to its MODELS max every cycle."""
    from core.hrrr_cam import MODELS
    if model == "hrrr":
        return 48 if datetime.fromisoformat(cycle_iso).hour in (0, 6, 12, 18) else 18
    return int(MODELS[model]["max_fhr"])


def _model_max_fhr(model: str) -> int:
    from core.hrrr_cam import MODELS
    return 48 if model == "hrrr" else int(MODELS[model]["max_fhr"])


@st.cache_data(ttl=600, show_spinner=False, max_entries=256)
def cached_cycle_for_hour(model: str, fhr: int, bucket: str):
    """Newest run of `model` that has published forecast hour `fhr`
    (an .idx HEAD probe, walking back through the model's cycles).
    So every position of the slider shows the freshest data that
    exists for that hour, and a run still in progress never 404s."""
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(model, max(1, fhr))
    return cyc.isoformat() if cyc else None


@st.cache_data(ttl=10800, show_spinner=False, max_entries=48)
def cached_conus_frame(model: str, field: str, cycle_iso: str, h: int,
                       ppd: int):
    """One CONUS frame as (webp_bytes, (w, s, e, n) of the model's
    domain). Fetch and decode at core.cam_warm's CONUS geometry, render
    through cam_fast with station dots only, then crop the square frame
    to the model's own lat/lon box so the picture is the domain and
    nothing else."""
    import io

    from PIL import Image

    from core import cam_fast as _CF
    from core.cam_warm import CONUS_CENTER, CONUS_KEY, CONUS_ZOOM
    from core.hrrr_cam import fetch_and_decode, render_field

    la, lo = CONUS_CENTER
    zm = float(CONUS_ZOOM)
    cyc = datetime.fromisoformat(cycle_iso)
    vals, lats, lons = fetch_and_decode(model, field, cyc, h, la, lo, zm)
    import numpy as np
    box = (float(np.nanmin(lons)), float(np.nanmin(lats)),
           float(np.nanmax(lons)), float(np.nanmax(lats)))
    grid_key = f"{CONUS_KEY}|{model}"
    if _CF.supports(field):
        img = _CF.render_fast(field, vals, lats, lons, la, lo, zm,
                              grid_key=grid_key, ppd=ppd,
                              cache_dir=str(CACHE_ROOT / "cam_warm"),
                              stations="dots")
    else:
        img = render_field(field, vals, lats, lons, la, lo, zm, "")
    # Crop to the domain box. The frame is a linear lat/lon canvas
    # (axes fill the figure), so the pixel box is a proportion of the
    # square extent; the model's own edge is already masked inside it.
    try:
        im = Image.open(io.BytesIO(img))
        W, H = im.size
        w0, s0, e0, n0 = lo - zm, la - zm, lo + zm, la + zm
        pad = 0.4
        x0 = int(max(0, (box[0] - pad - w0) / (e0 - w0) * W))
        x1 = int(min(W, (box[2] + pad - w0) / (e0 - w0) * W))
        y0 = int(max(0, (n0 - (box[3] + pad)) / (n0 - s0) * H))
        y1 = int(min(H, (n0 - (box[1] - pad)) / (n0 - s0) * H))
        if x1 - x0 > 50 and y1 - y0 > 50:
            im = im.crop((x0, y0, x1, y1))
            out = io.BytesIO()
            im.save(out, "WEBP", quality=88, method=4)
            img = out.getvalue()
    except Exception:
        pass
    return img, box


def _fc_viewer(img: bytes, box) -> None:
    """The frame at full column width, its own aspect; wheel zooms
    about the cursor, drag pans, double-click resets."""
    import base64 as _b64
    mime = "image/webp" if img[:4] == b"RIFF" else "image/png"
    uri = f"data:{mime};base64," + _b64.b64encode(img).decode("ascii")
    w_deg = max(1.0, box[2] - box[0] + 0.8)
    h_deg = max(1.0, box[3] - box[1] + 0.8)
    # Column width is ~1600 px on the SOC wall; size the iframe from
    # the domain's aspect so there is no letterbox.
    px_h = int(min(1000, max(420, 1500 * h_deg / w_deg)))
    st_html(f"""
<div id="w" style="width:100%;aspect-ratio:{w_deg:.3f}/{h_deg:.3f};max-height:{px_h}px;
     margin:0 auto;overflow:hidden;background:#0b0c0e;border-radius:8px;
     cursor:grab;position:relative">
 <img id="m" src="{uri}" draggable="false"
      style="position:absolute;left:0;top:0;width:100%;height:100%;
             object-fit:contain;transform-origin:0 0;user-select:none">
</div>
<script>
(function(){{
  const w=document.getElementById('w'), m=document.getElementById('m');
  let s=1, tx=0, ty=0, drag=null;
  function apply(){{ m.style.transform=`translate(${{tx}}px,${{ty}}px) scale(${{s}})`; }}
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
}})();
</script>""", height=px_h + 6)


def _fc_colorbar(field: str) -> str:
    try:
        from core.cam_fast import PALETTES, _lut_for
        bounds = PALETTES[field]["bounds"]
        lut = _lut_for(field)
        cols = ["#%02x%02x%02x" % tuple(int(v) for v in c[:3])
                for c in lut[1:1 + len(bounds)]]
        cells = "".join(
            f'<div style="flex:1;text-align:center"><div style="height:8px;'
            f'background:{c}"></div>{bounds[i]:g}</div>'
            for i, c in enumerate(cols) if i < len(bounds))
        return (f'<div style="display:flex;font:11px Roboto Mono,DejaVu Sans Mono,'
                f'monospace;color:{_INK2};margin-top:4px;max-width:900px">{cells}'
                f'<div style="padding:8px 0 0 8px;white-space:nowrap">'
                f'{FC_UNITS.get(field, "")}</div></div>')
    except Exception:
        return ""


def render() -> None:
    """The whole Model forecast view: menus, slider, frame, legend."""
    from core.hrrr_cam import MODELS

    st.markdown(
        "<style>"
        "[data-testid='stVerticalBlockBorderWrapper']{background:#0A0A0A;"
        "border:1px solid #333 !important;border-radius:12px;padding:6px 10px}"
        "div[data-testid='stSlider'] label p{font-size:12pt}"
        "</style>", unsafe_allow_html=True)

    c1, c2, c3 = st.columns([1.3, 1.3, 2.4])
    with c1:
        m_label = st.selectbox("Model", [lbl for _k, lbl in FC_MODELS],
                               key="lsm_model")
    with c2:
        l_label = st.selectbox("Layer", [lbl for lbl, _k in FC_LAYERS],
                               key="lsm_layer")
    model = dict((lbl, k) for k, lbl in FC_MODELS)[m_label]
    field = dict(FC_LAYERS)[l_label]
    cfg = MODELS[model]

    max_h = min(FC_SLIDER_MAX, _model_max_fhr(model))
    # Clamp BEFORE the widget exists: switching RRFS (f84) -> HRRR
    # (f48) with the slider at f70 would otherwise raise out-of-range.
    if "lsm_fhr" not in st.session_state:
        st.session_state["lsm_fhr"] = 1
    st.session_state["lsm_fhr"] = max(0, min(int(st.session_state["lsm_fhr"]), max_h))
    fhr = int(st.slider("Forecast hour", 0, max_h, step=1, key="lsm_fhr",
                        format="f%d"))

    if field not in cfg["products"]:
        st.warning(f"{l_label} is not available from {cfg['label']}. "
                   "HRRR and RRFS carry the lightning threat field.")
        return

    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)
    cycle_iso = cached_cycle_for_hour(model, fhr, bucket10)
    if not cycle_iso:
        msg = f"No {cfg['label']} run has published f{fhr:02d} yet."
        if cfg.get("note"):
            msg += f" ({cfg['note']})"
        st.warning(msg)
        return
    cyc = datetime.fromisoformat(cycle_iso)
    valid = cyc + timedelta(hours=fhr)
    run_end = _run_max_fhr(model, cycle_iso)
    with c3:
        st.markdown(
            f'<div style="font:bold 12pt Roboto Mono,monospace;color:{_INK2};'
            f'text-align:right;margin-top:30px">{cfg["label"]} {cyc:%H}Z/{cyc:%d} '
            f'&middot; f{fhr:02d} &middot; valid {valid:%H}Z/{valid:%d} '
            f'&middot; run ends f{run_end:02d}</div>', unsafe_allow_html=True)

    try:
        with st.spinner(f"Rendering {cfg['label']} {l_label.lower()} f{fhr:02d}…"):
            img, box = cached_conus_frame(model, field, cycle_iso, fhr, FC_PPD)
        _fc_viewer(img, box)
        st.markdown(_fc_colorbar(field), unsafe_allow_html=True)
    except Exception as exc:
        st.error(f"{cfg['label']} {l_label}: {type(exc).__name__}: {str(exc)[:220]}")
        return

    st.caption(
        f"Frame edge is the {cfg['label']} grid edge - pixels past the "
        f"model's own domain are not drawn. Each hour shows the newest run "
        f"that has published it. Run lengths: RRFS f84 (3½ days), HRRR "
        f"f18 (f48 at 00/06/12/18Z), NAM nest f60 - the slider stops at the "
        f"model's last hour and extends automatically when a longer model "
        f"is added. Lightning is the model's composite lightning threat "
        f"(McCaul diagnostic), not observed strikes."
        + (f"  {cfg['label']}: {cfg['note']}." if cfg.get("note") else ""))


