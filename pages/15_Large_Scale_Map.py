"""Large Scale Map - tomorrow.io tiles over CONUS (125W-66W, 24N-50N)
plus the Canadian alternates in range.

The frames are built by the tomorrow.io warmer in core/tio_map.py
(started from Homepage.py) and stitched into WebPs under static/.
This page only writes the MapLibre viewer to static/ and embeds it;
field, time step, opacity and station toggles are handled in the
browser, so nothing here reruns while the map is used. The fragment
re-runs every 60 s to pick up newly warmed frames.
"""

import os
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Weather Mapping", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

from core import tio_map as TIO

STATIC = Path(__file__).resolve().parent.parent / "static"

# The bridge: a hidden Streamlit component (static/tio_bridge) that
# relays the viewer iframe's "what am I looking at" messages to the
# server as its value -> TIO.set_demand. Same origin as the map
# iframe, so the two frames can talk.
from core.tio_bridge import tio_bridge as _bridge   # a real module: see core/tio_bridge.py
HEIGHT = int(os.environ.get("TIO_PAGE_HEIGHT", "860"))

# The warmer normally starts on Homepage; this is the belt to that
# brace for a direct deep link after a restart.
TIO.ensure_tio_warmer(STATIC)
try:
    from core import model_tiles as MT
    MT.ensure_model_warmer(STATIC)
except Exception:
    MT = None

# Two views behind one switch (28 Sep). TOMORROW.IO (default): the
# warmed tile map - five layers that stack, an hourly TIME slider to
# +24 h then 3-hourly to +72 h, one model - NextGen (a FOCUS switch
# appears if TIO_MODELS names both; see core/tio_map.py). Its 2-panel
# button opens a meteogram of the layers that are on for any station
# you click, from warmed tomorrow.io point forecasts. NOAA MODELS: a
# CONUS frame of RRFS / HRRR / NAM nest from core/model_map.py - the
# lightning source, since tomorrow.io tiles are spent on the other
# five fields.
# PANEL MODE (9 Oct): the Map Splitter opens one layer, optionally on a
# hi-res sector, with ?layer=<field | model:code>&sector=<key>. No
# title, view radio, sector picker or status expander - just the map.
_qp = st.query_params
PANEL_LAYER = (_qp.get("layer") or "").strip() or None
PANEL_SECTOR = (_qp.get("sector") or "").strip()
# Time Series Plot panel: ?ts=KXXX - the meteogram alone for one station
PANEL_TS = (_qp.get("ts") or "").strip().upper() or None
if PANEL_TS and not PANEL_LAYER:
    PANEL_LAYER = "__ts__"
if PANEL_LAYER:
    st.session_state["bm_embed"] = True
    st.markdown("<style>header,[data-testid='stHeader'],.bm-clock{display:none !important}"
                ".stApp .block-container{padding:2px 4px 0 4px !important;max-width:100% !important}"
                "</style>", unsafe_allow_html=True)
    if PANEL_SECTOR and PANEL_SECTOR in TIO.SECTORS and TIO.active_sector(STATIC) != PANEL_SECTOR:
        TIO.set_sector(STATIC, PANEL_SECTOR)      # site-wide: one sector warms at a time

_h1, _h2 = st.columns([2, 1.6])
with _h1:
  if not PANEL_LAYER:
    st.markdown(
        '<div style="font-size:16px;font-weight:700;color:#FFFFFF;'
        'margin:0 0 2px 0">WEATHER MAPPING</div>'
        '<div style="font-size:11px;font-weight:700;color:#B8B8B8;'
        'margin:0 0 8px 0">tomorrow.io layers &middot; NOAA model layers '
        '(REFS ensemble first; RRFS, HRRR, GFS to follow) &middot; hourly '
        'to +24 h, 3-hourly to +72 h &nbsp;|&nbsp; NOAA MODELS view: a CONUS '
        'frame of RRFS &middot; HRRR &middot; NAM nest incl. lightning</div>',
        unsafe_allow_html=True)
with _h2:
    view = "tomorrow.io" if PANEL_LAYER else st.radio(
        "View", ["tomorrow.io", "NOAA models"], horizontal=True, key="lsm_view",
        label_visibility="collapsed")

if view == "NOAA models":
    from core import model_map as _MM

    _MM.render()
    st.stop()

# Model switch: a page control, not a per-viewer toggle, because the
# warmer serves ONE model and a switch re-warms every frame (~1,280
# requests). Shown only when TIO_MODELS names more than one.
_active = TIO.active_model(STATIC)
if len(TIO.MODELS) > 1 and not PANEL_LAYER:
    _m1, _m2 = st.columns([1.2, 3])
    with _m1:
        _pick = st.radio("Model", list(TIO.MODELS),
                         index=list(TIO.MODELS).index(_active),
                         format_func=TIO.model_label, horizontal=True,
                         key="tio_model_pick")
    if _pick != _active:
        _u = TIO.usage(STATIC)
        _cost = (TIO.tile_count(TIO.FCST_ZOOM) * len(TIO.FCST_HOURS)
                 + TIO.tile_count(TIO.NOW_ZOOM)) * len(TIO.FIELDS)
        with _m2:
            if _u["count"] + _cost > TIO.DAILY_CAP:
                st.warning(f"Switching to {TIO.model_label(_pick)} needs "
                           f"~{_cost:,} requests; {_u['count']:,} of "
                           f"{TIO.DAILY_CAP:,} already used today - the "
                           "warmer would stop at the cap. It switches "
                           "anyway at 00Z.")
            else:
                st.caption(f"Switching to {TIO.model_label(_pick)}: the "
                           f"warmer re-warms all frames (~{_cost:,} "
                           "requests); the nearest hours arrive first.")
        TIO.set_model(STATIC, _pick)
        _active = _pick

# High-resolution sector: one at a time, warmed at zoom 6 on the "now"
# cadence for TIO_HIRES_FIELDS; "None" spends nothing.
_sector = TIO.active_sector(STATIC)
if TIO.HIRES_ON and TIO.SECTORS and not PANEL_LAYER:
    _s1, _s2 = st.columns([1.2, 3])
    _opts = [""] + list(TIO.SECTORS)
    with _s1:
        _sp = st.selectbox("Hi-res sector", _opts,
                           index=_opts.index(_sector) if _sector in _opts else 0,
                           format_func=lambda k: "None" if not k else TIO.SECTORS[k][0],
                           key="tio_sector_pick")
    with _s2:
        if _sp:
            st.caption(f"{TIO.SECTORS[_sp][0]}: zoom {TIO.HIRES_ZOOM} "
                       f"({TIO.tile_count(TIO.HIRES_ZOOM, TIO.SECTORS[_sp][1])} tiles) "
                       f"for {', '.join(TIO.HIRES_FIELDS)} every {TIO.HIRES_MIN} min "
                       f"\u2248 +{TIO.sector_estimate(_sp):,} requests/day, shown "
                       "over the CONUS frame once the map is zoomed past 5.5.")
    if _sp != _sector:
        TIO.set_sector(STATIC, _sp)
        _sector = _sp


def _origin() -> str:
    """Same-origin base URL for /app/static images."""
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


@st.fragment(run_every=60)
def _body():
    base = _origin()
    TIO.note_view(STATIC)        # viewer heartbeat: keeps the warmer awake
    _dm = _bridge(key="tio_bridge", default=None)
    if _dm:
        if _dm.get("kind") == "point" and MT is not None:
            MT.point_request(STATIC, _dm)      # a map click: readout
        else:
            TIO.set_demand(STATIC, _dm)
    man = TIO.manifest(STATIC)
    # man[_active] holds one {step: entry} dict per field, PLUS a
    # "hires:<hub>" key nested one level deeper ({field: {step: entry}})
    # for the sector pass. Excluded here and below so a hi-res sector
    # doesn't get its field dicts mistaken for frame entries.
    _fc = {k: v for k, v in (man.get(_active) or {}).items()
           if not k.startswith("hires:")}
    n = sum(len(v) for v in _fc.values())
    if TIO.DEMO:
        st.warning("TIO_DEMO=on: synthetic tiles and station series, no "
                   "tomorrow.io calls. Remove the env var for live data.")
    elif not TIO.api_key():
        st.error("TOMORROWIO_API_KEY is not set - the warmer cannot "
                 "fetch tiles.")
    elif n == 0:
        st.info(f"No {TIO.model_label(_active)} frames yet - the warmer "
                "fetches the current frame for the layers that are on, then "
                "each hour as the slider asks for it (about a second per "
                "frame)."
                + (f"  Last error: {TIO.STATUS['err']}"
                   if TIO.STATUS.get("err") else "")
                + ((f"  tomorrow.io rate limit: the warmer resumes in "
                    + (f"{TIO.in_backoff() / 3600:.1f} h" if TIO.in_backoff() > 3600
                       else f"{TIO.in_backoff() / 60:.0f} min") + ".")
                   if TIO.in_backoff() else ""))
    try:
        _pt = TIO.points(STATIC, _active)
        _no = TIO.noaa(STATIC)
        html = TIO.map_html(man, base, TIO.stations_geojson(STATIC),
                            height=HEIGHT, model=_active,
                            pt_built=_pt.get("built", ""),
                            noaa_built=_no.get("built", ""),
                            sector=_sector,
                            preset=[PANEL_LAYER] if PANEL_LAYER and not PANEL_TS else None,
                            fit=(TIO.SECTORS[PANEL_SECTOR][1]
                                 if PANEL_LAYER and PANEL_SECTOR in TIO.SECTORS else None),
                            mode=("ts" if PANEL_TS else ("map" if PANEL_LAYER else "")),
                            station=PANEL_TS or "")
        # Panels get their own file (one layer, one sector / one station);
        # the page keeps tio_map.html.
        name = (f"tio_map_ts_{PANEL_TS}.html" if PANEL_TS
                else f"tio_map_{PANEL_LAYER.replace(':', '-')}_{PANEL_SECTOR or 'conus'}.html"
                if PANEL_LAYER else "tio_map.html")
        # Rewritten only when the page itself changes (model, sector);
        # new frames reach the open viewer through its own manifest
        # poll, so the iframe is never reloaded under the user.
        import hashlib
        v = hashlib.md5(html.encode()).hexdigest()[:10]
        if (not (STATIC / name).exists()
                or (STATIC / name).read_text() != html):
            tmp = STATIC / f".{name}.tmp"
            tmp.write_text(html)
            os.replace(tmp, STATIC / name)
        components.iframe(f"{base}/app/static/{name}?v={v}",
                          height=HEIGHT)
    except Exception as exc:
        st.error(f"Map failed to render: {type(exc).__name__}: {exc}")


_body()

if PANEL_LAYER:
    st.stop()

with st.expander("tomorrow.io warmer status", expanded=False):
    u = TIO.usage(STATIC)
    st.caption(f"Requests today ({u['day']} UTC): {u['count']:,} / "
               f"{TIO.DAILY_CAP:,} cap  |  model {TIO.model_label(_active)}  |  "
               f"fields {', '.join(TIO.FIELDS)}  |  "
               f"now z{TIO.NOW_ZOOM} ({TIO.tile_count(TIO.NOW_ZOOM)} tiles) "
               f"every {TIO.NOW_MIN} min  |  forecast {len(TIO.FCST_HOURS)} steps "
               f"to +{TIO.FCST_HOURS[-1] if TIO.FCST_HOURS else 0} h "
               f"z{TIO.FCST_ZOOM} ({TIO.tile_count(TIO.FCST_ZOOM)} tiles each) "
               f"every {TIO.FCST_MIN} min  |  hi-res sector "
               f"{TIO.SECTORS[_sector][0] if _sector in TIO.SECTORS else 'none'}"
               f"  |  on demand: ~{TIO.daily_estimate():,}/day with two layers in "
               "use all day and 40 scrubbed frames")
    _b = TIO.budget(STATIC)
    if _b.get("by"):
        st.caption("Spent on: " + "  |  ".join(
            f"{k} {v:,}" for k, v in sorted(_b["by"].items(), key=lambda kv: -kv[1]))
            + f"  (cap {TIO.CAP_PCT:.0f}% of the {TIO.PLAN_DAY:,}/day plan)")
    st.caption(f"Budget: {_b['left']:,} left today, of which {_b['mandatory_left']:,} "
               f"is owed to the hourly now-frames and {_b['reserve']:,} is the reserve "
               f"→ {_b['free']:,} free for forecast / points / hi-res  |  this hour "
               f"{_b['hour_used']:,} / {_b['hour_cap']:,}  |  "
               + ("nobody viewing for %.0f min (paused)" % _b["idle_min"]
                  if _b["idle_min"] > TIO.IDLE_MIN else "viewer present"))
    if TIO.STATUS.get("err"):
        st.error(TIO.STATUS["err"])
    lines = TIO.log_tail(STATIC, 15)
    st.code("\n".join(lines) or "(no log yet)")
    try:
        from core import model_tiles as _MT
        st.caption(f"Model layers: {', '.join(_MT.MODELS_ON) or 'none'} "
                   f"({len(_MT.field_keys())} layers) | "
                   + (f"building {_MT.STATUS['busy']}" if _MT.STATUS.get("busy") else "idle")
                   + (f" | last error: {_MT.STATUS['err']}" if _MT.STATUS.get("err") else ""))
        st.code("\n".join(_MT.log_tail(STATIC, 10)) or "(no model log yet)")
    except Exception as _me:
        st.caption(f"model layers unavailable: {_me}")
