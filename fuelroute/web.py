"""The browser page at ``/api/route/map``: a client of the public JSON API.

* ``planner_page`` renders only the shell: the form (pre-filled from the query
  string, what-if settings included), the page configuration and ``/api/about``
  embedded as JSON. It never plans and never calls an external service; the page's
  JavaScript calls the same ``GET /api/route`` that Postman uses and draws what it
  returns, so everything the page shows comes from the API (or from measurements
  taken in the browser). City suggestions come from ``/api/places`` and the Tests
  tab uses ``/api/tests``; the page degrades gracefully when a server lacks them.
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


def _endpoint(names: tuple[str, ...], path: str) -> str:
    """The first of ``names`` that reverses, else the path fixed by the API contract.

    The page asks the contract's path even when this server does not route it yet:
    the answer is then the API's JSON 404 and the page leaves that feature out.
    """
    for name in names:
        url = _url(name)
        if url:
            return url
    return path


# Optional what-if parameters of /api/route (the API validates them; the page only
# carries what the URL says, so a shared link opens the same plan).
# fuelroute/tests/test_web.py checks it is the serializer's list.
WHAT_IF_PARAMS = ("mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal")


def _about() -> dict:
    """``/api/about`` (versions, data, requirements, tests...), or {} if unavailable."""
    try:
        from .services.about import build_about
    except ImportError:
        return {}
    return build_about()


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
    # What-if settings present in the address, as written (the API validates them).
    initial = dict(form)
    for name in WHAT_IF_PARAMS:
        value = request.GET.get(name, "").strip()[:20]
        if value:
            initial[name] = value
    page_config = {
        "api": {
            "route": _url("route-plan"),
            "stats": _url("api-stats"),
            "about": _url("api-about"),
            "page": _url("route-map"),
            "places": _endpoint(("api-places", "places"), "/api/places"),
            "tests": _endpoint(("api-tests", "tests"), "/api/tests"),
            "tests_run": _endpoint(("api-tests-run", "tests-run"), "/api/tests/run"),
        },
        "initial": initial,
        "about": _about(),
    }
    context = {"form": form, "page_config": page_config, "asset_version": asset_version()}
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
