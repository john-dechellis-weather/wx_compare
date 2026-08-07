"""Station Quick View — one-airport operational dashboard.

Sections: current METAR, current TAF, live radar (NWS RIDGE loop OR raw
Level II frames rendered from volume data), hourly NBM table, NOTAMs.

Architecture note: the Refresh button commits the station to
st.session_state and the display gates on that (not on the button), so
widget interactions like the Level II frame slider rerun the page
without blanking it — every fetcher is cached, so reruns are instant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="BlueMet — Station Quick View",
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
# Data fetchers (all cached — reruns from widget interaction are instant)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False, max_entries=30)
def cached_metar_history(icao: str, hours_back: int):
    """All obs in the lookback window, oldest -> newest."""
    from core.metar import fetch_metars

    by_station = fetch_metars([icao], hours_back=hours_back)
    return by_station.get(icao.upper(), [])


@st.cache_data(ttl=300, show_spinner=False, max_entries=30)
def cached_taf_raw(icao: str) -> str | None:
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


@st.cache_data(ttl=600, show_spinner=False, max_entries=30)
def cached_station_coords(icao: str):
    from core.stations import StationResolver

    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved, _ = resolver.resolve_many([icao])
    if not resolved:
        return None
    stn = resolved[0]
    return float(stn.lat), float(stn.lon)


def _frames_to_gif(
    frames: list[tuple[bytes, str]],
    width: int = 800,
    frame_ms: int = 450,
    last_hold_ms: int = 1400,
) -> bytes:
    """Stitch rendered PNG frames into a looping radar-style GIF.
    Downscaled for fast loading; newest frame held longer, like RIDGE."""
    from io import BytesIO
    from PIL import Image

    imgs = []
    for png, _name in frames:
        im = Image.open(BytesIO(png)).convert("RGB")
        w, h = im.size
        if w > width:
            im = im.resize((width, int(h * width / w)), Image.LANCZOS)
        imgs.append(im.quantize(colors=256))
    durations = [frame_ms] * (len(imgs) - 1) + [last_hold_ms]
    buf = BytesIO()
    imgs[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=imgs[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


@st.cache_data(ttl=300, show_spinner=False, max_entries=5)
def cached_live_l2(
    icao: str, radar_site: str, zoom_deg: float, bucket: str,
    overlay_flights: bool = False,
):
    """Last ~45 min of raw Level II reflectivity rendered around the
    airport. `bucket` is a 5-minute stamp so the cache key rolls forward."""
    from core.radar import fetch_and_render_radar_loop

    coords = cached_station_coords(icao)
    if coords is None:
        raise ValueError(f"Cannot resolve coordinates for {icao}.")
    lat, lon = coords

    site = radar_site
    if len(site) == 4 and site.startswith("K"):
        site = site[1:]

    overlay = None
    if overlay_flights:
        try:
            from core.flights import fetch_positions_near
            overlay = fetch_positions_near(lat, lon, radius_deg=zoom_deg)
        except Exception:
            overlay = None

    start = datetime.now(timezone.utc) - timedelta(minutes=45)
    refl_frames, _ = fetch_and_render_radar_loop(
        start_time=start,
        duration_min=45,
        aircraft_lat=lat,
        aircraft_lon=lon,
        callsign=icao,
        station=site,
        zoom_deg=zoom_deg,
        include_velocity=False,
        overlay_aircraft=overlay,
    )
    gif = _frames_to_gif(refl_frames) if len(refl_frames) > 1 else b""
    return refl_frames, gif


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_nbh_table(icao: str) -> pd.DataFrame:
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
def cached_notams(icao: str):
    """FAA NOTAM Search JSON endpoint — unofficial.
    Returns (rows, None) on success or (None, error_detail) on failure
    so the page can show WHY it failed (status code vs timeout vs schema)."""
    try:
        r = requests.post(
            "https://notams.aim.faa.gov/notamSearch/search",
            data={"searchType": "0", "designatorsForLocation": icao},
            headers=_HEADERS,
            timeout=30,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code} from notams.aim.faa.gov"
        try:
            payload = r.json()
        except ValueError:
            snippet = r.text[:120].replace("\n", " ")
            return None, f"Non-JSON response (starts: {snippet!r})"
        items = payload.get("notamList", [])
        out = []
        for it in items:
            out.append({
                "number": it.get("notamNumber", ""),
                "text": (it.get("icaoMessage") or it.get("traditionalMessage")
                         or it.get("plainLanguageMessage") or "").strip(),
            })
        return out, None
    except requests.Timeout:
        return None, "Timeout after 30s"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Rendering helpers (terminal styling, sanitizer-proof: no !important)
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
st.title("Station Quick View")
st.caption("Current conditions, forecast, radar, and NOTAMs for one airport.")

with st.sidebar:
    st.header("Airport")
    icao_sidebar = st.text_input(
        "ICAO code",
        value="KJFK",
        max_chars=4,
    ).strip().upper()

    radar_override = st.text_input(
        "Radar site (blank = auto)",
        value="",
        max_chars=4,
        help="NEXRAD site for the radar section, e.g. KOKX. "
             "Auto-mapped for common airports.",
    ).strip().upper()

    n_metars = st.slider(
        "METARs to show", 1, 12, 1,
        help="1 = current only; more shows recent history, newest first.",
    )

    radar_mode = st.radio(
        "Radar display",
        options=["RIDGE loop (instant)", "Raw Level II (slower, full res)"],
        index=0,
    )
    l2_zoom = st.slider(
        "Level II zoom (degrees)", 0.5, 3.0, 1.5, 0.5,
        disabled=not radar_mode.startswith("Raw"),
    )
    l2_flights = st.checkbox(
        "Overlay live JBU flights",
        value=True,
        disabled=not radar_mode.startswith("Raw"),
        help="Current JetBlue aircraft positions (community ADS-B) drawn "
             "on the newest Level II frame.",
    )

    st.divider()
    run_button = st.button("Refresh", type="primary", use_container_width=True)


if run_button and icao_sidebar:
    st.session_state["status_icao"] = icao_sidebar
    st.session_state.pop("live_l2", None)  # fresh loop on explicit refresh

active_icao = st.session_state.get("status_icao")

if active_icao:
    icao = active_icao
    now = datetime.now(timezone.utc)
    st.info(f"Station: **{icao}** · as of **{now:%Y-%m-%d %H:%M UTC}**")

    # --- METAR ---
    st.subheader("Current METAR" if n_metars == 1 else
                 f"METARs (last {n_metars})")
    # Hourly obs + specials: n+4 hours of lookback comfortably covers n obs.
    with st.spinner("Fetching METARs..."):
        obs_list = cached_metar_history(icao, hours_back=n_metars + 4)
    if obs_list:
        recent = obs_list[-n_metars:][::-1]  # newest first
        st.markdown(
            mono_box("\n".join(o.raw_text for o in recent)),
            unsafe_allow_html=True,
        )
        latest = recent[0]
        age_min = int((now - latest.obs_time).total_seconds() // 60)
        st.caption(
            f"Latest observed {latest.obs_time:%H:%MZ} ({age_min} min ago)"
            + ("" if len(recent) == 1 else
               f" · showing {len(recent)} obs, newest first")
        )
    else:
        st.warning("No recent METAR found.")

    # --- TAF ---
    st.subheader("Current TAF")
    with st.spinner("Fetching TAF..."):
        taf_text = cached_taf_raw(icao)
    if taf_text:
        st.markdown(mono_box(taf_text), unsafe_allow_html=True)
    else:
        st.warning("No TAF available (station may not be a TAF site).")

    # --- Radar ---
    st.subheader("Live Radar")
    radar_site = radar_override or RADAR_FOR_AIRPORT.get(icao, "")
    if not radar_site:
        st.warning(
            f"No radar mapping for {icao}. Enter a NEXRAD site "
            "(e.g. KOKX) in the sidebar."
        )
    elif radar_mode.startswith("RIDGE"):
        loop_url = (
            f"https://radar.weather.gov/ridge/standard/{radar_site}_loop.gif"
        )
        st.image(loop_url, use_container_width=True)
        st.caption(
            f"NWS RIDGE loop for {radar_site} — refreshes on page reload."
        )
    else:
        # Raw Level II mode
        bucket = (
            datetime.now(timezone.utc).strftime("%Y%m%d%H")
            + str(datetime.now(timezone.utc).minute // 5)
        )
        if "live_l2" not in st.session_state:
            with st.spinner(
                "Fetching raw Level II volumes (30-60s first time)..."
            ):
                try:
                    frames, gif = cached_live_l2(
                        icao, radar_site, l2_zoom, bucket,
                        overlay_flights=l2_flights,
                    )
                    st.session_state["live_l2"] = frames
                    st.session_state["live_l2_gif"] = gif
                except Exception as e:
                    st.session_state["live_l2"] = []
                    st.session_state["live_l2_gif"] = b""
                    st.warning(f"Level II fetch failed: {e}")
        frames = st.session_state.get("live_l2", [])
        gif = st.session_state.get("live_l2_gif", b"")
        if frames:
            st.caption(
                f"{len(frames)} raw volumes from {radar_site} "
                "(last ~45 min), rendered from Level II data — "
                "not NWS imagery."
            )
            if gif:
                st.image(gif, use_container_width=True)
                st.download_button(
                    "Download loop GIF",
                    data=gif,
                    file_name=f"l2_loop_{radar_site}.gif",
                    mime="image/gif",
                    key="dl_l2_gif",
                )
            with st.expander("Frame-by-frame (full resolution)",
                             expanded=not gif):
                if len(frames) > 1:
                    idx = st.slider(
                        "Frame", 0, len(frames) - 1, len(frames) - 1,
                        key="live_l2_idx",
                        help="Newest frame is rightmost.",
                    )
                else:
                    idx = 0
                png, name = frames[idx]
                st.image(png, use_container_width=True)
                st.caption(f"Frame {idx + 1} of {len(frames)} · `{name}`")
        else:
            st.caption("No Level II volumes returned for the window.")

    # --- NBH MOS table ---
    st.subheader("NBM Hourly (NBH, f+1–25)")
    with st.spinner("Fetching NBM..."):
        try:
            nbh_df = cached_nbh_table(icao)
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
        notams, notam_err = cached_notams(icao)
    if notams is None:
        st.warning(f"NOTAM service unavailable — {notam_err}")
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