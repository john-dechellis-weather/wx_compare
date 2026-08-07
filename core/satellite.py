"""GOES satellite data fetching and plot rendering."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


_ZOOM_PAD_METERS = 350_000


def fetch_goes_data(
    target_time: datetime,
    satellite: str,
    cache_dir: Path,
) -> tuple["xr.Dataset", datetime]:
    """Fetch nearest available GOES ABI-L2-MCMIP scan."""
    import os
    os.environ["GOES2GO_SAVE"] = str(cache_dir)

    from goes2go.data import goes_nearesttime
    import pandas as pd

    # Convert to naive pandas Timestamp
    target_pd = pd.Timestamp(target_time).tz_localize(None)

    ds = goes_nearesttime(
        attime=target_pd,
        satellite=satellite,
        product="ABI-L2-MCMIP",
        domain="C",  # CONUS
        return_as="xarray",
        download=True,
        overwrite=False,
        verbose=False,
    )

    scan_time = _extract_scan_time(ds, fallback=target_time)
    return ds, scan_time


def _extract_scan_time(ds, fallback: datetime) -> datetime:
    """Try to pull the actual mid-scan time from a GOES dataset."""
    import pandas as pd
    for attr_name in ("time_coverage_start", "time_coverage_end", "date_created"):
        v = ds.attrs.get(attr_name)
        if v:
            try:
                ts = pd.Timestamp(str(v)).tz_localize(None)
                return ts.to_pydatetime()
            except Exception:
                pass
    try:
        if "t" in ds.coords:
            t_val = ds["t"].values
            ts = pd.Timestamp(str(t_val)).tz_localize(None)
            return ts.to_pydatetime()
    except Exception:
        pass
    return fallback


def render_true_color(
    ds,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
) -> bytes:
    """Render True Color plot with aircraft marker. Returns PNG bytes."""
    R = np.clip(ds["CMI_C02"].values, 0, 1)
    G = np.clip(ds["CMI_C03"].values, 0, 1)
    B = np.clip(ds["CMI_C01"].values, 0, 1)

    G_true = np.clip(0.45 * R + 0.10 * G + 0.45 * B, 0, 1)

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
    lightning=None,
    trail=None,
) -> bytes:
    """Render Clean IR (Band 13) plot with aircraft marker. Returns PNG bytes."""
    ir_raw = ds["CMI_C13"].values
    print(f"[IR DEBUG] CMI_C13 shape: {ir_raw.shape}")
    print(f"[IR DEBUG] CMI_C13 dtype: {ir_raw.dtype}")
    print(f"[IR DEBUG] CMI_C13 valid count: {np.sum(~np.isnan(ir_raw))}")
    print(f"[IR DEBUG] CMI_C13 nan count: {np.sum(np.isnan(ir_raw))}")
    try:
        print(f"[IR DEBUG] CMI_C13 min: {np.nanmin(ir_raw)}, max: {np.nanmax(ir_raw)}")
    except Exception as e:
        print(f"[IR DEBUG] Could not compute min/max: {e}")
    print(f"[IR DEBUG] ds x shape: {ds['x'].values.shape}")
    print(f"[IR DEBUG] ds y shape: {ds['y'].values.shape}")
    r_raw = ds["CMI_C02"].values
    print(f"[IR DEBUG] CMI_C02 (Red) shape: {r_raw.shape}")

    ir = ir_raw

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
        lightning=lightning,
        trail=trail,
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
    lightning=None,
    trail=None,
) -> bytes:
    """Shared rendering function for both True Color and IR plots."""
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import pyproj

    plt.switch_backend("Agg")

    proj_var = ds["goes_imager_projection"]
    sat_h = float(proj_var.perspective_point_height)
    lon_0 = float(proj_var.longitude_of_projection_origin)
    sweep = str(proj_var.sweep_angle_axis)

    x = ds["x"].values * sat_h
    y = ds["y"].values * sat_h

    # Adjust coordinates if image has different resolution than default x/y
    if image_data.ndim == 2:
        img_shape = image_data.shape
    else:
        img_shape = image_data.shape[:2]

    if img_shape != (len(y), len(x)):
        print(f"[RENDER DEBUG] Regenerating coords: img_shape={img_shape}, y_len={len(y)}, x_len={len(x)}")
        x = np.linspace(x.min(), x.max(), img_shape[1])
        y = np.linspace(y.min(), y.max(), img_shape[0])
    else:
        print(f"[RENDER DEBUG] Coords match: img_shape={img_shape}")

    geos_crs = ccrs.Geostationary(
        central_longitude=lon_0,
        satellite_height=sat_h,
        sweep_axis=sweep,
    )

    geos_proj = pyproj.Proj(proj="geos", h=sat_h, lon_0=lon_0, sweep=sweep)
    wgs84 = pyproj.Proj(proj="latlong", datum="WGS84")
    transformer = pyproj.Transformer.from_proj(wgs84, geos_proj, always_xy=True)
    aircraft_x, aircraft_y = transformer.transform(aircraft_lon, aircraft_lat)

    if not (np.isfinite(aircraft_x) and np.isfinite(aircraft_y)):
        raise ValueError(
            f"Aircraft position ({aircraft_lat}, {aircraft_lon}) is outside "
            f"the satellite's projection range."
        )

    fig = plt.figure(figsize=(12, 10))
    ax = plt.axes(projection=geos_crs)

    # Different rendering for RGB vs 2D data
    if image_data.ndim == 3:
        img = ax.imshow(
            image_data,
            origin="upper",
            extent=[x.min(), x.max(), y.min(), y.max()],
            transform=geos_crs,
            interpolation="nearest",
        )
    else:
        img_data = np.array(image_data, dtype=float)
        if vmin is not None:
            img_data = np.where(np.isnan(img_data), vmin, img_data)
        img = ax.pcolormesh(
            x, y[::-1], img_data[::-1, :],
            transform=geos_crs,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            shading="auto",
        )

    if add_colorbar and cbar_label is not None:
        cbar = plt.colorbar(img, ax=ax, shrink=0.7, pad=0.02)
        cbar.set_label(cbar_label)

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

    # Flight path trail (dashed red, under markers)
    if trail and len(trail) >= 2:
        ax.plot(
            [p[1] for p in trail], [p[0] for p in trail],
            color="red", linewidth=1.4, linestyle="--", alpha=0.85,
            zorder=8, transform=ccrs.PlateCarree(),
        )

    # GLM lightning flashes: gold crosses under the aircraft marker
    if lightning:
        _lons = [p[1] for p in lightning]
        _lats = [p[0] for p in lightning]
        ax.scatter(
            _lons, _lats, s=28, marker="+", color="#FFD700",
            linewidths=1.4, zorder=9, transform=ccrs.PlateCarree(),
        )

    ax.scatter(
        aircraft_x,
        aircraft_y,
        s=100,
        marker="x",
        color="red",
        zorder=10,
    )
    ax.text(
        aircraft_x + 20_000,
        aircraft_y + 20_000,
        callsign,
        color="red",
        fontsize=12,
        zorder=10,
        weight="bold",
    )

    ax.set_xlim(aircraft_x - _ZOOM_PAD_METERS, aircraft_x + _ZOOM_PAD_METERS)
    ax.set_ylim(aircraft_y - _ZOOM_PAD_METERS, aircraft_y + _ZOOM_PAD_METERS)
    ax.set_title(title_text)

    buf = io.BytesIO()
    # NOTE: no bbox_inches="tight" — on this matplotlib/cartopy combo it
    # crops the GeoAxes away entirely, leaving only the colorbar (the
    # exact bug this comment is here to prevent reintroducing).
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
