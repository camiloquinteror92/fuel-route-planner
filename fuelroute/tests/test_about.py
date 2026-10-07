"""/api/about: versions, truck defaults, the parameters of /api/route, deliverables, data counts, errors."""

import platform
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

# The contract the planner page reads (fuelroute/web.py embeds the same dict).
CONTRACT_KEYS = {
    "service", "versions", "vehicle", "planner", "external_services", "api", "deliverables", "data", "errors",
    "endpoints",
}
# Removed on purpose: git state, test results, links into the code and the requirement texts.
REMOVED_KEYS = {"build", "tests", "test_index", "code_links", "requirements", "pinned"}


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

    assert set(body) == CONTRACT_KEYS
    assert not REMOVED_KEYS & set(body)
    assert body["service"] == "Spotter fuel route API"
    assert body["versions"]["django"] == django.get_version()
    assert body["versions"]["python"] == platform.python_version()
    assert body["versions"]["numpy"] == np.__version__

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
    assert vehicle["miles_per_gallon"] == config["MILES_PER_GALLON"]
    assert vehicle["tank_gallons"] == config["MAX_RANGE_MILES"] / config["MILES_PER_GALLON"]
    assert vehicle["min_stop_gallons"] == config["MIN_STOP_GALLONS"]
    assert vehicle["max_consolidation_cost"] == config["MAX_CONSOLIDATION_COST"]
    assert body["planner"]["corridor_miles"] == config["CORRIDOR_MILES"]
    assert body["planner"]["quarter_tank_fraction"] == config["BASELINE_REFUEL_FRACTION"]

    api = body["api"]
    assert set(api) == {
        "include_values", "params", "state_codes", "what_if_params", "price_policies", "start_tank_modes",
    }
    what_if = ["mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal"]
    params = {p["name"]: p for p in api["params"]}
    assert list(params) == ["start", "finish", "start_tank", "include", *what_if]
    assert all(p["help_text"] for p in params.values())
    assert api["what_if_params"] == what_if == [name for name, p in params.items() if p["what_if"]]
    # Defaults come from the configuration; ranges from the serializer.
    assert params["mpg"]["default"] == config["MILES_PER_GALLON"]
    assert (params["mpg"]["min"], params["mpg"]["max"]) == (3, 30)
    assert params["max_range_miles"]["default"] == config["MAX_RANGE_MILES"]
    assert params["corridor_miles"]["default"] == config["CORRIDOR_MILES"]
    assert params["price_policy"]["default"] == "median"
    assert params["price_policy"]["choices"] == ["median", "min", "max"]
    assert params["consolidate"]["default"] is True and params["safety_reserve_gal"]["default"] == 0
    assert params["start_tank"]["default"] == "empty" and params["start"]["default"] is None
    assert [m["label"] for m in api["start_tank_modes"]] == ["Pay for every mile", "Start with a full tank"]
    assert api["include_values"] == ["candidates"]
    assert api["state_codes"] == sorted(api["state_codes"]) and "TX" in api["state_codes"]
    assert {s["name"] for s in body["external_services"]} == {"osrm", "nominatim", "openstreetmap_tiles"}
    assert {e["path"] for e in body["endpoints"]} == {"/api/route", "/api/route/map", "/api/places", "/api/about", "/"}


@pytest.mark.django_db
def test_deliverables_link_the_repository_postman_and_loom(settings, monkeypatch):
    monkeypatch.setitem(settings.FUEL_PLANNER, "REPO_URL", "https://github.com/someone/fuel-route-planner/")
    monkeypatch.setitem(settings.FUEL_PLANNER, "LOOM_URL", "")
    deliverables = about.build_about()["deliverables"]
    assert deliverables == {
        "repo_url": "https://github.com/someone/fuel-route-planner",  # without the trailing slash
        "postman_url": "https://github.com/someone/fuel-route-planner/blob/main/postman/collection.json",
        "loom_url": None,  # until the video exists
    }
    assert (project_settings.BASE_DIR / "postman" / "collection.json").is_file()

    monkeypatch.setitem(settings.FUEL_PLANNER, "LOOM_URL", "https://www.loom.com/share/abc")
    assert about.build_about()["deliverables"]["loom_url"] == "https://www.loom.com/share/abc"


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
    # The page's test runner and its errors were removed.
    assert not {code for code in by_code if code.startswith("test_run")}
