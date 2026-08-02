"""NEXRAD Level III radar fetching and plot rendering.

Uses siphon to query UCAR's THREDDS server, metpy to decode NIDS,
and matplotlib+cartopy for rendering.

Single-frame mode: fetch nearest N0B (reflectivity) + best-available
velocity product (N0U/N0V/NBU/N0S).

Loop mode: fetch ALL frames in a time window for both products,
returned as lists of (png_bytes, dataset_name) sorted by time.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import numpy as np

_VELOCITY_PRODUCTS = ["N0U", "N0V", "NBU", "N0S"]


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
    """Fetch NEXRAD reflectivity + velocity for one time, render both.

    Returns (reflectivity_png, velocity_png_or_empty, refl_time, vel_time).
    """
    rs = _get_radar_server()

    refl_png, refl_time = _fetch_single(
        rs, target_time, aircraft_lat, aircraft_lon, callsign, station,
        "N0B", zoom_deg, "Base Reflectivity", "Reflectivity (dBZ)",
    )

    vel_png = b""
    vel_time = "not available"
    for vp in _VELOCITY_PRODUCTS:
        try:
            vel_png, vel_time = _fetch_single(
                rs, target_time, aircraft_lat, aircraft_lon, callsign, station,
                vp, zoom_deg, f"Base Velocity ({vp})", "Velocity (kt)",
            )
            break
        except Exception:
            continue

    return refl_png, vel_png, refl_time, vel_time


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
    """Fetch all frames from start_time to start_time + duration_min.

    Returns (refl_frames, vel_frames), each a list of
    (png_bytes, dataset_name) sorted chronologically. vel_frames may be
    an empty list if no velocity product is available.
    """
    rs = _get_radar_server()

    # siphon prefers naive UTC datetimes
    start = start_time.replace(tzinfo=None)
    end = start + timedelta(minutes=duration_min)

    refl_frames = _fetch_range(
        rs, start, end, aircraft_lat, aircraft_lon, callsign, station,
        "N0B", zoom_deg, "Base Reflectivity", "Reflectivity (dBZ)",
    )
    if not refl_frames:
        raise ValueError(
            f"No N0B frames found for {station} between "
            f"{start:%Y-%m-%d %H:%M} and {end:%H:%M UTC}."
        )

    vel_frames: list[tuple[bytes, str]] = []
    for vp in _VELOCITY_PRODUCTS:
        try:
            vel_frames = _fetch_range(
                rs, start, end, aircraft_lat, aircraft_lon, callsign, station,
                vp, zoom_deg, f"Base Velocity ({vp})", "Velocity (kt)",
            )
        except Exception:
            vel_frames = []
        if vel_frames:
            break

    return refl_frames, vel_frames


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _get_radar_server():
    from siphon.radarserver import RadarServer, get_radarserver_datasets

    base_server = "https://thredds.ucar.edu/thredds/"
    datasets = get_radarserver_datasets(base_server)
    radar_ref = datasets["NEXRAD Level III Radar from IDD"]
    return RadarServer(radar_ref.follow().catalog_url)


def _fetch_single(
    rs, target_time, aircraft_lat, aircraft_lon, callsign, station,
    product, zoom_deg, title_prefix, cbar_label,
) -> tuple[bytes, str]:
    """Fetch one product at one time. Raises on missing dataset."""
    query = rs.query()
    query.stations(station).time(target_time).variables(product)
    catalog = rs.get_catalog(query)
    matches = list(catalog.datasets.values())

    if not matches:
        raise ValueError(
            f"No {product} dataset found for {station} at "
            f"{target_time:%Y-%m-%d %H:%M UTC}."
        )

    dataset = matches[0]
    return _render_dataset(
        dataset, aircraft_lat, aircraft_lon, callsign, station,
        product, zoom_deg, title_prefix, cbar_label, target_time,
    )


def _fetch_range(
    rs, start, end, aircraft_lat, aircraft_lon, callsign, station,
    product, zoom_deg, title_prefix, cbar_label,
) -> list[tuple[bytes, str]]:
    """Fetch all scans of a product in [start, end]. Returns sorted frames."""
    query = rs.query()
    query.stations(station).time_range(start, end).variables(product)
    catalog = rs.get_catalog(query)

    frames: list[tuple[bytes, str]] = []
    # Dataset names contain timestamps -> lexicographic sort is chronological
    for name in sorted(catalog.datasets):
        dataset = catalog.datasets[name]
        try:
            png, tstr = _render_dataset(
                dataset, aircraft_lat, aircraft_lon, callsign, station,
                product, zoom_deg, title_prefix, cbar_label, start,
            )
            frames.append((png, tstr))
        except Exception:
            continue
    return frames


def _render_dataset(
    dataset, aircraft_lat, aircraft_lon, callsign, station,
    product, zoom_deg, title_prefix, cbar_label, target_time,
) -> tuple[bytes, str]:
    """Download a NIDS dataset and render it to PNG bytes."""
    from io import BytesIO
    from urllib.request import urlopen
    from metpy.io import Level3File
    from metpy.calc import azimuth_range_to_lat_lon
    from metpy.plots import colortables
    from metpy.units import units
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    actual_time_str = dataset.name

    nids_url = dataset.access_urls["HTTPServer"]
    with urlopen(nids_url) as resp:
        raw = resp.read()

    f = Level3File(BytesIO(raw))

    datadict = f.sym_block[0][0]
    radar_data = f.map_data(datadict["data"])
    valid_frac = 1.0 - float(np.ma.getmaskarray(radar_data).mean())

    az = units.Quantity(
        np.array(datadict["start_az"] + [datadict["end_az"][-1]]),
        "degrees",
    )
    rng = units.Quantity(
        np.linspace(0, f.max_range, radar_data.shape[-1] + 1),
        "kilometers",
    )

    lon_grid, lat_grid = azimuth_range_to_lat_lon(az, rng, f.lon, f.lat)

    if product == "N0B":
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )
    else:
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 1.0)

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
        radar_data,
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
        f"K{station} {title_prefix}{echo_note}\n"
        f"Radar: {f.lat:.2f}°, {f.lon:.2f}°  ·  {dataset.name}"
    )

    plt.colorbar(mesh, ax=ax, pad=0.02, label=cbar_label, shrink=0.8)

    # NOTE: no bbox_inches="tight" — it crops the GeoAxes away and
    # leaves only the colorbar on this matplotlib/cartopy combination.
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), actual_time_str