"""N90 Airspace Forecast — CAM reflectivity over the live map.

The companion to the Airspace page. That one answers "what is the
airspace doing now"; this one answers "what will it be doing", with
the same fix structure, Class B shelves and route geometry underneath
so the two read identically.

WHAT IS FORECAST AND WHAT IS LIVE, stated plainly because mixing them
is the one genuinely dangerous thing this page does:

  * The REFLECTIVITY FIELD is a model forecast, valid at the hour on
    the slider.
  * Everything else — aircraft, fix structure, airspace — is live or
    static. Aircraft are where they are NOW, not where the model
    thinks they will be at F+06.

A page showing 6-hour-out convection under this minute's traffic
invites exactly the wrong inference, so the valid time is stated
above the map and again in the caption, and the aircraft layer is
labelled live every time.

MODEL REFLECTIVITY IS NOT RADAR. HRRR simulated composite is a
model's idea of where cells will be, and it is routinely right about
the regime and wrong about the placement of any individual storm.
Read it for coverage and timing, not for whether a specific arrival
gate is blocked at a specific minute.
"""

from __future__ import annotations

import os
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

from retro_theme import apply_retro_theme

apply_retro_theme()

# FIRST ACTION ON EVERY RERUN. The overlay warmer checks this
# timestamp before each frame and stands down while the site is in
# use, so a person scrubbing holds the warmer off rather than
# competing with it for the GIL.
try:
    from core.cam_overlay import note_request as _note_req

    _note_req()
except Exception:
    pass

from auth import check_password

check_password()

from core import airspace as AS
from core import cam_overlay as OVL

_STATIC = Path(__file__).resolve().parent.parent / "static"
# RRFS CONUS lives in the OPS bucket, noaa-rrfs-ops-pds, NOT in
# rrfs_a — that one carries only Hawaii and Puerto Rico, which is
# what makes "RRFS CONUS was retired" a tempting and wrong
# conclusion. core.hrrr_cam already resolves the right path.
#
# The three differ in ways that matter for how this page is read:
#   HRRR       f18, hourly cycles      - freshest, shortest
#   RRFS       f84, 3-hourly cycles    - longest range
#   REFS mean  f60, 6-hourly cycles    - ensemble central tendency
#
# The mean smooths displaced cells into broad signal, which is
# honest about uncertainty but understates peak intensity. PMMN
# keeps realistic reflectivity structure and is what HREF-style
# products are built on — better for "does this look like a squall
# line", worse for "how likely is this".
MODEL_CHOICES = {
    "HRRR": "hrrr",
    "RRFS": "rrfs",
    "REFS ensemble (PMMN)": "refs_pmmn",
    "REFS probabilities": "refs_prob",
}

# PRODUCTS PER MODEL. HRRR and RRFS are deterministic and carry the
# five fields. REFS PMMN publishes composite reflectivity — the
# probability-matched mean, which keeps realistic cell structure
# while averaging their placement. REFS prob is the exceedance
# fields: percent of members past a threshold, which is a different
# kind of picture and gets its own palette (0-100 %).
PRODUCT_CHOICES_BY_MODEL = {
    "hrrr": {
        "1 km reflectivity": "REFD",
        "Visibility": "VIS",
        "Ceiling": "CEIL",
        "Echo tops": "RETOP",
        "10 m wind gust": "GUST",
    },
    "refs_pmmn": {
        "Composite reflectivity (PMMN)": "REFC",
    },
    "refs_prob": {
        "P(ceiling < 1,000 ft)": "PROB_CIG1000",
        "P(visibility < 1 sm)": "PROB_VIS1",
        "P(reflectivity > 40 dBZ)": "PROB_REFC40",
        "P(ceiling < 500 ft)": "PROB_CIG500",
        "P(ceiling < 2,000 ft)": "PROB_CIG2000",
        "P(visibility < 1/2 sm)": "PROB_VIS05",
        "P(visibility < 3 sm)": "PROB_VIS3",
        "P(echo tops > FL300)": "PROB_RETOP30",
        "P(echo tops > FL350)": "PROB_RETOP35",
    },
}
PRODUCT_CHOICES_BY_MODEL["rrfs"] = PRODUCT_CHOICES_BY_MODEL["hrrr"]
PRODUCT_CHOICES = PRODUCT_CHOICES_BY_MODEL["hrrr"]

st.title("N90 Airspace Forecast")
st.caption(
    "Simulated reflectivity over the terminal area. Model output, "
    "not radar."
)


@st.cache_data(ttl=86400, show_spinner=False)
def _airspace():
    return AS.load_airspace()


data = _airspace()
for _e in data.get("errors", []):
    st.warning(f"Asset unavailable — {_e}")

# ---------------------------------------------------------------------------
# Warm status
# ---------------------------------------------------------------------------
# ALWAYS VISIBLE, above the controls. "Is RRFS being warmed?" should
# not require opening a diagnostics box. One line per model in
# OVL_MODELS: newest short cycle and its depth, newest long cycle and
# its depth, how many products, and how old the newest frame is —
# which is the number that says "constantly", or does not.
_warm_models = [m.strip() for m in os.environ.get(
    "OVL_MODELS", "hrrr").split(",") if m.strip()]
_ws_rows = []
_now = datetime.now(timezone.utc)
for _m in _warm_models:
    _label = next((k for k, v in MODEL_CHOICES.items() if v == _m), _m)
    _short_c = OVL.cycle_on_disk(_m, _STATIC, "REFD")
    _short_h = (OVL.available(_m, _STATIC, _short_c, "REFD")
                if _short_c else [])
    _long_cs = OVL.long_cycles(_m, _STATIC, "REFD")
    _long_h = (OVL.available(_m, _STATIC, _long_cs[0], "REFD")
               if _long_cs else [])
    _prods = [pn for pn in OVL.WARM_PRODUCTS
              if OVL.cycle_on_disk(_m, _STATIC, pn)]
    _newest = max((f.stat().st_mtime for f in
                   _STATIC.glob(f"ovl_{_m}_*.png")), default=None)
    _age = ((_now.timestamp() - _newest) / 60.0) if _newest else None
    if _short_c:
        _cyc_age = (_now - datetime.strptime(_short_c, "%Y%m%d%H")
                    .replace(tzinfo=timezone.utc)).total_seconds() / 3600
        _txt = (f"**{_label}** &mdash; {_short_c[-2:]}Z cycle "
                f"({_cyc_age:.1f} h old), f{min(_short_h):02d}\u2013"
                f"f{max(_short_h):02d}")
        if _long_cs:
            _txt += (f" &middot; long run {_long_cs[0][-2:]}Z to "
                     f"f{max(_long_h):02d}")
        else:
            _txt += " &middot; no long run yet"
        _txt += (f" &middot; {len(_prods)}/{len(OVL.WARM_PRODUCTS)} "
                 f"products &middot; last frame written "
                 f"{_age:.0f} min ago")
        _ws_rows.append(("ok" if _age is not None and _age < 90
                         else "stale", _txt))
    else:
        _ws_rows.append(("none", f"**{_label}** &mdash; nothing warmed "
                                 f"yet"))
_missing = [k for k, v in MODEL_CHOICES.items() if v not in _warm_models]
with st.container(border=True):
    st.markdown("**Warmer status** &mdash; models in `OVL_MODELS`: "
                + ", ".join(f"`{m}`" for m in _warm_models))
    for _state, _txt in _ws_rows:
        (st.markdown if _state == "ok" else st.warning)(_txt)
    if _missing:
        st.caption(
            f"Not being warmed: {', '.join(_missing)}. Add to "
            f"OVL_MODELS in Render \u2014 e.g. "
            f"`OVL_MODELS=hrrr,rrfs,refs_pmmn,refs_prob` \u2014 to warm it. "
            f"Each model "
            f"is its own fetch and render per cycle.")

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
c = st.columns([1, 1, 1, 1, 2])
with c[0]:
    show_fix = st.checkbox("Fixes", value=True)
    show_hull = st.checkbox("N90 extent", value=True)
with c[1]:
    show_cb = st.checkbox("Class B", value=True)
    show_rt = st.checkbox("Routes", value=True)
    show_oc = st.checkbox("Oceanic", value=True)
    show_ar = st.checkbox("ARTCC", value=True)
with c[2]:
    show_cam = st.checkbox("Forecast", value=True)
    show_ac = st.checkbox("Traffic (live)", value=True)
with c[3]:
    _mlabel = st.selectbox("Model", list(MODEL_CHOICES), index=0)
    MODEL = MODEL_CHOICES[_mlabel]
    PRODUCT_CHOICES = PRODUCT_CHOICES_BY_MODEL.get(
        MODEL, PRODUCT_CHOICES_BY_MODEL["hrrr"])
    _pl = st.selectbox("Product", list(PRODUCT_CHOICES), index=0,
                       key=f"product_{MODEL}")
    PRODUCT = PRODUCT_CHOICES[_pl]
    opacity = st.slider("Opacity", 0.2, 1.0, 0.75, 0.05)
with c[4]:
    radius_nm = st.select_slider(
        "View radius (nm)", options=[100, 150, 200, 250, 300, 350, 400],
        value=250)

# ---------------------------------------------------------------------------
# Forecast hour
# ---------------------------------------------------------------------------
# Frames come from the warmer started in Homepage.py. Nothing is
# rendered on this page: a CAM frame is a fetch, a decode and a
# matplotlib render, which is far too slow for a request thread.
# LONG RUN TOGGLE. HRRR runs hourly to f18 but its 00/06/12/18Z
# cycles reach f48; RRFS runs 3-hourly with the synoptic cycles
# reaching f84. The freshest run is the default because it is the
# best answer for the next few hours; the deep run is for planning
# and is by definition older.
# REFS has no short/long split: every run goes to f60, four times a
# day, and the warmer keeps the newest complete one. The toggle only
# makes sense for the models that have a freshness-vs-depth choice.
_is_refs = MODEL.startswith("refs")
_long_avail = [] if _is_refs else OVL.long_cycles(MODEL, _STATIC, PRODUCT)
_long_h = OVL.LONG_FHR.get(MODEL, 48)
want_long = False if _is_refs else st.toggle(
    f"{_mlabel} full {_long_h}-hour run",
    value=False, disabled=not _long_avail,
    help=f"{_mlabel} synoptic cycles (00/06/12/18Z) run to "
         f"f{_long_h}. Off, the page shows the freshest cycle, which "
         f"is more current but stops at f"
         f"{OVL.SHORT_FHR.get(MODEL, 18)}."
    if _long_avail else
    f"No {_long_h}-hour run warmed yet for this product.")
if _is_refs:
    st.caption(f"{_mlabel}: 00/06/12/18Z runs to f60; the newest complete "
               f"run is shown. Pre-implementation feed \u2014 a late or "
               f"missing cycle falls back to the previous one.")

if want_long and _long_avail:
    _cycle = _long_avail[0]
else:
    _cycle = OVL.cycle_on_disk(MODEL, _STATIC, PRODUCT)
_hours = (OVL.available(MODEL, _STATIC, _cycle, PRODUCT)
          if _cycle else [])

# THE HOUR SLIDER LIVES IN THE BROWSER. Every frame for this cycle
# and product is handed to the component below, preloaded, and
# swapped client-side — no rerun per step. Only model, product and
# the long-run toggle rerun, because those change which frames exist.
_frames = []
_base = (os.environ.get("RENDER_EXTERNAL_URL")
         or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
if _hours and _base:
    _init = datetime.strptime(_cycle, "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    _now = datetime.now(timezone.utc)
    for _h in _hours:
        _name = OVL.frame_name(MODEL, _cycle, _h, PRODUCT)
        if not (_STATIC / _name).exists():
            continue
        _v = _init + timedelta(hours=_h)
        _frames.append({
            "url": f"{_base}/app/static/{_name}",
            "bounds": OVL.bounds(),
            "valid": _v.isoformat(),
            "label": (f"F+{_h:02d}  {_v:%H%MZ %d %b}"
                      + ("  (past)" if _v < _now - timedelta(minutes=30)
                         else "")),
        })
    _cyc_age = (_now - _init).total_seconds() / 3600.0
    st.caption(
        f"{_mlabel} {_cycle[-2:]}Z cycle, initialised "
        f"{_cyc_age:.1f} h ago \u2014 {len(_frames)} frames preloaded. "
        f"Drag the slider under the map; it is instant after the "
        f"first load. Aircraft positions are LIVE.")
elif _hours and not _base:
    st.warning("RENDER_EXTERNAL_URL unset \u2014 no absolute URL for "
               "the frames.")
elif show_cam:
    st.info(
        f"No {_mlabel} {_pl} frames on disk yet. The overlay warmer "
        f"builds them in the background after each model cycle; the "
        f"first run takes a minute or two. Models are warmed only if "
        f"listed in OVL_MODELS (currently "
        f"{os.environ.get('OVL_MODELS', 'hrrr')})."
    )

# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
# Geometry goes to the component as plain dicts. The component draws
# it; nothing here is a pydeck Layer.
_sep = radius_nm / 22.0
_lab, _kept = [], []
for _f in data.get("fixes", []):
    _c = math.cos(math.radians(_f["lat"]))
    if all(math.hypot((_f["lon"] - k[1]) * 60.0 * _c,
                      (_f["lat"] - k[0]) * 60.0) >= _sep for k in _kept):
        _kept.append((_f["lat"], _f["lon"]))
        _lab.append(_f)

_ac, _ac_note = [], ""
if show_ac:
    try:
        import requests

        la, lo = AS.N90_CENTER
        r = requests.get(
            f"https://api.adsb.lol/v2/point/{la:.2f}/{lo:.2f}/"
            f"{min(int(radius_nm), 250)}",
            timeout=8, headers={"User-Agent": "n90 airspace"})
        # EVERY AIRBORNE AIRCRAFT, airline and private alike, coloured
        # by operator with the same colours as the other maps. A
        # callsign-less aircraft shows its registration. Labels go on
        # JetBlue and the majors; the rest are dots with a tooltip,
        # or six hundred labels would bury the forecast.
        from core.icons import (_OPERATOR_FILL, _OTHER_FILL, _who, atc_icon,
                                has_block, data_block)

        def _rgb(h):
            h = h.lstrip("#")
            return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [235]

        _n_jbu = _n_other = 0
        for p in ((r.json() or {}).get("ac") or []) if \
                r.status_code == 200 else []:
            cs = (p.get("flight") or "").strip().upper() \
                or (p.get("r") or "").strip().upper()
            alt = p.get("alt_baro")
            if not cs or alt in ("ground", None):
                continue
            try:
                alt = int(alt)
                pre = cs[:3]
                mine = cs.startswith("JBU")
                col = ("#005ADC" if mine else
                       _OPERATOR_FILL.get(pre, _OTHER_FILL))
                _n_jbu += mine
                _n_other += not mine
                _ac.append({
                    "lat": float(p["lat"]), "lon": float(p["lon"]),
                    "cs": cs, "hex": (p.get("hex") or "").lower(),
                    "col": _rgb(col),
                    "atc": atc_icon(has_block(cs))["url"],
                    "lbl": (data_block(cs, alt, p.get("gs"))
                            if has_block(cs) else ""),
                    "tip": f"{_who(cs)} | "
                           + (f"FL{round(alt / 100):03d}" if alt >= 18000
                              else f"{alt:,} ft")
                           + (f" | {p.get('t')}" if p.get("t") else ""),
                })
            except (TypeError, ValueError, KeyError):
                continue
        _ac_note = (f"{len(_ac)} aircraft (LIVE): {_n_jbu} JetBlue, "
                    f"{_n_other} other, airline and private")
        if show_oc and data.get("oceanic"):
            from core import fleet as _FL

            def _row(p, cs, mine):
                try:
                    alt = p.get("alt_baro")
                    alt = None if alt in ("ground", None) else int(alt)
                    return {"lat": float(p["lat"]),
                            "lon": float(p["lon"]), "cs": cs,
                            "hex": (p.get("hex") or "").lower(),
                            "_alt": alt or 0,
                            "_gs": float(p.get("gs") or 0),
                            "col": _rgb("#005ADC" if mine else
                                        _OPERATOR_FILL.get(cs[:3], _OTHER_FILL)),
                            "atc": atc_icon(has_block(cs))["url"],
                            "lbl": (data_block(cs, alt, p.get("gs"))
                                    if has_block(cs) else ""),
                            "tip": f"{cs} | "
                                   + (f"FL{round(alt / 100):03d}" if alt
                                      and alt >= 18000 else
                                      (f"{alt:,} ft" if alt else "GND"))}
                except (TypeError, ValueError, KeyError):
                    return None
            _flr, _fln = _FL.sweep(data["oceanic"], _row)
            _seen = {r.get("hex") for r in _ac if r.get("hex")}
            _ac += [r for r in _flr if r["hex"] not in _seen
                    and r.get("on_lroute")]
            _ac_note += f"; {_fln}"
    except Exception as _exc:
        _ac_note = f"traffic unavailable ({type(_exc).__name__})"

_geo = {
    "fixes": data.get("fixes", []) if show_fix else [],
    "fix_labels": _lab if show_fix else [],
    "classb": data.get("classb", []) if show_cb else [],
    "hull": data.get("hull", []) if show_hull else [],
    "routes": data.get("routes", []) if show_rt else [],
    "route_labels": data.get("route_labels", []) if show_rt else [],
    "artcc": data.get("artcc", []) if show_ar else [],
    "oceanic": data.get("oceanic", []) if show_oc else [],
    "oceanic_labels": data.get("oceanic_labels", []) if show_oc else [],
    "aircraft": _ac,
}


def _zoom_for(radius_nm, px=1400.0):
    """SAME FORMULA AS PAGE 1. The version this replaced was a
    from-memory approximation 0.73 zoom levels too far out at every
    radius — 250 nm on the slider showed Chicago. World is 512*2**z
    px wide; a radius in nm converts to degrees of LONGITUDE via
    cos(lat)."""
    deg = 2.0 * radius_nm / 60.0 / math.cos(math.radians(AS.N90_CENTER[0]))
    return round(math.log2(px * 360.0 / (512.0 * deg)), 2)


import streamlit.components.v1 as _components

from core.scrubber import scrubber_html

_components.html(
    scrubber_html(
        _frames if show_cam else [], _geo,
        {"lat": AS.N90_CENTER[0], "lon": AS.N90_CENTER[1],
         "zoom": _zoom_for(radius_nm)},
        opacity, height=760, dark=True),
    height=770)

_cam_note = (f"{_mlabel} {_cycle} \u2014 "
             f"{OVL.scale_for(PRODUCT)['label']}, "
             f"{len(_frames)} frames" if _frames else "")

# ---------------------------------------------------------------------------
# Gate impact from REFS
# ---------------------------------------------------------------------------
# When each arrival gate closes to thunderstorms and when it opens,
# from the newest REFS cycle: PMMN composite reflectivity within
# 10 nm of the gate (>= 40 dBZ closed, 30-40 marginal) and the share
# of members past 40 dBZ as confidence. Sampled by the warmer as it
# decodes each hour; this page only reads the file.
from core import gate_impact as GIMP

_gsrc = st.columns([1, 1, 1])
_gi_source_lbl = _gsrc[0].radio(
    "Source", ["REFS ensemble (PMMN)", "RRFS 84-h run"], index=0,
    horizontal=True, key="gi_source",
    help="REFS: probability-matched-mean composite, with the share of "
         "members past 40 dBZ as confidence. RRFS: the deterministic 1 km "
         "reflectivity out to f84 \u2014 one solution, no confidence column.")
_gi_source = "refs" if _gi_source_lbl.startswith("REFS") else "rrfs"
_gi_thr_lbl = _gsrc[1].radio(
    "Thresholds", ["Operational 30 / 40 dBZ", "Test 20 / 30 dBZ"], index=0,
    horizontal=True, key="gi_thr",
    help="Operational is the line pilots deviate around and SWAP "
         "triggers on. Test lowers both tiers so the display can be "
         "exercised on a day with only light returns.")
_gi_marg, _gi_closed = (30.0, 40.0) if _gi_thr_lbl.startswith("Oper") else (20.0, 30.0)
_gi_raw = GIMP.load(_STATIC, source=_gi_source)
if not _gi_raw:
    _ovl_env = os.environ.get("OVL_MODELS", "")
    _has_refs = "refs_pmmn" in _ovl_env
    if _gi_source == "rrfs":
        st.warning("Gate impact outlook: no RRFS samples yet. They are "
                   "written as the warmer decodes RRFS reflectivity frames "
                   "after this deploy; the first pass fills the short run "
                   "within a few minutes and the 84-hour run on its next "
                   "sweep.")
        _has_refs = True
    else:
      st.warning(
          "Gate impact outlook: no REFS data yet. "
          + ("`refs_pmmn` is in OVL_MODELS, so the warmer will sample the "
               "gates as it decodes the first REFS cycle \u2014 roughly 10 "
               "minutes after its HRRR and RRFS passes finish, up to an hour "
               "after a restart. Check the overlay warmer log in the "
               "diagnostics above for `refs_pmmn`."
               if _has_refs else
               f"OVL_MODELS in Render is `{_ovl_env or '(unset)'}` \u2014 it must "
               f"include `refs_pmmn` and `refs_prob` for REFS to be fetched at "
               f"all. Set it to `hrrr,rrfs,refs_pmmn,refs_prob` and redeploy."))
else:
    _gi_span = _gsrc[2].radio(
        "Outlook window",
        ["24 h", "36 h", "60 h"] + (["84 h"] if _gi_source == "rrfs" else []),
        index=1, horizontal=True, key=f"gi_span_{_gi_source}")
    _gi_hours = int(_gi_span.split()[0])
    _tl = GIMP.timeline(_gi_raw, hours=_gi_hours, marginal=_gi_marg,
                        closed=_gi_closed)
    _cyc = datetime.strptime(_tl["cycle"], "%Y%m%d%H").replace(
        tzinfo=timezone.utc)
    _hrs = _tl["hours"]
    _now = datetime.now(timezone.utc)
    # Only hours still ahead of us are worth a column.
    _cols = [h for h in _hrs if _cyc + timedelta(hours=h) >= _now - timedelta(hours=1)]
    # THE LOOK OF THE MOCK: a dark panel, sans-serif, the fixes down
    # the left, valid hours across, green/yellow/red cells with the
    # dBZ printed only where it matters, and the episode table in the
    # same panel beneath. Rows come from the lists in core/gate_impact,
    # never from the role stored in the file, so an impact file
    # written by an earlier version still displays.
    _cell = {0: "#7BC96F", 1: "#F1C232", 2: "#C0392B"}
    _txt = {0: "#7BC96F", 1: "#1A1A1A", 2: "#FFFFFF"}
    _panel = ("background:#161616;color:#EDEDED;border-radius:8px;"
              "padding:14px 16px 10px;margin:6px 0 14px;"
              "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif")
    _hd = ("background:#262626;color:#EDEDED;font-weight:700;font-size:13px;"
           "padding:5px 8px;border:1px solid #333;text-align:center;"
           "white-space:nowrap")
    _lb = ("background:#262626;color:#EDEDED;font-weight:700;font-size:14px;"
           "padding:5px 12px;border:1px solid #333;text-align:left;"
           "white-space:nowrap")
    _td = ("border:1px solid #2A2A2A;padding:4px 6px;text-align:center;"
           "font-size:13px;font-weight:600;min-width:34px")
    _et = ("border:1px solid #333;padding:6px 12px;font-size:14px;"
           "text-align:left;white-space:nowrap")

    def _outlook(title, names, noun):
        """One grid and one episode table for `names`, in that order."""
        names = [n for n in names if n in _tl["gates"]]
        if not names:
            st.markdown(
                f'<div style="{_panel}"><div style="font-size:15px;'
                f'font-weight:700">{title}</div><div style="color:#9A9A9A;'
                f'font-size:13px;margin-top:4px">None of these {noun} are in '
                f'this cycle\'s impact file yet \u2014 the warmer writes '
                f'them on its next {("REFS" if _gi_source == "refs" else "RRFS")} '
                f'pass.</div></div>', unsafe_allow_html=True)
            return
        src_lbl = "REFS composite reflectivity" if _gi_source == "refs" \
            else "RRFS 1 km reflectivity"
        # THE PEAK IN THE WINDOW, stated. An all-green grid should say
        # "quiet" in numbers, not leave the reader wondering whether
        # the samples are zeros.
        _pk = None
        for name in names:
            g = _tl["gates"][name]
            for h in _cols:
                i = _hrs.index(h)
                v = g["dbz"][i]
                if v is not None and (_pk is None or v > _pk[0]):
                    _pk = (v, name, _cyc + timedelta(hours=h))
        _pk_txt = (f"peak in window {_pk[0]:.0f} dBZ at {_pk[1]} "
                   f"{_pk[2]:%d %H}Z" if _pk else "no samples in window")
        rows = []
        for name in names:
            g = _tl["gates"][name]
            tds = []
            for h in _cols:
                i = _hrs.index(h)
                t, v, pr = g["tiers"][i], g["dbz"][i], g["prob40"][i]
                tip = (f"{name} valid {(_cyc + timedelta(hours=h)):%d %H}Z "
                       f"f{h:02d}: {v:.0f} dBZ" if v is not None else "")
                if pr is not None:
                    tip += f", {pr:.0f}% of members > 40"
                tds.append(f'<td title="{tip}" style="{_td};background:'
                           f'{_cell[t]};color:{_txt[t]}">'
                           f'{("%.0f" % v) if (v is not None and t) else ""}</td>')
            rows.append(f'<tr><td style="{_lb}">{name}</td>' + "".join(tds) + "</tr>")
        hdr = "".join(f'<th style="{_hd}">{(_cyc + timedelta(hours=h)):%H}Z</th>'
                      for h in _cols)
        grid = (f'<table style="border-collapse:collapse;margin-top:10px">'
                f'<tr><th style="{_hd};text-align:left">{noun} \\ valid</th>{hdr}</tr>'
                + "".join(rows) + "</table>")
        erows = []
        for name in names:
            for e in _tl["gates"][name]["episodes"]:
                if e["onset_fhr"] > _gi_hours:
                    continue
                on = _cyc + timedelta(hours=e["onset_fhr"])
                off = (_cyc + timedelta(hours=e["end_fhr"])) if e["end_fhr"] is not None else None
                cl = (f"{(_cyc + timedelta(hours=e['closed_from'])):%H}Z\u2013"
                      f"{(_cyc + timedelta(hours=e['closed_to'] + 1)):%H}Z"
                      if e["closed_from"] is not None else "\u2014")
                pm = (f"{e['max_prob40']:.0f}%" if e["max_prob40"] is not None
                      else ("\u2014" if _gi_source == "refs" else "n/a"))
                erows.append(
                    f'<tr><td style="{_et};font-weight:700">{name}</td>'
                    f'<td style="{_et}">{on:%d %H}Z</td><td style="{_et}">{cl}</td>'
                    f'<td style="{_et}">{(off.strftime("%d %H") + "Z") if off else "beyond window"}</td>'
                    f'<td style="{_et}">{e["hours"]} h</td>'
                    f'<td style="{_et}">{e["peak_dbz"]:.0f} dBZ</td>'
                    f'<td style="{_et}">{pm}</td></tr>')
        eh = "".join(f'<th style="{_hd};text-align:left;font-size:14px">{c}</th>' for c in
                     (noun.rstrip("s"), "impact begins", f"closed (\u2265 {_gi_closed:.0f})",
                      "opens", "duration", "peak", "members &gt; 40"))
        etable = (f'<table style="border-collapse:collapse;margin-top:14px">'
                  f'<tr>{eh}</tr>' + "".join(erows)
                  + (f'<tr><td colspan="7" style="{_et};color:#9A9A9A;'
                     f'font-weight:400">no {noun.rstrip("s")} reaches '
                     f'{_gi_marg:.0f} dBZ in the next {_gi_hours} h of this cycle'
                     f'</td></tr>' if not erows else "")
                  + "</table>")
        st.markdown(
            f'<div style="{_panel}">'
            f'<div style="font-size:19px;font-weight:700">Gate impact outlook '
            f'\u2014 {src_lbl} at the {title.lower()}</div>'
            f'<div style="color:#9A9A9A;font-size:13px;margin-top:2px">'
            f'{"REFS" if _gi_source == "refs" else "RRFS"} {_cyc:%d %HZ} '
            f'&middot; window {_gi_hours} h &middot; green clear &middot; '
            f'yellow {_gi_marg:.0f}\u2013{_gi_closed:.0f} dBZ marginal &middot; '
            f'red \u2265 {_gi_closed:.0f} dBZ closed &middot; '
            f'{"worst cell within 10 nm of the route in its last 75 nm into N90" if noun == "routes" else "max within 10 nm of the " + noun.rstrip("s")}'
            f' &middot; <b style="color:#EDEDED">{_pk_txt}</b></div>'
            f'<div style="overflow-x:auto">{grid}</div>'
            f'<div style="color:#9A9A9A;font-size:13px;margin-top:8px">Hover any '
            f'cell for the value'
            f'{" and the share of members over 40 dBZ" if _gi_source == "refs" else ""}.</div>'
            f'<div style="overflow-x:auto">{etable}</div></div>',
            unsafe_allow_html=True)

    _outlook("Arrival gates", GIMP.ARRIVAL_GATES, "gates")
    _outlook("Departure fixes", GIMP.DEPARTURE_FIXES, "fixes")
    _outlook("Jet routes", GIMP.ROUTE_ROWS, "routes")
    _missing = [r for r in GIMP.ROUTE_ROWS if r not in _tl["gates"]]
    if _missing:
        st.caption(f"Route rows with no data: {', '.join(_missing)} \u2014 "
                   f"no member of the pair is in the route file within 75 nm "
                   f"of N90 (J64, Q42 and Q480 were outside the bounding box "
                   f"when the routes were extracted; Q436 and Q109 stop short).")
    st.caption("REFS is a pre-implementation ensemble; a late or missing cycle "
               "falls back to the previous one. PMMN keeps cell intensity "
               "while averaging placement, so a red cell means a storm of that "
               "strength near that gate around that hour; the members column "
               "is how many of the seven agree. Advisory only.")


# ---------------------------------------------------------------------------
# Forecast diagnostics
# ---------------------------------------------------------------------------
# ON THIS PAGE, expanded whenever there are no frames. Every question
# that separates "warmer never ran" from "frames exist under a name
# the page is not looking for" is answered in one table.
with st.expander("Forecast diagnostics", expanded=not bool(_frames)):
    _kill = os.environ.get("OVL_WARMER") or "(unset = on)"
    _models_env = os.environ.get("OVL_MODELS") or "(unset = hrrr)"
    _prods_env = os.environ.get("OVL_PRODUCTS") or "(unset = all five)"
    _all_ovl = sorted(_STATIC.glob("ovl_*.png"))
    _this = sorted(_STATIC.glob(f"ovl_{MODEL}_{PRODUCT}_*.png"))
    _old_style = sorted(p for p in _all_ovl
                        if len(p.stem.split("_")) == 4)
    _cycles = OVL.cycles_on_disk(MODEL, _STATIC, PRODUCT)
    import threading as _th
    import time as _tm
    _alive = ", ".join(t.name for t in _th.enumerate()
                       if "warmer" in t.name)
    try:
        import psutil as _ps
        _up = f"{(_tm.time() - _ps.Process().create_time()) / 60:.1f} min"
    except Exception:
        _up = "(psutil unavailable)"
    st.markdown(f"""
| | |
|---|---|
| **build** | {__import__("core.version", fromlist=["BUILD"]).BUILD} |
| warmer thread alive in this process | {"**yes** — " + _alive if _alive else "**NO** — never started, or died"} |
| process uptime | {_up} |
| `OVL_WARMER` | `{_kill}`{" **← the warmer is OFF**" if _kill.lower() == "off" else ""} |
| `OVL_MODELS` / `OVL_PRODUCTS` | `{_models_env}` / `{_prods_env}` |
| cam_overlay.py has fast path / product naming | {hasattr(OVL, "render_overlay_fast")} / {hasattr(OVL, "product_for")} |
| `ovl_*.png` on disk, all models | {len(_all_ovl)} |
| frames for **{_mlabel} {_pl}** (`ovl_{MODEL}_{PRODUCT}_*`) | {len(_this)} |
| cycles on disk for this selection | {", ".join(_cycles) if _cycles else "none"} |
| old-style frames (no product in name) | {len(_old_style)}{" ← written by an old cam_overlay.py; the page cannot see these" if _old_style else ""} |
| frames handed to the scrubber | {len(_frames)} |
| `RENDER_EXTERNAL_URL` | `{_base or "MISSING"}` |
""")
    if _all_ovl and not _this:
        _seen = sorted({p.stem.split("_")[1] + "/" + p.stem.split("_")[2]
                        for p in _all_ovl if len(p.stem.split("_")) >= 5})
        st.warning(f"Frames exist but not for this selection. On disk: "
                   f"{', '.join(_seen[:12]) or 'old-style names only'}.")
    _lg = _STATIC / "overlay_warmer.log"
    if _lg.exists():
        try:
            _ll = _lg.read_text().splitlines()
            # Two tails: the newest lines, and the newest lines for the
            # SELECTED model — sixty errors from one product can push
            # the model you are debugging out of the first tail.
            _mine = [l for l in _ll if f" {MODEL} " in l or f" {MODEL}:" in l]
            st.caption(f"overlay_warmer.log \u2014 last 12 of {len(_ll)} lines")
            st.code("\n".join(_ll[-12:]) or "(empty)")
            st.caption(f"\u2026 and the last 12 of {len(_mine)} mentioning "
                       f"`{MODEL}`")
            st.code("\n".join(_mine[-12:]) or f"(no lines for {MODEL} yet)")
        except OSError as _lexc:
            st.caption(f"log unreadable: {_lexc}")
    else:
        if _kill.lower() == "off":
            st.error("OVL_WARMER is set to off in the environment, so the "
                     "warmer never starts. Delete that variable in Render "
                     "and the service will restart and begin warming.")
        else:
            st.error("overlay_warmer.log does not exist \u2014 the warmer "
                     "thread never reached its first log line (120 s "
                     "after boot). Check the Home page expander for a "
                     "FAILED import.")

st.caption(
    (f"Forecast: {_cam_note}. " if _cam_note else "")
    + (f"{_ac_note}. " if _ac_note else "")
    + "The reflectivity field is MODEL OUTPUT valid at the hour "
      "above; aircraft, fixes and airspace are live or static. "
      "Simulated reflectivity is reliable for regime and timing and "
      "unreliable for the placement of any individual cell — advisory "
      "only, not for operational use."
)


# ---------------------------------------------------------------------------
# ZNY CWSU SWAP outlooks, days 1-3
# ---------------------------------------------------------------------------
# Fixed URLs overwritten in place by the CWSU, so no scraping — but a
# filename that never changes means a stale graphic looks exactly like
# a current one. The HTTP Last-Modified header is the only tell, so
# it is read on every refresh and the age is shown above each panel.
#
# Drawn at a third of the page width on purpose: the source GIFs are
# larger than that, so they DOWNSCALE and read crisp, where a
# full-width stretch of the same file would blur.
SWAP_PANELS = (
    ("Day 1 SWAP forecast",
     "https://www.weather.gov/images/zny/SWAP_1.gif",
     "https://www.weather.gov/zny/SWAP_1"),
    ("Day 2 outlook",
     "https://www.weather.gov/images/zny/OUTLOOK_DAY2.gif",
     "https://www.weather.gov/zny/OUTLOOK_DAY2"),
    ("Day 3 outlook",
     "https://www.weather.gov/images/zny/OUTLOOK_DAY3.gif",
     "https://www.weather.gov/zny/OUTLOOK_DAY3"),
)
SWAP_STALE_H = 30.0     # issued daily; past this it is yesterday's


@st.cache_data(ttl=600, show_spinner=False)
def _img_age(url: str, _bucket: str):
    """(age_hours, issued_text, err). HEAD only — the browser draws
    the image straight from the NWS URL."""
    from email.utils import parsedate_to_datetime
    import requests

    try:
        r = requests.head(url, timeout=6, allow_redirects=True,
                          headers={"User-Agent": "n90-airspace/1.0"})
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}"
        lm = r.headers.get("Last-Modified")
        if not lm:
            return None, None, None
        when = parsedate_to_datetime(lm)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - when).total_seconds() / 3600
        return age, when.strftime("%d %b %H%MZ"), None
    except Exception as exc:
        return None, None, type(exc).__name__


st.markdown("**ZNY CWSU SWAP outlook** &mdash; NWS New York Center "
            "Weather Service Unit")
_sw_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
_sw_cols = st.columns(3)
for _col, (_title, _img, _page) in zip(_sw_cols, SWAP_PANELS):
    with _col:
        _age, _when, _err = _img_age(_img, _sw_bucket)
        if _err:
            st.caption(f"**{_title}** \u2014 unavailable ({_err})")
        elif _age is None:
            st.caption(f"**{_title}** \u2014 issued time not reported")
        elif _age > SWAP_STALE_H:
            st.error(f"{_title}: STALE, issued {_when} "
                     f"({_age / 24:.1f} days ago)")
        else:
            st.caption(f"**{_title}** \u2014 issued {_when} "
                       f"({_age:.1f} h ago)")
        st.image(_img, width="stretch")
        st.caption(f"[Open on weather.gov]({_page})")

st.caption(
    "The SWAP forecast is the CWSU's assessment of the PROBABILITY the "
    "FAA implements a Severe Weather Avoidance Plan \u2014 a planning "
    "product, not an observation. Advisory only."
)


# ---------------------------------------------------------------------------
# Compression outlook
# ---------------------------------------------------------------------------
# Port of the SOC's Google Sheets macro: surface wind and FL020/040/
# 080 AGL winds from the PSU BUFKIT sounding, per station, per
# forecast hour, with the compression thresholds highlighted. The
# BUFKIT fetch is cached an hour; the models it reads are 6-hourly.
from core import compression as CX

COMP_STATIONS = [s.strip().upper() for s in os.environ.get(
    "COMP_STATIONS", "JFK,LGA,EWR").split(",") if s.strip()]


@st.cache_data(ttl=3600, show_spinner=False)
def _comp_table(model: str, stations: tuple, _bucket: str):
    return CX.build(list(stations), model, hours=24, step_h=3)


st.markdown("**Compression outlook** &mdash; BUFKIT soundings via "
            "Penn State")
# GFS only. The NAM is being retired and its BUFKIT files have been
# lagging; one model is one fewer thing to explain on the page.
_comp_model = "GFS"
_ct = _comp_table(_comp_model, tuple(COMP_STATIONS),
                  datetime.now(timezone.utc).strftime("%Y%m%d%H"))
for _e in _ct.get("errors", []):
    st.warning(f"BUFKIT: {_e}")
if _ct["rows"]:
    st.markdown(CX.to_html(
        _ct, title="N90 compression outlook",
        issued=datetime.now(timezone.utc).strftime("%d %b %H%MZ")),
        unsafe_allow_html=True)
    st.caption(
        f"Stations from COMP_STATIONS ({', '.join(COMP_STATIONS)}). "
        f"Surface from the model's 2 m wind; FL020/040/080 are 2,000/"
        f"4,000/8,000 ft AGL interpolated between sounding levels. "
        f"Direction rounded to 10&deg;, aloft speed to 5 kt, surface "
        f"to 1 kt \u2014 the macro's rounding, kept as-is.")
else:
    st.info("No BUFKIT data returned for any station.")
