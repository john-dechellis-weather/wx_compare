"""Archive Radar Position — chunk 3: button click + validation, no fetch."""
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
st.caption("Chunk 3: button + validation added")

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
    station_input = st.text_input("Select Radar Site", value="KFTG", max_chars=4).strip().upper()
    zoom_input = st.slider("Zoom (degrees)", 0.5, 5.0, 2.0, 0.5)

    st.divider()
    run_button = st.button("Fetch & Render", type="primary", use_container_width=True)


if run_button:
    errors = []
    if not station_input or len(station_input) not in (3, 4):
        errors.append("Radar site must be 3 or 4 characters (e.g., KDIX or DIX).")
    if not lat_input.strip():
        errors.append("Please enter a latitude.")
    if not lon_input.strip():
        errors.append("Please enter a longitude.")

    aircraft_lat = None
    aircraft_lon = None
    if lat_input.strip():
        try:
            aircraft_lat = float(lat_input)
            if not (-90 <= aircraft_lat <= 90):
                errors.append("Latitude must be between -90 and 90.")
        except ValueError:
            errors.append("Latitude must be a number.")

    if lon_input.strip():
        try:
            aircraft_lon = float(lon_input)
            if not (-180 <= aircraft_lon <= 180):
                errors.append("Longitude must be between -180 and 180.")
        except ValueError:
            errors.append("Longitude must be a number.")

    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    request_time = datetime.combine(
        date_input, time_input, tzinfo=timezone.utc
    )

    if request_time > datetime.now(timezone.utc):
        st.error("Requested time is in the future. Pick a past time.")
        st.stop()

    if len(station_input) == 4 and station_input.startswith("K"):
        station_code = station_input[1:]
    else:
        station_code = station_input

    callsign = callsign_input.strip() or "AIRCRAFT"

    st.info(
        f"Requested: **{request_time:%Y-%m-%d %H:%M UTC}**  ·  "
        f"Position: **{aircraft_lat:.4f}°, {aircraft_lon:.4f}°**  ·  "
        f"Callsign: **{callsign}**  ·  "
        f"Radar: **K{station_code}** (searched as {station_code})"
    )
    st.success("Validation passed. In Chunk 4 we'll actually call cached_render.")
else:
    st.write("Chunk 3: click Fetch & Render to test validation")