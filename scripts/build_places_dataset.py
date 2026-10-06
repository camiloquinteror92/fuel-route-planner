"""Build ``data/us_places.csv.gz``: an offline lookup table of US place coordinates.

This runs once, offline, before the app is used. The output is committed to the
repository, so you only need to run it again to refresh the data.

Sources (both free, both allow redistribution):

1. US Census Bureau Gazetteer, "Places" file (public domain). Every incorporated
   place and Census Designated Place (CDP), with an internal point (lat/lon), its
   functional status (``FUNCSTAT``: ``A`` = incorporated, ``S`` = CDP) and its land
   area.
2. GeoNames ``US.zip`` (CC BY 4.0, https://www.geonames.org). "Populated place"
   features (class ``P``, without the historical / abandoned / destroyed codes).
   It fills the gaps the Census file has (small unincorporated communities, where
   many truck stops are) and, importantly, it has a POPULATION column, which the
   Gazetteer lacks. Minor civil divisions ("Town of Willington", class ``A`` /
   ``ADM3``) are used only as a last resort for names nothing else matches: that
   is how New England towns, which are not Census places, get coordinates.

Homonyms. A state can have several places with the same name (Tennessee has 12
"Antioch"; California has two "Mountain View" 70 miles apart). The output keeps
ALL of them, one row per distinct place, grouped by (normalized name, state) and
ordered best-first:

* Census and GeoNames points of the same place (same name, < 10 miles apart) are
  merged into one: Census internal point, GeoNames population.
* Places are ranked by population, then incorporated city > CDP > GeoNames
  populated place > neighbourhood > minor civil division, then land area.

The app uses the first row for user input ("Mountain View, CA" is the city of
80k people, not the 0.3 sq mi CDP) and all rows to detect ambiguous station
cities (see ``fuelroute/services/station_loader.py``).

Columns: ``key, state, lat, lon, population, source`` where ``key`` is the name
already normalized with ``fuelroute.services.text.normalize_place`` (so the app
does not have to normalize 200k names at start-up).

Usage::

    python scripts/build_places_dataset.py            # downloads into data/raw/
    python scripts/build_places_dataset.py --no-download
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import math
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fuelroute.services.text import normalize_place  # noqa: E402  (pure Python, no Django)

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
# GeoNames minor civil divisions are named "Town of Derby", "Township of Crescent".
_MCD_PREFIX = re.compile(r"^(town|township|city|village|borough|plantation) of\s+", re.IGNORECASE)

# Historical (H), abandoned (Q), destroyed (W) populated places are not used.
_SKIPPED_FEATURE_CODES = {"PPLH", "PPLQ", "PPLW", "PPLCH"}

# Lower is better when two places share a name and a population.
_KIND_RANK = {
    "census_A": 0,  # incorporated city / town
    "census_S": 1,  # Census Designated Place
    "geonames_PPLA": 1,  # county seat or state capital
    "geonames_PPL": 2,
    "geonames_other": 3,  # PPLX (section of a city), PPLL (locality)...
    "geonames_ADM3": 4,  # minor civil division, last resort
}

# Points of the same name closer than this are the same place.
MERGE_MILES = 10.0

# Station cities the sources spell differently. Values are (name, state) that exist
# in the sources; the alias gets the same coordinates.
ALIASES = {
    ("Hot Springs National Park", "AR"): ("Hot Springs", "AR"),  # postal name of Hot Springs
    ("Pueblo Of Acoma", "NM"): ("Acoma Pueblo", "NM"),
}
# Places that only exist in GeoNames as a non-populated feature. geonameid -> (name, state).
MANUAL_GEONAMES = {
    5321154: ("Willow Beach", "AZ"),  # settlement and marina on US-93, below Hoover Dam
}


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


def _miles(a: dict, b: dict) -> float:
    """Haversine distance between two candidates (dicts with lat/lon)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(min(1.0, h)))


def gazetteer_rows(zip_path: Path):
    reader = csv.DictReader(_read_zip_member(zip_path, ".txt"), delimiter="|")
    for row in reader:
        row = {key.strip(): (value or "").strip() for key, value in row.items()}
        funcstat = "A" if row["FUNCSTAT"] == "A" else "S"
        yield {
            "name": _LSAD_SUFFIX.sub("", row["NAME"]).strip(),
            "state": row["USPS"],
            "lat": float(row["INTPTLAT"]),
            "lon": float(row["INTPTLONG"]),
            "population": 0,  # the Gazetteer has no population; GeoNames fills it in _cluster
            "area": float(row["ALAND_SQMI"] or 0),
            "kind": f"census_{funcstat}",
            "source": "census",
        }


def geonames_rows(zip_path: Path):
    """Populated places, minor civil divisions and the manual extras.

    Tab-separated, no header. Column reference:
    https://download.geonames.org/export/dump/readme.txt
    """
    for line in _read_zip_member(zip_path, "US.txt"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 15:
            continue
        geonameid, feature_class, code = int(parts[0]), parts[6], parts[7]
        base = {
            "state": parts[10],
            "lat": float(parts[4]),
            "lon": float(parts[5]),
            "population": int(parts[14] or 0),
            "area": 0.0,
            "source": "geonames",
        }
        if geonameid in MANUAL_GEONAMES:
            name, state = MANUAL_GEONAMES[geonameid]
            yield {**base, "name": name, "state": state, "kind": "geonames_PPL"}
        elif feature_class == "P" and code not in _SKIPPED_FEATURE_CODES:
            if code == "PPL":
                kind = "geonames_PPL"
            elif code.startswith("PPLA") or code == "PPLC":
                kind = "geonames_PPLA"
            else:
                kind = "geonames_other"
            for name in {parts[1], parts[2]}:  # name and ASCII name
                if name:
                    yield {**base, "name": name, "kind": kind}
        elif feature_class == "A" and code == "ADM3":
            name = _MCD_PREFIX.sub("", parts[1]).strip()
            if name:
                yield {**base, "name": name, "kind": "geonames_ADM3"}


def _cluster(candidates: list[dict]) -> list[dict]:
    """Merge the points of one (name, state) that are the same place; best first.

    Candidates are visited Census first (their internal point is the most reliable
    centre), then by kind and population. Each one joins the first cluster within
    ``MERGE_MILES`` (the cluster keeps the larger population and the better kind)
    or starts a new cluster.
    """
    order = sorted(candidates, key=lambda c: (c["source"] != "census", _KIND_RANK[c["kind"]], -c["population"]))
    clusters: list[dict] = []
    for candidate in order:
        for cluster in clusters:
            if _miles(cluster, candidate) <= MERGE_MILES:
                cluster["population"] = max(cluster["population"], candidate["population"])
                if _KIND_RANK[candidate["kind"]] < _KIND_RANK[cluster["kind"]]:
                    cluster["kind"] = candidate["kind"]
                break
        else:
            clusters.append(dict(candidate))
    clusters.sort(key=lambda c: (-c["population"], _KIND_RANK[c["kind"]], -c["area"]))
    return clusters


def build_groups(rows) -> dict[tuple[str, str], list[dict]]:
    """Group rows by (normalized name, state) and cluster each group."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    mcd: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (normalize_place(row["name"]), row["state"])
        if not key[0] or not row["state"]:
            continue
        (mcd if row["kind"] == "geonames_ADM3" else groups)[key].append(row)

    # Minor civil divisions only fill names that nothing else has, and only when the
    # name is unique in the state (Ohio has dozens of "Township of Washington").
    for key, rows_for_key in mcd.items():
        if key not in groups and len(_cluster(rows_for_key)) == 1:
            groups[key] = rows_for_key

    for (alias, state), (name, target_state) in ALIASES.items():
        target = groups.get((normalize_place(name), target_state))
        if target:
            groups.setdefault((normalize_place(alias), state), [dict(c) for c in target])

    return {key: _cluster(candidates) for key, candidates in groups.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-download", action="store_true", help="use files already in data/raw/")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    gaz_zip = RAW_DIR / "2025_Gaz_place_national.zip"
    geo_zip = RAW_DIR / "US.zip"
    if not args.no_download:
        _download(GAZETTEER_URL, gaz_zip)
        _download(GEONAMES_URL, geo_zip)

    groups = build_groups([*gazetteer_rows(gaz_zip), *geonames_rows(geo_zip)])

    rows = 0
    with gzip.open(OUTPUT, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["key", "state", "lat", "lon", "population", "source"])
        for key, state in sorted(groups):
            for place in groups[(key, state)]:  # best first
                writer.writerow(
                    [key, state, f"{place['lat']:.5f}", f"{place['lon']:.5f}", place["population"], place["source"]]
                )
                rows += 1

    homonyms = sum(1 for places in groups.values() if len(places) > 1)
    print(
        f"wrote {rows:,} places ({len(groups):,} names, {homonyms:,} with homonyms) "
        f"to {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size / 1e6:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
