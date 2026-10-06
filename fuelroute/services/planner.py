"""Orchestrates one trip request; this is where the steps of "How it works" meet.

    start, finish ──► geocode ──► checks ──► route ──► stations ──► tank ──► optimizer ──► JSON
                     (offline)              (OSRM,    near route   rules    (greedy)      + GeoJSON
                                            1 call)   (numpy)

1. ``geocode`` both inputs (0 external calls for "City, ST" and "lat,lon").
2. Cheap checks BEFORE spending the routing call: same place, Alaska / Hawaii (no
   price data), station table loaded.
3. Plan cache: the finished plan is cached by (station data version, rounded
   coordinates, start_tank). A hit returns in a few ms with 0 external calls.
4. ``get_route``: the one OSRM call (itself cached by coordinates), already
   resampled every mile and simplified.
5. ``stations_along_route``: candidate stations within 10 miles, with mile markers.
6. ``_tank_rules``: how much fuel the truck leaves with and must arrive with
   (``start_tank``, see the README "Assumptions").
7. ``plan_fuel_stops``: where to stop and how much to buy.
8. Build the response: stops (money computed in Decimal, so the stop costs add up
   to the total to the cent), summary, warnings, GeoJSON, meta.
9. Explain it (observational only, the plan is already decided):
   ``summary.comparison`` prices the same trip for simple drivers that ignore
   prices (same stations, same gallons); ``pipeline`` records how the plan was
   computed (timings, the routing geometry, the corridor search funnel, the tank
   rule, consolidation). Both are cached with the plan, so a cache hit still shows
   what the first computation cost. ``include=candidates`` adds every corridor
   station (for the map); it is kept compact in the cache and named on request.
"""

from __future__ import annotations

import logging
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
    CorridorStation,
    data_version,
    get_station_arrays,
    stations_along_route,
)
from .usa import LOWER48

logger = logging.getLogger(__name__)

START_EMPTY = "empty"
START_FULL = "full"

# Why the truck leaves / arrives with that fuel (``pipeline.tank.reason``).
TANK_FULL = "full_tank"
TANK_RESERVE = "reserve"
TANK_FIRST_STATION = "first_station_beyond_reserve"
TANK_NO_STATION = "no_station_on_route"

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


class _Timer:
    """Milliseconds spent in each step, reported in ``meta.timings_ms``."""

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
) -> dict:
    """Plan a trip from ``start`` to ``finish`` (free text, see geocoding.py).

    Returns the response body (a dict). Raises a ``PlannerError`` subclass for
    every expected failure; ``views.py`` turns it into the HTTP error response.
    Pass ``client`` to read the number of external calls even when it raises.
    ``include``: optional extra blocks ("candidates"); they never change the plan
    nor its cache key.

    On a ``PlannerError`` the timings of the steps completed before it are attached
    to the exception (``timings_ms``), for the Server-Timing header of the error.
    """
    timer = _Timer()
    try:
        return _plan_trip(start, finish, start_tank, client or ExternalApiClient(), frozenset(include), timer)
    except PlannerError as exc:
        exc.timings_ms = dict(timer.timings)
        raise


def _plan_trip(
    start: str, finish: str, start_tank: str, client: ExternalApiClient, include: frozenset, timer: _Timer
) -> dict:
    config = settings.FUEL_PLANNER
    origin = geocode(start, client, field="start")
    destination = geocode(finish, client, field="finish")
    _check_endpoints(origin, destination)
    timer.lap("geocoding_ms")

    version = data_version()
    if version.startswith("0-"):
        raise StationDataNotLoaded("The station table is empty. Run `python manage.py load_stations` first.")

    plan_key = f"plan:v4:{version}:{origin.as_param};{destination.as_param};{start_tank}"
    cached_plan = cache.get(plan_key)  # the cache returns a fresh copy (unpickled)
    timer.lap("plan_cache_ms")
    if cached_plan is not None:
        compact = cached_plan.pop("_candidates", [])
        cached_plan["start"]["query"], cached_plan["finish"]["query"] = start, finish
        cached_plan["map"]["map_url"] = map_path(origin, destination, start_tank)
        cached_plan["warnings"] = _geocoder_warnings(origin, destination) + cached_plan["warnings"]
        if "candidates" in include:
            cached_plan["candidates"] = _build_candidates(compact, cached_plan["fuel_stops"])
            timer.lap("candidates_ms")
        # ``pipeline`` is left as it was: it describes the computation being reused.
        cached_plan["meta"] = _meta(client, version, "hit", "hit", timer.timings)
        return cached_plan

    route, route_from_cache = get_route(origin, destination, client)
    _check_snapping(route, origin, destination)
    timer.lap("routing_ms")

    funnel: dict = {}
    corridor = stations_along_route(
        route.samples,
        route.sample_miles,
        config["CORRIDOR_MILES"],
        get_station_arrays(version),
        route.sample_in_usa,
        stats=funnel,
    )
    timer.lap("corridor_ms")

    tank = _tank_rules(start_tank, route, corridor)
    plan = _optimize(route, corridor, tank)
    timer.lap("optimizer_ms")

    stops, total_cost, total_gallons = _build_stops(plan, route.distance_miles)
    summary = _build_summary(route, corridor, tank, plan, stops, total_cost, total_gallons)
    result = {
        "start": origin.to_dict(),
        "finish": destination.to_dict(),
        "route": {
            "distance_miles": _round(route.distance_miles, 1),
            "duration_hours": _round(route.duration_seconds / 3600, 2),
            "miles_outside_usa": _round(route.miles_outside_usa, 0),
            "road_snap_miles": {"start": _round(route.snap_miles[0], 2), "finish": _round(route.snap_miles[1], 2)},
        },
        "vehicle": {
            "max_range_miles": config["MAX_RANGE_MILES"],
            "miles_per_gallon": config["MILES_PER_GALLON"],
            "tank_gallons": _round(config["MAX_RANGE_MILES"] / config["MILES_PER_GALLON"], 1),
            "start_tank": start_tank,
        },
        "summary": summary,
        "warnings": _warnings(route, tank),
        "fuel_stops": stops,
        "map": {
            "map_url": map_path(origin, destination, start_tank),
            "geojson": _build_geojson(route, origin, destination, stops),
        },
    }
    timer.lap("response_build_ms")

    summary["comparison"] = _build_comparison(route, corridor, tank, plan)
    timer.lap("comparison_ms")

    result["meta"] = _meta(client, version, "hit" if route_from_cache else "miss", "miss", timer.timings)
    result["pipeline"] = _build_pipeline(client, route, route_from_cache, funnel, corridor, tank, plan, total_cost, timer)
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


def map_path(start: Location, finish: Location, start_tank: str) -> str:
    """Relative URL of the HTML map for this trip (the view makes it absolute).

    It carries the same inputs as the API call, so while the plan is cached the map
    page makes no external call; after the cache expires it plans again (1 call).
    """
    query = urlencode({"start": start.query, "finish": finish.query, "start_tank": start_tank})
    return f"{reverse('route-map')}?{query}"


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


def _tank_rules(start_tank: str, route: Route, corridor: list[CorridorStation]) -> TankRules:
    """Fuel at departure and at arrival, in miles of range.

    ``full``: leave with a full tank, arrive with whatever is needed (0); only the
    fuel bought on the way is counted.

    ``empty`` (default): the trip pays for EVERY mile it drives. The truck leaves
    with the ``START_RESERVE_MILES`` reserve and must arrive with the same amount,
    so fuel bought = fuel burned. Two real-data cases need more than that:

    * the first station of the file on this route is further than the reserve
      (Houston: mile 74; Los Angeles: mile ~250, the file has 8 stations in CA):
      the truck leaves with enough fuel to reach it, and must ARRIVE with that same
      amount ("return the tank as you got it"), so every mile is still paid;
    * the last station is so far from the destination that arriving with that much
      fuel is impossible: the truck arrives with what it can, and the difference is
      reported as unpriced fuel, with a warning.
    """
    config = settings.FUEL_PLANNER
    capacity = config["MAX_RANGE_MILES"]
    mpg = config["MILES_PER_GALLON"]
    if start_tank == START_FULL:
        note = (
            f"The truck leaves with a full tank ({capacity / mpg:.0f} gal); that fuel is not included in the cost. "
            "The plan buys only what it needs to reach the destination, so it may arrive nearly empty."
        )
        return TankRules(capacity, 0.0, note, reason=TANK_FULL)

    reserve = min(config["START_RESERVE_MILES"], capacity)
    route_miles = route.distance_miles
    if not corridor:
        # Nothing can be bought on this route. Only a trip the reserve covers works.
        final = max(0.0, reserve - route_miles)
        rules = TankRules(reserve, final, "", reason=TANK_NO_STATION, arrival_capped=final < reserve)
        rules.note = "No station of the price file is on this route, so no fuel can be bought or priced."
        rules.warnings.append(
            f"No station of the price file is within {config['CORRIDOR_MILES']:.0f} miles of this route. "
            f"The {route_miles / mpg:.2f} gal burned come from the {reserve:.0f}-mile reserve and are not priced."
        )
        return rules

    first = min(c.mile for c in corridor)
    last = max(c.mile for c in corridor)
    initial = min(capacity, max(reserve, first))
    final = initial
    rules = TankRules(initial, final, "", reason=TANK_FIRST_STATION if first > reserve else TANK_RESERVE)
    if first > reserve:
        rules.warnings.append(
            f"The first station of the price file on this route is at mile {first:.0f}, beyond the "
            f"{reserve:.0f}-mile reserve. The truck is assumed to leave with {initial / mpg:.1f} gal to reach it "
            "and must arrive with the same amount, so every mile is still paid for."
        )
    last_gap = route_miles - last
    if last_gap + final > capacity:
        rules.final = max(0.0, capacity - last_gap)
        rules.arrival_capped = True
        rules.warnings.append(
            f"The last station of the price file is {last_gap:.0f} miles before the destination, so the truck "
            f"can only arrive with {rules.final / mpg:.1f} gal instead of {final / mpg:.1f}: "
            f"{(final - rules.final) / mpg:.1f} gal burned on that stretch are not priced."
        )
    rules.note = (
        f"Every mile driven is paid for: the truck leaves with {initial / mpg:.1f} gal "
        f"({'the reserve' if first <= reserve else 'enough to reach the first station'}) and must arrive "
        "with the same amount, so the fuel bought equals the fuel burned"
        + (" (except the unpriced fuel in 'warnings')" if rules.final < final else "")
        + ". That fuel is borrowed at the start and returned at the end; it is not a safety margin."
    )
    return rules


def _candidates_for(corridor: list[CorridorStation]) -> list[Candidate]:
    return [Candidate(mile=c.mile, price=c.price, ref=c) for c in corridor]


def _optimize(route: Route, corridor: list[CorridorStation], tank: TankRules) -> FuelPlan:
    config = settings.FUEL_PLANNER
    capacity = config["MAX_RANGE_MILES"]
    route_miles = route.distance_miles
    if not corridor and route_miles + tank.final > tank.initial:
        raise NoFuelDataOnRoute(
            f"No station of the price file is within {config['CORRIDOR_MILES']:.0f} miles of this "
            f"{route_miles:.0f}-mile route, so fuel cannot be bought or priced. The file has few or no "
            "stations in some areas (for example only 8, all in the far south-east, in California). "
            + (
                f"With start_tank=full a trip under {capacity:.0f} miles needs no stop."
                if route_miles <= capacity and tank.initial < capacity
                else ""
            ),
            route_distance_miles=_round(route_miles, 1),
        )
    try:
        return plan_fuel_stops(
            route_miles,
            _candidates_for(corridor),
            max_range_miles=capacity,
            miles_per_gallon=config["MILES_PER_GALLON"],
            initial_fuel_miles=tank.initial,
            final_fuel_miles=tank.final,
            min_stop_gallons=config["MIN_STOP_GALLONS"],
            max_consolidation_cost=config["MAX_CONSOLIDATION_COST"],
        )
    except UnreachableError as exc:
        raise _unreachable(exc, route, capacity) from exc


def _point_at(route: Route, mile: float) -> dict:
    index = int(np.clip(np.searchsorted(route.sample_miles, mile), 0, len(route.sample_miles) - 1))
    lat, lon = route.samples[index]
    return {"mile": _round(mile, 1), "lat": _round(lat, 5), "lon": _round(lon, 5)}


def _unreachable(exc: UnreachableError, route: Route, capacity: float) -> NoReachableStation:
    """A 422 that says exactly which stretch has no station, and where it is."""
    start_mile = exc.from_mile
    end_mile = route.distance_miles if exc.next_mile is None else exc.next_mile
    if exc.at_start:
        message = f"The first station on this route is at mile {end_mile:.0f}, more than the {capacity:.0f}-mile range."
    elif exc.next_mile is None:
        message = (
            f"The last station on this route is at mile {start_mile:.0f}, {end_mile - start_mile:.0f} miles "
            f"before the destination: more than the {capacity:.0f}-mile range."
        )
    else:
        message = (
            f"No station between mile {start_mile:.0f} and mile {end_mile:.0f} "
            f"({end_mile - start_mile:.0f} miles): more than the {capacity:.0f}-mile range."
        )
    return NoReachableStation(
        f"{message} {_NO_DATA_HINT}",
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


def _build_stops(plan: FuelPlan, route_miles: float) -> tuple[list[dict], Decimal, Decimal]:
    """Stop rows with exact money (see ``_money_rows``) and why each stop is there."""
    opis_ids = [stop.candidate.ref.opis_id for stop in plan.stops]
    stations = FuelStation.objects.in_bulk(opis_ids, field_name="opis_id")
    if len(stations) != len(set(opis_ids)):
        # load_stations replaced the table while this request was running.
        raise StationDataChanged("The station data changed during the request; please send it again.")

    money, total_cost, total_gallons = _money_rows(plan.stops)
    tank_gallons = settings.FUEL_PLANNER["MAX_RANGE_MILES"] / settings.FUEL_PLANNER["MILES_PER_GALLON"]
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
                "price_quotes": {
                    "count": station.price_rows,
                    "min": float(station.price_min),
                    "max": float(station.price_max),
                },
                "mile_marker": _round(info.mile, 1),
                "distance_from_route_miles": _round(info.offset_miles, 1),
                "fuel_on_arrival_gallons": _round(stop.fuel_on_arrival_gallons),
                "gallons": float(gallons),
                "cost": float(cost),
                "decision": _decision(stop, number, plan, route_miles, tank_gallons, stop_of_greedy),
            }
        )
    return stops, total_cost, total_gallons


def _decision(
    stop: FuelStop, number: int, plan: FuelPlan, route_miles: float, tank_gallons: float, stop_of_greedy: dict
) -> dict:
    """Why this stop of the FINAL plan exists and what its purchase covers.

    * ``rule`` / ``cheaper_station_mile``: the greedy branch that created the stop.
    * ``greedy_gallons``: what the greedy bought here. ``moved_in``: fuel the greedy
      had planned at another station and consolidation moved here (``from_stop``:
      that station's stop in this plan, null when the stop was removed);
      ``moved_out``: fuel of this station's greedy purchase now bought at another
      stop. ``consolidation_extra_cost``: what the fuel moved here costs more (or
      less) at this price; summed over the stops it is the consolidation's cost.
    * ``reaches``: where the fuel bought here takes the truck in the final plan, the
      next stop or the destination (``stop`` null). ``fills_tank``: leaves full.
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
        moved_in.append(
            {"mile": _round(origin.candidate.mile, 1), "gallons": _round(gallons), "from_stop": stop_of_greedy.get(k)}
        )
    moved_out = [
        {"mile": _round(other.candidate.mile, 1), "gallons": _round(other.sources[stop.greedy_index]), "to_stop": n}
        for n, other in enumerate(plan.stops, start=1)
        if other is not stop and other.sources.get(stop.greedy_index, 0.0) >= 0.005
    ]
    return {
        "rule": stop.rule,
        "cheaper_station_mile": None if stop.cheaper_station_mile is None else _round(stop.cheaper_station_mile, 1),
        "consolidated": stop.consolidated,
        "greedy_gallons": _round(stop.greedy_gallons),
        "moved_in": moved_in,
        "moved_out": moved_out,
        "consolidation_extra_cost": float(_money(extra)),
        "reaches": {
            "stop": number + 1 if following else None,
            "mile": _round(following.candidate.mile if following else route_miles, 1),
        },
        "fills_tank": stop.fuel_on_arrival_gallons + stop.gallons >= tank_gallons - 0.005,
    }


def _build_summary(route, corridor, tank, plan, stops, total_cost: Decimal, total_gallons: Decimal) -> dict:
    mpg = settings.FUEL_PLANNER["MILES_PER_GALLON"]
    fuel_used = route.distance_miles / mpg
    return {
        "total_fuel_cost": float(total_cost),
        "total_gallons_purchased": float(total_gallons),
        "fuel_used_gallons": _round(fuel_used),
        "start_fuel_gallons": _round(tank.initial / mpg),
        "end_fuel_gallons": _round(plan.final_fuel_gallons),
        # Fuel burned that the total does not include: the free full tank in "full"
        # mode; normally 0 in "empty" mode (see warnings when it is not).
        "unpriced_fuel_gallons": _round(max(0.0, fuel_used - plan.total_gallons)),
        "number_of_stops": len(stops),
        "average_price_paid": _average_price(total_cost, total_gallons),
        "candidate_stations_on_route": len(corridor),
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


def _build_comparison(route: Route, corridor: list[CorridorStation], tank: TankRules, plan: FuelPlan) -> dict | None:
    """The same trip priced for simple drivers: same stations, same start / end fuel,
    so the same gallons; only WHERE they buy changes. None when nothing is bought."""
    if not plan.stops:
        return None
    config = settings.FUEL_PLANNER
    corridor_miles = config["CORRIDOR_MILES"]
    fraction = config["BASELINE_REFUEL_FRACTION"]
    limits = {
        "max_range_miles": config["MAX_RANGE_MILES"],
        "miles_per_gallon": config["MILES_PER_GALLON"],
        "initial_fuel_miles": tank.initial,
        "final_fuel_miles": tank.final,
    }
    candidates = _candidates_for(corridor)

    optimized, optimized_cost = _strategy(
        "This plan (optimized, consolidated)",
        (
            "Buys each mile of fuel at the cheapest station that can supply it, then fixes stops under "
            f"{config['MIN_STOP_GALLONS']:g} gal when the fix costs at most ${config['MAX_CONSOLIDATION_COST']:.2f}."
        ),
        plan.stops,
        with_stops=False,
    )
    before, _ = _strategy(
        "Pure optimum before consolidation",
        (
            "Buys each mile of fuel at the cheapest station that can supply it: just enough to reach a cheaper "
            "station, or a full tank when none is within one tank."
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
            f"{corridor_miles:.0f} mi of the route."
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


# --- how the plan was computed -------------------------------------------------------------


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
) -> dict:
    """What it took to compute this plan. Cached with it and returned untouched on a hit."""
    config = settings.FUEL_PLANNER
    mpg = config["MILES_PER_GALLON"]
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
            "corridor_miles": config["CORRIDOR_MILES"],
            "coarse_step_miles": COARSE_STEP_MILES,
            **{key: int(funnel.get(key, 0)) for key in FUNNEL_KEYS},
            "price_per_gallon": _price_stats(corridor),
        },
        "tank": {
            "start_fuel_gallons": _round(tank.initial / mpg),
            "required_end_fuel_gallons": _round(tank.final / mpg),
            "reason": tank.reason,
            "arrival_capped": tank.arrival_capped,
        },
        "optimizer": {
            "candidates": len(corridor),
            "stops_before_consolidation": len(plan.before_consolidation),
            "cost_before_consolidation": float(cost_before),
            "stops": len(plan.stops),
            "consolidation_extra_cost": float(total_cost - cost_before),
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


def _meta(client: ExternalApiClient, version: str, route_cache: str, plan_cache: str, timings: dict) -> dict:
    return {
        "external_api_calls": client.call_count,
        "external_api_services": client.calls,
        "external_api_ms": _round(client.elapsed_ms, 1),
        "route_cache": route_cache,
        "plan_cache": plan_cache,
        "station_data_version": version,
        "timings_ms": timings,
    }
