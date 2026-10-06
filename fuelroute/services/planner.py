"""Orchestrates one request: geocode -> route (1 external call) -> stations -> plan -> JSON."""

from __future__ import annotations

import copy
import logging
import time
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from ..models import FuelStation
from .errors import NoReachableStation
from .geo import cumulative_miles, resample, simplify
from .geocoding import Location, geocode
from .http import ExternalApiClient
from .optimizer import Candidate, UnreachableError, plan_fuel_stops
from .osrm import get_route
from .stations import get_station_arrays, stations_along_route

logger = logging.getLogger(__name__)

START_EMPTY = "empty"
START_FULL = "full"


def _r(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def plan_trip(
    start: str, finish: str, start_tank: str = START_EMPTY, client: ExternalApiClient | None = None
) -> dict:
    """Plan the trip. Text is geocoded first; the route and the finished plan are cached."""
    config = settings.FUEL_PLANNER
    client = client or ExternalApiClient()
    timings: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.perf_counter()
        timings[name] = _r((now - clock) * 1000, 1)
        clock = now

    origin = geocode(start, client, field="start")
    destination = geocode(finish, client, field="finish")
    lap("geocoding_ms")

    plan_key = f"plan:v1:{origin.as_param};{destination.as_param};{start_tank}"
    cached_plan = cache.get(plan_key)
    if cached_plan is not None:
        result = copy.deepcopy(cached_plan)
        result["start"]["query"], result["finish"]["query"] = start, finish
        result["meta"] = {
            "external_api_calls": client.call_count,
            "external_api_services": client.calls,
            "route_cache": "hit",
            "plan_cache": "hit",
            "timings_ms": timings,
        }
        return result

    route, from_cache = get_route(origin, destination, client)
    lap("routing_ms")

    # Mile markers: haversine along the geometry, scaled to OSRM's road distance.
    cumulative = cumulative_miles(route.points)
    scale = route.distance_miles / cumulative[-1] if cumulative[-1] > 0 else 1.0
    samples, sample_miles = resample(route.points, cumulative, config["RESAMPLE_MILES"] / scale)
    sample_miles = sample_miles * scale
    corridor = stations_along_route(samples, sample_miles, config["CORRIDOR_MILES"], get_station_arrays())
    lap("corridor_ms")

    max_range = config["MAX_RANGE_MILES"]
    mpg = config["MILES_PER_GALLON"]
    empty_start = start_tank == START_EMPTY
    # "empty": leave on the reserve and arrive with the same reserve -> every mile is paid.
    # "full": leave with a full tank, arrive empty -> only the fuel bought on the way is paid.
    reserve = min(config["START_RESERVE_MILES"], max_range)
    try:
        plan = plan_fuel_stops(
            route.distance_miles,
            [Candidate(mile=c.mile, price=c.price, ref=c) for c in corridor],
            max_range_miles=max_range,
            miles_per_gallon=mpg,
            initial_fuel_miles=reserve if empty_start else max_range,
            final_fuel_miles=reserve if empty_start else 0.0,
            min_stop_gallons=config["MIN_STOP_GALLONS"],
        )
    except UnreachableError as exc:
        raise NoReachableStation(
            str(exc), from_mile=_r(exc.from_mile, 1),
            next_station_mile=None if exc.next_mile is None else _r(exc.next_mile, 1),
        ) from exc
    lap("optimizer_ms")

    stations = FuelStation.objects.in_bulk([stop.candidate.ref.station_id for stop in plan.stops])
    stops = []
    for number, stop in enumerate(plan.stops, start=1):
        info = stop.candidate.ref
        station = stations[info.station_id]
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
                "price_per_gallon": _r(station.price, 3),
                "mile_marker": _r(info.mile, 1),
                "distance_from_route_miles": _r(info.offset_miles, 1),
                "fuel_on_arrival_gallons": _r(stop.fuel_on_arrival_gallons),
                "gallons": _r(stop.gallons),
                "cost": _r(stop.cost),
            }
        )

    line = simplify(route.points)
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "route", "distance_miles": _r(route.distance_miles, 1)},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[_r(lon, 5), _r(lat, 5)] for lat, lon in line],
                },
            },
            *[
                {
                    "type": "Feature",
                    "properties": {"kind": endpoint, "label": loc.label},
                    "geometry": {"type": "Point", "coordinates": [loc.longitude, loc.latitude]},
                }
                for endpoint, loc in (("start", origin), ("finish", destination))
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
    lap("response_build_ms")

    fuel_used = route.distance_miles / mpg
    result = {
        "start": origin.to_dict(),
        "finish": destination.to_dict(),
        "route": {
            "distance_miles": _r(route.distance_miles, 1),
            "duration_hours": _r(route.duration_seconds / 3600, 2),
        },
        "vehicle": {
            "max_range_miles": max_range,
            "miles_per_gallon": mpg,
            "tank_gallons": _r(max_range / mpg, 1),
            "start_tank": start_tank,
        },
        "summary": {
            "total_fuel_cost": _r(plan.total_cost),
            "total_gallons_purchased": _r(plan.total_gallons),
            "fuel_used_gallons": _r(fuel_used),
            "number_of_stops": len(stops),
            "average_price_paid": _r(plan.total_cost / plan.total_gallons, 3) if plan.total_gallons else None,
            "candidate_stations_on_route": len(corridor),
            "note": (
                f"Every mile of the trip is paid for: the truck leaves with a {reserve:.0f}-mile reserve "
                "and arrives with the same reserve."
                if empty_start
                else "The truck leaves with a full tank; that fuel is not included in the cost."
            ),
        },
        "fuel_stops": stops,
        "map": {"map_url": map_path(origin, destination, start_tank), "geojson": geojson},
        "meta": {
            "external_api_calls": client.call_count,
            "external_api_services": client.calls,
            "route_cache": "hit" if from_cache else "miss",
            "plan_cache": "miss",
            "timings_ms": timings,
        },
    }
    cache.set(plan_key, result)
    logger.info(
        "event=route_planned start=%r finish=%r miles=%.0f stops=%s cost=%.2f external_calls=%s cache=%s",
        origin.label, destination.label, route.distance_miles, len(stops), plan.total_cost,
        client.call_count, result["meta"]["route_cache"],
    )
    return result


def map_path(start: Location, finish: Location, start_tank: str) -> str:
    # Resolved coordinates, so the map page never needs to geocode again.
    query = urlencode({"start": start.as_param, "finish": finish.as_param, "start_tank": start_tank})
    return f"{reverse('route-map')}?{query}"
