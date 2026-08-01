"""Wind comparison — multi-model wind speed, direction, and gust."""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import streamlit as st


st.set_page_config(
    page_title=("Wind Comparison - Active Mouse Readout and Drag Box to Zoom in"),
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()


# Persistent disk on Render; falls back to /tmp locally.
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

# Tomorrow.io API key (optional)
try:
    if "TOMORROWIO_API_KEY" in st.secrets:
        os.environ["TOMORROWIO_API_KEY"] = st.secrets["TOMORROWIO_API_KEY"]
except Exception:
    pass


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_compare(icaos_tuple: tuple[str, ...], cycle_iso: str):
    from compare import compare_icaos
    cycle = datetime.fromisoformat(cycle_iso)
    df, resolved, unresolved = compare_icaos(
        icaos=list(icaos_tuple),
        cycle=cycle,
        cache_root=CACHE_ROOT,
    )
    return df, resolved, unresolved


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_latest_cycle(icaos_tuple: tuple[str, ...]) -> str | None:
    from core.stations import StationResolver
    from core.cycle_select import find_latest_complete
    from models import GfsMos, GfsLamp, Hrrr, Nbm

    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved_pre, _ = resolver.resolve_many(list(icaos_tuple))
    if not resolved_pre:
        return None
    probe_sources = [
        GfsMos(cache_dir=CACHE_ROOT / "gfs_mos"),
        GfsLamp(cache_dir=CACHE_ROOT / "gfs_lamp"),
        Hrrr(cache_dir=CACHE_ROOT / "hrrr",
             stations=resolved_pre, fhours=range(0, 19)),
        Nbm(cache_dir=CACHE_ROOT / "nbm"),
    ]
    cycle = find_latest_complete(probe_sources, verbose=False)
    return cycle.isoformat() if cycle else None


st.title("Wind Forecast Comparison")
st.caption(
    "Compare sustained wind speed, direction, and gust forecasts from "
    "HRRR, GFS MOS, GFS LAMP, NBM, and (optionally) Tomorrow.io."
)

with st.sidebar:
    st.header("Inputs")
    icaos_raw = st.text_input("ICAO codes", value="KJFK")
    cycle_mode = st.radio(
        "Cycle selection",
        options=["Auto (latest complete)", "Specific hour"],
        index=0,
    )
    specific_hour = None
    if cycle_mode == "Specific hour":
        specific_hour = st.selectbox(
            "Cycle hour (UTC)",
            options=["00", "06", "12", "18"],
            index=2,
        )
    st.divider()

    st.subheader("Display")
    hours_ahead = st.slider(
        "Forecast horizon",
        min_value=12, max_value=120, value=48, step=6,
    )
    speed_max = st.slider(
        "Max wind speed on Y-axis (kt)",
        min_value=20, max_value=80, value=40, step=5,
    )

    run_button = st.button("Run comparison", type="primary", use_container_width=True)

    st.divider()
    if os.environ.get("TOMORROWIO_API_KEY"):
        st.success("✓ Tomorrow.io enabled")
        st.caption("Daily quota shared across all users.")
    else:
        st.info("ⓘ NOAA models only — no Tomorrow.io key configured")


if run_button:
    icaos = [s.strip().upper() for s in icaos_raw.split(",") if s.strip()]
    if not icaos:
        st.error("Enter at least one ICAO code.")
        st.stop()

    if cycle_mode == "Auto (latest complete)":
        with st.spinner("Probing NOMADS for latest complete cycle..."):
            cycle_iso = cached_latest_cycle(tuple(icaos))
        if cycle_iso is None:
            st.error("No complete cycle found within recent probes.")
            st.stop()
        cycle = datetime.fromisoformat(cycle_iso)
    else:
        today = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        cycle = today.replace(hour=int(specific_hour))
        if cycle > datetime.now(timezone.utc):
            cycle = cycle - timedelta(days=1)
        cycle_iso = cycle.isoformat()

    st.info(f"Using cycle: **{cycle:%Y-%m-%d %H:%M UTC}**")

    with st.spinner("Fetching forecasts..."):
        df, resolved, unresolved = cached_compare(tuple(icaos), cycle_iso)

    # Fetch METAR observations for ground truth overlay
    from core.metar import fetch_metars, filter_since, metars_to_df
    icao_list = [s.icao for s in resolved]
    try:
        raw_metars = fetch_metars(icao_list, hours_back=48)
        filtered = filter_since(raw_metars, cycle)
        metars_df = metars_to_df(filtered)
    except Exception as e:
        st.warning(f"METAR fetch failed: {e}")
        metars_df = None

    if unresolved:
        st.warning(f"Could not resolve these ICAOs: {', '.join(unresolved)}")

    if not resolved:
        st.error("No stations could be resolved. Check your ICAO codes.")
        st.stop()

    if len(df) == 0:
        st.warning("Comparison returned no data.")
        st.stop()
     
    for s in resolved:
        st.markdown(f"**{s.icao}** — {s.name}")
    st.markdown("---")

    # Render per station
    from compare import plot_wind_comparison_interactive

    tab_plots, tab_data = st.tabs(["Plots", "Raw data"])

    with tab_plots:
        for s in resolved:
            st.subheader(s.icao)
            fig = plot_wind_comparison_interactive(
                df, s.icao,
                cycle=cycle,
                speed_ylim=(0, speed_max),
                hours_ahead=hours_ahead,
                metars_df=metars_df,
            )
            fig.update_layout(width=None, autosize=True)
            st.plotly_chart(fig, use_container_width=True)

    with tab_data:
        wind_cols = ["station_id", "model", "valid_time", "forecast_hour",
                     "wind_speed_kt", "wind_dir_deg", "wind_gust_kt"]
        st.dataframe(df[wind_cols], use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download as CSV",
            data=csv,
            file_name=f"wx_compare_wind_{cycle:%Y%m%d_%H}Z.csv",
            mime="text/csv",
        )

else:
    st.info("Enter ICAO codes in the sidebar and click **Run comparison**.")
    st.markdown(
        """
        ### Plot Description

        - **Top panel**: sustained wind speed (kt). Gust values (where reported) appear as dotted lines for forecast data.
        - **Bottom panel**: wind direction (degrees true*). Markers only — no connecting lines — so 360° → 0° wraps don't create misleading jumps.
        - **Forecast Horizon Slider**: adjusts the end range of the model data
        - **Max Wind Speed on Y-Axis**: adjusts the y-axis maximum wind value  (default is 40kts)
        - **Vertical line with triangle represents observed wind gust from the hourly METARs

        ### Wind Direction Panel

        - Hover over any marker for cardinal direction (e.g. "WSW") and active readout

        ### Models With Gust (dashed line)

        | Model | Sustained | Gust |
        | --- | --- | --- |
        | HRRR | ✓ | — |
        | GFS MOS | ✓ | — |
        | GFS LAMP | ✓ | — |
        | NBM | ✓ | ✓ |
        | Tomorrow.io | ✓ | ✓ |
        """
    )
