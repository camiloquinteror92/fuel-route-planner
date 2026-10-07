"""The page's own logic, run in Node.js: what the browser computes from an API answer.

The page draws only what the API answers, but some sentences are arithmetic on it:
the "Why here" of every stop, the comparison with the standard truck, the derived
values of the template, how long "Play" lasts and the bold part of a city
suggestion. These tests import the real ES modules of
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
            "start_tank": "empty", "safety_reserve_gal": 0.0, "price_policy": "median", "consolidate": True,
        },
        "summary": {
            "total_fuel_cost": 183.75, "total_gallons_purchased": 61.25, "fuel_used_gallons": 12.5,
            "start_fuel_gallons": 6.25, "end_fuel_gallons": 55.0, "unpriced_fuel_gallons": 0.0, "number_of_stops": 1,
            "average_price_paid": 3.0, "candidate_stations_on_route": 3, "comparison": None,
        },
        "fuel_stops": [
            {"stop": 1, "city": "C", "state": "TX", "mile_marker": 40.0, "price_per_gallon": 3.0,
             "fuel_on_arrival_gallons": 1.25, "gallons": 61.25, "cost": 183.75},
        ],
        "meta": {"external_api_calls": 0, "external_api_services": [], "plan_cache": "miss", "route_cache": "hit", "settings_changed": []},
        "pipeline": {"corridor": {"corridor_miles": 10.0, "candidates": 3, "price_per_gallon": {"min": 2.9, "max": 3.6}}},
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


def stop(n, mile, price, decision):
    return {"stop": n, "city": "C", "state": "TX", "mile_marker": mile, "price_per_gallon": price,
            "fuel_on_arrival_gallons": 0.0, "gallons": 10.0, "cost": 30.0, "decision": decision}


def decision(rule, **fields):
    base = {"rule": rule, "cheaper_station_mile": None, "consolidated": False, "greedy_gallons": 10.0, "moved_in": [],
            "moved_out": [], "consolidation_extra_cost": 0.0, "reaches": None, "fills_tank": rule == "fill_up"}
    return {**base, **fields}


def test_why_text_is_plain_and_uses_the_answers_numbers():
    stops = [
        # Rule 1, the cheaper station is the next stop: its price is named.
        stop(1, 10.0, 3.099, decision("reach_cheaper", cheaper_station_mile=70.0, reaches={"stop": 2, "mile": 70.0})),
        stop(2, 70.0, 3.009, decision("reach_cheaper", cheaper_station_mile=300.0, reaches={"stop": 3, "mile": 250.0})),
        stop(3, 250.0, 2.9, decision("fill_up", reaches={"stop": 4, "mile": 700.0})),
        stop(4, 700.0, 3.2, decision("finish", reaches={"stop": None, "mile": 900.0})),
        # Merged stops: the real stop 1 of New York -> Los Angeles.
        stop(5, 10.0, 3.099, decision(
            "reach_cheaper", cheaper_station_mile=70.0, consolidated=True, greedy_gallons=2.0,
            moved_in=[{"mile": 70.0, "gallons": 32.6, "from_stop": None}], consolidation_extra_cost=0.65,
            reaches={"stop": 2, "mile": 396.0})),
        stop(6, 1103.0, 2.959, decision(
            "reach_cheaper", cheaper_station_mile=1252.0, consolidated=True, greedy_gallons=14.9,
            moved_in=[{"mile": 1252.0, "gallons": 1.2, "from_stop": None}, {"mile": 1264.0, "gallons": 7.6, "from_stop": None}],
            consolidation_extra_cost=0.33)),
        stop(7, 500.0, 2.9, decision(
            "fill_up", consolidated=True, moved_in=[{"mile": 640.0, "gallons": 3.0, "from_stop": 8}], consolidation_extra_cost=0.2)),
        stop(8, 640.0, 3.0, decision(
            "finish", consolidated=True, moved_out=[{"mile": 500.0, "gallons": 3.0, "to_stop": 7}], consolidation_extra_cost=0.004)),
        stop(9, 800.0, 3.0, decision("fill_up", consolidated=True, consolidation_extra_cost=0.0)),
    ]
    body = plan(fuel_stops=stops, vehicle__usable_range_miles=450.0)
    why = run_js(f"""
        import {{ whyText }} from 'JS/plan.js';
        const body = {json.dumps(body)};
        const about = {{ vehicle: {{ min_stop_gallons: 10 }} }};
        console.log(JSON.stringify(body.fuel_stops.map((s) => whyText(s, body, about))));
    """)
    assert why == [
        "Fuel is cheaper at stop 2 ($3.009 vs $3.099 here). Buy just enough to get there.",
        "Fuel is cheaper at mile 300.0. Buy just enough to get there.",
        "Nothing cheaper within 450 mi. Fill the tank.",
        "No cheaper fuel before the finish. Buy just enough to finish.",
        "Fuel is cheaper at mile 70.0, so the rule alone buys only 2.00 gal here. To avoid a tiny stop, it also buys"
        " here the 32.60 gal planned at mile 70.0, and skips that stop. Cost of this change: +$0.65.",
        "Fuel is cheaper at mile 1,252.0, so the rule alone buys only 14.90 gal here. To avoid tiny stops, it also buys"
        " here the fuel planned at miles 1,252.0 and 1,264.0 (8.80 gal), and skips those stops. Cost of this change: +$0.33.",
        "Nothing cheaper within 450 mi, so the rule fills the tank. To avoid a tiny stop, it also buys here 3.00 gal"
        " that stop 8 would have bought. Cost of this change: +$0.20.",
        # Less than half a cent is not a cost worth naming.
        "No cheaper fuel before the finish, so the rule buys just enough to finish. To avoid a tiny stop, 3.00 gal of it"
        " is bought at stop 7 instead.",
        "Nothing cheaper within 450 mi, so the rule fills the tank. Adjusted so no stop buys less than 10.00 gal.",
    ]
    for text in why:
        assert "greedy" not in text.lower() and "consolidat" not in text.lower(), text


def test_compare_is_honest_with_a_full_tank():
    out = run_js(f"""
        import {{ answerText, compareRows, quickTrySettings, QUICK_TRIES }} from 'JS/whatif.js';
        const base = {json.dumps(plan())};
        const full = {json.dumps(plan(
            vehicle__start_tank="full", summary__total_fuel_cost=150.0, summary__average_price_paid=2.9,
            meta__settings_changed=["start_tank"],
        ))};
        const thirsty = {json.dumps(plan(summary__total_fuel_cost=200.0, summary__total_gallons_purchased=66.0,
                                         meta__settings_changed=["mpg", "max_range_miles"]))};
        const defaults = {{ mpg: 10, max_range_miles: 500, corridor_miles: 10, safety_reserve_gal: 0, price_policy: 'median', consolidate: true }};
        console.log(JSON.stringify({{
            full: compareRows(full, base), fullText: answerText(full, base),
            thirsty: compareRows(thirsty, base), thirstyText: answerText(thirsty, base),
            tries: QUICK_TRIES.map((t) => quickTrySettings(t, defaults)),
        }}));
    """)
    # Regression: "−$33.75" read as a saving; the free first tank is not comparable.
    assert out["full"]["notComparable"] is True
    assert [row["diff"] for row in out["full"]["rows"]] == ["not comparable", "same", "not comparable"]
    assert out["full"]["perGallon"] == "Price per gallon: $2.900 (yours) vs $3.000 (standard)."
    assert "$33.75" not in out["fullText"] and out["fullText"].endswith(out["full"]["perGallon"])
    # A comparable change says the difference in dollars, stops and gallons.
    assert [row["diff"] for row in out["thirsty"]["rows"]] == ["+$16.25", "same", "+4.75 gal"]
    assert out["thirstyText"] == "Your truck: $200.00, 1 stop: +$16.25 against the standard truck."
    # "8 mpg" keeps the 50-gal tank of the standard truck: 400 miles on a full tank.
    assert out["tries"] == [
        {"start_tank": "empty", "settings": {"mpg": 8, "max_range_miles": 400}},
        {"start_tank": "empty", "settings": {"safety_reserve_gal": 5}},
        {"start_tank": "full", "settings": {}},
    ]


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
            "price_blind": {"total_fuel_cost": 184.75, "number_of_stops": 0}}))};
        const fewer = {json.dumps(plan(summary__comparison={
            "savings_vs_price_blind": {"amount": 1.0, "percent": 0.5, "extra_stops": -2},
            "price_blind": {"total_fuel_cost": 184.75, "number_of_stops": 3}}))};
        const full = {json.dumps(plan(vehicle__start_tank="full", vehicle__safety_reserve_gal=5.0, vehicle__miles_per_gallon=8,
                                       vehicle__max_range_miles=400.0, summary__start_fuel_gallons=50.0))};
        const d = (body, params = {{ settings: {{}} }}) => derive({{ route: {{ ok: true, body }}, about: {{}}, params }});
        console.log(JSON.stringify({{
            one: d(one), fewer: d(fewer), full: d(full, {{ start_tank: 'full', settings: {{ corridor_miles: 25 }} }}),
            truck: yourTruckText(full), hidden: hiddenSettingsText({{ corridor_miles: 25, price_policy: 'max', consolidate: false }}),
        }}));
    """)
    # Regression: "This plan makes 1 more stops".
    assert out["one"]["extra_stops_text"] == "1 more stop" and out["one"]["stops_text"] == "1 stop"
    assert out["fewer"]["fewer_stops_text"] == "2 fewer stops" and out["fewer"]["extra_stops_text"] is None
    # "Every mile is paid for" only when it is true: not with a free full tank.
    assert out["one"]["pays_every_mile"] is False  # leaves with 6.25, arrives with 55
    assert out["full"]["pays_every_mile"] is False and out["full"]["is_your_truck"] is True
    assert out["one"]["calls_route_hit"] is True and out["one"]["calls_text"] == "0 requests"
    assert out["truck"] == "8 miles per gallon, 400 mi on a full tank, safety fuel 5.00 gal, full tank at the start"
    assert out["full"]["your_truck_text"] == out["truck"] + ", stations up to 25 mi from the road"
    assert out["hidden"] == ["stations up to 25 mi from the road", "the highest price of each station", "tiny stops kept"]


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
