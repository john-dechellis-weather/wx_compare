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

    - **VIS/CIG Comparison Tool** — Visibility and ceiling forecast data from the five models below 
    - **Wind Comparison Tool** — Wind speed, direction, and gust forecast data from the five models below

    Select a tool from the sidebar to start. Do not share this site URL outside of the SOC team. 

    ### Models compared

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
