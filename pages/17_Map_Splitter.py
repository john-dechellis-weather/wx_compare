"""Map Splitter (5 Oct): split the screen into panels - templates or
drawn splits, NVIDIA-desktop style - and put any page of the site in
each one.

Everything happens in static/map_splitter.html (layout editor,
"Add Data" catalogue, saved layouts in the browser). Panels embed the
site's own pages with ?embed=true (Homepage hides the top bar) and
the login token, so each panel is the real page with its own
controls. This file only hosts the iframe and passes the token.
"""

import hashlib
import os
from pathlib import Path

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

# The Add Data catalogue (9 Oct): written here from the site's own
# registries so the picker always matches what the pages can show.
import json
from core import tio_map as _TIO
try:
    from core import model_tiles as _MT
    _model_layers = [{"k": k, "n": _MT.field_label(k)} for k in _MT.field_keys()]
except Exception:
    _model_layers = []
_catalog = {
    "wm_sectors": [{"k": "", "n": "CONUS"}] + [{"k": k, "n": v[0]} for k, v in _TIO.SECTORS.items()],
    "wm_layers": ([{"k": f, "n": "tomorrow.io · " + _TIO.FIELD_LABEL.get(f, f)} for f in _TIO.FIELDS]
                  + [{"k": d["k"], "n": "NOAA · " + d["n"]} for d in _model_layers]),
}
_cat_path = Path(__file__).resolve().parent.parent / "static" / "splitter_catalog.json"
_cat_txt = json.dumps(_catalog)
if not _cat_path.exists() or _cat_path.read_text() != _cat_txt:
    _cat_path.write_text(_cat_txt)
# Cache-buster: the iframe URL carries a hash of the file, so a new
# version is always fetched (6 Oct: a stale copy kept the old catalogue).
_v = hashlib.md5((Path(__file__).resolve().parent.parent / "static" / "map_splitter.html")
                 .read_bytes()).hexdigest()[:10]
_src = (f"{_base}/app/static/map_splitter.html?v={_v}&base={_base}&cat={hashlib.md5(_cat_txt.encode()).hexdigest()[:8]}"
        + (f"&k={_tok}" if _tok else ""))
with st.container(key="bm-splitter"):
    components.iframe(_src, height=HEIGHT)
st.caption("Layouts and panels are kept in this browser (Save keeps named ones). "
           "Each panel is one table or map. "
           "Esc leaves a full-screen panel.")
