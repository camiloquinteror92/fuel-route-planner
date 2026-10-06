"""JSON renderer that measures itself, for the ``render`` entry of Server-Timing.

DRF renders the response inside the request handler, before the middleware sees
it, so ``ResponseTimeMiddleware`` can report how long the JSON serialization took.
The output bytes are exactly DRF's ``JSONRenderer``'s.
"""

import time

from rest_framework.renderers import JSONRenderer


class TimedJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        started = time.perf_counter()
        body = super().render(data, accepted_media_type, renderer_context)
        request = (renderer_context or {}).get("request")
        if request is not None:
            # DRF's Request wraps Django's HttpRequest, which the middleware holds.
            django_request = getattr(request, "_request", request)
            django_request.fuel_render_ms = (time.perf_counter() - started) * 1000
        return body
