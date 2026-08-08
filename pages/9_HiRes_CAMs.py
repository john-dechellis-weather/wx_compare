"""Hi-Res CAMs - convection-allowing model viewer, 2x2 model grid.

Top-right panel: HRRR (hourly-updating, aviation products only, live
JBU aircraft overlaid). Remaining quadrants are placeholders for the
next models (NAM Nest, HRW, RRFS).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1

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


@st.cache_data(ttl=1800, show_spinner=False, max_entries=48)
def cached_grid_frame(
    model_cycle_fhr: tuple,   # ((model, cycle_iso, fhr), ...)
    product: str,
    clat: float, clon: float, zoom: float,
    aircraft, routes_t=tuple(),
):
    """One forecast frame for MANY models: all fetch+decode run
    concurrently (the slow, parallelizable part), then panels render
    serially. Returns {model: png | error_string}."""
    from core.hrrr_cam import (
        MODELS, parallel_fetch_decode, render_field,
    )
    from datetime import timedelta as _td

    tasks = []
    for model, cycle_iso, fhr in model_cycle_fhr:
        tasks.append({
            "key": model,
            "model": model,
            "product": product,
            "cycle": datetime.fromisoformat(cycle_iso),
            "fhr": fhr,
            "lat": clat, "lon": clon, "zoom_deg": zoom,
        })
    data = parallel_fetch_decode(tasks)

    out = {}
    routes = _routes_from_tuple(routes_t) if routes_t else {}
    for model, cycle_iso, fhr in model_cycle_fhr:
        res = data.get(model)
        if isinstance(res, Exception) or res is None:
            out[model] = f"error: {res}"
            continue
        vals, lats, lons = res
        cycle = datetime.fromisoformat(cycle_iso)
        valid = cycle + _td(hours=fhr)
        title = (
            f"{MODELS[model]['label']} {cycle:%m/%d %H}Z  f{fhr:02d}  "
            f"valid {valid:%m/%d %H}Z"
        )
        try:
            out[model] = render_field(
                product, vals, lats, lons, clat, clon, zoom, title,
                aircraft=aircraft, routes=routes,
            )
        except Exception as e:
            out[model] = f"error: {e}"
    return out


def _routes_from_tuple(routes_t):
    return {
        cs: {"label": lbl, "orig": o, "dest": d}
        for cs, (lbl, o, d) in routes_t
    }


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
    show_jbu = st.checkbox("Overlay live JBU aircraft", value=True)

    smooth = st.checkbox(
        "Smooth scrub mode", value=False,
        help="Preloads every hour in the range below, then scrubbing "
             "is instant (frames swap in the browser, no reloading). "
             "Preload takes a while on first run; hours cache on the "
             "server, so rebuilding later is fast.",
    )
    if smooth:
        fhr_lo, fhr_hi = st.slider(
            "Preload hours", 0, 60, (0, 12),
            help="All hours in this range are fetched upfront. "
             "Span capped at 24 hours to keep the page light.",
        )
    else:
        fhr_all = st.slider(
            "Forecast hour (all models)", 0, 60, 1,
            help="One slider drives every panel. Models that don't "
                 "reach the selected hour clamp to their own maximum "
                 "(HRRR f18, ARW f48, NAM/RRFS f60).",
        )

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
        f"**{icao}** | {product_label}"
        + (f" | {len(aircraft)} JBU live | {len(routes)} routes"
           if show_jbu else "")
    )
    if show_jbu and aircraft and not routes:
        from core.flights import last_route_error
        err = last_route_error()
        if err:
            st.caption(f"Route lookup: {err}")

    from core.hrrr_cam import MODELS

    PRODUCT_LABELS_SHORT = {
        "REFC": "Composite reflectivity", "RETOP": "Echo tops",
        "VIS": "Visibility", "CEIL": "Ceiling", "GUST": "Gusts",
    }

    def _panel_specs():
        """(model, cycle_iso, clamped_fhr) for every renderable model,
        plus per-model skip reasons."""
        specs, notes = [], {}
        for m in GRID_ORDER:
            cfg = MODELS[m]
            if not show_models.get(m):
                notes[m] = "(unchecked in sidebar)"
                continue
            if product not in cfg["products"]:
                notes[m] = (
                    f"{PRODUCT_LABELS_SHORT.get(product, product)} is "
                    f"not available in {cfg['label']}."
                )
                continue
            fh = min(fhr_all, cfg["max_fhr"])
            cyc = cached_model_cycle(m, fh, bucket10)
            if cyc is None:
                msg = f"No complete {cfg['label']} cycle found."
                if cfg["note"]:
                    msg += f" ({cfg['note']})"
                notes[m] = msg
                continue
            specs.append((m, cyc, fh))
        return specs, notes

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
        # Shared slider drives all panels; clamp to this model's reach.
        fhr = min(fhr_all, cfg["max_fhr"])
        if fhr != fhr_all:
            st.caption(f"f{fhr_all:02d} beyond {cfg['label']} range; "
                       f"showing f{fhr:02d} (its max).")
        cycle_iso = cached_model_cycle(model, fhr, bucket10)
        if cycle_iso is None:
            msg = f"No complete {cfg['label']} cycle found for f{fhr:02d}."
            if cfg["note"]:
                msg += f" ({cfg['note']})"
            st.caption(msg)
            return
        try:
            with st.spinner(f"{cfg['label']} f{fhr:02d}..."):
                png = cached_panel(
                    model, product, cycle_iso, fhr,
                    round(clat, 2), round(clon, 2), zoom,
                    aircraft, routes=routes,
                )
            st.image(png, use_container_width=True)
        except Exception as e:
            st.error(f"{cfg['label']} f{fhr:02d} failed: {e}")
        if cfg["note"]:
            st.caption(cfg["note"])

    GRID_ORDER = ["nam_nest", "hrrr", "hiresw_arw", "rrfs"]

    if smooth:
        span = min(fhr_hi - fhr_lo, 24)
        hours = list(range(fhr_lo, fhr_lo + span + 1))
        active_models = [
            m for m in GRID_ORDER
            if show_models.get(m) and product in MODELS[m]["products"]
        ]
        frames = {}      # model -> {fhr: png}
        skipped = []
        # Phase 1: every (model, hour) fetch+decode in PARALLEL
        from core.hrrr_cam import parallel_fetch_decode, render_field
        from datetime import timedelta as _td

        plan = []      # (model, cycle_iso, hour)
        for m in active_models:
            cfg = MODELS[m]
            mh = [h for h in hours if h <= cfg["max_fhr"]]
            if not mh:
                skipped.append(f"{cfg['label']}: range beyond its max")
                continue
            cycle_iso = cached_model_cycle(m, mh[-1], bucket10)
            if cycle_iso is None:
                skipped.append(f"{cfg['label']}: no cycle found")
                continue
            for h in mh:
                plan.append((m, cycle_iso, h))

        prog = st.progress(
            0.0, text=f"Downloading {len(plan)} fields in parallel..."
        )
        tasks = [{
            "key": (m, h),
            "model": m, "product": product,
            "cycle": datetime.fromisoformat(cyc), "fhr": h,
            "lat": round(clat, 2), "lon": round(clon, 2),
            "zoom_deg": zoom,
        } for m, cyc, h in plan]
        data = parallel_fetch_decode(tasks, max_workers=8)
        prog.progress(0.5, text="Rendering frames...")

        # Phase 2: serial renders (matplotlib), with progress
        for i, (m, cyc, h) in enumerate(plan):
            prog.progress(
                0.5 + 0.5 * (i + 1) / max(len(plan), 1),
                text=f"Rendering {MODELS[m]['label']} f{h:02d} "
                     f"({i + 1}/{len(plan)})",
            )
            res = data.get((m, h))
            if isinstance(res, Exception) or res is None:
                continue
            vals, lats, lons = res
            cycle = datetime.fromisoformat(cyc)
            valid = cycle + _td(hours=h)
            title = (
                f"{MODELS[m]['label']} {cycle:%m/%d %H}Z  f{h:02d}  "
                f"valid {valid:%m/%d %H}Z"
            )
            try:
                png = render_field(
                    product, vals, lats, lons,
                    round(clat, 2), round(clon, 2), zoom, title,
                    aircraft=aircraft, routes=routes,
                )
            except Exception:
                continue
            frames.setdefault(m, {})[h] = png
        prog.empty()
        frames = {m: f for m, f in frames.items() if f}
        if skipped:
            st.caption(" | ".join(skipped))
        if not frames:
            st.error("No frames preloaded - check model/product/range.")
        else:
            import base64
            import json as _json
            model_arrays = {}
            hour_axis = hours
            for m, fd in frames.items():
                arr = []
                last = None
                for h in hour_axis:
                    if h in fd:
                        last = ("data:image/png;base64,"
                                + base64.b64encode(fd[h]).decode())
                    arr.append(last or "")
                model_arrays[m] = arr
            labels = [f"f{h:02d}" for h in hour_axis]
            order = [m for m in GRID_ORDER if m in model_arrays]
            names = {m: MODELS[m]["label"] for m in order}
            html = (
                "<style>"
                ".camgrid{display:grid;grid-template-columns:1fr 1fr;"
                "gap:6px}"
                ".camgrid img{width:100%;border:1px solid #888}"
                ".camlbl{font:bold 13px monospace;margin:2px 0}"
                ".ctl{font:13px monospace;margin:8px 0}"
                "input[type=range]{width:70%}"
                "</style>"
                "<div class='ctl'>Forecast hour: "
                "<span id='hlbl'></span><br>"
                "<input type='range' id='hsl' min='0' max='"
                + str(len(hour_axis) - 1) + "' value='0' step='1'>"
                "</div><div class='camgrid'>"
            )
            for m in order:
                html += ("<div><div class='camlbl'>" + names[m]
                         + "</div><img id='img_" + m + "'></div>")
            html += "</div><script>"
            html += "const D=" + _json.dumps(model_arrays) + ";"
            html += "const L=" + _json.dumps(labels) + ";"
            html += (
                "const sl=document.getElementById('hsl');"
                "function upd(){const i=+sl.value;"
                "document.getElementById('hlbl').textContent=L[i];"
                "for(const m in D){const el="
                "document.getElementById('img_'+m);"
                "if(D[m][i]){el.src=D[m][i];el.style.display='';}"
                "else{el.style.display='none';}}}"
                "sl.addEventListener('input',upd);upd();"
                "</script>"
            )
            rows = (len(order) + 1) // 2
            st.components.v1.html(html, height=140 + rows * 560)
            st.caption(
                f"{sum(len(v) for v in frames.values())} frames "
                f"preloaded across {len(order)} model(s). Scrub away."
            )
    else:
        # 2x2 model grid - HRRR keeps the top-right quadrant. All
        # models' data fetches run in PARALLEL inside
        # cached_grid_frame; panels then display from the dict.
        specs, notes = _panel_specs()
        routes_tuple = tuple(sorted(
            (cs, (rt["label"], rt["orig"], rt["dest"]))
            for cs, rt in routes.items()
        ))
        grid = {}
        if specs:
            with st.spinner(
                f"Fetching {len(specs)} model(s) in parallel..."
            ):
                grid = cached_grid_frame(
                    tuple(specs), product,
                    round(clat, 2), round(clon, 2), zoom,
                    aircraft, routes_t=routes_tuple,
                )

        spec_fhr = {m: fh for m, _c, fh in specs}
        top_left, top_right = st.columns(2)
        bot_left, bot_right = st.columns(2)
        for m, col in (("nam_nest", top_left), ("hrrr", top_right),
                       ("hiresw_arw", bot_left), ("rrfs", bot_right)):
            with col:
                cfg = MODELS[m]
                st.markdown(f"**{cfg['label']}**")
                if m in notes:
                    st.caption(notes[m])
                    continue
                res = grid.get(m)
                if isinstance(res, (bytes, bytearray)):
                    if spec_fhr.get(m, fhr_all) != fhr_all:
                        st.caption(
                            f"f{fhr_all:02d} beyond {cfg['label']} "
                            f"range; showing f{spec_fhr[m]:02d} "
                            f"(its max)."
                        )
                    st.image(res, use_container_width=True)
                else:
                    st.error(f"{cfg['label']}: {res}")
                if cfg["note"]:
                    st.caption(cfg["note"])

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