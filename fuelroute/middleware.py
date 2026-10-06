import logging
import time

logger = logging.getLogger("fuelroute.request")


class ResponseTimeMiddleware:
    """Adds ``X-Response-Time-ms`` to every response and logs one line per request."""

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
