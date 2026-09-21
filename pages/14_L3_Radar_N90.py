"""Level III Radar - N90 (EXPERIMENTAL prototype).

KOKX super-res base reflectivity (N0B, 250 m) over the N90 box, built
by the L3 warmer in core/radar_l3.py and drawn as one BitmapLayer.
MRMS can be drawn under it for comparison. The page never fetches or
decodes radar itself; it reads the newest manifest the warmer wrote.
"""

import os
from pathlib import Path

import pydeck as pdk
import streamlit as st

st.set_page_config(page_title="Level III Radar - N90", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

from core import mrms as MR
from core import radar_l3 as L3

STATIC = Path(__file__).resolve().parent.parent / "static"

st.title("Level III Radar - N90 (prototype)")
st.caption(
    "KOKX 0.5 deg super-resolution reflectivity (N0B): 250 m gates, "
    "0.5 deg azimuth, the same detail as Level II at that tilt. Built "
    "in about a second per scan by the L3 warmer, against 40-100 s for "
    "the Level II mosaic. Echo MRMS does not confirm (clutter, birds, "
    "clear-air return) is removed.")


def _origin() -> str:
    """Same-origin base URL, as the CONUS map does it: a cross-origin
    image is refused as a WebGL texture and draws nothing."""
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


# How old a Level III image may be before the page builds a fresh one
# itself instead of waiting for the warmer. KOKX scans every 4-6 min.
STALE_S = int(os.environ.get("L3_PAGE_STALE_S", "600"))


def _age_s(stamp):
    from datetime import datetime, timezone
    try:
        t = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return 1e9


def _ensure_image():
    """Newest manifest, building one ON THIS RUN when there is none or
    it is stale. A build is ~1 s (a few more the very first time in a
    process), so nobody waits minutes for the warmer to come round -
    that wait, with a page that never refreshed, was the 3+ minutes."""
    man, stamp = L3.newest(STATIC, "N90")
    if man is not None and _age_s(stamp) < STALE_S:
        return man, stamp, None
    err = None
    with st.spinner("Building KOKX Level III image..."):
        try:
            _st, note = L3.build("N90", STATIC)
            if _st is None:
                err = note
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
    man2, stamp2 = L3.newest(STATIC, "N90")
    return (man2 or man), (stamp2 or stamp), err


c1, c2 = st.columns([1, 3])
with c1:
    show = st.radio("Radar", ["Level III (KOKX)", "MRMS", "Both"],
                    index=0, key="l3_show")


# Re-runs itself every 60 s so a new scan appears without a reload.
@st.fragment(run_every=60)
def _map():
    base = _origin()
    layers = []
    note = []

    if show in ("MRMS", "Both"):
        chunks, rs = MR.newest(STATIC, "REFL")
        if chunks and base:
            layers += [pdk.Layer("BitmapLayer", data=None,
                                 image=f"{base}/app/static/{c['name']}",
                                 bounds=c["bounds"], opacity=1.0)
                       for c in chunks]
            note.append(f"MRMS {rs[9:11]}:{rs[11:13]}Z")
        else:
            note.append("MRMS: no scan yet")

    if show in ("Level III (KOKX)", "Both"):
        man, stamp, err = _ensure_image()
        if man and base:
            layers.append(pdk.Layer(
                "BitmapLayer", data=None,
                image=f"{base}/app/static/{man['name']}",
                bounds=man["bounds"], opacity=1.0))
            note.append(f"KOKX N0B {stamp[9:11]}:{stamp[11:13]}Z, "
                        f"{_age_s(stamp) / 60:.0f} min old, filter: "
                        f"{man.get('filter', '?')}")
        if err:
            st.error(f"Level III build failed: {err}")
        elif not man:
            st.warning("Level III: no image yet.")
        if not base:
            st.warning("Could not work out this site's address, so the "
                       "radar image cannot be loaded.")

    # KOKX itself, for orientation.
    layers.append(pdk.Layer(
        "ScatterplotLayer", [{"position": [-72.864, 40.865]}],
        get_position="position", get_radius=1500, radius_min_pixels=3,
        radius_max_pixels=5, get_fill_color=[255, 255, 255, 255]))

    st.caption(" | ".join(note))
    st.pydeck_chart(pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=40.6398,
                                         longitude=-73.7789, zoom=7.2),
        map_style=os.environ.get(
            "BLUEMET_MAP_STYLE",
            "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/"
            "style.json"),
    ), height=720)


_map()

with st.expander("L3 warmer log", expanded=False):
    try:
        lines = (STATIC / "l3_warmer.log").read_text().splitlines()[-15:]
        st.code("\n".join(lines) or "(empty)")
    except OSError:
        st.caption("No log yet.")
