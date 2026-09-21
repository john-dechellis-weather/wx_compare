"""Station Forecast - one airport, everything a dispatcher asks for.

Target path: pages/2_Station_Forecast.py

Replaces Forecast Wind Plots and Forecast Flight Conditions, and adds
the pieces those two pages sent people elsewhere for. Pods, top to
bottom:

    METAR + TAF            |  MRMS 1 km reflectivity, 30 nm
    Wind (multi-model)     |  Flight conditions (multi-model)
    NBM hourly             |  GFS LAMP
    JetBlue arrivals & departures

Every fetch is behind st.cache_data and none of them blocks the page
for long: the model comparison and the radar are the two that cost
anything, and both are cached by cycle / scan.

Shared with Station Quick View: METAR/TAF colouring (core/wxtext),
the movement sampler (core/mov_sampler), radar chunks (core/mrms).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

st.set_page_config(page_title="BlueMet \u2014 Station Forecast", layout="wide")

from retro_theme import apply_retro_theme
apply_retro_theme()

from dark_theme import apply_dark_theme
apply_dark_theme()

from auth import check_password
check_password()

# ---------------------------------------------------------------- store
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
STATIC_MRMS = Path(__file__).resolve().parent.parent / "static"

try:
    from core.mov_sampler import (
        is_sampled, ensure_sampler_started, derive_movements, sampling_since,
    )
    ensure_sampler_started(CACHE_ROOT)
    _SAMPLER_OK, _SAMPLER_ERR = True, ""
except Exception as _se:
    _SAMPLER_OK, _SAMPLER_ERR = False, f"{type(_se).__name__}: {_se}"

    def is_sampled(_i):
        return False

MODELS = {"HRRR": "#00E5FF", "NBM": "#B388FF", "GFS_LAMP": "#FF8A65",
          "GFS_MOS": "#8A93A6"}
CAT = {"VFR": "#00FF7F", "MVFR": "#FFD400", "IFR": "#FF8A00", "LIFR": "#FF00C8"}
EDGE, PANEL, INK, INK2, MUTED = "#2D3957", "#0A0A0A", "#FFFFFF", "#B8B8B8", "#6E6E6E"
JBU = "#4DA3FF"

HUBS = ["KJFK", "KBOS", "KFLL", "KMCO", "KEWR", "KLGA", "KDCA", "KLAX",
        "KSFO", "KTPA", "KDJT", "KBDL", "KHPN", "TJSJ"]

# ------------------------------------------------------------- pods CSS
st.markdown(
    "<style>"
    # Every bordered container on this page is a pod.
    f'[data-testid="stVerticalBlockBorderWrapper"]{{'
    f"background:{PANEL} !important;border:1px solid {EDGE} !important;"
    "border-radius:4px !important;padding:10px 14px 12px !important;}"
    ".pod-title{color:#B8B8B8;font-size:11px;font-weight:700;"
    "letter-spacing:.6px;text-transform:uppercase;display:flex;"
    "justify-content:space-between;align-items:baseline;"
    "border-bottom:1px solid #1A2233;padding-bottom:6px;margin-bottom:8px;}"
    ".pod-title span{color:#6E6E6E;font-size:10px;font-weight:400;"
    "text-transform:none;letter-spacing:0}"
    "</style>", unsafe_allow_html=True)


def pod_title(title: str, sub: str = "") -> None:
    st.markdown(f'<div class="pod-title">{title}<span>{sub}</span></div>',
                unsafe_allow_html=True)


# ------------------------------------------------------------ fetchers
@st.cache_data(ttl=300, show_spinner=False, max_entries=30)
def cached_metars(icao: str, hours_back: int):
    from core.metar import fetch_metars
    obs = fetch_metars([icao], hours_back=hours_back).get(icao.upper(), [])
    return sorted(obs, key=lambda o: o.obs_time)


@st.cache_data(ttl=300, show_spinner=False, max_entries=30)
def cached_taf(icao: str):
    """Raw TAF text, the same source and shape Station Quick View uses."""
    import requests
    try:
        r = requests.get("https://aviationweather.gov/api/data/taf",
                         params={"ids": icao, "format": "raw"},
                         headers={"User-Agent": "BlueMet/1.0 (aviation weather tool)"},
                         timeout=30)
        r.raise_for_status()
        return r.text.strip() or None
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False, max_entries=30)
def cached_coords(icao: str):
    try:
        from core import airports as AP
        c = AP.centre(icao)
        if c:
            return c
    except Exception:
        pass
    try:
        from core.stations import StationResolver
        r = StationResolver(cache_dir=CACHE_ROOT / "stations")
        res, _ = r.resolve_many([icao])
        return (res[0].lat, res[0].lon) if res else None
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_latest_cycle(icao: str) -> str | None:
    from core.stations import StationResolver
    from core.cycle_select import find_latest_complete
    from models import GfsMos, GfsLamp, Hrrr, Nbm

    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved, _ = resolver.resolve_many([icao])
    if not resolved:
        return None
    probes = [GfsMos(cache_dir=CACHE_ROOT / "gfs_mos"),
              GfsLamp(cache_dir=CACHE_ROOT / "gfs_lamp"),
              Hrrr(cache_dir=CACHE_ROOT / "hrrr", stations=resolved,
                   fhours=range(0, 19)),
              Nbm(cache_dir=CACHE_ROOT / "nbm")]
    cycle = find_latest_complete(probes, verbose=False)
    return cycle.isoformat() if cycle else None


@st.cache_data(ttl=600, show_spinner=False, max_entries=20)
def cached_compare(icao: str, cycle_iso: str):
    """Every model, one station, one cycle - the frame both plots and
    both grids read from. One fetch, four consumers."""
    from compare import compare_icaos
    cycle = datetime.fromisoformat(cycle_iso)
    df, resolved, _ = compare_icaos(icaos=[icao], cycle=cycle,
                                    cache_root=CACHE_ROOT)
    return df, bool(resolved)


@st.cache_data(ttl=45, show_spinner=False, max_entries=20)
def cached_traffic(lat: float, lon: float, bucket: str):
    """Aircraft within 20 nm; the bucket shares one query across
    viewers of the same field."""
    from core import airport_scope as _AS
    return _AS.traffic(lat, lon, 20)


@st.cache_data(ttl=120, show_spinner=False, max_entries=12)
def cached_movements(icao: str, hours_back: int, bucket: str):
    if not _SAMPLER_OK:
        return [], None
    try:
        return derive_movements(CACHE_ROOT, icao, hours_back), \
            sampling_since(CACHE_ROOT, icao)
    except Exception:
        return [], None


def _origin() -> str:
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")


# ------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Station")
    hub = st.selectbox("Hub", HUBS, index=0)
    typed = st.text_input("or any ICAO", value="", max_chars=4,
                          placeholder="e.g. KSYR").strip().upper()
    icao = typed if len(typed) == 4 else hub
    n_metars = st.selectbox("METARs to show", [1, 2, 3, 4, 5, 6], index=2)
    st.divider()
    st.subheader("Plots")
    horizon = st.slider("Forecast horizon (h)", 12, 72, 36, 6)
    speed_max = st.slider("Wind y-axis max (kt)", 20, 80, 40, 5)
    st.divider()
    mv_hours = st.selectbox("JBU movements window", [3, 6, 12, 24], index=1,
                            format_func=lambda h: f"Last {h} hours")

now = datetime.now(timezone.utc)

# -------------------------------------------------------------- header
h1, h2 = st.columns([3, 2])
with h1:
    st.title("Station Forecast")
    st.caption("Multi-model guidance, observations and radar for one airport")
with h2:
    chips = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;'
        f'background:#0D111A;border:1px solid {EDGE};border-radius:3px;'
        f'padding:4px 9px;margin:0 6px 0 0;color:{INK};font-size:11px;'
        f'font-weight:700"><span style="width:8px;height:8px;'
        f'border-radius:50%;background:{c};display:inline-block"></span>'
        f'{m.replace("_", " ")}</span>'
        for m, c in MODELS.items())
    st.markdown(f'<div style="margin-top:26px;text-align:right">{chips}</div>',
                unsafe_allow_html=True)

coords = cached_coords(icao)

cycle_iso = cached_latest_cycle(icao)
df, ok = (cached_compare(icao, cycle_iso) if cycle_iso else (pd.DataFrame(), False))
cycle = datetime.fromisoformat(cycle_iso) if cycle_iso else None
cyc_txt = f"cycle {cycle:%d/%HZ}" if cycle else "no complete cycle"

from core import mos_grid as G

# ============================================================ row 1
c_obs, c_rad = st.columns([2, 1], gap="small")

with c_obs:
    with st.container(border=True):
        from core.wxtext import wx_colored_box
        obs = cached_metars(icao, n_metars + 4)
        if obs:
            recent = obs[-n_metars:][::-1]
            age = int((now - recent[0].obs_time).total_seconds() // 60)
            pod_title("METAR", f"last {len(recent)} \u00b7 newest first \u00b7 "
                               f"{recent[0].obs_time:%H:%MZ}, {age} min ago")
            st.markdown(wx_colored_box([o.raw_text for o in recent]),
                        unsafe_allow_html=True)
        else:
            pod_title("METAR")
            st.warning("No recent METAR.")

    with st.container(border=True):
        taf = cached_taf(icao)
        if taf:
            first = taf.strip().split("\n")[0]
            pod_title("TAF", first[:60])
            st.markdown(wx_colored_box(taf.splitlines(), taf_mode=True),
                        unsafe_allow_html=True)
        else:
            pod_title("TAF")
            st.warning("No TAF available (station may not be a TAF site).")

    with st.container(border=True):
        pod_title("Flight conditions",
                  f"category by model, hour by hour · {cyc_txt}")
        if ok and len(df) and {"ceiling_ft", "vsby_sm"} <= set(df.columns):
            d = df[(df["station_id"] == icao)
                   & (df["valid_time"] <= cycle + pd.Timedelta(hours=horizon))]
            models = [m for m in MODELS if m in set(d["model"])]
            times = [cycle + pd.Timedelta(hours=h) for h in range(0, horizon + 1)]
            obs_rows = []
            try:
                for o in cached_metars(icao, 12):
                    obs_rows.append((o.obs_time, getattr(o, "ceiling_ft", None),
                                     bool(getattr(o, "ceiling_unlimited", False)),
                                     getattr(o, "vsby_sm", None)))
            except Exception:
                obs_rows = []
            st.markdown(G.category_strip(d, models, times, obs=obs_rows),
                        unsafe_allow_html=True)
        else:
            st.caption("No ceiling/visibility guidance for this station and cycle.")

with c_rad:
    with st.container(border=True):
        # The airport scope, exactly as Station Quick View draws it -
        # same runway table, same finals, same ATIS colouring - with
        # MRMS reflectivity underneath and no traffic, at 20 nm.
        stamp_txt, cfg = "", {}
        if coords:
            from core import airport_scope as AS
            base = _origin()
            try:
                from core import mrms as MR
                chunks, stamp = MR.newest(STATIC_MRMS, "REFL")
                stamp_txt = f"{stamp[9:11]}:{stamp[11:13]}Z" if stamp else ""
            except Exception:
                chunks = []
            surface = AS.surface(CACHE_ROOT / "scope", icao, coords[0], coords[1])
            ac = [a for a in cached_traffic(round(coords[0], 3), round(coords[1], 3),
                                            now.strftime("%Y%m%d%H%M")[:-1])
                  if a.get("airline")]
            layers, cfg = AS.mini_layers(icao, surface, coords[0], coords[1],
                                         mrms_chunks=chunks, base_url=base,
                                         range_nm=20, ac=ac)
        pod_title("Airport scope · MRMS",
                  f"20 nm · {stamp_txt or 'no current scan'}"
                  + (f" · {len(ac)} aircraft" if coords and ac else ""))
        if cfg.get("describe"):
            st.markdown(
                f'<div style="border:1px solid {EDGE};padding:6px 10px;'
                f'margin-bottom:6px;color:{INK};font-size:12px;font-weight:700;'
                f'font-family:DejaVu Sans Mono,monospace">{cfg["describe"]}</div>',
                unsafe_allow_html=True)
        elif coords:
            st.caption("No D-ATIS for this field \u2014 finals drawn off every end.")
        if coords:
            style = None
            try:
                host = st.context.headers.get("Host", "")
                if host and os.environ.get("BLUEMET_SCOPE_COAST", "carto") == "carto":
                    proto = st.context.headers.get("X-Forwarded-Proto", "https")
                    style = f"{proto}://{host}/app/static/scope_style.json"
            except Exception:
                style = None
            if style is None:
                cl = AS.coast_layer(icao)
                if cl is not None:
                    layers = [cl] + layers
            st.pydeck_chart(pdk.Deck(
                layers=layers,
                initial_view_state=AS.view(coords[0], coords[1], width_px=520,
                                           width_nm=20),
                views=[pdk.View(type="MapView", controller=False)],
                map_style=style, map_provider=("carto" if style else None),
                parameters={"clearColor": [0, 0, 0, 1]},
            ), use_container_width=True, height=372)
        else:
            st.caption(f"No coordinates for {icao}.")

# ============================================================ row 2
c_wind, c_mos = st.columns(2, gap="small")

with c_wind:
    with st.container(border=True):
        pod_title("Wind", f"speed, gust, direction · {cyc_txt}")
        if ok and len(df):
            from compare import plot_wind_comparison_interactive
            try:
                from core.metar import filter_since, metars_to_df
                mdf = metars_to_df(filter_since(
                    {icao: cached_metars(icao, 48)}, cycle))
            except Exception:
                mdf = None
            fig = plot_wind_comparison_interactive(
                df, icao, cycle=cycle, speed_ylim=(0, speed_max),
                hours_ahead=horizon, metars_df=mdf)
            fig.update_layout(
                width=None, autosize=True, paper_bgcolor=PANEL,
                plot_bgcolor="#05070B", font=dict(color=INK2, size=11),
                margin=dict(l=40, r=16, t=24, b=30),
                legend=dict(bgcolor="rgba(0,0,0,0)"))
            fig.update_xaxes(gridcolor="#1A2233", zerolinecolor="#1A2233")
            fig.update_yaxes(gridcolor="#1A2233", zerolinecolor="#1A2233")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("No model data for this station and cycle.")

with c_mos:
    for model, label, rows_fn in (("NBM", "NBM hourly", G.nbm_rows),
                                  ("GFS_LAMP", "GFS LAMP", G.lamp_rows)):
        with st.container(border=True):
            pod_title(label, f"{cyc_txt} · next 24 h")
            if ok and len(df):
                dm = df[(df["station_id"] == icao) & (df["model"] == model)]
                dm = dm[dm["valid_time"] <= cycle + pd.Timedelta(hours=25)]
                dm = dm.sort_values("valid_time").head(25)
                if len(dm):
                    st.markdown(G.grid(pd.to_datetime(dm["valid_time"]).tolist(),
                                       rows_fn(dm)), unsafe_allow_html=True)
                else:
                    st.caption(f"No {label} rows for this cycle.")
            else:
                st.caption("No model data.")
    st.markdown(G.legend(), unsafe_allow_html=True)

# ============================================================ row 4
def _board_table(rows, kind: str) -> str:
    """The board as HTML. st.dataframe draws through a canvas that
    takes its colours from .streamlit/config.toml, not from CSS, so
    without that file in place it can come out unreadable; this
    cannot."""
    th = (f"background:#121212;color:#00E5FF;font:bold 11px DejaVu Sans Mono,"
          f"monospace;padding:4px 10px;text-align:left;border:1px solid {EDGE};")
    td = (f"color:{INK};-webkit-text-fill-color:{INK};font:bold 12px DejaVu Sans "
          f"Mono,monospace;padding:4px 10px;border:1px solid #141A26;")
    out = [f'<div style="color:{INK2};font-size:11px;font-weight:700;'
           f'margin:4px 0 6px">{kind} · {len(rows)}</div>',
           '<table style="border-collapse:collapse;width:100%">'
           f'<tr><th style="{th}">Flight</th><th style="{th}">Time (Z)</th>'
           f'<th style="{th}">Alt band</th></tr>']
    if not rows:
        out.append(f'<tr><td colspan="3" style="{td}color:{MUTED};'
                   f'-webkit-text-fill-color:{MUTED};font-style:italic">'
                   'none derived in window</td></tr>')
    for m in rows[:8]:
        cs = m["callsign"]
        fl = f"B6 {cs[3:]}" if cs.startswith("JBU") else cs
        t = datetime.fromtimestamp(m["time_unix"], timezone.utc)
        out.append(f'<tr><td style="{td}">{fl}</td>'
                   f'<td style="{td}">{t:%m/%d %H:%M}</td>'
                   f'<td style="{td}">{m["alt_from"]}–{m["alt_to"]} ft</td></tr>')
    out.append("</table>")
    return "".join(out)


with st.container(border=True):
    movements, since_ts = cached_movements(icao, mv_hours,
                                           now.strftime("%Y%m%d%H%M")[:11])
    pod_title("JetBlue arrivals & departures",
              f"last {mv_hours} h · BlueMet terminal-area sampling, "
              f"fresh to 2–4 min")
    if not _SAMPLER_OK:
        st.caption(f"Movement sampler unavailable ({_SAMPLER_ERR})")
    elif not is_sampled(icao):
        st.caption(f"{icao} is not a JetBlue destination; the sampler does "
                   "not cover it.")
    else:
        if not movements:
            if since_ts:
                since = datetime.fromtimestamp(since_ts, timezone.utc)
                st.caption(f"No JetBlue movements derived in the last "
                           f"{mv_hours} h (sampling {icao} since "
                           f"{since:%m/%d %H:%M}Z).")
            else:
                st.caption("Sampler has no observations for this station yet.")
        arr = [m for m in movements if m["kind"] == "ARR"]
        dep = [m for m in movements if m["kind"] == "DEP"]
        a, d_ = st.columns(2)
        with a:
            st.markdown(_board_table(arr, "Arrivals"), unsafe_allow_html=True)
        with d_:
            st.markdown(_board_table(dep, "Departures"), unsafe_allow_html=True)
