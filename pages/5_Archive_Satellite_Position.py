"""Archive Satellite Position — plot aircraft location on archived GOES imagery.

User provides a date/time, lat/lon, and callsign. The page fetches the nearest
available GOES satellite image and renders TWO plots stacked (True Color on top,
Clean IR on bottom) with the aircraft position marked.

Uses goes2go for AWS-hosted GOES archive.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="BlueMet — Archive Satellite Position",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


# ---------------------------------------------------------------------------
# Cache dir
# ---------------------------------------------------------------------------
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
SATELLITE_CACHE = CACHE_ROOT / "satellite"
SATELLITE_CACHE.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Fetch + render — cached so identical requests skip re-work
# ---------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False, max_entries=10)
def fetch_and_render(
    request_time_iso: str,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    satellite: str,
) -> tuple[bytes, bytes, str]:
    """Fetch nearest GOES image and render both True Color and IR plots.

    Returns (true_color_png_bytes, ir_png_bytes, actual_image_time_str).
    """
    from core.satellite import (
        fetch_goes_data,
        render_true_color,
        render_infrared,
    )

    request_time = datetime.fromisoformat(request_time_iso)

    # Fetch the nearest GOES scan
    ds, actual_time = fetch_goes_data(
        target_time=request_time,
        satellite=satellite,
        cache_dir=SATELLITE_CACHE,
    )

    # Render both plots as PNG bytes
    true_color_png = render_true_color(
        ds=ds,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
    )
    ir_png = render_infrared(
        ds=ds,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
    )

    return true_color_png, ir_png, actual_time.strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Archive Satellite Position")
st.caption("Plot aircraft position on archived GOES satellite imagery.")

with st.sidebar:
    st.header("Date & Time (UTC)")
    date_input = st.date_input(
        "Date",
        value=None,
        min_value=datetime(2022, 1, 1).date(),  # GOES archive availability
        max_value=datetime.now(timezone.utc).date(),
        help="Date of the aircraft event (UTC).",
    )
    time_input = st.time_input(
        "Time (UTC)",
        value=None,
        step=timedelta(minutes=10),
        help="Nearest available satellite image will be used.",
    )

    st.divider()
    st.header("Aircraft Position")
    lat_input = st.text_input(
        "Latitude",
        value="",
        placeholder="e.g. 40.64",
        help="Decimal degrees. Positive = North, negative = South.",
    )
    lon_input = st.text_input(
        "Longitude",
        value="",
        placeholder="e.g. -73.78",
        help="Decimal degrees. Negative = West.",
    )
    callsign_input = st.text_input(
        "Callsign / Label",
        value="",
        placeholder="e.g. F1873",
        help="Optional label to display next to the aircraft marker.",
    )

    st.divider()
    st.header("Satellite")
    satellite_choice = st.radio(
        "Which GOES?",
        options=["GOES-East (GOES-19)", "GOES-West (GOES-18)"],
        index=0,
    )
    satellite_id = "goes19" if "East" in satellite_choice else "goes18"

    st.divider()
    run_button = st.button("Fetch & Render", type="primary", use_container_width=True)

    st.divider()
    st.caption(
        "**Notes:**\n\n"
        "GOES-East covers CONUS + Atlantic\n\n"
        "GOES-West covers CONUS + Pacific\n\n"
        "Archived imagery from AWS (~10 min cadence)."
    )


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
if run_button:
    # Validate all inputs
    errors = []
    if date_input is None:
        errors.append("Please pick a date.")
    if time_input is None:
        errors.append("Please pick a time.")
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

    # Combine date + time into a UTC datetime
    request_time = datetime.combine(
        date_input, time_input, tzinfo=timezone.utc
    )

    # Check the time isn't in the future
    if request_time > datetime.now(timezone.utc):
        st.error("Requested time is in the future. Pick a past time.")
        st.stop()

    callsign = callsign_input.strip() or "AIRCRAFT"

    st.info(
        f"Requested: **{request_time:%Y-%m-%d %H:%M UTC}**  ·  "
        f"Position: **{aircraft_lat:.4f}°, {aircraft_lon:.4f}°**  ·  "
        f"Callsign: **{callsign}**  ·  "
        f"Satellite: **{satellite_choice}**"
    )

    with st.spinner("Fetching GOES imagery and rendering plots..."):
        try:
            true_color_png, ir_png, actual_time_str = fetch_and_render(
                request_time_iso=request_time.isoformat(),
                aircraft_lat=aircraft_lat,
                aircraft_lon=aircraft_lon,
                callsign=callsign,
                satellite=satellite_id,
            )
        except Exception as e:
            st.error(f"Failed to fetch/render: {e}")
            st.stop()

    st.success(f"Actual satellite image time: **{actual_time_str}**")

    st.subheader("True Color (Bands 1/2/3)")
    st.image(true_color_png, use_container_width=True)

    st.download_button(
        label="Download True Color PNG",
        data=true_color_png,
        file_name=f"goes_true_color_{callsign}_{request_time:%Y%m%d_%H%M}Z.png",
        mime="image/png",
    )

    st.subheader("Clean IR Window (Band 13)")
    st.image(ir_png, use_container_width=True)

    st.download_button(
        label="Download IR PNG",
        data=ir_png,
        file_name=f"goes_ir_{callsign}_{request_time:%Y%m%d_%H%M}Z.png",
        mime="image/png",
    )

else:
    st.info(
        "Fill in date/time, latitude, longitude, and callsign in the sidebar, "
        "then click **Fetch & Render**."
    )
    st.markdown(
        """
        ### About

        This page displays the aircraft position overlaid on archived GOES
        satellite imagery — useful for forensic weather analysis of past
        events.

        Two plots are generated:

        - **True Color** — Approximates what a human would see from space,
          using ABI Bands 1 (blue), 2 (red), and 3 (veggie).
        - **Clean IR Window** — Band 13 (10.3 μm) with color-coded cloud-top
          temperatures. Colder tops (deeper convection) appear as darker/blue
          colors.

        Both plots are zoomed to ~350 km around the aircraft position.
        Cached imagery persists 24 hours to avoid re-downloading.
        """
    )
