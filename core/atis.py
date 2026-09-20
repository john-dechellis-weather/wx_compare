"""FAA Digital ATIS — the authoritative runway configuration.

WHY THIS EXISTS. core/runways.py infers the configuration from what
aircraft are doing: aligned and low before a threshold is an arrival,
past the far end climbing is a departure. That works with no external
feed at all, but it is a reading of traffic, it needs 15 minutes of
it, and it says nothing during a lull. The D-ATIS says outright which
approach is in use and which runway is departing, straight from the
tower, and changes the moment the tower changes it.

SOURCE. https://datis.clowd.io/api/<ICAO> — the JSON API behind
atis.info and datis.clowd.io (the same site: atis.info renders it in
the browser, which is why fetching that page returns no text). It
carries the FAA SWIM D-ATIS feed. NOT an FAA address: it is a third
party relaying the SWIM data, so treat it as very good information
rather than as an official source, which is the same standing the
rest of this app's feeds have.

The API returns a list. Airports that split arrival and departure
ATIS return two entries (type "arr" and "dep"); the rest return one
("combined"). Each entry has `airport`, `type`, `code` (the phonetic
letter) and `datis` (the full text).

PARSING. The text is fixed-format enough to read the runways out of:

    ... APPROACH IN USE ILS RY 4R, ILS RY 4L. DEPG RY 4L. NOTAMS ...
    ... APCH IN USE ILS RY 22L, ILS 22R. DEPTG RY 22R... BIRD ...
    ... APPROACH IN USE RNAV GPS Z RY 13L. DEPG RY 13R.

so: take the span after the approach phrase up to the next period,
and the span after the departure phrase up to the next period, and
pull the runway identifiers out of each. The phrases vary by field
and by controller (APPROACH/APCH, DEPG/DEPTG/DEPARTING, RY/RWY), so
all of the spellings seen in the wild are matched.

NOT VERIFIED FROM THE SANDBOX: datis.clowd.io is not reachable from
the build container, so the fetch path is untested against the live
API. The PARSER is tested — against real KJFK D-ATIS text captured
from three different relays, in tests at the bottom of this file.
"""
from __future__ import annotations

import re
import threading
import time

URL = "https://datis.clowd.io/api/{icao}"
TTL_S = 120.0          # the ATIS changes hourly; the code letter sooner
TIMEOUT_S = 6.0

_LOCK = threading.Lock()
_CACHE: dict = {}

# "ILS RY 4R", "RY 22L", "RWY 13R", "ILS 22R" (no RY at all, seen at JFK)
_RWY = re.compile(r"\b(?:R(?:W)?Y\s*)?(\d{1,2}[LRC]?)\b")
_APCH = re.compile(r"\b(?:APPROACH|APCH|APP)\s+IN\s+USE\b(.*?)(?:\.|$)", re.S)
_DEP = re.compile(r"\b(?:DEPG|DEPTG|DEPARTING|DEPARTURE|DEP)\s+(?:R(?:W)?Y\s*)?(.*?)(?:\.|$)", re.S)


def _runways(span: str) -> list:
    """Runway identifiers in order of first appearance, de-duplicated.

    Guards against swallowing the numbers that are not runways: an
    approach span like "RNAV GPS Z RY 13L" has a Z in it, and "ILS RY
    4R, ILS RY 4L" repeats the word ILS. Anything that is not 1-2
    digits with an optional L/R/C is dropped by the pattern itself.
    """
    out = []
    for m in _RWY.finditer(span or ""):
        r = m.group(1).upper()
        # a bare number over 36 is not a runway (it is a frequency,
        # an altimeter, a temperature)
        try:
            if int(re.sub(r"[LRC]$", "", r)) > 36:
                continue
        except ValueError:
            continue
        if r not in out:
            out.append(r)
    return out


def parse(text: str) -> dict:
    """{"arr": [...], "dep": [...], "code": "A"} from one D-ATIS string."""
    t = (text or "").upper()
    code = ""
    m = re.search(r"\bATIS\s+INFO\s+([A-Z])\b", t) or re.search(r"\bINFORMATION\s+([A-Z])\b", t)
    if m:
        code = m.group(1)
    arr = _runways(_APCH.search(t).group(1)) if _APCH.search(t) else []
    dep = _runways(_DEP.search(t).group(1)) if _DEP.search(t) else []
    return {"arr": arr, "dep": dep, "code": code}


def config(icao: str = "KJFK") -> dict:
    """{"arr", "dep", "code", "raw", "age_s", "err"} for one airport.

    Never raises. On any failure `err` is set and arr/dep are empty,
    which the caller should read as "no ATIS" and fall back to the
    movement-derived configuration rather than as "no runways in use".
    """
    key = icao.upper()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit["at"] < TTL_S:
            return dict(hit["val"], age_s=int(now - hit["at"]))
    out = {"arr": [], "dep": [], "code": "", "raw": "", "age_s": 0, "err": ""}
    try:
        import requests

        r = requests.get(URL.format(icao=key), timeout=TIMEOUT_S,
                         headers={"User-Agent": "N90-Weather (ops display)"})
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, dict):
            rows = [rows]
        # Merge every entry: a field with split ATIS gives the
        # approach in the "arr" one and the departure in the "dep"
        # one, so taking only the first would lose half of it.
        raw = []
        for row in rows or []:
            txt = (row or {}).get("datis") or ""
            if not txt:
                continue
            raw.append(txt)
            p = parse(txt)
            for k in ("arr", "dep"):
                for rwy in p[k]:
                    if rwy not in out[k]:
                        out[k].append(rwy)
            out["code"] = out["code"] or p["code"]
        out["raw"] = "\n\n".join(raw)
        if not raw:
            out["err"] = "no ATIS published"
    except Exception as exc:
        out["err"] = f"{type(exc).__name__}"
    with _LOCK:
        _CACHE[key] = {"at": now, "val": dict(out)}
    return out


def norm(rwy: str) -> str:
    """'4R' -> '04R'. core/runways.py zero-pads its end names (it
    formats them '%02d'), the ATIS does not, so anything compared
    against a runway end has to go through here first."""
    m = re.match(r"^(\d{1,2})([LRC]?)$", (rwy or "").strip().upper())
    return f"{int(m.group(1)):02d}{m.group(2)}" if m else (rwy or "").strip().upper()


def ends(cfg: dict, which: str) -> set:
    """The arr or dep runways as zero-padded end names."""
    return {norm(r) for r in (cfg or {}).get(which) or []}


def describe(cfg: dict) -> str:
    """'landing 22L, 22R - departing 22R (ATIS A)' or '' if nothing."""
    if not cfg or (not cfg.get("arr") and not cfg.get("dep")):
        return ""
    bits = []
    if cfg.get("arr"):
        bits.append("landing " + ", ".join(cfg["arr"]))
    if cfg.get("dep"):
        bits.append("departing " + ", ".join(cfg["dep"]))
    s = " \u00b7 ".join(bits)
    return s + (f" (ATIS {cfg['code']})" if cfg.get("code") else "")
