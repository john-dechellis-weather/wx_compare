"""BlueMet login - the network board.

Target path: login_board.py (repo root, beside auth.py)

auth.py owns the password check and the st.stop(); this module only
draws. Layout: wordmark and clock, twelve station chips, the colour
key, then sign-in on the left with the fixed-view map on the right.

The map does NOT pan or zoom. It shows the lower 48 with MRMS
reflectivity and the network stations as small blue dots. Everything
it draws is either already on disk (the MRMS chunks the warmer wrote)
or a constant, so the login page starts no fetches of its own except
the twelve METARs behind a five-minute cache.

Fleet: see FLEET, below.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import pydeck as pdk
import streamlit as st

import dark_theme as T

try:
    from core import station_status as S
except ImportError:          # repo-root copy, if it ever moves back
    import station_status as S

# ---------------------------------------------------------------- view

# Fixed extent, matched to the CONUS map's usual framing. Nothing here
# is user-adjustable by design.
VIEW_LAT = float(os.environ.get("BLUEMET_LOGIN_LAT", "37.8"))
VIEW_LON = float(os.environ.get("BLUEMET_LOGIN_LON", "-95.5"))
VIEW_ZOOM = float(os.environ.get("BLUEMET_LOGIN_ZOOM", "3.0"))

MAP_STYLE = os.environ.get(
    "BLUEMET_MAP_STYLE",
    "https://basemaps.cartocdn.com/gl/"
    "dark-matter-nolabels-gl-style/style.json")

# FLEET
# -----
# Aircraft and their 5-minute trails are NOT drawn yet, and the reason
# is worth keeping: the ADS-B sweep lives inside pages/3, in module
# scope (_fleet_state, fleet_now, cached_fleet). A page module cannot
# be imported, so the only ways to put the fleet here are to move the
# sweep into core/ - where both the page and this module can read the
# same singleton - or to run a second sweep. A second sweep doubles
# the request rate against adsb.lol and adsb.fi, on a page anyone who
# finds the URL can load, and 429s there degrade the map page that
# matters. JBU_FLEET_SWEEP_S exists precisely to stop the rate being
# multiplied.
#
# When the sweep moves to core/, set FLEET_READER to a callable
# returning [{"lat", "lon", "trail": [[lon, lat], ...]}, ...] and the
# two layers below start drawing. Nothing else has to change.
FLEET_READER = None
TRAIL_MINUTES = 5


def _rgb(h: str) -> list[int]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


# ------------------------------------------------------------- data

@st.cache_data(ttl=300, show_spinner=False)
def _metars() -> dict:
    """Latest raw METAR per board station. Best effort: on any failure
    the chips render grey, which reads as 'no data', not as 'fine'."""
    icaos = [f"K{c}" for c in S.LOGIN_STATIONS]
    try:
        from core.metar import fetch_metars
        by_station = fetch_metars(icaos, hours_back=2)
    except Exception:
        return {}
    out = {}
    for icao, obs in (by_station or {}).items():
        if not obs:
            continue
        raw = getattr(obs[-1], "raw_text", None)
        if raw:
            # Key by the 3-letter code the board displays.
            out[icao.upper()[-3:]] = raw
    return out


def _origin() -> str:
    """The origin the browser is actually on.

    Same reasoning as the CONUS map: a chunk served from a different
    origin is cross-origin, and WebGL refuses a cross-origin image as
    a texture - the layer draws nothing, with no error.
    """
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


def _mrms_layers() -> tuple[list, str]:
    """BitmapLayer per warmed MRMS chunk, newest complete scan."""
    base = _origin()
    if not base:
        return [], ""
    try:
        from core import mrms as _MR
        static_dir = Path(__file__).resolve().parent / "static"
        chunks, stamp = _MR.newest(static_dir, "REFL")
    except Exception:
        return [], ""
    if not chunks:
        return [], ""

    st.markdown("".join(
        f"<link rel='preload' as='image' "
        f"href='{base}/app/static/{c['name']}'>" for c in chunks),
        unsafe_allow_html=True)

    return [pdk.Layer("BitmapLayer", data=None,
                      image=f"{base}/app/static/{c['name']}",
                      bounds=c["bounds"], opacity=1.0)
            for c in chunks], (stamp or "")


def _station_layers(statuses) -> list:
    """Small blue dots with small labels, and a hover that says why
    the station is the colour it is on the board above.

    SIZING: get_radius / get_size are in METRES with a pixel clamp,
    not radius_units="pixels". The units props are ignored by the
    pydeck build running here - that is what drew continent-sized
    dots and labels - and the clamp is how pages/3 already does it.
    """
    blue = _rgb(T.JBU_BLUE)
    why = {s.icao: s for s in statuses}
    pts = []
    for code, (lat, lon) in S.STATION_LATLON.items():
        if code not in S.LOGIN_STATIONS:
            continue
        st_ = why.get(code)
        pts.append({
            "position": [lon, lat],
            "name": code,
            "reason": (st_.reason if st_ else "no observation"),
            "level": (st_.name if st_ else "none"),
        })
    return [
        pdk.Layer("ScatterplotLayer", pts, get_position="position",
                  get_fill_color=blue + [255],
                  get_line_color=[0, 0, 0, 255], stroked=True,
                  line_width_min_pixels=1,
                  get_radius=9000,
                  radius_min_pixels=3, radius_max_pixels=5,
                  pickable=True, auto_highlight=True),
        pdk.Layer("TextLayer", pts, get_position="position",
                  get_text="name", get_color=blue + [255],
                  get_size=2600, size_min_pixels=0, size_max_pixels=11,
                  get_pixel_offset=[10, -7],
                  get_text_anchor='"start"',
                  get_alignment_baseline='"center"',
                  pickable=False),
    ]


def _fleet_layers() -> list:
    if FLEET_READER is None:
        return []
    try:
        fleet = FLEET_READER() or []
    except Exception:
        return []
    if not fleet:
        return []
    blue = _rgb(T.JBU_BLUE)
    trails = [{"path": a["trail"]} for a in fleet
              if len(a.get("trail") or ()) > 1]
    out = []
    if trails:
        out.append(pdk.Layer("PathLayer", trails, get_path="path",
                             get_color=blue + [150], get_width=2,
                             width_units="pixels", width_min_pixels=2,
                             pickable=False))
    out.append(pdk.Layer(
        "ScatterplotLayer",
        [{"position": [a["lon"], a["lat"]]} for a in fleet],
        get_position="position", get_fill_color=blue + [230],
        get_radius=3, radius_units="pixels", radius_min_pixels=3,
        pickable=False))
    return out


def _deck(layers) -> pdk.Deck:
    # controller=False freezes the view. min_zoom == max_zoom is the
    # second line of defence: if a pydeck version ignores the view's
    # controller flag, the view state still cannot change scale.
    return pdk.Deck(
        layers=layers,
        views=[pdk.View(type="MapView", controller=False)],
        initial_view_state=pdk.ViewState(
            latitude=VIEW_LAT, longitude=VIEW_LON, zoom=VIEW_ZOOM,
            min_zoom=VIEW_ZOOM, max_zoom=VIEW_ZOOM,
            bearing=0, pitch=0),
        map_style=MAP_STYLE,
        tooltip={
            "html": "<b>{name}</b><br/>{reason}",
            "style": {"backgroundColor": "#0A0A0A",
                      "color": "#FFFFFF",
                      "border": "1px solid #333333",
                      "fontSize": "12px",
                      "fontFamily": "DejaVu Sans Mono, monospace"},
        },
        parameters={"clearColor": [0, 0, 0, 1]},
    )


# -------------------------------------------------------------- chrome

def _chips(statuses) -> str:
    cells = "".join(
        f'<div title="{s.icao}: {s.reason}" '
        f'style="flex:0 0 auto;min-width:92px;background:{T.PANEL};'
        f'border:1px solid {T.RULE};border-radius:3px;padding:10px 12px;'
        f'cursor:help">'
        f'<div style="color:{T.TEXT};font-size:17px;font-weight:700;'
        f'letter-spacing:.5px">{s.icao}</div>'
        f'<div style="height:6px;margin-top:10px;background:{s.color}">'
        f'</div></div>'
        for s in statuses)
    return f'<div style="display:flex;gap:9px;flex-wrap:wrap">{cells}</div>'


def _key() -> str:
    rows = [
        (T.GREEN,  "VFR \u00b7 gusts \u2264 25 kt"),
        (T.YELLOW, "MVFR \u00b7 VCTS \u00b7 gusts 26\u201330 kt"),
        (T.ORANGE, "IFR \u00b7 +RA \u00b7 gusts 31\u201335 kt"),
        (T.PINK,   "LIFR \u00b7 gusts \u2265 36 kt"),
        (T.RED,    "Thunderstorms"),
        (S.COLORS[S.NONE], "No observation"),
    ]
    items = "".join(
        f'<div style="display:flex;align-items:center;gap:10px">'
        f'<span style="width:26px;height:6px;background:{c};'
        f'display:inline-block"></span>'
        f'<span style="color:{T.TEXT};font-size:12px;font-weight:700">'
        f'{label}</span></div>'
        for c, label in rows)
    return (f'<div style="display:flex;gap:30px;flex-wrap:wrap;'
            f'margin-top:12px">{items}</div>')


def render() -> str | None:
    """Draw the board. Returns the typed password, or None."""
    T.apply_dark_theme()
    st.markdown(
        "<style>"
        '[data-testid="stSidebar"]{display:none}'
        # ~16 characters wide, as the old login form was.
        '[data-testid="stTextInput"]{max-width:200px !important;}'
        '[data-testid="InputInstructions"]{display:none !important;}'
        # The eye toggle draws as the word "visibility" without the
        # icon font, which looks like stray text in the box.
        '[data-testid="stTextInput"] button{display:none !important;}'
        "</style>", unsafe_allow_html=True)

    now = _dt.datetime.now(_dt.timezone.utc)

    a, b = st.columns([3, 1])
    with a:
        st.markdown(
            f'<div style="font-size:44px;font-weight:700;color:{T.TEXT};'
            f'letter-spacing:1px;line-height:1.1">BLUEMET</div>'
            f'<div style="font-size:13px;color:{T.TEXT_2};margin-top:6px">'
            f'JetBlue System Operations weather</div>',
            unsafe_allow_html=True)
    with b:
        st.markdown(
            f'<div style="text-align:right">'
            f'<div style="font-size:13px;color:{T.TEXT_2}">'
            f'{now:%d %b %Y}</div>'
            f'<div style="font-size:30px;font-weight:700;color:{T.TEXT}">'
            f'{now:%H:%M}Z</div></div>', unsafe_allow_html=True)

    st.markdown(f'<hr style="border-color:{T.RULE};margin:18px 0 20px 0">',
                unsafe_allow_html=True)

    statuses = S.board(S.LOGIN_STATIONS, _metars())
    st.markdown(
        f'<div style="color:{T.TEXT_2};font-size:12px;font-weight:700;'
        f'margin-bottom:8px">Network conditions</div>' + _chips(statuses),
        unsafe_allow_html=True)
    st.markdown(_key(), unsafe_allow_html=True)

    st.markdown(f'<hr style="border-color:{T.RULE};margin:24px 0 8px 0">',
                unsafe_allow_html=True)

    left, right = st.columns([1, 2], gap="large")

    with left:
        st.markdown(
            f'<div style="font-size:22px;font-weight:700;color:{T.TEXT};'
            f'margin-top:16px">Sign in to continue</div>'
            f'<div style="font-size:13px;color:{T.TEXT_2};'
            f'margin:8px 0 14px 0">This site is for authorized users '
            f'only.</div>', unsafe_allow_html=True)
        pw = st.text_input("Password", type="password",
                           key="password_input")
        st.markdown(
            f'<div style="color:{T.MUTED};font-size:12px;margin-top:18px">'
            f'bluemet.org</div>', unsafe_allow_html=True)

    with right:
        mrms, stamp = _mrms_layers()
        note = "MRMS 1 km reflectivity"
        if stamp:
            note += f", {stamp[9:11]}:{stamp[11:13]}Z"
        elif not mrms:
            note = "MRMS reflectivity \u2014 no current scan"
        st.markdown(
            f'<div style="color:{T.TEXT_2};font-size:12px;font-weight:700;'
            f'margin:16px 0 6px 0">{note}</div>', unsafe_allow_html=True)
        st.pydeck_chart(
            _deck(mrms + _station_layers(statuses) + _fleet_layers()),
            use_container_width=True, height=470)

    return pw or None
