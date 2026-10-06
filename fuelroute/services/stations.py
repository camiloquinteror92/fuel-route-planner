"""Stations near a route: an in-memory numpy copy of the station table + corridor search.

``get_station_arrays`` keeps (opis_id, lat, lon, price) of every geocoded station
in numpy arrays, loaded once per process and reloaded when the table changes.
``stations_along_route`` finds the ones within ``CORRIDOR_MILES`` of the route and
gives each its mile marker on the route, in ~20 ms for a coast-to-coast trip.

Data version. ``load_stations`` replaces every row (delete + insert), so primary
keys change and a running server would hold stale arrays. ``data_version()`` is a
cheap aggregate (row count, highest primary key) that changes on every reload; the
arrays and the plan cache key include it, so a server picks up new prices on its
next request without a restart, and never mixes old arrays with new rows.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
from django.db.models import Count, Max

from ..models import FuelStation
from .geo import nearest_on_line, within_distance


@dataclass
class StationArrays:
    opis_ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray
    version: str = ""

    def __len__(self) -> int:
        return len(self.opis_ids)


# The coarse pass of the corridor search checks one route sample every this many miles.
_COARSE_STEP_MILES = 20.0
_lock = threading.Lock()
_arrays: StationArrays | None = None


def data_version() -> str:
    """Changes whenever load_stations replaces the table (one indexed aggregate query)."""
    stats = FuelStation.objects.aggregate(rows=Count("id"), last=Max("id"))
    return f"{stats['rows']}-{stats['last'] or 0}"


def get_station_arrays(version: str | None = None) -> StationArrays:
    """All geocoded stations as numpy arrays, (re)loaded when the data version changes."""
    global _arrays
    version = version or data_version()
    if _arrays is None or _arrays.version != version:
        with _lock:
            if _arrays is None or _arrays.version != version:
                rows = list(
                    FuelStation.objects.filter(latitude__isnull=False)
                    .order_by("opis_id")
                    .values_list("opis_id", "latitude", "longitude", "price")
                )
                data = np.array(rows, dtype=float).reshape(-1, 4)
                _arrays = StationArrays(
                    opis_ids=data[:, 0].astype(np.int64),
                    lat=data[:, 1],
                    lon=data[:, 2],
                    price=data[:, 3],
                    version=version,
                )
    return _arrays


def reset_station_arrays() -> None:
    """Forget the arrays of THIS process (tests, and load_stations after its commit)."""
    global _arrays
    with _lock:
        _arrays = None


@dataclass
class CorridorStation:
    opis_id: int
    mile: float  # mile marker of the closest route sample
    offset_miles: float  # straight-line distance from that sample (the detour, one way)
    price: float


def stations_along_route(
    samples: np.ndarray,
    sample_miles: np.ndarray,
    corridor_miles: float,
    stations: StationArrays,
    sample_in_usa: np.ndarray | None = None,
) -> list[CorridorStation]:
    """Stations within ``corridor_miles`` of the route, with their mile marker.

    ``samples``: route points every ~1 mile, (N, 2) lat/lon; ``sample_miles``: their
    mile markers. Three passes so that the exact (and costly) one runs on a few
    hundred stations, not 6,500:

    1. Bounding box of the route plus the corridor (drops most of the country).
    2. Coarse pass against one sample every ~20 miles: keeps stations within
       ``corridor + spacing/2 + 1`` miles of one of them. No false negatives: every
       route point is at most spacing/2 from a coarse sample.
    3. Exact nearest sample for the survivors.
    Coast to coast: ~5,000 stations -> ~700 -> ~460, in ~20 ms.

    ``sample_in_usa``: optional mask of the samples. A station whose nearest route
    point is outside the USA (a US station seen from a Canadian highway) is dropped:
    reaching it would mean crossing the border.
    """
    if len(stations) == 0 or len(samples) == 0:
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

    sample_spacing = float(sample_miles[1] - sample_miles[0]) if len(sample_miles) > 1 else 1.0
    step = max(1, int(round(_COARSE_STEP_MILES / max(sample_spacing, 1e-6))))
    coarse = samples[::step]
    if not np.array_equal(coarse[-1], samples[-1]):
        coarse = np.vstack((coarse, samples[-1]))
    spacing = float(sample_miles[min(step, len(sample_miles) - 1)] - sample_miles[0])
    idx = idx[within_distance(stations.lat[idx], stations.lon[idx], coarse, corridor_miles + spacing / 2 + 1)]
    if len(idx) == 0:
        return []
    nearest, distance = nearest_on_line(stations.lat[idx], stations.lon[idx], samples)
    close = distance <= corridor_miles
    if sample_in_usa is not None:
        close &= sample_in_usa[nearest]
    return [
        CorridorStation(
            opis_id=int(stations.opis_ids[i]),
            mile=float(sample_miles[n]),
            offset_miles=float(d),
            price=float(stations.price[i]),
        )
        for i, n, d in zip(idx[close], nearest[close], distance[close])
    ]
