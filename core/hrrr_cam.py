"""HRRR convection-allowing model fields for the Hi-Res CAMs page.

Data path: NOMADS grib_filter CGI — requests a single field over a
subregion, returning a small (~0.5-3 MB) GRIB2 that cfgrib decodes.
NOMADS reachability from the app server is proven (NBM text bulletins
come from the same host).
"""HRRR convection-allowing model fields for the Hi-Res CAMs page.

Data path: NOMADS grib_filter CGI — requests a single field over a
subregion, returning a small (~0.5-3 MB) GRIB2 that cfgrib decodes.
NOMADS reachability from the app server is proven (NBM text bulletins
come from the same host).

Aviation product set (HRRR 2D surface file, wrfsfcf):
  REFC    composite reflectivity (dBZ)
  RETOP   echo tops (m -> rendered kft)
  VIS     surface visibility (m -> statute miles)
  CEIL    cloud ceiling height (m -> hundreds of feet)
  GUST    10 m wind gust (m/s -> kt)
"""
from __future__ import annotations

import io
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import requests

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}

FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
IDX_URL = (
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/"
    "hrrr.{ymd}/conus/hrrr.t{cc}z.wrfsfcf{ff}.grib2.idx"
)

# product -> (filter var params, filter lev params)
PRODUCTS = {
    "REFC": ({"var_REFC": "on"}, {"lev_entire_atmosphere": "on"}),
    "RETOP": ({"var_RETOP": "on"}, {"lev_cloud_top": "on",
                                    "lev_entire_atmosphere": "on"}),
    "VIS": ({"var_VIS": "on"}, {"lev_surface": "on"}),
    "CEIL": ({"var_HGT": "on"}, {"lev_cloud_ceiling": "on"}),
    "GUST": ({"var_GUST": "on"}, {"lev_surface": "on"}),
}

PRODUCT_LABELS = {
    "REFC": "Composite Reflectivity (dBZ)",
    "RETOP": "Echo Tops (kft)",
    "VIS": "Visibility (SM)",
    "CEIL": "Ceiling (hundreds ft)",
    "GUST": "10 m Wind Gust (kt)",
}


def latest_cycle(fhr: int, now: Optional[datetime] = None) -> Optional[datetime]:
    """Newest HRRR cycle whose requested forecast hour exists on NOMADS.
    Walks back up to 6 hours probing the .idx sidecar (tiny)."""
    now = now or datetime.now(timezone.utc)
    for back in range(1, 7):
        cyc = (now - timedelta(hours=back)).replace(
            minute=0, second=0, microsecond=0
        )
        # F19-F48 only exist for 00/06/12/18Z cycles
        if fhr > 18 and cyc.hour % 6 != 0:
            continue
        url = IDX_URL.format(ymd=cyc.strftime("%Y%m%d"),
                             cc=f"{cyc.hour:02d}", ff=f"{fhr:02d}")
        try:
            r = requests.head(url, headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                return cyc
        except Exception:
            continue
    return None


def fetch_field(
    product: str,
    cycle: datetime,
    fhr: int,
    lat: float,
    lon: float,
    zoom_deg: float,
) -> bytes:
    """Small subregion GRIB2 for one field via the NOMADS filter."""
    var_p, lev_p = PRODUCTS[product]
    pad = zoom_deg + 0.4  # margin so pcolormesh fills the frame
    left = (lon - pad) % 360
    right = (lon + pad) % 360
    params = {
        "file": f"hrrr.t{cycle.hour:02d}z.wrfsfcf{fhr:02d}.grib2",
        "dir": f"/hrrr.{cycle:%Y%m%d}/conus",
        "subregion": "",
        "leftlon": f"{left:.2f}",
        "rightlon": f"{right:.2f}",
        "toplat": f"{lat + pad:.2f}",
        "bottomlat": f"{lat - pad:.2f}",
        **var_p,
        **lev_p,
    }
    r = requests.get(FILTER_URL, params=params, headers=_HEADERS, timeout=90)
    r.raise_for_status()
    if len(r.content) < 500 or r.content[:4] != b"GRIB":
        raise RuntimeError(
            f"NOMADS filter returned non-GRIB ({len(r.content)} bytes) "
            f"for {product} f{fhr:02d}"
        )
    return r.content


def decode_field(raw: bytes):
    """(values_2d, lat_2d, lon_2d) from a single-message GRIB2."""
    import xarray as xr

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(raw)
        path = tf.name
    ds = xr.open_dataset(path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    var = list(ds.data_vars)[0]
    vals = np.asarray(ds[var].values, dtype=float)
    lats = np.asarray(ds["latitude"].values, dtype=float)
    lons = np.asarray(ds["longitude"].values, dtype=float)
    lons = np.where(lons > 180, lons - 360, lons)
    ds.close()
    return vals, lats, lons


def render_field(
    product: str,
    vals: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    center_lat: float,
    center_lon: float,
    zoom_deg: float,
    title: str,
    aircraft=None,          # list[AircraftPos] JBU overlay
    routes=None,            # {callsign: {"label", "orig", "dest"}}
) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    data = np.ma.masked_invalid(vals)

    if product == "REFC":
        from metpy.plots import colortables
        norm, cmap = colortables.get_with_steps(
            "NWSReflectivity", 5, 5)
        # Standard CAM convention: mask < 5 dBZ so clear air stays clean
        data = np.ma.masked_less(data, 5)
    elif product == "RETOP":
        data = np.ma.masked_less(data, 0) / 304.8  # m -> kft
        bounds = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70]
        colors = ["#C8C8C8", "#9BD4F5", "#4FA8E8", "#2E6FDB", "#22B14C",
                  "#7CD934", "#FFF200", "#FFC90E", "#FF7F27", "#ED1C24",
                  "#B21E28", "#A349A4", "#6F2DA8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "VIS":
        data = data / 1609.34  # m -> SM
        bounds = [0, 0.5, 1, 2, 3, 5, 7, 10]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "CEIL":
        data = data * 3.28084 / 100.0  # m -> hundreds of ft
        data = np.ma.masked_greater(data, 300)  # >30kft ~ unlimited
        bounds = [0, 2, 4, 10, 20, 30, 50, 100, 300]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#A8D8A8", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    else:  # GUST
        data = data * 1.94384  # m/s -> kt
        bounds = [0, 10, 15, 20, 25, 30, 35, 40, 50, 65]
        colors = ["#E8E8E8", "#B0E0FF", "#60B0E0", "#FFFF00", "#FFC90E",
                  "#FF9900", "#FF4040", "#B21E28", "#A349A4"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)

    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
    ax.set_extent(
        [center_lon - zoom_deg, center_lon + zoom_deg,
         center_lat - zoom_deg, center_lat + zoom_deg],
        crs=ccrs.PlateCarree(),
    )
    mesh = ax.pcolormesh(
        lons, lats, data, cmap=cmap, norm=norm, shading="auto",
        transform=ccrs.PlateCarree(), zorder=2,
    )
    try:
        coast = cfeature.COASTLINE.with_scale("10m")
        states = cfeature.STATES.with_scale("10m")
        next(iter(coast.geometries()))
        ax.add_feature(coast, linewidth=0.8, zorder=3)
        ax.add_feature(states, linewidth=0.5, zorder=3)
    except Exception:
        pass
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, linestyle=":",
                      color="gray")
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 8}
    gl.ylabel_style = {"size": 8}

    routes = routes or {}
    for ac in aircraft or []:
        rt = routes.get(ac.callsign)
        if rt:
            # Great-circle route line origin -> destination (Geodetic
            # transform draws the true arc through the current position)
            (olat, olon), (dlat, dlon) = rt["orig"], rt["dest"]
            ax.plot(
                [olon, dlon], [olat, dlat],
                color="#0000CC", linewidth=1.0, linestyle="--",
                alpha=0.55, zorder=9, transform=ccrs.Geodetic(),
            )
        ax.scatter(ac.lon, ac.lat, s=70, marker="^", color="#0000CC",
                   edgecolors="white", linewidths=0.8, zorder=10,
                   transform=ccrs.PlateCarree())
        lbl = ac.callsign
        if ac.alt_ft is not None:
            lbl += f"\nFL{int(round(ac.alt_ft / 100)):03d}"
        if rt:
            lbl += f"\n{rt['label']}"
        ax.annotate(lbl, xy=(ac.lon, ac.lat), xytext=(4, 4),
                    textcoords="offset points", fontsize=6,
                    fontweight="bold", color="#0000CC", zorder=10)

    ax.set_title(title, fontsize=10)
    plt.colorbar(mesh, ax=ax, pad=0.02, shrink=0.85,
                 label=PRODUCT_LABELS[product])
    # NOTE: no bbox_inches="tight" — crops the GeoAxes (see core.radar).
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

Aviation product set (HRRR 2D surface file, wrfsfcf):
  REFC    composite reflectivity (dBZ)
  RETOP   echo tops (m -> rendered kft)
  VIS     surface visibility (m -> statute miles)
  CEIL    cloud ceiling height (m -> hundreds of feet)
  GUST    10 m wind gust (m/s -> kt)
"""
from __future__ import annotations

import io
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import requests

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}

FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
IDX_URL = (
    "https://nomads.ncep.noaa.gov/pub/data/nccf/com/hrrr/prod/"
    "hrrr.{ymd}/conus/hrrr.t{cc}z.wrfsfcf{ff}.grib2.idx"
)

# product -> (filter var params, filter lev params)
PRODUCTS = {
    "REFC": ({"var_REFC": "on"}, {"lev_entire_atmosphere": "on"}),
    "RETOP": ({"var_RETOP": "on"}, {"lev_cloud_top": "on",
                                    "lev_entire_atmosphere": "on"}),
    "VIS": ({"var_VIS": "on"}, {"lev_surface": "on"}),
    "CEIL": ({"var_HGT": "on"}, {"lev_cloud_ceiling": "on"}),
    "GUST": ({"var_GUST": "on"}, {"lev_surface": "on"}),
}

PRODUCT_LABELS = {
    "REFC": "Composite Reflectivity (dBZ)",
    "RETOP": "Echo Tops (kft)",
    "VIS": "Visibility (SM)",
    "CEIL": "Ceiling (hundreds ft)",
    "GUST": "10 m Wind Gust (kt)",
}


def latest_cycle(fhr: int, now: Optional[datetime] = None) -> Optional[datetime]:
    """Newest HRRR cycle whose requested forecast hour exists on NOMADS.
    Walks back up to 6 hours probing the .idx sidecar (tiny)."""
    now = now or datetime.now(timezone.utc)
    for back in range(1, 7):
        cyc = (now - timedelta(hours=back)).replace(
            minute=0, second=0, microsecond=0
        )
        # F19-F48 only exist for 00/06/12/18Z cycles
        if fhr > 18 and cyc.hour % 6 != 0:
            continue
        url = IDX_URL.format(ymd=cyc.strftime("%Y%m%d"),
                             cc=f"{cyc.hour:02d}", ff=f"{fhr:02d}")
        try:
            r = requests.head(url, headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                return cyc
        except Exception:
            continue
    return None


def fetch_field(
    product: str,
    cycle: datetime,
    fhr: int,
    lat: float,
    lon: float,
    zoom_deg: float,
) -> bytes:
    """Small subregion GRIB2 for one field via the NOMADS filter."""
    var_p, lev_p = PRODUCTS[product]
    pad = zoom_deg + 0.4  # margin so pcolormesh fills the frame
    left = (lon - pad) % 360
    right = (lon + pad) % 360
    params = {
        "file": f"hrrr.t{cycle.hour:02d}z.wrfsfcf{fhr:02d}.grib2",
        "dir": f"/hrrr.{cycle:%Y%m%d}/conus",
        "subregion": "",
        "leftlon": f"{left:.2f}",
        "rightlon": f"{right:.2f}",
        "toplat": f"{lat + pad:.2f}",
        "bottomlat": f"{lat - pad:.2f}",
        **var_p,
        **lev_p,
    }
    r = requests.get(FILTER_URL, params=params, headers=_HEADERS, timeout=90)
    r.raise_for_status()
    if len(r.content) < 500 or r.content[:4] != b"GRIB":
        raise RuntimeError(
            f"NOMADS filter returned non-GRIB ({len(r.content)} bytes) "
            f"for {product} f{fhr:02d}"
        )
    return r.content


def decode_field(raw: bytes):
    """(values_2d, lat_2d, lon_2d) from a single-message GRIB2."""
    import xarray as xr

    with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as tf:
        tf.write(raw)
        path = tf.name
    ds = xr.open_dataset(path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    var = list(ds.data_vars)[0]
    vals = np.asarray(ds[var].values, dtype=float)
    lats = np.asarray(ds["latitude"].values, dtype=float)
    lons = np.asarray(ds["longitude"].values, dtype=float)
    lons = np.where(lons > 180, lons - 360, lons)
    ds.close()
    return vals, lats, lons


def render_field(
    product: str,
    vals: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    center_lat: float,
    center_lon: float,
    zoom_deg: float,
    title: str,
    aircraft=None,          # list[AircraftPos] JBU overlay
    routes=None,            # {callsign: {"label", "orig", "dest"}}
) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    data = np.ma.masked_invalid(vals)

    if product == "REFC":
        from metpy.plots import colortables
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5)
        data = np.ma.masked_less(data, -25)
    elif product == "RETOP":
        data = np.ma.masked_less(data, 0) / 304.8  # m -> kft
        bounds = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70]
        colors = ["#C8C8C8", "#9BD4F5", "#4FA8E8", "#2E6FDB", "#22B14C",
                  "#7CD934", "#FFF200", "#FFC90E", "#FF7F27", "#ED1C24",
                  "#B21E28", "#A349A4", "#6F2DA8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "VIS":
        data = data / 1609.34  # m -> SM
        bounds = [0, 0.5, 1, 2, 3, 5, 7, 10]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "CEIL":
        data = data * 3.28084 / 100.0  # m -> hundreds of ft
        data = np.ma.masked_greater(data, 300)  # >30kft ~ unlimited
        bounds = [0, 2, 4, 10, 20, 30, 50, 100, 300]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#A8D8A8", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    else:  # GUST
        data = data * 1.94384  # m/s -> kt
        bounds = [0, 10, 15, 20, 25, 30, 35, 40, 50, 65]
        colors = ["#E8E8E8", "#B0E0FF", "#60B0E0", "#FFFF00", "#FFC90E",
                  "#FF9900", "#FF4040", "#B21E28", "#A349A4"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)

    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
    ax.set_extent(
        [center_lon - zoom_deg, center_lon + zoom_deg,
         center_lat - zoom_deg, center_lat + zoom_deg],
        crs=ccrs.PlateCarree(),
    )
    mesh = ax.pcolormesh(
        lons, lats, data, cmap=cmap, norm=norm, shading="auto",
        transform=ccrs.PlateCarree(), zorder=2,
    )
    try:
        coast = cfeature.COASTLINE.with_scale("10m")
        states = cfeature.STATES.with_scale("10m")
        next(iter(coast.geometries()))
        ax.add_feature(coast, linewidth=0.8, zorder=3)
        ax.add_feature(states, linewidth=0.5, zorder=3)
    except Exception:
        pass
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, linestyle=":",
                      color="gray")
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 8}
    gl.ylabel_style = {"size": 8}

    routes = routes or {}
    for ac in aircraft or []:
        rt = routes.get(ac.callsign)
        if rt:
            # Great-circle route line origin -> destination (Geodetic
            # transform draws the true arc through the current position)
            (olat, olon), (dlat, dlon) = rt["orig"], rt["dest"]
            ax.plot(
                [olon, dlon], [olat, dlat],
                color="#0000CC", linewidth=1.0, linestyle="--",
                alpha=0.55, zorder=9, transform=ccrs.Geodetic(),
            )
        ax.scatter(ac.lon, ac.lat, s=70, marker="^", color="#0000CC",
                   edgecolors="white", linewidths=0.8, zorder=10,
                   transform=ccrs.PlateCarree())
        lbl = ac.callsign
        if ac.alt_ft is not None:
            lbl += f"\nFL{int(round(ac.alt_ft / 100)):03d}"
        if rt:
            lbl += f"\n{rt['label']}"
        ax.annotate(lbl, xy=(ac.lon, ac.lat), xytext=(4, 4),
                    textcoords="offset points", fontsize=6,
                    fontweight="bold", color="#0000CC", zorder=10)

    ax.set_title(title, fontsize=10)
    plt.colorbar(mesh, ax=ax, pad=0.02, shrink=0.85,
                 label=PRODUCT_LABELS[product])
    # NOTE: no bbox_inches="tight" — crops the GeoAxes (see core.radar).
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
