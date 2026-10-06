"""manage.py benchmark: the committed data/benchmark.json, an offline run on a saved
route (the first run makes the one routing call that creates it), and /api/stats."""

import json
from datetime import datetime
from io import StringIO
from pathlib import Path

import numpy as np
import pytest
from django.conf import settings as project_settings
from django.core.management import call_command
from rest_framework.test import APIClient

from fuelroute.services import benchmark

from .conftest import osrm_answer
from .test_api import add_stations

SCENARIOS = ["plan_cache_hit", "what_if_replan", "planning_no_network", "route_preparation", "new_trip_our_code"]
NEW_YORK, LOS_ANGELES = (40.7128, -74.006), (34.0522, -118.2437)


def _check_document(document: dict) -> None:
    assert datetime.fromisoformat(document["measured_at"]).tzinfo is not None
    assert document["command"] == "python manage.py benchmark"
    machine = document["machine"]
    assert set(machine) == {"os", "cpu", "logical_processors", "physical_cores", "python", "django", "numpy"}
    assert machine["logical_processors"] >= 1 and machine["python"] and machine["django"]
    # Physical cores are never more than logical processors (hyper-threads); None when unknown.
    assert machine["physical_cores"] is None or 1 <= machine["physical_cores"] <= machine["logical_processors"]
    assert document["external_api_calls"] == 0
    # A new trip replays OSRM's saved answer instead of calling it: once per measured request.
    assert document["osrm_answers_replayed"] >= document["results"]["new_trip_our_code"]["iterations"]
    assert document["stations"] > 0
    assert document["memory"]["rss_mb"] is None or document["memory"]["rss_mb"] > 0
    trip = document["trip"]
    assert trip["start"] == "New York, NY" and trip["finish"] == "Los Angeles, CA"
    assert trip["distance_miles"] > 1000 and trip["corridor_candidates"] > 0
    assert list(document["results"]) == SCENARIOS
    for name, block in document["results"].items():
        assert block["iterations"] >= 1, name
        assert 0 < block["min_ms"] <= block["p50_ms"] <= block["p95_ms"] <= block["max_ms"], name
        assert block["requests_per_second"] > 0 and block["description"], name


def test_committed_benchmark_has_the_documented_shape():
    """data/benchmark.json is measured by `python manage.py benchmark`, never written by hand."""
    path = Path(project_settings.BASE_DIR) / "data" / "benchmark.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    _check_document(document)
    assert document["trip"]["fuel_stops"] >= 4 and document["stations"] > 5000
    # A cached plan is much cheaper than re-planning, which is cheaper than nothing at all.
    results = document["results"]
    assert results["plan_cache_hit"]["p50_ms"] < results["what_if_replan"]["p50_ms"]
    # A new trip is a what-if plus parsing and preparing OSRM's answer.
    assert results["what_if_replan"]["p50_ms"] < results["new_trip_our_code"]["p50_ms"]
    route = json.loads((path.parent / "benchmark_route.json").read_text(encoding="utf-8"))
    assert route["osrm_answer"]["code"] == "Ok" and route["attribution"]


def _synthetic_line(points: int = 600) -> list[tuple[float, float]]:
    lat = np.linspace(NEW_YORK[0], LOS_ANGELES[0], points)
    lon = np.linspace(NEW_YORK[1], LOS_ANGELES[1], points)
    return list(zip(lat.round(5), lon.round(5)))


@pytest.fixture
def benchmark_files(settings, tmp_path):
    settings.FUEL_PLANNER = {
        **settings.FUEL_PLANNER,
        "BENCHMARK_FILE": tmp_path / "benchmark.json",
        "BENCHMARK_ROUTE_FILE": tmp_path / "benchmark_route.json",
    }
    return tmp_path


@pytest.fixture
def stations_on_the_line(db):
    line = _synthetic_line()
    add_stations(
        [(n, lat + 0.02, lon, f"{3.0 + (n % 7) * 0.11:.2f}") for n, (lat, lon) in enumerate(line[5::25], start=1)]
    )


@pytest.mark.django_db
def test_benchmark_runs_offline_on_the_saved_route(upstream, benchmark_files, stations_on_the_line):
    upstream.respond((200, osrm_answer(_synthetic_line(), 2790, hours=41)))
    out = StringIO()
    call_command("benchmark", iterations=4, replans=3, stdout=out)
    assert len(upstream.calls) == 1  # the route file did not exist: ONE routing call creates it
    route_file = benchmark_files / "benchmark_route.json"
    record = json.loads(route_file.read_text(encoding="utf-8"))
    assert record["osrm_answer"]["routes"][0]["geometry"] and record["start"] == "New York, NY"
    document = json.loads((benchmark_files / "benchmark.json").read_text(encoding="utf-8"))
    _check_document(document)
    assert document["results"]["plan_cache_hit"]["iterations"] == 4
    assert document["results"]["what_if_replan"]["iterations"] == 3
    assert "plan_cache_hit" in out.getvalue() and "Wrote" in out.getvalue()

    call_command("benchmark", iterations=2, replans=2, stdout=StringIO())
    assert len(upstream.calls) == 1  # the second run reads the file: no call at all


@pytest.mark.django_db
def test_benchmark_without_write_leaves_the_file_alone(upstream, benchmark_files, stations_on_the_line):
    upstream.respond((200, osrm_answer(_synthetic_line(), 2790, hours=41)))
    call_command("benchmark", iterations=1, replans=1, no_write=True, stdout=StringIO())
    assert not (benchmark_files / "benchmark.json").exists()


@pytest.mark.django_db
def test_stats_publishes_the_benchmark(benchmark_files):
    assert APIClient().get("/api/stats").json()["benchmark"] is None
    document = {"measured_at": "2026-10-06T10:00:00+00:00", "results": {"plan_cache_hit": {"p50_ms": 1.0}}}
    (benchmark_files / "benchmark.json").write_text(json.dumps(document), encoding="utf-8")
    body = APIClient().get("/api/stats").json()
    assert body["benchmark"] == document
    assert "route_requests" in body  # the live counters are still there


def test_process_memory_and_machine_are_read_from_the_system():
    memory = benchmark.process_memory()
    assert memory["kind"] in ("current", "peak") and memory["rss_mb"] > 10
    machine = benchmark.machine()
    assert machine["cpu"] and machine["logical_processors"] >= 1
    # Regression: "22 cores" were logical processors (os.cpu_count()); the cores are counted apart.
    assert machine["physical_cores"] is None or machine["physical_cores"] <= machine["logical_processors"]
