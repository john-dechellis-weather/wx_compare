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

RANGE_WARN_KM = 200.0


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False, max_entries=20)
def cached_flight(callsign: str, minute_bucket: str):
    from core.flights import fetch_callsign
    return fetch_callsign(callsign)


@st.cache_data(ttl=120, show_spinner=False, max_entries=20)
def cached_others(lat: float, lon: float, radius: float, bucket: str):
    from core.flights import fetch_positions_near
    return fetch_positions_near(lat, lon, radius_deg=radius)


@st.cache_data(ttl=300, show_spinner=False, max_entries=10)
def cached_l3_frame(
    product: str, site: str, clat: float, clon: float,
    zoom: float, bucket: str, target, others,
) -> bytes:
    from core.radar3 import fetch_latest, parse_l3, render_l3
    raw = fetch_latest(product, site)
    parsed = parse_l3(raw)
    return render_l3(
        parsed, product, clat, clon, zoom, site,
        target_aircraft=target, other_aircraft=others,
        title_note="latest",
    )


@st.cache_data(ttl=300, show_spinner=False, max_entries=6)
def cached_l3_loop(
    product: str, site: str, clat: float, clon: float,
    zoom: float, bucket: str, target, others, n: int = 6,
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
                title_note=name,
            )
            frames.append((png, name))
        except Exception:
            continue
    gif = _frames_to_gif(frames) if len(frames) > 1 else b""
    return frames, gif


@st.cache_data(ttl=300, show_spinner=False, max_entries=4)
def cached_l2(
    site: str, clat: float, clon: float, zoom: float,
    bucket: str, callsign: str, others, loop: bool,
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
              lightning=tuple()) -> bytes:
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
                ds, clat, clon, callsign, lightning=lightning
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
                frames, gif = cached_l2(
                    site, ckey_lat, ckey_lon, zoom, bucket5,
                    target.callsign, others, loop_mode,
                )
        else:
            product = "ET" if "Echo Tops" in radar_product else "REF"
            if loop_mode:
                with st.spinner("Rendering Level III loop..."):
                    frames, gif = cached_l3_loop(
                        product, site, ckey_lat, ckey_lon, zoom, bucket5,
                        target, others,
                    )
            else:
                with st.spinner("Rendering Level III..."):
                    png = cached_l3_frame(
                        product, site, ckey_lat, ckey_lon, zoom, bucket5,
                        target, others,
                    )
                    frames, gif = [(png, "sn.last")], b""
    except Exception as e:
        frames, gif = [], b""
        st.error(f"Radar fetch/render failed: {e}")

    if gif:
        st.image(gif, use_container_width=True)
        st.download_button(
            "Download loop GIF", data=gif,
            file_name=f"tracker_{site}.gif", mime="image/gif",
            key="dl_tracker_gif",
        )
        with st.expander("Frame-by-frame (full resolution)"):
            idx = st.slider("Frame", 0, len(frames) - 1, len(frames) - 1,
                            key="tracker_idx")
            png, name = frames[idx]
            st.image(png, use_container_width=True)
            st.caption(f"`{name}`")
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
                    lightning=flashes,
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
