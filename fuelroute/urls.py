"""API routes, mounted under /api/. A trailing slash is optional on both endpoints."""

from django.urls import re_path

from .views import RouteMapView, RoutePlanView, api_not_found

urlpatterns = [
    re_path(r"^route/?$", RoutePlanView.as_view(), name="route-plan"),
    re_path(r"^route/map/?$", RouteMapView.as_view(), name="route-map"),
    # Anything else under /api/ answers a JSON 404 (not Django's HTML page).
    re_path(r"^.*$", api_not_found),
]
