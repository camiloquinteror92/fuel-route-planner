"""The HTTP API end to end, with the network faked (see conftest.Upstream).

The synthetic route runs east along latitude 35 from (35, -100) to (35, -88):
about 680 road miles, 1 degree of longitude ~ 56.7 miles.
"""

import threading
import time
from decimal import ROUND_HALF_UP, Decimal

import pytest
import urllib3
from rest_framework.test import APIClient

from fuelroute.models import FuelStation
from fuelroute.services.geocoding import Location
from fuelroute.services.http import ExternalApiClient
from fuelroute.services.osrm import get_route

from .conftest import osrm_answer

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
def test_second_request_and_map_page_use_the_cache(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    first = get(client).json()
    second = client.get("/api/route", {"start": " 35.0 , -100.0 ", "finish": FINISH}).json()
    page = client.get(first["map"]["map_url"])

    assert len(upstream.calls) == 1
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["plan_cache"] == "hit"
    # A cache hit answers with the text of THIS request, not the first one.
    assert second["start"]["query"] == "35.0 , -100.0"
    assert "35.0+%2C+-100.0" in second["map"]["map_url"]
    assert page.status_code == 200
    assert b"leaflet" in page.content and b"route-data" in page.content


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


@pytest.mark.django_db
def test_trailing_slash_and_unknown_paths(client):
    assert client.get("/api/route/").json()["error"] == "invalid_request"  # same endpoint
    missing = client.get("/api/nope")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    assert client.get("/").json()["endpoints"]


@pytest.mark.django_db
def test_errors_report_the_external_calls_already_made(client, upstream, stations):
    # Free text goes to Nominatim (filtered to the USA): "Toronto, ON" is not found.
    upstream.respond((200, []))
    response = client.get("/api/route", {"start": "Toronto, ON", "finish": FINISH})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "location_not_found"
    assert body["meta"] == {"external_api_calls": 1, "external_api_services": ["nominatim"]}


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


# --- map page, data reloads, concurrency --------------------------------------------------


@pytest.mark.django_db
def test_map_page_shows_readable_errors(client):
    page = client.get("/api/route/map", {"finish": "Austin, TX"})
    assert page.status_code == 400
    assert b"start: This field is required." in page.content
    assert b"ErrorDetail" not in page.content


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
