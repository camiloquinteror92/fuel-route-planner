"""Cheapest refueling plan along a fixed route (the "gas station problem").

Pure Python, no Django: easy to unit test.

Positions are miles along the route. Fuel is tracked in miles of range
(gallons = miles / mpg) so the arithmetic stays simple.

Step 1 - greedy (optimal for a fixed route, linear prices, tank of R miles).
At a station with price p:
  * if a CHEAPER station is within R miles, buy only enough fuel to reach the
    nearest such station, and go there;
  * else, if the destination is within R miles, buy just enough to finish;
  * else, FILL the tank and go to the cheapest station within R miles.
Every gallon is bought at the cheapest price available before it is burned, so
no plan is cheaper. It runs in O(n * k), k = stations inside one tank range.

Step 2 - consolidation. The pure optimum can contain silly stops (pull over to
buy 1.2 gallons because the next station is 0.6 cents cheaper). A stop that buys
less than ``min_stop_miles`` is merged into the previous or next stop, whichever
costs less and keeps the tank within limits. The extra cost is tiny (cents) and
the plan becomes something a driver would actually follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

_EPS = 1e-9


@dataclass(frozen=True)
class Candidate:
    mile: float  # position along the route, miles from the start
    price: float  # USD per gallon
    ref: Any = None  # whatever the caller needs back (station id, row...)


@dataclass
class FuelStop:
    candidate: Candidate
    gallons: float
    cost: float
    fuel_on_arrival_gallons: float


@dataclass
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)
    total_gallons: float = 0.0
    total_cost: float = 0.0


class UnreachableError(Exception):
    def __init__(self, from_mile: float, next_mile: float | None):
        self.from_mile = from_mile
        self.next_mile = next_mile
        gap = "the destination" if next_mile is None else f"the next station (mile {next_mile:.0f})"
        super().__init__(f"No fuel station reachable after mile {from_mile:.0f}; {gap} is out of range.")


@dataclass
class _Purchase:
    station: Candidate
    bought: float  # miles of range bought
    arrival: float  # miles of range in the tank on arrival


def _greedy(
    route_miles: float,
    stations: list[Candidate],
    capacity: float,
    initial_fuel: float,
    final_fuel: float,
) -> list[_Purchase]:
    purchases: list[_Purchase] = []
    position, fuel, current = 0.0, initial_fuel, -1  # -1: at the origin, nothing to buy

    while True:
        if current == -1:
            if route_miles + final_fuel <= fuel + _EPS:
                return purchases
            # At the origin the price is "infinite": drive to the nearest station.
            reachable = [i for i, s in enumerate(stations) if s.mile <= fuel + _EPS]
            if not reachable:
                raise UnreachableError(0.0, stations[0].mile if stations else None)
            current = reachable[0]
            position = stations[current].mile
            fuel -= position
            continue

        here = stations[current]
        limit = position + capacity
        ahead = [
            i for i in range(current + 1, len(stations))
            if stations[i].mile <= limit + _EPS and stations[i].mile < route_miles - _EPS
        ]
        cheaper = next((i for i in ahead if stations[i].price < here.price), None)
        to_finish = route_miles - position + final_fuel

        if cheaper is not None:
            target, distance = cheaper, stations[cheaper].mile - position
            buy = max(0.0, distance - fuel)
        elif to_finish <= capacity + _EPS:
            target, distance = None, route_miles - position
            buy = max(0.0, to_finish - fuel)
        elif ahead:
            # Fill up, then go to the cheapest station in range (the farthest one on ties).
            target = min(ahead, key=lambda i: (stations[i].price, -stations[i].mile))
            distance = stations[target].mile - position
            buy = capacity - fuel
        else:
            later = next((s.mile for s in stations[current + 1:] if s.mile > limit), None)
            raise UnreachableError(position, later)

        if buy > _EPS:
            purchases.append(_Purchase(here, buy, fuel))
        fuel = fuel + buy - distance
        if target is None:
            return purchases
        current, position = target, stations[target].mile


def _consolidate(purchases: list[_Purchase], capacity: float, min_stop_miles: float) -> list[_Purchase]:
    """Merge stops that buy less than ``min_stop_miles`` into a neighbour stop.

    * into the previous stop: it buys more, so the tank must still fit;
    * into the next stop: we arrive there with less fuel, so it must stay >= 0.
    Departure fuel at every other stop is unchanged, so the rest of the plan holds.
    """
    purchases = list(purchases)
    while True:
        small = sorted(
            (k for k, p in enumerate(purchases) if p.bought < min_stop_miles - _EPS),
            key=lambda k: purchases[k].bought,
        )
        for k in small:
            stop = purchases[k]
            options = []
            if k > 0:
                prev = purchases[k - 1]
                if prev.arrival + prev.bought + stop.bought <= capacity + _EPS:
                    options.append((stop.bought * (prev.station.price - stop.station.price), k - 1))
            if k + 1 < len(purchases):
                nxt = purchases[k + 1]
                if nxt.arrival - stop.bought >= -_EPS:
                    options.append((stop.bought * (nxt.station.price - stop.station.price), k + 1))
            if not options:
                continue
            _delta, into = min(options)
            target = purchases[into]
            target.bought += stop.bought
            if into > k:
                target.arrival -= stop.bought
            del purchases[k]
            break
        else:
            return purchases


def plan_fuel_stops(
    route_miles: float,
    candidates: Sequence[Candidate],
    *,
    max_range_miles: float,
    miles_per_gallon: float,
    initial_fuel_miles: float,
    final_fuel_miles: float = 0.0,
    min_stop_gallons: float = 0.0,
) -> FuelPlan:
    """Cheapest stops and how many gallons to buy at each.

    ``initial_fuel_miles``: range in the tank at the start (no station at mile 0).
    ``final_fuel_miles``: range that must be left on arrival. Setting it equal to
    ``initial_fuel_miles`` makes the plan pay for every mile of the trip.
    ``min_stop_gallons``: stops smaller than this are merged when possible.
    """
    if max_range_miles <= 0 or miles_per_gallon <= 0:
        raise ValueError("max_range_miles and miles_per_gallon must be positive")
    if not 0 <= initial_fuel_miles <= max_range_miles or not 0 <= final_fuel_miles <= max_range_miles:
        raise ValueError("initial and final fuel must be between 0 and max_range_miles")

    stations = sorted(
        (c for c in candidates if -_EPS <= c.mile <= route_miles + _EPS), key=lambda c: (c.mile, c.price)
    )
    purchases = _greedy(route_miles, stations, max_range_miles, initial_fuel_miles, final_fuel_miles)
    if min_stop_gallons > 0:
        purchases = _consolidate(purchases, max_range_miles, min_stop_gallons * miles_per_gallon)

    plan = FuelPlan()
    for purchase in purchases:
        gallons = purchase.bought / miles_per_gallon
        plan.stops.append(
            FuelStop(
                candidate=purchase.station,
                gallons=gallons,
                cost=gallons * purchase.station.price,
                fuel_on_arrival_gallons=max(0.0, purchase.arrival) / miles_per_gallon,
            )
        )
    plan.total_gallons = sum(s.gallons for s in plan.stops)
    plan.total_cost = sum(s.cost for s in plan.stops)
    return plan
