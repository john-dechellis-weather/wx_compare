"""METAR / TAF text with the site's hazard colouring.

Target path: core/wxtext.py

Shared by Station Forecast and Station Quick View. Rules, as on the
Quick View page: vis <1 SM magenta, <3 red; ceiling <500 magenta,
<1000 red, <2000 yellow; gusts G30+ orange, G35+ red, G40+ magenta;
thunder red. The box is black with the site's #2D3957 border.
"""

from __future__ import annotations

_MAGENTA, _RED, _YELLOW, _ORANGE = (
    "#FF00FF", "#FF4040", "#FFFF00", "#FF9900",
)
BORDER = "#2D3957"

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


def _reindent_taf(lines: list) -> list:
    """Normalize TAF hierarchy: header flush left, FM/BECMG groups
    at two spaces, TEMPO/PROB/INTER overlays one space deeper (they
    modify the group above), other continuations deepest."""
    out = []
    for i, raw in enumerate(lines):
        t = raw.strip()
        if not t:
            out.append("")
        elif i == 0 or t.startswith("TAF"):
            out.append(t)
        elif t.startswith(("FM", "BECMG")):
            out.append("  " + t)
        elif t.startswith(("TEMPO", "PROB", "INTER")):
            out.append("   " + t)
        else:
            out.append("      " + t)
    return out


def wx_colored_box(lines: list, taf_mode: bool = False, font_px: int = 13) -> str:
    """Retro box (slate-blue border, black background, white text) with
    token-level hazard coloring. TAF mode normalizes group
    indentation (overlays nest one space under their group)."""
    if taf_mode:
        lines = _reindent_taf(lines)
    body = "\n".join(_colorize_line(ln) for ln in lines)
    return (
        # inline-block: the box is as wide as its longest line, not the
        # column, so a short METAR does not sit in a wide empty frame
        # a full-width rectangle, so METAR and TAF read as one block
        '<div style="display:block;width:100%;box-sizing:border-box;'
        'background:#000000;border:2px solid #2D3957;'
        'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
        f"font-family:'Roboto Mono','DejaVu Sans Mono',Menlo,Consolas,"
        f"'Courier New',monospace;"
        f"font-size:{font_px}px;line-height:1.6;"
        'padding:12px 14px;white-space:pre-wrap;word-break:break-word;">'
        f"{body}</div>"
    )


# The hazard-colour legend that used to print under the TAF box was
# removed - it sat hard against the box and read as part of it. The
# colouring in wx_colored_box is unchanged.


def mono_box(text: str) -> str:
    from html import escape
    return (
        '<div style="background:#000000;border:2px solid #2D3957;'
        'color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;'
        'font-family:Courier New,monospace;font-size:12px;'
        'padding:8px 10px;white-space:pre-wrap;word-break:break-word;">'
        f"{escape(text)}</div>"
    )
