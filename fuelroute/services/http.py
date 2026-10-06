"""Thin HTTP client for the free external APIs.

One instance per API request. It counts the calls it makes, so the response can
report exactly how many external calls a request needed (the goal is 1, or 0 when
the route comes from the cache).
"""

from __future__ import annotations

import logging
import time

import requests
from django.conf import settings

from .errors import ExternalServiceError

logger = logging.getLogger(__name__)


class ExternalApiClient:
    def __init__(self, session: requests.Session | None = None):
        config = settings.FUEL_PLANNER
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config["USER_AGENT"]
        self.timeout = config["HTTP_TIMEOUT_SECONDS"]
        self.calls: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def get_json(self, service: str, url: str, params: dict | None = None):
        self.calls.append(service)
        started = time.perf_counter()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("event=external_call_failed service=%s error=%r", service, exc)
            raise ExternalServiceError(f"{service} is unreachable, try again later.") from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "event=external_call service=%s status=%s duration_ms=%.0f",
            service, response.status_code, elapsed_ms,
        )
        if response.status_code >= 500 or response.status_code == 429:
            raise ExternalServiceError(f"{service} answered HTTP {response.status_code}.")
        try:
            return response.status_code, response.json()
        except ValueError as exc:
            raise ExternalServiceError(f"{service} returned an invalid response.") from exc
