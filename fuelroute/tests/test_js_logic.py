"""The page's own logic, run in Node.js: what the browser computes from an API answer.

The page draws only what the API answers, but some sentences are arithmetic on it:
the "Why here" of every stop, the comparison with the standard truck and why it
changed, the derived values of the template, how long "Play" lasts and the bold part
of a city suggestion. These tests import the real ES modules of
``fuelroute/static/fuelroute/js/`` (pure functions, no DOM) and check them on small
answers built by hand. They need Node.js; without it they are skipped (the rest of
the suite does not).
"""

import json
import shutil
import subprocess

import pytest

from fuelroute import web

NODE = shutil.which("node")
JS = (web.STATIC_DIR / "fuelroute" / "js").as_uri()
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def run_js(script: str):
    """Run an ES module in Node (``JS/`` = the page's js folder) and return what it prints as JSON."""
    code = script.replace("JS/", f"{JS}/")
    done = subprocess.run(
        [NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60, check=False,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def plan(**changes) -> dict:
    """A 100-mile trip at 8 mpg (62.5-gal tank): leaves with 6.25 gal, one stop at mile 40
    that fills the tank, arrives with 55 gal."""
    body = {
        "start": {"label": "A", "query": "A"},
        "finish": {"label": "B", "query": "B"},
        "route": {"distance_miles": 100.0},
        "vehicle": {
            "miles_per_gallon": 8.0, "max_range_miles": 500.0, "usable_range_miles": 500.0, "tank_gallons": 62.5,
            "start_tank": "empty", "safety_reserve_gal": 0.0, "price_policy": "median", "consolidate": False,
            "corridor_miles": 10.0,
        },
        "summary": {
            "total_fuel_cost": 183.75, "total_gallons_purchased": 61.25, "fuel_used_gallons": 12.5,
            "start_fuel_gallons": 6.25, "end_fuel_gallons": 55.0, "unpriced_fuel_gallons": 0.0, "number_of_stops": 1,
            "average_price_paid": 3.0, "candidate_stations_on_route": 3, "comparison": None,
            "price_per_gallon_on_route": {"min": 2.9, "max": 3.6}, "tiny_stops_merged": None,
        },
        "fuel_stops": [
            {"stop": 1, "city": "C", "state": "TX", "name": "STOP 1", "mile_marker": 40.0, "price_per_gallon": 3.0,
             "fuel_on_arrival_gallons": 1.25, "gallons": 61.25, "cost": 183.75},
        ],
        "meta": {"external_api_calls": 0, "external_api_services": [], "plan_cache": "miss", "route_cache": "hit", "settings_changed": []},
        "warnings": [],
        "map": {"geojson": {"features": []}},
    }
    for path, value in changes.items():
        node = body
        *parents, last = path.split("__")
        for key in parents:
            node = node[key]
        node[last] = value
    return body


def stop(n, mile, price, decision, city="C"):
    return {"stop": n, "city": city, "state": "TX", "name": f"STOP {n}", "mile_marker": mile, "price_per_gallon": price,
            "fuel_on_arrival_gallons": 0.0, "gallons": 10.0, "cost": 30.0, "decision": decision}


def station(city, mile, price):
    return {"name": f"{city} STOP", "city": city, "state": "OH", "mile": mile, "price_per_gallon": price}


def decision(rule, **fields):
    base = {"rule": rule, "cheaper_station": None, "consolidated": False, "greedy_gallons": 10.0, "moved_in": [],
            "moved_out": [], "consolidation_extra_cost": 0.0, "reaches": None, "fills_tank": rule == "fill_up"}
    return {**base, **fields}


def test_why_text_is_plain_and_names_the_places():
    stops = [
        # Rule 1, the cheaper station is the next stop: its number, town and price are named.
        stop(1, 10.0, 3.099, decision("reach_cheaper", cheaper_station=station("Toledo", 70.0, 3.009),
                                      reaches={"stop": 2, "mile": 70.0})),
        stop(2, 70.0, 3.009, decision("reach_cheaper", cheaper_station=station("Gary", 300.0, 2.95),
                                      reaches={"stop": 3, "mile": 250.0})),
        stop(3, 250.0, 2.9, decision("fill_up", reaches={"stop": 4, "mile": 700.0})),
        stop(4, 700.0, 3.2, decision("finish", reaches={"stop": None, "mile": 900.0})),
        # "Skip tiny stops": the rules alone make a tiny stop here, so the next one is folded in.
        stop(5, 10.0, 3.099, decision(
            "reach_cheaper", cheaper_station=station("Bloomsbury", 70.0, 3.08), consolidated=True, greedy_gallons=2.0,
            moved_in=[{"mile": 70.0, "city": "Bloomsbury", "state": "NJ", "gallons": 32.6, "from_stop": None}],
            consolidation_extra_cost=0.65, reaches={"stop": 2, "mile": 396.0})),
        # ...or the stop it skips is the tiny one (the case of the Denver -> Chicago review).
        stop(6, 35.0, 2.899, decision(
            "reach_cheaper", cheaper_station=station("Ogallala", 190.0, 2.85), consolidated=True, greedy_gallons=14.0,
            moved_in=[{"mile": 190.0, "city": "Ogallala", "state": "NE", "gallons": 1.9, "from_stop": None}],
            consolidation_extra_cost=0.14)),
        stop(7, 500.0, 2.9, decision(
            "fill_up", consolidated=True, greedy_gallons=20.0,
            moved_in=[{"mile": 640.0, "city": "C", "state": "TX", "gallons": 3.0, "from_stop": 8}],
            consolidation_extra_cost=0.2)),
        stop(8, 640.0, 3.0, decision(
            "finish", consolidated=True, greedy_gallons=13.0,
            moved_out=[{"mile": 500.0, "gallons": 3.0, "to_stop": 7}], consolidation_extra_cost=0.004)),
        stop(9, 800.0, 3.0, decision("fill_up", consolidated=True, greedy_gallons=12.0, consolidation_extra_cost=0.0)),
    ]
    body = plan(fuel_stops=stops, vehicle__usable_range_miles=450.0)
    why = run_js(f"""
        import {{ whyText }} from 'JS/plan.js';
        const body = {json.dumps(body)};
        const about = {{ vehicle: {{ min_stop_gallons: 10 }} }};
        console.log(JSON.stringify(body.fuel_stops.map((s) => whyText(s, body, about))));
    """)
    assert why == [
        "Fuel is cheaper at stop 2, Toledo, OH: $3.009 vs $3.099 here. Buy just enough to get there.",
        "Fuel is cheaper at Gary, OH (mile 300.0): $2.950 vs $3.009 here. Buy just enough to get there.",
        "Nothing within 450 mi is cheaper. Fill the tank.",
        "No cheaper fuel before the finish. Buy just enough to finish.",
        "The three rules alone would buy 2.00 gal here, to reach cheaper fuel in Bloomsbury, OH: a tiny stop. So it also"
        " buys here the 32.60 gal planned in Bloomsbury, NJ (mile 70.0), and that stop is skipped. Cost of the change: +$0.65.",
        "The three rules alone buy 14.00 gal here, to reach cheaper fuel in Ogallala, OH. It also buys here the 1.90 gal"
        " the rules planned in Ogallala, NE (mile 190.0), a tiny stop that is skipped. Cost of the change: +$0.14.",
        "The three rules alone fill the tank here (20.00 gal): nothing within 450 mi is cheaper. It also buys here 3.00 gal"
        " that stop 8 would have bought, so no stop is tiny. Cost of the change: +$0.20.",
        # Less than half a cent is not a cost worth naming.
        "The three rules alone buy 13.00 gal here, just enough to finish. 3.00 gal of it is bought at stop 7 instead,"
        " so no stop is tiny.",
        "The three rules alone fill the tank here (12.00 gal): nothing within 450 mi is cheaper. Adjusted so no stop buys"
        " less than 10.00 gal.",
    ]
    for text in why:
        assert "greedy" not in text.lower() and "consolidat" not in text.lower(), text


def test_compare_is_honest_with_a_full_tank_and_says_why():
    out = run_js(f"""
        import {{ answerText, compareRows, quickTrySettings, whyChanged, QUICK_TRIES }} from 'JS/whatif.js';
        const base = {json.dumps(plan())};
        const full = {json.dumps(plan(
            vehicle__start_tank="full", summary__total_fuel_cost=150.0, summary__average_price_paid=2.9,
            summary__start_fuel_gallons=62.5, meta__settings_changed=["start_tank"],
        ))};
        const thirsty = {json.dumps(plan(summary__total_fuel_cost=200.0, summary__total_gallons_purchased=66.0,
                                         summary__fuel_used_gallons=13.75, vehicle__miles_per_gallon=7.0,
                                         vehicle__max_range_miles=437.5, meta__settings_changed=["mpg", "max_range_miles"]))};
        const safety = {json.dumps(plan(summary__total_fuel_cost=186.27, vehicle__safety_reserve_gal=5.0,
                                        vehicle__usable_range_miles=460.0, meta__settings_changed=["safety_reserve_gal"]))};
        const merged = {json.dumps(plan(summary__total_fuel_cost=185.28, summary__number_of_stops=0,
                                        vehicle__consolidate=True, meta__settings_changed=["consolidate"]))};
        const defaults = {{ mpg: 10, max_range_miles: 500, corridor_miles: 10, safety_reserve_gal: 0, price_policy: 'median', consolidate: false }};
        console.log(JSON.stringify({{
            full: compareRows(full, base), fullText: answerText(full, base), fullWhy: whyChanged(full, base),
            thirsty: compareRows(thirsty, base), thirstyText: answerText(thirsty, base), thirstyWhy: whyChanged(thirsty, base),
            safetyWhy: whyChanged(safety, base), mergedWhy: whyChanged(merged, base), sameWhy: whyChanged(base, base),
            tries: QUICK_TRIES.map((t) => quickTrySettings(t, defaults)),
        }}));
    """)
    # Regression: "−$33.75" read as a saving; the free first tank is not comparable.
    assert out["full"]["notComparable"] is True
    assert [row["diff"] for row in out["full"]["rows"]] == ["not comparable", "same", "not comparable"]
    assert out["full"]["perGallon"] == "Price per gallon: $2.900 (yours) vs $3.000 (standard)."
    assert "$33.75" not in out["fullText"] and out["fullText"].endswith(out["full"]["perGallon"])
    assert out["fullWhy"] == ["The first tank (62.50 gal) is free, so the bill counts only the fuel bought on the way."]
    # A comparable change says the difference in dollars, stops and gallons, and why.
    assert [row["diff"] for row in out["thirsty"]["rows"]] == ["+$16.25", "same", "+4.75 gal"]
    assert out["thirstyText"] == "Your truck: $200.00, 1 stop: +$16.25 against the standard truck."
    assert out["thirstyWhy"] == [
        "Same miles, worse mileage: the trip burns 13.75 gal instead of 12.50 gal.",
        "Same 62.50 gal tank, but it now lasts 437.5 mi instead of 500 mi.",
    ]
    # Regression: the same gallons for more money, with no word on why.
    assert out["safetyWhy"] == [
        "Keeping 5.00 gal unused shortens each tank to 460 mi, so some fuel must be bought at pricier truck stops.",
        "Both trucks buy the same fuel; only where they buy it changes.",
    ]
    assert out["mergedWhy"][0] == "Skipping tiny stops: 1 fewer stop, for $1.53 more."
    assert out["sameWhy"] == ["On this trip it changed nothing."]
    # "8 mpg" keeps the 50-gal tank of the standard truck: 400 miles on a full tank.
    assert out["tries"] == [
        {"start_tank": "empty", "settings": {"mpg": 8, "max_range_miles": 400}},
        {"start_tank": "empty", "settings": {"safety_reserve_gal": 5}},
        {"start_tank": "full", "settings": {}},
        {"start_tank": "empty", "settings": {"consolidate": True}},
    ]


def test_skipping_tiny_stops_that_changes_nothing_says_only_that():
    # Regression: "some fuel moved from a tiny stop to a nearby one" next to "On this trip
    # it changed nothing", on a trip with no tiny stop. The API's tiny_stops_merged says
    # what merging itself changed.
    merging = {"vehicle__consolidate": True, "meta__settings_changed": ["consolidate"]}
    out = run_js(f"""
        import {{ whyChanged }} from 'JS/whatif.js';
        const base = {json.dumps(plan())};
        const same = {json.dumps(plan(**merging, summary__tiny_stops_merged={"stops_before": 1, "extra_cost": 0.0}))};
        const moved = {json.dumps(plan(**merging, summary__total_fuel_cost=184.0,
                                       summary__tiny_stops_merged={"stops_before": 1, "extra_cost": 0.25}))};
        console.log(JSON.stringify({{ same: whyChanged(same, base), moved: whyChanged(moved, base) }}));
    """)
    assert out["same"] == ["On this trip it changed nothing."]
    assert out["moved"][0] == "Skipping tiny stops: some fuel moved from a tiny stop to a nearby one, for $0.25 more."


def test_the_tank_controls_turn_a_tank_and_mileage_into_a_range():
    out = run_js("""
        import { rangeOfTank, tankLimits } from 'JS/settings.js';
        const about = { api: { params: [{ name: 'max_range_miles', min: 100, max: 1500 }] } };
        console.log(JSON.stringify({ thirsty: rangeOfTank(50, 8), odd: rangeOfTank('62.5', '7'), bad: rangeOfTank('x', 8),
                                     at8: tankLimits(about, 8), at10: tankLimits(about, 10) }));
    """)
    # Regression: 8 mpg grew the tank to 62.5 gal and kept 500 miles; now the tank stays and the range follows.
    assert out["thirsty"] == 400 and out["odd"] == 437.5 and out["bad"] is None
    assert out["at10"] == {"min": 10, "max": 150, "step": 0.5}
    assert out["at8"] == {"min": 12.5, "max": 187.5, "step": 0.5}


def test_longest_stretch():
    out = run_js(f"""
        import {{ longestStretch }} from 'JS/checks.js';
        const one = {json.dumps(plan())};
        const none = {json.dumps(plan(fuel_stops=[]))};
        const many = {json.dumps(plan(route__distance_miles=900.0, fuel_stops=[
            {"stop": 1, "mile_marker": 10.0}, {"stop": 2, "mile_marker": 396.0}, {"stop": 3, "mile_marker": 562.0}]))};
        console.log(JSON.stringify([longestStretch(one), longestStretch(none), longestStretch(many)]));
    """)
    # From the start, between stops and to the finish: 40 then 60; the whole trip; 386.
    assert out == [60.0, 100.0, 386.0]


def test_derived_values_agree_in_number_and_name_the_truck():
    out = run_js(f"""
        import {{ derive, hiddenSettingsText, yourTruckText }} from 'JS/format.js';
        const one = {json.dumps(plan(summary__comparison={
            "savings_vs_price_blind": {"amount": 1.0, "percent": 0.5, "extra_stops": 1},
            "price_blind": {"total_fuel_cost": 184.75, "total_gallons_purchased": 61.25, "number_of_stops": 0}}))};
        const fewer = {json.dumps(plan(summary__comparison={
            "savings_vs_price_blind": {"amount": 1.0, "percent": 0.5, "extra_stops": -2},
            "price_blind": {"total_fuel_cost": 184.75, "total_gallons_purchased": 61.3, "number_of_stops": 3}}))};
        const full = {json.dumps(plan(vehicle__start_tank="full", vehicle__safety_reserve_gal=5.0, vehicle__miles_per_gallon=8,
                                       vehicle__max_range_miles=400.0, vehicle__tank_gallons=50.0, vehicle__consolidate=True,
                                       summary__start_fuel_gallons=50.0))};
        // Dallas -> Austin: 196 miles, one tank would do, but the truck leaves with 5 gal.
        const short = {json.dumps(plan(route__distance_miles=196.0, vehicle__miles_per_gallon=10.0, vehicle__tank_gallons=50.0,
                                        summary__start_fuel_gallons=5.0, summary__end_fuel_gallons=5.0))};
        const long = {json.dumps(plan(route__distance_miles=2810.4))};
        const about = {{ vehicle: {{ min_stop_gallons: 10 }}, data: {{ stations_by_state: [{{ state: 'CA', stations: 8, geocoded: 8 }}] }} }};
        const d = (body, params = {{ settings: {{}} }}, extra = {{}}) => derive({{ route: {{ ok: true, body }}, about, params, ...extra }});
        console.log(JSON.stringify({{
            one: d(one), fewer: d(fewer), full: d(full, {{ start_tank: 'full', settings: {{ corridor_miles: 25 }} }}),
            short: d(short), long: d(long), withStandard: d(full, {{ settings: {{}} }}, {{ standard: one, tripCalls: 1 }}),
            truck: yourTruckText(full), hidden: hiddenSettingsText({{ corridor_miles: 25, price_policy: 'max' }}),
            nothing: derive({{ route: null, about }}),
        }}));
    """)
    # Regression: "This plan makes 1 more stops".
    assert out["one"]["extra_stops_text"] == "1 more stop" and out["one"]["stops_text"] == "1 stop"
    assert out["fewer"]["fewer_stops_text"] == "2 fewer stops" and out["fewer"]["extra_stops_text"] is None
    # "Both drivers buy the same gallons" only when they do (to each stop's rounding).
    assert out["one"]["same_gallons"] is True and out["fewer"]["same_gallons"] is False
    # "Every mile is paid for" only when it is true: not with a free full tank, nor 6.25 -> 55 gal.
    assert out["one"]["tank_same_back"] is False and out["one"]["tank_arrives_lower"] is True
    assert out["full"]["tank_same_back"] is False and out["full"]["is_your_truck"] is True
    assert out["one"]["calls_route_hit"] is True
    # Regression: "This whole trip fits in one tank" and then a stop, without a word on why.
    assert out["short"]["tank_same_back"] is True and out["short"]["start_fuel_miles"] == 50
    assert out["short"]["trip_over_range"] is False and out["short"]["fits_but_starts_low"] is True
    assert out["long"]["trip_over_range"] is True and out["long"]["fits_but_starts_low"] is False
    assert out["truck"] == "8 miles per gallon, a 50-gallon tank (400 mi), safety fuel 5.00 gal, full tank at the start, tiny stops skipped"
    assert out["full"]["your_truck_text"] == out["truck"] + ", stations up to 25 mi from the road"
    assert out["hidden"] == ["stations up to 25 mi from the road", "the highest price of each station"]
    # Tiny stops are named when the rules alone make one, never when they were merged.
    tiny = plan(fuel_stops=[{**plan()["fuel_stops"][0], "gallons": 1.9}])
    assert out["one"]["has_tiny_stops"] is False and out["full"]["has_tiny_stops"] is False
    small = run_js(f"""
        import {{ derive }} from 'JS/format.js';
        const about = {{ vehicle: {{ min_stop_gallons: 10 }} }};
        console.log(JSON.stringify(derive({{ route: {{ ok: true, body: {json.dumps(tiny)} }}, about, params: {{ settings: {{}} }} }})));
    """)
    assert small["has_tiny_stops"] is True and small["smallest_purchase"] == 1.9
    # The "For Spotter" step reads the standard truck, never the one on screen.
    assert out["withStandard"]["standard"]["stops_text"] == "1 stop"
    assert out["withStandard"]["standard"]["savings_amount"] == 1.0
    assert out["withStandard"]["trip_osrm_text"] == "1 request"
    assert out["nothing"]["ca_stations"] == 8


def test_server_time_is_said_in_seconds():
    out = run_js("""
        import { fmt } from 'JS/format.js';
        console.log(JSON.stringify([fmt.seconds(7.3), fmt.seconds(1473), fmt.seconds(250)]));
    """)
    assert out == ["under a tenth of a second", "1.5 seconds", "0.3 seconds"]


def test_play_lasts_ten_to_fifteen_seconds():
    out = run_js("""
        import { timing } from 'JS/player.js';
        const total = (miles, stops) => { const t = timing(miles, stops); return t.driving + t.perStop * stops; };
        console.log(JSON.stringify([total(30, 0), total(1090, 5), total(2810.4, 12), total(9000, 40)]));
    """)
    short, chicago, coast, huge = out
    assert short == pytest.approx(10.0) and chicago == pytest.approx(10.0)
    assert 10.0 < coast < 15.0 and huge == pytest.approx(15.0)


def test_city_suggestions_bold_what_was_typed_the_way_the_server_reads_it():
    pieces = run_js("""
        import { highlight } from 'JS/places.js';
        const show = (label, typed) => highlight(label, typed).map(([text, bold]) => (bold ? `[${text}]` : text)).join('');
        console.log(JSON.stringify([
            show('Saint Louis, MO', 'st lou'), show('Chicago, IL', 'chi, il'), show('New York City, NY', 'new york'),
            show('Cañon City, CO', 'canon c'), show("O'Fallon, MO", 'ofal'), show('Winston-Salem, NC', 'winston-s'),
        ]));
    """)
    # Regression: "st lou" (read as "saint lou") highlighted nothing.
    assert pieces == [
        "[Saint] [Lou]is, MO", "[Chi]cago, IL", "[New] [York] City, NY", "[Cañon] [C]ity, CO", "[O'Fal]lon, MO",
        "[Winston]-[S]alem, NC",
    ]
