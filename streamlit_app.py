"""Landing page for the wx_compare suite.

The actual tools live in pages/ and Streamlit auto-discovers them.
"""
import streamlit as st

st.set_page_config(
    page_title="wx_compare — aviation forecast tools",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Aviation Forecast Tools")
st.caption("Multi-model comparison of forecast variables for any US airport.")

st.markdown(
    """
    ### Available tools

    - **VIS / CIG Comparison** — Visibility and ceiling forecasts from five models
    - **Wind Comparison** — Wind speed, direction, and gust forecasts from five models

    Select a tool from the sidebar to get started.

    ### Models compared

    | Source | Forecast Range | Resolution |
    | --- | --- | --- |
    | HRRR | f+0 to f+18 | hourly |
    | GFS MOS (MAV) | f+6 to f+72 | 3-hourly |
    | GFS LAMP (LAV) | f+1 to f+25 | hourly |
    | NBM (NBH + NBS) | f+1 to f+72 | hourly + 3-hourly |
    | Tomorrow.io | f+1 to ~120 | hourly (optional) |

    All NOAA models are pulled live from NOMADS. Tomorrow.io is included when an
    API key is configured by the site operator.
    """
)
