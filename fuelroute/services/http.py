"""HTTP client for the two free external APIs (OSRM routing, Nominatim geocoding).

* One shared, thread-safe ``urllib3.PoolManager`` for the whole process, so a new
  trip reuses the open HTTPS connection to OSRM instead of paying a new TCP + TLS
  handshake (~0.4-0.5 s measured) on every request.
* ``ExternalApiClient`` is created per API request and COUNTS the calls it makes;
  the response reports them in ``meta.external_api_calls``.
* Separate connect / read timeouts, and ONE retry (after 0.5 s) for transient
  failures: connection errors, timeouts, HTTP 502/503/504. A retry counts as a
  call. HTTP 429 (rate limited) is not retried; it becomes a 503 with Retry-After.
* ``min_interval``: a process-wide spacing between calls to one service. Nominatim's
  usage policy allows at most 1 request per second.

``send`` is the only function that touches the network; tests replace it.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import urllib3
from django.conf import settings

from .errors import ExternalServiceError, UpstreamBusy

logger = logging.getLogger(__name__)

_pool: urllib3.PoolManager | None = None
_pool_lock = threading.Lock()
_turn_lock = threading.Lock()
_next_turn: dict[str, float] = {}

_RETRY_STATUSES = {502, 503, 504}
_RETRY_DELAY_SECONDS = 0.5


def _get_pool() -> urllib3.PoolManager:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                try:
                    import certifi

                    ca_certs = certifi.where()
                except ImportError:  # fall back to the system store
                    ca_certs = None
                config = settings.FUEL_PLANNER
                _pool = urllib3.PoolManager(
                    num_pools=4,
                    maxsize=8,
                    retries=False,  # retries are decided below, and counted
                    ca_certs=ca_certs,
                    headers={"User-Agent": config["USER_AGENT"], "Accept-Encoding": "gzip, deflate"},
                )
    return _pool


def send(url: str, params: dict | None, timeout: urllib3.Timeout) -> tuple[int, bytes, dict]:
    """One GET. Returns (status, body, headers); raises urllib3 errors on network failure."""
    response = _get_pool().request("GET", url, fields=params, timeout=timeout)
    return response.status, response.data, dict(response.headers)


def _wait_turn(service: str, interval: float) -> None:
    """Space calls to ``service`` by ``interval`` seconds across all threads."""
    if interval <= 0:
        return
    with _turn_lock:
        now = time.monotonic()
        turn = max(now, _next_turn.get(service, 0.0))
        _next_turn[service] = turn + interval  # reserve the slot, then sleep outside the lock
    if turn > now:
        time.sleep(turn - now)


class ExternalApiClient:
    """Per-request client: makes the calls and records them (service name per attempt)."""

    def __init__(self):
        config = settings.FUEL_PLANNER
        self.timeout = urllib3.Timeout(
            connect=config["HTTP_CONNECT_TIMEOUT_SECONDS"], read=config["HTTP_READ_TIMEOUT_SECONDS"]
        )
        self.retries = int(config["HTTP_RETRIES"])
        self.calls: list[str] = []
        self.elapsed_ms = 0.0
        # (service, milliseconds) of every attempt, failed ones included: the
        # Server-Timing header and /api/stats break the external time down with it.
        self.call_log: list[tuple[str, float]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _record(self, service: str, started: float) -> float:
        elapsed = (time.perf_counter() - started) * 1000
        self.elapsed_ms += elapsed
        self.call_log.append((service, elapsed))
        return elapsed

    def get_json(self, service: str, url: str, params: dict | None = None, min_interval: float = 0.0):
        """GET ``url`` and parse JSON. Returns (status, payload) for any status below 500
        except 429; raises ``UpstreamBusy`` (429/503) or ``ExternalServiceError``."""
        for attempt in range(self.retries + 1):
            last_attempt = attempt == self.retries
            _wait_turn(service, min_interval)
            self.calls.append(service)
            started = time.perf_counter()
            try:
                status, body, headers = send(url, params, self.timeout)
            except urllib3.exceptions.HTTPError as exc:
                self._record(service, started)
                logger.warning("event=external_call_failed service=%s attempt=%s error=%r", service, attempt + 1, exc)
                if not last_attempt:
                    time.sleep(_RETRY_DELAY_SECONDS)
                    continue
                raise ExternalServiceError(f"{service} is unreachable, try again later.") from exc
            elapsed = self._record(service, started)
            logger.info(
                "event=external_call service=%s status=%s duration_ms=%.0f attempt=%s",
                service, status, elapsed, attempt + 1,
            )
            if status in _RETRY_STATUSES and not last_attempt:
                time.sleep(_RETRY_DELAY_SECONDS)
                continue
            if status in (429, 503):
                raise UpstreamBusy(
                    f"{service} is rate limiting or overloaded (HTTP {status}); try again shortly.",
                    retry_after=headers.get("Retry-After") or headers.get("retry-after") or "5",
                )
            if status >= 500:
                raise ExternalServiceError(f"{service} answered HTTP {status}.")
            try:
                return status, json.loads(body)
            except ValueError as exc:
                raise ExternalServiceError(f"{service} returned an invalid response.") from exc
        raise AssertionError("unreachable")  # pragma: no cover
