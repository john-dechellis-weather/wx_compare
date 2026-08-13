"""JBU Flight Tracker — radar + IR satellite centered on a live flight.

Enter a JetBlue flight (e.g. JBU123). The page resolves its live
position (adsb.lol), auto-selects the nearest WSR-88D, and renders
radar centered on the aircraft with the target in red and other JBU
traffic in blue. Radar products: Level III reflectivity (default,
fast), Level III echo tops, Level II reflectivity (full res, slower),
plus loop variants. IR satellite is opt-in.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    page_title="BlueMet — JBU Flight Tracker",
    layout="wide",
)

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()


_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

try:
    from core.mov_sampler import ensure_sampler_started
    ensure_sampler_started(CACHE_ROOT)
except Exception:
    pass   # sampler is optional; never block the Tracker

RANGE_WARN_KM = 200.0


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False, max_entries=20)
def cached_flight(callsign: str, minute_bucket: str):
    from core.flights import fetch_callsign
    return fetch_callsign(callsign)


@st.cache_data(ttl=120, show_spinner=False, max_entries=4)
def cached_airborne_sweep(bucket: str):
    """JBU aircraft currently airborne near the hub network - used
    when a requested flight isn't found, both as suggestions and as a
    live diagnostic (empty mid-day = position feed problem, not a
    landed flight)."""
    from core.flights import fetch_positions_near
    hubs = {
        "JFK": (40.64, -73.78), "BOS": (42.36, -71.01),
        "MCO": (28.43, -81.31), "FLL": (26.07, -80.15),
        "DCA": (38.85, -77.04), "LAX": (33.94, -118.41),
    }
    seen = {}
    for name, (la, lo) in hubs.items():
        try:
            for p in fetch_positions_near(la, lo, radius_deg=3.0):
                if p.callsign not in seen:
                    seen[p.callsign] = (p, name)
        except Exception:
            continue
    return [
        (cs, p.alt_ft, near) for cs, (p, near) in sorted(seen.items())
    ]


@st.cache_data(ttl=120, show_spinner=False, max_entries=20)
def cached_others(lat: float, lon: float, radius: float, bucket: str):
    from core.flights import fetch_positions_near
    return fetch_positions_near(lat, lon, radius_deg=radius)


def _routes_dict(routes_t):
    """Rebuild {cs: {label, orig, dest}} from the hashable tuple form."""
    return {
        cs: {"label": lbl, "orig": o, "dest": d}
        for cs, (lbl, o, d) in routes_t
    }


@st.cache_data(ttl=600, show_spinner=False, max_entries=10)
def cached_fleet_routes(planes, bucket: str):
    """Origin/destination for every visible aircraft, as a hashable
    sorted tuple: ((cs, (label, (olat, olon), (dlat, dlon))), ...)."""
    from core.flights import fetch_routes
    try:
        routes = fetch_routes(list(planes))
    except Exception:
        return tuple()
    return tuple(sorted(
        (cs, (rt["label"], rt["orig"], rt["dest"]))
        for cs, rt in routes.items()
    ))


@st.cache_data(ttl=120, show_spinner=False, max_entries=10)
def cached_fleet_trails(planes, bucket: str):
    """Trails for every visible aircraft (target + others). Returns
    ({callsign: points_tuple} as sorted tuple, summary)."""
    from core.flights import fetch_fleet_trails
    trails, summary = fetch_fleet_trails(
        list(planes), CACHE_ROOT / "tracks"
    )
    return (
        tuple(sorted((cs, tuple(tr)) for cs, tr in trails.items())),
        summary,
    )


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_l3_frame(
    product: str, site: str, clat: float, clon: float,
    zoom: float, bucket: str, target, others, trail=tuple(),
    others_trails=tuple(), routes_t=tuple(),
) -> bytes:
    from core.radar3 import fetch_latest, parse_l3, render_l3
    raw = fetch_latest(product, site)
    parsed = parse_l3(raw)
    return render_l3(
        parsed, product, clat, clon, zoom, site,
        target_aircraft=target, other_aircraft=others,
        title_note="latest", trail=trail,
        others_trails=dict(others_trails),
        routes=_routes_dict(routes_t),
    )


@st.cache_data(ttl=300, show_spinner=False, max_entries=6)
def cached_l3_loop(
    product: str, site: str, clat: float, clon: float,
    zoom: float, bucket: str, target, others, n: int = 6,
    trail=tuple(), others_trails=tuple(), routes_t=tuple(),
):
    from core.radar3 import fetch_recent, parse_l3, render_l3
    files = fetch_recent(product, site, n=n)
    frames = []
    for raw, name in files:
        try:
            parsed = parse_l3(raw)
            png = render_l3(
                parsed, product, clat, clon, zoom, site,
                target_aircraft=target, other_aircraft=others,
                title_note=name, trail=trail,
                others_trails=dict(others_trails),
                routes=_routes_dict(routes_t),
            )
            frames.append((png, name))
        except Exception:
            continue
    gif = _frames_to_gif(frames) if len(frames) > 1 else b""
    return frames, gif


@st.cache_data(ttl=90, show_spinner=False, max_entries=6)
def cached_l2_realtime(
    site: str, clat: float, clon: float, zoom: float,
    bucket: str, callsign: str, others, trail=tuple(),
    others_trails=tuple(), routes_t=tuple(),
):
    """Near-live Level II from the AWS chunk feed: the 0.5 deg sweep
    ~1 minute into the volume instead of after the full scan. Returns
    (png, info) - raises on any failure so the caller falls back to
    the completed-volume IDD path."""
    import os
    import tempfile

    from core.radar import _ScanRef, _download_and_render
    from core.radar_l2rt import fetch_live_volume_bytes

    blob, info = fetch_live_volume_bytes(site)
    tmpdir = tempfile.mkdtemp(prefix="l2rt_")
    path = os.path.join(tmpdir, f"{site}_rt")
    with open(path, "wb") as fh:
        fh.write(blob)
    scan = _ScanRef(
        filename=f"{site} live vol {info['volume']}",
        scan_time=datetime.fromisoformat(info["chunk_time"]),
        download_url="",
    )
    refl_png, _vel, _name = _download_and_render(
        scan, clat, clon, callsign, site, zoom,
        include_velocity=False,
        overlay_aircraft=list(others),
        trail=trail,
        others_trails=dict(others_trails),
        routes=_routes_dict(routes_t),
        prefetched_path=path,
        newest_low_sweep=True,
    )
    return refl_png, info


@st.cache_data(ttl=300, show_spinner=False, max_entries=4)
def cached_l2(
    site: str, clat: float, clon: float, zoom: float,
    bucket: str, callsign: str, others, loop: bool, trail=tuple(),
    others_trails=tuple(), routes_t=tuple(),
):
    from core.radar import fetch_and_render_radar_loop
    minutes = 45 if loop else 30   # single: wide window, newest frame kept
    start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    st4 = site[1:] if len(site) == 4 and site.startswith("K") else site
    frames, _ = fetch_and_render_radar_loop(
        start_time=start,
        duration_min=minutes,
        aircraft_lat=clat,
        aircraft_lon=clon,
        callsign=callsign,
        station=st4,
        zoom_deg=zoom,
        include_velocity=False,
        overlay_aircraft=others,
        trail=trail,
        others_trails=dict(others_trails),
        routes=_routes_dict(routes_t),
    )
    if not loop:
        frames = frames[-1:]
    gif = _frames_to_gif(frames) if len(frames) > 1 else b""
    return frames, gif


@st.cache_data(ttl=300, show_spinner=False, max_entries=4)
def cached_glm(clat: float, clon: float, zoom: float, bucket: str):
    """GLM lightning flash locations from the last ~6 minutes within the
    view window. Returns tuple of (lat, lon) pairs; empty on failure."""
    import xarray as xr
    from goes2go.data import goes_timerange

    sat = "goes19" if clon > -105 else "goes18"
    end = datetime.now(timezone.utc) - timedelta(minutes=6)
    start = end - timedelta(minutes=6)
    try:
        files = goes_timerange(
            start=start.replace(tzinfo=None),
            end=end.replace(tzinfo=None),
            satellite=sat,
            product="GLM-L2-LCFA",
            return_as="filelist",
            download=True,
            overwrite=False,
            verbose=False,
            save_dir=str(CACHE_ROOT / "glm"),
        )
    except Exception:
        return tuple()
    pts = []
    base = CACHE_ROOT / "glm"
    for _, row in files.iterrows():
        try:
            ds = xr.open_dataset(base / row["file"])
            la = ds["flash_lat"].values
            lo = ds["flash_lon"].values
            ds.close()
        except Exception:
            continue
        for a, o in zip(la, lo):
            if (abs(float(a) - clat) <= zoom
                    and abs(float(o) - clon) <= zoom):
                pts.append((round(float(a), 3), round(float(o), 3)))
    return tuple(pts)


@st.cache_data(ttl=600, show_spinner=False, max_entries=4)
def cached_ir(clat: float, clon: float, callsign: str, bucket: str,
              lightning=tuple(), trail=tuple(),
              others_trails=tuple(), routes_t=tuple()) -> bytes:
    from core.satellite import fetch_goes_data, render_infrared
    # GOES-19 became GOES-East in 2025 (GOES-16 retired); GOES-18 is West.
    # ABI files land on S3 with real latency — ask 30 min back, and step
    # further back if the nearest-time lookup finds nothing yet.
    sat = "goes19" if clon > -105 else "goes18"
    last_err = None
    for minutes_back in (30, 75, 120):
        try:
            ds, scan_time = fetch_goes_data(
                target_time=datetime.now(timezone.utc)
                - timedelta(minutes=minutes_back),
                satellite=sat,
                cache_dir=CACHE_ROOT / "satellite",
            )
            return render_infrared(
                ds, clat, clon, callsign, lightning=lightning,
                trail=trail, others_trails=dict(others_trails),
                routes=_routes_dict(routes_t),
            )
        except Exception as e:
            last_err = e
    raise RuntimeError(f"GOES {sat} unavailable back to 2h: {last_err}")


def _frames_to_gif(
    frames, width: int = 800, frame_ms: int = 450, last_hold_ms: int = 1400
) -> bytes:
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


def _fmt_alt(alt_ft):
    if alt_ft is None:
        return "alt unknown"
    return f"FL{int(round(alt_ft / 100)):03d}"


def _client_scrubber(frames, key: str) -> str:
    """Instant client-side frame scrubber with play/pause plus
    wheel-zoom (toward cursor), drag-pan, and double-click reset.
    All pure browser JS on the already-shipped frames."""
    import base64
    import json as _json

    srcs = ["data:image/png;base64," + base64.b64encode(p).decode()
            for p, _n in frames]
    names = [n for _p, n in frames]
    n = len(srcs)
    return (
        "<style>"
        ".scr{font:13px monospace}"
        ".scr .vp{overflow:hidden;border:1px solid #888;"
        "cursor:zoom-in;position:relative;"
        "height:700px}"
        ".scr .vp.z{cursor:grab}"
        ".scr .vp.drag{cursor:grabbing}"
        ".scr img{max-width:100%;max-height:100%;"
        "width:auto;height:auto;display:block;"
        "transform-origin:0 0;user-select:none;"
        "-webkit-user-drag:none}"
        ".scr input[type=range]{width:55%;vertical-align:middle}"
        ".scr button{font:bold 13px monospace;margin-right:6px;"
        "padding:2px 10px}"
        ".scr .zl{position:absolute;right:4px;top:4px;"
        "background:#000a;color:#0f0;padding:1px 6px;"
        "font:11px monospace;display:none}"
        "</style>"
        "<div class='scr'>"
        "<div class='vp' id='vp_" + key + "'>"
        "<img id='im_" + key + "'>"
        "<span class='zl' id='zl_" + key + "'></span>"
        "</div>"
        "<div>"
        "<button id='pb_" + key + "'>PAUSE</button>"
        "<input type='range' id='sl_" + key + "' min='0' max='"
        + str(n - 1) + "' value='" + str(n - 1) + "' step='1'>"
        " <span id='lb_" + key + "'></span>"
        "</div></div>"
        "<script>"
        "(function(){"
        "const F=" + _json.dumps(srcs) + ";"
        "const N=" + _json.dumps(names) + ";"
        "const im=document.getElementById('im_" + key + "');"
        "const vp=document.getElementById('vp_" + key + "');"
        "const zl=document.getElementById('zl_" + key + "');"
        "const sl=document.getElementById('sl_" + key + "');"
        "const lb=document.getElementById('lb_" + key + "');"
        "const pb=document.getElementById('pb_" + key + "');"
        "let playing=true;let t=null;"
        "let s=1,tx=0,ty=0;"
        "function apply(){"
        "im.style.transform='translate('+tx+'px,'+ty+'px) "
        "scale('+s+')';"
        "vp.classList.toggle('z',s>1);"
        "zl.style.display=s>1?'block':'none';"
        "zl.textContent=s.toFixed(1)+'x';}"
        "function clamp(){"
        "const w=im.clientWidth,h=im.clientHeight;"
        "tx=Math.min(0,Math.max(tx,w-w*s));"
        "ty=Math.min(0,Math.max(ty,h-h*s));}"
        "vp.addEventListener('wheel',function(e){"
        "e.preventDefault();"
        "const r=vp.getBoundingClientRect();"
        "const mx=e.clientX-r.left,my=e.clientY-r.top;"
        "const s0=s;"
        "s=Math.min(6,Math.max(1,s*(e.deltaY<0?1.2:1/1.2)));"
        "tx=mx-(mx-tx)*(s/s0);ty=my-(my-ty)*(s/s0);"
        "if(s===1){tx=0;ty=0;}"
        "clamp();apply();},{passive:false});"
        "let dragging=false,dx=0,dy=0;"
        "vp.addEventListener('mousedown',function(e){"
        "if(s<=1)return;dragging=true;"
        "vp.classList.add('drag');"
        "dx=e.clientX-tx;dy=e.clientY-ty;e.preventDefault();});"
        "window.addEventListener('mousemove',function(e){"
        "if(!dragging)return;"
        "tx=e.clientX-dx;ty=e.clientY-dy;clamp();apply();});"
        "window.addEventListener('mouseup',function(){"
        "dragging=false;vp.classList.remove('drag');});"
        "vp.addEventListener('dblclick',function(){"
        "s=1;tx=0;ty=0;apply();});"
        "function show(i){im.src=F[i];lb.textContent=N[i];}"
        "function step(){let i=(+sl.value+1)%F.length;"
        "sl.value=i;show(i);"
        "t=setTimeout(step,i==F.length-1?1400:450);}"
        "sl.addEventListener('input',function(){"
        "clearTimeout(t);playing=false;pb.textContent='PLAY';"
        "show(+sl.value);});"
        "pb.addEventListener('click',function(){"
        "if(playing){clearTimeout(t);playing=false;"
        "pb.textContent='PLAY';}"
        "else{playing=true;pb.textContent='PAUSE';step();}});"
        "show(+sl.value);t=setTimeout(step,450);apply();"
        "})();"
        "</script>"
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("JBU Flight Tracker")
st.caption(
    "Radar and IR satellite centered on a live JetBlue flight. "
    "Radar site auto-selected nearest the aircraft."
)

with st.sidebar:
    st.header("Flight")
    flight_input = st.text_input(
        "Flight (callsign or number)",
        value="",
        max_chars=8,
        help="JBU123, B6123, or just 123 — all resolve to callsign JBU123.",
    ).strip().upper()

    zoom = st.slider("Zoom (degrees)", 0.5, 4.0, 1.5, 0.5)

    radar_product = st.radio(
        "Radar product",
        options=[
            "Level III Reflectivity (fast)",
            "Level III Echo Tops",
            "Level II Reflectivity (full res, slower)",
        ],
        index=0,
    )
    loop_mode = st.checkbox("Loop (last ~30-45 min)", value=False)

    show_ir = st.checkbox("IR Satellite (GOES C13)", value=False)

    site_override = st.text_input(
        "Radar site override (blank = auto)",
        value="", max_chars=4,
    ).strip().upper()

    st.divider()
    run_button = st.button("Track", type="primary", use_container_width=True)


def _normalize_callsign(s: str) -> str:
    s = s.replace(" ", "")
    if s.startswith("JBU"):
        return s
    if s.startswith("B6"):
        return "JBU" + s[2:]
    if s.isdigit():
        return "JBU" + s
    return s


if run_button and flight_input:
    st.session_state["track_cs"] = _normalize_callsign(flight_input)

track_cs = st.session_state.get("track_cs")

if track_cs:
    now = datetime.now(timezone.utc)
    minute_bucket = now.strftime("%Y%m%d%H%M")
    bucket5 = now.strftime("%Y%m%d%H") + str(now.minute // 5)

    with st.spinner(f"Locating {track_cs}..."):
        target = cached_flight(track_cs, minute_bucket)

    if target is None:
        st.warning(
            f"**{track_cs}** not found on live ADS-B — it may not be "
            "airborne yet, already landed, or briefly out of receiver "
            "coverage. Try again in a minute, or check the flight number."
        )
        from core.flights import last_callsign_diag
        _diag = last_callsign_diag()
        if _diag:
            st.caption(f"Source-by-source: {_diag}")
        with st.spinner("Checking what IS airborne near the hubs..."):
            airborne = cached_airborne_sweep(bucket5)
        if airborne:
            st.markdown(
                f"**{len(airborne)} JBU currently airborne near the "
                f"hub network** (position feed is healthy - the "
                f"requested flight just isn't up):"
            )
            import pandas as _pd
            st.dataframe(_pd.DataFrame([{
                "Flight": (f"B6 {cs[3:]}" if cs.startswith("JBU")
                           else cs),
                "Callsign": cs,
                "Altitude": (f"{int(a):,} ft" if a is not None
                             else "?"),
                "Near": near,
            } for cs, a, near in airborne[:15]]),
                use_container_width=True, hide_index=True)
            st.caption(
                "Enter any of these callsigns above to track it. "
                "(Sweep covers hub vicinity only - a mid-country "
                "flight can be airborne yet absent from this list.)"
            )
        else:
            st.error(
                "Hub sweep found ZERO airborne JBU - during normal "
                "operating hours that means the position feed "
                "(adsb.lol) is failing, not that flights are landed. "
                "Tell Claude this happened."
            )
        st.stop()

    clat, clon = target.lat, target.lon

    # Radar site selection
    from core.nexrad_sites import nearest_site, site_coords
    if site_override and site_coords(site_override):
        site, dist_km = site_override, None
        from core.nexrad_sites import _haversine_km
        slat, slon = site_coords(site_override)
        dist_km = _haversine_km(clat, clon, slat, slon)
    else:
        site, dist_km = nearest_site(clat, clon)

    hdr = (
        f"**{target.callsign}** \u00b7 {clat:.3f}\u00b0, {clon:.3f}\u00b0 "
        f"\u00b7 {_fmt_alt(target.alt_ft)}"
    )
    if target.heading_deg is not None:
        hdr += f" \u00b7 trk {int(target.heading_deg):03d}\u00b0"
    hdr += f" \u00b7 radar **{site}** ({dist_km:.0f} km)"
    st.info(hdr)
    if dist_km and dist_km > RANGE_WARN_KM:
        st.warning(
            f"Aircraft is {dist_km:.0f} km from {site} — beyond "
            f"~{RANGE_WARN_KM:.0f} km the beam overshoots low altitudes "
            "and coverage degrades (no NEXRAD over open ocean)."
        )

    # Other JBU traffic in the window (target excluded)
    others_all = cached_others(round(clat, 2), round(clon, 2), zoom, bucket5)
    others = [a for a in others_all if a.callsign != target.callsign]

    # Flight path trails for the whole visible fleet
    trails_t, trails_summary = cached_fleet_trails(
        tuple([target] + others), now.strftime("%Y%m%d%H%M"),
    )
    all_trails = dict(trails_t)
    trail = all_trails.pop(target.callsign, tuple())
    others_trails_t = tuple(sorted(all_trails.items()))
    routes_t = cached_fleet_routes(
        tuple([target] + others), now.strftime("%Y%m%d%H")
    )
    st.caption(
        f"Trails: {trails_summary} | Routes: {len(routes_t)} resolved"
    )
    if not routes_t:
        from core.flights import last_route_error
        _rerr = last_route_error()
        if _rerr:
            st.caption(f"Route lookup: {_rerr}")

    # --- Radar ---
    st.subheader("Radar")
    ckey_lat, ckey_lon = round(clat, 2), round(clon, 2)
    try:
        # NOTE: "Level III...".startswith("Level II") is True (string
        # prefix!) — branch on Level III explicitly, never on the
        # "Level II" prefix.
        if not radar_product.startswith("Level III"):
            with st.spinner(
                "Rendering Level II"
                + (" loop (30-60s)..." if loop_mode else " (10-20s)...")
            ):
                if not loop_mode:
                    try:
                        rt_png, rt_info = cached_l2_realtime(
                            site, ckey_lat, ckey_lon, zoom, bucket5,
                            target.callsign, others, trail=trail,
                            others_trails=others_trails_t,
                            routes_t=routes_t,
                        )
                        frames, gif = [(rt_png, "live-chunks")], b""
                        st.caption(
                            f"Level II real-time: volume "
                            f"{rt_info['volume']}, chunk "
                            f"{rt_info['newest_chunk']}, ~"
                            f"{rt_info['age_s']}s old "
                            f"({rt_info['n_used']}/"
                            f"{rt_info['n_chunks']} chunks)"
                        )
                    except Exception as rt_err:
                        st.caption(
                            f"Real-time chunk feed unavailable "
                            f"({rt_err}); using completed volume."
                        )
                        frames, gif = cached_l2(
                            site, ckey_lat, ckey_lon, zoom, bucket5,
                            target.callsign, others, loop_mode,
                            trail=trail,
                            others_trails=others_trails_t,
                            routes_t=routes_t,
                        )
                else:
                    frames, gif = cached_l2(
                        site, ckey_lat, ckey_lon, zoom, bucket5,
                        target.callsign, others, loop_mode, trail=trail,
                        others_trails=others_trails_t, routes_t=routes_t,
                    )
        else:
            product = "ET" if "Echo Tops" in radar_product else "REF"
            if loop_mode:
                with st.spinner("Rendering Level III loop..."):
                    frames, gif = cached_l3_loop(
                        product, site, ckey_lat, ckey_lon, zoom, bucket5,
                        target, others, trail=trail,
                        others_trails=others_trails_t,
                        routes_t=routes_t,
                    )
            else:
                with st.spinner("Rendering Level III..."):
                    png = cached_l3_frame(
                        product, site, ckey_lat, ckey_lon, zoom, bucket5,
                        target, others, trail=trail,
                        others_trails=others_trails_t,
                        routes_t=routes_t,
                    )
                    frames, gif = [(png, "sn.last")], b""
    except Exception as e:
        frames, gif = [], b""
        st.error(f"Radar fetch/render failed: {e}")

    if len(frames) > 1:
        # Client-side scrubber: auto-plays like the old GIF, but the
        # slider swaps frames instantly in the browser - no reloading.
        _embed_html(
            _client_scrubber(frames, key="rad"), height=770,
        )
        if gif:
            st.download_button(
                "Download loop GIF", data=gif,
                file_name=f"tracker_{site}.gif", mime="image/gif",
                key="dl_tracker_gif",
            )
    elif frames:
        st.image(frames[-1][0], use_container_width=True)
        st.caption(f"`{frames[-1][1]}`")

    # --- IR Satellite (opt-in) ---
    if show_ir:
        st.subheader("IR Satellite (GOES Band 13) + GLM Lightning")
        with st.spinner("Fetching GOES + GLM (15-40s first time)..."):
            try:
                flashes = cached_glm(ckey_lat, ckey_lon, zoom, bucket5)
            except Exception:
                flashes = tuple()
            try:
                ir_png = cached_ir(
                    ckey_lat, ckey_lon, target.callsign, bucket5,
                    lightning=flashes, trail=trail,
                    others_trails=others_trails_t,
                    routes_t=routes_t,
                )
                st.image(ir_png, use_container_width=True)
                st.caption(
                    f"{len(flashes)} GLM flashes (last ~6 min) in view"
                    if flashes else
                    "No GLM flashes in view (last ~6 min)"
                )
            except Exception as e:
                st.warning(f"Satellite fetch failed: {e}")

else:
    st.info(
        "Enter a JetBlue flight number in the sidebar and click **Track**."
    )
    st.markdown(
        """
        ### What this does

        Locates a live JetBlue flight via ADS-B, auto-selects the nearest
        NEXRAD site, and renders radar centered on the aircraft:

        - **Level III Reflectivity** — fast (~20 KB products, updated
          every volume scan)
        - **Level III Echo Tops** — storm-top heights in kft, the
          convective-avoidance view
        - **Level II Reflectivity** — full 0.25 km resolution from raw
          volume data (slower)
        - **Loop** variants of each, plus opt-in **GOES IR satellite**

        The tracked flight renders as a red triangle; other JetBlue
        aircraft in the window render in blue.
        """
    )
