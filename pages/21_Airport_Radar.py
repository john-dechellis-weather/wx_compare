"""Airport Radar (22 Oct).

One large live radar scope for any JetBlue station: the nearest
WSR-88D read as Level II from the AWS chunk feed (core/airport_radar),
or - where the airport has one - the TDWR (Level III TZL from the L3
warmer). Over it: the airport surface, range rings and finals, live
aircraft from the fleet snapshot, echo-top tags, and two alert pods:
aircraft in holding (core/tracks) and diversions (RadarBox status).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pydeck as pdk
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="BlueMet — Airport Radar", layout="wide")

from retro_theme import apply_retro_theme
apply_retro_theme()
from dark_theme import apply_dark_theme
apply_dark_theme()
from auth import check_password
check_password()

from core import airport_radar as AR
from core import airport_scope as AS
from core import live_traffic as LT

STATIC = Path(__file__).resolve().parent.parent / "static"
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
SCOPE_H = int(os.environ.get("BLUEMET_ARADAR_H", "1050"))
NAV_NM = float(os.environ.get("BLUEMET_ARADAR_NAV_NM", "80"))     # VOR-class navaids drawn
HOLD_NM = float(os.environ.get("BLUEMET_ARADAR_HOLD_NM", "30"))   # published holding fixes drawn
_FONT = "'Roboto Mono', 'DejaVu Sans Mono', Menlo, Consolas, monospace"
EDGE, PANEL, INK, INK2, MUTED = "#2D3957", "#0A0A0A", "#FFFFFF", "#B8B8B8", "#6E6E6E"

AR.ensure_warmer(STATIC)
try:
    from core import radar_l3 as L3
    L3.ensure_l3_warmer(STATIC)
except Exception:
    L3 = None
try:
    from core import etop_tags as ET
    ET.ensure_etop_tags_warmer(STATIC)
except Exception:
    ET = None
try:
    from core import fleet as FL
    FL.ensure_fleet_warmer()
except Exception:
    FL = None

st.markdown(
    "<style>"
    f'[data-testid="stVerticalBlockBorderWrapper"]{{background:{PANEL} !important;'
    f"border:1px solid {EDGE} !important;border-radius:4px !important;padding:10px 14px 12px !important;}}"
    ".pod-title{color:#B8B8B8;font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;"
    "display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #1A2233;"
    "padding-bottom:6px;margin-bottom:8px}.pod-title span{color:#6E6E6E;font-size:10px;font-weight:400;"
    "text-transform:none;letter-spacing:0}"
    "</style>", unsafe_allow_html=True)


def pod_title(t, sub=""):
    st.markdown(f'<div class="pod-title">{t}<span>{sub}</span></div>', unsafe_allow_html=True)


def _origin() -> str:
    try:
        host = st.context.headers.get("Host", "")
        if host:
            return f"{st.context.headers.get('X-Forwarded-Proto', 'https')}://{host}"
    except Exception:
        pass
    return (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


# ------------------------------------------------------------- controls
_stations = AR.stations()
_codes = [s[0] for s in _stations]
_default = _codes.index("KJFK") if "KJFK" in _codes else 0
h1, h2, h3 = st.columns([1.2, 2.2, 2.6], gap="small", vertical_alignment="bottom")
with h1:
    icao = st.selectbox("Station", _codes, index=_default, key="ar_icao",
                        format_func=lambda k: f"{k[1:] if k[0] == 'K' and len(k) == 4 else k}  ·  "
                                              f"{next((s[1] for s in _stations if s[0] == k), k)[:28]}")
rad = AR.radars_for(icao)
l2_site, l2_km = rad.get("l2", ("", 0.0))
tdwr = rad.get("tdwr")
opts = [f"Level II · {l2_site} (WSR-88D, {l2_km * 0.539957:.0f} nm)"]
if tdwr:
    opts.append(f"TDWR · {tdwr[2:]} (Level III TZL)")
with h2:
    pick = st.radio("Radar", opts, horizontal=True, key=f"ar_radar_{icao}", label_visibility="visible")
use_tdwr = pick.startswith("TDWR")
with h3:
    st.markdown(
        f'<div style="font-size:16px;font-weight:700;color:{INK};-webkit-text-fill-color:{INK}">AIRPORT RADAR</div>'
        f'<div style="font-size:11px;color:{INK2};-webkit-text-fill-color:{INK2}">live Level II from the nearest WSR-88D '
        '(TDWR where the airport has one) · aircraft, holding and diversion alerts, echo tops</div>',
        unsafe_allow_html=True)

coords = (rad.get("lat"), rad.get("lon"))
if coords[0] is None:
    st.error(f"No position for {icao}.")
    st.stop()
if not use_tdwr:
    AR.request(STATIC, l2_site, icao, coords[0], coords[1])


# ------------------------------------------------------------- the scope
@st.fragment(run_every=60)
def _scope():
    now = datetime.now(timezone.utc)
    base = _origin()
    if not use_tdwr:
        AR.request(STATIC, l2_site, icao, coords[0], coords[1])
    # frames
    frames, rtxt = [], ""
    if use_tdwr and L3 is not None:
        dom = L3.station_domain(icao) if icao.upper() in L3.STATION_RADAR else None
        if dom:
            try:
                L3.build(dom, STATIC, wait=False, site=tdwr)
            except Exception:
                pass
            fr = [f for f in L3.frames(STATIC, dom) if L3.age_s(f["stamp"]) < 4800]
            frames = [{"url": f"{base}/app/static/{f['name']}", "bounds": f["bounds"],
                       "label": f"{f['stamp'][9:11]}:{f['stamp'][11:13]}Z"} for f in fr]
            rtxt = f"TDWR {tdwr[2:]} · {len(frames)} frames" if frames else f"TDWR {tdwr[2:]}: no frames yet"
    else:
        man = AR.manifest(STATIC, l2_site)
        # frames older than 90 min are history, not a loop (the warmer
        # may have been down); leave them out
        _fresh = [f for f in man.get("frames", [])
                  if (now - datetime.strptime(f["stamp"], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)).total_seconds() < 5400]
        if _fresh and man.get("bounds"):
            man = dict(man, frames=_fresh)
            frames = [({"bin_url": f"{base}/app/static/{f['name']}", "g0": f["g0"], "gw": f["gw"],
                        "radar": man["radar"], "label": f["valid"]} if f.get("bin") else
                       {"url": f"{base}/app/static/{f['name']}", "bounds": man["bounds"], "label": f["valid"], "raw": True})
                      for f in man["frames"]]
            rtxt = f"{l2_site} Level II 0.5° · {len(frames)} frames · latest {man['frames'][-1]['valid']}"
        else:
            rtxt = f"{l2_site} Level II: first volume rendering (about a minute)"
    # layers: surface, rings, finals, aircraft
    surface = AS.surface(CACHE_ROOT / "scope", icao, coords[0], coords[1])
    # LIVE AIRCRAFT (22 Oct): a point query every LIVE_S around this
    # airport feeds aradar_live_<ICAO>.json; the viewer polls it and
    # dead-reckons between fixes. The server-side aircraft are only
    # the first paint until that file is fresh (and leaving them out
    # keeps the HTML stable, so the iframe is not reloaded every run).
    LT.demand(icao, coords[0], coords[1], STATIC)
    live_ok = LT.fresh(STATIC, icao, 60)
    try:
        ac_all = [] if live_ok else AS.traffic(coords[0], coords[1], 150)
    except Exception:
        ac_all = []
    ac = [a for a in ac_all if a.get("jbu") or (a.get("airline") and AS.distance_nm(
        coords[0], coords[1], a["position"][1], a["position"][0]) <= 60)]
    layers, cfg = AS.mini_layers(icao, surface, coords[0], coords[1], mrms_chunks=None,
                                 base_url=base, range_nm=20, ac=ac)
    # holding: ring the aircraft that look to be holding
    holding, exited = [], []
    try:
        from core import tracks as TR
        TR.update_holds()
        holding, exited = TR.hold_events()
    except Exception:
        pass
    hold_cs = {h[0] for h in holding}
    hold_rows = [{"position": a["position"], "cs": a.get("callsign", "")} for a in ac_all
                 if a.get("callsign") in hold_cs]
    if hold_rows:
        layers.append(pdk.Layer("ScatterplotLayer", hold_rows, id="hold_ring", get_position="position",
                                get_radius=2600, radius_min_pixels=16, radius_max_pixels=26,
                                filled=False, stroked=True, get_line_color=[255, 212, 0, 255],
                                line_width_min_pixels=2.5, pickable=False))
        layers.append(pdk.Layer("TextLayer", hold_rows, id="hold_lab", get_position="position", get_text="cs",
                                get_color=[255, 212, 0, 255], get_size=2600, size_min_pixels=0,
                                size_max_pixels=12, get_pixel_offset=[0, -22],
                                get_text_anchor='"middle"', pickable=False))
    # NAVAIDS (22 Oct): VOR-class navaids within NAV_NM and published
    # holding fixes within HOLD_NM, from the FAA NASR cycle the
    # system map already caches. Navaid = ring + centre dot + ident;
    # hold fix = small triangle + ident. Under the aircraft.
    try:
        from core import navdata as ND
        ND.ensure(CACHE_ROOT)
        _nd = ND.load(CACHE_ROOT) or {}
    except Exception:
        _nd = {}
    _navs = [{"position": [n["lon"], n["lat"]], "id": n["id"], "name": f"{n['type']} {n['id']} {n['name']}"}
             for n in _nd.get("navaids", [])
             if AS.distance_nm(coords[0], coords[1], n["lat"], n["lon"]) <= NAV_NM]
    _seen = set()
    _holds = []
    for h in _nd.get("holds", []):
        if h["id"] in _seen or AS.distance_nm(coords[0], coords[1], h["lat"], h["lon"]) > HOLD_NM:
            continue
        _seen.add(h["id"])
        _holds.append({"position": [h["lon"], h["lat"]], "id": h["id"],
                       "name": f"hold {h['id']} {h.get('course') or ''}° {h.get('turn') or ''}"})
    _NAV_C = [170, 200, 255, 230]
    _HLD_C = [230, 190, 60, 165]
    if _holds:
        layers.append(pdk.Layer("ScatterplotLayer", _holds, id="nav_holds", get_position="position",
                                get_radius=400, radius_min_pixels=3, radius_max_pixels=4,
                                filled=True, get_fill_color=_HLD_C, pickable=False))
        layers.append(pdk.Layer("TextLayer", _holds, id="nav_holds_lab", get_position="position", get_text="id",
                                get_color=_HLD_C, get_size=3000, size_min_pixels=0, size_max_pixels=9,
                                get_pixel_offset=[0, -7], get_text_anchor='"middle"',
                                get_alignment_baseline='"bottom"', font_family=_FONT, font_weight=700,
                                pickable=False))
    if _navs:
        layers.append(pdk.Layer("ScatterplotLayer", _navs, id="nav_ring", get_position="position",
                                get_radius=900, radius_min_pixels=7, radius_max_pixels=9,
                                filled=False, stroked=True, get_line_color=_NAV_C,
                                line_width_min_pixels=1.5, pickable=False))
        layers.append(pdk.Layer("ScatterplotLayer", _navs, id="nav_dot", get_position="position",
                                get_radius=200, radius_min_pixels=2, radius_max_pixels=2,
                                filled=True, get_fill_color=_NAV_C, pickable=False))
        layers.append(pdk.Layer("TextLayer", _navs, id="nav_lab", get_position="position", get_text="id",
                                get_color=_NAV_C, get_size=3000, size_min_pixels=0, size_max_pixels=12,
                                get_pixel_offset=[11, 0], get_text_anchor='"start"',
                                get_alignment_baseline='"center"', font_family=_FONT, font_weight=700,
                                pickable=False))
    # echo-top tags
    tags = []
    try:
        tags = (ET.read(STATIC) or {}).get("tags") or [] if ET else []
    except Exception:
        tags = []
    tags = [t for t in tags if AS.distance_nm(coords[0], coords[1], t["lat"], t["lon"]) <= 180]
    if tags:
        layers.append(pdk.Layer("TextLayer", [{"position": [t["lon"], t["lat"]], "text": f"{t['fl']}"} for t in tags],
                                get_position="position", get_text="text", get_color=[255, 255, 255, 255],
                                get_size=3000, size_min_pixels=0, size_max_pixels=13,
                                get_text_anchor='"middle"', get_alignment_baseline='"center"',
                                background=True, get_background_color=[0, 0, 0, 200],
                                get_border_color=[255, 255, 255, 255], get_border_width=1,
                                background_padding=[4, 2, 4, 2], font_weight=700, pickable=False))
    # view: whole radar box
    _vs = AS.view(coords[0], coords[1], width_px=1000, width_nm=120)
    import math as _m
    _vs.min_zoom = round(_m.log2(156543.03 * _m.cos(_m.radians(coords[0])) * 1000 / (400 * 1609.34)), 2)
    style = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
    from core.scope_html import scope_html
    _tip = {"html": "<b>{callsign}</b> {type}<br/>{alt} ft &middot; {gs} kt",
            "style": {"backgroundColor": "#0A0A0A", "color": "#FFFFFF",
                      "border": f"1px solid {EDGE}", "fontSize": "12px"}}
    deck = pdk.Deck(layers=layers, initial_view_state=_vs,
                    views=[pdk.View(type="MapView", controller={"scrollZoom": True, "dragPan": True,
                                                                  "dragRotate": False, "doubleClickZoom": True,
                                                                  "inertia": True})],
                    map_style=None, map_provider=None, parameters={"clearColor": [0, 0, 0, 1]})
    html = scope_html(json.loads(deck.to_json()), frames, style, icao, height=SCOPE_H, tooltip=_tip,
                      live_url=f"{base}/app/static/{LT.path(STATIC, icao).name}", declutter=True,
                      autoplay=False)
    name = f"aradar_scope_{icao}_{'tdwr' if use_tdwr else 'l2'}.html"
    v = hashlib.md5(html.encode()).hexdigest()[:10]
    p = STATIC / name
    if not p.exists() or p.read_text() != html:
        tmp = STATIC / f".{name}.tmp"
        tmp.write_text(html)
        os.replace(tmp, p)
    _lst = LT.STATUS.get(icao.upper(), {})
    _n_ac = _lst.get("n", len(ac)) if live_ok else len(ac)
    _src = (f"live {_lst.get('host', '')} every {LT.LIVE_S:.0f} s" if live_ok
            else ("live feed starting" if not _lst.get("err") else f"live feed: {_lst['err']}"))
    st.caption(f"{rtxt} · {_n_ac} aircraft ({_src}) · {len(tags)} echo-top tags · "
               f"{len(_navs)} navaids · {len(_holds)} holding fixes · "
               f"ATIS {cfg.get('code', '') or '-'} · {now:%H:%MZ}")
    components.iframe(f"{base}/app/static/{name}?v={v}", height=SCOPE_H + 44)

    # ------------------------------------------------------------ alert pods
    c1, c2 = st.columns(2, gap="small")
    with c1:
        with st.container(border=True):
            pod_title("Aircraft in holding", f"{len(holding)} now · {len(exited)} recently exited")
            if not holding and not exited:
                st.caption("No aircraft show a holding pattern.")
            for cs, held_min, path_nm, turn, _hx in holding:
                st.markdown(f'<div style="color:#FFD400;-webkit-text-fill-color:#FFD400;font-weight:700;'
                            f'font-family:DejaVu Sans Mono,monospace">⚠ {cs} holding {held_min:.0f} min '
                            f'<span style="color:{INK2};-webkit-text-fill-color:{INK2};font-weight:400">'
                            f'({path_nm:.0f} nm flown, {turn:.0f}° of turn)</span></div>',
                            unsafe_allow_html=True)
            for cs, held_min, ago_min, _hx in exited:
                st.markdown(f'<div style="color:{INK2};-webkit-text-fill-color:{INK2};'
                            f'font-family:DejaVu Sans Mono,monospace">{cs} exited hold {ago_min:.0f} min ago '
                            f'after {held_min:.0f} min</div>', unsafe_allow_html=True)
    with c2:
        with st.container(border=True):
            divs = []
            try:
                from core import radarbox as RBX
                doc = RBX.cached(icao)
                for f in (doc or {}).get("flights") or []:
                    stt = (f.get("status") or "").upper()
                    arr = (f.get("arrAirportIcao") or "").upper()
                    if "DIVERT" in stt or (arr and arr != icao.upper() and stt in ("IN_FLIGHT", "LANDED")):
                        divs.append((f.get("callsign") or f.get("flightNumberIata") or "?",
                                     f.get("depAirportIcao") or "", arr, stt))
            except Exception:
                doc = None
            pod_title("Diversion alerts", "RadarBox status for flights due here" if doc else "no RadarBox data for this station")
            if not divs:
                st.caption("No diversions among the flights due at this station.")
            for cs, dep, arr, stt in divs:
                st.markdown(f'<div style="color:#FF5A5A;-webkit-text-fill-color:#FF5A5A;font-weight:700;'
                            f'font-family:DejaVu Sans Mono,monospace">⚠ {cs} {dep} → {arr or "?"} '
                            f'<span style="color:{INK2};-webkit-text-fill-color:{INK2};font-weight:400">{stt.replace("_", " ").title()}</span></div>',
                            unsafe_allow_html=True)


_scope()

with st.expander("radar status", expanded=False):
    st.caption(f"Level II warmer: {'busy ' + AR.STATUS['busy'] if AR.STATUS.get('busy') else 'idle'}"
               + (f" | last error: {AR.STATUS['err']}" if AR.STATUS.get("err") else "")
               + f" | warming: {', '.join(AR.demanded(STATIC)) or 'none'}")
    st.code("\n".join(AR.log_tail(STATIC, 8)) or "(no log yet)")
