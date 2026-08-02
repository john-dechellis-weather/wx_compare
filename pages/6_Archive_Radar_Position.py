"""Archive Radar Position — chunk 2: add cached fetch."""
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


@st.cache_data(ttl=86400, show_spinner=False, max_entries=10)
def cached_render(
    request_time_iso: str,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
) -> tuple[bytes, bytes, str, str]:
    """Cache wrapper — same request within 24hr returns immediately."""
    from core.radar import fetch_and_render_radar

    request_time = datetime.fromisoformat(request_time_iso)
    return fetch_and_render_radar(
        target_time=request_time,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
        station=station,
        zoom_deg=zoom_deg,
    )


st.title("Archive Radar Position")
st.caption("Chunk 2: cached fetch wrapper added")

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

st.write("Chunk 2: sidebar + cached function definition loaded")