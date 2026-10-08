# Data sources and licences

## `fuel-prices-for-be-assessment.csv`

OPIS truck stop retail prices, provided by Spotter with the backend coding
assessment. Included so the project runs out of the box. It is commercial pricing
data: before publishing this repository outside the assessment, check that
redistribution is allowed, or remove the file and load your own copy with
`python manage.py load_stations --file <path>`.

## `us_places.csv.gz` (built by `scripts/build_places_dataset.py`)

Contains information from two sources, downloaded on 2026-10-06:

1. **US Census Bureau, 2025 Gazetteer Files, "Places"**
   (`2025_Gaz_place_national.zip`,
   https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html).
   Public domain (work of the US Government).
2. **GeoNames**, US country dump (`US.zip`, https://download.geonames.org/export/dump/),
   by GeoNames (https://www.geonames.org), licensed under
   **Creative Commons Attribution 4.0** (https://creativecommons.org/licenses/by/4.0/).

**Changes made to the GeoNames data:** only populated places (feature class P,
without historical / abandoned / destroyed codes) and, for names that nothing else
has, minor civil divisions (ADM3, "Town of" / "Township of" prefix removed) are
kept; names are normalised (lower case, ASCII, abbreviations expanded); points with
the same name within 10 miles are merged with the Census points (Census coordinates,
GeoNames population); places are ranked and written with 5-decimal coordinates.
One non-populated GeoNames feature is added by id (5321154, Willow Beach, AZ).
No endorsement by GeoNames is implied.

## `us_mask.npz` (built by `scripts/build_us_mask.py`)

Raster derived from the **US Census Bureau cartographic boundary file "United
States outline", 1:5,000,000** (`cb_2024_us_nation_5m.zip`,
https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html).
Public domain. Changed: rasterised to 0.01-0.02 degree cells for the contiguous
states, Alaska and Hawaii, and grown by 1.5 miles.

## `station_coords.csv` (built by `scripts/build_station_coords.py`)

Truck stop coordinates matched, once and offline, against **OpenStreetMap** fuel
stations and motorway exits downloaded from the Overpass API (OSM data of
2026-10-08; see `station_coords_report.md` for the matching rules). Read by
`manage.py load_stations`, which uses a row instead of the station's city center;
the app downloads nothing at run time for it.
Derived from OpenStreetMap data, © OpenStreetMap contributors
(https://www.openstreetmap.org/copyright), available under the **Open Database
License (ODbL 1.0)** (https://opendatacommons.org/licenses/odbl/). This derived
database is made available under the same licence. **Changes made:** for each matched
OPIS station ID, only the position of the matched feature (a fuel station's center, a
motorway exit node, or the midpoint of the nodes of one exit), its OSM id(s) and OSM
label are kept, with what the matching computed: a confidence (`high` / `medium`), the
miles from the station's city center and the rule that placed it. The columns are
`opis_id, lat, lon, source, confidence, osm_type_id, matched_label,
miles_from_city_center, reason`; the reasons name rules and exits, not station names.

## At run time

- Routing: OSRM (http://project-osrm.org) public demo server, on OpenStreetMap
  data.
- Geocoding of free text: Nominatim (https://nominatim.org), OpenStreetMap data.
- Map tiles: © OpenStreetMap contributors (https://www.openstreetmap.org/copyright),
  attribution shown on the map page.

OpenStreetMap data is available under the Open Database License (ODbL).
