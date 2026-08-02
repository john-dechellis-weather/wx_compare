"""Archive Radar Position — chunk 1: imports + sidebar."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import streamlit as st

st.set_page_config(
    page_title="BlueMet — Archive Radar Position",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


st.title("Archive Radar Position")
st.caption("Chunk 1: sidebar only")

with st.sidebar:
    st.header("Date & Time (UTC)")
    date_input = st.date_input(
        "Date",
        value=datetime(2026, 7, 6).date(),
        min_value=datetime(2022, 1, 1).date(),
        max_value=datetime.now(timezone.utc).date(),
    )
    time_input = st.time_input(
        "Time (UTC)",
        value=datetime(2026, 7, 6, 23, 30).time(),
        step=timedelta(minutes=5),
    )

    st.divider()
    st.header("Aircraft Position")
    lat_input = st.text_input("Latitude", value="39.76")
    lon_input = st.text_input("Longitude", value="-86.15")
    callsign_input = st.text_input("Callsign / Label", value="TEST")

    st.divider()
    st.header("Radar")
    station_input = st.text_input("Select Radar Site", value="KFTG", max_chars=4)

    zoom_input = st.slider("Zoom (degrees)", 0.5, 5.0, 2.0, 0.5)

    st.divider()
    run_button = st.button("Fetch & Render", type="primary", use_container_width=True)

st.write("Sidebar loaded successfully")