"""HTTP layer: validates the input, calls the planner, shapes the response.

* ``GET|POST /api/route``  -> JSON plan (``RoutePlanView``)
* ``GET /api/route/map``   -> browser page: form + HTML map of the same plan (``RouteMapView``)
* ``GET /``                -> browsers: redirect to the page above; API clients: a small JSON index
* anything else under ``/api/`` -> JSON 404

Every error has the body ``{"error": "<code>", "detail": ..., "meta": {...}}``
where ``meta`` says how many external calls were made before the error.
"""

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

from .serializers import RouteRequestSerializer
from .services.errors import PlannerError
from .services.http import ExternalApiClient
from .services.planner import plan_trip


def _plan_from(data) -> tuple[dict | None, dict | None, int, dict]:
    """Validate ``data`` and plan the trip: (result, error_body, status, headers)."""
    serializer = RouteRequestSerializer(data=data)
    if not serializer.is_valid():
        meta = {"external_api_calls": 0, "external_api_services": []}
        return None, {"error": "invalid_request", "detail": serializer.errors, "meta": meta}, 400, {}
    client = ExternalApiClient()
    try:
        result = plan_trip(**serializer.validated_data, client=client)
    except PlannerError as exc:
        body = exc.as_dict()
        body["meta"] = {"external_api_calls": client.call_count, "external_api_services": client.calls}
        return None, body, exc.status_code, exc.headers
    return result, None, 200, {}


class RoutePlanView(APIView):
    """Plan a trip and its cheapest fuel stops.

    GET  /api/route?start=New York, NY&finish=Los Angeles, CA[&start_tank=empty|full]
    POST /api/route  {"start": "...", "finish": "...", "start_tank": "empty"}
    """

    def get(self, request):
        return self._respond(request.query_params)

    def post(self, request):
        return self._respond(request.data)

    def _respond(self, data):
        result, error, status, headers = _plan_from(data)
        if error:
            return Response(error, status=status, headers=headers)
        result["map"]["map_url"] = self.request.build_absolute_uri(result["map"]["map_url"])
        return Response(result)


def _messages(detail) -> list[str]:
    """Flatten a serializer error dict / list / string into readable lines."""
    if isinstance(detail, dict):
        lines = []
        for field, value in detail.items():
            prefix = "" if field == "non_field_errors" else f"{field}: "
            lines += [prefix + line for line in _messages(value)]
        return lines
    if isinstance(detail, (list, tuple)):
        return [line for item in detail for line in _messages(item)]
    return [str(detail)]


class RouteMapView(View):
    """Browser page: a form plus the HTML map (Leaflet + OpenStreetMap tiles) of the plan.

    * No ``start`` and no ``finish``: only the form is shown; nothing is planned.
    * Otherwise the same inputs as ``/api/route`` are planned and drawn, with a
      table of the stops. It uses the plan cache, so right after an API call it
      makes no external call.
    """

    def get(self, request):
        form = {
            "start": request.GET.get("start", ""),
            "finish": request.GET.get("finish", ""),
            "start_tank": request.GET.get("start_tank", "empty"),
        }
        if not form["start"].strip() and not form["finish"].strip():
            return render(request, "fuelroute/map.html", {"form": form})
        result, error, status, headers = _plan_from(request.GET)
        if error:
            context = {"form": form, "error_code": error["error"], "error_lines": _messages(error["detail"])}
            response = render(request, "fuelroute/map.html", context, status=status)
        else:
            api_url = reverse("route-plan") + "?" + request.GET.urlencode()
            context = {"form": form, "result": result, "geojson": result["map"]["geojson"], "api_url": api_url}
            response = render(request, "fuelroute/map.html", context)
        for name, value in headers.items():
            response[name] = value
        return response


def index(request):
    """``/``: browsers go to the planner page; API clients (Postman, curl) get a JSON index."""
    if "text/html" in request.headers.get("Accept", ""):
        return redirect("route-map")
    return JsonResponse(
        {
            "service": "Spotter fuel route API",
            "endpoints": {
                "GET|POST /api/route": "start, finish ('City, ST' or 'lat,lon'), start_tank (empty|full)",
                "GET /api/route/map": "same parameters, HTML map",
            },
            "example": request.build_absolute_uri("/api/route?start=New+York,+NY&finish=Los+Angeles,+CA"),
        }
    )


def api_not_found(request, *args, **kwargs):
    return JsonResponse(
        {"error": "not_found", "detail": f"No endpoint at {request.path}. Use /api/route or /api/route/map."},
        status=404,
    )


def api_exception_handler(exc, context):
    """DRF's handler, reshaped to {"error": <code>, "detail": <message>}.

    Covers what DRF raises itself before our code runs: malformed JSON (400
    parse_error), wrong Content-Type (415 unsupported_media_type), wrong method
    (405 method_not_allowed), Accept mismatch (406).
    """
    response = drf_exception_handler(exc, context)
    if response is not None and isinstance(response.data, dict) and "error" not in response.data:
        codes = exc.get_codes() if hasattr(exc, "get_codes") else "error"
        response.data = {
            "error": codes if isinstance(codes, str) else "invalid_request",
            "detail": response.data.get("detail", response.data),
        }
    return response
