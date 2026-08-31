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

# Tell the warmer the site is in use, BEFORE any work on this page.
# It backs off while requests are arriving, so the CONUS map does not
# compete with matplotlib for the GIL. First line that runs, because
# announcing it after the slow part would be pointless.
try:
    from core.cam_warm import note_request as _note_req

    _note_req()
except Exception:
    pass

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
# N90 coordination fixes + approximate boundary
# ---------------------------------------------------------------------------
# PROVENANCE, because this one matters. The fix NAMES and which
# facility each borders come from the vice ATC simulator's ZNY
# adaptation (facility_adaptations.coordination_fixes) - community
# built from N90 facility documents, not an FAA feed. The
# COORDINATES are real: RNAV waypoints from the FAA NASR waypoint
# file, VOR/DMEs from the OurAirports navaid database. 31 of 32
# resolved; BELIT did not and is listed in the asset.
#
# The "boundary" is a CONVEX HULL of those fixes - where N90 meets
# its neighbours, which is an approximation and NOT the delegated
# TRACON boundary. It is labelled that way everywhere in the UI.
# It does pass the checks the earlier hand-made polygon failed:
# ~10,400 nm^2 (a 50 nm circle is 7,854), and all seven N90
# airports fall inside, Islip included - which Class B does not
# manage. Being a hull it cannot show concavities, so it will
# overstate the airspace in the notches.


@st.cache_data(ttl=86400, show_spinner=False)
def n90_data():
    """UNUSED on this page — the N90 fixes layer was
    removed. Kept because the loader is small and the
    asset it reads is still shipped for the N90
    Airspace page; deleting it here saves nothing."""
    """(fixes, hull_rows, meta, error) for the N90 layer."""
    try:
        import json as _json
        from pathlib import Path as _PP
        blob = _json.loads(
            (_PP(__file__).resolve().parent.parent
             / "static" / "n90_fixes.json").read_text())
        fixes = []
        for f in blob.get("fixes", []):
            # BLUE = arrival/departure gate, PURPLE = everything
            # else. The split is data-driven, not a guess: gates
            # come from the adaptation's airspace_awareness entries
            # (MERIT, GREKI, BETTE, DIXIE, SHIPP, WAVEY, HAPIE,
            # COATE, NEION, GAYEL, PARKE, BIGGY, ELIOT ...), which
            # is a DIFFERENT list from coordination_fixes. Seven
            # fixes appear in both and count as gates.
            # GREEN  departure gate  (airspace_awareness only)
            # YELLOW arrival AND departure (in both source lists)
            # WHITE  coordination fix only (not a gate)
            # The split is data-driven: vice lists gates under
            # airspace_awareness and boundary-crossing points under
            # coordination_fixes, and the seven fixes appearing in
            # both are the ones that work traffic in both directions.
            role = f.get("role")
            col = {"dep": [40, 190, 70],
                   "both": [250, 210, 40]}.get(role, [255, 255, 255])
            # Key is "tcolor", not "color": the per-datum accessor
            # that demonstrably works in this file is the callsign
            # layer's "lcolor", and a plain "color" key did not
            # take. Same convention, same result.
            fixes.append({
                "name": f["name"], "lat": f["lat"], "lon": f["lon"],
                "tcolor": col,
                "tip": (f"{f['name']} &mdash; "
                        + {"dep": "departure gate",
                           "both": "arrival + departure"}.get(
                               role, "coordination fix")
                        + (f", from {f['from']}" if f.get("from") else "")
                        + f" ({f.get('dist_nm', '?')} nm)"),
            })
        hull = blob.get("hull") or []
        rows = ([{"polygon": hull,
                  "tip": "N90 approximate extent (hull of "
                         "coordination fixes) &mdash; NOT the "
                         "delegated TRACON boundary"}]
                if len(hull) > 3 else [])
        return fixes, rows, blob.get("extracted", "?"), None
    except Exception as exc:
        return [], [], "?", f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# New York Class B shelves
# ---------------------------------------------------------------------------
# NOTE ON WHAT THIS IS: FAA Class B, NOT N90. The TRACON's delegated
# airspace is much larger (Islip sits outside Class B entirely, and
# the arrival/departure gates - CAMRN, MERIT, WAVEY - all fall well
# outside it, because gates sit at the TRACON boundary, not the
# Class B edge). Class B is the protected core: 58 nm E-W by 49 nm
# N-S. Labelled as Class B everywhere in the UI so nobody reads it
# as a TRACON boundary. Swap in real N90 geometry if it ever turns
# up; the fix symbology does not depend on this layer.
# Vintage matters too: this is a static snapshot of a third-party
# FAA mirror, not a live feed. Class B amendments are years apart,
# so staleness is low-risk, but the caption states the date.
# pathlib is imported further down as _Path; keep this self-contained
# so the helper does not depend on import order.



def ny_class_b():
    """UNUSED on this page — the Class B layer was
    removed. Kept because the loader is small and the
    asset it reads is still shipped for the N90
    Airspace page; deleting it here saves nothing."""
    """16 shelf rings, outlines only. Returns (rows, meta, error).

    Drawn as separate outlined rings rather than a merged union:
    a true union needs shapely, which is not in requirements, and
    the stacked rings render as the familiar wedding-cake anyway.
    """
    try:
        import json as _json
        from pathlib import Path as _PP
        _p = (_PP(__file__).resolve().parent.parent
              / "static" / "ny_class_b.json")
        blob = _json.loads(_p.read_text())
        rows = []
        for a in blob.get("areas", []):
            rows.append({
                "polygon": a["polygon"],
                "tip": (f"NY Class B {a['name']} &mdash; "
                        f"{a['low']} to {a['high']} ft"),
            })
        return rows, blob.get("extracted", "?"), None
    except Exception as exc:
        return [], "?", f"{type(exc).__name__}: {exc}"


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


def _fmt_windows(wins_iso):
    """First-start..last-end of a set of (start_iso, end_iso)
    windows as compact 'HH-HHZ'."""
    from datetime import datetime as _dt
    ds = []
    for s, e in wins_iso:
        try:
            ds.append((_dt.fromisoformat(s),
                       _dt.fromisoformat(e)))
        except Exception:
            continue
    if not ds:
        return ""
    lo = min(d[0] for d in ds)
    hi = max(d[1] for d in ds)
    return f"{lo:%H}-{hi:%H}Z"


def _timing_for(results):
    """{icao: compact timing string} for the board column:
    TS window (with TEMPO/PROB tag when the label carries one)
    plus the IFR span from first to last IFR-criteria mention."""
    out = {}
    ts_map = {a.icao: a for a in results.tsra_alerts}
    ifr_map = {}
    for a in results.vis_alerts:
        ifr_map.setdefault(a.icao, []).extend(
            getattr(a, "windows", ()))
    for a in results.ceiling_alerts:
        ifr_map.setdefault(a.icao, []).extend(
            getattr(a, "windows", ()))
    icaos = set(ts_map) | set(ifr_map)
    for ic in icaos:
        parts = []
        a = ts_map.get(ic)
        if a is not None:
            rng = _fmt_windows(getattr(a, "windows", ()))
            if rng:
                tag = ""
                lbl = (a.period_label or "").upper()
                for w in ("TEMPO", "PROB30", "PROB40", "BECMG"):
                    if w in lbl:
                        tag = f" {w}"
                        break
                parts.append(f"TS {rng}{tag}")
        wins = ifr_map.get(ic)
        if wins:
            rng = _fmt_windows(wins)
            if rng:
                parts.append(f"IFR {rng}")
        if parts:
            out[ic] = "; ".join(parts)
    return out


def build_status_board(results, metar_rows):
    timing = _timing_for(results)
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
                     cands,
                     timing.get(icao, "")))
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
def _a320_icon_uri(fill="#005ADC", stroke="#FFFFFF",
                   stroke_w=0.5):
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
        f'<g fill="{fill}" stroke="{stroke}" stroke-width="{stroke_w}">'
        f'<path d="{body}"/><path d="{eng_r}"/>'
        f'<path d="{eng_l}"/></g></svg>'
    )
    return ("data:image/svg+xml;charset=utf-8,"
            + urllib.parse.quote(svg))


_AC_ICON = {"url": _a320_icon_uri(), "width": 64, "height": 64,
            "anchorX": 32, "anchorY": 32, "mask": False}
_AC_ICON_GRN = {"url": _a320_icon_uri("#0E8A3E"), "width": 64,
                "height": 64, "anchorX": 32, "anchorY": 32,
                "mask": False}
_AC_ICON_PUR = {"url": _a320_icon_uri("#A020F0"), "width": 64,
                "height": 64, "anchorX": 32, "anchorY": 32,
                "mask": False}
_AC_ICON_ORG = {"url": _a320_icon_uri("#EE7700"), "width": 64,
                "height": 64, "anchorX": 32, "anchorY": 32,
                "mask": False}
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
            (plane_b, "JBU flight (mouse over for flight "
                      "info)"),
            (_a320_icon_uri("#A020F0"),
             "Destination METAR is LIFR"),
            (plane_r, "TS / &gt;G35kt in current METAR"),
            (_a320_icon_uri("#EE7700"),
             "Arriving (&lt;20 min): lightning in remarks"),
            (_a320_icon_uri("#0E8A3E"),
             "Arriving (&lt;20 min): +RA or RA &amp; vis "
             "&le;3sm"),

        )
    )
    def ring_sw(color, dot=None):
        inner = ""
        if dot:
            inner = (f'<circle cx="9" cy="9" r="3.2" '
                     f'fill="{dot}" stroke="#000000" '
                     'stroke-width="0.7"/>')
        return ('<svg width="18" height="18" '
                'style="flex:none;" '
                'xmlns="http://www.w3.org/2000/svg">'
                f'<circle cx="9" cy="9" r="6.3" fill="none" '
                f'stroke="{color}" stroke-width="2.4"/>'
                + inner + "</svg>")

    ktxt = ("color:#000; -webkit-text-fill-color:#000; "
            "font-family:Georgia, 'Times New Roman', serif; "
            "font-size:clamp(8px, 0.62vw, 11px); "
            "white-space:nowrap;")
    pair_row = ("display:flex; align-items:center; gap:5px; "
                "padding:2px 0; flex-wrap:nowrap;")

    def pair(sw1, l1, sw2, l2):
        # First column fixed-width so every second swatch starts
        # at the same x - uniform spacing across all pair rows
        return (f'<div style="{pair_row}">'
                '<span style="display:inline-flex; '
                'align-items:center; gap:5px; '
                'min-width:118px; flex:none;">'
                f'{sw1}<span style="{ktxt}">{l1}</span></span>'
                f"{sw2}"
                f'<span style="{ktxt}">{l2}</span></div>')

    rings_sec = (
        pair(ring_sw("#FF00FF"), "LIFR in TAF",
             ring_sw("#FF00FF", "#FF00FF"),
             "LIFR in TAF and METAR")
        + pair(ring_sw("#E01A1A"), "IFR in TAF",
               ring_sw("#E01A1A", "#E01A1A"),
               "IFR in TAF and METAR")
        + pair(ring_sw("#4CBB17"), "&ge;30kt in TAF",
               ring_sw("#4CBB17", "#4CBB17"),
               "&ge;30kt in TAF and METAR")
        + pair(ring_sw("#0B6B0B"), "&ge;40kt in TAF",
               ring_sw("#0B6B0B", "#0B6B0B"),
               "&ge;40kt in TAF and METAR")
        + pair(ring_sw("#F2C200"), "TS in TAF",
               ring_sw("#F2C200", "#EE7700"),
               "TS in TAF and METAR")
    )
    return (
        # width:100% + box-sizing so the MAP KEY and the alert
        # table above it are the same width in the left column
        '<div style="background:#FFFFFF; border:1px solid #000; '
        'padding:6px 10px; margin-top:26px; width:100%; '
        'box-sizing:border-box; display:block;">'
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
    ts_drive = False
    if include_ts and r.get("ts_now"):
        ts_drive = True
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
    wind_g40 = wind_g30 = False
    if r.get("wind_bad"):
        w = max([x for x in (r.get("spd"), r.get("gst"))
                 if x is not None], default=None)
        if w is not None:
            wind_g40 = w >= 40
            wind_g30 = w >= 30
            toks.append(f"{int(w)}KT")
    if not toks:
        return None, ""
    # Dot color ladder mirrors the ring ladder:
    # LIFR magenta > IFR red > G40 dk green > G30 lt green >
    # TS orange
    if tier <= 1:
        return (_MAGENTA, _RED)[tier], "/".join(toks)
    if wind_g40:
        return "#0B6B0B", "/".join(toks)
    if wind_g30:
        return "#4CBB17", "/".join(toks)
    if ts_drive:
        return "#EE7700", "/".join(toks)
    if tier <= 2:
        return _ORANGE, "/".join(toks)
    return None, ""


def build_map_markers(board_rows, metar_rows, coords,
                      taf_ring_map=None):
    """Merge TAF board + all METARs into map datasets.

    Solid dot = METAR breach now (severity color); ring = TAF at
    fixed criteria (magenta LIFR / red IFR / greens for 40 and
    30 kt wind / yellow TS); red TS text above the station when a
    thunderstorm is in the current METAR or TAF."""
    taf_ring_map = taf_ring_map or {}
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
                  + cell("ALERT 1", header=True)
                  + cell("ALERT 2", header=True)
                  + cell("WHEN", header=True) + "</tr>")
    body = []
    for (rank, icao, chip, color, period, e, all_txt, _c,
         when) in rows:
        # Split off the real candidate list rather than chopping
        # the joined string: each alert keeps its OWN severity
        # colour that way. _c is sorted worst-first, so Alert 1 is
        # always the driving hazard. Anything past the second is
        # folded into Alert 2 so nothing is silently dropped.
        _cands = list(_c or [])
        _a1 = _cands[0] if _cands else None
        _rest = _cands[1:]
        _a1_txt = _a1[1] if _a1 else all_txt
        _a1_col = _TEXT_COLOR.get(_a1[2], _a1[2]) if _a1 else color
        _a1_hot = bool(_a1) and _a1[2] in (_RED, _MAGENTA)
        _a2_txt = "/".join(c[1] for c in _rest)
        _a2_col = (_TEXT_COLOR.get(_rest[0][2], _rest[0][2])
                   if _rest else "#444444")
        _a2_hot = bool(_rest) and _rest[0][2] in (_RED, _MAGENTA)
        body.append(
            "<tr>"
            + cell(icao, bold=True)
            + cell(_a1_txt, fg=_a1_col, bold=_a1_hot)
            + cell(_a2_txt or "&mdash;", fg=_a2_col, bold=_a2_hot)
            + cell(when, fg="#444444")
            + "</tr>"
        )
    return (
        f'<table style="border-collapse:collapse; '
        f'background-color:#FFFFFF; border:2px solid {_WHITE}; '
        f'width:100%;">'
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
import threading as _threading_rc
_route_cache: dict = {}     # cs -> (dest_icao_or_"", expiry_ts)
# v2 schema: {cs: {"d": icao, "oll": [lat,lon], "dll": [lat,lon],
# "exp": ts}}. Renamed from route_cache.json so v1 entries (which
# carried no origin) are simply ignored rather than mis-parsed.
_ROUTE_CACHE_PATH = _MAP_CACHE_ROOT / "route_cache_v2.json"
try:
    import json as _json_rc
    for _k, _v in _json_rc.loads(
            _ROUTE_CACHE_PATH.read_text()).items():
        if isinstance(_v, dict):
            _route_cache[_k] = _v
except Exception:
    pass


def _route_plausible(alat, alon, trk, oll, dll):
    """Is this aircraft actually flying the route adsbdb claims?

    adsbdb returns the SCHEDULED route for a callsign from
    historical data, not today's flight. Observed 8/17: JBU759
    came back BOS-PHL while the aircraft was en route to Florida.
    That is not a cosmetic error - destination drives the red
    thunderstorm flag, so a wrong route plus a real hazard renders
    a confident, wrong warning on an ops display.

    Two independent checks against the claimed origin-destination
    great circle:
      * cross-track distance - how far off the route line the
        aircraft sits. Generous (120 nm) so reroutes, weather
        deviations and vectoring do not trip it.
      * along-track fraction - >1.25 means well past the claimed
        destination and still going.
    Falls back to bearing-vs-track when origin is unknown.

    Returns (ok, reason). Deliberately fails OPEN on bad input:
    a missing coordinate should not silently strip destinations
    from the whole fleet.
    """
    import math as _m
    R = 3440.065  # nm

    def _rad(d):
        return _m.radians(d)

    def _bearing(la1, lo1, la2, lo2):
        y = _m.sin(_rad(lo2 - lo1)) * _m.cos(_rad(la2))
        x = (_m.cos(_rad(la1)) * _m.sin(_rad(la2))
             - _m.sin(_rad(la1)) * _m.cos(_rad(la2))
             * _m.cos(_rad(lo2 - lo1)))
        return (_m.degrees(_m.atan2(y, x)) + 360.0) % 360.0

    def _dist(la1, lo1, la2, lo2):
        p1, p2 = _rad(la1), _rad(la2)
        dp, dl = _rad(la2 - la1), _rad(lo2 - lo1)
        a = (_m.sin(dp / 2) ** 2
             + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2)
        return 2 * R * _m.asin(min(1.0, _m.sqrt(a)))

    try:
        if not dll:
            return True, ""
        dla, dlo = float(dll[0]), float(dll[1])
        d_to_dest = _dist(alat, alon, dla, dlo)
        if d_to_dest < 150:
            return True, ""      # close in: vectors dominate
        if oll:
            ola, olo = float(oll[0]), float(oll[1])
            d12 = _dist(ola, olo, dla, dlo)
            if d12 < 50:
                return True, ""
            d13 = _dist(ola, olo, alat, alon)
            t13 = _rad(_bearing(ola, olo, alat, alon))
            t12 = _rad(_bearing(ola, olo, dla, dlo))
            xtd = _m.asin(_m.sin(d13 / R) * _m.sin(t13 - t12)) * R
            if abs(xtd) > 120:
                return False, f"{abs(xtd):.0f} nm off route line"
            catd = _m.cos(d13 / R) / max(1e-9, _m.cos(xtd / R))
            atd = _m.acos(max(-1.0, min(1.0, catd))) * R
            if atd / d12 > 1.25:
                return False, "past claimed destination"
            return True, ""
        # no origin: fall back to heading sanity
        if trk is None:
            return True, ""
        brg = _bearing(alat, alon, dla, dlo)
        diff = abs((brg - float(trk) + 180.0) % 360.0 - 180.0)
        if diff > 90:
            return False, f"track {diff:.0f}deg off bearing"
        return True, ""
    except Exception:
        return True, ""          # fail open


def _save_route_cache():
    try:
        import json as _json_rc
        import time as _t_rc
        now = _t_rc.time()
        keep = {k: v for k, v in _route_cache.items()
                if v.get("exp", 0) > now}
        _ROUTE_CACHE_PATH.write_text(_json_rc.dumps(keep))
    except Exception:
        pass


# Route resolution runs OFF the render path. Measured 8/17: the
# adsbdb lookups were sequential inside cached_fleet - up to 70
# callsigns x (latency + 0.05s sleep) = 18-38s that the map sat
# waiting on, every cold start and again whenever the 90s cache
# expired with new callsigns airborne. The tile sweep itself is
# only ~3s.
#
# Destinations only drive the hazard COLOUR, never aircraft
# position, so they do not belong on the critical path. The
# renderer now reads whatever routes are already cached and
# returns immediately; a daemon thread fills the rest in parallel
# and the colours appear on the next 120s beat.
_route_lock = _threading_rc.Lock()
_route_busy = {"on": False}


def _resolve_routes_bg(callsigns):
    """Fetch missing routes in the background. Never blocks a
    render; never raises into one."""
    def _work():
        import time as _t
        import requests as _r
        from concurrent.futures import ThreadPoolExecutor
        HDRS = {"User-Agent": "bluemet.org ops dashboard"}
        now_ts = _t.time()

        def _one(cs):
            try:
                r = _r.get(f"https://api.adsbdb.com/v0/callsign/{cs}",
                           headers=HDRS, timeout=5)
            except Exception:
                return cs, None
            d_icao = ""
            oll = dll = None
            if r.status_code == 200:
                try:
                    fr = (r.json().get("response") or {})
                    if isinstance(fr, dict):
                        fr = fr.get("flightroute") or {}
                        de = fr.get("destination") or {}
                        d_icao = (de.get("icao_code") or "").upper()
                        if de.get("latitude") is not None:
                            dll = [float(de["latitude"]),
                                   float(de["longitude"])]
                        og = fr.get("origin") or {}
                        if og.get("latitude") is not None:
                            oll = [float(og["latitude"]),
                                   float(og["longitude"])]
                except Exception:
                    pass
            return cs, {"d": d_icao, "oll": oll, "dll": dll,
                        "exp": now_ts + (6 * 3600 if d_icao else 3600)}
        try:
            # 8 workers: adsbdb is a lookup API, not the rate-limited
            # position feeds, so it tolerates modest concurrency.
            with ThreadPoolExecutor(max_workers=8) as ex:
                for cs, rec in ex.map(_one, callsigns[:120]):
                    if rec is not None:
                        _route_cache[cs] = rec
            _save_route_cache()
        except Exception:
            pass
        finally:
            with _route_lock:
                _route_busy["on"] = False

    with _route_lock:
        if _route_busy["on"] or not callsigns:
            return
        _route_busy["on"] = True
    t = _threading_rc.Thread(target=_work, daemon=True)
    t.start()




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


# Whether the sweep keeps non-JBU aircraft at all. Off by default:
# carrying 5,000-8,000 rows through dedupe, caching and JSON
# serialisation costs real time even when nothing draws them.
import os as _os_ko
# Other operators are no longer displayed on this page, so the sweep
# does not keep them: 5,000-8,000 rows carried through dedupe,
# caching and JSON serialisation cost real time even when nothing
# drew them. JBU_KEEP_OTHERS=on re-enables collection if the layer
# ever comes back.
KEEP_OTHERS = _os_ko.environ.get("JBU_KEEP_OTHERS", "off").lower() == "on"



# ---------------------------------------------------------------------------
# Fleet positions, refreshed off the render path
# ---------------------------------------------------------------------------
# The map fragment reruns every 120 s and cached_fleet has a 90 s
# TTL, so the cache had ALWAYS expired by the time the fragment ran —
# meaning every single beat did a full 17-tile ADS-B sweep inside the
# fragment, blocking the map for as long as it took. That is the grey
# flash on a timer.
#
# Same shape as every other external call on this page now: a
# background thread keeps the last good result, the render reads
# whatever is there. Positions are at most one beat old, which for
# aircraft on a 2-minute refresh is invisible.
_FLEET = {"res": None, "bucket": None, "busy": False,
          "err": None, "tb": None}
# How long the FIRST render may wait for positions. Only ever paid
# once per process; after that the background thread is always a beat
# ahead.
# 20 s, not 8. The sweep is 17 tiles paced across two lanes with
# retries; 8 s cut it off often enough that the map drew with no
# aircraft and waited 120 s for the next beat. Only ever paid once
# per process, and only if the module-level kick has not finished.
FIRST_WAIT_S = float(_os_ko.environ.get("JBU_FLEET_FIRST_WAIT", "20"))



# ---------------------------------------------------------------------------
# Position history
# ---------------------------------------------------------------------------
# One icon per aircraft at its CURRENT position, and a trail of dots
# behind it for where it has been. That is the honest way to show
# movement: a second icon at an old position looks like a second
# aircraft, which is exactly the confusion this replaces.
#
# History lives at module scope, so it survives fragment reruns and
# is shared across sessions — everyone looking at the map sees the
# same trails rather than each building their own.
_TRACKS: dict = {}
TRAIL_MAX = int(_os_ko.environ.get("JBU_TRAIL_POINTS", "12"))
TRAIL_TTL_S = float(_os_ko.environ.get("JBU_TRAIL_TTL_S", "3600"))


def _note_positions(rows):
    """Append current positions and drop stale aircraft."""
    import time as _t

    now = _t.time()
    for r in rows or []:
        # Fleet rows key the callsign as "callsign"; the OTHER-traffic
        # rows use "cs". This read only "cs", so every fleet row was
        # skipped and _TRACKS stayed permanently empty — which is why
        # no track line or dots ever appeared, however many fixes had
        # been collected.
        cs = (r.get("callsign") or r.get("cs") or "").strip().upper()
        la, lo = r.get("lat"), r.get("lon")
        if not cs or la is None or lo is None:
            continue
        h = _TRACKS.setdefault(cs, [])
        # Only record real movement. Without this, a parked aircraft
        # accumulates a dozen dots in one spot and reads as a smudge.
        if h and abs(h[-1][0] - lo) < 0.002 and abs(h[-1][1] - la) < 0.002:
            h[-1] = (lo, la, now)
            continue
        h.append((lo, la, now))
        if len(h) > TRAIL_MAX:
            del h[:-TRAIL_MAX]
    # An aircraft that lands or leaves coverage should not leave a
    # trail hanging on the map forever.
    for cs in [k for k, v in _TRACKS.items()
               if not v or now - v[-1][2] > TRAIL_TTL_S]:
        _TRACKS.pop(cs, None)


def _trail_paths():
    """One polyline per aircraft, oldest fix to current position.

    A continuous thin line rather than dashes. The dashed version
    built segment geometry by hand — deck.gl cannot dash a PathLayer
    without an extension pydeck cannot serialise — and at typical fix
    spacing the dashes read as a broken line rather than a track.
    """
    out = []
    for cs, h in _TRACKS.items():
        if len(h) < 2:
            continue
        out.append({"path": [[lo, la] for lo, la, _ts in h],
                    "cs": cs})
    return out


def _trail_dots():
    """A dot at every PREVIOUS fix.

    The newest is excluded: that is where the aircraft icon sits, and
    a dot beneath it only thickens the symbol.
    """
    out = []
    for cs, h in _TRACKS.items():
        for lo, la, _ts in h[:-1]:
            out.append({"position": [lo, la], "cs": cs})
    return out


def _fleet_refresh(bucket: str):
    if _FLEET["busy"]:
        return
    _FLEET["busy"] = True
    try:
        res = cached_fleet(bucket)
        _FLEET.update({"res": res, "bucket": bucket,
                       "err": None, "tb": None})
        try:
            _note_positions(res[0] if res else [])
        except Exception:
            pass          # trails are decoration; never break the fleet
    except Exception as exc:
        import traceback as _tbf
        _FLEET.update({"err": f"{type(exc).__name__}: {exc}",
                       "tb": _tbf.format_exc()})
    finally:
        _FLEET["busy"] = False


def _kick_fleet_now():
    """Start a sweep at MODULE LOAD, before anything renders.

    The refresher used to start on the first fragment run, so the
    first render raced it: the sweep paces 17 tiles across two lanes
    with 0.25 s between calls plus retries on rate limits, which
    regularly beat the wait. When it did, the map drew with an EMPTY
    fleet and the next attempt was 120 s away — aircraft simply
    missing for two minutes.
    """
    import threading as _th
    from datetime import datetime as _d, timezone as _tz

    b = _d.now(_tz.utc).strftime("%Y%m%d%H%M")
    if _FLEET["bucket"] != b and not _FLEET["busy"]:
        _th.Thread(target=_fleet_refresh, args=(b,),
                   daemon=True).start()


def fleet_now(bucket: str):
    """(result, err, tb). Kicks a refresh if stale.

    Blocks ONLY on the very first call of the process, and only up
    to FIRST_WAIT_S — otherwise the first person through the door
    gets an empty map, which is worse than a short wait. Every
    subsequent call returns immediately with the last good result.
    """
    import threading as _th
    import time as _t

    if _FLEET["bucket"] != bucket and not _FLEET["busy"]:
        _th.Thread(target=_fleet_refresh, args=(bucket,),
                   daemon=True).start()
    if _FLEET["res"] is None:
        deadline = _t.time() + FIRST_WAIT_S
        while _FLEET["res"] is None and _FLEET["err"] is None \
                and _t.time() < deadline:
            _t.sleep(0.25)
    return _FLEET["res"], _FLEET["err"], _FLEET["tb"]


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
            # Strip AND upper-case. ADS-B callsigns are space
            # padded to eight characters and hosts differ on whether
            # they trim them, so "JBU2582 " and "JBU2582" arrive from
            # different tiles as different strings that display
            # identically. Normalising at the source is what stops
            # the same aircraft being counted twice everywhere
            # downstream.
            cs = (p.get("flight") or "").strip().upper()
            # Other operators are kept now rather than dropped. The
            # sweep already paid for them — they arrive in the same
            # payload — so carrying them costs nothing but a tag, and
            # a JBU aircraft is far easier to read when you can see
            # the traffic it is flying among.
            mine = cs.upper().startswith("JBU")
            if p.get("lat") is None:
                continue
            if not mine and not KEEP_OTHERS:
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
                            gs, (int, float)) else None,
                        mine))
        tile_stats.append(
            f"({la:.0f},{lo:.0f}) {host}: {len(ac)} ac, "
            f"{sum(1 for r in out if r[6])} JBU"
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
            for tile in empties[h][:6]:   # bounded: a fully
                # defective host used to add 17 serial retries
                # (~11s) to every cold start
                _time.sleep(0.25)
                res, err = _call(other, tile)
                if res is not None and err != "EMPTY200":
                    results.append(res)

    # Stragglers: one paced retry on the OTHER host
    fails = []
    for h in hosts:
        other = hosts[1] if h == hosts[0] else hosts[0]
        for tile, err1 in leftovers[h][:6]:
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
    others = {}
    for res in results:
        for cs, la, lo, alt, trk, gs, mine in res:
            tgt = seen if mine else others
            if cs not in tgt and cs not in seen:
                tgt[cs] = (la, lo, alt, trk)
    # Route lookups and the hazard ladder run on JBU only: `seen` is
    # the fleet. `others` is context and never drives a colour, an
    # alert or an API call — it is drawn and nothing more. That keeps
    # the adsbdb load unchanged and stops a hundred foreign callsigns
    # entering the route cache.

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
              or _route_cache[cs].get("exp", 0) < now_ts]
    # Non-blocking: read what is cached, kick the rest to a
    # background thread, return now.
    _resolve_routes_bg(new_cs)
    _suppressed = []
    for cs in seen:
        cached = _route_cache.get(cs) or {}
        if not cached.get("d"):
            continue
        _la, _lo, _alt, _trk = seen[cs]
        ok, why = _route_plausible(_la, _lo, _trk,
                                   cached.get("oll"),
                                   cached.get("dll"))
        if ok:
            dests[cs] = cached["d"]
        else:
            # Drop the destination entirely rather than colour on
            # it. No flag beats a confidently wrong flag.
            _suppressed.append(f"{cs}->{cached['d']} ({why})")
    if _suppressed:
        rs_diag.append(
            "route sanity: dropped "
            + "; ".join(_suppressed[:8])
            + (f" (+{len(_suppressed) - 8} more)"
               if len(_suppressed) > 8 else ""))
    rs_diag.append(
        f"adsbdb: {len(new_cs)} routes resolving in background, "
        f"cache holds "
        f"{sum(1 for v in _route_cache.values() if v.get('d'))} "
        f"routes (destinations appear on the next beat)"
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
            "alt": alt,
            "tip": f"{cs} | {alt_s}",
        })
    ok = len(_FLEET_TILES) - len(fails)
    out_other = [{"cs": cs, "lat": v[0], "lon": v[1],
                  "alt": v[2], "trk": v[3]}
                 for cs, v in others.items()]
    return (out, ok, len(_FLEET_TILES), fails, tile_stats, rs_diag,
            out_other)

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
# Prefetch overlap: start the paced fetchers (fleet sweep,
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
# Fleet sweep starts HERE, at module load, so it overlaps page
# setup instead of racing the first render.
try:
    _kick_fleet_now()
except Exception:
    pass

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
        "Map height (px)", 450, 1400, 1000, 50,
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
        # 12 s, not 45. These are PREFETCH threads — if one is slow
        # the code below falls back to fetching directly, so a long
        # timeout does not make the page more correct, only slower.
        # Two of these back to back could hold the page for 105 s,
        # which is most of the "takes a minute to load".
        metar_rows = _metar_fut.result(timeout=12)
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

    # {icao: [(start_dt, end_dt), ...]} TS windows from TAFs,
    # for the near-arrival orange check
    dest_ts_windows = {}
    for a in results.tsra_alerts:
        wins = []
        for s, e in getattr(a, "windows", ()):
            try:
                wins.append((datetime.fromisoformat(s),
                             datetime.fromisoformat(e)))
            except Exception:
                continue
        if wins:
            dest_ts_windows[a.icao] = wins

    dest_warn = {}
    def _ltg_cb_hazard(rmk: str):
        """Lightning/CB in remarks, movement-aware. Non-distant
        groups always trigger; DSNT groups trigger only when a
        MOV direction points back toward the station (within 45
        degrees of the location's reciprocal). Returns the
        verbatim remark segment, or None."""
        import re as _r2
        _AZ = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180,
               "SW": 225, "W": 270, "NW": 315}
        toks = rmk.split()
        for i, t in enumerate(toks):
            if "LTG" not in t and t != "CB":
                continue
            j = i + 1
            seg = [t]
            if i > 0 and toks[i - 1] in ("OCNL", "FRQ", "CONS"):
                seg.insert(0, toks[i - 1])
            dsnt = False
            locs, mov = [], None
            while j < len(toks) and len(seg) < 7:
                u = toks[j]
                if u == "DSNT":
                    dsnt = True
                elif u == "MOV" and j + 1 < len(toks):
                    mov = toks[j + 1]
                    seg.append(u)
                    seg.append(mov)
                    j += 2
                    continue
                elif _r2.fullmatch(r"[NSEW]{1,2}(-[NSEW]{1,2})*",
                                   u) or u in ("ALQDS", "OHD",
                                               "VC"):
                    locs.append(u)
                else:
                    break
                seg.append(u)
                j += 1
            if not dsnt:
                return " ".join(seg)
            # Distant-but-ALQDS: lightning in all quadrants
            # implies airmass storms building around the field
            if "ALQDS" in locs:
                return " ".join(seg)
            if mov and mov in _AZ:
                for lo in locs:
                    for p in lo.split("-"):
                        if p in _AZ:
                            recip = (_AZ[p] + 180) % 360
                            diff = abs(_AZ[mov] - recip)
                            if min(diff, 360 - diff) <= 45:
                                return " ".join(seg)
        return None

    # Arrival-hazard lookups from current METARs:
    #  - rain: heavy rain, or any rain with vis <= 3 sm
    #  - ltg: any lightning group in the remarks (LTG covers
    #    FRQ/OCNL/CONS prefixes, DSNT, and the CA/CG/IC/CC
    #    type suffixes per the ASOS remarks format)
    import re as _re
    dest_rain = {}
    dest_ltg = {}
    for r in metar_all:
        raw = r.get("raw") or ""
        body, _, rmk = raw.partition(" RMK")
        heavy = bool(_re.search(
            r"(?:^|\s)\+(?:FZ|SH|DZ|TS)?RA", body))
        anyra = bool(_re.search(
            r"(?:^|\s)[+-]?(?:FZ|SH|DZ|TS)?RA(?:[A-Z]{2})?"
            r"(?:\s|$)", body))
        visv = r.get("vis")
        # Remarks-side heavy rain: the hourly precip group
        # (Prrrr, hundredths of an inch) at >= 0.30"/hr is a
        # heavy-rate hour even when the body says plain RA
        p_amt = None
        _pm = _re.search(r"(?:^|\s)P(\d{4})(?:\s|$)", rmk)
        if _pm:
            p_amt = int(_pm.group(1)) / 100.0
        heavy_rmk = (anyra and p_amt is not None
                     and p_amt >= 0.30)
        if heavy or heavy_rmk or (anyra and visv is not None
                                  and visv <= 3):
            dest_rain[r["icao"]] = (
                "+RA" if heavy
                else f"RA P{p_amt:.2f}\"/hr" if heavy_rmk
                else f"RA/{visv:g}SM")
        if rmk:
            hit = _ltg_cb_hazard(rmk)
            if hit:
                dest_ltg[r["icao"]] = hit

    dest_lifr = {}
    for r in metar_all:
        toks = []
        lifr_here = False
        if r.get("ts_now"):
            toks.append("TS")
        if r.get("lifr"):
            lifr_here = True
            if r.get("vis") is not None and r["vis"] < 1:
                toks.append(f"{r['vis']:g}SM")
            if (r.get("cig") is not None and not r.get("cig_unl")
                    and r["cig"] < 500):
                toks.append(f"CIG {int(r['cig'])}")
        if r.get("wind35") and r.get("gst"):
            toks.append(f"G{int(r['gst'])}")
        if toks:
            dest_warn[r["icao"]] = "/".join(toks)
            if lifr_here:
                dest_lifr[r["icao"]] = True

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
                _fres = _fut.result(timeout=20)
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
             tile_stats, rs_diag, fleet_other) = _fres
        else:
            fleet, ok_tiles, n_tiles = [], 0, 0
            tile_fails, tile_stats, rs_diag = [], [], []
            fleet_other = []

        # No custom city layer: the light basemap already labels
        # major cities at these zooms (ours doubled them)
        # Controls FIRST. This row used to sit ~130 lines below the
        # layer list, so `radar_on` was read before it existed —
        # "name 'radar_on' is not defined". A control has to be
        # declared before anything reads it.
        # Two controls: flight numbers and radar. The third column
        # is a spacer so neither stretches across the page.
        _ctl = st.columns([1.0, 1.0, 4.0], gap="small")
        with _ctl[0]:
            show_cs = st.checkbox(
                "Flight numbers", value=True, key="show_cs_f",
            )
        with _ctl[1]:
            # Reads a PRE-WARMED frame off disk. No fetch, no
            # decode, no wait — toggling this costs a layer append.
            radar_on = st.checkbox(
                "Radar", value=True, key="mrms_on",
                help="MRMS merged reflectivity, 1 km national "
                     "mosaic, ~2-minute updates.",
            )
        # FIXED opacity, no widget.
        #
        # pydeck cannot change a layer property client-side, so every
        # opacity control rebuilt the whole deck — a slider did it on
        # each drag step, a radio once per click. Opacity is a
        # set-once preference rather than something adjusted while
        # watching weather, so it is a constant here and an env var
        # for tuning without a deploy.
        # 1.0 here: transparency is baked into the palette alpha in
        # core.mrms (MRMS_ALPHA). Setting it in both places would
        # multiply them and wash the radar out entirely.
        radar_op = 1.0
        layers = []
        if radar_on:
            # One BitmapLayer per CHUNK.
            #
            # A single 7000 px CONUS image exceeds WebGL's
            # MAX_TEXTURE_SIZE, which is 4096 on a lot of integrated
            # graphics. Over the cap the texture silently fails to
            # upload and the layer draws nothing — no error, caption
            # still says the frame loaded. That is what "radar shows
            # nothing" was.
            #
            # A TileLayer would be the textbook answer, but deck.gl
            # needs a renderSubLayers CALLBACK to draw raster tiles
            # and pydeck cannot serialise a JS function; adding one
            # kills the whole deck. A handful of BitmapLayers is the
            # same idea expressed in JSON.
            try:
                from core import mrms as _MR

                _sd = _Path(__file__).resolve().parent.parent / "static"
                _rbase = (_os_ko.environ.get("RENDER_EXTERNAL_URL")
                          or _os_ko.environ.get("PUBLIC_BASE_URL")
                          or "").rstrip("/")
                _chunks, _rs = _MR.newest(_sd)
                if _chunks and _rbase:
                    for _c in _chunks:
                        layers.append(pdk.Layer(
                            "BitmapLayer", data=None,
                            image=f"{_rbase}/app/static/{_c['name']}",
                            bounds=_c["bounds"],
                            opacity=float(radar_op),
                        ))
                    _radar_note = (
                        f" Radar: MRMS 1 km merged reflectivity"
                        + (f", {_rs[9:11]}:{_rs[11:13]}Z." if _rs
                           else ".")
                    )
                elif not _rbase:
                    _radar_note = " Radar: RENDER_EXTERNAL_URL unset."
                else:
                    # Highly visible, because a blank radar layer is
                    # otherwise indistinguishable from clear skies —
                    # the single most dangerous ambiguity on this
                    # page. Say plainly that data is missing rather
                    # than letting an empty map imply "no weather".
                    st.markdown(
                        "<div style='background:#FFF3B0;"
                        "border:2px solid #B38600;border-radius:6px;"
                        "padding:10px 14px;margin:6px 0;"
                        "text-align:center;font-size:20px;"
                        "font-weight:700;color:#5A4300;'>"
                        "MRMS radar rendering\u2026 "
                        "<span style='font-size:15px;font-weight:400'>"
                        "first national scan takes a few minutes "
                        "after a restart. The map is NOT showing "
                        "radar right now.</span></div>",
                        unsafe_allow_html=True)
                    _radar_note = (" Radar: warming, first scan "
                                   "appears within a few minutes.")
            except Exception as _rexc:
                st.markdown(
                    "<div style='background:#FFD9D9;"
                    "border:2px solid #B30000;border-radius:6px;"
                    "padding:10px 14px;margin:6px 0;"
                    "text-align:center;font-size:19px;"
                    "font-weight:700;color:#7A0000;'>"
                    f"Radar unavailable \u2014 {_rexc}</div>",
                    unsafe_allow_html=True)
                _radar_note = f" Radar unavailable ({_rexc})."
        else:
            _radar_note = ""

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

    @st.fragment(run_every="120s")
    def _map_fragment_outer():
        """Owns its own container.

        THIS is why aircraft accumulated into chains along their
        flight paths, one set per refresh.

        The fragment was called from inside `with col_m:` — a column
        created OUTSIDE it. On a run_every rerun Streamlit
        re-executes only the fragment body, and writes went into that
        pre-existing container, APPENDING rather than replacing. Every
        beat added another complete set of icons at the then-current
        positions, which is exactly the chain pattern: same spacing
        as the refresh interval, following each track.

        A container created INSIDE the fragment is cleared and
        rebuilt on every fragment run, because it belongs to the
        fragment rather than to the page around it.
        """
        with st.container():
            _map_fragment()

    def _map_fragment():
        # Bound at the TOP OF THE FUNCTION, not outside it.
        #
        # `_n_dupe` is assigned further down inside `if fleet:`,
        # which makes it LOCAL to this function for its whole body.
        # An initialisation in the enclosing scope is therefore
        # invisible here, and reading it when `if fleet:` did not run
        # raises UnboundLocalError rather than falling back to the
        # outer value. Same trap applies to anything else the caption
        # reads unconditionally.
        _n_dupe = 0
        # Bound here, not inside `if fleet:` — the caption reads it
        # unconditionally, and a name assigned only inside a branch
        # is local to the whole function, so the outer value would
        # be invisible.
        _seen_cs = {}
        _dup_dots = []
        import pydeck as pdk
        # ONE control left on this page: flight numbers. Radar,
        # Class B, other traffic and N90 fixes have all been removed
        # — the value here is the fleet against station conditions
        # and TAF breaches, and every one of those layers was either
        # better served elsewhere or cost an external fetch on the
        # render path.
        #
        # A single st.columns([2.4]) with `with _ctl[1]:` was left
        # behind by that removal and raised IndexError on every load:
        # one column, index 1. Narrow column so the checkbox does not
        # stretch across the page.
        # Live positions from the background refresher. The fragment
        # NEVER waits on the sweep — it draws the last good result
        # and a fresh one arrives for the next beat.
        _fb = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        # fleet MUST be bound before the try. It was not, so any
        # exception inside cached_fleet was swallowed by a bare
        # except and surfaced three lines down as
        # "UnboundLocalError: fleet" - which names the symptom and
        # hides the cause. Seen live 8/17.
        fleet = []
        fleet_other = []
        _fleet_err = _fleet_tb = None
        _lres, _fleet_err, _fleet_tb = fleet_now(_fb)
        if _lres is not None:
            fleet = _lres[0]
            fleet_other = _lres[6] if len(_lres) > 6 else []
        if _fleet_err:
            st.warning(
                f"Fleet positions unavailable - {_fleet_err}. "
                "Map still shows stations, radar and airspace."
            )
            with st.expander("Fleet fetch traceback"):
                st.code(_fleet_tb or "", language="text")
        if not fleet and not _fleet_err:
            # An empty fleet layer is indistinguishable from "no
            # aircraft airborne", which is never true. Say so.
            st.markdown(
                "<div style='background:#FFF3B0;"
                "border:2px solid #B38600;border-radius:6px;"
                "padding:8px 12px;margin:6px 0;text-align:center;"
                "font-size:18px;font-weight:700;color:#5A4300;'>"
                "Aircraft positions loading\u2026 "
                "<span style='font-size:14px;font-weight:400'>"
                "the ADS-B sweep is still running. Traffic will "
                "appear on the next refresh.</span></div>",
                unsafe_allow_html=True)
        if fleet:
            fleet_disp = []
            gnd_disp = []
            _entries = []
            for d in fleet:
                dest = (d.get("dest") or "").upper()
                warn = dest_warn.get(dest)
                tip = d.get("tip", d.get("callsign", "?"))
                if dest:
                    tip += f" | -> {dest}"
                if warn:
                    tip += f" WARNING {warn}"
                # Near-arrival tiers (within ~20 min of the
                # field, groundspeed-derived):
                #  ORANGE - lightning in destination remarks
                #  GREEN  - heavy rain, or rain with vis <= 3sm
                # Purple/red (LIFR / TS-G35) outrank both.
                ltg_arr = False
                rain_arr = False
                if (not warn and dest in coords
                        and (dest in dest_ltg
                             or dest in dest_rain)):
                    import math as _m
                    _gs = d.get("gs") or 0
                    if _gs and _gs > 60:
                        la1, lo1 = d["lat"], d["lon"]
                        la2, lo2 = coords[dest]
                        _dnm = 3440.1 * _m.acos(min(1.0, max(
                            -1.0,
                            _m.sin(_m.radians(la1))
                            * _m.sin(_m.radians(la2))
                            + _m.cos(_m.radians(la1))
                            * _m.cos(_m.radians(la2))
                            * _m.cos(_m.radians(lo2 - lo1)))))
                        if _dnm / _gs * 60 + 6 <= 20:
                            if dest in dest_ltg:
                                ltg_arr = True
                                tip += (" | ARRIVING - RMK: "
                                        + dest_ltg[dest])
                            elif dest in dest_rain:
                                rain_arr = True
                                tip += (" | ARRIVING: "
                                        + dest_rain[dest])
                _alt = d.get("alt")
                _gsv = d.get("gs") or 0
                _ground = ((_alt is None or _alt < 1500)
                           and _gsv < 60)
                _entries.append(({
                    "lon": d["lon"], "lat": d["lat"],
                    "cs": d.get("callsign", ""),
                    "tip": tip,
                    "angle": d.get("angle", 0),
                    "icon": (_AC_ICON_PUR
                             if dest_lifr.get(dest)
                             else _AC_ICON_RED if warn
                             else _AC_ICON_ORG if ltg_arr
                             else _AC_ICON_GRN if rain_arr
                             else _AC_ICON),
                    "lcolor": ([160, 32, 240, 255]
                               if dest_lifr.get(dest)
                               else [224, 26, 26, 255] if warn
                               else [238, 119, 0, 255]
                               if ltg_arr
                               else [14, 138, 62, 255]
                               if rain_arr
                               else [0, 90, 220, 255]),
                }, bool(warn), (ltg_arr or rain_arr), _ground,
                    _alt or 0))
            # Hub-cluster thinning: within 12nm of any JBU
            # airport, only the 2 highest-priority aircraft stay
            # always-visible (warning colors never thinned
            # below the cap); the rest join the ground traffic
            # on the meter-sized zoom-in layer.
            import math as _m2
            _apts = list(coords.values())

            def _near_apt(e):
                la, lo = e[0]["lat"], e[0]["lon"]
                best, bi = 99.0, -1
                for i, (ala, alo) in enumerate(_apts):
                    dx = (lo - alo) * _m2.cos(
                        _m2.radians((la + ala) / 2))
                    dnm = 60.0 * _m2.hypot(la - ala, dx)
                    if dnm < best:
                        best, bi = dnm, i
                return bi if best <= 12.0 else -1

            _clusters = {}
            for e in _entries:
                if e[3]:                     # ground -> recede
                    gnd_disp.append(e[0])
                    continue
                k = _near_apt(e)
                if k < 0:
                    fleet_disp.append(e[0])  # enroute: always
                else:
                    _clusters.setdefault(k, []).append(e)
            for k, group in _clusters.items():
                group.sort(key=lambda e: (not e[1], not e[2],
                                          -e[4]))
                for n, e in enumerate(group):
                    (fleet_disp if n < 2
                     else gnd_disp).append(e[0])

            # Defensive dedupe by callsign. The sweep already keys
            # on callsign, so this should be a no-op — but a
            # duplicate icon is indistinguishable from a second
            # aircraft to whoever is reading the map, and the cost
            # of being sure is one dict comprehension.
            # ONE ICON PER FLIGHT NUMBER. Anything beyond the first
            # becomes a blue dot.
            #
            # The key is NORMALISED — stripped and upper-cased —
            # because ADS-B callsigns are space-padded to eight
            # characters. One host returns "JBU2582 " and another
            # "JBU2582"; they are different dict keys and render
            # identically, which is why every earlier dedupe passed
            # them straight through.
            #
            # Extras are not discarded. They are real observations of
            # the same aircraft at other positions, so they are drawn
            # as dots — the same treatment as a track fix, which is
            # what they effectively are.
            def _norm(cs):
                return (cs or "").strip().upper()

            _n_before = len(fleet_disp) + len(gnd_disp)
            _seen_cs = {}
            _f2, _g2 = [], []
            _dup_dots = []
            for _tier, _src in ((_f2, fleet_disp), (_g2, gnd_disp)):
                for _r in _src:
                    _k = _norm(_r.get("cs"))
                    if not _k:
                        _tier.append(_r)      # unlabelled: keep
                        continue
                    if _k in _seen_cs:
                        _lo, _la = _r.get("lon"), _r.get("lat")
                        if _lo is not None and _la is not None:
                            _dup_dots.append(
                                {"position": [_lo, _la], "cs": _k})
                        continue
                    _seen_cs[_k] = True
                    _tier.append(_r)
            fleet_disp, gnd_disp = _f2, _g2
            _n_dupe = _n_before - len(fleet_disp) - len(gnd_disp)

            # TRACK TRAILS, under the icons. Previous positions as
            # small dots; the icon marks only where the aircraft is
            # NOW. Drawn first so a trail never sits on top of an
            # aircraft symbol.
            #
            # Radius in METERS, not pixels: a trail should shrink as
            # you zoom out, the way the track itself does, rather
            # than staying a constant blob and swamping the map at
            # continental zoom.
            try:
                _trail = _trail_paths()
                _tdots = _trail_dots()
            except Exception:
                _trail, _tdots = [], []
            if _trail:
                layers.append(pdk.Layer(
                    "PathLayer", data=_trail,
                    get_path="path",
                    get_color=[0, 110, 240, 160],
                    get_width=1.4, width_units="pixels",
                    width_min_pixels=1, width_max_pixels=2,
                    pickable=False,
                ))
            # Extra positions for a flight that appeared more than
            # once: same visual language as a track fix.
            if _dup_dots:
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=_dup_dots,
                    get_position="position",
                    get_radius=2.6, radius_units="pixels",
                    radius_min_pixels=2, radius_max_pixels=4,
                    get_fill_color=[0, 90, 220, 210],
                    stroked=False, pickable=False,
                ))
            if _tdots:
                # Radius in PIXELS so dots stay legible at
                # continental zoom instead of shrinking away. The
                # line carries the shape; the dots show the sampling.
                layers.append(pdk.Layer(
                    "ScatterplotLayer", data=_tdots,
                    get_position="position",
                    get_radius=2.2, radius_units="pixels",
                    radius_min_pixels=2, radius_max_pixels=3,
                    get_fill_color=[0, 90, 220, 200],
                    stroked=False, pickable=False,
                ))

            layers.append(pdk.Layer(
                "IconLayer", data=fleet_disp,
                get_position="[lon, lat]",
                get_icon="icon",
                get_size=24, size_min_pixels=14,
                size_max_pixels=34,
                get_angle="angle",
                pickable=True,
            ))
            if gnd_disp:
                layers.append(pdk.Layer(
                    "IconLayer", data=gnd_disp,
                    get_position="[lon, lat]",
                    get_icon="icon",
                    get_size=850, size_units="meters",
                    size_max_pixels=26,
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
        # Reported ALWAYS, not only when nonzero. If the counts
        # agree and aircraft still appear twice on screen, the
        # duplication is in the RENDERING, not the data — and that
        # distinction is the thing worth knowing.
        # LOUD check. If the number of icons drawn does not equal
        # the number of distinct flight numbers, something upstream
        # is still producing duplicates and the map is lying about
        # how many aircraft are airborne. Say it in red rather than
        # burying it in a caption.
        try:
            _icons = len(fleet_disp) + len(gnd_disp)
        except Exception:
            _icons = 0
        if _seen_cs and _icons != len(_seen_cs):
            st.markdown(
                "<div style='background:#FFD9D9;border:2px solid "
                "#B30000;border-radius:6px;padding:8px 12px;"
                "margin:6px 0;text-align:center;font-size:17px;"
                "font-weight:700;color:#7A0000;'>"
                f"{_icons} icons for {len(_seen_cs)} flight numbers "
                "\u2014 duplicates are NOT being removed.</div>",
                unsafe_allow_html=True)
        _rad_dupe = (
            f" {len(_seen_cs)} aircraft"
            + (f", {_n_dupe} duplicate rows removed." if _n_dupe
               else ".")
        ) if _seen_cs else ""
        _rad = (_rad_dupe + _radar_note
                + " Map auto-refreshes every 2 min; aircraft "
                  "positions update each refresh.")
        # Claimed before the chart so it paints while deck.gl builds.
        # Only on the FIRST render of a session. A control toggle
        # rebuilds the deck from cached data in a moment, and
        # flashing "rendering" over it turns a brief redraw into a
        # visible interruption. The notice exists for the cold open,
        # where the wait is real.
        _map_notice = st.empty()
        if not st.session_state.get("_map_drawn_once"):
            _map_notice.markdown(
                "<p style='text-align:center;font-size:22px;"
                "font-weight:700;margin:10px 0'>"
                "Flight map rendering\u2026</p>",
                unsafe_allow_html=True)
        # STABLE KEY. Without one, a fragment rerun can create a NEW
        # chart element rather than replacing the existing one, and
        # the previous render stays on screen underneath — which is
        # why aircraft appeared twice, at their old and new
        # positions, the longer the page stayed open.
        # Plain call, positional identity.
        #
        # A previous attempt stored st.empty() in session_state and
        # wrote into it on later runs. That does not work: a
        # DeltaGenerator captured in one script run points at a
        # container that no longer exists on the next, so the write
        # silently goes nowhere and the map never appears. Streamlit
        # elements are identified by POSITION in the script, so
        # calling pydeck_chart at the same point each run already
        # replaces the previous chart.
        st.pydeck_chart(deck, height=map_height)
        _map_notice.empty()
        st.session_state["_map_drawn_once"] = True
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
            _map_fragment_outer()
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
