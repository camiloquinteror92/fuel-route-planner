"""Per-process counters of ``/api/route`` requests, served by ``GET /api/stats``.

``ResponseTimeMiddleware`` calls ``route_metrics.record(...)`` once per request to
``/api/route`` (any method, any status, the API's own 429 included). The counters
live in memory: they start at zero when the process starts and each worker has its
own (exporting them, e.g. to Prometheus, is a "next step" in the README).

Nothing the user typed is stored: no locations, only status, timings, external
calls and the error code. Latencies are kept for the last ``STATS_WINDOW``
requests of each outcome, so percentiles stay cheap and recent.

Outcome of a request:
* ``plan_cache_hit``: a 200 served from the plan cache (0 external calls);
* ``route_cache_hit``: a 200 planned again on a cached route (e.g. the other
  start_tank mode, or new station data);
* ``cold``: a 200 that needed routing;
* ``error``: any status >= 400.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import Counter, deque
from datetime import UTC, datetime

from django.conf import settings

OUTCOMES = ("cold", "route_cache_hit", "plan_cache_hit", "error")
SERVICES = ("osrm", "nominatim")
RECENT = 20

STARTED_AT = datetime.now(UTC)
_STARTED_CLOCK = time.monotonic()


def _window() -> int:
    try:
        return max(1, int(settings.FUEL_PLANNER.get("STATS_WINDOW", 500)))
    except Exception:  # settings not configured (imported outside Django)
        return 500


def _percentiles(values) -> dict | None:
    """Nearest-rank p50 / p95 and the max, or None without data."""
    if not values:
        return None
    ordered = sorted(values)

    def rank(p: float) -> float:
        return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

    return {
        "count": len(ordered),
        "p50": round(rank(50), 1),
        "p95": round(rank(95), 1),
        "max": round(ordered[-1], 1),
    }


def outcome_of(status: int, meta: dict | None) -> str:
    if status >= 400:
        return "error"
    meta = meta or {}
    if meta.get("plan_cache") == "hit":
        return "plan_cache_hit"
    if meta.get("route_cache") == "hit":
        return "route_cache_hit"
    return "cold"


class RouteMetrics:
    """Thread-safe counters; ``snapshot()`` is the body of ``/api/stats``."""

    def __init__(self):
        self._lock = threading.Lock()
        self._warm_up: dict | None = None
        self.reset()

    def reset(self) -> None:
        """Back to zero (tests). Keeps the warm-up figures: they belong to the process."""
        window = _window()
        with self._lock:
            self._window = window
            self._total = 0
            self._by_status: Counter = Counter()
            self._by_outcome: Counter = Counter()
            self._errors: Counter = Counter()
            self._errors_without_calls = 0
            self._calls_by_service: Counter = Counter()
            self._external_ms = 0.0
            self._requests_with_osrm = 0
            self._osrm_calls = 0
            self._max_calls = 0
            self._latency = {outcome: deque(maxlen=window) for outcome in OUTCOMES}
            self._external_latency = {service: deque(maxlen=window) for service in SERVICES}
            self._recent: deque = deque(maxlen=RECENT)

    def set_warm_up(self, ms: float, places: int, stations: int) -> None:
        with self._lock:
            self._warm_up = {"ms": round(ms, 1), "places": int(places), "stations": int(stations)}

    def record(
        self,
        method: str,
        status: int,
        ms: float,
        meta: dict | None = None,
        error: str | None = None,
        calls: list[tuple[str, float]] | None = None,
    ) -> None:
        """One request to /api/route. ``calls``: (service, ms) of each external call."""
        calls = list(calls or [])
        call_count = len(calls) if calls else int((meta or {}).get("external_api_calls") or 0)
        outcome = outcome_of(status, meta)
        osrm_calls = sum(1 for service, _ in calls if service == "osrm")
        with self._lock:
            self._total += 1
            self._by_status[str(status)] += 1
            self._by_outcome[outcome] += 1
            self._latency[outcome].append(ms)
            if outcome == "error":
                self._errors[error or f"http_{status}"] += 1
                if call_count == 0:
                    self._errors_without_calls += 1
            for service, call_ms in calls:
                self._calls_by_service[service] += 1
                self._external_ms += call_ms
                self._external_latency.setdefault(service, deque(maxlen=self._window)).append(call_ms)
            if osrm_calls:
                self._requests_with_osrm += 1
                self._osrm_calls += osrm_calls
            self._max_calls = max(self._max_calls, call_count)
            self._recent.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "method": method,
                    "status": status,
                    "ms": round(ms, 1),
                    "outcome": outcome,
                    "external_api_calls": call_count,
                    "error": error if outcome == "error" else None,
                }
            )

    def snapshot(self) -> dict:
        with self._lock:
            by_service = {service: 0 for service in SERVICES}
            by_service.update(self._calls_by_service)
            return {
                "process": {
                    "pid": os.getpid(),
                    "started_at": STARTED_AT.isoformat(),
                    "uptime_seconds": round(time.monotonic() - _STARTED_CLOCK, 1),
                    "warm_up": dict(self._warm_up) if self._warm_up else None,
                },
                "route_requests": {
                    "total": self._total,
                    "by_status": dict(sorted(self._by_status.items())),
                    "by_outcome": {outcome: self._by_outcome.get(outcome, 0) for outcome in OUTCOMES},
                    "errors": dict(self._errors.most_common()),
                    "errors_without_external_calls": self._errors_without_calls,
                },
                "external_api": {
                    "calls": sum(self._calls_by_service.values()),
                    "by_service": by_service,
                    "total_ms": round(self._external_ms, 1),
                    "requests_with_osrm_calls": self._requests_with_osrm,
                    "osrm_calls_per_routing_request": (
                        round(self._osrm_calls / self._requests_with_osrm, 2) if self._requests_with_osrm else None
                    ),
                    "max_calls_in_one_request": self._max_calls,
                },
                "latency_ms": {
                    "window": self._window,
                    **{outcome: _percentiles(self._latency[outcome]) for outcome in OUTCOMES},
                },
                "external_latency_ms": {
                    service: _percentiles(self._external_latency.get(service, ())) for service in SERVICES
                },
                "recent": list(reversed(self._recent)),
            }


route_metrics = RouteMetrics()
