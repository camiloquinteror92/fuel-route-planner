"""The planner page (``fuelroute/web.py``): a shell that never plans, its static
files, and the guards behind its rules.

* Rendering the page costs 0 external calls; the browser calls ``/api/route``.
* CSS and ES modules are served with the right types (DEBUG off, no collectstatic).
* "No number is written by hand": visible text has no digits unless it is a live
  value (``data-live``) or a fixed literal (``data-literal``), and the JavaScript
  has no hard-coded measurements.
* The page and the API agree: every live path, format, code link, test id and
  requirement the page uses exists on the server side, and every what-if control,
  interview question and start-tank label is one the API accepts.
* The interactive parts (city suggestions, "What if…", "Play trip", the Tests and
  "How it scales" tabs) are accessible and only show numbers from the API.
"""

import ast
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
REPO = Path(web.__file__).resolve().parent.parent
ALLOWED_ROOTS = {"route", "stats", "about", "client", "derived"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Page(HTMLParser):
    """Elements (tag, attrs) plus visible text that is not a live value or a literal."""

    SKIP_TAGS = {"script", "style", "code", "pre", "kbd"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []
        self.free_text: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag in VOID:
            return
        parent_skips = self._stack[-1][1] if self._stack else False
        skip = parent_skips or tag in self.SKIP_TAGS or "data-live" in attrs or "data-literal" in attrs
        self._stack.append((tag, skip))

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        skip = self._stack[-1][1] if self._stack else False
        if not skip and data.strip():
            self.free_text.append(data.strip())

    def find(self, **attrs) -> list[dict]:
        return [a for _, a in self.elements if all(a.get(k) == v for k, v in attrs.items())]


def page_config(html: str) -> dict:
    match = re.search(r'<script id="page-config" type="application/json">(.*?)</script>', html, re.S)
    assert match, "page-config is missing"
    return json.loads(match.group(1))


def js_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(JS_DIR.glob("*.js"))}


def template_sources() -> str:
    """The page templates without their {% comment %} blocks."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(TEMPLATES.rglob("*.html")))
    return re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", text, flags=re.S)


# --- the shell --------------------------------------------------------------------------


@pytest.mark.django_db
def test_page_without_inputs_renders_the_shell_without_external_calls(client, upstream):
    response = client.get(PAGE)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    html = response.content.decode()
    assert 'id="trip-form"' in html and 'id="map"' in html and 'id="profile"' in html
    assert page_config(html)["initial"] == {"start": "", "finish": "", "start_tank": "empty"}
    assert client.get(PAGE + "/").status_code == 200  # trailing slash, same page
    assert client.post(PAGE).status_code == 405
    assert upstream.calls == []


@pytest.mark.django_db
def test_page_with_inputs_prefills_the_form_and_makes_no_external_call(client, upstream):
    hostile = '"><script>alert(1)</script>'
    response = client.get(PAGE, {"start": hostile, "finish": FINISH, "start_tank": "full"})
    assert response.status_code == 200
    html = response.content.decode()
    assert "<script>alert(1)</script>" not in html
    assert 'value="&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html
    assert f'value="{FINISH}"' in html
    full = Page(html).find(type="radio", value="full")[0]
    assert "checked" in full
    assert page_config(html)["initial"] == {"start": hostile, "finish": FINISH, "start_tank": "full"}
    # Anything but "full" is the default.
    odd = page_config(client.get(PAGE, {"start": START, "finish": FINISH, "start_tank": "half"}).content.decode())
    assert odd["initial"]["start_tank"] == "empty"
    assert upstream.calls == []


@pytest.mark.django_db
def test_page_embeds_about_as_json(client):
    config = page_config(client.get(PAGE).content.decode())
    assert config["api"] == {
        "route": "/api/route", "stats": "/api/stats", "about": "/api/about", "page": PAGE,
        "places": "/api/places", "tests": "/api/tests", "tests_run": "/api/tests/run",
    }
    about = config["about"]
    assert about["versions"]["django"] == django.get_version()
    assert about["service"] == build_about()["service"]
    assert {"vehicle", "planner", "data", "requirements", "errors", "test_index", "code_links"} <= about.keys()


@pytest.mark.django_db
def test_map_url_of_an_api_response_opens_the_page_without_another_call(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = get(client).json()
    map_url = body["map"]["map_url"]
    page = client.get(map_url)
    assert page.status_code == 200
    assert len(upstream.calls) == 1

    # What the page's JavaScript then asks: the same trip plus the map layer. It is
    # a plan cache hit, so opening map_url right after Postman costs no call.
    params = dict(parse_qsl(urlsplit(map_url).query))
    assert page_config(page.content.decode())["initial"] == params
    again = client.get("/api/route", {**params, "include": "candidates"}).json()
    assert again["meta"]["plan_cache"] == "hit"
    assert again["meta"]["external_api_calls"] == 0
    assert len(again["candidates"]["rows"]) == again["summary"]["candidate_stations_on_route"]
    assert len(upstream.calls) == 1


# --- static files ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_static_assets_are_served_with_the_right_content_type(client, settings):
    settings.DEBUG = False
    html = client.get(PAGE).content.decode()
    own = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
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
    assert f"?v={web.asset_version()}" in html
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


# --- accessibility ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_tabs_are_accessible(client):
    page = Page(client.get(PAGE).content.decode())
    by_id = {a["id"]: (tag, a) for tag, a in page.elements if "id" in a}
    tabs = page.find(role="tab")
    assert [t["id"] for t in tabs] == [
        "tab-plan", "tab-requirements", "tab-tests", "tab-performance", "tab-scale", "tab-how", "tab-api",
    ]
    main = js_sources()["main.js"]
    assert "const TABS = ['plan', 'requirements', 'tests', 'performance', 'scale', 'how', 'api'];" in main
    assert [t["aria-selected"] for t in tabs].count("true") == 1
    for tab in tabs:
        tag, panel = by_id[tab["aria-controls"]]
        assert panel.get("role") == "tabpanel"
        assert panel.get("aria-labelledby") == tab["id"]
        assert tab.get("tabindex", "0") == ("0" if tab["aria-selected"] == "true" else "-1")
    assert page.find(role="tablist")
    labelled = {a.get("for") for tag, a in page.elements if tag == "label"}
    for tag, attrs in page.elements:
        if tag == "input" and attrs.get("type") not in ("radio", "hidden"):
            assert attrs["id"] in labelled, f"input {attrs['id']} has no label"
    assert page.find(id="live").pop()["aria-live"] == "polite"


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
    paths = 0
    for _, attrs in page.elements:
        for key in ("data-live", "data-live-if", "data-live-unless", "data-href"):
            if key in attrs:
                paths += 1
                assert attrs[key].split(".")[0] in ALLOWED_ROOTS, f"{key}={attrs[key]}"
        if "data-format" in attrs:
            assert attrs["data-format"] in formats, f"unknown data-format {attrs['data-format']}"
    assert paths > 50


# --- the page and the API agree ----------------------------------------------------------------


def _functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


@pytest.mark.django_db
def test_edge_cases_link_to_existing_tests():
    about = build_about()
    cases_js = js_sources()["cases.js"]
    prefix = re.search(r"const T = '([^']+)';", cases_js).group(1)
    test_ids = [prefix + name for name in re.findall(r"test_id: `\$\{T\}(\w+)`", cases_js)]
    test_ids += re.findall(r'data-test="([^"]+)"', template_sources())
    assert len(test_ids) > 15
    for test_id in test_ids:
        path, func = test_id.split("::")
        assert func in _functions(REPO / path), f"{test_id} does not exist"
        assert test_id in about["test_index"], f"{test_id} is not in /api/about test_index"

    # Every error code with a live case is in the catalog the API publishes.
    codes = {e["code"] for e in about["errors"]}
    mapped = re.search(r"CASE_FOR_ERROR = \{(.*?)\};", cases_js, re.S).group(1)
    for code in re.findall(r"^\s*(\w+):", mapped, re.M):
        assert code in codes, f"{code} is not in /api/about errors"


@pytest.mark.django_db
def test_page_code_links_and_requirements_exist_on_the_server():
    about = build_about()
    keys = set(re.findall(r'data-code="([^"]+)"', template_sources()))
    keys |= set(re.findall(r"'([a-z_]+\.[a-z_]+)'\)", js_sources()["performance.js"]))
    assert keys, "no code links found"
    assert keys <= about["code_links"].keys(), f"unknown code links: {keys - about['code_links'].keys()}"
    # The Requirements tab shows live evidence for every requirement the API lists.
    handled = set(re.findall(r"case '(\w+)':", js_sources()["requirements.js"]))
    assert {r["id"] for r in about["requirements"]} <= handled


# --- regressions found by reviewing the page -----------------------------------------------------


@pytest.mark.django_db
def test_profile_is_drawn_in_real_pixels(client):
    # Regression: viewBox 1000x320 with preserveAspectRatio="none" squeezed the labels to a
    # few pixels on a phone and turned the stop circles into ovals.
    svg = Page(client.get(PAGE).content.decode()).find(id="profile")[0]
    assert "preserveaspectratio" not in svg and "viewbox" not in svg
    profile = js_sources()["profile.js"]
    assert "setAttribute('viewBox', `0 0 ${width} ${height}`)" in profile
    assert "new ResizeObserver" in profile  # redrawn when the box changes size


def test_swap_does_not_plan_a_new_trip():
    # Regression: Swap planned the reversed trip at once, spending a routing call the user
    # had not asked for. It now swaps the fields and marks the results as stale.
    main = js_sources()["main.js"]
    handler = main.split("$('#swap').addEventListener('click', () => {", 1)[1].split("\n});", 1)[0]
    assert "planTrip" not in handler and "markStale()" in handler


def test_the_map_keeps_the_trip_framed_until_the_user_moves_it():
    # Regression: the trip was framed once; after a resize (desktop -> phone) stops fell
    # outside the map.
    map_js = js_sources()["map.js"]
    assert "new ResizeObserver(frame)" in map_js
    assert "if (!lastBounds || userMoved) return;" in map_js


# --- what if… ------------------------------------------------------------------------------------


def _questions() -> list[dict]:
    """The interview questions of whatif.js: [{"id", "set": {param: value}, "keep_tank"}]."""
    source = js_sources()["whatif.js"]
    block = source.split("export const QUESTIONS = [", 1)[1].split("\n];", 1)[0]
    found = []
    for qid, settings, keep in re.findall(r"id: '(\w+)', set: \{ ([^}]*) \}(, keepTank: true)?", block):
        values = {}
        for key, raw in re.findall(r"(\w+): ('[^']*'|true|false|[\d.]+)", settings):
            values[key] = raw.strip("'") if raw.startswith("'") else raw
        found.append({"id": qid, "set": values, "keep_tank": bool(keep)})
    return found


@pytest.mark.django_db
def test_what_if_settings_travel_in_the_page_address(client, upstream):
    """A shared link with what-if settings opens the same plan: the page carries them, escaped."""
    response = client.get(PAGE, {
        "start": START, "finish": FINISH, "mpg": "8", "consolidate": "false",
        "price_policy": "</script><script>alert(1)</script>", "bogus": "1",
    })
    html = response.content.decode()
    initial = page_config(html)["initial"]
    assert initial["mpg"] == "8" and initial["consolidate"] == "false"
    assert "bogus" not in initial
    assert "</script><script>alert(1)" not in html  # json_script escapes it
    assert upstream.calls == []
    # Only the API's own what-if parameters are carried.
    assert set(initial) - {"start", "finish", "start_tank"} <= set(web.WHAT_IF_PARAMS)


@pytest.mark.django_db
def test_map_url_of_a_what_if_opens_the_page_with_the_same_settings(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = get(client, mpg="8", safety_reserve_gal="2").json()
    map_url = body["map"]["map_url"]
    params = dict(parse_qsl(urlsplit(map_url).query))
    assert params["mpg"] == "8"
    assert page_config(client.get(map_url).content.decode())["initial"] == params
    # What the page then asks is the same plan, from the cache.
    again = client.get("/api/route", {**params, "include": "candidates"}).json()
    assert again["meta"]["plan_cache"] == "hit" and again["meta"]["external_api_calls"] == 0
    assert len(upstream.calls) == 1


@pytest.mark.django_db
def test_what_if_panel_has_a_control_for_every_api_setting(client):
    page = Page(client.get(PAGE).content.decode())
    settings_js = js_sources()["settings.js"]
    js_names = re.findall(r"^    name: '(\w+)'", settings_js, re.M)
    assert sorted(js_names) == sorted(web.WHAT_IF_PARAMS)
    assert web.WHAT_IF_PARAMS == WHAT_IF_PARAMS  # the API's own list
    ids = {a.get("id") for _, a in page.elements}
    for name in web.WHAT_IF_PARAMS:
        control = f"set-{name}" in ids or bool(page.find(type="radio", name=name))
        assert control, f"no control for {name}"
        assert f"{name}-error" in ids, f"no place for the API's message about {name}"
    for value in ("median", "min", "max"):
        assert page.find(type="radio", name="price_policy", value=value)
    # The start tank belongs to the trip form (also submitted without JavaScript).
    for value in ("empty", "full"):
        assert page.find(type="radio", name="start_tank", value=value)[0]["form"] == "trip-form"
    assert {"whatif-apply", "whatif-reset", "whatif-effect", "whatif-questions"} <= ids


@pytest.mark.django_db
def test_start_tank_labels_are_the_apis(client):
    """The page and the API name the two modes the same way."""
    from fuelroute.services.planner import START_TANK_LABELS

    html = client.get(PAGE).content.decode()
    settings_js = js_sources()["settings.js"]
    for value, label in START_TANK_LABELS.items():
        assert f"<strong>{label}</strong>" in html
        assert f"value: '{value}',\n    label: '{label}'," in settings_js


@pytest.mark.django_db
def test_every_interview_question_is_answered_without_an_external_call(client, upstream, stations):
    """Each question changes one thing (a new mpg with the same tank also changes the range);
    after the trip is routed once, each answer is planned on the cached route: 0 external
    calls, and the API names what changed."""
    from fuelroute.services.planner import SETTING_NAMES, PlanSettings

    questions = _questions()
    assert 6 <= len(questions) <= 8
    upstream.respond(OK_ROUTE)
    assert get(client).status_code == 200
    defaults = PlanSettings.defaults()
    for question in questions:
        ((name, value),) = question["set"].items()
        assert name in (*web.WHAT_IF_PARAMS, "start_tank"), question
        params = {name: value}
        if question["keep_tank"]:  # "8 mpg with the same tank": the range follows
            params["max_range_miles"] = str(defaults.tank_gallons * float(value))
        response = get(client, **params)
        body = response.json()
        assert body["meta"]["external_api_calls"] == 0, question
        if response.status_code == 200:
            assert body["meta"]["settings_changed"] == [n for n in SETTING_NAMES if n in params], question
            if question["keep_tank"]:
                assert body["vehicle"]["tank_gallons"] == defaults.tank_gallons
        else:
            assert response.status_code == 422, (question, body)
    assert len(upstream.calls) == 1
    # The "8 mpg" question keeps the tank: an interviewer means the same truck.
    assert any(q["keep_tank"] and "mpg" in q["set"] for q in questions)


# --- city suggestions, play trip, tests, scale ---------------------------------------------------


@pytest.mark.django_db
def test_city_fields_are_accessible_comboboxes(client):
    page = Page(client.get(PAGE).content.decode())
    by_id = {a["id"]: (tag, a) for tag, a in page.elements if "id" in a}
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


def test_the_page_never_sends_arguments_to_the_test_runner():
    api = js_sources()["api.js"]
    run = api.split("runTests() {", 1)[1].split("},", 1)[0]
    assert "request('POST', endpoints.tests_run, { origin: 'tests tab' })" in run


@pytest.mark.django_db
def test_every_live_number_of_the_scale_tab_is_filled_from_data(client):
    html = client.get(PAGE).content.decode()
    slots = set(re.findall(r'data-scale="(\w+)"', html))
    scale_js = js_sources()["scale.js"]
    values = scale_js.split("const values = {", 1)[1].split("};", 1)[0]
    filled = set(re.findall(r"^    (\w+):", values, re.M))
    assert slots and slots <= filled, f"slots without a value: {slots - filled}"


def test_the_javascript_never_parses_html_strings():
    for name, text in js_sources().items():
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text, name
