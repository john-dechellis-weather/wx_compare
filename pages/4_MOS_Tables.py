"""MOS Tables — hourly NBM + GFS LAMP for one airport.

Transposed layout: time runs across columns, fields run down rows.
Aviation-category tiered color coding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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


_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_mos_tables(icao: str, cycle_iso: str) -> pd.DataFrame:
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

    df = df_long[df_long["station_id"] == icao.upper()].copy()
    nbm = df[df["model"] == "NBM"].set_index("valid_time")
    lamp = df[df["model"] == "GFS_LAMP"].set_index("valid_time")

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
    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_latest_cycle_mos(icao: str) -> str | None:
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
    if row is None:
        return None
    try:
        v = row[col]
    except (KeyError, IndexError):
        return None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v


def _fmt_vis(v):
    if v is None or pd.isna(v):
        return "—"
    if v == int(v):
        return f"{int(v)}"
    return f"{v:g}"


def _fmt_cig(cig, unl):
    if unl is True:
        return "UNL"
    if cig is None or pd.isna(cig):
        return "—"
    hundreds = int(round(cig / 100))
    return f"{hundreds:03d}"


def _fmt_wind(dir_deg, spd_kt, gst_kt):
    if spd_kt is None or pd.isna(spd_kt):
        return "—"
    if dir_deg is not None and not pd.isna(dir_deg):
        d = f"{int(dir_deg):03d}"
    else:
        d = "VRB"
    s = f"{int(spd_kt):02d}"
    if gst_kt is not None and not pd.isna(gst_kt):
        g = f"G{int(gst_kt):02d}"
    else:
        g = ""
    return f"{d}{s}{g}KT"


def _fmt_time(t):
    return pd.to_datetime(t).strftime("%m/%d %HZ")


def _row_needs_highlight(row) -> bool:
    for v in (row["NBM_vis_sm"], row["LAMP_vis_sm"]):
        if v is not None and not pd.isna(v) and v < 3:
            return True
    for cig, unl in [
        (row["NBM_cig_ft"], row["NBM_cig_unl"]),
        (row["LAMP_cig_ft"], row["LAMP_cig_unl"]),
    ]:
        if unl is True:
            continue
        if cig is not None and not pd.isna(cig) and cig < 3000:
            return True
    for spd, gst in [
        (row["NBM_wind_spd"], row["NBM_wind_gst"]),
        (row["LAMP_wind_spd"], row["LAMP_wind_gst"]),
    ]:
        for w in (spd, gst):
            if w is not None and not pd.isna(w) and w >= 25:
                return True
    return False


def _build_summary(df_flags) -> str | None:
    now = datetime.now(timezone.utc)
    upcoming = df_flags[df_flags["valid_time"] >= now]
    if len(upcoming) == 0:
        return None
    hits = upcoming[upcoming["flagged"]]
    if len(hits) == 0:
        return "No low-condition periods forecast in the visible window."
    first = hits.iloc[0]
    return (
        f"⚠ Next low-condition period starts at "
        f"**{_fmt_time(first['valid_time'])}** (f+{int(first['fhr'])})"
    )


st.title("MOS Tables")
st.caption("Side-by-side hourly NBM + GFS LAMP for one airport.")

with st.sidebar:
    st.header("Airport")
    icao_input = st.text_input(
        "ICAO code",
        value="KJFK",
        max_chars=4,
    ).strip().upper()

    st.divider()
    run_button = st.button("Refresh", type="primary", use_container_width=True)

    st.divider()
    st.caption(
        "**Color coding:**\n\n"
        "Yellow: vis < 3sm, cig < 3000ft, wind ≥ 25kt\n\n"
        "Orange: vis < 2sm, cig ≤ 2000ft, wind ≥ 30kt\n\n"
        "Red: vis < 1sm, cig ≤ 1000ft, wind ≥ 35kt\n\n"
        "Pink: vis ≤ 0.5sm, cig < 400ft, wind ≥ 40kt"
    )


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

    df["flagged"] = df.apply(_row_needs_highlight, axis=1)

    summary = _build_summary(df)
    if summary:
        st.markdown(summary)

    df_clipped = df[df["fhr"] <= 25].copy()

    if len(df_clipped) == 0:
        st.warning("No data in the 0-25 hour overlap window.")
        st.stop()

    time_cols = [f"{t:%m/%d %HZ}" for t in df_clipped["valid_time"]]

    row_fhr = [f"f+{int(f)}" for f in df_clipped["fhr"]]
    row_nbm_vis = [_fmt_vis(v) for v in df_clipped["NBM_vis_sm"]]
    row_lamp_vis = [_fmt_vis(v) for v in df_clipped["LAMP_vis_sm"]]
    row_nbm_cig = [_fmt_cig(c, u) for c, u in zip(
        df_clipped["NBM_cig_ft"], df_clipped["NBM_cig_unl"])]
    row_lamp_cig = [_fmt_cig(c, u) for c, u in zip(
        df_clipped["LAMP_cig_ft"], df_clipped["LAMP_cig_unl"])]
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

    _PINK   = "background-color: #660066; color: #FF80FF;"
    _RED    = "background-color: #660000; color: #FF3333;"
    _ORANGE = "background-color: #663300; color: #FF8000;"
    _YELLOW = "background-color: #666600; color: #FFFF00;"

    def _vis_style(v):
        if v is None or pd.isna(v):
            return ""
        if v <= 0.5: return _PINK
        if v < 1:    return _RED
        if v < 2:    return _ORANGE
        if v < 3:    return _YELLOW
        return ""

    def _cig_style(cig, unl):
        if unl is True or cig is None or pd.isna(cig):
            return ""
        if cig < 400:   return _PINK
        if cig <= 1000: return _RED
        if cig <= 2000: return _ORANGE
        if cig < 3000:  return _YELLOW
        return ""

    def _wind_style(spd, gst):
        vals = [x for x in (spd, gst) if x is not None and not pd.isna(x)]
        if not vals:
            return ""
        worst = max(vals)
        if worst >= 40: return _PINK
        if worst >= 35: return _RED
        if worst >= 30: return _ORANGE
        if worst >= 25: return _YELLOW
        return ""

    def _cell_style(row_label, col_idx):
        r = df_clipped.iloc[col_idx]
        if row_label == "NBM VIS":
            return _vis_style(r["NBM_vis_sm"])
        if row_label == "LAMP VIS":
            return _vis_style(r["LAMP_vis_sm"])
        if row_label == "NBM CIG":
            return _cig_style(r["NBM_cig_ft"], r["NBM_cig_unl"])
        if row_label == "LAMP CIG":
            return _cig_style(r["LAMP_cig_ft"], r["LAMP_cig_unl"])
        if row_label == "NBM Wind":
            return _wind_style(r["NBM_wind_spd"], r["NBM_wind_gst"])
        if row_label == "LAMP Wind":
            return _wind_style(r["LAMP_wind_spd"], r["LAMP_wind_gst"])
        return ""

    def _style_dataframe(df_to_style):
        styles = pd.DataFrame(
            "", index=df_to_style.index, columns=df_to_style.columns
        )
        for row_label in df_to_style.index:
            for col_idx, col_name in enumerate(df_to_style.columns):
                styles.loc[row_label, col_name] = _cell_style(row_label, col_idx)
        return styles

    styled = display.style.apply(_style_dataframe, axis=None)

    # Wrap with CSS + render as HTML directly (bypasses Streamlit widget)
    table_css = """
    <style>
    .mos-wrap {
        overflow-x: auto;
        background: #000000;
        padding: 4px;
        border: 1px solid #003300;
    }
    .mos-wrap table {
        border-collapse: collapse;
        font-family: 'Courier New', monospace;
        font-size: 8px;
        background: #000000;
        color: #00FF00;
        margin: 0;
    }
    .mos-wrap th, .mos-wrap td {
        border: 1px solid #003300;
        padding: 1px 2px;
        text-align: center;
        white-space: nowrap;
        min-width: 32px;
        max-width: 32px;
        color: #00FF00;
    }
    .mos-wrap thead th {
        background: #001100;
        font-weight: bold;
    }
    .mos-wrap tbody th {
        background: #001100;
        text-align: left;
        min-width: 60px;
        padding: 1px 4px;
    }
    </style>
    """

    html = styled.to_html()
    st.markdown(table_css + f'<div class="mos-wrap">{html}</div>',
                unsafe_allow_html=True)

    csv = display.to_csv().encode("utf-8")
    st.download_button(
        label="Download as CSV",
        data=csv,
        file_name=f"mos_tables_{icao_input}_{cycle:%Y%m%d_%H}Z.csv",
        mime="text/csv",
    )

else:
    st.info("Enter an ICAO code in the sidebar and click **Refresh**.")