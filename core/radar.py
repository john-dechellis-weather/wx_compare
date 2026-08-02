"""NEXRAD Level II radar fetching and plot rendering.

Data source: AWS public archive (noaa-nexrad-level2) via nexradaws —
full history back to 1991, no credentials needed.

Each Level II volume contains BOTH reflectivity and velocity, so both
plots come from the same downloaded file (no product-code fallbacks).

Decode: metpy Level2File. Reflectivity from the lowest surveillance
sweep (0.5° CS), velocity from the lowest Doppler sweep (0.5° CD).
Velocity is converted from m/s to knots for aviation use.
"""
from __future__ import annotations

import io
import shutil
import tempfile
from datetime import datetime, timedelta

import numpy as np

_MS_TO_KT = 1.94384


# ---------------------------------------------------------------------------
# Public API — single frame
# ---------------------------------------------------------------------------
def fetch_and_render_radar(
    target_time: datetime,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
) -> tuple[bytes, bytes, str, str]:
    """Fetch the Level II volume nearest target_time, render REF + VEL.

    Returns (reflectivity_png, velocity_png_or_empty, refl_time, vel_time).
    """
    scans = _find_scans(
        station,
        target_time - timedelta(minutes=20),
        target_time + timedelta(minutes=20),
    )
    if not scans:
        raise ValueError(
            f"No Level II volumes found for {_station4(station)} within "
            f"20 minutes of {target_time:%Y-%m-%d %H:%M UTC}."
        )

    # Pick the scan closest to the requested time
    tgt = target_time.replace(tzinfo=None)
    best = min(scans, key=lambda s: abs(s.scan_time.replace(tzinfo=None) - tgt))

    refl_png, vel_png, name = _download_and_render(
        best, aircraft_lat, aircraft_lon, callsign, station, zoom_deg
    )
    vel_time = name if vel_png else "not available"
    return refl_png, vel_png, name, vel_time


# ---------------------------------------------------------------------------
# Public API — loop
# ---------------------------------------------------------------------------
def fetch_and_render_radar_loop(
    start_time: datetime,
    duration_min: int,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
) -> tuple[list[tuple[bytes, str]], list[tuple[bytes, str]]]:
    """Fetch all Level II volumes in [start, start+duration], render each.

    Returns (refl_frames, vel_frames), each a list of
    (png_bytes, name) sorted chronologically.
    """
    start = start_time.replace(tzinfo=None)
    end = start + timedelta(minutes=duration_min)

    scans = _find_scans(station, start, end)
    if not scans:
        raise ValueError(
            f"No Level II volumes found for {_station4(station)} between "
            f"{start:%Y-%m-%d %H:%M} and {end:%H:%M UTC}."
        )

    scans.sort(key=lambda s: s.scan_time)

    refl_frames: list[tuple[bytes, str]] = []
    vel_frames: list[tuple[bytes, str]] = []
    for scan in scans:
        try:
            refl_png, vel_png, name = _download_and_render(
                scan, aircraft_lat, aircraft_lon, callsign, station, zoom_deg
            )
        except Exception:
            continue
        refl_frames.append((refl_png, name))
        if vel_png:
            vel_frames.append((vel_png, name))

    if not refl_frames:
        raise ValueError("All volumes in the window failed to decode/render.")

    return refl_frames, vel_frames


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _station4(station: str) -> str:
    """Level II uses 4-letter IDs (KDIX). Re-add K if the page stripped it."""
    s = station.strip().upper()
    return s if len(s) == 4 else "K" + s


def _find_scans(station: str, start: datetime, end: datetime):
    """List available Level II volumes for a station in a naive-UTC window."""
    import nexradaws

    conn = nexradaws.NexradAwsInterface()
    scans = conn.get_avail_scans_in_range(
        start.replace(tzinfo=None), end.replace(tzinfo=None), _station4(station)
    )
    # Skip MDM (metadata) files
    return [s for s in scans if "MDM" not in s.filename]


def _download_and_render(
    scan, aircraft_lat, aircraft_lon, callsign, station, zoom_deg
) -> tuple[bytes, bytes, str]:
    """Download one volume, render REF and VEL. Returns (ref_png, vel_png, name)."""
    import nexradaws
    from metpy.io import Level2File

    conn = nexradaws.NexradAwsInterface()
    tmpdir = tempfile.mkdtemp(prefix="nexrad_l2_")
    try:
        results = conn.download(scan, tmpdir)
        success = list(results.iter_success())
        if not success:
            raise ValueError(f"Download failed for {scan.filename}.")

        f = Level2File(success[0].filepath)

        # Radar location from the message-31 header of the first ray
        radar_lat = float(f.sweeps[0][0][1].lat)
        radar_lon = float(f.sweeps[0][0][1].lon)

        name = scan.filename

        # Reflectivity — lowest sweep containing REF
        az, rng_m, data = _extract_moment(f, b"REF")
        refl_png = _render_sweep(
            az, rng_m, data, radar_lat, radar_lon,
            aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
            product="REF", title_prefix="Base Reflectivity (0.5°)",
            cbar_label="Reflectivity (dBZ)", volume_name=name,
        )

        # Velocity — lowest sweep containing VEL (converted to knots)
        vel_png = b""
        try:
            az_v, rng_v, data_v = _extract_moment(f, b"VEL")
            data_v = data_v * _MS_TO_KT
            vel_png = _render_sweep(
                az_v, rng_v, data_v, radar_lat, radar_lon,
                aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
                product="VEL", title_prefix="Base Velocity (0.5°)",
                cbar_label="Velocity (kt)", volume_name=name,
            )
        except Exception:
            vel_png = b""

        return refl_png, vel_png, name
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _extract_moment(f, moment: bytes):
    """Pull (azimuths_deg, ranges_m, data) for the lowest sweep with moment."""
    for sweep in f.sweeps:
        if moment in sweep[0][4]:
            az = np.array([ray[0].az_angle for ray in sweep])
            hdr = sweep[0][4][moment][0]
            rng_m = np.arange(hdr.num_gates) * hdr.gate_width + hdr.first_gate
            data = np.array(
                [ray[4][moment][1] for ray in sweep], dtype=float
            )
            data = np.ma.masked_invalid(data)
            return az, rng_m, data
    raise ValueError(f"Moment {moment!r} not found in any sweep.")


def _render_sweep(
    az, rng_m, data, radar_lat, radar_lon,
    aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
    product, title_prefix, cbar_label, volume_name,
) -> bytes:
    """Render one sweep to PNG bytes with the shared BlueMet radar styling."""
    from metpy.calc import azimuth_range_to_lat_lon
    from metpy.plots import colortables
    from metpy.units import units
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    valid_frac = 1.0 - float(np.ma.getmaskarray(data).mean())

    lon_grid, lat_grid = azimuth_range_to_lat_lon(
        units.Quantity(az, "degrees"),
        units.Quantity(rng_m, "meters"),
        radar_lon,
        radar_lat,
    )

    if product == "REF":
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )
    else:
        # ±64 kt span using the NWS velocity table
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 0.5)

    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    ax.set_extent(
        [
            aircraft_lon - zoom_deg,
            aircraft_lon + zoom_deg,
            aircraft_lat - zoom_deg,
            aircraft_lat + zoom_deg,
        ],
        crs=ccrs.PlateCarree(),
    )

    mesh = ax.pcolormesh(
        lon_grid,
        lat_grid,
        data,
        cmap=cmap,
        norm=norm,
        shading="auto",
        transform=ccrs.PlateCarree(),
    )

    ax.coastlines(resolution="10m", color="black", linewidth=0.8)
    ax.add_feature(
        cfeature.BORDERS.with_scale("10m"),
        edgecolor="black",
        linewidth=0.6,
    )
    ax.add_feature(
        cfeature.STATES.with_scale("10m"),
        edgecolor="black",
        linewidth=0.5,
        facecolor="none",
    )

    gl = ax.gridlines(
        crs=ccrs.PlateCarree(),
        draw_labels=True,
        linewidth=0.6,
        color="gray",
        alpha=0.7,
        linestyle="--",
    )
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 9}
    gl.ylabel_style = {"size": 9}

    ax.scatter(
        aircraft_lon,
        aircraft_lat,
        s=180,
        marker="x",
        color="red",
        zorder=10,
        transform=ccrs.PlateCarree(),
    )
    ax.text(
        aircraft_lon + 0.05,
        aircraft_lat + 0.05,
        callsign,
        color="red",
        fontsize=12,
        zorder=10,
        transform=ccrs.PlateCarree(),
        weight="bold",
    )

    echo_note = "" if valid_frac > 0.01 else "  ·  NO ECHOES DETECTED (clear)"
    ax.set_title(
        f"{_station4(station)} {title_prefix}{echo_note}\n"
        f"Radar: {radar_lat:.2f}°, {radar_lon:.2f}°  ·  {volume_name}"
    )

    plt.colorbar(mesh, ax=ax, pad=0.02, label=cbar_label, shrink=0.8)

    # NOTE: no bbox_inches="tight" — it crops the GeoAxes away and
    # leaves only the colorbar on this matplotlib/cartopy combination.
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()