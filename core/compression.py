"""Compression outlook from BUFKIT soundings.

A port of a Google Apps Script macro. For each station it reads the
PSU BUFKIT file for a model (GFS or NAM), pulls the surface wind and
the winds at FL020 / FL040 / FL080 above ground for each forecast
hour, and formats them as DDDSSKT with the compression thresholds
highlighted.

WHY THIS TABLE. "Compression" is arrival spacing collapsing on final
because aircraft decelerate into a strong low-level headwind. What
matters is the CONTRAST between the winds at 2000-8000 ft and the
surface: 65 kt at FL040 over 15 kt on the ground is the profile that
turns a 5 nm gap into 3. The thresholds — bold at 50, red at 55,
magenta past 60 — are the ones the SOC forecasters already use.

The macro's logic is kept exactly, including its rounding: direction
to 10 degrees everywhere, surface speed to 1 kt, aloft speed to 5 kt.

BUFKIT LAYOUT, as this parser relies on it:

  * a SNPARM = PRES;TMPC;...;DRCT;SKNT;...;HGHT line naming the
    sounding columns
  * one block per forecast hour starting "STID = KJFK", carrying
    TIME = YYMMDD/HHMM and SELV = station elevation in METRES, then
    after BRCH = ... a flat run of numbers, len(SNPARM) per level
  * a surface section starting "STN YYMMDD/HHMM" with a fixed 23
    column layout; the data follow the "TD2M" header token

Sounding HGHT is MSL metres, so an AGL target is SELV + ft*0.3048.
Surface wind is UWND/VWND in m/s and has to be turned into a
direction and knots.
"""

from __future__ import annotations

import math
import re

BASE = {
    "GFS": "https://www.meteo.psu.edu/bufkit/data/GFS/gfs3_{id}.buf",
    "NAM": "https://www.meteo.psu.edu/bufkit/data/NAM/nam_{id}.buf",
}

SURFACE_HEADER = [
    "STN", "TIME", "PMSL", "PRES", "SKTC", "STC1", "EVAP", "P03M",
    "C03M", "SWEM", "LCLD", "MCLD", "HCLD", "UWND", "VWND", "T2MS",
    "Q2MS", "WXTS", "WXTP", "WXTZ", "WXTR", "S03M", "TD2M",
]

# Levels the table reports, in feet AGL. FL020 is read as 2,000 ft.
LEVELS_FT = (2000, 4000, 8000)

# Compression thresholds in knots, as (lower bound, style). The macro
# used bold at 50-54, red at 55-60, magenta above 60.
STYLES = ((60.01, "magenta"), (55, "red"), (50, "bold"))


def bufkit_id(station: str) -> str:
    s = station.strip().lower()
    return "k" + s if len(s) == 3 else s


def fetch(station: str, model: str, timeout: int = 20) -> str:
    import requests

    url = BASE[model.upper()].format(id=bufkit_id(station))
    r = requests.get(url, timeout=timeout,
                     headers={"User-Agent": "n90-airspace/1.0"})
    r.raise_for_status()
    return r.text


def parse(text: str) -> dict:
    """{"blocks": [...], "surface": [...]} — same shape the macro built."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = re.search(r"SNPARM\s*=\s*([A-Z0-9;]+)", text)
    if not m:
        raise ValueError("no SNPARM line — not a BUFKIT file")
    snparm = m.group(1).split(";")

    cut = text.find("STN YYMMDD/HHMM")
    sounding_text = text[:cut] if cut >= 0 else text
    surface_text = text[cut:] if cut >= 0 else ""

    blocks = []
    for bm in re.finditer(r"^STID\s*=\s*(\S+)[\s\S]*?(?=^STID\s*=|\Z)",
                          sounding_text, re.M):
        block = bm.group(0)
        meta = {}
        for k in ("STID", "TIME", "SELV"):
            km = re.search(k + r"\s*=\s*(\S+)", block)
            if km:
                meta[k] = km.group(1)
        rows = []
        after = re.search(r"BRCH\s*=\s*\S+\s*([\s\S]*)$", block)
        if after:
            nums = re.findall(r"[-+]?\d+(?:\.\d+)?", after.group(1))
            n = len(snparm)
            for i in range(0, len(nums) - n + 1, n):
                rows.append({snparm[j]: float(nums[i + j])
                             for j in range(n)})
        try:
            meta["SELV"] = float(meta.get("SELV", "nan"))
        except ValueError:
            meta["SELV"] = float("nan")
        meta["rows"] = rows
        blocks.append(meta)

    surface = []
    if surface_text:
        i = surface_text.find("TD2M")
        if i >= 0:
            tokens = surface_text[i + 4:].split()
            n = len(SURFACE_HEADER)
            for k in range(0, len(tokens) - n + 1, n):
                surface.append({SURFACE_HEADER[j]: tokens[k + j]
                                for j in range(n)})
    return {"blocks": blocks, "surface": surface}


def _time_key(bufkit_time: str):
    """'250903/1200' -> '03/12', matching the macro's DD/HH key."""
    m = re.match(r"(\d{2})(\d{2})(\d{2})/(\d{2})(\d{2})", bufkit_time)
    return f"{m.group(3)}/{m.group(4)}" if m else None


def surface_wind(parsed: dict, key: str):
    for row in parsed["surface"]:
        if _time_key(row["TIME"]) == key:
            try:
                u, v = float(row["UWND"]), float(row["VWND"])
            except ValueError:
                return None
            spd = math.hypot(u, v) * 1.94384
            d = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
            return d, spd
    return None


def _interp_dir(d1, d2, frac):
    delta = d2 - d1
    if delta > 180:
        delta -= 360
    if delta < -180:
        delta += 360
    return (d1 + frac * delta) % 360.0


def sounding_wind(parsed: dict, key: str, agl_ft: float):
    for b in parsed["blocks"]:
        if _time_key(b.get("TIME", "")) != key:
            continue
        if math.isnan(b["SELV"]):
            continue
        target = b["SELV"] + agl_ft * 0.3048
        rows = b["rows"]
        for a, c in zip(rows, rows[1:]):
            vals = [a.get("HGHT"), c.get("HGHT"), a.get("SKNT"),
                    c.get("SKNT"), a.get("DRCT"), c.get("DRCT")]
            if any(v is None or math.isnan(v) for v in vals):
                continue
            if a["HGHT"] <= target <= c["HGHT"]:
                frac = (target - a["HGHT"]) / (c["HGHT"] - a["HGHT"])
                spd = a["SKNT"] + frac * (c["SKNT"] - a["SKNT"])
                return _interp_dir(a["DRCT"], c["DRCT"], frac), spd
    return None


def _round(v, step):
    return round(v / step) * step


def fmt(direction, speed, spd_step) -> str:
    d = int(_round(direction, 10)) or 360
    s = int(_round(speed, spd_step))
    return f"{d:03d}{s:02d}KT"


def style(speed) -> str:
    for lo, name in STYLES:
        if speed >= lo:
            return name
    return ""


def forecast_keys(parsed: dict, hours: int = 24, step_h: int = 3):
    """Time keys present in the file, ascending, within `hours` of the
    first, thinned to every `step_h`. Read from the data rather than
    assumed, so a late model run still lines up."""
    from datetime import datetime

    seen = []
    for b in parsed["blocks"]:
        t = b.get("TIME", "")
        m = re.match(r"(\d{2})(\d{2})(\d{2})/(\d{2})(\d{2})", t)
        if not m:
            continue
        dt = datetime(2000 + int(m.group(1)), int(m.group(2)),
                      int(m.group(3)), int(m.group(4)), int(m.group(5)))
        seen.append((dt, _time_key(t)))
    seen.sort()
    if not seen:
        return []
    t0 = seen[0][0]
    out, last = [], None
    for dt, key in seen:
        h = (dt - t0).total_seconds() / 3600.0
        if h > hours:
            break
        if last is None or h - last >= step_h - 1e-6:
            out.append((dt, key))
            last = h
    return out


def build(stations, model: str, hours: int = 24, step_h: int = 3):
    """One table: {"model", "columns", "rows", "errors"}.

    rows: [{"label", "kind", "cells": [(text, style) | None, ...]}]
    """
    columns, rows, errors = [], [], []
    parsed_by = {}
    for stn in stations:
        try:
            parsed_by[stn] = parse(fetch(stn, model))
        except Exception as exc:
            errors.append(f"{stn}: {type(exc).__name__}: {exc}")
    if not parsed_by:
        return {"model": model, "columns": [], "rows": [],
                "errors": errors}

    # Columns come from the first station that parsed; all PSU files
    # for one model share the same cycle so this lines up.
    first = next(iter(parsed_by.values()))
    times = forecast_keys(first, hours, step_h)
    columns = [f"{dt:%d/%H}Z" for dt, _k in times]
    keys = [k for _dt, k in times]

    for stn, parsed in parsed_by.items():
        cells = []
        for k in keys:
            w = surface_wind(parsed, k)
            cells.append((fmt(w[0], w[1], 1), style(_round(w[1], 1)))
                         if w else None)
        rows.append({"label": f"{stn.upper()} SURFACE",
                     "kind": "surface", "cells": cells})
        for agl in LEVELS_FT:
            cells = []
            for k in keys:
                w = sounding_wind(parsed, k, agl)
                cells.append((fmt(w[0], w[1], 5), style(_round(w[1], 5)))
                             if w else None)
            rows.append({"label": f"FL{agl // 100:03d}",
                         "kind": "level", "cells": cells})
        rows.append({"label": "", "kind": "gap", "cells": []})
    if rows and rows[-1]["kind"] == "gap":
        rows.pop()
    return {"model": model, "columns": columns, "rows": rows,
            "errors": errors}


def to_html(table: dict, title: str = "N90 compression outlook",
            issued: str = "") -> str:
    """The sheet's look: blue header band, grey label column, a gap
    row between stations, colour by threshold. Retro-theme borders."""
    cols = table["columns"]
    col_css = {"bold": "font-weight:bold;",
               "red": "font-weight:bold;color:#FF0000;",
               "magenta": "font-weight:bold;color:#FF00FF;"}
    # BLACK TEXT, STATED. Without an explicit colour the cells inherit
    # the page theme's text colour, which is grey on the retro theme
    # and hard to read against the light cells. The red and magenta
    # threshold cells override it below.
    th = ("border:1px solid #000;padding:3px 8px;background:#D9D9D9;"
          "font-weight:bold;text-align:center;color:#000;")
    td = ("border:1px solid #000;padding:3px 8px;text-align:center;"
          "color:#000;background:#FFFFFF;")
    h = ['<table style="border-collapse:collapse;border:2px solid #000;'
         'font-family:\'Times New Roman\',serif;font-size:14px;'
         'color:#000;width:100%">']
    h.append(f'<tr><td colspan="{len(cols) + 1}" style="background:'
             f'#6FA8DC;border:1px solid #000;padding:6px 10px;'
             f'font-weight:bold;font-size:16px;text-align:center;color:#000">'
             f'{title} &mdash; {table["model"]}'
             + (f' &nbsp;&middot;&nbsp; issued {issued}' if issued
                else '') + '</td></tr>')
    h.append('<tr>' + f'<th style="{th}">DATE/TIME</th>'
             + ''.join(f'<th style="{th}">{c}</th>' for c in cols)
             + '</tr>')
    for r in table["rows"]:
        if r["kind"] == "gap":
            h.append(f'<tr><td colspan="{len(cols) + 1}" style="'
                     f'background:#D9D9D9;height:14px;border:1px solid '
                     f'#000"></td></tr>')
            continue
        lbl_bg = "#6FA8DC" if r["kind"] == "surface" else "#D9D9D9"
        h.append(f'<tr><td style="{td}background:{lbl_bg};'
                 f'font-weight:bold;text-align:left;color:#000">{r["label"]}</td>')
        for c in r["cells"]:
            if c is None:
                h.append(f'<td style="{td}"></td>')
            else:
                h.append(f'<td style="{td}{col_css.get(c[1], "")}">'
                         f'{c[0]}</td>')
        h.append('</tr>')
    h.append(f'<tr><td colspan="{len(cols) + 1}" style="border:1px solid '
             f'#000;padding:3px 8px;font-style:italic;font-size:13px">'
             'Bold black = 50&ndash;54 kt, '
             '<span style="color:#FF0000;font-weight:bold">red</span> '
             '= 55&ndash;60 kt, '
             '<span style="color:#FF00FF;font-weight:bold">magenta'
             '</span> = &gt;60 kt. Winds at FL020/040/080 are AGL, '
             'interpolated from the model sounding.</td></tr>')
    h.append('</table>')
    return ''.join(h)
