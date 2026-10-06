"""Pay the one-off costs at process start instead of on the first request.

Called from ``config/wsgi.py``. Loads the places index (~0.4 s), the US mask, the
station arrays (if the table exists) and runs numpy once with the shapes of a
coast-to-coast request. The station arrays reload by themselves later if
``load_stations`` changes the data (``stations.data_version``).
"""

import logging
import time

import numpy as np
from django.db import DatabaseError

logger = logging.getLogger(__name__)


def warm_up() -> None:
    from .services.geo import nearest_on_line
    from .services.places import get_place_index
    from .services.stations import get_station_arrays
    from .services.usa import in_usa

    started = time.perf_counter()
    places = len(get_place_index())
    in_usa(40.0, -100.0)
    try:
        stations = len(get_station_arrays())
    except DatabaseError:  # not migrated yet: the first request will load them
        stations = 0
    # Same shapes as a coast-to-coast request.
    line = np.column_stack((np.linspace(30, 40, 3000), np.linspace(-120, -75, 3000)))
    nearest_on_line(np.full(5000, 35.0), np.linspace(-120, -75, 5000), line)
    logger.info(
        "event=warm_up places=%s stations=%s duration_ms=%.0f",
        places, stations, (time.perf_counter() - started) * 1000,
    )
