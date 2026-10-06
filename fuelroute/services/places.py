"""Offline US place lookup: (city, state) -> coordinates, with homonyms.

Backed by ``data/us_places.csv.gz`` (Census Gazetteer + GeoNames, built by
``scripts/build_places_dataset.py``). The file is grouped by (normalized name,
state) and each group is ordered best-first, so:

* ``lookup()`` returns the first place of the group: the most populated /
  incorporated one. Used for user input ("Mountain View, CA").
* ``candidates()`` returns every place of the group. Used by the station loader to
  detect ambiguous city names (12 "Antioch" in Tennessee) instead of guessing.
* ``search()`` is the type-ahead of ``GET /api/places``: names that START with what
  was typed, most populated first, one row per (name, state) (the place
  ``lookup()`` would pick, so a suggestion always plans to the same point). A name
  with no population whose "<name> City" twin is within a few miles is the same
  place under two names ("New York" / "New York City", "Carson" / "Carson City"): it
  ranks with the twin's population, so "New York, NY" (the form the examples use)
  comes first.

Memory: ~190k places are kept in numpy arrays and two dicts of
``"name|ST" -> packed (first row, row count)`` integers, about 55 MB per process
instead of ~200 MB for dicts of Python objects. Loading takes about 0.4 s and is
done once per process (``fuelroute/warmup.py``). The type-ahead adds a sorted list
of the same key strings (pointers, ~1.5 MB) and two small arrays, built on first use
(also by the warm-up); a search is a binary search plus a top-k on numpy, well
under a millisecond.
"""

from __future__ import annotations

import csv
import gzip
import re
import threading
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from django.conf import settings

from .geo import haversine_miles
from .text import US_STATES, compact, normalize_place

# States whose places can be suggested ("City, ST" geocodes there). Alaska and Hawaii
# are suggested but flagged: the price file has no station there (planning is a 422).
_NO_FUEL_DATA_STATES = ("AK", "HI")
_LETTER = re.compile(r"[a-z]")
# The type-ahead answers nothing until this many letters of the name were typed.
MIN_QUERY_LETTERS = 2
# Sorts after any character of a name: [prefix, prefix + this) holds every name with that prefix.
_AFTER_EVERY_NAME = chr(0x10FFFF)

# "New York" (Census, no population) and "New York City" (GeoNames, 8.8 million) are 5.0
# miles apart: closer than this, a "<name> City" twin is the same place (the next pair of
# the file, Broad Top / Broad Top City, PA, is 6.2 miles apart and already two places).
_TWIN_MILES = 6.0

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

        # Type-ahead index (``search``), built on first use.
        self._prefix: tuple | None = None
        self._prefix_lock = threading.Lock()

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

    # --- type-ahead -------------------------------------------------------------------

    def _prefix_index(self) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(sorted "name|ST" keys, first row, population, state, rank) of every US-state group.

        ``population`` is the twin's for a name that has a "<name> city" twin; ``rank``
        orders by it, the name before its twin on a tie."""
        index = self._prefix
        if index is None:
            with self._prefix_lock:
                index = self._prefix
                if index is None:
                    keys = sorted(key for key in self._exact if key[-2:] in US_STATES)
                    rows = np.array([self._exact[key] >> _COUNT_BITS for key in keys], dtype=np.int64)
                    population = self._population[rows] if len(rows) else np.zeros(0, dtype=np.int64)
                    population, has_twin = self._with_twin_population(keys, rows, population)
                    index = (
                        keys,
                        rows,
                        population,
                        np.array([key[-2:] for key in keys], dtype="<U2"),
                        population * 2 + has_twin,
                    )
                    self._prefix = index
        return index

    def _with_twin_population(
        self, keys: list[str], rows: np.ndarray, population: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """(``population``, has a twin): a name without population takes that of its "<name> city"
        twin in the same state when the twin is within ``_TWIN_MILES`` (the same place, two names)."""
        has_twin = np.zeros(len(population), dtype=np.int64)
        pairs = []
        for j, key in enumerate(keys):
            if " city|" not in key or population[j] <= 0:
                continue
            name, state = key.rsplit("|", 1)
            base = f"{name[: -len(' city')]}|{state}"
            i = bisect_left(keys, base)  # ``keys`` is sorted
            if i < len(keys) and keys[i] == base and population[i] == 0:
                pairs.append((i, j))
        if not pairs:
            return population, has_twin
        mine, twin = (np.array(side, dtype=np.int64) for side in zip(*pairs))
        near = haversine_miles(
            self._lat[rows[mine]], self._lon[rows[mine]], self._lat[rows[twin]], self._lon[rows[twin]]
        ) <= _TWIN_MILES
        ranked = population.copy()
        ranked[mine[near]] = population[twin[near]]
        has_twin[mine[near]] = 1
        return ranked, has_twin

    def search(self, query: str, limit: int = 8) -> tuple[list[dict], int]:
        """Places whose name starts with ``query``, most populated first: (rows, matches).

        ``query``: what is being typed, "chi" or "chi, il" (after the comma, a state code
        or name, or the start of one: "chi, i" keeps IA, ID, IL and IN). The name is
        normalized like every lookup (accents, "St." = "Saint", case). Fewer than
        ``MIN_QUERY_LETTERS`` letters, or a state part that is no US state: no rows.
        """
        parsed = parse_place_query(query)
        if parsed is None or limit <= 0:
            return [], 0
        prefix, states = parsed
        keys, rows, population, state_of, rank = self._prefix_index()
        low = bisect_left(keys, prefix)
        high = bisect_left(keys, prefix + _AFTER_EVERY_NAME, lo=low)
        if high <= low:
            return [], 0
        found = np.arange(low, high)
        if states is not None:
            found = found[np.isin(state_of[low:high], sorted(states))]
        matches = len(found)
        if matches > limit:
            # Top ``limit`` by population, then in name order (``found`` is sorted by key).
            found = found[np.argpartition(-rank[found], limit - 1)[:limit]]
        found = found[np.lexsort((found, -rank[found]))]
        return [self._suggestion(keys[i], int(rows[i]), int(population[i])) for i in found], matches

    def _suggestion(self, key: str, row: int, population: int) -> dict:
        name, state = key.split("|")
        city = " ".join(word[:1].upper() + word[1:] for word in name.split())
        return {
            "label": f"{city}, {state}",
            "city": city,
            "state": state,
            "lat": round(float(self._lat[row]), 6),
            "lon": round(float(self._lon[row]), 6),
            "population": population,
            # False in Alaska and Hawaii: a real US place, but no fuel prices there.
            "plannable": state not in _NO_FUEL_DATA_STATES,
        }

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


def _states_starting_with(text: str) -> set[str] | None:
    """US state codes whose code or name starts with ``text`` ("il", "ill", "new", "d.c.");
    None for an empty text (no filter)."""
    typed = " ".join(text.replace(".", "").split()).lower()
    if not typed:
        return None
    return {
        code
        for code, name in US_STATES.items()
        if code.lower().startswith(typed) or name.lower().startswith(typed)
        or (code == "DC" and len(typed) > len("washington") and "washington dc".startswith(typed))
    }


def parse_place_query(query: str) -> tuple[str, set[str] | None] | None:
    """("normalized name prefix", state codes or None) of a type-ahead query, or None
    when nothing should be suggested (too short, or a state part that is no US state).

    A space typed after a word is kept ("new " finds New York, not Newark).
    """
    text = (query or "").lstrip()
    states = None
    if "," in text:
        text, state_part = text.rsplit(",", 1)
        states = _states_starting_with(state_part)
        if states is not None and not states:
            return None
    prefix = normalize_place(text)
    if len(_LETTER.findall(prefix)) < MIN_QUERY_LETTERS:
        return None
    if text[-1:].isspace() and "," not in query:
        prefix += " "
    return prefix, states


@lru_cache(maxsize=1)
def get_place_index() -> PlaceIndex:
    """The process-wide index, loaded on first use (or by the warm-up at start-up)."""
    return PlaceIndex.from_file(settings.FUEL_PLANNER["PLACES_FILE"])
