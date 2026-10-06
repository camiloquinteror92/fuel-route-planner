"""The HTTP API end to end, with the network faked (see conftest.Upstream).

The synthetic route runs east along latitude 35 from (35, -100) to (35, -88):
about 680 road miles, 1 degree of longitude ~ 56.7 miles.
"""

import gzip
import json
import threading
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
import urllib3
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from fuelroute.models import FuelStation
from fuelroute.renderers import TimedJSONRenderer
from fuelroute.services.about import error_catalog
from fuelroute.services.errors import PlannerError
from fuelroute.services.geocoding import Location
from fuelroute.services.http import ExternalApiClient
from fuelroute.services.osrm import get_route

from .conftest import encode_polyline, osrm_answer

START = "35.0,-100.0"
FINISH = "35.0,-88.0"
ROUTE_MILES = 680
LINE = [(35.0, -100.0 + i * 0.05) for i in range(241)]
OK_ROUTE = (200, osrm_answer(LINE, ROUTE_MILES))


def mile_of(lon: float) -> float:
    return (lon + 100.0) / 12.0 * ROUTE_MILES


def add_stations(rows):
    """rows: (opis_id, lat, lon, price as str)."""
    FuelStation.objects.bulk_create(
        FuelStation(
            opis_id=opis_id, name=f"STOP {opis_id}", address="I-40", city="Town", state="OK",
            price=Decimal(price), price_min=Decimal(price), price_max=Decimal(price),
            latitude=lat, longitude=lon, geocode_source="census",
        )
        for opis_id, lat, lon, price in rows
    )


@pytest.fixture
def stations(db):
    add_stations(
        [
            (1, 35.02, -99.3, "3.50"),  # ~40 miles in: reachable on the 50-mile reserve
            (2, 35.02, -97.5, "2.90"),  # cheap
            (3, 35.02, -95.0, "3.60"),
            (4, 35.02, -93.0, "3.10"),
            (5, 35.02, -90.5, "3.30"),
            (6, 35.60, -96.0, "2.50"),  # very cheap but 40 miles off the route -> ignored
        ]
    )


@pytest.fixture
def client():
    return APIClient()


def get(client, **params):
    return client.get("/api/route", {"start": START, "finish": FINISH, **params})


# --- the happy path ---------------------------------------------------------------------


@pytest.mark.django_db
def test_route_returns_plan_map_and_uses_one_external_call(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    response = get(client)
    assert response.status_code == 200, response.json()
    body = response.json()

    assert len(upstream.calls) == 1
    assert "router.project-osrm.org/route/v1/driving/-100.000000,35.000000;-88.000000,35.000000" in upstream.calls[0]
    assert upstream.params[0]["geometries"] == "polyline6"
    assert body["meta"]["external_api_calls"] == 1
    assert body["route"]["distance_miles"] == pytest.approx(ROUTE_MILES, abs=0.5)

    stops = body["fuel_stops"]
    assert stops, "a 680-mile trip on a reserve needs fuel"
    assert 6 not in [s["opis_id"] for s in stops]  # outside the corridor
    assert stops[0]["opis_id"] == 1  # nearest station, just enough to reach the cheap one
    assert any(s["opis_id"] == 2 for s in stops)
    summary = body["summary"]
    assert summary["total_gallons_purchased"] == pytest.approx(ROUTE_MILES / 10, abs=0.05)  # every mile paid
    assert summary["unpriced_fuel_gallons"] == pytest.approx(0, abs=0.01)
    assert body["warnings"] == []

    features = body["map"]["geojson"]["features"]
    assert features[0]["geometry"]["type"] == "LineString"
    assert [f["properties"]["kind"] for f in features].count("fuel_stop") == len(stops)
    assert body["map"]["map_url"].startswith("http://testserver/api/route/map?")
    assert "X-Response-Time-ms" in response.headers


@pytest.mark.django_db
def test_stop_costs_add_up_to_the_total_to_the_cent(client, upstream):
    # Prices with 4 decimals (the file has up to 8): what is shown must add up.
    add_stations([(1, 35.02, -99.4, "3.2823"), (2, 35.02, -97.0, "2.8571"), (3, 35.02, -94.1, "3.0733"),
                  (4, 35.02, -91.3, "2.9467")])
    upstream.respond(OK_ROUTE)
    body = get(client).json()
    stops = body["fuel_stops"]
    assert len(stops) >= 2
    for stop in stops:
        shown = Decimal(str(stop["gallons"])) * Decimal(str(stop["price_per_gallon"]))
        assert Decimal(str(stop["cost"])) == shown.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert sum(Decimal(str(s["cost"])) for s in stops) == Decimal(str(body["summary"]["total_fuel_cost"]))
    assert sum(Decimal(str(s["gallons"])) for s in stops) == Decimal(str(body["summary"]["total_gallons_purchased"]))


@pytest.mark.django_db
def test_second_request_uses_the_plan_cache(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    get(client)
    second = client.get("/api/route", {"start": " 35.0 , -100.0 ", "finish": FINISH}).json()

    assert len(upstream.calls) == 1
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["plan_cache"] == "hit"
    # A cache hit answers with the text of THIS request, not the first one.
    assert second["start"]["query"] == "35.0 , -100.0"
    assert "35.0+%2C+-100.0" in second["map"]["map_url"]


# --- explaining the plan: decisions, comparison, pipeline, candidates, timings ----------------


def _dec(value) -> Decimal:
    return Decimal(str(value))


def _server_timing(response) -> dict:
    """Parse the Server-Timing header: {name: {"dur": float, "desc": str}}, in order."""
    entries = {}
    for item in response["Server-Timing"].split(", "):
        name, *params = item.split(";")
        values = dict(param.split("=", 1) for param in params)
        entries[name] = {"dur": float(values["dur"]), "desc": values.get("desc", "").strip('"')}
    return entries


@pytest.mark.django_db
def test_response_explains_the_pipeline_and_compares_strategies(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    body = get(client).json()
    summary, stops = body["summary"], body["fuel_stops"]

    # Every stop says why it is there: the first one buys just enough to reach the cheap one.
    assert all(s["decision"]["rule"] in ("reach_cheaper", "fill_up", "finish") for s in stops)
    assert stops[0]["decision"]["rule"] == "reach_cheaper"
    assert stops[0]["decision"]["cheaper_station_mile"] == stops[1]["mile_marker"]
    assert any(s["decision"]["consolidated"] for s in stops)  # this trip has stops under the minimum
    # ...and what its fuel covers in the FINAL plan: the next stop or the destination,
    # with the fuel consolidation moved here or away (regression: a consolidated stop
    # was explained with a station that was no longer in the plan).
    for n, stop in enumerate(stops):
        decision = stop["decision"]
        following = stops[n + 1] if n + 1 < len(stops) else None
        assert decision["reaches"] == {
            "stop": following["stop"] if following else None,
            "mile": following["mile_marker"] if following else body["route"]["distance_miles"],
        }
        moved_here = sum(m["gallons"] for m in decision["moved_in"])
        moved_away = sum(m["gallons"] for m in decision["moved_out"])
        assert stop["gallons"] == pytest.approx(decision["greedy_gallons"] + moved_here - moved_away, abs=0.02)
        if decision["moved_in"] or decision["moved_out"]:
            assert decision["consolidated"]
        for moved in decision["moved_in"]:
            if moved["from_stop"] is not None:
                assert stops[moved["from_stop"] - 1]["mile_marker"] == moved["mile"]
        tank = (stop["fuel_on_arrival_gallons"] + stop["gallons"]) >= body["vehicle"]["tank_gallons"] - 0.01
        assert decision["fills_tank"] == tank

    comparison = summary["comparison"]
    optimized = comparison["optimized"]
    assert "stops" not in optimized  # the plan's stops are fuel_stops
    assert _dec(optimized["total_fuel_cost"]) == _dec(summary["total_fuel_cost"])
    assert optimized["total_gallons_purchased"] == summary["total_gallons_purchased"]
    assert optimized["number_of_stops"] == summary["number_of_stops"]

    for key in ("optimum_before_consolidation", "price_blind", "quarter_tank"):
        strategy = comparison[key]
        assert strategy["label"] and strategy["rule"]
        assert strategy["number_of_stops"] == len(strategy["stops"])
        assert sum(_dec(s["cost"]) for s in strategy["stops"]) == _dec(strategy["total_fuel_cost"])
        for stop in strategy["stops"]:
            assert _dec(stop["cost"]) == (_dec(stop["gallons"]) * _dec(stop["price_per_gallon"])).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        # Same stations, same start / end fuel: the same gallons (up to each stop's rounding).
        assert strategy["total_gallons_purchased"] == pytest.approx(
            summary["total_gallons_purchased"], abs=0.005 * (len(strategy["stops"]) + len(stops)) + 1e-9
        )
    before, blind = comparison["optimum_before_consolidation"], comparison["price_blind"]
    assert _dec(before["total_fuel_cost"]) <= _dec(blind["total_fuel_cost"])  # the optimum is optimal
    assert before["stops"][0]["opis_id"] == 1 and (before["stops"][0]["lat"], before["stops"][0]["lon"]) == (35.02, -99.3)

    savings = comparison["savings_vs_price_blind"]
    amount = _dec(blind["total_fuel_cost"]) - _dec(summary["total_fuel_cost"])
    assert _dec(savings["amount"]) == amount > 0
    assert _dec(savings["percent"]) == (amount / _dec(blind["total_fuel_cost"]) * 100).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    assert savings["extra_stops"] == summary["number_of_stops"] - blind["number_of_stops"]

    average = comparison["corridor_average"]
    prices = [Decimal(p) for p in ("3.50", "2.90", "3.60", "3.10", "3.30")]  # station 6 is off the corridor
    assert _dec(average["price_per_gallon"]) == sum(prices) / len(prices)
    assert average["stations"] == summary["candidate_stations_on_route"] == len(prices)
    assert _dec(average["total_fuel_cost"]) == (
        _dec(summary["total_gallons_purchased"]) * _dec(average["price_per_gallon"])
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert _dec(comparison["savings_vs_corridor_average"]["amount"]) == (
        _dec(average["total_fuel_cost"]) - _dec(summary["total_fuel_cost"])
    )

    pipeline = body["pipeline"]
    assert pipeline["external_api_calls"] == 1 and pipeline["external_api_services"] == ["osrm"]
    routing = pipeline["routing"]
    assert routing["polyline_chars"] == len(encode_polyline(LINE))
    assert routing["geometry_points"] == len(LINE)
    assert routing["samples"] > routing["map_points"] >= 2
    assert routing["sample_spacing_miles"] == pytest.approx(1.0, abs=0.01)
    assert routing["from_route_cache"] is False
    corridor = pipeline["corridor"]
    assert corridor["stations_searched"] == 6
    assert corridor["candidates"] == summary["candidate_stations_on_route"]
    assert corridor["within_corridor"] - corridor["dropped_outside_usa"] == corridor["candidates"]
    assert corridor["price_per_gallon"] == {"min": 2.9, "median": 3.3, "max": 3.6, "mean": 3.28}
    assert pipeline["tank"] == {
        "start_fuel_gallons": 5.0, "required_end_fuel_gallons": 5.0, "reason": "reserve", "arrival_capped": False,
        "safety_reserve_gallons": 0.0,
        "lowest_fuel_gallons": min([s["fuel_on_arrival_gallons"] for s in stops] + [summary["end_fuel_gallons"]]),
    }
    optimizer = pipeline["optimizer"]
    assert optimizer["candidates"] == corridor["candidates"]
    assert optimizer["stops"] == summary["number_of_stops"]
    assert optimizer["stops_before_consolidation"] == before["number_of_stops"]
    assert _dec(optimizer["cost_before_consolidation"]) == _dec(before["total_fuel_cost"])
    assert _dec(optimizer["consolidation_extra_cost"]) == _dec(summary["total_fuel_cost"]) - _dec(
        before["total_fuel_cost"]
    )
    assert optimizer["consolidation_extra_cost"] >= 0
    # Per stop, the extra cost of the fuel moved there adds up to the consolidation's cost.
    per_stop = sum(_dec(s["decision"]["consolidation_extra_cost"]) for s in stops)
    assert per_stop == pytest.approx(_dec(optimizer["consolidation_extra_cost"]), abs=Decimal("0.01") * len(stops))


@pytest.mark.django_db
def test_candidates_are_opt_in_and_share_the_plan_cache(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    plain = get(client).json()
    assert "candidates" not in plain and "_candidates" not in plain

    body = get(client, include="candidates").json()
    assert len(upstream.calls) == 1  # the same plan, from the cache
    assert body["meta"]["plan_cache"] == "hit" and body["meta"]["external_api_calls"] == 0
    assert "_candidates" not in body
    candidates = body["candidates"]
    assert candidates["fields"] == [
        "opis_id", "name", "city", "state", "lat", "lon",
        "mile_marker", "distance_from_route_miles", "price_per_gallon", "stop",
    ]
    rows = [dict(zip(candidates["fields"], row)) for row in candidates["rows"]]
    assert len(rows) == body["summary"]["candidate_stations_on_route"] == 5
    assert [(r["mile_marker"], r["opis_id"]) for r in rows] == sorted((r["mile_marker"], r["opis_id"]) for r in rows)
    assert 6 not in [r["opis_id"] for r in rows]  # 40 miles off the route
    assert {r["opis_id"]: r["stop"] for r in rows if r["stop"] is not None} == {
        s["opis_id"]: s["stop"] for s in body["fuel_stops"]
    }
    first = rows[0]
    assert (first["opis_id"], first["name"], first["city"], first["state"]) == (1, "STOP 1", "Town", "OK")
    assert (first["lat"], first["lon"], first["price_per_gallon"]) == (35.02, -99.3, 3.5)
    assert first["distance_from_route_miles"] == pytest.approx(1.4, abs=0.1)
    # The plan is the same with or without the extra block.
    assert {k: v for k, v in body.items() if k not in ("candidates", "meta")} == {
        k: v for k, v in plain.items() if k != "meta"
    }

    # A plan computed with include (here on the cached route) has it too.
    full = get(client, start_tank="full", include="candidates").json()
    assert full["meta"]["plan_cache"] == "miss" and full["meta"]["route_cache"] == "hit"
    assert len(full["candidates"]["rows"]) == 5
    assert len(upstream.calls) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(("value", "unknown"), [("foo", "foo"), ("candidates, foo", "foo"), ("CANDIDATES", "CANDIDATES")])
def test_unknown_include_value_is_400(client, upstream, stations, value, unknown):
    response = get(client, include=value)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert body["detail"] == {"include": [f"Unknown value(s): {unknown}. Allowed: candidates."]}
    assert body["meta"]["external_api_calls"] == 0
    assert upstream.calls == []


@pytest.mark.django_db
def test_include_does_not_change_map_url(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    with_include = get(client, include="candidates").json()
    without = get(client).json()
    assert with_include["map"]["map_url"] == without["map"]["map_url"]
    assert "include" not in with_include["map"]["map_url"]
    assert without["meta"]["plan_cache"] == "hit"  # include is not part of the plan cache key


@pytest.mark.django_db
def test_server_timing_header_breaks_down_the_request(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    first = get(client)
    timing = _server_timing(first)
    assert list(timing) == [
        "geocoding", "plan-cache", "routing", "osrm", "corridor", "optimizer", "build", "comparison", "render", "total",
    ]
    meta = first.json()["meta"]
    assert timing["osrm"]["desc"] == "1 call"
    assert timing["osrm"]["dur"] == pytest.approx(meta["external_api_ms"], abs=0.051)
    assert timing["routing"]["dur"] == meta["timings_ms"]["routing_ms"]
    assert timing["render"]["desc"] == "JSON render"
    assert timing["total"] == {"dur": float(first["X-Response-Time-ms"]), "desc": "Server total"}
    # The timed renderer writes exactly what DRF's JSONRenderer writes.
    assert TimedJSONRenderer().render(first.json()) == JSONRenderer().render(first.json())

    hit = get(client, include="candidates")
    assert list(_server_timing(hit)) == ["geocoding", "plan-cache", "candidates", "render", "total"]

    invalid = client.get("/api/route", {"finish": FINISH})
    assert list(_server_timing(invalid)) == ["render", "total"]

    upstream.respond(urllib3.exceptions.ProtocolError("boom"))
    failed = client.get("/api/route", {"start": START, "finish": "35.0,-90.0"})
    assert failed.status_code == 502
    timing = _server_timing(failed)  # the steps completed before the error, and the calls
    assert list(timing) == ["geocoding", "plan-cache", "osrm", "render", "total"]
    assert timing["osrm"]["desc"] == "2 calls"  # first try + one retry
    assert set(failed.json()) == {"error", "detail", "meta"}  # the error body is unchanged

    upstream.respond(OK_ROUTE)
    FuelStation.objects.filter(opis_id__gte=2).delete()  # leaves a gap longer than the range
    gap = client.get("/api/route", {"start": START, "finish": "35.0,-89.0"})
    assert gap.status_code == 422
    assert list(_server_timing(gap)) == ["geocoding", "plan-cache", "routing", "osrm", "corridor", "render", "total"]


LEGACY_TIMINGS = {"geocoding_ms", "routing_ms", "corridor_ms", "optimizer_ms", "response_build_ms"}


@pytest.mark.django_db
def test_timings_keep_the_legacy_keys(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    miss = get(client).json()["meta"]["timings_ms"]
    assert set(miss) == LEGACY_TIMINGS | {"plan_cache_ms", "comparison_ms"}
    assert all(isinstance(value, float) and value >= 0 for value in miss.values())
    assert set(get(client).json()["meta"]["timings_ms"]) == {"geocoding_ms", "plan_cache_ms"}
    with_candidates = get(client, include="candidates").json()["meta"]["timings_ms"]
    assert set(with_candidates) == {"geocoding_ms", "plan_cache_ms", "candidates_ms"}


@pytest.mark.django_db
def test_cache_hit_keeps_the_pipeline_of_the_first_computation(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    first = get(client).json()
    second = get(client).json()
    assert second["meta"]["plan_cache"] == "hit" and second["meta"]["external_api_calls"] == 0
    assert second["pipeline"] == first["pipeline"]
    assert second["pipeline"]["external_api_calls"] == 1  # what the first computation cost
    assert second["pipeline"]["timings_ms"] == first["meta"]["timings_ms"]
    assert datetime.fromisoformat(first["pipeline"]["computed_at"]).tzinfo is not None

    other = get(client, start_tank="full").json()  # the other tank mode: same route, new plan
    assert other["meta"]["route_cache"] == "hit" and other["meta"]["plan_cache"] == "miss"
    assert other["pipeline"]["external_api_calls"] == 0
    assert other["pipeline"]["routing"]["from_route_cache"] is True
    assert other["pipeline"]["routing"]["polyline_chars"] == first["pipeline"]["routing"]["polyline_chars"]
    assert other["pipeline"]["tank"]["reason"] == "full_tank"
    assert other["pipeline"]["computed_at"] >= first["pipeline"]["computed_at"]


@pytest.mark.django_db
def test_post_json_and_full_tank_mode(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    response = client.post("/api/route", {"start": START, "finish": FINISH, "start_tank": "full"}, format="json")
    assert response.status_code == 200
    body = response.json()
    assert body["vehicle"]["start_tank"] == "full"
    # Leaves with 500 miles in the tank: only 180 more miles to buy.
    assert body["summary"]["total_gallons_purchased"] == pytest.approx(18.0, abs=0.05)
    assert body["summary"]["unpriced_fuel_gallons"] == pytest.approx(50.0, abs=0.05)


@pytest.mark.django_db
def test_short_trip_with_full_tank_has_no_stops(client, upstream, stations):
    upstream.respond((200, osrm_answer(LINE[:41], 120)))
    body = client.get("/api/route", {"start": START, "finish": "35.0,-98.0", "start_tank": "full"}).json()
    assert body["fuel_stops"] == []
    assert body["summary"]["total_fuel_cost"] == 0
    assert body["summary"]["comparison"] is None  # nothing bought: nothing to compare
    assert body["pipeline"]["optimizer"]["stops"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("blank", ["", None])
def test_blank_start_tank_means_the_default(client, upstream, stations, blank):
    upstream.respond(OK_ROUTE)
    if blank is None:
        body = client.post("/api/route", {"start": START, "finish": FINISH, "start_tank": ""}, format="json").json()
    else:
        body = get(client, start_tank=blank).json()
    assert body["vehicle"]["start_tank"] == "empty"


# --- start_tank=empty edge cases ----------------------------------------------------------


@pytest.mark.django_db
def test_first_station_beyond_the_reserve_is_planned_not_rejected(client, upstream):
    # Houston / Los Angeles pattern: no station in the first 50 miles. Regression:
    # this was a 422 "the next station (mile 153) is out of range".
    add_stations([(1, 35.02, -97.3, "3.10"), (2, 35.02, -95.0, "3.00"), (3, 35.02, -92.5, "3.20"),
                  (4, 35.02, -90.5, "2.95")])
    upstream.respond(OK_ROUTE)
    response = get(client)
    assert response.status_code == 200, response.json()
    body = response.json()
    first_mile = mile_of(-97.3)
    assert body["summary"]["start_fuel_gallons"] == pytest.approx(first_mile / 10, abs=0.1)
    assert body["summary"]["end_fuel_gallons"] == pytest.approx(first_mile / 10, abs=0.1)
    # Every mile is still paid for.
    assert body["summary"]["total_gallons_purchased"] == pytest.approx(ROUTE_MILES / 10, abs=0.05)
    assert any("first station" in w for w in body["warnings"])
    assert body["pipeline"]["tank"]["reason"] == "first_station_beyond_reserve"
    assert body["pipeline"]["tank"]["arrival_capped"] is False


@pytest.mark.django_db
def test_short_trip_with_no_station_on_the_route(client, upstream):
    # A 27-mile trip with no station nearby, on the default tank. Regression: 422
    # "the destination is out of range" although the reserve covers it.
    add_stations([(1, 40.0, -80.0, "3.00")])  # data loaded, but far away
    upstream.respond((200, osrm_answer(LINE[:9], 27)))
    response = client.get("/api/route", {"start": START, "finish": "35.0,-99.6"})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["fuel_stops"] == []
    assert body["summary"]["unpriced_fuel_gallons"] == pytest.approx(2.7)
    assert any("not priced" in w for w in body["warnings"])
    assert body["pipeline"]["tank"]["reason"] == "no_station_on_route"
    assert body["pipeline"]["corridor"]["candidates"] == 0 and body["pipeline"]["corridor"]["price_per_gallon"] is None


@pytest.mark.django_db
def test_trip_needing_fuel_with_no_station_on_the_route_is_422(client, upstream):
    add_stations([(1, 40.0, -80.0, "3.00")])
    upstream.respond(OK_ROUTE)
    response = get(client)
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "no_fuel_data_on_route"
    assert body["meta"]["external_api_calls"] == 1


@pytest.mark.django_db
def test_last_stretch_too_long_for_the_reserve_arrives_with_less(client, upstream):
    # Last station at mile ~198, destination 482 miles later: arriving with the
    # 50-mile reserve is impossible (482 + 50 > 500). Regression: 422.
    add_stations([(1, 35.02, -99.3, "3.00"), (2, 35.02, -96.5, "3.00")])
    upstream.respond(OK_ROUTE)
    response = get(client)
    assert response.status_code == 200, response.json()
    summary = response.json()["summary"]
    assert summary["end_fuel_gallons"] == pytest.approx((500 - (ROUTE_MILES - mile_of(-96.5))) / 10, abs=0.1)
    assert summary["unpriced_fuel_gallons"] > 0
    tank = response.json()["pipeline"]["tank"]
    assert tank["arrival_capped"] is True
    assert tank["required_end_fuel_gallons"] == pytest.approx(summary["end_fuel_gallons"], abs=0.01)


@pytest.mark.django_db
def test_gap_longer_than_the_range_is_422_and_says_where(client, upstream):
    add_stations([(1, 35.02, -99.3, "3.00")])  # nothing after mile ~40 of 680
    upstream.respond(OK_ROUTE)
    for tank in ("empty", "full"):
        response = get(client, start_tank=tank)
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "no_reachable_fuel_station"
        assert "last station" in body["detail"]
        assert body["gap_start"]["mile"] == pytest.approx(mile_of(-99.3), abs=1)
        assert body["gap_end"]["mile"] == pytest.approx(ROUTE_MILES, abs=1)


# --- checks before routing ----------------------------------------------------------------


@pytest.mark.django_db
def test_no_station_data_loaded_is_503_without_calling_osrm(client, upstream):
    response = get(client)
    assert response.status_code == 503
    assert response.json()["error"] == "station_data_not_loaded"
    assert upstream.calls == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start", "finish"),
    [
        ("Austin, TX", "austin, tx"),
        ("Austin, TX", "Austin, Texas"),
        ("Austin, TX", "Austin TX"),
        ("35.0,-100.0", "35.001,-100.001"),
    ],
)
def test_same_place_written_differently_is_400_without_routing(client, upstream, stations, start, finish):
    response = client.get("/api/route", {"start": start, "finish": finish})
    assert response.status_code == 400
    assert response.json()["error"] == "same_location"
    assert upstream.calls == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "point",
    [
        "48.85,2.35",  # Paris
        "43.6532,-79.3832",  # Toronto
        "25.6866,-100.3161",  # Monterrey, 140 miles into Mexico
        "60.7212,-135.0568",  # Whitehorse, Yukon (inside the old Alaska box)
        "27.0,-90.0",  # Gulf of Mexico
        "35.0,-70.0",  # Atlantic
        "95.0,-88.0",  # invalid latitude
    ],
)
def test_points_outside_the_usa_are_400_without_calls(client, upstream, stations, point):
    response = client.get("/api/route", {"start": point, "finish": FINISH})
    assert response.status_code == 400
    assert "start" in response.json()["detail"]
    assert upstream.calls == []


@pytest.mark.django_db
@pytest.mark.parametrize("place", ["Anchorage, AK", "Honolulu, HI", "21.3069,-157.8583"])
def test_alaska_and_hawaii_are_422_before_routing(client, upstream, stations, place):
    response = client.get("/api/route", {"start": place, "finish": "Seattle, WA"})
    assert response.status_code == 422
    assert response.json()["error"] == "no_fuel_data_in_region"
    assert upstream.calls == []


@pytest.mark.django_db
def test_point_far_from_any_road_is_422(client, upstream, stations):
    upstream.respond((200, osrm_answer(LINE, ROUTE_MILES, snap_miles=(146.0, 0.0))))
    response = get(client)
    assert response.status_code == 422
    assert response.json()["error"] == "location_not_near_road"
    assert response.json()["field"] == "start"


@pytest.mark.django_db
def test_route_through_canada_warns_and_reports_the_miles(client, upstream, stations):
    # Detroit -> Buffalo: OSRM's fastest road crosses Ontario.
    line = [(42.33, -83.05), (42.98, -81.25), (43.25, -79.87), (42.89, -78.88)]
    upstream.respond((200, osrm_answer(line, 265)))
    response = client.get("/api/route", {"start": "Detroit, MI", "finish": "Buffalo, NY", "start_tank": "full"})
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["route"]["miles_outside_usa"] > 150
    assert any("outside the USA" in w for w in body["warnings"])


# --- input validation and error format ----------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"finish": FINISH}, "start"),
        ({"start": START}, "finish"),
        ({"start": START, "finish": FINISH, "start_tank": "half"}, "start_tank"),
        ({"start": "???", "finish": FINISH}, "start"),
        ({"start": START, "finish": FINISH, "start_tnak": "full"}, "non_field_errors"),
    ],
)
def test_invalid_input_returns_400(client, upstream, params, field):
    response = client.get("/api/route", params)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert field in body["detail"]
    assert body["meta"]["external_api_calls"] == 0
    assert upstream.calls == []


@pytest.mark.django_db
def test_drf_errors_use_the_same_format(client):
    bad_json = client.post("/api/route", data="{not json", content_type="application/json")
    assert bad_json.status_code == 400 and bad_json.json()["error"] == "parse_error"
    text = client.post("/api/route", data="start=a", content_type="text/plain")
    assert text.status_code == 415 and text.json()["error"] == "unsupported_media_type"
    put = client.put("/api/route", {}, format="json")
    assert put.status_code == 405 and put.json()["error"] == "method_not_allowed"


def _planner_error_codes(cls=PlannerError) -> set[str]:
    return {cls.code}.union(*(_planner_error_codes(sub) for sub in cls.__subclasses__()))


@pytest.mark.django_db
def test_every_error_code_of_the_catalog_has_the_same_body_with_meta(client, upstream, stations, settings, monkeypatch):
    # Regression: DRF's errors, the JSON 404 and the 429 came without meta, although
    # the catalog says every error has {error, detail, meta}.
    answers = {
        "invalid_request": client.get("/api/route"),
        "parse_error": client.post("/api/route", data="{not json", content_type="application/json"),
        "unsupported_media_type": client.post("/api/route", data="start=a", content_type="text/plain"),
        "method_not_allowed": client.put("/api/route", {}, format="json"),
        "not_acceptable": client.get("/api/route", HTTP_ACCEPT="text/csv"),
        "not_found": client.get("/api/nope"),
    }
    monkeypatch.setitem(settings.FUEL_PLANNER, "RATE_LIMIT_PER_MINUTE", 1)
    answers["rate_limited"] = get(client)
    for code, response in answers.items():
        body = response.json()
        assert body["error"] == code, body
        assert body["detail"]
        assert body["meta"] == {"external_api_calls": 0, "external_api_services": []}, code
    # The other codes are PlannerErrors, whose body always gets meta (test_errors_report_*).
    assert {error["code"] for error in error_catalog()} <= set(answers) | _planner_error_codes()
    assert upstream.calls == []


@pytest.mark.django_db
def test_rate_limit_counts_the_endpoint_not_the_page(client, upstream, stations, settings, monkeypatch):
    # Regression: /api/route/map shared the quota, and the 61st reload of the page
    # answered a JSON 429 instead of the page.
    monkeypatch.setitem(settings.FUEL_PLANNER, "RATE_LIMIT_PER_MINUTE", 2)
    for _ in range(4):
        assert client.get("/api/route/map").status_code == 200
    upstream.respond(OK_ROUTE)
    assert [get(client).status_code for _ in range(3)] == [200, 200, 429]


@pytest.mark.django_db
def test_answers_are_gzipped_for_clients_that_accept_it(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    plain = get(client)
    zipped = client.get("/api/route", {"start": START, "finish": FINISH}, HTTP_ACCEPT_ENCODING="gzip, deflate")
    assert not plain.has_header("Content-Encoding")
    assert zipped["Content-Encoding"] == "gzip"
    assert len(zipped.content) < len(plain.content) / 3
    assert json.loads(gzip.decompress(zipped.content))["fuel_stops"] == plain.json()["fuel_stops"]
    page = client.get("/api/route/map", HTTP_ACCEPT_ENCODING="gzip")
    assert page["Content-Encoding"] == "gzip"


@pytest.mark.django_db
def test_trailing_slash_and_unknown_paths(client):
    assert client.get("/api/route/").json()["error"] == "invalid_request"  # same endpoint
    missing = client.get("/api/nope")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    assert client.get("/").json()["endpoints"]


@pytest.mark.django_db
def test_errors_report_the_external_calls_already_made(client, upstream, stations):
    # Free text goes to Nominatim (filtered to the USA), which finds nothing.
    upstream.respond((200, []))
    response = client.get("/api/route", {"start": "Nowhere in particular", "finish": FINISH})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "location_not_found"
    assert body["meta"] == {"external_api_calls": 1, "external_api_services": ["nominatim"]}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("start", "status", "error"),
    [
        # Regression: "Toronto, ON" went to Nominatim, matched "Toronto Court" in
        # Indianapolis and a 1,101-mile trip was planned without any error.
        ("Toronto, ON", 400, "location_outside_usa"),
        ("Toronto ON", 400, "location_outside_usa"),
        ("Vancouver, BC", 400, "location_outside_usa"),
        ("Toronto, Ontario, Canada", 400, "location_outside_usa"),
        ("Monterrey, NL", 400, "location_outside_usa"),
        ("Monterrey, Nuevo León", 400, "location_outside_usa"),
        ("Tijuana, Mexico", 400, "location_outside_usa"),
        ("San Juan, PR", 422, "no_fuel_data_in_region"),
        ("Austin, TZ", 400, "location_not_found"),  # a typo: not a US state, not a place abroad
    ],
)
def test_places_written_with_a_region_outside_the_states_are_rejected_without_calls(
    client, upstream, stations, start, status, error
):
    response = client.get("/api/route", {"start": start, "finish": FINISH})
    assert response.status_code == status
    body = response.json()
    assert body["error"] == error
    assert body["field"] == "start"
    assert body["meta"] == {"external_api_calls": 0, "external_api_services": []}
    assert upstream.calls == []


@pytest.mark.django_db
def test_a_street_matched_by_free_text_is_not_a_place(client, upstream, stations):
    street = {
        "lat": "39.7", "lon": "-86.2", "category": "highway", "type": "residential", "addresstype": "road",
        "display_name": "Toronto Court, Westover, Indianapolis, Indiana, United States",
    }
    upstream.respond((200, [street]))
    for _ in range(2):  # the rejection is cached: one Nominatim call in total
        response = client.get("/api/route", {"start": "Toronto Ontario", "finish": FINISH})
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "location_not_found"
        assert "Toronto Court" in body["detail"]
    assert len(upstream.calls) == 1


@pytest.mark.django_db
def test_free_text_match_is_planned_with_a_warning_that_names_it(client, upstream, stations):
    town = {"lat": "35.0", "lon": "-100.0", "category": "place", "type": "town", "display_name": "Somewhere, Texas"}
    upstream.respond(lambda url, params: (200, [town]) if "nominatim" in url else OK_ROUTE)
    body = client.get("/api/route", {"start": "Somewhere", "finish": FINISH}).json()
    assert body["start"]["geocoder"] == "nominatim"
    assert any("'Somewhere'" in w and "Somewhere, Texas" in w for w in body["warnings"])
    # The warning depends on the text typed, not on the plan: a cache hit by coordinates has none.
    again = client.get("/api/route", {"start": "35.0,-100.0", "finish": FINISH}).json()
    assert again["meta"]["plan_cache"] == "hit"
    assert not any("free-text" in w for w in again["warnings"])


# --- upstream failures --------------------------------------------------------------------


@pytest.mark.django_db
def test_transient_failure_is_retried_once(client, upstream, stations):
    upstream.respond(urllib3.exceptions.ProtocolError("connection reset"), OK_ROUTE)
    response = get(client)
    assert response.status_code == 200
    assert response.json()["meta"]["external_api_calls"] == 2


@pytest.mark.django_db
def test_routing_service_down_returns_502(client, upstream, stations):
    upstream.respond(urllib3.exceptions.ProtocolError("boom"))
    response = get(client)
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"
    assert response.json()["meta"]["external_api_calls"] == 2  # first try + one retry


@pytest.mark.django_db
def test_rate_limited_upstream_returns_503_with_retry_after(client, upstream, stations):
    upstream.respond((429, {"message": "Too Many Requests"}, {"Retry-After": "7"}))
    response = get(client)
    assert response.status_code == 503
    assert response.json()["error"] == "upstream_busy"
    assert response.headers["Retry-After"] == "7"
    assert len(upstream.calls) == 1  # a 429 is not retried


@pytest.mark.django_db
def test_no_route_returns_422(client, upstream, stations):
    upstream.respond((400, {"code": "NoRoute", "message": "Impossible route"}))
    response = get(client)
    assert response.status_code == 422
    assert response.json()["error"] == "no_route"


@pytest.mark.django_db
def test_own_rate_limit_answers_429(client, upstream, stations, settings, monkeypatch):
    monkeypatch.setitem(settings.FUEL_PLANNER, "RATE_LIMIT_PER_MINUTE", 2)
    upstream.respond(OK_ROUTE)
    statuses = [get(client).status_code for _ in range(3)]
    assert statuses[:2] == [200, 200]
    assert statuses[2] == 429


# --- root, data reloads, concurrency ------------------------------------------------------


@pytest.mark.django_db
def test_root_sends_browsers_to_the_planner_page_and_api_clients_get_json(client):
    browser = client.get("/", HTTP_ACCEPT="text/html,application/xhtml+xml,*/*;q=0.8")
    assert browser.status_code == 302 and browser["Location"].endswith("/api/route/map")
    api = client.get("/", HTTP_ACCEPT="*/*")
    assert api.status_code == 200 and "endpoints" in api.json()


@pytest.mark.django_db
def test_new_station_data_is_used_without_restart(client, upstream, stations):
    # Regression: re-running load_stations while the server ran gave HTTP 500
    # (KeyError on the old primary keys) and kept serving cached plans.
    upstream.respond(OK_ROUTE)
    first = get(client).json()
    FuelStation.objects.all().delete()  # what load_stations does: new rows, new ids
    add_stations([(1, 35.02, -99.3, "3.50"), (2, 35.02, -97.5, "1.90"), (3, 35.02, -93.0, "3.10")])
    response = get(client)
    assert response.status_code == 200
    second = response.json()
    assert second["meta"]["plan_cache"] == "miss"
    assert second["meta"]["external_api_calls"] == 0  # the route itself is still cached
    assert second["summary"]["total_fuel_cost"] < first["summary"]["total_fuel_cost"]


def test_the_suite_cannot_reach_the_network():
    # Without the upstream fixture any real HTTP call fails the test (conftest.clean_state).
    with pytest.raises(AssertionError, match="network"):
        ExternalApiClient().get_json("osrm", "https://router.project-osrm.org/route/v1/driving/0,0;1,1")


def test_identical_concurrent_requests_make_one_routing_call(upstream):
    def slow_answer(url, params):
        time.sleep(0.2)
        return OK_ROUTE

    upstream.respond(slow_answer)
    start, finish = (Location("a", 35.0, -100.0, "a", "coordinates"), Location("b", 35.0, -88.0, "b", "coordinates"))
    threads = [threading.Thread(target=get_route, args=(start, finish, ExternalApiClient())) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(upstream.calls) == 1
