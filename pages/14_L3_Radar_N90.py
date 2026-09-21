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


c1, c2 = st.columns([1, 3])
with c1:
    show = st.radio("Radar", ["Level III (KOKX)", "MRMS", "Both"],
                    index=0, key="l3_show")

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

man, stamp = L3.newest(STATIC, "N90")
if show in ("Level III (KOKX)", "Both"):
    if man and base:
        layers.append(pdk.Layer(
            "BitmapLayer", data=None,
            image=f"{base}/app/static/{man['name']}",
            bounds=man["bounds"], opacity=1.0))
        note.append(f"KOKX N0B {stamp[9:11]}:{stamp[11:13]}Z, "
                    f"filter: {man.get('filter', '?')}")
    else:
        note.append("Level III: warming - first scan within ~2 min of "
                    "a restart")

# KOKX itself, for orientation.
layers.append(pdk.Layer(
    "ScatterplotLayer", [{"position": [-72.864, 40.865]}],
    get_position="position", get_radius=1500, radius_min_pixels=3,
    radius_max_pixels=5, get_fill_color=[255, 255, 255, 255]))

with c2:
    st.caption(" | ".join(note))

st.pydeck_chart(pdk.Deck(
    layers=layers,
    initial_view_state=pdk.ViewState(latitude=40.6398,
                                     longitude=-73.7789, zoom=7.2),
    map_style=os.environ.get(
        "BLUEMET_MAP_STYLE",
        "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"),
), height=720)

with st.expander("L3 warmer log", expanded=False):
    try:
        lines = (STATIC / "l3_warmer.log").read_text().splitlines()[-15:]
        st.code("\n".join(lines) or "(empty)")
    except OSError:
        st.caption("No log yet.")
