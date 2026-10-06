"""In-process benchmark of the planner: ``python manage.py benchmark`` -> ``data/benchmark.json``.

Nothing here touches the network while it measures. The New York -> Los Angeles
route comes from ``data/benchmark_route.json``: the answer OSRM gave once, kept on
disk exactly as it came (the command makes that ONE call only when the file is
missing, or with ``--refresh-route``). It is fed through the same code a live
answer goes through (``osrm.route_from_payload``) and put in the route cache, so
every measured request is a route-cache hit with 0 external calls (the network
function is replaced by one that fails, to prove it).

Scenarios (sequential, one thread, in this process, no sockets):

* ``plan_cache_hit``: ``GET /api/route`` for a trip already planned, through the
  whole Django stack (middleware, DRF, JSON render, gzip): what a repeated trip
  costs the server.
* ``what_if_replan``: the same trip with a different ``mpg`` each time: the plan
  cache misses, the route cache hits, so the request re-plans everything after the
  routing call (corridor, optimizer, comparison, response).
* ``planning_no_network``: the planner's own work on that route, called directly:
  the corridor search + the tank rules + the optimizer.
* ``route_preparation``: decoding OSRM's polyline and preparing it (resample, US
  mask, simplify): the part of a NEW trip that is ours, not OSRM's.

Results: latency p50 / p95 / mean / max in ms and requests per second
(iterations / total time), plus the machine, the process memory (RSS) and the
number of stations. ``/api/stats`` publishes the file (``load_benchmark``).
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import django
import numpy as np
from django.conf import settings
from django.core.cache import cache

from . import http
from .geocoding import geocode
from .http import ExternalApiClient
from .osrm import METERS_PER_MILE, ROUTE_PARAMS, decode_polyline, prepare_route, route_cache_key, route_from_payload
from .stations import get_station_arrays, stations_along_route

START, FINISH = "New York, NY", "Los Angeles, CA"
SCENARIOS = ("plan_cache_hit", "what_if_replan", "planning_no_network", "route_preparation")
DESCRIPTIONS = {
    "plan_cache_hit": "A trip that was already planned: GET /api/route through the whole Django stack.",
    "what_if_replan": (
        "The same trip with a different mpg each time: the plan is recomputed on the cached route "
        "(corridor, optimizer, comparison, response), with no external call."
    ),
    "planning_no_network": "The planner's own work on this route: corridor search, tank rules and optimizer.",
    "route_preparation": "Decoding OSRM's polyline and preparing the route (resample, US outline, simplify).",
}
_lock = threading.Lock()
_cache: dict = {}


def benchmark_file() -> Path:
    return Path(settings.FUEL_PLANNER["BENCHMARK_FILE"])


def route_file() -> Path:
    return Path(settings.FUEL_PLANNER["BENCHMARK_ROUTE_FILE"])


def load_benchmark() -> dict | None:
    """Contents of ``data/benchmark.json`` (cached per modification time), or None."""
    path = benchmark_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _lock:
        cached = _cache.get(str(path))
    if cached and cached[0] == mtime:
        return json.loads(cached[1])
    try:
        text = path.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, ValueError):
        return None
    with _lock:
        _cache[str(path)] = (mtime, text)
    return json.loads(text)  # a fresh copy each time: callers may change theirs


# --- the machine -------------------------------------------------------------------------------


def _cpu_name() -> str:
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, timeout=2, check=True
            )
            return out.stdout.decode().strip()
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001 - best effort, any platform
        pass
    return platform.processor() or platform.machine() or "unknown"


def process_memory() -> dict:
    """Resident memory of this process: {"rss_mb", "kind"} ("current", or "peak" on macOS)."""
    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            query = kernel32.K32GetProcessMemoryInfo
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            query.restype = wintypes.BOOL
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                # The working set: the memory of this process that is in RAM now.
                return {"rss_mb": round(counters.WorkingSetSize / 2**20, 1), "kind": "current"}
        elif sys.platform.startswith("linux"):
            with open("/proc/self/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        return {"rss_mb": round(int(line.split()[1]) / 1024, 1), "kind": "current"}
        else:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS
            return {"rss_mb": round(peak / 2**20, 1), "kind": "peak"}
    except Exception:  # noqa: BLE001 - best effort
        pass
    return {"rss_mb": None, "kind": None}


def machine() -> dict:
    return {
        "os": platform.platform(),
        "cpu": _cpu_name(),
        "cores": os.cpu_count(),
        "python": platform.python_version(),
        "django": django.get_version(),
        "numpy": np.__version__,
    }


# --- the route ---------------------------------------------------------------------------------


def _fetch_route_answer(origin, destination) -> dict:
    """The ONE external call: ask OSRM for the route and keep its answer on disk."""
    from .osrm import route_url

    client = ExternalApiClient()
    status, payload = client.get_json("osrm", route_url(origin, destination), params=dict(ROUTE_PARAMS))
    route_from_payload(status, payload)  # raises if the answer is not a usable route
    best = payload["routes"][0]
    answer = {
        "code": payload["code"],
        "routes": [{"geometry": best["geometry"], "distance": best["distance"], "duration": best["duration"]}],
        "waypoints": [{"distance": w.get("distance")} for w in (payload.get("waypoints") or [])[:2]],
    }
    record = {
        "start": origin.query,
        "finish": destination.query,
        "start_point": [origin.latitude, origin.longitude],
        "finish_point": [destination.latitude, destination.longitude],
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "service": settings.FUEL_PLANNER["OSRM_URL"],
        "params": dict(ROUTE_PARAMS),
        "attribution": "Route by OSRM from OpenStreetMap data, (c) OpenStreetMap contributors, ODbL.",
        "osrm_answer": answer,
    }
    path = route_file()
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8", newline="\n")
    return record


def benchmark_route(refresh: bool = False, log=print):
    """(origin, destination, route, record): from the file, or ONE OSRM call to create it."""
    client = ExternalApiClient()
    origin = geocode(START, client, field="start")
    destination = geocode(FINISH, client, field="finish")
    path = route_file()
    record = None
    if path.exists() and not refresh:
        record = json.loads(path.read_text(encoding="utf-8"))
        same = (
            np.allclose(record["start_point"], [origin.latitude, origin.longitude])
            and np.allclose(record["finish_point"], [destination.latitude, destination.longitude])
        )
        if not same:
            log(f"{path.name} is for other coordinates: fetching the route again.")
            record = None
    if record is None:
        log(f"Fetching {START} -> {FINISH} from OSRM (one external call) into {path.name} ...")
        record = _fetch_route_answer(origin, destination)
    route = route_from_payload(200, record["osrm_answer"])
    return origin, destination, route, record


# --- measuring -----------------------------------------------------------------------------------


def _stats(samples_ms: list[float]) -> dict:
    ordered = sorted(samples_ms)

    def rank(p: float) -> float:
        return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

    total_s = sum(samples_ms) / 1000
    return {
        "iterations": len(ordered),
        "p50_ms": round(rank(50), 2),
        "p95_ms": round(rank(95), 2),
        "mean_ms": round(sum(ordered) / len(ordered), 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
        "requests_per_second": round(len(ordered) / total_s, 1) if total_s > 0 else None,
    }


def _measure(function, iterations: int, warmup: int = 3) -> list[float]:
    for i in range(warmup):
        function(-1 - i)
    samples = []
    for i in range(iterations):
        started = time.perf_counter()
        function(i)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def _no_network(url, params, timeout):
    raise AssertionError(f"the benchmark tried to reach the network: {url}")


def run_benchmark(iterations: int = 300, replans: int = 100, refresh_route: bool = False, log=print) -> dict:
    """Measure the four scenarios and return the document written to benchmark.json."""
    from django.test import Client

    from .planner import PlanSettings, _optimize, _tank_rules

    origin, destination, route, record = benchmark_route(refresh_route, log)
    config = settings.FUEL_PLANNER
    app_logger = logging.getLogger("fuelroute")
    saved_limit, saved_send, saved_level = config["RATE_LIMIT_PER_MINUTE"], http.send, app_logger.level
    config["RATE_LIMIT_PER_MINUTE"] = 0  # the benchmark is one client sending many requests
    http.send = _no_network
    app_logger.setLevel(logging.WARNING)  # one log line per request would flood the console
    try:
        cache.set(route_cache_key(origin, destination), route)
        client = Client(HTTP_HOST="127.0.0.1", HTTP_ACCEPT_ENCODING="gzip")
        params = {"start": START, "finish": FINISH}
        calls = {"total": 0}

        def get(extra: dict) -> dict:
            response = client.get("/api/route", {**params, **extra})
            if response.status_code != 200:
                raise RuntimeError(f"/api/route answered {response.status_code}: {response.content[:300]!r}")
            calls["total"] += int(response["Server-Timing"].count("osrm;"))
            return response

        first = get({})
        body = json.loads(_decode(first))
        if body["meta"]["route_cache"] != "hit" or body["meta"]["external_api_calls"] != 0:
            raise RuntimeError("the benchmark route was not served from the cache")

        log(f"plan_cache_hit x{iterations} ...")
        hit_ms = _measure(lambda i: get({}), iterations)
        hit_bytes = len(get({}).content)

        log(f"what_if_replan x{replans} ...")
        # A distinct mpg per request (never the default): every request misses the plan cache.
        replan_ms = _measure(lambda i: get({"mpg": f"{9.5 - (i + 10) * 0.004:.3f}"}), replans)

        log(f"planning_no_network x{replans} ...")
        plan = PlanSettings.defaults()
        arrays = get_station_arrays()

        def plan_only(_i):
            corridor = stations_along_route(
                route.samples, route.sample_miles, plan.corridor_miles, arrays, route.sample_in_usa
            )
            _optimize(route, corridor, _tank_rules(plan, route, corridor), plan)

        planning_ms = _measure(plan_only, replans)
        corridor = stations_along_route(
            route.samples, route.sample_miles, plan.corridor_miles, arrays, route.sample_in_usa
        )

        answer = record["osrm_answer"]["routes"][0]
        snaps = [float(w.get("distance") or 0) / METERS_PER_MILE for w in record["osrm_answer"]["waypoints"]]
        preparations = max(5, replans // 5)
        log(f"route_preparation x{preparations} ...")
        prepare_ms = _measure(
            lambda i: prepare_route(
                decode_polyline(answer["geometry"]), answer["distance"] / METERS_PER_MILE, answer["duration"], snaps
            ),
            preparations,
            warmup=1,
        )
    finally:
        config["RATE_LIMIT_PER_MINUTE"] = saved_limit
        http.send = saved_send
        app_logger.setLevel(saved_level)

    results = {
        "plan_cache_hit": {**_stats(hit_ms), "response_bytes_gzip": hit_bytes},
        "what_if_replan": _stats(replan_ms),
        "planning_no_network": _stats(planning_ms),
        "route_preparation": _stats(prepare_ms),
    }
    for key, block in results.items():
        block["description"] = DESCRIPTIONS[key]
    summary = body["summary"]
    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": "python manage.py benchmark",
        "method": (
            "Sequential requests in one process and one thread, through Django's test client (no sockets, "
            "so no network time) with gzip and without the per-request log lines; the route comes from "
            "data/benchmark_route.json, so 0 external calls. requests_per_second = iterations / total time."
        ),
        "machine": machine(),
        "trip": {
            "start": START,
            "finish": FINISH,
            "distance_miles": body["route"]["distance_miles"],
            "fuel_stops": summary["number_of_stops"],
            "total_fuel_cost": summary["total_fuel_cost"],
            "corridor_candidates": len(corridor),
            "route_samples": len(route.samples),
            "osrm_geometry_points": route.geometry_points,
            "route_fetched_at": record.get("fetched_at"),
        },
        "stations": len(get_station_arrays()),
        "memory": process_memory(),
        "external_api_calls": calls["total"],
        "results": results,
    }


def _decode(response) -> bytes:
    import gzip

    content = response.content
    return gzip.decompress(content) if response.get("Content-Encoding") == "gzip" else content


def write_benchmark(document: dict) -> Path:
    path = benchmark_file()
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path
