"""Station Quick View — one-airport operational dashboard.

Sections: current METAR, current TAF, live radar (Level III loop OR raw
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

# BlueMet's own JBU movement log (OpenSky is unreachable from this
# server): background sampler polls hub airports via adsb.lol.
# Defensive import: a sampler problem must never kill the page.
try:
    from core.mov_sampler import (
        is_sampled, ensure_sampler_started, derive_movements,
        sampling_since,
    )
    ensure_sampler_started(CACHE_ROOT)
    _SAMPLER_OK = True
    from core.radar_warm import (
        ensure_radar_warmer_started, warm_get_loop, stamp_aircraft,
    )
    ensure_radar_warmer_started(CACHE_ROOT)
    _RADAR_WARM_OK = True
    _SAMPLER_ERR = ""
except Exception as _se:
    _SAMPLER_OK = False
    _SAMPLER_ERR = f"{type(_se).__name__}: {_se}"
    def is_sampled(_i):
        return False
    _RADAR_WARM_OK = False

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
    "KPBI": "KAMX", "KDJT": "KAMX", "KFLL": "KAMX", "KMIA": "KAMX",
    "KEYW": "KBYX",
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
    """Last ~45 min of Level II reflectivity as 1-minute sub-frames:
    radar advances per scan (~5 min), aircraft advance per minute via
    interpolated self-recorded snapshots. Returns (frames, gif, mode)."""
    from core.radar import fetch_and_render_base_frames, composite_aircraft

    coords = cached_station_coords(icao)
    if coords is None:
        raise ValueError(f"Cannot resolve coordinates for {icao}.")
    lat, lon = coords

    site = radar_site
    if len(site) == 4 and site.startswith("K"):
        site = site[1:]

    start = datetime.now(timezone.utc) - timedelta(minutes=45)
    base = fetch_and_render_base_frames(
        start_time=start,
        duration_min=45,
        center_lat=lat,
        center_lon=lon,
        label=icao,
        station=site,
        zoom_deg=zoom_deg,
    )
    if not base:
        return [], b"", "no radar volumes"

    overlay_mode = "off"
    frames: list[tuple[bytes, str]] = []

    if not overlay_flights:
        frames = [(b["png"], b["name"]) for b in base]
    else:
        try:
            from core.flights import (
                fetch_positions_near,
                record_snapshot,
                interpolate_at,
                positions_at_time,
            )
            hist_dir = CACHE_ROOT / "flights"
            current = fetch_positions_near(lat, lon, radius_deg=zoom_deg)
            record_snapshot(hist_dir, icao, current)

            # Sub-frame timeline: every minute from first scan to now
            t0 = base[0]["scan_time"].timestamp()
            t1 = datetime.now(timezone.utc).timestamp()
            n_interp = 0
            t = t0
            while t <= t1:
                # radar frame: latest scan at or before t
                b = base[0]
                for cand in base:
                    if cand["scan_time"].timestamp() <= t:
                        b = cand
                    else:
                        break
                planes = interpolate_at(hist_dir, icao, t, tolerance_s=240)
                if planes is None:
                    planes = current
                else:
                    n_interp += 1
                png = composite_aircraft(b["png"], b["geo"], b["px"], planes)
                tstr = datetime.fromtimestamp(
                    t, tz=timezone.utc
                ).strftime("%H:%MZ")
                frames.append((png, f"{tstr} \u00b7 {b['name']}"))
                t += 60
            n_total = len(frames)
            overlay_mode = (
                f"interpolated history ({n_interp}/{n_total} sub-frames "
                f"from snapshots, rest use current; {len(current)} JBU "
                "live). Fills in with continued use."
            )
        except Exception as e:
            frames = [(b["png"], b["name"]) for b in base]
            overlay_mode = f"failed ({type(e).__name__}: {e})"

    # Faster flip for the denser timeline
    gif = (
        _frames_to_gif(frames, frame_ms=160, last_hold_ms=1200)
        if len(frames) > 1 else b""
    )
    return frames, gif, overlay_mode


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


@st.cache_data(ttl=90, show_spinner=False, max_entries=12)
def cached_live_inbound(icao: str, lat: float, lon: float,
                        bucket: str):
    """JBU aircraft near the field RIGHT NOW, classified by heading
    geometry: tracking toward the airport = inbound. Instant (live
    ADS-B), unlike the sampled history which accrues over time."""
    import math
    from core.flights import fetch_positions_near
    from core.nexrad_sites import _haversine_km

    try:
        planes = fetch_positions_near(lat, lon, radius_deg=0.9)
    except Exception:
        return []

    def bearing_to(alat, alon):
        p1, p2 = math.radians(alat), math.radians(lat)
        dl = math.radians(lon - alon)
        x = math.sin(dl) * math.cos(p2)
        y = (math.cos(p1) * math.sin(p2)
             - math.sin(p1) * math.cos(p2) * math.cos(dl))
        return (math.degrees(math.atan2(x, y)) + 360) % 360

    out = []
    for p in planes:
        if p.alt_ft is not None and p.alt_ft > 18000:
            continue
        dist = _haversine_km(p.lat, p.lon, lat, lon)
        if p.heading_deg is not None:
            diff = abs((bearing_to(p.lat, p.lon)
                        - p.heading_deg + 180) % 360 - 180)
            status = "Inbound" if diff <= 60 else "Outbound"
        else:
            status = "In area"
        out.append({
            "cs": p.callsign, "alt": p.alt_ft,
            "dist_km": dist, "status": status,
        })
    out.sort(key=lambda r: r["dist_km"])
    return out


def _sampler_obs_today(icao: str) -> int:
    """Raw observation count in today's log file (daemon liveness)."""
    from datetime import datetime as _dt, timezone as _tz
    code = "KDJT" if icao.upper() == "KPBI" else icao.upper()
    f = (CACHE_ROOT / "movements" / "obs" / code
         / f"{_dt.now(_tz.utc):%Y%m%d}.jsonl")
    try:
        return sum(1 for _ in open(f))
    except OSError:
        return 0


@st.cache_data(ttl=120, show_spinner=False, max_entries=12)
def cached_jbu_movements(icao: str, hours_back: int, bucket: str):
    """Movements from BlueMet's own sampler log."""
    if not _SAMPLER_OK:
        return [], None
    try:
        rows = derive_movements(CACHE_ROOT, icao, hours_back)
        since = sampling_since(CACHE_ROOT, icao)
        return rows, since
    except Exception:
        return [], None


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


# --- Token-level METAR/TAF hazard coloring ---
# All text renders white; ONLY the specific token that meets a
# criterion is colored:
#   visibility  <1 SM magenta, <3 SM red
#   ceiling     BKN/OVC/VV <500 magenta, <1000 red, <2000 yellow
#   wind gust   G30+ orange, G35+ red, G40+ magenta (whole wind token)
#   TS weather  red (TSRA, +TSRA, VCTS, ...)
_MAGENTA, _RED, _YELLOW, _ORANGE = (
    "#FF00FF", "#FF4040", "#FFFF00", "#FF9900",
)


def _span(token: str, color: str) -> str:
    return (f'<span style="color:{color};'
            f'-webkit-text-fill-color:{color};font-weight:bold;">'
            f"{token}</span>")


def _vis_value(tok: str):
    t = tok[1:] if tok.startswith("M") else tok
    t = t[:-2]  # strip SM
    try:
        if " " in t:
            whole, frac = t.split()
            num, den = frac.split("/")
            v = float(whole) + float(num) / float(den)
        elif "/" in t:
            num, den = t.split("/")
            v = float(num) / float(den)
        else:
            v = float(t)
    except (ValueError, ZeroDivisionError):
        return None
    if tok.startswith("M"):
        v = max(v - 0.01, 0.0)
    return v


def _colorize_line(line: str) -> str:
    """Escape a METAR/TAF line, then wrap qualifying tokens in
    colored spans. Everything else stays white via the box style."""
    import re
    from html import escape

    s = escape(line.rstrip())

    def vis_sub(m):
        tok = m.group(1)
        if tok == "P6SM":
            return tok
        v = _vis_value(tok)
        if v is None:
            return tok
        if v < 1:
            return _span(tok, _MAGENTA)
        if v < 3:
            return _span(tok, _RED)
        return tok
    s = re.sub(
        r"(?<![A-Z0-9/])(P6SM|M?\d+\s+\d/\dSM|M?\d+/\d+SM|"
        r"M?\d+SM)(?![A-Z0-9])",
        vis_sub, s,
    )

    def cig_sub(m):
        tok = m.group(0)
        ft = int(m.group(2)) * 100
        if ft < 500:
            return _span(tok, _MAGENTA)
        if ft < 1000:
            return _span(tok, _RED)
        if ft < 2000:
            return _span(tok, _YELLOW)
        return tok
    s = re.sub(r"(BKN|OVC|VV)(\d{3})(CB|TCU)?", cig_sub, s)

    def wind_sub(m):
        tok = m.group(0)
        g = int(m.group(1))
        if g >= 40:
            return _span(tok, _MAGENTA)
        if g >= 35:
            return _span(tok, _RED)
        if g >= 30:
            return _span(tok, _ORANGE)
        return tok
    s = re.sub(r"(?:\d{3}|VRB)\d{2,3}G(\d{2,3})KT", wind_sub, s)

    s = re.sub(
        r"(?<![A-Z])([+-]?(?:VC)?TS[A-Z]*)",
        lambda m: _span(m.group(1), _RED),
        s,
    )
    return s


def wx_colored_box(lines: list, taf_mode: bool = False) -> str:
    """Retro box (green border, black background, white text) with
    token-level hazard coloring."""
    body = "\n".join(_colorize_line(ln) for ln in lines)
    return (
        '<div style="background:#000000;border:2px solid #00FF00;'
        'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
        'font-family:Courier New,monospace;font-size:12px;'
        'padding:8px 10px;white-space:pre-wrap;word-break:break-word;">'
        f"{body}</div>"
    )


_WX_LEGEND = (
    "colored values only: vis <1SM magenta / <3SM red | "
    "cig <500 magenta / <1000 red / <2000 yellow | "
    "gusts G30+ orange / G35+ red / G40+ magenta | TS red"
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


@st.cache_data(ttl=25, show_spinner=False, max_entries=12)
def cached_l3_planes(clat: float, clon: float, zoom: float,
                     bucket: str):
    from core.flights import fetch_positions_near
    try:
        return fetch_positions_near(clat, clon, radius_deg=zoom)
    except Exception:
        return []


@st.cache_data(ttl=300, show_spinner=False, max_entries=12)
def cached_l3_station_loop(
    product: str, site: str, clat: float, clon: float, zoom: float,
    bucket: str, n: int = 6,
):
    """Recent Level III frames (REF or ET) centered on the station,
    rendered AIRCRAFT-FREE with pixel geometry. Live JBU triangles
    are stamped at display time (milliseconds), so positions update
    without re-rendering radar - same architecture as the warm store.
    Returns [(png, name, geom), ...] oldest first."""
    from core.radar3 import fetch_recent, parse_l3, render_l3
    frames = []
    for raw, name in fetch_recent(product, site, n=n):
        try:
            parsed = parse_l3(raw)
            png, geom = render_l3(
                parsed, product, clat, clon, zoom, site,
                title_note=name, return_geometry=True,
            )
            frames.append((png, name, geom))
        except Exception:
            continue
    return frames


def _stamp_all(frames_g, planes):
    """[(png, name, geom)] + live planes -> [(stamped_png, name)]."""
    try:
        from core.radar_warm import stamp_aircraft
    except Exception:
        return [(p, n) for p, n, _g in frames_g]
    return [(stamp_aircraft(p, g, planes), n) for p, n, g in frames_g]


def _embed_html(html: str, height: int) -> None:
    fn = getattr(st, "iframe", None)
    if fn is not None:
        try:
            fn(html, height=height)
            return
        except TypeError:
            pass
    st.components.v1.html(html, height=height)


def _client_scrubber(frames, key: str) -> str:
    """Instant client-side frame scrubber with play/pause plus
    wheel-zoom (toward cursor), drag-pan, and double-click reset.
    All pure browser JS on the already-shipped frames."""
    import base64
    import json as _json

    srcs = ["data:image/png;base64," + base64.b64encode(p).decode()
            for p, _n in frames]
    names = [n for _p, n in frames]
    n = len(srcs)
    return (
        "<style>"
        ".scr{font:13px monospace}"
        ".scr .vp{overflow:hidden;border:1px solid #888;"
        "cursor:zoom-in;position:relative}"
        ".scr .vp.z{cursor:grab}"
        ".scr .vp.drag{cursor:grabbing}"
        ".scr img{width:100%;display:block;"
        "transform-origin:0 0;user-select:none;"
        "-webkit-user-drag:none}"
        ".scr input[type=range]{width:55%;vertical-align:middle}"
        ".scr button{font:bold 13px monospace;margin-right:6px;"
        "padding:2px 10px}"
        ".scr .zl{position:absolute;right:4px;top:4px;"
        "background:#000a;color:#0f0;padding:1px 6px;"
        "font:11px monospace;display:none}"
        "</style>"
        "<div class='scr'>"
        "<div class='vp' id='vp_" + key + "'>"
        "<img id='im_" + key + "'>"
        "<span class='zl' id='zl_" + key + "'></span>"
        "</div>"
        "<div>"
        "<button id='pb_" + key + "'>PAUSE</button>"
        "<input type='range' id='sl_" + key + "' min='0' max='"
        + str(n - 1) + "' value='" + str(n - 1) + "' step='1'>"
        " <span id='lb_" + key + "'></span>"
        "</div></div>"
        "<script>"
        "(function(){"
        "const F=" + _json.dumps(srcs) + ";"
        "const N=" + _json.dumps(names) + ";"
        "const im=document.getElementById('im_" + key + "');"
        "const vp=document.getElementById('vp_" + key + "');"
        "const zl=document.getElementById('zl_" + key + "');"
        "const sl=document.getElementById('sl_" + key + "');"
        "const lb=document.getElementById('lb_" + key + "');"
        "const pb=document.getElementById('pb_" + key + "');"
        "let playing=true;let t=null;"
        "let s=1,tx=0,ty=0;"
        "function apply(){"
        "im.style.transform='translate('+tx+'px,'+ty+'px) "
        "scale('+s+')';"
        "vp.classList.toggle('z',s>1);"
        "zl.style.display=s>1?'block':'none';"
        "zl.textContent=s.toFixed(1)+'x';}"
        "function clamp(){"
        "const w=vp.clientWidth,h=vp.clientHeight;"
        "tx=Math.min(0,Math.max(tx,w-w*s));"
        "ty=Math.min(0,Math.max(ty,h-h*s));}"
        "vp.addEventListener('wheel',function(e){"
        "e.preventDefault();"
        "const r=vp.getBoundingClientRect();"
        "const mx=e.clientX-r.left,my=e.clientY-r.top;"
        "const s0=s;"
        "s=Math.min(6,Math.max(1,s*(e.deltaY<0?1.2:1/1.2)));"
        "tx=mx-(mx-tx)*(s/s0);ty=my-(my-ty)*(s/s0);"
        "if(s===1){tx=0;ty=0;}"
        "clamp();apply();},{passive:false});"
        "let dragging=false,dx=0,dy=0;"
        "vp.addEventListener('mousedown',function(e){"
        "if(s<=1)return;dragging=true;"
        "vp.classList.add('drag');"
        "dx=e.clientX-tx;dy=e.clientY-ty;e.preventDefault();});"
        "window.addEventListener('mousemove',function(e){"
        "if(!dragging)return;"
        "tx=e.clientX-dx;ty=e.clientY-dy;clamp();apply();});"
        "window.addEventListener('mouseup',function(){"
        "dragging=false;vp.classList.remove('drag');});"
        "vp.addEventListener('dblclick',function(){"
        "s=1;tx=0;ty=0;apply();});"
        "function show(i){im.src=F[i];lb.textContent=N[i];}"
        "function step(){let i=(+sl.value+1)%F.length;"
        "sl.value=i;show(i);"
        "t=setTimeout(step,i==F.length-1?1400:450);}"
        "sl.addEventListener('input',function(){"
        "clearTimeout(t);playing=false;pb.textContent='PLAY';"
        "show(+sl.value);});"
        "pb.addEventListener('click',function(){"
        "if(playing){clearTimeout(t);playing=false;"
        "pb.textContent='PLAY';}"
        "else{playing=true;pb.textContent='PAUSE';step();}});"
        "show(+sl.value);t=setTimeout(step,450);apply();"
        "})();"
        "</script>"
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
        options=[
            "Level III loop (fast, high res)",
            "Raw Level II (slower, full res)",
        ],
        index=0,
    )
    l3_zoom = st.slider(
        "Level III zoom (degrees)", 0.5, 3.0, 1.5, 0.5,
        disabled=not radar_mode.startswith("Level III"),
    )
    l3_auto = st.checkbox(
        "Auto-refresh aircraft (60s)",
        value=True,
        key="l3_auto",
        help="Re-stamps current JBU positions onto the radar loops "
             "every minute without re-rendering radar. Resets "
             "zoom/pause state on each tick.",
    )
    l3_flights = st.checkbox(
        "Overlay live JBU flights",
        value=True,
        disabled=not radar_mode.startswith("Level III"),
        key="l3_flights",
        help="Current JetBlue positions (community ADS-B) drawn on "
             "every loop frame. Positions are as-of-now even on "
             "older frames.",
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

    # --- Radar ---
    st.subheader("Live Radar")
    radar_site = radar_override or RADAR_FOR_AIRPORT.get(icao, "")
    if not radar_site:
        st.warning(
            f"No radar mapping for {icao}. Enter a NEXRAD site "
            "(e.g. KOKX) in the sidebar."
        )
    else:
        # Two-panel radar deck inside a live fragment: radar frames
        # are cached aircraft-free; JBU triangles re-stamp on a
        # timer, so positions stay current on a static loop.
        _deck_kwargs = dict(
            icao=icao, radar_site=radar_site,
            radar_mode=radar_mode, l3_zoom=l3_zoom,
            l3_flights=l3_flights, l2_zoom=l2_zoom,
            l2_flights=l2_flights,
        )

        @st.fragment(run_every=60 if l3_auto else None)
        def _radar_deck(icao, radar_site, radar_mode, l3_zoom,
                        l3_flights, l2_zoom, l2_flights):
            now_f = datetime.now(timezone.utc)
            coords = cached_station_coords(icao)
            if coords is None:
                st.warning(f"Cannot resolve coordinates for {icao}.")
                return
            s_lat, s_lon = coords
            bucket5 = (now_f.strftime("%Y%m%d%H")
                       + str(now_f.minute // 5))
            plane_bucket = str(int(now_f.timestamp()) // 30)
            l3_planes = []
            if l3_flights:
                l3_planes = cached_l3_planes(
                    round(s_lat, 2), round(s_lon, 2), l3_zoom,
                    plane_bucket,
                )

            col_ref, col_et = st.columns(2)

            with col_ref:
                st.markdown("**Reflectivity**")
                if radar_mode.startswith("Level III"):
                    ref_g = None
                    ref_warm = False
                    if _RADAR_WARM_OK:
                        ref_g = warm_get_loop(
                            CACHE_ROOT, icao, "REF", l3_zoom
                        )
                        ref_warm = bool(ref_g)
                    if not ref_g:
                        with st.spinner(
                            "Rendering Level III loop..."
                        ):
                            try:
                                ref_g = cached_l3_station_loop(
                                    "REF", radar_site, s_lat,
                                    s_lon, l3_zoom, bucket5,
                                )
                            except Exception as e:
                                ref_g = []
                                st.warning(
                                    f"Level III loop failed: {e}"
                                )
                    ref_frames = _stamp_all(ref_g or [], l3_planes)
                    if len(ref_frames) > 1:
                        _embed_html(
                            _client_scrubber(ref_frames, key="qvl3"),
                            height=560,
                        )
                        st.caption(
                            f"L3 reflectivity, {radar_site}, frames "
                            f"~5 min apart"
                            + (f" | {len(l3_planes)} JBU as of "
                               f"{now_f:%H:%M:%S}Z"
                               if l3_planes else "")
                            + (" | prewarmed" if ref_warm else "")
                        )
                    elif ref_frames:
                        st.image(ref_frames[0][0],
                                 use_container_width=True)
                    else:
                        st.caption("No Level III frames returned.")
                else:
                    # Raw Level II mode: planes baked at render
                    # time (its pipeline differs), so no live
                    # re-stamping here.
                    bucket = bucket5
                    if "live_l2" not in st.session_state:
                        with st.spinner(
                            "Fetching raw Level II volumes (30-60s "
                            "first time)..."
                        ):
                            try:
                                frames, gif, ov_mode = cached_live_l2(
                                    icao, radar_site, l2_zoom,
                                    bucket,
                                    overlay_flights=l2_flights,
                                )
                                st.session_state["live_l2"] = frames
                                st.session_state["live_l2_gif"] = gif
                                st.session_state["live_l2_mode"] = \
                                    ov_mode
                            except Exception as e:
                                st.session_state["live_l2"] = []
                                st.session_state["live_l2_gif"] = b""
                                st.warning(
                                    f"Level II fetch failed: {e}"
                                )
                    frames = st.session_state.get("live_l2", [])
                    gif = st.session_state.get("live_l2_gif", b"")
                    if frames:
                        st.caption(
                            f"{len(frames)} raw L2 volumes from "
                            f"{radar_site} (last ~45 min). Flight "
                            f"overlay: "
                            f"{st.session_state.get('live_l2_mode', '?')}"
                            f" (baked at render)."
                        )
                        if len(frames) > 1:
                            _embed_html(
                                _client_scrubber(frames, key="qvl2"),
                                height=560,
                            )
                        else:
                            st.image(frames[-1][0],
                                     use_container_width=True)
                        if gif:
                            st.download_button(
                                "Download loop GIF", data=gif,
                                file_name=f"l2_loop_{radar_site}.gif",
                                mime="image/gif", key="dl_l2_gif",
                            )
                    else:
                        st.caption(
                            "No Level II volumes returned for the "
                            "window."
                        )

            with col_et:
                st.markdown("**Echo Tops (L3)**")
                et_g = None
                et_warm = False
                if _RADAR_WARM_OK:
                    et_g = warm_get_loop(
                        CACHE_ROOT, icao, "ET", l3_zoom
                    )
                    et_warm = bool(et_g)
                if not et_g:
                    with st.spinner("Rendering echo tops loop..."):
                        try:
                            et_g = cached_l3_station_loop(
                                "ET", radar_site, s_lat, s_lon,
                                l3_zoom, bucket5,
                            )
                        except Exception as e:
                            et_g = []
                            st.warning(
                                f"Echo tops loop failed: {e}"
                            )
                et_frames = _stamp_all(et_g or [], l3_planes)
                if len(et_frames) > 1:
                    _embed_html(
                        _client_scrubber(et_frames, key="qvet"),
                        height=560,
                    )
                    st.caption(
                        f"L3 echo tops (kft), {radar_site}"
                        + (f" | {len(l3_planes)} JBU as of "
                           f"{now_f:%H:%M:%S}Z"
                           if l3_planes else "")
                        + (" | prewarmed" if et_warm else "")
                    )
                elif et_frames:
                    st.image(et_frames[0][0],
                             use_container_width=True)
                else:
                    st.caption("No echo tops frames returned.")

        _radar_deck(**_deck_kwargs)

    # --- METAR ---
    st.subheader("Current METAR" if n_metars == 1 else
                 f"METARs (last {n_metars})")
    # Hourly obs + specials: n+4 hours of lookback comfortably covers n obs.
    with st.spinner("Fetching METARs..."):
        obs_list = cached_metar_history(icao, hours_back=n_metars + 4)
    if obs_list:
        recent = obs_list[-n_metars:][::-1]  # newest first
        st.markdown(
            wx_colored_box([o.raw_text for o in recent]),
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
        st.markdown(
            wx_colored_box(taf_text.splitlines(), taf_mode=True),
            unsafe_allow_html=True,
        )
        st.caption(_WX_LEGEND)
    else:
        st.warning("No TAF available (station may not be a TAF site).")

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

    # --- Live inbound (instant, from ADS-B positions) ---
    st.subheader("JBU Inbound Now")

    @st.fragment(run_every=30)
    def _inbound_live(icao):
        now = datetime.now(timezone.utc)
        _inbound_body(icao, now)

    def _inbound_body(icao, now):
        _mv_coords = cached_station_coords(icao)
        if _mv_coords is None:
            live = []
            st.caption(f"Cannot resolve coordinates for {icao}.")
        else:
            live = cached_live_inbound(
                icao, _mv_coords[0], _mv_coords[1],
                now.strftime("%Y%m%d%H%M")[:11],
            )
        inbound = [r for r in live if r["status"] == "Inbound"]
        if inbound:
            import pandas as _pd2
            df_in = _pd2.DataFrame([{
                "Flight": (f"B6 {r['cs'][3:]}" if r["cs"].startswith("JBU")
                           else r["cs"]),
                "Callsign": r["cs"],
                "Altitude": (f"{int(r['alt']):,} ft"
                             if r["alt"] is not None else "?"),
                "Distance": f"{r['dist_km']:.0f} km",
            } for r in inbound[:8]])
            st.dataframe(df_in, use_container_width=True, hide_index=True)
            st.caption(
                f"{len(inbound)} JBU inbound within ~100 km (live ADS-B, "
                f"heading toward the field, below 18,000 ft)"
            )
        else:
            n_other = len(live) - len(inbound)
            st.caption(
                "No JBU currently inbound within ~100 km"
                + (f" ({n_other} JBU in area, outbound or heading "
                   f"unknown)" if n_other else "")
                + "."
            )


    _inbound_live(icao)

    # --- Sampled arrivals/departures history ---
    st.subheader("JBU Arrivals & Departures")
    mv_hours = st.selectbox(
        "Window", [3, 6, 12, 24], index=1,
        format_func=lambda h: f"Last {h} hours",
        key="mv_hours",
    )
    movements, since_ts = cached_jbu_movements(
        icao, mv_hours, now.strftime("%Y%m%d%H%M")[:11]
    )
    sampled_here = is_sampled(icao)
    if not _SAMPLER_OK:
        st.caption(f"Movement sampler unavailable ({_SAMPLER_ERR})")
    elif not sampled_here:
        st.caption(
            f"{icao} is not a JBU destination, so the movement "
            f"sampler does not cover it (the whole JBU network is "
            f"sampled)."
        )
    elif not movements:
        if since_ts:
            from datetime import datetime as _dt, timezone as _tz
            since = _dt.fromtimestamp(since_ts, _tz.utc)
            st.caption(
                f"No JBU movements derived in the last {mv_hours}h. "
                f"(BlueMet has been sampling {icao} since "
                f"{since:%m/%d %H:%M}Z.)"
            )
        else:
            n_obs = _sampler_obs_today(icao)
            if n_obs:
                st.caption(
                    f"Sampler is recording ({n_obs} observations "
                    f"today) but no complete arrival/departure "
                    f"pattern derived yet - movements appear after "
                    f"an aircraft is seen across several polls."
                )
            else:
                st.caption(
                    "Sampler has no observations yet - it records "
                    "from deploy time forward (polls every 2 min). "
                    "If this persists 10+ min during active JBU "
                    "traffic, tell Claude - the daemon may not be "
                    "running."
                )
    else:
        import pandas as _pd
        from datetime import datetime as _dt, timezone as _tz
        def _flight_no(cs: str) -> str:
            """JBU1234 -> 'B6 1234' (the exact flight number)."""
            if cs.startswith("JBU") and cs[3:]:
                return f"B6 {cs[3:]}"
            return cs

        def _mv_df(rows):
            return _pd.DataFrame([{
                "Flight": _flight_no(m_["callsign"]),
                "Callsign": m_["callsign"],
                "Time (Z)": _dt.fromtimestamp(
                    m_["time_unix"], _tz.utc
                ).strftime("%m/%d %H:%M"),
                "Alt band": f"{m_['alt_from']}-{m_['alt_to']} ft",
            } for m_ in rows])

        arrivals = [m_ for m_ in movements if m_["kind"] == "ARR"]
        departures = [m_ for m_ in movements if m_["kind"] == "DEP"]
        st.caption(
            f"{len(arrivals)} arrivals, {len(departures)} departures "
            f"in {mv_hours}h (showing most recent 5 of each) - "
            f"BlueMet's own terminal-area sampling, fresh to ~2-4 min"
        )
        col_a, col_d = st.columns(2)
        with col_a:
            st.markdown("**Arrivals**")
            if arrivals:
                st.dataframe(_mv_df(arrivals[:5]),
                             use_container_width=True,
                             hide_index=True)
            else:
                st.caption("None derived in window.")
        with col_d:
            st.markdown("**Departures**")
            if departures:
                st.dataframe(_mv_df(departures[:5]),
                             use_container_width=True,
                             hide_index=True)
            else:
                st.caption("None derived in window.")

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
