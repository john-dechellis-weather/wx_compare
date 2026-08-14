"""Airport Alerts — flags stations whose TAFs forecast VIS/CIG/TSRA
below thresholds within a user-selected time window.

Uses AWC's API + avwx-engine (see core/taf.py) for TAF parsing.

Tables are hand-built HTML with inline styles (terminal green-on-black,
matching the MOS Tables page) because st.dataframe ignores page CSS.
Critical severity — vis < 1 sm or ceiling < 400 ft — highlights the
ICAO and value cells in red.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import streamlit as st

st.set_page_config(
    page_title="BlueMet — Airport Alerts",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


# ---------------------------------------------------------------------------
# JetBlue destinations — static list
# ---------------------------------------------------------------------------
JETBLUE_ICAOS = [
    # CONUS only (page scope per 8/13): international, Caribbean,
    # PR/USVI, and Canada removed from boards and map alike.
    "KJFK", "KEWR", "KLGA", "KHPN", "KISP", "KPHL", "KBOS", "KORH", "KBDL",
    "KPVD", "KPWM", "KPQI", "KACK", "KHYA", "KMVY", "KALB", "KSYR", "KROC",
    "KBUF", "KPIT", "KDCA", "KBWI", "KRIC", "KORF", "KILM", "KRDU", "KCLT",
    "KCHS", "KSAV", "KJAX", "KVPS", "KVRB", "KMCO", "KDAB", "KTPA", "KSRQ",
    "KRSW", "KDJT", "KFLL", "KEYW", "KORD", "KMKE", "KTVC", "KDTW", "KCLE",
    "KBNA", "KATL", "KMSY", "KDFW", "KAUS", "KIAH", "KABQ", "KPHX", "KBUR",
    "KLAX", "KSAN", "KONT", "KLAS", "KSFO", "KRNO", "KSMF", "KSLC", "KBZN",
    "KDEN", "KHDN", "KSEA", "KPDX", "KCMH", "KIND",
]

# Critical-severity thresholds (fixed): red highlight in tables
CRITICAL_VIS_SM = 1.0
CRITICAL_CIG_FT = 400


# ---------------------------------------------------------------------------
# Terminal-style HTML table builders (st.dataframe ignores page CSS)
# ---------------------------------------------------------------------------
_GREEN = "#00FF00"
_BLACK = "#000000"
_RED = "#CC0000"
_WHITE = "#FFFFFF"
_FONT = "'Courier New', Courier, monospace"


def _td(text, bg="#FFFFFF", fg="#000000", bold=False,
        align="left") -> str:
    weight = "bold" if bold else "normal"
    return (
        f'<td style="background-color:{bg}; color:{fg}; -webkit-text-fill-color:{fg}; '
        f"font-family:{_FONT}; font-size:11px; padding:3px 10px; "
        f"border:1px solid #000000; font-weight:{weight}; "
        f'text-align:{align}; white-space:nowrap;">{text}</td>'
    )


def _th(text, align="left") -> str:
    return (
        f'<td style="background-color:#FFFFFF; color:#000000; '
        f"-webkit-text-fill-color:#000000; "
        f"font-family:{_FONT}; font-size:11px; padding:4px 10px; "
        f"border:1px solid #000000; font-weight:bold; "
        f'text-align:{align}; text-decoration:underline; '
        f'white-space:nowrap;">{text}</td>'
    )


def _table(header_cells: list[str], body_rows: list[str],
           full_width: bool = True) -> str:
    w = "width:100%;" if full_width else "width:auto;"
    return (
        f'<table style="border-collapse:collapse; background-color:#FFFFFF; '
        f'border:2px solid {_WHITE}; {w}">'
        f"<tr>{''.join(header_cells)}</tr>"
        f"{''.join(body_rows)}"
        f"</table>"
    )


def _no_alerts() -> str:
    return (
        f'<div style="background-color:#FFFFFF; border:2px solid {_WHITE}; '
        f"color:#000000; -webkit-text-fill-color:#000000; font-family:{_FONT}; font-size:11px; "
        f'padding:6px 10px;">NO AIRPORTS FLAGGED</div>'
    )


def _fmt_vis(v: float) -> str:
    """0.5 -> '0.5', 2.0 -> '2', 1.75 -> '1.75'."""
    return f"{v:g}"


def render_vis_table(alerts) -> str:
    header = [_th("ICAO"), _th("MIN VIS (SM)", align="right"), _th("WORST PERIOD")]
    rows = []
    for a in alerts:
        critical = a.min_vis_sm < CRITICAL_VIS_SM
        bg = _RED if critical else _BLACK
        fg = _WHITE
        rows.append(
            "<tr>"
            + _td(a.icao, bg=bg, fg=fg, bold=critical)
            + _td(_fmt_vis(a.min_vis_sm), bg=bg, fg=fg, bold=critical, align="right")
            + _td(a.worst_period_label)
            + "</tr>"
        )
    return _table(header, rows)


def render_ceiling_table(alerts) -> str:
    header = [_th("ICAO"), _th("MIN CIG (FT)", align="right"), _th("WORST PERIOD")]
    rows = []
    for a in alerts:
        critical = a.min_ceiling_ft < CRITICAL_CIG_FT
        bg = _RED if critical else _BLACK
        fg = _WHITE
        rows.append(
            "<tr>"
            + _td(a.icao, bg=bg, fg=fg, bold=critical)
            + _td(str(a.min_ceiling_ft), bg=bg, fg=fg, bold=critical, align="right")
            + _td(a.worst_period_label)
            + "</tr>"
        )
    return _table(header, rows)


def render_wind_table(alerts) -> str:
    header = [_th("ICAO"), _th("WIND (KT)", align="right"), _th("WORST PERIOD")]
    rows = []
    for a in alerts:
        rows.append(
            "<tr>"
            + _td(a.icao, bold=True)
            + _td(a.wind_str, bold=True, align="right")
            + _td(a.worst_period_label)
            + "</tr>"
        )
    return _table(header, rows)


def render_tsra_table(alerts) -> str:
    header = [_th("ICAO"), _th("CODE"), _th("PERIOD")]
    rows = []
    for a in alerts:
        rows.append(
            "<tr>"
            + _td(a.icao)
            + _td(a.weather_code)
            + _td(a.period_label)
            + "</tr>"
        )
    return _table(header, rows)


def _fmt_vis_obs(v) -> str:
    if v is None:
        return "-"
    return f"{v:g}"


def _fmt_cig_obs(c, unl) -> str:
    if unl or c is None:
        return "UNL"
    return f"{int(round(c / 100)):03d}"


def _fmt_wind_obs(spd, gst) -> str:
    if spd is None:
        return "-"
    s = f"{int(spd):02d}"
    return f"{s}G{int(gst):02d}" if gst else s


def render_metar_table(rows) -> str:
    """rows: list of dicts with icao, obs_time, vis, cig, cig_unl, spd, gst,
    raw, and breach flags vis_bad/cig_bad/wind_bad."""
    header = [_th("ICAO"), _th("TIME"), _th("VIS", align="right"),
              _th("CIG", align="right"), _th("WIND", align="right"),
              _th("RAW METAR")]
    body = []
    for r in rows:
        def cell(text, bad, align="right"):
            if bad:
                return _td(text, bg=_RED, fg=_WHITE, bold=True, align=align)
            return _td(text, align=align)
        body.append(
            "<tr>"
            + _td(r["icao"], bold=True)
            + _td(r["obs_time"].strftime("%H:%MZ"))
            + cell(_fmt_vis_obs(r["vis"]), r["vis_bad"])
            + cell(_fmt_cig_obs(r["cig"], r["cig_unl"]), r["cig_bad"])
            + cell(_fmt_wind_obs(r["spd"], r["gst"]), r["wind_bad"])
            + _td(r["raw"])
            + "</tr>"
        )
    return _table(header, body)


@st.cache_data(ttl=300, show_spinner=False)
def cached_current_metars(
    icaos_tuple: tuple[str, ...],
    vis_threshold_sm: float,
    ceiling_threshold_ft: int,
    wind_threshold_kt: int,
):
    """Latest METAR per station; return rows breaching any threshold.
    5-minute cache — METARs are hourly with specials in between."""
    from core.metar import fetch_metars

    by_station = fetch_metars(list(icaos_tuple), hours_back=2)
    rows = []
    for icao, obs_list in by_station.items():
        if not obs_list:
            continue
        o = obs_list[-1]  # latest
        vis_bad = o.vsby_sm is not None and o.vsby_sm < vis_threshold_sm
        cig_bad = (not o.ceiling_unlimited and o.ceiling_ft is not None
                   and o.ceiling_ft < ceiling_threshold_ft)
        wind_max = max(
            [x for x in (o.wind_speed_kt, o.wind_gust_kt) if x is not None],
            default=None,
        )
        wind_bad = wind_max is not None and wind_max >= wind_threshold_kt
        if vis_bad or cig_bad or wind_bad:
            rows.append({
                "icao": icao,
                "obs_time": o.obs_time,
                "vis": o.vsby_sm, "vis_bad": vis_bad,
                "cig": o.ceiling_ft, "cig_unl": o.ceiling_unlimited,
                "cig_bad": cig_bad,
                "spd": o.wind_speed_kt, "gst": o.wind_gust_kt,
                "wind_bad": wind_bad,
                "raw": o.raw_text,
            })
    rows.sort(key=lambda r: r["icao"])
    return rows


# ---------------------------------------------------------------------------
# Airport status board: one row per alerting airport, worst-first
# ---------------------------------------------------------------------------


_MAGENTA = "#FF00FF"
_YELLOW = "#FFFF00"
_ORANGE = "#FF9900"
_LT_RED = "#FF9999"      # TSRA (plain) highlight; +TSRA gets _RED


def _wind_max_kt(wind_str: str):
    """Max of speed/gust from a TAF wind group like '29025G42KT'.
    Proper group parse - naive digit-matching would read 29025."""
    import re
    m = re.match(r"(?:VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?KT",
                 (wind_str or "").strip())
    if m:
        spd = int(m.group(1))
        gst = int(m.group(2)) if m.group(2) else 0
        return max(spd, gst)
    nums = [int(x) for x in re.findall(r"\d{2,3}", wind_str or "")]
    return max(nums) if nums else None


def build_status_board(results, metar_rows):
    """One aggregated row per alerting airport with severity rank
    (0 magenta / 1 red / 2 yellow-orange) and the driving-condition
    chip."""
    board: dict = {}

    def ent(icao):
        return board.setdefault(icao, {
            "vis": None, "vis_p": "", "ceil": None, "ceil_p": "",
            "ts": None, "ts_p": "", "wind": None, "wind_p": "",
            "raw": "", "obs": "",
        })

    for a in results.vis_alerts:
        e = ent(a.icao)
        e["vis"], e["vis_p"] = a.min_vis_sm, a.worst_period_label
    for a in results.ceiling_alerts:
        e = ent(a.icao)
        e["ceil"], e["ceil_p"] = (a.min_ceiling_ft,
                                  a.worst_period_label)
    for a in results.tsra_alerts:
        e = ent(a.icao)
        e["ts"], e["ts_p"] = a.weather_code, a.period_label
    for a in results.wind_alerts:
        e = ent(a.icao)
        e["wind"], e["wind_p"] = a.wind_str, a.worst_period_label
    for r in (metar_rows or []):
        if r["icao"] in board or True:
            e = ent(r["icao"])
            e["raw"] = r["raw"]
            e["obs"] = r["obs_time"].strftime("%H:%MZ")

    rows = []
    for icao, e in board.items():
        cands = []   # (rank, chip, color, period)
        if e["vis"] is not None:
            v = e["vis"]
            tier = 0 if v < 1 else (1 if v < 3 else 2)
            col = (_MAGENTA, _RED, _YELLOW)[tier]
            cands.append((tier, f"{v:g}SM", col, e["vis_p"]))
        if e["ceil"] is not None:
            c = e["ceil"]
            tier = 0 if c < 500 else (1 if c < 1000 else 2)
            col = (_MAGENTA, _RED, _YELLOW)[tier]
            cands.append((tier, f"CIG {c}", col, e["ceil_p"]))
        if e["ts"]:
            ts_col = _RED if e["ts"].startswith("+") else _LT_RED
            cands.append((1, e["ts"], ts_col, e["ts_p"]))
        if e["wind"]:
            w = _wind_max_kt(e["wind"])
            if w is not None and w >= 40:
                cands.append((0, f"G{w}", _MAGENTA, e["wind_p"]))
            elif w is not None and w >= 35:
                cands.append((1, f"G{w}", _RED, e["wind_p"]))
            else:
                wtxt = f"{w}KT" if w is not None else e["wind"]
                cands.append((2, wtxt, _ORANGE, e["wind_p"]))
        if not cands:
            continue
        cands.sort(key=lambda c: c[0])
        rank, chip, color, period = cands[0]
        all_txt = "/".join(c[1] for c in cands)
        rows.append((rank, icao, chip, color, period, e, all_txt))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def _metar_severity(r):
    """(color, token_str) for a breaching METAR row, on the same
    severity ladder as the TAF tiers."""
    toks = []
    tier = 3
    if r.get("vis_bad") and r.get("vis") is not None:
        v = r["vis"]
        t = 0 if v < 1 else (1 if v < 3 else 2)
        tier = min(tier, t)
        toks.append(f"{v:g}SM")
    if r.get("cig_bad") and r.get("cig") is not None:
        c = int(r["cig"])
        t = 0 if c < 500 else (1 if c < 1000 else 2)
        tier = min(tier, t)
        toks.append(f"CIG {c}")
    if r.get("wind_bad"):
        w = max([x for x in (r.get("spd"), r.get("gst"))
                 if x is not None], default=None)
        if w is not None:
            t = 0 if w >= 40 else (1 if w >= 35 else 2)
            tier = min(tier, t)
            toks.append(f"{int(w)}KT")
    if not toks:
        return None, ""
    color = (_MAGENTA, _RED, _ORANGE)[min(tier, 2)]
    return color, "/".join(toks)


def build_map_markers(board_rows, metar_rows, coords):
    """Merge TAF board + breaching METARs into ring/fill datasets.
    Solid fill = current METAR breach; ring = TAF forecast;
    concentric when both. Each carries its own severity color."""
    taf = {r[1]: (r[3], r[6]) for r in board_rows}
    met = {}
    for r in (metar_rows or []):
        color, toks = _metar_severity(r)
        if color:
            met[r["icao"]] = (color, toks)

    def _rgb(hexc):
        h = hexc.lstrip("#")
        return [int(h[k:k+2], 16) for k in (0, 2, 4)]

    fills, rings = [], []
    for icao in sorted(set(taf) | set(met)):
        if icao not in coords:
            continue
        la, lo = coords[icao]
        parts = []
        if icao in met:
            parts.append(f"NOW: {met[icao][1]}")
        if icao in taf:
            parts.append(f"TAF: {taf[icao][1]}")
        tip = f"{icao} | " + " | ".join(parts)
        base = {"lat": la, "lon": lo, "tip": tip}
        if icao in met:
            fills.append({**base,
                          "color": _rgb(met[icao][0]) + [235]})
        if icao in taf:
            rings.append({**base,
                          "color": _rgb(taf[icao][0]) + [235]})
    return fills, rings


def render_status_board(rows) -> str:
    """TAF board at ~2x scale: 16px cells, generous padding, and
    bold text whenever the severity is red or magenta."""
    _TEXT_COLOR = {_YELLOW: "#B8860B", _ORANGE: "#CC6600",
                   _LT_RED: "#E05555"}

    def cell(text, fg="#000000", bold=False, header=False):
        w = "bold" if (bold or header) else "normal"
        deco = "text-decoration:underline;" if header else ""
        return (
            f'<td style="background-color:#FFFFFF; color:{fg}; '
            f"-webkit-text-fill-color:{fg}; font-family:{_FONT}; "
            f"font-size:16px; padding:6px 16px; "
            f"border:1px solid #000000; font-weight:{w}; {deco}"
            f'white-space:nowrap;">{text}</td>'
        )

    header_row = ("<tr>" + cell("ICAO", header=True)
                  + cell("ALERTS", header=True) + "</tr>")
    body = []
    for rank, icao, chip, color, period, e, all_txt in rows:
        fg = _TEXT_COLOR.get(color, color)
        hot = color in (_RED, _MAGENTA)
        body.append(
            "<tr>"
            + cell(icao, bold=True)
            + cell(all_txt, fg=fg, bold=hot)
            + "</tr>"
        )
    return (
        f'<table style="border-collapse:collapse; '
        f'background-color:#FFFFFF; border:2px solid {_WHITE}; '
        f'width:auto;">'
        f"{header_row}{''.join(body)}</table>"
    )


# ---------------------------------------------------------------------------
# Alert map: whole network in gray, alerting airports in their chip
# color
# ---------------------------------------------------------------------------
from pathlib import Path as _Path

_persist = _Path("/opt/render/project/src/cache")
_MAP_CACHE_ROOT = _persist if _persist.exists() \
    else _Path("/tmp/wx_compare_cache")
_MAP_CACHE_ROOT.mkdir(parents=True, exist_ok=True)


@st.cache_data(ttl=3600, show_spinner=False)
def cached_station_coords(icaos_tuple: tuple):
    from core.stations import StationResolver
    resolver = StationResolver(
        cache_dir=_MAP_CACHE_ROOT / "stations"
    )
    out = {}
    for icao in icaos_tuple:
        try:
            stn = resolver.resolve(icao)
            if stn is not None:
                out[icao] = (float(stn.lat), float(stn.lon))
        except Exception:
            continue
    return out


# CONUS tile centers for fleet-wide position queries (each point
# query covers ~250 nm; rows sized so circles overlap coast to
# coast including Florida/Gulf)
_FLEET_TILES = [
    # PROVEN by route-sampling: 31 JBU great circles
    # sampled pointwise + a 0.4-deg heartland lattice all
    # covered. Base rows + seam rows (mid-country diagonal
    # holes that ate Kansas cruisers) + north tier
    # (Seattle was never covered before) + Bahamas + GA
    # offshore + CA Central Valley + Ontario seam.
    (26.5, -81.5), (26.5, -90.5), (26.5, -99.5),
    (34.0, -118.0), (34.0, -109.0), (34.0, -100.0),
    (34.0, -91.0), (34.0, -82.0), (34.0, -76.0),
    (41.5, -122.0), (41.5, -112.0), (41.5, -102.0),
    (41.5, -92.0), (41.5, -82.0), (41.5, -73.0),
    (30.3, -86.0), (30.3, -95.0), (30.3, -104.0),
    (37.8, -113.5), (37.8, -104.5), (37.8, -95.5),
    (37.8, -86.5), (37.8, -78.5), (44.4, -107.0),
    (44.4, -97.0), (44.4, -87.0), (46.9, -121.5),
    (46.9, -111.5), (46.9, -101.0), (46.9, -90.5),
    (45.8, -69.5), (25.0, -76.5), (30.8, -79.2),
    (37.3, -120.5), (44.8, -77.5),
]


@st.cache_data(ttl=90, show_spinner=False, max_entries=2)
def cached_fleet(bucket: str):
    """All airborne JBU over CONUS.

    Field-measured reality (8/13 verdicts): airplanes.live 403s
    every request from Render - dropped entirely. adsb.lol and
    adsb.fi both 429 under burst load - so each gets ONE paced
    sequential lane (~0.45s between calls, backoff-retry on 429),
    with cross-host retry for stragglers. Slower (~5s cold, 90s
    cached) but built to finish 15/15."""
    import threading
    import time as _time

    import requests as _rq

    HDRS = {"User-Agent": "bluemet.org ops dashboard"}

    def _url(host, la, lo):
        if host == "adsb.lol":
            return (f"https://api.adsb.lol/v2/point/"
                    f"{la:.2f}/{lo:.2f}/246")
        return (f"https://opendata.adsb.fi/api/v2/lat/"
                f"{la:.2f}/lon/{lo:.2f}/dist/246")

    tile_stats: list = []

    def _call(host, tile):
        la, lo = tile
        try:
            r = _rq.get(_url(host, la, lo), headers=HDRS,
                        timeout=5)
        except Exception as e:
            return None, f"{host}:{type(e).__name__}"
        if r.status_code != 200:
            return None, f"{host}:HTTP{r.status_code}"
        j = r.json()
        # adsb.fi proved capable of HTTP 200 with a different (or
        # empty) payload shape - accept both common keys
        ac = j.get("ac") or j.get("aircraft") or []
        out = []
        for p in ac:
            cs = (p.get("flight") or "").strip()
            if not cs.upper().startswith("JBU"):
                continue
            if p.get("lat") is None:
                continue
            alt = p.get("alt_baro")
            out.append((cs, float(p["lat"]), float(p["lon"]),
                        alt if isinstance(alt, (int, float))
                        else None))
        tile_stats.append(
            f"({la:.0f},{lo:.0f}) {host}: {len(ac)} ac, "
            f"{len(out)} JBU"
        )
        return out, ("EMPTY200" if not ac else None)

    def _lane(host, tiles, results, leftovers, empties):
        for tile in tiles:
            res, err = _call(host, tile)
            if res is None and "429" in (err or ""):
                _time.sleep(1.3)
                res, err = _call(host, tile)
            if res is None:
                leftovers.append((tile, err))
            else:
                results.append(res)
                if err == "EMPTY200":
                    empties.append(tile)
            _time.sleep(0.35)

    hosts = ("adsb.lol", "adsb.fi")
    lanes = {h: [t for i, t in enumerate(_FLEET_TILES)
                 if i % 2 == k] for k, h in enumerate(hosts)}
    results: list = []
    leftovers: dict = {h: [] for h in hosts}
    empties: dict = {h: [] for h in hosts}
    threads = [
        threading.Thread(target=_lane,
                         args=(h, lanes[h], results,
                               leftovers[h], empties[h]))
        for h in hosts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Self-healing: a host answering 200-but-zero-aircraft on 3+
    # tiles is defective (measured 8/13: adsb.fi did this on ALL
    # 17 of its tiles - half the country silently blank). Its
    # empty tiles re-run on the other host, paced.
    for h in hosts:
        if len(empties[h]) >= 3:
            other = hosts[1] if h == hosts[0] else hosts[0]
            for tile in empties[h]:
                _time.sleep(0.35)
                res, err = _call(other, tile)
                if res is not None and err != "EMPTY200":
                    results.append(res)

    # Stragglers: one paced retry on the OTHER host
    fails = []
    for h in hosts:
        other = hosts[1] if h == hosts[0] else hosts[0]
        for tile, err1 in leftovers[h]:
            _time.sleep(0.35)
            res, err2 = _call(other, tile)
            if res is None:
                fails.append(
                    f"({tile[0]:.0f},{tile[1]:.0f}) "
                    f"{err1} -> {err2}"
                )
            else:
                results.append(res)

    seen = {}
    for res in results:
        for cs, la, lo, alt in res:
            if cs not in seen:
                seen[cs] = (la, lo, alt)

    out = []
    for cs, (la, lo, alt) in seen.items():
        alt_s = (f"FL{int(alt // 100):03d}"
                 if alt and alt >= 18000
                 else (f"{int(alt):,} ft" if alt else "alt n/a"))
        out.append({
            "callsign": cs, "lat": la, "lon": lo,
            "tip": f"{cs} | {alt_s}",
        })
    ok = len(_FLEET_TILES) - len(fails)
    return out, ok, len(_FLEET_TILES), fails, tile_stats

# ---------------------------------------------------------------------------
# Cached analysis — TAFs update every 6 hours; 15-min cache is fresh enough
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def cached_analyze(
    icaos_tuple: tuple[str, ...],
    window_start_iso: str,
    window_end_iso: str,
    vis_threshold_sm: float,
    ceiling_threshold_ft: int,
    tsra_enabled: bool,
):
    """Run TAF analysis. Cached by exact parameter combination."""
    from core.taf import analyze_tafs
    return analyze_tafs(
        icaos=list(icaos_tuple),
        window_start=datetime.fromisoformat(window_start_iso),
        window_end=datetime.fromisoformat(window_end_iso),
        vis_threshold_sm=vis_threshold_sm,
        ceiling_threshold_ft=ceiling_threshold_ft,
        tsra_enabled=tsra_enabled,
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Airport Alerts")
st.caption(
    f"Scans TAFs from {len(JETBLUE_ICAOS)} JetBlue destinations and flags "
    "airports forecast to see low visibility, low ceilings, or thunderstorms."
)

with st.sidebar:
    st.header("Alert thresholds")

    vis_threshold = st.slider(
        "Visibility threshold (sm)",
        min_value=0.5, max_value=6.0, value=2.0, step=0.5,
        help="Flag airports forecast BELOW this value.",
    )
    ceiling_threshold = st.slider(
        "Ceiling threshold (ft AGL)",
        min_value=200, max_value=3000, value=1000, step=100,
        help="Flag airports forecast BELOW this value.",
    )
    tsra_enabled = st.checkbox(
        "Flag thunderstorms (TS/TSRA)",
        value=True,
        help="Includes TS, TSRA, +TSRA, -TSRA. Excludes VCTS (vicinity).",
    )
    wind_threshold = st.slider(
        "Wind/gust threshold (kt) — METARs",
        min_value=15, max_value=50, value=25, step=5,
        help="Current-METAR section flags sustained or gust at/above this.",
    )

    st.divider()
    st.header("Time window")

    hours_ahead = st.slider(
        "Alert horizon (hours from now)",
        min_value=1, max_value=24, value=12, step=1,
        help="How far into the future to scan. TAFs typically cover 24-30 hours.",
    )

    st.divider()
    run_button = st.button(
        "Refresh alerts", type="primary", use_container_width=True
    )

    st.divider()
    st.markdown(
        '<span style="color:#CC0000; -webkit-text-fill-color:#CC0000; font-weight:bold;">'
        "RED highlight</span> = critical severity: "
        f"vis &lt; {_fmt_vis(CRITICAL_VIS_SM)} sm or ceiling &lt; "
        f"{CRITICAL_CIG_FT} ft.",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
if run_button:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    window_end = now + timedelta(hours=hours_ahead)

    st.info(
        f"Window: **{now:%Y-%m-%d %H:%M UTC}** to **{window_end:%H:%M UTC}** "
        f"(next {hours_ahead}h)"
    )

    with st.spinner(f"Fetching TAFs for {len(JETBLUE_ICAOS)} stations..."):
        try:
            results = cached_analyze(
                icaos_tuple=tuple(JETBLUE_ICAOS),
                window_start_iso=now.isoformat(),
                window_end_iso=window_end.isoformat(),
                vis_threshold_sm=vis_threshold,
                ceiling_threshold_ft=ceiling_threshold,
                tsra_enabled=tsra_enabled,
            )
        except Exception as e:
            st.error(f"Failed to fetch TAFs: {e}")
            st.stop()

    # Airport status board: one row per alerting airport,
    # severity-sorted, with the driving condition as a colored chip.
    with st.spinner("Fetching current METARs..."):
        try:
            metar_rows = cached_current_metars(
                icaos_tuple=tuple(JETBLUE_ICAOS),
                vis_threshold_sm=vis_threshold,
                ceiling_threshold_ft=ceiling_threshold,
                wind_threshold_kt=wind_threshold,
            )
        except Exception as e:
            metar_rows = []
            st.warning(f"METAR fetch failed: {e}")

    # Layout: TAF alerts beside the map; METARs centered below
    board_rows = build_status_board(results, metar_rows)

    _deck = None
    _fleet_n = 0
    _cov = ""
    _map_err = None
    try:
        import pydeck as pdk

        import pydeck as pdk

        coords = cached_station_coords(tuple(JETBLUE_ICAOS))

        fills, rings = build_map_markers(
            board_rows, metar_rows, coords
        )

        bucket1 = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        try:
            (fleet, ok_tiles, n_tiles, tile_fails,
             tile_stats) = cached_fleet(bucket1)
        except Exception:
            fleet, ok_tiles, n_tiles = [], 0, 0
            tile_fails, tile_stats = [], []

        # No custom city layer: the light basemap already labels
        # major cities at these zooms (ours doubled them)
        layers = []
        if fleet:
            layers.append(pdk.Layer(
                "ScatterplotLayer", data=fleet,
                get_position="[lon, lat]",
                get_fill_color=[0, 90, 220, 230],
                get_radius=12000, radius_min_pixels=4,
                radius_max_pixels=9, stroked=True,
                get_line_color=[255, 255, 255],
                line_width_min_pixels=1, pickable=True,
            ))
            layers.append(pdk.Layer(
                "TextLayer", data=fleet,
                get_position="[lon, lat]",
                get_text="callsign", get_size=8,
                get_color=[0, 70, 190, 175],
                get_text_anchor='"start"',
                get_pixel_offset=[6, -6],
            ))
        if fills:
            # Solid core: current METAR breach (trouble NOW)
            layers.append(pdk.Layer(
                "ScatterplotLayer", data=fills,
                get_position="[lon, lat]",
                get_fill_color="color",
                get_radius=15000,
                radius_min_pixels=5, radius_max_pixels=13,
                stroked=True, get_line_color=[0, 0, 0],
                line_width_min_pixels=1, pickable=True,
            ))
        if rings:
            # Hollow ring: TAF forecast breach (trouble COMING);
            # concentric around a core when both apply
            layers.append(pdk.Layer(
                "ScatterplotLayer", data=rings,
                get_position="[lon, lat]",
                get_line_color="color",
                get_radius=30000,
                radius_min_pixels=10, radius_max_pixels=24,
                filled=False, stroked=True,
                line_width_min_pixels=3.5, pickable=True,
            ))

        _deck = pdk.Deck(
            layers=layers,
            initial_view_state=pdk.ViewState(
                latitude=38.3, longitude=-96.0,
                zoom=3.5, min_zoom=3.4, max_zoom=11,
            ),
            map_style="light",
            tooltip={"html": "<b>{tip}</b>"},
        )
        _fleet_n = len(fleet)
        _cov = (f" (coverage {ok_tiles}/{n_tiles} tiles)"
                if ok_tiles < n_tiles else "")
    except Exception as e:
        _map_err = str(e)

    # Status strip: the three numbers that summarize the network
    n_sev_all = sum(1 for r in board_rows if r[0] == 0)
    m1, m2, m3 = st.columns(3)
    m1.metric("JBU flights airborne", _fleet_n if _deck else "-")
    m2.metric("Airports alerting", len(board_rows))
    m3.metric("Severe (magenta-tier)", n_sev_all)

    col_taf, col_map, _col_r = st.columns([1, 2, 1],
                                          gap="medium")

    with col_taf:
        st.subheader("TAF alerts")
        if not tsra_enabled:
            st.caption("TSRA alerts disabled in sidebar.")
        if board_rows:
            st.markdown(render_status_board(board_rows),
                        unsafe_allow_html=True)

        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)

    with col_map:
        if _deck is not None:
            st.pydeck_chart(_deck, height=660)
            st.caption(
                "Solid dot = METAR breaching NOW; ring = TAF "
                "forecast; concentric = both (each in its own "
                f"severity color). {_fleet_n} JBU airborne "
                f"(blue{_cov}). Hover for details."
            )
        else:
            st.caption(f"Map unavailable: {_map_err}")

    if tile_fails:
        with st.expander(
            f"Fleet coverage report - {len(tile_fails)} tile(s) "
            f"failed all three hosts"
        ):
            for f in tile_fails:
                st.text(f)
    with st.expander("Fleet tile stats (debug)"):
        st.caption(
            "Per-tile aircraft totals - a tile pinned at a round "
            "number (250/1000) with JBU flights missing suggests a "
            "response cap truncating dense airspace."
        )
        for line in tile_stats:
            st.text(line)

    with st.expander("Missing flight finder (debug)"):
        st.caption(
            "Enter flight numbers (e.g. 374, 1174). Each is "
            "queried directly by callsign, then tested against the "
            "tile grid - the verdict says whether it was missed by "
            "a bug, outside coverage, or not transmitting."
        )
        _mff = st.text_input("Flight numbers", key="mff_in")
        if st.button("Find flights", key="mff_go") and _mff:
            import math as _math

            import requests as _rq3

            def _tile_covered(la, lo):
                for ta, to in _FLEET_TILES:
                    dlat = la - ta
                    dlon = ((lo - to)
                            * _math.cos(_math.radians(
                                (la + ta) / 2)))
                    if dlat*dlat + dlon*dlon <= 4.1*4.1:
                        return True
                return False

            fleet_cs = {d["callsign"].upper() for d in fleet}
            for num in [x.strip() for x in _mff.split(",")
                        if x.strip()]:
                cs = f"JBU{num}"
                try:
                    rr = _rq3.get(
                        f"https://api.adsb.lol/v2/callsign/{cs}",
                        headers={"User-Agent": "bluemet.org"},
                        timeout=6,
                    )
                    ac = (rr.json().get("ac") or []) \
                        if rr.status_code == 200 else []
                except Exception as e:
                    st.text(f"{cs}: query failed "
                            f"({type(e).__name__})")
                    continue
                if not ac:
                    st.text(f"{cs}: no data - not currently "
                            f"transmitting (or not airborne)")
                    continue
                p = ac[0]
                la, lo = p.get("lat"), p.get("lon")
                alt = p.get("alt_baro")
                if la is None:
                    st.text(f"{cs}: known but no position")
                    continue
                inside = _tile_covered(la, lo)
                on_map = cs in fleet_cs
                verdict = (
                    "ON MAP" if on_map else
                    ("INSIDE TILES BUT MISSED - likely response "
                     "cap or timing" if inside else
                     f"OUTSIDE tile coverage")
                )
                st.text(f"{cs}: ({la:.1f},{lo:.1f}) alt={alt} "
                        f"-> {verdict}")

    with st.expander("Route lookup probe (debug)"):
        st.caption(
            "Raw routeset response for up to 3 live callsigns - "
            "paste this to Claude to finish the destination-"
            "warning feature."
        )
        if st.button("Probe routes", key="route_probe"):
            try:
                import json as _json

                import requests as _rq2
                sample = fleet[:3]
                payload = {"planes": [
                    {"callsign": d["callsign"], "lat": d["lat"],
                     "lng": d["lon"]} for d in sample
                ]}
                rr = _rq2.post(
                    "https://api.adsb.lol/api/0/routeset",
                    json=payload, timeout=8,
                    headers={"User-Agent": "bluemet.org"},
                )
                st.text(f"HTTP {rr.status_code}")
                st.code(_json.dumps(rr.json(), indent=1)[:3000])
            except Exception as e:
                st.text(f"probe failed: {type(e).__name__}: {e}")

    _ml, mid_col, _mr = st.columns([1, 2, 1])
    with mid_col:
        st.subheader("Current METARs at/beyond thresholds")
        st.caption(
            f"Latest ob per station - vis < {vis_threshold:g} sm, "
            f"cig < {ceiling_threshold} ft, wind >= "
            f"{wind_threshold} kt. Red cell = breaching value."
        )
        if metar_rows:
            st.markdown(render_metar_table(metar_rows),
                        unsafe_allow_html=True)
        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)

    # TAF unavailable + parse errors — smaller notes at bottom
    st.divider()
    with st.expander(
        f"TAF unavailable for {len(results.unavailable_icaos)} stations",
        expanded=False,
    ):
        if results.unavailable_icaos:
            st.write(", ".join(results.unavailable_icaos))
        else:
            st.write("All stations returned a TAF.")

    if results.parse_errors:
        with st.expander(
            f"Parse errors on {len(results.parse_errors)} stations",
            expanded=False,
        ):
            for icao, err in results.parse_errors.items():
                st.write(f"**{icao}**: {err}")

else:
    st.info("Adjust thresholds and click **Refresh alerts** in the sidebar.")

    st.markdown(
        """
        ### What this does

        Scans the latest TAF for every JetBlue destination and flags airports
        forecast to experience:

        - **Low visibility** — below a threshold you set (default: 2 sm)
        - **Low ceilings** — below a threshold you set (default: 1000 ft)
        - **Thunderstorms** — TS, TSRA, or +TSRA (excludes VCTS)

        Rows highlighted in **red** mark critical severity: visibility below
        1 sm or ceiling below 400 ft.

        Only forecast periods that overlap your chosen time window count.
        A station is listed once per table with its worst value and the
        forecast period responsible for it.

        ### Sources

        Latest raw TAFs from
        [aviationweather.gov](https://aviationweather.gov/api/data/taf),
        parsed with the [avwx-engine](https://github.com/avwx-rest/avwx-engine)
        library. TAFs are cached for 15 minutes to reduce load on AWC.
        """
    )
