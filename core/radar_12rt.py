"""Real-time Level II via the AWS chunk feed.

Bucket unidata-nexrad-level2-chunks receives each radar volume as
chunks WHILE the antenna scans: {SITE}/{VOL}/{YYYYMMDD-HHMMSS-CCC-T}
where VOL cycles 001-999 and T is S(tart), I(ntermediate), E(nd).
The 0.5 deg reflectivity sweep lives in the first few chunks, so a
near-live image is available ~1 minute into the volume instead of
after the full 4-6 minute scan.

Strategy:
  1. find_live_volume: coarse-sample volume dirs (chunk NAMES carry
     timestamps), then walk forward to the newest - a bounded ~20
     tiny list requests.
  2. fetch_live_chunks: list the volume's chunks, download in order,
     concatenate.
  3. parse_partial: metpy Level2File on the concatenation; on failure
     retry dropping the last chunk (truncation tolerance varies).

Callers should treat any exception as "fall back to the IDD path".
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree

import requests

_HEADERS = {"User-Agent": "BlueMet/1.0 (aviation weather tool)"}
BUCKET = "https://unidata-nexrad-level2-chunks.s3.amazonaws.com"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _list(prefix: str, delimiter: str = "", max_keys: int = 1000):
    """One S3 ListObjectsV2 call. Returns (keys, common_prefixes)."""
    params = {"list-type": "2", "prefix": prefix,
              "max-keys": str(max_keys)}
    if delimiter:
        params["delimiter"] = delimiter
    r = requests.get(BUCKET, params=params, headers=_HEADERS,
                     timeout=20)
    r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    keys = [el.text for el in root.iter(f"{_NS}Key")]
    prefixes = [el.text for el in root.iter(f"{_NS}Prefix")
                if el.text != prefix]
    return keys, prefixes


def _first_chunk_time(site: str, vol: str) -> Optional[datetime]:
    """Timestamp of a volume's first chunk, parsed from its NAME."""
    keys, _ = _list(f"{site}/{vol}/", max_keys=1)
    if not keys:
        return None
    name = keys[0].rsplit("/", 1)[-1]  # YYYYMMDD-HHMMSS-CCC-T
    try:
        return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def find_live_volume(site: str) -> tuple[str, datetime]:
    """Newest volume dir for a site. Volume numbers cycle 001-999 in
    time order, so coarse-sample then walk forward."""
    site = site.upper()
    _, vols = _list(f"{site}/", delimiter="/")
    vol_ids = sorted(
        p.split("/")[-2] for p in vols if p.split("/")[-2].isdigit()
    )
    if not vol_ids:
        raise RuntimeError(
            f"chunk feed: no volume dirs for {site} "
            f"(listed {len(vols)} prefixes)"
        )
    # Coarse sample every ~50th volume
    stride = max(1, len(vol_ids) // 20)
    sampled = vol_ids[::stride]
    best_vol, best_t = None, None
    for v in sampled:
        t = _first_chunk_time(site, v)
        if t and (best_t is None or t > best_t):
            best_vol, best_t = v, t
    if best_vol is None:
        raise RuntimeError(f"chunk feed: no readable chunks for {site}")
    # Walk forward (with wraparound) while newer volumes exist
    idx = vol_ids.index(best_vol)
    for _ in range(len(vol_ids)):
        nxt = vol_ids[(idx + 1) % len(vol_ids)]
        t = _first_chunk_time(site, nxt)
        if t is None or t <= best_t:
            break
        best_vol, best_t, idx = nxt, t, (idx + 1) % len(vol_ids)
    return best_vol, best_t


def fetch_live_chunks(site: str, vol: str) -> tuple[bytes, int, str]:
    """Download and concatenate the volume's chunks in order.
    Returns (bytes, n_chunks, newest_chunk_name)."""
    site = site.upper()
    keys, _ = _list(f"{site}/{vol}/")
    keys = sorted(keys)  # timestamped names: lexical == chronological
    if not keys:
        raise RuntimeError(f"chunk feed: volume {vol} has no chunks")
    parts = []
    for k in keys:
        r = requests.get(f"{BUCKET}/{k}", headers=_HEADERS, timeout=30)
        r.raise_for_status()
        parts.append(r.content)
    newest = keys[-1].rsplit("/", 1)[-1]
    return b"".join(parts), len(parts), newest


def parse_partial(raw_chunks: list[bytes]):
    """Level2File from concatenated chunks, dropping trailing chunks
    until the parse succeeds (partial-volume tolerance)."""
    from metpy.io import Level2File

    last_err: Exception = RuntimeError("no chunks")
    for upto in range(len(raw_chunks), 1, -1):
        blob = b"".join(raw_chunks[:upto])
        try:
            f = Level2File(io.BytesIO(blob))
            # Must contain at least one usable sweep
            if getattr(f, "sweeps", None):
                return f, upto
        except Exception as e:
            last_err = e
    raise RuntimeError(
        f"chunk feed: no parseable prefix of {len(raw_chunks)} chunks "
        f"(last error: {type(last_err).__name__}: {last_err})"
    )


def fetch_live_volume_bytes(site: str) -> tuple[bytes, dict]:
    """High-level: locate the live volume, fetch chunks, verify a
    parseable prefix, and return (bytes_for_Level2File, info).

    info: {volume, n_chunks, n_used, newest_chunk, chunk_time,
           age_s}
    """
    vol, start_t = find_live_volume(site)
    site = site.upper()
    keys, _ = _list(f"{site}/{vol}/")
    keys = sorted(keys)
    if not keys:
        raise RuntimeError(f"chunk feed: volume {vol} empty")
    parts = []
    for k in keys:
        r = requests.get(f"{BUCKET}/{k}", headers=_HEADERS, timeout=30)
        r.raise_for_status()
        parts.append(r.content)
    _f, upto = parse_partial(parts)
    newest = keys[upto - 1].rsplit("/", 1)[-1]
    try:
        chunk_t = datetime.strptime(
            newest[:15], "%Y%m%d-%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        chunk_t = start_t
    age = (datetime.now(timezone.utc) - chunk_t).total_seconds()
    info = {
        "volume": vol,
        "n_chunks": len(parts),
        "n_used": upto,
        "newest_chunk": newest,
        "chunk_time": chunk_t.isoformat(),
        "age_s": int(age),
    }
    return b"".join(parts[:upto]), info