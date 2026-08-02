"""NEXRAD Level III radar fetching and plot rendering.

Uses siphon to query UCAR's THREDDS server for archived radar data,
metpy to decode the NIDS binary format, and matplotlib+cartopy for
rendering.

Two products fetched per request:
    - N0B: Base Reflectivity (tilt 1, 0.5° elevation) — dBZ
    - N0U: Base Velocity (tilt 1, 0.5° elevation) — knots
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

import numpy as np


def fetch_and_render_radar(
    target_time: datetime,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
) -> tuple[bytes, bytes, str, str]:
    """Fetch NEXRAD N0B (reflectivity) and N0U (velocity), render both.

    Args:
        target_time: UTC datetime of the desired scan.
        aircraft_lat, aircraft_lon: Aircraft position in decimal degrees.
        callsign: Label to show next to aircraft marker.
        station: 3-letter NEXRAD site code (e.g., 'DIX', 'FTG').
        zoom_deg: Half-width of view in degrees.

    Returns:
        (reflectivity_png_bytes, velocity_png_bytes,
         reflectivity_actual_time, velocity_actual_time)
    """
    from siphon.radarserver import RadarServer, get_radarserver_datasets

    base_server = "https://thredds.ucar.edu/thredds/"
    datasets = get_radarserver_datasets(base_server)
    radar_ref = datasets["NEXRAD Level III Radar from IDD"]
    rs = RadarServer(radar_ref.follow().catalog_url)

    # Fetch and render each product
    refl_png, refl_time = _fetch_and_render_product(
        rs=rs,
        target_time=target_time,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
        station=station,
        product="N0B",
        zoom_deg=zoom_deg,
        title_prefix="Base Reflectivity",
        cbar_label="Reflectivity (dBZ)",
    )

    vel_png, vel_time = _fetch_and_render_product(
        rs=rs,
        target_time=target_time,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
        station=station,
        product="N0U",
        zoom_deg=zoom_deg,
        title_prefix="Base Velocity",
        cbar_label="Velocity (kt)",
    )

    return refl_png, vel_png, refl_time, vel_time


def _fetch_and_render_product(
    rs,
    target_time: datetime,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    product: str,
    zoom_deg: float,
    title_prefix: str,
    cbar_label: str,
) -> tuple[bytes, str]:
    """Fetch a single NEXRAD product and render its plot. Returns PNG bytes."""
    from io import BytesIO
    from urllib.request import urlopen
    from metpy.io import Level3File
    from metpy.calc import azimuth_range_to_lat_lon
    from metpy.plots import colortables
    from metpy.units import units
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    plt.switch_backend("Agg")

    # Query THREDDS
    query = rs.query()
    query.stations(station).time(target_time).variables(product)
    catalog = rs.get_catalog(query)
    matches = list(catalog.datasets.values())

    if not matches:
        raise ValueError(
            f"No {product} dataset found for {station} at {target_time:%Y-%m-%d %H:%M UTC}."
        )

    dataset = matches[0]
    actual_time_str = dataset.name  # Contains time info

    # Fetch NIDS file
    nids_url = dataset.access_urls["HTTPServer"]
    with urlopen(nids_url) as resp:
        raw = resp.read()

    f = Level3File(BytesIO(raw))

    # Decode radar data
    datadict = f.sym_block[0][0]
    radar_data = f.map_data(datadict["data"])

    az = units.Quantity(
        np.array(datadict["start_az"] + [datadict["end_az"][-1]]),
        "degrees",
    )
    rng = units.Quantity(
        np.linspace(0, f.max_range, radar_data.shape[-1] + 1),
        "kilometers",
    )

    lon_grid, lat_grid = azimuth_range_to_lat_lon(az, rng, f.lon, f.lat)

    # Select colortable per product
    if product == "N0B":
        # Reflectivity: NWS storm-clear reflectivity table (-20 to 75 dBZ)
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )
    elif product == "N0U":
        # Velocity: NWS8bitVel table (-64 to +64 kt)
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 1.0)
    else:
        # Fallback default
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )

    # Build figure
    fig = plt.figure(figsize=(12, 10))
    ax = plt.axes(projection=ccrs.PlateCarree())

    mesh = ax.pcolormesh(
        lon_grid,
        lat_grid,
        radar_data,
        cmap=cmap,
        norm=norm,
        shading="auto",
        transform=ccrs.PlateCarree(),
    )

    # Geographic features
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

    # Gridlines
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

    # Aircraft marker
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

    # Zoom around aircraft
    ax.set_extent(
        [
            aircraft_lon - zoom_deg,
            aircraft_lon + zoom_deg,
            aircraft_lat - zoom_deg,
            aircraft_lat + zoom_deg,
        ],
        crs=ccrs.PlateCarree(),
    )

    # Title
    ax.set_title(
        f"K{station} {title_prefix}\n"
        f"Radar: {f.lat:.2f}°, {f.lon:.2f}°  ·  "
        f"Requested: {target_time:%Y-%m-%d %H:%M UTC}"
    )

    # Colorbar
    plt.colorbar(mesh, ax=ax, pad=0.02, label=cbar_label)

    # Render to PNG bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), actual_time_str