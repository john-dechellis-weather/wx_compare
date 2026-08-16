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
    page_title="BlueMet — JBU Weather Map CONUS",
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
        f"font-family:{_FONT}; font-size:clamp(6px, 0.45vw, 8px); padding:0.2em 0.5em; "
        f"border:1px solid #000000; font-weight:{weight}; "
        f'text-align:{align}; white-space:nowrap;">{text}</td>'
    )


def _th(text, align="left") -> str:
    return (
        f'<td style="background-color:#FFFFFF; color:#000000; '
        f"-webkit-text-fill-color:#000000; "
        f"font-family:{_FONT}; font-size:clamp(6px, 0.45vw, 8px); padding:0.22em 0.5em; "
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
        f"color:#000000; -webkit-text-fill-color:#000000; font-family:{_FONT}; font-size:clamp(6px, 0.45vw, 8px); "
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
        # Destination-hazard fields (fixed criteria, independent of
        # the sidebar sliders): thunderstorm in the ob, LIFR, or
        # 35+ kt
        import re as _re
        # Present-weather TS only: parse the METAR BODY (remarks
        # carry TSE##/TSB## end/begin times and LTG chatter that
        # must not trigger - a remarks 'TSE15' once painted a
        # false red for a storm that had ENDED). Strict token
        # grammar: optional +/-/VC, TS, optional precip pairs,
        # then a hard token boundary.
        _body = (o.raw_text or "").split(" RMK")[0]
        ts_now = bool(_re.search(
            r"(?:^|\s)(?:\+|-|VC)?TS(?:[A-Z]{2}){0,3}(?=\s|$)",
            _body))
        lifr = ((o.vsby_sm is not None and o.vsby_sm < 1)
                or (not o.ceiling_unlimited
                    and o.ceiling_ft is not None
                    and o.ceiling_ft < 500))
        wind35 = (o.wind_gust_kt is not None
                  and o.wind_gust_kt > 35)
        rows.append({
            "icao": icao,
            "obs_time": o.obs_time,
            "vis": o.vsby_sm, "vis_bad": vis_bad,
            "cig": o.ceiling_ft, "cig_unl": o.ceiling_unlimited,
            "cig_bad": cig_bad,
            "spd": o.wind_speed_kt, "gst": o.wind_gust_kt,
            "wind_bad": wind_bad,
            "raw": o.raw_text,
            "ts_now": ts_now, "lifr": lifr, "wind35": wind35,
            "wind_max": wind_max,
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
            # All TS intensities (incl VCTS) read light red; IFR
            # keeps dark red and LIFR magenta as the only
            # escalations
            cands.append((2, e["ts"], _LT_RED, e["ts_p"]))
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
        rows.append((rank, icao, chip, color, period, e, all_txt,
                     cands))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


# Bold orange "TS" as an image icon: TextLayer proved unreliable
# for this one layer, and IconLayer has never failed on this map
# (aircraft, rings, triangles all shipped through it). Bold comes
# free - it's just pixels.
def _ts_text_icon_uri():
    import urllib.parse
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" '
        'height="40" viewBox="0 0 96 40">'
        '<text x="48" y="29" text-anchor="middle" '
        'font-family="Arial, Helvetica, sans-serif" '
        'font-size="23" font-weight="900" fill="#E01A1A" '
        'stroke="#FFFFFF" stroke-width="1.3" '
        'paint-order="stroke">TS</text>'
        "</svg>"
    )
    return ("data:image/svg+xml;charset=utf-8,"
            + urllib.parse.quote(svg))


# anchorY 106 puts the glyph's BOTTOM edge (anchorY-56 units)
# above the station scaled with icon size - paired with meter
# units below, clearance tracks the ring across all zooms
# Geometry locked to the ring: icon meters = ring meters x 1.4375
# and clamps [11.5, 23] = ring clamps [8, 16] x 1.4375, so the
# proportion holds even when zoom pins both at their bounds.
_TS_TEXT_ICON = {"url": _ts_text_icon_uri(), "width": 96,
                 "height": 40, "anchorX": 48, "anchorY": 20,
                 "mask": False}


# A320-detailed aircraft icon (style 1: nacelles + sharklets),
# baked as an inline SVG data-URI - no external image dependency.
def _a320_icon_uri(fill="#005ADC"):
    import urllib.parse
    body = ("M0,-10 L0.35,-9.6 L0.55,-8.8 L0.6,-6 L0.6,-1.6 "
            "L9.2,3.2 L9.6,3.4 L9.6,4 L9.1,4.1 L2.6,3.3 "
            "L0.6,3.1 L0.6,6.4 L3.3,8.2 L3.3,9 L0.5,8.5 "
            "L0.45,9.4 L0,9.7 L-0.45,9.4 L-0.5,8.5 L-3.3,9 "
            "L-3.3,8.2 L-0.6,6.4 L-0.6,3.1 L-2.6,3.3 "
            "L-9.1,4.1 L-9.6,4 L-9.6,3.4 L-9.2,3.2 L-0.6,-1.6 "
            "L-0.6,-6 L-0.55,-8.8 L-0.35,-9.6 Z")
    eng_r = ("M2.6,-0.9 L3.35,-0.9 L3.45,-0.4 L3.45,1.6 "
             "L3.3,1.9 L2.75,1.9 L2.6,1.5 Z")
    eng_l = ("M-2.6,-0.9 L-3.35,-0.9 L-3.45,-0.4 L-3.45,1.6 "
             "L-3.3,1.9 L-2.75,1.9 L-2.6,1.5 Z")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" '
        'height="64" viewBox="-11 -11 22 22">'
        f'<g fill="{fill}" stroke="#FFFFFF" stroke-width="0.5">'
        f'<path d="{body}"/><path d="{eng_r}"/>'
        f'<path d="{eng_l}"/></g></svg>'
    )
    return ("data:image/svg+xml;charset=utf-8,"
            + urllib.parse.quote(svg))


_AC_ICON = {"url": _a320_icon_uri(), "width": 64, "height": 64,
            "anchorX": 32, "anchorY": 32, "mask": False}
_AC_ICON_RED = {"url": _a320_icon_uri("#E01A1A"), "width": 64,
                "height": 64, "anchorX": 32, "anchorY": 32,
                "mask": False}


def _legend_html() -> str:
    """Design B: composite marker left, right-stacked labels with
    elbow leaders computed to touch each element's edge; JBU
    aircraft rows beneath in the same visual scheme."""
    plane_b = _a320_icon_uri()
    plane_r = _a320_icon_uri("#E01A1A")
    # Geometry (px, computed so leaders touch edges exactly):
    # marker center (74, 96); ring R=26 (stroke 5); dot r=11;
    # TS centered at (74, 40), ~15px half-width at font 22
    LBL = ('font-family="Arial, Helvetica, sans-serif" '
           'font-size="12.5" font-weight="bold" fill="#4477DD"')
    LINE = ('stroke="#88AAEE" stroke-width="1.6" fill="none" '
            'stroke-linejoin="round"')
    LX = 118          # label column x; leaders end at LX-6
    diagram = (
        '<svg viewBox="0 0 320 168" width="100%" '
        'style="max-width:160px; display:block;" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="74" cy="96" r="26" fill="none" '
        'stroke="#FF00FF" stroke-width="5"/>'
        '<circle cx="74" cy="96" r="11" fill="#FF00FF" '
        'stroke="#000000"/>'
        '<text x="74" y="47" text-anchor="middle" '
        'font-family="Arial, Helvetica, sans-serif" '
        'font-size="22" font-weight="900" fill="#E01A1A" '
        'stroke="#FFFFFF" stroke-width="1.5" '
        'paint-order="stroke">TS</text>'
        # TS leader: from right edge of letters (74+17, 40)
        f'<line x1="93" y1="40" x2="112" y2="40" {LINE}/>'
        f'<text x="{LX}" y="44" {LBL}>Thunderstorm in current '
        "METAR</text>"
        # ring leader: rim right point at its own height (100,88)
        # rim point toward label: (74+25.9, 96-7) ~ (100, 89)
        f'<line x1="101" y1="89" x2="112" y2="89" {LINE}/>'
        f'<text x="{LX}" y="93" {LBL}>TAF below '
        "criteria</text>"
        # dot leader: down from dot bottom (74,107)+edge, elbow
        # right at y=140
        f'<polyline points="74,110 74,140 112,140" {LINE}/>'
        f'<text x="{LX}" y="144" {LBL}>METAR below '
        "criteria</text>"
        "</svg>"
    )
    row = ("display:flex; align-items:center; gap:10px; "
           "padding:4px 0;")
    txt = ("color:#000; -webkit-text-fill-color:#000; "
           "font-family:Georgia, 'Times New Roman', serif; "
           "font-size:clamp(9px, 0.7vw, 12px);")
    planes = "".join(
        f'<div style="{row}">'
        f'<img src="{u}" '
        'style="flex:none; width:1.4em; height:1.4em;"/>'
        f'<span style="{txt}">{label}</span></div>'
        for u, label in (
            (plane_b, "JBU flight (with heading)"),
            (plane_r, "Destination METAR has TS / LIFR / "
                      "&gt;G35kt"),

        )
    )
    def ring_sw(color):
        return ('<svg width="18" height="18" '
                'xmlns="http://www.w3.org/2000/svg">'
                f'<circle cx="9" cy="9" r="6.5" fill="none" '
                f'stroke="{color}" stroke-width="2.6"/></svg>')

    rings_sec = "".join(
        f'<div style="{row}">'
        f'<span style="width:22px; text-align:center;">'
        f"{ring_sw(c)}</span>"
        f'<span style="{txt}">{label}</span></div>'
        for c, label in (
            ("#FF00FF", "LIFR in TAF (cig &lt;400ft or "
                        "vis &lt;1sm)"),
            ("#E01A1A", "IFR in TAF (cig &lt;1000ft or "
                        "vis &lt;2sm)"),
            ("#0B6B0B", "&ge;40kt wind in TAF"),
            ("#4CBB17", "&ge;30kt wind in TAF"),
            ("#F2C200", "Thunderstorm in TAF"),
        )
    )
    return (
        '<div style="background:#FFFFFF; border:1px solid #000; '
        'padding:6px 10px; margin-top:26px; width:auto; '
        'display:inline-block;">'
        '<div style="color:#000; -webkit-text-fill-color:#000; '
        f"font-family:{_FONT}; "
        "font-size:clamp(10px, 0.85vw, 13px); "
        'font-weight:bold; text-decoration:underline; '
        'margin-bottom:2px;">MAP KEY</div>'
        + diagram + planes + rings_sec + "</div>"
    )


def _metar_severity(r, include_ts=True):
    """(color, token_str) for a breaching METAR row, on the same
    severity ladder as the TAF tiers. include_ts=False computes
    the non-thunderstorm severity (the map's dot; TS gets its own
    glyph)."""
    toks = []
    tier = 3
    if include_ts and r.get("ts_now"):
        tier = min(tier, 1)
        toks.append("TS")
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


def build_map_markers(board_rows, metar_rows, coords,
                      taf_ring_map=None):
    taf_ring_map = taf_ring_map or {}
    """Merge TAF board + breaching METARs into map datasets.

    Solid fill = current NON-TS METAR breach; ring = NON-TS TAF
    forecast; thunderstorms (either source) render as the classic
    red TS text above the station instead,
    offset right of the dot/ring when other conditions coexist.
    Each element keeps its own severity color."""
    taf = {}
    for r in board_rows:
        icao, e, cands = r[1], r[5], r[7]
        # Ring color comes from the fixed-criteria map built in
        # the data phase (LIFR/IFR/wind tiers/TS)
        taf[icao] = {
            "ts": bool(e.get("ts")),
            "ring": taf_ring_map.get(icao),
            "txt": r[6],
        }
    met = {}
    for r in (metar_rows or []):
        color, toks = _metar_severity(r, include_ts=True)
        met[r["icao"]] = {
            "ts": bool(r.get("ts_now")),
            "fill": color, "toks": toks,
        }

    def _rgb(hexc):
        h = hexc.lstrip("#")
        return [int(h[k:k+2], 16) for k in (0, 2, 4)]

    fills, rings, ts_marks = [], [], []
    for icao in sorted(set(taf) | set(met)):
        if icao not in coords:
            continue
        la, lo = coords[icao]
        t, m = taf.get(icao), met.get(icao)
        parts = []
        if m and (m["toks"] or m["ts"]):
            now = m["toks"] or ""
            if m["ts"]:
                now = ("TS/" + now) if now else "TS"
            parts.append(f"NOW: {now}")
        if t:
            parts.append(f"TAF: {t['txt']}")
        tip = f"{icao} | " + " | ".join(parts)
        base = {"lat": la, "lon": lo, "tip": tip}
        if m and m["fill"]:
            fills.append({**base,
                          "color": _rgb(m["fill"]) + [235]})
        if t and t["ring"]:
            rings.append({**base,
                          "color": _rgb(t["ring"]) + [235]})
        if (m and m["ts"]) or (t and t["ts"]):
            ts_marks.append(base)
    return fills, rings, ts_marks


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
            f"font-size:clamp(9px, 0.7vw, 11px); "
            f"padding:0.3em 0.75em; "
            f"border:1px solid #000000; font-weight:{w}; {deco}"
            f'white-space:nowrap;">{text}</td>'
        )

    header_row = ("<tr>" + cell("ICAO", header=True)
                  + cell("ALERTS", header=True) + "</tr>")
    body = []
    for rank, icao, chip, color, period, e, all_txt, _c in rows:
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
# Callsign -> destination ICAO cache (adsbdb.com lookups). Routes
# are stable per-callsign within a day, so each fleet cycle only
# fetches callsigns it hasn't seen; misses negative-cache for an
# hour so unknowns aren't hammered.
_route_cache: dict = {}     # cs -> (dest_icao_or_"", expiry_ts)
_ROUTE_CACHE_PATH = _MAP_CACHE_ROOT / "route_cache.json"
try:
    import json as _json_rc
    for _k, _v in _json_rc.loads(
            _ROUTE_CACHE_PATH.read_text()).items():
        _route_cache[_k] = (_v[0], float(_v[1]))
except Exception:
    pass


def _save_route_cache():
    try:
        import json as _json_rc
        import time as _t_rc
        now = _t_rc.time()
        keep = {k: v for k, v in _route_cache.items()
                if v[1] > now}
        _ROUTE_CACHE_PATH.write_text(_json_rc.dumps(keep))
    except Exception:
        pass


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
            trk = p.get("track")
            gs = p.get("gs")
            out.append((cs, float(p["lat"]), float(p["lon"]),
                        alt if isinstance(alt, (int, float))
                        else None,
                        float(trk) if isinstance(
                            trk, (int, float)) else 0.0,
                        float(gs) if isinstance(
                            gs, (int, float)) else None))
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
            _time.sleep(0.25)

    hosts = ("adsb.lol", "adsb.fi")
    lanes = {h: [t for i, t in enumerate(_FLEET_TILES)
                 if i % 2 == k] for k, h in enumerate(hosts)}
    results: list = []
    leftovers: dict = {h: [] for h in hosts}
    empties: dict = {h: [] for h in hosts}
    # Two paced sub-lanes per host (4 workers total): each worker
    # keeps the 0.25s inter-call pacing, so per-host request rate
    # stays modest while wall time halves
    threads = [
        threading.Thread(target=_lane,
                         args=(h, lanes[h][k::2], results,
                               leftovers[h], empties[h]))
        for h in hosts for k in (0, 1)
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
                _time.sleep(0.25)
                res, err = _call(other, tile)
                if res is not None and err != "EMPTY200":
                    results.append(res)

    # Stragglers: one paced retry on the OTHER host
    fails = []
    for h in hosts:
        other = hosts[1] if h == hosts[0] else hosts[0]
        for tile, err1 in leftovers[h]:
            _time.sleep(0.25)
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
        for cs, la, lo, alt, trk, gs in res:
            if cs not in seen:
                seen[cs] = (la, lo, alt, trk)

    # Destinations: one routeset POST for the whole fleet. The
    # parser is shape-defensive (public schema: _airports list of
    # {icao,...}; fallback: "airport_codes" like "JFK-MCO", IATA
    # mapped K+code for CONUS). A flight that can't resolve simply
    # has no dest - never an error.
    dests = {}
    rs_diag = []
    now_ts = _time.time()
    new_cs = [cs for cs in seen
              if cs not in _route_cache
              or _route_cache[cs][1] < now_ts]
    fetched = hits = 0
    for cs in new_cs[:70]:      # cap per cycle; converges fast
        try:
            r = _rq.get(
                f"https://api.adsbdb.com/v0/callsign/{cs}",
                headers=HDRS, timeout=5,
            )
        except Exception as e:
            rs_diag.append(f"{cs}: {type(e).__name__}"[:80])
            break               # host trouble: stop this cycle
        fetched += 1
        d_icao = ""
        if r.status_code == 200:
            try:
                fr = (r.json().get("response") or {})
                if isinstance(fr, dict):
                    fr = fr.get("flightroute") or {}
                    dest = fr.get("destination") or {}
                    d_icao = (dest.get("icao_code") or "").upper()
            except Exception:
                pass
        if d_icao:
            _route_cache[cs] = (d_icao, now_ts + 6 * 3600)
            hits += 1
        else:
            _route_cache[cs] = ("", now_ts + 3600)
        _time.sleep(0.05)
    if fetched:
        _save_route_cache()
    for cs in seen:
        cached = _route_cache.get(cs)
        if cached and cached[0]:
            dests[cs] = cached[0]
    rs_diag.append(
        f"adsbdb: {fetched} fetched this cycle ({hits} routed), "
        f"{max(0, len(new_cs) - fetched)} still pending, "
        f"cache holds "
        f"{sum(1 for v in _route_cache.values() if v[0])} routes"
    )

    out = []
    for cs, (la, lo, alt, trk) in seen.items():
        alt_s = (f"FL{int(alt // 100):03d}"
                 if alt and alt >= 18000
                 else (f"{int(alt):,} ft" if alt else "alt n/a"))
        out.append({
            "callsign": cs, "lat": la, "lon": lo,
            "dest": dests.get(cs, ""),
            # deck.gl IconLayer angle is CCW; heading is CW from N
            "angle": (360.0 - trk) % 360.0,
            "gs": gs,
            "tip": f"{cs} | {alt_s}",
        })
    ok = len(_FLEET_TILES) - len(fails)
    return out, ok, len(_FLEET_TILES), fails, tile_stats, rs_diag

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
st.title("JBU Weather Map CONUS")
st.markdown(
    """
    <style>
    div[data-testid="stVerticalBlock"] { gap: 0.25rem; }
    div[data-testid="stElementContainer"] { margin: 0; }
    </style>
    """,
    unsafe_allow_html=True,
)
# Prefetch overlap: start the paced fetchers (fleet sweep, MRMS
# decode) in background threads immediately - their politeness
# sleeps then run concurrently with TAF/METAR fetching and
# analysis instead of serially after it (this was most of the
# minute-long cold load).
def _kick_prefetch():
    from concurrent.futures import ThreadPoolExecutor

    from streamlit.runtime.scriptrunner import (
        add_script_run_ctx, get_script_run_ctx,
    )
    if "_prefetch_pool" not in st.session_state:
        st.session_state["_prefetch_pool"] = \
            ThreadPoolExecutor(max_workers=2)
    pool = st.session_state["_prefetch_pool"]
    ctx = get_script_run_ctx()
    b1 = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")

    def _fleet_job():
        try:
            return cached_fleet(b1)
        except Exception:
            return None

    for name, job in (("_fleet_future", _fleet_job),):
        fut = pool.submit(job)
        try:
            for t in pool._threads:
                add_script_run_ctx(t, ctx)
        except Exception:
            pass
        st.session_state[name] = fut


_kick_prefetch()

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
        help="Includes TS, TSRA, +TSRA, -TSRA, and VCTS (vicinity).",
    )
    wind_threshold = st.slider(
        "Wind/gust threshold (kt) — METARs",
        min_value=15, max_value=50, value=25, step=5,
        help="Current-METAR section flags sustained or gust at/above this.",
    )

    st.divider()
    st.header("Map")
    map_height = st.slider(
        "Map height (px)", 450, 1200, 650, 50,
        help="Width is fluid (fills the space beside the TAF "
             "table and follows the window); height is set "
             "here - Streamlit's component sizing defeats "
             "pure-CSS viewport tracking.",
    )

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

    # METAR fetch overlaps the TAF analysis (independent AWC
    # calls; the pool carries Streamlit's script context)
    def _kick_metars():
        from concurrent.futures import ThreadPoolExecutor

        from streamlit.runtime.scriptrunner import (
            add_script_run_ctx, get_script_run_ctx,
        )
        if "_prefetch_pool2" not in st.session_state:
            st.session_state["_prefetch_pool2"] = \
                ThreadPoolExecutor(max_workers=1)
        pool = st.session_state["_prefetch_pool2"]
        ctx = get_script_run_ctx()

        def _job():
            try:
                return cached_current_metars(
                    icaos_tuple=tuple(JETBLUE_ICAOS),
                    vis_threshold_sm=vis_threshold,
                    ceiling_threshold_ft=ceiling_threshold,
                    wind_threshold_kt=wind_threshold,
                )
            except Exception:
                return None

        fut = pool.submit(_job)
        try:
            for t in pool._threads:
                add_script_run_ctx(t, ctx)
        except Exception:
            pass
        return fut

    _metar_fut = _kick_metars()

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
    metar_rows = None
    try:
        metar_rows = _metar_fut.result(timeout=45)
    except Exception:
        metar_rows = None
    if metar_rows is None:
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
    metar_all = metar_rows
    metar_rows = [r for r in metar_all
                  if r["vis_bad"] or r["cig_bad"] or r["wind_bad"]]

    # Destination hazards: {icao: "TSRA/0.5SM/G38"} for the
    # aircraft-warning layer
    # Ring colors from FIXED criteria on TAF minima (independent
    # of the sidebar sliders, which govern the board):
    #   magenta  LIFR: cig < 400 ft or vis < 1 sm
    #   red      IFR:  cig < 1000 ft or vis < 2 sm
    #   dk green wind >= 40 kt   |   lt green wind >= 30 kt
    #   yellow   thunderstorm in TAF
    _min_vis = {a.icao: a.min_vis_sm for a in results.vis_alerts}
    _min_cig = {a.icao: a.min_ceiling_ft
                for a in results.ceiling_alerts}
    _max_wind = {a.icao: a.max_wind_kt
                 for a in results.wind_alerts}
    _has_ts = {a.icao for a in results.tsra_alerts}
    taf_ring = {}
    for _ic in (set(_min_vis) | set(_min_cig) | set(_max_wind)
                | _has_ts):
        v = _min_vis.get(_ic)
        c = _min_cig.get(_ic)
        w = _max_wind.get(_ic) or 0
        if (c is not None and c < 400) or \
                (v is not None and v < 1):
            taf_ring[_ic] = "#FF00FF"
        elif (c is not None and c < 1000) or \
                (v is not None and v < 2):
            taf_ring[_ic] = "#E01A1A"
        elif w >= 40:
            taf_ring[_ic] = "#0B6B0B"
        elif w >= 30:
            taf_ring[_ic] = "#4CBB17"
        elif _ic in _has_ts:
            taf_ring[_ic] = "#F2C200"

    dest_warn = {}
    for r in metar_all:
        toks = []
        if r.get("ts_now"):
            toks.append("TS")
        if r.get("lifr"):
            if r.get("vis") is not None and r["vis"] < 1:
                toks.append(f"{r['vis']:g}SM")
            if (r.get("cig") is not None and not r.get("cig_unl")
                    and r["cig"] < 500):
                toks.append(f"CIG {int(r['cig'])}")
        if r.get("wind35") and r.get("gst"):
            toks.append(f"G{int(r['gst'])}")
        if toks:
            dest_warn[r["icao"]] = "/".join(toks)

    # Layout: TAF alerts beside the map; METARs centered below
    board_rows = build_status_board(results, metar_rows)

    _deck = None
    _fleet_n = 0
    _n_warn = 0
    _cov = ""
    _rad = ""
    _map_err = None
    try:
        import pydeck as pdk

        import pydeck as pdk

        coords = cached_station_coords(tuple(JETBLUE_ICAOS))

        # metar_all, not metar_rows: the breach-only subset hid
        # TS at stations that weren't also breaching vis/cig/wind
        # (KDEN's METAR said TS but earned no label)
        fills, rings, ts_marks = build_map_markers(
            board_rows, metar_all, coords, taf_ring
        )

        bucket1 = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        _fres = None
        try:
            _fut = st.session_state.pop("_fleet_future", None)
            if _fut is not None:
                _fres = _fut.result(timeout=60)
        except Exception:
            _fres = None
        if _fres is None:
            # Prefetch missed or failed in its thread - fall back
            # to fetching directly (slower, never empty-by-bug)
            try:
                _fres = cached_fleet(bucket1)
            except Exception:
                _fres = None
        if _fres is not None:
            (fleet, ok_tiles, n_tiles, tile_fails,
             tile_stats, rs_diag) = _fres
        else:
            fleet, ok_tiles, n_tiles = [], 0, 0
            tile_fails, tile_stats, rs_diag = [], [], []

        # No custom city layer: the light basemap already labels
        # major cities at these zooms (ours doubled them)
        layers = []
        if fills:
            # Solid core: current METAR breach (trouble NOW)
            layers.append(pdk.Layer(
                "ScatterplotLayer", data=fills,
                get_position="[lon, lat]",
                get_fill_color="color",
                get_radius=15000,
                radius_min_pixels=4, radius_max_pixels=11,
                stroked=True, get_line_color=[0, 0, 0],
                line_width_min_pixels=1, pickable=True,
            ))
        if rings:
            # Hollow ring: TAF forecast breach, concentric
            # around the METAR dot when both apply
            layers.append(pdk.Layer(
                "ScatterplotLayer", data=rings,
                get_position="[lon, lat]",
                get_line_color="color",
                get_radius=22500,
                radius_min_pixels=8, radius_max_pixels=16,
                filled=False, stroked=True,
                line_width_min_pixels=2.5, pickable=True,
            ))
        if ts_marks:
            for d in ts_marks:
                d["icon"] = _TS_TEXT_ICON
            # Calibrated by eye 8/15: (0, -20) screen px from
            # ring center
            _dx, _dy = 0, -20
            layers.append(pdk.Layer(
                "IconLayer", data=ts_marks,
                get_position="[lon, lat]",
                get_icon="icon",
                get_size=32344, size_units="meters",
                size_min_pixels=11.5, size_max_pixels=23,
                get_pixel_offset=[_dx, _dy],
                pickable=True,
            ))

        _n_warn = (sum(1 for d in fleet
                       if dest_warn.get(d.get("dest", "")))
                   if fleet else 0)
        _base_layers = layers
        _deck = True   # data phase ok; the fragment builds the deck
        _fleet_n = len(fleet)
        _cov = (f" (coverage {ok_tiles}/{n_tiles} tiles)"
                if ok_tiles < n_tiles else "")
    except Exception as e:
        _map_err = str(e)

    # Status strip: the three numbers that summarize the network
    n_sev_all = sum(1 for r in board_rows if r[0] == 0)

    @st.fragment
    def _map_fragment():
        import pydeck as pdk
        radar_mode = st.radio(
            "Radar overlay",
            ["MRMS hi-res", "Echo tops", "Off"],
            index=2, horizontal=True, key="radar_mode_f",
        )
        radar_on = radar_mode != "Off"
        show_cs = st.checkbox(
            "Show flight numbers", value=True, key="show_cs_f",
        )
        layers = []
        _mrms_ts = None
        if radar_on:
            _rb = datetime.now(timezone.utc).strftime(
                "%Y%m%d%H%M")[:-1]
            _mrms_ts = None
            if radar_mode == "MRMS hi-res":
                # NOAA ArcGIS export: 1km QC'd MRMS merged
                # reflectivity (Level-2 network mosaic) as one
                # transparent PNG - the highest-quality national
                # radar image publicly served
                _img = (
                    "https://mapservices.weather.noaa.gov/"
                    "eventdriven/rest/services/radar/"
                    "radar_base_reflectivity/MapServer/export"
                    "?bbox=-126,23,-65,50&bboxSR=4326"
                    "&imageSR=4326&size=4880,2160"
                    "&format=png32&transparent=true&f=image"
                    f"&_={_rb}"
                )
                layers.append(pdk.Layer(
                    "BitmapLayer", data=None, image=_img,
                    bounds=[-126.0, 23.0, -65.0, 50.0],
                    opacity=0.6,
                ))
                _mrms_ts = "hires"
            if radar_mode == "Echo tops":
                # Echo tops - or cells fallback - via IEM WMS,
                # now requested near the composite's native
                # resolution (was 5x undersampled at 2440px)
                _svc = ("eet" if radar_mode == "Echo tops"
                        else "n0q")
                _wms = (
                    "https://mesonet.agron.iastate.edu/"
                    f"cgi-bin/wms/nexrad/{_svc}.cgi"
                    "?SERVICE=WMS&VERSION=1.1.1"
                    "&REQUEST=GetMap"
                    f"&LAYERS=nexrad-{_svc}&STYLES="
                    "&SRS=EPSG:4326&BBOX=-126,23,-65,50"
                    "&WIDTH=4880&HEIGHT=2160"
                    "&FORMAT=image/png&TRANSPARENT=TRUE"
                    f"&_={_rb}"
                )
                layers.append(pdk.Layer(
                    "BitmapLayer", data=None, image=_wms,
                    bounds=[-126.0, 23.0, -65.0, 50.0],
                    opacity=0.6,
                ))
        if fleet:
            fleet_disp = []
            for d in fleet:
                dest = (d.get("dest") or "").upper()
                warn = dest_warn.get(dest)
                tip = d.get("tip", d.get("callsign", "?"))
                if dest:
                    tip += f" | -> {dest}"
                if warn:
                    tip += f" WARNING {warn}"
                fleet_disp.append({
                    "lon": d["lon"], "lat": d["lat"],
                    "cs": d.get("callsign", ""),
                    "tip": tip,
                    "angle": d.get("angle", 0),
                    "icon": (_AC_ICON_RED if warn else _AC_ICON),
                    "lcolor": ([224, 26, 26, 255] if warn
                               else [0, 90, 220, 255]),
                })
            layers.append(pdk.Layer(
                "IconLayer", data=fleet_disp,
                get_position="[lon, lat]",
                get_icon="icon",
                get_size=24, size_min_pixels=14,
                size_max_pixels=34,
                get_angle="angle",
                pickable=True,
            ))
            if show_cs:
                layers.append(pdk.Layer(
                    "TextLayer", data=fleet_disp,
                    get_position="[lon, lat]",
                    get_text="cs",
                    get_size=11,
                    get_color="lcolor",
                    get_text_anchor='"start"',
                    get_pixel_offset=[12, -10],
                ))

        layers.extend(_base_layers)
        deck = pdk.Deck(
            layers=layers,
            initial_view_state=pdk.ViewState(
                latitude=38.5, longitude=-96.0,
                zoom=4.3, min_zoom=4.1, max_zoom=11,
            ),
            map_style="light",
            tooltip={"html": "<b>{tip}</b>"},
        )
        _rad = ""
        if radar_on:
            if radar_mode == "MRMS hi-res":
                _rad = (" Radar: MRMS 1km merged reflectivity "
                        "(NOAA), ~2-min updates.")
            elif radar_mode == "Echo tops":
                _rad = " Radar: NEXRAD echo tops via IEM."
            else:
                _rad = (" Radar: NEXRAD reflectivity via IEM "
                        "(cells fallback).")
        st.pydeck_chart(deck, height=map_height)
        st.caption(
            "Solid dot = METAR breach NOW; ring = TAF "
            "forecast (concentric = both); orange TS above = "
            "thunderstorm. "
            "RED aircraft = "
            "destination METAR has TS / LIFR / gusts over "
            f"35kt (hover for detail). {_fleet_n} JBU "
            f"airborne{_cov}.{_rad}"
        )


    # Board + key left; METAR table with the MAP directly under
    # it right - the map fills the column, width auto-fitting
    # the window
    col_b, col_m = st.columns([1, 2.6], gap="small")
    with col_b:
        if board_rows:
            st.markdown(render_status_board(board_rows),
                        unsafe_allow_html=True)
        else:
            st.markdown(_no_alerts(), unsafe_allow_html=True)
        st.markdown(_legend_html(), unsafe_allow_html=True)
    with col_m:
        if metar_rows:
            st.markdown(render_metar_table(metar_rows),
                        unsafe_allow_html=True)
        else:
            st.markdown(
                '<div style="background:#FFFFFF; border:1px '
                'solid #000; display:inline-block; '
                'padding:4px 14px; margin-bottom:6px; '
                'color:#000; -webkit-text-fill-color:#000; '
                f'font-family:{_FONT}; font-size:11px;">'
                "NO METARs AT/BEYOND THRESHOLDS</div>",
                unsafe_allow_html=True,
            )
        if _deck is not None:
            _map_fragment()
        else:
            st.caption(f"Map unavailable: {_map_err}")

    if tile_fails:
        with st.expander(
            f"Fleet coverage report - {len(tile_fails)} tile(s) "
            f"failed all three hosts"
        ):
            for f in tile_fails:
                st.text(f)
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
        - **Thunderstorms** — TS, TSRA, +TSRA, or VCTS


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
