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

from .test_api import stations  # noqa: F401  (a fixture)

# The contract the planner page reads (fuelroute/web.py embeds the same dict).
CONTRACT_KEYS = {"service", "versions", "vehicle", "api", "deliverables", "data", "errors"}
# Kept out on purpose: internals nobody needs to use the API (timeouts, cache sizes, the
# corridor search step) and lists the page no longer reads.
REMOVED_KEYS = {"planner", "external_services", "endpoints", "build", "tests", "requirements"}


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
    assert data["price_file"] == "fuel-prices-for-be-assessment.csv"
    assert data["stations"] == FuelStation.objects.count() == 8
    assert data["price_quotes"] == FuelStation.objects.aggregate(total=Sum("price_rows"))["total"] == 11
    assert data["stations_with_several_quotes"] == FuelStation.objects.filter(price_rows__gt=1).count() == 2
    assert data["geocoded"] == FuelStation.objects.filter(latitude__isnull=False).count() == 7
    assert data["not_geocoded"] == 1
    assert data["states"] == 3
    assert data["stations_by_state"] == [
        {"state": "OK", "stations": 6, "geocoded": 6},
        {"state": "AR", "stations": 1, "geocoded": 1},
        {"state": "TX", "stations": 1, "geocoded": 0},
    ]
    assert data["price_per_gallon"] == {"min": 2.4, "median": 3.1, "max": 3.6}  # geocoded stations only
    assert set(data) == {
        "price_file", "stations", "price_quotes", "stations_with_several_quotes", "geocoded", "not_geocoded",
        "states", "stations_by_state", "price_per_gallon",
    }

    vehicle = body["vehicle"]
    config = project_settings.FUEL_PLANNER
    assert vehicle["max_range_miles"] == config["MAX_RANGE_MILES"]
    assert vehicle["miles_per_gallon"] == config["MILES_PER_GALLON"]
    assert vehicle["tank_gallons"] == config["MAX_RANGE_MILES"] / config["MILES_PER_GALLON"]
    assert vehicle["min_stop_gallons"] == config["MIN_STOP_GALLONS"]
    assert vehicle["max_consolidation_cost"] == config["MAX_CONSOLIDATION_COST"]
    assert vehicle["corridor_miles"] == config["CORRIDOR_MILES"]
    assert vehicle["start_reserve_gallons"] == config["START_RESERVE_MILES"] / config["MILES_PER_GALLON"]

    api = body["api"]
    assert set(api) == {"params", "start_tank_modes"}
    settings_names = ["mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal"]
    params = {p["name"]: p for p in api["params"]}
    assert list(params) == ["start", "finish", "start_tank", "include", *settings_names]
    assert all(p["help_text"] for p in params.values())
    assert not any("what-if" in p["help_text"].lower() for p in params.values())
    # Defaults come from the configuration; ranges from the serializer.
    assert params["mpg"]["default"] == config["MILES_PER_GALLON"]
    assert (params["mpg"]["min"], params["mpg"]["max"]) == (3, 30)
    assert params["max_range_miles"]["default"] == config["MAX_RANGE_MILES"]
    assert params["corridor_miles"]["default"] == config["CORRIDOR_MILES"]
    assert params["price_policy"]["default"] == "median"
    assert params["price_policy"]["choices"] == ["median", "min", "max"]
    assert params["consolidate"]["default"] is False and params["safety_reserve_gal"]["default"] == 0
    assert params["start_tank"]["default"] == "empty" and params["start"]["default"] is None
    assert params["include"]["choices"] is None and "'details'" in params["include"]["help_text"]
    assert [m["label"] for m in api["start_tank_modes"]] == [
        "Almost empty: pay for every mile", "Full: the first tank is free",
    ]


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
