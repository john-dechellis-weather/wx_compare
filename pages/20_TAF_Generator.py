"""TAF Generator (15 Oct).

Builds a TAF from tomorrow.io Timelines data with George Delanjian's
"practically perfect" criteria (core/taf_maker.py) for one or more
stations at once, and sets each result beside what a forecaster
would check it against: the government TAF, the last 6 hours of
METARs, and the NBM hourly + GFS LAMP MOS grids.

Each TAF costs 27 tomorrow.io requests, charged to the site's daily
budget before the run starts.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="BlueMet — TAF Generator", layout="wide")

from retro_theme import apply_retro_theme
apply_retro_theme()
from dark_theme import apply_dark_theme
apply_dark_theme()
from auth import check_password
check_password()

from core import mos_grid as G
from core.wxtext import wx_colored_box

_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent.parent / "static"

EDGE, PANEL, INK, INK2, MUTED = "#2D3957", "#0A0A0A", "#FFFFFF", "#B8B8B8", "#6E6E6E"
TEXT_PX = 13
METAR_HOURS = 6

st.markdown(
    "<style>"
    f'[data-testid="stVerticalBlockBorderWrapper"]{{'
    f"background:{PANEL} !important;border:1px solid {EDGE} !important;"
    "border-radius:4px !important;padding:10px 14px 12px !important;}"
    ".pod-title{color:#B8B8B8;font-size:11px;font-weight:700;"
    "letter-spacing:.6px;text-transform:uppercase;display:flex;"
    "justify-content:space-between;align-items:baseline;"
    "border-bottom:1px solid #1A2233;padding-bottom:6px;margin-bottom:8px;}"
    ".pod-title span{color:#6E6E6E;font-size:10px;font-weight:400;"
    "text-transform:none;letter-spacing:0}"
    ".tg-station{font-size:2rem;font-weight:700;color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;"
    "margin:6px 0 2px;letter-spacing:.5px}"
    ".tg-station span{font-size:12px;color:#6E6E6E;-webkit-text-fill-color:#6E6E6E;"
    "font-weight:400;margin-left:12px}"
    "</style>", unsafe_allow_html=True)


def pod_title(title: str, sub: str = "") -> None:
    st.markdown(f'<div class="pod-title">{title}<span>{sub}</span></div>',
                unsafe_allow_html=True)


# ------------------------------------------------------------ fetchers
@st.cache_data(ttl=300, show_spinner=False, max_entries=40)
def cached_metars(icao: str, hours_back: int):
    from core.metar import fetch_metars
    obs = fetch_metars([icao], hours_back=hours_back).get(icao.upper(), [])
    return sorted(obs, key=lambda o: o.obs_time)


@st.cache_data(ttl=300, show_spinner=False, max_entries=40)
def cached_taf(icao: str):
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


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_latest_cycle(icao: str) -> str | None:
    try:
        from core.compare_warm import latest_cycle
        _c = latest_cycle(CACHE_ROOT)
        if _c:
            return _c
    except Exception:
        pass
    try:
        from core.stations import StationResolver
        from core.cycle_select import find_latest_complete
        from models import GfsMos, GfsLamp, Hrrr, Nbm
        resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
        resolved, _ = resolver.resolve_many([icao])
        if not resolved:
            return None
        probes = [GfsMos(cache_dir=CACHE_ROOT / "gfs_mos"),
                  GfsLamp(cache_dir=CACHE_ROOT / "gfs_lamp"),
                  Hrrr(cache_dir=CACHE_ROOT / "hrrr", stations=resolved, fhours=range(0, 19)),
                  Nbm(cache_dir=CACHE_ROOT / "nbm")]
        cycle = find_latest_complete(probes, verbose=False)
        return cycle.isoformat() if cycle else None
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False, max_entries=40)
def cached_compare(icao: str, cycle_iso: str):
    try:
        from core.compare_warm import read_frame
        _hit = read_frame(CACHE_ROOT, icao, cycle_iso)
        if _hit is not None:
            return _hit
    except Exception:
        pass
    try:
        from compare import compare_icaos
        cycle = datetime.fromisoformat(cycle_iso)
        df, resolved, _ = compare_icaos(icaos=[icao], cycle=cycle, cache_root=CACHE_ROOT)
        return df, bool(resolved)
    except Exception:
        return pd.DataFrame(), False


# -------------------------------------------------------------- header
st.markdown(
    f'<div style="font-size:3.2rem;font-weight:700;line-height:1.1;'
    f'color:{INK};-webkit-text-fill-color:{INK};margin:0">TAF Generator</div>',
    unsafe_allow_html=True)
st.caption("tomorrow.io Timelines → TAF (practically-perfect criteria, G. Delanjian). "
           "Each generated TAF is shown beside the government TAF, the last "
           f"{METAR_HOURS} h of METARs and the NBM / GFS LAMP guidance.")

try:
    from core import tio_map as TIO
    _b = TIO.budget(STATIC)
    _free = int(_b.get("free", 0))
    _bud_txt = (f"tomorrow.io today: {_b['used']:,} of {_b['cap']:,} used · "
                f"{_free:,} free for TAFs ({_free // 27} TAFs at 27 requests each)")
except Exception:
    TIO, _free, _bud_txt = None, 0, "tomorrow.io budget unavailable"

# ------------------------------------------------------------- controls
with st.container(border=True):
    pod_title("Generate", _bud_txt)
    c1, c2, c3, c4 = st.columns([3, 1.2, 1.2, 1], gap="small", vertical_alignment="bottom")
    with c1:
        _raw = st.text_input("Stations (ICAO, space or comma separated)",
                             value=st.session_state.get("tg_icaos", "KJFK KBOS KFLL"),
                             key="tg_icaos_in",
                             help="Up to 12 stations per run. Each TAF costs 27 tomorrow.io requests.")
    with c2:
        _now = datetime.now(timezone.utc)
        _hours_opts = ["now"] + [f"{h:02d}Z" for h in range(24)]
        _start_pick = st.selectbox("Valid start (UTC)", _hours_opts, index=0, key="tg_start",
                                   help="Blank/now = issue now, valid from the nearest hour. "
                                        "An hour earlier than the current UTC hour means tomorrow.")
    with c3:
        _hours = st.selectbox("TAF length (h)", [6, 9, 12, 18, 24, 30], index=4, key="tg_hours")
    with c4:
        _go = st.button("Generate TAFs", type="primary", use_container_width=True, key="tg_go")

    _icaos = []
    for tok in _raw.replace(",", " ").upper().split():
        tok = tok.strip()
        if 4 <= len(tok) <= 6 and tok not in _icaos:
            _icaos.append(tok)
    _icaos = _icaos[:12]

    if _go:
        st.session_state["tg_icaos"] = _raw
        if not _icaos:
            st.error("Enter at least one ICAO.")
        else:
            if _start_pick == "now":
                _start = None
            else:
                hh = int(_start_pick[:2])
                _start = _now.replace(hour=hh, minute=0, second=0, microsecond=0)
                if hh < _now.hour:
                    _start += timedelta(days=1)
            from core import taf_maker as TM
            results = []
            prog = st.progress(0.0, text="Generating…")
            for i, ic in enumerate(_icaos):
                prog.progress(i / len(_icaos), text=f"Generating {ic}…")
                try:
                    r = TM.generate(ic, hours=int(_hours), start=_start, cache_root=str(CACHE_ROOT))
                    r["error"] = ""
                except Exception as exc:
                    r = {"icao": ic, "taf": "", "banner": "", "error": f"{type(exc).__name__}: {exc}",
                         "issued": _now, "valid_from": None, "valid_to": None}
                results.append(r)
            prog.empty()
            st.session_state["tg_results"] = results
            st.session_state["tg_run_at"] = _now

_results = st.session_state.get("tg_results") or []
if not _results:
    st.info("Enter stations and press **Generate TAFs**. Results stay on the page "
            "until the next run; the comparison pods refresh on their own.")
    st.stop()

_run_at = st.session_state.get("tg_run_at")
st.caption(f"Generated {_run_at:%d %b %H:%MZ} · {len(_results)} station"
           f"{'s' if len(_results) != 1 else ''} · "
           f"{sum(1 for r in _results if not r['error']) * 27} tomorrow.io requests")

# ------------------------------------------------------------- stations
now = datetime.now(timezone.utc)
for r in _results:
    icao = r["icao"]
    short = icao[1:] if icao[0] == "K" and len(icao) == 4 else icao
    cycle_iso = cached_latest_cycle(icao)
    df, ok = (cached_compare(icao, cycle_iso) if cycle_iso else (pd.DataFrame(), False))
    cycle = datetime.fromisoformat(cycle_iso) if cycle_iso else None
    cyc_txt = f"cycle {cycle:%d/%HZ}" if cycle else "no complete cycle"

    st.markdown(f'<div class="tg-station">{short}<span>{r.get("banner") or icao}</span></div>',
                unsafe_allow_html=True)

    t1, t2 = st.columns(2, gap="small")
    with t1:
        with st.container(border=True):
            if r["error"]:
                pod_title("Generated TAF", "failed")
                st.error(r["error"])
            else:
                pod_title("Generated TAF · tomorrow.io",
                          f"issued {r['issued']:%d%H%MZ} · valid "
                          f"{r['valid_from']:%d%H}/{r['valid_to']:%d%H}")
                st.markdown(wx_colored_box(r["taf"].splitlines(), taf_mode=True, font_px=TEXT_PX),
                            unsafe_allow_html=True)
    with t2:
        with st.container(border=True):
            gov = cached_taf(icao)
            if gov:
                pod_title("Government TAF", gov.strip().split("\n")[0][:60])
                st.markdown(wx_colored_box(gov.splitlines(), taf_mode=True, font_px=TEXT_PX),
                            unsafe_allow_html=True)
            else:
                pod_title("Government TAF")
                st.warning("No TAF available (station may not be a TAF site).")

    with st.container(border=True):
        obs = cached_metars(icao, METAR_HOURS + 1)
        obs = [o for o in obs if o.obs_time >= now - timedelta(hours=METAR_HOURS)]
        if obs:
            recent = obs[::-1]
            age = int((now - recent[0].obs_time).total_seconds() // 60)
            pod_title("METAR", f"last {METAR_HOURS} h · {len(recent)} obs · newest first · "
                               f"{recent[0].obs_time:%H:%MZ}, {age} min ago")
            st.markdown(wx_colored_box([o.raw_text for o in recent], font_px=TEXT_PX),
                        unsafe_allow_html=True)
        else:
            pod_title("METAR")
            st.warning("No METAR in the last 6 hours.")

    g1, g2 = st.columns(2, gap="small")
    for col, (model, label, rows_fn) in zip(
            (g1, g2), (("NBM", "NBM hourly", G.nbm_rows), ("GFS_LAMP", "GFS LAMP", G.lamp_rows))):
        with col:
            with st.container(border=True):
                pod_title(label, f"{cyc_txt} · next 24 h")
                if ok and len(df):
                    dm = df[(df["station_id"] == icao) & (df["model"] == model)]
                    dm = dm[dm["valid_time"] <= cycle + pd.Timedelta(hours=25)]
                    dm = dm.sort_values("valid_time").head(25)
                    if len(dm):
                        st.markdown(G.grid(pd.to_datetime(dm["valid_time"]).tolist(), rows_fn(dm)),
                                    unsafe_allow_html=True)
                    else:
                        st.caption(f"No {label} rows for this cycle.")
                else:
                    st.caption("No model data for this station.")
    with g2:
        st.markdown(G.legend(), unsafe_allow_html=True)
    st.markdown("<hr style='border:0;border-top:1px solid #1A2233;margin:14px 0'>",
                unsafe_allow_html=True)
