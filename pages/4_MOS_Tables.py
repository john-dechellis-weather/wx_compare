"""MOS Tables — hourly NBM + GFS LAMP for one airport."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from html import escape

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
    rows = []
    for t in all_times:
        n = nbm.loc[t] if t in nbm.index else None
        l = lamp.loc[t] if t in lamp.index else None
        fhr = int((t - cycle).total_seconds() // 3600)
        rows.append({
            "valid_time": t, "fhr": fhr,
            "NBM_vis_sm": _safe(n, "vsby_sm"),
            "LAMP_vis_sm": _safe(l, "vsby_sm"),
            "NBM_cig_ft": _safe(n, "ceiling_ft"),
            "NBM_cig_unl": _safe(n, "ceiling_unlimited"),
            "LAMP_cig_ft": _safe(l, "ceiling_ft"),
            "LAMP_cig_unl": _safe(l, "ceiling_unlimited"),
            "NBM_wind_dir": _safe(n, "wind_dir_deg"),
            "NBM_wind_spd": _safe(n, "wind_speed_kt"),
            "NBM_wind_gst": _safe(n, "wind_gust_kt"),
            "LAMP_wind_dir": _safe(l, "wind_dir_deg"),
            "LAMP_wind_spd": _safe(l, "wind_speed_kt"),
            "LAMP_wind_gst": _safe(l, "wind_gust_kt"),
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


def _safe(row, col):
    if row is None:
        return None
    try:
        v = row[col]
    except (KeyError, IndexError):
        return None
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return v


def fmt_vis(v):
    if v is None or pd.isna(v):
        return "-"
    if v == int(v):
        return f"{int(v)}"
    return f"{v:g}"


def fmt_cig(cig, unl):
    if unl is True:
        return "UNL"
    if cig is None or pd.isna(cig):
        return "-"
    return f"{int(round(cig / 100)):03d}"


def fmt_wind(dr, sp, gs):
    if sp is None or pd.isna(sp):
        return "-"
    d = f"{int(dr):03d}" if (dr is not None and not pd.isna(dr)) else "VRB"
    s = f"{int(sp):02d}"
    g = f"G{int(gs):02d}" if (gs is not None and not pd.isna(gs)) else ""
    return f"{d}{s}{g}KT"


def vis_bg(v):
    if v is None or pd.isna(v): return None
    if v <= 0.5: return ("#CC00CC", "#FFFFFF")   # pink
    if v < 1: return ("#CC0000", "#FFFFFF")       # red
    if v < 2: return ("#CC6600", "#FFFFFF")       # orange
    if v < 3: return ("#CCCC00", "#FFFFFF")       # yellow
    return None


def cig_bg(c, u):
    if u is True or c is None or pd.isna(c): return None
    if c < 400: return ("#CC00CC", "#FFFFFF")
    if c <= 1000: return ("#CC0000", "#FFFFFF")
    if c <= 2000: return ("#CC6600", "#FFFFFF")
    if c < 3000: return ("#CCCC00", "#FFFFFF")
    return None


def wind_bg(s, g):
    vals = [x for x in (s, g) if x is not None and not pd.isna(x)]
    if not vals: return None
    w = max(vals)
    if w >= 40: return ("#CC00CC", "#FFFFFF")
    if w >= 35: return ("#CC0000", "#FFFFFF")
    if w >= 30: return ("#CC6600", "#FFFFFF")
    if w >= 25: return ("#CCCC00", "#FFFFFF")
    return None


def row_flagged(row):
    for v in (row["NBM_vis_sm"], row["LAMP_vis_sm"]):
        if v is not None and not pd.isna(v) and v < 3:
            return True
    for c, u in [(row["NBM_cig_ft"], row["NBM_cig_unl"]),
                 (row["LAMP_cig_ft"], row["LAMP_cig_unl"])]:
        if u is True: continue
        if c is not None and not pd.isna(c) and c < 3000:
            return True
    for s, g in [(row["NBM_wind_spd"], row["NBM_wind_gst"]),
                 (row["LAMP_wind_spd"], row["LAMP_wind_gst"])]:
        for w in (s, g):
            if w is not None and not pd.isna(w) and w >= 25:
                return True
    return False


def build_summary(df):
    now = datetime.now(timezone.utc)
    up = df[df["valid_time"] >= now]
    if len(up) == 0: return None
    hits = up[up["flagged"]]
    if len(hits) == 0:
        return "No low-condition periods forecast."
    f = hits.iloc[0]
    t = pd.to_datetime(f['valid_time'])
    return f"⚠ Next low-condition period starts at **{t:%m/%d %HZ}** (f+{int(f['fhr'])})"


def make_cell(text, bg="#ffffff", fg="#FFFFFF"):
    return (
        f'<td style="'
        f'background:{bg};'
        f'color:{fg} !important;'
        f'font-family:Courier New,monospace;'
        f'font-size:9px;'
        f'padding:2px 3px;'
        f'text-align:center;'
        f'border:1px solid #003300;'
        f'white-space:nowrap;'
        f'min-width:38px;'
        f'max-width:38px;'
        f'">{escape(str(text))}</td>'
    )
    return (
        f'<td style="'
        f'background:{bg};'
        f'color:{fg};'
        f'font-family:Courier New,monospace;'
        f'font-size:9px;'
        f'padding:2px 3px;'
        f'text-align:center;'
        f'border:1px solid #003300;'
        f'white-space:nowrap;'
        f'min-width:38px;'
        f'max-width:38px;'
        f'">{escape(str(text))}</td>'
    )


def make_th(text, is_row_label=False):
    min_w = "60px" if is_row_label else "38px"
    align = "left" if is_row_label else "center"
    return (
        f'<th style="'
        f'background:#D3D3D3;'
        f'color:#FFFFFF !important;'
        f'font-family:Courier New,monospace;'
        f'font-size:9px;'
        f'font-weight:bold;'
        f'padding:2px 4px;'
        f'text-align:{align};'
        f'border:1px solid #003300;'
        f'white-space:nowrap;'
        f'min-width:{min_w};'
        f'">{escape(str(text))}</th>'
    )


st.title("MOS Tables")
st.caption("Side-by-side hourly NBM + GFS LAMP for one airport.")

with st.sidebar:
    st.header("Airport")
    icao_input = st.text_input("ICAO code", value="KJFK", max_chars=4).strip().upper()
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

    with st.spinner("Probing NOMADS..."):
        cycle_iso = cached_latest_cycle_mos(icao_input)
    if cycle_iso is None:
        st.error("No complete cycle found.")
        st.stop()

    cycle = datetime.fromisoformat(cycle_iso)
    st.info(f"Cycle: **{cycle:%Y-%m-%d %H:%M UTC}**  ·  Station: **{icao_input}**")

    with st.spinner("Fetching NBM + LAMP..."):
        df = cached_mos_tables(icao_input, cycle_iso)

    if len(df) == 0:
        st.warning("No data returned.")
        st.stop()

    df["flagged"] = df.apply(row_flagged, axis=1)
    summary = build_summary(df)
    if summary:
        st.markdown(summary)

    df_c = df[df["fhr"] <= 25].copy()
    if len(df_c) == 0:
        st.warning("No data in overlap window.")
        st.stop()

    # Build table row by row as raw HTML strings
    times = df_c["valid_time"].tolist()
    fhrs = df_c["fhr"].tolist()

    # Header row
    header_cells = [make_th("Field", is_row_label=True)]
    for t in times:
        tstr = pd.to_datetime(t).strftime("%m/%d<br>%HZ")
        header_cells.append(
            f'<th style="background:#002200;color:#FFFFFF !important;'
            f'font-family:Courier New,monospace;font-size:9px;font-weight:bold;'
            f'padding:2px 3px;text-align:center;border:1px solid #003300;'
            f'white-space:nowrap;min-width:38px;max-width:38px;">{tstr}</th>'
        )
    header_row = "<tr>" + "".join(header_cells) + "</tr>"

    # F+ row
    fhr_cells = [make_th("F+", is_row_label=True)]
    for f in fhrs:
        fhr_cells.append(make_cell(f"f+{int(f)}"))
    fhr_row = "<tr>" + "".join(fhr_cells) + "</tr>"

    # NBM VIS row
    nbm_vis_cells = [make_th("NBM VIS", is_row_label=True)]
    for v in df_c["NBM_vis_sm"]:
        colors = vis_bg(v)
        if colors:
            nbm_vis_cells.append(make_cell(fmt_vis(v), colors[0], colors[1]))
        else:
            nbm_vis_cells.append(make_cell(fmt_vis(v)))
    nbm_vis_row = "<tr>" + "".join(nbm_vis_cells) + "</tr>"

    # LAMP VIS row
    lamp_vis_cells = [make_th("LAMP VIS", is_row_label=True)]
    for v in df_c["LAMP_vis_sm"]:
        colors = vis_bg(v)
        if colors:
            lamp_vis_cells.append(make_cell(fmt_vis(v), colors[0], colors[1]))
        else:
            lamp_vis_cells.append(make_cell(fmt_vis(v)))
    lamp_vis_row = "<tr>" + "".join(lamp_vis_cells) + "</tr>"

    # NBM CIG row
    nbm_cig_cells = [make_th("NBM CIG", is_row_label=True)]
    for c, u in zip(df_c["NBM_cig_ft"], df_c["NBM_cig_unl"]):
        colors = cig_bg(c, u)
        if colors:
            nbm_cig_cells.append(make_cell(fmt_cig(c, u), colors[0], colors[1]))
        else:
            nbm_cig_cells.append(make_cell(fmt_cig(c, u)))
    nbm_cig_row = "<tr>" + "".join(nbm_cig_cells) + "</tr>"

    # LAMP CIG row
    lamp_cig_cells = [make_th("LAMP CIG", is_row_label=True)]
    for c, u in zip(df_c["LAMP_cig_ft"], df_c["LAMP_cig_unl"]):
        colors = cig_bg(c, u)
        if colors:
            lamp_cig_cells.append(make_cell(fmt_cig(c, u), colors[0], colors[1]))
        else:
            lamp_cig_cells.append(make_cell(fmt_cig(c, u)))
    lamp_cig_row = "<tr>" + "".join(lamp_cig_cells) + "</tr>"

    # NBM Wind row
    nbm_wind_cells = [make_th("NBM Wind", is_row_label=True)]
    for d, s, g in zip(df_c["NBM_wind_dir"], df_c["NBM_wind_spd"], df_c["NBM_wind_gst"]):
        colors = wind_bg(s, g)
        if colors:
            nbm_wind_cells.append(make_cell(fmt_wind(d, s, g), colors[0], colors[1]))
        else:
            nbm_wind_cells.append(make_cell(fmt_wind(d, s, g)))
    nbm_wind_row = "<tr>" + "".join(nbm_wind_cells) + "</tr>"

    # LAMP Wind row
    lamp_wind_cells = [make_th("LAMP Wind", is_row_label=True)]
    for d, s, g in zip(df_c["LAMP_wind_dir"], df_c["LAMP_wind_spd"], df_c["LAMP_wind_gst"]):
        colors = wind_bg(s, g)
        if colors:
            lamp_wind_cells.append(make_cell(fmt_wind(d, s, g), colors[0], colors[1]))
        else:
            lamp_wind_cells.append(make_cell(fmt_wind(d, s, g)))
    lamp_wind_row = "<tr>" + "".join(lamp_wind_cells) + "</tr>"

    table_html = (
        '<div style="overflow-x:auto;background:#000;padding:4px;border:1px solid #003300;">'
        '<table style="border-collapse:collapse;margin:0;">'
        f'<thead>{header_row}</thead>'
        f'<tbody>{fhr_row}{nbm_vis_row}{lamp_vis_row}{nbm_cig_row}{lamp_cig_row}{nbm_wind_row}{lamp_wind_row}</tbody>'
        '</table>'
        '</div>'
    )

    st.markdown(table_html, unsafe_allow_html=True)

    # CSV
    csv_df = pd.DataFrame({
        "time": [pd.to_datetime(t).strftime("%Y-%m-%d %H:%MZ") for t in df_c["valid_time"]],
        "fhr": df_c["fhr"].tolist(),
        "NBM_vis": [fmt_vis(v) for v in df_c["NBM_vis_sm"]],
        "LAMP_vis": [fmt_vis(v) for v in df_c["LAMP_vis_sm"]],
        "NBM_cig": [fmt_cig(c, u) for c, u in zip(df_c["NBM_cig_ft"], df_c["NBM_cig_unl"])],
        "LAMP_cig": [fmt_cig(c, u) for c, u in zip(df_c["LAMP_cig_ft"], df_c["LAMP_cig_unl"])],
        "NBM_wind": [fmt_wind(d, s, g) for d, s, g in zip(df_c["NBM_wind_dir"], df_c["NBM_wind_spd"], df_c["NBM_wind_gst"])],
        "LAMP_wind": [fmt_wind(d, s, g) for d, s, g in zip(df_c["LAMP_wind_dir"], df_c["LAMP_wind_spd"], df_c["LAMP_wind_gst"])],
    })
    st.download_button(
        "Download as CSV",
        csv_df.to_csv(index=False).encode("utf-8"),
        f"mos_tables_{icao_input}_{cycle:%Y%m%d_%H}Z.csv",
        "text/csv",
    )

else:
    st.info("Enter an ICAO code and click Refresh.")
