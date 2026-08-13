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

# ET tries Enhanced Echo Tops (135, 1 km) first, legacy (41, 4 km) second.
PRODUCT_DIRS = {
    "REF": ["DS.p94r0"],
    "VEL": ["DS.p99v0"],
    "ET": ["DS.135et", "DS.p41et"],
}


def _site_dirs(product: str, site: str) -> list[str]:
    """site like KOKX/TJUA/PHKI -> SI.kokx (server uses lowercase)."""
    return [
        f"{TGFTP_BASE}/{d}/SI.{site.lower()}"
        for d in PRODUCT_DIRS[product]
    ]


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
    tgftp_err: Exception = RuntimeError("no tgftp dirs")
    for base in _site_dirs(product, site):
        try:
            r = requests.get(f"{base}/sn.last", headers=_HEADERS, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as e:
            tgftp_err = e
    if True:
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
    last_err: Exception = RuntimeError("no tgftp dirs")
    for base in _site_dirs(product, site):
        try:
            return _fetch_recent_tgftp_one(base, n)
        except Exception as e:
            last_err = e
    raise last_err


def _fetch_recent_tgftp_one(base: str, n: int) -> list[tuple[bytes, str]]:
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
    if arr.ndim == 3 and arr.shape[0] <= 3:
        # Some digital products (e.g. EET/135) map into multiple planes:
        # physical values plus a small flag layer. Keep the plane with
        # the largest magnitudes — kft values dwarf 0/1 flags.
        def _plane_score(p):
            with np.errstate(all="ignore"):
                m = np.nanmax(p)
            return m if np.isfinite(m) else -1.0
        arr = max((arr[i] for i in range(arr.shape[0])), key=_plane_score)
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



# --- Major-city labels (Natural Earth populated_places) ---
_city_cache: dict = {}


def _city_records():
    """Load NE 10m populated places once per process."""
    if "recs" in _city_cache:
        return _city_cache["recs"]
    try:
        from cartopy.io import shapereader
        path = shapereader.natural_earth(
            resolution="10m", category="cultural",
            name="populated_places",
        )
        recs = []
        for r in shapereader.Reader(path).records():
            a = r.attributes
            recs.append((
                float(r.geometry.y), float(r.geometry.x),
                a.get("NAME") or "",
                int(a.get("SCALERANK") or 10),
                int(a.get("POP_MAX") or 0),
            ))
        _city_cache["recs"] = recs
    except Exception:
        _city_cache["recs"] = []
    return _city_cache["recs"]


def _plot_cities(ax, lat0, lat1, lon0, lon1, max_n=9):
    """Label the most significant cities in the view: dot + outlined
    name, count-limited and overlap-thinned for readability."""
    import matplotlib.patheffects as pe

    import cartopy.crs as ccrs

    in_view = [
        c for c in _city_records()
        if lat0 <= c[0] <= lat1 and lon0 <= c[1] <= lon1
    ]
    in_view.sort(key=lambda c: (c[3], -c[4]))
    shown = []
    min_sep = (lat1 - lat0) * 0.07
    for lat, lon, name, _rank, _pop in in_view:
        if len(shown) >= max_n:
            break
        if any(abs(lat - sa) < min_sep and abs(lon - so) < min_sep
               for sa, so in shown):
            continue
        ax.plot(lon, lat, marker="o", markersize=2.5,
                color="#FFFFFF", markeredgecolor="#000000",
                markeredgewidth=0.5, zorder=4,
                transform=ccrs.PlateCarree())
        ax.text(
            lon, lat, "  " + name, fontsize=7, color="#FFFFFF",
            zorder=4, va="center",
            path_effects=[pe.withStroke(linewidth=2,
                                        foreground="#000000")],
            transform=ccrs.PlateCarree(),
        )
        shown.append((lat, lon))


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
    trail=None,                 # [(lat, lon), ...] flight path so far
    others_trails=None,         # {callsign: [(lat, lon), ...]}
    routes=None,                # {callsign: {"orig", "dest", "label"}}
    return_geometry: bool = False,   # also return px<->deg mapping
    mark_center: bool = False,  # blue ring at the center point
                                # (airport on Quick View; OFF on the
                                # Tracker where center is an aircraft)
):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from metpy.calc import azimuth_range_to_lat_lon
    from metpy.plots import colortables
    from metpy.units import units as mpunits

    fig = plt.figure(figsize=(12, 10))
    # Tight explicit margins (bbox_inches="tight" is banned with
    # cartopy): less dead white around the map.
    fig.subplots_adjust(left=0.06, right=0.90, top=0.95,
                        bottom=0.06)
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
    ax.set_extent(
        [center_lon - zoom_deg, center_lon + zoom_deg,
         center_lat - zoom_deg, center_lat + zoom_deg],
        crs=ccrs.PlateCarree(),
    )
    # True-scale aspect: without this, equal-degree axes over-stretch
    # east-west at latitude (the oval-ring tell). Set after
    # set_extent so cartopy keeps it.
    try:
        ax.set_aspect(1.0 / np.cos(np.radians(center_lat)))
    except Exception:
        pass

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
    if product == "REF":
        # Suppress clear-air/light clutter: 10 dBZ and under hidden
        data = np.ma.masked_less_equal(data, 10.0)
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
    if mark_center:
        try:
            # Blue ring at the field + geographically true 20 nm
            # range ring (1 nm = 1/60 deg latitude; longitude
            # stretched by cos(lat) so the ring stays circular on
            # the earth, not the screen).
            ax.plot(
                center_lon, center_lat, marker="o", markersize=9,
                markerfacecolor="none", markeredgecolor="#0055FF",
                markeredgewidth=2.2, zorder=5,
                transform=ccrs.PlateCarree(),
            )
            ring_nm = 20.0
            r_lat = ring_nm / 60.0
            th = np.linspace(0, 2 * np.pi, 121)
            ring_lats = center_lat + r_lat * np.cos(th)
            ring_lons = center_lon + (
                r_lat * np.sin(th)
                / np.cos(np.radians(center_lat))
            )
            ax.plot(
                ring_lons, ring_lats, color="#FFFFFF",
                linewidth=4.8, linestyle="--", alpha=0.9,
                zorder=5, transform=ccrs.PlateCarree(),
            )
            ax.text(
                center_lon, center_lat + r_lat, " 20 nm",
                fontsize=6.5, color="#FFFFFF", va="bottom",
                ha="center", zorder=5,
                transform=ccrs.PlateCarree(),
            )
        except Exception:
            pass
    try:
        _plot_cities(
            ax,
            center_lat - zoom_deg, center_lat + zoom_deg,
            center_lon - zoom_deg, center_lon + zoom_deg,
        )
    except Exception:
        pass   # cities are decoration; never block the render

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

    target_cs = getattr(target_aircraft, "callsign", None)

    # Route arcs (origin -> destination great circles): where each
    # aircraft is GOING, dotted to contrast with the dashed been-trails
    for _cs, _rt in (routes or {}).items():
        (_ola, _olo), (_dla, _dlo) = _rt["orig"], _rt["dest"]
        _is_t = (target_cs is not None and _cs == target_cs)
        ax.plot(
            [_olo, _dlo], [_ola, _dla],
            color=("red" if _is_t else "#0000CC"),
            linewidth=(1.3 if _is_t else 0.8), linestyle=":",
            alpha=(0.7 if _is_t else 0.45), zorder=8,
            transform=ccrs.Geodetic(),
        )

    # Other-aircraft trails (thin blue, under everything)
    for _cs, _tr in (others_trails or {}).items():
        if _tr and len(_tr) >= 2:
            ax.plot(
                [p[1] for p in _tr], [p[0] for p in _tr],
                color="#0000CC", linewidth=0.8, linestyle="--",
                alpha=0.45, zorder=7, transform=ccrs.PlateCarree(),
            )

    # Flight path trail (dashed, under the target marker)
    if trail and len(trail) >= 2:
        ax.plot(
            [p[1] for p in trail], [p[0] for p in trail],
            color="red", linewidth=1.4, linestyle="--", alpha=0.85,
            zorder=8, transform=ccrs.PlateCarree(),
        )

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
    if return_geometry:
        # Pin the axes so the saved-PNG pixel box is knowable, then
        # record the mapping for millisecond aircraft stamping onto
        # the finished PNG (PlateCarree = linear lat/lon <-> px).
        fig.canvas.draw()
        bbox = ax.get_window_extent()
        fig_h_px = fig.get_size_inches()[1] * 100  # dpi=100
        geometry = {
            "x0": float(bbox.x0), "x1": float(bbox.x1),
            # matplotlib bbox origin is bottom-left; PNG rows are
            # top-down, so flip y against figure height.
            "y_top": float(fig_h_px - bbox.y1),
            "y_bot": float(fig_h_px - bbox.y0),
            "lon0": center_lon - zoom_deg,
            "lon1": center_lon + zoom_deg,
            "lat0": center_lat - zoom_deg,
            "lat1": center_lat + zoom_deg,
        }
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    if return_geometry:
        return buf.getvalue(), geometry
    return buf.getvalue()
