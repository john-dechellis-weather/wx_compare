"""Archive Radar Position — aircraft location on archived NEXRAD Level II radar.

Data from the AWS public Level II archive (full history). Single-frame
mode or Loop mode with a frame slider. Loop results are kept in
session_state so the slider works without refetching.
"""
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
# Cached fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False, max_entries=10)
def cached_render_single(
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


@st.cache_data(ttl=86400, show_spinner=False, max_entries=5)
def cached_render_loop(
    request_time_iso: str,
    duration_min: int,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
) -> tuple[list[tuple[bytes, str]], list[tuple[bytes, str]]]:
    from core.radar import fetch_and_render_radar_loop

    request_time = datetime.fromisoformat(request_time_iso)
    return fetch_and_render_radar_loop(
        start_time=request_time,
        duration_min=duration_min,
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
st.caption("Plot aircraft position on archived NEXRAD Level II radar imagery.")

with st.sidebar:
    st.header("Date & Time (UTC)")
    date_input = st.date_input(
        "Date",
        value=None,
        min_value=datetime(2020, 1, 1).date(),
        max_value=datetime.now(timezone.utc).date(),
        help="Date of the aircraft event (UTC). Level II archive covers years back.",
    )
    time_input = st.time_input(
        "Time (UTC)",
        value=None,
        step=timedelta(minutes=5),
        help="The Level II volume nearest this time will be used.",
    )

    st.divider()
    st.header("Aircraft Position")
    lat_input = st.text_input(
        "Latitude",
        value="",
        placeholder="e.g. 40.64",
        help="Decimal degrees. Positive = North.",
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
    )

    st.divider()
    st.header("Radar")
    station_input = st.text_input(
        "Select Radar Site",
        value="",
        max_chars=4,
        placeholder="e.g. KDIX",
        help="4-letter code like KDIX, KOKX, KMLB. Case insensitive.",
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
    st.header("Loop")
    loop_mode = st.toggle(
        "Loop from start time",
        value=False,
        help="Fetch all volumes in a window starting at the selected time.",
    )
    loop_duration = st.selectbox(
        "Loop duration (minutes)",
        options=[15, 30, 60],
        index=1,
        disabled=not loop_mode,
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
# Fetch on click
# ---------------------------------------------------------------------------
if run_button:
    errors = []
    if date_input is None:
        errors.append("Please pick a date.")
    if time_input is None:
        errors.append("Please pick a time.")
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

    request_time = datetime.combine(date_input, time_input, tzinfo=timezone.utc)

    if request_time > datetime.now(timezone.utc):
        st.error("Requested time is in the future. Pick a past time.")
        st.stop()

    if len(station_input) == 4 and station_input.startswith("K"):
        station_code = station_input[1:]
    else:
        station_code = station_input

    callsign = callsign_input.strip() or "AIRCRAFT"

    meta = {
        "request_time": request_time,
        "aircraft_lat": aircraft_lat,
        "aircraft_lon": aircraft_lon,
        "callsign": callsign,
        "station_code": station_code,
    }

    if loop_mode:
        with st.spinner(
            f"Fetching {loop_duration}-minute Level II loop... "
            "volumes are large, this can take a few minutes."
        ):
            try:
                refl_frames, vel_frames = cached_render_loop(
                    request_time_iso=request_time.isoformat(),
                    duration_min=int(loop_duration),
                    aircraft_lat=aircraft_lat,
                    aircraft_lon=aircraft_lon,
                    callsign=callsign,
                    station=station_code,
                    zoom_deg=zoom_input,
                )
            except Exception as e:
                st.error(f"Failed to fetch loop: {e}")
                st.stop()
        st.session_state["radar_loop"] = {
            "refl_frames": refl_frames,
            "vel_frames": vel_frames,
            "meta": meta,
        }
        st.session_state.pop("radar_single", None)
    else:
        with st.spinner("Fetching Level II volume and rendering plots..."):
            try:
                refl_png, vel_png, refl_time, vel_time = cached_render_single(
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
        st.session_state["radar_single"] = {
            "refl_png": refl_png,
            "vel_png": vel_png,
            "refl_time": refl_time,
            "vel_time": vel_time,
            "meta": meta,
        }
        st.session_state.pop("radar_loop", None)


# ---------------------------------------------------------------------------
# Display from session state (survives slider reruns)
# ---------------------------------------------------------------------------
if "radar_loop" in st.session_state:
    data = st.session_state["radar_loop"]
    refl_frames = data["refl_frames"]
    vel_frames = data["vel_frames"]
    meta = data["meta"]
    request_time = meta["request_time"]
    callsign = meta["callsign"]
    station_code = meta["station_code"]

    st.info(
        f"Loop start: **{request_time:%Y-%m-%d %H:%M UTC}**  ·  "
        f"Position: **{meta['aircraft_lat']:.4f}°, {meta['aircraft_lon']:.4f}°**  ·  "
        f"Radar: **K{station_code}**  ·  "
        f"Frames: **{len(refl_frames)}** reflectivity, **{len(vel_frames)}** velocity"
    )

    n = len(refl_frames)
    if n == 1:
        idx = 0
        st.caption("Only one volume available in this window.")
    else:
        idx = st.slider(
            "Frame",
            min_value=0,
            max_value=n - 1,
            value=0,
            help="Scrub through the radar volumes chronologically.",
        )

    refl_png, refl_name = refl_frames[idx]
    st.subheader("Base Reflectivity (0.5°)")
    st.caption(f"Frame {idx + 1} of {n}  ·  `{refl_name}`")
    st.image(refl_png, use_container_width=True)
    st.download_button(
        label="Download this Reflectivity frame",
        data=refl_png,
        file_name=f"radar_refl_{callsign}_K{station_code}_frame{idx + 1}.png",
        mime="image/png",
        key="dl_refl_loop",
    )

    st.subheader("Base Velocity (0.5°)")
    if vel_frames:
        v_idx = min(idx, len(vel_frames) - 1)
        vel_png, vel_name = vel_frames[v_idx]
        st.caption(f"Frame {v_idx + 1} of {len(vel_frames)}  ·  `{vel_name}`")
        st.image(vel_png, use_container_width=True)
        st.download_button(
            label="Download this Velocity frame",
            data=vel_png,
            file_name=f"radar_vel_{callsign}_K{station_code}_frame{v_idx + 1}.png",
            mime="image/png",
            key="dl_vel_loop",
        )
    else:
        st.warning("Velocity not available in this window.")

elif "radar_single" in st.session_state:
    data = st.session_state["radar_single"]
    meta = data["meta"]
    request_time = meta["request_time"]
    callsign = meta["callsign"]
    station_code = meta["station_code"]

    st.info(
        f"Requested: **{request_time:%Y-%m-%d %H:%M UTC}**  ·  "
        f"Position: **{meta['aircraft_lat']:.4f}°, {meta['aircraft_lon']:.4f}°**  ·  "
        f"Callsign: **{callsign}**  ·  "
        f"Radar: **K{station_code}**"
    )

    st.subheader("Base Reflectivity (0.5°)")
    st.caption(f"Volume: `{data['refl_time']}`")
    st.image(data["refl_png"], use_container_width=True)
    st.download_button(
        label="Download Reflectivity PNG",
        data=data["refl_png"],
        file_name=f"radar_refl_{callsign}_K{station_code}_{request_time:%Y%m%d_%H%M}Z.png",
        mime="image/png",
        key="dl_refl",
    )

    st.subheader("Base Velocity (0.5°)")
    st.caption(f"Volume: `{data['vel_time']}`")
    if data["vel_png"]:
        st.image(data["vel_png"], use_container_width=True)
        st.download_button(
            label="Download Velocity PNG",
            data=data["vel_png"],
            file_name=f"radar_vel_{callsign}_K{station_code}_{request_time:%Y%m%d_%H%M}Z.png",
            mime="image/png",
            key="dl_vel",
        )
    else:
        st.warning("Velocity not available in this volume.")

else:
    st.info(
        "Fill in date/time, position, callsign, and radar station in the sidebar, "
        "then click **Fetch & Render**. Turn on **Loop** to fetch a window of "
        "volumes and scrub through them."
    )
    st.markdown(
        """
        ### About

        Aircraft position overlaid on archived **NEXRAD Level II** radar —
        the full-resolution archive going back years, ideal for forensic
        weather analysis of past events.

        - **Base Reflectivity (0.5°)** — Echo intensity at the lowest tilt.
        - **Base Velocity (0.5°)** — Doppler radial velocity in knots.
          Green = away from radar, red = toward radar. (Raw velocity may
          show aliasing in strong wind regimes.)
        - **Loop mode** — Fetches every volume in a 15/30/60-minute window
          (typical cadence 4-10 minutes). Level II volumes are large, so a
          long loop can take a few minutes on first fetch; results are
          cached for 24 hours.

        Choose a radar site within ~250 km of the aircraft position, and
        note that clear weather legitimately shows no echoes.
        """
    )