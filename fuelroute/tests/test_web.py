"""The planner page (``fuelroute/web.py``): a shell that never plans, its static
files, and the guards behind its rules.

* Rendering the page costs 0 external calls; the browser calls ``/api/route`` with
  exactly the parameters a Postman request carries.
* The page is a guided tour in six steps (Trip, Route, Fuel stops, Cost, Truck,
  For Spotter), accessible, in plain words.
* CSS and ES modules are served with the right types (DEBUG off, no collectstatic).
* "No number is written by hand": visible text has no digits unless it is a live
  value (``data-live``) or a fixed literal (``data-literal``), and the JavaScript
  has no hard-coded measurements.
* The page and the API agree: every live path and format exists, the Truck step's
  settings and start-tank labels are the API's, and every quick try is planned on
  the saved road with 0 external calls.
"""

import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import django
import pytest

from fuelroute import web
from fuelroute.serializers import WHAT_IF_PARAMS
from fuelroute.services.about import build_about

from .test_api import FINISH, OK_ROUTE, START, get, stations  # noqa: F401  (stations is a fixture)

PAGE = "/api/route/map"
JS_DIR = web.STATIC_DIR / "fuelroute" / "js"
TEMPLATES = Path(web.__file__).resolve().parent / "templates" / "fuelroute"
ALLOWED_ROOTS = {"route", "standard", "about", "client", "derived"}
STEPS = ["trip", "route", "stops", "cost", "truck", "assignment"]
# Number boxes (with a slider) of the Truck step; the tank sets max_range_miles (tank x mpg).
NUMBER_CONTROLS = {"mpg", "tank_gal", "safety_reserve_gal"}
# The API settings the Truck step controls.
CONTROLLED = {"mpg", "max_range_miles", "safety_reserve_gal", "consolidate"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
JARGON = (
    "greedy", "consolidat", "corridor", "cache", "pipeline", "OPIS", "GeoJSON", "polyline", "price-blind", "what-if",
    "Server-Timing", "p95", "baseline", "optimum",
)


class Page(HTMLParser):
    """Elements (tag, attrs), visible text that is not a live value or a literal
    (``free_text``), and every visible word with the texts people read in attributes
    (``all_text``)."""

    SKIP_TAGS = {"script", "style", "code", "pre", "kbd"}
    READ_ATTRS = ("title", "aria-label", "placeholder", "alt")

    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.free_text: list[str] = []
        self.all_text: list[str] = []
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, skip digits, hidden from people)
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        parent_skips, parent_unread = self._stack[-1][1:] if self._stack else (False, False)
        unread = parent_unread or tag in ("script", "style")
        if not unread:
            self.all_text.extend(attrs[name] for name in self.READ_ATTRS if attrs.get(name))
        if tag in VOID:
            return
        skip = parent_skips or tag in self.SKIP_TAGS or "data-live" in attrs or "data-literal" in attrs
        self._stack.append((tag, skip, unread))

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        skip, unread = self._stack[-1][1:] if self._stack else (False, False)
        if data.strip() and not unread:
            self.all_text.append(data.strip())
        if not skip and data.strip():
            self.free_text.append(data.strip())

    def find(self, **attrs) -> list[dict]:
        return [a for _, a in self.elements if all(a.get(k) == v for k, v in attrs.items())]

    def by_id(self) -> dict[str, tuple[str, dict]]:
        return {a["id"]: (tag, a) for tag, a in self.elements if "id" in a}


def page_config(page: str) -> dict:
    match = re.search(r'<script id="page-config" type="application/json">(.*?)</script>', page, re.S)
    assert match, "page-config is missing"
    return json.loads(match.group(1))


def js_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(JS_DIR.glob("*.js"))}


# --- the shell --------------------------------------------------------------------------


@pytest.mark.django_db
def test_page_without_inputs_renders_the_shell_without_external_calls(client, upstream):
    response = client.get(PAGE)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    page = response.content.decode()
    assert 'id="trip-form"' in page and 'id="map"' in page and 'id="player"' in page
    assert page_config(page)["initial"] == {"start": "", "finish": "", "start_tank": "empty"}
    assert client.get(PAGE + "/").status_code == 200  # trailing slash, same page
    assert client.post(PAGE).status_code == 405
    assert upstream.calls == []


@pytest.mark.django_db
def test_page_with_inputs_prefills_the_form_and_makes_no_external_call(client, upstream):
    hostile = '"><script>alert(1)</script>'
    response = client.get(PAGE, {"start": hostile, "finish": FINISH, "start_tank": "full"})
    assert response.status_code == 200
    page = response.content.decode()
    assert "<script>alert(1)</script>" not in page
    assert 'value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in page
    assert f'value="{FINISH}"' in page
    full = Page(page).find(type="radio", value="full")[0]
    assert "checked" in full
    assert page_config(page)["initial"] == {"start": hostile, "finish": FINISH, "start_tank": "full"}
    # Anything but "full" is the default.
    odd = page_config(client.get(PAGE, {"start": START, "finish": FINISH, "start_tank": "half"}).content.decode())
    assert odd["initial"]["start_tank"] == "empty"
    assert upstream.calls == []


@pytest.mark.django_db
def test_page_embeds_about_as_json(client):
    config = page_config(client.get(PAGE).content.decode())
    assert config["api"] == {"route": "/api/route", "about": "/api/about", "page": PAGE, "places": "/api/places"}
    about = config["about"]
    assert about["versions"]["django"] == django.get_version()
    assert about["service"] == build_about()["service"]
    assert {"vehicle", "api", "data", "errors", "versions"} <= about.keys()


@pytest.mark.django_db
def test_map_url_of_an_api_response_opens_the_page_without_another_call(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = get(client).json()
    map_url = body["map"]["map_url"]
    page = client.get(map_url)
    assert page.status_code == 200
    assert len(upstream.calls) == 1

    # What the page's JavaScript then asks: the same request, a plan cache hit, so
    # opening map_url right after Postman costs no call.
    params = dict(parse_qsl(urlsplit(map_url).query))
    assert page_config(page.content.decode())["initial"] == params
    again = client.get("/api/route", params).json()
    assert again["meta"]["plan_cache"] == "hit"
    assert again["meta"]["external_api_calls"] == 0
    assert len(upstream.calls) == 1


def test_the_page_asks_exactly_what_postman_sends():
    """No extra layer is requested: the page's GET /api/route is a Postman request."""
    for name in ("main.js", "api.js"):
        source = js_sources()[name]
        assert not re.search(r"\binclude\b|candidates", source), f"{name} asks for more than Postman does"
    api = js_sources()["api.js"]
    assert "return `${base}?${tripQuery(params)}`;" in api


# --- static files ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_static_assets_are_served_with_the_right_content_type(client, settings):
    settings.DEBUG = False
    page = client.get(PAGE).content.decode()
    own = re.findall(r'(?:src|href)="(/static/[^"]+)"', page)
    assert any(u.split("?")[0].endswith(".css") for u in own) and any(u.split("?")[0].endswith(".js") for u in own)
    expected = {".js": "text/javascript", ".css": "text/css"}
    module_urls = [f"/static/fuelroute/js/{name}" for name in js_sources()]
    for url in own + module_urls:
        response = client.get(url)
        try:
            assert response.status_code == 200, url
            assert response["Content-Type"].startswith(expected[Path(url.split("?")[0]).suffix]), url
            # Versioned URLs (?v=<mtime>) can be cached for good; the rest revalidate.
            assert ("immutable" in response["Cache-Control"]) == ("?v=" in url), url
        finally:
            response.close()
    assert f"?v={web.asset_version()}" in page
    assert client.get("/static/fuelroute/js/missing.js").status_code == 404
    assert client.get("/static/../../config/settings.py").status_code in (400, 404)


def test_every_js_import_points_to_an_existing_file():
    sources = js_sources()
    import_re = re.compile(r"""import\s+(?:\{([^}]*)\}|\*\s+as\s+\w+)\s+from\s+['"](\.{1,2}/[^'"]+)['"]""")
    reachable, todo = set(), ["main.js"]
    while todo:
        name = todo.pop()
        if name in reachable:
            continue
        reachable.add(name)
        for names, target in import_re.findall(sources[name]):
            path = (JS_DIR / target).resolve()
            assert path.is_file(), f"{name} imports missing {target}"
            # Named imports must be exported (no bundler would catch a typo).
            text = sources[path.name]
            for item in filter(None, (n.strip() for n in names.split(","))):
                exported = item.split(" as ")[0].strip()
                assert re.search(
                    rf"export\s+(?:async\s+)?(?:function|const|let|class)\s+{exported}\b", text
                ), f"{name} imports {exported} from {target}, which does not export it"
            todo.append(path.name)
    assert reachable == set(sources), f"modules never imported: {set(sources) - reachable}"


# --- the guided tour ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_steps_are_a_guided_tour(client):
    source = client.get(PAGE).content.decode()
    page = Page(source)
    by_id = page.by_id()
    assert page.find(**{"aria-label": "Steps"})[0]
    buttons = [a for tag, a in page.elements if tag == "button" and "data-step" in a]
    assert [b["id"] for b in buttons] == [f"step-btn-{name}" for name in STEPS]
    for name, button in zip(STEPS, buttons, strict=True):
        assert button["aria-controls"] == f"panel-{name}"
        tag, panel = by_id[f"panel-{name}"]
        assert tag == "section" and panel["tabindex"] == "-1"
        # Named by its h2: the h2 itself, or the words inside it when the h2 also holds an
        # "i" button (whose label must not join the panel's name).
        assert by_id[f"{name}-title"][0] == "h2"
        heading = re.search(rf'<h2 id="{name}-title"[^>]*>(.*?)</h2>', source, re.S).group(1)
        for label in panel["aria-labelledby"].split():
            assert label == f"{name}-title" or f'id="{label}"' in heading, f"panel-{name} is not named by its h2"
        # Only the first step shows, and the others wait for a plan.
        assert ("hidden" in panel) == (name != "trip")
        assert button.get("aria-current") == ("step" if name == "trip" else None)
        assert button.get("aria-disabled") == (None if name == "trip" else "true")
    # Back / Next of each card go to the neighbouring steps.
    template = (TEMPLATES / "partials").glob("_step_*.html")
    goes = {p.stem.removeprefix("_step_"): re.findall(r'data-go="(\w+)"', p.read_text(encoding="utf-8")) for p in template}
    for i, name in enumerate(STEPS):
        assert goes[name] == [s for s in (STEPS[i - 1] if i else None, STEPS[i + 1] if i + 1 < len(STEPS) else None) if s]
    # Every field has a label; the page speaks to screen readers.
    labelled = {a.get("for") for tag, a in page.elements if tag == "label"}
    for tag, attrs in page.elements:
        if tag == "input" and attrs.get("type") not in ("radio", "hidden"):
            assert attrs["id"] in labelled, f"input {attrs['id']} has no label"
    assert page.find(id="live")[0]["aria-live"] == "polite"
    assert "export const STEPS = ['trip', 'route', 'stops', 'cost', 'truck', 'assignment'];" in js_sources()["steps.js"]


@pytest.mark.django_db
def test_page_text_has_no_jargon(client):
    """The page explains the problem to a newcomer: no word an engineer would have to translate."""
    for params in ({}, {"start": START, "finish": FINISH, "corridor_miles": "25"}):
        words = " ".join(Page(client.get(PAGE, params).content.decode()).all_text)
        found = [term for term in JARGON if term.lower() in words.lower()]
        assert found == [], f"jargon on the page: {found}"


@pytest.mark.django_db
def test_truck_step_offers_only_the_essential_settings(client):
    page = Page(client.get(PAGE).content.decode())
    ids = set(page.by_id())
    for name in NUMBER_CONTROLS:
        assert {f"set-{name}", f"set-{name}-range", f"{name}-error"} <= ids, name
    # People think in tank size: the range is shown, not asked (regression: 8 mpg grew the tank).
    assert "set-max_range_miles" not in ids and "set-range" in ids
    assert page.find(id="set-consolidate")[0]["type"] == "checkbox"
    for value in ("empty", "full"):
        # The start tank belongs to the trip form (also submitted without JavaScript).
        assert page.find(type="radio", name="start_tank", value=value)[0]["form"] == "trip-form"
    for name in set(WHAT_IF_PARAMS) - CONTROLLED:
        assert f"set-{name}" not in ids and not page.find(name=name), f"{name} should have no control"
    # The page still knows all of the API's settings: a link that brings one keeps it.
    settings_js = js_sources()["settings.js"]
    assert sorted(re.findall(r"^    name: '(\w+)'", settings_js, re.M)) == sorted(web.WHAT_IF_PARAMS)
    assert set(re.findall(r"^    name: '(\w+)', control: true", settings_js, re.M)) == CONTROLLED
    assert web.WHAT_IF_PARAMS == WHAT_IF_PARAMS  # the API's own list
    # Regression: the quick tries silently undid the user's changes. They say they start over.
    assert "Try one change on the standard truck:" in " ".join(page.all_text)


@pytest.mark.django_db
def test_start_tank_labels_are_the_apis(client):
    """The page and the API name and explain the two modes the same way."""
    from fuelroute.services.planner import START_TANK_HELP, START_TANK_LABELS

    page = html.unescape(client.get(PAGE).content.decode())
    for value, label in START_TANK_LABELS.items():
        assert f"<strong>{label}</strong>" in page
        assert START_TANK_HELP[value] in page


# --- "no number is written by hand" ---------------------------------------------------------


@pytest.mark.django_db
def test_page_has_no_hand_written_numbers(client):
    for params in ({}, {"start": START, "finish": FINISH, "start_tank": "full"}):
        page = Page(client.get(PAGE, params).content.decode())
        with_digits = [text for text in page.free_text if re.search(r"\d", text)]
        assert with_digits == [], f"visible text with digits (use data-live or data-literal): {with_digits}"


def test_js_has_no_hand_written_measurements():
    measurement = re.compile(r"\d\s*(?:ms|mi|miles|gal|gallons|calls|stations|tests)\b|\d\s*%|\$\d")
    found = []
    for name, text in js_sources().items():
        for number, line in enumerate(text.splitlines(), start=1):
            if measurement.search(line):
                found.append(f"{name}:{number}: {line.strip()}")
    assert found == [], "hard-coded measurements in the JavaScript:\n" + "\n".join(found)


@pytest.mark.django_db
def test_data_live_paths_use_allowed_roots(client):
    page = Page(client.get(PAGE).content.decode())
    format_js = js_sources()["format.js"]
    roots = re.search(r"ROOTS = new Set\(\[([^\]]+)\]\)", format_js).group(1)
    assert {r.strip(" '\"") for r in roots.split(",")} == ALLOWED_ROOTS
    formats = set(re.findall(r"^  (\w+): \(", format_js.split("export const fmt = {")[1].split("\n};")[0], re.M))
    derived = set(re.findall(r"\bd\.(\w+) =", format_js)) | set(re.findall(r"^    (\w+): ", format_js.split("const d = {")[1], re.M))
    paths = 0
    for _, attrs in page.elements:
        for key in ("data-live", "data-live-if", "data-href"):
            if key in attrs:
                paths += 1
                root, *rest = attrs[key].split(".")
                assert root in ALLOWED_ROOTS, f"{key}={attrs[key]}"
                if root == "derived":
                    assert rest[0] in derived, f"{key}={attrs[key]}: derive() never sets it"
        if "data-format" in attrs:
            assert attrs["data-format"] in formats, f"unknown data-format {attrs['data-format']}"
    assert paths > 50


# --- regressions found by reviewing the page -----------------------------------------------------


def test_swap_does_not_plan_a_new_trip():
    # Regression: Swap planned the reversed trip at once, spending a routing call the user
    # had not asked for. It now swaps the fields and marks the results as stale.
    main = js_sources()["main.js"]
    handler = main.split("$('#swap').addEventListener('click', () => {", 1)[1].split("\n});", 1)[0]
    assert "planTrip" not in handler and "updateStrips()" in handler


def test_the_map_keeps_the_trip_framed_until_the_user_moves_it():
    # Regression: the trip was framed once; after a resize (desktop -> phone) stops fell
    # outside the map.
    map_js = js_sources()["map.js"]
    assert "new ResizeObserver(frame)" in map_js
    assert "if (!lastBounds || userMoved) return;" in map_js


# --- the Truck step -----------------------------------------------------------------------------


def _quick_tries() -> list[dict]:
    """The quick tries of whatif.js: [{"id", "set": {param: value}, "keep_tank"}] (values as text)."""
    source = js_sources()["whatif.js"]
    block = source.split("export const QUICK_TRIES = [", 1)[1].split("\n];", 1)[0]
    found = []
    for tid, settings, keep in re.findall(r"id: '(\w+)', set: \{ ([^}]*) \}(, keepTank: true)?", block):
        values = {}
        for key, raw in re.findall(r"(\w+): ('[^']*'|true|false|[\d.]+)", settings):
            values[key] = raw.strip("'") if raw.startswith("'") else raw
        found.append({"id": tid, "set": values, "keep_tank": bool(keep)})
    return found


@pytest.mark.django_db
def test_what_if_settings_travel_in_the_page_address(client, upstream):
    """A shared link with truck settings opens the same plan: the page carries them, escaped."""
    response = client.get(PAGE, {
        "start": START, "finish": FINISH, "mpg": "8", "consolidate": "false",
        "price_policy": "</script><script>alert(1)</script>", "bogus": "1",
    })
    page = response.content.decode()
    initial = page_config(page)["initial"]
    assert initial["mpg"] == "8" and initial["consolidate"] == "false"
    assert "bogus" not in initial
    assert "</script><script>alert(1)" not in page  # json_script escapes it
    assert upstream.calls == []
    # Only the API's own settings are carried.
    assert set(initial) - {"start", "finish", "start_tank"} <= set(web.WHAT_IF_PARAMS)


@pytest.mark.django_db
def test_map_url_of_a_what_if_opens_the_page_with_the_same_settings(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = get(client, mpg="8", safety_reserve_gal="2", corridor_miles="25").json()
    map_url = body["map"]["map_url"]
    params = dict(parse_qsl(urlsplit(map_url).query))
    assert params["mpg"] == "8" and params["corridor_miles"] == "25"
    assert page_config(client.get(map_url).content.decode())["initial"] == params
    # What the page then asks is the same plan, from the cache.
    again = client.get("/api/route", params).json()
    assert again["meta"]["plan_cache"] == "hit" and again["meta"]["external_api_calls"] == 0
    assert len(upstream.calls) == 1


@pytest.mark.django_db
def test_every_quick_try_is_planned_without_an_external_call(client, upstream, stations):
    """Each quick try changes one thing (a new mpg with the same tank also changes the
    range); after the trip is routed once, each is planned on the saved road: 0
    external calls, and the API names what changed."""
    from fuelroute.services.planner import SETTING_NAMES, PlanSettings

    tries = _quick_tries()
    assert [t["id"] for t in tries] == ["thirsty", "safety", "full", "merge"]
    upstream.respond(OK_ROUTE)
    assert get(client).status_code == 200
    defaults = PlanSettings.defaults()
    for quick in tries:
        ((name, value),) = quick["set"].items()
        assert name in (*web.WHAT_IF_PARAMS, "start_tank"), quick
        params = {name: value}
        if quick["keep_tank"]:  # "8 mpg, same tank": the range follows
            params["max_range_miles"] = str(defaults.tank_gallons * float(value))
        response = get(client, **params)
        body = response.json()
        assert body["meta"]["external_api_calls"] == 0, quick
        if response.status_code == 200:
            assert body["meta"]["settings_changed"] == [n for n in SETTING_NAMES if n in params], quick
            if quick["keep_tank"]:
                assert body["vehicle"]["tank_gallons"] == defaults.tank_gallons
        else:
            assert response.status_code == 422, (quick, body)
    assert len(upstream.calls) == 1
    # Postman reflects the same numbers (8 mpg with a 400-mile range, 5 gallons of safety fuel).
    assert {"mpg": "8"} == tries[0]["set"] and tries[0]["keep_tank"]
    assert defaults.tank_gallons * 8 == 400
    assert {"safety_reserve_gal": "5"} == tries[1]["set"]
    assert {"consolidate": "true"} == tries[3]["set"]


# --- city suggestions, play trip ------------------------------------------------------------------


@pytest.mark.django_db
def test_city_fields_are_accessible_comboboxes(client):
    page = Page(client.get(PAGE).content.decode())
    by_id = page.by_id()
    for name in ("start", "finish"):
        _, field = by_id[name]
        assert field["role"] == "combobox"
        assert field["aria-autocomplete"] == "list" and field["aria-expanded"] == "false"
        _, listbox = by_id[field["aria-controls"]]
        assert listbox["role"] == "listbox" and "hidden" in listbox
        assert by_id[listbox["aria-labelledby"]][0] == "label"
    places = js_sources()["places.js"]
    for key in ("ArrowDown", "ArrowUp", "Enter", "Escape", "aria-activedescendant", "aria-selected"):
        assert key in places, key


def test_play_trip_replays_the_apis_numbers():
    """The animation reads the answer (no number of its own) and respects reduced motion."""
    player = js_sources()["player.js"]
    for field in ("mile_marker", "fuel_on_arrival_gallons", "gallons", "cost", "total_fuel_cost", "start_fuel_gallons"):
        assert field in player, field
    assert "prefers-reduced-motion: reduce" in player and "jumpToEnd()" in player
    # The cost counter adds integer cents and ends on the API's total, saying whether they match.
    assert "s.exact = s.costCents === trip.totalCents;" in player
    assert "s.costCents = trip.totalCents;" in player
    # Each stop says the rule it follows, and lights up its card.
    assert "RULES[ruleOf(stop)]" in player and "onStop?.(stop.stop);" in player


def test_the_javascript_never_parses_html_strings():
    for name, text in js_sources().items():
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text, name


# --- regressions found by walking through the tour ------------------------------------------------


@pytest.mark.django_db
def test_for_spotter_step_always_shows_the_standard_truck(client):
    """Regression: after "A thirstier truck" the table said "at 10 miles per gallon" next to
    the 8 mpg total. Its proof now reads the standard truck's answer, never the one on screen."""
    page = client.get(PAGE).content.decode()
    panel = page.split('id="panel-assignment"', 1)[1].split("</section>", 1)[0]
    paths = re.findall(r'data-live(?:-if)?="([^"]+)"', panel)
    assert paths and not [p for p in paths if p.startswith("route.")]
    assert any(p.startswith("standard.") for p in paths) and any(p.startswith("derived.standard.") for p in paths)
    main = js_sources()["main.js"]
    assert "const standard = standardBody();" in main and "derived: derive({ ...state, standard, tripCalls })" in main


def test_plan_another_trip_starts_clean():
    """Regression: a changed truck went on to the next trip without a word, with the old places."""
    main = js_sources()["main.js"]
    body = main.split("function planAnother() {", 1)[1].split("\n}", 1)[0]
    for line in ("truck.write({});", "state.nextTruck = { ...STANDARD };", "inputs.start.value = '';",
                 "inputs.finish.value = '';", "inputs.start.focus();"):
        assert line in body, line
    # In the Trip step the strip says the next trip uses a changed truck.
    assert "'Your next trip uses your truck'" in main


def test_a_road_without_truck_stops_offers_a_full_tank():
    """Regression: San Francisco -> Los Angeles ended in an error with no way out and the
    API's own words ("start_tank=full")."""
    main = js_sources()["main.js"]
    assert "'Try leaving with a full tank'" in main
    assert "planTrip({ ...params, start_tank: 'full' }, { card })" in main
    block = main.split("if (code === 'no_fuel_data_on_route') {", 1)[1].split("\n  }\n", 1)[0]
    assert "start_tank=" not in block


def test_a_locked_step_says_why():
    """Regression: clicking a step before planning did nothing visible."""
    steps = js_sources()["steps.js"]
    assert "showStatus(LOCKED_TEXT, 'locked');" in steps and "onLocked?.();" in steps
    assert "Step ${index + 1} of ${STEPS.length}: ${label(currentStep)}" in steps


def test_whole_trip_button_does_not_cover_the_popups():
    """Regression: "Whole trip" sat on top of the first stop's popup title."""
    map_js = js_sources()["map.js"]
    assert "L.control({ position: 'bottomleft' })" in map_js
    assert "autoPanPaddingTopLeft" in map_js and map_js.count(", POPUP)") == 2
