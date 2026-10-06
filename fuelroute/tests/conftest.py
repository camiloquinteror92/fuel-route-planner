"""Shared fixtures. No test can touch the network: ``fuelroute.services.http.send`` (the
only function that does) fails for every test (``clean_state``, autouse), and tests
that need an external service ask for ``upstream``, a fake that records every call."""

import json

import pytest
from django.core.cache import cache

from fuelroute.services import http
from fuelroute.services.stations import reset_station_arrays


def encode_polyline(points, precision=6):
    """Google polyline encoder (the inverse of osrm.decode_polyline), for fake OSRM answers."""
    factor = 10**precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        lat_i, lon_i = round(lat * factor), round(lon * factor)
        for delta in (lat_i - prev_lat, lon_i - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = lat_i, lon_i
    return "".join(out)


def osrm_answer(points, miles, hours=10.0, snap_miles=(0.0, 0.0)):
    """A successful OSRM /route body for ``points`` [(lat, lon), ...]."""
    return {
        "code": "Ok",
        "routes": [{"geometry": encode_polyline(points), "distance": miles * 1609.344, "duration": hours * 3600}],
        "waypoints": [{"distance": snap_miles[0] * 1609.344}, {"distance": snap_miles[1] * 1609.344}],
    }


class Upstream:
    """Fake network. ``responses`` is consumed in order; the last one repeats.

    Each item: (status, payload[, headers]) | an Exception to raise | a callable(url, params).
    """

    def __init__(self):
        self.calls: list[str] = []
        self.params: list[dict | None] = []
        self.responses: list = []

    def respond(self, *items):
        self.responses = list(items)

    def __call__(self, url, params, timeout):
        self.calls.append(url)
        self.params.append(params)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(item) and not isinstance(item, Exception):
            item = item(url, params)
        if isinstance(item, Exception):
            raise item
        status, payload = item[0], item[1]
        headers = item[2] if len(item) > 2 else {}
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, body, headers


@pytest.fixture
def upstream(monkeypatch):
    fake = Upstream()
    fake.respond((500, {"message": "no response configured"}))
    monkeypatch.setattr(http, "send", fake)
    return fake


def _no_network(url, params, timeout):
    raise AssertionError(f"a test tried to reach the network: {url} (ask for the upstream fixture)")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, settings):
    monkeypatch.setattr(http, "send", _no_network)  # ``upstream`` replaces it with a fake
    cache.clear()
    reset_station_arrays()
    http._next_turn.clear()
    monkeypatch.setattr(http, "_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setitem(settings.FUEL_PLANNER, "NOMINATIM_MIN_INTERVAL_SECONDS", 0.0)
    yield
    cache.clear()
    reset_station_arrays()
