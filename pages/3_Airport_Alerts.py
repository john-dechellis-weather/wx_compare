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
    "KJAX", "KVPS", "KVRB", "KMCO", "KDAB", "KTPA", "KSRQ", "KRSW", "KDJT",
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


def _td(text, bg="#FFFFFF", fg="#000000", bold=False,
        align="left") -> str:
    weight = "bold" if bold else "normal"
    return (
        f'<td style="background-color:{bg}; color:{fg}; -webkit-text-fill-color:{fg}; '
        f"font-family:{_FONT}; font-size:11px; padding:3px 10px; "
        f"border:1px solid {_WHITE}; font-weight:{weight}; "
        f'text-align:{align}; white-space:nowrap;">{text}</td>'
    )


def _th(text, align="left") -> str:
    return (
        f'<td style="background-color:#FFFFFF; color:#000000; '
        f"-webkit-text-fill-color:#000000; "
        f"font-family:{_FONT}; font-size:11px; padding:4px 10px; "
        f"border:1px solid {_WHITE}; font-weight:bold; "
        f'text-align:{align}; text-decoration:underline; '
        f'white-space:nowrap;">{text}</td>'
    )


def _table(header_cells: list[str], body_rows: list[str]) -> str:
    return (
        f'<table style="border-collapse:collapse; background-color:#FFFFFF; '
        f'border:2px solid {_WHITE}; width:100%;">'
        f"<tr>{''.join(header_cells)}</tr>"
        f"{''.join(body_rows)}"
        f"</table>"
    )


def _no_alerts() -> str:
    return (
        f'<div style="background-color:#FFFFFF; border:2px solid {_WHITE}; '
        f"color:#000000; -webkit-text-fill-color:#000000; font-family:{_FONT}; font-size:11px; "
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


def render_wind_table(alerts) -> str:
    header = [_th("ICAO"), _th("WIND (KT)", align="right"), _th("WORST PERIOD")]
    rows = []
    for a in alerts:
        rows.append(
            "<tr>"
            + _td(a.icao, bold=True)
            + _td(a.wind_str, bold=True, align="right")
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


def _fmt_vis_obs(v) -> str:
    if v is None:
        return "-"
    return f"{v:g}"


def _fmt_cig_obs(c, unl) -> str:
    if unl or c is None:
        return "UNL"
    return f"{int(round(c / 100)):03d}"


def _fmt_wind_obs(spd, gst) -> str:
    if spd is None:
        return "-"
    s = f"{int(spd):02d}"
    return f"{s}G{int(gst):02d}" if gst else s


def render_metar_table(rows) -> str:
    """rows: list of dicts with icao, obs_time, vis, cig, cig_unl, spd, gst,
    raw, and breach flags vis_bad/cig_bad/wind_bad."""
    header = [_th("ICAO"), _th("TIME"), _th("VIS", align="right"),
              _th("CIG", align="right"), _th("WIND", align="right"),
              _th("RAW METAR")]
    body = []
    for r in rows:
        def cell(text, bad, align="right"):
            if bad:
                return _td(text, bg=_RED, fg=_WHITE, bold=True, align=align)
            return _td(text, align=align)
        body.append(
            "<tr>"
            + _td(r["icao"], bold=True)
            + _td(r["obs_time"].strftime("%H:%MZ"))
            + cell(_fmt_vis_obs(r["vis"]), r["vis_bad"])
            + cell(_fmt_cig_obs(r["cig"], r["cig_unl"]), r["cig_bad"])
            + cell(_fmt_wind_obs(r["spd"], r["gst"]), r["wind_bad"])
            + _td(r["raw"])
            + "</tr>"
        )
    return _table(header, body)


@st.cache_data(ttl=300, show_spinner=False)
def cached_current_metars(
    icaos_tuple: tuple[str, ...],
    vis_threshold_sm: float,
    ceiling_threshold_ft: int,
    wind_threshold_kt: int,
):
    """Latest METAR per station; return rows breaching any threshold.
    5-minute cache — METARs are hourly with specials in between."""
    from core.metar import fetch_metars

    by_station = fetch_metars(list(icaos_tuple), hours_back=2)
    rows = []
    for icao, obs_list in by_station.items():
        if not obs_list:
            continue
        o = obs_list[-1]  # latest
        vis_bad = o.vsby_sm is not None and o.vsby_sm < vis_threshold_sm
        cig_bad = (not o.ceiling_unlimited and o.ceiling_ft is not None
                   and o.ceiling_ft < ceiling_threshold_ft)
        wind_max = max(
            [x for x in (o.wind_speed_kt, o.wind_gust_kt) if x is not None],
            default=None,
        )
        wind_bad = wind_max is not None and wind_max >= wind_threshold_kt
        if vis_bad or cig_bad or wind_bad:
            rows.append({
                "icao": icao,
                "obs_time": o.obs_time,
                "vis": o.vsby_sm, "vis_bad": vis_bad,
                "cig": o.ceiling_ft, "cig_unl": o.ceiling_unlimited,
                "cig_bad": cig_bad,
                "spd": o.wind_speed_kt, "gst": o.wind_gust_kt,
                "wind_bad": wind_bad,
                "raw": o.raw_text,
            })
    rows.sort(key=lambda r: r["icao"])
    return rows


# ---------------------------------------------------------------------------
# Airport status board: one row per alerting airport, worst-first
# ---------------------------------------------------------------------------
_MAGENTA = "#FF00FF"
_YELLOW = "#FFFF00"
_ORANGE = "#FF9900"


def _wind_max_kt(wind_str: str):
    """Max of speed/gust from a TAF wind group like '29025G42KT'.
    Proper group parse - naive digit-matching would read 29025."""
    import re
    m = re.match(r"(?:VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?KT",
                 (wind_str or "").strip())
    if m:
        spd = int(m.group(1))
        gst = int(m.group(2)) if m.group(2) else 0
        return max(spd, gst)
    nums = [int(x) for x in re.findall(r"\d{2,3}", wind_str or "")]
    return max(nums) if nums else None


def build_status_board(results, metar_rows):
    """One aggregated row per alerting airport with severity rank
    (0 magenta / 1 red / 2 yellow-orange) and the driving-condition
    chip."""
    board: dict = {}

    def ent(icao):
        return board.setdefault(icao, {
            "vis": None, "vis_p": "", "ceil": None, "ceil_p": "",
            "ts": None, "ts_p": "", "wind": None, "wind_p": "",
            "raw": "", "obs": "",
        })

    for a in results.vis_alerts:
        e = ent(a.icao)
        e["vis"], e["vis_p"] = a.min_vis_sm, a.worst_period_label
    for a in results.ceiling_alerts:
        e = ent(a.icao)
        e["ceil"], e["ceil_p"] = (a.min_ceiling_ft,
                                  a.worst_period_label)
    for a in results.tsra_alerts:
        e = ent(a.icao)
        e["ts"], e["ts_p"] = a.weather_code, a.period_label
    for a in results.wind_alerts:
        e = ent(a.icao)
        e["wind"], e["wind_p"] = a.wind_str, a.worst_period_label
    for r in (metar_rows or []):
        if r["icao"] in board or True:
            e = ent(r["icao"])
            e["raw"] = r["raw"]
            e["obs"] = r["obs_time"].strftime("%H:%MZ")

    rows = []
    for icao, e in board.items():
        cands = []   # (rank, chip, color, period)
        if e["vis"] is not None:
            v = e["vis"]
            tier = 0 if v < 1 else (1 if v < 3 else 2)
            col = (_MAGENTA, _RED, _YELLOW)[tier]
            cands.append((tier, f"{v:g}SM", col, e["vis_p"]))
        if e["ceil"] is not None:
            c = e["ceil"]
            tier = 0 if c < 500 else (1 if c < 1000 else 2)
            col = (_MAGENTA, _RED, _YELLOW)[tier]
            cands.append((tier, f"CIG {c}", col, e["ceil_p"]))
        if e["ts"]:
            cands.append((1, e["ts"], _RED, e["ts_p"]))
        if e["wind"]:
            w = _wind_max_kt(e["wind"])
            if w is not None and w >= 40:
                cands.append((0, f"G{w}", _MAGENTA, e["wind_p"]))
            elif w is not None and w >= 35:
                cands.append((1, f"G{w}", _RED, e["wind_p"]))
            else:
                cands.append((2, e["wind"], _ORANGE, e["wind_p"]))
        if not cands:
            continue
        cands.sort(key=lambda c: c[0])
        rank, chip, color, period = cands[0]
        rows.append((rank, icao, chip, color, period, e))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def render_status_board(rows) -> str:
    header = [
        _th("ICAO"), _th("ALERT"), _th("VIS", align="right"),
        _th("CIG", align="right"), _th("TS"),
        _th("WIND", align="right"), _th("WORST PERIOD"),
    ]
    body = []
    for rank, icao, chip, color, period, e in rows:
        def dim(v):
            return str(v) if v not in (None, "") else "-"
        fg = "#000000" if color in (_YELLOW, _ORANGE) else _WHITE
        body.append(
            "<tr>"
            + _td(icao, bold=True)
            + _td(chip, bg=color, fg=fg, bold=True)
            + _td(dim(e["vis"]), align="right")
            + _td(dim(e["ceil"]), align="right")
            + _td(dim(e["ts"]))
            + _td(dim(e["wind"]), align="right")
            + _td(period)
            + "</tr>"
        )
    return _table(header, body)


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
    wind_threshold = st.slider(
        "Wind/gust threshold (kt) — METARs",
        min_value=15, max_value=50, value=25, step=5,
        help="Current-METAR section flags sustained or gust at/above this.",
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

    # Airport status board: one row per alerting airport,
    # severity-sorted, with the driving condition as a colored chip.
    with st.spinner("Fetching current METARs..."):
        try:
            metar_rows = cached_current_metars(
                icaos_tuple=tuple(JETBLUE_ICAOS),
                vis_threshold_sm=vis_threshold,
                ceiling_threshold_ft=ceiling_threshold,
                wind_threshold_kt=wind_threshold,
            )
        except Exception as e:
            metar_rows = []
            st.warning(f"METAR fetch failed: {e}")

    col_taf, col_metar = st.columns(2, gap="medium")

    with col_taf:
        st.subheader("TAF alerts")
        st.caption(
            "Forecast to breach thresholds - worst first. Chip = "
            "driving condition (magenta severe / red / "
            "yellow-orange advisory)."
        )
        if not tsra_enabled:
            st.caption("TSRA alerts disabled in sidebar.")
        board_rows = build_status_board(results, metar_rows)
        if board_rows:
            st.markdown(render_status_board(board_rows),
                        unsafe_allow_html=True)
            n_sev = sum(1 for r in board_rows if r[0] == 0)
            st.caption(
                f"{len(board_rows)} airports alerting"
                + (f" | {n_sev} severe" if n_sev else "")
            )
        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)

    with col_metar:
        st.subheader("Current METARs at/beyond thresholds")
        st.caption(
            f"Latest ob per station - vis < {vis_threshold:g} sm, "
            f"cig < {ceiling_threshold} ft, wind >= "
            f"{wind_threshold} kt. Red cell = breaching value."
        )
        if metar_rows:
            st.markdown(render_metar_table(metar_rows),
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
