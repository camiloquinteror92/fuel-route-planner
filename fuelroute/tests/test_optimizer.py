"""The optimizer: hand-checked cases, consolidation, and an exact DP comparison."""

import math
import random

import pytest

from fuelroute.services.optimizer import Candidate, UnreachableError, plan_fuel_stops

RANGE, MPG = 500.0, 10.0


def plan(route, stations, initial=RANGE, final=0.0, min_stop=0.0, capacity=RANGE):
    return plan_fuel_stops(
        route,
        [Candidate(mile=m, price=p, ref=f"S{m}") for m, p in stations],
        max_range_miles=capacity,
        miles_per_gallon=MPG,
        initial_fuel_miles=initial,
        final_fuel_miles=final,
        min_stop_gallons=min_stop,
    )


def drive(result, route, initial, capacity=RANGE):
    """Follow a plan mile by mile; return the fuel left at the finish (asserts tank limits)."""
    fuel, position = initial, 0.0
    for stop in result.stops:
        fuel -= stop.candidate.mile - position
        assert fuel >= -1e-6, "ran dry before a stop"
        fuel += stop.gallons * MPG
        assert fuel <= capacity + 1e-6, "bought more than the tank holds"
        position = stop.candidate.mile
    fuel -= route - position
    assert fuel >= -1e-6, "ran dry before the finish"
    return fuel


def test_short_trip_with_full_tank_needs_no_stop():
    result = plan(300, [(100, 3.0), (200, 2.5)])
    assert result.stops == []
    assert result.total_cost == 0
    assert result.final_fuel_gallons == pytest.approx(20.0)


def test_long_trip_needs_several_stops_and_never_runs_dry():
    stations = [(m, 3.0) for m in range(100, 1500, 100)]
    result = plan(1500, stations)
    assert len(result.stops) >= 2
    # Fuel bought + initial tank covers the whole trip exactly (arrive empty).
    assert result.total_gallons == pytest.approx((1500 - RANGE) / MPG)
    assert drive(result, 1500, RANGE) == pytest.approx(0.0)


def test_buys_only_enough_to_reach_a_cheaper_station_ahead():
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


def test_unreachable_mid_route_gap_says_where():
    with pytest.raises(UnreachableError) as error:
        plan(1200, [(100, 3.0), (700, 3.0)])
    assert error.value.from_mile == 100
    assert error.value.next_mile == 700
    assert not error.value.at_start


def test_unreachable_from_the_start_is_flagged():
    with pytest.raises(UnreachableError) as error:
        plan(400, [(80, 3.0)], initial=50)
    assert error.value.at_start
    assert error.value.next_mile == 80


def test_unreachable_destination_has_no_next_station():
    with pytest.raises(UnreachableError) as error:
        plan(1200, [(100, 3.0), (500, 3.0)])
    assert error.value.from_mile == 500
    assert error.value.next_mile is None


def test_empty_start_pays_for_every_mile():
    stations = [(m, 3.0 + (m % 7) / 10) for m in range(20, 2000, 60)]
    result = plan(2000, stations, initial=50, final=50)
    assert result.total_gallons == pytest.approx(2000 / MPG)


# --- stations at the start / destination city (mile 0 and mile == route) ----------------


def test_station_in_the_destination_city_can_sell_the_final_reserve():
    # Regression: stations exactly at the destination were never "ahead", so the
    # 50-mile reserve had to be bought at the dearer station at mile 0 ($90.00).
    result = plan(300, [(0, 3.0), (300, 2.0)], initial=50, final=50)
    assert result.total_cost == pytest.approx(250 / MPG * 3.0 + 50 / MPG * 2.0)  # $85.00
    assert [s.candidate.mile for s in result.stops] == [0, 300]


def test_station_in_the_destination_city_avoids_a_false_unreachable():
    # 460 miles + 50 reserve > 500 from mile 0, but buying the reserve at the
    # destination works. Regression: this used to raise "destination out of range".
    result = plan(460, [(0, 3.0), (460, 2.0)], initial=50, final=50)
    assert result.total_gallons == pytest.approx(46.0)
    assert drive(result, 460, 50) == pytest.approx(50.0)


# --- consolidation -----------------------------------------------------------------------


def test_consolidation_removes_tiny_stops_for_a_small_extra_cost():
    stations = [(10, 3.00), (20, 2.99), (480, 2.98), (490, 2.97), (900, 3.5)]
    exact = plan(1000, stations, initial=10)
    merged = plan(1000, stations, initial=10, min_stop=10)
    assert [s.candidate.mile for s in exact.stops] == [10, 20, 480, 490, 900]
    # The 1-gallon stops at miles 10 and 480 are merged away for $0.48 in total.
    assert [s.candidate.mile for s in merged.stops] == [10, 490, 900]
    assert merged.total_gallons == pytest.approx(exact.total_gallons)
    assert merged.total_cost - exact.total_cost == pytest.approx(0.48)
    # The last stop (1 gal at mile 900) is needed to finish and the stop before it
    # is already full: topping it up would cost $4.77, more than the $1 cap. Kept.
    assert merged.stops[-1].gallons == pytest.approx(1.0)
    assert drive(merged, 1000, 10) == pytest.approx(0.0)


def test_small_first_stop_absorbs_the_next_stop():
    # The New York -> Los Angeles pattern: the first station (mile 10) is 2 cents
    # dearer than the next (mile 70), so the optimum buys 2 gal there to reach it.
    # Merging it into the next stop runs dry, and there is no previous stop; buying
    # the next stop's 32.6 gal at mile 10 instead removes a stop for $0.65.
    stations = [(10, 3.099), (70, 3.079), (396, 2.90), (800, 3.2)]
    exact = plan(1000, stations, initial=50, final=50)
    assert exact.stops[0].gallons == pytest.approx(2.0)
    merged = plan(1000, stations, initial=50, final=50, min_stop=10)
    assert len(merged.stops) == len(exact.stops) - 1
    assert all(s.gallons >= 10 - 1e-9 for s in merged.stops)
    assert merged.stops[0].gallons == pytest.approx(34.6)
    assert merged.total_cost - exact.total_cost == pytest.approx(32.6 * 0.02)
    assert drive(merged, 1000, 50) == pytest.approx(50.0)


def test_consolidation_tops_up_when_removing_a_stop_costs_too_much():
    # The 9-gallon stop at mile 300 can only be removed by buying its fuel at $4.00
    # instead of $3.00 (+$9, over the $1 cap). Buying 1 more gallon there, and 1 less
    # at the $2.00 station, leaves every stop at 10+ gallons for exactly $1.
    stations = [(10, 4.00), (300, 3.00), (390, 2.00), (850, 2.50)]
    exact = plan(1000, stations, initial=10)
    merged = plan(1000, stations, initial=10, min_stop=10)
    assert exact.total_cost == pytest.approx(270.50)
    assert merged.total_cost == pytest.approx(271.50)
    assert all(s.gallons >= 10 - 1e-9 for s in merged.stops)


def test_consolidation_cost_cap_can_be_raised():
    stations = [(10, 4.00), (300, 3.00), (390, 2.00), (850, 2.50)]
    merged = plan_fuel_stops(
        1000, [Candidate(m, p) for m, p in stations], max_range_miles=RANGE, miles_per_gallon=MPG,
        initial_fuel_miles=10, min_stop_gallons=10, max_consolidation_cost=10.0,
    )
    # With a $10 cap the stop can be removed (+$9): one stop fewer.
    assert len(merged.stops) == 3
    assert merged.total_cost == pytest.approx(279.50)


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
    assert drive(merged, route, 50) == pytest.approx(50.0)
    assert merged.total_cost >= exact.total_cost - 1e-6
    assert merged.total_cost - exact.total_cost < 0.01 * exact.total_cost


# --- the greedy against an exact solution -------------------------------------------------


def _exact_cost(route, stations, capacity, initial, final):
    """Minimum cost by dynamic programming over integer fuel levels (integer miles).

    State: fuel in the tank on arrival at each station -> cheapest cost so far.
    Stations may be at mile 0 and at the destination. None when infeasible.
    """
    best, position = {initial: 0.0}, 0
    for mile, price in sorted(stations):
        leg = mile - position
        best = {fuel - leg: cost for fuel, cost in best.items() if fuel >= leg}
        bought = {}
        for fuel, cost in best.items():
            for buy in range(0, capacity - fuel + 1):
                value = cost + buy / MPG * price
                if value < bought.get(fuel + buy, math.inf):
                    bought[fuel + buy] = value
        best, position = bought, mile
    leg = route - position
    costs = [cost for fuel, cost in best.items() if fuel - leg >= final]
    return min(costs) if costs else None


@pytest.mark.parametrize("mode", ["reserve", "full", "random"])
@pytest.mark.parametrize("seed", range(30))
def test_greedy_matches_exact_dp(seed, mode):
    rng = random.Random(seed * 7 + len(mode))
    capacity, route = 30, 100
    miles = sorted(rng.choice([0, route]) if rng.random() < 0.15 else rng.randint(0, route) for _ in range(9))
    stations = [(m, round(rng.uniform(2.5, 4.0), 2)) for m in miles]
    if mode == "reserve":  # the API's default: arrive with what you left with
        initial = final = rng.randint(3, 12)
    elif mode == "full":
        initial, final = capacity, 0
    else:
        initial, final = rng.randint(0, capacity), rng.randint(0, 10)
    expected = _exact_cost(route, stations, capacity, initial, final)
    try:
        result = plan(route, stations, initial=initial, final=final, capacity=capacity)
    except UnreachableError:
        assert expected is None
        return
    assert expected is not None
    assert result.total_cost == pytest.approx(expected)
    assert drive(result, route, initial, capacity) >= final - 1e-6


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError):
        plan(100, [], initial=600)
    with pytest.raises(ValueError):
        plan_fuel_stops(100, [], max_range_miles=0, miles_per_gallon=10, initial_fuel_miles=0)
