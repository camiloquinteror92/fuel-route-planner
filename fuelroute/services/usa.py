"""Is a point in the USA? Raster lookup on the Census outline of the country.

``data/us_mask.npz`` (built by ``scripts/build_us_mask.py``) holds three boolean
grids: the contiguous states (0.01 deg cells, ~0.7 mi), Alaska and Hawaii. A cell
is "in" when its centre is inside the Census cartographic boundary (shoreline
clipped, Canada / Mexico borders), grown by ~1.5 mi so coastal and border points
are not rejected because of the generalised outline.

Used for:
* request validation (``region_of``): Toronto, Monterrey, Nassau or a point in the
  Gulf of Mexico are rejected; Alaska / Hawaii are recognised so the API can say
  the price file has no stations there;
* route samples (``in_usa``): which parts of a route run outside the country
  (Detroit -> Buffalo crosses Ontario), so stations reachable only from Canada
  are not offered and the response can warn about it.

Points within ~1.5 mi of the border can be classified either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from django.conf import settings

LOWER48, ALASKA, HAWAII = "lower48", "alaska", "hawaii"


@dataclass(frozen=True)
class _Grid:
    lat_min: float
    lon_min: float
    step: float
    cells: np.ndarray  # (rows, cols) bool

    def contains(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        rows = np.floor((lat - self.lat_min) / self.step).astype(np.int64)
        cols = np.floor((lon - self.lon_min) / self.step).astype(np.int64)
        valid = (rows >= 0) & (rows < self.cells.shape[0]) & (cols >= 0) & (cols < self.cells.shape[1])
        out = np.zeros(lat.shape, dtype=bool)
        out[valid] = self.cells[rows[valid], cols[valid]]
        return out


@lru_cache(maxsize=1)
def _grids() -> dict[str, _Grid]:
    data = np.load(settings.FUEL_PLANNER["US_MASK_FILE"])
    grids = {}
    for name in (LOWER48, ALASKA, HAWAII):
        lat_min, lon_min, step, rows, cols = data[f"{name}_meta"]
        bits = np.unpackbits(data[f"{name}_bits"], count=int(rows) * int(cols))
        grids[name] = _Grid(lat_min, lon_min, step, bits.reshape(int(rows), int(cols)).astype(bool))
    return grids


def in_usa(lat, lon) -> np.ndarray:
    """Vectorised: True where (lat, lon) is in the USA (any of the three grids)."""
    lat = np.atleast_1d(np.asarray(lat, dtype=np.float64))
    lon = np.atleast_1d(np.asarray(lon, dtype=np.float64))
    result = np.zeros(lat.shape, dtype=bool)
    for grid in _grids().values():
        result |= grid.contains(lat, lon)
    return result


def region_of(latitude: float, longitude: float) -> str | None:
    """"lower48", "alaska", "hawaii", or None when the point is outside the USA."""
    lat, lon = np.array([latitude], dtype=np.float64), np.array([longitude], dtype=np.float64)
    for name, grid in _grids().items():
        if grid.contains(lat, lon)[0]:
            return name
    return None
