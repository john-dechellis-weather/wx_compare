"""Background fetch of every JetBlue station's airport diagram.

Target path: core/surface_warm.py
Started from Homepage.py, never from a page (a warmer started by its
own page only runs while someone is looking at that page).

One Overpass query per station, PACE_S apart, then a pause of
CYCLE_S before checking again. Each surface is re-fetched only once
its cache is older than a week, so after the first pass this is a
handful of requests a day. A 429 from Overpass backs off rather than
retrying on the spot.

The coverage report - runways, taxiways, gates and terminals per
station - is written to surface_coverage.json beside the surfaces, and
the tail of the log is what the Homepage "Background warmers"
expander shows. Thin OpenStreetMap coverage shows up there, not on
the scope in front of someone.

Kill switch: SURFACE_WARMER=off.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

PACE_S = float(os.environ.get("SURFACE_WARM_PACE_S", "6"))
BACKOFF_S = 120.0
CYCLE_S = 6 * 3600
START_DELAY_S = 90          # let the MRMS and CAM warmers start first

_STARTED = {"t": None}
_LOCK = threading.Lock()


def _log(outdir: Path, msg: str) -> None:
    try:
        p = outdir / "surface_warm.log"
        line = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()) + "  " + msg
        lines = p.read_text().splitlines()[-400:] if p.exists() else []
        lines.append(line)
        p.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def tail(outdir, n: int = 12) -> list:
    p = Path(outdir) / "surface_warm.log"
    try:
        return p.read_text().splitlines()[-n:]
    except Exception:
        return []


def _pass(outdir: Path) -> dict:
    from core import airports as AP, surface as SF

    cov = {}
    fetched = failed = 0
    for icao in AP.stations():
        if os.environ.get("SURFACE_WARMER", "on").lower() == "off":
            _log(outdir, "stopped: SURFACE_WARMER=off")
            break
        c = AP.centre(icao)
        if not c:
            continue
        code = AP.code(icao)
        SF.register(code, c[0], c[1])
        p = SF._path(outdir, code)
        fresh = p.exists() and time.time() - p.stat().st_mtime < SF.SURFACE_MAX_AGE_S
        if not fresh:
            try:
                d = SF.fetch(code=code)
                if d.get("runways"):
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(d))
                    fetched += 1
                else:
                    _log(outdir, f"{icao}: Overpass returned no runways; kept old")
                    failed += 1
            except Exception as exc:
                failed += 1
                name = type(exc).__name__
                code_ = getattr(getattr(exc, "response", None), "status_code", None)
                _log(outdir, f"{icao}: {name}" + (f" {code_}" if code_ else ""))
                if code_ == 429:
                    time.sleep(BACKOFF_S)
            time.sleep(PACE_S)
        d = SF.load(outdir, code)
        cov[icao] = {k: len(d.get(k) or []) for k in
                     ("runways", "taxiways", "gates", "terminals", "aprons")}
    try:
        (outdir / "surface_coverage.json").write_text(json.dumps(cov, indent=0))
    except Exception:
        pass
    have = sum(1 for v in cov.values() if v["runways"])
    thin = sorted(k for k, v in cov.items() if v["runways"] and not v["taxiways"])
    _log(outdir, f"pass done: {have}/{len(cov)} stations have a diagram; "
                 f"{fetched} fetched, {failed} failed"
                 + (f"; runways but no taxiways: {', '.join(thin)}" if thin else ""))
    return cov


def _loop(outdir: Path) -> None:
    time.sleep(START_DELAY_S)
    while True:
        try:
            _pass(outdir)
        except Exception as exc:
            _log(outdir, f"pass failed: {type(exc).__name__}: {exc}")
        time.sleep(CYCLE_S)


def ensure_surface_warmer(outdir) -> bool:
    """Idempotent: starts the thread once per process."""
    if os.environ.get("SURFACE_WARMER", "on").lower() == "off":
        return False
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if _STARTED["t"] is not None and _STARTED["t"].is_alive():
            return True
        t = threading.Thread(target=_loop, args=(outdir,), daemon=True,
                             name="surface-warmer")
        t.start()
        _STARTED["t"] = t
    return True


def coverage(outdir) -> dict:
    try:
        return json.loads((Path(outdir) / "surface_coverage.json").read_text())
    except Exception:
        return {}
