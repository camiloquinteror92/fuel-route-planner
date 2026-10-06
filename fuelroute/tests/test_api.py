from decimal import Decimal

import pytest
import requests
from rest_framework.test import APIClient

from fuelroute.models import FuelStation

# A straight east-west synthetic route at latitude 35 (~680 road miles).
START = "35.0,-100.0"
FINISH = "35.0,-88.0"
ROUTE_METERS = 680 * 1609.344


def encode_polyline(points, precision=6):
    factor = 10 ** precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        lat_i, lon_i = round(lat * factor), round(lon * factor)
        for delta in (lat_i - prev_lat, lon_i - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = lat_i, lon_i
    return "".join(out)


ROUTE_GEOMETRY = encode_polyline([(35.0, -100.0 + i * 0.05) for i in range(241)])


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def osrm(monkeypatch):
    """Replaces every outgoing HTTP call; records the URLs."""
    state = {"calls": [], "response": FakeResponse(200, {
        "code": "Ok",
        "routes": [{"geometry": ROUTE_GEOMETRY, "distance": ROUTE_METERS, "duration": 36000}],
    })}

    def fake_get(self, url, params=None, timeout=None):
        state["calls"].append(url)
        if isinstance(state["response"], Exception):
            raise state["response"]
        return state["response"]

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return state


@pytest.fixture
def stations(db):
    rows = [
        (1, -99.3, "3.50"),  # ~40 miles in: reachable on the 50-mile reserve
        (2, -97.5, "2.90"),  # cheap
        (3, -95.0, "3.60"),
        (4, -93.0, "3.10"),
        (5, -90.5, "3.30"),
        (6, -96.0, "2.50"),  # very cheap but 40 miles off the route -> ignored
    ]
    objects = []
    for opis_id, lon, price in rows:
        lat = 35.6 if opis_id == 6 else 35.02
        objects.append(FuelStation(
            opis_id=opis_id, name=f"STOP {opis_id}", address="I-40", city="Town", state="OK",
            price=Decimal(price), latitude=lat, longitude=lon, geocode_source="census",
        ))
    FuelStation.objects.bulk_create(objects)


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_route_returns_plan_map_and_uses_one_external_call(client, osrm, stations):
    response = client.get("/api/route", {"start": START, "finish": FINISH})
    assert response.status_code == 200, response.json()
    body = response.json()

    assert len(osrm["calls"]) == 1
    assert "router.project-osrm.org/route/v1/driving/-100.000000,35.000000;-88.000000,35.000000" in osrm["calls"][0]
    assert body["meta"]["external_api_calls"] == 1
    assert body["route"]["distance_miles"] == pytest.approx(680, abs=0.5)

    stops = body["fuel_stops"]
    assert stops, "a 680-mile trip on a reserve needs fuel"
    assert 6 not in [s["opis_id"] for s in stops]  # outside the corridor
    assert stops[0]["opis_id"] == 1  # nearest station, just enough to reach the cheap one
    assert any(s["opis_id"] == 2 for s in stops)
    summary = body["summary"]
    assert summary["total_gallons_purchased"] == pytest.approx(68.0, abs=0.05)  # every mile paid
    assert summary["total_fuel_cost"] == pytest.approx(sum(s["cost"] for s in stops), abs=0.02)

    features = body["map"]["geojson"]["features"]
    assert features[0]["geometry"]["type"] == "LineString"
    assert [f["properties"]["kind"] for f in features].count("fuel_stop") == len(stops)
    assert body["map"]["map_url"].startswith("http://testserver/api/route/map?")
    assert "X-Response-Time-ms" in response.headers


@pytest.mark.django_db
def test_second_request_and_map_page_use_the_cache(client, osrm, stations):
    first = client.get("/api/route", {"start": START, "finish": FINISH}).json()
    second = client.get("/api/route", {"start": START, "finish": FINISH}).json()
    page = client.get(first["map"]["map_url"])

    assert len(osrm["calls"]) == 1
    assert second["meta"]["external_api_calls"] == 0
    assert second["meta"]["route_cache"] == "hit"
    assert page.status_code == 200
    assert b"leaflet" in page.content
    assert b"route-data" in page.content


@pytest.mark.django_db
def test_post_json_and_full_tank_mode(client, osrm, stations):
    response = client.post(
        "/api/route", {"start": START, "finish": FINISH, "start_tank": "full"}, format="json"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["vehicle"]["start_tank"] == "full"
    # Leaves with 500 miles in the tank: only 180 more miles to buy.
    assert body["summary"]["total_gallons_purchased"] == pytest.approx(18.0, abs=0.05)


@pytest.mark.django_db
def test_short_trip_with_full_tank_has_no_stops(client, osrm, stations):
    osrm["response"] = FakeResponse(200, {
        "code": "Ok",
        "routes": [{"geometry": ROUTE_GEOMETRY, "distance": 120 * 1609.344, "duration": 7200}],
    })
    body = client.get("/api/route", {"start": START, "finish": "35.0,-98.0", "start_tank": "full"}).json()
    assert body["fuel_stops"] == []
    assert body["summary"]["total_fuel_cost"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"finish": FINISH}, "start"),
        ({"start": START}, "finish"),
        ({"start": "48.85,2.35", "finish": FINISH}, "start"),  # Paris
        ({"start": START, "finish": "95.0,-88.0"}, "finish"),  # invalid latitude
        ({"start": START, "finish": FINISH, "start_tank": "half"}, "start_tank"),
    ],
)
def test_invalid_input_returns_400(client, osrm, params, field):
    response = client.get("/api/route", params)
    assert response.status_code == 400
    assert field in response.json()["detail"]
    assert osrm["calls"] == []


@pytest.mark.django_db
def test_same_start_and_finish_is_rejected(client, osrm):
    response = client.get("/api/route", {"start": "Austin, TX", "finish": "austin, tx"})
    assert response.status_code == 400


@pytest.mark.django_db
def test_routing_service_down_returns_502(client, osrm, stations):
    osrm["response"] = requests.ConnectionError("boom")
    response = client.get("/api/route", {"start": START, "finish": FINISH})
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


@pytest.mark.django_db
def test_no_route_returns_422(client, osrm, stations):
    osrm["response"] = FakeResponse(400, {"code": "NoRoute", "message": "Impossible route"})
    response = client.get("/api/route", {"start": START, "finish": FINISH})
    assert response.status_code == 422
    assert response.json()["error"] == "no_route"


@pytest.mark.django_db
def test_no_reachable_station_returns_422(client, osrm):
    # No stations loaded at all.
    response = client.get("/api/route", {"start": START, "finish": FINISH})
    assert response.status_code == 422
    assert response.json()["error"] == "no_reachable_fuel_station"
