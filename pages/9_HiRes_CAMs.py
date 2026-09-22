"""Hi-Res CAMs - four square pods (22 Sep).

Top row Northeast, bottom row Florida, the same layout as the REFS
page. Each pod picks its own model + product; one run toggle and one
forecast-hour slider (between the rows) drive all four; a pod-size
slider scales the maps. Frames come from the warm store when the hour
is warmed and render on demand otherwise. The renderer draws the dark
ground and the N90 outline (core/cam_fast.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="BlueMet - Hi-Res CAMs", layout="wide")

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
    HUB_LABELS as _HUB_LABELS, ensure_warmer_started, hub_geom as _hub_geom,
    warm_get, warm_hours, warm_status,
)
from core.hrrr_cam import MODELS

ensure_warmer_started(CACHE_ROOT)

st.title("Hi-Res CAMs")
st.caption("HRRR (hourly, 18 h; 48 h at 00/06/12/18Z) and RRFS (3-hourly, "
           "84 h). Top row Northeast, bottom row Florida.")

# ---------------------------------------------------------------- choices
_MODEL_LABEL = {"hrrr": "HRRR", "rrfs": "RRFS"}
_PRODUCT_LABEL = {"REFD": "1 km reflectivity", "REFC": "Composite reflectivity",
                  "RETOP": "Echo tops", "VIS": "Visibility", "CEIL": "Ceiling",
                  "GUST": "10 m wind gust"}
# Every model x product the page offers, as "HRRR · Composite reflectivity".
PRODUCTS = {}
for _m in ("hrrr", "rrfs"):
    for _p in ("REFD", "REFC", "RETOP", "VIS", "CEIL", "GUST"):
        if _p in MODELS[_m]["products"]:
            PRODUCTS[f"{_MODEL_LABEL[_m]} \u00b7 {_PRODUCT_LABEL[_p]}"] = (_m, _p)
_POD_DEFAULTS = ["HRRR \u00b7 1 km reflectivity", "RRFS \u00b7 Composite reflectivity",
                 "HRRR \u00b7 1 km reflectivity", "RRFS \u00b7 Composite reflectivity"]
_POD_REGION = ["NE", "NE", "FL", "FL"]

# Run choice -> the forecast hour a run must have reached. HRRR runs
# to 18 every hour and to 48 at 00/06/12/18Z; RRFS to 84 every run.
_RUNS = {"Latest run": {"hrrr": 1, "rrfs": 1},
         "Latest long run (HRRR 48 h / RRFS 84 h)": {"hrrr": 48, "rrfs": 84}}

_PANEL, _EDGE, _INK, _INK2 = "#0A0A0A", "#333333", "#FFFFFF", "#B8B8B8"
st.markdown(
    "<style>"
    "[data-testid='stVerticalBlockBorderWrapper']{"
    f"background:{_PANEL};border:1px solid {_EDGE} !important;"
    "border-radius:12px;padding:6px 10px}"
    "</style>", unsafe_allow_html=True)

now = datetime.now(timezone.utc)
bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

_h1, _h2 = st.columns([2.6, 1.4])
with _h1:
    run_choice = st.radio("Run", list(_RUNS), horizontal=True, key="cam_run",
                          label_visibility="collapsed")
with _h2:
    pod_pct = st.slider("Pod size", 40, 100, 100, 5, key="cam_pod_pct",
                        format="%d%%")


@st.cache_data(ttl=600, show_spinner=False, max_entries=48)
def _cycle_for(model: str, need_fhr: int, bucket: str):
    """Newest cycle of a model that has reached need_fhr: the warmer's
    manifest when it covers the hour (a disk stat), else a probe."""
    try:
        if need_fhr <= max(warm_hours(model)):
            wc = warm_status(CACHE_ROOT).get(model)
            if wc:
                return wc
    except Exception:
        pass
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(model, need_fhr)
    return cyc.isoformat() if cyc else None


_cycles = {m: _cycle_for(m, _RUNS[run_choice][m], bucket10)
           for m in ("hrrr", "rrfs")}


def _max_fhr(model: str, cycle_iso: str) -> int:
    if model == "hrrr":
        return 48 if datetime.fromisoformat(cycle_iso).hour in (0, 6, 12, 18) else 18
    return MODELS[model]["max_fhr"]


_maxes = [_max_fhr(m, c) for m, c in _cycles.items() if c]
max_fhr = max(_maxes) if _maxes else 18
fhr = int(st.session_state.get("cam_fhr", 1) or 1)
fhr = max(1, min(fhr, max_fhr))


@st.cache_data(ttl=10800, show_spinner=False, max_entries=800)
def cached_cam_frame(model: str, field: str, cycle_iso: str, h: int,
                     la: float, lo: float, zm: float):
    from core.hrrr_cam import fetch_and_decode, render_frame
    _c = datetime.fromisoformat(cycle_iso)
    vals, lats, lons = fetch_and_decode(model, field, _c, h, la, lo, zm)
    return render_frame(field, vals, lats, lons, la, lo, zm, "",
                        grid_key=f"{la:.2f},{lo:.2f},{zm:.2f}|{model}",
                        cache_root=CACHE_ROOT / "cam_warm")


def _frame(region: str, model: str, field: str, h: int, cycle_iso: str):
    got = warm_get(CACHE_ROOT, f"{model}@{field}", region, h)
    if got and got[1] == cycle_iso:
        return got[0], "warm"
    clat, clon, zm = _hub_geom(region)
    return cached_cam_frame(model, field, cycle_iso, h, round(clat, 2),
                            round(clon, 2), zm), "live"


def _viewer(img: bytes, height: int = 560) -> None:
    """Wheel over the map zooms about the cursor, drag pans,
    double-click resets."""
    import base64 as _b64
    import streamlit.components.v1 as _components
    mime = "image/webp" if img[:4] == b"RIFF" else "image/png"
    uri = f"data:{mime};base64," + _b64.b64encode(img).decode("ascii")
    _components.html(f"""
<div id="w" style="width:100%;height:{height}px;overflow:hidden;
     background:#0b0c0e;border-radius:8px;cursor:grab;position:relative">
 <img id="m" src="{uri}" draggable="false"
      style="position:absolute;left:0;top:0;width:100%;height:100%;
             transform-origin:0 0;user-select:none">
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
</script>""", height=height + 4)


def _colorbar(field: str) -> str:
    try:
        from core.cam_fast import PALETTES, _lut_for
        spec = PALETTES[field]
        bounds = spec["bounds"]
        lut = _lut_for(field)
        cols = ["#%02x%02x%02x" % tuple(int(v) for v in c[:3])
                for c in lut[1:1 + len(bounds)]]
        unit = {"REFD": "dBZ", "REFC": "dBZ", "RETOP": "kft", "VIS": "sm",
                "CEIL": "x100 ft", "GUST": "kt"}.get(field, "")
        cells = "".join(
            f'<div style="flex:1;text-align:center"><div style="height:8px;'
            f'background:{c}"></div>{bounds[i]:g}</div>'
            for i, c in enumerate(cols) if i < len(bounds))
        return (f'<div style="display:flex;font:11px DejaVu Sans Mono,'
                f'monospace;color:{_INK2};margin-top:4px">{cells}'
                f'<div style="padding:8px 0 0 6px">{unit}</div></div>')
    except Exception:
        return ""


def _pod(i: int):
    region = _POD_REGION[i]
    with st.container(border=True):
        c1, c2 = st.columns([1.3, 1])
        with c1:
            label = st.selectbox(
                "Model and product", list(PRODUCTS),
                index=list(PRODUCTS).index(
                    st.session_state.get(f"cam_pod{i}", _POD_DEFAULTS[i])),
                key=f"cam_pod{i}", label_visibility="collapsed")
        model, field = PRODUCTS[label]
        cycle_iso = _cycles.get(model)
        if not cycle_iso:
            st.warning(f"{_MODEL_LABEL[model]}: no cycle found for that run.")
            return
        cyc = datetime.fromisoformat(cycle_iso)
        h = fhr
        mx = _max_fhr(model, cycle_iso)
        note = ""
        if h > mx:
            h, note = mx, f" (run ends at f{mx:02d})"
        if model == "rrfs" and h % 3:
            h, note = (h // 3) * 3 or 3, " (RRFS is 3-hourly)"
        valid = cyc + timedelta(hours=h)
        with c2:
            st.markdown(
                f'<div style="font:bold 12px DejaVu Sans Mono,monospace;'
                f'color:{_INK2};text-align:right;margin-top:10px">'
                f'{_HUB_LABELS.get(region, region).split(" and ")[0]} '
                f'&middot; {_MODEL_LABEL[model]} {cyc:%H}Z/{cyc:%d} '
                f'&middot; f{h:02d} &middot; valid {valid:%H}Z/{valid:%d}'
                f'{note}</div>', unsafe_allow_html=True)
        try:
            with st.spinner(""):
                img, src = _frame(region, model, field, h, cycle_iso)
            _viewer(img, height=int(560 * pod_pct / 100))
            st.markdown(_colorbar(field), unsafe_allow_html=True)
            if src == "live":
                st.caption("rendered on demand (not yet in the warm store)")
        except Exception as exc:
            st.warning(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")


_side = max(0.001, (100 - pod_pct) / 2)
_spec = [_side, pod_pct / 2, pod_pct / 2, _side]

_r1 = st.columns(_spec, gap="small")
with _r1[1]:
    _pod(0)
with _r1[2]:
    _pod(1)

_s1, _s2, _s3 = st.columns([_side, pod_pct, _side], gap="small")
with _s2:
    _sa, _sb = st.columns([5, 1.2])
    with _sa:
        st.slider("Forecast hour", 1, max_fhr, min(fhr, max_fhr),
                  key="cam_fhr", label_visibility="collapsed")
    with _sb:
        st.markdown(
            f'<div style="font:bold 13px DejaVu Sans Mono,monospace;'
            f'color:{_INK2};margin-top:10px">f{fhr:02d}</div>',
            unsafe_allow_html=True)

_r2 = st.columns(_spec, gap="small")
with _r2[1]:
    _pod(2)
with _r2[2]:
    _pod(3)

st.caption(
    "One run and one forecast hour for all four pods; RRFS pods snap to "
    "the nearest 3-hourly frame. Warmed hours open instantly; others "
    "render on demand and are kept for three hours. The red outline is "
    "the N90 extent.")
