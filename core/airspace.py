"""Shared airspace geometry for the N90 pages.

Lifted out of pages/1_Airspace.py so the forecast page draws the same
fixes, Class B shelves, routes and ARTCC boundaries without a second
copy of the loading code.

NO STREAMLIT IMPORT ON PURPOSE. The caching decorator belongs to
whichever page calls this, not to the data layer — a core module that
imports streamlit cannot be tested or reused anywhere else.

Page 1 still carries its own copy. Pointing it here is a one-line
change once this is proven on page 2, and worth doing, but not worth
risking a working page on the same upload.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent / "static"

N90_CENTER = (40.6398, -73.7789)


def _json(name):
    """Load a static asset. Returns (data, error) — never raises into
    the page, because one missing asset should not take the map down."""
    import json

    try:
        return json.loads((_STATIC / name).read_text()), None
    except Exception as exc:
        return None, f"{name}: {type(exc).__name__}: {exc}"


def _nav_icon(kind):
    from core.icons import nav_icon
    return nav_icon(kind)


def _nav_kind(name, navtype=None):
    from core.icons import nav_kind
    return nav_kind(name, navtype)


def load_airspace():
    """All static airspace assets in one cached call."""
    out = {"errors": []}
    fx, err = _json("n90_fixes.json")
    if err:
        out["errors"].append(err)
    else:
        # GREEN departure gate | YELLOW arrival AND departure |
        # WHITE coordination fix. Data-driven: vice lists gates under
        # airspace_awareness and boundary crossings under
        # coordination_fixes; fixes in both work traffic both ways.
        # Two colour sets. The triangle keeps the bright role colour
        # so it reads against the basemap; the LABEL uses a darkened
        # version, because a light plate needs dark text. Bright
        # green or white on light grey is unreadable, so switching
        # the plate without darkening the text would have traded one
        # legibility problem for another.
        # Scheme K. Three pieces per class: a muted TRIANGLE, a
        # pastel CHIP behind the label, and dark text on it. No chip
        # border — dropping it is what makes this read calm rather
        # than decorated, and the fills stay distinct enough without.
        #
        # Class mapping, inferred from the vice adaptation rather
        # than stated by it: ARRIVAL = coordination_fixes, which
        # carry traffic INTO N90 from a centre. DEPARTURE =
        # airspace_awareness only. OTHER = the seven in both lists.
        #
        # Text is dark enough to clear 4.5 contrast on its own chip —
        # measured 9.0 arrival, 8.4 departure, 8.8 other — while the
        # chips themselves sit at ~1.1 against the basemap, which is
        # the point: visible, not shouting.
        tri  = {"coord": [76, 139, 63], "dep": [180, 99, 90]}
        txt  = {"coord": [27, 67, 50], "dep": [107, 31, 31]}
        chip = {"coord": [216, 240, 192], "dep": [247, 214, 214]}
        lbl  = {"coord": "arrival gate", "dep": "departure gate"}
        # THE OPERATIONAL NINE. The gate outlook's lists decide the
        # role of the four arrival gates and five departure fixes —
        # ARD and IGN are arrival gates in practice whatever the file
        # says — and those nine are flagged `key`: always labelled,
        # drawn a step larger, never thinned.
        try:
            from core.gate_impact import ARRIVAL_GATES, DEPARTURE_FIXES
        except Exception:
            ARRIVAL_GATES, DEPARTURE_FIXES = (), ()
        _role_override = {**{n: "coord" for n in ARRIVAL_GATES},
                          **{n: "dep" for n in DEPARTURE_FIXES}}
        fixes = fx.get("fixes", [])
        for f in fixes:
            if f["name"] in _role_override:
                f["role"] = _role_override[f["name"]]
        _key = set(ARRIVAL_GATES) | set(DEPARTURE_FIXES)
        out["fixes"] = [{
            "name": f["name"], "lat": f["lat"], "lon": f["lon"],
            "key": f["name"] in _key,
            "nsize": 7000 if f["name"] in _key else 5200,
            # Carried through as-is: coord = arrival gate, dep =
            # departure fix, both. Pages that draw only arrival gates
            # (the gate status page) filter on it.
            "role": f.get("role"),
            "tcolor": tri.get(f.get("role"), [160, 139, 60]),
            # chart glyph: RNAV waypoint star, VOR/DME, VORTAC, NDB
            "nav": _nav_icon(_nav_kind(f["name"], f.get("navtype"))),
            "navkind": _nav_kind(f["name"], f.get("navtype")),
            "lcolor": txt.get(f.get("role"), [74, 59, 18]),
            "chip": chip.get(f.get("role"), [239, 231, 198]) + [238],
            "tip": (f"{f['name']} &mdash; "
                    + lbl.get(f.get("role"), "other nav aid")
                    + (f", from {f['from']}" if f.get("from") else "")
                    + f" ({f.get('dist_nm', '?')} nm)"),
        } for f in fixes]
        out["hull_fixes"] = [{"polygon": fx.get("hull_fixes") or fx.get("hull", [])}]
        out["hull"] = [{"polygon": fx.get("hull", []),
                        "tip": "N90 approximate extent (hull of "
                               "coordination fixes) &mdash; NOT the "
                               "delegated TRACON boundary"}]
        out["fix_vintage"] = fx.get("extracted", "?")

    cb, err = _json("ny_class_b.json")
    if err:
        out["errors"].append(err)
    else:
        out["classb"] = [{
            "polygon": a["polygon"],
            "tip": f"NY Class B {a['name']} &mdash; "
                   f"{a['low']} to {a['high']} ft",
        } for a in cb.get("areas", [])]

    rt, err = _json("n90_routes.json")
    if err:
        out["errors"].append(err)
    else:
        # Colour carries DIRECTION, from the FAA's own field — not a
        # guess from geometry. GREEN eastbound, RED westbound, AMBER
        # where the FAA lists no direction.
        #
        # AMBER MEANS "THE DATA DOES NOT SAY", NOT "BALANCED". Most of
        # these routes are bidirectional by design and colouring them
        # inbound or outbound would invent information. Green matches
        # the arrival-gate fix chips and the NY-bound fleet, so green
        # means "toward New York" everywhere on this map.
        dcol = {"E": [40, 130, 70, 205],
                "W": [190, 55, 45, 205],
                "BOTH": [150, 115, 45, 165]}
        dname = {"E": "eastbound only", "W": "westbound only",
                 "BOTH": "no published direction"}
        out["routes"] = [{
            "path": c["path"],
            "ident": c["ident"],
            "color": dcol.get(c.get("dir"), dcol["BOTH"]),
            "tip": f"{c['ident']} &mdash; {c.get('type') or 'ATS route'}"
                   f", {dname.get(c.get('dir'), 'unknown')}",
        } for c in rt.get("routes", [])]
        out["route_labels"] = [{
            "lon": L["lon"], "lat": L["lat"],
            "ident": L["ident"],
            "angle": L.get("angle", 0.0),
            "color": dcol.get(L.get("dir"), dcol["BOTH"])[:3],
        } for L in rt.get("labels", [])]

    oc, err = _json("n90_oceanic.json")
    if err:
        out["errors"].append(err)
    else:
        # ONE colour, no direction split. Oceanic L-routes are drawn
        # as a family distinct from the domestic J/Q set, and the
        # slate blue is unused elsewhere on either map. Direction is
        # carried in the tooltip only.
        oc_col = [70, 100, 130, 210]
        out["oceanic"] = [{
            "path": c["path"], "ident": c["ident"], "color": oc_col,
            "tip": f"{c['ident']} &mdash; oceanic "
                   f"{c.get('type') or 'route'}",
        } for c in oc.get("routes", [])]
        out["oceanic_labels"] = [{
            "lon": L["lon"], "lat": L["lat"], "ident": L["ident"],
            "angle": L.get("angle", 0.0), "color": oc_col[:3],
        } for L in oc.get("labels", [])]

    ar, err = _json("artcc_high.json")
    if err:
        out["errors"].append(err)
    else:
        out["artcc"] = [{
            "polygon": c["polygon"],
            "tip": f"{c['ident']} &mdash; {c['name']} Center (high)",
        } for c in ar.get("centers", [])]
    return out
