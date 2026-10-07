"""What this server runs and what it plans with: ``GET /api/about``.

``build_about()`` returns one JSON-serializable dict. The planner page embeds it
(defaults, limits, labels, data counts) and ``/api/about`` serves it:

* ``versions``: Python, Django, DRF, numpy and urllib3 as they run;
* ``vehicle``: the standard truck and the planner defaults (``settings.FUEL_PLANNER``);
* ``api``: every parameter of ``/api/route`` with its help text, default and allowed
  range, and the two ``start_tank`` modes with their labels;
* ``deliverables``: links to the repository, the Postman collection and the Loom
  video (null until ``LOOM_URL`` is set);
* ``data``: counts of the loaded station data;
* ``errors``: every error code the API can answer, built from the ``PlannerError``
  classes plus the framework's and the rate limit's.

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
from ..serializers import RouteRequestSerializer
from .errors import PlannerError
from .planner import START_TANK_HELP, START_TANK_LABELS, PlanSettings
from .stations import data_version, get_station_arrays

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
        "price_file": Path(config["FUEL_PRICES_FILE"]).name,
        "stations": 0,
        "price_quotes": 0,
        "stations_with_several_quotes": 0,
        "geocoded": 0,
        "not_geocoded": 0,
        "states": 0,
        "stations_by_state": [],
        "price_per_gallon": None,
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
    by_state = list(
        FuelStation.objects.values("state")
        .annotate(stations=Count("id"), geocoded=Count("id", filter=located))
        .order_by("-stations", "state")
    )
    prices = arrays.price
    summary.update(
        stations=totals["stations"],
        price_quotes=totals["quotes"] or 0,
        stations_with_several_quotes=totals["several"],
        geocoded=totals["geocoded"],
        # In the file, but their city is in neither US place list: left out of the plans.
        not_geocoded=totals["stations"] - totals["geocoded"],
        states=len(by_state),
        stations_by_state=[
            {"state": row["state"], "stations": row["stations"], "geocoded": row["geocoded"]} for row in by_state
        ],
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
    with _lock:
        _data_cache.update(arrays=arrays, summary=summary)
    return summary


def _vehicle(config: dict) -> dict:
    """The standard truck and the planner defaults a request can change."""
    return {
        "max_range_miles": config["MAX_RANGE_MILES"],
        "miles_per_gallon": config["MILES_PER_GALLON"],
        "tank_gallons": round(config["MAX_RANGE_MILES"] / config["MILES_PER_GALLON"], 1),
        # start_tank=empty: the fuel the truck leaves with (and must arrive with) when the
        # first station is closer than that.
        "start_reserve_miles": config["START_RESERVE_MILES"],
        "start_reserve_gallons": round(config["START_RESERVE_MILES"] / config["MILES_PER_GALLON"], 2),
        "corridor_miles": config["CORRIDOR_MILES"],
        "min_stop_gallons": config["MIN_STOP_GALLONS"],
        "max_consolidation_cost": config["MAX_CONSOLIDATION_COST"],
    }


def _params() -> list[dict]:
    """Parameters of /api/route: help text, choices, and for the truck settings their
    default (the configuration) and allowed range."""
    defaults = PlanSettings.defaults()
    params = []
    for name, field in RouteRequestSerializer().fields.items():
        choices = getattr(field, "choices", None)
        params.append(
            {
                "name": name,
                "required": bool(field.required),
                "choices": list(choices) if choices else None,
                "help_text": str(field.help_text or ""),
                "default": getattr(defaults, name, None),
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
        "api": {
            "params": _params(),
            "start_tank_modes": [
                {"value": mode, "label": START_TANK_LABELS[mode], "help": START_TANK_HELP[mode]}
                for mode in START_TANK_LABELS
            ],
        },
        "deliverables": _deliverables(config),
        "data": copy.deepcopy(_data_summary()),  # cached: callers may change their copy
        "errors": error_catalog(),
    }
