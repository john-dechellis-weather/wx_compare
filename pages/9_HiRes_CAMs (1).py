"""Hi-Res CAMs - convection-allowing model viewer, 2x2 model grid.

HRRR viewer (single-model for now) with a hub pre-warmer
serving JFK/MCO/FLL/DCA reflectivity instantly. Other CAM configs
stay dormant in core.hrrr_cam for later re-enable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1

def _embed_html(html: str, height: int) -> None:
    """Render raw HTML: st.iframe on newer Streamlit, else the
    deprecated components.v1.html (removed after 2026-06)."""
    fn = getattr(st, "iframe", None)
    if fn is not None:
        try:
            fn(html, height=height)
            return
        except TypeError:
            pass
    st.components.v1.html(html, height=height)


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

# Background hub pre-warmer: renders JFK/MCO/FLL/DCA reflectivity
# frames for each new model cycle so hub views serve instantly.
from core.cam_warm import (
    HUBS as WARM_HUBS, WARM_HOURS, WARM_PRODUCT, WARM_ZOOM,
    ensure_warmer_started, warm_get, warm_hours, warm_report,
    warm_status,
)
ensure_warmer_started(CACHE_ROOT)


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
    # The warmer's manifest already knows each model's latest
    # complete cycle - reading it costs a disk stat instead of
    # NOMADS round-trips, and the warmer refreshes it continuously.
    # Only fall back to live probing for models the warmer doesn't
    # track (or before its first fill), or hours beyond the warm
    # window where a newer partial cycle may exist.
    try:
        if fhr <= max(warm_hours(model)):
            wc = warm_status(CACHE_ROOT).get(model)
            if wc:
                return wc
    except Exception:
        pass
    from core.hrrr_cam import latest_cycle
    cyc = latest_cycle(model, fhr)
    return cyc.isoformat() if cyc else None


@st.cache_data(ttl=1800, show_spinner=False, max_entries=48)
def cached_grid_frame(
    model_cycle_fhr: tuple,   # ((model, cycle_iso, fhr), ...)
    product: str,
    clat: float, clon: float, zoom: float,
    style_v: int = 2,
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
    data = parallel_fetch_decode(tasks, max_workers=2)

    out = {}
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
            )
        except Exception as e:
            out[model] = f"error: {e}"
    return out


@st.cache_data(ttl=1800, show_spinner=False, max_entries=96)
def cached_panel(
    model: str, product: str, cycle_iso: str, fhr: int,
    clat: float, clon: float, zoom: float,
    style_v: int = 2,
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
    )


def build_scrub_html(frames: dict, hour_axis: list,
                     order: list) -> tuple:
    """Client-side shared-slider grid from {model: {fhr: png}}.
    Returns (html, height). Used by smooth-scrub mode AND the
    instant-open warm path."""
    import base64
    import json as _json

    from core.hrrr_cam import MODELS

    model_arrays = {}
    for m in order:
        if m not in frames or not frames[m]:
            continue
        arr = []
        for h in hour_axis:
            png = frames[m].get(h)
            arr.append(
                "data:image/png;base64,"
                + base64.b64encode(png).decode() if png else None
            )
        model_arrays[m] = arr
    labels = [f"f{h:02d}" for h in hour_axis]
    order = [m for m in order if m in model_arrays]
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
    return html, 140 + rows * 560


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("Hi-Res CAMs - 4 Panel ""(HRRR | NAM | ARW | FV3)")
st.caption(
    "Convection-allowing model viewer - aviation products, "
    "hourly-updating, with prewarmed hub views."
)

with st.sidebar:
    st.header("Region")
    icao_input = st.text_input("Airport ICAO", value="KJFK",
                               max_chars=4).strip().upper()
    hub_cols = st.columns(len(WARM_HUBS))
    for i, hub in enumerate(WARM_HUBS):
        if hub_cols[i].button(hub[1:], key=f"hub_{hub}",
                              use_container_width=True):
            st.session_state["cam_icao"] = hub
    zoom = st.slider(
        "Zoom (degrees)", 1.0, 6.0, 2.5, 0.5,
        help="Geographic window - zoom in/out. 2.5 serves "
             "instantly from the warm store; other values "
             "render live.",
    )
    panel_scale = st.slider(
        "Panel size (%)", 50, 100, 85, 5,
        help="Display size of the CAM panels",
    )
    st.session_state["panel_scale_v"] = panel_scale

    # HRRR + NAM 3km nest (proven idx path, instantaneous fields
    # like HRRR - clean scrubber pairing). NBM/RRFS stay dormant
    # in core.hrrr_cam. NAM retires Oct 2026; revisit then.
    show_models = {"hrrr": True, "nam_nest": True,
                   "hiresw_arw": True, "hiresw_fv3": True}

    st.header("Product")
    product_label = st.selectbox(
        "Product",
        options=[
            "1km Reflectivity",
            "Composite Reflectivity",
            "Echo Tops",
            "Visibility",
            "Ceiling",
            "10 m Wind Gust",
        ],
        index=0,
    )
    PRODUCT_KEY = {
        "1km Reflectivity": "REFD",
        "Composite Reflectivity": "REFC",
        "Echo Tops": "RETOP",
        "Visibility": "VIS",
        "Ceiling": "CEIL",
        "10 m Wind Gust": "GUST",
    }

    with st.expander("Warm store status (debug)"):
        try:
            st.text(
                "view state: cam_icao="
                f"{st.session_state.get('cam_icao')!r} open_hub="
                f"{st.session_state.get('open_hub')!r}"
            )
            for _ph in WARM_HUBS:
                _hits = 0
                for _pm in ["hrrr", "nam_nest",
                            "hiresw_arw", "hiresw_fv3"]:
                    if warm_get(CACHE_ROOT, _pm, _ph, 1):
                        _hits += 1
                st.text(f"live warm_get {_ph}: "
                        f"{_hits}/4 models answer at f01")
            for line in warm_report(CACHE_ROOT):
                st.text(line)
            from core.cam_warm import WARM_STYLE as _ws
            st.caption(
                f"style={_ws} + full frames = current-era "
                "renders everywhere from the warm store. Older "
                "style or partial frames = rebuild "
                "pending/stuck; paste this to Claude."
            )
        except Exception as e:
            st.text(f"report failed: {e}")

    smooth = st.checkbox(
        "Smooth scrub mode", value=True,
        help="Preloads every hour in the range below, then scrubbing "
             "is instant (frames swap in the browser, no reloading). "
             "Preload takes a while on first run; hours cache on the "
             "server, so rebuilding later is fast.",
    )
    if smooth:
        fhr_lo, fhr_hi = st.slider(
            "Preload hours", 0, 60, (0, 24),
            help="All hours in this range are fetched upfront. "
             "Span capped at 24 hours to keep the page light.",
        )
    else:
        fhr_all = st.slider(
            "Forecast hour (all models)", 0, 60, 1,
            help="HRRR f18 hourly (f48 synoptic); NAM to f60 "
                 "on synoptics; HRW ARW/FV3 to f48 on 00/12Z. "
                 "Panels clamp to their own max.",
        )

    st.divider()
    run_button = st.button("Render", type="primary",
                           use_container_width=True)

if run_button and icao_input:
    st.session_state["cam_icao"] = icao_input

active = st.session_state.get("cam_icao")

if active:
    # Always-visible hub switcher: session state pins cam_icao
    # across opens, which once trapped the page on a single hub
    # (the welcome branch's switcher became unreachable). One tap
    # now swaps stations from anywhere.
    _sw_cols = st.columns(len(WARM_HUBS))
    for _i, _hk in enumerate(WARM_HUBS):
        _lbl = ("* " + _hk[1:]
                if _hk == active else _hk[1:])
        if _sw_cols[_i].button(_lbl, key=f"sw_{_hk}",
                               use_container_width=True):
            st.session_state["cam_icao"] = _hk
            st.rerun()

    icao = active
    now = datetime.now(timezone.utc)
    bucket10 = now.strftime("%Y%m%d%H") + str(now.minute // 10)

    coords = cached_station_coords(icao)
    if coords is None:
        st.error(f"Cannot resolve coordinates for {icao}.")
        st.stop()
    clat, clon = coords

    product = PRODUCT_KEY[product_label]

    st.info(f"**{icao}** | {product_label}")

    from core.hrrr_cam import MODELS

    PRODUCT_LABELS_SHORT = {
        "REFD": "1km reflectivity",
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
                pd = cfg.get("_probe_diag") or {}
                if pd:
                    msg += " Probe verdicts: " + "; ".join(
                        f"{u.split('/com/')[-1].split('/rrfs.')[0]}"
                        f" -> {v}"
                        for u, v in list(pd.items())[:4]
                    )
                notes[m] = msg
                continue
            specs.append((m, cyc, fh))
        return specs, notes

    GRID_ORDER = ["hrrr", "nam_nest", "hiresw_arw", "hiresw_fv3"]

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
                pd = MODELS[m].get("_probe_diag") or {}
                extra = ("; probes: " + "; ".join(
                    list(pd.values())[:3]) if pd else "")
                skipped.append(
                    f"{cfg['label']}: no cycle found{extra}"
                )
                continue
            for h in mh:
                plan.append((m, cycle_iso, h))

        # Pull any prewarmed frames first; only the rest download.
        warm_ok_s = (
            icao in WARM_HUBS and product == WARM_PRODUCT
            and abs(zoom - WARM_ZOOM) < 0.01
        )
        if warm_ok_s:
            still_plan = []
            for m, cyc, h in plan:
                got = warm_get(CACHE_ROOT, m, icao, h)                     if h in warm_hours(m) else None
                if got:
                    frames.setdefault(m, {})[h] = got[0]
                else:
                    still_plan.append((m, cyc, h))
            if len(still_plan) < len(plan):
                st.caption(
                    f"{len(plan) - len(still_plan)} prewarmed frames "
                    f"loaded instantly from disk"
                )
            plan = still_plan

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
        data = parallel_fetch_decode(tasks, max_workers=2)
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
                )
            except Exception:
                continue
            frames.setdefault(m, {})[h] = png
        prog.empty()
        if skipped:
            st.warning(" | ".join(skipped))
        frames = {m: f for m, f in frames.items() if f}
        if skipped:
            st.caption(" | ".join(skipped))
        if not frames:
            st.error("No frames preloaded - check model/product/range.")
        else:
            html, hgt = build_scrub_html(
                frames, hours, GRID_ORDER
            )
            _sc = st.session_state.get("panel_scale_v", 85)
            if _sc >= 100:
                _embed_html(html, height=hgt)
            else:
                _wl, _wm, _wr = st.columns(
                    [(100 - _sc) / 2, float(_sc),
                     (100 - _sc) / 2])
                with _wm:
                    _embed_html(html, height=hgt)
            st.caption(
                f"{sum(len(v) for v in frames.values())} frames "
                f"preloaded across {len(frames)} model(s). Scrub away."
            )
    else:
        # 2x2 model grid - HRRR keeps the top-right quadrant. All
        # models' data fetches run in PARALLEL inside
        # cached_grid_frame; panels then display from the dict.
        specs, notes = _panel_specs()
        # Warm-store eligibility: hub airport, warm product/zoom,
        # in-range hour.
        warm_ok = (
            icao in WARM_HUBS and product == WARM_PRODUCT
            and abs(zoom - WARM_ZOOM) < 0.01
        )
        grid = {}
        warm_hits = []
        remaining = list(specs)
        if warm_ok:
            still = []
            for m, cyc, fh in remaining:
                got = warm_get(CACHE_ROOT, m, icao, fh)                     if fh in WARM_HOURS else None
                if got:
                    grid[m] = got[0]
                    warm_hits.append(m)
                else:
                    still.append((m, cyc, fh))
            remaining = still
        if warm_hits:
            st.caption(
                f"Prewarmed hub frames: {', '.join(warm_hits)} "
                f"(instant from disk)"
            )
        if remaining:
            with st.spinner(
                f"Fetching {len(remaining)} model(s) in parallel..."
            ):
                grid.update(cached_grid_frame(
                    tuple(remaining), product,
                    round(clat, 2), round(clon, 2), zoom,
                ))

        spec_fhr = {m: fh for m, _c, fh in specs}
        model_cols = st.columns(len(GRID_ORDER))
        for m, col in zip(GRID_ORDER, model_cols):
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
                    if panel_scale >= 100:
                        st.image(res,
                                 use_container_width=True)
                    else:
                        st.image(
                            res,
                            width=int(430 * panel_scale / 100),
                        )
                else:
                    st.error(f"{cfg['label']}: {res}")
                if cfg.get("note"):
                    st.caption(cfg["note"])

else:
    # INSTANT-OPEN: before any click, serve the prewarmed default-hub
    # scrub straight from disk (sub-second). Custom anything = the
    # normal Render flow.
    _hub_keys = list(WARM_HUBS)
    _hub_cols = st.columns(len(_hub_keys))
    for _i, _hk in enumerate(_hub_keys):
        if _hub_cols[_i].button(_hk[1:], key=f"open_{_hk}",
                                use_container_width=True):
            st.session_state["open_hub"] = _hk
    _open_hub = st.session_state.get("open_hub", "KJFK")
    _OPEN_ORDER = ["hrrr", "nam_nest", "hiresw_arw",
                   "hiresw_fv3"]
    _warm_frames: dict = {}
    try:
        for _m in _OPEN_ORDER:
            for _h in warm_hours(_m):
                got = warm_get(CACHE_ROOT, _m, _open_hub, _h)
                if got:
                    _warm_frames.setdefault(_m, {})[_h] = got[0]
    except Exception:
        _warm_frames = {}
    _n_warm = sum(len(v) for v in _warm_frames.values())
    if _n_warm >= 6:
        st.info(
            f"**{_open_hub}** | 1km Reflectivity | prewarmed - "
            f"scrub instantly, or set up a custom view in the "
            f"sidebar and click **Render**."
        )
        _axis = sorted({h for v in _warm_frames.values()
                        for h in v})
        _html, _hgt = build_scrub_html(
            _warm_frames, _axis, _OPEN_ORDER
        )
        _sc2 = st.session_state.get("panel_scale_v", 85)
        if _sc2 >= 100:
            _embed_html(_html, height=_hgt)
        else:
            _owl, _owm, _owr = st.columns(
                [(100 - _sc2) / 2, float(_sc2), (100 - _sc2) / 2])
            with _owm:
                _embed_html(_html, height=_hgt)
        st.caption(
            f"{_n_warm} prewarmed frames served from disk at page "
            f"open."
        )
    else:
        st.info(
            "Enter an airport in the sidebar and click **Render**. "
            "(Instant-open view appears once the hub warmer has "
            "built its first frames.)"
        )
    st.markdown(
        """
        ### What this page is

        The classic 4-panel CAM comparison - HRRR, NAM 3km,
        and both HiRes Window members (ARW, FV3) - centered on
        your airport,
        aviation products only: 1km reflectivity, echo tops, visibility, ceiling, and gusts,
        with smooth scrubbing. Hub buttons serve prewarmed HRRR
        reflectivity instantly; NAM renders live.
        """
    )
