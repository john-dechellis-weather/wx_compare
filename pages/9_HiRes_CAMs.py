"""Hi-Res CAMs - convection-allowing model viewer, 2x2 model grid.

Top-right panel: HRRR (hourly-updating, aviation products only, live
JBU aircraft overlaid). Remaining quadrants are placeholders for the
next models (NAM Nest, HRW, RRFS).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="BlueMet - Hi-Res CAMs",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False, max_entries=30)
def cached_station_coords(icao: str):
    from core.stations import StationResolver
    resolver = StationResolver(cache_dir=CACHE_ROOT / "stations")
    resolved, _ = resolver.resolve_many([icao])
    if not resolved:
        return None
    stn = resolved[0]
    return float(stn.lat), float(stn.lon)


@st.cache_data(ttl=300, show_spinner=False, max_entries=20)
def cached_model_cycle(model: str, fhr: int, bucket: str):
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(model, fhr)
    return cyc.isoformat() if cyc else None


@st.cache_data(ttl=120, show_spinner=False, max_entries=20)
def cached_jbu(lat: float, lon: float, radius: float, bucket: str):
    from core.flights import fetch_positions_near
    try:
        return fetch_positions_near(lat, lon, radius_deg=radius)
    except Exception:
        return []


@st.cache_data(ttl=600, show_spinner=False, max_entries=10)
def cached_routes(aircraft, bucket: str):
    from core.flights import fetch_routes
    try:
        return fetch_routes(list(aircraft))
    except Exception:
        return {}


@st.cache_data(ttl=1800, show_spinner=False, max_entries=96)
def cached_panel(
    model: str, product: str, cycle_iso: str, fhr: int,
    clat: float, clon: float, zoom: float, aircraft, routes=None,
) -> bytes:
    """One rendered panel for one model. Keyed on model+cycle+fhr+
    product+region, so new model cycles refresh naturally."""
    from core.hrrr_cam import (
        fetch_field, decode_field, render_field, MODELS,
    )
    cycle = datetime.fromisoformat(cycle_iso)
    raw = fetch_field(model, product, cycle, fhr, clat, clon, zoom)
    vals, lats, lons = decode_field(raw)
    valid = cycle + __import__("datetime").timedelta(hours=fhr)
    title = (
        f"{MODELS[model]['label']} {cycle:%m/%d %H}Z  f{fhr:02d}  "
        f"valid {valid:%m/%d %H}Z"
    )
    return render_field(
        product, vals, lats, lons, clat, clon, zoom, title,
        aircraft=aircraft, routes=routes,
    )


def _frames_to_gif(frames, width=800, frame_ms=500, last_hold_ms=1500):
    from io import BytesIO
    from PIL import Image

    imgs = []
    for png, _name in frames:
        im = Image.open(BytesIO(png)).convert("RGB")
        w, h = im.size
        if w > width:
            im = im.resize((width, int(h * width / w)), Image.LANCZOS)
        imgs.append(im.quantize(colors=256))
    durations = [frame_ms] * (len(imgs) - 1) + [last_hold_ms]
    buf = BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:],
                 duration=durations, loop=0, disposal=2)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Hi-Res CAMs")
st.caption(
    "Convection-allowing model viewer - aviation products, "
    "hourly-updating, live JBU aircraft overlaid."
)

with st.sidebar:
    st.header("Region")
    icao_input = st.text_input("Airport ICAO", value="KJFK",
                               max_chars=4).strip().upper()
    zoom = st.slider("Zoom (degrees)", 1.0, 6.0, 2.5, 0.5)

    st.header("Models")
    show_models = {
        "hrrr": st.checkbox("HRRR", value=True),
        "nam_nest": st.checkbox("NAM 3km Nest", value=True),
        "hiresw_arw": st.checkbox("HRW ARW", value=True),
        "rrfs": st.checkbox("RRFS", value=True),
    }

    st.header("Product")
    product_label = st.selectbox(
        "Product",
        options=[
            "Composite Reflectivity",
            "Echo Tops",
            "Visibility",
            "Ceiling",
            "10 m Wind Gust",
        ],
        index=0,
    )
    PRODUCT_KEY = {
        "Composite Reflectivity": "REFC",
        "Echo Tops": "RETOP",
        "Visibility": "VIS",
        "Ceiling": "CEIL",
        "10 m Wind Gust": "GUST",
    }
    fhr_range = st.slider(
        "Forecast hours (loop range)", 0, 18, (1, 8),
        help="Renders every hour in the range as an animated loop. "
             "Collapse the range to a single hour for one frame. "
             "Frames cache per hour, so re-loops are fast.",
    )
    show_jbu = st.checkbox("Overlay live JBU aircraft", value=True)

    st.divider()
    run_button = st.button("Render", type="primary",
                           use_container_width=True)

if run_button and icao_input:
    st.session_state["cam_icao"] = icao_input

active = st.session_state.get("cam_icao")

if active:
    icao = active
    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve coordinates for {icao}.")
        st.stop()
    clat, clon = coords

    product = PRODUCT_KEY[product_label]
    fhr_start, fhr_end = fhr_range
    hours = list(range(fhr_start, fhr_end + 1))

    aircraft = []
    routes = {}
    if show_jbu:
        aircraft = cached_jbu(round(clat, 2), round(clon, 2), zoom,
                              now.strftime("%Y%m%d%H%M"))
        if aircraft:
            routes = cached_routes(
                tuple(aircraft), now.strftime("%Y%m%d%H%M")
            )

    st.info(
        f"**{icao}** | f{fhr_start:02d}-f{fhr_end:02d} | {product_label}"
        + (f" | {len(aircraft)} JBU live | {len(routes)} routes"
           if show_jbu else "")
    )
    if show_jbu and aircraft and not routes:
        from core.flights import last_route_error
        err = last_route_error()
        if err:
            st.caption(f"Route lookup: {err}")

    from core.hrrr_cam import MODELS

    def render_model_panel(model: str):
        cfg = MODELS[model]
        st.markdown(f"**{cfg['label']}**")
        if not show_models.get(model):
            st.caption("(unchecked in sidebar)")
            return
        if product not in cfg["products"]:
            st.caption(
                f"{PRODUCT_LABELS_SHORT.get(product, product)} is not "
                f"available in {cfg['label']}."
            )
            return
        cycle_iso = cached_model_cycle(model, fhr_end, bucket10)
        if cycle_iso is None:
            msg = f"No complete {cfg['label']} cycle found."
            if cfg["note"]:
                msg += f" ({cfg['note']})"
            st.caption(msg)
            return
        frames = []
        errors = []
        prog = st.progress(0.0, text="Rendering...")
        for i, h in enumerate(hours):
            prog.progress(
                (i + 1) / len(hours),
                text=f"{cfg['label']} f{h:02d} ({i + 1}/{len(hours)})",
            )
            try:
                png = cached_panel(
                    model, product, cycle_iso, h,
                    round(clat, 2), round(clon, 2), zoom,
                    aircraft, routes=routes,
                )
                frames.append((png, f"f{h:02d}"))
            except Exception as e:
                errors.append(f"f{h:02d}: {e}")
        prog.empty()

        if not frames:
            st.error(
                f"{cfg['label']}: all frames failed. First error: "
                + (errors[0] if errors else "unknown")
            )
        elif len(frames) == 1:
            st.image(frames[0][0], use_container_width=True)
            st.caption(f"`{frames[0][1]}`")
        else:
            gif = _frames_to_gif(frames)
            st.image(gif, use_container_width=True)
            st.download_button(
                "Loop GIF", data=gif,
                file_name=f"{model}_{product}_{icao}.gif",
                mime="image/gif", key=f"dl_{model}",
            )
            with st.expander("Frame-by-frame"):
                idx = st.slider(
                    "Frame", 0, len(frames) - 1, len(frames) - 1,
                    key=f"idx_{model}",
                )
                st.image(frames[idx][0], use_container_width=True)
                st.caption(f"`{frames[idx][1]}`")
        if errors and frames:
            st.caption(f"{len(errors)} frame(s) failed: {errors[0]}")
        if cfg["note"]:
            st.caption(cfg["note"])

    PRODUCT_LABELS_SHORT = {
        "REFC": "Composite reflectivity", "RETOP": "Echo tops",
        "VIS": "Visibility", "CEIL": "Ceiling", "GUST": "Gusts",
    }

    # 2x2 model grid - HRRR keeps the top-right quadrant
    top_left, top_right = st.columns(2)
    bot_left, bot_right = st.columns(2)
    with top_left:
        render_model_panel("nam_nest")
    with top_right:
        render_model_panel("hrrr")
    with bot_left:
        render_model_panel("hiresw_arw")
    with bot_right:
        render_model_panel("rrfs")

else:
    st.info("Enter an airport in the sidebar and click **Render**.")
    st.markdown(
        """
        ### What this page is

        A 2\u00d72 grid of convection-allowing models centered on your
        airport, aviation products only. **HRRR** (top right) is live:
        composite reflectivity, echo tops, visibility, ceiling, and
        gusts, forecast hours f00\u2013f18, updating every hour, with
        live JetBlue aircraft overlaid. The other quadrants fill in as
        models are added.
        """
    )
