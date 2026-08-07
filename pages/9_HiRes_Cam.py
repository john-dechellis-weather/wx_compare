"""Hi-Res CAMs — convection-allowing model viewer, 2x2 model grid.

Top-right panel: HRRR (hourly-updating, aviation products only, live
JBU aircraft overlaid). Remaining quadrants are placeholders for the
next models (NAM Nest, HRW, RRFS).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="BlueMet — Hi-Res CAMs",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False, max_entries=30)
def cached_station_coords(icao: str):
    from core.stations import StationResolver
    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved, _ = resolver.resolve_many([icao])
    if not resolved:
        return None
    stn = resolved[0]
    return float(stn.lat), float(stn.lon)


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_hrrr_cycle(fhr: int, bucket: str):
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(fhr)
    return cyc.isoformat() if cyc else None


@st.cache_data(ttl=120, show_spinner=False, max_entries=20)
def cached_jbu(lat: float, lon: float, radius: float, bucket: str):
    from core.flights import fetch_positions_near
    try:
        return fetch_positions_near(lat, lon, radius_deg=radius)
    except Exception:
        return []


@st.cache_data(ttl=600, show_spinner=False, max_entries=10)
def cached_routes(aircraft, bucket: str):
    from core.flights import fetch_routes
    try:
        return fetch_routes(list(aircraft))
    except Exception:
        return {}


@st.cache_data(ttl=1800, show_spinner=False, max_entries=24)
def cached_hrrr_panel(
    product: str, cycle_iso: str, fhr: int,
    clat: float, clon: float, zoom: float, aircraft, routes=None,
) -> bytes:
    """One rendered HRRR panel. Keyed on cycle+fhr+product+region, so a
    new model cycle naturally refreshes it (HRRR runs hourly)."""
    from core.hrrr_cam import fetch_field, decode_field, render_field
    cycle = datetime.fromisoformat(cycle_iso)
    raw = fetch_field(product, cycle, fhr, clat, clon, zoom)
    vals, lats, lons = decode_field(raw)
    valid = cycle + __import__("datetime").timedelta(hours=fhr)
    title = (
        f"HRRR {cycle:%m/%d %H}Z  f{fhr:02d}  "
        f"valid {valid:%m/%d %H}Z"
    )
    return render_field(
        product, vals, lats, lons, clat, clon, zoom, title,
        aircraft=aircraft, routes=routes,
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Hi-Res CAMs")
st.caption(
    "Convection-allowing model viewer — aviation products, "
    "hourly-updating, live JBU aircraft overlaid."
)

with st.sidebar:
    st.header("Region")
    icao_input = st.text_input("Airport ICAO", value="KJFK",
                               max_chars=4).strip().upper()
    zoom = st.slider("Zoom (degrees)", 1.0, 6.0, 2.5, 0.5)

    st.header("HRRR")
    product_label = st.selectbox(
        "Product",
        options=[
            "Composite Reflectivity",
            "Echo Tops",
            "Visibility",
            "Ceiling",
            "10 m Wind Gust",
        ],
        index=0,
    )
    PRODUCT_KEY = {
        "Composite Reflectivity": "REFC",
        "Echo Tops": "RETOP",
        "Visibility": "VIS",
        "Ceiling": "CEIL",
        "10 m Wind Gust": "GUST",
    }
    fhr = st.slider("Forecast hour", 0, 18, 1)
    show_jbu = st.checkbox("Overlay live JBU aircraft", value=True)

    st.divider()
    run_button = st.button("Render", type="primary",
                           use_container_width=True)

if run_button and icao_input:
    st.session_state["cam_icao"] = icao_input

active = st.session_state.get("cam_icao")

if active:
    icao = active
    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve coordinates for {icao}.")
        st.stop()
    clat, clon = coords

    product = PRODUCT_KEY[product_label]
    cycle_iso = cached_hrrr_cycle(fhr, bucket10)
    if cycle_iso is None:
        st.error("No complete HRRR cycle found on NOMADS (last 6 hours).")
        st.stop()
    cycle = datetime.fromisoformat(cycle_iso)

    aircraft = []
    routes = {}
    if show_jbu:
        aircraft = cached_jbu(round(clat, 2), round(clon, 2), zoom,
                              now.strftime("%Y%m%d%H%M"))
        if aircraft:
            routes = cached_routes(
                tuple(aircraft), now.strftime("%Y%m%d%H%M")
            )

    st.info(
        f"**{icao}** \u00b7 HRRR **{cycle:%m/%d %H}Z** \u00b7 f{fhr:02d} "
        f"\u00b7 {product_label}"
        + (f" \u00b7 {len(aircraft)} JBU live"
           f" \u00b7 {len(routes)} routes" if show_jbu else "")
    )

    # 2x2 model grid — HRRR occupies the TOP-RIGHT quadrant
    top_left, top_right = st.columns(2)
    bot_left, bot_right = st.columns(2)

    with top_right:
        st.markdown("**HRRR**")
        with st.spinner("Fetching + decoding HRRR (5-20s)..."):
            try:
                png = cached_hrrr_panel(
                    product, cycle_iso, fhr,
                    round(clat, 2), round(clon, 2), zoom,
                    aircraft, routes=routes,
                )
                st.image(png, use_container_width=True)
            except Exception as e:
                st.error(f"HRRR panel failed: {e}")

    _ph = (
        '<div style="border:2px dashed #00FF00;padding:40px 10px;'
        'text-align:center;color:#FFFFFF;'
        '-webkit-text-fill-color:#FFFFFF;'
        'font-family:Courier New,monospace;font-size:12px;">'
        "{name}<br>coming soon</div>"
    )
    with top_left:
        st.markdown("**NAM Nest**")
        st.markdown(_ph.format(name="NAM 3km CONUS Nest"),
                    unsafe_allow_html=True)
    with bot_left:
        st.markdown("**HRW ARW**")
        st.markdown(_ph.format(name="Hi-Res Window ARW"),
                    unsafe_allow_html=True)
    with bot_right:
        st.markdown("**RRFS**")
        st.markdown(_ph.format(name="Rapid Refresh Forecast System"),
                    unsafe_allow_html=True)

else:
    st.info("Enter an airport in the sidebar and click **Render**.")
    st.markdown(
        """
        ### What this page is

        A 2\u00d72 grid of convection-allowing models centered on your
        airport, aviation products only. **HRRR** (top right) is live:
        composite reflectivity, echo tops, visibility, ceiling, and
        gusts, forecast hours f00\u2013f18, updating every hour, with
        live JetBlue aircraft overlaid. The other quadrants fill in as
        models are added.
        """
    )
