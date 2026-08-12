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
    "REFD": ({"var_REFD": "on"}, [
        {"lev_1000_m_above_ground": "on"},
    ]),
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
    "REFD": "1 km AGL Reflectivity (dBZ)",
    "REFC": "Composite Reflectivity (dBZ)",
    "RETOP": "Echo Tops (kft)",
    "VIS": "Visibility (SM)",
    "CEIL": "Ceiling (hundreds ft)",
    "GUST": "10 m Wind Gust (kt)",
}

# For idx-based fetching: (grib var name, level substring to match)
IDX_MATCHERS = {
    "REFD": ("REFD", "1000 m above ground"),
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
        "max_fhr": 18,
        "products": {"REFD", "REFC", "RETOP", "VIS", "CEIL", "GUST"},
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
        "max_fhr": 60,
        "products": {"REFD", "REFC", "VIS", "CEIL", "GUST"},
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
        "max_fhr": 48,
        "products": {"REFD", "REFC", "VIS", "CEIL", "GUST"},
        "note": "retires Oct 2026 (replaced by RRFS)",
    },
    "rrfs": {
        "label": "RRFS",
        # v1.0 feed on NOMADS publishes NO .idx sidecars (verified
        # by directory listing 8/12), and full files run 330-370 MB,
        # so RRFS uses the grib-filter CGI like HRRR. Cycle detection
        # HEAD-probes the grib file itself (zero-byte check). The
        # 2dfld file carries all six of our 2-D products.
        "file": "rrfs.t{cc:02d}z.2dfld.3km.f{ff:03d}.conus.grib2",
        "probe_candidates": [
            (f"{NOMADS}/pub/data/nccf/com/rrfs/v1.0/"
             "rrfs.{ymd}/{cc:02d}/rrfs.t{cc:02d}z.2dfld.3km"
             ".f{ff:03d}.conus.grib2"),
            (f"{NOMADS}/pub/data/nccf/com/rrfs/para/"
             "rrfs.{ymd}/{cc:02d}/rrfs.t{cc:02d}z.2dfld.3km"
             ".f{ff:03d}.conus.grib2"),
        ],
        # Modern NOMADS filter is gribfilter.php?ds=NAME (hrrr_2d
        # convention suggests rrfs_2d for the 2dfld files); entries
        # are (url, ds_or_None). Legacy .pl spellings trail as
        # fallbacks.
        "filter_candidates": [
            (f"{NOMADS}/gribfilter.php", "rrfs_2d"),
            (f"{NOMADS}/gribfilter.php", "rrfs"),
            (f"{NOMADS}/gribfilter.php", "rrfs_3km"),
            (f"{NOMADS}/gribfilter.php", "rrfs_conus_2d"),
            (f"{NOMADS}/cgi-bin/filter_rrfs_2d.pl", None),
            (f"{NOMADS}/cgi-bin/filter_rrfs.pl", None),
        ],
        "dir_candidates": [
            "/rrfs.{ymd}/{cc:02d}",
            "/v1.0/rrfs.{ymd}/{cc:02d}",
            "/para/rrfs.{ymd}/{cc:02d}",
        ],
        "cycles": list(range(24)),
        "max_fhr": 60,
        "products": {"REFD", "REFC", "RETOP", "VIS", "CEIL",
                     "GUST"},
        "note": ("RRFS v1.0 feed via grib filter; pre-operational "
                 "until Oct 2026"),
    },
}

# Candidates that connection-error get dropped for the session so
# repeated timeouts don't tax every cycle probe.
_dead_candidates: set = set()


def latest_cycle(
    model: str, fhr: int, now: Optional[datetime] = None
) -> Optional[datetime]:
    """Newest cycle whose requested forecast hour exists (idx probe).
    Walks back up to 30 hours to cover sparse-cycle models."""
    cfg = MODELS[model]
    now = now or datetime.now(timezone.utc)
    candidates = (cfg.get("probe_candidates")
                  or cfg.get("idx_candidates")
                  or [cfg["idx"]])
    diag: dict = {}
    cfg["_probe_diag"] = diag
    if cfg.get("_idx_resolved"):
        candidates = [cfg["_idx_resolved"]]
    max_back = 31 if len(candidates) == 1 else 13
    for back in range(1, max_back):
        cyc = (now - timedelta(hours=back)).replace(
            minute=0, second=0, microsecond=0
        )
        if cyc.hour not in cfg["cycles"]:
            continue
        for tmpl in candidates:
            if tmpl in _dead_candidates:
                continue
            url = tmpl.format(ymd=cyc.strftime("%Y%m%d"),
                              cc=cyc.hour, ff=fhr)
            try:
                r = requests.head(url, headers=_HEADERS, timeout=8)
            except requests.exceptions.ConnectionError as e:
                _dead_candidates.add(tmpl)
                diag[tmpl] = f"ConnectionError: {e}"[:120]
                continue
            except Exception as e:
                diag[tmpl] = f"{type(e).__name__}: {e}"[:120]
                continue
            # Some servers reject HEAD; retry as a zero-range GET
            if r.status_code in (403, 405):
                try:
                    r = requests.get(
                        url, headers={**_HEADERS,
                                      "Range": "bytes=0-0"},
                        timeout=8,
                    )
                except Exception as e:
                    diag[tmpl] = f"{type(e).__name__}"[:120]
                    continue
            diag[tmpl] = f"HTTP {r.status_code} @{cyc:%d/%H}z"
            if r.status_code in (200, 206):
                if cfg.get("idx_candidates"):
                    cfg["_idx_resolved"] = tmpl
                return cyc
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
        "subregion": "",
        "leftlon": f"{(lon - pad) % 360:.2f}",
        "rightlon": f"{(lon + pad) % 360:.2f}",
        "toplat": f"{lat + pad:.2f}",
        "bottomlat": f"{lat - pad:.2f}",
        **var_p,
    }
    if cfg.get("ds"):
        base["ds"] = cfg["ds"]

    # Filter URL and dir may each have several plausible spellings
    # (RRFS is brand-new); the first working (filter, dir) pair is
    # memoized for the session.
    if cfg.get("_filter_resolved"):
        filter_specs = [cfg["_filter_resolved"][0]]
        dir_tmpls = [cfg["_filter_resolved"][1]]
    else:
        raw_specs = (cfg.get("filter_candidates")
                     or [cfg["filter"]])
        filter_specs = [
            s if isinstance(s, tuple) else (s, cfg.get("ds"))
            for s in raw_specs
        ]
        dir_tmpls = cfg.get("dir_candidates") or [cfg["dir"]]

    attempts: list = []
    for f_spec in filter_specs:
        f_url, f_ds = f_spec
        if (f_url, f_ds) in _dead_candidates:
            continue
        for d_tmpl in dir_tmpls:
            d = d_tmpl.format(ymd=cycle.strftime("%Y%m%d"),
                              cc=cycle.hour)
            params = {**base, "dir": d}
            if f_ds:
                params["ds"] = f_ds
            for lev_p in lev_candidates:
                try:
                    r = requests.get(
                        f_url, params={**params, **lev_p},
                        headers=_HEADERS, timeout=90,
                    )
                except requests.exceptions.ConnectionError as e:
                    _dead_candidates.add((f_url, f_ds))
                    attempts.append(
                        f"{f_url.rsplit('/', 1)[-1]}: ConnErr"
                    )
                    break
                except Exception as e:
                    attempts.append(f"{type(e).__name__}")
                    continue
                if (r.status_code == 200 and len(r.content) >= 500
                        and r.content[:4] == b"GRIB"):
                    cfg["_filter_resolved"] = (f_spec, d_tmpl)
                    return r.content
                tag = f_url.rsplit('/', 1)[-1]
                if f_ds:
                    tag += f"?ds={f_ds}"
                attempts.append(
                    f"{tag} dir={d_tmpl.split('/')[1][:9]}: "
                    f"HTTP {r.status_code} {len(r.content)}B"
                )
                # 404 on the script itself: skip its dir variants
                if r.status_code == 404 and len(r.content) < 500:
                    break
            else:
                continue
            break
    # Report EVERY distinct attempt - the informative failure is
    # often mid-list, not last.
    seen = []
    for a in attempts:
        if a not in seen:
            seen.append(a)
    raise RuntimeError(
        f"{cfg['label']} filter failed for {product} f{fhr:02d}. "
        f"Attempts: " + " | ".join(seen[:10])
    )


def _fetch_field_idx(cfg: dict, product: str, cycle, fhr: int) -> bytes:
    """Byte-range fetch of a single GRIB message using the .idx sidecar
    (the Herbie technique). Filter-independent: works for any NOMADS
    GRIB that publishes an index, so it survives interface migrations.
    Returns the full-domain field (~1-5 MB); the renderer crops to the
    requested extent."""
    tmpl = cfg.get("_idx_resolved") or cfg["idx"]
    idx_url = tmpl.format(ymd=cycle.strftime("%Y%m%d"),
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


def fetch_and_decode(
    model: str,
    product: str,
    cycle: datetime,
    fhr: int,
    lat: float,
    lon: float,
    zoom_deg: float,
):
    """fetch_field + decode_field in one call (thread-safe: pure I/O
    and numpy, no matplotlib)."""
    raw = fetch_field(model, product, cycle, fhr, lat, lon, zoom_deg)
    return decode_field(raw)


def parallel_fetch_decode(tasks: list[dict], max_workers: int = 6):
    """Run many fetch_and_decode calls concurrently.

    tasks: [{"key": anything-hashable, "model", "product", "cycle",
             "fhr", "lat", "lon", "zoom_deg"}, ...]
    Returns {key: (vals, lats, lons) | Exception}. Downloads and
    decodes overlap across models and hours; rendering stays with the
    caller (matplotlib is not thread-safe).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {
            ex.submit(
                fetch_and_decode, t["model"], t["product"], t["cycle"],
                t["fhr"], t["lat"], t["lon"], t["zoom_deg"],
            ): t["key"]
            for t in tasks
        }
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                out[key] = fut.result()
            except Exception as e:
                out[key] = e
    return out


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

    if product in ("REFC", "REFD"):
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
