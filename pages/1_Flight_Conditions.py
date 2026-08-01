"""Streamlit web app for wx_compare.

Phase 1: single-page VIS/CIG comparison. Wraps the existing notebook logic
in a web UI. Adding more comparison tools later means dropping new files
into pages/ — this file stays as-is.

Run locally:
    streamlit run streamlit_app.py

Deploy:
    Streamlit Community Cloud connects to your GitHub repo and auto-deploys
    on push. App URL will be something like
    https://wx-compare.streamlit.app
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import streamlit as st


# Page config — must be first Streamlit command
st.set_page_config(
    page_title="wx_compare — Flight Conditions",
    layout="wide",
    initial_sidebar_state="expanded",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

# -------------------------------- #
# Persistent disk on Render; falls back to /tmp locally.
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Tomorrow.io API key (optional)
# ---------------------------------------------------------------------------
# On Streamlit Community Cloud: set in app settings under "Secrets" using
# TOML format:  TOMORROWIO_API_KEY = "your_key"
# Locally: create .streamlit/secrets.toml with the same content
try:
    if "TOMORROWIO_API_KEY" in st.secrets:
        os.environ["TOMORROWIO_API_KEY"] = st.secrets["TOMORROWIO_API_KEY"]
except Exception:
    # st.secrets raises if no secrets are configured at all; that's fine
    pass


# ---------------------------------------------------------------------------
# Heavy operations — cached so multiple users hitting the same cycle reuse work
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_compare(icaos_tuple: tuple[str, ...], cycle_iso: str):
    """Run the comparison. Cached for 10 minutes so repeated identical queries
    don't re-fetch from NOMADS. Cache keys must be hashable, hence the tuples
    and ISO string."""
    from compare import compare_icaos
    cycle = datetime.fromisoformat(cycle_iso)
    df, resolved, unresolved = compare_icaos(
        icaos=list(icaos_tuple),
        cycle=cycle,
        cache_root=CACHE_ROOT,
    )
    # Streamlit's cache requires picklable returns. Stations are dataclasses
    # which pickle fine; DataFrame is fine; lists of strings are fine.
    return df, resolved, unresolved


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_latest_cycle(icaos_tuple: tuple[str, ...]) -> str | None:
    """Find the latest complete cycle. Cached briefly — cycles update hourly
    so a 5-minute cache strikes the right balance."""
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


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Flight Conditions")


with st.sidebar:
    st.header("Inputs")

    icaos_raw = st.text_input(
        "ICAO codes",
        value="KJFK",
        help="One or more 4-letter ICAOs, comma-separated. Example: KJFK, KORD, KSEA",
    )

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
        help="X-axis range, in hours past the cycle time.",
    )

    run_button = st.button("Run comparison", type="primary", use_container_width=True)

    st.divider()
    st.caption("Tomorrow.io is optional — leave the API key unset to use NOAA models only.")
    if os.environ.get("TOMORROWIO_API_KEY"):
        st.success("✓ Tomorrow.io key loaded")
    else:
        st.info("ⓘ No Tomorrow.io key configured")


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------
if run_button:
    icaos = [s.strip().upper() for s in icaos_raw.split(",") if s.strip()]
    if not icaos:
        st.error("Enter at least one ICAO code.")
        st.stop()

    # Resolve cycle
    if cycle_mode == "Auto (latest complete)":
        with st.spinner("Probing NOMADS for latest complete cycle…"):
            cycle_iso = cached_latest_cycle(tuple(icaos))
        if cycle_iso is None:
            st.error(
                "No complete cycle found within the last 8 probes. "
                "NOMADS may be experiencing issues, or all stations are unresolvable."
            )
            st.stop()
        cycle = datetime.fromisoformat(cycle_iso)
    else:
        today = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        cycle = today.replace(hour=int(specific_hour))
        if cycle > datetime.now(timezone.utc):
            cycle = cycle - timedelta(days=1)
        cycle_iso = cycle.isoformat()

    st.info(f"Using cycle: **{cycle:%Y-%m-%d %H:%M UTC}**")

    # Run the comparison
    with st.spinner("Fetching forecasts…"):
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
    
    # DEBUG: show METAR ceiling data
    if metars_df is not None and len(metars_df) > 0:
        st.write("DEBUG METAR data:")
        st.dataframe(metars_df[["station_id", "obs_time", "vsby_sm", "ceiling_ft", "ceiling_unlimited"]])
    
    # DEBUG: show raw AWC response for KPIA
    if "KPIA" in icao_list:
        import requests
        r = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": "KPIA", "hours": 6, "format": "json"},
            headers={"User-Agent": "wx_compare/1.0"},
            timeout=15,
        )
        st.write("DEBUG raw AWC KPIA response:")
        st.json(r.json()[:2])  # Just first 2 METARs

    if unresolved:
        st.warning(f"Could not resolve these ICAOs: {', '.join(unresolved)}")

    if not resolved:
        st.error("No stations could be resolved. Check your ICAO codes.")
        st.stop()

    # Show model counts
    if len(df) > 0:
        cols = st.columns(len(df["model"].unique()))
        for col, (model, count) in zip(cols, df["model"].value_counts().items()):
            col.metric(label=model, value=f"{count} rows")
    else:
        st.warning("Comparison returned no data.")
        st.stop()

    # Plot per station
    from compare import plot_comparison_interactive

    for s in resolved:
        st.subheader(s.icao)
        fig = plot_comparison_interactive(
            df, s.icao,
            cycle=cycle,
            hours_ahead=hours_ahead,
            metars_df=metars_df,
        )
        # Streamlit will handle width — override the function's default
        fig.update_layout(width=None, autosize=True)
        st.plotly_chart(fig, use_container_width=True)

    # Raw data download (optional but nice for power users)
    with st.expander("Raw data", expanded=False):
        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download as CSV",
            data=csv,
            file_name=f"wx_compare_{cycle:%Y%m%d_%H}Z.csv",
            mime="text/csv",
        )

else:
    # Initial state — landing message
    st.info("Enter ICAO codes in the sidebar and click **Run comparison**.")
    st.markdown(
        """
        ### Plot Description

        - Fetches the latest visibility and ceiling forecast data from the models below
        - The forecast horizon slider adjusts the end range of the model data 

        ### Models

        | Source | Hours | 
        | --- | --- | 
        | HRRR | 0-18 | 
        | GFS MOS (MAV) | 6-72 |
        | GFS LAMP (LAV) | 1-25 | 
        | NBM (NBH + NBS) | 1-72 | 
        | Tomorrow.io | 1-120 | 
        """
    )
