"""MOS Tables — quick side-by-side hourly NBM + GFS LAMP for one airport.

Simple table view: one row per valid hour, columns grouped by field:
    Time | F+ | NBM VIS | LAMP VIS | NBM CIG | LAMP CIG | NBM Wind | LAMP Wind

Both NBM (72 hr) and LAMP (25 hr) are hourly; times align cleanly.
Rows below user-defined thresholds highlight red.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="BlueMet — MOS Tables",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


# ---------------------------------------------------------------------------
# Cache dir (persistent disk on Render, /tmp locally)
# ---------------------------------------------------------------------------
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cached fetch — reuse existing NBM + LAMP parsers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_mos_tables(icao: str, cycle_iso: str) -> pd.DataFrame:
    """Fetch NBM and LAMP for one station, return a merged wide DataFrame.

    Column layout: [time, fhr, NBM_vis, LAMP_vis, NBM_cig, LAMP_cig, NBM_wind, LAMP_wind]
    """
    from compare import compare_icaos
    from models import Nbm, GfsLamp

    cycle = datetime.fromisoformat(cycle_iso)
    df_long, resolved, unresolved = compare_icaos(
        icaos=[icao],
        cycle=cycle,
        cache_root=CACHE_ROOT,
        model_classes=[Nbm, GfsLamp],
    )
    if not resolved or len(df_long) == 0:
        return pd.DataFrame()

    # Filter to just this ICAO
    df = df_long[df_long["station_id"] == icao.upper()].copy()

    # Split by model, then pivot to wide
    nbm = df[df["model"] == "NBM"].set_index("valid_time")
    lamp = df[df["model"] == "GFS_LAMP"].set_index("valid_time")

    # Build the union of times, ascending
    all_times = sorted(set(nbm.index).union(set(lamp.index)))
    if not all_times:
        return pd.DataFrame()

    rows = []
    for t in all_times:
        n = nbm.loc[t] if t in nbm.index else None
        l = lamp.loc[t] if t in lamp.index else None
        fhr = int((t - cycle).total_seconds() // 3600)
        rows.append({
            "valid_time": t,
            "fhr": fhr,
            "NBM_vis_sm": _safe_get(n, "vsby_sm"),
            "LAMP_vis_sm": _safe_get(l, "vsby_sm"),
            "NBM_cig_ft": _safe_get(n, "ceiling_ft"),
            "NBM_cig_unl": _safe_get(n, "ceiling_unlimited"),
            "LAMP_cig_ft": _safe_get(l, "ceiling_ft"),
            "LAMP_cig_unl": _safe_get(l, "ceiling_unlimited"),
            "NBM_wind_dir": _safe_get(n, "wind_dir_deg"),
            "NBM_wind_spd": _safe_get(n, "wind_speed_kt"),
            "NBM_wind_gst": _safe_get(n, "wind_gust_kt"),
            "LAMP_wind_dir": _safe_get(l, "wind_dir_deg"),
            "LAMP_wind_spd": _safe_get(l, "wind_speed_kt"),
            "LAMP_wind_gst": _safe_get(l, "wind_gust_kt"),
        })
    out = pd.DataFrame(rows)
    return out


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_latest_cycle_mos(icao: str) -> str | None:
    """Latest complete cycle for the ICAO across NBM + LAMP."""
    from core.stations import StationResolver
    from core.cycle_select import find_latest_complete
    from models import Nbm, GfsLamp

    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved_pre, _ = resolver.resolve_many([icao])
    if not resolved_pre:
        return None

    probe_sources = [
        Nbm(cache_dir=CACHE_ROOT / "nbm"),
        GfsLamp(cache_dir=CACHE_ROOT / "gfs_lamp"),
    ]
    cycle = find_latest_complete(probe_sources, verbose=False)
    return cycle.isoformat() if cycle else None


def _safe_get(row, col):
    """Return a scalar from a pandas Series row, or None."""
    if row is None:
        return None
    try:
        v = row[col]
    except (KeyError, IndexError):
        return None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------
def _fmt_vis(v):
    """Compact decimal visibility: '0.5', '1', '1.5', '3', '10'."""
    if v is None or pd.isna(v):
        return "—"
    # Whole numbers show without decimal
    if v == int(v):
        return f"{int(v)}"
    return f"{v:g}"


def _fmt_cig(cig, unl):
    """Ceiling as 3-digit hundreds: '008', '025', '100', 'UNL'."""
    if unl is True:
        return "UNL"
    if cig is None or pd.isna(cig):
        return "—"
    hundreds = int(round(cig / 100))
    return f"{hundreds:03d}"


def _fmt_wind(dir_deg, spd_kt, gst_kt):
    """METAR-style wind: '18012KT' or '18012G26KT'."""
    if spd_kt is None or pd.isna(spd_kt):
        return "—"
    # Direction
    if dir_deg is not None and not pd.isna(dir_deg):
        d = f"{int(dir_deg):03d}"
    else:
        d = "VRB"
    # Sustained
    s = f"{int(spd_kt):02d}"
    # Gust (optional)
    if gst_kt is not None and not pd.isna(gst_kt):
        g = f"G{int(gst_kt):02d}"
    else:
        g = ""
    return f"{d}{s}{g}KT"


def _fmt_time(t):
    return pd.to_datetime(t).strftime("%m/%d %HZ")


# ---------------------------------------------------------------------------
# Row highlighting
# ---------------------------------------------------------------------------
def _row_needs_highlight(row, vis_thr, cig_thr, wind_thr) -> bool:
    """A row triggers highlight if any product's field crosses a threshold."""
    # Visibility
    for v in (row["NBM_vis_sm"], row["LAMP_vis_sm"]):
        if v is not None and not pd.isna(v) and v < vis_thr:
            return True
    # Ceiling (only real, non-unlimited values count)
    for cig, unl in [
        (row["NBM_cig_ft"], row["NBM_cig_unl"]),
        (row["LAMP_cig_ft"], row["LAMP_cig_unl"]),
    ]:
        if unl is True:
            continue
        if cig is not None and not pd.isna(cig) and cig < cig_thr:
            return True
    # Wind (sustained OR gust)
    for spd, gst in [
        (row["NBM_wind_spd"], row["NBM_wind_gst"]),
        (row["LAMP_wind_spd"], row["LAMP_wind_gst"]),
    ]:
        for w in (spd, gst):
            if w is not None and not pd.isna(w) and w > wind_thr:
                return True
    return False


# ---------------------------------------------------------------------------
# Summary bar — find the next hour that triggers a highlight
# ---------------------------------------------------------------------------
def _build_summary(df_flags, vis_thr, cig_thr, wind_thr) -> str | None:
    """Return a short summary of the next low-condition window, or None."""
    now = datetime.now(timezone.utc)
    upcoming = df_flags[df_flags["valid_time"] >= now]
    if len(upcoming) == 0:
        return None
    # Find first row that triggers highlight
    hits = upcoming[upcoming["flagged"]]
    if len(hits) == 0:
        return "No low-condition periods forecast in the visible window."
    first = hits.iloc[0]
    return (
        f"⚠ Next low-condition period starts at "
        f"**{_fmt_time(first['valid_time'])}** (f+{int(first['fhr'])})"
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("MOS Tables")
st.caption("Side-by-side hourly NBM + GFS LAMP for one airport.")

with st.sidebar:
    st.header("Airport")
    icao_input = st.text_input(
        "ICAO code",
        value="KJFK",
        max_chars=4,
        help="Single 4-letter ICAO (CONUS airports)",
    ).strip().upper()

    st.divider()
    st.header("Highlight thresholds")

    vis_thr = st.slider(
        "Visibility (sm) — flag if below",
        min_value=0.5, max_value=6.0, value=2.0, step=0.5,
    )
    cig_thr = st.slider(
        "Ceiling (ft) — flag if below",
        min_value=200, max_value=3000, value=1000, step=100,
    )
    wind_thr = st.slider(
        "Wind (kt) — flag if above (sustained or gust)",
        min_value=10, max_value=50, value=25, step=1,
    )

    st.divider()
    run_button = st.button("Refresh", type="primary", use_container_width=True)


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
if run_button:
    if not icao_input or len(icao_input) != 4:
        st.error("Enter a 4-letter ICAO code.")
        st.stop()

    with st.spinner("Probing NOMADS for latest complete cycle..."):
        cycle_iso = cached_latest_cycle_mos(icao_input)

    if cycle_iso is None:
        st.error("No complete cycle found within recent probes.")
        st.stop()

    cycle = datetime.fromisoformat(cycle_iso)
    st.info(f"Cycle: **{cycle:%Y-%m-%d %H:%M UTC}**  ·  Station: **{icao_input}**")

    with st.spinner("Fetching NBM + LAMP..."):
        df = cached_mos_tables(icao_input, cycle_iso)

    if len(df) == 0:
        st.warning("No data returned. Station may not be in NBM/LAMP.")
        st.stop()

    # Flag rows for highlighting
    df["flagged"] = df.apply(
        _row_needs_highlight, axis=1,
        args=(vis_thr, cig_thr, wind_thr),
    )

    # Summary bar
    summary = _build_summary(df, vis_thr, cig_thr, wind_thr)
    if summary:
        st.markdown(summary)

 # Clip to first 25 hours (overlap window where LAMP has data)
    df_clipped = df[df["fhr"] <= 25].copy()

    if len(df_clipped) == 0:
        st.warning("No data in the 0-25 hour overlap window.")
        st.stop()

    # Build column headers as time strings
    time_cols = [f"{t:%m/%d %HZ}" for t in df_clipped["valid_time"]]

    # Build each row of the transposed table
    # Row 1: F+ hour
    row_fhr = [f"f+{int(f)}" for f in df_clipped["fhr"]]
    # Row 2-3: Visibility
    row_nbm_vis = [_fmt_vis(v) for v in df_clipped["NBM_vis_sm"]]
    row_lamp_vis = [_fmt_vis(v) for v in df_clipped["LAMP_vis_sm"]]
    # Row 4-5: Ceiling
    row_nbm_cig = [_fmt_cig(c, u) for c, u in zip(
        df_clipped["NBM_cig_ft"], df_clipped["NBM_cig_unl"])]
    row_lamp_cig = [_fmt_cig(c, u) for c, u in zip(
        df_clipped["LAMP_cig_ft"], df_clipped["LAMP_cig_unl"])]
    # Row 6-7: Wind
    row_nbm_wind = [_fmt_wind(d, s, g) for d, s, g in zip(
        df_clipped["NBM_wind_dir"],
        df_clipped["NBM_wind_spd"],
        df_clipped["NBM_wind_gst"])]
    row_lamp_wind = [_fmt_wind(d, s, g) for d, s, g in zip(
        df_clipped["LAMP_wind_dir"],
        df_clipped["LAMP_wind_spd"],
        df_clipped["LAMP_wind_gst"])]

    display = pd.DataFrame({
        col: [
            row_fhr[i],
            row_nbm_vis[i], row_lamp_vis[i],
            row_nbm_cig[i], row_lamp_cig[i],
            row_nbm_wind[i], row_lamp_wind[i],
        ]
        for i, col in enumerate(time_cols)
    }, index=[
        "F+ hour",
        "NBM VIS", "LAMP VIS",
        "NBM CIG", "LAMP CIG",
        "NBM Wind", "LAMP Wind",
    ])

    # Cell-level highlighting: track which (row, col) violate thresholds
    # We iterate through df_clipped and mark cells red per-cell
    def _cell_style(row_label, col_idx):
        """Return CSS for one cell based on its (row_label, col_idx)."""
        r = df_clipped.iloc[col_idx]
        red = "background-color: #660000; color: #FFFF00;"
        # Visibility rows
        if row_label == "NBM VIS":
            v = r["NBM_vis_sm"]
            if v is not None and not pd.isna(v) and v < vis_thr:
                return red
        elif row_label == "LAMP VIS":
            v = r["LAMP_vis_sm"]
            if v is not None and not pd.isna(v) and v < vis_thr:
                return red
        # Ceiling rows (skip if unlimited)
        elif row_label == "NBM CIG":
            c, u = r["NBM_cig_ft"], r["NBM_cig_unl"]
            if u is not True and c is not None and not pd.isna(c) and c < cig_thr:
                return red
        elif row_label == "LAMP CIG":
            c, u = r["LAMP_cig_ft"], r["LAMP_cig_unl"]
            if u is not True and c is not None and not pd.isna(c) and c < cig_thr:
                return red
        # Wind rows (speed OR gust)
        elif row_label == "NBM Wind":
            s, g = r["NBM_wind_spd"], r["NBM_wind_gst"]
            for w in (s, g):
                if w is not None and not pd.isna(w) and w > wind_thr:
                    return red
        elif row_label == "LAMP Wind":
            s, g = r["LAMP_wind_spd"], r["LAMP_wind_gst"]
            for w in (s, g):
                if w is not None and not pd.isna(w) and w > wind_thr:
                    return red
        return ""

    # Apply the styling
    def _style_dataframe(df_to_style):
        styles = pd.DataFrame(
            "", index=df_to_style.index, columns=df_to_style.columns
        )
        for row_label in df_to_style.index:
            for col_idx, col_name in enumerate(df_to_style.columns):
                styles.loc[row_label, col_name] = _cell_style(row_label, col_idx)
        return styles

    # Terminal aesthetic: green on black
    # Highlighted cells use amber/red for contrast
    styled = display.style.apply(_style_dataframe, axis=None)
    styled = styled.set_properties(**{
        "background-color": "#000000",
        "color": "#00FF00",
        "font-family": "Courier New, monospace",
        "font-size": "12px",
        "text-align": "center",
        "padding": "2px 6px",
    })
    styled = styled.set_table_styles([
        {"selector": "th",
         "props": [
             ("background-color", "#000000"),
             ("color", "#00FF00"),
             ("font-family", "Courier New, monospace"),
             ("font-size", "12px"),
             ("font-weight", "bold"),
             ("text-align", "center"),
             ("padding", "2px 6px"),
             ("border", "1px solid #003300"),
         ]},
        {"selector": "td",
         "props": [
             ("border", "1px solid #003300"),
         ]},
    ])

    st.dataframe(styled, use_container_width=True, height=350)

    # CSV download
    csv = display.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download as CSV",
        data=csv,
        file_name=f"mos_tables_{icao_input}_{cycle:%Y%m%d_%H}Z.csv",
        mime="text/csv",
    )

else:
    st.info("Enter an ICAO code in the sidebar and click **Refresh**.")
    st.markdown(
        """
        ### About

        This page shows **NBM** (hourly, out to 72 h) and **GFS LAMP**
        (hourly, out to 25 h) side-by-side for one airport.

        Rows are highlighted red when any product forecasts:
        - Visibility below your threshold
        - Ceiling below your threshold
        - Wind speed or gust above your threshold

        A summary at the top shows the next expected low-condition period.
        """
    )
