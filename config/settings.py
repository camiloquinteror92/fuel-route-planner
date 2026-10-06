"""Django settings for the fuel route planner.

Everything that changes the behaviour of the planner can be overridden with an
environment variable of the same name.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = _env("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = _env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuelroute",
]

MIDDLEWARE = [
    "fuelroute.middleware.ResponseTimeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
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

# In-process cache. Routes are cached by (start, finish) so repeated requests and
# the map page never call the routing API again. Swap for Redis in production.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-route",
        "TIMEOUT": int(_env("ROUTE_CACHE_SECONDS", "3600")),
        "OPTIONS": {"MAX_ENTRIES": 1000},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}

# --- Fuel planner ------------------------------------------------------------
FUEL_PLANNER = {
    # Vehicle
    "MAX_RANGE_MILES": float(_env("MAX_RANGE_MILES", "500")),
    "MILES_PER_GALLON": float(_env("MILES_PER_GALLON", "10")),
    # start_tank=empty: the truck leaves with only this reserve (enough to reach a
    # station) and must arrive with the same reserve, so every mile is paid for.
    "START_RESERVE_MILES": float(_env("START_RESERVE_MILES", "50")),
    # Stops that buy less than this are merged into a neighbour stop (see optimizer).
    "MIN_STOP_GALLONS": float(_env("MIN_STOP_GALLONS", "10")),
    # A station is a candidate if it is at most this far from the route line.
    # Station coordinates are city-level, so this also absorbs that error.
    "CORRIDOR_MILES": float(_env("CORRIDOR_MILES", "10")),
    # Spacing used to resample the route geometry.
    "RESAMPLE_MILES": float(_env("RESAMPLE_MILES", "1")),
    # External services (both free, no API key).
    "OSRM_URL": _env("OSRM_URL", "https://router.project-osrm.org"),
    "NOMINATIM_URL": _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"),
    "HTTP_TIMEOUT_SECONDS": float(_env("HTTP_TIMEOUT_SECONDS", "20")),
    "USER_AGENT": _env("HTTP_USER_AGENT", "spotter-fuel-route/1.0 (coding assessment)"),
    # Data files
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
