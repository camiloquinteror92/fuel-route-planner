"""Cross-cutting HTTP concerns: response-time header and a small rate limit.

* ``ResponseTimeMiddleware`` adds ``X-Response-Time-ms`` to every response (the
  number quoted in the README) and logs one key=value line per request.
* ``RateLimitMiddleware`` limits each client IP to ``RATE_LIMIT_PER_MINUTE``
  requests per minute on ``/api/``. The API forwards new trips to free public
  services (OSRM demo server, Nominatim) whose fair-use policies are about one
  request per second; this keeps a loop or a Postman runner from getting the
  server's IP blocked. Fixed one-minute window in the Django cache (per process
  with LocMemCache; shared with Redis).
"""

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger("fuelroute.request")


class ResponseTimeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
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
        if limit > 0 and request.path.startswith("/api/"):
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
                    {"error": "rate_limited", "detail": f"More than {limit} requests per minute; retry in {retry_after} s."},
                    status=429,
                )
                response["Retry-After"] = str(retry_after)
                return response
        return self.get_response(request)
