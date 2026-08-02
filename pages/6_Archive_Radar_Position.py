"""Archive Radar Position — plot aircraft location on archived NEXRAD radar."""
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


# ---------------------------------------------------------------------------
# Cached fetch + render
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Archive Radar Position")
st.caption("Plot aircraft position on archived NEXRAD Level III radar imagery.")

with st.sidebar:
    st.header("Date & Time (UTC)")
    date_input = st.date_input(
        "Date",
        value=datetime(2026, 7, 6).date(),
        min_value=datetime(2022, 1, 1).date(),
        max_value=datetime.now(timezone.utc).date(),
        help="Date of the aircraft event (UTC).",
    )
    time_input = st.time_input(
        "Time (UTC)",
        value=datetime(2026, 7, 6, 23, 30).time(),
        step=timedelta(minutes=5),
        help="Nearest available radar scan will be used.",
    )

    st.divider()
    st.header("Aircraft Position")
    lat_input = st.text_input(
        "Latitude",
        value="39.76",
        help="Decimal degrees. Positive = North.",
    )
    lon_input = st.text_input(
        "Longitude",
        value="-86.15",
        help="Decimal degrees. Negative = West.",
    )
    callsign_input = st.text_input(
        "Callsign / Label",
        value="TEST",
    )

    st.divider()
    st.header("Radar")
    station_input = st.text_input(
        "Select Radar Site",
        value="KFTG",
        max_chars=4,
        help="4-letter code like KDIX, KFTG, KJFK. Case insensitive.",
    ).strip().upper()

    zoom_input = st.slider(
        "Zoom (degrees)",
        min_value=0.5,
        max_value=5.0,
        value=2.0,
        step=0.5,
        help="Half-width of view around aircraft. 1° ≈ 60 nautical miles.",
    )

    st.divider()
    run_button = st.button("Fetch & Render", type="primary", use_container_width=True)

    st.divider()
    st.caption(
        "**Reflectivity scale (dBZ):**\n\n"
        "Light blue/green (10-25): Light rain\n\n"
        "Yellow (30-40): Moderate rain\n\n"
        "Orange (40-50): Heavy rain\n\n"
        "Red (50-60): Very heavy / hail\n\n"
        "Magenta (60+): Extreme / large hail\n\n"
        "---\n\n"
        "**Velocity scale (kt):**\n\n"
        "Green: Away from radar\n\n"
        "Red: Toward radar\n\n"
        "Deep colors: Higher speeds"
    )


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
if run_button:
    # Validate inputs
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

    # Combine into UTC datetime
    request_time = datetime.combine(
        date_input, time_input, tzinfo=timezone.utc
    )

    if request_time > datetime.now(timezone.utc):
        st.error("Requested time is in the future. Pick a past time.")
        st.stop()

    # Strip 'K' prefix if present
    if len(station_input) == 4 and station_input.startswith("K"):
        station_code = station_input[1:]
    else:
        station_code = station_input

    callsign = callsign_input.strip() or "AIRCRAFT"

    st.info(
        f"Requested: **{request_time:%Y-%m-%d %H:%M UTC}**  ·  "
        f"Position: **{aircraft_lat:.4f}°, {aircraft_lon:.4f}°**  ·  "
        f"Callsign: **{callsign}**  ·  "
        f"Radar: **K{station_code}**"
    )

    with st.spinner("Fetching NEXRAD data and rendering plots..."):
        try:
            refl_png, vel_png, refl_time, vel_time = cached_render(
                request_time_iso=request_time.isoformat(),
                aircraft_lat=aircraft_lat,
                aircraft_lon=aircraft_lon,
                callsign=callsign,
                station=station_code,
                zoom_deg=zoom_input,
            )
        except Exception as e:
            st.error(f"Failed to fetch/render: {e}")
            st.stop()

    # Reflectivity — always shown
    st.subheader("Base Reflectivity (N0B)")
    st.caption(f"Dataset: `{refl_time}`")
    st.image(refl_png, use_container_width=True)
    st.download_button(
        label="Download Reflectivity PNG",
        data=refl_png,
        file_name=f"radar_refl_{callsign}_K{station_code}_{request_time:%Y%m%d_%H%M}Z.png",
        mime="image/png",
        key="dl_refl",
    )

    # Velocity — shown if available, otherwise warning
    st.subheader("Base Velocity")
    st.caption(f"Dataset: `{vel_time}`")
    if vel_png:
        st.image(vel_png, use_container_width=True)
        st.download_button(
            label="Download Velocity PNG",
            data=vel_png,
            file_name=f"radar_vel_{callsign}_K{station_code}_{request_time:%Y%m%d_%H%M}Z.png",
            mime="image/png",
            key="dl_vel",
        )
    else:
        st.warning("Velocity product not available for this radar/time.")

else:
    st.info(
        "Fill in date/time, position, callsign, and radar station in the sidebar, "
        "then click **Fetch & Render**."
    )
    st.markdown(
        """
        ### About

        This page displays aircraft position overlaid on archived NEXRAD
        Level III radar imagery — useful for forensic weather analysis of
        past events involving convective weather or turbulence.

        Two plots are generated:

        - **Base Reflectivity (N0B)** — Radar echo intensity at the lowest
          tilt (0.5°). Shows where precipitation is falling.
        - **Base Velocity** — Doppler radial velocity at the lowest tilt.
          Green = motion away from radar, red = motion toward radar.

        Both plots are centered on the aircraft position with a configurable
        zoom. Radar data is from UCAR's THREDDS archive.
        """
    )
