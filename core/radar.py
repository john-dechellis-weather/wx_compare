"""NEXRAD Level III radar fetching and plot rendering."""
from __future__ import annotations

import io
from datetime import datetime

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

    Returns (reflectivity_png, velocity_png_or_empty, refl_time, vel_time).
    """
    from siphon.radarserver import RadarServer, get_radarserver_datasets

    print(f"[RADAR] fetch_and_render_radar for station={station}")

    base_server = "https://thredds.ucar.edu/thredds/"
    datasets = get_radarserver_datasets(base_server)
    radar_ref = datasets["NEXRAD Level III Radar from IDD"]
    rs = RadarServer(radar_ref.follow().catalog_url)

    # Reflectivity
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

    # Velocity — try multiple product codes
    vel_png = b""
    vel_time = "not available"
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
            print(f"[RADAR] velocity succeeded with {vel_product}")
            break
        except Exception as e:
            print(f"[RADAR] velocity {vel_product} failed: {e}")
            continue

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
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

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
    print(f"[RADAR DEBUG] {product}: radar lat/lon = {f.lat}, {f.lon}")
    print(f"[RADAR DEBUG] {product}: max_range = {f.max_range}")

    # Decode data
    datadict = f.sym_block[0][0]
    radar_data = f.map_data(datadict["data"])
    valid_frac = 1.0 - float(np.ma.getmaskarray(radar_data).mean())
    print(f"[RADAR DEBUG] {product}: valid (unmasked) fraction = {valid_frac:.3f}")
    print(f"[RADAR DEBUG] {product}: data shape = {radar_data.shape}, "
          f"dtype = {radar_data.dtype}")

    az = units.Quantity(
        np.array(datadict["start_az"] + [datadict["end_az"][-1]]),
        "degrees",
    )
    rng = units.Quantity(
        np.linspace(0, f.max_range, radar_data.shape[-1] + 1),
        "kilometers",
    )

    lon_grid, lat_grid = azimuth_range_to_lat_lon(az, rng, f.lon, f.lat)
    print(f"[RADAR DEBUG] {product}: lon_grid shape = {lon_grid.shape}, "
          f"lat_grid shape = {lat_grid.shape}")
    print(f"[RADAR DEBUG] {product}: lon range = {lon_grid.min():.2f} to {lon_grid.max():.2f}")
    print(f"[RADAR DEBUG] {product}: lat range = {lat_grid.min():.2f} to {lat_grid.max():.2f}")

    # Colortable
    if product == "N0B":
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )
    else:
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 1.0)

    # Build figure — extent set FIRST, then plot
    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    # Set extent BEFORE plotting
    ax.set_extent(
        [
            aircraft_lon - zoom_deg,
            aircraft_lon + zoom_deg,
            aircraft_lat - zoom_deg,
            aircraft_lat + zoom_deg,
        ],
        crs=ccrs.PlateCarree(),
    )

    # Plot the radar data — no explicit transform needed since ax is already PlateCarree
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
    
    echo_note = "" if valid_frac > 0.01 else "  ·  NO ECHOES DETECTED (clear)"
    ax.set_title(
        f"K{station} {title_prefix}\n"
        f"Radar: {f.lat:.2f}°, {f.lon:.2f}°  ·  "
        f"Requested: {target_time:%Y-%m-%d %H:%M UTC}"
    )

    plt.colorbar(mesh, ax=ax, pad=0.02, label=cbar_label, shrink=0.8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), actual_time_str