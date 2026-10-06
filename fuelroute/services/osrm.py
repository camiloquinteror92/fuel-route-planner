"""Routing with the public OSRM server (free, no API key).

ONE call per (start, finish) pair. The decoded route is cached, so repeating the
request or opening the map page makes no new call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .errors import ExternalServiceError, NoRouteFound
from .geocoding import Location
from .http import ExternalApiClient

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344


@dataclass
class Route:
    points: np.ndarray  # (N, 2) lat, lon
    distance_miles: float
    duration_seconds: float


def decode_polyline(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode a Google encoded polyline (OSRM ``polyline6``) into (lat, lon) rows.

    polyline6 is ~5x smaller than GeoJSON on the wire, which makes the one
    external call faster.
    """
    coordinates = []
    index = lat = lon = 0
    factor = 10 ** precision
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coordinates.append((lat / factor, lon / factor))
    return np.array(coordinates, dtype=float)


def _cache_key(start: Location, finish: Location) -> str:
    return f"osrm:v1:{start.latitude:.5f},{start.longitude:.5f};{finish.latitude:.5f},{finish.longitude:.5f}"


def get_route(start: Location, finish: Location, client: ExternalApiClient) -> tuple[Route, bool]:
    """Return (route, from_cache)."""
    key = _cache_key(start, finish)
    cached = cache.get(key)
    if cached is not None:
        return cached, True

    url = (
        f"{settings.FUEL_PLANNER['OSRM_URL']}/route/v1/driving/"
        f"{start.longitude:.6f},{start.latitude:.6f};{finish.longitude:.6f},{finish.latitude:.6f}"
    )
    status, payload = client.get_json(
        "osrm", url, params={"overview": "full", "geometries": "polyline6", "steps": "false"}
    )
    code = payload.get("code") if isinstance(payload, dict) else None
    if code in ("NoRoute", "NoSegment"):
        raise NoRouteFound("No drivable route between the two locations.", upstream_code=code)
    if status != 200 or code != "Ok" or not payload.get("routes"):
        raise ExternalServiceError(f"Routing failed (HTTP {status}, code {code}).")

    best = payload["routes"][0]
    route = Route(
        points=decode_polyline(best["geometry"]),
        distance_miles=best["distance"] / METERS_PER_MILE,
        duration_seconds=best["duration"],
    )
    if len(route.points) < 2:
        raise NoRouteFound("The routing service returned an empty route.")
    cache.set(key, route)
    return route, False
