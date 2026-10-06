"""In-memory numpy view of the geocoded stations, used to find stations near a route."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from ..models import FuelStation
from .geo import nearest_on_line


@dataclass
class StationArrays:
    ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray

    def __len__(self) -> int:
        return len(self.ids)


_lock = threading.Lock()
_arrays: StationArrays | None = None


def get_station_arrays() -> StationArrays:
    """Load all geocoded stations once per process (~6.6k rows, a few ms)."""
    global _arrays
    if _arrays is None:
        with _lock:
            if _arrays is None:
                rows = list(
                    FuelStation.objects.filter(latitude__isnull=False)
                    .order_by("opis_id")
                    .values_list("id", "latitude", "longitude", "price")
                )
                data = np.array(rows, dtype=float).reshape(-1, 4)
                _arrays = StationArrays(
                    ids=data[:, 0].astype(np.int64), lat=data[:, 1], lon=data[:, 2], price=data[:, 3]
                )
    return _arrays


def reset_station_arrays() -> None:
    global _arrays
    with _lock:
        _arrays = None


@dataclass
class CorridorStation:
    station_id: int
    mile: float
    offset_miles: float
    price: float


def stations_along_route(
    samples: np.ndarray, sample_miles: np.ndarray, corridor_miles: float, stations: StationArrays
) -> list[CorridorStation]:
    """Stations within ``corridor_miles`` of the route, with their mile marker.

    1. Bounding-box prefilter (cheap, drops most of the country).
    2. Exact nearest route sample for the rest with one matrix multiply.
    """
    if len(stations) == 0:
        return []
    margin_lat = corridor_miles / 69.0
    max_abs_lat = float(np.max(np.abs(samples[:, 0]))) + margin_lat
    margin_lon = corridor_miles / (69.172 * max(np.cos(np.radians(min(max_abs_lat, 89.0))), 0.01))
    in_box = (
        (stations.lat >= samples[:, 0].min() - margin_lat)
        & (stations.lat <= samples[:, 0].max() + margin_lat)
        & (stations.lon >= samples[:, 1].min() - margin_lon)
        & (stations.lon <= samples[:, 1].max() + margin_lon)
    )
    idx = np.nonzero(in_box)[0]
    if len(idx) == 0:
        return []
    nearest, distance = nearest_on_line(stations.lat[idx], stations.lon[idx], samples)
    close = distance <= corridor_miles
    return [
        CorridorStation(
            station_id=int(stations.ids[i]),
            mile=float(sample_miles[n]),
            offset_miles=float(d),
            price=float(stations.price[i]),
        )
        for i, n, d in zip(idx[close], nearest[close], distance[close])
    ]
