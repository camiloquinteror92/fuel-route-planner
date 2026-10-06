"""Routing: the ONE external call per new trip, to the public OSRM server (free, no key).

``get_route(start, finish)`` asks OSRM for the driving route
(``/route/v1/driving/{lon,lat};{lon,lat}?overview=full&geometries=polyline6``),
decodes it and prepares everything the planner needs from the geometry:

* ``samples`` / ``sample_miles``: a point every ~1 mile with its mile marker, scaled
  so the last marker equals OSRM's road distance (the corridor search uses them);
* ``sample_in_usa``: which samples are inside the USA (a route between two US cities
  can cross Canada);
* ``line``: the geometry simplified to ~50 m for the GeoJSON / map;
* ``snap_miles``: how far OSRM had to move the start / finish to reach a road
  (``waypoints[].distance``), so a point in the sea or the desert can be rejected.

Only that prepared route is cached (~100 KB for coast to coast, instead of the
~560 KB raw 35k-vertex geometry), keyed by the rounded coordinates. A per-key lock
makes concurrent identical requests wait for the first one instead of all calling
OSRM ("single flight").
"""

from __future__ import annotations

import logging
import threading
import zlib
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .errors import ExternalServiceError, NoRouteFound
from .geo import cumulative_miles, resample, simplify
from .geocoding import Location
from .http import ExternalApiClient
from .usa import in_usa

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
# 64 lock stripes: requests for the same route share a lock; different routes rarely do.
_LOCKS = [threading.Lock() for _ in range(64)]


@dataclass
class Route:
    distance_miles: float
    duration_seconds: float
    samples: np.ndarray  # (N, 2) lat, lon, one every ~RESAMPLE_MILES of road
    sample_miles: np.ndarray  # (N,) road mile marker of each sample
    sample_in_usa: np.ndarray  # (N,) bool
    line: np.ndarray  # (M, 2) lat, lon, simplified for display
    snap_miles: tuple[float, float]  # start / finish distance to the road OSRM used
    geometry_points: int = 0  # vertices of the decoded OSRM geometry
    polyline_chars: int = 0  # length of the polyline6 string OSRM sent

    @property
    def sample_spacing_miles(self) -> float:
        if len(self.sample_miles) < 2:
            return 0.0
        return float(self.sample_miles[-1]) / (len(self.sample_miles) - 1)

    @property
    def miles_outside_usa(self) -> float:
        return float(np.count_nonzero(~self.sample_in_usa)) * self.sample_spacing_miles


def decode_polyline(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode a Google encoded polyline (OSRM ``polyline6``) into (lat, lon) rows.

    Format: each coordinate delta is zig-zag encoded and split into 5-bit chunks,
    least significant first; every chunk except the last of a value has the 0x20
    bit set; each chunk is stored as chr(chunk + 63). Deltas alternate lat, lon.

    Vectorised with numpy (a coast-to-coast route is ~120k characters): ~2 ms
    instead of ~35 ms for the classic per-character loop.
    """
    if not encoded:
        return np.zeros((0, 2))
    chunks = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    if chunks.min() < 0 or chunks.max() > 63:
        raise ValueError("invalid character in polyline")
    last = chunks < 0x20  # last chunk of each value
    if not last[-1]:
        raise ValueError("truncated polyline")
    value_index = np.concatenate(([0], np.cumsum(last)[:-1]))
    starts = np.flatnonzero(np.concatenate(([True], last[:-1])))
    position = np.arange(len(chunks)) - starts[value_index]  # chunk number inside its value
    values = np.bitwise_or.reduceat((chunks & 0x1F) << (5 * position), starts)
    if len(values) % 2:
        raise ValueError("odd number of values in polyline")
    deltas = np.where(values & 1, ~(values >> 1), values >> 1)
    return np.cumsum(deltas.reshape(-1, 2), axis=0) / 10**precision


def prepare_route(
    points: np.ndarray, distance_miles: float, duration_seconds: float, snap_miles=(0.0, 0.0), polyline_chars: int = 0
) -> Route:
    """Resample, mark the USA part and simplify a decoded geometry (see module docstring)."""
    cumulative = cumulative_miles(points)
    # The geometry's haversine length is slightly shorter than OSRM's road distance;
    # scale the markers so the last sample is at exactly ``distance_miles``.
    scale = distance_miles / cumulative[-1] if cumulative[-1] > 0 else 1.0
    step = settings.FUEL_PLANNER["RESAMPLE_MILES"]
    samples, sample_miles = resample(points, cumulative, step / scale)
    return Route(
        distance_miles=distance_miles,
        duration_seconds=duration_seconds,
        samples=samples,
        sample_miles=sample_miles * scale,
        sample_in_usa=in_usa(samples[:, 0], samples[:, 1]),
        line=simplify(points),
        snap_miles=(float(snap_miles[0]), float(snap_miles[1])),
        geometry_points=len(points),
        polyline_chars=int(polyline_chars),
    )


def _cache_key(start: Location, finish: Location) -> str:
    step = settings.FUEL_PLANNER["RESAMPLE_MILES"]
    return f"route:v3:{step}:{start.latitude:.5f},{start.longitude:.5f};{finish.latitude:.5f},{finish.longitude:.5f}"


def get_route(start: Location, finish: Location, client: ExternalApiClient) -> tuple[Route, bool]:
    """Return (route, from_cache). At most one OSRM call per (start, finish) per process."""
    key = _cache_key(start, finish)
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    with _LOCKS[zlib.crc32(key.encode()) % len(_LOCKS)]:
        cached = cache.get(key)  # another request may have fetched it while we waited
        if cached is not None:
            return cached, True
        route = _fetch(start, finish, client)
        cache.set(key, route)
        return route, False


def _fetch(start: Location, finish: Location, client: ExternalApiClient) -> Route:
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
    try:
        points = decode_polyline(best["geometry"])
    except (ValueError, UnicodeEncodeError) as exc:
        raise ExternalServiceError("The routing service returned an invalid geometry.") from exc
    if len(points) < 2:
        raise NoRouteFound("The routing service returned an empty route.")
    waypoints = payload.get("waypoints") or []
    snaps = [float(w.get("distance") or 0.0) / METERS_PER_MILE for w in waypoints[:2]]
    snaps += [0.0] * (2 - len(snaps))
    return prepare_route(
        points, best["distance"] / METERS_PER_MILE, best["duration"], snaps, polyline_chars=len(best["geometry"])
    )
