"""Live NEXRAD Level III (NIDS) products from the NWS tgftp server.

Directory layout (verified against the live server):
  https://tgftp.nws.noaa.gov/SL.us008001/DF.of/DC.radar/
      DS.p94r0/SI.k<site>/sn.last     hi-res base reflectivity 0.5 deg
      DS.p99v0/SI.k<site>/sn.last     hi-res base velocity 0.5 deg
      DS.p41et/SI.k<site>/sn.last     echo tops (legacy raster, kft)
  Files are ~20-30 KB, refreshed every volume scan (~5-7 min), and each
  site keeps a ~250-file ring buffer (sn.0000..sn.0250, ~29 h) whose
  recency is only knowable from Last-Modified — the loop fetcher parses
  the directory listing to pick the newest N.

Rendering reuses the cartopy pipeline conventions from core.radar
(same colortables, same no-tight-bbox rule) and supports a target
aircraft (red) plus other traffic (blue).
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import requests

TGFTP_BASE = "https://tgftp.nws.noaa.gov/SL.us008001/DF.of/DC.radar"
_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}

PRODUCT_DIRS = {
    "REF": "DS.p94r0",
    "VEL": "DS.p99v0",
    "ET": "DS.p41et",
}


def _site_dir(product: str, site: str) -> str:
    """site like KOKX/TJUA/PHKI -> SI.kokx (server uses lowercase)."""
    return f"{TGFTP_BASE}/{PRODUCT_DIRS[product]}/SI.{site.lower()}"


# THREDDS fallback: UCAR IDD serves the same NIDS products; the host is
# already proven reachable from the app server (Level II uses it).
THREDDS_L3_PRODUCTS = {"REF": "N0Q", "VEL": "N0U", "ET": "EET"}


def _site3(site: str) -> str:
    """KOKX -> OKX, TJUA -> JUA, PHKI -> HKI."""
    return site[1:].upper() if len(site) == 4 else site.upper()


def _thredds_l3_datasets(product: str, site: str):
    """Newest-first dataset list for today (and yesterday as spillover)."""
    from datetime import timedelta as _td
    from siphon.catalog import TDSCatalog

    prod = THREDDS_L3_PRODUCTS[product]
    s3 = _site3(site)
    out = []
    day = datetime.now(timezone.utc)
    for d in (day, day - _td(days=1)):
        url = (
            "https://thredds.ucar.edu/thredds/catalog/nexrad/level3/"
            f"{prod}/{s3}/{d:%Y%m%d}/catalog.xml"
        )
        try:
            cat = TDSCatalog(url)
        except Exception:
            continue
        names = sorted(cat.datasets.keys(), reverse=True)
        out.extend((name, cat.datasets[name]) for name in names)
        if out:
            break
    return out


def _thredds_fetch(ds) -> bytes:
    url = ds.access_urls.get("HTTPServer") or ds.access_urls.get("httpserver")
    r = requests.get(url, headers=_HEADERS, timeout=60)
    r.raise_for_status()
    return r.content


def fetch_latest(product: str, site: str) -> bytes:
    try:
        url = f"{_site_dir(product, site)}/sn.last"
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return r.content
    except Exception as tgftp_err:
        dsets = _thredds_l3_datasets(product, site)
        if not dsets:
            raise RuntimeError(
                f"tgftp failed ({type(tgftp_err).__name__}) and THREDDS "
                f"has no Level III {product} datasets for {site}"
            )
        return _thredds_fetch(dsets[0][1])


_DIR_ROW_RE = re.compile(
    r'href="(sn\.\d{4})".*?(\d{2}-\w{3}-\d{4} \d{2}:\d{2})', re.S
)


def fetch_recent(product: str, site: str, n: int = 6) -> list[tuple[bytes, str]]:
    """Newest n frames, oldest first. tgftp ring buffer first; THREDDS
    dataset list as fallback."""
    try:
        return _fetch_recent_tgftp(product, site, n)
    except Exception:
        out = []
        for name, ds in _thredds_l3_datasets(product, site)[:n]:
            try:
                out.append((_thredds_fetch(ds), name))
            except Exception:
                continue
        out.reverse()  # newest-first list -> oldest-first frames
        return out


def _fetch_recent_tgftp(product: str, site: str, n: int) -> list[tuple[bytes, str]]:
    base = _site_dir(product, site)
    r = requests.get(base + "/", headers=_HEADERS, timeout=20)
    r.raise_for_status()
    entries = []
    for m in _DIR_ROW_RE.finditer(r.text):
        name, stamp = m.group(1), m.group(2)
        try:
            dt = datetime.strptime(stamp, "%d-%b-%Y %H:%M")
        except ValueError:
            continue
        entries.append((dt, name))
    entries.sort()
    newest = entries[-n:]
    out = []
    for dt, name in newest:
        try:
            rr = requests.get(f"{base}/{name}", headers=_HEADERS, timeout=30)
            rr.raise_for_status()
            out.append((rr.content, name))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Parsing + rendering
# ---------------------------------------------------------------------------
def parse_l3(raw: bytes):
    """Return dict with kind ('radial'|'raster'), data, geometry, meta."""
    from metpy.io import Level3File

    f = Level3File(io.BytesIO(raw))
    d = f.sym_block[0][0]
    mapped = f.map_data(d["data"])
    try:
        arr = np.asarray(mapped, dtype=float)
    except (TypeError, ValueError):
        # Legacy products (e.g. p41 Echo Tops) map through coded level
        # strings ("ND", "TH", ...) — coerce element-wise, non-numeric
        # becomes NaN (masked).
        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return np.nan
        arr = np.array(
            [[_num(v) for v in row] for row in np.asarray(mapped, dtype=object)],
            dtype=float,
        )
    data = np.ma.masked_invalid(arr)
    meta = {
        "site_lat": float(f.lat),
        "site_lon": float(f.lon),
        "prod_code": int(f.prod_desc.prod_code),
        "valid": getattr(f.metadata, "get", lambda *_: None)("prod_time")
                 if isinstance(getattr(f, "metadata", None), dict) else None,
    }
    if isinstance(getattr(f, "metadata", None), dict):
        meta["valid"] = f.metadata.get("prod_time")
    if "start_az" in d:
        az = np.array(d["start_az"] + [d["end_az"][-1]])
        rng_km = np.linspace(0, float(f.max_range), data.shape[-1] + 1)
        return {"kind": "radial", "data": data, "az": az,
                "rng_km": rng_km, **meta}
    # Raster (echo tops): square grid centered on the radar.
    half_km = float(getattr(f, "max_range", 230.0) or 230.0)
    return {"kind": "raster", "data": data, "half_km": half_km, **meta}


def render_l3(
    parsed: dict,
    product: str,
    center_lat: float,
    center_lon: float,
    zoom_deg: float,
    site: str,
    target_aircraft=None,       # AircraftPos of the tracked flight (red)
    other_aircraft=None,        # list[AircraftPos] (blue)
    title_note: str = "",
) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from metpy.calc import azimuth_range_to_lat_lon
    from metpy.plots import colortables
    from metpy.units import units as mpunits

    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
    ax.set_extent(
        [center_lon - zoom_deg, center_lon + zoom_deg,
         center_lat - zoom_deg, center_lat + zoom_deg],
        crs=ccrs.PlateCarree(),
    )

    if product == "REF":
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5)
        cbar_label = "Reflectivity (dBZ)"
        title = "Level III Base Reflectivity (0.5\u00b0)"
    elif product == "VEL":
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 0.5)
        cbar_label = "Velocity (kt)"
        title = "Level III Base Velocity (0.5\u00b0)"
    else:  # ET
        from matplotlib.colors import BoundaryNorm, ListedColormap
        bounds = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70]
        colors = [
            "#C8C8C8", "#9BD4F5", "#4FA8E8", "#2E6FDB", "#22B14C",
            "#7CD934", "#FFF200", "#FFC90E", "#FF7F27", "#ED1C24",
            "#B21E28", "#A349A4", "#6F2DA8",
        ]
        cmap = ListedColormap(colors)
        norm = BoundaryNorm(bounds, cmap.N)
        cbar_label = "Echo Top (kft)"
        title = "Level III Echo Tops"

    data = parsed["data"]
    if product == "VEL":
        # Digital velocity is stored in m/s by the product; convert when
        # magnitudes look like m/s (Nyquist ~30 m/s vs ~60+ kt).
        finite = data.compressed() if hasattr(data, "compressed") else data
        if finite.size and np.nanmax(np.abs(finite)) < 45:
            data = data * 1.94384

    if parsed["kind"] == "radial":
        lon_grid, lat_grid = azimuth_range_to_lat_lon(
            mpunits.Quantity(parsed["az"], "degrees"),
            mpunits.Quantity(parsed["rng_km"], "kilometers"),
            parsed["site_lon"], parsed["site_lat"],
        )
        mesh = ax.pcolormesh(
            lon_grid, lat_grid, data, cmap=cmap, norm=norm,
            shading="auto", transform=ccrs.PlateCarree(), zorder=2,
        )
    else:
        half_m = parsed["half_km"] * 1000.0
        aeqd = ccrs.AzimuthalEquidistant(
            central_longitude=parsed["site_lon"],
            central_latitude=parsed["site_lat"],
        )
        mesh = ax.imshow(
            data,
            extent=(-half_m, half_m, -half_m, half_m),
            origin="upper",
            cmap=cmap, norm=norm,
            transform=aeqd, zorder=2, interpolation="nearest",
        )

    # Map features: cached on the server; environments without Natural
    # Earth access still render the data. geometries() is materialized
    # here because cartopy otherwise defers (and fails) at draw time.
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
    gl.xlabel_style = {"size": 9}
    gl.ylabel_style = {"size": 9}

    # Other JBU traffic (blue)
    for ac in other_aircraft or []:
        ax.scatter(ac.lon, ac.lat, s=80, marker="^", color="#0000CC",
                   edgecolors="white", linewidths=0.8, zorder=10,
                   transform=ccrs.PlateCarree())
        lbl = ac.callsign
        if ac.alt_ft is not None:
            lbl += f"\nFL{int(round(ac.alt_ft / 100)):03d}"
        ax.annotate(lbl, xy=(ac.lon, ac.lat), xytext=(5, 5),
                    textcoords="offset points", fontsize=7,
                    fontweight="bold", color="#0000CC", zorder=10)

    # Target flight (red, prominent)
    if target_aircraft is not None:
        t = target_aircraft
        ax.scatter(t.lon, t.lat, s=200, marker="^", color="red",
                   edgecolors="white", linewidths=1.2, zorder=11,
                   transform=ccrs.PlateCarree())
        lbl = t.callsign
        if t.alt_ft is not None:
            lbl += f"\nFL{int(round(t.alt_ft / 100)):03d}"
        ax.annotate(lbl, xy=(t.lon, t.lat), xytext=(8, 8),
                    textcoords="offset points", fontsize=10,
                    fontweight="bold", color="red", zorder=11)

    note = f" \u00b7 {title_note}" if title_note else ""
    ax.set_title(
        f"{site} {title}{note}\n"
        f"Radar: {parsed['site_lat']:.2f}\u00b0, {parsed['site_lon']:.2f}\u00b0",
        fontsize=11,
    )
    plt.colorbar(mesh, ax=ax, pad=0.02, shrink=0.8, label=cbar_label)

    # NOTE: no bbox_inches="tight" — crops the GeoAxes (see core.radar).
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
