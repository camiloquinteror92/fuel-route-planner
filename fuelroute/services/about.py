"""What this server runs and what it plans with: ``GET /api/about``.

``build_about()`` returns one JSON-serializable dict. The planner page embeds it
(defaults, limits, labels, data counts, error texts) and ``/api/about`` serves it.
It publishes:

* ``versions``: Python, Django, DRF, numpy and urllib3 as they run;
* ``vehicle`` and ``planner``: the truck and planner defaults (``settings.FUEL_PLANNER``);
* ``external_services``: the free services the API and the page use;
* ``api``: every parameter of ``/api/route`` with its help text, default and
  allowed range, the US state codes, the price policies and the two
  ``start_tank`` modes with their labels;
* ``deliverables``: links to the repository, the Postman collection and the Loom
  video (null until ``LOOM_URL`` is set);
* ``data``: counts of the loaded station data;
* ``errors``: every error code the API can answer, built from the ``PlannerError``
  classes plus the framework's and the rate limit's;
* ``endpoints``: the public endpoints.

Every figure is read at run time, never written by hand. The data summary is
cached until the station data is reloaded. No network access.
"""

from __future__ import annotations

import copy
import platform
import threading
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
    {"method": "GET", "path": "/api/about", "description": "Versions, truck defaults, loaded data and error codes."},
    {"method": "GET", "path": "/", "description": "Browsers are sent to the planner page; API clients get a JSON index."},
]

_lock = threading.Lock()
_data_cache: dict = {}


def reset_caches() -> None:
    """Forget the cached data summary (tests)."""
    with _lock:
        _data_cache.clear()


def _versions() -> dict:
    return {
        "python": platform.python_version(),
        "django": django.get_version(),
        "djangorestframework": rest_framework.VERSION,
        "numpy": np.__version__,
        "urllib3": urllib3.__version__,
    }


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


def _plan_cache_seconds(alias: str = "default") -> int | None:
    timeout = settings.CACHES.get(alias, {}).get("TIMEOUT", 300)
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
        # Prepared routes live in their own cache, apart from the plans (see settings.CACHES).
        "route_cache_seconds": _plan_cache_seconds("routes") if "routes" in settings.CACHES else None,
        "route_cache_max_entries": settings.CACHES.get("routes", {}).get("OPTIONS", {}).get("MAX_ENTRIES"),
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


def _deliverables(config: dict) -> dict:
    repo_url = str(config["REPO_URL"]).rstrip("/")
    return {
        "repo_url": repo_url,
        "postman_url": f"{repo_url}/blob/main/postman/collection.json",
        "loom_url": str(config.get("LOOM_URL") or "") or None,
    }


def build_about() -> dict:
    """The body of ``GET /api/about`` (JSON-serializable)."""
    config = settings.FUEL_PLANNER
    return {
        "service": SERVICE,
        "versions": _versions(),
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
        "deliverables": _deliverables(config),
        "data": copy.deepcopy(_data_summary()),  # cached: callers may change their copy
        "errors": error_catalog(),
        "endpoints": [dict(endpoint) for endpoint in ENDPOINTS],
    }
