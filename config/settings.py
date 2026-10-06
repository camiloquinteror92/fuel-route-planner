"""Django settings for the fuel route planner.

Everything that changes the behaviour of the planner lives in the ``FUEL_PLANNER``
block at the end and can be overridden with an environment variable of the same
name (see the README, "Configuration").

Secure by default: ``DEBUG`` is off unless ``DJANGO_DEBUG=true``, and when no
``DJANGO_SECRET_KEY`` is given a random one is generated per process (the app
signs nothing that must survive a restart: no sessions, no auth).
"""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_list(name: str, default: str) -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)
DEBUG = _env("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuelroute",
]

MIDDLEWARE = [
    # Outermost, so the header measures the whole request.
    "fuelroute.middleware.ResponseTimeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "fuelroute.middleware.RateLimitMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# In-process cache: prepared routes (~100 KB for coast to coast), finished plans
# (~60 KB), Nominatim answers and rate-limit counters. Each process has its own;
# with several workers use Redis (django.core.cache.backends.redis.RedisCache) so
# they share it.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-route",
        "TIMEOUT": int(_env("ROUTE_CACHE_SECONDS", "3600")),
        "OPTIONS": {"MAX_ENTRIES": int(_env("CACHE_MAX_ENTRIES", "500"))},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # JSON only; the browsable HTML API (heavier, needs static files) only in DEBUG.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    # Same {"error", "detail"} body for DRF's own errors (bad JSON, 405, 415...).
    "EXCEPTION_HANDLER": "fuelroute.views.api_exception_handler",
}

# --- Fuel planner ------------------------------------------------------------
FUEL_PLANNER = {
    # Vehicle (from the assessment): 500-mile range, 10 MPG -> 50-gallon tank.
    "MAX_RANGE_MILES": float(_env("MAX_RANGE_MILES", "500")),
    "MILES_PER_GALLON": float(_env("MILES_PER_GALLON", "10")),
    # start_tank=empty: the truck leaves with this reserve (or enough to reach the
    # first station, if that is further) and must arrive with the same amount, so
    # every mile driven is paid for. See planner._tank_rules.
    "START_RESERVE_MILES": float(_env("START_RESERVE_MILES", "50")),
    # Stops that buy less than this are consolidated with a neighbour (optimizer).
    "MIN_STOP_GALLONS": float(_env("MIN_STOP_GALLONS", "10")),
    # A station is a candidate if it is at most this far from the route line.
    # Station coordinates are city-level, so this also absorbs that error.
    "CORRIDOR_MILES": float(_env("CORRIDOR_MILES", "10")),
    # Spacing used to resample the route geometry.
    "RESAMPLE_MILES": float(_env("RESAMPLE_MILES", "1")),
    # Reject a start / finish that OSRM had to move further than this to reach a road.
    "MAX_ROAD_SNAP_MILES": float(_env("MAX_ROAD_SNAP_MILES", "5")),
    # Price of a station listed several times in the file: "median" or "min".
    # Applied by load_stations (run it again after changing this).
    "PRICE_POLICY": _env("PRICE_POLICY", "median"),
    # External services (both free, no API key).
    "OSRM_URL": _env("OSRM_URL", "https://router.project-osrm.org"),
    "NOMINATIM_URL": _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"),
    "NOMINATIM_MIN_INTERVAL_SECONDS": float(_env("NOMINATIM_MIN_INTERVAL_SECONDS", "1")),
    "HTTP_CONNECT_TIMEOUT_SECONDS": float(_env("HTTP_CONNECT_TIMEOUT_SECONDS", "3.05")),
    "HTTP_READ_TIMEOUT_SECONDS": float(_env("HTTP_READ_TIMEOUT_SECONDS", "15")),
    "HTTP_RETRIES": int(_env("HTTP_RETRIES", "1")),
    # Nominatim's policy asks for an identifying User-Agent; add a contact URL/email.
    "USER_AGENT": _env("HTTP_USER_AGENT", "spotter-fuel-route/1.0 (coding assessment)"),
    # Requests per minute per client IP on /api/ (0 = no limit). Protects the free
    # upstream services from a loop or a Postman runner.
    "RATE_LIMIT_PER_MINUTE": int(_env("RATE_LIMIT_PER_MINUTE", "60")),
    # Data files (committed; see scripts/ for how they are built)
    "PLACES_FILE": BASE_DIR / "data" / "us_places.csv.gz",
    "US_MASK_FILE": BASE_DIR / "data" / "us_mask.npz",
    "FUEL_PRICES_FILE": BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv",
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "kv": {"format": "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "kv"}},
    "loggers": {
        "fuelroute": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO"), "propagate": False},
    },
}
