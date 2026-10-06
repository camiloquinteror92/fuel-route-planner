"""Turn the user's ``start`` / ``finish`` text into coordinates.

Order, cheapest first:
1. "lat,lon"                    -> no lookup at all
2. "City, ST" / "City, State"   -> offline place index (0 external calls)
3. anything else                -> Nominatim (1 external call, cached for a day)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from .errors import LocationNotFound, LocationOutsideUSA
from .http import ExternalApiClient
from .places import get_place_index
from .text import normalize_state

_COORDINATES = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

# Coarse bounding boxes: contiguous US, Alaska, Hawaii (lat_min, lat_max, lon_min, lon_max).
_US_BOXES = (
    (24.3, 49.5, -125.0, -66.8),
    (51.0, 71.6, -180.0, -129.9),
    (18.8, 22.4, -160.5, -154.6),
)
_NOMINATIM_CACHE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Location:
    query: str
    latitude: float
    longitude: float
    label: str
    geocoder: str  # "coordinates" | "offline" | "nominatim"

    @property
    def as_param(self) -> str:
        return f"{self.latitude:.6f},{self.longitude:.6f}"

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "label": self.label,
            "lat": round(self.latitude, 6),
            "lon": round(self.longitude, 6),
            "geocoder": self.geocoder,
        }


def parse_coordinates(text: str) -> tuple[float, float] | None:
    match = _COORDINATES.match(text)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def is_in_usa(latitude: float, longitude: float) -> bool:
    return any(a <= latitude <= b and c <= longitude <= d for a, b, c, d in _US_BOXES)


def _split_city_state(text: str) -> tuple[str, str] | None:
    """Accept "Austin, TX", "Austin, Texas", "Austin TX" and "New York, NY, USA"."""
    cleaned = re.sub(r",?\s*(usa|us|united states( of america)?)\s*$", "", text.strip(), flags=re.I)
    if "," in cleaned:
        city, state = cleaned.rsplit(",", 1)
    else:
        parts = cleaned.rsplit(" ", 1)
        if len(parts) != 2:
            return None
        city, state = parts
    code = normalize_state(state)
    if not code or not city.strip():
        return None
    return city.strip(), code


def _offline(text: str) -> Location | None:
    parsed = _split_city_state(text)
    if not parsed:
        return None
    city, state = parsed
    place = get_place_index().lookup(city, state)
    if not place:
        return None
    return Location(text, place.latitude, place.longitude, f"{city.title()}, {state}", "offline")


def _nominatim(text: str, client: ExternalApiClient) -> Location | None:
    key = "nominatim:v1:" + hashlib.sha1(text.strip().lower().encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return Location(text, *cached) if cached else None
    status, payload = client.get_json(
        "nominatim",
        settings.FUEL_PLANNER["NOMINATIM_URL"],
        params={"q": text, "format": "jsonv2", "countrycodes": "us", "limit": 1},
    )
    if status != 200 or not payload:
        cache.set(key, (), _NOMINATIM_CACHE_SECONDS)
        return None
    hit = payload[0]
    values = (float(hit["lat"]), float(hit["lon"]), hit.get("display_name", text), "nominatim")
    cache.set(key, values, _NOMINATIM_CACHE_SECONDS)
    return Location(text, *values)


def geocode(text: str, client: ExternalApiClient, field: str) -> Location:
    coordinates = parse_coordinates(text)
    if coordinates:
        latitude, longitude = coordinates
        location = Location(text, latitude, longitude, f"{latitude:.5f},{longitude:.5f}", "coordinates")
    else:
        location = _offline(text) or _nominatim(text, client)
        if location is None:
            raise LocationNotFound(
                f"Could not find '{text}' in the USA. Use 'City, ST' or 'lat,lon'.", field=field
            )
    if not is_in_usa(location.latitude, location.longitude):
        raise LocationOutsideUSA(f"'{text}' is outside the USA.", field=field)
    return location

