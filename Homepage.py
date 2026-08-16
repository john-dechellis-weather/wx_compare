"""BlueMet entry point and navigation router.

Declares the sidebar's grouped navigation via st.navigation; the
tool pages live in pages/ and are referenced here by path, with
sidebar titles set explicitly (filename prefixes no longer control
order or labels).
"""

import time
from pathlib import Path

import streamlit as st


def _cleanup_old_cache():
    """Delete cache files older than the cutoff to prevent disk
    fill on the persistent volume."""
    cache_root = Path("/opt/render/project/src/cache")
    if not cache_root.exists():
        return
    cutoff = time.time() - (24 * 3600)
    for p in cache_root.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            continue


if "_cache_cleanup_done" not in st.session_state:
    _cleanup_old_cache()
    st.session_state["_cache_cleanup_done"] = True

st.set_page_config(
    page_title="BlueMet",
    layout="wide",
    initial_sidebar_state="expanded",
)

from retro_theme import apply_retro_theme

apply_retro_theme()

from auth import check_password

check_password()


def _home():
    st.title("BlueMet")
    st.markdown(
        "<p style='color: #B30000; font-size: 32px; "
        "font-weight: bold;'>"
        "IMPORTANT: Use Prohbited outside of the JetBlue SOC or "
        "for Tomorrow.io employees"
        "</p>",
        unsafe_allow_html=True,
    )
    st.caption("Multi-model comparison for CONUS airports.")
    st.markdown(
        """
        ### Sections

        - **Forecast Tools** — Hi-res CAMs, wind plots, flight
          conditions, and MOS guidance
        - **Situational Awareness Products** — the JBU Weather
          Map, station quick view, and fleet tracker
        - **Archive Flight Conditions** — historical satellite
          and radar with flight overlay
        """
    )


PAGES = {
    "": [
        st.Page(_home, title="Home", default=True),
    ],
    "Forecast Tools": [
        st.Page("pages/9_HiRes_CAMs.py",
                title="Hi-Res CAMs"),
        st.Page("pages/8_Forecast_Wind_Plots.py",
                title="Forecast Wind Plots"),
        st.Page("pages/1_Forecast_Flight_Conditions.py",
                title="Forecast Flight Conditions"),
        st.Page("pages/4_MOS_Tables.py",
                title="MOS Tables"),
    ],
    "Situational Awareness Products": [
        st.Page("pages/3_JBU_Weather_Map.py",
                title="JBU Weather Map"),
        st.Page("pages/10_JBU_Weather_Map_Only.py",
                title="JBU Weather Map Only"),
        st.Page("pages/7_Station_Quick_View.py",
                title="Station Quick View"),
        st.Page("pages/8_JBU_Flight_Tracker.py",
                title="JBU Flight Tracker"),
    ],
    "Archive Flight Conditions": [
        st.Page("pages/5_Archive_Satellite_Position.py",
                title="Archive Satellite"),
        st.Page("pages/6_Archive_Radar_Position.py",
                title="Archive Radar"),
    ],
}

st.markdown(
    """
    <style>
    [data-testid="stNavSectionHeader"] {
        font-weight: bold !important;
        color: #000000 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

nav = st.navigation(PAGES)
nav.run()
