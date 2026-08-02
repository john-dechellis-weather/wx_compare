"""GOES satellite data fetching and plot rendering.

Uses goes2go for AWS-hosted archive access. Renders two products:
    - True Color (Bands 1/2/3 → RGB)
    - Clean IR Window (Band 13, color-mapped brightness temperature)

Both plots include:
    - GOES geostationary projection
    - Coastlines / borders / state lines overlays
    - Lat/lon gridlines
    - Aircraft position marker with callsign label
    - ~350 km zoom around the aircraft
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


# Zoom padding around aircraft position, in meters (matches the notebook)
_ZOOM_PAD_METERS = 350_000


def fetch_goes_data(
    target_time: datetime,
    satellite: str,
    cache_dir: Path,
) -> tuple["xr.Dataset", datetime]:
    """Fetch nearest available GOES ABI-L2-MCMIP scan.

    Args:
        target_time: UTC datetime of the desired scan.
        satellite: 'goes19' (East) or 'goes18' (West).
        cache_dir: Directory to cache downloaded NetCDF files.

    Returns:
        (dataset, actual_scan_time)
    """
    import os
    # goes2go looks at GOES2GO_SAVE env var for its download location
    os.environ["GOES2GO_SAVE"] = str(cache_dir)

    from goes2go.data import goes_nearesttime
    import pandas as pd

    # Convert to naive pandas Timestamp — goes2go's internal date math
    # can trip over tz-aware datetimes with certain pandas versions
    target_pd = pd.Timestamp(target_time).tz_localize(None)

    ds = goes_nearesttime(
        attime=target_pd,
        satellite=satellite,
        product="ABI-L2-MCMIP",
        domain="C",
        return_as="xarray",
        download=True,
        overwrite=False,
        verbose=False,
    )

    # Actual scan time from the dataset
    scan_time = _extract_scan_time(ds, fallback=target_time)
    return ds, scan_time


def _extract_scan_time(ds, fallback: datetime) -> datetime:
    """Try to pull the actual mid-scan time from a GOES dataset."""
    import pandas as pd
    for attr_name in ("time_coverage_start", "time_coverage_end", "date_created"):
        v = ds.attrs.get(attr_name)
        if v:
            try:
                # Handle both string timestamps and numpy datetime64
                ts = pd.Timestamp(str(v)).tz_localize(None)
                return ts.to_pydatetime()
            except Exception:
                pass
    # Fallback: try the dataset's time coordinate
    try:
        if "t" in ds.coords:
            t_val = ds["t"].values
            ts = pd.Timestamp(str(t_val)).tz_localize(None)
            return ts.to_pydatetime()
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_true_color(
    ds,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
) -> bytes:
    """Render True Color plot with aircraft marker. Returns PNG bytes."""
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    # Extract and process RGB bands
    R = np.clip(ds["CMI_C02"].values, 0, 1)
    G = np.clip(ds["CMI_C03"].values, 0, 1)
    B = np.clip(ds["CMI_C01"].values, 0, 1)

    # Approximate true green
    G_true = np.clip(0.45 * R + 0.10 * G + 0.45 * B, 0, 1)

    # Gamma correction
    gamma = 2.2
    R = np.power(R, 1 / gamma)
    G_true = np.power(G_true, 1 / gamma)
    B = np.power(B, 1 / gamma)

    RGB = np.dstack([R, G_true, B])
    RGB = np.clip((RGB - 0.02) / 0.9, 0, 1)
    RGB = np.clip(RGB * 1.15, 0, 1)

    return _render_plot(
        ds=ds,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
        image_data=RGB,
        cmap=None,
        vmin=None,
        vmax=None,
        coastline_color="white",
        border_color="white",
        state_color="yellow",
        title_text="GOES True Color with Aircraft Position",
        add_colorbar=False,
        cbar_label=None,
    )


def render_infrared(
    ds,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
) -> bytes:
    """Render Clean IR (Band 13) plot with aircraft marker. Returns PNG bytes."""
    ir = np.ma.masked_invalid(ds["CMI_C13"].values)

    return _render_plot(
        ds=ds,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        callsign=callsign,
        image_data=ir,
        cmap="nipy_spectral_r",
        vmin=180,
        vmax=330,
        coastline_color="cyan",
        border_color="cyan",
        state_color="yellow",
        title_text="GOES Clean IR Window (Band 13) with Aircraft Position",
        add_colorbar=True,
        cbar_label="Brightness Temperature (K)",
    )


def _render_plot(
    ds,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    image_data,
    cmap: Optional[str],
    vmin: Optional[float],
    vmax: Optional[float],
    coastline_color: str,
    border_color: str,
    state_color: str,
    title_text: str,
    add_colorbar: bool,
    cbar_label: Optional[str],
) -> bytes:
    """Shared rendering function for both True Color and IR plots.

    Returns PNG bytes ready to display or download.
    """
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import pyproj

    # Force matplotlib to use a non-interactive backend
    plt.switch_backend("Agg")

    # GOES projection info from the dataset
    proj_var = ds["goes_imager_projection"]
    sat_h = float(proj_var.perspective_point_height)
    lon_0 = float(proj_var.longitude_of_projection_origin)
    sweep = str(proj_var.sweep_angle_axis)

    x = ds["x"].values * sat_h
    y = ds["y"].values * sat_h

    # Cartopy CRS for the geostationary view
    geos_crs = ccrs.Geostationary(
        central_longitude=lon_0,
        satellite_height=sat_h,
        sweep_axis=sweep,
    )

    # Transform aircraft lat/lon → GOES x/y meters
    geos_proj = pyproj.Proj(proj="geos", h=sat_h, lon_0=lon_0, sweep=sweep)
    wgs84 = pyproj.Proj(proj="latlong", datum="WGS84")
    transformer = pyproj.Transformer.from_proj(wgs84, geos_proj, always_xy=True)
    aircraft_x, aircraft_y = transformer.transform(aircraft_lon, aircraft_lat)

    # Set up figure
    fig = plt.figure(figsize=(12, 10))
    ax = plt.axes(projection=geos_crs)

    # Plot the imagery
    img = ax.imshow(
        image_data,
        origin="upper",
        extent=[x.min(), x.max(), y.min(), y.max()],
        transform=geos_crs,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        interpolation="nearest",
    )

    # Colorbar (only for IR)
    if add_colorbar and cbar_label is not None:
        cbar = plt.colorbar(img, ax=ax, shrink=0.7, pad=0.02)
        cbar.set_label(cbar_label)

    # Geographic overlays
    ax.coastlines(resolution="10m", color=coastline_color, linewidth=0.8)
    ax.add_feature(
        cfeature.BORDERS.with_scale("10m"),
        edgecolor=border_color,
        linewidth=0.5,
    )
    ax.add_feature(
        cfeature.STATES.with_scale("10m"),
        edgecolor=state_color,
        linewidth=0.5,
        facecolor="none",
    )

    # Lat/lon gridlines
    gl = ax.gridlines(
        crs=ccrs.PlateCarree(),
        draw_labels=True,
        linewidth=0.6,
        color="white",
        alpha=0.7,
        linestyle="--",
    )
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 9, "color": "white"}
    gl.ylabel_style = {"size": 9, "color": "white"}

    # Aircraft marker
    ax.scatter(
        aircraft_x,
        aircraft_y,
        s=100,
        marker="x",
        color="red",
        zorder=10,
    )

    # Callsign label offset slightly from the marker
    ax.text(
        aircraft_x + 20_000,
        aircraft_y + 20_000,
        callsign,
        color="red",
        fontsize=12,
        zorder=10,
        weight="bold",
    )

    # Zoom to region around the aircraft
    ax.set_xlim(aircraft_x - _ZOOM_PAD_METERS, aircraft_x + _ZOOM_PAD_METERS)
    ax.set_ylim(aircraft_y - _ZOOM_PAD_METERS, aircraft_y + _ZOOM_PAD_METERS)

    ax.set_title(title_text)

    # Render to PNG bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
