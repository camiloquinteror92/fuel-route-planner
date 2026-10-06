"""Offline US place lookup: (city, state) -> coordinates, with homonyms.

Backed by ``data/us_places.csv.gz`` (Census Gazetteer + GeoNames, built by
``scripts/build_places_dataset.py``). The file is grouped by (normalized name,
state) and each group is ordered best-first, so:

* ``lookup()`` returns the first place of the group: the most populated /
  incorporated one. Used for user input ("Mountain View, CA").
* ``candidates()`` returns every place of the group. Used by the station loader to
  detect ambiguous city names (12 "Antioch" in Tennessee) instead of guessing.

Memory: ~190k places are kept in numpy arrays and two dicts of
``"name|ST" -> packed (first row, row count)`` integers, about 55 MB per process
instead of ~200 MB for dicts of Python objects. Loading takes about 0.4 s and is
done once per process (``fuelroute/warmup.py``).
"""

from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from django.conf import settings

from .text import compact, normalize_place

_SOURCES = ("census", "geonames")
# A group never has more than a few dozen places; 12 bits leave plenty of room.
_COUNT_BITS = 12


@dataclass(frozen=True)
class Place:
    latitude: float
    longitude: float
    source: str  # "census" or "geonames": which dataset gave the coordinates
    population: int = 0


class PlaceIndex:
    def __init__(self, rows, normalized: bool = False):
        """``rows``: iterables of (name, state, lat, lon, population, source), grouped by
        (name, state) and best-first inside each group, as the dataset file is.

        ``normalized=True`` means the names already went through ``normalize_place``
        (the dataset file); tests pass raw names like "Saint Louis".
        """
        lat, lon, population, source = [], [], [], []
        self._exact: dict[str, int] = {}
        current, start = None, 0
        for index, (name, state, row_lat, row_lon, row_population, row_source) in enumerate(rows):
            key = f"{name if normalized else normalize_place(name)}|{state.strip().upper()}"
            if key != current:
                if current is not None and current not in self._exact:
                    self._exact[current] = self._pack(start, index - start)
                current, start = key, index
            lat.append(float(row_lat))
            lon.append(float(row_lon))
            population.append(int(row_population or 0))
            source.append(_SOURCES.index(row_source) if row_source in _SOURCES else 1)
        if current is not None and current not in self._exact:
            self._exact[current] = self._pack(start, len(lat) - start)

        self._lat = np.array(lat, dtype=np.float64)
        self._lon = np.array(lon, dtype=np.float64)
        self._population = np.array(population, dtype=np.int64)
        self._source = np.array(source, dtype=np.int8)

        # Second-chance keys without spaces, only for names that have spaces and whose
        # compact form is not already a name: "la place" -> "laplace".
        self._compact: dict[str, int] = {}
        for key, packed in self._exact.items():
            name, state = key.split("|")
            if " " in name:
                self._compact.setdefault(f"{compact(name)}|{state}", packed)

    @staticmethod
    def _pack(start: int, count: int) -> int:
        return (start << _COUNT_BITS) | min(count, (1 << _COUNT_BITS) - 1)

    def __len__(self) -> int:
        return len(self._exact)

    def _place(self, row: int) -> Place:
        return Place(
            float(self._lat[row]), float(self._lon[row]), _SOURCES[self._source[row]], int(self._population[row])
        )

    def _group(self, city: str, state: str) -> range:
        name = normalize_place(city)
        state = state.strip().upper()
        packed = (
            self._exact.get(f"{name}|{state}")
            or self._exact.get(f"{compact(name)}|{state}")
            or self._compact.get(f"{compact(name)}|{state}")
        )
        if packed is None:
            return range(0)
        start = packed >> _COUNT_BITS
        return range(start, start + (packed & ((1 << _COUNT_BITS) - 1)))

    def lookup(self, city: str, state: str) -> Place | None:
        """Best place for (city, state), or None. The best is the most populated one."""
        group = self._group(city, state)
        return self._place(group[0]) if group else None

    def candidates(self, city: str, state: str) -> list[Place]:
        """Every distinct place with that name in that state, best first."""
        return [self._place(row) for row in self._group(city, state)]

    @classmethod
    def from_file(cls, path: Path) -> "PlaceIndex":
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            next(reader)  # header
            return cls(reader, normalized=True)


@lru_cache(maxsize=1)
def get_place_index() -> PlaceIndex:
    """The process-wide index, loaded on first use (or by the warm-up at start-up)."""
    return PlaceIndex.from_file(settings.FUEL_PLANNER["PLACES_FILE"])
