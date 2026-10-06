"""Cheapest refuelling plan along a fixed route (the "gas station problem").

Pure Python, no Django, no I/O: it is unit-tested directly, including against an
exact dynamic-programming solution (``tests/test_optimizer.py``).

Model
-----
Positions are miles along the route (0 = start, ``route_miles`` = finish). Fuel is
tracked in MILES OF RANGE (gallons = miles / mpg), which keeps the arithmetic
simple: the tank holds ``max_range_miles``, driving d miles burns d. A station is a
``Candidate(mile, price)``; stations may sit at mile 0 (in the start city) and at
``route_miles`` (in the destination city).

The truck starts with ``initial_fuel_miles`` and must arrive with at least
``final_fuel_miles``. Prices are linear (no volume discounts) and there is no
cost per stop, so only WHERE each gallon is bought matters.

Step 1 - greedy (optimal under that model)
------------------------------------------
At each station, with price p:

* if a CHEAPER station is within one tank, buy only enough fuel to reach the
  nearest such station, and drive there;
* else, if the destination is within one tank, buy just enough to finish;
* else, FILL the tank and drive to the cheapest station within one tank.

Why it is optimal: every mile is driven on fuel bought at the cheapest station
that could have supplied it. Fuel bought here and burned after the next cheaper
station would have been cheaper there; and when no cheaper station is in range,
filling up here is the cheapest way to cover the miles until the next stop.
The scan stops at the end of the current tank (stations are sorted by mile, and a
binary search finds the last reachable one), so it is O(n log n + n*k) with k the
stations within one tank.

Step 2 - consolidation (practical, not optimal)
-----------------------------------------------
The pure optimum can say "stop to buy 2 gallons here because the next station is
2 cents cheaper". A stop that buys less than ``min_stop_gallons`` is fixed with
one of its neighbours, as long as the tank stays between empty and full:

1. preferably by REMOVING a stop: merge the small purchase into the previous /
   next stop, or move the neighbour's whole purchase into the small stop;
2. otherwise by topping the small stop up to the minimum (buying less at the
   neighbour), so the plan has no "2 gallons" line even if the stop stays.

Among the moves of the preferred kind the cheapest wins, and a move is applied only
if it costs at most ``max_extra_cost`` dollars ($1 by default). So the plan stays
within $1 per fix of the true optimum, and a small stop that cannot be fixed that
cheaply is kept (e.g. the last stop before the destination, when every other
station is already full). Each move changes only the two stops involved (fuel
leaving the second one is unchanged), so the rest of the plan stays valid.
"""

from __future__ import annotations

from bisect import bisect_right
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
    final_fuel_gallons: float = 0.0  # in the tank on arrival at the destination


class UnreachableError(Exception):
    """No feasible plan: some stretch of the route is longer than the range.

    ``from_mile``: where the truck gets stuck (0 = the start).
    ``next_mile``: the next station after that point, or None when the problem is
    reaching the destination.
    """

    def __init__(self, from_mile: float, next_mile: float | None, at_start: bool = False):
        self.from_mile = from_mile
        self.next_mile = next_mile
        self.at_start = at_start
        target = "the destination" if next_mile is None else f"the next station (mile {next_mile:.0f})"
        super().__init__(f"No fuel station reachable after mile {from_mile:.0f}; {target} is out of range.")


@dataclass
class _Purchase:
    station: Candidate
    bought: float  # miles of range bought here
    arrival: float  # miles of range in the tank on arrival here


def _greedy(
    route_miles: float,
    stations: list[Candidate],
    capacity: float,
    initial_fuel: float,
    final_fuel: float,
) -> tuple[list[_Purchase], float]:
    """Optimal purchases (see the module docstring) and the fuel left at the finish."""
    miles = [s.mile for s in stations]
    purchases: list[_Purchase] = []
    # At the start: nothing to buy, drive to the nearest station (price "infinite"
    # here, so any station is better), unless the tank already covers the trip.
    if route_miles + final_fuel <= initial_fuel + _EPS:
        return purchases, initial_fuel - route_miles
    if not stations or stations[0].mile > initial_fuel + _EPS:
        raise UnreachableError(0.0, stations[0].mile if stations else None, at_start=True)
    current, position = 0, stations[0].mile
    fuel = initial_fuel - position

    while True:
        here = stations[current]
        limit = position + capacity
        # Stations after this one and within one tank: indexes current+1 .. end-1.
        end = bisect_right(miles, limit + _EPS)
        ahead = range(current + 1, end)
        cheaper = next((i for i in ahead if stations[i].price < here.price), None)
        to_finish = route_miles - position + final_fuel

        if cheaper is not None:
            # Any fuel bought here beyond what reaches the cheaper station would be
            # burned after it, where it costs less: buy only the shortfall (maybe 0).
            target, distance = cheaper, stations[cheaper].mile - position
            buy = max(0.0, distance - fuel)
        elif to_finish <= capacity + _EPS:
            # This is the cheapest price left on the way: buy exactly what the rest of
            # the trip (plus the required arrival fuel) needs.
            target, distance = None, route_miles - position
            buy = max(0.0, to_finish - fuel)
        elif len(ahead):
            # No cheaper station within a tank: this price is the best for the next
            # ``capacity`` miles, so fill up, then continue from the cheapest station in
            # range (the farthest one on ties), where the same rules apply again.
            target = min(ahead, key=lambda i: (stations[i].price, -stations[i].mile))
            distance = stations[target].mile - position
            buy = capacity - fuel
        else:
            # Neither another station nor the destination is within one full tank.
            raise UnreachableError(position, stations[end].mile if end < len(stations) else None)

        if buy > _EPS:
            purchases.append(_Purchase(here, buy, fuel))
        fuel = fuel + buy - distance
        if target is None:
            return purchases, fuel
        current, position = target, stations[target].mile


def _shift(purchases: list[_Purchase], i: int, x: float, capacity: float, min_stop: float) -> bool:
    """Is moving ``x`` miles of purchase from stop i+1 to stop i valid (x < 0 = the other way)?

    Valid: both purchases stay >= 0, the tank leaving i stays <= capacity, the tank
    arriving at i+1 stays >= 0, and each of the two stops ends at 0 (removed) or at
    >= min_stop unless it was already smaller (a removal is always progress).
    """
    a, b = purchases[i], purchases[i + 1]
    new_a, new_b = a.bought + x, b.bought - x
    if new_a < -_EPS or new_b < -_EPS:
        return False
    if a.arrival + new_a > capacity + _EPS or b.arrival + x < -_EPS:
        return False
    for old, new in ((a.bought, new_a), (b.bought, new_b)):
        if new > _EPS and new < min_stop - _EPS and new < old - _EPS:
            return False  # would shrink a stop below the minimum
    return True


def _consolidate(
    purchases: list[_Purchase], capacity: float, min_stop: float, max_extra: float
) -> list[_Purchase]:
    """Fix stops that buy less than ``min_stop`` miles (see the module docstring).

    ``max_extra``: the most one move may add to the cost, in miles x $/gal (the
    caller converts dollars). Every applied move either removes a stop, or raises a
    small stop to exactly the minimum without creating a new small one, so the
    loop ends.
    """
    purchases = list(purchases)
    while True:
        options = []  # (keeps every stop -> False first, extra cost, pair index, x)
        for k, stop in enumerate(purchases):
            if stop.bought >= min_stop - _EPS:
                continue
            for i in (k - 1, k):  # the pair (k-1, k) and the pair (k, k+1)
                if i < 0 or i + 1 >= len(purchases):
                    continue
                a, b = purchases[i], purchases[i + 1]
                raise_by = min_stop - stop.bought
                for x in (
                    -a.bought,  # everything bought at a moves to b (a disappears)
                    b.bought,  # everything bought at b moves to a (b disappears)
                    raise_by if i == k else -raise_by,  # top the small stop up to min_stop
                ):
                    extra = x * (a.station.price - b.station.price)
                    if abs(x) > _EPS and extra <= max_extra + _EPS and _shift(purchases, i, x, capacity, min_stop):
                        removes = abs(a.bought + x) <= _EPS or abs(b.bought - x) <= _EPS
                        options.append((not removes, extra, i, x))
        if not options:
            return purchases
        _keeps, _extra, i, x = min(options)
        purchases[i].bought += x
        purchases[i + 1].bought -= x
        purchases[i + 1].arrival += x
        purchases = [p for p in purchases if p.bought > _EPS]


def plan_fuel_stops(
    route_miles: float,
    candidates: Sequence[Candidate],
    *,
    max_range_miles: float,
    miles_per_gallon: float,
    initial_fuel_miles: float,
    final_fuel_miles: float = 0.0,
    min_stop_gallons: float = 0.0,
    max_consolidation_cost: float = 1.0,
) -> FuelPlan:
    """Cheapest stops and how many gallons to buy at each.

    ``candidates``: stations on the route, any order; those outside [0, route_miles]
    are ignored. ``initial_fuel_miles``: range in the tank at the start.
    ``final_fuel_miles``: range that must be left on arrival (equal to the initial
    fuel means the plan pays for every mile driven). ``min_stop_gallons``: stops
    smaller than this are consolidated when a neighbour allows it (0 = off), each
    fix costing at most ``max_consolidation_cost`` dollars.

    Raises ``UnreachableError`` when some stretch is longer than the range, and
    ``ValueError`` for impossible parameters.
    """
    if max_range_miles <= 0 or miles_per_gallon <= 0:
        raise ValueError("max_range_miles and miles_per_gallon must be positive")
    if not 0 <= initial_fuel_miles <= max_range_miles or not 0 <= final_fuel_miles <= max_range_miles:
        raise ValueError("initial and final fuel must be between 0 and max_range_miles")
    if route_miles < 0:
        raise ValueError("route_miles must be >= 0")

    stations = sorted(
        (c for c in candidates if -_EPS <= c.mile <= route_miles + _EPS), key=lambda c: (c.mile, c.price)
    )
    purchases, final_fuel = _greedy(route_miles, stations, max_range_miles, initial_fuel_miles, final_fuel_miles)
    if min_stop_gallons > 0:
        purchases = _consolidate(
            purchases,
            max_range_miles,
            min_stop_gallons * miles_per_gallon,
            max_consolidation_cost * miles_per_gallon,  # dollars -> miles x $/gal
        )

    plan = FuelPlan(final_fuel_gallons=final_fuel / miles_per_gallon)
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
