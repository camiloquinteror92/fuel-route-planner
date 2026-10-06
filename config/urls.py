"""Root URLs.

* ``/``: browsers are sent to the planner page; API clients get a small JSON index.
* ``/api/route/map``: the planner page (``fuelroute/web.py``), a client of the JSON
  API. It keeps the URL and the name ``route-map`` that ``map.map_url`` points to.
* ``/static/``: the page's CSS and JavaScript, served without ``collectstatic``.
* ``/api/``: the JSON API (``fuelroute/urls.py``).
"""

from django.urls import include, path, re_path

from fuelroute import web
from fuelroute.views import index

urlpatterns = [
    path("", index, name="index"),
    re_path(r"^api/route/map/?$", web.planner_page, name="route-map"),
    re_path(r"^static/(?P<path>.+)$", web.static_file, name="static-file"),
    path("api/", include("fuelroute.urls")),
]
