"""Station Status — one-airport operational dashboard.

Sections:
  1. Current METAR   — latest ob via core.metar (AWC JSON)
  2. Current TAF     — raw text via AWC data API
  3. Live radar loop — NWS RIDGE standard station loop GIF (no rendering
                       on our side; browser pulls the current loop)
  4. NBH MOS table   — hourly NBM, f+1..25, same terminal styling as the
                       MOS Tables page
  5. Active NOTAMs   — FAA NOTAM Search JSON endpoint (unofficial but
                       long-stable; degrades gracefully if unavailable)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="BlueMet — Station Status",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}

# Airport -> nearest NEXRAD site for the RIDGE loop. Manual override in
# sidebar covers anything missing here.
RADAR_FOR_AIRPORT = {
    "KJFK": "KOKX", "KLGA": "KOKX", "KHPN": "KOKX", "KISP": "KOKX",
    "KEWR": "KDIX", "KPHL": "KDIX",
    "KBOS": "KBOX", "KPVD": "KBOX", "KORH": "KBOX",
    "KDCA": "KLWX", "KBWI": "KLWX", "KIAD": "KLWX",
    "KRIC": "KAKQ", "KORF": "KAKQ",
    "KCLT": "KGSP", "KRDU": "KRAX", "KCHS": "KCLX", "KSAV": "KCLX",
    "KJAX": "KJAX", "KMCO": "KMLB", "KDAB": "KMLB",
    "KTPA": "KTBW", "KSRQ": "KTBW", "KRSW": "KTBW",
    "KPBI": "KAMX", "KFLL": "KAMX", "KMIA": "KAMX", "KEYW": "KBYX",
    "KATL": "KFFC", "KMSY": "KLIX", "KBNA": "KOHX",
    "KORD": "KLOT", "KMKE": "KMKX", "KDTW": "KDTX", "KCLE": "KCLE",
    "KPIT": "KPBZ", "KBUF": "KBUF", "KROC": "KBUF",
    "KDFW": "KFWS", "KIAH": "KHGX", "KAUS": "KEWX",
    "KDEN": "KFTG", "KSLC": "KMTX", "KPHX": "KIWA", "KLAS": "KESX",
    "KABQ": "KABX", "KSAN": "KNKX", "KSFO": "KMUX", "KSMF": "KDAX",
    "KRNO": "KRGX", "KSEA": "KATX", "KPDX": "KRTX",
    "TJSJ": "TJUA",
}


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False, max_entries=30)
def cached_metar(icao: str):
    """Latest METAR object (raw + decoded fields) or None."""
    from core.metar import fetch_metars

    by_station = fetch_metars([icao], hours_back=3)
    obs = by_station.get(icao.upper(), [])
    return obs[-1] if obs else None


@st.cache_data(ttl=300, show_spinner=False, max_entries=30)
def cached_taf_raw(icao: str) -> str | None:
    """Raw TAF text from AWC."""
    try:
        r = requests.get(
            "https://aviationweather.gov/api/data/taf",
            params={"ids": icao, "format": "raw"},
            headers=_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        text = r.text.strip()
        return text or None
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_nbh_table(icao: str) -> pd.DataFrame:
    """Hourly NBM (NBH portion, f+1..25) as a per-model wide frame."""
    from compare import compare_icaos
    from core.stations import StationResolver
    from core.cycle_select import find_latest_complete
    from models import Nbm

    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved_pre, _ = resolver.resolve_many([icao])
    if not resolved_pre:
        return pd.DataFrame()
    cycle = find_latest_complete(
        [Nbm(cache_dir=CACHE_ROOT / "nbm")], verbose=False
    )
    if cycle is None:
        return pd.DataFrame()

    df_long, resolved, _ = compare_icaos(
        icaos=[icao],
        cycle=cycle,
        cache_root=CACHE_ROOT,
        model_classes=[Nbm],
    )
    if not resolved or len(df_long) == 0:
        return pd.DataFrame()

    m = df_long[
        (df_long["station_id"] == icao.upper())
        & (df_long["model"] == "NBM")
    ].sort_values("valid_time")
    rows = []
    for _, r in m.iterrows():
        fhr = int((r["valid_time"] - cycle).total_seconds() // 3600)
        if fhr > 25:
            continue
        rows.append({
            "valid_time": r["valid_time"], "fhr": fhr,
            "vis_sm": r.get("vsby_sm"),
            "cig_ft": r.get("ceiling_ft"),
            "cig_unl": r.get("ceiling_unlimited"),
            "wdr": r.get("wind_dir_deg"),
            "wsp": r.get("wind_speed_kt"),
            "gst": r.get("wind_gust_kt"),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_notams(icao: str) -> list[dict] | None:
    """Active NOTAMs via FAA NOTAM Search JSON endpoint.

    Unofficial but long-stable. Returns None on any failure so the page
    can show 'unavailable' instead of an error.
    """
    try:
        r = requests.post(
            "https://notams.aim.faa.gov/notamSearch/search",
            data={"searchType": "0", "designatorsForLocation": icao},
            headers=_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        items = payload.get("notamList", [])
        out = []
        for it in items:
            out.append({
                "number": it.get("notamNumber", ""),
                "text": (it.get("icaoMessage") or it.get("traditionalMessage")
                         or it.get("plainLanguageMessage") or "").strip(),
            })
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Terminal-style table (compact copy of the MOS page builders)
# ---------------------------------------------------------------------------
def _cell(text, bg="#FFFFFF", fg="#000000", bold=False):
    weight = "bold" if bold or bg != "#FFFFFF" else "normal"
    return (
        f'<td style="background:{bg};color:{fg};'
        f'-webkit-text-fill-color:{fg};'
        f'font-family:Courier New,monospace;font-size:9px;'
        f'font-weight:{weight};padding:2px 3px;text-align:center;'
        f'border:1px solid #000000;white-space:nowrap;min-width:38px;">'
        f"{text}</td>"
    )


def _rowlabel(text):
    return (
        f'<th style="background:#E0E0E0;color:#000000;'
        f'-webkit-text-fill-color:#000000;'
        f'font-family:Courier New,monospace;font-size:9px;font-weight:bold;'
        f'padding:2px 4px;text-align:left;border:1px solid #000000;'
        f'white-space:nowrap;min-width:60px;">{text}</th>'
    )


def _vis_colors(v):
    if v is None or pd.isna(v): return None
    if v <= 0.5: return ("#FF80FF", "#000000")
    if v < 1: return ("#FF4040", "#000000")
    if v < 2: return ("#FF9900", "#000000")
    if v < 3: return ("#FFFF00", "#000000")
    return None


def _cig_colors(c, u):
    if u is True or c is None or pd.isna(c): return None
    if c < 400: return ("#FF80FF", "#000000")
    if c <= 1000: return ("#FF4040", "#000000")
    if c <= 2000: return ("#FF9900", "#000000")
    if c < 3000: return ("#FFFF00", "#000000")
    return None


def _wind_colors(w):
    if w is None or pd.isna(w): return None
    if w >= 40: return ("#FF80FF", "#000000")
    if w >= 35: return ("#FF4040", "#000000")
    if w >= 30: return ("#FF9900", "#000000")
    if w >= 25: return ("#FFFF00", "#000000")
    return None


def _gust_colors(g):
    if g is None or pd.isna(g): return None
    if g >= 35: return ("#FF4040", "#000000")
    if g >= 25: return ("#FF9900", "#000000")
    return None


def _fmt_vis(v):
    if v is None or pd.isna(v): return "-"
    return f"{v:g}"


def _fmt_cig(c, u):
    if u is True: return "UNL"
    if c is None or pd.isna(c): return "-"
    return f"{int(round(c / 100)):03d}"


def _fmt_wdr(d):
    if d is None or pd.isna(d): return "VRB"
    return f"{int(d):03d}"


def _fmt_kt(x):
    if x is None or pd.isna(x): return "-"
    return f"{int(x):02d}"


def build_nbh_table(df_m: pd.DataFrame) -> str:
    header = [_rowlabel("Field")]
    for t in df_m["valid_time"]:
        tstr = pd.to_datetime(t).strftime("%m/%d<br>%HZ")
        header.append(
            f'<th style="background:#E0E0E0;color:#000000;'
            f'-webkit-text-fill-color:#000000;'
            f'font-family:Courier New,monospace;font-size:9px;'
            f'font-weight:bold;padding:2px 3px;text-align:center;'
            f'border:1px solid #000000;white-space:nowrap;min-width:38px;">'
            f"{tstr}</th>"
        )
    rows = ["<tr>" + "".join(header) + "</tr>"]

    r = [_rowlabel("F+")] + [_cell(f"f+{int(f)}") for f in df_m["fhr"]]
    rows.append("<tr>" + "".join(r) + "</tr>")

    r = [_rowlabel("VIS")]
    for v in df_m["vis_sm"]:
        c = _vis_colors(v)
        r.append(_cell(_fmt_vis(v), *c) if c else _cell(_fmt_vis(v)))
    rows.append("<tr>" + "".join(r) + "</tr>")

    r = [_rowlabel("CIG")]
    for cv, u in zip(df_m["cig_ft"], df_m["cig_unl"]):
        c = _cig_colors(cv, u)
        r.append(_cell(_fmt_cig(cv, u), *c) if c else _cell(_fmt_cig(cv, u)))
    rows.append("<tr>" + "".join(r) + "</tr>")

    r = [_rowlabel("WDR")] + [_cell(_fmt_wdr(d)) for d in df_m["wdr"]]
    rows.append("<tr>" + "".join(r) + "</tr>")

    r = [_rowlabel("WSP")]
    for s in df_m["wsp"]:
        c = _wind_colors(s)
        r.append(_cell(_fmt_kt(s), *c) if c else _cell(_fmt_kt(s)))
    rows.append("<tr>" + "".join(r) + "</tr>")

    r = [_rowlabel("GST")]
    for g in df_m["gst"]:
        c = _gust_colors(g)
        r.append(_cell(_fmt_kt(g), *c) if c else _cell(_fmt_kt(g)))
    rows.append("<tr>" + "".join(r) + "</tr>")

    return (
        '<div style="overflow-x:auto;background:#FFFFFF;padding:4px;'
        'border:2px solid #000000;">'
        '<table style="border-collapse:collapse;margin:0;">'
        + "".join(rows)
        + "</table></div>"
    )


def mono_box(text: str) -> str:
    """Raw-text display box (METAR/TAF/NOTAM) in terminal styling."""
    from html import escape
    return (
        '<div style="background:#000000;border:2px solid #00FF00;'
        'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
        'font-family:Courier New,monospace;font-size:12px;'
        'padding:8px 10px;white-space:pre-wrap;word-break:break-word;">'
        f"{escape(text)}</div>"
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Station Status")
st.caption("Current conditions, forecast, radar, and NOTAMs for one airport.")

with st.sidebar:
    st.header("Airport")
    icao_input = st.text_input(
        "ICAO code",
        value="KJFK",
        max_chars=4,
    ).strip().upper()

    radar_override = st.text_input(
        "Radar site (blank = auto)",
        value="",
        max_chars=4,
        help="NEXRAD site for the live loop, e.g. KOKX. "
             "Auto-mapped for common airports.",
    ).strip().upper()

    st.divider()
    run_button = st.button("Refresh", type="primary", use_container_width=True)

if run_button and icao_input:
    now = datetime.now(timezone.utc)
    st.info(f"Station: **{icao_input}** · as of **{now:%Y-%m-%d %H:%M UTC}**")

    # --- METAR ---
    st.subheader("Current METAR")
    with st.spinner("Fetching METAR..."):
        ob = cached_metar(icao_input)
    if ob is not None:
        st.markdown(mono_box(ob.raw_text), unsafe_allow_html=True)
        age_min = int((now - ob.obs_time).total_seconds() // 60)
        st.caption(f"Observed {ob.obs_time:%H:%MZ} ({age_min} min ago)")
    else:
        st.warning("No recent METAR found.")

    # --- TAF ---
    st.subheader("Current TAF")
    with st.spinner("Fetching TAF..."):
        taf_text = cached_taf_raw(icao_input)
    if taf_text:
        st.markdown(mono_box(taf_text), unsafe_allow_html=True)
    else:
        st.warning("No TAF available (station may not be a TAF site).")

    # --- Live radar loop ---
    st.subheader("Live Radar Loop")
    radar_site = radar_override or RADAR_FOR_AIRPORT.get(icao_input, "")
    if radar_site:
        loop_url = f"https://radar.weather.gov/ridge/standard/{radar_site}_loop.gif"
        st.image(loop_url, use_container_width=True)
        st.caption(
            f"NWS RIDGE loop for {radar_site} — refreshes on page reload."
        )
    else:
        st.warning(
            f"No radar mapping for {icao_input}. Enter a NEXRAD site "
            "(e.g. KOKX) in the sidebar."
        )

    # --- NBH MOS table ---
    st.subheader("NBM Hourly (NBH, f+1–25)")
    with st.spinner("Fetching NBM..."):
        try:
            nbh_df = cached_nbh_table(icao_input)
        except Exception as e:
            nbh_df = pd.DataFrame()
            st.warning(f"NBM fetch failed: {e}")
    if len(nbh_df):
        st.markdown(build_nbh_table(nbh_df), unsafe_allow_html=True)
    else:
        st.caption("No NBM data for this station.")

    # --- NOTAMs ---
    st.subheader("Active NOTAMs")
    with st.spinner("Fetching NOTAMs..."):
        notams = cached_notams(icao_input)
    if notams is None:
        st.warning(
            "NOTAM service unavailable (unofficial FAA endpoint — "
            "may be intermittent)."
        )
    elif not notams:
        st.caption("No active NOTAMs returned.")
    else:
        st.caption(f"{len(notams)} NOTAMs")
        for n in notams[:40]:
            title = n["number"] or "NOTAM"
            with st.expander(title, expanded=False):
                st.markdown(mono_box(n["text"]), unsafe_allow_html=True)

else:
    st.info("Enter an ICAO code in the sidebar and click **Refresh**.")