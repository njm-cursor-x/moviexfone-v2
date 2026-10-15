"""Haversine distance and borough polygons. No PostGIS."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Rings are (lng, lat). They are simplified, non-overlapping outlines tuned so
# every theater in the registry falls in its own borough, and a borough name
# does not collapse to a centroid (Times Square is not in Brooklyn).
BOROUGHS: dict[str, list[tuple[float, float]]] = {
    "Staten Island": [
        (-74.26, 40.49),
        (-74.05, 40.49),
        (-74.05, 40.65),
        (-74.26, 40.65),
    ],
    "Bronx": [
        (-73.933, 40.798),
        (-73.910, 40.812),
        (-73.785, 40.800),
        (-73.765, 40.850),
        (-73.790, 40.915),
        (-73.910, 40.900),
        (-73.908, 40.875),
        (-73.933, 40.840),
        (-73.933, 40.798),
    ],
    "Manhattan": [
        (-74.020, 40.700),
        (-74.019, 40.720),
        (-74.012, 40.755),
        (-73.985, 40.800),
        (-73.960, 40.835),
        (-73.928, 40.878),
        (-73.910, 40.872),
        (-73.922, 40.848),
        (-73.934, 40.820),
        (-73.942, 40.798),
        (-73.940, 40.770),
        (-73.945, 40.758),
        (-73.960, 40.760),
        (-73.970, 40.735),
        (-73.973, 40.712),
        (-73.990, 40.700),
        (-74.013, 40.698),
        (-74.020, 40.700),
    ],
    "Brooklyn": [
        (-74.042, 40.548),
        (-74.042, 40.696),
        (-74.001, 40.696),
        (-73.978, 40.702),
        (-73.968, 40.728),
        (-73.948, 40.742),
        (-73.905, 40.735),
        (-73.905, 40.650),
        (-73.855, 40.570),
        (-74.042, 40.548),
    ],
    "Queens": [
        (-73.962, 40.748),
        (-73.940, 40.755),
        (-73.910, 40.802),
        (-73.780, 40.802),
        (-73.700, 40.760),
        (-73.700, 40.540),
        (-73.860, 40.540),
        (-73.910, 40.655),
        (-73.948, 40.730),
        (-73.962, 40.748),
    ],
}


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 3958.8
    la1, lo1, la2, lo2 = (math.radians(v) for v in (lat1, lng1, lat2, lng2))
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(h))


def point_in_polygon(lng: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray cast. `ring` is closed or open; the last edge closes it."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = ring[i]
        xj, yj = ring[j]
        intersects = (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (
            yj - yi + 0.0
        ) + xi
        if intersects:
            inside = not inside
        j = i
    return inside


def borough_of(lat: float, lng: float) -> str | None:
    for name in ("Staten Island", "Bronx", "Manhattan", "Brooklyn", "Queens"):
        if point_in_polygon(lng, lat, BOROUGHS[name]):
            return name
    return None


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lng: float
    label: str
    borough: str | None = None
