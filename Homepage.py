"""Landing page for the wx_compare suite.

The actual tools live in pages/ and Streamlit auto-discovers them.
"""

import streamlit as st

import time
from pathlib import Path

def _cleanup_old_cache():
    """Delete cache files older than 3 days to prevent disk fill."""
    cache_root = Path("/opt/render/project/src/cache")
    if not cache_root.exists():
        return
    cutoff = time.time() - (24 * 3600)  # 1 days
    for p in cache_root.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            continue

# Run cleanup once per session
if "_cache_cleanup_done" not in st.session_state:
    _cleanup_old_cache()
    st.session_state["_cache_cleanup_done"] = True

st.set_page_config(
    page_title="BlueMet",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.set_page_config(
    page_title="Homepage",
    layout="wide",
    initial_sidebar_state="expanded",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()

st.title("BlueMet")
st.markdown(
    "<p style='color: #B30000; font-size: 32px; font-weight: bold;'>"
    "IMPORTANT: Use Prohbited outside of the JetBlue SOC or for Tomorrow.io employees"
    "</p>",
    unsafe_allow_html=True,
)
st.caption("Multi-model comparison for CONUS airports.")

st.markdown(
    """
    ### Available tools

    - **VIS/CIG Comparison Tool** — visibility and ceiling model data 
    - **Wind Comparison Tool** — wind speed, direction, and wind gust model data
    - **Situational Awarness Tool** — flags low ceilings, visibility, and TSRA based on NWS TAFs

    Select a tool from the sidebar to start. Do not share this site URL outside of the SOC team. 

    ### Models Included

    | Source | Forecast Range | Resolution |
    | --- | --- | --- |
    | HRRR | 0-18 | hourly |
    | GFS MOS (MAV) | 6-72 | 3-hourly |
    | GFS LAMP (LAV) | 1-25 | hourly |
    | NBM (NBH + NBS) | 1-72 | hourly + 3-hourly |
    | Tomorrow.io | 1-120 | hourly (optional) |

    All NOAA models are pulled live from NOMADS. Tomorrow.io is included when an
    API key is configured by the site operator.
    """
)
