"""Landing page for the wx_compare suite.

The actual tools live in pages/ and Streamlit auto-discovers them.
"""
import streamlit as st

st.set_page_config(
    page_title="Homepage",
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

st.title("Aviation Forecast Tools")
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
