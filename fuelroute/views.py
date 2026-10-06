from django.shortcuts import render
from django.views import View
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.errors import PlannerError
from .services.planner import plan_trip


def _plan_from(data):
    serializer = RouteRequestSerializer(data=data)
    if not serializer.is_valid():
        return None, {"error": "invalid_request", "detail": serializer.errors}, 400
    try:
        result = plan_trip(**serializer.validated_data)
    except PlannerError as exc:
        return None, exc.as_dict(), exc.status_code
    return result, None, 200


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
        result, error, status = _plan_from(data)
        if error:
            return Response(error, status=status)
        result["map"]["map_url"] = self.request.build_absolute_uri(result["map"]["map_url"])
        return Response(result)


class RouteMapView(View):
    """HTML map (Leaflet + OpenStreetMap tiles). Reuses the cached route: no new routing call."""

    def get(self, request):
        result, error, status = _plan_from(request.GET)
        if error:
            return render(request, "fuelroute/map.html", {"error": error}, status=status)
        return render(
            request,
            "fuelroute/map.html",
            {"result": result, "geojson": result["map"]["geojson"]},
        )
