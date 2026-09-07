"""Gate impact from the REFS forecast: when each arrival gate closes
to thunderstorms and when it opens again.

WHAT IS SAMPLED. Every hour of the newest REFS cycle, at the moment
the overlay warmer holds the decoded field, two things are read at
every arrival gate:

  * PMMN composite reflectivity — the maximum within RADIUS_NM of
    the gate. PMMN keeps realistic cell intensity while averaging
    placement, so "a 45 dBZ cell on the CAMRN corridor at 21Z" is a
    question it can answer; an ensemble mean would smear it to 25.
  * PROB_REFC40 — the maximum within the same radius: the share of
    members that put a > 40 dBZ cell there. That is the confidence.

No extra fetch: the warmer already has the array. One JSON per cycle.

WHAT "IMPACTED" MEANS. Two tiers on the sampled maximum:
  CLOSED    >= 40 dBZ — the line pilots deviate around and SWAP triggers on
  MARGINAL  30-40 dBZ
An episode opens at the first hour at or above a tier and closes at
the first hour that has been clear of it for CLEAR_HOURS running, so
a single clear hour between two storms does not read as "open".

A 10 nm radius rather than a point: a gate is a point, traffic is a
corridor, and a 3 km model's cells are small enough that one grid
cell would flicker hour to hour.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

RADIUS_NM = 10.0
CLOSED_DBZ = 40.0
MARGINAL_DBZ = 30.0
CLEAR_HOURS = 2
_LOCK = threading.Lock()


# THE FIXES THE OUTLOOK COVERS: the ones the operation actually runs
# through, chosen by the user, not everything in the fix file. Four
# arrival gates and five departure fixes. The roles here override the
# fix file's — ARD and IGN are arrival gates in practice whatever the
# file says. Edit these two lists to change the table.
ARRIVAL_GATES = ("CAMRN", "ARD", "LENDY", "IGN")
DEPARTURE_FIXES = ("WHITE", "WAVEY", "RBV", "COATE", "GREKI")
ROUTE_ROWS = ("Q818/Q436", "J64/J60", "Q42/Q480", "J6", "J48/Q75", "Q133/Q109")


# JET ROUTES, in pairs as the operation names them. A row samples
# every vertex of its member routes that lies within ROUTE_REACH_NM
# of the N90 outline — the approach corridor, where a closed airway
# actually costs something — and reports the worst of them. Routes
# missing from the route file (J64, Q42, Q480 as of this writing)
# contribute nothing; a row with no member in reach shows no data.
ROUTE_GROUPS = (("Q818", "Q436"), ("J64", "J60"), ("Q42", "Q480"),
                ("J6",), ("J48", "Q75"), ("Q133", "Q109"))
ROUTE_REACH_NM = 75.0


def _route_points(static_dir) -> dict:
    """{row_label: [(lon, lat), ...]} of route vertices in reach."""
    try:
        d = Path(static_dir)
        routes = {r["ident"]: r["path"] for r in
                  json.loads((d / "n90_routes.json").read_text())["routes"]}
        hull = json.loads((d / "n90_fixes.json").read_text()).get("hull") or []
    except Exception:
        return {}
    if not hull:
        return {}
    k = math.cos(math.radians(40.7))

    def d_edge(q):
        best = 1e9
        for i in range(len(hull)):
            a, b = hull[i], hull[(i + 1) % len(hull)]
            ax, ay = (a[0] - q[0]) * 60 * k, (a[1] - q[1]) * 60
            bx, by = (b[0] - q[0]) * 60 * k, (b[1] - q[1]) * 60
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
        return best

    out = {}
    for grp in ROUTE_GROUPS:
        pts = []
        for ident in grp:
            for q in routes.get(ident, []):
                if d_edge(q) <= ROUTE_REACH_NM:
                    pts.append((q[0], q[1]))
        out["/".join(grp)] = pts
    return out


def _gates(static_dir) -> list:
    """The outlook fixes with positions from the fix file and roles
    from the lists above. Every sampled fix is still recorded in the
    impact file; only these are displayed."""
    try:
        fx = {f["name"]: f for f in json.loads(
            (Path(static_dir) / "n90_fixes.json").read_text())["fixes"]}
    except Exception:
        return []
    out = []
    for name in ARRIVAL_GATES:
        if name in fx:
            out.append(dict(fx[name], role="arr"))
    for name in DEPARTURE_FIXES:
        if name in fx:
            out.append(dict(fx[name], role="dep"))
    return out


def _path(outdir, cycle, source: str = "refs") -> Path:
    c = cycle.strftime("%Y%m%d%H") if hasattr(cycle, "strftime") else str(cycle)
    # refs keeps its original name so files already on disk still load
    stem = "refs_impact" if source == "refs" else f"impact_{source}"
    return Path(outdir) / f"{stem}_{c}.json"


def sample(vals, lats, lons, product: str, cycle, fhr: int, outdir,
           static_dir, source: str = "refs") -> int:
    """Record the maximum of `vals` within RADIUS_NM of every gate for
    this forecast hour. `product` is REFC / REFD (dBZ) or PROB_REFC40
    (%). `source` names the file: refs or rrfs. Returns the number of
    gates sampled. Never raises."""
    try:
        import numpy as np
        from scipy.spatial import cKDTree

        gates = _gates(static_dir)
        if not gates:
            return 0
        la = np.asarray(lats, dtype="float64").ravel()
        lo = np.asarray(lons, dtype="float64").ravel()
        v = np.asarray(vals, dtype="float32").ravel()
        good = np.isfinite(v) & np.isfinite(la) & np.isfinite(lo)
        if not good.any():
            return 0
        la, lo, v = la[good], lo[good], v[good]
        # Local flat metric in nautical miles: x scaled by cos(lat).
        k = math.cos(math.radians(float(la.mean())))
        tree = cKDTree(np.column_stack([lo * 60.0 * k, la * 60.0]))
        key = "prob40" if product.startswith("PROB") else "dbz"
        out = {}
        for g in gates:
            idx = tree.query_ball_point([g["lon"] * 60.0 * k, g["lat"] * 60.0],
                                        RADIUS_NM)
            if not idx:
                _, i = tree.query([g["lon"] * 60.0 * k, g["lat"] * 60.0])
                idx = [i]
            out[g["name"]] = round(float(v[idx].max()), 1)
        # Routes: the worst cell along the approach corridor.
        rout = {}
        for label, pts in _route_points(static_dir).items():
            if not pts:
                continue
            best = None
            for lo_, la_ in pts:
                idx = tree.query_ball_point([lo_ * 60.0 * k, la_ * 60.0], RADIUS_NM)
                if idx:
                    m = float(v[idx].max())
                    best = m if best is None else max(best, m)
            if best is not None:
                rout[label] = round(best, 1)
        p = _path(outdir, cycle, source)
        with _LOCK:
            try:
                d = json.loads(p.read_text()) if p.exists() else {}
            except Exception:
                d = {}
            d.setdefault("cycle", cycle.strftime("%Y%m%d%H")
                         if hasattr(cycle, "strftime") else str(cycle))
            d.setdefault("source", source)
            d.setdefault("radius_nm", RADIUS_NM)
            d.setdefault("gates", {})
            roles = {g["name"]: g.get("role") for g in gates}
            for name, val in out.items():
                ent = d["gates"].setdefault(name, {})
                ent["role"] = roles.get(name)
                ent.setdefault(key, {})[str(fhr)] = val
            d.setdefault("routes", {})
            for label, val in rout.items():
                d["routes"].setdefault(label, {}).setdefault(key, {})[str(fhr)] = val
            p.write_text(json.dumps(d))
        return len(out)
    except Exception:
        return 0


def load(outdir, cycle=None, source: str = "refs") -> dict:
    """The impact file for `cycle` and `source`, or the newest."""
    d = Path(outdir)
    if cycle:
        p = _path(d, cycle, source)
        files = [p] if p.exists() else []
    else:
        stem = "refs_impact" if source == "refs" else f"impact_{source}"
        files = sorted(d.glob(f"{stem}_*.json"))
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return {}


def tier(dbz, marginal: float = MARGINAL_DBZ, closed: float = CLOSED_DBZ) -> int:
    """0 clear, 1 marginal, 2 closed."""
    if dbz is None:
        return 0
    if dbz >= closed:
        return 2
    if dbz >= marginal:
        return 1
    return 0


def timeline(impact: dict, hours: int = 60, marginal: float = MARGINAL_DBZ,
             closed: float = CLOSED_DBZ) -> dict:
    """Per gate: an hourly tier list and the episodes it contains.

    Returns {"cycle", "hours": [fhr...], "gates": {name: {
        "tiers": [0/1/2 per hour], "dbz": [...], "prob40": [...],
        "episodes": [{"onset_fhr", "end_fhr", "hours", "closed_from",
                      "closed_to", "closed_hours", "tier",
                      "peak_dbz", "max_prob40"}]}}}
    One episode per storm: from the first marginal hour to the first
    hour after CLEAR_HOURS clear ones; the closed span inside it, if
    any, is the >= 40 dBZ hours. Still open at the last hour reports
    end_fhr None.
    """
    gates = dict(impact.get("gates") or {})
    # Routes ride the same machinery under their row labels; the page
    # tells them apart by asking for ROUTE_GROUPS labels.
    for label, r in (impact.get("routes") or {}).items():
        gates[label] = dict(r, role="route")
    fhrs = sorted({int(h) for g in gates.values()
                   for h in (g.get("dbz") or {})})
    fhrs = [h for h in fhrs if h <= hours]
    out = {"cycle": impact.get("cycle"), "source": impact.get("source", "refs"),
           "hours": fhrs, "gates": {}, "marginal": marginal, "closed": closed}
    for name, g in gates.items():
        dbz = [g.get("dbz", {}).get(str(h)) for h in fhrs]
        prob = [g.get("prob40", {}).get(str(h)) for h in fhrs]
        tiers = [tier(v, marginal, closed) for v in dbz]
        # ONE WINDOW PER STORM, the way a briefer would say it: impact
        # begins at the first marginal hour, the closed span is the
        # first-to-last >= 40 dBZ hour inside it, it ends when the
        # gate has been clear CLEAR_HOURS running. A window with no
        # closed span is marginal only.
        episodes = []
        i = 0
        while i < len(fhrs):
            if tiers[i] >= 1:
                j = i
                while j < len(fhrs):
                    nxt = tiers[j + 1:j + 1 + CLEAR_HOURS]
                    if len(nxt) == CLEAR_HOURS and all(t < 1 for t in nxt):
                        break
                    j += 1
                j = min(j, len(fhrs) - 1)
                seg = list(range(i, j + 1))
                shut = [x for x in seg if tiers[x] == 2]
                episodes.append({
                    "onset_fhr": fhrs[i],
                    "end_fhr": (fhrs[j + 1] if j + 1 < len(fhrs) else None),
                    "hours": len(seg),
                    "closed_from": fhrs[shut[0]] if shut else None,
                    "closed_to": fhrs[shut[-1]] if shut else None,
                    "closed_hours": len(shut),
                    "tier": 2 if shut else 1,
                    "peak_dbz": max((dbz[x] for x in seg if dbz[x] is not None),
                                    default=None),
                    "max_prob40": max((prob[x] for x in seg if prob[x] is not None),
                                      default=None),
                })
                i = j + 1
            else:
                i += 1
        keep = episodes
        out["gates"][name] = {"tiers": tiers, "dbz": dbz, "prob40": prob,
                              "episodes": keep, "role": g.get("role")}
    return out
