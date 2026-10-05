"""Map Splitter (5 Oct): split the screen into panels - templates or
drawn splits, NVIDIA-desktop style - and put any page of the site in
each one.

Everything happens in static/map_splitter.html (layout editor,
"Add Data" catalogue, saved layouts in the browser). Panels embed the
site's own pages with ?embed=true (Homepage hides the top bar) and
the login token, so each panel is the real page with its own
controls. This file only hosts the iframe and passes the token.
"""

import os

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Map Splitter", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

HEIGHT = int(os.environ.get("BLUEMET_SPLITTER_H", "940"))


def _origin() -> str:
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


# The page fills the window below the top bar: no block padding, the
# iframe as tall as the viewport allows.
st.markdown(
    "<style>.stApp .block-container{padding:0 .4rem 0 .4rem !important;max-width:100% !important}"
    ".bm-splitter iframe{border:1px solid #333;border-radius:4px}</style>",
    unsafe_allow_html=True)

_base = _origin()
_tok = st.query_params.get("k", "")
_src = f"{_base}/app/static/map_splitter.html?base={_base}" + (f"&k={_tok}" if _tok else "")
with st.container(key="bm-splitter"):
    components.iframe(_src, height=HEIGHT)
st.caption("Layouts and panels are kept in this browser (Save keeps named ones). "
           "Panels are live pages: pick stations and products inside each one. "
           "Esc leaves a full-screen panel.")
