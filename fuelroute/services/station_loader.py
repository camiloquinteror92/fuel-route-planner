"""Load the OPIS fuel price CSV into ``FuelStation``: parse, merge duplicates, geocode.

Runs once, from ``manage.py load_stations`` (never at request time). Steps:

1. ``parse_price_file``: one ``StationRow`` per OPIS ID. The file repeats some
   stations (same ID, same address, same rack) with different prices and no date,
   so the price used for planning follows ``PRICE_POLICY`` (median by default) and
   the min / max / number of quotes are kept so the spread is visible.
2. ``load_stations``: give each US station the coordinates of its city from the
   offline places index. City names that exist several times in the same state
   (homonyms far apart) are resolved with the highway in the station's address;
   when that is not conclusive the station is left WITHOUT coordinates (reported
   as ambiguous) instead of being placed hundreds of miles from where it is.
3. Exact positions: ``data/station_coords.csv`` (built once, offline, by
   ``scripts/build_station_coords.py`` from OpenStreetMap) gives some stations the
   position of their own fuel station ("osm_fuel") or of the highway exit of their
   address ("osm_exit"). Such a row replaces the city center, and places a station
   whose city could not be placed. No row, or no file: the city center, as before.
   The file is read from disk; nothing here touches the network.
4. Replace the table in one transaction. Rows get new primary keys, so every
   in-memory cache refers to stations by ``opis_id`` (stable) and checks the data
   version (see ``stations.py``).
"""

from __future__ import annotations

import csv
import logging
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction

from ..models import FuelStation
from .geo import haversine_miles
from .places import Place, PlaceIndex
from .stations import reset_station_arrays
from .text import US_STATES
from .usa import in_usa

logger = logging.getLogger(__name__)

PRICE_POLICIES = ("median", "min")
_FOUR_PLACES = Decimal("0.0001")

# Two places with the same name further apart than this are different places.
AMBIGUOUS_MILES = 25.0
# A homonym this populated, with every far-away namesake at least 20x smaller, is the
# one a truck stop address means ("Chesapeake, VA": the city of 235k, not a hamlet).
DOMINANT_POPULATION = 5000
DOMINANT_RATIO = 20
# Highway refs in addresses like "I-24, EXIT 62 & US-41 & SR-12". State routes are
# numbered per state, which is fine: anchors are always looked up inside one state.
_HIGHWAY = re.compile(r"\b(I|IH|US|SR|ST|SH|HWY|RT|FM)[- ]?(\d{1,4})\b")
_HIGHWAY_KIND = {"IH": "I", "ST": "SR", "SH": "SR", "HWY": "SR", "RT": "SR"}
# "EX 360" and "EXT 138C" are abbreviations of EXIT (as in scripts/build_station_coords.py).
_EXIT = re.compile(r"\b(?:EXIT|EXT|EX)(?![A-Z])\s*(\d{1,3})")

# geocode_source of a station placed by data/station_coords.csv: its own fuel station
# in OpenStreetMap, or the highway exit of its address.
EXACT_SOURCES = ("osm_fuel", "osm_exit")
_COORDS_COLUMNS = ("opis_id", "lat", "lon", "source")
# The builder never moves a station more than 25 mi from its city. A row further than
# this from every place the station's city can be does not belong to this price file
# (another version of one of the two files): it is ignored and reported.
EXACT_MAX_MILES_FROM_CITY = 30.0


@dataclass
class StationRow:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    prices: list[Decimal] = field(default_factory=list)

    def price(self, policy: str) -> Decimal:
        """The price used for planning, per ``policy`` ("median" or "min")."""
        if policy == "min":
            return min(self.prices)
        return Decimal(str(statistics.median(self.prices))).quantize(_FOUR_PLACES)


@dataclass
class LoadReport:
    rows_read: int = 0
    rows_invalid: int = 0
    unique_stations: int = 0
    non_us_skipped: int = 0
    geocoded: int = 0  # stations with coordinates (exact or city center)
    geocoded_by_source: dict[str, int] = field(default_factory=dict)
    homonyms_resolved: int = 0
    # City not placed (homonyms far apart / not in the places index). A station listed
    # here can still have coordinates from an exact row (``exact_without_city``).
    ambiguous: list[tuple[str, str]] = field(default_factory=list)
    unmatched: list[tuple[str, str]] = field(default_factory=list)
    # data/station_coords.csv
    coords_file: str = ""  # the file read, "" when there was none
    coords_rows: int = 0  # valid rows in it
    coords_invalid: int = 0  # rows skipped: bad number, unknown source, repeated opis_id
    coords_unused: int = 0  # rows for an opis_id that is not a US station of the price file
    coords_rejected: list[int] = field(default_factory=list)  # too far from the station's city
    exact: int = 0  # stations placed at their exact position
    exact_without_city: int = 0  # of them, stations whose city could not be placed

    @property
    def not_geocoded(self) -> int:
        """US stations left without coordinates (the planner ignores them)."""
        return self.unique_stations - self.non_us_skipped - self.geocoded


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_price_file(path: Path, report: LoadReport) -> dict[int, StationRow]:
    """Read the CSV and keep one row per OPIS ID with all its price quotes.

    The repeated rows of a station share ID, address, city and rack; only the name
    spelling and the price change, and the file has no date to tell which quote is
    the latest. Prices are rounded to 4 decimals (the file has up to 8).
    """
    stations: dict[int, StationRow] = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            report.rows_read += 1
            try:
                opis_id = int(_clean(raw["OPIS Truckstop ID"]))
                price = Decimal(_clean(raw["Retail Price"]))
            except (InvalidOperation, ValueError, KeyError):
                report.rows_invalid += 1
                continue
            if not price.is_finite() or price <= 0:
                report.rows_invalid += 1
                continue
            row = stations.get(opis_id)
            if row is None:
                rack = _clean(raw.get("Rack ID"))
                row = stations[opis_id] = StationRow(
                    opis_id=opis_id,
                    name=_clean(raw["Truckstop Name"]),
                    address=_clean(raw["Address"]),
                    city=_clean(raw["City"]),
                    state=_clean(raw["State"]).upper(),
                    rack_id=int(rack) if rack.isdigit() else None,
                )
            row.prices.append(price.quantize(_FOUR_PLACES))
    report.unique_stations = len(stations)
    return stations


def highway_refs(address: str) -> tuple[set[str], tuple[str, int] | None]:
    """Highways in an address and the exit, if any: "I-24, EXIT 62" -> ({"I-24"}, ("I-24", 62)).

    The exit belongs to the first interstate of the address.
    """
    text = address.upper()
    highways = [f"{_HIGHWAY_KIND.get(kind, kind)}-{number}" for kind, number in _HIGHWAY.findall(text)]
    exit_match = _EXIT.search(text)
    interstates = [h for h in highways if h.startswith("I-")]
    exit_ref = (interstates[0], int(exit_match.group(1))) if exit_match and interstates else None
    return set(highways), exit_ref


def _miles(a: Place, lat: float, lon: float) -> float:
    return float(haversine_miles(a.latitude, a.longitude, lat, lon))


def _is_ambiguous(candidates: list[Place]) -> bool:
    best = candidates[0]
    return any(_miles(other, best.latitude, best.longitude) > AMBIGUOUS_MILES for other in candidates[1:])


def _only_place_near(candidates: list[Place], distance, radius: float) -> Place | None:
    """The candidate nearest to a reference, if it is the ONLY place within ``radius``.

    ``distance(candidate)`` measures from the reference (an anchor station). Two
    candidates within ``radius`` that are themselves far apart mean we cannot tell.
    """
    near = sorted((c for c in candidates if distance(c) <= radius), key=distance)
    if near and all(_miles(c, near[0].latitude, near[0].longitude) <= AMBIGUOUS_MILES for c in near[1:]):
        return near[0]
    return None


def _exit_clue(row: StationRow, candidates: list[Place], anchors: dict) -> Place | None:
    """Interstate exits are numbered by mile marker. The anchor with the closest exit
    on the same interstate is about ``gap`` miles from the station, so the station's
    city must be the only candidate within gap + 20 mi of it (20 mi of slack because
    every coordinate is a city centroid)."""
    _highways, exit_ref = highway_refs(row.address)
    if not exit_ref:
        return None
    highway, exit_number = exit_ref
    with_exit = [a for a in anchors.get((row.state, highway), ()) if a[2] is not None]
    if not with_exit:
        return None
    lat, lon, anchor_exit = min(with_exit, key=lambda a: abs(a[2] - exit_number))
    gap = abs(anchor_exit - exit_number)
    if gap > 30:
        return None
    return _only_place_near(candidates, lambda c: _miles(c, lat, lon), radius=gap + 20)


def _population_clue(candidates: list[Place]) -> Place | None:
    """One candidate is a real town (5,000+ people) and every far-away namesake is at
    least 20 times smaller: "Chesapeake, VA" is the city of 235k, not a hamlet."""
    best = candidates[0]
    others = [c for c in candidates[1:] if _miles(c, best.latitude, best.longitude) > AMBIGUOUS_MILES]
    if best.population >= DOMINANT_POPULATION and all(
        c.population * DOMINANT_RATIO <= best.population for c in others
    ):
        return best
    return None


def _highway_clue(row: StationRow, candidates: list[Place], anchors: dict) -> Place | None:
    """The candidate nearest to a station on the same highway(s), if it is within
    20 mi and every other place is at least 25 mi further. Weaker than the other
    clues (a US route crosses the whole state), so it is only used alone."""
    highways, _exit = highway_refs(row.address)
    points = [a for highway in highways for a in anchors.get((row.state, highway), ())]
    if not points:
        return None

    def nearest_anchor(candidate: Place) -> float:
        return min(_miles(candidate, lat, lon) for lat, lon, _exit in points)

    ranked = sorted(candidates, key=nearest_anchor)
    winner, best = ranked[0], nearest_anchor(ranked[0])
    others = [c for c in ranked[1:] if _miles(c, winner.latitude, winner.longitude) > AMBIGUOUS_MILES]
    if best <= 20 and all(nearest_anchor(c) >= best + AMBIGUOUS_MILES for c in others):
        return winner
    return None


def _resolve_homonym(row: StationRow, candidates: list[Place], anchors: dict) -> Place | None:
    """Pick the homonym the station is in, or None if unsure.

    ``anchors[(state, highway)]`` lists (lat, lon, exit) of the stations of the same
    state whose city is NOT ambiguous and whose address names that highway.

    The exit clue and the population clue are used first; when both apply they
    must agree. The highway clue is used only when neither applies. A wrong guess
    would put a truck stop hundreds of miles away, where it would show up as a
    phantom candidate on other routes; a station left out only removes one option.
    """
    by_exit, by_population = _exit_clue(row, candidates, anchors), _population_clue(candidates)
    if by_exit and by_population:
        same = _miles(by_exit, by_population.latitude, by_population.longitude) <= AMBIGUOUS_MILES
        return by_exit if same else None
    return by_exit or by_population or _highway_clue(row, candidates, anchors)


def geocode_rows(rows: list[StationRow], places: PlaceIndex, report: LoadReport) -> dict[int, Place]:
    """Coordinates for each US station (by opis_id). Unmatched / ambiguous are left out."""
    located: dict[int, Place] = {}
    ambiguous: list[tuple[StationRow, list[Place]]] = []
    anchors: dict[tuple[str, str], list[tuple[float, float, int | None]]] = defaultdict(list)

    for row in rows:
        candidates = places.candidates(row.city, row.state)
        if not candidates:
            report.unmatched.append((row.city, row.state))
        elif len(candidates) > 1 and _is_ambiguous(candidates):
            ambiguous.append((row, candidates))
        else:
            place = located[row.opis_id] = candidates[0]
            highways, exit_ref = highway_refs(row.address)
            for highway in highways:
                exit_number = exit_ref[1] if exit_ref and exit_ref[0] == highway else None
                anchors[(row.state, highway)].append((place.latitude, place.longitude, exit_number))

    for row, candidates in ambiguous:
        place = _resolve_homonym(row, candidates, anchors)
        if place is None:
            report.ambiguous.append((row.city, row.state))
        else:
            located[row.opis_id] = place
            report.homonyms_resolved += 1
    return located


@dataclass(frozen=True)
class ExactPosition:
    """A row of data/station_coords.csv: where the station really is."""

    latitude: float
    longitude: float
    source: str  # one of EXACT_SOURCES


def read_station_coords(path: Path | None, report: LoadReport) -> dict[int, ExactPosition]:
    """The exact positions of ``data/station_coords.csv``, by opis_id ({} without the file).

    Only the columns opis_id, lat, lon and source are used (the others explain the
    match to a human). A row that cannot be trusted is skipped and counted: a number
    that does not parse or is out of range, a source that is not in EXACT_SOURCES,
    an opis_id listed twice (both rows are dropped: they disagree). A file without
    those columns is an error, not something to ignore silently.
    """
    if path is None or not Path(path).is_file():
        return {}
    report.coords_file = str(path)
    positions: dict[int, ExactPosition] = {}
    repeated: set[int] = set()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in _COORDS_COLUMNS if column not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for raw in reader:
            try:
                opis_id = int(_clean(raw["opis_id"]))
                lat, lon = float(raw["lat"]), float(raw["lon"])
            except (TypeError, ValueError):
                report.coords_invalid += 1
                continue
            source = _clean(raw["source"])
            valid = (
                source in EXACT_SOURCES
                and math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180
            )
            if not valid or opis_id in repeated:
                report.coords_invalid += 1
            elif opis_id in positions:
                del positions[opis_id]
                repeated.add(opis_id)
                report.coords_invalid += 2
            else:
                positions[opis_id] = ExactPosition(lat, lon, source)
    report.coords_rows = len(positions)
    return positions


def _trusted(row: StationRow, exact: ExactPosition | None, city: Place | None, places: PlaceIndex) -> bool:
    """Whether the exact row of a station can replace (or stand in for) its city center:
    it must be within EXACT_MAX_MILES_FROM_CITY of the city, or of one of the homonyms
    when the city is ambiguous. A city that is in no list cannot be checked that way:
    the row is used only if the point is in the USA (the offline outline, ``usa.py``)."""
    if exact is None:
        return False
    references = [city] if city else places.candidates(row.city, row.state)
    if not references:
        return bool(in_usa(exact.latitude, exact.longitude)[0])
    return any(_miles(place, exact.latitude, exact.longitude) <= EXACT_MAX_MILES_FROM_CITY for place in references)


@transaction.atomic
def load_stations(
    path: Path, places: PlaceIndex, price_policy: str = "median", coords_path: Path | None = None
) -> LoadReport:
    """Replace the FuelStation table with the content of the price file.

    ``coords_path``: the exact positions file (``data/station_coords.csv``); None, or
    a path with no file, places every station at its city center.
    """
    if price_policy not in PRICE_POLICIES:
        raise ValueError(f"price_policy must be one of {PRICE_POLICIES}")
    report = LoadReport()
    stations = parse_price_file(path, report)

    us_rows = []
    for row in stations.values():
        if row.state not in US_STATES:
            # Canadian truck stops (ON, AB, BC...). The offline places table only
            # covers the USA, so they cannot be placed on a map; and trip endpoints
            # must be in the USA. A route that crosses Canada is planned with US
            # stations only (see the README, "Known data gaps").
            report.non_us_skipped += 1
        else:
            us_rows.append(row)

    # City centers first, exactly as without the exact positions file: the homonym
    # resolution (and scripts/build_station_coords.py, which matches around these
    # centers) must not depend on it.
    located = geocode_rows(us_rows, places, report)
    exact_positions = read_station_coords(coords_path, report)
    report.coords_unused = len(exact_positions.keys() - {row.opis_id for row in us_rows})
    objects = []
    for row in us_rows:
        city, exact = located.get(row.opis_id), exact_positions.get(row.opis_id)
        if _trusted(row, exact, city, places):
            lat, lon, source = exact.latitude, exact.longitude, exact.source
            report.exact += 1
            if city is None:
                report.exact_without_city += 1
        else:
            if exact is not None:
                report.coords_rejected.append(row.opis_id)
            lat, lon, source = (city.latitude, city.longitude, city.source) if city else (None, None, "")
        if source:
            report.geocoded += 1
            report.geocoded_by_source[source] = report.geocoded_by_source.get(source, 0) + 1
        objects.append(
            FuelStation(
                opis_id=row.opis_id,
                name=row.name,
                address=row.address,
                city=row.city,
                state=row.state,
                rack_id=row.rack_id,
                price=row.price(price_policy),
                price_min=min(row.prices),
                price_max=max(row.prices),
                price_rows=len(row.prices),
                latitude=lat,
                longitude=lon,
                geocode_source=source,
            )
        )

    FuelStation.objects.all().delete()
    FuelStation.objects.bulk_create(objects, batch_size=1000)
    # This process forgets its arrays now; other processes (a running server) notice
    # the new data version on their next request (stations.get_station_arrays).
    transaction.on_commit(reset_station_arrays)
    logger.info(
        "event=stations_loaded unique=%s geocoded=%s exact=%s ambiguous=%s unmatched=%s non_us=%s policy=%s",
        report.unique_stations, report.geocoded, report.exact, len(report.ambiguous), len(report.unmatched),
        report.non_us_skipped, price_policy,
    )
    return report
