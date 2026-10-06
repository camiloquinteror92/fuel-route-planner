"""/api/about: versions, data counts, requirements -> code and tests, the last test run, errors."""

import os
import platform
import re
import time
from decimal import Decimal

import django
import numpy as np
import pytest
from django.conf import settings as project_settings
from django.db.models import Sum
from rest_framework.test import APIClient

from fuelroute.models import FuelStation
from fuelroute.services import about
from fuelroute.services.errors import PlannerError
from fuelroute.services.places import get_place_index
from fuelroute.services.stations import data_version

from .test_api import stations  # noqa: F401  (a fixture)

REQUIREMENT_IDS = [
    "usa_inputs", "route_map", "cost_effective_stops", "range_multiple_stops", "total_cost_mpg", "price_file",
    "free_routing_api", "django_version", "fast", "few_routing_calls", "deliverables",
    "exact_money", "robust_errors", "messy_data", "production_ready", "tested",
]
CONTRACT_CODE_LINKS = {
    "serializers.request", "geocoding.geocode", "places.index", "usa.in_usa", "planner.plan_trip",
    "planner.check_endpoints", "planner.tank_rules", "planner.build_stops", "planner.build_geojson",
    "planner.comparison", "osrm.get_route", "osrm.decode_polyline", "osrm.prepare_route", "http.client",
    "stations.stations_along_route", "optimizer.greedy", "optimizer.consolidate", "optimizer.plan_price_blind",
    "station_loader.load_stations", "station_loader.resolve_homonym", "middleware.response_time",
    "middleware.rate_limit", "metrics.route_metrics",
    "planner.settings", "optimizer.plan_fuel_stops", "places.search",
}

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" \
errors="0" failures="1" skipped="1" tests="4" time="1.5" timestamp="2026-01-02T03:04:05.000000-05:00" hostname="h">\
<testcase classname="fuelroute.tests.test_api" name="test_invalid_input_returns_400[params0-start]" time="0.01" />\
<testcase classname="fuelroute.tests.test_api" name="test_invalid_input_returns_400[params1-finish]" time="0.01" />\
<testcase classname="fuelroute.tests.test_api" name="test_invalid_input_returns_400[params2-start_tank]" time="0.01">\
<failure message="boom">trace</failure></testcase>\
<testcase classname="fuelroute.tests.test_optimizer" name="test_short_trip_with_full_tank_needs_no_stop" time="0.0">\
<skipped message="skip" /></testcase></testsuite></testsuites>"""


@pytest.fixture(autouse=True)
def fresh_caches():
    about.reset_caches()
    yield
    about.reset_caches()


def _station(opis_id, state, price, rows=1, lat=None, lon=None, source=""):
    return FuelStation.objects.create(
        opis_id=opis_id, name=f"STOP {opis_id}", address="I-10", city="Town", state=state,
        price=Decimal(price), price_min=Decimal(price), price_max=Decimal(price), price_rows=rows,
        latitude=lat, longitude=lon, geocode_source=source,
    )


@pytest.mark.django_db
def test_about_reports_running_versions_and_data_counts(stations):
    _station(7, "TX", "3.10", rows=3)  # in the file, but its city could not be placed
    _station(8, "AR", "2.40", rows=2, lat=35.0, lon=-92.0, source="geonames")
    response = APIClient().get("/api/about")
    assert response.status_code == 200
    body = response.json()

    assert body["service"] == "Spotter fuel route API"
    assert body["versions"]["django"] == django.get_version()
    assert body["versions"]["python"] == platform.python_version()
    assert body["versions"]["numpy"] == np.__version__
    requirements = (project_settings.BASE_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert f"Django=={body['pinned']['Django']}" in requirements

    data = body["data"]
    assert data["version"] == data_version()
    assert data["price_file"] == "fuel-prices-for-be-assessment.csv"
    assert data["stations"] == FuelStation.objects.count() == 8
    assert data["price_quotes"] == FuelStation.objects.aggregate(total=Sum("price_rows"))["total"] == 11
    assert data["stations_with_several_quotes"] == FuelStation.objects.filter(price_rows__gt=1).count() == 2
    assert data["geocoded"] == FuelStation.objects.filter(latitude__isnull=False).count() == 7
    assert data["not_geocoded"] == 1
    assert data["geocoded_by_source"] == {"census": 6, "geonames": 1}
    assert data["states"] == 3
    assert data["stations_by_state"] == [
        {"state": "OK", "stations": 6, "geocoded": 6},
        {"state": "AR", "stations": 1, "geocoded": 1},
        {"state": "TX", "stations": 1, "geocoded": 0},
    ]
    # Lower 48 without a station that can be planned with: TX has one, but without
    # coordinates. Regression: DC (not a state) was listed as a "lower 48 state".
    assert "TX" in data["states_without_stations"]
    assert not {"OK", "AR", "AK", "HI", "DC"} & set(data["states_without_stations"])
    assert len(data["states_without_stations"]) == 48 - 2
    assert data["thinnest_states"] == [{"state": "AR", "geocoded": 1}, {"state": "OK", "geocoded": 6}]
    assert data["district_of_columbia_geocoded"] == 0
    assert data["price_per_gallon"] == {"min": 2.4, "median": 3.1, "max": 3.6}  # geocoded stations only
    assert data["places_index_entries"] == len(get_place_index())

    vehicle = body["vehicle"]
    config = project_settings.FUEL_PLANNER
    assert vehicle["max_range_miles"] == config["MAX_RANGE_MILES"]
    assert vehicle["tank_gallons"] == config["MAX_RANGE_MILES"] / config["MILES_PER_GALLON"]
    assert body["planner"]["corridor_miles"] == config["CORRIDOR_MILES"]
    assert body["planner"]["quarter_tank_fraction"] == config["BASELINE_REFUEL_FRACTION"]
    what_if = ["mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal"]
    params = {p["name"]: p for p in body["api"]["params"]}
    assert list(params) == ["start", "finish", "start_tank", "include", *what_if]
    assert all(p["help_text"] for p in params.values())
    assert body["api"]["what_if_params"] == what_if == [name for name, p in params.items() if p["what_if"]]
    # Defaults come from the configuration; ranges from the serializer.
    assert params["mpg"]["default"] == config["MILES_PER_GALLON"]
    assert (params["mpg"]["min"], params["mpg"]["max"]) == (3, 30)
    assert params["max_range_miles"]["default"] == config["MAX_RANGE_MILES"]
    assert params["corridor_miles"]["default"] == config["CORRIDOR_MILES"]
    assert params["price_policy"]["default"] == "median"
    assert params["price_policy"]["choices"] == ["median", "min", "max"]
    assert params["consolidate"]["default"] is True and params["safety_reserve_gal"]["default"] == 0
    assert params["start_tank"]["default"] == "empty" and params["start"]["default"] is None
    assert [m["label"] for m in body["api"]["start_tank_modes"]] == ["Pay for every mile", "Start with a full tank"]
    assert body["api"]["include_values"] == ["candidates"]
    assert {s["name"] for s in body["external_services"]} == {"osrm", "nominatim", "openstreetmap_tiles"}
    assert {e["path"] for e in body["endpoints"]} >= {
        "/api/route", "/api/route/map", "/api/places", "/api/stats", "/api/about",
    }

    build = body["build"]
    if build["commit"]:  # a git checkout (always, except in an exported tarball)
        assert re.fullmatch(r"[0-9a-f]{40}", build["commit"])
        assert build["history"][0]["commit"] == build["commit"]
        assert isinstance(build["dirty"], bool) and isinstance(build["pushed"], bool)
        for commit in build["history"]:  # only commits GitHub has are linked
            assert commit["url"] == (f"{build['repo_url']}/commit/{commit['commit']}" if commit["pushed"] else None)
    deliverables = body["deliverables"]
    assert deliverables["postman_collection"] == "postman/collection.json"
    assert (project_settings.BASE_DIR / deliverables["postman_collection"]).is_file()
    assert deliverables["release_check"]["django_latest_stable"] == config["DJANGO_LATEST_STABLE"]
    assert body["api"]["state_codes"] == sorted(body["api"]["state_codes"]) and "TX" in body["api"]["state_codes"]


@pytest.mark.django_db
def test_every_requirement_points_to_existing_tests_and_code():
    body = about.build_about()
    requirements = body["requirements"]
    assert [r["id"] for r in requirements] == REQUIREMENT_IDS
    assert {r["kind"] for r in requirements[:11]} == {"explicit"}
    assert {r["kind"] for r in requirements[11:]} == {"implicit"}
    assert CONTRACT_CODE_LINKS <= set(body["code_links"])
    for requirement in requirements:
        assert requirement["brief"] and requirement["how"]
        for source in requirement["sources"]:
            assert source in body["code_links"], (requirement["id"], source)
        for test in requirement["tests"]:
            assert test in body["test_index"], (requirement["id"], test)
    for key, link in body["code_links"].items():
        assert link["start_line"] and link["end_line"] >= link["start_line"], key
        if link["url"]:  # null while the symbol is not on GitHub yet
            start, end = link["url_lines"]
            assert link["url"].startswith(f"{body['build']['repo_url']}/blob/")
            assert link["url"].endswith(f"/{link['path']}#L{start}-L{end}")
    assert all(entry["line"] > 0 and entry["path"].startswith("fuelroute/tests/") for entry in body["test_index"].values())
    assert "fuelroute/tests/test_about.py::test_every_requirement_points_to_existing_tests_and_code" in body["test_index"]


def test_requirement_texts_have_no_digits():
    # Numbers come from the configuration (placeholders), never from the text itself.
    for requirement in about.REQUIREMENTS:
        for field in ("brief", "how"):
            text = re.sub(r"\{[a-z_]+\}", "", requirement[field])
            assert not re.search(r"\d", text), (requirement["id"], field, text)


@pytest.mark.django_db
def test_test_report_is_parsed_and_flagged_stale(tmp_path, settings, monkeypatch):
    report = tmp_path / "pytest.xml"
    report.write_text(JUNIT, encoding="utf-8")
    monkeypatch.setitem(settings.FUEL_PLANNER, "TEST_REPORT_FILE", report)
    long_ago = time.time() - 10 * 365 * 24 * 3600
    os.utime(report, (long_ago, long_ago))

    body = about.build_about()
    assert body["tests"] == {
        "report": "found",
        "ran_at": "2026-01-02T08:04:05+00:00",
        "duration_seconds": 1.5,
        "total": 4,
        "passed": 2,
        "failed": 1,
        "errors": 0,
        "skipped": 1,
        "stale": True,  # the code is newer than that report
    }
    entry = body["test_index"]["fuelroute/tests/test_api.py::test_invalid_input_returns_400"]
    assert (entry["cases"], entry["passed"], entry["failed"]) == (3, 2, 1)
    skipped = body["test_index"]["fuelroute/tests/test_optimizer.py::test_short_trip_with_full_tank_needs_no_stop"]
    assert (skipped["cases"], skipped["passed"], skipped["failed"]) == (1, 0, 0)
    not_run = body["test_index"]["fuelroute/tests/test_geo.py::test_us_mask"]
    assert (not_run["cases"], not_run["passed"], not_run["failed"]) == (0, 0, 0)

    in_the_future = time.time() + 3600
    os.utime(report, (in_the_future, in_the_future))
    assert about.build_about()["tests"]["stale"] is False

    monkeypatch.setitem(settings.FUEL_PLANNER, "TEST_REPORT_FILE", tmp_path / "missing.xml")
    body = about.build_about()
    assert body["tests"]["report"] == "missing" and body["tests"]["total"] is None
    assert all(entry["cases"] is None for entry in body["test_index"].values())


def _subclasses(cls):
    for subclass in cls.__subclasses__():
        yield subclass
        yield from _subclasses(subclass)


def test_error_catalog_lists_every_planner_error():
    catalog = about.error_catalog()
    by_code = {error["code"]: error for error in catalog}
    assert len(by_code) == len(catalog)  # one entry per code
    for cls in (PlannerError, *_subclasses(PlannerError)):
        assert by_code[cls.code]["status"] == cls.status_code
        assert by_code[cls.code]["description"], cls.__name__
    for code, status, _ in about.HTTP_ERRORS:
        assert by_code[code]["status"] == status
    assert catalog == sorted(catalog, key=lambda error: (error["status"], error["code"]))


@pytest.mark.django_db
def test_about_tolerates_missing_git(monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(about.subprocess, "run", no_git)
    body = about.build_about()
    assert body["build"] == {
        "repo_url": body["build"]["repo_url"],
        "commit": None,
        "commit_short": None,
        "commit_time": None,
        "subject": None,
        "dirty": None,
        "pushed": None,
        "linked_commit": None,
        "linked_commit_short": None,
        "commits_not_on_github": None,
        "history": [],
    }
    assert all("/blob/main/" in link["url"] for link in body["code_links"].values())


@pytest.mark.django_db
def test_links_point_to_what_github_has_not_to_the_local_checkout(monkeypatch):
    # Regression: before a push the links went to blob/main with the LOCAL line numbers:
    # most opened the wrong line, and new files, symbols and commits gave a 404.
    head, pushed = "a" * 40, "b" * 40
    log = (
        f"{head}\x1f2026-10-06T07:00:00-05:00\x1ffeat: not pushed\n"
        f"{pushed}\x1f2026-10-06T06:00:00-05:00\x1fdocs: pushed\n"
    )
    answers = {"log": log, "status": "", "rev-list": f"{pushed}\n"}
    monkeypatch.setattr(about, "_git", lambda *args, **kwargs: answers[args[0]])
    asked = []

    def blobs(commit, paths):
        asked.append(commit)
        texts = dict.fromkeys(paths)  # every file missing at that commit...
        texts["fuelroute/services/optimizer.py"] = "\n" * 9 + "def _greedy():\n    pass\n"  # ...but this one
        texts["fuelroute/tests/test_api.py"] = "def test_invalid_input_returns_400():\n    pass\n"
        return texts

    monkeypatch.setattr(about, "_git_blobs", blobs)
    body = about.build_about()
    build = body["build"]
    assert (build["commit"], build["pushed"], build["linked_commit"], build["commits_not_on_github"]) == (
        head, False, pushed, 1,
    )
    assert [c["url"] for c in build["history"]] == [None, f"{build['repo_url']}/commit/{pushed}"]
    greedy = body["code_links"]["optimizer.greedy"]
    assert greedy["url"] == f"{build['repo_url']}/blob/{pushed}/fuelroute/services/optimizer.py#L10-L11"
    assert greedy["url_lines"] == [10, 11] and greedy["start_line"] != 10  # GitHub's lines, not the local ones
    assert body["code_links"]["metrics.route_metrics"]["url"] is None  # not on GitHub yet
    tests = body["test_index"]
    assert tests["fuelroute/tests/test_api.py::test_invalid_input_returns_400"]["url"].endswith("/test_api.py#L1")
    assert tests["fuelroute/tests/test_web.py::test_tabs_are_accessible"]["url"] is None
    assert asked == [pushed]  # every file in one git call
    about.build_about()
    assert asked == [pushed]  # a commit never changes: cached for good


def test_conventional_commit_types_are_parsed():
    assert about._CONVENTIONAL.match("feat(api): Server-Timing").group(1) == "feat"
    assert about._CONVENTIONAL.match("fix!: breaking").group(1) == "fix"
    assert about._CONVENTIONAL.match("Merge pull request #1") is None
