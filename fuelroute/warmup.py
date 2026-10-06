"""Pay the one-off costs at process start instead of on the first request.

Called from ``config/wsgi.py``. Loads the places index (~0.4 s) and its type-ahead
index for ``/api/places`` (~0.1 s), the US mask, the
station arrays (if the table exists), runs numpy once with the shapes of a
coast-to-coast request and builds ``/api/about`` once (git, source AST, JUnit
report), so the planner page's first load is fast too. The station arrays reload
by themselves later if ``load_stations`` changes the data
(``stations.data_version``). The figures are published in ``/api/stats``
(``process.warm_up``).
"""

import logging
import time

import numpy as np
from django.db import DatabaseError

logger = logging.getLogger(__name__)


def warm_up() -> None:
    from .services.geo import nearest_on_line
    from .services.metrics import route_metrics
    from .services.places import get_place_index
    from .services.stations import get_station_arrays
    from .services.usa import in_usa

    started = time.perf_counter()
    places = len(get_place_index())
    get_place_index().search("warm up", 1)  # builds the type-ahead index of /api/places
    in_usa(40.0, -100.0)
    try:
        stations = len(get_station_arrays())
    except DatabaseError:  # not migrated yet: the first request will load them
        stations = 0
    # Same shapes as a coast-to-coast request.
    line = np.column_stack((np.linspace(30, 40, 3000), np.linspace(-120, -75, 3000)))
    nearest_on_line(np.full(5000, 35.0), np.linspace(-120, -75, 5000), line)
    planner_ms = (time.perf_counter() - started) * 1000
    try:
        from .services.about import build_about

        build_about()
    except Exception:  # never block the start-up: /api/about builds it on first use
        logger.exception("event=warm_up_about_failed")
    total_ms = (time.perf_counter() - started) * 1000
    route_metrics.set_warm_up(total_ms, places, stations)
    logger.info(
        "event=warm_up places=%s stations=%s planner_ms=%.0f duration_ms=%.0f",
        places, stations, planner_ms, total_ms,
    )
