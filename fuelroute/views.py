"""HTTP layer of the JSON API: validates the input, calls the planner, shapes the response.

* ``GET|POST /api/route`` -> JSON plan (``RoutePlanView``), with optional what-if settings
* ``GET /api/places``     -> type-ahead of US places from the offline index (``PlacesView``)
* ``GET /api/stats``      -> requests, external calls and latency since start (``StatsView``)
* ``GET /api/about``      -> versions, config, data, requirements, tests, errors (``AboutView``)
* ``GET /api/tests``      -> the test inventory and the last run (``SuiteView``)
* ``POST /api/tests/run`` -> run pytest on this machine, local requests only (``SuiteRunView``)
* ``GET /``               -> browsers: redirect to the planner page; API clients: a small JSON index
* anything else under ``/api/`` -> JSON 404

The planner page (``/api/route/map``) is ``web.py``: a client of these endpoints.

Every error has the body ``{"error": "<code>", "detail": ..., "meta": {...}}``
where ``meta`` says how many external calls were made before the error: the
planner's errors, the serializer's, DRF's own (bad JSON, 405, 406, 415), the JSON
404 and the rate limit's 429 (``no_calls_meta``: those never reach an external
service). Every ``/api/route`` response, errors included, carries a
``Server-Timing`` header with the time of each step (``server_timing``).
"""

import time
from collections import defaultdict

from django.http import JsonResponse
from django.shortcuts import redirect
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

from .serializers import PlacesRequestSerializer, RouteRequestSerializer
from .services import testrunner
from .services.about import build_about
from .services.errors import PlannerError
from .services.http import ExternalApiClient
from .services.metrics import route_metrics
from .services.places import get_place_index
from .services.planner import plan_trip


def no_calls_meta() -> dict:
    """``meta`` of an error answered before any external call could be made."""
    return {"external_api_calls": 0, "external_api_services": []}


# meta.timings_ms key -> Server-Timing metric name, in the order of the pipeline.
# The external calls are listed right after "routing" (one entry per service).
_TIMING_ENTRIES = (
    ("geocoding_ms", "geocoding"),
    ("plan_cache_ms", "plan-cache"),
    ("routing_ms", "routing"),
    ("corridor_ms", "corridor"),
    ("optimizer_ms", "optimizer"),
    ("response_build_ms", "build"),
    ("comparison_ms", "comparison"),
    ("candidates_ms", "candidates"),
)


def _calls_text(count: int) -> str:
    return f"{count} call" if count == 1 else f"{count} calls"


def _external_entries(meta: dict, calls: list[tuple[str, float]] | None) -> list[str]:
    if calls is None:  # only the totals are known
        count = int(meta.get("external_api_calls") or 0)
        if not count:
            return []
        return [f'osrm;dur={float(meta.get("external_api_ms") or 0):.1f};desc="{_calls_text(count)}"']
    per_service: dict[str, list[float]] = defaultdict(list)
    for service, ms in calls:
        per_service[service].append(ms)
    return [
        f'{service};dur={sum(times):.1f};desc="{_calls_text(len(times))}"' for service, times in per_service.items()
    ]


def server_timing(meta: dict, calls: list[tuple[str, float]] | None = None) -> str:
    """``Server-Timing`` value from ``meta.timings_ms`` and the external calls.

    e.g. ``geocoding;dur=0.4, plan-cache;dur=0.6, routing;dur=812.3, osrm;dur=790.1;desc="1 call", ...``
    The middleware appends ``render`` and ``total``.
    """
    timings = meta.get("timings_ms") or {}
    entries = []
    for key, name in _TIMING_ENTRIES:
        if key in timings:
            entries.append(f"{name};dur={float(timings[key]):.1f}")
        if key == "routing_ms":
            entries += _external_entries(meta, calls)
    return ", ".join(entries)


def _plan_from(data) -> tuple[dict | None, dict | None, int, dict, ExternalApiClient | None]:
    """Validate ``data`` and plan the trip: (result, error_body, status, headers, client)."""
    serializer = RouteRequestSerializer(data=data)
    if not serializer.is_valid():
        return None, {"error": "invalid_request", "detail": serializer.errors, "meta": no_calls_meta()}, 400, {}, None
    client = ExternalApiClient()
    try:
        result = plan_trip(**serializer.validated_data, client=client)
    except PlannerError as exc:
        body = exc.as_dict()
        body["meta"] = {"external_api_calls": client.call_count, "external_api_services": client.calls}
        body["_timings_ms"] = getattr(exc, "timings_ms", {})  # private: popped before sending
        return None, body, exc.status_code, exc.headers, client
    return result, None, 200, {}, client


class RoutePlanView(APIView):
    """Plan a trip and its cheapest fuel stops.

    GET  /api/route?start=New York, NY&finish=Los Angeles, CA[&start_tank=empty|full][&include=candidates]
    POST /api/route  {"start": "...", "finish": "...", "start_tank": "empty"}
    """

    def get(self, request):
        return self._respond(request.query_params)

    def post(self, request):
        return self._respond(request.data)

    def _respond(self, data):
        result, error, status, headers, client = _plan_from(data)
        calls = list(client.call_log) if client else []
        if error:
            # The body keeps its {external_api_calls, external_api_services}; the header also
            # gets the steps completed before the error and the time of the external calls.
            meta = {
                **error["meta"],
                "external_api_ms": round(client.elapsed_ms, 1) if client else 0.0,
                "timings_ms": error.pop("_timings_ms", {}),
            }
            response = Response(error, status=status, headers=headers)
            response.fuel_error = error["error"]
        else:
            result["map"]["map_url"] = self.request.build_absolute_uri(result["map"]["map_url"])
            response = Response(result)
            meta = result["meta"]
            response.fuel_error = None
        timing = server_timing(meta, calls)
        if timing:  # an invalid request has no step to report (the middleware adds the total)
            response["Server-Timing"] = timing
        # Read by ResponseTimeMiddleware for /api/stats (never sent to the client).
        response.fuel_meta = meta
        response.fuel_calls = calls
        return response


class StatsView(APIView):
    """GET /api/stats: what this server process has answered on /api/route since it started.

    Request counts by status and outcome, external calls by service, latency
    percentiles and the last requests (without locations). 0 external calls.
    """

    def get(self, request):
        return Response(route_metrics.snapshot(), headers={"Cache-Control": "no-store"})


def _error_response(exc: PlannerError) -> Response:
    body = exc.as_dict()
    body["meta"] = no_calls_meta()
    return Response(body, status=exc.status_code, headers=exc.headers)


class PlacesView(APIView):
    """GET /api/places?q=chi[, il]&limit=8: US places whose name starts with ``q``.

    From the offline index the planner geocodes with (0 external calls), most
    populated first; each ``label`` plans to exactly the point shown. Fewer than two
    letters: no results.
    """

    def get(self, request):
        started = time.perf_counter()
        serializer = PlacesRequestSerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response(
                {"error": "invalid_request", "detail": serializer.errors, "meta": no_calls_meta()}, status=400
            )
        query, limit = serializer.validated_data["q"], serializer.validated_data["limit"]
        results, matches = get_place_index().search(query, limit)
        return Response(
            {
                "results": results,
                "meta": {
                    "external_api_calls": 0,
                    "query": query,
                    "matches": matches,
                    "took_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )


class SuiteView(APIView):
    """GET /api/tests: the test inventory (files, functions, one-line descriptions), the
    last run (of this process, else of the last local ``pytest``) and whether this
    request may start a run (``runner``)."""

    def get(self, request):
        return Response(
            {
                "runner": {
                    **testrunner.runner_status(request),
                    "running": testrunner.is_running(),
                    "command": testrunner.display_command(),
                },
                "last_run": testrunner.last_run(),
                "inventory": testrunner.inventory(),
            },
            headers={"Cache-Control": "no-store"},
        )


class SuiteRunView(APIView):
    """POST /api/tests/run: run the whole pytest suite now and answer its results.

    Local requests only (403 otherwise), one run at a time (409), a fixed command:
    nothing in the request is read.
    """

    def post(self, request):
        try:
            testrunner.check_allowed(request)
            result = testrunner.run_tests()
        except PlannerError as exc:
            return _error_response(exc)
        return Response(result, headers={"Cache-Control": "no-store"})


class AboutView(APIView):
    """GET /api/about: versions, configuration, loaded data, requirements -> code and tests,
    the last pytest run and the error catalog (``services/about.py``). 0 external calls."""

    def get(self, request):
        return Response(build_about(), headers={"Cache-Control": "no-cache"})


def index(request):
    """``/``: browsers go to the planner page; API clients (Postman, curl) get a JSON index."""
    if "text/html" in request.headers.get("Accept", ""):
        return redirect("route-map")
    return JsonResponse(
        {
            "service": "Spotter fuel route API",
            "endpoints": {
                "GET|POST /api/route": (
                    "start, finish ('City, ST' or 'lat,lon'), start_tank (empty|full), include (candidates); "
                    "what-if: mpg, max_range_miles, corridor_miles, price_policy (median|min|max), consolidate, "
                    "safety_reserve_gal"
                ),
                "GET /api/route/map": "same parameters, the planner page (HTML)",
                "GET /api/places": "q (the start of a US place name, 'chi' or 'chi, il'), limit: type-ahead",
                "GET /api/stats": "requests, external calls and latency since the server started",
                "GET /api/about": "versions, configuration, loaded data, requirements, tests and error codes",
                "GET /api/tests": "the test inventory and the last run",
                "POST /api/tests/run": "runs the test suite (only when the server runs on your machine)",
            },
            "example": request.build_absolute_uri("/api/route?start=New+York,+NY&finish=Los+Angeles,+CA"),
        }
    )


def api_not_found(request, *args, **kwargs):
    return JsonResponse(
        {
            "error": "not_found",
            "detail": (
                f"No endpoint at {request.path}. Use /api/route, /api/places, /api/stats, /api/about or /api/tests."
            ),
            "meta": no_calls_meta(),
        },
        status=404,
    )


def api_exception_handler(exc, context):
    """DRF's handler, reshaped to {"error": <code>, "detail": <message>, "meta": {...}}.

    Covers what DRF raises itself before our code runs: malformed JSON (400
    parse_error), wrong Content-Type (415 unsupported_media_type), wrong method
    (405 method_not_allowed), Accept mismatch (406). None of them spends a call.
    """
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "error" not in response.data:
        codes = exc.get_codes() if hasattr(exc, "get_codes") else "error"
        response.data = {
            "error": codes if isinstance(codes, str) else "invalid_request",
            "detail": response.data.get("detail", response.data),
            "meta": no_calls_meta(),
        }
    return response
