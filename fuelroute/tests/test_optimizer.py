import itertools
import random

import pytest

from fuelroute.services.optimizer import Candidate, UnreachableError, plan_fuel_stops

RANGE, MPG = 500.0, 10.0


def plan(route, stations, initial=RANGE, final=0.0, min_stop=0.0):
    return plan_fuel_stops(
        route,
        [Candidate(mile=m, price=p, ref=f"S{m}") for m, p in stations],
        max_range_miles=RANGE,
        miles_per_gallon=MPG,
        initial_fuel_miles=initial,
        final_fuel_miles=final,
        min_stop_gallons=min_stop,
    )


def test_short_trip_with_full_tank_needs_no_stop():
    result = plan(300, [(100, 3.0), (200, 2.5)])
    assert result.stops == []
    assert result.total_cost == 0


def test_long_trip_needs_several_stops_and_never_runs_dry():
    stations = [(m, 3.0) for m in range(100, 1500, 100)]
    result = plan(1500, stations)
    assert len(result.stops) >= 2
    # Fuel bought + initial tank covers the whole trip exactly (arrive empty).
    assert result.total_gallons == pytest.approx((1500 - RANGE) / MPG)
    assert all(stop.fuel_on_arrival_gallons >= -1e-9 for stop in result.stops)


def test_buys_only_enough_to_reach_a_cheaper_station_ahead():
    # Start empty-ish (reserve 50), cheap station 300 miles after an expensive one.
    result = plan(700, [(40, 4.0), (340, 2.0)], initial=50)
    first, second = result.stops
    assert first.candidate.mile == 40
    # Arrives with 10 miles left, needs 300 miles to reach the cheap one: buys 29 gal.
    assert first.gallons == pytest.approx(29.0)
    assert second.candidate.mile == 340
    assert second.gallons == pytest.approx(36.0)  # 360 miles to finish
    assert result.total_cost == pytest.approx(29 * 4.0 + 36 * 2.0)


def test_fills_up_when_current_station_is_the_cheapest_in_range():
    result = plan(900, [(10, 2.0), (400, 3.0), (700, 3.5)], initial=10)
    first = result.stops[0]
    assert first.candidate.mile == 10
    assert first.gallons == pytest.approx(50.0)  # full tank at the cheap station


def test_unreachable_when_gap_is_longer_than_the_range():
    with pytest.raises(UnreachableError) as error:
        plan(1200, [(100, 3.0), (700, 3.0)])
    assert error.value.from_mile == 100
    assert error.value.next_mile == 700


def test_unreachable_when_no_station_near_the_start():
    with pytest.raises(UnreachableError):
        plan(400, [(80, 3.0)], initial=50)


def test_empty_start_pays_for_every_mile():
    stations = [(m, 3.0 + (m % 7) / 10) for m in range(20, 2000, 60)]
    result = plan(2000, stations, initial=50, final=50)
    assert result.total_gallons == pytest.approx(2000 / MPG)


def test_consolidation_removes_tiny_stops_for_a_small_extra_cost():
    stations = [(10, 3.00), (20, 2.99), (480, 2.98), (490, 2.97), (900, 3.5)]
    exact = plan(1000, stations, initial=10)
    merged = plan(1000, stations, initial=10, min_stop=10)
    assert len(merged.stops) < len(exact.stops)
    # The 1-gallon top-up at mile 480 is folded into the next stop (mile 490).
    assert 480 in [s.candidate.mile for s in exact.stops]
    assert 480 not in [s.candidate.mile for s in merged.stops]
    assert merged.total_gallons == pytest.approx(exact.total_gallons)
    assert merged.total_cost - exact.total_cost < 1.0


def _brute_force(route, stations, capacity, initial):
    """Exact DP over integer fuel levels (only valid for integer miles)."""
    points = sorted(stations) + [(route, 0.0)]
    best = {initial - points[0][0]: 0.0} if initial >= points[0][0] else {}
    for (mile, price), (next_mile, _) in itertools.pairwise(points):
        leg = next_mile - mile
        nxt = {}
        for fuel, cost in best.items():
            for buy in range(0, capacity - fuel + 1):
                left = fuel + buy - leg
                if left >= 0:
                    value = cost + buy / MPG * price
                    if value < nxt.get(left, float("inf")):
                        nxt[left] = value
        best = nxt
    return min(best.values()) if best else None


@pytest.mark.parametrize("seed", range(40))
def test_greedy_matches_brute_force_on_random_routes(seed):
    rng = random.Random(seed)
    capacity, route = 30, 100
    miles = sorted(rng.sample(range(1, route), 8))
    stations = [(m, round(rng.uniform(2.5, 4.0), 2)) for m in miles]
    initial = rng.randint(miles[0], capacity)
    expected = _brute_force(route, stations, capacity, initial)
    candidates = [Candidate(mile=m, price=p) for m, p in stations]
    try:
        result = plan_fuel_stops(
            route, candidates, max_range_miles=capacity, miles_per_gallon=MPG, initial_fuel_miles=initial
        )
    except UnreachableError:
        assert expected is None
        return
    assert result.total_cost == pytest.approx(expected)


@pytest.mark.parametrize("seed", range(40))
def test_consolidated_plan_is_feasible_and_close_to_optimal(seed):
    rng = random.Random(seed)
    route = 3000.0
    stations = [(rng.uniform(1, route - 1), round(rng.uniform(2.8, 3.6), 3)) for _ in range(120)]
    try:
        exact = plan(route, stations, initial=50, final=50)
    except UnreachableError:
        return
    merged = plan(route, stations, initial=50, final=50, min_stop=10)

    # Drive the plan: the tank never goes below zero nor above capacity.
    fuel, position = 50.0, 0.0
    for stop in merged.stops:
        fuel -= stop.candidate.mile - position
        assert fuel >= -1e-6
        fuel += stop.gallons * MPG
        assert fuel <= RANGE + 1e-6
        position = stop.candidate.mile
    assert fuel - (route - position) == pytest.approx(50.0)
    assert merged.total_cost >= exact.total_cost - 1e-6
    assert merged.total_cost - exact.total_cost < 0.01 * exact.total_cost
