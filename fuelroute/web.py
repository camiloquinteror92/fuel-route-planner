"""The browser page at ``/api/route/map``: a client of the public JSON API.

* ``planner_page`` renders only the shell of a guided tour in six steps (Trip,
  Route, Fuel stops, Cost, Truck, For Spotter): the trip form pre-filled from the
  query string (the truck settings included), the page configuration and
  ``/api/about`` embedded as JSON. It never plans and never calls an external
  service; the page's JavaScript calls the same ``GET /api/route`` that Postman
  uses and draws what it returns, so everything the page shows comes from the API
  (or from measurements taken in the browser). City suggestions come from
  ``/api/places``.
* ``static_file`` serves ``fuelroute/static/`` so the page works with the
  documented ``runserver`` (``DEBUG`` off, no ``collectstatic``). In production a
  reverse proxy or a CDN serves these files instead.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_GET
from django.views.static import serve

STATIC_DIR = Path(__file__).resolve().parent / "static"

# On Windows the registry can map .js to text/plain, and browsers refuse to run an
# ES module served with that type. Force the standard types.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def _url(name: str) -> str | None:
    """``reverse(name)``, or None when that endpoint does not exist on this server."""
    try:
        return reverse(name)
    except NoReverseMatch:
        return None


# Optional truck settings of /api/route (the API validates them; the page only carries
# what the URL says, so a shared link opens the same plan). The Truck step has a control
# for four of them; the other ones are kept when a link brings them.
# fuelroute/tests/test_web.py checks it is the serializer's list.
WHAT_IF_PARAMS = ("mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal")


def _about() -> dict:
    """``/api/about`` (versions, truck defaults, loaded data, errors...), or {} if unavailable."""
    try:
        from .services.about import build_about
    except ImportError:
        return {}
    return build_about()


def _start_tank_modes(about: dict) -> list[dict]:
    """The two start-tank modes with the API's own label and help text."""
    modes = (about.get("api") or {}).get("start_tank_modes")
    if modes:
        return modes
    from .services.planner import START_TANK_HELP, START_TANK_LABELS

    return [{"value": value, "label": label, "help": START_TANK_HELP[value]} for value, label in START_TANK_LABELS.items()]


def asset_version() -> int:
    """Newest modification time of the static files: a cache-busting ``?v=`` value."""
    mtimes = [path.stat().st_mtime for path in STATIC_DIR.rglob("*") if path.is_file()]
    return int(max(mtimes, default=0))


@require_GET
def planner_page(request):
    """The planner page. Always 200 and 0 external calls: the browser does the planning."""
    form = {
        "start": request.GET.get("start", ""),
        "finish": request.GET.get("finish", ""),
        "start_tank": "full" if request.GET.get("start_tank") == "full" else "empty",
    }
    # Truck settings present in the address, as written (the API validates them).
    initial = dict(form)
    for name in WHAT_IF_PARAMS:
        value = request.GET.get(name, "").strip()[:20]
        if value:
            initial[name] = value
    about = _about()
    page_config = {
        "api": {
            "route": _url("route-plan"),
            "about": _url("api-about"),
            "page": _url("route-map"),
            "places": _url("api-places"),
        },
        "initial": initial,
        "about": about,
    }
    context = {
        "form": form,
        "start_tank_modes": _start_tank_modes(about),
        "page_config": page_config,
        "asset_version": asset_version(),
    }
    return render(request, "fuelroute/planner.html", context)


@require_GET
def static_file(request, path: str):
    """Serve a file from ``fuelroute/static/`` (any DEBUG setting, no collectstatic).

    Versioned URLs (``?v=<mtime>``, written by the template) can be cached for good;
    the ES modules they import are revalidated with ``Last-Modified``.
    """
    response = serve(request, path, document_root=STATIC_DIR)
    if response.status_code == 200:
        if request.GET.get("v"):
            response["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response["Cache-Control"] = "no-cache"
    return response
