"""Level III Radar (EXPERIMENTAL prototype) - KLWX over DC.

Pinned to one site while the prototype is proved out: L3_PAGE_DOMAIN
picks another entry of core.radar_l3.DOMAINS.

Super-res base reflectivity (N0B, 250 m) built by core/radar_l3.py,
one image per scan, and a loop of the last ~hour.

LOOP. Animated in the BROWSER: every frame's image is loaded once and
a few lines of JavaScript swap which one is visible. Stepping frames
through Streamlit reruns would resend the whole map every step and
flicker; this plays smoothly and costs nothing on the server. The map
is MapLibre on CARTO's dark vector style (the pydeck basemap). An
image source is stretched between its corners in Web Mercator, which
is exactly how radar_l3 lays out its rows, so frames sit correctly
across the whole box.

COMPARE. The pydeck view with MRMS under or instead of Level III.

The page builds the newest frame itself if none is fresh (~1 s), and
starts a background backfill for the rest of the loop; frames appear
as they land (the fragment re-runs every 60 s).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pydeck as pdk
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Level III Radar", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

from core import mrms as MR
from core import radar_l3 as L3

STATIC = Path(__file__).resolve().parent.parent / "static"
STALE_S = int(os.environ.get("L3_PAGE_STALE_S", "600"))
domain = os.environ.get("L3_PAGE_DOMAIN", "DCA")
if domain not in L3.DOMAINS:
    domain = "DCA"

st.title(f"Level III Radar (prototype) - {L3.DOMAIN_LABEL[domain]}")
st.caption(
    "Base reflectivity only: 0.5 deg super-resolution (N0B), 250 m "
    "gates, 0.5 deg azimuth, about a second per scan to build. One-hour "
    "loop. Echo MRMS does not confirm (clutter, birds, clear-air "
    "return) is removed.")


def _origin() -> str:
    """Same-origin base URL for /app/static images."""
    try:
        host = st.context.headers.get("Host", "")
        if host:
            proto = st.context.headers.get("X-Forwarded-Proto", "https")
            return f"{proto}://{host}"
    except Exception:
        pass
    return (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")


def _age_s(stamp) -> float:
    try:
        t = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return 1e9


def _ensure(domain: str):
    """Newest frame now (building it on this run if needed), and the
    rest of the loop in the background. Returns an error string or
    None."""
    man, stamp = L3.newest(STATIC, domain)
    err = None
    if man is None or _age_s(stamp) >= STALE_S:
        with st.spinner(f"Building {L3.DOMAIN_LABEL[domain]} image..."):
            try:
                s_, note = L3.build(domain, STATIC)
                if s_ is None:
                    err = note
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
    # Fill the hour in the background if any scan in it is missing.
    have = {f["stamp"] for f in L3.frames(STATIC, domain)}
    try:
        want = {L3.key_stamp(k) for k in L3.loop_keys(L3.DOMAINS[domain][0])}
    except Exception:
        want = set()
    if want - have:
        L3.backfill_bg(domain, STATIC)
    return err


c2, c3 = st.columns([1.2, 3])
with c2:
    view = st.radio("View", ["Loop", "Compare with MRMS"], index=0,
                    key="l3_view")
with c3:
    speed = st.select_slider("Loop speed", ["Slow", "Normal", "Fast"],
                             value="Normal", key="l3_speed")
    mrms_mode = None
    if view == "Compare with MRMS":
        mrms_mode = st.radio("Show", ["Level III", "MRMS", "Both"],
                             index=2, horizontal=True, key="l3_cmp")


@st.fragment(run_every=60)
def _body():
    base = _origin()
    err = _ensure(domain)
    frames = L3.frames(STATIC, domain)
    want_more = domain in L3._bg["busy"]
    if err:
        st.error(f"Level III build failed: {err}")
    if not base:
        st.warning("Could not work out this site's address, so the "
                   "radar images cannot be loaded.")
    if frames:
        last = frames[-1]
        filt = {"mrms": "MRMS-masked", "cc": "CC-filtered (no MRMS "
                "mask for this scan)", "none": "unfiltered"}.get(
            last.get("filter"), last.get("filter", "?"))
        st.caption(
            f"{last['site']} N0B  |  {len(frames)} frames, "
            f"{frames[0]['stamp'][9:11]}:{frames[0]['stamp'][11:13]}Z to "
            f"{last['stamp'][9:11]}:{last['stamp'][11:13]}Z  |  latest "
            f"{_age_s(last['stamp']) / 60:.0f} min old, {filt}"
            + ("  |  filling the rest of the hour..." if want_more else ""))

    if view == "Loop":
        ms = {"Slow": 900, "Normal": 500, "Fast": 250}[speed]
        name = f"l3loop_{domain}.html"
        tmp = STATIC / f".{name}.tmp"
        _site, clat, clon, *_ = L3.DOMAINS[domain]
        tmp.write_text(L3.loop_html(frames, base, clat, clon, zoom=7.3))
        os.replace(tmp, STATIC / name)
        v = frames[-1]["stamp"] if frames else "none"
        components.iframe(f"{base}/app/static/{name}?ms={ms}&v={v}",
                          height=720)
        return

    # Compare view: pydeck, newest frame, MRMS under / instead.
    layers = []
    if mrms_mode in ("MRMS", "Both"):
        chunks, _rs = MR.newest(STATIC, "REFL")
        if chunks and base:
            layers += [pdk.Layer("BitmapLayer", data=None,
                                 image=f"{base}/app/static/{c['name']}",
                                 bounds=c["bounds"], opacity=1.0)
                       for c in chunks]
    if mrms_mode in ("Level III", "Both") and frames and base:
        layers.append(pdk.Layer(
            "BitmapLayer", data=None,
            image=f"{base}/app/static/{frames[-1]['name']}",
            bounds=frames[-1]["bounds"], opacity=1.0))
    _site, clat, clon, *_ = L3.DOMAINS[domain]
    st.pydeck_chart(pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=clat, longitude=clon,
                                         zoom=7.2),
        map_style=os.environ.get(
            "BLUEMET_MAP_STYLE",
            "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/"
            "style.json"),
    ), height=720)


_body()

with st.expander("Level III warmer log", expanded=False):
    try:
        lines = (STATIC / "l3_warmer.log").read_text().splitlines()[-15:]
        st.code("\n".join(lines) or "(empty)")
    except OSError:
        st.caption("No log yet.")
