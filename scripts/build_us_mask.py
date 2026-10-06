"""Build ``data/us_mask.npz``: a raster of US land used to answer "is this point in the USA?".

Source: US Census Bureau cartographic boundary file "United States outline",
1:5,000,000 (``cb_2024_us_nation_5m.zip``, public domain). It is a shapefile of
polygons clipped to the shoreline, with the Canada / Mexico borders.

Why a raster and not the polygon: the API needs the check for 2 endpoints per
request AND for ~3,000 route samples (to know which parts of a route run outside
the USA, e.g. Detroit -> Buffalo through Ontario). A raster lookup is one numpy
indexing operation for all of them (microseconds), with no geometry dependency.

Grids (cell centre inside the polygon = 1, by scanline fill with the even-odd rule):

* lower48: 0.01 deg (~0.7 mi), lat 24.3..49.5, lon -125..-66.8
* alaska:  0.02 deg (~1 mi),  lat 51..71.6,  lon -180..-129.9
* hawaii:  0.01 deg,          lat 18.8..22.4, lon -160.5..-154.6

Then each grid is dilated by ~1.5 mi so that coastal points (a pier, the Keys, the
Outer Banks) and points on the exact border are not rejected because of the
generalised outline. Territories (Puerto Rico, Guam...) are not included: the
price file has no stations there.

Usage::

    python scripts/build_us_mask.py            # downloads into data/raw/
    python scripts/build_us_mask.py --no-download
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUTPUT = ROOT / "data" / "us_mask.npz"
URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_nation_5m.zip"
USER_AGENT = "spotter-fuel-route/1.0 (dataset build script)"

# name: (lat_min, lat_max, lon_min, lon_max, step_degrees)
GRIDS = {
    "lower48": (24.3, 49.5, -125.0, -66.8, 0.01),
    "alaska": (51.0, 71.6, -180.0, -129.9, 0.02),
    "hawaii": (18.8, 22.4, -160.5, -154.6, 0.01),
}
DILATE_MILES = 1.5


def _download(url: str, target: Path) -> Path:
    if target.exists():
        print(f"  using cached {target.name}")
        return target
    for attempt in range(1, 4):
        try:
            print(f"  downloading {url}")
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                target.write_bytes(response.read())
            return target
        except OSError as exc:  # DNS hiccups happen; retry a couple of times
            print(f"  attempt {attempt} failed: {exc}")
            time.sleep(2 * attempt)
    raise SystemExit(f"could not download {url}")


def read_rings(zip_path: Path) -> list[np.ndarray]:
    """Every ring of every polygon in the shapefile, as (N, 2) arrays of (lon, lat).

    Shapefile format (ESRI whitepaper): 100-byte header, then records of
    [record number, content length] (big-endian) + content: shape type (5 =
    polygon), bounding box, number of parts, number of points, part start indexes,
    then the points as little-endian doubles (x = lon, y = lat).
    """
    archive = zipfile.ZipFile(zip_path)
    name = next(n for n in archive.namelist() if n.endswith(".shp"))
    data = archive.read(name)
    rings, offset = [], 100
    while offset < len(data):
        _number, length_words = struct.unpack(">ii", data[offset:offset + 8])
        content = data[offset + 8: offset + 8 + 2 * length_words]
        offset += 8 + 2 * length_words
        shape_type = struct.unpack("<i", content[:4])[0]
        if shape_type != 5:
            continue
        num_parts, num_points = struct.unpack("<ii", content[36:44])
        parts = list(struct.unpack(f"<{num_parts}i", content[44:44 + 4 * num_parts]))
        points = np.frombuffer(content, dtype="<f8", count=2 * num_points, offset=44 + 4 * num_parts)
        points = points.reshape(-1, 2)
        for start, end in zip(parts, parts[1:] + [num_points]):
            rings.append(points[start:end])
    return rings


def rasterize(rings: list[np.ndarray], lat_min, lat_max, lon_min, lon_max, step) -> np.ndarray:
    """Scanline fill: for each row, x-crossings of every edge with the row's centre line."""
    rows = int(round((lat_max - lat_min) / step))
    cols = int(round((lon_max - lon_min) / step))
    edges = np.concatenate([np.column_stack((ring[:-1], ring[1:])) for ring in rings if len(ring) > 1])
    x1, y1, x2, y2 = edges.T
    keep = ~((np.maximum(y1, y2) < lat_min) | (np.minimum(y1, y2) > lat_max))
    x1, y1, x2, y2 = x1[keep], y1[keep], x2[keep], y2[keep]
    grid = np.zeros((rows, cols), dtype=bool)
    centres_x = lon_min + (np.arange(cols) + 0.5) * step
    for row in range(rows):
        y = lat_min + (row + 0.5) * step
        crossing = (y1 <= y) != (y2 <= y)
        if not crossing.any():
            continue
        xs = np.sort(x1[crossing] + (y - y1[crossing]) * (x2[crossing] - x1[crossing]) / (y2[crossing] - y1[crossing]))
        # Cells between crossings 0-1, 2-3, ... are inside (even-odd rule).
        inside = np.searchsorted(xs, centres_x) % 2 == 1
        grid[row] = inside
    return grid


def dilate(grid: np.ndarray, cells_lat: int, cells_lon: int) -> np.ndarray:
    """Grow the land by a rectangle of cells (cheap morphological dilation)."""
    out = grid.copy()
    for shift in range(1, cells_lat + 1):
        out[shift:] |= grid[:-shift]
        out[:-shift] |= grid[shift:]
    grown = out.copy()
    for shift in range(1, cells_lon + 1):
        grown[:, shift:] |= out[:, :-shift]
        grown[:, :-shift] |= out[:, shift:]
    return grown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use the file already in data/raw/")
    args = parser.parse_args()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = RAW_DIR / "cb_2024_us_nation_5m.zip"
    if not args.no_download:
        _download(URL, zip_path)

    rings = read_rings(zip_path)
    payload = {}
    for name, (lat_min, lat_max, lon_min, lon_max, step) in GRIDS.items():
        grid = rasterize(rings, lat_min, lat_max, lon_min, lon_max, step)
        mid_lat = np.radians((lat_min + lat_max) / 2)
        cells_lat = max(1, int(round(DILATE_MILES / (69.0 * step))))
        cells_lon = max(1, int(round(DILATE_MILES / (69.17 * np.cos(mid_lat) * step))))
        grid = dilate(grid, cells_lat, cells_lon)
        payload[f"{name}_bits"] = np.packbits(grid, axis=None)
        payload[f"{name}_meta"] = np.array([lat_min, lon_min, step, grid.shape[0], grid.shape[1]], dtype=np.float64)
        print(f"  {name}: {grid.shape[0]}x{grid.shape[1]} cells, {grid.mean():.1%} land")
    np.savez_compressed(OUTPUT, **payload)
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size / 1e3:.0f} KB) from {len(rings):,} rings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
