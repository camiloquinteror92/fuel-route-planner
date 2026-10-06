"""The page's own logic, run in Node.js: what the browser computes from an API answer.

The page draws only what the API answers, but some sentences are arithmetic on it:
the contract checks, the "what if" answers, the derived values of the template and
the bold part of a city suggestion. These tests import the real ES modules of
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
            "miles_per_gallon": 8.0, "max_range_miles": 500.0, "tank_gallons": 62.5, "start_tank": "empty",
            "safety_reserve_gal": 0.0, "price_policy": "median",
        },
        "summary": {
            "total_fuel_cost": 183.75, "total_gallons_purchased": 61.25, "fuel_used_gallons": 12.5,
            "start_fuel_gallons": 6.25, "end_fuel_gallons": 55.0, "unpriced_fuel_gallons": 0.0, "number_of_stops": 1,
            "average_price_paid": 3.0, "candidate_stations_on_route": 3, "comparison": None,
        },
        "fuel_stops": [
            {"stop": 1, "mile_marker": 40.0, "distance_from_route_miles": 2.0, "price_per_gallon": 3.0,
             "fuel_on_arrival_gallons": 1.25, "gallons": 61.25, "cost": 183.75},
        ],
        "meta": {"external_api_calls": 0, "external_api_services": [], "plan_cache": "miss", "settings_changed": []},
        "pipeline": {"corridor": {"corridor_miles": 10.0, "candidates": 3, "stations_with_several_prices": 1}},
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


def test_contract_checks_see_the_safety_reserve_and_a_full_tank_without_slack():
    checks = run_js(f"""
        import {{ runChecks }} from 'JS/checks.js';
        const ok = {json.dumps(plan())};
        const reserve = {json.dumps(plan(vehicle__safety_reserve_gal=5.0))};
        const over = {json.dumps(plan(fuel_stops=[{**plan()["fuel_stops"][0], "fuel_on_arrival_gallons": 1.28}]))};
        const byId = (body) => Object.fromEntries(runChecks(body, {{}}).map((c) => [c.id, c.ok]));
        console.log(JSON.stringify({{ ok: byId(ok), reserve: byId(reserve), over: byId(over) }}));
    """)
    # Arrives with 1.25 and buys 61.25: exactly full, within the tank with no rounding slack.
    assert checks["ok"]["tank_bounds"] is True and checks["ok"]["safety_reserve"] is None
    # Regression: the checks did not look at the safety reserve; arriving with 1.25 < 5 fails.
    assert checks["reserve"]["safety_reserve"] is False
    # Regression: "62.53 gal (capacity 62.50) within the tank" passed on a 0.05 slack.
    assert checks["over"]["tank_bounds"] is False


def test_derived_values_name_the_full_tank_and_agree_in_number():
    derived = run_js(f"""
        import {{ derive }} from 'JS/format.js';
        import {{ planTrace, priceBlindTrace, summarize }} from 'JS/checks.js';
        const full = {json.dumps(plan(
            vehicle__start_tank="full", vehicle__safety_reserve_gal=5.0, summary__start_fuel_gallons=62.5,
            summary__unpriced_fuel_gallons=57.5,
            summary__comparison={"savings_vs_price_blind": {"amount": 1.0, "percent": 0.5, "extra_stops": 1},
                                 "price_blind": {"total_fuel_cost": 2.0, "number_of_stops": 0}},
        ))};
        const state = {{ route: {{ ok: true, body: full, headers: {{}} }}, about: {{}}, session: [], checks: [] }};
        const d = derive(state, {{ planTrace, priceBlindTrace, summarize }});
        console.log(JSON.stringify({{ tank: d.full_tank_gallons, extra: d.extra_stops_text, detour: d.detour_approx, cost: d.detour_cost }}));
    """)
    # Regression: "the 57.50 gal in the tank are not counted" in a 62.5-gal tank.
    assert derived["tank"] == 62.5
    # Regression: "This plan makes 1 more stops".
    assert derived["extra"] == "1 more stop"
    # The detour, there and back, priced at what the plan paid: 4 mi / 8 mpg x $3.
    assert derived["detour"] == 4.0 and derived["cost"] == pytest.approx(1.5)


def test_what_if_answers_compare_what_is_comparable():
    answers = run_js(f"""
        import {{ answerText, questionSettings, QUESTIONS }} from 'JS/whatif.js';
        const base = {json.dumps(plan())};
        const full = {json.dumps(plan(
            vehicle__start_tank="full", summary__total_fuel_cost=150.0, summary__average_price_paid=2.9,
            meta__settings_changed=["start_tank"],
        ))};
        const wide = {json.dumps(plan(
            summary__total_fuel_cost=182.75, meta__settings_changed=["corridor_miles"],
            fuel_stops=[{**plan()["fuel_stops"][0], "distance_from_route_miles": 12.0}],
        ))};
        const mpg = QUESTIONS.find((q) => q.id === 'mpg');
        console.log(JSON.stringify({{
            full: answerText(full, base), wide: answerText(wide, base),
            mpg: questionSettings(mpg, {{ mpg: 10, max_range_miles: 500 }}),
        }}));
    """)
    # Regression: "−$33.75 vs the defaults" read as a saving; the free tank is not comparable.
    assert "vs the defaults" not in answers["full"] and "per gallon it paid $2.900 vs $3.000" in answers["full"]
    # The wider corridor saves $1.00 but drives 20 more miles to the stations: priced, it costs more.
    assert "−$1.00 vs the defaults" in answers["wide"]
    assert "detours of about 24.0 mi there and back vs 4.0 mi" in answers["wide"]
    assert "with that fuel counted, +$6.50 vs the defaults" in answers["wide"]
    # "8 mpg" keeps the 50-gal tank of the defaults: 400 miles of range.
    assert answers["mpg"] == {"mpg": 8, "max_range_miles": 400}


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
