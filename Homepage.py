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

from auth import check_password

check_password()


# ---------------------------------------------------------------------------
# Background warmers
# ---------------------------------------------------------------------------
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

try:
    from core.radar_l2 import ensure_radar_warmer

    # Radar frames live under static/ so they can be served as
    # absolute URLs to the map, not in the cache dir.
    _static = Path(__file__).resolve().parent / "static"
    ensure_radar_warmer(_static)
    _warm_notes.append("radar warmer started")
except Exception as _exc:
    _warm_notes.append(f"radar warmer FAILED: "
                       f"{type(_exc).__name__}: {_exc}")


def _home():
    st.title("BlueMet")
    st.markdown(
        "<p style='color: #B30000; font-size: 32px; "
        "font-weight: bold;'>"
        "IMPORTANT: Use Prohbited outside of the JetBlue SOC or "
        "for Tomorrow.io employees"
        "</p>",
        unsafe_allow_html=True,
    )
    st.caption("Multi-model comparison for CONUS airports.")
    with st.expander("Background warmers", expanded=False):
        for _n in _warm_notes:
            (st.error if "FAILED" in _n else st.caption)(_n)
        st.caption(
            f"Store: {CACHE_ROOT}"
            + ("" if _persistent.exists() else
               "  \u2014 WARNING: the persistent disk is NOT mounted "
               "at /opt/render/project/src/cache, so this is /tmp and "
               "is wiped on every restart.")
        )
    st.markdown(
        """
        ### Sections

        - **Forecast Tools** — Hi-res CAMs, wind plots, flight
          conditions, and MOS guidance
        - **Situational Awareness Products** — the JBU Weather
          Map, station quick view, and fleet tracker
        - **Archive Flight Conditions** — historical satellite
          and radar with flight overlay
        """
    )


PAGES = {
    "": [
        st.Page(_home, title="Home", default=True),
    ],
    "Forecast Tools": [
        st.Page("pages/9_HiRes_CAMs.py",
                title="Hi-Res CAMs"),
        st.Page("pages/11_REFS_Ensemble.py",
                title="REFS Ensemble"),
        st.Page("pages/8_Forecast_Wind_Plots.py",
                title="Forecast Wind Plots"),
        st.Page("pages/1_Forecast_Flight_Conditions.py",
                title="Forecast Flight Conditions"),
        st.Page("pages/4_MOS_Tables.py",
                title="MOS Tables"),
    ],
    "Situational Awareness Products": [
        st.Page("pages/3_JBU_Weather_Map.py",
                title="JBU Weather Map CONUS"),
        st.Page("pages/10_JBU_Weather_Map_Only.py",
                title="JBU Weather Map International"),
        st.Page("pages/7_Station_Quick_View.py",
                title="Station Quick View"),
        st.Page("pages/8_JBU_Flight_Tracker.py",
                title="JBU Flight Tracker"),
    ],
    "Airspace": [
        st.Page("pages/13_N90_Airspace.py",
                title="N90 Airspace"),
    ],
    "Experimental": [
        st.Page("pages/12_L2_Radar_Lab.py",
                title="L2 Radar Lab"),
    ],
    "Archive Flight Conditions": [
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
        color: #000000 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

nav = st.navigation(PAGES)
nav.run()
