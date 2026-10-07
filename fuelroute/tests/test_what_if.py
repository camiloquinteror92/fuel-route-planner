"""Truck settings of /api/route (mpg, max_range_miles, corridor_miles, price_policy,
consolidate, safety_reserve_gal): the plans match a golden file written before they
existed, each one changes the plan the way it should, on the cached route (0 external
calls), and out-of-range values are a clear 400.

The synthetic route of test_api.py: east along latitude 35, about 680 road miles.
"""

import json
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import caches

from fuelroute.models import FuelStation
from fuelroute.services.errors import PlannerError
from fuelroute.services.planner import PlanSettings
from fuelroute.services.stations import reset_station_arrays

from .conftest import osrm_answer
from .test_api import FINISH, OK_ROUTE, ROUTE_MILES, START, add_stations, client, get, mile_of, stations  # noqa: F401

GOLDEN = Path(__file__).parent / "data" / "default_plans.json"
SETTINGS = ("mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal")
SUMMARY_KEYS = (
    "total_fuel_cost", "total_gallons_purchased", "fuel_used_gallons", "start_fuel_gallons", "end_fuel_gallons",
    "unpriced_fuel_gallons", "number_of_stops", "average_price_paid", "candidate_stations_on_route",
)
# What a stop IS in both versions of the answer (the wording and the explanation blocks changed).
STOP_KEYS = (
    "stop", "opis_id", "name", "address", "city", "state", "lat", "lon", "price_per_gallon", "mile_marker",
    "distance_from_route_miles", "fuel_on_arrival_gallons", "gallons", "cost",
)
STRATEGY_STOP_KEYS = ("opis_id", "mile_marker", "price_per_gallon", "fuel_on_arrival_gallons", "gallons", "cost")


def _ok(response) -> dict:
    assert response.status_code == 200, response.json()
    return response.json()


def _get_details(client, **params):
    return get(client, include="details", **params)


def _plan_of(body: dict) -> dict:
    """The plan itself: where to stop, what to buy and pay, and the driver it is compared with."""
    blind = (body["summary"]["comparison"] or {}).get("price_blind")
    return {
        "route": {key: body["route"][key] for key in ("distance_miles", "duration_hours", "miles_outside_usa")},
        "summary": {key: body["summary"][key] for key in SUMMARY_KEYS},
        "price_blind": blind and {key: blind[key] for key in ("total_fuel_cost", "number_of_stops")},
        "stops": [
            {
                **{key: stop[key] for key in STOP_KEYS},
                **{key: stop["decision"][key] for key in ("rule", "consolidated", "reaches", "fills_tank")},
            }
            for stop in body["fuel_stops"]
        ],
        "warnings": body["warnings"],
        "geojson": body["map"]["geojson"],
    }


def _strategy_of(stops: list[dict]) -> list[dict]:
    return [{key: stop[key] for key in STRATEGY_STOP_KEYS} for stop in stops]


# --- the plans of the golden file ---------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "scenario", json.loads(GOLDEN.read_text(encoding="utf-8"))["scenarios"], ids=lambda scenario: scenario["name"]
)
def test_plans_match_the_golden_file(client, upstream, scenario):
    """The golden file was written by the code of the commit before the truck settings
    (generated_from), when merging tiny stops was always on; a scenario marked "revised"
    was regenerated after a deliberate change, explained in the file.

    * With ``consolidate=true`` the plan is the golden plan, stop for stop and cent for cent.
    * The default (``consolidate=false``) is the golden file's "pure optimum before
      consolidation": the three rules alone.
    """
    add_stations([tuple(row) for row in scenario["stations"]])
    upstream.respond((200, osrm_answer([tuple(p) for p in scenario["route"]["points"]], scenario["route"]["miles"])))
    golden = scenario["response"]

    merged = _ok(client.get("/api/route", {**scenario["request"], "consolidate": "true"}))
    assert _plan_of(merged) == _plan_of(golden)

    pure = _ok(client.get("/api/route", scenario["request"]))
    assert pure["meta"]["settings_changed"] == (["start_tank"] if scenario["request"].get("start_tank") else [])
    assert pure["meta"]["external_api_calls"] == 0  # the same road, from the cache
    before = (golden["summary"]["comparison"] or {}).get("optimum_before_consolidation")
    if before is None:
        assert pure["fuel_stops"] == golden["fuel_stops"] == []
    else:
        assert _strategy_of(pure["fuel_stops"]) == _strategy_of(before["stops"])
        assert pure["summary"]["total_fuel_cost"] == before["total_fuel_cost"]
        assert pure["summary"]["number_of_stops"] == before["number_of_stops"]
    assert len(upstream.calls) == 1


@pytest.mark.django_db
def test_settings_equal_to_the_defaults_are_the_default_plan(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    bare = _ok(get(client))
    explicit = _ok(
        get(client, mpg="10", max_range_miles="500.0", corridor_miles=10, price_policy="median",
            consolidate="false", safety_reserve_gal="0")
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
    settings = dict.fromkeys(SETTINGS, blank)
    if blank is None:
        body = _ok(client.post("/api/route", {"start": START, "finish": FINISH, **settings}, format="json"))
    else:
        body = _ok(get(client, **settings))
    assert body["meta"]["settings_changed"] == []
    assert body["vehicle"]["miles_per_gallon"] == 10 and body["vehicle"]["safety_reserve_gal"] == 0


# --- every effective setting in the response ----------------------------------------------------------


@pytest.mark.django_db
def test_vehicle_lists_every_setting_and_meta_the_changed_ones(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = _ok(get(client))
    assert body["vehicle"] == {
        "miles_per_gallon": 10.0, "tank_gallons": 50.0, "max_range_miles": 500.0, "safety_reserve_gal": 0.0,
        "usable_range_miles": 500.0, "start_tank": "empty", "consolidate": False, "corridor_miles": 10.0,
        "price_policy": "median",
    }

    changed = _ok(
        get(client, start_tank="full", mpg=8, max_range_miles=640, corridor_miles=12, price_policy="max",
            consolidate="true", safety_reserve_gal=5)
    )
    assert changed["meta"]["settings_changed"] == ["start_tank", *SETTINGS]
    vehicle = changed["vehicle"]
    assert (vehicle["miles_per_gallon"], vehicle["max_range_miles"], vehicle["corridor_miles"]) == (8, 640, 12)
    assert (vehicle["price_policy"], vehicle["consolidate"], vehicle["safety_reserve_gal"]) == ("max", True, 5)
    assert vehicle["tank_gallons"] == 80 and vehicle["usable_range_miles"] == 600  # 640 - 5 gal x 8 mpg
    # Every changed setting is in vehicle, under the request's name (mpg is miles_per_gallon).
    assert all(name in vehicle for name in changed["meta"]["settings_changed"] if name != "mpg")


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
        {"consolidate": "true"}, {"safety_reserve_gal": 4}, {"start_tank": "full"},
    ]
    for params in variants:
        body = _ok(_get_details(client, **params))
        assert body["meta"]["external_api_calls"] == 0, params
        assert (body["meta"]["route_cache"], body["meta"]["plan_cache"]) == ("hit", "miss"), params
        assert body["details"]["pipeline"]["routing"]["from_route_cache"] is True
        again = _ok(get(client, **params))
        assert again["meta"]["plan_cache"] == "hit", params
    assert len(upstream.calls) == 1  # one routing call for the whole session


@pytest.mark.django_db
def test_each_setting_changes_the_plan_with_no_external_call(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    base = _ok(_get_details(client))
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
    wide = _ok(_get_details(client, corridor_miles=45))
    assert wide["summary"]["candidate_stations_on_route"] == base["summary"]["candidate_stations_on_route"] + 1
    assert 6 in [s["opis_id"] for s in wide["fuel_stops"]]
    assert wide["summary"]["total_fuel_cost"] < base["summary"]["total_fuel_cost"]
    assert wide["details"]["pipeline"]["corridor"]["corridor_miles"] == 45

    # consolidate=true: tiny stops merged into a neighbour, at most a dollar more each.
    assert not any("consolidated" in s["decision"] for s in stops)  # the default: the three rules alone
    merged = _ok(_get_details(client, consolidate="true"))
    before = merged["details"]["comparison"]["optimum_before_consolidation"]
    assert [s["opis_id"] for s in stops] == [s["opis_id"] for s in before["stops"]]
    assert base["summary"]["total_fuel_cost"] == before["total_fuel_cost"] <= merged["summary"]["total_fuel_cost"]
    assert merged["summary"]["number_of_stops"] <= base["summary"]["number_of_stops"]
    assert any(s["decision"]["consolidated"] for s in merged["fuel_stops"])
    assert merged["details"]["pipeline"]["optimizer"]["consolidate"] is True
    assert merged["summary"]["tiny_stops_merged"]["stops_before"] == base["summary"]["number_of_stops"]
    assert "tiny stops merged" in merged["details"]["comparison"]["optimized"]["label"]
    assert "the three rules" in base["details"]["comparison"]["optimized"]["label"]


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
        body = _ok(_get_details(client, price_policy=policy))
        for stop in body["fuel_stops"]:
            assert Decimal(str(stop["price_per_gallon"])) == quotes[stop["opis_id"]][policy], (policy, stop)
        assert body["summary"]["price_per_gallon_on_route"]["min"] == float(min(q[policy] for q in quotes.values()))
        corridor = body["details"]["pipeline"]["corridor"]
        assert corridor["price_policy"] == policy
        # Only the stations whose quotes disagree can change price with the policy.
        assert corridor["stations_with_several_prices"] == 4
        costs[policy] = body["summary"]["total_fuel_cost"]
    assert costs["min"] < costs["median"] < costs["max"]
    assert len(upstream.calls) == 1


# --- the safety reserve -----------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("tank", ["empty", "full"])
def test_safety_reserve_is_kept_at_every_stop_and_at_the_destination(client, upstream, stations, tank):
    upstream.respond(OK_ROUTE)
    base = _ok(_get_details(client, start_tank=tank))
    assert base["details"]["pipeline"]["tank"]["lowest_fuel_gallons"] < 8  # without it, the plan goes lower
    body = _ok(_get_details(client, start_tank=tank, safety_reserve_gal=8))
    assert body["meta"]["external_api_calls"] == 0
    for stop in body["fuel_stops"]:
        assert stop["fuel_on_arrival_gallons"] >= 8 - 0.005, stop
    summary, tank_info = body["summary"], body["details"]["pipeline"]["tank"]
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
        strategy = body["details"]["comparison"][key]
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
    assert PlanSettings.defaults().consolidate is False  # the three rules alone, unless asked
    with pytest.raises(PlannerError):
        PlanSettings.resolve(safety_reserve_gal=50)  # = the default tank
    with pytest.raises(PlannerError):
        PlanSettings.resolve(price_policy="average")
    with pytest.raises(TypeError):
        PlanSettings.resolve(colour="red")
    for bad in ({"mpg": "ten"}, {"mpg": float("nan")}, {"corridor_miles": float("inf")}, {"max_range_miles": 0}):
        with pytest.raises(PlannerError):
            PlanSettings.resolve(**bad)
    assert PlanSettings.resolve(consolidate="true").consolidate is True  # text, as in a query string
    assert PlanSettings.resolve(consolidate="FALSE").changed() == []
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


# --- the last stretch, and the words and rounding of a plan -------------------------------------------


@pytest.mark.django_db
def test_a_last_stretch_just_over_the_reserve_still_pays_every_mile(client, upstream, stations):
    """Regression ("can only arrive with 5.0 gal instead of 5.0: 0.0 gal ... not priced"): with a
    short range the last station is so far that the truck can arrive with a bit less than the
    reserve. It now LEAVES with that much, so the fuel bought is still the fuel burned."""
    upstream.respond(OK_ROUTE)
    body = _ok(get(client, include="candidates"))
    fields, rows = body["candidates"]["fields"], body["candidates"]["rows"]
    gap = body["route"]["distance_miles"] - max(row[fields.index("mile_marker")] for row in rows)
    max_range = round(gap + 49.6, 1)  # a full tank at the last station arrives with ~49.6 of the 50 miles
    body = _ok(_get_details(client, max_range_miles=max_range))
    summary, tank = body["summary"], body["details"]["pipeline"]["tank"]
    assert body["meta"]["external_api_calls"] == 0
    assert tank["reason"] == "last_stretch" and tank["arrival_capped"] is False
    assert summary["unpriced_fuel_gallons"] == 0 and body["warnings"] == []
    assert summary["start_fuel_gallons"] == summary["end_fuel_gallons"]
    assert summary["start_fuel_gallons"] == pytest.approx((max_range - gap) / 10, abs=0.02)
    assert summary["total_gallons_purchased"] == pytest.approx(summary["fuel_used_gallons"], abs=0.02)
    assert "less than the usual 50 miles" in summary["note"]


@pytest.mark.django_db
def test_an_unpriced_last_stretch_names_two_different_amounts(client, upstream):
    # Last station ~482 miles before the destination: even leaving with just enough to reach
    # the first station, the truck arrives with less.
    add_stations([(1, 35.02, -99.3, "3.00"), (2, 35.02, -96.5, "3.00")])
    upstream.respond(OK_ROUTE)
    body = _ok(_get_details(client))
    (warning,) = [text for text in body["warnings"] if "last station" in text]
    found = re.search(r"arrive with ([\d.]+) gal instead of ([\d.]+): ([\d.]+) gal", warning)
    arrive, left, unpriced = map(float, found.groups())
    assert arrive < left and unpriced == pytest.approx(left - arrive, abs=0.011)
    assert unpriced == pytest.approx(body["summary"]["unpriced_fuel_gallons"], abs=0.011)
    assert body["details"]["pipeline"]["tank"]["arrival_capped"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("mpg", ["7", "8", "9.5", "11"])
def test_rounded_arrival_plus_purchase_never_shows_a_tank_above_full(client, upstream, stations, mpg):
    """Regression: at 8 mpg a stop arrived with 15.38 gal and bought 47.13 in a 62.5-gal tank."""
    upstream.respond(OK_ROUTE)
    body = _ok(get(client, mpg=mpg))
    tank = body["vehicle"]["tank_gallons"]
    assert tank == round(500 / float(mpg), 2)
    for stop in body["fuel_stops"]:
        assert round(stop["fuel_on_arrival_gallons"] + stop["gallons"], 2) <= tank, stop


@pytest.mark.django_db
def test_the_full_tank_note_names_the_real_tank(client, upstream, stations):
    """Regression: "a full tank (62 gal)" for a 62.5-gal tank."""
    upstream.respond(OK_ROUTE)
    summary = _ok(get(client, start_tank="full", mpg=8))["summary"]
    assert "a full tank (62.5 gal)" in summary["note"]
    assert summary["start_fuel_gallons"] == 62.5


@pytest.mark.django_db
def test_almost_no_usable_range_is_said_with_a_decimal(client, upstream, stations):
    """Regression: "more than the 0-mile usable range" for a reserve that almost fills the tank."""
    upstream.respond(OK_ROUTE)
    _ok(get(client))
    response = get(client, safety_reserve_gal="49.99")
    assert response.status_code == 422
    assert "0.1-mile usable range" in response.json()["detail"]
    assert response.json()["meta"]["external_api_calls"] == 0


@pytest.mark.django_db
def test_a_flood_of_plans_does_not_evict_the_route(client, upstream, stations):
    """Plans and routes live in separate caches: truck changes (one plan each) can fill the plan
    cache without evicting the route they are planned on, so a truck change stays free."""
    upstream.respond(OK_ROUTE)
    _ok(get(client))
    plans = caches["default"]
    for i in range(plans._max_entries * 2):
        plans.set(f"filler:{i}", i)
    body = _ok(get(client, mpg="9.1"))
    assert body["meta"]["route_cache"] == "hit" and body["meta"]["external_api_calls"] == 0
    assert len(upstream.calls) == 1
