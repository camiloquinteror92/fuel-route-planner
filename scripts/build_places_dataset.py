"""Build ``data/us_places.csv.gz``: an offline lookup table of US place coordinates.

This runs once, offline, before the app is used. The output is committed to the
repository, so you only need to run it again to refresh the data.

Sources (both free, both allow redistribution):

1. US Census Bureau Gazetteer, "Places" file (public domain). Every incorporated
   place and Census Designated Place, with an internal point (lat/lon).
2. GeoNames ``US.zip`` (CC BY 4.0, https://www.geonames.org). Only "populated
   place" features (class ``P``). It fills the gaps the Census file has
   (small unincorporated communities, which is where many truck stops are).

Usage::

    python scripts/build_places_dataset.py            # downloads into data/raw/
    python scripts/build_places_dataset.py --no-download
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUTPUT = ROOT / "data" / "us_places.csv.gz"

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_place_national.zip"
)
GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"
USER_AGENT = "spotter-fuel-route/1.0 (dataset build script)"

# The Census NAME column ends with the legal/statistical area description
# ("Austin city", "Abanda CDP"). We strip it so the name matches what people type.
_LSAD_SUFFIX = re.compile(
    r"\s+("
    r"city and borough|consolidated government \(balance\)|"
    r"metropolitan government \(balance\)|unified government \(balance\)|"
    r"metro government \(balance\)|city \(balance\)|\(balance\)|"
    r"municipality|city|town|village|borough|CDP|comunidad|zona urbana|"
    r"urban county|plantation|township|corporation"
    r")$",
    re.IGNORECASE,
)


def _download(url: str, target: Path) -> Path:
    if target.exists():
        print(f"  using cached {target.name}")
        return target
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        target.write_bytes(response.read())
    return target


def _read_zip_member(zip_path: Path, suffix: str) -> io.TextIOWrapper:
    archive = zipfile.ZipFile(zip_path)
    member = next(name for name in archive.namelist() if name.endswith(suffix))
    return io.TextIOWrapper(archive.open(member), encoding="utf-8")


def gazetteer_rows(zip_path: Path):
    reader = csv.DictReader(_read_zip_member(zip_path, ".txt"), delimiter="|")
    for row in reader:
        row = {key.strip(): value.strip() for key, value in row.items()}
        name = _LSAD_SUFFIX.sub("", row["NAME"]).strip()
        yield name, row["USPS"], float(row["INTPTLAT"]), float(row["INTPTLONG"]), 0, "census"


def geonames_rows(zip_path: Path):
    # Tab-separated, no header. Column reference: https://download.geonames.org/export/dump/readme.txt
    for line in _read_zip_member(zip_path, "US.txt"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 15 or parts[6] != "P":
            continue
        state = parts[10]
        lat, lon = float(parts[4]), float(parts[5])
        population = int(parts[14] or 0)
        names = {parts[1], parts[2]}  # name and ASCII name
        for name in names:
            if name:
                yield name, state, lat, lon, population, "geonames"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-download", action="store_true", help="use files already in data/raw/")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    gaz_zip = RAW_DIR / "2025_Gaz_place_national.zip"
    geo_zip = RAW_DIR / "US.zip"
    if not args.no_download:
        _download(GAZETTEER_URL, gaz_zip)
        _download(GEONAMES_URL, geo_zip)

    # Keep one row per (name, state, source). For GeoNames keep the most populated
    # one, because a state can have several places with the same name.
    best: dict[tuple[str, str, str], tuple] = {}
    for row in [*gazetteer_rows(gaz_zip), *geonames_rows(geo_zip)]:
        name, state, _lat, _lon, population, source = row
        key = (name.lower(), state, source)
        if key not in best or population > best[key][4]:
            best[key] = row

    with gzip.open(OUTPUT, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "state", "lat", "lon", "population", "source"])
        for name, state, lat, lon, population, source in sorted(best.values(), key=lambda r: (r[1], r[0])):
            writer.writerow([name, state, f"{lat:.5f}", f"{lon:.5f}", population, source])

    print(f"wrote {len(best):,} places to {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
