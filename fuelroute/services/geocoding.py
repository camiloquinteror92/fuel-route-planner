"""Turn the user's ``start`` / ``finish`` text into coordinates (step 1 of a request).

Order, cheapest first:

1. "lat,lon" or "lat lon"                  -> parsed, no lookup
2. "City, ST", "City, State", "City ST",
   "City State" ("Albany New York")         -> offline place index, 0 external calls
3. anything else with letters               -> Nominatim (1 call, cached; at most
                                               1 request/second per process)

Every result is then checked against the US outline (``usa.py``). Ambiguous city
names resolve to the most populated place with that name in that state; use
"lat,lon" to be exact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from .errors import ExternalServiceError, LocationNotFound, LocationOutsideUSA
from .http import ExternalApiClient
from .places import get_place_index
from .text import normalize_state
from .usa import region_of

_COORDINATES = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:,|\s)\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_COUNTRY_SUFFIX = re.compile(r",?\s*(?<![a-z])(usa|us|u\.s\.(a\.)?|united states( of america)?)\s*$", re.I)
_HAS_LETTER = re.compile(r"[A-Za-z]")
_NOMINATIM_HIT_SECONDS = 24 * 3600
# A "not found" answer is cached briefly: long enough to absorb retries, short
# enough that a temporary Nominatim problem is not remembered for a day.
_NOMINATIM_MISS_SECONDS = 30 * 60


@dataclass(frozen=True)
class Location:
    query: str  # what the user typed
    latitude: float
    longitude: float
    label: str  # normalized name shown back ("Austin, TX") or the coordinates
    geocoder: str  # "coordinates" | "offline" | "nominatim"
    region: str = "lower48"  # "lower48" | "alaska" | "hawaii" (see usa.py)

    @property
    def as_param(self) -> str:
        """Rounded coordinates, used in cache keys (1e-6 deg ~ 0.1 m)."""
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
    """"40.7128,-74.0060" or "40.7128 -74.0060" -> (lat, lon); anything else -> None."""
    match = _COORDINATES.match(text)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def has_letters(text: str) -> bool:
    return bool(_HAS_LETTER.search(text))


def split_city_state(text: str) -> tuple[str, str] | None:
    """("City", "ST") from "Austin, TX", "Austin, Texas", "Austin TX", "Albany New York",
    "Washington, D.C." or "New York, NY, USA"; None if no US state is recognised.

    Without a comma the state can be 1 to 3 words ("TX", "New York", "District of
    Columbia"); the longest match wins, so "Charleston West Virginia" is
    ("Charleston", "WV"), not ("Charleston West", "VA").
    """
    cleaned = _COUNTRY_SUFFIX.sub("", text.strip()).strip()
    if "," in cleaned:
        city, state = cleaned.rsplit(",", 1)
        code = normalize_state(state)
        return (city.strip(), code) if code and city.strip() else None
    words = cleaned.split()
    for size in (3, 2, 1):
        if len(words) > size:
            code = normalize_state(" ".join(words[-size:]))
            if code:
                return " ".join(words[:-size]), code
    return None


def _offline(text: str) -> Location | None:
    parsed = split_city_state(text)
    if not parsed:
        return None
    city, state = parsed
    place = get_place_index().lookup(city, state)
    if not place:
        return None
    return Location(text, place.latitude, place.longitude, f"{city.title()}, {state}", "offline")


def _nominatim(text: str, client: ExternalApiClient) -> Location | None:
    key = "nominatim:v2:" + hashlib.sha1(text.strip().lower().encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return Location(text, *cached) if cached else None
    config = settings.FUEL_PLANNER
    status, payload = client.get_json(
        "nominatim",
        config["NOMINATIM_URL"],
        params={"q": text, "format": "jsonv2", "countrycodes": "us", "limit": "1"},
        min_interval=config["NOMINATIM_MIN_INTERVAL_SECONDS"],
    )
    if status != 200:
        # 403 (blocked), 400... is a problem on their side or ours, not "not found".
        # Not cached, so the next request tries again.
        raise ExternalServiceError(f"nominatim answered HTTP {status}.")
    if not payload:
        cache.set(key, (), _NOMINATIM_MISS_SECONDS)
        return None
    hit = payload[0]
    values = (float(hit["lat"]), float(hit["lon"]), hit.get("display_name", text), "nominatim")
    cache.set(key, values, _NOMINATIM_HIT_SECONDS)
    return Location(text, *values)


def geocode(text: str, client: ExternalApiClient, field: str) -> Location:
    """Location for ``text``. Raises LocationNotFound / LocationOutsideUSA (HTTP 400).

    ``field`` ("start" / "finish") is echoed in the error so the client knows which
    input was wrong.
    """
    coordinates = parse_coordinates(text)
    if coordinates:
        latitude, longitude = coordinates
        location = Location(text, latitude, longitude, f"{latitude:.5f},{longitude:.5f}", "coordinates")
    else:
        location = _offline(text) or (_nominatim(text, client) if has_letters(text) else None)
        if location is None:
            raise LocationNotFound(
                f"Could not find '{text}' in the USA. Use 'City, ST' (e.g. 'Austin, TX') or 'lat,lon'.",
                field=field,
            )
    region = region_of(location.latitude, location.longitude)
    if region is None:
        raise LocationOutsideUSA(f"'{text}' is outside the USA.", field=field)
    return Location(location.query, location.latitude, location.longitude, location.label, location.geocoder, region)
