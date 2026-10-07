"""API routes, mounted under /api/. A trailing slash is optional on every endpoint.

The planner page (``/api/route/map``, name ``route-map``) is registered in
``config/urls.py`` (``fuelroute/web.py``), before this include.
"""

from django.urls import re_path

from .views import AboutView, PlacesView, RoutePlanView, api_not_found

urlpatterns = [
    re_path(r"^route/?$", RoutePlanView.as_view(), name="route-plan"),
    re_path(r"^places/?$", PlacesView.as_view(), name="api-places"),
    re_path(r"^about/?$", AboutView.as_view(), name="api-about"),
    # Anything else under /api/ answers a JSON 404 (not Django's HTML page).
    re_path(r"^.*$", api_not_found),
]
