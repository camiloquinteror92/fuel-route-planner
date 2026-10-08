"""Vectorised geometry helpers (numpy). All coordinates are (lat, lon) in degrees.

Used by the routing step (mile markers, resampling, simplification for the map)
and by the corridor search (nearest route point of each station). Distances are
great-circle (haversine) miles: at the scale of a US road trip the error against
an ellipsoid is well below the error of the station coordinates (city centers).
"""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance; works on scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_miles(points: np.ndarray) -> np.ndarray:
    """Distance from the first point to every point along a polyline."""
    if len(points) < 2:
        return np.zeros(len(points))
    steps = haversine_miles(points[:-1, 0], points[:-1, 1], points[1:, 0], points[1:, 1])
    return np.concatenate(([0.0], np.cumsum(steps)))


def resample(points: np.ndarray, cumulative: np.ndarray, step_miles: float):
    """Points every ``step_miles`` along the line (plus the last point).

    Linear interpolation of lat/lon between vertices is fine at this scale: route
    vertices are at most a few miles apart.
    """
    total = float(cumulative[-1])
    marks = np.arange(0.0, total, step_miles)
    marks = np.append(marks, total)
    lat = np.interp(marks, cumulative, points[:, 0])
    lon = np.interp(marks, cumulative, points[:, 1])
    return np.column_stack((lat, lon)), marks


def to_unit_vectors(lat, lon) -> np.ndarray:
    """3D unit vectors. The angle between two of them is the great-circle angle,
    so "nearest point" becomes "largest dot product"."""
    lat, lon = np.radians(lat), np.radians(lon)
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def _dots(query: np.ndarray, line: np.ndarray) -> np.ndarray:
    """(Q, 3) x (L, 3) -> (Q, L) dot products.

    Written as three broadcast multiply-adds instead of ``query @ line.T``: with an
    inner dimension of 3 BLAS gains nothing, and OpenBLAS pays ~300 ms of set-up
    the first time it runs on each new thread (every request thread in runserver).
    """
    out = query[:, 0, None] * line[:, 0]
    out += query[:, 1, None] * line[:, 1]
    out += query[:, 2, None] * line[:, 2]
    return out


def nearest_on_line(
    query_lat: np.ndarray, query_lon: np.ndarray, line_points: np.ndarray, chunk: int = 512
):
    """For each query point: index of the closest line point and the distance (miles)."""
    line_vectors = to_unit_vectors(line_points[:, 0], line_points[:, 1])
    query_vectors = to_unit_vectors(query_lat, query_lon)
    indexes = np.empty(len(query_vectors), dtype=np.int64)
    for start in range(0, len(query_vectors), chunk):
        indexes[start:start + chunk] = np.argmax(_dots(query_vectors[start:start + chunk], line_vectors), axis=1)
    nearest = line_points[indexes]
    distances = haversine_miles(query_lat, query_lon, nearest[:, 0], nearest[:, 1])
    return indexes, distances


def within_distance(
    query_lat: np.ndarray, query_lon: np.ndarray, line_points: np.ndarray, miles: float, chunk: int = 2048
) -> np.ndarray:
    """Boolean mask: query points that are within ``miles`` of ANY line point."""
    line_vectors = to_unit_vectors(line_points[:, 0], line_points[:, 1])
    query_vectors = to_unit_vectors(query_lat, query_lon)
    min_dot = np.cos(miles / EARTH_RADIUS_MILES)
    mask = np.empty(len(query_vectors), dtype=bool)
    for start in range(0, len(query_vectors), chunk):
        mask[start:start + chunk] = (_dots(query_vectors[start:start + chunk], line_vectors) >= min_dot).any(axis=1)
    return mask


def simplify(points: np.ndarray, tolerance_deg: float = 0.0005) -> np.ndarray:
    """Ramer-Douglas-Peucker in lat/lon space. Used only to shrink the geometry we
    send back (a coast-to-coast route has ~30k vertices). 0.0005 deg ~ 50 m."""
    if len(points) < 3:
        return points
    keep = np.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        segment = points[first + 1:last]
        a, b = points[first], points[last]
        direction = b - a
        length = np.hypot(*direction)
        if length == 0:
            distances = np.hypot(*(segment - a).T)
        else:
            distances = np.abs(direction[0] * (segment[:, 1] - a[1]) - direction[1] * (segment[:, 0] - a[0])) / length
        worst = int(np.argmax(distances))
        if distances[worst] > tolerance_deg:
            index = first + 1 + worst
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return points[keep]
