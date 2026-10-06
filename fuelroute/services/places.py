"""Offline US place lookup: (city, state) -> (lat, lon).

Backed by ``data/us_places.csv.gz`` (Census Gazetteer + GeoNames, see
``scripts/build_places_dataset.py``). Loaded once per process (~0.5 s) and then
every lookup is a dict access.
"""

from __future__ import annotations

import csv
import gzip
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from .text import compact, normalize_place

# Lower rank wins. Census internal points are the most reliable source.
_SOURCE_RANK = {"census": 0, "geonames": 1}


@dataclass(frozen=True)
class Place:
    latitude: float
    longitude: float
    source: str


class PlaceIndex:
    def __init__(self, rows):
        best: dict[tuple[str, str], tuple[tuple[int, int], Place]] = {}
        for name, state, lat, lon, population, source in rows:
            rank = (_SOURCE_RANK.get(source, 9), -int(population or 0))
            place = Place(float(lat), float(lon), source)
            key = (normalize_place(name), state)
            if key not in best or rank < best[key][0]:
                best[key] = (rank, place)
        self._exact = {key: place for key, (_rank, place) in best.items()}
        self._compact: dict[tuple[str, str], Place] = {}
        for (name, state), (_rank, place) in sorted(best.items(), key=lambda item: item[1][0]):
            self._compact.setdefault((compact(name), state), place)

    def __len__(self) -> int:
        return len(self._exact)

    def lookup(self, city: str, state: str) -> Place | None:
        name = normalize_place(city)
        state = state.strip().upper()
        return self._exact.get((name, state)) or self._compact.get((compact(name), state))

    @classmethod
    def from_file(cls, path: Path) -> "PlaceIndex":
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            next(reader)  # header
            return cls(list(reader))


@lru_cache(maxsize=1)
def get_place_index() -> PlaceIndex:
    return PlaceIndex.from_file(settings.FUEL_PLANNER["PLACES_FILE"])
