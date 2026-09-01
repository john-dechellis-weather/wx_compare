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
            "NBM_tmp_f": _safe(n, "temp_f"),
            "NBM_dpt_f": _safe(n, "dewpoint_f"),
            "LAMP_tmp_f": _safe(l, "temp_f"),
            "LAMP_dpt_f": _safe(l, "dewpoint_f"),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_extended_tables(icao: str, cycle_iso: str):
    """NBS (3-hourly, f+6-72) and NBE (12-hourly, wind only) frames."""
    from compare import compare_icaos
    from models import Nbs, Nbe

    cycle = datetime.fromisoformat(cycle_iso)
    df_long, resolved, unresolved = compare_icaos(
        icaos=[icao],
        cycle=cycle,
        cache_root=CACHE_ROOT,
        model_classes=[Nbs, Nbe],
    )
    if not resolved or len(df_long) == 0:
        return pd.DataFrame(), pd.DataFrame()

    df = df_long[df_long["station_id"] == icao.upper()].copy()

    def _wide(model_name):
        m = df[df["model"] == model_name].sort_values("valid_time")
        if len(m) == 0:
            return pd.DataFrame()
        rows = []
        for _, r in m.iterrows():
            t = r["valid_time"]
            rows.append({
                "valid_time": t,
                "fhr": int((t - cycle).total_seconds() // 3600),
                "vis_sm": r.get("vsby_sm"),
                "cig_ft": r.get("ceiling_ft"),
                "cig_unl": r.get("ceiling_unlimited"),
                "wdr": r.get("wind_dir_deg"),
                "wsp": r.get("wind_speed_kt"),
                "gst": r.get("wind_gust_kt"),
                "tmp_f": r.get("temp_f"),
                "dpt_f": r.get("dewpoint_f"),
            })
        return pd.DataFrame(rows)

    return _wide("NBS"), _wide("NBE")


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
    if v <= 0.5: return ("#FF80FF", "#000000")   # pink
    if v < 1: return ("#FF4040", "#000000")      # red
    if v < 2: return ("#FF9900", "#000000")      # orange
    if v < 3: return ("#FFFF00", "#000000")      # yellow
    return None


def cig_bg(c, u):
    if u is True or c is None or pd.isna(c): return None
    if c < 400: return ("#FF80FF", "#000000")    # pink
    if c <= 1000: return ("#FF4040", "#000000")  # red
    if c <= 2000: return ("#FF9900", "#000000")  # orange
    if c < 3000: return ("#FFFF00", "#000000")   # yellow
    return None


def temp_bg(t):
    """Background for a temperature in F, or None for benign values.

    Cold and hot are separate ladders that both run from mild to
    extreme, so the eye reads severity the same way in either
    direction. Checked coldest-first and hottest-first because the
    bands are open-ended at each end.

    White text on the dark ends: purple and magenta are too dark for
    black to stay legible at 10 px.
    """
    if t is None or pd.isna(t):
        return None
    if t < 10:
        return ("#8B00C8", "#FFFFFF")    # purple
    if t < 20:
        return ("#0033CC", "#FFFFFF")    # blue
    if t < 32:
        return ("#9FD4F5", "#000000")    # light blue
    if t > 110:
        return ("#FF00FF", "#000000")    # magenta
    if t > 100:
        return ("#FF2020", "#FFFFFF")    # red
    if t > 90:
        return ("#FFFF00", "#000000")    # yellow
    return None


def fmt_temp(t):
    if t is None or pd.isna(t):
        return "-"
    return f"{int(round(t))}"


def fmt_wdr(dr):
    if dr is None or pd.isna(dr):
        return "VRB"
    return f"{int(dr):03d}"


def fmt_kt(x):
    if x is None or pd.isna(x):
        return "-"
    return f"{int(x):02d}"


def build_wind_row(label, series, colored=False, spd_series=None, gst_series=None, fmt=fmt_kt):
    """One table row for a wind component. If colored, tint by wind_bg."""
    cells = [make_th(label, is_row_label=True)]
    for i, v in enumerate(series):
        if colored:
            s = spd_series.iloc[i] if spd_series is not None else None
            g = gst_series.iloc[i] if gst_series is not None else None
            if gst_series is not None and spd_series is None:
                colors = gust_bg(g)          # gust rows: 2-tier scheme
            else:
                colors = wind_bg(s, g)       # sustained rows: 4-tier scheme
        else:
            colors = None
        if colors:
            cells.append(make_cell(fmt(v), colors[0], colors[1]))
        else:
            cells.append(make_cell(fmt(v)))
    return "<tr>" + "".join(cells) + "</tr>"


def wind_bg(s, g):
    vals = [x for x in (s, g) if x is not None and not pd.isna(x)]
    if not vals: return None
    w = max(vals)
    if w >= 40: return ("#FF80FF", "#000000")    # pink
    if w >= 35: return ("#FF4040", "#000000")    # red
    if w >= 30: return ("#FF9900", "#000000")    # orange
    if w >= 25: return ("#FFFF00", "#000000")    # yellow
    return None


def gust_bg(g):
    """Gust-specific tiers: orange >= 25 kt, red >= 35 kt."""
    if g is None or pd.isna(g):
        return None
    if g >= 35: return ("#FF4040", "#000000")    # red
    if g >= 25: return ("#FF9900", "#000000")    # orange
    return None


def build_generic_table(df_m, table_label, show_viscig=True):
    """One stacked table for a per-model frame (columns: valid_time, fhr,
    vis_sm, cig_ft, cig_unl, wdr, wsp, gst)."""
    times = df_m["valid_time"].tolist()
    fhrs = df_m["fhr"].tolist()

    header_cells = [make_th("Field", is_row_label=True)]
    for t in times:
        tstr = pd.to_datetime(t).strftime("%m/%d<br>%HZ")
        header_cells.append(
            f'<th style="background:#E0E0E0;color:#000000;'
            f'-webkit-text-fill-color:#000000;'
            f'font-family:Courier New,monospace;font-size:9px;font-weight:bold;'
            f'padding:2px 3px;text-align:center;border:1px solid #000000;'
            f'white-space:nowrap;min-width:38px;">{tstr}</th>'
        )
    header_row = "<tr>" + "".join(header_cells) + "</tr>"

    fhr_cells = [make_th("F+", is_row_label=True)]
    for f in fhrs:
        fhr_cells.append(make_cell(f"f+{int(f)}"))
    rows_html = ["<tr>" + "".join(fhr_cells) + "</tr>"]

    if show_viscig:
        cells = [make_th("VIS", is_row_label=True)]
        for v in df_m["vis_sm"]:
            colors = vis_bg(v)
            cells.append(make_cell(fmt_vis(v), colors[0], colors[1])
                         if colors else make_cell(fmt_vis(v)))
        rows_html.append("<tr>" + "".join(cells) + "</tr>")

        cells = [make_th("CIG", is_row_label=True)]
        for c, u in zip(df_m["cig_ft"], df_m["cig_unl"]):
            colors = cig_bg(c, u)
            cells.append(make_cell(fmt_cig(c, u), colors[0], colors[1])
                         if colors else make_cell(fmt_cig(c, u)))
        rows_html.append("<tr>" + "".join(cells) + "</tr>")

    # Temperature above the wind block: it is the field most often
    # scanned first, and the colour ladder makes it the fastest row
    # to read at a glance.
    # TMP is coloured; DPT is not. Dewpoint shares the temperature
    # scale but not its meaning — a 15 F dewpoint is unremarkable and
    # would light up blue on a ladder built for air temperature,
    # putting alarm colours on a row that is rarely the concern.
    for _lab, _col, _color in (("TMP", "tmp_f", True),
                               ("DPT", "dpt_f", False)):
        if _col not in df_m.columns:
            continue
        cells = [make_th(_lab, is_row_label=True)]
        for _t in df_m[_col]:
            _c = temp_bg(_t) if _color else None
            cells.append(make_cell(fmt_temp(_t), _c[0], _c[1])
                         if _c else make_cell(fmt_temp(_t)))
        rows_html.append("<tr>" + "".join(cells) + "</tr>")

    rows_html.append(build_wind_row("WDR", df_m["wdr"], fmt=fmt_wdr))
    rows_html.append(build_wind_row("WSP", df_m["wsp"], colored=True,
                                    spd_series=df_m["wsp"]))
    rows_html.append(build_wind_row("GST", df_m["gst"], colored=True,
                                    gst_series=df_m["gst"]))

    return (
        '<div style="overflow-x:auto;background:#FFFFFF;padding:4px;'
        'border:2px solid #000000;margin-bottom:10px;">'
        + f'<div style="font-family:Courier New,monospace;font-size:10px;'
        + f'font-weight:bold;color:#000000;-webkit-text-fill-color:#000000;'
        + f'padding:1px 2px;">{table_label}</div>'
        + '<table style="border-collapse:collapse;margin:0;">'
        + f'<thead>{header_row}</thead>'
        + f'<tbody>{"".join(rows_html)}</tbody>'
        + '</table></div>'
    )


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


# ---------------------------------------------------------------------------
# REFS ensemble probabilities at the station
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False, max_entries=24)
def cached_refs_probs(icao: str, cycle_iso: str, hours: tuple):
    """{product: {fhr: pct}}, or ({}, reason) on failure.

    Cached per station and cycle: the first open costs ~144 small
    idx-range fetches through the parallel fetcher; every later open
    on the same cycle is instant.
    """
    from core import refs_point
    from core.stations import StationResolver

    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    stn = resolver.resolve(icao)
    if stn is None:
        return {}, f"cannot resolve {icao}"
    cycle = datetime.fromisoformat(cycle_iso)
    try:
        return refs_point.sample(stn.lat, stn.lon, cycle,
                                 list(hours)), None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


@st.cache_data(ttl=600, show_spinner=False)
def cached_refs_cycle(bucket: str) -> str | None:
    """Newest REFS prob cycle that reaches f24."""
    from core.hrrr_cam import latest_cycle

    c = latest_cycle("refs_prob", 24)
    return c.isoformat() if c else None


def build_refs_prob_table(probs: dict, cycle: datetime,
                          hours) -> str:
    """Rows = thresholds, columns = valid hour in Z.

    Coloured by PROBABILITY rather than by severity, so reading down
    a column shows the shape of the distribution: high for <2000 ft
    and low for <500 ft says stratus is likely but how low is
    uncertain.
    """
    from datetime import timedelta

    from core import refs_point

    header = [make_th("REFS PROB", is_row_label=True)]
    for h in hours:
        header.append(make_th(f"{(cycle + timedelta(hours=h)):%H}Z"))
    rows = ["<tr>" + "".join(header) + "</tr>"]
    for key, label in refs_point.THRESHOLDS:
        cells = [make_th(label, is_row_label=True)]
        col = probs.get(key, {})
        for h in hours:
            v = col.get(h)
            c = refs_point.cell_style(v)
            txt = "-" if v is None else str(v)
            cells.append(make_cell(txt, c[0], c[1]) if c
                         else make_cell(txt))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<div style="overflow-x:auto;background:#FFFFFF;padding:4px;'
        'border:2px solid #000000;margin-top:10px;">'
        '<div style="font-family:Courier New,monospace;font-size:10px;'
        'font-weight:bold;color:#000000;-webkit-text-fill-color:#000000;'
        'padding:1px 2px;">'
        f'REFS ensemble probability (%) \u2014 {cycle:%HZ} run</div>'
        '<table style="border-collapse:collapse;margin:0;">'
        + "".join(rows) + "</table>"
        '<div style="font-family:Courier New,monospace;font-size:8px;'
        'color:#333;-webkit-text-fill-color:#333;padding:3px 2px 0;">'
        "\u226570 red | 50\u201369 orange | 30\u201349 yellow | "
        "10\u201329 pale | &lt;10 blank</div></div>"
    )


def make_cell(text, bg="#FFFFFF", fg="#000000"):
    """Return a <td>: white/black by default; tier colors pass a bg.
    plain color + -webkit-text-fill-color, NO important flags: the Streamlit sanitizer strips inline declarations containing them; text-fill-color wins the paint step regardless."""
    weight = "bold" if bg != "#FFFFFF" else "normal"
    return (
        f'<td style="'
        f'background:{bg};'
        f'color:{fg};'
        f'-webkit-text-fill-color:{fg};'
        f'font-family:Courier New,monospace;'
        f'font-size:9px;'
        f'font-weight:{weight};'
        f'padding:2px 3px;'
        f'text-align:center;'
        f'border:1px solid #000000;'
        f'white-space:nowrap;'
        f'min-width:38px;'
        f'">{escape(str(text))}</td>'
    )


def make_th(text, is_row_label=False):
    """Return a <th>."""
    min_w = "60px" if is_row_label else "38px"
    align = "left" if is_row_label else "center"
    return (
        f'<th style="'
        f'background:#E0E0E0;'
        f'color:#000000;'
        f'-webkit-text-fill-color:#000000;'
        f'font-family:Courier New,monospace;'
        f'font-size:9px;'
        f'font-weight:bold;'
        f'padding:2px 4px;'
        f'text-align:{align};'
        f'border:1px solid #000000;'
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
            f'<th style="background:#E0E0E0;color:#000000;'
            f'-webkit-text-fill-color:#000000;'
            f'font-family:Courier New,monospace;font-size:9px;font-weight:bold;'
            f'padding:2px 3px;text-align:center;border:1px solid #000000;'
            f'white-space:nowrap;min-width:38px;">{tstr}</th>'
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

    def _temp_row(label, series, color=True):
        cells = [make_th(label, is_row_label=True)]
        for _t in series:
            _c = temp_bg(_t) if color else None
            cells.append(make_cell(fmt_temp(_t), _c[0], _c[1])
                         if _c else make_cell(fmt_temp(_t)))
        return "<tr>" + "".join(cells) + "</tr>"

    nbm_tmp_row = _temp_row("NBM TMP", df_c["NBM_tmp_f"]) \
        if "NBM_tmp_f" in df_c.columns else ""
    nbm_dpt_row = _temp_row("NBM DPT", df_c["NBM_dpt_f"],
                            color=False) \
        if "NBM_dpt_f" in df_c.columns else ""
    lamp_tmp_row = _temp_row("LAMP TMP", df_c["LAMP_tmp_f"]) \
        if "LAMP_tmp_f" in df_c.columns else ""
    lamp_dpt_row = _temp_row("LAMP DPT", df_c["LAMP_dpt_f"],
                             color=False) \
        if "LAMP_dpt_f" in df_c.columns else ""

    # Wind rows — direction / sustained / gust, per model.
    # WSP colored by sustained speed; GST colored by gust; WDR plain.
    nbm_wdr_row = build_wind_row("NBM WDR", df_c["NBM_wind_dir"], fmt=fmt_wdr)
    nbm_wsp_row = build_wind_row(
        "NBM WSP", df_c["NBM_wind_spd"], colored=True,
        spd_series=df_c["NBM_wind_spd"])
    nbm_gst_row = build_wind_row(
        "NBM GST", df_c["NBM_wind_gst"], colored=True,
        gst_series=df_c["NBM_wind_gst"])
    lamp_wdr_row = build_wind_row("LAMP WDR", df_c["LAMP_wind_dir"], fmt=fmt_wdr)
    lamp_wsp_row = build_wind_row(
        "LAMP WSP", df_c["LAMP_wind_spd"], colored=True,
        spd_series=df_c["LAMP_wind_spd"])
    lamp_gst_row = build_wind_row(
        "LAMP GST", df_c["LAMP_wind_gst"], colored=True,
        gst_series=df_c["LAMP_wind_gst"])

    _wrap_open = (
        '<div style="overflow-x:auto;background:#FFFFFF;padding:4px;'
        'border:2px solid #000000;{margin}">'
    )
    _label = (
        '<div style="font-family:Courier New,monospace;font-size:10px;'
        'font-weight:bold;color:#000000;-webkit-text-fill-color:#000000;'
        'padding:1px 2px;">{name}</div>'
    )
    table_html = (
        _wrap_open.format(margin="margin-bottom:10px;")
        + _label.format(name="NBM")
        + '<table style="border-collapse:collapse;margin:0;">'
        + f'<thead>{header_row}</thead>'
        + f'<tbody>{fhr_row}{nbm_vis_row}{nbm_cig_row}'
        + f'{nbm_tmp_row}{nbm_dpt_row}'
        + f'{nbm_wdr_row}{nbm_wsp_row}{nbm_gst_row}</tbody>'
        + '</table></div>'
        + _wrap_open.format(margin="")
        + _label.format(name="GFS LAMP")
        + '<table style="border-collapse:collapse;margin:0;">'
        + f'<thead>{header_row}</thead>'
        + f'<tbody>{fhr_row}{lamp_vis_row}{lamp_cig_row}'
        + f'{lamp_tmp_row}{lamp_dpt_row}'
        + f'{lamp_wdr_row}{lamp_wsp_row}{lamp_gst_row}</tbody>'
        + '</table></div>'
    )

    st.markdown(table_html, unsafe_allow_html=True)

    # REFS ensemble probabilities, directly under the deterministic
    # tables so the NBM/LAMP categorical CIG/VIS call and the
    # ensemble probability sit on one page and disagreements are
    # visible at a glance.
    _rp = st.empty()
    _rp.markdown(
        "<p style='text-align:center;font-size:18px;font-weight:700;"
        "margin:8px 0'>Loading REFS probabilities\u2026</p>",
        unsafe_allow_html=True)
    _refs_hours = tuple(range(1, 25))
    _rc = cached_refs_cycle(
        datetime.now(timezone.utc).strftime("%Y%m%d%H")
        + str(datetime.now(timezone.utc).minute // 10))
    if _rc:
        _probs, _rerr = cached_refs_probs(icao_input, _rc, _refs_hours)
        _rp.empty()
        if _probs and any(_probs.values()):
            st.markdown(build_refs_prob_table(
                _probs, datetime.fromisoformat(_rc), _refs_hours),
                unsafe_allow_html=True)
        else:
            st.caption(f"REFS probabilities unavailable"
                       + (f" \u2014 {_rerr}" if _rerr else "") + ".")
    else:
        _rp.empty()
        st.caption("REFS probabilities: no complete cycle found.")

    # Extended tables: NBS (3-hourly) + NBE (12-hourly, wind only)
    with st.spinner("Fetching NBS + NBE..."):
        try:
            nbs_df, nbe_df = cached_extended_tables(icao_input, cycle_iso)
        except Exception as e:
            nbs_df, nbe_df = pd.DataFrame(), pd.DataFrame()
            st.warning(f"NBS/NBE fetch failed: {e}")

    if len(nbs_df):
        st.markdown(
            build_generic_table(nbs_df, "NBS (3-hourly)", show_viscig=True),
            unsafe_allow_html=True,
        )
    else:
        st.caption("NBS: no data for this cycle.")

    if len(nbe_df):
        st.markdown(
            build_generic_table(
                nbe_df, "NBE (12-hourly \u00b7 wind only \u2014 "
                "VIS/CIG not produced at extended range)",
                show_viscig=False,
            ),
            unsafe_allow_html=True,
        )
    else:
        st.caption("NBE: no data for this cycle.")

    # CSV
    csv_df = pd.DataFrame({
        "time": [pd.to_datetime(t).strftime("%Y-%m-%d %H:%MZ") for t in df_c["valid_time"]],
        "fhr": df_c["fhr"].tolist(),
        "NBM_vis": [fmt_vis(v) for v in df_c["NBM_vis_sm"]],
        "LAMP_vis": [fmt_vis(v) for v in df_c["LAMP_vis_sm"]],
        "NBM_cig": [fmt_cig(c, u) for c, u in zip(df_c["NBM_cig_ft"], df_c["NBM_cig_unl"])],
        "LAMP_cig": [fmt_cig(c, u) for c, u in zip(df_c["LAMP_cig_ft"], df_c["LAMP_cig_unl"])],
        "NBM_wdr": [fmt_wdr(d) for d in df_c["NBM_wind_dir"]],
        "NBM_wsp": [fmt_kt(s) for s in df_c["NBM_wind_spd"]],
        "NBM_gst": [fmt_kt(g) for g in df_c["NBM_wind_gst"]],
        "LAMP_wdr": [fmt_wdr(d) for d in df_c["LAMP_wind_dir"]],
        "LAMP_wsp": [fmt_kt(s) for s in df_c["LAMP_wind_spd"]],
        "LAMP_gst": [fmt_kt(g) for g in df_c["LAMP_wind_gst"]],
    })
    st.download_button(
        "Download as CSV",
        csv_df.to_csv(index=False).encode("utf-8"),
        f"mos_tables_{icao_input}_{cycle:%Y%m%d_%H}Z.csv",
        "text/csv",
    )

else:
    st.info("Enter an ICAO code and click Refresh.")