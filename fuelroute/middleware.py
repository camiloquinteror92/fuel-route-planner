"""Cross-cutting HTTP concerns: response-time headers, request metrics, a small rate limit.

* ``ResponseTimeMiddleware`` adds ``X-Response-Time-ms`` to every response (the
  number quoted in the README), completes the ``Server-Timing`` header (the view
  writes the per-step entries; this adds the JSON render and the server total, so
  the browser's DevTools show the same breakdown), records every ``/api/route``
  request for ``/api/stats`` and logs one key=value line per request.
* ``RateLimitMiddleware`` limits each client IP to ``RATE_LIMIT_PER_MINUTE``
  requests per minute on ``/api/route`` (the endpoint only). The API forwards new
  trips to free public services (OSRM demo server, Nominatim) whose fair-use
  policies are about one request per second; this keeps a loop or a Postman runner
  from getting the server's IP blocked. The planner page (``/api/route/map``),
  ``/api/stats`` and ``/api/about`` never leave the server, so they are not
  limited: reloading the page does not eat the quota of the trips it plans. Fixed
  one-minute window in the Django cache (per process with LocMemCache; shared with
  Redis). The 429 body has the API's error shape, ``meta`` included.
"""

import logging
import re
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

from .services.metrics import route_metrics

logger = logging.getLogger("fuelroute.request")

_ROUTE_ENDPOINT = re.compile(r"^/api/route/?$")


def _error_code(response) -> str | None:
    """The ``error`` code of an error response (ours, DRF's or the rate limit's)."""
    if response.status_code < 400:
        return None
    code = getattr(response, "fuel_error", None)
    if code:
        return code
    data = getattr(response, "data", None)
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return None


class ResponseTimeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        total = f"{elapsed_ms:.1f}"
        response["X-Response-Time-ms"] = total

        entries = [response["Server-Timing"]] if response.get("Server-Timing") else []
        render_ms = getattr(request, "fuel_render_ms", None)
        if render_ms is not None:
            entries.append(f'render;dur={render_ms:.1f};desc="JSON render"')
        entries.append(f'total;dur={total};desc="Server total"')
        response["Server-Timing"] = ", ".join(entries)

        if _ROUTE_ENDPOINT.match(request.path):
            route_metrics.record(
                request.method,
                response.status_code,
                float(total),
                meta=getattr(response, "fuel_meta", None),
                error=_error_code(response),
                calls=getattr(response, "fuel_calls", None),
            )
        logger.info(
            "event=request method=%s path=%s status=%s duration_ms=%.1f",
            request.method, request.path, response.status_code, elapsed_ms,
        )
        return response


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = settings.FUEL_PLANNER["RATE_LIMIT_PER_MINUTE"]
        if limit > 0 and _ROUTE_ENDPOINT.match(request.path):
            window = int(time.time() // 60)
            key = f"ratelimit:{request.META.get('REMOTE_ADDR', '')}:{window}"
            cache.add(key, 0, timeout=61)
            try:
                count = cache.incr(key)
            except ValueError:  # evicted between add and incr: count this one as the first
                cache.set(key, 1, timeout=61)
                count = 1
            if count > limit:
                retry_after = 60 - int(time.time()) % 60
                response = JsonResponse(
                    {
                        "error": "rate_limited",
                        "detail": f"More than {limit} requests per minute; retry in {retry_after} s.",
                        "meta": {"external_api_calls": 0, "external_api_services": []},
                    },
                    status=429,
                )
                response["Retry-After"] = str(retry_after)
                response.fuel_error = "rate_limited"
                return response
        return self.get_response(request)
