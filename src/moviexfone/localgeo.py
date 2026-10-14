"""Local NYC ZIP centroids and subway stations. GeoSearch has no ZIP centroids."""

from __future__ import annotations

import re

# Approximate centroids, good enough to land in the right neighborhood.
_ZIPS: dict[str, tuple[float, float, str, str]] = {
    "10001": (40.750, -73.997, "Chelsea", "Manhattan"),
    "10002": (40.716, -73.987, "Lower East Side", "Manhattan"),
    "10003": (40.732, -73.989, "East Village", "Manhattan"),
    "10004": (40.704, -74.013, "Financial District", "Manhattan"),
    "10005": (40.706, -74.009, "Financial District", "Manhattan"),
    "10006": (40.709, -74.013, "Battery Park", "Manhattan"),
    "10007": (40.714, -74.008, "Tribeca", "Manhattan"),
    "10009": (40.727, -73.979, "East Village", "Manhattan"),
    "10010": (40.739, -73.983, "Gramercy", "Manhattan"),
    "10011": (40.742, -74.000, "Chelsea", "Manhattan"),
    "10012": (40.726, -73.998, "SoHo", "Manhattan"),
    "10013": (40.720, -74.005, "Tribeca", "Manhattan"),
    "10014": (40.734, -74.006, "West Village", "Manhattan"),
    "10016": (40.745, -73.978, "Murray Hill", "Manhattan"),
    "10017": (40.752, -73.973, "Midtown East", "Manhattan"),
    "10018": (40.755, -73.993, "Midtown", "Manhattan"),
    "10019": (40.765, -73.985, "Hell's Kitchen", "Manhattan"),
    "10021": (40.769, -73.959, "Upper East Side", "Manhattan"),
    "10023": (40.775, -73.982, "Upper West Side", "Manhattan"),
    "10024": (40.786, -73.974, "Upper West Side", "Manhattan"),
    "10025": (40.799, -73.966, "Upper West Side", "Manhattan"),
    "10026": (40.803, -73.953, "Harlem", "Manhattan"),
    "10027": (40.811, -73.953, "Harlem", "Manhattan"),
    "10028": (40.776, -73.953, "Upper East Side", "Manhattan"),
    "10029": (40.792, -73.944, "East Harlem", "Manhattan"),
    "10030": (40.818, -73.943, "Harlem", "Manhattan"),
    "10031": (40.825, -73.950, "Hamilton Heights", "Manhattan"),
    "10032": (40.838, -73.943, "Washington Heights", "Manhattan"),
    "10033": (40.850, -73.934, "Washington Heights", "Manhattan"),
    "10034": (40.867, -73.922, "Inwood", "Manhattan"),
    "10036": (40.759, -73.990, "Times Square", "Manhattan"),
    "10038": (40.709, -74.003, "Financial District", "Manhattan"),
    "10065": (40.765, -73.963, "Upper East Side", "Manhattan"),
    "10075": (40.773, -73.956, "Upper East Side", "Manhattan"),
    "10128": (40.781, -73.951, "Upper East Side", "Manhattan"),
    "11201": (40.694, -73.990, "Brooklyn Heights", "Brooklyn"),
    "11205": (40.694, -73.966, "Fort Greene", "Brooklyn"),
    "11206": (40.702, -73.942, "Williamsburg", "Brooklyn"),
    "11211": (40.712, -73.953, "Williamsburg", "Brooklyn"),
    "11215": (40.661, -73.986, "Park Slope", "Brooklyn"),
    "11217": (40.681, -73.978, "Boerum Hill", "Brooklyn"),
    "11222": (40.727, -73.951, "Greenpoint", "Brooklyn"),
    "11231": (40.678, -74.001, "Carroll Gardens", "Brooklyn"),
    "11238": (40.679, -73.963, "Prospect Heights", "Brooklyn"),
    "11101": (40.750, -73.940, "Long Island City", "Queens"),
    "11106": (40.764, -73.929, "Astoria", "Queens"),
    "11354": (40.769, -73.830, "Flushing", "Queens"),
    "11375": (40.721, -73.845, "Forest Hills", "Queens"),
    "10451": (40.820, -73.925, "Concourse", "Bronx"),
    "10452": (40.837, -73.923, "Highbridge", "Bronx"),
    "10463": (40.881, -73.906, "Riverdale", "Bronx"),
    "10301": (40.633, -74.096, "St. George", "Staten Island"),
    "10314": (40.599, -74.166, "Bulls Head", "Staten Island"),
}

# A short list of stations people actually ask for. Not the full MTA book.
_STATIONS: tuple[tuple[str, float, float, str], ...] = (
    ("14th Street Union Square", 40.735, -73.991, "Manhattan"),
    ("Union Square", 40.735, -73.991, "Manhattan"),
    ("Times Square 42nd Street", 40.756, -73.987, "Manhattan"),
    ("Bedford Avenue", 40.717, -73.957, "Brooklyn"),
    ("Lorimer Street", 40.714, -73.950, "Brooklyn"),
    ("Metropolitan Avenue", 40.712, -73.951, "Brooklyn"),
    ("Atlantic Avenue Barclays Center", 40.684, -73.978, "Brooklyn"),
    ("Jay Street MetroTech", 40.692, -73.987, "Brooklyn"),
    ("Court Street", 40.694, -73.991, "Brooklyn"),
    ("72nd Street", 40.778, -73.982, "Manhattan"),
    ("96th Street", 40.794, -73.972, "Manhattan"),
    ("125th Street", 40.808, -73.945, "Manhattan"),
    ("Canal Street", 40.718, -74.000, "Manhattan"),
    ("Fulton Street", 40.710, -74.007, "Manhattan"),
    ("Grand Central 42nd Street", 40.752, -73.977, "Manhattan"),
    ("Herald Square 34th Street", 40.750, -73.988, "Manhattan"),
    ("Astoria Boulevard", 40.770, -73.918, "Queens"),
    ("Jackson Heights Roosevelt Avenue", 40.747, -73.891, "Queens"),
    ("Forest Hills 71st Avenue", 40.721, -73.844, "Queens"),
    ("161st Street Yankee Stadium", 40.828, -73.926, "Bronx"),
    ("St George", 40.644, -74.074, "Staten Island"),
)


def _norm(text: str) -> str:
    lowered = text.lower().replace(".", " ")
    lowered = re.sub(r"\b(station|stop|subway|train|the)\b", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def lookup_local(query: str) -> dict[str, object] | None:
    text = query.strip()
    if re.fullmatch(r"\d{5}", text) and text in _ZIPS:
        lat, lng, label, borough = _ZIPS[text]
        return {"label": label, "lat": lat, "lng": lng, "borough": borough}
    folded = _norm(text)
    if not folded:
        return None
    asking_station = bool(re.search(r"\b(station|stop|subway|train)\b", text.lower()))
    best: tuple[int, tuple[str, float, float, str]] | None = None
    for station in _STATIONS:
        name = _norm(station[0])
        if folded == name or (asking_station and (folded in name or name in folded)):
            score = len(name)
            if best is None or score > best[0]:
                best = (score, station)
    if best is None:
        return None
    name, lat, lng, borough = best[1]
    return {"label": name, "lat": lat, "lng": lng, "borough": borough}
