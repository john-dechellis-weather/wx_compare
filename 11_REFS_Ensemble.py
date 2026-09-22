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


# ---------------------------------------------------------------------------
# Pods (22 Sep): four square maps, each with its own product, one run
# toggle and one valid-hour slider shared by all four. Frames come
# from the warm store when the hour is warmed (instant) and render on
# demand otherwise. The renderer draws the dark ground and the N90
# outline; see core/cam_fast.py.
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
_POD_DEFAULTS = ["PMMN composite reflectivity",
                 "Probability REFC \u2265 40 dBZ",
                 "Probability ceiling < 1000 ft",
                 "Probability visibility < 3 sm"]

# Run choice -> the forecast hour a run must have reached to count.
# REFS runs 00Z and 12Z to f60, 06Z and 18Z to f48.
_RUNS = {"Latest run": 1,
         "Latest 60-hr run (00Z/12Z)": 60,
         "Latest 48-hr run": 48}

_PANEL, _EDGE, _INK, _INK2 = "#0A0A0A", "#333333", "#FFFFFF", "#B8B8B8"
st.markdown(
    "<style>"
    "[data-testid='stVerticalBlockBorderWrapper']{"
    f"background:{_PANEL};border:1px solid {_EDGE} !important;"
    "border-radius:12px;padding:6px 10px}"
    "div[data-testid='stImage'] img{border-radius:8px}"
    "</style>", unsafe_allow_html=True)

# Top two pods are the Northeast, bottom two Florida (22 Sep): both
# regions on one page instead of a region switch.
_POD_REGION = ["NE", "NE", "FL", "FL"]
from core.cam_warm import hub_geom as _hub_geom
now = datetime.now(timezone.utc)
bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

# ---- run toggle + shared hour -------------------------------------------
_h1, _h2 = st.columns([2.6, 1.4])
with _h1:
    run_choice = st.radio("Run", list(_RUNS), horizontal=True,
                          key="refs_run", label_visibility="collapsed")
with _h2:
    # Pod size: the maps' display width as a share of the page. The
    # rows stay two across; the pair shrinks toward the centre.
    pod_pct = st.slider("Pod size", 40, 100, 100, 5, key="refs_pod_pct",
                        format="%d%%")
need_fhr = _RUNS[run_choice]


@st.cache_data(ttl=600, show_spinner=False, max_entries=24)
def _cycle_for(need_fhr: int, bucket: str):
    """The newest REFS cycle that has reached need_fhr, from the
    warm store when it has one (no probing), else by probing."""
    if need_fhr <= 1:
        _wc = warm_cycle(CACHE_ROOT, "refs_prob@PROB_REFC40")
        if _wc:
            return _wc
    return cached_refs_cycle("refs_prob", need_fhr, bucket)


cycle_iso = _cycle_for(need_fhr, bucket10)
if cycle_iso is None:
    st.warning("No REFS cycle found for that run length yet.")
    st.stop()
cyc = datetime.fromisoformat(cycle_iso)
max_fhr = 60 if cyc.hour in (0, 12) else 48
# The hour slider is read here (its widget is drawn between the two
# rows, below) so every pod on this run shows the same hour.
fhr = int(st.session_state.get("refs_fhr", 1) or 1)
fhr = max(1, min(fhr, max_fhr))
valid = cyc + timedelta(hours=fhr)


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


def _viewer(img: bytes, key: str, height: int = 560) -> None:
    """The frame in a small pan/zoom viewer: wheel over the map zooms
    about the cursor, drag pans, double-click resets. The image is
    square, so the viewer is too."""
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
    // keep the point under the cursor fixed
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
    """The fast renderer's palette for a product, as a strip."""
    try:
        from core.cam_fast import PALETTES, _lut_for
        spec = PALETTES.get(field) or PALETTES["PROB"]
        bounds = spec["bounds"]
        # _lut_for gives the exact colours the frame used, including
        # metpy's reflectivity table; index 0 is transparent.
        lut = _lut_for(field if field in PALETTES else "PROB")
        cols = ["#%02x%02x%02x" % tuple(int(v) for v in c[:3])
                for c in lut[1:1 + len(bounds)]]
        unit = ("dBZ" if field == "REFC" else "%")
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
                "Product", list(PRODUCTS), index=list(PRODUCTS).index(
                    st.session_state.get(f"refs_pod{i}", _POD_DEFAULTS[i])),
                key=f"refs_pod{i}", label_visibility="collapsed")
        with c2:
            st.markdown(
                f'<div style="font:bold 12px DejaVu Sans Mono,monospace;'
                f'color:{_INK2};text-align:right;margin-top:10px">'
                f'{_HUB_LABELS.get(region, region).split(" and ")[0]} '
                f'&middot; REFS {cyc:%H}Z/{cyc:%d} &middot; f{fhr:02d} '
                f'&middot; valid {valid:%H}Z/{valid:%d}</div>',
                unsafe_allow_html=True)
        model, field = PRODUCTS[label]
        try:
            with st.spinner(""):
                img, src = _frame(region, model, field, fhr)
            _viewer(img, key=f"v{i}_{fhr}_{field}_{region}",
                    height=int(560 * pod_pct / 100))
            st.markdown(_colorbar(field), unsafe_allow_html=True)
            if src == "live":
                st.caption("rendered on demand (not yet in the warm store)")
        except Exception as exc:
            st.warning(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")


# Two pods per row at pod_pct of the page width, centred: a spacer
# column each side takes up the rest.
_side = max(0.001, (100 - pod_pct) / 2)
_spec = [_side, pod_pct / 2, pod_pct / 2, _side]

_r1 = st.columns(_spec, gap="small")
with _r1[1]:
    _pod(0)
with _r1[2]:
    _pod(1)

# THE HOUR, between the rows: one slider for all four pods.
_s1, _s2, _s3 = st.columns([_side, pod_pct, _side], gap="small")
with _s2:
    _sa, _sb = st.columns([5, 1.2])
    with _sa:
        st.slider("Forecast hour", 1, max_fhr, min(fhr, max_fhr),
                  key="refs_fhr", label_visibility="collapsed")
    with _sb:
        st.markdown(
            f'<div style="font:bold 13px DejaVu Sans Mono,monospace;'
            f'color:{_INK2};margin-top:10px">f{fhr:02d} &middot; valid '
            f'<span style="color:{_INK}">{valid:%HZ %d %b}</span></div>',
            unsafe_allow_html=True)

_r2 = st.columns(_spec, gap="small")
with _r2[1]:
    _pod(2)
with _r2[2]:
    _pod(3)

st.caption(
    "Top row Northeast, bottom row Florida. One run and one valid hour "
    "for all four pods. Warmed hours open "
    "instantly; others render on demand and are kept for three hours. "
    "The yellow outline is the N90 extent.")
