"""BlueMet entry point and navigation router.

Declares the sidebar's grouped navigation via st.navigation; the
tool pages live in pages/ and are referenced here by path, with
sidebar titles set explicitly (filename prefixes no longer control
order or labels).
"""

import time
from pathlib import Path

import streamlit as st


def _cleanup_old_cache():
    """Delete cache files older than the cutoff to prevent disk
    fill on the persistent volume."""
    cache_root = Path("/opt/render/project/src/cache")
    if not cache_root.exists():
        return
    cutoff = time.time() - (24 * 3600)
    for p in cache_root.rglob("*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            continue


if "_cache_cleanup_done" not in st.session_state:
    _cleanup_old_cache()
    st.session_state["_cache_cleanup_done"] = True

st.set_page_config(
    page_title="BlueMet",
    layout="wide",
    initial_sidebar_state="expanded",
)

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

try:
    from core.cam_warm import note_request as _note_req

    _note_req()
except Exception:
    pass

from auth import check_password


# ---------------------------------------------------------------------------
# Background warmers
# ---------------------------------------------------------------------------
# PRIORITY ORDER, stated so it is not re-litigated by whoever adds
# the next warmer:
#
#   1. JBU Weather Map CONUS  — the page that matters. Must open in
#      seconds, always. Nothing may be added that competes with it.
#   2. Hi-Res CAMs
#   3. REFS Ensemble
#
# The constraint that makes this awkward: a warmer thread rendering
# matplotlib holds the GIL, which blocks the request thread serving
# page 3. There is no scheduling trick around that on a small box —
# the only real lever is running FEWER and LIGHTER warmers. That is
# why the two newest ones default to off rather than merely being
# delayed, and why the memory ceiling skips a pass instead of
# queueing it.
#
# If page 3 is ever slow, the order to disable things is the reverse
# of the priority list: L2_WARMER=off, then OVL_WARMER=off, then
# CAM_WARMER=off. Reach for that before optimising anything.
# Started HERE, not on the pages that consume them.
#
# Both used to be started by their own page — the CAM warmer from
# pages 9 and 11, the radar warmer from page 13. That meant they only
# ran while someone was looking at the page they fed, which is exactly
# backwards: a warmer exists so the data is ready BEFORE anyone
# arrives. A container restart killed the thread and nothing revived
# it until the next visit to that specific page. Across a day of
# deploys the CAM warmer never got an uninterrupted run at a job that
# needs 2-3 hours, and the store stayed empty while appearing to be
# configured correctly.
#
# Homepage is on every path into the app, so starting them here means
# a restart costs one page load rather than a page load OF THE RIGHT
# PAGE. Both calls are idempotent and both have env kill switches
# (CAM_WARMER, L2_WARMER), so this is safe to run on every rerun.
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = (_persistent if _persistent.exists()
              else Path("/tmp/wx_compare_cache"))
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

_warm_notes = []
try:
    from core.cam_warm import ensure_warmer_started

    ensure_warmer_started(CACHE_ROOT)
    _warm_notes.append("CAM warmer started")
except Exception as _exc:
    _warm_notes.append(f"CAM warmer FAILED: {type(_exc).__name__}: {_exc}")

# MRMS national mosaic for the CONUS map. Cheap: one 150 KB frame
# every ~150 s, rendered with numpy rather than matplotlib, and it
# skips a pass if memory is tight. Started here because the CONUS
# map must never wait on it.
try:
    from core.mrms import ensure_mrms_warmer

    _static_mrms = Path(__file__).resolve().parent / "static"
    ensure_mrms_warmer(_static_mrms)
    _warm_notes.append("MRMS warmer started (1 km national mosaic)")
except Exception as _exc:
    _warm_notes.append(f"MRMS warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# JetBlue fleet sweep (core/fleet.py). Every FLEET_SWEEP_S (120 s)
# whether or not anyone is looking, so the login map and the CONUS
# map open on current positions, trails have no gaps, and holds are
# detected with nobody watching. Same request rate as one viewer
# leaving the CONUS map open. JBU_FLEET_WARMER=off stops it.
try:
    from core.fleet import ensure_fleet_warmer

    if ensure_fleet_warmer():
        _warm_notes.append("Fleet warmer started (ADS-B sweep every "
                           "2 min)")
    else:
        _warm_notes.append("Fleet warmer off (JBU_FLEET_WARMER=off)")
except Exception as _exc:
    _warm_notes.append(f"Fleet warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# Level III super-res radar (core/radar_l3.py): KOKX over N90 and
# KLWX over DC, about a second per scan, polled every 60 s, keeping
# an hour's loop. Needs the MRMS
# warmer's echo mask to strip clutter; falls back to a CC filter
# without it. L3_WARMER=off stops it.
try:
    from core.radar_l3 import ensure_l3_warmer

    if ensure_l3_warmer(Path(__file__).resolve().parent / "static"):
        _warm_notes.append("Level III warmer started (KOKX, KLWX)")
    else:
        _warm_notes.append("Level III warmer off (L3_WARMER=off)")
except Exception as _exc:
    _warm_notes.append(f"Level III warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# Airport diagrams for every JetBlue station (the airport scope on
# Station Forecast). One Overpass query per station, paced, refreshed weekly; a
# page never waits on Overpass. Lowest priority of the warmers: it
# starts last and is I/O-bound, so it does not hold the GIL against
# page 3. SURFACE_WARMER=off stops it.
_SCOPE_DIR = CACHE_ROOT / "scope"
try:
    from core.surface_warm import ensure_surface_warmer

    if ensure_surface_warmer(_SCOPE_DIR):
        _warm_notes.append("Surface warmer started (airport diagrams)")
    else:
        _warm_notes.append("Surface warmer off (SURFACE_WARMER=off)")
except Exception as _exc:
    _warm_notes.append(f"Surface warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# Model comparison for every Station Forecast hub, built once per
# cycle (core/compare_warm.py). Turns a 4-15 s compute on page load
# into a file read. COMPARE_WARMER=off stops it.
try:
    from core.compare_warm import ensure_compare_warmer

    if ensure_compare_warmer(CACHE_ROOT):
        _warm_notes.append("Compare warmer started (model frames per hub)")
    else:
        _warm_notes.append("Compare warmer off (COMPARE_WARMER=off)")
except Exception as _exc:
    _warm_notes.append(f"Compare warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# Echo-top tags for the CONUS map: every 18 dBZ top at or above FL320
# with 30 nm spacing, from the same MRMS file the mosaic uses, written
# to static/etop_tags.json. Cheap (150 KB, ~0.4 s) and I/O-bound.
try:
    from core.etop_tags import ensure_etop_tags_warmer

    if ensure_etop_tags_warmer(_static_mrms):
        _warm_notes.append("Echo-top tag warmer started (FL320+, 30 nm)")
    else:
        _warm_notes.append("Echo-top tag warmer off (ETOP_TAGS=off)")
except Exception as _exc:
    _warm_notes.append(f"Echo-top tag warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# tomorrow.io tiles for the Large Scale Map (core/tio_map.py): the
# fixed tile set for 125W-50W / 52N-5S, stitched into WebPs under
# static/. Keeps the API key server-side and the spend fixed (about
# 6,900 requests/day of the 10,000 plan; TIO_DAILY_CAP stops it at
# 9,000). I/O-bound. TIO_WARMER=off stops it.
try:
    from core.tio_map import ensure_tio_warmer

    if ensure_tio_warmer(_static_mrms):
        _warm_notes.append("tomorrow.io warmer started (Large Scale Map)")
    else:
        _warm_notes.append("tomorrow.io warmer off (TIO_WARMER=off)")
except Exception as _exc:
    _warm_notes.append(f"tomorrow.io warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# NOAA model layers for the Weather Mapping page (core/model_tiles.py):
# REFS first, then RRFS / HRRR as MDL_MODELS enables them. Demand-
# driven like the tomorrow.io layers; GRIB fetch + render per frame.
# MDL_WARMER=off stops it.
try:
    from core.model_tiles import ensure_model_warmer

    if ensure_model_warmer(_static_mrms):
        _warm_notes.append("Model-layer warmer started (Weather Mapping)")
    else:
        _warm_notes.append("Model-layer warmer off (MDL_WARMER=off / no MDL_MODELS)")
except Exception as _exc:
    _warm_notes.append(f"Model-layer warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")

# The CAM-overlay and radar warmers were started here for the N90
# Airspace page, which is no longer in the navigation. Both imports
# are gone rather than merely disabled: an import of core.radar_l2
# pulls in pyart, numpy and matplotlib on EVERY page load, which is
# real memory and startup time for a page nobody can reach.
#
# core/cam_overlay.py and core/radar_l2.py are untouched. Restoring
# the N90 page means restoring its nav entry and these two blocks.


def _warmer_status():
    """Warmer health, in a collapsed sidebar expander on every page.
    This used to be the Home page's only content; the Home page is
    gone (login lands on Station Forecast), the diagnostics are not."""
    with st.popover("Warmers", help="Background warmer status"):
        for _n in _warm_notes:
            (st.error if "FAILED" in _n else st.caption)(_n)
        st.caption(
            f"Store: {CACHE_ROOT}"
            + ("" if _persistent.exists() else
               "  \u2014 WARNING: the persistent disk is NOT mounted "
               "at /opt/render/project/src/cache, so this is /tmp and "
               "is wiped on every restart.")
        )
        try:
            from core.surface_warm import tail as _sw_tail, coverage as _sw_cov
            _cov = _sw_cov(_SCOPE_DIR)
            if _cov:
                _have = sum(1 for v in _cov.values() if v.get("runways"))
                st.caption(f"Airport diagrams: {_have}/{len(_cov)} "
                           "stations have a surface")
            for _ln in _sw_tail(_SCOPE_DIR, 6):
                st.caption(_ln)
        except Exception:
            pass
        try:
            from core.etop_tags import log_tail as _et_tail
            for _ln in _et_tail(4):
                st.caption("echo tops: " + _ln)
        except Exception:
            pass
        try:
            from core.compare_warm import log_tail as _cw_tail
            for _ln in _cw_tail(3):
                st.caption("compare: " + _ln)
        except Exception:
            pass
        try:
            from core import fleet as _fl
            _at = _fl.STATE.get("at") or 0
            _res = _fl.STATE.get("res")
            if _res:
                st.caption(f"fleet: {len(_res[0] or [])} JBU aircraft, "
                           f"swept {max(0, time.time() - _at) / 60:.0f} "
                           "min ago")
            elif _fl.STATE.get("err"):
                st.caption("fleet: " + str(_fl.STATE["err"])[:120])
        except Exception:
            pass
        try:
            _l3log = (Path(__file__).resolve().parent / "static"
                      / "l3_warmer.log")
            for _ln in _l3log.read_text().splitlines()[-3:]:
                st.caption("level III: " + _ln)
        except Exception:
            pass
        try:
            from core import tio_map as _tio
            _u = _tio.usage(Path(__file__).resolve().parent / "static")
            st.caption(f"tomorrow.io: {_u['count']}/{_tio.DAILY_CAP} "
                       "requests today")
            for _ln in _tio.log_tail(
                    Path(__file__).resolve().parent / "static", 2):
                st.caption("tomorrow.io: " + _ln)
        except Exception:
            pass


PAGES = {
    "Forecast Tools": [
        st.Page("pages/9_HiRes_CAMs.py",
                title="Hi-Res CAMs"),
        st.Page("pages/11_REFS_Ensemble.py",
                title="REFS Ensemble"),
        # Station Forecast replaced Forecast Wind Plots and Forecast
        # Flight Conditions: both plots, the NBM and LAMP grids, the
        # METAR/TAF, a radar snapshot and the JetBlue movement board
        # for one station on one page.
        # DEFAULT: the login lands here. There is no Home page.
        st.Page("pages/2_Station_Forecast.py",
                title="Station Forecast", default=True),
        st.Page("pages/4_MOS_Tables.py",
                title="MOS Tables"),
        # 1 Oct: tomorrow.io's convective fields for one station, 48 h
        st.Page("pages/16_Convection_Parameters.py",
                title="Convection Parameters"),
    ],
    "Situational Awareness": [
        st.Page("pages/3_JBU_Weather_Map.py",
                title="JBU Weather Map CONUS"),
        st.Page("pages/15_Large_Scale_Map.py",
                title="Weather Mapping"),
        # Station Quick View removed from navigation 21 Sep; the file
        # stays in pages/ unlisted. Its airport scope lives on in
        # Station Forecast, so the surface warmer above still runs.
        # JBU Flight Tracker removed 22 Sep (unused); delete
        # pages/8_JBU_Flight_Tracker.py from the repo.
    ],
    # N90 Airspace removed from navigation. The page file stays in
    # pages/ and still works if reached directly, but it is not
    # listed, so nothing it imports is loaded and none of its
    # warmers or fetchers can start. Streamlit only executes a page
    # when it is selected — the cost of an unlisted page is the disk
    # it sits on.
    #
    # To bring it back: restore this block. Everything else about the
    # page is unchanged.
    "Experimental": [
        st.Page("pages/12_L2_Radar_Lab.py",
                title="L2 Radar Lab"),
        st.Page("pages/14_L3_Radar_N90.py",
                title="Level III Radar"),
    ],
    "Archive": [
        st.Page("pages/5_Archive_Satellite_Position.py",
                title="Archive Satellite"),
        st.Page("pages/6_Archive_Radar_Position.py",
                title="Archive Radar"),
    ],
}

st.markdown(
    """
    <style>
    [data-testid="stNavSectionHeader"] {
        font-weight: bold !important;
        color: #FFFFFF !important;
        font-size: 13px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ORDER MATTERS. st.navigation must run BEFORE the auth gate.
#
# check_password() ends in st.stop() when nobody is logged in, so
# calling it first meant st.navigation never executed — and with no
# explicit navigation, Streamlit falls back to auto-discovering
# pages/, which is a flat alphabetical list with no group headings.
# That is why the sidebar lost "Forecast Tools", "Airspace" and the
# rest on the password screen and got them back after login.
#
# Declaring the nav first renders the grouped sidebar immediately;
# the gate then stops the script before nav.run() executes any page,
# so nothing is reachable without the password. Every page also
# calls check_password itself, so this is belt-and-braces rather
# than the only guard.
# TOP NAVIGATION (23 Sep). The sidebar is gone; the pages are a tab
# bar under the header: the wordmark, then every page as a tab with
# its group name in small grey type before the group, the current
# page underlined in cyan. Streamlit's own routing is kept (the
# navigation is declared with position="hidden" and the tabs are
# st.page_link), so nothing about the pages changes.
nav = st.navigation(PAGES, position="hidden")

check_password()


def _slug(title: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in title.lower())


def _top_nav(current) -> None:
    _cur = _slug(current.title) if current is not None else ""
    css = [
        # the sidebar and its toggle
        '[data-testid="stSidebar"],[data-testid="stSidebarCollapsedControl"],'
        '[data-testid="collapsedControl"]{display:none !important}',
        # the bar
        '.st-key-topnav{background:#0A0A0A;border-bottom:1px solid #333;'
        'margin:-8px 0 14px 0;padding:0 6px}',
        '.st-key-topnav [data-testid="stHorizontalBlock"]{gap:0 !important;'
        'align-items:stretch !important;flex-wrap:wrap !important}',
        # every item takes its natural width: no equal-share columns,
        # no clipping of the tab text
        '.st-key-topnav [data-testid="stHorizontalBlock"] > div,'
        '.st-key-topnav [data-testid="stElementContainer"],'
        '.st-key-topnav [data-testid="stVerticalBlock"],'
        '.st-key-topnav [data-testid="stVerticalBlockBorderWrapper"]{'
        'flex:0 0 auto !important;width:auto !important;min-width:0 !important}',
        # a tab
        'div.st-key-topnav a[data-testid="stPageLink-NavLink"]{padding:11px 9px !important;display:inline-block;'
        'border-bottom:3px solid transparent !important;border-radius:0 !important;'
        'background:transparent !important;white-space:nowrap;'
        'text-decoration:none !important}',
        'div.st-key-topnav a[data-testid="stPageLink-NavLink"], div.st-key-topnav a[data-testid="stPageLink-NavLink"] *{'
        'color:#B8B8B8 !important;-webkit-text-fill-color:#B8B8B8 !important;'
        'font-family:Barlow,"DejaVu Sans",sans-serif !important;'
        'font-size:13px !important;font-weight:600 !important;'
        'text-decoration:none !important}',
        'div.st-key-topnav a[data-testid="stPageLink-NavLink"]:hover, div.st-key-topnav a[data-testid="stPageLink-NavLink"]:hover *{'
        'color:#FFFFFF !important;-webkit-text-fill-color:#FFFFFF !important}',
        # the current page
        f'div.st-key-nav_{_cur} a[data-testid="stPageLink-NavLink"]{{border-bottom:3px solid #00E5FF !important}}',
        f'div.st-key-nav_{_cur} a[data-testid="stPageLink-NavLink"], div.st-key-nav_{_cur} a[data-testid="stPageLink-NavLink"] *{{'
        'color:#FFFFFF !important;-webkit-text-fill-color:#FFFFFF !important}',
        # group labels and the wordmark
        'div.st-key-topnav div.bm-grp{font-family:Barlow,"DejaVu Sans",sans-serif !important;'
        'font-size:10px !important;font-weight:700 !important;color:#6E6E6E !important;'
        '-webkit-text-fill-color:#6E6E6E !important;letter-spacing:1px;'
        'text-transform:uppercase;padding:6px 2px 0 9px;white-space:nowrap}',
        # a group column: label above, its tabs in a row, a thin rule
        # between groups
        '.st-key-topnav [class*="st-key-grp_"]{margin:0 6px 0 0 !important;'
        'padding:0 6px 0 0 !important;border-right:1px solid #222}',
        '.st-key-topnav [class*="st-key-grp_"] > div{gap:0 !important}',
        'div.st-key-topnav div.bm-brand{font-family:Barlow,"DejaVu Sans",sans-serif !important;'
        'font-size:22px !important;font-weight:700 !important;color:#FFFFFF !important;'
        '-webkit-text-fill-color:#FFFFFF !important;letter-spacing:1px;'
        'padding:8px 14px 0 6px;white-space:nowrap}',
        'div.st-key-topnav div.bm-brand span{color:#4DA3FF !important;'
        '-webkit-text-fill-color:#4DA3FF !important;font-size:22px !important}',
        # the page body starts closer to the bar
        '.stApp .block-container{padding-top:2.6rem !important}',
        '.st-key-nav_warmers{margin-left:14px;padding-top:6px}',
        '.st-key-nav_warmers button{background:transparent !important;'
        'border:1px solid #333 !important;font-size:11px !important;'
        'padding:2px 10px !important}',
    ]
    st.markdown("<style>" + "".join(css) + "</style>", unsafe_allow_html=True)
    with st.container(key="topnav"):
        with st.container(horizontal=True, horizontal_alignment="left",
                          vertical_alignment="bottom"):
            st.markdown('<div class="bm-brand">BLUE<span>MET</span></div>',
                        unsafe_allow_html=True)
            # 1 Oct: one column per group - the label on top, its
            # pages in a row beneath it - instead of labels and tabs
            # strung along one line.
            for group, pages in PAGES.items():
                with st.container(key=f"grp_{_slug(group)}"):
                    st.markdown(f'<div class="bm-grp">{group}</div>',
                                unsafe_allow_html=True)
                    with st.container(horizontal=True,
                                      horizontal_alignment="left",
                                      vertical_alignment="bottom"):
                        for pg in pages:
                            with st.container(key=f"nav_{_slug(pg.title)}"):
                                st.page_link(pg, label=pg.title)
            # warmer status, at the end of the bar
            with st.container(key="nav_warmers"):
                _warmer_status()


_top_nav(nav)

nav.run()
