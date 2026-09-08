"""Aircraft icons, operator colours and names — SHARED by every map.

The Airspace page and the gate page must draw a JetBlue aircraft the
same way, so the silhouettes, the per-airline styles, the wide-body
type tables, the tooltip colours and the operator name table live
here and nowhere else. Pages import; nothing here touches Streamlit.

Call set_static_dir(path) once so _icon() can write each SVG variant
to static/ and hand rows a short URL instead of a data URI.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = None


def set_static_dir(path):
    global _STATIC
    _STATIC = Path(path)


# Two silhouettes, chosen per airline (see _OPERATOR_STYLE):
#   A  the original A320 top-down, with engines
#   H  winglet airliner: A320 with raked tips and slim engines
# Wide-bodies use the same style broadened (see _WIDE_PATHS).
_NARROW_PATHS = {
    "A": (
        "M0,-10 L0.35,-9.6 L0.55,-8.8 L0.6,-6 L0.6,-1.6 L9.2,3.2 L9.6,3.4 "
        "L9.6,4 L9.1,4.1 L2.6,3.3 L0.9,3.9 L0.6,5.5 L0.6,7.4 L3.3,8.9 "
        "L3.3,9.5 L2.75,9.5 L0.5,8.8 L0,9.2 L-0.5,8.8 L-2.75,9.5 L-3.3,9.5 "
        "L-3.3,8.9 L-0.6,7.4 L-0.6,5.5 L-0.9,3.9 L-2.6,3.3 L-9.1,4.1 "
        "L-9.6,4 L-9.6,3.4 L-9.2,3.2 L-0.6,-1.6 L-0.6,-6 L-0.55,-8.8 "
        "L-0.35,-9.6 Z",
        "M2.6,-0.9 L3.35,-0.9 L3.45,-0.4 L3.45,1.6 L3.35,1.9 L2.75,1.9 "
        "L2.6,1.5 Z",
        "M-2.6,-0.9 L-3.35,-0.9 L-3.45,-0.4 L-3.45,1.6 L-3.35,1.9 "
        "L-2.75,1.9 L-2.6,1.5 Z",
    ),
    "H": (
        "M0,-10.8 C0.7,-10.8 0.9,-9.4 0.9,-6.5 L0.9,-2.4 L9,3 L10.4,2.2 "
        "L10.6,3.9 L9.4,4.6 L0.9,3.6 L0.9,6.4 L4.1,8.8 L4.1,10.3 L0,9.3 "
        "L-4.1,10.3 L-4.1,8.8 L-0.9,6.4 L-0.9,3.6 L-9.4,4.6 L-10.6,3.9 "
        "L-10.4,2.2 L-9,3 L-0.9,-2.4 L-0.9,-6.5 C-0.9,-9.4 -0.7,-10.8 "
        "0,-10.8 Z",
        "M2.6,-1 L3.6,-1 L3.7,1.8 L2.7,1.8 Z",
        "M-2.6,-1 L-3.6,-1 L-3.7,1.8 L-2.7,1.8 Z",
    ),
}

# Wide-body TWINS keep their airline's style, broadened: a Delta A330
# is unmistakably a bigger Delta. B was retired — its swept shape
# did not read as an airliner at map size next to A and H. Four-engine types get one quad
# silhouette in the airline's treatment (D airlines draw it hollow).
_WIDE_PATHS = {
    "A": (
        "M0,-11.2 L0.6,-10.6 L0.9,-9.4 L1.0,-6.2 L1.0,-2.2 L11.4,3.6 "
        "L11.8,3.9 L11.8,4.6 L11.1,4.7 L3.4,3.8 L1.4,4.4 L1.0,6.4 L1.0,8.4 "
        "L4.4,10.2 L4.4,10.8 L3.7,10.8 L0.8,10.1 L0,10.6 L-0.8,10.1 "
        "L-3.7,10.8 L-4.4,10.8 L-4.4,10.2 L-1.0,8.4 L-1.0,6.4 L-1.4,4.4 "
        "L-3.4,3.8 L-11.1,4.7 L-11.8,4.6 L-11.8,3.9 L-11.4,3.6 L-1.0,-2.2 "
        "L-1.0,-6.2 L-0.9,-9.4 L-0.6,-10.6 Z",
        "M3.6,-2.6 L5.1,-2.6 L5.3,-1.8 L5.3,1.8 L5.1,2.3 L3.8,2.3 L3.6,1.7 Z",
        "M-3.6,-2.6 L-5.1,-2.6 L-5.3,-1.8 L-5.3,1.8 L-5.1,2.3 L-3.8,2.3 "
        "L-3.6,1.7 Z",
    ),
    "H": (
        "M0,-11.6 C1.1,-11.6 1.4,-9.8 1.4,-7 L1.4,-2.6 L10.4,3 L12,2 "
        "L12.3,4 L10.8,4.9 L1.4,3.9 L1.4,7 L5,9.6 L5,11.2 L0,10 L-5,11.2 "
        "L-5,9.6 L-1.4,7 L-1.4,3.9 L-10.8,4.9 L-12.3,4 L-12,2 L-10.4,3 "
        "L-1.4,-2.6 L-1.4,-7 C-1.4,-9.8 -1.1,-11.6 0,-11.6 Z",
        "M3.6,-1.6 L5,-1.6 L5.2,2.2 L3.8,2.2 Z",
        "M-3.6,-1.6 L-5,-1.6 L-5.2,2.2 L-3.8,2.2 Z",
    ),
}

def _a320_icon_uri(fill="#005ADC", stroke="#FFFFFF", stroke_w=0.5,
                   body="narrow", style="A"):
    """Top-down airliner silhouette as an SVG data URI.

    `body` is narrow / wide / quad from the type designator; `style`
    is the airline's letter. Style D draws hollow regardless of
    `fill`. A quad is one shape for every airline, in that airline's
    fill-or-outline treatment."""
    import urllib.parse

    if body == "wide":
        paths = _WIDE_PATHS.get(style) or _WIDE_PATHS["A"]
    else:
        paths = _NARROW_PATHS.get(style) or _NARROW_PATHS["A"]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" '
        'height="64" viewBox="-12.5 -12.5 25 25">'
        f'<g fill="{fill}" stroke="{stroke}" stroke-width="{stroke_w}" '
        'stroke-linejoin="round">'
        + "".join(f'<path d="{d}"/>' for d in paths)
        + '</g></svg>'
    )
    return ("data:image/svg+xml;charset=utf-8,"
            + urllib.parse.quote(svg))


# ICAO type designators by body class. Twins are broadened in their
# airline's style; quads (and tri-jets, which read as "big
# multi-engine" not as twins) get the four-engine silhouette. A
# missing or unknown type is a narrow-body, never an error.
_WIDE_TWIN = {
    "A306", "A310", "A330", "A332", "A333", "A338", "A339", "A350",
    "A359", "A35K", "B762", "B763", "B764", "B772", "B773", "B77L",
    "B77W", "B778", "B779", "B788", "B789", "B78X",
}
_WIDE_QUAD = {
    "A340", "A342", "A343", "A345", "A346", "A380", "A388", "B741",
    "B742", "B743", "B744", "B748", "B74S", "IL96", "MD11", "DC10",
    "L101", "C17",
}


def _body_class(typ: str) -> str:
    """narrow or wide. Four silhouettes on the map — H and A, each in
    a narrow and a broadened wide — and nothing else; a 747 draws as
    a wide with the size bumped a step further, not as a fifth shape."""
    t = (typ or "").strip().upper()
    if t in _WIDE_QUAD or t in _WIDE_TWIN:
        return "wide"
    return "narrow"


# Per-airline icon: (style, fill, stroke, stroke width). Solid styles
# carry the brand in the fill with a white edge; D is hollow and the
# brand is the edge, so the edge is heavier.
_OPERATOR_STYLE = {
    "JBU": ("H", "#005ADC", "#FFFFFF", 0.5),   # JetBlue: winglet A320
    "DAL": ("A", "#C8102E", "#FFFFFF", 0.5),   # Delta: A320, red
    "UAL": ("A", "#4A90D9", "#FFFFFF", 0.5),   # United: A320, light blue
    "AAL": ("A", "#8A8F94", "#FFFFFF", 0.5),   # American: A320, silver
    "ASA": ("A", "#00A5A8", "#FFFFFF", 0.5),   # Alaska: A320, teal
    "SWA": ("A", "#D99A00", "#FFFFFF", 0.5),   # Southwest: A320, gold
    "FFT": ("A", "#0B6B3A", "#FFFFFF", 0.5),   # Frontier: A320, dark green
}
_OPERATOR_FILL = {k: v[1] for k, v in _OPERATOR_STYLE.items()
                  if k != "JBU"}
_OPERATOR_STROKE = _OPERATOR_FILL      # membership test for the majors tier
_OTHER_STROKE = "#000000"
_OTHER_FILL = "#5A5F63"      # everyone not listed above: solid slate

# Tooltip text colour per operator. Same hues as the outlines except
# American, whose silver stroke is fine on the map but unreadable as
# text on a white tooltip, so it darkens to slate.
# Tooltip (box, text). The box is the airline's colour with white
# text — unless the airline is white, in which case the box is white
# and the text takes the edge colour.
_OPERATOR_TIP = {
    "JBU": ("#005ADC", "#FFFFFF"),
    "DAL": ("#C8102E", "#FFFFFF"),
    "UAL": ("#4A90D9", "#FFFFFF"),
    "AAL": ("#5A5F63", "#FFFFFF"),
    "SWA": ("#B37F00", "#FFFFFF"),
    "ASA": ("#00A5A8", "#FFFFFF"),
    "FFT": ("#0B6B3A", "#FFFFFF"),
}
_ICON_CACHE = {}


def _tcol(cs: str) -> tuple:
    """(box, text) tooltip colours for a callsign. Substituted into
    the tooltip TEMPLATE — pydeck escapes substituted values, so
    markup inside `tip` renders as literal text, but a hex colour has
    nothing to escape."""
    return _OPERATOR_TIP.get((cs or "")[:3].upper(), ("#000000", "#FFFFFF"))


def _who(cs: str) -> str:
    """'AAL605' -> 'American 605'; 'JBU1234' -> 'JetBlue 1234'. An
    unknown prefix is left as the raw callsign."""
    cs = (cs or "").strip().upper()
    name = _OPERATORS.get(cs[:3])
    return f"{name} {cs[3:]}" if name and cs[3:] else cs


def _icon(fill, stroke, sw, body, style="A"):
    """Icon descriptor for a row. The SVG is written to static/ ONCE
    per variant and the row carries its URL — about 110 bytes instead
    of a 930-byte data URI repeated on every aircraft. With 300
    aircraft that was 280 KB of identical strings in every rerun's
    payload, and the browser had to parse them before it could start
    fetching the radar. Falls back to the data URI if there is no
    base URL to build from."""
    import os as _o
    import urllib.parse as _u

    key = (fill, stroke, sw, body, style)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    uri = _a320_icon_uri(fill, stroke, sw, body, style)
    base = (_o.environ.get("RENDER_EXTERNAL_URL")
            or _o.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    url = uri
    if base and _STATIC is not None:
        name = ("icon_" + "_".join(str(k).lstrip("#").replace("none", "x")
                                   for k in key) + ".svg")
        try:
            fp = _STATIC / name
            if not fp.exists():
                fp.write_text(_u.unquote(uri.split(",", 1)[1]))
            url = f"{base}/app/static/{name}"
        except OSError:
            pass
    _ICON_CACHE[key] = {"url": url, "width": 64, "height": 64,
                        "anchorX": 32, "anchorY": 32, "mask": False}
    return _ICON_CACHE[key]


_OPERATORS = {
    # US majors
    "AAL": "American", "DAL": "Delta", "UAL": "United",
    "SWA": "Southwest", "JBU": "JetBlue", "ASA": "Alaska",
    "NKS": "Spirit", "FFT": "Frontier", "AAY": "Allegiant",
    "SCX": "Sun Country", "HAL": "Hawaiian", "BRZ": "Breeze",
    # US regionals
    "SKW": "SkyWest", "RPA": "Republic", "EDV": "Endeavor",
    "ENY": "Envoy", "JIA": "PSA", "PDT": "Piedmont",
    "ASH": "Mesa", "GJS": "GoJet", "QXE": "Horizon",
    "UCA": "CommutAir", "LOF": "Air Wisconsin", "CJT": "Cargojet",
    # Cargo
    "FDX": "FedEx", "UPS": "UPS", "GTI": "Atlas Air",
    "ATN": "Air Transport Intl", "ABX": "ABX Air",
    "CKS": "Kalitta", "PAC": "Polar Air Cargo",
    "GEC": "Lufthansa Cargo", "CLX": "Cargolux",
    "BOX": "AeroLogic", "NCA": "Nippon Cargo",
    # Canada / Mexico / Caribbean / Latin America
    "ACA": "Air Canada", "ROU": "Air Canada Rouge", "JZA": "Jazz",
    "WJA": "WestJet", "TSC": "Air Transat", "POE": "Porter",
    "AMX": "Aeromexico", "VOI": "Volaris", "VIV": "Viva",
    "CMP": "Copa", "AVA": "Avianca", "LAN": "LATAM",
    "TAM": "LATAM Brasil", "ARG": "Aerolineas Argentinas",
    "AZU": "Azul", "GLO": "GOL", "BWA": "Caribbean Airlines",
    "CAW": "Caribbean", "JBW": "interCaribbean",
    # Europe
    "BAW": "British Airways", "VIR": "Virgin Atlantic",
    "DLH": "Lufthansa", "AFR": "Air France", "KLM": "KLM",
    "IBE": "Iberia", "ITY": "ITA Airways", "SWR": "SWISS",
    "AUA": "Austrian", "SAS": "SAS", "FIN": "Finnair",
    "TAP": "TAP Portugal", "EIN": "Aer Lingus",
    "ICE": "Icelandair", "NAX": "Norwegian", "LOT": "LOT",
    "AEA": "Air Europa", "AEE": "Aegean", "THY": "Turkish",
    "UKR": "Ukraine Intl", "BEL": "Brussels", "TVS": "SmartWings",
    "PGT": "Pegasus", "WZZ": "Wizz Air", "EZY": "easyJet",
    "RYR": "Ryanair", "LEV": "LEVEL",
    # Middle East / Africa / Asia / Pacific
    "UAE": "Emirates", "QTR": "Qatar Airways", "ETD": "Etihad",
    "SVA": "Saudia", "ELY": "El Al", "MSR": "EgyptAir",
    "RAM": "Royal Air Maroc", "ETH": "Ethiopian",
    "KQA": "Kenya Airways", "RJA": "Royal Jordanian",
    "KAC": "Kuwait Airways", "AIC": "Air India",
    "JAL": "Japan Airlines", "ANA": "All Nippon",
    "KAL": "Korean Air", "AAR": "Asiana", "CCA": "Air China",
    "CES": "China Eastern", "CSN": "China Southern",
    "SIA": "Singapore Airlines", "CPA": "Cathay Pacific",
    "EVA": "EVA Air", "CAL": "China Airlines", "PAL": "Philippine",
    "THA": "Thai Airways", "QFA": "Qantas", "ANZ": "Air New Zealand",
    "UZB": "Uzbekistan", "AZG": "Silk Way",
    # Fractional / charter / business
    "EJA": "NetJets", "LXJ": "Flexjet", "JTL": "Jet Linx",
    "XOJ": "XO", "OPT": "Flight Options", "VTE": "Vista",
    "TFF": "Wheels Up", "GAJ": "Gama Aviation",
    # State / other
    "RCH": "US Air Mobility Cmd", "PAT": "US Army",
    "CNV": "US Navy", "LIFE": "Air Ambulance",
}


# ---------------------------------------------------------------------------
# ATC scope symbols (STARS style)
# ---------------------------------------------------------------------------
# A filled square at the position, drawn WHITE and tinted per row by
# the layer's colour accessor (deck.gl "mask" icons), so one icon
# serves every operator. Targets do not rotate — a scope square never
# does; heading is in the trail and the data block. JetBlue and the
# majors carry a leader to a two-line data block: callsign over
# altitude and speed in scope shorthand, "250 38" for FL250 at 380 kt.
# Every commercial callsign carries the block; GA is the square alone.
_ATC_SQUARE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'viewBox="0 0 64 64"><rect x="22" y="22" width="20" height="20" '
    'fill="#FFFFFF"/></svg>')
_ATC_SQUARE_LEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
    'viewBox="0 0 64 64"><rect x="22" y="22" width="20" height="20" '
    'fill="#FFFFFF"/><path d="M42,22 L60,4" stroke="#FFFFFF" '
    'stroke-width="3" stroke-linecap="round"/></svg>')
_ATC_CACHE = {}


def atc_icon(leader: bool) -> dict:
    """Icon descriptor for the square, with or without a leader line.
    Written to static/ once like the silhouettes; mask=True so the
    row's colour tints it."""
    import os as _o
    import urllib.parse as _u

    if leader in _ATC_CACHE:
        return _ATC_CACHE[leader]
    svg = _ATC_SQUARE_LEADER if leader else _ATC_SQUARE
    uri = "data:image/svg+xml;charset=utf-8," + _u.quote(svg)
    base = (_o.environ.get("RENDER_EXTERNAL_URL")
            or _o.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    url = uri
    if base and _STATIC is not None:
        name = f"icon_atc_{'leader' if leader else 'square'}.svg"
        try:
            fp = _STATIC / name
            if not fp.exists():
                fp.write_text(svg)
            url = f"{base}/app/static/{name}"
        except OSError:
            pass
    _ATC_CACHE[leader] = {"url": url, "width": 64, "height": 64,
                          "anchorX": 32, "anchorY": 32, "mask": True}
    return _ATC_CACHE[leader]


def atc_color(cs: str) -> list:
    """RGB for a callsign: JetBlue blue, a major's colour, else slate."""
    pre = (cs or "")[:3].upper()
    h = ("#005ADC" if pre == "JBU" else _OPERATOR_FILL.get(pre, _OTHER_FILL)).lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


import re as _re

_COMMERCIAL = _re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]?$")


def has_block(cs: str) -> bool:
    """Every airline-format callsign gets a leader and a data block —
    DAL182, EJA411, FFT3134 — as on a scope. N-numbers, generic
    callsigns and blanks get the square alone with a tooltip."""
    return bool(_COMMERCIAL.match((cs or "").strip().upper()))


def data_block(cs: str, alt_ft, gs_kt) -> str:
    """Two lines, scope shorthand: callsign, then altitude in hundreds
    of feet and ground speed in tens of knots."""
    a = "" if alt_ft is None else f"{int(round(alt_ft / 100)):03d}"
    g = "" if gs_kt is None else f"{int(round(gs_kt / 10)):02d}"
    return f"{cs}\n{a} {g}".rstrip()


# ---------------------------------------------------------------------------
# Chart symbols for fixes and navaids
# ---------------------------------------------------------------------------
# The sectional-chart glyphs: a four-point star for an RNAV waypoint,
# a hexagon inside a square for a VOR/DME, a hexagon with three lobes
# for a VORTAC, concentric circles for an NDB. Drawn white and tinted
# per row through the icon mask, so arrival gates and departure fixes
# keep their colours. The fix file carries no navaid type, so the
# kind is read from the name: three letters is a navaid, five an RNAV
# waypoint; every three-letter fix in the N90 file is a VOR/DME or a
# VORTAC and the VOR/DME glyph is used for all of them until the file
# says otherwise.
_NAV_SVG = {
    "waypoint": ('<path d="M32,6 L38,26 L58,32 L38,38 L32,58 L26,38 L6,32 L26,26 Z" '
                 'fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linejoin="round"/>'
                 '<circle cx="32" cy="32" r="3.5" fill="#FFFFFF"/>'),
    "vordme": ('<rect x="8" y="8" width="48" height="48" fill="none" stroke="#FFFFFF" '
               'stroke-width="4"/><path d="M20,32 L26,20 L38,20 L44,32 L38,44 L26,44 Z" '
               'fill="none" stroke="#FFFFFF" stroke-width="4"/>'
               '<circle cx="32" cy="32" r="3.5" fill="#FFFFFF"/>'),
    "vortac": ('<path d="M20,32 L26,20 L38,20 L44,32 L38,44 L26,44 Z" fill="none" '
               'stroke="#FFFFFF" stroke-width="4"/>'
               '<path d="M26,20 L20,8 L32,12 Z M38,20 L44,8 L32,12 Z M32,44 L26,56 L38,56 Z" '
               'fill="#FFFFFF"/><circle cx="32" cy="32" r="3.5" fill="#FFFFFF"/>'),
    "ndb": ('<circle cx="32" cy="32" r="22" fill="none" stroke="#FFFFFF" stroke-width="3"/>'
            '<circle cx="32" cy="32" r="13" fill="none" stroke="#FFFFFF" stroke-width="3"/>'
            '<circle cx="32" cy="32" r="4" fill="#FFFFFF"/>'),
    "vfr": ('<path d="M32,8 L56,54 L8,54 Z" fill="none" stroke="#FFFFFF" '
            'stroke-width="4" stroke-linejoin="round"/>'),
}
_NAV_CACHE = {}


def nav_kind(name: str, navtype: str = None) -> str:
    """Chart glyph for a fix. An explicit type wins; else the name."""
    t = (navtype or "").lower()
    if t in _NAV_SVG:
        return t
    n = (name or "").strip()
    return "vordme" if len(n) == 3 else "waypoint"


def nav_icon(kind: str) -> dict:
    """Mask icon descriptor for a chart glyph, written to static/ once."""
    import os as _o
    import urllib.parse as _u

    kind = kind if kind in _NAV_SVG else "waypoint"
    if kind in _NAV_CACHE:
        return _NAV_CACHE[kind]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
           'viewBox="0 0 64 64">' + _NAV_SVG[kind] + '</svg>')
    uri = "data:image/svg+xml;charset=utf-8," + _u.quote(svg)
    base = (_o.environ.get("RENDER_EXTERNAL_URL")
            or _o.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    url = uri
    if base and _STATIC is not None:
        name = f"icon_nav_{kind}.svg"
        try:
            fp = _STATIC / name
            if not fp.exists():
                fp.write_text(svg)
            url = f"{base}/app/static/{name}"
        except OSError:
            pass
    _NAV_CACHE[kind] = {"url": url, "width": 64, "height": 64,
                        "anchorX": 32, "anchorY": 32, "mask": True}
    return _NAV_CACHE[kind]
