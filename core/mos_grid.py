"""Hourly guidance grid, dark, for the Station Forecast page.

Target path: core/mos_grid.py

One row per field, one column per valid hour, in the shape of the
NBH/LAMP text bulletins a dispatcher already reads. Cells are black
text on a coloured fill only when a rule fires, white on black
otherwise, so a quiet forecast is a quiet table.

Colours follow the NETWORK LADDER (core/station_status.py), the same
four the login chips use: MVFR yellow, IFR orange, LIFR pink; gusts
30+ orange, 35+ red; thunder probability 25%+ red.
"""

from __future__ import annotations

import math
from html import escape

FONT = "'Roboto Mono', 'DejaVu Sans Mono', 'Courier New', monospace"
PANEL, PANEL_HEAD, EDGE, RULE = "#0A0A0A", "#121212", "#2D3957", "#141A26"
INK, INK2, CYAN = "#FFFFFF", "#B8B8B8", "#00E5FF"
GREEN, YELLOW, ORANGE, PINK, RED = "#00FF7F", "#FFD400", "#FF8A00", "#FF00C8", "#FF3B30"


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool:
    return v in (True, 1, "1", "Y", "y", "true", "True")


# --------------------------------------------------------- rule fills

def cig_fill(c, unl=False):
    if unl or c is None:
        return None
    return PINK if c < 500 else ORANGE if c < 1000 else YELLOW if c <= 3000 else None


def vis_fill(v):
    if v is None:
        return None
    return PINK if v < 1 else ORANGE if v < 3 else YELLOW if v <= 5 else None


def gust_fill(g):
    if g is None:
        return None
    return RED if g >= 35 else ORANGE if g >= 30 else None


def wind_tier_fill(g):
    """Threshold ladder for the Wind meteogram's direction/speed/gust
    row (core/mos_grid.py:wind_text_row) — deliberately separate from
    gust_fill() above, which the NBM/LAMP GST rows already use and
    which stays put. Yellow 25-29, orange 30-34, red 35+; below 25,
    no highlight."""
    if g is None:
        return None
    return RED if g >= 35 else ORANGE if g >= 30 else YELLOW if g >= 25 else None


def prob_fill(p, threshold=25):
    return RED if p is not None and p >= threshold else None


# ------------------------------------------------------------- format

def f_int(v):
    n = _num(v)
    return "" if n is None else f"{int(round(n))}"


def f_vis(v):
    n = _num(v)
    if n is None:
        return ""
    if n >= 10:
        return "10"
    return f"{n:.1f}" if n < 3 else f"{int(round(n))}"


def f_cig(c, unl=False):
    if unl:
        return "UNL"
    n = _num(c)
    return "" if n is None else f"{int(round(n / 100))}"


def f_wdr(d):
    n = _num(d)
    return "" if n is None else f"{int(round(n / 10)) % 36:02d}"


def f_gust(g):
    n = _num(g)
    return "NG" if n is None or n < 15 else f"{int(round(n))}"


# ------------------------------------------------------ wind consensus row

_COMPASS8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass8(deg) -> str:
    n = _num(deg)
    if n is None:
        return ""
    return _COMPASS8[int(((n % 360) + 22.5) // 45) % 8]


def circular_mean_deg(degs) -> float | None:
    """Vector-mean of a list of headings (degrees), so 350 and 10
    average to 0 rather than 180. None for an empty/all-null list."""
    xs, ys = [], []
    for d in degs:
        n = _num(d)
        if n is None:
            continue
        xs.append(math.cos(math.radians(n)))
        ys.append(math.sin(math.radians(n)))
    if not xs:
        return None
    return math.degrees(math.atan2(sum(ys) / len(ys), sum(xs) / len(xs))) % 360.0


def wind_cell_text(dir_deg, spd, gust=None) -> str:
    """'093°-14kt-E' or, with a reporting model, '093°-14kt-E G28'."""
    d, s = _num(dir_deg), _num(spd)
    if d is None or s is None:
        return ""
    txt = f"{int(round(d)):03d}°-{int(round(s))}kt-{compass8(d)}"
    g = _num(gust)
    if g is not None:
        txt += f" G{int(round(g))}"
    return txt


def wind_text_row(times, dirs, spds, gusts, label: str = "WIND (avg)",
                   font_px: int = 12) -> str:
    """One-row grid: consensus direction-speed(-gust) per hour, colour
    keyed off wind_tier_fill() (gust when a model reports one that
    hour, else sustained speed)."""
    cells = []
    for d, s, g in zip(dirs, spds, gusts):
        text = wind_cell_text(d, s, g)
        key = _num(g) if _num(g) is not None else _num(s)
        cells.append((text, wind_tier_fill(key)))
    return grid(times, [(label, cells)], font_px=font_px)


def consensus_category_row(times, cig, vis, label: str = "CATEGORY (avg)",
                            font_px: int = 12) -> str:
    """One-row grid: flight category from the AVERAGED ceiling/vis
    trend across models (the same series the bold consensus line on
    the ceiling & visibility plots draws) rather than any one model."""
    cells = []
    for c, v in zip(cig, vis):
        if _num(c) is None and _num(v) is None:
            cells.append(("", None))
            continue
        cat = category(c, False, v)
        cells.append((cat, CAT_FILL[cat]))
    return grid(times, [(label, cells)], font_px=font_px)


# --------------------------------------------------------------- grid

def grid(times, rows, title: str = "", subtitle: str = "",
         font_px: int = 11) -> str:
    """HTML for one grid.

    times: valid times (datetime), one per column.
    rows:  [(label, [(text, fill_or_None), ...]), ...]
    """
    ncol = len(times)
    fs = int(font_px)
    pad_y = max(3, fs // 3)
    th = (f"background:{PANEL_HEAD};color:{CYAN};font:bold {fs}px {FONT};"
          f"padding:{pad_y + 1}px 5px;text-align:center;border:1px solid {EDGE};"
          "white-space:nowrap;")
    td = (f"font:bold {fs}px {FONT};padding:{pad_y}px 5px;text-align:center;"
          f"border:1px solid {RULE};white-space:nowrap;")
    lab = (f"background:{PANEL_HEAD};color:{INK2};font:bold {fs}px {FONT};"
           f"padding:{pad_y}px 8px;text-align:left;border:1px solid {EDGE};"
           "white-space:nowrap;")

    out = [f'<div style="overflow-x:auto;background:{PANEL};'
           f'border:1px solid {EDGE};border-radius:4px;padding:6px 8px 8px;">']
    if title:
        out.append(
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:baseline;padding:2px 4px 8px;">'
            f'<span style="color:{INK2};font:bold 11px {FONT};'
            f'letter-spacing:.5px">{escape(title.upper())}</span>'
            f'<span style="color:#6E6E6E;font:10px {FONT}">'
            f'{escape(subtitle)}</span></div>')
    out.append('<table style="border-collapse:collapse;width:100%;">')
    # header: day/hour
    out.append("<tr>" + f'<th style="{lab}">UTC</th>')
    prev_day = None
    for t in times:
        day = t.strftime("%d") if prev_day != t.day else ""
        prev_day = t.day
        out.append(f'<th style="{th}">'
                   f'<span style="color:{INK2};font-size:9px">{day}</span>'
                   f'<br>{t:%H}</th>')
    out.append("</tr>")
    for label, cells in rows:
        out.append(f'<tr><td style="{lab}">{escape(label)}</td>')
        for i in range(ncol):
            text, fill = cells[i] if i < len(cells) else ("", None)
            if fill:
                out.append(f'<td style="{td}background:{fill};color:#000000;'
                           f'-webkit-text-fill-color:#000000;">{escape(text)}</td>')
            else:
                out.append(f'<td style="{td}color:{INK};'
                           f'-webkit-text-fill-color:{INK};">{escape(text)}</td>')
        out.append("</tr>")
    out.append("</table></div>")
    return "".join(out)


def nbm_rows(df):
    """Rows for an NBM hourly frame. Expected columns: valid_time,
    temp_f, dewpoint_f, wind_dir_deg, wind_speed_kt, wind_gust_kt,
    ceiling_ft, ceiling_unlimited, vsby_sm, plus any of pop1,
    p_tstm. Missing columns simply produce a blank row."""
    def col(name):
        return df[name].tolist() if name in df.columns else [None] * len(df)

    tmp, dpt = col("temp_f"), col("dewpoint_f")
    wdr, wsp, gst = col("wind_dir_deg"), col("wind_speed_kt"), col("wind_gust_kt")
    cig, unl, vis = col("ceiling_ft"), col("ceiling_unlimited"), col("vsby_sm")
    pop = col("pop1") if "pop1" in df.columns else col("pop")
    tsp = col("p_tstm") if "p_tstm" in df.columns else col("tstm_prob")

    rows = [
        ("TMP", [(f_int(v), None) for v in tmp]),
        ("DPT", [(f_int(v), None) for v in dpt]),
        ("WDR", [(f_wdr(v), None) for v in wdr]),
        ("WSP", [(f_int(v), None) for v in wsp]),
        ("GST", [(f_gust(v), gust_fill(_num(v))) for v in gst]),
        ("CIG", [(f_cig(c, _bool(u)), cig_fill(_num(c), _bool(u)))
                 for c, u in zip(cig, unl)]),
        ("VIS", [(f_vis(v), vis_fill(_num(v))) for v in vis]),
    ]
    if any(_num(v) is not None for v in pop):
        rows.append(("P01", [(f_int(v), None) for v in pop]))
    if any(_num(v) is not None for v in tsp):
        rows.append(("T01", [(f_int(v), prob_fill(_num(v))) for v in tsp]))
    return rows


def lamp_rows(df):
    """Rows for a GFS LAMP frame: same columns as NBM where present."""
    rows = nbm_rows(df)
    # LAMP publishes gusts as WGS
    return [("WGS", c) if lab == "GST" else (lab, c) for lab, c in rows]


def legend() -> str:
    items = [(YELLOW, "MVFR"), (ORANGE, "IFR \u00b7 G30+"), (PINK, "LIFR"),
             (RED, "G35+ \u00b7 TS 25%+")]
    return ('<div style="display:flex;gap:18px;margin-top:6px">' + "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;'
        f'color:{INK2};font:10px {FONT}"><span style="width:16px;height:6px;'
        f'background:{c};display:inline-block"></span>{t}</span>'
        for c, t in items) + "</div>")


# ----------------------------------------------------- category strip

def category(cig_ft, unl, vis_sm) -> str:
    c = 1e9 if (unl or _num(cig_ft) is None) else _num(cig_ft)
    v = 99 if _num(vis_sm) is None else _num(vis_sm)
    if c < 500 or v < 1:
        return "LIFR"
    if c < 1000 or v < 3:
        return "IFR"
    if c <= 3000 or v <= 5:
        return "MVFR"
    return "VFR"


CAT_FILL = {"VFR": GREEN, "MVFR": YELLOW, "IFR": ORANGE, "LIFR": PINK}


def category_strip(df, models, times, obs=None, font_px: int = 11) -> str:
    """One row per model, one cell per valid hour, each cell printing
    its flight category on the category colour. A 3-hourly model
    leaves the hours between blank rather than pretending.

    df: the comparison frame (station_id, model, valid_time,
    ceiling_ft, ceiling_unlimited, vsby_sm). times: the columns.
    obs: optional [(time, cig, unl, vis)] for an OBS row.
    """
    import pandas as pd
    key = lambda t: pd.Timestamp(t).floor("h")
    cols = [key(t) for t in times]
    rows = []
    for m in models:
        dm = df[df["model"] == m]
        by = {}
        for _, r in dm.iterrows():
            unl = _bool(r.get("ceiling_unlimited", False))
            by[key(r["valid_time"])] = category(r.get("ceiling_ft"), unl,
                                                 r.get("vsby_sm"))
        cells = [((by[t], CAT_FILL[by[t]]) if t in by else ("", None))
                 for t in cols]
        rows.append((m.replace("_", " "), cells))
    if obs:
        by = {key(t): category(c, u, v) for t, c, u, v in obs}
        rows.append(("OBS", [((by[t], CAT_FILL[by[t]]) if t in by else ("", None))
                             for t in cols]))
    return grid(cols, rows, font_px=font_px)
