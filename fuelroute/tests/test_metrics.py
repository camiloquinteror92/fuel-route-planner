"""/api/stats: what the server answered on /api/route since it started (network faked)."""

import json

import pytest
import urllib3
from rest_framework.test import APIClient

from fuelroute.services.metrics import route_metrics

from .test_api import FINISH, OK_ROUTE, START, get, stations  # noqa: F401  (stations is a fixture)


@pytest.fixture(autouse=True)
def fresh_metrics():
    route_metrics.reset()
    yield
    route_metrics.reset()


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_stats_count_requests_calls_and_latency_by_outcome(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    responses = [
        get(client),  # cold: 1 OSRM call
        get(client),  # the same trip: plan cache
        get(client, start_tank="full"),  # the other tank mode: route cache, new plan
        client.get("/api/route", {"finish": FINISH}),  # invalid: 400 before any call
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 400]
    client.get("/api/stats")
    client.get("/api/about")  # neither of these is counted
    stats = client.get("/api/stats").json()

    requests = stats["route_requests"]
    assert requests["total"] == 4
    assert requests["by_status"] == {"200": 3, "400": 1}
    assert requests["by_outcome"] == {
        "cold": 1, "nominatim_call": 0, "route_cache_hit": 1, "plan_cache_hit": 1, "error": 1,
    }
    assert requests["errors"] == {"invalid_request": 1}
    assert requests["errors_without_external_calls"] == 1

    external = stats["external_api"]
    assert external["calls"] == 1
    assert external["by_service"] == {"osrm": 1, "nominatim": 0}
    assert external["requests_with_osrm_calls"] == 1
    assert external["osrm_calls_per_routing_request"] == 1.0
    assert external["max_calls_in_one_request"] == external["max_osrm_calls_in_one_request"] == 1
    assert external["total_ms"] == pytest.approx(responses[0].json()["meta"]["external_api_ms"], abs=0.1)

    latency = stats["latency_ms"]
    assert latency["window"] > 0
    for outcome, response in zip(("cold", "plan_cache_hit", "route_cache_hit", "error"), responses):
        assert latency[outcome] == {
            "count": 1,
            "p50": float(response["X-Response-Time-ms"]),
            "p95": float(response["X-Response-Time-ms"]),
            "max": float(response["X-Response-Time-ms"]),
        }
    assert stats["external_latency_ms"]["osrm"]["count"] == 1
    assert stats["external_latency_ms"]["nominatim"] is None

    recent = stats["recent"]
    assert [(r["status"], r["outcome"]) for r in recent] == [
        (400, "error"), (200, "route_cache_hit"), (200, "plan_cache_hit"), (200, "cold"),
    ]  # newest first
    assert [r["external_api_calls"] for r in recent] == [0, 0, 0, 1]
    assert recent[0]["error"] == "invalid_request" and recent[1]["error"] is None
    assert stats["process"]["uptime_seconds"] >= 0 and stats["process"]["pid"] > 0


@pytest.mark.django_db
def test_retries_and_upstream_errors_are_counted(client, upstream, stations):
    upstream.respond(urllib3.exceptions.ProtocolError("reset"), OK_ROUTE)
    assert get(client).status_code == 200  # 2 OSRM calls: a failure and the retry
    upstream.respond(urllib3.exceptions.ProtocolError("down"))
    assert client.get("/api/route", {"start": START, "finish": "35.0,-90.0"}).status_code == 502
    external = client.get("/api/stats").json()["external_api"]
    assert external["calls"] == 4
    assert external["osrm_calls_per_routing_request"] == 2.0
    assert external["max_calls_in_one_request"] == external["max_osrm_calls_in_one_request"] == 2
    errors = client.get("/api/stats").json()["route_requests"]
    assert errors["errors"] == {"upstream_unavailable": 1}
    assert errors["errors_without_external_calls"] == 0


@pytest.mark.django_db
def test_geocoding_calls_are_kept_apart_from_the_routing_budget(client, upstream, stations):
    # Regression: after a free-text trip (Nominatim + OSRM) the page said "at most 2
    # OSRM calls in one request", and the cold latency included Nominatim's.
    town = {"lat": "35.0", "lon": "-100.0", "category": "place", "type": "town", "display_name": "Somewhere, Texas"}
    upstream.respond(lambda url, params: (200, [town]) if "nominatim" in url else OK_ROUTE)
    assert client.get("/api/route", {"start": "Somewhere", "finish": FINISH}).status_code == 200
    stats = client.get("/api/stats").json()
    assert stats["route_requests"]["by_outcome"]["nominatim_call"] == 1
    assert stats["latency_ms"]["cold"] is None and stats["latency_ms"]["nominatim_call"]["count"] == 1
    external = stats["external_api"]
    assert external["by_service"] == {"osrm": 1, "nominatim": 1}
    assert external["max_calls_in_one_request"] == 2
    assert external["max_osrm_calls_in_one_request"] == 1
    assert external["osrm_calls_per_routing_request"] == 1.0


@pytest.mark.django_db
def test_stats_and_about_are_not_rate_limited(client, upstream, stations, settings, monkeypatch):
    monkeypatch.setitem(settings.FUEL_PLANNER, "RATE_LIMIT_PER_MINUTE", 2)
    upstream.respond(OK_ROUTE)
    assert [get(client).status_code for _ in range(3)] == [200, 200, 429]
    for _ in range(4):
        assert client.get("/api/stats").status_code == 200
        assert client.get("/api/about").status_code == 200
    requests = client.get("/api/stats").json()["route_requests"]
    assert requests["total"] == 3  # the API's own 429 is counted too
    assert requests["errors"] == {"rate_limited": 1}


@pytest.mark.django_db
def test_recent_requests_have_no_locations(client, upstream, stations):
    upstream.respond(OK_ROUTE)
    client.get("/api/route", {"start": "Amarillo, TX", "finish": "Memphis, TN"})
    client.post("/api/route", {"start": "Oklahoma City, OK", "finish": "Nashville, TN"}, format="json")
    text = json.dumps(client.get("/api/stats").json())
    for word in ("Amarillo", "Memphis", "Oklahoma", "Nashville", START, FINISH):
        assert word not in text
    assert {r["method"] for r in client.get("/api/stats").json()["recent"]} == {"GET", "POST"}


@pytest.mark.django_db
def test_warm_up_is_reported_with_what_it_loaded(client, stations):
    from fuelroute.services.places import get_place_index
    from fuelroute.warmup import warm_up

    warm_up()
    warm = client.get("/api/stats").json()["process"]["warm_up"]
    assert warm["places"] == len(get_place_index())
    assert warm["stations"] == 6  # the geocoded stations of the fixture
    assert warm["ms"] > 0


def test_stats_endpoint_answers_get_only(client):
    assert client.post("/api/stats", {}, format="json").json()["error"] == "method_not_allowed"
    assert client.get("/api/stats/").status_code == 200  # trailing slash optional
