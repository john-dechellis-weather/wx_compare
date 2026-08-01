"""Airport Alerts — flags stations whose TAFs forecast VIS/CIG/TSRA
below thresholds within a user-selected time window.

Uses AWC's API + avwx-engine (see core/taf.py) for TAF parsing.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Airport Alerts",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()


# ---------------------------------------------------------------------------
# JetBlue destinations — static list
# ---------------------------------------------------------------------------
# Duplicates removed; ordering preserved from user's original list.
# Some international stations may not publish TAFs to AWC and will appear
# in the "TAF unavailable" list at the bottom of the page.
JETBLUE_ICAOS = [
    # Northeast US
    "KJFK", "KEWR", "KLGA", "KHPN", "KISP", "KPHL", "KBOS", "KORH", "KBDL",
    "KPVD", "KPWM", "KPQI", "KACK", "KHYA", "KMVY", "KALB", "KSYR", "KROC",
    "KBUF", "KPIT",
    # Mid-Atlantic
    "KDCA", "KBWI", "KRIC", "KORF", "KILM", "KRDU", "KCLT", "KCHS", "KSAV",
    # Southeast + Florida
    "KJAX", "KVPS", "KVRB", "KMCO", "KDAB", "KTPA", "KSRQ", "KRSW", "KPBI",
    "KDJT", "KFLL", "KEYW",
    # Midwest
    "KORD", "KMKE", "KTVC", "KDTW", "KCLE", "KBNA", "KATL", "KMSY",
    # Central / Texas
    "KDFW", "KAUS", "KIAH", "KABQ", "KPHX",
    # SoCal
    "KBUR", "KLAX", "KSAN", "KONT", "KLAS",
    # Northwest / Mountain
    "KSFO", "KRNO", "KSMF", "KSLC", "KBZN", "KDEN", "KHDN", "KSEA", "KPDX",
    "CYVR",
    # Caribbean / Bermuda / Bahamas
    "TXKF", "MYNN", "MBPV", "TJSJ", "TJPS", "TJBQ", "TIST", "TISX", "TNCM",
    "TKPK", "TAPA", "TLPL", "TVSA", "TBPB", "TGPY", "TTPP",
    # Guyana + Dominican Republic
    "SYCJ", "MDST", "MDSD", "MDPP", "MDPC",
    # Curacao / Aruba / Bonaire / Jamaica / Costa Rica
    "TNCA", "TNCC", "TNCB", "MKJP", "MKJS", "MWCR",
    # Colombia, Ecuador, Costa Rica, Guatemala, Belize, Honduras, Mexico
    "SKCG", "SKRG", "SEGU", "MROC", "MRLB", "MGGT", "MZBZ", "MHLM",
    "MMUN", "MMSD",
    # Europe
    "EGLL", "EGKK", "EIDW", "EGPF", "LFPG", "EHAM", "LEMD", "LEBL", "LIMC",
    # Additional Colombia + Brazil
    "SKCL", "SBAQ",
    # Mid-Atlantic + Ohio / Indiana
    "KCMH", "KIND",
]


# ---------------------------------------------------------------------------
# Cached analysis — TAFs update every 6 hours; 15-min cache is fresh enough
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def cached_analyze(
    icaos_tuple: tuple[str, ...],
    window_start_iso: str,
    window_end_iso: str,
    vis_threshold_sm: float,
    ceiling_threshold_ft: int,
    tsra_enabled: bool,
):
    """Run TAF analysis. Cached by exact parameter combination."""
    from core.taf import analyze_tafs
    return analyze_tafs(
        icaos=list(icaos_tuple),
        window_start=datetime.fromisoformat(window_start_iso),
        window_end=datetime.fromisoformat(window_end_iso),
        vis_threshold_sm=vis_threshold_sm,
        ceiling_threshold_ft=ceiling_threshold_ft,
        tsra_enabled=tsra_enabled,
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Airport Alerts")
st.caption(
    f"Scans TAFs from {len(JETBLUE_ICAOS)} JetBlue destinations and flags "
    "airports forecast to see low visibility, low ceilings, or thunderstorms."
)

with st.sidebar:
    st.header("Alert thresholds")

    vis_threshold = st.slider(
        "Visibility threshold (sm)",
        min_value=0.5, max_value=6.0, value=2.0, step=0.5,
        help="Flag airports forecast BELOW this value.",
    )
    ceiling_threshold = st.slider(
        "Ceiling threshold (ft AGL)",
        min_value=200, max_value=3000, value=1000, step=100,
        help="Flag airports forecast BELOW this value.",
    )
    tsra_enabled = st.checkbox(
        "Flag thunderstorms (TS/TSRA)",
        value=True,
        help="Includes TS, TSRA, +TSRA, -TSRA. Excludes VCTS (vicinity).",
    )

    st.divider()
    st.header("Time window")

    hours_ahead = st.slider(
        "Alert horizon (hours from now)",
        min_value=1, max_value=24, value=12, step=1,
        help="How far into the future to scan. TAFs typically cover 24-30 hours.",
    )

    st.divider()
    run_button = st.button(
        "Refresh alerts", type="primary", use_container_width=True
    )


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
if run_button:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    window_end = now + timedelta(hours=hours_ahead)

    st.info(
        f"Window: **{now:%Y-%m-%d %H:%M UTC}** to **{window_end:%H:%M UTC}** "
        f"(next {hours_ahead}h)"
    )

    with st.spinner(f"Fetching TAFs for {len(JETBLUE_ICAOS)} stations..."):
        try:
            results = cached_analyze(
                icaos_tuple=tuple(JETBLUE_ICAOS),
                window_start_iso=now.isoformat(),
                window_end_iso=window_end.isoformat(),
                vis_threshold_sm=vis_threshold,
                ceiling_threshold_ft=ceiling_threshold,
                tsra_enabled=tsra_enabled,
            )
        except Exception as e:
            st.error(f"Failed to fetch TAFs: {e}")
            st.stop()

    # Three tables side-by-side
    col_vis, col_ceil, col_tsra = st.columns(3, gap="medium")

    with col_vis:
        st.subheader(f"Low visibility (<{vis_threshold} sm)")
        if results.vis_alerts:
            df = pd.DataFrame([
                {"ICAO": a.icao,
                 "Min vis (sm)": a.min_vis_sm,
                 "Worst period": a.worst_period_label}
                for a in results.vis_alerts
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.write("_No airports flagged._")

    with col_ceil:
        st.subheader(f"Low ceilings (<{ceiling_threshold} ft)")
        if results.ceiling_alerts:
            df = pd.DataFrame([
                {"ICAO": a.icao,
                 "Min ceiling (ft)": a.min_ceiling_ft,
                 "Worst period": a.worst_period_label}
                for a in results.ceiling_alerts
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.write("_No airports flagged._")

    with col_tsra:
        st.subheader("Thunderstorms (TS/TSRA)")
        if not tsra_enabled:
            st.write("_TSRA alerts disabled in sidebar._")
        elif results.tsra_alerts:
            df = pd.DataFrame([
                {"ICAO": a.icao,
                 "Code": a.weather_code,
                 "Period": a.period_label}
                for a in results.tsra_alerts
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.write("_No airports flagged._")

    # TAF unavailable + parse errors — smaller notes at bottom
    st.divider()
    with st.expander(
        f"TAF unavailable for {len(results.unavailable_icaos)} stations",
        expanded=False,
    ):
        if results.unavailable_icaos:
            st.write(", ".join(results.unavailable_icaos))
        else:
            st.write("All stations returned a TAF.")

    # Surface parse errors if any occurred (should be rare)
    if results.parse_errors:
        st.divider()
        with st.expander(
            f"Parse errors on {len(results.parse_errors)} stations",
            expanded=False,
        ):
            for icao, err in results.parse_errors.items():
                st.write(f"**{icao}**: {err}")

else:
    st.info("Adjust thresholds and click **Refresh alerts** in the sidebar.")

    st.markdown(
        """
        ### What this does

        Scans the latest TAF for every JetBlue destination and flags airports
        forecast to experience:

        - **Low visibility** — below a threshold you set (default: 2 sm)
        - **Low ceilings** — below a threshold you set (default: 1000 ft)
        - **Thunderstorms** — TS, TSRA, or +TSRA (excludes VCTS)

        Only forecast periods that overlap your chosen time window count.
        A station is listed once per table with its worst value and the
        forecast period responsible for it.

        ### Sources

        Latest raw TAFs from
        [aviationweather.gov](https://aviationweather.gov/api/data/taf),
        parsed with the [avwx-engine](https://github.com/avwx-rest/avwx-engine)
        library. TAFs are cached for 15 minutes to reduce load on AWC.
        """
    )
