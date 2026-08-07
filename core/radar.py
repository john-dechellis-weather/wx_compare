"""NEXRAD Level II radar fetching and plot rendering.

Hybrid data discovery (all plain HTTPS / proven-reachable services):

  1. AWS public bucket XML listing — tried first, currently denies
     anonymous listing (kept in case the policy is relaxed again).
  2. Google Cloud public mirror (gcp-public-data-nexrad-l2) — deep
     archive of past years, but stale for recent dates.
  3. UCAR THREDDS "NEXRAD Level II Radar from IDD" via siphon — rolling
     window of recent weeks; covers what the GCS mirror lacks.

All three yield Level II volume files decoded identically with metpy
Level2File (gzip handled transparently). Reflectivity from the lowest
surveillance sweep, velocity from the lowest Doppler sweep (m/s → kt).
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import requests

_AWS_BASE = "https://noaa-nexrad-level2.s3.amazonaws.com"
_GCS_LIST = "https://storage.googleapis.com/storage/v1/b/gcp-public-data-nexrad-l2/o"
_GCS_DL = "https://storage.googleapis.com/gcp-public-data-nexrad-l2"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_THREDDS_BASE = "https://thredds.ucar.edu/thredds/"
_THREDDS_L2_NAME = "NEXRAD Level II Radar from IDD"
_MS_TO_KT = 1.94384
# Matches KAMX20260724_211005 (6-digit time) and Level2_KAMX_20260724_2110 (4-digit)
_TIME_RE = re.compile(r"(\d{8})_(\d{4,6})")
_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}


@dataclass
class _ScanRef:
    filename: str
    scan_time: datetime
    download_url: str


# ---------------------------------------------------------------------------
# Public API — single frame
# ---------------------------------------------------------------------------
def fetch_and_render_radar(
    target_time: datetime,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
) -> tuple[bytes, bytes, str, str]:
    """Fetch the Level II volume nearest target_time, render REF + VEL."""
    tgt = target_time.replace(tzinfo=None)
    scans = _find_scans(
        station, tgt - timedelta(minutes=20), tgt + timedelta(minutes=20)
    )
    if not scans:
        raise ValueError(
            f"No Level II volumes found for {_station4(station)} within "
            f"20 minutes of {target_time:%Y-%m-%d %H:%M UTC} in any source "
            f"(archive mirror + recent THREDDS window)."
        )

    best = min(scans, key=lambda s: abs(s.scan_time - tgt))

    refl_png, vel_png, name = _download_and_render(
        best, aircraft_lat, aircraft_lon, callsign, station, zoom_deg
    )
    vel_time = name if vel_png else "not available"
    return refl_png, vel_png, name, vel_time


# ---------------------------------------------------------------------------
# Public API — loop
# ---------------------------------------------------------------------------
def fetch_and_render_radar_loop(
    start_time: datetime,
    duration_min: int,
    aircraft_lat: float,
    aircraft_lon: float,
    callsign: str,
    station: str,
    zoom_deg: float,
    include_velocity: bool = True,
    overlay_aircraft: list | None = None,
    overlay_fn=None,
) -> tuple[list[tuple[bytes, str]], list[tuple[bytes, str]]]:
    """Fetch all Level II volumes in [start, start+duration], render each."""
    start = start_time.replace(tzinfo=None)
    end = start + timedelta(minutes=duration_min)

    scans = _find_scans(station, start, end)
    if not scans:
        raise ValueError(
            f"No Level II volumes found for {_station4(station)} between "
            f"{start:%Y-%m-%d %H:%M} and {end:%H:%M UTC} in any source."
        )

    scans.sort(key=lambda s: s.scan_time)

    refl_frames: list[tuple[bytes, str]] = []
    vel_frames: list[tuple[bytes, str]] = []
    for scan in scans:
        # Per-frame overlay: overlay_fn(scan_time) -> positions at that
        # moment (OpenSky time-travel), letting planes move frame to
        # frame. Falls back to the static overlay_aircraft list ("now"
        # positions repeated on every frame) when no callable is given.
        frame_overlay = overlay_aircraft
        if overlay_fn is not None:
            try:
                frame_overlay = overlay_fn(scan.scan_time)
            except Exception:
                frame_overlay = overlay_aircraft
        try:
            refl_png, vel_png, name = _download_and_render(
                scan, aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
                include_velocity=include_velocity,
                overlay_aircraft=frame_overlay,
            )
        except Exception:
            continue
        refl_frames.append((refl_png, name))
        if vel_png:
            vel_frames.append((vel_png, name))

    if not refl_frames:
        raise ValueError("All volumes in the window failed to decode/render.")

    return refl_frames, vel_frames


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _station4(station: str) -> str:
    s = station.strip().upper()
    return s if len(s) == 4 else "K" + s


def _parse_time(filename: str) -> datetime | None:
    m = _TIME_RE.search(filename)
    if not m:
        return None
    timepart = m.group(2).ljust(6, "0")  # pad HHMM → HHMM00
    try:
        return datetime.strptime(m.group(1) + timepart, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _find_scans(station: str, start: datetime, end: datetime) -> list[_ScanRef]:
    """Bucket sources first (AWS→GCS); THREDDS fallback for recent data."""
    st4 = _station4(station)

    scans = _find_scans_buckets(st4, start, end)
    if scans:
        return scans

    try:
        return _find_scans_thredds(st4, start, end)
    except Exception as e:
        print(f"[RADAR] THREDDS Level II fallback failed: {e}")
        return []


def _find_scans_buckets(st4: str, start: datetime, end: datetime) -> list[_ScanRef]:
    scans: list[_ScanRef] = []
    day = start.date()
    while day <= end.date():
        datepath = f"{day:%Y/%m/%d}"
        for filename, url in _list_day_buckets(datepath, st4):
            if "MDM" in filename:
                continue
            ts = _parse_time(filename)
            if ts is None:
                continue
            if start <= ts <= end:
                scans.append(
                    _ScanRef(filename=filename, scan_time=ts, download_url=url)
                )
        day += timedelta(days=1)
    return scans


def _list_day_buckets(datepath: str, st4: str) -> list[tuple[str, str]]:
    """List (filename, download_url) for one station-day. AWS, then GCS."""
    prefix = f"{datepath}/{st4}/"

    # AWS anonymous XML listing (currently denied; kept as first cheap try)
    try:
        r = requests.get(
            f"{_AWS_BASE}/?list-type=2&prefix={prefix}",
            headers=_HEADERS,
            timeout=30,
        )
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            out = []
            for contents in root.findall(f"{_S3_NS}Contents"):
                key = contents.find(f"{_S3_NS}Key").text
                filename = key.rsplit("/", 1)[-1]
                out.append((filename, f"{_AWS_BASE}/{key}"))
            return out
    except Exception:
        pass

    # GCS public mirror JSON listing (deep archive; stale for recent dates)
    out: list[tuple[str, str]] = []
    try:
        params: dict[str, str] = {"prefix": prefix, "maxResults": "1000"}
        while True:
            r = requests.get(
                _GCS_LIST, params=params, headers=_HEADERS, timeout=60
            )
            r.raise_for_status()
            payload = r.json()
            for item in payload.get("items", []):
                name = item["name"]
                filename = name.rsplit("/", 1)[-1]
                out.append((filename, f"{_GCS_DL}/{name}"))
            token = payload.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
    except Exception:
        return []
    return out


def _find_scans_thredds(st4: str, start: datetime, end: datetime) -> list[_ScanRef]:
    """Recent Level II volumes from UCAR THREDDS (rolling window)."""
    from siphon.radarserver import RadarServer, get_radarserver_datasets

    datasets = get_radarserver_datasets(_THREDDS_BASE)
    radar_ref = datasets[_THREDDS_L2_NAME]
    rs = RadarServer(radar_ref.follow().catalog_url)

    query = rs.query()
    query.stations(st4).time_range(start, end)
    catalog = rs.get_catalog(query)

    scans: list[_ScanRef] = []
    for name in sorted(catalog.datasets):
        ds = catalog.datasets[name]
        ts = _parse_time(name)
        if ts is None:
            continue
        try:
            url = ds.access_urls["HTTPServer"]
        except Exception:
            continue
        scans.append(_ScanRef(filename=name, scan_time=ts, download_url=url))
    return scans


# ---------------------------------------------------------------------------
# Download + decode + render
# ---------------------------------------------------------------------------
def _download_and_render(
    scan: _ScanRef, aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
    include_velocity: bool = True,
    overlay_aircraft: list | None = None,
    return_geo: bool = False,
):
    """Download one volume over HTTPS, render REF and VEL. With
    return_geo, returns (refl_png, vel_png, name, geo, px_box)."""
    from metpy.io import Level2File

    tmpdir = tempfile.mkdtemp(prefix="nexrad_l2_")
    try:
        local_path = os.path.join(tmpdir, scan.filename)
        with requests.get(
            scan.download_url, headers=_HEADERS, stream=True, timeout=300
        ) as r:
            r.raise_for_status()
            with open(local_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)

        f = Level2File(local_path)

        radar_lat = float(f.sweeps[0][0][1].lat)
        radar_lon = float(f.sweeps[0][0][1].lon)

        name = scan.filename

        az, rng_km, data = _extract_moment(f, b"REF")
        geo = px_box = None
        if return_geo:
            refl_png, geo, px_box = _render_sweep(
                az, rng_km, data, radar_lat, radar_lon,
                aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
                product="REF", title_prefix="Base Reflectivity (0.5°)",
                overlay_aircraft=overlay_aircraft,
                cbar_label="Reflectivity (dBZ)", volume_name=name,
                return_geo=True,
            )
        else:
            refl_png = _render_sweep(
                az, rng_km, data, radar_lat, radar_lon,
                aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
                product="REF", title_prefix="Base Reflectivity (0.5°)",
                overlay_aircraft=overlay_aircraft,
                cbar_label="Reflectivity (dBZ)", volume_name=name,
            )

        vel_png = b""
        if not include_velocity:
            if return_geo:
                return refl_png, vel_png, name, geo, px_box
            return refl_png, vel_png, name
        try:
            az_v, rng_km_v, data_v = _extract_moment(f, b"VEL")
            data_v = data_v * _MS_TO_KT
            vel_png = _render_sweep(
                az_v, rng_km_v, data_v, radar_lat, radar_lon,
                aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
                product="VEL", title_prefix="Base Velocity (0.5°)",
                cbar_label="Velocity (kt)", volume_name=name,
            )
        except Exception:
            vel_png = b""

        if return_geo:
            return refl_png, vel_png, name, geo, px_box
        return refl_png, vel_png, name
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _extract_moment(f, moment: bytes):
    """Pull (azimuths_deg, ranges_m, data) for the lowest sweep with moment."""
    for sweep in f.sweeps:
        if moment in sweep[0][4]:
            az = np.array([ray[0].az_angle for ray in sweep])
            hdr = sweep[0][4][moment][0]
            # NOTE: metpy Level2File gate_width/first_gate are KILOMETERS
            # (e.g. 0.25 km gates). Treating them as meters shrinks the
            # whole sweep to a ~460 m dot — invisible on the map.
            rng_km = np.arange(hdr.num_gates) * hdr.gate_width + hdr.first_gate
            data = np.array(
                [ray[4][moment][1] for ray in sweep], dtype=float
            )
            data = np.ma.masked_invalid(data)
            return az, rng_km, data
    raise ValueError(f"Moment {moment!r} not found in any sweep.")


def _render_sweep(
    az, rng_km, data, radar_lat, radar_lon,
    aircraft_lat, aircraft_lon, callsign, station, zoom_deg,
    product, title_prefix, cbar_label, volume_name,
    overlay_aircraft=None,
    return_geo: bool = False,
):
    """Render one sweep to PNG bytes with the shared BlueMet radar styling."""
    from metpy.calc import azimuth_range_to_lat_lon
    from metpy.plots import colortables
    from metpy.units import units
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    valid_frac = 1.0 - float(np.ma.getmaskarray(data).mean())

    lon_grid, lat_grid = azimuth_range_to_lat_lon(
        units.Quantity(az, "degrees"),
        units.Quantity(rng_km, "kilometers"),
        radar_lon,
        radar_lat,
    )

    if product == "REF":
        norm, cmap = colortables.get_with_steps(
            "NWSStormClearReflectivity", -20, 0.5
        )
    else:
        norm, cmap = colortables.get_with_steps("NWS8bitVel", -64, 0.5)

    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())

    ax.set_extent(
        [
            aircraft_lon - zoom_deg,
            aircraft_lon + zoom_deg,
            aircraft_lat - zoom_deg,
            aircraft_lat + zoom_deg,
        ],
        crs=ccrs.PlateCarree(),
    )

    mesh = ax.pcolormesh(
        lon_grid,
        lat_grid,
        data,
        cmap=cmap,
        norm=norm,
        shading="auto",
        transform=ccrs.PlateCarree(),
    )

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

    ax.scatter(
        aircraft_lon,
        aircraft_lat,
        s=180,
        marker="x",
        color="red",
        zorder=10,
        transform=ccrs.PlateCarree(),
    )

    # Live aircraft overlay: draw each plane with callsign + flight level
    if overlay_aircraft:
        for ac in overlay_aircraft:
            ax.scatter(
                ac.lon, ac.lat, s=90, marker="^", color="#0000CC",
                edgecolors="white", linewidths=0.8, zorder=11,
                transform=ccrs.PlateCarree(),
            )
            _lbl = ac.callsign
            if ac.alt_ft is not None:
                _lbl += f"\nFL{int(round(ac.alt_ft / 100)):03d}"
            ax.annotate(
                _lbl,
                xy=(ac.lon, ac.lat),
                xytext=(5, 5), textcoords="offset points",
                fontsize=7, fontweight="bold", color="#0000CC",
                zorder=11,
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
        f"{_station4(station)} {title_prefix}{echo_note}\n"
        f"Radar: {radar_lat:.2f}°, {radar_lon:.2f}°  ·  {volume_name}"
    )

    plt.colorbar(mesh, ax=ax, pad=0.02, label=cbar_label, shrink=0.8)

    # NOTE: no bbox_inches="tight" — it crops the GeoAxes away and
    # leaves only the colorbar on this matplotlib/cartopy combination.
    geo = px_box = None
    if return_geo:
        # Axes pixel box + geographic extent for later PIL compositing
        # (PlateCarree is linear in lon/lat so the mapping is affine).
        fig.canvas.draw()
        pos = ax.get_position()
        fw, fh = fig.get_size_inches()
        W, H = fw * 100, fh * 100  # dpi=100 below
        px_box = (pos.x0 * W, (1 - pos.y1) * H, pos.x1 * W, (1 - pos.y0) * H)
        west, east, south, north = ax.get_extent(crs=ccrs.PlateCarree())
        geo = (west, east, south, north)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    if return_geo:
        return buf.getvalue(), geo, px_box
    return buf.getvalue()


def fetch_and_render_base_frames(
    start_time: datetime,
    duration_min: int,
    center_lat: float,
    center_lon: float,
    label: str,
    station: str,
    zoom_deg: float,
) -> list[dict]:
    """Reflectivity base frames WITHOUT aircraft overlay, each carrying
    scan time and geo->pixel transform for later compositing."""
    end_time = start_time + timedelta(minutes=duration_min)
    scans = _find_scans(station, start_time, end_time)
    out: list[dict] = []
    for scan in scans:
        try:
            refl_png, _vel, name, geo, px_box = _download_and_render(
                scan, center_lat, center_lon, label, station, zoom_deg,
                include_velocity=False,
                overlay_aircraft=None,
                return_geo=True,
            )
        except Exception:
            continue
        out.append({
            "png": refl_png,
            "name": name,
            "scan_time": scan.scan_time,
            "geo": geo,
            "px": px_box,
        })
    out.sort(key=lambda d: d["scan_time"])
    return out


def composite_aircraft(png_bytes: bytes, geo, px_box, aircraft) -> bytes:
    """Draw aircraft triangles + labels onto a rendered frame with PIL."""
    from PIL import Image, ImageDraw

    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(im)
    west, east, south, north = geo
    x0, y0, x1, y1 = px_box

    def to_px(lon, lat):
        fx = (lon - west) / (east - west)
        fy = (north - lat) / (north - south)
        return x0 + fx * (x1 - x0), y0 + fy * (y1 - y0)

    for ac in aircraft or []:
        if not (west <= ac.lon <= east and south <= ac.lat <= north):
            continue
        cx, cy = to_px(ac.lon, ac.lat)
        r = 7
        draw.polygon(
            [(cx, cy - r), (cx - r * 0.8, cy + r * 0.7),
             (cx + r * 0.8, cy + r * 0.7)],
            fill=(0, 0, 204), outline=(255, 255, 255),
        )
        lbl = ac.callsign
        if ac.alt_ft is not None:
            lbl += f" FL{int(round(ac.alt_ft / 100)):03d}"
        draw.text((cx + 8, cy - 14), lbl, fill=(0, 0, 204))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
