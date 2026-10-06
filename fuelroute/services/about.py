"""What this server is, what it runs and how it meets the assessment: ``GET /api/about``.

``build_about()`` returns one JSON-serializable dict. The planner page embeds it
and ``/api/about`` serves it. Every figure in it is read at run time, never
written by hand:

* versions of the running Python / Django / DRF / numpy / urllib3, and the pins of
  ``requirements.txt``;
* the git commit this checkout is at (and whether it has local changes or is not
  pushed yet), plus the recent history, from ``git`` itself. Links to GitHub point
  to the newest commit GitHub has (``build.linked_commit``) with the line numbers
  of the files AT THAT COMMIT (read with ``git cat-file``), so they open the right
  line even before the latest work is pushed; a symbol, test or commit GitHub does
  not have yet gets no link (``url`` null) instead of a broken one;
* the vehicle and planner configuration (``settings.FUEL_PLANNER``);
* counts of the loaded station data (ORM aggregates and the numpy arrays);
* the last local pytest run, from its JUnit report (``pytest.ini`` writes it),
  flagged ``stale`` when code, templates, JavaScript or CSS changed after it;
* an index of every test function and links to the code of each step, with line
  numbers from the AST of the source files;
* the assessment requirements, each with how it is met and the code / tests that
  show it (``REQUIREMENTS``; a test fails if one points to a missing test or
  symbol, and the texts are templates without digits: numbers come from config);
* the error catalog, built from the ``PlannerError`` classes.

Caches: the data summary until the station data is reloaded, the JUnit parse per file mtime, the AST
per file mtime, and git for ``_GIT_TTL_SECONDS`` (each git command has a timeout;
without git the build fields are null). No network access.
"""

from __future__ import annotations

import ast
import copy
import platform
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import django
import numpy as np
import rest_framework
import urllib3
from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Q, Sum

from ..models import FuelStation
from ..serializers import INCLUDE_VALUES, WHAT_IF_PARAMS, RouteRequestSerializer
from .errors import PlannerError
from .places import get_place_index
from .planner import SAME_PLACE_MILES, START_TANK_HELP, START_TANK_LABELS, PlanSettings
from .stations import COARSE_STEP_MILES, PRICE_POLICIES, data_version, get_station_arrays
from .text import US_STATES

# The contiguous states: where the price file has stations. DC is a district, not a
# state, and is reported apart.
LOWER_48 = tuple(sorted(code for code in US_STATES if code not in ("AK", "HI", "DC")))
_THINNEST = 3

SERVICE = "Spotter fuel route API"
_GIT_TTL_SECONDS = 60.0
_GIT_TIMEOUT_SECONDS = 2.0
_HISTORY_COMMITS = 50
_CONVENTIONAL = re.compile(r"^([a-z]+)(?:\([^)]*\))?!?:\s")

# Code of each step of the pipeline: key -> (path, qualified name).
CODE_LINKS: dict[str, tuple[str, str]] = {
    "serializers.request": ("fuelroute/serializers.py", "RouteRequestSerializer"),
    "geocoding.geocode": ("fuelroute/services/geocoding.py", "geocode"),
    "places.index": ("fuelroute/services/places.py", "PlaceIndex"),
    "usa.in_usa": ("fuelroute/services/usa.py", "in_usa"),
    "planner.plan_trip": ("fuelroute/services/planner.py", "plan_trip"),
    "planner.check_endpoints": ("fuelroute/services/planner.py", "_check_endpoints"),
    "planner.tank_rules": ("fuelroute/services/planner.py", "_tank_rules"),
    "planner.build_stops": ("fuelroute/services/planner.py", "_build_stops"),
    "planner.build_geojson": ("fuelroute/services/planner.py", "_build_geojson"),
    "planner.comparison": ("fuelroute/services/planner.py", "_build_comparison"),
    "planner.pipeline": ("fuelroute/services/planner.py", "_build_pipeline"),
    "osrm.get_route": ("fuelroute/services/osrm.py", "get_route"),
    "osrm.decode_polyline": ("fuelroute/services/osrm.py", "decode_polyline"),
    "osrm.prepare_route": ("fuelroute/services/osrm.py", "prepare_route"),
    "http.client": ("fuelroute/services/http.py", "ExternalApiClient"),
    "stations.stations_along_route": ("fuelroute/services/stations.py", "stations_along_route"),
    "optimizer.greedy": ("fuelroute/services/optimizer.py", "_greedy"),
    "optimizer.consolidate": ("fuelroute/services/optimizer.py", "_consolidate"),
    "optimizer.plan_price_blind": ("fuelroute/services/optimizer.py", "plan_price_blind"),
    "optimizer.plan_quarter_tank": ("fuelroute/services/optimizer.py", "plan_quarter_tank"),
    "station_loader.load_stations": ("fuelroute/services/station_loader.py", "load_stations"),
    "station_loader.resolve_homonym": ("fuelroute/services/station_loader.py", "_resolve_homonym"),
    "middleware.response_time": ("fuelroute/middleware.py", "ResponseTimeMiddleware"),
    "middleware.rate_limit": ("fuelroute/middleware.py", "RateLimitMiddleware"),
    "metrics.route_metrics": ("fuelroute/services/metrics.py", "RouteMetrics"),
    "planner.settings": ("fuelroute/services/planner.py", "PlanSettings"),
    "optimizer.plan_fuel_stops": ("fuelroute/services/optimizer.py", "plan_fuel_stops"),
    "places.search": ("fuelroute/services/places.py", "PlaceIndex.search"),
    "testrunner.run_tests": ("fuelroute/services/testrunner.py", "run_tests"),
    "testrunner.runner_status": ("fuelroute/services/testrunner.py", "runner_status"),
    "benchmark.run_benchmark": ("fuelroute/services/benchmark.py", "run_benchmark"),
}

# The assessment, requirement by requirement. ``brief`` / ``how`` are templates
# formatted with the configuration (``_template_values``); they contain no digits.
# ``tests``: "<file stem>::<function>" under fuelroute/tests/.
REQUIREMENTS: list[dict] = [
    {
        "id": "usa_inputs",
        "kind": "explicit",
        "brief": "The API takes a start and a finish location, both within the USA.",
        "how": (
            "Inputs are 'City, ST' or 'lat,lon'. 'lat,lon' is parsed and 'City, ST' is looked up in an offline index "
            "of US places (Census Gazetteer and GeoNames), so neither costs an external call. Coordinates outside the "
            "US outline are rejected by validation (invalid_request on the field); a place written with a Canadian "
            "province or a Mexican state ('Toronto, ON') is location_outside_usa; Alaska, Hawaii and the territories, "
            "where the price file has no stations, are no_fuel_data_in_region. All of it before any routing call."
        ),
        "sources": ["serializers.request", "geocoding.geocode", "places.index", "usa.in_usa", "planner.check_endpoints"],
        "tests": [
            "test_places::test_every_label_plans_to_the_same_point",
            "test_api::test_points_outside_the_usa_are_400_without_calls",
            "test_api::test_places_written_with_a_region_outside_the_states_are_rejected_without_calls",
            "test_api::test_alaska_and_hawaii_are_422_before_routing",
            "test_geocoding::test_city_state_is_geocoded_offline",
            "test_geo::test_us_mask",
        ],
    },
    {
        "id": "route_map",
        "kind": "explicit",
        "brief": "Return a map of the route.",
        "how": (
            "Every plan has map.geojson (the route line, start, finish and one point per fuel stop) and "
            "map.map_url, a page that draws it with Leaflet on OpenStreetMap tiles."
        ),
        "sources": ["osrm.prepare_route", "planner.build_geojson"],
        "tests": ["test_api::test_route_returns_plan_map_and_uses_one_external_call"],
    },
    {
        "id": "cost_effective_stops",
        "kind": "explicit",
        "brief": "Mark the optimal, cost-effective places to fuel up along the route, based on fuel prices.",
        "how": (
            "Stations within {corridor} mi of the route are the candidates. A greedy algorithm, exact for linear "
            "prices, buys every mile of fuel at the cheapest station that can supply it; then stops under "
            "{min_stop} gal are merged when a fix costs at most ${max_fix}. Each response compares the plan "
            "with a price-blind driver on the same stations."
        ),
        "sources": [
            "stations.stations_along_route", "optimizer.greedy", "optimizer.consolidate",
            "optimizer.plan_price_blind", "planner.comparison",
        ],
        "tests": [
            "test_optimizer::test_greedy_matches_exact_dp",
            "test_optimizer::test_consolidated_plan_is_feasible_and_close_to_optimal",
            "test_optimizer::test_price_blind_driver_buys_the_same_gallons_and_never_beats_the_optimum",
            "test_geo::test_corridor_search_matches_brute_force",
        ],
    },
    {
        "id": "range_multiple_stops",
        "kind": "explicit",
        "brief": "The vehicle has a range of {range} miles, so a trip may need several fuel stops.",
        "how": (
            "The optimizer tracks the tank along the route: it never goes below empty nor above the {tank}-gallon "
            "tank, and the plan has as many stops as the trip needs. A stretch without stations longer than the "
            "range is an error (no_reachable_fuel_station) that says where it is. The range, the mpg and a safety "
            "reserve (gallons that must be left at every stop and at the destination) can be changed per request "
            "with max_range_miles, mpg and safety_reserve_gal, without touching the code."
        ),
        "sources": ["optimizer.greedy", "planner.tank_rules", "planner.settings"],
        "tests": [
            "test_optimizer::test_long_trip_needs_several_stops_and_never_runs_dry",
            "test_api::test_gap_longer_than_the_range_is_422_and_says_where",
            "test_what_if::test_safety_reserve_is_kept_at_every_stop_and_at_the_destination",
            "test_optimizer::test_safety_reserve_is_the_optimum_of_a_smaller_tank",
        ],
    },
    {
        "id": "total_cost_mpg",
        "kind": "explicit",
        "brief": "Return the total money spent on fuel, at {mpg} miles per gallon.",
        "how": (
            "summary.total_fuel_cost is the sum of the stop costs. With start_tank=empty (the default) every "
            "mile driven is paid for, so the gallons bought are the miles divided by {mpg}, except where the price "
            "file has no station to buy from (then the gallons burned there are reported as unpriced, with a "
            "warning); start_tank=full counts only the fuel bought on the way."
        ),
        "sources": ["planner.tank_rules", "planner.build_stops"],
        "tests": [
            "test_api::test_stop_costs_add_up_to_the_total_to_the_cent",
            "test_optimizer::test_empty_start_pays_for_every_mile",
            "test_api::test_post_json_and_full_tank_mode",
            "test_what_if::test_each_setting_changes_the_plan_with_no_external_call",
        ],
    },
    {
        "id": "price_file",
        "kind": "explicit",
        "brief": "Use the provided file of fuel prices.",
        "how": (
            "load_stations reads the OPIS file once. A station listed several times keeps all its quotes and "
            "plans with the {policy}; each station gets city-level coordinates offline, and ambiguous city "
            "names are left without coordinates instead of guessed."
        ),
        "sources": ["station_loader.load_stations", "station_loader.resolve_homonym"],
        "tests": [
            "test_station_loader::test_parse_keeps_every_quote_of_a_duplicated_station",
            "test_station_loader::test_load_stores_the_policy_price_and_the_spread",
            "test_station_loader::test_homonyms_are_resolved_from_the_address_or_left_out",
        ],
    },
    {
        "id": "free_routing_api",
        "kind": "explicit",
        "brief": "Use a free API for the map and the route.",
        "how": (
            "The public OSRM server (no key) returns the driving route as an encoded polyline, and the map uses "
            "OpenStreetMap tiles. Nominatim is called only for text the offline index cannot place (an address, a "
            "landmark, a city without its state)."
        ),
        "sources": ["osrm.get_route", "http.client"],
        "tests": ["test_api::test_route_returns_plan_map_and_uses_one_external_call"],
    },
    {
        "id": "django_version",
        "kind": "explicit",
        "brief": "Build it with the latest stable Django.",
        "how": (
            "Pinned in requirements.txt and read from Django itself at run time. Latest stable release on PyPI "
            "when it was last checked: {django_latest} ({django_checked_on})."
        ),
        "sources": [],
        "tests": [],
    },
    {
        "id": "fast",
        "kind": "explicit",
        "brief": "Return results quickly: the quicker the better.",
        "how": (
            "Offline geocoding, one routing call with a compact polyline, a numpy corridor search in three passes, "
            "a reused HTTPS connection, a route cache and a plan cache (a repeated trip makes no external call), "
            "one routing call for identical concurrent trips, and gzip on the wire. Every response carries "
            "X-Response-Time-ms and a Server-Timing breakdown, and manage.py benchmark measures cached plans, "
            "what-if re-plans and the planner itself on this machine (published by /api/stats)."
        ),
        "sources": [
            "osrm.get_route", "stations.stations_along_route", "planner.plan_trip", "middleware.response_time",
            "benchmark.run_benchmark",
        ],
        "tests": [
            "test_api::test_second_request_uses_the_plan_cache",
            "test_api::test_identical_concurrent_requests_make_one_routing_call",
            "test_api::test_server_timing_header_breaks_down_the_request",
            "test_benchmark::test_committed_benchmark_has_the_documented_shape",
            "test_places::test_search_is_fast_on_the_real_index",
        ],
    },
    {
        "id": "few_routing_calls",
        "kind": "explicit",
        "brief": "Call the routing API as little as possible: one call is ideal, two or three acceptable.",
        "how": (
            "One OSRM call per new trip, {retry_rule}. A repeated trip, the other start_tank mode, every what-if "
            "setting and the planner page reuse the cached plan or route (kept {ttl_text} in each server process) "
            "with no call. "
            "meta.external_api_calls counts the calls on every response, errors included, and /api/stats counts "
            "them per service since the server started."
        ),
        "sources": ["osrm.get_route", "http.client", "metrics.route_metrics"],
        "tests": [
            "test_api::test_route_returns_plan_map_and_uses_one_external_call",
            "test_api::test_second_request_uses_the_plan_cache",
            "test_api::test_identical_concurrent_requests_make_one_routing_call",
            "test_api::test_errors_report_the_external_calls_already_made",
            "test_what_if::test_settings_are_in_the_plan_cache_key_not_the_route_cache_key",
        ],
    },
    {
        "id": "deliverables",
        "kind": "explicit",
        "brief": "Share the code on GitHub, with a Postman demo in a short Loom video.",
        "how": (
            "The repository has the code, the README, postman/collection.json with the trips and edge cases, "
            "and the Loom script; the video link is published here when LOOM_URL is set. This server reports the "
            "commit it runs and links to the code as GitHub has it."
        ),
        "sources": [],
        "tests": [],
    },
    {
        "id": "exact_money",
        "kind": "implicit",
        "brief": "Money that adds up.",
        "how": (
            "Gallons are rounded to hundredths first and each cost is gallons times the four-decimal price, "
            "rounded to the cent in Decimal; the totals are the sums of the rows, so the stops add up to the "
            "total to the cent."
        ),
        "sources": ["planner.build_stops"],
        "tests": ["test_api::test_stop_costs_add_up_to_the_total_to_the_cent"],
    },
    {
        "id": "robust_errors",
        "kind": "implicit",
        "brief": "Clear errors for bad input and for failures of the free services.",
        "how": (
            "Every error has the same body (error, detail, meta), the framework's and the rate limit's included. "
            "Invalid input is rejected before any external call; external calls have connect and read timeouts and "
            "are {retry_rule}; an upstream rate limit becomes upstream_busy with Retry-After; each client may send "
            "{rate} requests per minute to /api/route (the page itself is not counted)."
        ),
        "sources": ["serializers.request", "http.client", "middleware.rate_limit"],
        "tests": [
            "test_api::test_invalid_input_returns_400",
            "test_what_if::test_out_of_range_settings_are_400",
            "test_api::test_every_error_code_of_the_catalog_has_the_same_body_with_meta",
            "test_api::test_transient_failure_is_retried_once",
            "test_api::test_routing_service_down_returns_502",
            "test_api::test_own_rate_limit_answers_429",
        ],
    },
    {
        "id": "messy_data",
        "kind": "implicit",
        "brief": "Cope with a messy price file.",
        "how": (
            "Duplicated stations are merged keeping the spread of their quotes; city names are normalized "
            "(St. becomes Saint); a city name that exists several times in a state is resolved from the highway "
            "in the address or left out; Canadian stations are skipped."
        ),
        "sources": ["station_loader.load_stations", "station_loader.resolve_homonym"],
        "tests": [
            "test_station_loader::test_homonyms_are_resolved_from_the_address_or_left_out",
            "test_station_loader::test_load_geocodes_offline_and_reports_unmatched",
        ],
    },
    {
        "id": "production_ready",
        "kind": "implicit",
        "brief": "Behave like a service, not a script.",
        "how": (
            "Station data lives in numpy arrays reloaded when the data version changes; plans and routes are "
            "cached for {ttl} s; identical concurrent trips share one routing call; a pooled HTTPS client with "
            "timeouts; a rate limit; key=value logs; Server-Timing, /api/stats and /api/about."
        ),
        "sources": ["osrm.get_route", "http.client", "middleware.rate_limit", "metrics.route_metrics"],
        "tests": [
            "test_api::test_identical_concurrent_requests_make_one_routing_call",
            "test_api::test_rate_limited_upstream_returns_503_with_retry_after",
            "test_api::test_new_station_data_is_used_without_restart",
        ],
    },
    {
        "id": "tested",
        "kind": "implicit",
        "brief": "Tested.",
        "how": (
            "A pytest suite that cannot touch the network (an autouse fixture makes any real HTTP call fail; the "
            "routing service is faked): the greedy against an exact dynamic-programming solution, the corridor "
            "search against brute force, the price file loader, the API end to end and this page's contract. The "
            "page reads the report of the last local run and, when the server runs on your own machine, runs the "
            "whole suite itself (POST /api/tests/run) and shows every test with what it checks."
        ),
        "sources": ["testrunner.run_tests", "testrunner.runner_status"],
        "tests": [
            "test_testrunner::test_run_answers_every_test_grouped_by_file",
            "test_testrunner::test_runner_answers_403_unless_the_request_is_local",
        ],
    },
]

# Errors produced by DRF / the rate limit rather than by a PlannerError.
HTTP_ERRORS = [
    ("parse_error", 400, "The request body is not valid JSON."),
    ("not_found", 404, "There is no endpoint at that path."),
    ("method_not_allowed", 405, "The endpoint does not accept that HTTP method."),
    ("not_acceptable", 406, "The Accept header asks for a format the API does not produce (it answers JSON)."),
    ("unsupported_media_type", 415, "A POST body must be JSON (or a form)."),
    ("rate_limited", 429, "Too many requests per minute from one client to /api/route; see Retry-After."),
]

ENDPOINTS = [
    {"method": "GET|POST", "path": "/api/route", "description": "Plan a trip: route, cheapest fuel stops, total cost and map (JSON)."},
    {"method": "GET", "path": "/api/route/map", "description": "The planner page (HTML), a client of /api/route."},
    {"method": "GET", "path": "/api/places", "description": "Type-ahead of US places (offline index, no external call)."},
    {"method": "GET", "path": "/api/stats", "description": "Requests, external calls and latency since this server process started, and the last benchmark."},
    {"method": "GET", "path": "/api/about", "description": "Versions, configuration, loaded data, requirements, tests and errors."},
    {"method": "GET", "path": "/api/tests", "description": "The test inventory and the last test run."},
    {"method": "POST", "path": "/api/tests/run", "description": "Run the test suite (only when the server runs on your own machine)."},
    {"method": "GET", "path": "/", "description": "Browsers are sent to the planner page; API clients get a JSON index."},
]

_lock = threading.Lock()
_git_cache: dict = {"at": None, "value": None}
_junit_cache: dict = {}
_ast_cache: dict = {}
_blob_cache: dict = {}  # (commit, path) -> symbols of that file at that commit (immutable)
_data_cache: dict = {}


def reset_caches() -> None:
    """Forget every cached part (tests)."""
    with _lock:
        _git_cache.update(at=None, value=None)
        _junit_cache.clear()
        _ast_cache.clear()
        _blob_cache.clear()
        _data_cache.clear()


def _base_dir() -> Path:
    return Path(settings.BASE_DIR)


# --- versions and build ----------------------------------------------------------------------


def _versions() -> dict:
    return {
        "python": platform.python_version(),
        "django": django.get_version(),
        "djangorestframework": rest_framework.VERSION,
        "numpy": np.__version__,
        "urllib3": urllib3.__version__,
    }


def _pinned() -> dict:
    """``name==version`` lines of requirements.txt."""
    pins = {}
    try:
        lines = (_base_dir() / "requirements.txt").read_text(encoding="utf-8").splitlines()
    except OSError:
        return pins
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip()] = version.strip()
    return pins


def _git(*args: str, stdin: bytes | None = None) -> str | None:
    """stdout of a git command in the project directory, or None (no git, not a repo, timeout)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_base_dir(),
            capture_output=True,
            input=stdin,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _git_info(repo_url: str) -> dict:
    """Commit, local changes, what GitHub has, and recent history. Cached for a minute.

    "On GitHub" means reachable from a remote-tracking branch (as of the last fetch
    or push). ``linked_commit``: the newest commit of this history that GitHub has,
    the one every link points to; null when GitHub has none of them.
    """
    now = time.monotonic()
    with _lock:
        if _git_cache["at"] is not None and now - _git_cache["at"] < _GIT_TTL_SECONDS:
            return _git_cache["value"]
    log = _git("log", f"-n{_HISTORY_COMMITS}", "--format=%H%x1f%cI%x1f%s")
    status = _git("status", "--porcelain", "--untracked-files=no") if log else None
    remote = _git("rev-list", "--remotes", "--max-count=2000") if log else None
    on_github = set((remote or "").split())
    history = []
    for line in (log or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        kind = _CONVENTIONAL.match(subject)
        pushed = sha in on_github
        history.append(
            {
                "commit": sha,
                "commit_short": sha[:7],
                "date": date,
                "type": kind.group(1) if kind else None,
                "subject": subject,
                "pushed": pushed,
                "url": f"{repo_url}/commit/{sha}" if pushed else None,
            }
        )
    head = history[0] if history else None
    linked = next((c for c in history if c["pushed"]), None)
    value = {
        "repo_url": repo_url,
        "commit": head["commit"] if head else None,
        "commit_short": head["commit_short"] if head else None,
        "commit_time": head["date"] if head else None,
        "subject": head["subject"] if head else None,
        "dirty": None if status is None else bool(status.strip()),
        "pushed": None if remote is None else bool(head and head["pushed"]),
        "linked_commit": linked["commit"] if linked else None,
        "linked_commit_short": linked["commit_short"] if linked else None,
        "commits_not_on_github": None if remote is None else sum(1 for c in history if not c["pushed"]),
        "history": history,
    }
    with _lock:
        _git_cache.update(at=now, value=value)
    return value


def _git_blobs(commit: str, paths: list[str]) -> dict[str, str | None]:
    """Text of each file at ``commit`` (None when it does not exist there), in ONE git call."""
    request = "".join(f"{commit}:{path}\n" for path in paths).encode()
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch"], cwd=_base_dir(), input=request, capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    out, position, texts = result.stdout, 0, {}
    for path in paths:
        end = out.find(b"\n", position)
        if end < 0:
            break
        header = out[position:end].split()
        position = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts[path] = out[position:position + size].decode("utf-8", errors="replace")
            position += size + 1  # the blob is followed by a newline
        else:  # "<commit>:<path> missing"
            texts[path] = None
    return texts


# --- tests: the JUnit report of the last pytest run, and an index of the test functions ----


def _case_key(classname: str, name: str) -> str:
    """JUnit (classname, name) -> test_index key "<path>::<function>" (parameters dropped)."""
    parts = classname.split(".")
    function = name.split("[", 1)[0]
    path = "/".join(parts) + ".py"
    if not (_base_dir() / path).exists() and len(parts) > 1:  # a test class: module.Class
        return f"{'/'.join(parts[:-1])}.py::{parts[-1]}::{function}"
    return f"{path}::{function}"


def _junit_summary(path: Path) -> tuple[dict, dict | None]:
    """(``tests`` block, per-function results) of a JUnit report; results None if missing."""
    missing = {
        "report": "missing", "ran_at": None, "duration_seconds": None,
        "total": None, "passed": None, "failed": None, "errors": None, "skipped": None, "stale": None,
    }
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return missing, None
    with _lock:
        cached = _junit_cache.get(str(path))
    if cached and cached[0] == mtime:
        summary, cases = cached[1], cached[2]
    else:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            return missing, None
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        duration, ran_at = 0.0, None
        cases: dict[str, dict] = {}
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.get(key, 0) or 0)
            duration += float(suite.get("time", 0) or 0)
            ran_at = ran_at or suite.get("timestamp")
            for case in suite.iter("testcase"):
                result = cases.setdefault(
                    _case_key(case.get("classname", ""), case.get("name", "")), {"cases": 0, "passed": 0, "failed": 0}
                )
                result["cases"] += 1
                if case.find("failure") is not None or case.find("error") is not None:
                    result["failed"] += 1
                elif case.find("skipped") is None:
                    result["passed"] += 1
        if ran_at:
            try:
                parsed = datetime.fromisoformat(ran_at)
                ran_at = (parsed if parsed.tzinfo else parsed.astimezone()).astimezone(UTC).isoformat()
            except ValueError:
                ran_at = None
        if not ran_at:
            ran_at = datetime.fromtimestamp(mtime, UTC).isoformat()
        failed = totals["failures"]
        summary = {
            "report": "found",
            "ran_at": ran_at,
            "duration_seconds": round(duration, 2),
            "total": totals["tests"],
            "passed": totals["tests"] - failed - totals["errors"] - totals["skipped"],
            "failed": failed,
            "errors": totals["errors"],
            "skipped": totals["skipped"],
        }
        with _lock:
            _junit_cache[str(path)] = (mtime, summary, cases)
    return {**summary, "stale": _code_changed_since(mtime)}, cases


_STALE_PATTERNS = ("*.py", "*.html", "*.js", "*.css")


def _code_changed_since(mtime: float) -> bool:
    """Is any code, template, script or stylesheet of the app, or the settings, newer than the report?"""
    base = _base_dir()
    for folder in ("fuelroute", "config"):
        for pattern in _STALE_PATTERNS:
            for path in (base / folder).rglob(pattern):
                try:
                    if path.stat().st_mtime > mtime:
                        return True
                except OSError:
                    continue
    return False


def _symbols_of(source: str) -> dict[str, tuple[int, int]]:
    """Qualified name -> (first line, last line) of every function / class / method of a source."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    symbols: dict[str, tuple[int, int]] = {}
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def visit(nodes, prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, kinds):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                symbols[prefix + node.name] = (first, node.end_lineno or node.lineno)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body)
    return symbols


def _symbols(path: Path) -> dict[str, tuple[int, int]]:
    """Symbols of a file of this checkout (cached per modification time)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        cached = _ast_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        symbols = _symbols_of(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    with _lock:
        _ast_cache[str(path)] = (mtime, symbols)
    return symbols


class _GitHubLines:
    """Line numbers of symbols as GitHub shows them at ``build.linked_commit``.

    When that commit is the checkout itself (pushed, no local changes) the files on
    disk are used. Otherwise each file is read from git at that commit, all in one
    ``git cat-file`` call, and cached for good (a commit never changes). Without git
    the links fall back to ``main`` and the local lines (best effort).
    """

    def __init__(self, build: dict, paths: list[str]):
        self.commit = build.get("linked_commit")
        self.local = self.commit is None or (self.commit == build.get("commit") and build.get("dirty") is False)
        self.ref = self.commit or ("main" if build.get("commit") is None else None)
        self.symbols: dict[str, dict] = {}
        if self.local:
            return
        missing = []
        with _lock:
            for path in paths:
                cached = _blob_cache.get((self.commit, path))
                if cached is None:
                    missing.append(path)
                else:
                    self.symbols[path] = cached
        if missing:
            texts = _git_blobs(self.commit, missing)
            for path in missing:
                if path not in texts:  # git failed: unknown, not cached
                    continue
                symbols = _symbols_of(texts[path]) if texts[path] is not None else {}
                self.symbols[path] = symbols
                with _lock:
                    _blob_cache[(self.commit, path)] = symbols

    def lines(self, path: str, symbol: str) -> tuple[int, int] | None:
        if self.ref is None:  # git works but GitHub has none of this history
            return None
        if self.local:
            return _symbols(_base_dir() / path).get(symbol)
        return self.symbols.get(path, {}).get(symbol)

    def url(self, repo_url: str, path: str, symbol: str, whole_range: bool = True) -> tuple[str | None, list | None]:
        found = self.lines(path, symbol)
        if not found:
            return None, None
        start, end = found
        return _blob_url(repo_url, self.ref, path, start, end if whole_range else None), [start, end]


def _blob_url(repo_url: str, ref: str, path: str, start: int | None = None, end: int | None = None) -> str:
    url = f"{repo_url}/blob/{ref}/{path}"
    if start:
        url += f"#L{start}" + (f"-L{end}" if end and end != start else "")
    return url


def _test_files() -> list[str]:
    base = _base_dir()
    return [path.relative_to(base).as_posix() for path in sorted((base / "fuelroute" / "tests").glob("test_*.py"))]


def _test_index(repo_url: str, github: _GitHubLines, results: dict | None) -> dict:
    index = {}
    base = _base_dir()
    for relative in _test_files():
        for name, (start, _end) in _symbols(base / relative).items():
            if not name.split(".")[-1].startswith("test"):
                continue
            key = f"{relative}::{name.replace('.', '::')}"
            result = None if results is None else results.get(key, {"cases": 0, "passed": 0, "failed": 0})
            url, _lines = github.url(repo_url, relative, name, whole_range=False)
            index[key] = {
                "path": relative,
                "line": start,
                "url": url,  # null: this test is not on GitHub yet
                "cases": None if result is None else result["cases"],
                "passed": None if result is None else result["passed"],
                "failed": None if result is None else result["failed"],
            }
    return index


def _code_links(repo_url: str, github: _GitHubLines) -> dict:
    links = {}
    for key, (path, symbol) in CODE_LINKS.items():
        start, end = _symbols(_base_dir() / path).get(symbol, (None, None))
        url, url_lines = github.url(repo_url, path, symbol)
        links[key] = {
            "path": path,
            "symbol": symbol,
            "start_line": start,  # in this checkout
            "end_line": end,
            "url": url,  # null: not on GitHub yet (new file or symbol)
            "url_lines": url_lines,  # the lines the url opens, at build.linked_commit
        }
    return links


def expand_test_id(short: str) -> str:
    """"test_api::test_x" -> "fuelroute/tests/test_api.py::test_x" (a test_index key)."""
    module, function = short.split("::", 1)
    return f"fuelroute/tests/{module}.py::{function}"


# --- data, configuration, requirements, errors ------------------------------------------------


def _data_summary() -> dict:
    """Counts of the loaded station data.

    Cached as long as the station arrays are the same object: ``get_station_arrays``
    reloads them exactly when the data version changes (load_stations), so the
    summary is rebuilt at the same moment.
    """
    config = settings.FUEL_PLANNER
    try:
        version = data_version()
        arrays = get_station_arrays(version)
    except DatabaseError:  # not migrated yet
        version, arrays = None, None
    with _lock:
        if arrays is not None and _data_cache.get("arrays") is arrays:
            return _data_cache["summary"]
    summary = {
        "version": version,
        "price_file": Path(config["FUEL_PRICES_FILE"]).name,
        "stations": 0,
        "price_quotes": 0,
        "stations_with_several_quotes": 0,
        "geocoded": 0,
        "geocoded_by_source": {"census": 0, "geonames": 0},
        "not_geocoded": 0,
        "states": 0,
        "stations_by_state": [],
        "states_without_stations": [],
        "thinnest_states": [],
        "district_of_columbia_geocoded": 0,
        "price_per_gallon": None,
        "places_index_entries": len(get_place_index()),
    }
    if version is None:
        return summary
    located = Q(latitude__isnull=False)
    totals = FuelStation.objects.aggregate(
        stations=Count("id"),
        quotes=Sum("price_rows"),
        several=Count("id", filter=Q(price_rows__gt=1)),
        geocoded=Count("id", filter=located),
    )
    by_source = FuelStation.objects.filter(located).values("geocode_source").annotate(n=Count("id"))
    by_state = list(
        FuelStation.objects.values("state")
        .annotate(stations=Count("id"), geocoded=Count("id", filter=located))
        .order_by("-stations", "state")
    )
    geocoded_in = {row["state"]: row["geocoded"] for row in by_state}
    in_lower_48 = sorted((geocoded_in.get(code, 0), code) for code in LOWER_48)
    prices = arrays.price
    summary.update(
        stations=totals["stations"],
        price_quotes=totals["quotes"] or 0,
        stations_with_several_quotes=totals["several"],
        geocoded=totals["geocoded"],
        not_geocoded=totals["stations"] - totals["geocoded"],
        states=len(by_state),
        stations_by_state=[
            {"state": row["state"], "stations": row["stations"], "geocoded": row["geocoded"]} for row in by_state
        ],
        # Lower 48 only: a state whose stations all lack coordinates counts as without.
        states_without_stations=[code for count, code in in_lower_48 if count == 0],
        thinnest_states=[{"state": code, "geocoded": count} for count, code in in_lower_48 if count > 0][:_THINNEST],
        district_of_columbia_geocoded=geocoded_in.get("DC", 0),
        price_per_gallon=(
            {
                "min": round(float(prices.min()), 4),
                "median": round(float(np.median(prices)), 4),
                "max": round(float(prices.max()), 4),
            }
            if len(prices)
            else None
        ),
    )
    for row in by_source:
        if row["geocode_source"]:
            summary["geocoded_by_source"][row["geocode_source"]] = row["n"]
    with _lock:
        _data_cache.update(arrays=arrays, summary=summary)
    return summary


def _plan_cache_seconds() -> int | None:
    timeout = settings.CACHES.get("default", {}).get("TIMEOUT", 300)
    return None if timeout is None else int(timeout)


def _vehicle(config: dict) -> dict:
    return {
        "max_range_miles": config["MAX_RANGE_MILES"],
        "miles_per_gallon": config["MILES_PER_GALLON"],
        "tank_gallons": round(config["MAX_RANGE_MILES"] / config["MILES_PER_GALLON"], 1),
        "start_reserve_miles": config["START_RESERVE_MILES"],
        "min_stop_gallons": config["MIN_STOP_GALLONS"],
        "max_consolidation_cost": config["MAX_CONSOLIDATION_COST"],
    }


def _planner(config: dict) -> dict:
    return {
        "corridor_miles": config["CORRIDOR_MILES"],
        "coarse_step_miles": COARSE_STEP_MILES,
        "resample_miles": config["RESAMPLE_MILES"],
        "max_road_snap_miles": config["MAX_ROAD_SNAP_MILES"],
        "same_place_miles": SAME_PLACE_MILES,
        "price_policy": config["PRICE_POLICY"],
        "plan_cache_seconds": _plan_cache_seconds(),
        "cache_max_entries": settings.CACHES.get("default", {}).get("OPTIONS", {}).get("MAX_ENTRIES"),
        "rate_limit_per_minute": config["RATE_LIMIT_PER_MINUTE"],
        "http_retries": config["HTTP_RETRIES"],
        "http_connect_timeout_seconds": config["HTTP_CONNECT_TIMEOUT_SECONDS"],
        "http_read_timeout_seconds": config["HTTP_READ_TIMEOUT_SECONDS"],
        "quarter_tank_fraction": config["BASELINE_REFUEL_FRACTION"],
    }


def _external_services(config: dict) -> list[dict]:
    return [
        {"name": "osrm", "purpose": "Driving route: one call per new trip, cached.", "url": config["OSRM_URL"]},
        {
            "name": "nominatim",
            "purpose": "Geocoding of text the offline index cannot place: an address, a landmark (rare, cached).",
            "url": config["NOMINATIM_URL"],
        },
        {
            "name": "openstreetmap_tiles",
            "purpose": "Map tiles of the planner page, loaded by the browser.",
            "url": "https://tile.openstreetmap.org",
        },
    ]


def _params() -> list[dict]:
    """Parameters of /api/route: help text, choices, and for the what-if settings their
    default (the configuration) and allowed range."""
    defaults = PlanSettings.defaults()
    params = []
    for name, field in RouteRequestSerializer().fields.items():
        choices = getattr(field, "choices", None)
        what_if = name in WHAT_IF_PARAMS
        default = getattr(defaults, name, None) if what_if or name == "start_tank" else None
        params.append(
            {
                "name": name,
                "required": bool(field.required),
                "choices": list(choices) if choices else None,
                "help_text": str(field.help_text or ""),
                "what_if": what_if,
                "default": default,
                "min": getattr(field, "min_value", None),
                "max": getattr(field, "max_value", None),
            }
        )
    return params


def _template_values(config: dict) -> dict:
    return {
        "range": f"{config['MAX_RANGE_MILES']:g}",
        "mpg": f"{config['MILES_PER_GALLON']:g}",
        "tank": f"{config['MAX_RANGE_MILES'] / config['MILES_PER_GALLON']:g}",
        "corridor": f"{config['CORRIDOR_MILES']:g}",
        "min_stop": f"{config['MIN_STOP_GALLONS']:g}",
        "max_fix": f"{config['MAX_CONSOLIDATION_COST']:.2f}",
        "policy": f"{config['PRICE_POLICY']} of its quotes",
        "retries": int(config["HTTP_RETRIES"]),
        "retry_rule": _retry_rule(int(config["HTTP_RETRIES"])),
        "rate": config["RATE_LIMIT_PER_MINUTE"],
        "ttl": _plan_cache_seconds(),
        "ttl_text": _duration_text(_plan_cache_seconds()),
        "django_latest": config["DJANGO_LATEST_STABLE"],
        "django_checked_on": config["DJANGO_LATEST_CHECKED_ON"],
    }


def _retry_rule(retries: int) -> str:
    if retries <= 0:
        return "never retried"
    if retries == 1:
        return "retried once on a transient failure"
    return f"retried up to {retries} times on a transient failure"


def _duration_text(seconds: int | None) -> str:
    if seconds is None:
        return "with no expiry"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"for {hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"for {minutes} minute{'s' if minutes != 1 else ''}"
    return f"for {seconds} seconds"


def _requirements(config: dict) -> list[dict]:
    values = _template_values(config)
    return [
        {
            "id": item["id"],
            "kind": item["kind"],
            "brief": item["brief"].format(**values),
            "how": item["how"].format(**values),
            "sources": list(item["sources"]),
            "tests": [expand_test_id(short) for short in item["tests"]],
        }
        for item in REQUIREMENTS
    ]


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def error_catalog() -> list[dict]:
    """Every error code the API can answer: the PlannerError classes plus DRF / rate limit."""
    found: dict[str, dict] = {}

    def visit(cls) -> None:
        found.setdefault(
            cls.code, {"code": cls.code, "status": cls.status_code, "description": _first_line(cls.__doc__)}
        )
        for subclass in cls.__subclasses__():
            visit(subclass)

    visit(PlannerError)
    for code, status, description in HTTP_ERRORS:
        found.setdefault(code, {"code": code, "status": status, "description": description})
    return sorted(found.values(), key=lambda error: (error["status"], error["code"]))


def build_about() -> dict:
    """The body of ``GET /api/about`` (JSON-serializable)."""
    config = settings.FUEL_PLANNER
    repo_url = str(config["REPO_URL"]).rstrip("/")
    build = _git_info(repo_url)
    github = _GitHubLines(build, sorted({path for path, _ in CODE_LINKS.values()} | set(_test_files())))
    tests, results = _junit_summary(Path(config["TEST_REPORT_FILE"]))
    return {
        "service": SERVICE,
        "versions": _versions(),
        "pinned": _pinned(),
        "build": copy.deepcopy(build),  # cached parts are copied: callers may change theirs
        "vehicle": _vehicle(config),
        "planner": _planner(config),
        "external_services": _external_services(config),
        "api": {
            "include_values": list(INCLUDE_VALUES),
            "params": _params(),
            "state_codes": sorted(US_STATES),
            "what_if_params": list(WHAT_IF_PARAMS),
            "price_policies": list(PRICE_POLICIES),
            "start_tank_modes": [
                {"value": mode, "label": START_TANK_LABELS[mode], "help": START_TANK_HELP[mode]}
                for mode in START_TANK_LABELS
            ],
        },
        "deliverables": {
            "loom_url": str(config.get("LOOM_URL") or "") or None,
            "postman_collection": "postman/collection.json",
            "release_check": {
                "django_latest_stable": config["DJANGO_LATEST_STABLE"],
                "checked_on": config["DJANGO_LATEST_CHECKED_ON"],
            },
        },
        "data": copy.deepcopy(_data_summary()),
        "tests": tests,
        "test_index": _test_index(repo_url, github, results),
        "code_links": _code_links(repo_url, github),
        "requirements": _requirements(config),
        "errors": error_catalog(),
        "endpoints": [dict(endpoint) for endpoint in ENDPOINTS],
    }
