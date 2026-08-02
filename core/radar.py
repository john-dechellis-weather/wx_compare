"""NEXRAD Level III radar fetching and plot rendering.

Uses siphon to query UCAR's THREDDS server, metpy to decode NIDS,
and matplotlib+cartopy for rendering.

Fetches base reflectivity (N0B) and tries several velocity codes
(N0U, N0V, NBU, N0S) since availability varies by radar site.
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
    """Fetch NEXRAD reflectivity + velocity, render both.

    Returns (reflectivity_png, velocity_png_or_empty, refl_time, vel_time)
    Velocity may be b"" if no velocity product available.
    """
    from siphon.radarserver import RadarServer, get_radarserver_datasets

    base_server = "https://thredds.ucar.edu/thredds/"
    datasets = get_radarserver_datasets(base_server)
    radar_ref = datasets["NEXRAD Level III Radar from IDD"]
    rs = RadarServer(radar_ref.follow().catalog_url)

    # Reflectivity — required
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

    # Velocity — try multiple product codes, fall back gracefully
    vel_png = b""
    vel_time = "not available"
    last_error = None
    for vel_product in ["N0U", "N0V", "NBU", "N0S"]:
        try:
            vel_png, vel_time = _fetch_and_render_product(
                rs=rs,
                target_time=target_time,
                aircraft_lat=aircraft_lat,
                aircraft_lon=aircraft_lon,
                callsign=callsign,
                station=station,
                product=vel_product,
                zoom_deg=zoom_deg,
                title_prefix=f"Base Velocity ({vel_product})",
                cbar_label="Velocity (kt)",
            )
            print(f"[RADAR] Velocity succeeded with product {vel_product}")
            break
        except Exception as e:
            last_error = str(e)
            print(f"[RADAR] Velocity {vel_product} failed: {e}")
            continue

    if not vel_png:
        vel_time = f"Not available (tried N0U/N0V/NBU/N0S). Last error: {last_error}"

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
    """Fetch a single NEXRAD product and render its plot."""
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
    actual_time_str = dataset.name

    # Fetch NIDS file
    nids_url = dataset.access_urls["HTTPServer"]
    with urlopen(nids_url) as resp:
        raw = resp.read()

    f = Level3File(BytesIO(raw))

    # Decode data
    datadict = f.sym_block[0][0]
    radar_data = f.map_data(datadict["data"])

    print(f"[RADAR DEBUG] {product}: data shape {radar_data.shape}, "
          f"dtype {radar_data.dtype}, "
          f"has_mask {hasattr(radar_data, 'mask')}")

    az = units.Quantity(
        np.array(datadict["start_az"] + [datadict["end_az"][-1]]),
        "degrees",
    )
    rng = units.Quantity(
        np.linspace(0, f.max_range, radar_data.shape[-1] + 1),
        "kilometers",
    )

    lon_grid, lat_grid = azimuth_range_to_lat_lon(az, rng, f.lon, f.lat)

    # Colortable selection
    if product == "N0B":
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )
    elif product in ("N0U", "N0V", "NBU", "N0S"):
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 1.0)
    else:
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

    # Features
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

    # Zoom
    ax.set_extent(
        [
            aircraft_lon - zoom_deg,
            aircraft_lon + zoom_deg,
            aircraft_lat - zoom_deg,
            aircraft_lat + zoom_deg,
        ],
        crs=ccrs.PlateCarree(),
    )

    ax.set_title(
        f"K{station} {title_prefix}\n"
        f"Radar: {f.lat:.2f}°, {f.lon:.2f}°  ·  "
        f"Requested: {target_time:%Y-%m-%d %H:%M UTC}"
    )

    plt.colorbar(mesh, ax=ax, pad=0.02, label=cbar_label)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), actual_time_str