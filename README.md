# Spotter Fuel Route API

A Django REST API that takes a start and a finish location in the USA and returns:

- the driving route (as GeoJSON, plus a link to an interactive map),
- the **cheapest places to fuel up** along the route, for a truck with a **500-mile range** and **10 MPG**,
- how many gallons to buy at each stop and the **total money spent on fuel**.

It makes **one call to a free routing API per new trip** (OSRM, no API key) and **zero** for a trip it has seen before. A new coast-to-coast trip takes about 1.5 s (almost all of it is the routing API); a repeated one takes about 5 ms.

---

## Quick start (5 commands)

Requires Python 3.12+ (Django 6.1). Tested with Python 3.14 on Windows.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations                           # ~3 s, geocodes all stations offline
python manage.py runserver                               # http://127.0.0.1:8000
```

Try it:

```bash
curl "http://127.0.0.1:8000/api/route?start=New%20York,%20NY&finish=Los%20Angeles,%20CA"
```

Run the tests: `pytest` (125 tests, about 2 seconds, no network).

Postman: import `postman/collection.json` (variable `baseUrl` = `http://127.0.0.1:8000`).

---

## API

### `GET /api/route` (or `POST /api/route` with a JSON body)

| Parameter    | Required | Example                     | Notes |
|--------------|----------|-----------------------------|-------|
| `start`      | yes      | `New York, NY` or `40.71,-74.00` | "City, ST", "City, State", "City ST" or "lat,lon" |
| `finish`     | yes      | `Los Angeles, CA`           | same formats |
| `start_tank` | no       | `empty` (default) or `full` | see [Assumptions](#assumptions) |

Response (trimmed; `Chicago, IL -> Houston, TX`, real output):

```json
{
  "start":  {"query": "Chicago, IL", "label": "Chicago, IL", "lat": 41.83705, "lon": -87.68494, "geocoder": "offline"},
  "finish": {"query": "Houston, TX", "label": "Houston, TX", "lat": 29.78574, "lon": -95.38881, "geocoder": "offline"},
  "route":   {"distance_miles": 1083.1, "duration_hours": 19.89},
  "vehicle": {"max_range_miles": 500.0, "miles_per_gallon": 10.0, "tank_gallons": 50.0, "start_tank": "empty"},
  "summary": {
    "total_fuel_cost": 316.76,
    "total_gallons_purchased": 108.31,
    "fuel_used_gallons": 108.31,
    "number_of_stops": 6,
    "average_price_paid": 2.925,
    "candidate_stations_on_route": 179,
    "note": "Every mile of the trip is paid for: the truck leaves with a 50-mile reserve and arrives with the same reserve."
  },
  "fuel_stops": [
    {
      "stop": 2, "opis_id": 68266, "name": "FREDS FUEL AND FOOD #2",
      "address": "I-57, EXIT 283 & US-24", "city": "Gilman", "state": "IL",
      "lat": 40.7635, "lon": -88.00208,
      "price_per_gallon": 3.049, "mile_marker": 82.0, "distance_from_route_miles": 0.4,
      "fuel_on_arrival_gallons": 0.0, "gallons": 12.4, "cost": 37.81
    }
  ],
  "map": {
    "map_url": "http://127.0.0.1:8000/api/route/map?start=Chicago%2C+IL&finish=Houston%2C+TX&start_tank=empty",
    "geojson": {"type": "FeatureCollection", "features": ["LineString of the route", "start/finish Points", "one Point per fuel stop"]}
  },
  "meta": {
    "external_api_calls": 1,
    "external_api_services": ["osrm"],
    "route_cache": "miss",
    "plan_cache": "miss",
    "timings_ms": {"geocoding_ms": 0.0, "routing_ms": 1128.0, "corridor_ms": 4.9, "optimizer_ms": 0.2, "response_build_ms": 13.8}
  }
}
```

Every response has an `X-Response-Time-ms` header.

### `GET /api/route/map?start=...&finish=...`

An HTML page (Leaflet + OpenStreetMap tiles) with the route, the start/finish and numbered fuel stops (click a stop to see price, gallons and cost). It reads the plan from the cache, so it makes **no** external call. The JSON response already contains its URL in `map.map_url`.

### Errors

| Status | `error`                     | When |
|--------|-----------------------------|------|
| 400    | `invalid_request`           | missing field, bad coordinates, point outside the USA, start == finish |
| 400    | `location_not_found`        | a place name that cannot be geocoded |
| 400    | `location_outside_usa`      | a place name that geocodes outside the USA |
| 422    | `no_route`                  | OSRM finds no drivable route |
| 422    | `no_reachable_fuel_station` | a gap longer than 500 miles without a station (response says where) |
| 502    | `upstream_unavailable`      | the routing / geocoding service is down or rate-limited |

---

## How it works

```
 start, finish ──► geocode ──► route ──► stations near the route ──► optimizer ──► JSON + map
                   (offline)   (OSRM,    (numpy, ~20 ms)             (greedy,
                               1 call,                                <2 ms)
                               cached)
```

1. **Geocoding the inputs (0 external calls).** "lat,lon" is used as is. "City, ST" is looked up in an offline table of ~180k US places (see below). Only free text that is not "City, ST" (for example a street address) goes to Nominatim, once, and the answer is cached for a day.
2. **Routing (1 external call).** One request to the public OSRM server: `/route/v1/driving/{lon,lat};{lon,lat}?overview=full&geometries=polyline6`. `polyline6` is about 5x smaller than GeoJSON on the wire, so the call is faster. The decoded route is cached (Django cache) by start/finish, and so is the final plan.
3. **Stations near the route.** The route is resampled every mile, with the mile marker of each sample (scaled to OSRM's road distance). A station is a candidate if it is within **10 miles** of the route. Search is vectorised with numpy on 3D unit vectors (nearest point = largest dot product):
   - bounding box of the route,
   - coarse pass against one sample every 20 miles,
   - exact nearest sample for the few hundred survivors.
   Coast to coast: ~5,000 stations -> ~460 candidates in about 20 ms. A test compares it to a brute-force haversine search.
4. **Optimizer** (`fuelroute/services/optimizer.py`, pure Python). The classic "gas station problem" greedy, which is optimal for a fixed route:
   - if a **cheaper** station is within range, buy just enough fuel to reach the nearest one;
   - else, if the destination is within range, buy just enough to finish;
   - else, **fill the tank** and go to the cheapest station within range.
   A test checks it against an exact dynamic-programming solution on 40 random routes.
   Then a **consolidation** step merges stops that buy less than 10 gallons into the previous or next stop when the tank allows it. The pure optimum sometimes says "stop to buy 1.2 gallons because the next station is 0.6 cents cheaper". On New York -> Los Angeles this takes the plan from 17 to 12 stops for $0.90 more (0.1%).
5. **Response.** The route geometry is simplified (Ramer-Douglas-Peucker, ~50 m tolerance: 35k -> 2.6k points) and returned as a GeoJSON FeatureCollection together with the stops.

### The station data (`load_stations`)

The price file has no coordinates, so they are added **once, offline**, when the data is loaded:

```
$ python manage.py load_stations
Rows read:            8,151
Invalid rows skipped: 0
Unique stations (ID): 6,738
Non-US skipped:       112
Geocoded:             6,611 / 6,626 (99.8%) {'census': 6275, 'geonames': 336}
Not geocoded:         15
  - Willow Beach, AZ (3)
  - Willington, CT (2)
  - Dundee, IL (2)
  ...
```

- **Duplicates.** 678 OPIS IDs appear more than once (same address, different rack prices). They are merged into one station with the **lowest** retail price.
- **Non-US rows.** 112 stations are in Canada (ON, AB, BC...). They are skipped because the API only routes inside the USA.
- **Coordinates.** Matched by (city, state) against `data/us_places.csv.gz` (2.8 MB, committed):
  1. **US Census Gazetteer** "Places" 2025 (public domain): every incorporated place and CDP, with an internal point.
  2. **GeoNames** US populated places (CC BY 4.0): fills the gaps (small unincorporated communities, where many truck stops are).
  Names are normalised before matching: case, accents, punctuation, "St." -> Saint, "Ste" -> Sainte, "Ft" -> Fort, "Mt" -> Mount, leading "S"/"N" -> South/North, and a second try without spaces ("Mc Lean" = "McLean", "La Place" = "LaPlace").
  The 15 unmatched stations are New England towns that are not Census places, and similar cases. They stay in the database without coordinates and are ignored by the planner.
- `scripts/build_places_dataset.py` rebuilds the places file from the original sources (downloads ~72 MB once).

Why not geocode each address with an API? 6.6k calls to a free geocoder take hours with fair-use limits, and the "addresses" are interstate exits ("I-44, EXIT 283 & US-69") that geocoders do not resolve well. A city centroid is a few miles off at most. The 10-mile corridor absorbs that.

---

## Assumptions

- **Vehicle:** 500-mile range, 10 MPG, so a 50-gallon tank. All values are in `settings.FUEL_PLANNER` and can be changed with environment variables.
- **Start fuel (`start_tank`):**
  - `empty` (default): the truck leaves with only a **50-mile reserve** (enough to reach a station) and must **arrive with the same reserve**. So the plan pays for **every mile of the trip**: total gallons = distance / 10. I picked this as the default because "total money spent on fuel" should be the cost of the trip, not the trip minus a free first tank.
  - `full`: the truck leaves with a full tank and arrives empty. Only the fuel bought on the way is counted. A trip under 500 miles has no stops and costs $0.
- **Price:** the station's retail price from the file. The lowest one when a station has several.
- **"Optimal"** means the lowest fuel cost, with no stop under 10 gallons when that can be avoided. Time and detour are not priced. A candidate station is at most 10 miles from the route, and its detour is reported as `distance_from_route_miles`.
- **Range check:** distances between stops use the mile markers on the route. The small detour to a station is not deducted from the range. The 500-mile range has no safety margin.

---

## External API calls

| Request                                   | Calls | Service |
|-------------------------------------------|-------|---------|
| New trip, "City, ST" or "lat,lon" inputs  | **1** | OSRM `route` |
| Same trip again (any `start_tank`)        | **0** | cache |
| Map page for a trip already planned       | **0** | cache |
| Free-text input like a street address     | +1 per new text | Nominatim (cached 24 h) |

`meta.external_api_calls` in every response reports the real number for that request.

---

## Measured performance

Local machine (Windows 11, Python 3.14, `runserver`), real OSRM public server, 2026-10-06:

| Request                                 | Miles  | Stops | Total cost | Gallons | External calls | Time (`X-Response-Time-ms`) |
|-----------------------------------------|-------:|------:|-----------:|--------:|---------------:|----------------------------:|
| New York, NY -> Los Angeles, CA         | 2810.4 | 12    | $854.28    | 281.04  | 1              | 1,501 ms (OSRM: 1,353 ms) |
| same, again (cached)                    |        |       |            |         | 0              | **4.9 ms** |
| same, `start_tank=full` (route cached)  | 2810.4 | 10    | $699.48    | 231.04  | 0              | 70 ms |
| Chicago, IL -> Houston, TX              | 1083.1 | 6     | $316.76    | 108.31  | 1              | 1,150 ms (OSRM: 1,128 ms) |
| same, again (cached)                    |        |       |            |         | 0              | **1.9 ms** |
| Dallas, TX -> Austin, TX, `start_tank=full` | 196.0 | 0  | $0.00      | 0       | 1              | 762 ms (OSRM: 753 ms) |
| Dallas, TX -> Austin, TX, `empty`       | 196.0  | 1     | $54.00     | 19.60   | 0              | 5.3 ms |
| Paris coordinates -> Austin (invalid)   |        |       | 400        |         | 0              | 0.6 ms |

Our own work (geocoding + corridor + optimizer + response) is about 150 ms for a coast-to-coast trip and under 30 ms for a regional one. The rest is the routing API. On start-up the server pre-loads the places table, the station arrays and numpy (`fuelroute/warmup.py`, ~1.5 s), so the first request does not pay for that.

---

## Project layout

```
config/                      settings (FUEL_PLANNER block), urls, wsgi (+ warm-up)
fuelroute/
  models.py                  FuelStation (indexed on lat/lon and state/city)
  serializers.py             input validation (USA check, formats, start != finish)
  views.py                   /api/route (GET/POST) and /api/route/map (HTML)
  middleware.py              X-Response-Time-ms header + one log line per request
  services/
    planner.py               orchestrates one request, caching, response
    geocoding.py             lat,lon / offline "City, ST" / Nominatim fallback
    places.py                offline place index (Census + GeoNames)
    osrm.py                  the single routing call + polyline6 decoder
    geo.py                   numpy geometry: haversine, resample, nearest point, RDP
    stations.py              stations near the route (vectorised)
    optimizer.py             cheapest refuelling plan (greedy + consolidation)
    station_loader.py        CSV parsing, dedup, offline geocoding
  management/commands/load_stations.py
  templates/fuelroute/map.html  Leaflet map
  tests/                     pytest-django
scripts/build_places_dataset.py
data/                        fuel prices CSV + us_places.csv.gz
postman/collection.json
```

## Configuration

Environment variables (defaults in brackets): `MAX_RANGE_MILES` [500], `MILES_PER_GALLON` [10], `START_RESERVE_MILES` [50], `MIN_STOP_GALLONS` [10], `CORRIDOR_MILES` [10], `RESAMPLE_MILES` [1], `ROUTE_CACHE_SECONDS` [3600], `OSRM_URL` [public demo server], `NOMINATIM_URL`, `HTTP_TIMEOUT_SECONDS` [20], `LOG_LEVEL` [INFO], `DJANGO_DEBUG`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`.

## Tests

`pytest` runs 125 tests in about 2 s, with no network access:

- **Optimizer:** short trip with no stop, long trip with several stops, buying just enough to reach a cheaper station, filling up at the cheapest station, unreachable gaps, "empty" start pays every mile, consolidation. It is also compared with an exact DP on 40 random routes, and 40 random plans are driven mile by mile to check the tank never goes below 0 or above 50 gallons.
- **Data loading:** CSV parsing, duplicate IDs keep the lowest price, invalid rows, Canadian rows skipped, name normalisation, idempotent reload.
- **Geocoding:** coordinates, "City, ST" variants offline, Nominatim fallback called once and cached, outside-USA rejection.
- **Geometry:** polyline decoding, haversine, resampling, simplification. The corridor search is compared with brute force on 4,000 random stations.
- **API** (OSRM mocked): exactly **1** external call for a new trip, **0** for the repeat and for the map page, GET and POST, `start_tank=full`, short trip with no stop, 400 validation cases, 502 when OSRM is down, 422 for no route / no reachable station, `X-Response-Time-ms` header.

## Limitations

- Station coordinates are **city-level**, not the exact exit. A station can be placed a few miles from its real spot, so `distance_from_route_miles` is approximate, and a station in a big city can look closer to a route than it is.
- The public OSRM server is a demo service: no SLA, fair-use rate limits, and it can be slow at times. For production, run your own OSRM (or use a paid provider) by changing `OSRM_URL`.
- The cache is in-process (`LocMemCache`). With several workers, use Redis so they share it.
- The USA check uses bounding boxes (contiguous US, Alaska, Hawaii). A point just across the border in Canada or Mexico can pass validation. OSRM will still route it.
- Prices are a static snapshot from the file. Restart the server after `load_stations` so it picks up new data.
- The optimizer does not price time or detours. A stop slightly off the route that saves a few cents still wins.

## Data licences

- Fuel prices: provided with the assessment.
- US Census Bureau Gazetteer Files: public domain.
- GeoNames (https://www.geonames.org): Creative Commons Attribution 4.0.
- Map tiles: © OpenStreetMap contributors. Routing: OSRM (http://project-osrm.org) on OpenStreetMap data.
