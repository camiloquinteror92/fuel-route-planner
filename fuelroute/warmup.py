"""Pay the one-off costs at process start instead of on the first request:
the place index (~1 s), the station arrays and numpy/BLAS initialisation."""

import logging
import time

import numpy as np
from django.db import DatabaseError

logger = logging.getLogger(__name__)


def warm_up() -> None:
    from .services.geo import nearest_on_line
    from .services.places import get_place_index
    from .services.stations import get_station_arrays

    started = time.perf_counter()
    places = len(get_place_index())
    try:
        stations = len(get_station_arrays())
    except DatabaseError:  # not migrated yet
        stations = 0
    line = np.array([[30.0, -97.0], [31.0, -96.0]] * 600)
    nearest_on_line(np.full(2000, 30.5), np.full(2000, -96.5), line)
    logger.info(
        "event=warm_up places=%s stations=%s duration_ms=%.0f",
        places, stations, (time.perf_counter() - started) * 1000,
    )
