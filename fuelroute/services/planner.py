"""Orchestrates one trip request: from two place names to the JSON answer.

    start, finish ──► geocode ──► checks ──► route ──► stations ──► tank ──► optimizer ──► JSON
                     (offline)              (OSRM,    near route   rules    (3 rules)     + GeoJSON
                                            1 call)   (numpy)

1. ``geocode`` both inputs (0 external calls for "City, ST" and "lat,lon").
2. Cheap checks BEFORE spending the routing call: same place, Alaska / Hawaii (no
   price data), station table loaded.
3. Plan cache: the finished plan is cached by (station data version, rounded
   coordinates, every truck setting). A hit returns in a few ms with 0 external calls.
4. ``get_route``: the one OSRM call (itself cached by coordinates), already
   resampled every mile and simplified.
5. ``stations_along_route``: candidate stations within 10 miles, with mile markers.
6. ``_tank_rules``: how much fuel the truck leaves with and must arrive with
   (``start_tank``, see the README "Assumptions").
7. ``plan_fuel_stops``: where to stop and how much to buy.
8. Build the answer: stops (money computed in Decimal, so the stop costs add up to
   the total to the cent), summary, warnings, GeoJSON, meta. The summary compares
   the plan with a driver who ignores prices (same stations, same gallons).
9. ``details`` (sent only with ``include=details``): every strategy it was compared
   with and how the plan was computed (timings, routing geometry, corridor counts,
   tank rule, merges). Cached with the plan, so a cache hit still shows what the
   first computation cost. ``include=candidates`` adds every corridor station.

Truck settings (``PlanSettings``): mpg, range, corridor width, which price of a
station to use, merging tiny stops and a safety reserve can be changed per request.
They are part of the PLAN cache key but not of the ROUTE cache key, so another truck
on a trip already routed is planned in milliseconds with 0 external calls. Their
defaults are ``settings.FUEL_PLANNER``.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from ..models import FuelStation
from .errors import (
    LocationNotNearRoad,
    NoFuelDataInRegion,
    NoFuelDataOnRoute,
    NoReachableStation,
    PlannerError,
    SameLocation,
    StationDataChanged,
    StationDataNotLoaded,
)
from .geo import haversine_miles
from .geocoding import Location, geocode
from .http import ExternalApiClient
from .optimizer import (
    Candidate,
    FuelPlan,
    FuelStop,
    UnreachableError,
    plan_fuel_stops,
    plan_price_blind,
    plan_quarter_tank,
)
from .osrm import Route, get_route
from .stations import (
    COARSE_STEP_MILES,
    FUNNEL_KEYS,
    PRICE_POLICIES,
    CorridorStation,
    data_version,
    get_station_arrays,
    stations_along_route,
)
from .usa import LOWER48

logger = logging.getLogger(__name__)

START_EMPTY = "empty"
START_FULL = "full"
# Plain-language name and one-line meaning of each start_tank mode (``/api/about``, the page).
START_TANK_LABELS = {START_EMPTY: "Almost empty: pay for every mile", START_FULL: "Full: the first tank is free"}
START_TANK_HELP = {
    START_EMPTY: (
        "The truck leaves with just enough fuel to reach a truck stop and must arrive with the same amount. "
        "So the bill is exactly the fuel the trip burns."
    ),
    START_FULL: "The truck leaves with a full tank that costs nothing. Only the fuel bought on the way is in the bill.",
}

# Why the truck leaves / arrives with that fuel (``pipeline.tank.reason``).
TANK_FULL = "full_tank"
TANK_RESERVE = "reserve"
TANK_FIRST_STATION = "first_station_beyond_reserve"
TANK_SAFETY_RESERVE = "safety_reserve"  # leaves with more to reach the first station above the safety reserve
# Leaves with less than the reserve: the last station is so far from the destination that
# the truck can only arrive with that little, and it must arrive with what it left with.
TANK_LAST_STRETCH = "last_stretch"
TANK_NO_STATION = "no_station_on_route"

# Allowed range of each numeric truck setting of /api/route (validated by the
# serializer, published by /api/about). safety_reserve_gal: from 0 to less than the tank.
WHAT_IF_RANGES = {
    "mpg": (3.0, 30.0),
    "max_range_miles": (100.0, 1500.0),
    "corridor_miles": (1.0, 50.0),
}
SETTING_NAMES = (
    "start_tank", "mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal",
)


@dataclass(frozen=True)
class PlanSettings:
    """Every setting that changes a plan (not the route). Field names are the request's.

    ``defaults()`` reads ``settings.FUEL_PLANNER``; ``resolve(**overrides)`` fills the
    ones a request did not send (None) from it.
    """

    start_tank: str
    mpg: float
    max_range_miles: float
    corridor_miles: float
    price_policy: str
    consolidate: bool
    safety_reserve_gal: float

    @classmethod
    def defaults(cls) -> PlanSettings:
        config = settings.FUEL_PLANNER
        return cls(
            start_tank=START_EMPTY,
            mpg=float(config["MILES_PER_GALLON"]),
            max_range_miles=float(config["MAX_RANGE_MILES"]),
            corridor_miles=float(config["CORRIDOR_MILES"]),
            price_policy="median",
            # Off: the plan is the pure cheapest one (the three rules). On, a stop that
            # buys less than MIN_STOP_GALLONS is merged into a neighbour when that costs
            # at most MAX_CONSOLIDATION_COST more.
            consolidate=False,
            safety_reserve_gal=0.0,
        )

    @classmethod
    def resolve(cls, **overrides) -> PlanSettings:
        base = cls.defaults()
        values = {name: getattr(base, name) for name in SETTING_NAMES}
        for name, value in overrides.items():
            if name not in values:
                raise TypeError(f"unknown setting {name!r}")
            if value is None or value == "":
                continue
            if name == "consolidate":
                text = isinstance(value, str)
                values[name] = value.strip().lower() in ("true", "1", "yes", "on") if text else bool(value)
            elif name in ("start_tank", "price_policy"):
                values[name] = str(value)
            else:
                try:
                    values[name] = float(value)
                except (TypeError, ValueError) as exc:
                    raise PlannerError(f"{name} must be a number.", field=name) from exc
        resolved = cls(**values)
        resolved.check()
        return resolved

    def check(self) -> None:
        """Planner-level guard (the serializer gives the user-facing messages)."""
        if self.start_tank not in (START_EMPTY, START_FULL):
            raise PlannerError(f"start_tank must be '{START_EMPTY}' or '{START_FULL}'.")
        if self.price_policy not in PRICE_POLICIES:
            raise PlannerError(f"price_policy must be one of: {', '.join(PRICE_POLICIES)}.")
        numbers = (self.mpg, self.max_range_miles, self.corridor_miles)
        if not all(math.isfinite(number) and number > 0 for number in numbers):
            raise PlannerError("mpg, max_range_miles and corridor_miles must be positive numbers.")
        if not 0 <= self.safety_reserve_gal < self.tank_gallons:
            raise PlannerError(
                f"safety_reserve_gal must be at least 0 and less than the tank ({self.tank_gallons:g} gal).",
                field="safety_reserve_gal",
            )

    @property
    def tank_gallons(self) -> float:
        return self.max_range_miles / self.mpg

    @property
    def safety_reserve_miles(self) -> float:
        return self.safety_reserve_gal * self.mpg

    @property
    def usable_range_miles(self) -> float:
        """Miles a full tank covers while keeping the safety reserve."""
        return self.max_range_miles - self.safety_reserve_miles

    @property
    def min_stop_gallons(self) -> float:
        return float(settings.FUEL_PLANNER["MIN_STOP_GALLONS"]) if self.consolidate else 0.0

    def changed(self) -> list[str]:
        """Names of the settings that differ from the defaults, in ``SETTING_NAMES`` order."""
        base = PlanSettings.defaults()
        return [name for name in SETTING_NAMES if getattr(self, name) != getattr(base, name)]

    def cache_key(self) -> str:
        return "|".join(repr(getattr(self, name)) for name in SETTING_NAMES)

    def query(self) -> dict:
        """The request parameters that reproduce these settings (only the changed ones)."""
        return {name: _param_text(getattr(self, name)) for name in self.changed()}

    def vehicle(self) -> dict:
        """``vehicle`` block of the response: every effective setting (``mpg`` of the
        request is ``miles_per_gallon``; the other names are the request's)."""
        return {
            "miles_per_gallon": self.mpg,
            "tank_gallons": _round(self.tank_gallons, 2),
            "max_range_miles": self.max_range_miles,
            "safety_reserve_gal": self.safety_reserve_gal,
            # How far a full tank goes while keeping the safety reserve.
            "usable_range_miles": _round(self.usable_range_miles, 1),
            "start_tank": self.start_tank,
            "consolidate": self.consolidate,
            "corridor_miles": self.corridor_miles,
            "price_policy": self.price_policy,
        }


def _param_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".10g")
    return str(value)


CENT = Decimal("0.01")
_PRICE_PLACES = Decimal("0.0001")
_TENTH = Decimal("0.1")
# Inputs closer than this are the same place ("Austin, TX" vs "Austin, Texas").
SAME_PLACE_MILES = 0.5
_NO_DATA_HINT = "The price file has no stations along that stretch (see the README, 'Known data gaps')."
CANDIDATE_FIELDS = [
    "opis_id", "name", "city", "state", "lat", "lon",
    "mile_marker", "distance_from_route_miles", "price_per_gallon", "stop",
]


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _fixed(value: float, places: int) -> str:
    """``value`` with ``places`` decimals, rounded half up like the page (6.25 -> "6.3", not "6.2")."""
    return str(Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _gal(value: float) -> str:
    """Gallons in a sentence, to the hundredth: "5.0", "4.96", "6.25" (never "5.0 instead of 5.0")."""
    text = _fixed(value, 2)
    return text[:-1] if text.endswith("0") else text


def _short(value: float) -> str:
    """A configured quantity without useless zeros: 50 -> "50", 62.5 -> "62.5", 41.666 -> "41.67"."""
    return f"{float(_fixed(value, 2)):g}"


def _miles_text(miles: float) -> str:
    """Miles in a sentence: whole miles, one decimal under ten ("0.1", not "0")."""
    return _fixed(miles, 1) if abs(miles) < 10 else _fixed(miles, 0)


class _Timer:
    """Milliseconds spent in each step, reported in ``details.timings_ms``."""

    def __init__(self):
        self.timings: dict[str, float] = {}
        self._clock = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.timings[name] = _round((now - self._clock) * 1000, 1)
        self._clock = now


@dataclass
class TankRules:
    """Fuel (in miles of range) at departure and required at arrival, plus why."""

    initial: float
    final: float
    note: str
    warnings: list[str] = field(default_factory=list)
    reason: str = ""  # TANK_*
    # The last stretch is too long to arrive with the required fuel: the truck
    # arrives with less, and the difference is unpriced (there is a warning).
    arrival_capped: bool = False


def plan_trip(
    start: str,
    finish: str,
    start_tank: str = START_EMPTY,
    client: ExternalApiClient | None = None,
    include: frozenset = frozenset(),
    **what_if,
) -> dict:
    """Plan a trip from ``start`` to ``finish`` (free text, see geocoding.py).

    Returns the response body (a dict). Raises a ``PlannerError`` subclass for
    every expected failure; ``views.py`` turns it into the HTTP error response.
    Pass ``client`` to read the number of external calls even when it raises.
    ``include``: optional extra blocks ("candidates"); they never change the plan
    nor its cache key. ``what_if``: the optional truck settings of ``PlanSettings``
    (mpg, max_range_miles, corridor_miles, price_policy, consolidate,
    safety_reserve_gal); None or missing means the default.

    On a ``PlannerError`` the timings of the steps completed before it are attached
    to the exception (``timings_ms``), for the Server-Timing header of the error.
    """
    timer = _Timer()
    try:
        plan_settings = PlanSettings.resolve(start_tank=start_tank or START_EMPTY, **what_if)
        return _plan_trip(start, finish, plan_settings, client or ExternalApiClient(), frozenset(include), timer)
    except PlannerError as exc:
        exc.timings_ms = dict(timer.timings)
        raise


def _plan_trip(
    start: str, finish: str, plan: PlanSettings, client: ExternalApiClient, include: frozenset, timer: _Timer
) -> dict:
    origin = geocode(start, client, field="start")
    destination = geocode(finish, client, field="finish")
    _check_endpoints(origin, destination)
    timer.lap("geocoding_ms")

    version = data_version()
    if version.startswith("0-"):
        raise StationDataNotLoaded("The station table is empty. Run `python manage.py load_stations` first.")

    # The route is cached apart (osrm.get_route), keyed by the coordinates only: a
    # changed truck misses this key but hits the route, so it costs no external call.
    plan_key = f"plan:v7:{version}:{origin.as_param};{destination.as_param};{plan.cache_key()}"
    cached_plan = cache.get(plan_key)  # the cache returns a fresh copy (unpickled)
    timer.lap("plan_cache_ms")
    if cached_plan is not None:
        compact = cached_plan.pop("_candidates", [])
        cached_plan["start"]["query"], cached_plan["finish"]["query"] = start, finish
        cached_plan["map"]["map_url"] = map_path(origin, destination, plan)
        cached_plan["warnings"] = _geocoder_warnings(origin, destination) + cached_plan["warnings"]
        if "candidates" in include:
            cached_plan["candidates"] = _build_candidates(compact, cached_plan["fuel_stops"])
            timer.lap("candidates_ms")
        cached_plan["meta"] = _meta(client, "hit", "hit", plan)
        # ``details.pipeline`` is left as it was: it describes the computation being reused.
        cached_plan["details"].update(_request_details(client, version, timer))
        return cached_plan

    route, route_from_cache = get_route(origin, destination, client)
    _check_snapping(route, origin, destination)
    timer.lap("routing_ms")

    funnel: dict = {}
    arrays = get_station_arrays(version)
    corridor = stations_along_route(
        route.samples,
        route.sample_miles,
        plan.corridor_miles,
        arrays.priced(plan.price_policy),
        route.sample_in_usa,
        stats=funnel,
    )
    funnel["stations_with_several_prices"] = _with_several_prices(arrays, corridor)
    timer.lap("corridor_ms")

    tank = _tank_rules(plan, route, corridor)
    fuel_plan = _optimize(route, corridor, tank, plan)
    timer.lap("optimizer_ms")

    stops, total_cost, total_gallons = _build_stops(fuel_plan, route.distance_miles, plan)
    summary = _build_summary(route, corridor, tank, fuel_plan, stops, total_cost, total_gallons, plan)
    result = {
        "start": origin.to_dict(),
        "finish": destination.to_dict(),
        "route": {
            "distance_miles": _round(route.distance_miles, 1),
            "duration_hours": _round(route.duration_seconds / 3600, 2),
            "miles_outside_usa": _round(route.miles_outside_usa, 0),
        },
        "vehicle": plan.vehicle(),
        "summary": summary,
        "warnings": _warnings(route, tank),
        "fuel_stops": stops,
        "map": {
            "map_url": map_path(origin, destination, plan),
            "geojson": _build_geojson(route, origin, destination, stops),
        },
    }
    timer.lap("response_build_ms")

    comparison = _build_comparison(route, corridor, tank, fuel_plan, plan)
    summary["comparison"] = _short_comparison(comparison)
    timer.lap("comparison_ms")

    result["meta"] = _meta(client, "hit" if route_from_cache else "miss", "miss", plan)
    result["details"] = {
        **_request_details(client, version, timer),
        "road_snap_miles": {"start": _round(route.snap_miles[0], 2), "finish": _round(route.snap_miles[1], 2)},
        "comparison": comparison,
        "pipeline": _build_pipeline(
            client, route, route_from_cache, funnel, corridor, tank, fuel_plan, total_cost, timer, plan
        ),
    }
    result["_candidates"] = _compact_candidates(corridor)
    cache.set(plan_key, result)
    compact = result.pop("_candidates")  # private: kept in the cache, never sent
    # They depend on the text typed, not on the coordinates: never cached.
    result["warnings"] = _geocoder_warnings(origin, destination) + result["warnings"]
    if "candidates" in include:
        result["candidates"] = _build_candidates(compact, stops)
        timer.lap("candidates_ms")
    logger.info(
        "event=route_planned start=%r finish=%r miles=%.0f stops=%s cost=%s external_calls=%s route_cache=%s",
        origin.label, destination.label, route.distance_miles, len(stops), total_cost,
        client.call_count, result["meta"]["route_cache"],
    )
    return result


def _request_details(client: ExternalApiClient, version: str, timer: _Timer) -> dict:
    """The parts of ``details`` that belong to THIS request (a cache hit replaces them).
    ``timings_ms`` is the timer's own dict, so a step timed later (candidates) shows too."""
    return {
        "timings_ms": timer.timings,
        "external_api_ms": _round(client.elapsed_ms, 1),
        "station_data_version": version,
    }


def map_path(start: Location, finish: Location, plan: PlanSettings | str) -> str:
    """Relative URL of the HTML map for this trip (the view makes it absolute).

    It carries the same inputs as the API call, so while the plan is cached the map
    page makes no external call; after the cache expires it plans again (1 call).
    Truck settings are added only when they differ from the defaults, so the link
    of a default plan is the same as before they existed.
    """
    if isinstance(plan, str):  # a bare start_tank
        plan = PlanSettings.resolve(start_tank=plan)
    params = {"start": start.query, "finish": finish.query, "start_tank": plan.start_tank}
    params.update({name: value for name, value in plan.query().items() if name != "start_tank"})
    return f"{reverse('route-map')}?{urlencode(params)}"


# --- checks ---------------------------------------------------------------------------


def _check_endpoints(origin: Location, destination: Location) -> None:
    """Reject, before the routing call, trips that cannot be planned."""
    if haversine_miles(origin.latitude, origin.longitude, destination.latitude, destination.longitude) < SAME_PLACE_MILES:
        raise SameLocation("start and finish are the same place.")
    for field_name, location in (("start", origin), ("finish", destination)):
        if location.region != LOWER48:
            raise NoFuelDataInRegion(
                f"'{location.query}' is in {location.region.title()}. The price file has no stations in "
                "Alaska or Hawaii, so the fuel cost of this trip cannot be planned.",
                field=field_name,
            )


def _check_snapping(route: Route, origin: Location, destination: Location) -> None:
    """OSRM moves each point to the nearest road; far means the point is not on land/roads."""
    limit = settings.FUEL_PLANNER["MAX_ROAD_SNAP_MILES"]
    for field_name, location, snap in (("start", origin, route.snap_miles[0]), ("finish", destination, route.snap_miles[1])):
        if snap > limit:
            raise LocationNotNearRoad(
                f"'{location.query}' is {snap:.1f} miles from the nearest road the router knows "
                f"(limit {limit:.0f}). Use a point on or near a road.",
                field=field_name,
                snap_miles=_round(snap, 1),
            )


# --- tank rules and optimizer ------------------------------------------------------------


def _tank_rules(plan: PlanSettings, route: Route, corridor: list[CorridorStation]) -> TankRules:
    """Fuel at departure and at arrival, in miles of range.

    ``full``: leave with a full tank, arrive with whatever is needed (0, or the
    safety reserve); only the fuel bought on the way is counted.

    ``empty`` (default): the trip pays for EVERY mile it drives. The truck leaves
    with the ``START_RESERVE_MILES`` reserve and must arrive with the same amount,
    so fuel bought = fuel burned. Two real-data cases need more than that:

    * the first station of the file on this route is further than the reserve
      (Houston: mile 74; Los Angeles: mile ~250, the file has 8 stations in CA):
      the truck leaves with enough fuel to reach it, and must ARRIVE with that same
      amount ("return the tank as you got it"), so every mile is still paid;
    * the last station is so far from the destination that arriving with that much
      fuel is impossible: the truck LEAVES with what it can still have on arrival
      (a full tank at the last station minus the last stretch), so every mile is
      still paid. Only when that is too little to reach the first station does it
      arrive with less than it left with; the difference is reported as unpriced
      fuel, with a warning.

    A safety reserve (``safety_reserve_gal``) must still be in the tank on arrival at
    the first station, so the truck leaves with at least that plus the miles to it.
    The optimizer keeps it at every other stop and at the destination.
    """
    capacity = plan.max_range_miles
    mpg = plan.mpg
    safety = plan.safety_reserve_miles
    safety_text = (
        f" It never arrives anywhere with less than the {plan.safety_reserve_gal:g}-gal safety reserve."
        if safety > 0
        else ""
    )
    if plan.start_tank == START_FULL:
        note = (
            f"The truck leaves with a full tank ({_short(capacity / mpg)} gal) that costs nothing, so it is not in "
            "the total. The plan buys only what it needs to reach the destination, so it may arrive nearly empty."
            if safety <= 0
            else f"The truck leaves with a full tank ({_short(capacity / mpg)} gal) that costs nothing, so it is not "
            "in the total. The plan buys only what it needs to reach the destination with the safety reserve."
        )
        return TankRules(capacity, 0.0, note + safety_text, reason=TANK_FULL)

    reserve = min(settings.FUEL_PLANNER["START_RESERVE_MILES"], capacity)
    route_miles = route.distance_miles
    if not corridor:
        # Nothing can be bought on this route. Only a trip the reserve covers works.
        initial = min(capacity, max(reserve, safety))
        final = max(0.0, initial - route_miles)
        rules = TankRules(initial, final, "", reason=TANK_NO_STATION, arrival_capped=final < initial)
        rules.note = "No station of the price file is on this route, so no fuel can be bought or priced."
        rules.warnings.append(
            f"No station of the price file is within {plan.corridor_miles:g} miles of this route. "
            f"The {_fixed(route_miles / mpg, 2)} gal burned come from the {_miles_text(initial)}-mile reserve and are "
            "not priced."
        )
        return rules

    first = min(c.mile for c in corridor)
    last = max(c.mile for c in corridor)
    initial = min(capacity, max(reserve, first + safety))
    if first > reserve:
        reason = TANK_FIRST_STATION
    elif first + safety > reserve:
        reason = TANK_SAFETY_RESERVE
    else:
        reason = TANK_RESERVE
    warnings = []
    if first > reserve:
        warnings.append(
            f"The first station of the price file on this route is at mile {_miles_text(first)}, beyond the "
            f"{_miles_text(reserve)}-mile reserve. The truck is assumed to leave with {_gal(initial / mpg)} gal to "
            "reach it and must arrive with the same amount, so every mile is still paid for."
        )
    # The most the truck can still have on arrival: a full tank at the last station,
    # minus the last stretch. It must arrive with what it left with, so when that is
    # less than the reserve it LEAVES with less (still enough to reach the first
    # station): the books balance and every mile is still paid for.
    last_gap = route_miles - last
    reachable = capacity - last_gap
    lowered = max(reachable, first + safety)
    if lowered < initial:
        initial, reason = lowered, TANK_LAST_STRETCH
    final = min(initial, max(0.0, reachable))
    rules = TankRules(initial, final, "", warnings=warnings, reason=reason, arrival_capped=final < initial)
    if rules.arrival_capped:
        # Even leaving with just enough to reach the first station, the truck cannot
        # arrive with that much: the difference is burned without a station to buy from.
        rules.warnings.append(
            f"The last station of the price file is {_miles_text(last_gap)} miles before the destination, so the "
            f"truck can only arrive with {_gal(final / mpg)} gal instead of {_gal(initial / mpg)}: "
            f"{_gal((initial - final) / mpg)} gal burned on that stretch are not priced."
        )
    why = {
        TANK_RESERVE: f"{_miles_text(reserve)} miles of fuel, enough to reach a truck stop",
        TANK_FIRST_STATION: f"enough to reach the first station of the price file, at mile {_miles_text(first)}",
        TANK_SAFETY_RESERVE: "enough to reach the first station with the safety reserve",
        TANK_LAST_STRETCH: (
            f"less than the usual {_miles_text(reserve)} miles: the last station is {_miles_text(last_gap)} miles "
            "before the destination, so that is all it can still have on arrival"
        ),
    }[reason]
    rules.note = (
        f"The truck leaves with {_gal(initial / mpg)} gal ({why}) and must arrive with the same amount, "
        "so the fuel bought is exactly the fuel the trip burns"
        + (" (except the unpriced fuel in 'warnings')" if rules.arrival_capped else "")
        + "."
        + safety_text
    )
    return rules


def _candidates_for(corridor: list[CorridorStation]) -> list[Candidate]:
    return [Candidate(mile=c.mile, price=c.price, ref=c) for c in corridor]


def _optimize(route: Route, corridor: list[CorridorStation], tank: TankRules, plan: PlanSettings) -> FuelPlan:
    config = settings.FUEL_PLANNER
    capacity = plan.max_range_miles
    safety = plan.safety_reserve_miles
    route_miles = route.distance_miles
    if not corridor and route_miles + max(tank.final, safety) > tank.initial + 1e-9:
        full_tank_covers_it = route_miles <= plan.usable_range_miles and tank.initial < capacity
        raise NoFuelDataOnRoute(
            f"No station of the price file is within {plan.corridor_miles:g} miles of this "
            f"{route_miles:.0f}-mile route, so fuel cannot be bought or priced. The file has few or no "
            "stations in some areas (for example only 8, all in the far south-east, in California)."
            + (
                f" Leaving with a full tank (start_tank=full), a trip under {_miles_text(plan.usable_range_miles)} "
                "miles needs no stop."
                if full_tank_covers_it
                else ""
            ),
            route_distance_miles=_round(route_miles, 1),
        )
    try:
        return plan_fuel_stops(
            route_miles,
            _candidates_for(corridor),
            max_range_miles=capacity,
            miles_per_gallon=plan.mpg,
            initial_fuel_miles=tank.initial,
            final_fuel_miles=tank.final,
            min_stop_gallons=plan.min_stop_gallons,
            max_consolidation_cost=config["MAX_CONSOLIDATION_COST"],
            min_fuel_miles=safety,
        )
    except UnreachableError as exc:
        raise _unreachable(exc, route, plan) from exc


def _point_at(route: Route, mile: float) -> dict:
    index = int(np.clip(np.searchsorted(route.sample_miles, mile), 0, len(route.sample_miles) - 1))
    lat, lon = route.samples[index]
    return {"mile": _round(mile, 1), "lat": _round(lat, 5), "lon": _round(lon, 5)}


def _unreachable(exc: UnreachableError, route: Route, plan: PlanSettings) -> NoReachableStation:
    """A 422 that says exactly which stretch has no station, and where it is."""
    start_mile = exc.from_mile
    end_mile = route.distance_miles if exc.next_mile is None else exc.next_mile
    if plan.safety_reserve_miles > 0:
        range_text = (
            f"{_miles_text(plan.usable_range_miles)}-mile usable range (the {_miles_text(plan.max_range_miles)}-mile "
            f"range minus the {plan.safety_reserve_gal:g}-gal safety reserve)"
        )
    else:
        range_text = f"{_miles_text(plan.max_range_miles)}-mile range"
    if exc.at_start:
        message = f"The first station on this route is at mile {end_mile:.0f}, more than the {range_text}."
    elif exc.next_mile is None:
        message = (
            f"The last station on this route is at mile {start_mile:.0f}, {end_mile - start_mile:.0f} miles "
            f"before the destination: more than the {range_text}."
        )
    else:
        message = (
            f"No station between mile {start_mile:.0f} and mile {end_mile:.0f} "
            f"({end_mile - start_mile:.0f} miles): more than the {range_text}."
        )
    hint = _NO_DATA_HINT
    if plan.changed():
        hint += (
            " A larger range, a smaller safety reserve or a wider corridor may make it plannable."
        )
    return NoReachableStation(
        f"{message} {hint}",
        from_mile=_round(start_mile, 1),
        next_station_mile=None if exc.next_mile is None else _round(exc.next_mile, 1),
        gap_miles=_round(end_mile - start_mile, 1),
        gap_start=_point_at(route, start_mile),
        gap_end=_point_at(route, end_mile),
        route_distance_miles=_round(route.distance_miles, 1),
    )


# --- response ----------------------------------------------------------------------------


def _money_rows(stops: list[FuelStop]) -> tuple[list[tuple[Decimal, Decimal, Decimal]], Decimal, Decimal]:
    """(gallons, price, cost) per stop and the totals, all in Decimal.

    gallons are rounded to 2 decimals first and the price is the stored 4-decimal
    one, so ``gallons * price_per_gallon`` matches ``cost`` to the cent, and the
    totals are the sums of the rows. Every money figure of the response (the plan
    and the strategies it is compared with) goes through here.
    """
    rows, total_cost, total_gallons = [], Decimal("0"), Decimal("0")
    for stop in stops:
        gallons = Decimal(f"{stop.gallons:.6f}").quantize(CENT, rounding=ROUND_HALF_UP)
        price = Decimal(f"{stop.candidate.price:.4f}")
        cost = _money(gallons * price)
        rows.append((gallons, price, cost))
        total_cost += cost
        total_gallons += gallons
    return rows, total_cost, total_gallons


def _average_price(total_cost: Decimal, total_gallons: Decimal) -> float | None:
    return float((total_cost / total_gallons).quantize(_PRICE_PLACES)) if total_gallons else None


def _build_stops(
    plan: FuelPlan, route_miles: float, plan_settings: PlanSettings
) -> tuple[list[dict], Decimal, Decimal]:
    """Stop rows with exact money (see ``_money_rows``) and why each stop is there."""
    opis_ids = [stop.candidate.ref.opis_id for stop in plan.stops]
    # The other stations a decision names: the cheaper one a stop buys fuel to reach,
    # and the stops the merges removed.
    named = [stop.cheaper_station.ref.opis_id for stop in plan.stops if stop.cheaper_station is not None]
    named += [stop.candidate.ref.opis_id for stop in plan.before_consolidation]
    stations = FuelStation.objects.in_bulk(set(opis_ids + named), field_name="opis_id")
    if len(stations) != len(set(opis_ids + named)):
        # load_stations replaced the table while this request was running.
        raise StationDataChanged("The station data changed during the request; please send it again.")

    money, total_cost, total_gallons = _money_rows(plan.stops)
    tank_gallons = plan_settings.tank_gallons
    tank_shown = _round(tank_gallons)
    stop_of_greedy = {stop.greedy_index: n for n, stop in enumerate(plan.stops, start=1)}
    stops = []
    for number, (stop, (gallons, price, cost)) in enumerate(zip(plan.stops, money), start=1):
        info: CorridorStation = stop.candidate.ref
        station = stations[info.opis_id]
        stops.append(
            {
                "stop": number,
                "opis_id": station.opis_id,
                "name": station.name,
                "address": station.address,
                "city": station.city,
                "state": station.state,
                "lat": station.latitude,
                "lon": station.longitude,
                "price_per_gallon": float(price),
                "mile_marker": _round(info.mile, 1),
                "distance_from_route_miles": _round(info.offset_miles, 1),
                # Rounded so that arrival + purchase never shows a tank above full
                # (15.375 + 47.125 of a 62.5-gal tank: 15.37 + 47.13, not 15.38 + 47.13).
                "fuel_on_arrival_gallons": max(
                    0.0, min(_round(stop.fuel_on_arrival_gallons), _round(tank_shown - float(gallons)))
                ),
                "gallons": float(gallons),
                "cost": float(cost),
                "decision": _decision(
                    stop, number, plan, route_miles, tank_gallons, stop_of_greedy, stations, plan_settings.consolidate
                ),
            }
        )
    return stops, total_cost, total_gallons


def _station_brief(candidate: Candidate, stations: dict) -> dict:
    """A station a decision names: where it is and its price."""
    station = stations[candidate.ref.opis_id]
    return {
        "name": station.name,
        "city": station.city,
        "state": station.state,
        "mile": _round(candidate.mile, 1),
        "price_per_gallon": float(Decimal(f"{candidate.price:.4f}")),
    }


def _decision(
    stop: FuelStop,
    number: int,
    plan: FuelPlan,
    route_miles: float,
    tank_gallons: float,
    stop_of_greedy: dict,
    stations: dict,
    merges: bool,
) -> dict:
    """Why this stop of the FINAL plan exists and what its purchase covers.

    * ``rule``: the rule that created the stop (``reach_cheaper``, ``finish``, ``fill_up``)
      and, for ``reach_cheaper``, the ``cheaper_station`` it buys just enough to reach.
    * ``reaches``: where the fuel bought here takes the truck in the final plan, the
      next stop or the destination (``stop`` null). ``fills_tank``: leaves full.
    * Merges (``consolidate=true`` only, ``merges``; the keys are left out otherwise):
      ``consolidated`` says a merge changed this stop; ``greedy_gallons`` is what the three rules alone buy here;
      ``moved_in`` is fuel the rules had planned at another station and the merge moved
      here (``from_stop``: that station's stop in this plan, null when the merge removed
      it); ``moved_out`` is fuel of this station now bought at another stop;
      ``consolidation_extra_cost`` is what the fuel moved here costs more at this price.
      Summed over the stops it is what the merges cost.
    """
    following = plan.stops[number] if number < len(plan.stops) else None
    greedy = plan.before_consolidation
    price = Decimal(f"{stop.candidate.price:.4f}")
    extra = Decimal(0)
    moved_in = []
    for k, gallons in sorted(stop.sources.items()):
        if k == stop.greedy_index or gallons < 0.005:
            continue
        origin = greedy[k]
        extra += Decimal(f"{gallons:.6f}") * (price - Decimal(f"{origin.candidate.price:.4f}"))
        place = stations[origin.candidate.ref.opis_id]
        moved_in.append(
            {
                "mile": _round(origin.candidate.mile, 1),
                "city": place.city,
                "state": place.state,
                "gallons": _round(gallons),
                "from_stop": stop_of_greedy.get(k),
            }
        )
    moved_out = [
        {"mile": _round(other.candidate.mile, 1), "gallons": _round(other.sources[stop.greedy_index]), "to_stop": n}
        for n, other in enumerate(plan.stops, start=1)
        if other is not stop and other.sources.get(stop.greedy_index, 0.0) >= 0.005
    ]
    cheaper = stop.cheaper_station
    decision = {
        "rule": stop.rule,
        "cheaper_station": None if cheaper is None else _station_brief(cheaper, stations),
        "reaches": {
            "stop": number + 1 if following else None,
            "mile": _round(following.candidate.mile if following else route_miles, 1),
        },
        "fills_tank": stop.fuel_on_arrival_gallons + stop.gallons >= tank_gallons - 0.005,
    }
    if merges:
        decision.update(
            consolidated=stop.consolidated,
            greedy_gallons=_round(stop.greedy_gallons),
            moved_in=moved_in,
            moved_out=moved_out,
            consolidation_extra_cost=float(_money(extra)),
        )
    return decision


def _build_summary(
    route, corridor, tank, plan, stops, total_cost: Decimal, total_gallons: Decimal, plan_settings: PlanSettings
) -> dict:
    mpg = plan_settings.mpg
    fuel_used = route.distance_miles / mpg
    prices = _price_stats(corridor)
    merged = None
    if plan_settings.consolidate:
        _, cost_before, _ = _money_rows(plan.before_consolidation)
        merged = {
            "stops_before": len(plan.before_consolidation),
            "extra_cost": float(total_cost - cost_before),
        }
    return {
        "total_fuel_cost": float(total_cost),
        "total_gallons_purchased": float(total_gallons),
        "number_of_stops": len(stops),
        "average_price_paid": _average_price(total_cost, total_gallons),
        "fuel_used_gallons": _round(fuel_used),
        "start_fuel_gallons": _round(tank.initial / mpg),
        "end_fuel_gallons": _round(plan.final_fuel_gallons),
        # Fuel burned that the total does not include: the free full tank in "full"
        # mode; normally 0 in "empty" mode (see warnings when it is not).
        "unpriced_fuel_gallons": _round(max(0.0, fuel_used - plan.total_gallons)),
        "candidate_stations_on_route": len(corridor),
        "price_per_gallon_on_route": None if prices is None else {"min": prices["min"], "max": prices["max"]},
        # consolidate=true: how many stops the three rules alone make, and what merging costs.
        "tiny_stops_merged": merged,
        "note": tank.note,
    }


# --- comparison with simple drivers --------------------------------------------------------


def _strategy(label: str, rule: str, stops: list[FuelStop], with_stops: bool = True) -> tuple[dict, Decimal]:
    """One strategy of ``summary.comparison`` and its total cost (Decimal)."""
    money, total_cost, total_gallons = _money_rows(stops)
    block = {
        "label": label,
        "rule": rule,
        "total_fuel_cost": float(total_cost),
        "total_gallons_purchased": float(total_gallons),
        "number_of_stops": len(stops),
        "average_price_paid": _average_price(total_cost, total_gallons),
    }
    if with_stops:
        block["stops"] = [
            {
                "opis_id": stop.candidate.ref.opis_id,
                "mile_marker": _round(stop.candidate.mile, 1),
                "lat": _round(stop.candidate.ref.lat, 5),
                "lon": _round(stop.candidate.ref.lon, 5),
                "price_per_gallon": float(price),
                "fuel_on_arrival_gallons": _round(stop.fuel_on_arrival_gallons),
                "gallons": float(gallons),
                "cost": float(cost),
            }
            for stop, (gallons, price, cost) in zip(stops, money)
        ]
    return block, total_cost


def _savings(baseline_cost: Decimal, optimized_cost: Decimal) -> dict:
    """What the plan saves against a baseline (negative if the baseline is cheaper)."""
    amount = baseline_cost - optimized_cost
    percent = (amount / baseline_cost * 100).quantize(_TENTH, rounding=ROUND_HALF_UP) if baseline_cost else Decimal(0)
    return {"amount": float(amount), "percent": float(percent)}


def _build_comparison(
    route: Route, corridor: list[CorridorStation], tank: TankRules, plan: FuelPlan, plan_settings: PlanSettings
) -> dict | None:
    """The same trip priced for simple drivers: same stations, same start / end fuel
    (and safety reserve), so the same gallons; only WHERE they buy changes. None when
    nothing is bought."""
    if not plan.stops:
        return None
    config = settings.FUEL_PLANNER
    corridor_miles = plan_settings.corridor_miles
    fraction = config["BASELINE_REFUEL_FRACTION"]
    limits = {
        "max_range_miles": plan_settings.max_range_miles,
        "miles_per_gallon": plan_settings.mpg,
        "initial_fuel_miles": tank.initial,
        "final_fuel_miles": tank.final,
        "min_fuel_miles": plan_settings.safety_reserve_miles,
    }
    candidates = _candidates_for(corridor)

    if plan_settings.consolidate:
        label, rule = (
            "This plan (tiny stops merged)",
            "The three rules, then a stop that buys less than "
            f"{config['MIN_STOP_GALLONS']:g} gal is merged into a neighbour when that costs at most "
            f"${config['MAX_CONSOLIDATION_COST']:.2f} more.",
        )
    else:
        label, rule = (
            "This plan (the three rules)",
            "Buys each gallon at the cheapest station that can supply it; small stops are kept (consolidate=false).",
        )
    optimized, optimized_cost = _strategy(label, rule, plan.stops, with_stops=False)
    before, _ = _strategy(
        "The three rules alone (before merging tiny stops)",
        (
            "Buys each gallon at the cheapest station that can supply it: just enough to reach a cheaper "
            "station, enough to finish, or a full tank when nothing within one tank is cheaper."
        ),
        plan.before_consolidation,
    )

    def baseline(planner, label: str, rule: str, **extra) -> tuple[dict | None, Decimal | None]:
        try:
            result = planner(route.distance_miles, candidates, **limits, **extra)
        except UnreachableError:
            return None, None
        return _strategy(label, rule, result.stops)

    price_blind, price_blind_cost = baseline(
        plan_price_blind,
        "Price-blind driver",
        "Ignores prices: drives until the next station is out of reach, then fills the tank "
        "(or buys just what the rest of the trip needs).",
    )
    quarter_tank, _ = baseline(
        plan_quarter_tank,
        "Quarter-tank driver",
        f"Refuels at the first station reached with the tank at or below {fraction:.0%}, or when the next "
        "station is out of reach; fills up, and at the last stop buys only what the trip needs.",
        refuel_below_fraction=fraction,
    )

    prices = [Decimal(f"{c.price:.4f}") for c in corridor]
    average_price = (sum(prices) / len(prices)).quantize(_PRICE_PLACES, rounding=ROUND_HALF_UP)
    gallons = Decimal(str(optimized["total_gallons_purchased"]))
    average_cost = _money(gallons * average_price)
    corridor_average = {
        "label": "Corridor average price",
        "rule": (
            f"Same gallons at the average price of the {len(corridor)} stations within "
            f"{corridor_miles:g} mi of the route."
        ),
        "price_per_gallon": float(average_price),
        "stations": len(corridor),
        "total_gallons_purchased": float(gallons),
        "total_fuel_cost": float(average_cost),
    }

    savings_vs_price_blind = None
    if price_blind is not None:
        savings_vs_price_blind = _savings(price_blind_cost, optimized_cost)
        savings_vs_price_blind["extra_stops"] = optimized["number_of_stops"] - price_blind["number_of_stops"]
    return {
        "optimized": optimized,
        "optimum_before_consolidation": before,
        "price_blind": price_blind,
        "quarter_tank": quarter_tank,
        "corridor_average": corridor_average,
        "savings_vs_price_blind": savings_vs_price_blind,
        "savings_vs_corridor_average": _savings(average_cost, optimized_cost),
    }


def _short_comparison(comparison: dict | None) -> dict | None:
    """``summary.comparison``: only the driver who ignores prices and what the plan saves
    against it (the other strategies are in ``details.comparison``)."""
    if comparison is None:
        return None
    blind = comparison["price_blind"]
    return {
        "price_blind": None if blind is None else {
            key: blind[key]
            for key in ("total_fuel_cost", "total_gallons_purchased", "number_of_stops", "average_price_paid")
        },
        "savings_vs_price_blind": comparison["savings_vs_price_blind"],
    }


def _lowest_fuel_gallons(plan: FuelPlan) -> float:
    """Lowest tank level on arrival anywhere: at a stop or at the destination."""
    return min([stop.fuel_on_arrival_gallons for stop in plan.stops] + [plan.final_fuel_gallons])


# --- how the plan was computed -------------------------------------------------------------


def _with_several_prices(arrays, corridor: list[CorridorStation]) -> int:
    """How many corridor stations have quotes that disagree (cheapest != dearest)."""
    if not corridor or arrays.price_min is None or arrays.price_max is None or not len(arrays):
        return 0
    ids = np.fromiter((c.opis_id for c in corridor), dtype=np.int64, count=len(corridor))
    index = np.searchsorted(arrays.opis_ids, ids)  # the arrays are sorted by opis_id
    found = index < len(arrays.opis_ids)
    index = np.where(found, index, 0)
    found &= arrays.opis_ids[index] == ids
    return int(np.count_nonzero(found & (arrays.price_min[index] != arrays.price_max[index])))


def _price_stats(corridor: list[CorridorStation]) -> dict | None:
    if not corridor:
        return None
    prices = np.array([c.price for c in corridor])
    return {
        "min": _round(prices.min(), 4),
        "median": _round(np.median(prices), 4),
        "max": _round(prices.max(), 4),
        "mean": _round(prices.mean(), 4),
    }


def _build_pipeline(
    client: ExternalApiClient,
    route: Route,
    route_from_cache: bool,
    funnel: dict,
    corridor: list[CorridorStation],
    tank: TankRules,
    plan: FuelPlan,
    total_cost: Decimal,
    timer: _Timer,
    plan_settings: PlanSettings,
) -> dict:
    """What it took to compute this plan. Cached with it and returned untouched on a hit."""
    config = settings.FUEL_PLANNER
    mpg = plan_settings.mpg
    _, cost_before, _ = _money_rows(plan.before_consolidation)
    return {
        "computed_at": timezone.now().isoformat(),
        "timings_ms": dict(timer.timings),
        "external_api_calls": client.call_count,
        "external_api_services": list(client.calls),
        "external_api_ms": _round(client.elapsed_ms, 1),
        "routing": {
            "service": "osrm",
            "geometry": "polyline6",
            "polyline_chars": route.polyline_chars,
            "geometry_points": route.geometry_points,
            "samples": len(route.samples),
            "sample_spacing_miles": _round(route.sample_spacing_miles, 3),
            "map_points": len(route.line),
            "from_route_cache": route_from_cache,
        },
        "corridor": {
            "corridor_miles": plan_settings.corridor_miles,
            "coarse_step_miles": COARSE_STEP_MILES,
            **{key: int(funnel.get(key, 0)) for key in FUNNEL_KEYS},
            "price_per_gallon": _price_stats(corridor),
            "price_policy": plan_settings.price_policy,
            # Candidates whose quotes in the file disagree: only these change price with
            # price_policy (median / min / max).
            "stations_with_several_prices": int(funnel.get("stations_with_several_prices", 0)),
        },
        "tank": {
            "start_fuel_gallons": _round(tank.initial / mpg),
            "required_end_fuel_gallons": _round(tank.final / mpg),
            "reason": tank.reason,
            "arrival_capped": tank.arrival_capped,
            # The safety reserve and the lowest level the plan reaches on arrival
            # anywhere (a stop or the destination): never below the reserve.
            "safety_reserve_gallons": plan_settings.safety_reserve_gal,
            "lowest_fuel_gallons": _round(_lowest_fuel_gallons(plan)),
        },
        "optimizer": {
            "candidates": len(corridor),
            "stops_before_consolidation": len(plan.before_consolidation),
            "cost_before_consolidation": float(cost_before),
            "stops": len(plan.stops),
            "consolidation_extra_cost": float(total_cost - cost_before),
            "consolidate": plan_settings.consolidate,
            "min_stop_gallons": config["MIN_STOP_GALLONS"],
            "max_consolidation_cost": config["MAX_CONSOLIDATION_COST"],
        },
    }


def _compact_candidates(corridor: list[CorridorStation]) -> list[tuple]:
    """Corridor stations as small numeric tuples for the plan cache, by (mile, opis_id):
    (opis_id, mile, offset, price, lat, lon). Names are looked up only when asked for."""
    rows = [
        (c.opis_id, _round(c.mile, 1), _round(c.offset_miles, 1), _round(c.price, 4), _round(c.lat, 5), _round(c.lon, 5))
        for c in corridor
    ]
    rows.sort(key=lambda row: (row[1], row[0]))
    return rows


def _build_candidates(compact: list[tuple], stops: list[dict]) -> dict:
    """``include=candidates``: every corridor station, with the plan stop it is (or null)."""
    stop_of = {stop["opis_id"]: stop["stop"] for stop in stops}
    names = {}
    if compact:
        names = {
            opis_id: (name, city, state)
            for opis_id, name, city, state in FuelStation.objects.filter(
                opis_id__in=[row[0] for row in compact]
            ).values_list("opis_id", "name", "city", "state")
        }
    return {
        "fields": list(CANDIDATE_FIELDS),
        "rows": [
            [opis_id, *names.get(opis_id, ("", "", "")), lat, lon, mile, offset, price, stop_of.get(opis_id)]
            for opis_id, mile, offset, price, lat, lon in compact
        ],
    }


def _geocoder_warnings(origin: Location, destination: Location) -> list[str]:
    """Free text that was not "City, ST" was placed by a search engine: say what it
    matched, so a wrong match ("Toronto" -> Toronto, Ohio) is visible."""
    return [
        f"{field_name} '{location.query}' is not 'City, ST', so it was looked up by free-text search "
        f"(Nominatim) and matched '{location.label}'. If that is not the place you meant, use 'City, ST' "
        "or 'lat,lon'."
        for field_name, location in (("start", origin), ("finish", destination))
        if location.geocoder == "nominatim"
    ]


def _warnings(route: Route, tank: TankRules) -> list[str]:
    warnings = list(tank.warnings)
    if route.miles_outside_usa >= 1:
        warnings.append(
            f"About {route.miles_outside_usa:.0f} miles of this route run outside the USA (the fastest road "
            "crosses the border). The price file has no stations there, and US stations that can only be "
            "reached from that stretch are not used."
        )
    return warnings


def _build_geojson(route: Route, origin: Location, destination: Location, stops: list[dict]) -> dict:
    """FeatureCollection: the route line, the start / finish points and one point per stop."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "route", "distance_miles": _round(route.distance_miles, 1)},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[_round(lon, 5), _round(lat, 5)] for lat, lon in route.line],
                },
            },
            *[
                {
                    "type": "Feature",
                    "properties": {"kind": kind, "label": location.label},
                    "geometry": {"type": "Point", "coordinates": [location.longitude, location.latitude]},
                }
                for kind, location in (("start", origin), ("finish", destination))
            ],
            *[
                {
                    "type": "Feature",
                    "properties": {
                        "kind": "fuel_stop",
                        "stop": s["stop"],
                        "name": s["name"],
                        "city": f"{s['city']}, {s['state']}",
                        "price_per_gallon": s["price_per_gallon"],
                        "gallons": s["gallons"],
                        "cost": s["cost"],
                        "mile_marker": s["mile_marker"],
                    },
                    "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                }
                for s in stops
            ],
        ],
    }


def _meta(client: ExternalApiClient, route_cache: str, plan_cache: str, plan: PlanSettings) -> dict:
    return {
        "external_api_calls": client.call_count,
        "external_api_services": client.calls,
        "route_cache": route_cache,
        "plan_cache": plan_cache,
        # Settings of this plan that differ from the defaults (their values are in ``vehicle``).
        "settings_changed": plan.changed(),
    }
