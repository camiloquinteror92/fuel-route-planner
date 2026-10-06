"""The optimizer: hand-checked cases, consolidation, an exact DP comparison, the
per-stop decisions and the baselines it is compared with."""

import hashlib
import math
import random

import pytest

from fuelroute.services.optimizer import (
    Candidate,
    UnreachableError,
    plan_fuel_stops,
    plan_price_blind,
    plan_quarter_tank,
)

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
    with pytest.raises(ValueError):
        plan_price_blind(100, [], max_range_miles=RANGE, miles_per_gallon=MPG, initial_fuel_miles=600)
    with pytest.raises(ValueError):
        plan_quarter_tank(
            100, [], max_range_miles=RANGE, miles_per_gallon=MPG, initial_fuel_miles=0, refuel_below_fraction=2
        )


# --- per-stop decisions, the snapshot before consolidation and the baselines ---------------


def _random_instance(seed):
    """The instances of test_consolidated_plan_is_feasible_and_close_to_optimal."""
    rng = random.Random(seed)
    route = 3000.0
    return route, [(rng.uniform(1, route - 1), round(rng.uniform(2.8, 3.6), 3)) for _ in range(120)]


def _small_instance(seed, mode):
    """The instances of test_greedy_matches_exact_dp."""
    rng = random.Random(seed * 7 + len(mode))
    capacity, route = 30, 100
    miles = sorted(rng.choice([0, route]) if rng.random() < 0.15 else rng.randint(0, route) for _ in range(9))
    stations = [(m, round(rng.uniform(2.5, 4.0), 2)) for m in miles]
    if mode == "reserve":
        initial = final = rng.randint(3, 12)
    elif mode == "full":
        initial, final = capacity, 0
    else:
        initial, final = rng.randint(0, capacity), rng.randint(0, 10)
    return route, stations, capacity, initial, final


def _baseline(function, route, stations, initial, final, capacity=RANGE, **extra):
    return function(
        route,
        [Candidate(mile=m, price=p, ref=f"S{m}") for m, p in stations],
        max_range_miles=capacity,
        miles_per_gallon=MPG,
        initial_fuel_miles=initial,
        final_fuel_miles=final,
        **extra,
    )


@pytest.mark.parametrize("mode", ["reserve", "full", "random"])
@pytest.mark.parametrize("seed", range(30))
def test_price_blind_driver_buys_the_same_gallons_and_never_beats_the_optimum(seed, mode):
    route, stations, capacity, initial, final = _small_instance(seed, mode)
    try:
        optimum = plan(route, stations, initial=initial, final=final, capacity=capacity)  # not consolidated
    except UnreachableError:
        with pytest.raises(UnreachableError):
            _baseline(plan_price_blind, route, stations, initial, final, capacity)
        return
    blind = _baseline(plan_price_blind, route, stations, initial, final, capacity)
    assert blind.total_gallons == pytest.approx(optimum.total_gallons, abs=1e-6)
    assert blind.total_cost >= optimum.total_cost - 1e-9
    assert drive(blind, route, initial, capacity) >= final - 1e-6
    assert all(stop.rule == "baseline" for stop in blind.stops)


@pytest.mark.parametrize("mode", ["reserve", "full", "random"])
@pytest.mark.parametrize("seed", range(30))
def test_quarter_tank_driver_is_feasible_iff_greedy_is(seed, mode):
    route, stations, capacity, initial, final = _small_instance(seed, mode)
    try:
        optimum = plan(route, stations, initial=initial, final=final, capacity=capacity)
    except UnreachableError:
        with pytest.raises(UnreachableError):
            _baseline(plan_quarter_tank, route, stations, initial, final, capacity, refuel_below_fraction=0.25)
        return
    quarter = _baseline(plan_quarter_tank, route, stations, initial, final, capacity, refuel_below_fraction=0.25)
    assert quarter.total_gallons == pytest.approx(optimum.total_gallons, abs=1e-6)
    assert quarter.total_cost >= optimum.total_cost - 1e-9
    assert drive(quarter, route, initial, capacity) >= final - 1e-6


def test_baselines_refuel_by_habit_not_by_price():
    stations = [(100, 2.0), (200, 4.0), (650, 3.0)]
    # Mile 100 is the cheapest, but with fuel to reach mile 200 the price-blind driver
    # drives on, fills up at mile 200 ($4.00) and then has enough to finish.
    blind = _baseline(plan_price_blind, 700, stations, 250, 0)
    assert [(s.candidate.mile, s.gallons) for s in blind.stops] == [(200, pytest.approx(45.0))]
    # A "refuel at half a tank" driver stops at mile 100 (150 miles left): full tank.
    half = _baseline(plan_quarter_tank, 700, stations, 250, 0, refuel_below_fraction=0.5)
    assert [(s.candidate.mile, s.gallons) for s in half.stops] == [(100, pytest.approx(35.0)), (200, pytest.approx(10.0))]
    optimum = plan(700, stations, initial=250)
    assert optimum.total_cost < half.total_cost < blind.total_cost
    assert optimum.total_gallons == pytest.approx(blind.total_gallons) == pytest.approx(half.total_gallons)


def test_baselines_do_not_pick_a_station_by_price_in_a_town():
    # Two stations in one town (same mile: coordinates are city-level). The driver
    # stops at the first one in the caller's order, whichever is cheaper.
    for order in ([(300, 4.0), (300, 2.0)], [(300, 2.0), (300, 4.0)]):
        result = _baseline(plan_price_blind, 600, order, 300, 0)
        assert [s.candidate.price for s in result.stops] == [order[0][1]]


@pytest.mark.parametrize("seed", range(40))
def test_decisions_explain_each_purchase(seed):
    route, stations = _random_instance(seed)
    price_at = dict(stations)
    try:
        result = plan(route, stations, initial=50, final=50)
    except UnreachableError:
        return
    assert result.stops
    for stop in result.stops:
        mile, price = stop.candidate.mile, stop.candidate.price
        leaving = (stop.fuel_on_arrival_gallons + stop.gallons) * MPG  # miles in the tank
        assert stop.rule in ("reach_cheaper", "fill_up", "finish")
        assert not stop.consolidated
        if stop.rule == "reach_cheaper":
            target = stop.cheaper_station_mile
            assert price_at[target] < price
            assert 0 < target - mile <= RANGE + 1e-6
            assert leaving == pytest.approx(target - mile)  # just enough to reach it
        else:
            assert stop.cheaper_station_mile is None
        if stop.rule == "fill_up":
            assert leaving == pytest.approx(RANGE)
        if stop.rule == "finish":
            assert leaving == pytest.approx(route - mile + 50)  # arrives with the required 50
    assert result.stops[-1].rule == "finish"


@pytest.mark.parametrize("seed", range(40))
def test_plan_reports_the_stops_before_consolidation(seed):
    route, stations = _random_instance(seed)
    try:
        exact = plan(route, stations, initial=50, final=50)
    except UnreachableError:
        return
    assert exact.before_consolidation == exact.stops  # consolidation off: the same stops
    merged = plan(route, stations, initial=50, final=50, min_stop=10)
    # The snapshot is the plain optimum: _consolidate (which works in place) did not touch it.
    assert [(s.candidate, s.gallons) for s in merged.before_consolidation] == [
        (s.candidate, s.gallons) for s in exact.stops
    ]
    before = {s.candidate: s for s in merged.before_consolidation}
    for stop in merged.stops:
        original = before[stop.candidate]  # consolidation only moves fuel between existing stops
        assert stop.consolidated == (abs(stop.gallons - original.gallons) * MPG > 1e-9)
        assert stop.rule == original.rule


def test_consolidated_stops_are_flagged():
    stations = [(10, 3.00), (20, 2.99), (480, 2.98), (490, 2.97), (900, 3.5)]
    merged = plan(1000, stations, initial=10, min_stop=10)
    assert [s.candidate.mile for s in merged.before_consolidation] == [10, 20, 480, 490, 900]
    # The 1-gallon stop at mile 480 moves back into mile 20, and mile 20's 47 gal into
    # mile 10: only mile 10 changed; 490 and the last stop are the greedy's.
    assert [(s.candidate.mile, s.consolidated) for s in merged.stops] == [(10, True), (490, False), (900, False)]
    assert merged.stops[0].gallons == pytest.approx(48.0)
    # Where mile 10's 48 gal were planned by the greedy: here, at mile 20 and at mile 480.
    greedy = merged.before_consolidation
    origins = {greedy[k].candidate.mile: gallons for k, gallons in merged.stops[0].sources.items()}
    assert origins == {
        10: pytest.approx(greedy[0].gallons), 20: pytest.approx(greedy[1].gallons), 480: pytest.approx(1.0),
    }
    assert merged.stops[1].sources == {3: pytest.approx(greedy[3].gallons)}  # untouched: only its own fuel


@pytest.mark.parametrize("seed", range(40))
def test_consolidation_says_where_each_gallon_was_planned(seed):
    route, stations = _random_instance(seed)
    try:
        merged = plan(route, stations, initial=50, final=50, min_stop=10)
    except UnreachableError:
        return
    greedy = merged.before_consolidation
    received = [0.0] * len(greedy)
    extra = 0.0
    for stop in merged.stops:
        assert greedy[stop.greedy_index].candidate == stop.candidate
        assert stop.greedy_gallons == pytest.approx(greedy[stop.greedy_index].gallons)
        assert sum(stop.sources.values()) == pytest.approx(stop.gallons)  # every gallon has an origin
        for k, gallons in stop.sources.items():
            assert gallons > 0
            received[k] += gallons
            extra += gallons * (stop.candidate.price - greedy[k].candidate.price)
    # Nothing is lost or invented: each greedy purchase ends up somewhere, whole...
    assert received == pytest.approx([s.gallons for s in greedy])
    # ...and the cost of consolidation is exactly the cost of moving that fuel.
    assert extra == pytest.approx(merged.total_cost - sum(s.cost for s in greedy))


# Fingerprint of plan_fuel_stops on the 40 random instances above, with and without
# consolidation, computed with the optimizer as it was before the decisions, the
# snapshot and the baselines were added (commit ad124b0). Explaining the plan must
# not change a single stop, gallon or cent of it.
_RESULTS_BEFORE_TRACING = "4500b5953208f1afcf9ebe41abeac92d6eb6907b392c5a8e6c64398fea69c00d"


def test_trace_does_not_change_results():
    lines = []
    for seed in range(40):
        for min_stop in (0.0, 10.0):
            route, stations = _random_instance(seed)
            try:
                result = plan_fuel_stops(
                    route,
                    [Candidate(mile, price) for mile, price in stations],
                    max_range_miles=RANGE,
                    miles_per_gallon=MPG,
                    initial_fuel_miles=50,
                    final_fuel_miles=50,
                    min_stop_gallons=min_stop,
                )
            except UnreachableError as error:
                lines.append(f"{seed}:{min_stop}:unreachable:{error.from_mile:.6f}")
                continue
            stops = ";".join(
                f"{s.candidate.mile:.6f}/{s.gallons:.6f}/{s.fuel_on_arrival_gallons:.6f}" for s in result.stops
            )
            lines.append(f"{seed}:{min_stop}:{result.total_cost:.6f}:{result.final_fuel_gallons:.6f}:{stops}")
    assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == _RESULTS_BEFORE_TRACING
