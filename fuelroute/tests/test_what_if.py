"""What-if settings of /api/route (mpg, max_range_miles, corridor_miles, price_policy,
consolidate, safety_reserve_gal): the defaults plan exactly as before they existed,
each one changes the plan the way it should, on the cached route (0 external calls),
and out-of-range values are a clear 400.

The synthetic route of test_api.py: east along latitude 35, about 680 road miles.
"""

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from fuelroute.models import FuelStation
from fuelroute.services.errors import PlannerError
from fuelroute.services.planner import PlanSettings
from fuelroute.services.stations import reset_station_arrays

from .conftest import osrm_answer
from .test_api import FINISH, OK_ROUTE, ROUTE_MILES, START, add_stations, client, get, mile_of, stations  # noqa: F401

GOLDEN = Path(__file__).parent / "data" / "default_plans.json"
WHAT_IF = ("mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal")
# Keys added to the response by the what-if settings (everything else must be unchanged).
NEW_VEHICLE_KEYS = {
    "start_tank_label", "start_tank_help", "mpg", "corridor_miles", "price_policy", "consolidate",
    "safety_reserve_gal", "usable_range_miles",
}


def _ok(response) -> dict:
    assert response.status_code == 200, response.json()
    return response.json()


def _without_new_keys(body: dict) -> dict:
    """The response as it was before the what-if settings: new keys, timings and the data
    version (row ids of the test database) removed."""
    body = json.loads(json.dumps(body))
    for key in ("timings_ms", "external_api_ms", "settings_changed", "station_data_version"):
        body["meta"].pop(key, None)
    for key in ("timings_ms", "external_api_ms", "computed_at"):
        body["pipeline"].pop(key, None)
    for key in NEW_VEHICLE_KEYS:
        body["vehicle"].pop(key)
    body["pipeline"]["tank"].pop("safety_reserve_gallons")
    body["pipeline"]["tank"].pop("lowest_fuel_gallons")
    body["pipeline"]["corridor"].pop("price_policy")
    body["pipeline"]["optimizer"].pop("consolidate")
    return body


# --- defaults: identical to the API before the what-if settings ------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scenario", json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"], ids=lambda scenario: scenario["name"]
)
def test_default_plans_are_identical_to_the_ones_before_what_if_settings(client, upstream, scenario):
    """The golden file was written by the code of the commit before this feature (generated_from)."""
    add_stations([tuple(row) for row in scenario["stations"]])
    upstream.respond((200, osrm_answer([tuple(p) for p in scenario["route"]["points"]], scenario["route"]["miles"])))
    body = _ok(client.get("/api/route", scenario["request"]))
    assert body["meta"]["settings_changed"] == (["start_tank"] if scenario["request"].get("start_tank") else [])
    golden = dict(scenario["response"], meta=dict(scenario["response"]["meta"]))
    golden["meta"].pop("station_data_version")
    assert _without_new_keys(body) == golden


@pytest.mark.django_db
def test_settings_equal_to_the_defaults_are_the_default_plan(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    bare = _ok(get(client))
    explicit = _ok(
        get(client, mpg="10", max_range_miles="500.0", corridor_miles=10, price_policy="median",
            consolidate="true", safety_reserve_gal="0")
    )
    assert explicit["meta"]["plan_cache"] == "hit"  # the same plan cache key
    assert explicit["meta"]["settings_changed"] == bare["meta"]["settings_changed"] == []
    assert explicit["fuel_stops"] == bare["fuel_stops"]
    assert explicit["map"]["map_url"] == bare["map"]["map_url"]
    assert len(upstream.calls) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("blank", ["", None])
def test_blank_or_null_settings_mean_the_default(client, upstream, stations, blank):
    upstream.respond(OK_ROUTE)
    settings = dict.fromkeys(WHAT_IF, blank)
    if blank is None:
        body = _ok(client.post("/api/route", {"start": START, "finish": FINISH, **settings}, format="json"))
    else:
        body = _ok(get(client, **settings))
    assert body["meta"]["settings_changed"] == []
    assert body["vehicle"]["mpg"] == 10 and body["vehicle"]["safety_reserve_gal"] == 0


# --- every effective setting in the response ----------------------------------------------------------


@pytest.mark.django_db
def test_vehicle_lists_every_setting_and_meta_the_changed_ones(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = _ok(get(client))
    assert body["vehicle"] == {
        "max_range_miles": 500.0, "miles_per_gallon": 10.0, "tank_gallons": 50.0,
        "start_tank": "empty", "start_tank_label": "Pay for every mile",
        "start_tank_help": body["vehicle"]["start_tank_help"],
        "mpg": 10.0, "corridor_miles": 10.0, "price_policy": "median", "consolidate": True,
        "safety_reserve_gal": 0.0, "usable_range_miles": 500.0,
    }
    assert body["vehicle"]["start_tank_help"].endswith(".")

    changed = _ok(
        get(client, start_tank="full", mpg=8, max_range_miles=640, corridor_miles=12, price_policy="max",
            consolidate="false", safety_reserve_gal=5)
    )
    assert changed["meta"]["settings_changed"] == ["start_tank", *WHAT_IF]
    vehicle = changed["vehicle"]
    assert (vehicle["mpg"], vehicle["max_range_miles"], vehicle["corridor_miles"]) == (8, 640, 12)
    assert (vehicle["price_policy"], vehicle["consolidate"], vehicle["safety_reserve_gal"]) == ("max", False, 5)
    assert vehicle["tank_gallons"] == 80 and vehicle["usable_range_miles"] == 600  # 640 - 5 gal x 8 mpg
    assert vehicle["start_tank_label"] == "Start with a full tank"
    assert all(name in vehicle for name in changed["meta"]["settings_changed"])


@pytest.mark.django_db
def test_map_url_carries_only_the_changed_settings(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    default = parse_qs(urlsplit(_ok(get(client))["map"]["map_url"]).query)
    assert set(default) == {"start", "finish", "start_tank"}
    changed = _ok(get(client, mpg=6.5, safety_reserve_gal=2))
    query = parse_qs(urlsplit(changed["map"]["map_url"]).query)
    assert query["mpg"] == ["6.5"] and query["safety_reserve_gal"] == ["2"]
    assert "corridor_miles" not in query and "consolidate" not in query


# --- each setting changes the plan, on the cached route ---------------------------------------------


@pytest.mark.django_db
def test_settings_are_in_the_plan_cache_key_not_the_route_cache_key(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    first = _ok(get(client))
    assert first["meta"]["external_api_calls"] == 1
    variants = [
        {"mpg": 7}, {"max_range_miles": 350}, {"corridor_miles": 45}, {"price_policy": "min"},
        {"consolidate": "false"}, {"safety_reserve_gal": 4}, {"start_tank": "full"},
    ]
    for params in variants:
        body = _ok(get(client, **params))
        assert body["meta"]["external_api_calls"] == 0, params
        assert (body["meta"]["route_cache"], body["meta"]["plan_cache"]) == ("hit", "miss"), params
        assert body["pipeline"]["routing"]["from_route_cache"] is True
        again = _ok(get(client, **params))
        assert again["meta"]["plan_cache"] == "hit", params
    assert len(upstream.calls) == 1  # one routing call for the whole session


@pytest.mark.django_db
def test_each_setting_changes_the_plan_with_no_external_call(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    base = _ok(get(client))
    stops = base["fuel_stops"]

    # mpg: every mile is still paid for, at the new consumption.
    thirsty = _ok(get(client, mpg=5))
    assert thirsty["summary"]["total_gallons_purchased"] == pytest.approx(ROUTE_MILES / 5, abs=0.05)
    assert thirsty["summary"]["total_fuel_cost"] > base["summary"]["total_fuel_cost"]
    assert thirsty["vehicle"]["tank_gallons"] == 100

    # max_range_miles: a smaller tank never holds more than it can, nor goes further.
    short = _ok(get(client, max_range_miles=180))
    for stop in short["fuel_stops"]:
        assert stop["fuel_on_arrival_gallons"] + stop["gallons"] <= 18 + 0.01
    miles = [0.0] + [s["mile_marker"] for s in short["fuel_stops"]] + [short["route"]["distance_miles"]]
    assert max(b - a for a, b in zip(miles, miles[1:])) <= 180 + 1

    # corridor_miles: the cheap station 40 miles off the route becomes a candidate and is used.
    assert 6 not in [s["opis_id"] for s in stops]
    wide = _ok(get(client, corridor_miles=45))
    assert wide["summary"]["candidate_stations_on_route"] == base["summary"]["candidate_stations_on_route"] + 1
    assert 6 in [s["opis_id"] for s in wide["fuel_stops"]]
    assert wide["summary"]["total_fuel_cost"] < base["summary"]["total_fuel_cost"]
    assert wide["pipeline"]["corridor"]["corridor_miles"] == 45

    # consolidate=false: the pure optimum, small stops included, never dearer.
    pure = _ok(get(client, consolidate="false"))
    before = base["summary"]["comparison"]["optimum_before_consolidation"]
    assert [s["opis_id"] for s in pure["fuel_stops"]] == [s["opis_id"] for s in before["stops"]]
    assert pure["summary"]["total_fuel_cost"] == before["total_fuel_cost"] <= base["summary"]["total_fuel_cost"]
    assert not any(s["decision"]["consolidated"] for s in pure["fuel_stops"])
    assert pure["pipeline"]["optimizer"]["consolidate"] is False
    assert "no consolidation" in pure["summary"]["comparison"]["optimized"]["label"]


@pytest.mark.django_db
def test_price_policy_uses_the_cheapest_or_dearest_quote(client, upstream):
    rows = [
        (1, 35.02, -99.3, "3.50", "3.10", "3.90"),
        (2, 35.02, -97.5, "2.90", "2.80", "3.95"),  # cheapest median, dearest worst case
        (3, 35.02, -95.0, "3.20", "2.60", "3.30"),  # cheapest best case
        (4, 35.02, -93.0, "3.10", "3.10", "3.10"),
        (5, 35.02, -90.5, "3.30", "3.00", "3.35"),
    ]
    FuelStation.objects.bulk_create(
        FuelStation(
            opis_id=i, name=f"STOP {i}", address="I-40", city="Town", state="OK", price=Decimal(p),
            price_min=Decimal(low), price_max=Decimal(high), price_rows=3, latitude=lat, longitude=lon,
            geocode_source="census",
        )
        for i, lat, lon, p, low, high in rows
    )
    upstream.respond(OK_ROUTE)
    quotes = {i: {"median": Decimal(p), "min": Decimal(low), "max": Decimal(high)} for i, _, _, p, low, high in rows}
    costs = {}
    for policy in ("median", "min", "max"):
        body = _ok(get(client, price_policy=policy))
        for stop in body["fuel_stops"]:
            assert Decimal(str(stop["price_per_gallon"])) == quotes[stop["opis_id"]][policy], (policy, stop)
        prices = body["pipeline"]["corridor"]["price_per_gallon"]
        assert prices["min"] == float(min(q[policy] for q in quotes.values()))
        assert body["pipeline"]["corridor"]["price_policy"] == policy
        costs[policy] = body["summary"]["total_fuel_cost"]
    assert costs["min"] < costs["median"] < costs["max"]
    assert len(upstream.calls) == 1


# --- the safety reserve -----------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("tank", ["empty", "full"])
def test_safety_reserve_is_kept_at_every_stop_and_at_the_destination(client, upstream, stations, tank):
    upstream.respond(OK_ROUTE)
    base = _ok(get(client, start_tank=tank))
    assert base["pipeline"]["tank"]["lowest_fuel_gallons"] < 8  # without it, the plan goes lower
    body = _ok(get(client, start_tank=tank, safety_reserve_gal=8))
    assert body["meta"]["external_api_calls"] == 0
    for stop in body["fuel_stops"]:
        assert stop["fuel_on_arrival_gallons"] >= 8 - 0.005, stop
    summary, tank_info = body["summary"], body["pipeline"]["tank"]
    assert summary["end_fuel_gallons"] >= 8 - 0.005
    assert tank_info["safety_reserve_gallons"] == 8 and tank_info["lowest_fuel_gallons"] >= 8 - 0.005
    assert any("safety reserve" in text for text in (summary["note"],))
    if tank == "empty":
        # Still pays for every mile: it leaves with the reserve plus the miles to the first station.
        assert summary["total_gallons_purchased"] == pytest.approx(ROUTE_MILES / 10, abs=0.05)
        assert summary["start_fuel_gallons"] == pytest.approx(8 + mile_of(-99.3) / 10, abs=0.1)
        assert tank_info["reason"] == "safety_reserve"
    else:
        # Leaves full, buys only what reaches the destination with the reserve.
        assert summary["total_gallons_purchased"] == pytest.approx((ROUTE_MILES - 500) / 10 + 8, abs=0.05)
    # The simple drivers it is compared with keep the same reserve.
    for key in ("price_blind", "quarter_tank"):
        strategy = body["summary"]["comparison"][key]
        if strategy:
            assert all(s["fuel_on_arrival_gallons"] >= 8 - 0.005 for s in strategy["stops"])


@pytest.mark.django_db
def test_safety_reserve_that_cannot_be_kept_is_422_and_says_why(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    _ok(get(client))
    # 40 gal of reserve leave 100 usable miles: the stations of this route are ~140 miles apart.
    response = get(client, safety_reserve_gal=40)
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "no_reachable_fuel_station"
    assert "100-mile usable range" in body["detail"] and "40-gal safety reserve" in body["detail"]
    assert body["meta"]["external_api_calls"] == 0  # the route was cached


@pytest.mark.django_db
def test_safety_reserve_on_a_route_without_stations_is_422(client, upstream):
    add_stations([(1, 40.0, -80.0, "3.00")])  # data loaded, none near this 27-mile trip
    upstream.respond((200, osrm_answer([(35.0, -100.0 + i * 0.05) for i in range(9)], 27)))
    ok = client.get("/api/route", {"start": START, "finish": "35.0,-99.6"})
    assert ok.status_code == 200  # the 50-mile reserve covers it
    response = client.get("/api/route", {"start": START, "finish": "35.0,-99.6", "safety_reserve_gal": 3})
    assert response.status_code == 422  # 50 - 27 = 23 miles left: less than 3 gal
    assert response.json()["error"] == "no_fuel_data_on_route"


# --- validation ---------------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "field", "message"),
    [
        ({"mpg": "2.9"}, "mpg", "Must be between 3 and 30."),
        ({"mpg": "31"}, "mpg", "Must be between 3 and 30."),
        ({"mpg": "nan"}, "mpg", "A valid number is required."),
        ({"mpg": "ten"}, "mpg", "A valid number is required."),
        ({"max_range_miles": "99"}, "max_range_miles", "Must be between 100 and 1500."),
        ({"max_range_miles": "1e9"}, "max_range_miles", "Must be between 100 and 1500."),
        ({"corridor_miles": "0.5"}, "corridor_miles", "Must be between 1 and 50."),
        ({"corridor_miles": "51"}, "corridor_miles", "Must be between 1 and 50."),
        ({"price_policy": "average"}, "price_policy", None),
        ({"consolidate": "maybe"}, "consolidate", None),
        ({"safety_reserve_gal": "-1"}, "safety_reserve_gal", "Must be at least 0."),
        ({"safety_reserve_gal": "50"}, "safety_reserve_gal", "Must be less than the tank: 50 gal"),
        ({"safety_reserve_gal": "15", "max_range_miles": "150"}, "safety_reserve_gal", "Must be less than the tank: 15 gal"),
        ({"safety_reserve_gal": "20", "mpg": "25"}, "safety_reserve_gal", "Must be less than the tank: 20 gal"),
    ],
)
def test_out_of_range_settings_are_400(client, upstream, stations, params, field, message):
    response = get(client, **params)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert field in body["detail"], body
    if message:
        assert body["detail"][field][0].startswith(message), body["detail"]
    assert body["meta"]["external_api_calls"] == 0
    assert upstream.calls == []


def test_plan_settings_guard_the_planner_too(settings):
    assert PlanSettings.resolve().changed() == []
    assert PlanSettings.resolve(mpg=None, consolidate=None).changed() == []
    with pytest.raises(PlannerError):
        PlanSettings.resolve(safety_reserve_gal=50)  # = the default tank
    with pytest.raises(PlannerError):
        PlanSettings.resolve(price_policy="average")
    with pytest.raises(TypeError):
        PlanSettings.resolve(colour="red")
    for bad in ({"mpg": "ten"}, {"mpg": float("nan")}, {"corridor_miles": float("inf")}, {"max_range_miles": 0}):
        with pytest.raises(PlannerError):
            PlanSettings.resolve(**bad)
    assert PlanSettings.resolve(consolidate="false").consolidate is False  # text, as in a query string
    assert PlanSettings.resolve(consolidate="TRUE").changed() == []
    # Defaults follow the configuration (environment variables of the README).
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "MILES_PER_GALLON": 8.0}
    assert PlanSettings.defaults().mpg == 8.0 and PlanSettings.resolve(mpg=8).changed() == []


@pytest.mark.django_db
def test_new_station_prices_are_used_by_every_policy(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    _ok(get(client, price_policy="max"))
    FuelStation.objects.filter(opis_id=2).update(price_max=Decimal("9.99"))
    FuelStation.objects.create(  # a reload changes the data version (count, last id)
        opis_id=99, name="NEW", address="", city="Far", state="ME", price=Decimal("3"), price_min=Decimal("3"),
        price_max=Decimal("3"), latitude=45.0, longitude=-69.0,
    )
    reset_station_arrays()
    body = _ok(get(client, price_policy="max"))
    assert body["meta"]["plan_cache"] == "miss"
    assert 2 not in [s["opis_id"] for s in body["fuel_stops"]]  # now the dearest station of the route
