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

# Height of the airport scope. The left column - METAR, TAF, the
# JetBlue board, the flight-conditions strip - adds up to about this,
# so the two columns end level and the Wind / MOS row starts under
# both.
SCOPE_H = int(os.environ.get("BLUEMET_SF_SCOPE_H", "1000"))

# Text size in the left column's boxes (METAR, TAF, category strip,
# JetBlue board). Sized so that column stands as tall as the scope
# beside it; raise or lower with the env var if a station's TAF runs
# long or short.
SF_TEXT_PX = int(os.environ.get("BLUEMET_SF_TEXT_PX", "15"))
PLOT_H = 620          # wind, level with the two grids beside it
CV_H = 420            # ceiling & visibility, under the strip

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
    # The compare warmer (core/compare_warm.py) already found it - no
    # NOMADS probing on the request path. Falls through to probing
    # only when the warmer has not run yet.
    try:
        from core.compare_warm import latest_cycle
        _c = latest_cycle(CACHE_ROOT)
        if _c:
            return _c
    except Exception:
        pass
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
    both grids read from. One fetch, four consumers.

    Read from the compare warmer's saved frame when it has one (a few
    ms); computed here only for a station or cycle it has not built
    yet (4-15 s)."""
    try:
        from core.compare_warm import read_frame
        _hit = read_frame(CACHE_ROOT, icao, cycle_iso)
        if _hit is not None:
            return _hit
    except Exception:
        pass
    from compare import compare_icaos
    cycle = datetime.fromisoformat(cycle_iso)
    df, resolved, _ = compare_icaos(icaos=[icao], cycle=cycle,
                                    cache_root=CACHE_ROOT)
    return df, bool(resolved)


@st.cache_data(ttl=45, show_spinner=False, max_entries=20)
def cached_traffic(lat: float, lon: float, bucket: str):
    """Aircraft within 150 nm - wide enough that JetBlue traffic is
    already loaded when the map is zoomed well out. The bucket shares
    one query across viewers of the same field."""
    from core import airport_scope as _AS
    return _AS.traffic(lat, lon, 150)


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

now = datetime.now(timezone.utc)


def _inbound_now(icao: str, coords, now) -> list:
    """JetBlue aircraft within 30 nm tracking toward the field, nearest
    first: the 'next' side of the board. Reads the same traffic query
    the scope uses, so it costs nothing extra."""
    import math
    rows = cached_traffic(round(coords[0], 3), round(coords[1], 3),
                          now.strftime("%Y%m%d%H%M")[:-1])
    lat, lon = coords

    def brg_to(alat, alon):
        p1, p2 = math.radians(alat), math.radians(lat)
        dl = math.radians(lon - alon)
        x = math.sin(dl) * math.cos(p2)
        y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return (math.degrees(math.atan2(x, y)) + 360) % 360

    out = []
    for a in rows:
        if not a.get("jbu"):
            continue
        alon, alat = a["position"]
        alt = a.get("alt")
        if isinstance(alt, (int, float)) and alt > 18000:
            continue
        diff = abs((brg_to(alat, alon) - float(a.get("hdg") or 0) + 180) % 360 - 180)
        if diff > 60:
            continue
        from core import airport_scope as _AS
        out.append({"callsign": a["callsign"],
                    "nm": _AS.distance_nm(lat, lon, alat, alon),
                    "alt": alt, "gs": a.get("gs")})
    out.sort(key=lambda r: r["nm"])
    return out


def _board_table(rows, kind: str, inbound=None) -> str:
    """The board as HTML. st.dataframe draws through a canvas that
    takes its colours from .streamlit/config.toml, not from CSS, so
    without that file in place it can come out unreadable; this
    cannot."""
    th = (f"background:#121212;color:#00E5FF;font:bold {SF_TEXT_PX - 2}px "
          f"DejaVu Sans Mono,monospace;padding:5px 10px;text-align:left;"
          f"border:1px solid {EDGE};")
    td = (f"color:{INK};-webkit-text-fill-color:{INK};font:bold {SF_TEXT_PX - 1}px "
          f"DejaVu Sans Mono,monospace;padding:5px 10px;border:1px solid #141A26;")
    muted = f"{td}color:{MUTED};-webkit-text-fill-color:{MUTED};font-style:italic;"

    def fl(cs):
        return f"B6 {cs[3:]}" if cs.startswith("JBU") else cs

    out = []
    if inbound is not None:
        out += [f'<div style="color:{INK2};font-size:{SF_TEXT_PX - 2}px;'
                f'font-weight:700;margin:4px 0 6px">Inbound now \u00b7 '
                f'{len(inbound)}</div>',
                '<table style="border-collapse:collapse;width:100%">'
                f'<tr><th style="{th}">Flight</th><th style="{th}">Dist</th>'
                f'<th style="{th}">Alt</th></tr>']
        if not inbound:
            out.append(f'<tr><td colspan="3" style="{muted}">none within 30 nm</td></tr>')
        for r in inbound[:6]:
            alt = r["alt"]
            alt_s = (f"{int(alt):,} ft" if isinstance(alt, (int, float))
                     else ("GND" if str(alt).lower().startswith("gr") else "\u2014"))
            out.append(f'<tr><td style="{td}">{fl(r["callsign"])}</td>'
                       f'<td style="{td}">{r["nm"]:.0f} nm</td>'
                       f'<td style="{td}">{alt_s}</td></tr>')
        out.append("</table>")
    out += [f'<div style="color:{INK2};font-size:{SF_TEXT_PX - 2}px;'
            f'font-weight:700;margin:10px 0 6px">{kind} \u00b7 last 3 h \u00b7 '
            f'{len(rows)}</div>',
            '<table style="border-collapse:collapse;width:100%">'
            f'<tr><th style="{th}">Flight</th><th style="{th}">Time (Z)</th>'
            f'<th style="{th}">Alt band</th></tr>']
    if not rows:
        out.append(f'<tr><td colspan="3" style="{muted}">none derived in window</td></tr>')
    for m in rows[:6]:
        t = datetime.fromtimestamp(m["time_unix"], timezone.utc)
        out.append(f'<tr><td style="{td}">{fl(m["callsign"])}</td>'
                   f'<td style="{td}">{t:%H:%M}</td>'
                   f'<td style="{td}">{m["alt_from"]}\u2013{m["alt_to"]} ft</td></tr>')
    out.append("</table>")
    return "".join(out)

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
c_obs, c_rad = st.columns(2, gap="small")

with c_obs:
    with st.container(border=True):
        from core.wxtext import wx_colored_box
        obs = cached_metars(icao, n_metars + 4)
        if obs:
            recent = obs[-n_metars:][::-1]
            age = int((now - recent[0].obs_time).total_seconds() // 60)
            pod_title("METAR", f"last {len(recent)} \u00b7 newest first \u00b7 "
                               f"{recent[0].obs_time:%H:%MZ}, {age} min ago")
            st.markdown(wx_colored_box([o.raw_text for o in recent],
                                       font_px=SF_TEXT_PX),
                        unsafe_allow_html=True)
        else:
            pod_title("METAR")
            st.warning("No recent METAR.")

    with st.container(border=True):
        taf = cached_taf(icao)
        if taf:
            first = taf.strip().split("\n")[0]
            pod_title("TAF", first[:60])
            st.markdown(wx_colored_box(taf.splitlines(), taf_mode=True,
                                       font_px=SF_TEXT_PX),
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
            st.markdown(G.category_strip(d, models, times, obs=obs_rows,
                                         font_px=SF_TEXT_PX - 3),
                        unsafe_allow_html=True)
        else:
            st.caption("No ceiling/visibility guidance for this station and cycle.")

    with st.container(border=True):
        pod_title("Ceiling & visibility", f"by model · {cyc_txt}")
        if ok and len(df) and {"ceiling_ft", "vsby_sm"} <= set(df.columns):
            # THE ORIGINAL FIGURE, as the wind pod beside it already
            # does: compare.plot_comparison_interactive is what the
            # VIS/CIG page drew. A hand-built version here stacked
            # every point on one timestamp and put the axes on a log
            # scale; this is the plot people know.
            from compare import plot_comparison_interactive
            try:
                from core.metar import filter_since, metars_to_df
                mdf = metars_to_df(filter_since(
                    {icao: cached_metars(icao, 48)}, cycle))
            except Exception:
                mdf = None
            fig = plot_comparison_interactive(
                df, icao, cycle=cycle, hours_ahead=horizon, metars_df=mdf)
            # Same dark restyle the wind pod applies; the traces, axes
            # and category bands are the original's.
            fig.update_layout(
                title=None,
                height=CV_H, width=None, autosize=True, paper_bgcolor=PANEL,
                plot_bgcolor="#05070B",
                font=dict(color=INK2, size=11, family="Roboto, Arial"),
                margin=dict(l=40, r=16, t=24, b=30),
                legend=dict(bgcolor="rgba(0,0,0,0)"))
            fig.update_xaxes(gridcolor="#1A2233", zerolinecolor="#1A2233")
            fig.update_yaxes(gridcolor="#1A2233", zerolinecolor="#1A2233")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("No ceiling/visibility guidance for this station and cycle.")


def _search_fleet(q: str) -> list:
    """JetBlue aircraft in the last fleet sweep matching a flight
    number or callsign fragment ("1123", "JBU1123", "B61123")."""
    import re as _re
    q = (q or "").strip().upper()
    if not q:
        return []
    digits = _re.sub(r"\D", "", q)
    hits = []
    try:
        from core import fleet as _FL
        res = _FL.STATE.get("res")
        for r in (res[0] if res else []) or []:
            cs = (r.get("callsign") or "").upper()
            if q in cs or (digits and _re.sub(r"\D", "", cs) == digits):
                hits.append(r)
    except Exception:
        return []
    return hits[:5]


# Read the search box's value before the scope draws, so the highlight
# is on this run, not the next one.
_ac_hits = _search_fleet(st.session_state.get("sf_ac_query", ""))

with c_rad:
    with st.container(border=True):
        # The airport scope, exactly as Station Quick View draws it -
        # same runway table, same finals, same ATIS colouring - with
        # MRMS reflectivity underneath and no traffic, at 20 nm.
        stamp_txt, cfg, l3_txt, l3_frames, base = "", {}, "", [], ""
        _radar_diag = []
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
            _all = cached_traffic(round(coords[0], 3), round(coords[1], 3),
                                  now.strftime("%Y%m%d%H%M")[:-1])
            # JetBlue anywhere in the 150 nm pull, so they are there when
            # the map is zoomed out; other operators only inside the
            # frame the map opens at.
            ac = [a for a in _all
                  if a.get("jbu")
                  or (a.get("airline")
                      and AS.distance_nm(coords[0], coords[1],
                                         a["position"][1], a["position"][0]) <= 20)]
            _mode = st.session_state.get("sf_radar_mode", "NEXRAD Radar")
            # NEXRAD shows the single-site picture only; MRMS shows the
            # mosaic only. Each is one thing.
            _want_mrms = _mode == "MRMS Precipitation"
            _want_l3 = _mode == "NEXRAD Radar"
            layers, cfg = AS.mini_layers(
                icao, surface, coords[0], coords[1],
                mrms_chunks=(chunks if _want_mrms else None),
                base_url=base, range_nm=20, ac=ac)
            # LEVEL III on top of MRMS: the station's nearest NEXRAD at
            # 250 m, built by the L3 warmer for every hub (core/radar_l3
            # STATION_DOMAINS), read here as one image - no radar work
            # on page load. MRMS stays underneath for the area outside
            # the Level III box and as the fallback while it warms.
            l3_txt = ""
            l3_frames = []
            # Diagnostics for the "Radar status" expander: everything
            # the scope needed and what it found, so a missing radar
            # can be read off the page instead of the server log.
            _radar_diag.append(f"static dir: {STATIC_MRMS} "
                               f"({'exists' if STATIC_MRMS.exists() else 'MISSING'})")
            # Can this process write there? A deploy that mounts the
            # repo read-only would leave every warmer silent.
            try:
                _t = STATIC_MRMS / ".write_test"
                _t.write_text("ok")
                _t.unlink()
                _radar_diag.append("static dir writable: yes")
            except Exception as _we:
                _radar_diag.append(f"static dir writable: NO ({_we})")
            # Which warmer threads are alive in this process, and what
            # Homepage recorded when it started them.
            try:
                import threading as _th
                _names = sorted(t.name for t in _th.enumerate()
                                if t.name not in ("MainThread",))
                _radar_diag.append("threads: " + (", ".join(_names) or "none"))
            except Exception:
                pass
            try:
                import __main__ as _hp
                for _n in (getattr(_hp, "_warm_notes", None) or []):
                    _radar_diag.append("homepage: " + str(_n))
                if not getattr(_hp, "_warm_notes", None):
                    _radar_diag.append("homepage: no warmer notes (the "
                                       "running Homepage.py is not the "
                                       "current one)")
            except Exception as _he:
                _radar_diag.append(f"homepage notes unavailable: {_he}")
            try:
                _ml = (STATIC_MRMS / "mrms_warmer.log").read_text().splitlines()
                _radar_diag.extend("mrms log: " + ln for ln in _ml[-3:])
            except OSError:
                _radar_diag.append("mrms log: none")
            _radar_diag.append(f"MRMS newest: {stamp or 'none'}, "
                               f"{len(chunks) if chunks else 0} chunks")
            _radar_diag.append(f"page origin: {base or 'NONE (images cannot load)'}")
            try:
                from core import radar_l3 as L3
                _dom = L3.station_domain(icao)
                _all = sorted(STATIC_MRMS.glob(f"l3_{_dom}_*.json"))
                _radar_diag.append(
                    f"Level III domain {_dom}: {len(_all)} manifests on disk"
                    + (f", newest {_all[-1].name}" if _all else ""))
                _st = L3.STATUS.get(_dom)
                if _st:
                    _radar_diag.append(f"last build: {_st.get('note')}")
                try:
                    _lg = (STATIC_MRMS / "l3_warmer.log").read_text().splitlines()
                    _radar_diag.extend("log: " + ln for ln in _lg[-4:])
                except OSError:
                    _radar_diag.append("log: no l3_warmer.log yet (warmer "
                                       "has not started or cannot write)")
            except Exception as _de:
                _radar_diag.append(f"radar_l3 import/diag failed: {_de}")
            if base and _want_l3:
                try:
                    from core import radar_l3 as L3
                    l3_frames = L3.frames(STATIC_MRMS, L3.station_domain(icao))
                    # RADAR LOOP SLIDER (drawn above the scope, below):
                    # which of the last hour's frames to show. The
                    # slider's value is read here so this run draws it;
                    # 0 = oldest ... n-1 = latest. Latest by default and
                    # whenever the airport changes.
                    _n = len(l3_frames)
                    _pick = st.session_state.get(f"sf_l3_slider_{icao}")
                    if _pick is None or _pick >= _n:
                        _pick = _n - 1
                    _man = l3_frames[_pick] if _n else None
                    _l3s = _man["stamp"] if _man else None
                    if _man and L3.age_s(_l3s) < 4800:
                        _l3 = pdk.Layer(
                            "BitmapLayer", data=None,
                            image=f"{base}/app/static/{_man['name']}",
                            bounds=_man["bounds"], opacity=1.0)
                        _after = max([i for i, l in enumerate(layers)
                                      if getattr(l, "type", "") == "BitmapLayer"]
                                     + [-1]) + 1
                        layers.insert(_after, _l3)
                        l3_txt = (f" · NEXRAD {_man['site']} "
                                  f"{_l3s[9:11]}:{_l3s[11:13]}Z")
                    elif icao.upper() in L3.STATION_RADAR:
                        # Distinguish "the warmer has built nothing for
                        # this station" from "it looked and found no
                        # radar": the first is a warmer problem.
                        l3_txt = (" · NEXRAD: no frames yet (see Radar "
                                  "status)" if not l3_frames else
                                  " · NEXRAD frames are stale")
                except Exception:
                    l3_txt = ""
        pod_title("Airport scope",
                  "20 nm"
                  + ((f" · MRMS {stamp_txt or 'no current scan'}"
                      if _want_mrms else l3_txt if _want_l3 else " · radar off")
                     if coords else "")
                  + (f" · {len(ac)} aircraft" if coords and ac else ""))
        if coords and len(l3_frames) > 1:
            # Last hour of the airport's radar: drag to step back
            # through the frames. Labels are the scan times.
            _lab = [f"{f['stamp'][9:11]}:{f['stamp'][11:13]}Z"
                    for f in l3_frames]
            _k = f"sf_l3_slider_{icao}"
            if st.session_state.get(_k, len(_lab)) >= len(_lab):
                st.session_state[_k] = len(_lab) - 1
            st.select_slider(
                "Radar time (last hour)", options=list(range(len(_lab))),
                format_func=lambda k: _lab[k], key=_k,
                help="Level III frames from the last hour, oldest to "
                     "newest. MRMS underneath stays current.")
        _rc1, _rc2 = st.columns([2, 1])
        with _rc1:
            radar_mode = st.radio(
                "Radar", ["NEXRAD Radar", "MRMS Precipitation", "Off"],
                horizontal=True, key="sf_radar_mode",
                label_visibility="collapsed",
                help="NEXRAD Radar: the airport's nearest radar (TDWR or "
                     "NEXRAD) at 250 m. MRMS Precipitation: the 1 km "
                     "mosaic. Off: field and traffic on black.")
        with _rc2:
            with st.expander("Radar status", expanded=False):
                st.caption("\n".join(_radar_diag))
        if coords:
            from core import station_status as SS
            _latest = obs[-1].raw_text if obs else ""
            _alert = SS.alert_for(_latest)
            _eta = AS.inbound_eta(_all, coords[0], coords[1], max_min=60)
            if _eta:
                _items = " &nbsp;|&nbsp; ".join(
                    f"{r['callsign']} Arrival ETA: "
                    f"{(now + pd.Timedelta(minutes=r['eta_min'])):%H%M}Z"
                    for r in _eta[:4])
                if _alert:
                    st.markdown(
                        f'<div style="background:#B3000A;border:2px solid #FF3B30;'
                        f'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
                        f'font:bold 14px DejaVu Sans Mono,monospace;'
                        f'padding:8px 12px;margin-bottom:6px">'
                        f'{_items} &nbsp;|&nbsp; {_alert} ALERT</div>',
                        unsafe_allow_html=True)
                else:
                    st.markdown(
                        f'<div style="background:{PANEL};border:1px solid #00E5FF;'
                        f'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
                        f'font:bold 13px DejaVu Sans Mono,monospace;'
                        f'padding:6px 12px;margin-bottom:6px">{_items}</div>',
                        unsafe_allow_html=True)
        if cfg.get("describe"):
            st.markdown(
                f'<div style="border:1px solid {EDGE};padding:6px 10px;'
                f'margin-bottom:6px;color:{INK};font-size:12px;font-weight:700;'
                f'font-family:DejaVu Sans Mono,monospace">{cfg["describe"]}</div>',
                unsafe_allow_html=True)
        elif coords:
            st.caption("No D-ATIS for this field \u2014 finals drawn off every end.")
        if coords:
            _hl = [{"position": [r["lon"], r["lat"]],
                    "callsign": r.get("callsign", "")} for r in _ac_hits]
            if _hl:
                layers.append(pdk.Layer(
                    "ScatterplotLayer", _hl, get_position="position",
                    get_radius=1400, radius_min_pixels=14,
                    radius_max_pixels=22, filled=False, stroked=True,
                    get_line_color=[0, 229, 255, 255],
                    line_width_min_pixels=2.5, pickable=False))
                layers.append(pdk.Layer(
                    "TextLayer", _hl, get_position="position",
                    get_text="callsign", get_size=1400,
                    size_min_pixels=0, size_max_pixels=13,
                    get_color=[0, 229, 255, 255], font_weight="bold",
                    get_pixel_offset=[0, -22],
                    get_text_anchor='"middle"',
                    get_alignment_baseline='"center"', pickable=False))
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
            # WHOLE FLEET + STATIONS (22 Sep): every JetBlue aircraft
            # from the fleet sweep (one icon layer, ~130 rows, no
            # extra fetch) and every JBU station as a blue dot with a
            # blue label, so zooming out shows the network. The
            # scope's own traffic layer keeps the local detail.
            try:
                from core import fleet as _FL2
                from core import station_status as _SS2
                _fr = _FL2.STATE.get("res")
                _fleet_rows = [{"position": [r["lon"], r["lat"]],
                                "angle": float(r.get("angle") or 0.0),
                                "callsign": r.get("callsign", ""),
                                "type": "", "alt": r.get("alt", ""),
                                "gs": int(r.get("gs") or 0)}
                               for r in ((_fr[0] if _fr else []) or [])
                               if r.get("lat") is not None]
                if _fleet_rows:
                    import urllib.parse as _up
                    _svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" '
                            'height="64" viewBox="-11 -11 22 22"><path d="M0,-10 '
                            'L0.35,-9.6 L0.55,-8.8 L0.6,-6 L0.6,-1.6 L9.2,3.2 '
                            'L9.6,3.4 L9.6,4 L9.1,4.1 L2.6,3.3 L0.6,3.1 L0.6,6.4 '
                            'L3.3,8.2 L3.3,9 L0.5,8.5 L0.45,9.4 L0,9.7 L-0.45,9.4 '
                            'L-0.5,8.5 L-3.3,9 L-3.3,8.2 L-0.6,6.4 L-0.6,3.1 '
                            'L-2.6,3.3 L-9.1,4.1 L-9.6,4 L-9.6,3.4 L-9.2,3.2 '
                            'L-0.6,-1.6 L-0.6,-6 L-0.55,-8.8 L-0.35,-9.6 Z" '
                            'fill="#4DA3FF" stroke="#000" stroke-width="0.6"/></svg>')
                    _icon = {"url": "data:image/svg+xml;charset=utf-8,"
                                    + _up.quote(_svg), "width": 64, "height": 64,
                             "anchorX": 32, "anchorY": 32, "mask": False}
                    for r in _fleet_rows:
                        r["icon"] = _icon
                    layers.append(pdk.Layer(
                        "IconLayer", _fleet_rows, get_position="position",
                        get_icon="icon", get_angle="angle",
                        get_size=32344, size_units="meters",
                        size_min_pixels=9, size_max_pixels=18,
                        pickable=True))
                _stn = [{"position": [lo, la], "name": k}
                        for k, (la, lo) in _SS2.STATION_LATLON.items()]
                layers.append(pdk.Layer(
                    "ScatterplotLayer", _stn, get_position="position",
                    get_radius=1200, radius_min_pixels=3,
                    radius_max_pixels=5, get_fill_color=[77, 163, 255, 255],
                    pickable=False))
                layers.append(pdk.Layer(
                    "TextLayer", _stn, get_position="position",
                    get_text="name", get_size=2600, size_min_pixels=0,
                    size_max_pixels=11, get_color=[77, 163, 255, 255],
                    font_weight="bold", get_pixel_offset=[9, -6],
                    get_text_anchor='"start"',
                    get_alignment_baseline='"center"', pickable=False))
            except Exception:
                pass
            _vs = AS.view(coords[0], coords[1], width_px=780, width_nm=20)
            # Zoom out as far as you like (the scope used to stop two
            # levels out).
            _vs.min_zoom = 2
            st.pydeck_chart(pdk.Deck(
                layers=layers,
                initial_view_state=_vs,
                # Interactive: drag to pan, wheel to zoom. The view
                # state only sets where it opens.
                views=[pdk.View(type="MapView",
                                controller={"scrollZoom": True, "dragPan": True,
                                            "dragRotate": False,
                                            "doubleClickZoom": True,
                                            "inertia": True})],
                map_style=style, map_provider=("carto" if style else None),
                tooltip={"html": "<b>{callsign}</b> {type}<br/>{alt} ft "
                                 "&middot; {gs} kt",
                         "style": {"backgroundColor": "#0A0A0A",
                                   "color": "#FFFFFF",
                                   "border": f"1px solid {EDGE}",
                                   "fontSize": "12px"}},
                parameters={"clearColor": [0, 0, 0, 1]},
            ), use_container_width=True, height=SCOPE_H)
        else:
            st.caption(f"No coordinates for {icao}.")

    # Diagnostic line for the traffic path: which fetch served the
    # scope, how many rows it returned, and what it kept. Stays until
    # the live-aircraft question is settled.
    if coords:
        _d = AS.LAST_TRAFFIC
        st.caption(
            f"traffic: {_d['fetched']} via {_d['source'] or '?'} \u00b7 "
            f"{_d['kept']} with callsign \u00b7 "
            f"{sum(1 for a in ac if a.get('jbu'))} JetBlue"
            + (f" \u00b7 {_d['error']}" if _d.get("error") else ""))

# ============================================================ row 2

with c_rad:
    with st.container(border=True):
        # AIRCRAFT SEARCH. A flight number or callsign; the aircraft
        # is highlighted on the scope above and described here. Any
        # JetBlue aircraft the fleet sweep knows about is findable,
        # not only those within the scope's pull.
        pod_title("Aircraft search",
                  "flight number or callsign · highlighted on the scope")
        _q = st.text_input("Flight", value="", key="sf_ac_query",
                           placeholder="e.g. 1123, JBU1123, B61123",
                           label_visibility="collapsed").strip().upper()
        if _q:
            _hits = _ac_hits
            if not _hits:
                st.caption(f"No JetBlue aircraft matching {_q} in the last "
                           "sweep (airborne aircraft only; positions "
                           "refresh every 2 min).")
            else:
                for r in _hits[:5]:
                    _d = _b = None
                    if coords:
                        try:
                            _d = AS.distance_nm(coords[0], coords[1],
                                                r["lat"], r["lon"])
                            import math as _m
                            _dl = _m.radians(r["lon"] - coords[1])
                            _y = _m.sin(_dl) * _m.cos(_m.radians(r["lat"]))
                            _x = (_m.cos(_m.radians(coords[0]))
                                  * _m.sin(_m.radians(r["lat"]))
                                  - _m.sin(_m.radians(coords[0]))
                                  * _m.cos(_m.radians(r["lat"])) * _m.cos(_dl))
                            _b = (_m.degrees(_m.atan2(_y, _x)) + 360) % 360
                        except Exception:
                            _d = _b = None
                    _where = (f"{_d:.0f} nm on the {_b:03.0f}\u00b0 from {icao}"
                              if _d is not None else "")
                    _on = (" \u2014 on the scope" if _d is not None and _d <= 20
                           else " \u2014 outside the 20 nm scope"
                           if _d is not None else "")
                    st.markdown(
                        f'<div style="border:1px solid #00E5FF;padding:6px 10px;'
                        f'margin-bottom:6px;color:{INK};font-size:12px;'
                        'font-weight:700;font-family:DejaVu Sans Mono,monospace">'
                        f'{r.get("callsign","")}'
                        + (f' &rarr; {r["dest"]}' if r.get("dest") else "")
                        + f' &middot; FL{int((r.get("alt") or 0) / 100):03d}'
                        + (f' &middot; {int(r["gs"])} kt' if r.get("gs") else "")
                        + (f' &middot; hdg {int(r["angle"]):03d}'
                           if r.get("angle") is not None else "")
                        + (f'<br>{_where}{_on}' if _where else "")
                        + "</div>", unsafe_allow_html=True)

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
                # No figure title: the pod header carries it, and the
                # title sat on top of the "Wind speed (kt)" panel title.
                title=None,
                height=PLOT_H, width=None, autosize=True, paper_bgcolor=PANEL,
                plot_bgcolor="#05070B",
                font=dict(color=INK2, size=11, family="Roboto, Arial"),
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
# JetBlue board: the last 3 h from BlueMet's own movement sampler,
# the next 3 h from what is actually inbound right now.
with st.container(border=True):
    pod_title("JetBlue arrivals & departures",
              "past 3 h derived \u00b7 inbound now from live ADS-B")
    _movements, _since = cached_movements(icao, 3, now.strftime("%Y%m%d%H%M")[:11])
    _arr = [m for m in _movements if m["kind"] == "ARR"]
    _dep = [m for m in _movements if m["kind"] == "DEP"]
    _inbound = _inbound_now(icao, coords, now) if coords else []
    if not _SAMPLER_OK:
        st.caption(f"Movement sampler unavailable ({_SAMPLER_ERR})")
    elif not is_sampled(icao):
        st.caption(f"{icao} is not a JetBlue destination; the sampler "
                   "does not cover it.")
    ca, cd = st.columns(2)
    with ca:
        st.markdown(_board_table(_arr, "Arrived", inbound=_inbound),
                    unsafe_allow_html=True)
    with cd:
        st.markdown(_board_table(_dep, "Departed"), unsafe_allow_html=True)


