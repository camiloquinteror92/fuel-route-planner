"""Stations near a route: an in-memory numpy copy of the station table + corridor search.

``get_station_arrays`` keeps (opis_id, lat, lon, price, price_min, price_max) of every
geocoded station in numpy arrays, loaded once per process and reloaded when the
table changes. ``StationArrays.priced(policy)`` picks which price the planner uses
(the ``price_policy`` what-if of ``/api/route``) without copying anything.
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
from dataclasses import dataclass, replace

import numpy as np
from django.db.models import Count, Max

from ..models import FuelStation
from .geo import nearest_on_line, within_distance


# ``price_policy`` -> the column of the station table it plans with. "median" is the
# stored ``price`` (the median of the station's quotes, PRICE_POLICY at load time).
PRICE_POLICIES = ("median", "min", "max")


@dataclass
class StationArrays:
    opis_ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray
    version: str = ""
    # Cheapest / dearest quote of each station (None in hand-built arrays: same as price).
    price_min: np.ndarray | None = None
    price_max: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self.opis_ids)

    def priced(self, policy: str) -> "StationArrays":
        """The same stations with ``price`` set to the column of ``policy`` (views, no copy)."""
        if policy not in PRICE_POLICIES:
            raise ValueError(f"unknown price policy {policy!r}")
        column = {"median": self.price, "min": self.price_min, "max": self.price_max}[policy]
        if column is None or column is self.price:
            return self
        return replace(self, price=column)


# The coarse pass of the corridor search checks one route sample every this many miles.
COARSE_STEP_MILES = 20.0
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
                    .values_list("opis_id", "latitude", "longitude", "price", "price_min", "price_max")
                )
                data = np.array(rows, dtype=float).reshape(-1, 6)
                _arrays = StationArrays(
                    opis_ids=data[:, 0].astype(np.int64),
                    lat=data[:, 1],
                    lon=data[:, 2],
                    price=data[:, 3],
                    version=version,
                    price_min=data[:, 4],
                    price_max=data[:, 5],
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
    lat: float = 0.0  # the station's (city-level) coordinates
    lon: float = 0.0


FUNNEL_KEYS = (
    "stations_searched",
    "in_bounding_box",
    "after_coarse_pass",
    "within_corridor",
    "dropped_outside_usa",
    "candidates",
)


def stations_along_route(
    samples: np.ndarray,
    sample_miles: np.ndarray,
    corridor_miles: float,
    stations: StationArrays,
    sample_in_usa: np.ndarray | None = None,
    stats: dict | None = None,
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

    ``stats``: optional dict filled with how many stations survived each pass
    (``FUNNEL_KEYS``); the API shows it as the funnel of the search.
    """
    funnel = dict.fromkeys(FUNNEL_KEYS, 0)
    if stats is not None:
        stats.clear()
        stats.update(funnel)
        funnel = stats
    funnel["stations_searched"] = len(stations)
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
    funnel["in_bounding_box"] = len(idx)
    if len(idx) == 0:
        return []

    sample_spacing = float(sample_miles[1] - sample_miles[0]) if len(sample_miles) > 1 else 1.0
    step = max(1, int(round(COARSE_STEP_MILES / max(sample_spacing, 1e-6))))
    coarse = samples[::step]
    if not np.array_equal(coarse[-1], samples[-1]):
        coarse = np.vstack((coarse, samples[-1]))
    spacing = float(sample_miles[min(step, len(sample_miles) - 1)] - sample_miles[0])
    idx = idx[within_distance(stations.lat[idx], stations.lon[idx], coarse, corridor_miles + spacing / 2 + 1)]
    funnel["after_coarse_pass"] = len(idx)
    if len(idx) == 0:
        return []
    nearest, distance = nearest_on_line(stations.lat[idx], stations.lon[idx], samples)
    close = distance <= corridor_miles
    funnel["within_corridor"] = int(np.count_nonzero(close))
    if sample_in_usa is not None:
        close &= sample_in_usa[nearest]
    funnel["candidates"] = int(np.count_nonzero(close))
    funnel["dropped_outside_usa"] = funnel["within_corridor"] - funnel["candidates"]
    return [
        CorridorStation(
            opis_id=int(stations.opis_ids[i]),
            mile=float(sample_miles[n]),
            offset_miles=float(d),
            price=float(stations.price[i]),
            lat=float(stations.lat[i]),
            lon=float(stations.lon[i]),
        )
        for i, n, d in zip(idx[close], nearest[close], distance[close])
    ]
