"""Every number explains itself (``static/fuelroute/js/explain.js``).

Every value of the page has an "i" button that opens a card: what it is, where it
comes from, how it is calculated (with this trip's numbers), why it was built that
way, and where in the code. These tests keep that promise true:

* every live value of the templates, and every ``data-explain`` key of the templates
  and of the JavaScript, has an entry in ``EXPLAINERS`` (and none is unreachable);
* every entry has the five sections, and its "On this trip" lines never break or
  print ``undefined``, before a plan or with one;
* every "In the code" reference names a file and a function that exist;
* the live formulas print this trip's own numbers (fuel burned, total cost, gallons,
  savings, a stop's arithmetic).

The Node.js tests are skipped without Node, like ``test_js_logic.py``.
"""

import json
import re
from html.parser import HTMLParser

import pytest

from fuelroute import web

from .test_js_logic import NODE, plan, run_js
from .test_web import JS_DIR, PAGE, TEMPLATES, VOID, Page, js_sources

ROOT = web.STATIC_DIR.parent.parent  # the repository
EXPLAIN_JS = JS_DIR / "explain.js"
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def explainer_keys() -> set[str]:
    """The keys of EXPLAINERS, read from the source (one per line, two spaces in)."""
    source = EXPLAIN_JS.read_text(encoding="utf-8")
    block = source.split("export const EXPLAINERS = {", 1)[1].split("\n};", 1)[0]
    return set(re.findall(r"^  '([^']+)':", block, re.M))


def page_keys(client) -> tuple[set[str], set[str]]:
    """(data-live paths, data-explain keys) of the rendered page, with and without a trip."""
    live, explained = set(), set()
    for params in ({}, {"start": "New York, NY", "finish": "Los Angeles, CA", "start_tank": "full"}):
        page = Page(client.get(PAGE, params).content.decode())
        for _, attrs in page.elements:
            if "data-live" in attrs:
                live.add(attrs["data-live"])
            if "data-explain" in attrs:
                explained.add(attrs["data-explain"])
    return live, explained


def js_explain_keys() -> set[str]:
    """The data-explain keys the JavaScript draws (stop cards, checks, "What changed", Play)."""
    keys = set()
    for name, text in js_sources().items():
        if name != "explain.js":
            keys |= set(re.findall(r"'(concept:[a-z0-9-]+)'", text))
    return keys


# --- coverage ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_live_value_has_an_explainer(client):
    keys = explainer_keys()
    live, explained = page_keys(client)
    assert len(live) > 50 and len(explained) > 30
    assert sorted(live - keys) == [], "values of the page without an explanation"
    assert sorted(explained - keys) == [], "data-explain keys of the templates without an entry"
    assert sorted(js_explain_keys() - keys) == [], "data-explain keys of the JavaScript without an entry"


@needs_node
@pytest.mark.django_db
def test_every_explainer_can_be_opened_from_the_page(client):
    """No orphan card: each entry is a value, a data-explain key, or a "See also" of one."""
    graph = run_js("""
        import { EXPLAINERS } from 'JS/explain.js';
        console.log(JSON.stringify(Object.fromEntries(Object.entries(EXPLAINERS).map(([k, e]) => [k, e.see || []]))));
    """)
    # The source parser reads the same keys Node does.
    assert set(graph) == explainer_keys()
    live, explained = page_keys(client)
    reachable = set()
    todo = list((live | explained | js_explain_keys()) & set(graph))
    while todo:
        key = todo.pop()
        if key not in reachable:
            reachable.add(key)
            todo.extend(other for other in graph[key] if other in graph)
    assert sorted(set(graph) - reachable) == []
    dangling = {key: [o for o in see if o not in graph] for key, see in graph.items()}
    assert {k: v for k, v in dangling.items() if v} == {}, "See also names an entry that does not exist"


# --- every entry has its five sections ---------------------------------------------------------


def sample_answer() -> dict:
    """A 700-mile trip at 10 mpg (50-gal tank): leaves with 5 gal, three stops (one per rule),
    arrives with 5 gal, so it buys the 70 gal it burns. The stops are 0.4, 1.2 and 0.0 miles
    from the road (a straight line, one way)."""
    def decision(rule, **fields):
        return {"rule": rule, "cheaper_station": None, "consolidated": False, "reaches": None, **fields}

    stops = [
        {"stop": 1, "name": "STOP 1", "city": "A", "state": "TX", "mile_marker": 40.0, "price_per_gallon": 3.099,
         "distance_from_route_miles": 0.4, "fuel_on_arrival_gallons": 1.0, "gallons": 15.48, "cost": 47.97,
         "decision": decision("reach_cheaper", reaches={"stop": 2, "mile": 204.8}, cheaper_station={
             "name": "STOP 2", "city": "C", "state": "TX", "mile": 204.8, "price_per_gallon": 2.899})},
        {"stop": 2, "name": "STOP 2", "city": "C", "state": "TX", "mile_marker": 204.8, "price_per_gallon": 2.899,
         "distance_from_route_miles": 1.2, "fuel_on_arrival_gallons": 0.0, "gallons": 50.0, "cost": 144.95, "decision": decision("fill_up")},
        {"stop": 3, "name": "STOP 3", "city": "D", "state": "NM", "mile_marker": 650.0, "price_per_gallon": 3.2,
         "distance_from_route_miles": 0.0, "fuel_on_arrival_gallons": 5.48, "gallons": 4.52, "cost": 14.46, "decision": decision("finish")},
    ]
    body = plan(
        route__distance_miles=700.0, fuel_stops=stops,
        vehicle__miles_per_gallon=10.0, vehicle__tank_gallons=50.0, vehicle__max_range_miles=500.0,
        vehicle__usable_range_miles=500.0,
        summary__total_fuel_cost=207.38, summary__total_gallons_purchased=70.0, summary__fuel_used_gallons=70.0,
        summary__start_fuel_gallons=5.0, summary__end_fuel_gallons=5.0, summary__number_of_stops=3,
        summary__average_price_paid=2.9626, summary__candidate_stations_on_route=42,
        summary__comparison={
            "price_blind": {"total_fuel_cost": 215.0, "total_gallons_purchased": 70.0, "number_of_stops": 2,
                            "average_price_paid": 3.0714},
            "savings_vs_price_blind": {"amount": 7.62, "percent": 3.5, "extra_stops": 1},
        },
        meta__external_api_calls=1, meta__external_api_services=["osrm"], meta__plan_cache="miss", meta__route_cache="miss",
    )
    body["start"].update(lat=32.77, lon=-96.79, geocoder="offline", label="Dallas, TX", query="Dallas, TX")
    body["finish"].update(lat=35.08, lon=-106.65, geocoder="offline", label="Albuquerque, NM", query="Albuquerque, NM")
    body["route"].update(duration_hours=10.5, miles_outside_usa=0.0)
    body["summary"]["tiny_stops_merged"] = {"stops_before": 4, "extra_cost": 0.42}
    body["map"]["geojson"] = {"features": [{"geometry": {"type": "LineString", "coordinates": [[-96.8, 32.8], [-106.6, 35.1]]}}]}
    return body


ABOUT = {
    "versions": {"django": "6.1.2", "djangorestframework": "3.18.3", "python": "3.14.0"},
    "vehicle": {"miles_per_gallon": 10.0, "max_range_miles": 500.0, "tank_gallons": 50.0, "start_reserve_miles": 50.0,
                "start_reserve_gallons": 5.0, "corridor_miles": 10.0, "min_stop_gallons": 10.0, "max_consolidation_cost": 1.0},
    "data": {"price_file": "prices.csv", "stations": 6626, "price_quotes": 7531, "stations_with_several_quotes": 568,
             "geocoded": 6557, "not_geocoded": 69, "states": 48, "exact_positions": 3602,
             "geocoded_by_source": {"census": 2792, "osm_fuel": 1804, "osm_exit": 1798, "geonames": 163},
             # The server's order: by stations in the file, not by stations placed.
             "stations_by_state": [{"state": "TX", "stations": 900, "geocoded": 890}, {"state": "WY", "stations": 12, "geocoded": 2},
                                   {"state": "CA", "stations": 8, "geocoded": 8}],
             "price_per_gallon": {"min": 2.689, "median": 3.299, "max": 5.199}},
    "api": {"params": [{"name": "mpg", "default": 10.0, "min": 3.0, "max": 30.0},
                       {"name": "safety_reserve_gal", "default": 0.0, "min": 0.0, "max": None}],
            "start_tank_modes": [{"value": "empty", "label": "Almost empty"}, {"value": "full", "label": "Full"}]},
    "errors": [{"code": "no_route", "status": 422, "description": "OSRM finds no road."}],
}

CONTEXT = """
    import { derive } from 'JS/format.js';
    const body = __BODY__;
    const about = __ABOUT__;
    const derived = derive({ route: { ok: true, body }, about, params: { settings: {} }, standard: body, tripCalls: 1 });
    const ctx = { route: body, standard: body, about, client: { server_ms: 1826.4 }, derived };
"""


def context_js(body=None) -> str:
    return CONTEXT.replace("__BODY__", json.dumps(body or sample_answer())).replace("__ABOUT__", json.dumps(ABOUT))


@needs_node
def test_every_explainer_has_all_five_sections():
    out = run_js(context_js() + """
        import { EXPLAINERS, SECTIONS, ariaLabel, explain } from 'JS/explain.js';
        const cards = {};
        for (const key of Object.keys(EXPLAINERS)) {
            cards[key] = { before: explain(key, { about }), empty: explain(key, {}), after: explain(key, ctx, 2), label: ariaLabel(key) };
        }
        console.log(JSON.stringify({ sections: SECTIONS, cards }));
    """)
    assert out["sections"] == ["what", "source", "how", "why", "code"]
    with_live = 0
    for key, card in out["cards"].items():
        for when in ("empty", "before", "after"):
            data = card[when]
            assert data["title"].strip(), key
            for name in ("what", "source", "how", "why"):
                assert isinstance(data[name], str) and len(data[name]) > 15, (key, name)
            assert data["code"] and all(isinstance(ref, str) for ref in data["code"]), key
            # A live line never breaks: it is complete, or the card shows only the formula.
            assert data["error"] is None, (key, when, data["error"])
            for line in data["live"]:
                assert not re.search(r"undefined|NaN|null|\[object|—", line), (key, when, line)
        assert card["empty"]["live"] == [], key  # no trip, no "On this trip"
        with_live += bool(card["after"]["live"])
        # One neutral label for every button: many values are read, not calculated.
        assert card["label"] == f"Explain: {card['after']['title']}", key
    # With a trip, almost every card shows this trip's numbers.
    assert with_live >= 0.75 * len(out["cards"]), with_live


def test_every_code_reference_points_to_real_code():
    """"In the code" must lead somewhere: the file exists and names the function."""
    source = EXPLAIN_JS.read_text(encoding="utf-8")
    refs = set(re.findall(r"'((?:fuelroute|config|scripts)/[^'\s]+)'", source))
    assert len(refs) > 100
    missing = []
    for ref in sorted(refs):
        path, _, symbol = ref.partition(":")
        file = ROOT / path
        if not file.is_file():
            missing.append(f"{ref}: no such file")
            continue
        name = symbol.rsplit(".", 1)[-1]
        if name and not re.search(rf"\b{re.escape(name)}\b", file.read_text(encoding="utf-8")):
            missing.append(f"{ref}: {name} is not in {path}")
    assert missing == []


# --- the live formulas use this trip's numbers -------------------------------------------------


@needs_node
def test_live_formulas_use_this_trips_numbers():
    out = run_js(context_js() + """
        import { explain } from 'JS/explain.js';
        const live = (key, arg = null, c = ctx) => explain(key, c, arg).live;
        console.log(JSON.stringify({
            fuel: live('route.summary.fuel_used_gallons'),
            total: live('route.summary.total_fuel_cost'),
            gallons: live('route.summary.total_gallons_purchased'),
            savings: live('derived.savings_amount'),
            standardTotal: live('standard.summary.total_fuel_cost', null, { about, standard: body, derived: { standard: derived } }),
            standardSavings: live('derived.standard.savings_amount', null, { about, standard: body, derived: { standard: derived } }),
            stop1: live('concept:stop-card', '1'),
            stop2: live('concept:stop-card', '2'),
            stop3: live('concept:stop-card', '3'),
            range: live('route.vehicle.usable_range_miles'),
            average: live('route.summary.average_price_paid'),
            before: explain('route.summary.fuel_used_gallons', { about }),
        }));
    """)
    # Gallons: distance ÷ miles per gallon, with this trip's numbers.
    assert out["fuel"] == ["700.0 mi ÷ 10 mpg = 70.00 gal"]
    # Total cost: the stop costs, added in cents, equal the API's total.
    assert out["total"] == [
        "$47.97 + $144.95 + $14.46 = $207.38 (3 stops).",
        "The API’s total is $207.38: the same, to the cent.",
        "For example stop 1: 15.48 gal × $3.099 = $47.9725 → $47.97.",
        "Not in the total, the detours to the stops (straight-line estimate): 2 × (0.4 + 1.2 + 0.0) mi = 3.2 mi there"
        " and back ÷ 10 mpg ≈ 0.32 gal ≈ $0.94 at each stop’s price.",
    ]
    assert out["gallons"] == [
        "15.48 + 50 + 4.52 = 70.00 gal.",
        "Check: burned 70.00 gal + arrives with 5.00 gal − leaves with 5.00 gal = 70.00 gal.",
    ]
    # Savings: the driver who ignores prices minus this plan, and the percent of the driver's total.
    assert out["savings"] == [
        "$215.00 (driver who ignores prices) − $207.38 (this plan) = $7.62.",
        "$7.62 ÷ $215.00 × 100 = 3.5%.",
    ]
    # The For Spotter step reads the standard truck's answer with the same formulas.
    assert out["standardTotal"] == out["total"] and out["standardSavings"] == out["savings"]
    # A stop, line by line: arrival from the previous stop, the purchase by its rule, the cost.
    assert out["stop1"][1] == "Arrives with: 5.00 gal when leaving the start − (40.0 − 0.0) miles ÷ 10 mpg = 1.00 gal."
    assert out["stop1"][2] == (
        "Buys (rule “Cheaper ahead”): C, TX sells at $2.899 vs $3.099 here. Getting there takes (204.8 − 40.0) ÷ 10 mpg"
        " = 16.48 gal; the tank has 1.00 gal, so it buys 16.48 gal − 1.00 gal = 15.48 gal.")
    assert out["stop2"] == [
        "Stop 2: STOP 2, C, TX, at mile 204.8, $2.899 a gallon.",
        "Arrives with: 16.48 gal when leaving stop 1 − (204.8 − 40.0) miles ÷ 10 mpg = 0.00 gal.",
        "Buys (rule “Fill up”): a full tank 50.00 gal − the 0.00 gal it has = 50.00 gal.",
        "Cost: 50.00 gal × $2.899 = $144.95, rounded to the cent: $144.95.",
        "Leaves with 0.00 gal + 50.00 gal = 50.00 gal.",
        "Detour: ≈ 1.2 mi off the road → about 2.4 mi there and back ≈ 0.24 gal at this truck’s 10 mpg ≈ $0.70"
        " at this stop’s price, not in the total (straight-line estimate).",
    ]
    assert out["stop3"][2] == ("Buys (rule “Finish”): the rest of the road takes (700.0 − 650.0) ÷ 10 mpg = 5.00 gal,"
                               " plus the 5.00 gal it must arrive with, minus the 5.48 gal it has: 4.52 gal.")
    assert out["range"] == ["500 mi − 0.00 gal × 10 mpg = 500 mi."]
    assert out["average"][0] == "$207.38 ÷ 70.00 gal = $2.9626 a gallon (the API: $2.9626)."
    # Before a plan the card shows the general formula only.
    assert out["before"]["live"] == [] and out["before"]["how"].startswith("fuel burned = distance ÷ miles per gallon")


@needs_node
def test_a_hand_check_that_disagrees_with_the_api_says_so():
    """Rounding is named, not hidden: a stop whose arrival the API rounded differently."""
    body = sample_answer()
    body["fuel_stops"][1]["fuel_on_arrival_gallons"] = 0.03
    out = run_js(context_js(body) + """
        import { explain } from 'JS/explain.js';
        console.log(JSON.stringify(explain('concept:stop-card', ctx, '2').live[1]));
    """)
    assert out.endswith("= 0.00 gal (the API says 0.03 gal: miles and gallons are rounded).")


@needs_node
def test_detour_estimates_print_this_trips_numbers():
    """The plan does not charge the detour to a stop; the cards estimate it: there and back
    = 2 × distance_from_route_miles, ÷ the plan's mpg, × the stop's own price."""
    thirsty = sample_answer()
    thirsty["vehicle"]["miles_per_gallon"] = 8.0
    no_distance = sample_answer()
    del no_distance["fuel_stops"][0]["distance_from_route_miles"]
    out = run_js(context_js() + f"""
        import {{ explain }} from 'JS/explain.js';
        const withBody = (b) => ({{ ...ctx, route: b, derived: derive({{ route: {{ ok: true, body: b }}, about, params: {{ settings: {{}} }}, standard: b }}) }});
        const live = (key, c = ctx, arg = null) => explain(key, c, arg).live;
        const last = (lines) => lines[lines.length - 1];
        console.log(JSON.stringify({{
            stop1: last(live('concept:stop-card', ctx, '1')),
            stop3: last(live('concept:stop-card', ctx, '3')),
            thirsty: last(live('concept:stop-card', withBody({json.dumps(thirsty)}), '2')),
            total: last(live('route.summary.total_fuel_cost')),
            blind: live('concept:price-blind-driver').slice(2),
            estimate: live('concept:detour-estimate'),
            exact: live('concept:exact-positions'),
            exactCount: live('about.data.exact_positions'),
            missingStop: live('concept:stop-card', withBody({json.dumps(no_distance)}), '1'),
            missingTotal: live('route.summary.total_fuel_cost', withBody({json.dumps(no_distance)})),
            missingEstimate: live('concept:detour-estimate', withBody({json.dumps(no_distance)})),
        }}));
    """)
    # One stop: 0.4 mi off the road → 0.8 mi there and back ÷ 10 mpg = 0.08 gal × $3.099 = $0.248.
    assert out["stop1"] == ("Detour: ≈ 0.4 mi off the road → about 0.8 mi there and back ≈ 0.08 gal at this truck’s"
                            " 10 mpg ≈ $0.25 at this stop’s price, not in the total (straight-line estimate).")
    assert out["stop3"] == "Detour: 0.0 mi off the road, so nothing to add."
    # The truck's own mpg: 2.4 mi ÷ 8 mpg = 0.30 gal × $2.899 = $0.87.
    assert out["thirsty"] == ("Detour: ≈ 1.2 mi off the road → about 2.4 mi there and back ≈ 0.30 gal at this truck’s"
                              " 8 mpg ≈ $0.87 at this stop’s price, not in the total (straight-line estimate).")
    # The trip: $0.248 + $0.696 + $0 = $0.94, each stop at its own price.
    trip = ("Not in the total, the detours to the stops (straight-line estimate): 2 × (0.4 + 1.2 + 0.0) mi = 3.2 mi"
            " there and back ÷ 10 mpg ≈ 0.32 gal ≈ $0.94 at each stop’s price.")
    assert out["total"] == trip
    # The answer has no distances for the other driver's stops: only this plan's figure.
    assert out["blind"] == [
        "Not in either total, this plan’s detours to its stops: ≈ 3.2 mi there and back ≈ 0.32 gal ≈ $0.94"
        " (straight-line estimate).",
        "The answer does not say how far the other driver’s stops are from the road, so its detours are not estimated.",
    ]
    assert out["estimate"] == [trip, "Farthest from the road: stop 2, C, TX. Detour: ≈ 1.2 mi off the road → about"
                               " 2.4 mi there and back ≈ 0.24 gal at this truck’s 10 mpg ≈ $0.70 at this stop’s price,"
                               " not in the total (straight-line estimate)."]
    # Exact positions: this trip's stops under a mile from the road first, then the file's counts.
    assert out["exact"] == [
        "Stops less than a mile from the road on this trip (straight line): 2 of 3.",
        "In the whole price file: 3,602 of 6,626 US truck stops at their exact place; the others at their city center,"
        " or left out.",
    ]
    assert out["exactCount"] == [
        "3,602 of 6,626 US truck stops: 3,602 ÷ 6,626 × 100 = 54.4%.",
        "1,804 at their own fuel station, 1,798 at the exit in their address.",
        "2,955 at their city center, 69 left out.",
    ]
    # A stop without its distance: no detour line, and no trip estimate (never a partial sum).
    assert not any("Detour" in line for line in out["missingStop"])
    assert len(out["missingTotal"]) == 3 and out["missingEstimate"] == []



# --- the cards stay short and plain -----------------------------------------------------------


# The most each section may say: (sentences, words). A card is read on a phone and on a
# video: three short sentences per section, and two for "Where it comes from".
LIMITS = {"what": (3, 50), "how": (3, 50), "why": (3, 50), "edge": (3, 50), "source": (2, 50)}
CONSTANT = re.compile(r"\b_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def all_cards() -> dict:
    """Every entry as the page sees it (the standard truck's ones included), without a trip."""
    return run_js("""
        import { EXPLAINERS } from 'JS/explain.js';
        const pick = ({ title, what, source, how, why, edge }) => ({ title, what, source, how, why, edge: edge || null });
        console.log(JSON.stringify(Object.fromEntries(Object.entries(EXPLAINERS).map(([k, e]) => [k, pick(e)]))));
    """)


@needs_node
def test_explanations_stay_short():
    too_long = []
    for key, card in all_cards().items():
        for name, (most_sentences, most_words) in LIMITS.items():
            text = card[name] or ""
            sentences, words = len(re.findall(r"[.!?](?:\s|$)", text)), len(text.split())
            if sentences > most_sentences or words > most_words:
                too_long.append(f"{key}.{name}: {sentences} sentences, {words} words")
    assert too_long == []


@needs_node
def test_a_constant_name_comes_after_its_value_in_words():
    """“kept one hour by default (PLAN_CACHE_SECONDS)”, never a bare PLAN_CACHE_SECONDS: a
    newcomer reads the value, a developer finds the name."""
    bare = []
    for key, card in all_cards().items():
        for name in ("what", "how", "why"):
            text = card[name]
            for match in CONSTANT.finditer(text):
                before = text[: match.start()]
                if before.rfind("(") <= before.rfind(")"):  # not inside parentheses
                    bare.append(f"{key}.{name}: {match.group()}")
    assert bare == []


def test_every_how_cell_of_the_brief_has_an_explainer():
    template = (TEMPLATES / "partials" / "_step_assignment.html").read_text(encoding="utf-8")
    cells = re.findall(r'<td data-label="How">(.*?)</td>', template, re.S)
    assert len(cells) >= 9
    assert [cell.split(".")[0] for cell in cells if 'data-explain="' not in cell] == []


# --- where the buttons go ----------------------------------------------------------------------


class Ancestry(HTMLParser):
    """Each [data-live] / [data-explain] element of a page with its open ancestors."""

    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.found: list[tuple[str, dict, list[tuple[str, dict]]]] = []
        self.referenced: set[str] = set()
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in ("aria-labelledby", "aria-describedby"):
            self.referenced.update((attrs.get(name) or "").split())
        if "data-live" in attrs or "data-explain" in attrs:
            self.found.append((tag, attrs, list(self.stack)))
        if tag not in VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


@pytest.mark.django_db
def test_buttons_never_join_a_name_or_an_announcement(client):
    """An "i" button inside a label, a row header, a legend, an element that names or
    describes another one, or a live region would be read out with it ("Tank size,
    Explain: Range on a full tank"). The button goes inside a data-explain element and
    right after a value, so neither may sit in one of those."""
    wrong = []
    for params in ({}, {"start": "New York, NY", "finish": "Los Angeles, CA", "start_tank": "full"}):
        page = Ancestry(client.get(PAGE, params).content.decode())
        for tag, attrs, ancestors in page.found:
            host = "data-explain" in attrs
            if not host and "data-no-explain" in attrs:
                continue
            holders = ancestors + ([(tag, attrs)] if host else [])
            fieldset = None
            for holder_tag, holder in holders:
                fieldset = holder if holder_tag == "fieldset" else fieldset
                if (
                    holder.get("id") in page.referenced
                    or holder.get("role") in ("status", "alert")
                    or "aria-live" in holder
                    or holder_tag in ("label", "th")
                    or (holder_tag == "legend" and not (fieldset or {}).get("aria-labelledby"))
                ):
                    wrong.append(f"{attrs.get('data-explain') or attrs.get('data-live')} in <{holder_tag} {holder}>")
    assert wrong == []


@needs_node
def test_where_the_buttons_go():
    out = run_js("""
        import { planButtons } from 'JS/explain-ui.js';
        const known = (key) => ['a', 'b', 'concept:x'].includes(key);
        const item = (fields) => ({ live: null, explain: null, noExplain: false, done: false, block: 'p1', arg: null, ...fields });
        const plan = (items) => planButtons(items, known);
        console.log(JSON.stringify({
            oncePerBlock: plan([item({ live: 'a' }), item({ live: 'a' }), item({ live: 'a', block: 'p2' }), item({ live: 'b' })]),
            done: plan([item({ live: 'a', done: true }), item({ live: 'b' })]),
            noExplain: plan([item({ live: 'a', noExplain: true }), item({ explain: 'a', noExplain: true })]),
            unknown: plan([item({ live: 'zzz' }), item({ explain: 'concept:nope' })]),
            host: plan([item({ explain: 'concept:x', arg: '2' }), item({ live: 'concept:x' })]),
            real: planButtons([item({ live: 'route.summary.total_fuel_cost' }), item({ live: 'route.nope' })]),
        }));
    """)
    after = lambda i, key, arg=None: {"index": i, "key": key, "arg": arg, "where": "after"}  # noqa: E731
    # One button per key per block: a value repeated in a paragraph gets one.
    assert out["oncePerBlock"] == [after(0, "a"), after(2, "a"), after(3, "b")]
    # An element already done (decorate ran before) is never marked twice.
    assert out["done"] == [after(1, "b")]
    # data-no-explain skips a value, not a data-explain placeholder.
    assert out["noExplain"] == [{"index": 1, "key": "a", "arg": None, "where": "inside"}]
    assert out["unknown"] == []
    # A placeholder gets its button inside, with its argument (a stop number); the same
    # key in the same block gets no second one.
    assert out["host"] == [{"index": 0, "key": "concept:x", "arg": "2", "where": "inside"}]
    # By default the keys are those of EXPLAINERS.
    assert out["real"] == [after(0, "route.summary.total_fuel_cost")]


# --- the live lines say what the code does -------------------------------------------------------


@needs_node
def test_live_lines_follow_the_code_in_the_cases_a_review_found():
    with_safety = sample_answer()
    with_safety["vehicle"].update(safety_reserve_gal=5.0, usable_range_miles=450.0)
    with_safety["summary"]["start_fuel_gallons"] = 5.5
    lowered = sample_answer()
    lowered["summary"]["start_fuel_gallons"] = 4.0
    thirsty = sample_answer()
    thirsty["vehicle"]["miles_per_gallon"] = 8.0
    one_stop = sample_answer()
    one_stop["fuel_stops"] = one_stop["fuel_stops"][2:]
    one_stop["summary"].update(number_of_stops=1, candidate_stations_on_route=1)
    out = run_js(context_js() + f"""
        import {{ explain }} from 'JS/explain.js';
        const withBody = (b) => ({{ ...ctx, route: b, derived: derive({{ route: {{ ok: true, body: b }}, about, params: {{ settings: {{}} }}, standard: b }}) }});
        const live = (key, c = ctx, arg = null) => explain(key, c, arg).live;
        console.log(JSON.stringify({{
            reserve: live('route.summary.start_fuel_gallons'),
            safety: live('route.summary.start_fuel_gallons', withBody({json.dumps(with_safety)})),
            lowered: live('route.summary.start_fuel_gallons', withBody({json.dumps(lowered)})),
            safetyStop: live('concept:stop-card', withBody({json.dumps(with_safety)}), '1')[2],
            http500: live('concept:trip-error-box', ctx, 'http_500'),
            network: live('concept:trip-error-box', ctx, 'network')[0],
            timeout: live('concept:trip-error-box', ctx, 'timeout')[0],
            known: live('concept:trip-error-box', ctx, 'no_route')[0],
            limits: live('concept:defaults-and-limits'),
            states: live('about.data.states'),
            minStop: live('derived.min_stop', withBody({json.dumps(thirsty)})),
            oneStop: live('route.summary.number_of_stops', withBody({json.dumps(one_stop)})),
            pipeline: live('concept:pipeline', withBody({json.dumps(one_stop)})),
        }}));
    """)
    # Start fuel: the larger of the reserve and (first truck stop + safety fuel).
    assert out["reserve"][1] == "That is the start reserve (50 mi)."
    assert out["safety"][:2] == [
        "5.50 gal × 10 mpg = 55 mi of driving.",
        "More than the 50 mi start reserve: enough to reach the first truck stop near the road with the safety fuel"
        " still in the tank (first stop + 50 mi of safety fuel = 55 mi).",
    ]
    assert out["lowered"][1].startswith("Less than the 50 mi start reserve: the last truck stop is far from the finish")
    assert "above the safety fuel" in out["safetyStop"]
    # Only "network" and "timeout" come from the browser; http_500 is the server's.
    assert out["http500"][0] == ("http_500: the server answered HTTP 500 without one of the API’s error codes"
                                 " (for example a crash page or a proxy).")
    assert out["network"].startswith("network: the browser could not reach the server")
    assert out["timeout"].startswith("timeout: the browser gave up waiting")
    assert out["known"] == "no_route (HTTP 422): OSRM finds no road."
    # safety_reserve_gal has a limit: the tank of the truck.
    assert "safety_reserve_gal: 0 to less than the tank (500 mi ÷ 10 mpg = 50.00 gal), default 0." in out["limits"]
    # The fewest PLACED truck stops, whatever the server's order (by stations in the file).
    assert out["states"] == ["48 states.", "Fewest placed truck stops: WY 2, CA 8, TX 890."]
    # The optimizer converts the minimum with the plan's miles per gallon.
    assert out["minStop"] == ["10.00 gal × 8 mpg = 80 mi of driving."]
    # Never "1 stops".
    assert out["oneStop"][0].startswith("1 stop in fuel_stops.")
    assert "1 truck stop near the road, 1 stop." in out["pipeline"][0]
