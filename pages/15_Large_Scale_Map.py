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

st.markdown(
    '<div style="font-size:16px;font-weight:700;color:#FFFFFF;'
    'margin:0 0 2px 0">LARGE SCALE MAP</div>'
    '<div style="font-size:11px;font-weight:700;color:#B8B8B8;'
    'margin:0 0 8px 0">tomorrow.io weather tiles &middot; JetBlue '
    'network (ex-Europe) and Canadian alternates &middot; '
    'now and forecast steps</div>',
    unsafe_allow_html=True)


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
    n = sum(len(v) for v in man.values())
    if not TIO.api_key():
        st.error("TOMORROWIO_API_KEY is not set - the warmer cannot "
                 "fetch tiles.")
    elif n == 0:
        st.info("No frames yet - the tomorrow.io warmer builds the first "
                "set about 10 s after the app starts (64 tiles for the "
                "current frame, 16 per forecast step). Refresh in a "
                "minute." + (f"  Last error: {TIO.STATUS['err']}"
                              if TIO.STATUS.get("err") else ""))
    try:
        html = TIO.map_html(man, base, TIO.stations_geojson(STATIC),
                            height=HEIGHT)
        name = "tio_map.html"
        tmp = STATIC / f".{name}.tmp"
        tmp.write_text(html)
        os.replace(tmp, STATIC / name)
        newest = max((e["built"] for v in man.values() for e in v.values()),
                     default="none")
        components.iframe(f"{base}/app/static/{name}?v={newest}",
                          height=HEIGHT)
    except Exception as exc:
        st.error(f"Map failed to render: {type(exc).__name__}: {exc}")


_body()

with st.expander("tomorrow.io warmer status", expanded=False):
    u = TIO.usage(STATIC)
    st.caption(f"Requests today ({u['day']} UTC): {u['count']} / "
               f"{TIO.DAILY_CAP} cap  |  fields {', '.join(TIO.FIELDS)}  |  "
               f"now z{TIO.NOW_ZOOM} ({TIO.tile_count(TIO.NOW_ZOOM)} tiles) "
               f"every {TIO.NOW_MIN} min  |  forecast "
               f"{', '.join('+%d' % h for h in TIO.FCST_HOURS)} h z{TIO.FCST_ZOOM} "
               f"({TIO.tile_count(TIO.FCST_ZOOM)} tiles each) every "
               f"{TIO.FCST_MIN} min")
    if TIO.STATUS.get("err"):
        st.error(TIO.STATUS["err"])
    lines = TIO.log_tail(STATIC, 15)
    st.code("\n".join(lines) or "(no log yet)")
