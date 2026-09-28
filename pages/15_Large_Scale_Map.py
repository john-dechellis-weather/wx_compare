"""Large Scale Map - tomorrow.io tiles over every JetBlue destination
except Europe (125W-50W, 52N-5S) plus the Canadian alternates.

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

st.set_page_config(page_title="Large Scale Map", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

from core import tio_map as TIO

STATIC = Path(__file__).resolve().parent.parent / "static"
HEIGHT = int(os.environ.get("TIO_PAGE_HEIGHT", "860"))

# The warmer normally starts on Homepage; this is the belt to that
# brace for a direct deep link after a restart.
TIO.ensure_tio_warmer(STATIC)

# Two views behind one switch (28 Sep). TOMORROW.IO (default): the
# warmed tile map - five layers that stack, an hourly TIME slider to
# +48 h then 3-hourly to +72 h, one model at a time (FOCUS / NextGen
# once TIO_MODELS names them; see core/tio_map.py). NOAA MODELS: a
# CONUS frame of RRFS / HRRR / NAM nest from core/model_map.py - the
# lightning source, since tomorrow.io tiles are spent on the other
# five fields.
_h1, _h2 = st.columns([2, 1.6])
with _h1:
    st.markdown(
        '<div style="font-size:16px;font-weight:700;color:#FFFFFF;'
        'margin:0 0 2px 0">LARGE SCALE MAP</div>'
        '<div style="font-size:11px;font-weight:700;color:#B8B8B8;'
        'margin:0 0 8px 0">tomorrow.io: precipitation &middot; ceiling '
        '&middot; visibility &middot; wind speed &middot; wind gust, hourly '
        'to +48 h, 3-hourly to +72 h &nbsp;|&nbsp; NOAA models: RRFS '
        '&middot; HRRR &middot; NAM nest incl. lightning</div>',
        unsafe_allow_html=True)
with _h2:
    view = st.radio("View", ["tomorrow.io", "NOAA models"],
                    horizontal=True, key="lsm_view",
                    label_visibility="collapsed")

if view == "NOAA models":
    from core import model_map as _MM

    _MM.render()
    st.stop()

# Model switch: a page control, not a per-viewer toggle, because the
# warmer serves ONE model and a switch re-warms every frame (~1,760
# requests). Shown only when TIO_MODELS names more than one.
_active = TIO.active_model(STATIC)
if len(TIO.MODELS) > 1:
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
    man = TIO.manifest(STATIC)
    n = sum(len(v) for v in (man.get(_active) or {}).values())
    if not TIO.api_key():
        st.error("TOMORROWIO_API_KEY is not set - the warmer cannot "
                 "fetch tiles.")
    elif n == 0:
        st.info(f"No {TIO.model_label(_active)} frames yet - the warmer "
                "builds the current hour for every layer first, then walks "
                "out through the forecast (about a minute per hour of "
                "forecast at zoom 3). Refresh in a minute."
                + (f"  Last error: {TIO.STATUS['err']}"
                   if TIO.STATUS.get("err") else ""))
    try:
        html = TIO.map_html(man, base, TIO.stations_geojson(STATIC),
                            height=HEIGHT, model=_active)
        name = "tio_map.html"
        tmp = STATIC / f".{name}.tmp"
        tmp.write_text(html)
        os.replace(tmp, STATIC / name)
        newest = max((e["built"] for v in (man.get(_active) or {}).values()
                      for e in v.values()), default="none")
        components.iframe(f"{base}/app/static/{name}?v={newest}",
                          height=HEIGHT)
    except Exception as exc:
        st.error(f"Map failed to render: {type(exc).__name__}: {exc}")


_body()

with st.expander("tomorrow.io warmer status", expanded=False):
    u = TIO.usage(STATIC)
    st.caption(f"Requests today ({u['day']} UTC): {u['count']:,} / "
               f"{TIO.DAILY_CAP:,} cap  |  model {TIO.model_label(_active)}  |  "
               f"fields {', '.join(TIO.FIELDS)}  |  "
               f"now z{TIO.NOW_ZOOM} ({TIO.tile_count(TIO.NOW_ZOOM)} tiles) "
               f"every {TIO.NOW_MIN} min  |  forecast {len(TIO.FCST_HOURS)} steps "
               f"to +{TIO.FCST_HOURS[-1] if TIO.FCST_HOURS else 0} h "
               f"z{TIO.FCST_ZOOM} ({TIO.tile_count(TIO.FCST_ZOOM)} tiles each) "
               f"every {TIO.FCST_MIN} min  |  ~{TIO.daily_estimate():,}/day")
    if TIO.STATUS.get("err"):
        st.error(TIO.STATUS["err"])
    lines = TIO.log_tail(STATIC, 15)
    st.code("\n".join(lines) or "(no log yet)")
