"""Aircraft data cards and icons for the airport scope.

Target path: core/cards.py
Assets:      static/icons/plane_twin.png   the twin-podded silhouette
             static/logos/<ICAO3>.png       optional airline marks

The card, from the r202 design: white, text always black, outlined in
the aircraft's colour, a short leader to the aircraft. Six carriers
carry their mark. A mark 2.0:1 or wider sits on top of the two data
rows; Delta is the one compact card, four rows with the mark to the
right. Every other operator gets the same card with no mark, sized to
its text so there is no empty space.

HOW IT DRAWS. Every card is an SVG data URI handed to an IconLayer -
the path pages/3 has shipped all its icons through without a failure
- so there are no files to write, serve or clean up.

LOGOS. The marks are read from static/logos/<code>.png (AAL, JBU,
UAL, ASA, SWA, DAL). Until a file is there, that carrier's card is
drawn exactly like any other - outlined in its colour, no mark - so a
missing logo costs the mark, never the card.
"""

from __future__ import annotations

import base64
import io
import threading
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "static"
_PLANE = _ROOT / "icons" / "plane_twin.png"
_LOGOS = _ROOT / "logos"

# Aircraft and card-outline colour per carrier (23 Oct, "option A"):
# one colour per major, brand-leaning, all readable on the charcoal
# scope. United left steel grey (it matched American's silver) for
# violet; Alaska left navy (invisible on black) for teal.
CARRIERS = {
    "JBU": "#1463F3",   # JetBlue blue
    "DAL": "#E0344C",   # Delta red
    "AAL": "#D8DEE8",   # American silver
    "UAL": "#9B8CFF",   # United violet
    "SWA": "#F5C518",   # Southwest gold
    "NKS": "#FFF200",   # Spirit yellow
    "FFT": "#35C759",   # Frontier green
    "ASA": "#2BC4D6",   # Alaska teal
    "HAL": "#E85DB1",   # Hawaiian magenta
    "FDX": "#FF7A1A",   # FedEx orange
    "UPS": "#B0744A",   # UPS brown
}
# Regional operators take the colour of the major whose livery they
# fly. SkyWest and Republic fly for several; they get the partner
# they fly most for.
REGIONAL = {
    "EDV": "DAL",       # Endeavor
    "JIA": "AAL",       # PSA
    "PDT": "AAL",       # Piedmont
    "ENY": "AAL",       # Envoy
    "RPA": "AAL",       # Republic (also DL, UA)
    "ASH": "UAL",       # Mesa
    "GJS": "UAL",       # GoJet
    "SKW": "UAL",       # SkyWest (also AA, DL, AS)
    "CPZ": "DAL",       # Compass
    "QXE": "ASA",       # Horizon
    "AWI": "AAL",       # Air Wisconsin
}
# Private / fractional / charter jet operators: grey, bizjet glyph.
PRIVATE = {"EJA", "XOJ", "LXJ", "FLX", "JTL", "VTE", "WUP", "EDG", "DCM",
           "GAJ", "TWY", "JAS", "PJS", "NJE", "FJO"}
PRIVATE_COLOUR = "#9AA0A6"
# International majors (24 Oct): one colour for all of them - lime,
# the one hue nothing domestic uses - so "foreign carrier" reads at a
# glance. By ICAO operator code; extend freely.
INTERNATIONAL = {
    # Europe / Middle East
    "BAW", "VIR", "AFR", "DLH", "KLM", "IBE", "EIN", "SAS", "SWR", "AUA", "TAP", "ITY",
    "FIN", "ICE", "LOT", "NAX", "NOZ", "EZY", "RYR", "BEL", "CFG", "EDW", "VLG", "AZA",
    "UAE", "QTR", "ETD", "THY", "ELY", "SVA", "RJA", "MSR", "KAC",
    # Canada
    "ACA", "ROU", "WJA", "JZA", "TSC", "POE", "SWG",
    # Latin America / Caribbean
    "AMX", "SLI", "CMP", "AVA", "LAN", "LPE", "TAM", "AZU", "GLO", "ARG", "BWA", "CAY",
    "BHS", "VOI", "VIV", "SLM", "IWD", "ICL",
    # Asia-Pacific
    "JAL", "ANA", "KAL", "AAR", "CPA", "CES", "CSN", "CCA", "EVA", "CAL", "SIA", "QFA",
    "ANZ", "PAL", "HVN", "THA", "MAS", "GIA", "AIC", "VTI",
}
INTERNATIONAL_COLOUR = "#C8F03C"
OTHER = "#6E7681"

# ---------------------------------------------------- glyph by aircraft
# Four glyphs (23 Oct): bizjet (smaller), regional CRJ/ERJ (T-tail,
# slightly smaller), the airliner, and a widebody (larger).
_GLYPHS = {
    "bizjet": _ROOT / "icons" / "plane_bizjet.png",
    "regional": _ROOT / "icons" / "plane_regional.png",
    "narrow": _PLANE,
    "wide": _ROOT / "icons" / "plane_wide.png",
}
# 24 Oct: 75% of the first sizes (17/19/22/27) and a narrower widebody.
ICON_PX = {"bizjet": 13, "regional": 14, "narrow": 17, "wide": 19}
# Icon tint: carrier colour muted 25% (saturation x0.75) and 70% opaque,
# so the weather shows through the aircraft but the aircraft still
# holds its own on any echo colour (24 Oct, "row 5").
MUTE_SAT = 0.75
ICON_ALPHA = 0.70


def muted(hex_colour: str) -> str:
    import colorsys
    r, g, b = (int(hex_colour.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, l, s * MUTE_SAT)
    return "#%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))
WIDE_TYPES = ("A33", "A34", "A35", "A38", "B74", "B76", "B77", "B78",
              "MD11", "DC10", "A306", "A310", "IL96", "B703", "A124", "C5M",
              "C17", "K35R", "B52", "E3", "E6")
REGIONAL_TYPES = ("CRJ", "E135", "E145", "E170", "E175", "E75", "E17",
                  "AT4", "AT7", "DH8", "SF34", "E120")
BIZJET_TYPES = ("C25", "C50", "C51", "C52", "C55", "C56", "C68", "C70",
                "C750", "CL30", "CL35", "CL60", "GLF", "GL5", "GL6", "GL7",
                "GLEX", "GA5", "GA6", "GA7", "E55P", "E50P", "E545", "E550",
                "LJ", "H25", "HA4", "PRM1", "BE40", "FA7X", "FA8X", "F2TH",
                "F900", "FA50", "FA10", "F406", "G150", "G280", "ASTR",
                "GALX", "PC24", "EA50", "HDJT", "SBR1", "WW24", "C650",
                "GLXP", "GLX")


def kind(callsign: str, actype: str = "") -> str:
    """Which glyph: 'wide' / 'narrow' / 'regional' / 'bizjet'."""
    t = (actype or "").strip().upper()
    c = (callsign or "")[:3].upper()
    if t:
        if t.startswith(WIDE_TYPES):
            return "wide"
        if t.startswith(BIZJET_TYPES):
            return "bizjet"
        if t.startswith(REGIONAL_TYPES):
            return "regional"
        return "narrow"
    if c in PRIVATE:
        return "bizjet"
    if c in REGIONAL:
        return "regional"
    return "narrow"
COMPACT = {"DAL"}                  # mark to the right, four rows

FONT = ("'Roboto Mono', 'DejaVu Sans Mono', Menlo, Consolas, "
        "'Liberation Mono', monospace")
FS = 11.0                          # data rows, px
CW = FS * 0.602                    # monospace advance per character
LH = 13.0                          # line height
TAG_FS = round(FS * 2 / 3)         # zoomed-out flight number, 2/3 the card
PAD_X, PAD_Y = 4.0, 3.0
LEAD = 14.0                        # leader, aircraft to card edge
SCALE = 2                          # raster at 2x, display at 1x

_LOCK = threading.Lock()


def colour(callsign: str) -> str:
    c = (callsign or "")[:3].upper()
    if c in CARRIERS:
        return CARRIERS[c]
    if c in REGIONAL:
        return CARRIERS.get(REGIONAL[c], OTHER)
    if c in PRIVATE:
        return PRIVATE_COLOUR
    if c in INTERNATIONAL:
        return INTERNATIONAL_COLOUR
    return OTHER


def carrier(callsign: str) -> str:
    c = (callsign or "")[:3].upper()
    return c if c in CARRIERS else ""


# ----------------------------------------------------------- aircraft

@lru_cache(maxsize=64)
def plane_icon(hex_colour: str, glyph: str = "narrow") -> dict:
    """IconLayer icon for the silhouette in one colour. Cached per
    colour and glyph."""
    from PIL import Image

    base = Image.open(_GLYPHS.get(glyph, _PLANE)).convert("RGBA")
    mc = muted(hex_colour)
    r, g, b = (int(mc.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    tinted = Image.new("RGBA", base.size, (r, g, b, 255))
    tinted.putalpha(base.getchannel("A").point(lambda v: int(v * ICON_ALPHA)))
    buf = io.BytesIO()
    tinted.save(buf, format="PNG", optimize=True)
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    w, h = base.size
    return {"url": url, "width": w, "height": h,
            "anchorX": w / 2, "anchorY": h / 2}


# -------------------------------------------------------------- logos

@lru_cache(maxsize=16)
def _logo(code: str):
    """(data_uri, aspect) for a carrier's mark, or None."""
    p = _LOGOS / f"{code}.png"
    if not code or not p.exists():
        return None
    try:
        from PIL import Image
        im = Image.open(p)
        w, h = im.size
        data = base64.b64encode(p.read_bytes()).decode()
        return f"data:image/png;base64,{data}", w / max(h, 1)
    except Exception:
        return None


# --------------------------------------------------------------- text

def _alt(v) -> str:
    if v in (None, ""):
        return "---"
    if isinstance(v, str):
        return "GND" if v.lower().startswith("gr") else v
    try:
        return f"{int(round(float(v))):,} ft"
    except (TypeError, ValueError):
        return "---"


def _gs(v) -> str:
    try:
        return f"{int(round(float(v)))}kt"
    except (TypeError, ValueError):
        return "---kt"


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def _rows_two(cs, alt, gs, typ):
    """Two aligned rows: 'JBU124  9,850 ft' / '303kt   A320'."""
    left = max(len(cs), len(gs)) + 2
    return [cs.ljust(left) + alt, gs.ljust(left) + (typ or "")]


# --------------------------------------------------------------- card

def card(ac: dict) -> dict:
    """IconLayer icon (SVG data URI) for one aircraft's card.

    ac: {"callsign", "alt", "gs", "type"}. Returns the icon dict plus
    "px_h", the card's display height in pixels, which the caller uses
    to group cards into layers - a layer's size clamp is one value.
    """
    cs = (ac.get("callsign") or "").strip().upper()
    alt, gs, typ = _alt(ac.get("alt")), _gs(ac.get("gs")), (ac.get("type") or "")
    col = colour(cs)
    code = carrier(cs)
    logo = _logo(code)

    if logo and code in COMPACT:
        rows = [cs, alt, gs, typ]
        text_w = max(len(r) for r in rows) * CW
        logo_h = LH * 1.9
        logo_w = logo_h * logo[1]
        card_w = PAD_X + text_w + 8 + logo_w + PAD_X
        card_h = PAD_Y * 2 + LH * len(rows)
        logo_xy = (PAD_X + text_w + 8, (card_h - logo_h) / 2 + 2)
        text_y0 = PAD_Y
    else:
        rows = _rows_two(cs, alt, gs, typ)
        text_w = max(len(r) for r in rows) * CW
        top = 0.0
        logo_xy = None
        if logo:
            logo_h = LH * 0.95
            logo_w = min(logo_h * logo[1], text_w)
            logo_h = logo_w / logo[1]
            top = logo_h + 2
            logo_xy = (PAD_X, PAD_Y)
        card_w = PAD_X * 2 + text_w
        card_h = PAD_Y * 2 + top + LH * len(rows)
        text_y0 = PAD_Y + top

    W = LEAD + card_w + 1
    H = card_h + 1
    mid = H / 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{W * SCALE:.0f}" height="{H * SCALE:.0f}" '
        f'viewBox="0 0 {W:.2f} {H:.2f}">',
        # leader: from beside the aircraft to the card edge
        f'<line x1="2" y1="{mid + 5:.2f}" x2="{LEAD:.2f}" y2="{mid:.2f}" '
        f'stroke="{col}" stroke-width="1"/>',
        f'<rect x="{LEAD + 0.5:.2f}" y="0.5" width="{card_w:.2f}" '
        f'height="{card_h:.2f}" rx="2" fill="#FFFFFF" stroke="{col}" '
        f'stroke-width="1.3"/>',
    ]
    if logo_xy:
        lx = LEAD + 0.5 + logo_xy[0]
        parts.append(
            f'<image x="{lx:.2f}" y="{0.5 + logo_xy[1]:.2f}" '
            f'width="{logo_w:.2f}" height="{logo_h:.2f}" '
            f'href="{logo[0]}"/>')
    for i, r in enumerate(rows):
        y = 0.5 + text_y0 + LH * (i + 0.78)
        parts.append(
            f'<text x="{LEAD + 0.5 + PAD_X:.2f}" y="{y:.2f}" '
            f'font-family="{FONT}" font-size="{FS}" font-weight="700" '
            f'fill="#000000" xml:space="preserve">{_esc(r)}</text>')
    parts.append("</svg>")
    svg = "".join(parts)

    import urllib.parse
    url = "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg)
    return {"url": url, "width": int(round(W * SCALE)),
            "height": int(round(H * SCALE)),
            # the aircraft sits at the start of the leader
            "anchorX": 0, "anchorY": int(round((mid + 5) * SCALE)),
            "px_h": int(round(H))}


def logos_present() -> list:
    return [c for c in CARRIERS if _logo(c)]
