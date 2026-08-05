"""Airport Alerts — flags stations whose TAFs forecast VIS/CIG/TSRA
below thresholds within a user-selected time window.

Uses AWC's API + avwx-engine (see core/taf.py) for TAF parsing.

Tables are hand-built HTML with inline styles (terminal green-on-black,
matching the MOS Tables page) because st.dataframe ignores page CSS.
Critical severity — vis < 1 sm or ceiling < 400 ft — highlights the
ICAO and value cells in red.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import streamlit as st

st.set_page_config(
    page_title="BlueMet — Airport Alerts",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


# ---------------------------------------------------------------------------
# JetBlue destinations — static list
# ---------------------------------------------------------------------------
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

# Critical-severity thresholds (fixed): red highlight in tables
CRITICAL_VIS_SM = 1.0
CRITICAL_CIG_FT = 400


# ---------------------------------------------------------------------------
# Terminal-style HTML table builders (st.dataframe ignores page CSS)
# ---------------------------------------------------------------------------
_GREEN = "#00FF00"
_BLACK = "#000000"
_RED = "#CC0000"
_WHITE = "#FFFFFF"
_FONT = "'Courier New', Courier, monospace"


def _td(text, bg=_BLACK, fg=_WHITE, bold=False, align="left") -> str:
    weight = "bold" if bold else "normal"
    return (
        f'<td style="background-color:{bg}; color:{fg}; -webkit-text-fill-color:{fg}; '
        f"font-family:{_FONT}; font-size:11px; padding:3px 10px; "
        f"border:1px solid {_GREEN}; font-weight:{weight}; "
        f'text-align:{align}; white-space:nowrap;">{text}</td>'
    )


def _th(text, align="left") -> str:
    return (
        f'<td style="background-color:{_BLACK}; color:{_WHITE}; '
        f"-webkit-text-fill-color:{_WHITE}; "
        f"font-family:{_FONT}; font-size:11px; padding:4px 10px; "
        f"border:1px solid {_GREEN}; font-weight:bold; "
        f'text-align:{align}; text-decoration:underline; '
        f'white-space:nowrap;">{text}</td>'
    )


def _table(header_cells: list[str], body_rows: list[str]) -> str:
    return (
        f'<table style="border-collapse:collapse; background-color:{_BLACK}; '
        f'border:2px solid {_GREEN}; width:100%;">'
        f"<tr>{''.join(header_cells)}</tr>"
        f"{''.join(body_rows)}"
        f"</table>"
    )


def _no_alerts() -> str:
    return (
        f'<div style="background-color:{_BLACK}; border:2px solid {_GREEN}; '
        f"color:{_WHITE}; -webkit-text-fill-color:{_WHITE}; font-family:{_FONT}; font-size:11px; "
        f'padding:6px 10px;">NO AIRPORTS FLAGGED</div>'
    )


def _fmt_vis(v: float) -> str:
    """0.5 -> '0.5', 2.0 -> '2', 1.75 -> '1.75'."""
    return f"{v:g}"


def render_vis_table(alerts) -> str:
    header = [_th("ICAO"), _th("MIN VIS (SM)", align="right"), _th("WORST PERIOD")]
    rows = []
    for a in alerts:
        critical = a.min_vis_sm < CRITICAL_VIS_SM
        bg = _RED if critical else _BLACK
        fg = _WHITE
        rows.append(
            "<tr>"
            + _td(a.icao, bg=bg, fg=fg, bold=critical)
            + _td(_fmt_vis(a.min_vis_sm), bg=bg, fg=fg, bold=critical, align="right")
            + _td(a.worst_period_label)
            + "</tr>"
        )
    return _table(header, rows)


def render_ceiling_table(alerts) -> str:
    header = [_th("ICAO"), _th("MIN CIG (FT)", align="right"), _th("WORST PERIOD")]
    rows = []
    for a in alerts:
        critical = a.min_ceiling_ft < CRITICAL_CIG_FT
        bg = _RED if critical else _BLACK
        fg = _WHITE
        rows.append(
            "<tr>"
            + _td(a.icao, bg=bg, fg=fg, bold=critical)
            + _td(str(a.min_ceiling_ft), bg=bg, fg=fg, bold=critical, align="right")
            + _td(a.worst_period_label)
            + "</tr>"
        )
    return _table(header, rows)


def render_tsra_table(alerts) -> str:
    header = [_th("ICAO"), _th("CODE"), _th("PERIOD")]
    rows = []
    for a in alerts:
        rows.append(
            "<tr>"
            + _td(a.icao)
            + _td(a.weather_code)
            + _td(a.period_label)
            + "</tr>"
        )
    return _table(header, rows)


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

    st.divider()
    st.markdown(
        '<span style="color:#CC0000; -webkit-text-fill-color:#CC0000; font-weight:bold;">'
        "RED highlight</span> = critical severity: "
        f"vis &lt; {_fmt_vis(CRITICAL_VIS_SM)} sm or ceiling &lt; "
        f"{CRITICAL_CIG_FT} ft.",
        unsafe_allow_html=True,
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
        st.subheader(f"Low visibility (<{_fmt_vis(vis_threshold)} sm)")
        if results.vis_alerts:
            st.markdown(render_vis_table(results.vis_alerts),
                        unsafe_allow_html=True)
        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)

    with col_ceil:
        st.subheader(f"Low ceilings (<{ceiling_threshold} ft)")
        if results.ceiling_alerts:
            st.markdown(render_ceiling_table(results.ceiling_alerts),
                        unsafe_allow_html=True)
        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)

    with col_tsra:
        st.subheader("Thunderstorms (TS/TSRA)")
        if not tsra_enabled:
            st.write("_TSRA alerts disabled in sidebar._")
        elif results.tsra_alerts:
            st.markdown(render_tsra_table(results.tsra_alerts),
                        unsafe_allow_html=True)
        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)

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

    if results.parse_errors:
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

        Rows highlighted in **red** mark critical severity: visibility below
        1 sm or ceiling below 400 ft.

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