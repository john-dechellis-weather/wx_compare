"""Convection-allowing model fields for the Hi-Res CAMs page.

Generic multi-model layer over the NOMADS grib_filter CGIs. Each model
is a config entry; fetch/decode/render are shared. All requests are
single-field subregion subsets (~0.5-3 MB GRIB2), decoded with cfgrib.

Models:
  hrrr        HRRR CONUS (hourly cycles, f00-f18 here)
  nam_nest    NAM 3 km CONUS nest (00/06/12/18Z)   [retires Oct 2026]
  hiresw_arw  Hi-Res Window ARW CONUS (00/12Z)     [retires Oct 2026]
  rrfs        RRFS 3 km CONUS (00/06/12/18Z) - parallel NOMADS feed
              announced for ~Aug 11 2026, operational Oct 6 2026.
              Until files appear the panel reports "no cycle found".
"""
from __future__ import annotations

import io
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import requests

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}

NOMADS = "https://nomads.ncep.noaa.gov"

# product -> (var param, list of candidate level params). Different
# NOMADS filters name the same layer differently (e.g. HRRR's
# "entire_atmosphere" vs NAM's
# "entire_atmosphere_(considered_as_a_single_layer)"), so fetch_field
# tries candidates in order until one returns GRIB.
PRODUCT_PARAMS = {
    "REFC": ({"var_REFC": "on"}, [
        {"lev_entire_atmosphere": "on"},
        {"lev_entire_atmosphere_(considered_as_a_single_layer)": "on"},
    ]),
    "RETOP": ({"var_RETOP": "on"}, [
        {"lev_cloud_top": "on"},
        {"lev_entire_atmosphere": "on"},
    ]),
    "VIS": ({"var_VIS": "on"}, [{"lev_surface": "on"}]),
    "CEIL": ({"var_HGT": "on"}, [{"lev_cloud_ceiling": "on"}]),
    "GUST": ({"var_GUST": "on"}, [{"lev_surface": "on"}]),
}

PRODUCT_LABELS = {
    "REFC": "Composite Reflectivity (dBZ)",
    "RETOP": "Echo Tops (kft)",
    "VIS": "Visibility (SM)",
    "CEIL": "Ceiling (hundreds ft)",
    "GUST": "10 m Wind Gust (kt)",
}

# For idx-based fetching: (grib var name, level substring to match)
IDX_MATCHERS = {
    "REFC": ("REFC", "entire atmosphere"),
    "RETOP": ("RETOP", ""),
    "VIS": ("VIS", "surface"),
    "CEIL": ("HGT", "cloud ceiling"),
    "GUST": ("GUST", "surface"),
}

MODELS = {
    "hrrr": {
        "label": "HRRR",
        "filter": f"{NOMADS}/cgi-bin/filter_hrrr_2d.pl",
        "file": "hrrr.t{cc:02d}z.wrfsfcf{ff:02d}.grib2",
        "dir": "/hrrr.{ymd}/conus",
        "idx": (f"{NOMADS}/pub/data/nccf/com/hrrr/prod/"
                "hrrr.{ymd}/conus/hrrr.t{cc:02d}z.wrfsfcf{ff:02d}"
                ".grib2.idx"),
        "cycles": list(range(24)),
        "products": {"REFC", "RETOP", "VIS", "CEIL", "GUST"},
        "note": "",
    },
    "nam_nest": {
        "label": "NAM 3km Nest",
        "mechanism": "idx",
        "file": "nam.t{cc:02d}z.conusnest.hiresf{ff:02d}.tm00.grib2",
        "dir": "/nam.{ymd}",
        "idx": (f"{NOMADS}/pub/data/nccf/com/nam/prod/"
                "nam.{ymd}/nam.t{cc:02d}z.conusnest.hiresf{ff:02d}"
                ".tm00.grib2.idx"),
        "cycles": [0, 6, 12, 18],
        "products": {"REFC", "VIS", "CEIL", "GUST"},
        "note": "retires Oct 2026 (replaced by RRFS)",
    },
    "hiresw_arw": {
        "label": "HRW ARW",
        "mechanism": "idx",
        "file": "hiresw.t{cc:02d}z.arw_2p5km.f{ff:02d}.conus.grib2",
        "dir": "/hiresw.{ymd}",
        "idx": (f"{NOMADS}/pub/data/nccf/com/hiresw/prod/"
                "hiresw.{ymd}/hiresw.t{cc:02d}z.arw_2p5km.f{ff:02d}"
                ".conus.grib2.idx"),
        "cycles": [0, 12],
        "products": {"REFC", "VIS", "CEIL", "GUST"},
        "note": "retires Oct 2026 (replaced by RRFS)",
    },
    "rrfs": {
        "label": "RRFS",
        "mechanism": "idx",
        "file": "rrfs.t{cc:02d}z.prslev.3km.f{ff:03d}.conus.grib2",
        "dir": "/rrfs.{ymd}/{cc:02d}",
        "idx": (f"{NOMADS}/pub/data/nccf/com/rrfs/para/"
                "rrfs.{ymd}/{cc:02d}/rrfs.t{cc:02d}z.prslev.3km"
                ".f{ff:03d}.conus.grib2.idx"),
        "cycles": [0, 6, 12, 18],
        "products": {"REFC", "VIS", "CEIL", "GUST"},
        "note": ("parallel feed announced ~Aug 11 2026; "
                 "'no cycle found' is expected until it starts"),
    },
}


def latest_cycle(
    model: str, fhr: int, now: Optional[datetime] = None
) -> Optional[datetime]:
    """Newest cycle whose requested forecast hour exists (idx probe).
    Walks back up to 30 hours to cover sparse-cycle models."""
    cfg = MODELS[model]
    now = now or datetime.now(timezone.utc)
    for back in range(1, 31):
        cyc = (now - timedelta(hours=back)).replace(
            minute=0, second=0, microsecond=0
        )
        if cyc.hour not in cfg["cycles"]:
            continue
        url = cfg["idx"].format(ymd=cyc.strftime("%Y%m%d"),
                                cc=cyc.hour, ff=fhr)
        try:
            r = requests.head(url, headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                return cyc
        except Exception:
            continue
    return None


def fetch_field(
    model: str,
    product: str,
    cycle: datetime,
    fhr: int,
    lat: float,
    lon: float,
    zoom_deg: float,
) -> bytes:
    """Small subregion GRIB2 for one field via the model's filter CGI."""
    cfg = MODELS[model]
    if product not in cfg["products"]:
        raise RuntimeError(f"{cfg['label']} does not provide {product}")
    if cfg.get("mechanism") == "idx":
        return _fetch_field_idx(cfg, product, cycle, fhr)
    var_p, lev_candidates = PRODUCT_PARAMS[product]
    pad = zoom_deg + 0.4
    base = {
        "file": cfg["file"].format(cc=cycle.hour, ff=fhr),
        "dir": cfg["dir"].format(ymd=cycle.strftime("%Y%m%d"),
                                 cc=cycle.hour),
        "subregion": "",
        "leftlon": f"{(lon - pad) % 360:.2f}",
        "rightlon": f"{(lon + pad) % 360:.2f}",
        "toplat": f"{lat + pad:.2f}",
        "bottomlat": f"{lat - pad:.2f}",
        **var_p,
    }
    if cfg.get("ds"):
        base["ds"] = cfg["ds"]
    last_detail = "no level candidates"
    for lev_p in lev_candidates:
        try:
            r = requests.get(cfg["filter"], params={**base, **lev_p},
                             headers=_HEADERS, timeout=90)
        except Exception as e:
            last_detail = f"{type(e).__name__}: {e}"
            continue
        if (r.status_code == 200 and len(r.content) >= 500
                and r.content[:4] == b"GRIB"):
            return r.content
        last_detail = (
            f"HTTP {r.status_code}, {len(r.content)} bytes "
            f"with {list(lev_p)[0]}"
        )
    raise RuntimeError(
        f"{cfg['label']} filter failed for {product} f{fhr:02d} "
        f"(last attempt: {last_detail})"
    )


def _fetch_field_idx(cfg: dict, product: str, cycle, fhr: int) -> bytes:
    """Byte-range fetch of a single GRIB message using the .idx sidecar
    (the Herbie technique). Filter-independent: works for any NOMADS
    GRIB that publishes an index, so it survives interface migrations.
    Returns the full-domain field (~1-5 MB); the renderer crops to the
    requested extent."""
    idx_url = cfg["idx"].format(ymd=cycle.strftime("%Y%m%d"),
                                cc=cycle.hour, ff=fhr)
    grib_url = idx_url[:-4]  # strip ".idx"
    r = requests.get(idx_url, headers=_HEADERS, timeout=30)
    r.raise_for_status()

    var_name, lev_sub = IDX_MATCHERS[product]
    lines = r.text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        # "n:offset:d=YYYYMMDDHH:VAR:LEVEL:fcst:..."
        parts = line.split(":")
        if len(parts) < 5:
            continue
        if parts[3] == var_name and lev_sub in parts[4].lower():
            start = int(parts[1])
            for nxt in lines[i + 1:]:
                p2 = nxt.split(":")
                if len(p2) >= 2:
                    end = int(p2[1]) - 1
                    break
            break
    if start is None:
        available = sorted({
            p[3] for p in (l.split(":") for l in lines) if len(p) > 3
        })
        raise RuntimeError(
            f"{cfg['label']}: {var_name} ({lev_sub or 'any level'}) "
            f"not in index. Vars present: {', '.join(available[:25])}"
        )
    headers = {**_HEADERS,
               "Range": f"bytes={start}-{end}" if end else
                        f"bytes={start}-"}
    r2 = requests.get(grib_url, headers=headers, timeout=120)
    if r2.status_code not in (200, 206):
        raise RuntimeError(
            f"{cfg['label']}: range request HTTP {r2.status_code}"
        )
    if r2.content[:4] != b"GRIB":
        raise RuntimeError(
            f"{cfg['label']}: range response not GRIB "
            f"({len(r2.content)} bytes)"
        )
    return r2.content


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
    aircraft=None,
    routes=None,
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
        norm, cmap = colortables.get_with_steps("NWSReflectivity", 5, 5)
        # Standard CAM convention: mask < 5 dBZ so clear air stays clean
        data = np.ma.masked_less(data, 5)
    elif product == "RETOP":
        data = np.ma.masked_less(data, 0) / 304.8
        bounds = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70]
        colors = ["#C8C8C8", "#9BD4F5", "#4FA8E8", "#2E6FDB", "#22B14C",
                  "#7CD934", "#FFF200", "#FFC90E", "#FF7F27", "#ED1C24",
                  "#B21E28", "#A349A4", "#6F2DA8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "VIS":
        data = data / 1609.34
        bounds = [0, 0.5, 1, 2, 3, 5, 7, 10]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    elif product == "CEIL":
        data = data * 3.28084 / 100.0
        data = np.ma.masked_greater(data, 300)
        bounds = [0, 2, 4, 10, 20, 30, 50, 100, 300]
        colors = ["#FF80FF", "#FF4040", "#FF9900", "#FFFF00",
                  "#B0E000", "#60C060", "#A8D8A8", "#E8E8E8"]
        cmap = ListedColormap(colors); norm = BoundaryNorm(bounds, cmap.N)
    else:  # GUST
        data = data * 1.94384
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
    # NOTE: no bbox_inches="tight" - crops the GeoAxes (see core.radar).
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()