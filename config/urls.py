"""Root URLs: the API lives under /api/ (fuelroute/urls.py); / returns a small JSON index."""

from django.urls import include, path

from fuelroute.views import index

urlpatterns = [
    path("", index, name="index"),
    path("api/", include("fuelroute.urls")),
]
