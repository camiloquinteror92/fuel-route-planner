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

from ..models import FuelStation
from .errors import (
    LocationNotNearRoad,
    NoFuelDataInRegion,
    NoFuelDataOnRoute,
    NoReachableStation,
    SameLocation,
    StationDataChanged,
    StationDataNotLoaded,
)
from .geo import haversine_miles
from .geocoding import Location, geocode
from .http import ExternalApiClient
from .optimizer import Candidate, FuelPlan, UnreachableError, plan_fuel_stops
from .osrm import Route, get_route
from .stations import CorridorStation, data_version, get_station_arrays, stations_along_route
from .usa import LOWER48

logger = logging.getLogger(__name__)

START_EMPTY = "empty"
START_FULL = "full"

CENT = Decimal("0.01")
_PRICE_PLACES = Decimal("0.0001")
# Inputs closer than this are the same place ("Austin, TX" vs "Austin, Texas").
SAME_PLACE_MILES = 0.5
_NO_DATA_HINT = "The price file has no stations along that stretch (see the README, 'Known data gaps')."


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


def plan_trip(
    start: str, finish: str, start_tank: str = START_EMPTY, client: ExternalApiClient | None = None
) -> dict:
    """Plan a trip from ``start`` to ``finish`` (free text, see geocoding.py).

    Returns the response body (a dict). Raises a ``PlannerError`` subclass for
    every expected failure; ``views.py`` turns it into the HTTP error response.
    Pass ``client`` to read the number of external calls even when it raises.
    """
    config = settings.FUEL_PLANNER
    client = client or ExternalApiClient()
    timer = _Timer()

    origin = geocode(start, client, field="start")
    destination = geocode(finish, client, field="finish")
    _check_endpoints(origin, destination)
    timer.lap("geocoding_ms")

    version = data_version()
    if version.startswith("0-"):
        raise StationDataNotLoaded("The station table is empty. Run `python manage.py load_stations` first.")

    plan_key = f"plan:v2:{version}:{origin.as_param};{destination.as_param};{start_tank}"
    cached_plan = cache.get(plan_key)  # the cache returns a fresh copy (unpickled)
    if cached_plan is not None:
        cached_plan["start"]["query"], cached_plan["finish"]["query"] = start, finish
        cached_plan["map"]["map_url"] = map_path(origin, destination, start_tank)
        cached_plan["meta"] = _meta(client, version, "hit", "hit", timer.timings)
        return cached_plan

    route, route_from_cache = get_route(origin, destination, client)
    _check_snapping(route, origin, destination)
    timer.lap("routing_ms")

    corridor = stations_along_route(
        route.samples, route.sample_miles, config["CORRIDOR_MILES"], get_station_arrays(version), route.sample_in_usa
    )
    timer.lap("corridor_ms")

    tank = _tank_rules(start_tank, route, corridor)
    plan = _optimize(route, corridor, tank)
    timer.lap("optimizer_ms")

    stops, total_cost, total_gallons = _build_stops(plan)
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
        "summary": _build_summary(route, corridor, tank, plan, stops, total_cost, total_gallons),
        "warnings": _warnings(route, tank),
        "fuel_stops": stops,
        "map": {
            "map_url": map_path(origin, destination, start_tank),
            "geojson": _build_geojson(route, origin, destination, stops),
        },
    }
    timer.lap("response_build_ms")
    result["meta"] = _meta(client, version, "hit" if route_from_cache else "miss", "miss", timer.timings)
    cache.set(plan_key, result)
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
        note = f"The truck leaves with a full tank ({capacity / mpg:.0f} gal); that fuel is not included in the cost."
        return TankRules(capacity, 0.0, note)

    reserve = min(config["START_RESERVE_MILES"], capacity)
    route_miles = route.distance_miles
    if not corridor:
        # Nothing can be bought on this route. Only a trip the reserve covers works.
        rules = TankRules(reserve, max(0.0, reserve - route_miles), "")
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
    rules = TankRules(initial, final, "")
    if first > reserve:
        rules.warnings.append(
            f"The first station of the price file on this route is at mile {first:.0f}, beyond the "
            f"{reserve:.0f}-mile reserve. The truck is assumed to leave with {initial / mpg:.1f} gal to reach it "
            "and must arrive with the same amount, so every mile is still paid for."
        )
    last_gap = route_miles - last
    if last_gap + final > capacity:
        rules.final = max(0.0, capacity - last_gap)
        rules.warnings.append(
            f"The last station of the price file is {last_gap:.0f} miles before the destination, so the truck "
            f"can only arrive with {rules.final / mpg:.1f} gal instead of {final / mpg:.1f}: "
            f"{(final - rules.final) / mpg:.1f} gal burned on that stretch are not priced."
        )
    rules.note = (
        f"Every mile driven is paid for: the truck leaves with {initial / mpg:.1f} gal "
        f"({'the reserve' if first <= reserve else 'enough to reach the first station'}) and must arrive "
        "with the same amount, so the fuel bought equals the fuel burned"
        + (" (except the unpriced fuel in 'warnings')." if rules.final < final else ".")
    )
    return rules


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
            [Candidate(mile=c.mile, price=c.price, ref=c) for c in corridor],
            max_range_miles=capacity,
            miles_per_gallon=config["MILES_PER_GALLON"],
            initial_fuel_miles=tank.initial,
            final_fuel_miles=tank.final,
            min_stop_gallons=config["MIN_STOP_GALLONS"],
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


def _build_stops(plan: FuelPlan) -> tuple[list[dict], Decimal, Decimal]:
    """Stop rows with exact money: cost = round(gallons shown x price shown, 2).

    gallons are rounded to 2 decimals first and the price is the stored 4-decimal
    one, so ``gallons * price_per_gallon`` matches ``cost`` to the cent, and the
    totals are the sums of the rows.
    """
    opis_ids = [stop.candidate.ref.opis_id for stop in plan.stops]
    stations = FuelStation.objects.in_bulk(opis_ids, field_name="opis_id")
    if len(stations) != len(set(opis_ids)):
        # load_stations replaced the table while this request was running.
        raise StationDataChanged("The station data changed during the request; please send it again.")

    stops, total_cost, total_gallons = [], Decimal("0"), Decimal("0")
    for number, stop in enumerate(plan.stops, start=1):
        info: CorridorStation = stop.candidate.ref
        station = stations[info.opis_id]
        price = station.price.quantize(_PRICE_PLACES)
        gallons = Decimal(f"{stop.gallons:.6f}").quantize(CENT, rounding=ROUND_HALF_UP)
        cost = _money(gallons * price)
        total_cost += cost
        total_gallons += gallons
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
            }
        )
    return stops, total_cost, total_gallons


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
        "average_price_paid": (
            float((total_cost / total_gallons).quantize(_PRICE_PLACES)) if total_gallons else None
        ),
        "candidate_stations_on_route": len(corridor),
        "note": tank.note,
    }


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
