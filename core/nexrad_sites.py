"""WSR-88D site catalog + nearest-site lookup.

Coordinates are approximate (2-decimal, ~1 km) — plenty for choosing the
nearest radar. The Flight Tracker page has a manual override for any
case where the auto-pick is wrong (terrain blockage, catalog typo).
"""
from __future__ import annotations

import math

# site_id: (lat, lon)
NEXRAD_SITES: dict[str, tuple[float, float]] = {
    # Northeast
    "KOKX": (40.87, -72.86), "KDIX": (39.95, -74.41), "KBOX": (41.96, -71.14),
    "KENX": (42.59, -74.06), "KBGM": (42.20, -75.98), "KTYX": (43.76, -75.68),
    "KBUF": (42.95, -78.74), "KCXX": (44.51, -73.17), "KGYX": (43.89, -70.26),
    "KCBW": (46.04, -67.81), "KLWX": (38.98, -77.48), "KDOX": (38.83, -75.44),
    "KPBZ": (40.53, -80.22), "KCCX": (40.92, -78.00),
    # Southeast
    "KAKQ": (36.98, -77.01), "KRAX": (35.67, -78.49), "KMHX": (34.78, -76.88),
    "KLTX": (33.99, -78.43), "KGSP": (34.88, -82.22), "KCAE": (33.95, -81.12),
    "KCLX": (32.66, -81.04), "KFFC": (33.36, -84.57), "KJGX": (32.68, -83.35),
    "KVAX": (30.89, -83.00), "KJAX": (30.48, -81.70), "KSGF": (37.24, -93.40),
    "KMLB": (28.11, -80.65), "KTBW": (27.71, -82.40), "KAMX": (25.61, -80.41),
    "KBYX": (24.60, -81.70), "KEVX": (30.56, -85.92), "KTLH": (30.40, -84.33),
    "KMOB": (30.68, -88.24), "KBMX": (33.17, -86.77), "KMXX": (32.54, -85.79),
    "KEOX": (31.46, -85.46), "KHTX": (34.93, -86.08), "KGWX": (33.90, -88.33),
    "KDGX": (32.28, -89.98), "KLIX": (30.34, -89.83), "KPOE": (31.16, -92.98),
    "KSHV": (32.45, -93.84), "KLCH": (30.13, -93.22),
    # Mid-South / Ohio Valley
    "KOHX": (36.25, -86.56), "KNQA": (35.34, -89.87), "KMRX": (36.17, -83.40),
    "KJKL": (37.59, -83.31), "KLVX": (37.98, -85.94), "KPAH": (37.07, -88.77),
    "KHPX": (36.74, -87.29), "KVWX": (38.26, -87.72), "KIND": (39.71, -86.28),
    "KILN": (39.42, -83.82), "KCLE": (41.41, -81.86), "KDTX": (42.70, -83.47),
    "KGRR": (42.89, -85.54), "KAPX": (44.91, -84.72), "KMQT": (46.53, -87.55),
    "KIWX": (41.36, -85.70), "KLOT": (41.60, -88.08), "KILX": (40.15, -89.34),
    "KDVN": (41.61, -90.58), "KMKX": (42.97, -88.55), "KGRB": (44.50, -88.11),
    "KARX": (43.82, -91.19), "KMPX": (44.85, -93.57), "KDLH": (46.84, -92.21),
    "KRLX": (38.31, -81.72), "KFCX": (37.02, -80.27),
    # Plains
    "KEAX": (38.81, -94.26), "KTWX": (38.99, -96.23), "KICT": (37.65, -97.44),
    "KDDC": (37.76, -99.97), "KGLD": (39.37, -101.70), "KUEX": (40.32, -98.44),
    "KLNX": (41.96, -100.58), "KOAX": (41.32, -96.37), "KDMX": (41.73, -93.72),
    "KFSD": (43.59, -96.73), "KABR": (45.46, -98.41), "KUDX": (44.12, -102.83),
    "KBIS": (46.77, -100.76), "KMVX": (47.53, -97.33), "KMBX": (48.39, -100.86),
    "KTLX": (35.33, -97.28), "KINX": (36.18, -95.56), "KVNX": (36.74, -98.13),
    "KFDR": (34.36, -98.98), "KSRX": (35.29, -94.36), "KLZK": (34.84, -92.26),
    "KFWS": (32.57, -97.30), "KGRK": (30.72, -97.38), "KEWX": (29.70, -98.03),
    "KHGX": (29.47, -95.08), "KCRP": (27.78, -97.51), "KBRO": (25.92, -97.42),
    "KDYX": (32.54, -99.25), "KMAF": (31.94, -102.19), "KSJT": (31.37, -100.49),
    "KLBB": (33.65, -101.81), "KAMA": (35.23, -101.71), "KDFX": (29.27, -100.28),
    "KEPZ": (31.87, -106.70),
    # Mountain West
    "KFTG": (39.79, -104.55), "KPUX": (38.46, -104.18), "KGJX": (39.06, -108.21),
    "KCYS": (41.15, -104.81), "KRIW": (43.07, -108.48), "KBLX": (45.85, -108.61),
    "KGGW": (48.21, -106.62), "KTFX": (47.46, -111.39), "KMSX": (47.04, -113.99),
    "KSFX": (43.11, -112.69), "KPIH": (42.87, -112.40), "KMTX": (41.26, -112.45),
    "KICX": (37.59, -112.86), "KABX": (35.15, -106.82), "KFDX": (34.63, -103.62),
    "KHDX": (33.08, -106.12), "KIWA": (33.29, -111.67), "KEMX": (31.89, -110.63),
    "KFSX": (34.57, -111.20), "KYUX": (32.50, -114.66), "KESX": (35.70, -114.89),
    "KLRX": (40.74, -116.80), "KRGX": (39.75, -119.46),
    # West Coast / Northwest
    "KNKX": (32.92, -117.04), "KSOX": (33.82, -117.64), "KVTX": (34.41, -119.18),
    "KVBX": (34.84, -120.40), "KHNX": (36.31, -119.63), "KMUX": (37.16, -121.90),
    "KDAX": (38.50, -121.68), "KBBX": (39.50, -121.63), "KBHX": (40.50, -124.29),
    "KMAX": (42.08, -122.72), "KRTX": (45.71, -122.96), "KLGX": (47.12, -124.11),
    "KATX": (48.19, -122.50), "KOTX": (47.68, -117.63), "KPDT": (45.69, -118.85),
    "KBOI": (43.49, -116.24), "KCBX": (43.49, -116.24),
    # Alaska / Hawaii / Caribbean
    "PAHG": (60.73, -151.35), "PAPD": (65.04, -147.50), "PABC": (60.79, -161.87),
    "PAEC": (64.51, -165.29), "PAKC": (58.68, -156.63), "PAIH": (59.46, -146.30),
    "PACG": (56.85, -135.53),
    "PHKI": (21.89, -159.55), "PHMO": (21.13, -157.18), "PHKM": (20.13, -155.78),
    "PHWA": (19.09, -155.57),
    "TJUA": (18.12, -66.08),
}


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def nearest_site(lat: float, lon: float) -> tuple[str, float]:
    """(site_id, distance_km) of the closest WSR-88D."""
    best_id, best_d = "", float("inf")
    for sid, (slat, slon) in NEXRAD_SITES.items():
        d = _haversine_km(lat, lon, slat, slon)
        if d < best_d:
            best_id, best_d = sid, d
    return best_id, best_d


def site_coords(site_id: str) -> tuple[float, float] | None:
    return NEXRAD_SITES.get(site_id.upper())
