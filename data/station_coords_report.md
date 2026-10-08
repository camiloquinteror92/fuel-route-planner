# Station coordinates from OpenStreetMap

Built by `scripts/build_station_coords.py` (offline, once) into `data/station_coords.csv`:
a station with a row gets those coordinates, one without keeps its city center.

- Built: 2026-10-08 15:54 UTC. OSM data as of (states): 2026-10-08 (48) (the date of the Overpass instance that answered)
- US stations in the price file: **6,626** in 48 states (6,544 with a city center, 82 with an ambiguous city name)
- States that could not be downloaded: none

## Result

| source | confidence | stations |
|---|---|---:|
| osm_exit | high | 1,581 |
| osm_exit | medium | 217 |
| osm_fuel | high | 1,664 |
| osm_fuel | medium | 140 |
| **placed** | | **3,602** (54.4%) |
| kept at city center (no row) | | 2,955 |
| ambiguous city, still without coordinates | | 69 |

Ambiguous-city stations placed by a unique exit: 13 of 82.

OSM fuel stations matched by more than one OPIS ID: 14, each time OPIS IDs of the same exit (one truck stop listed twice in the price file).

## How far stations moved from their city center

Stations that had a city center: 3,589. Median **2.5 mi**, p75 4.3, p90 6.9, p99 14.4, max 22.9.

| miles moved | stations |
|---|---:|
| 0-1 | 717 |
| 1-2 | 806 |
| 2-5 | 1,362 |
| 5-10 | 576 |
| 10-15 | 94 |
| 15-20 | 32 |
| 20-30 | 2 |

## The 20 largest moves

| opis_id | station | city | source | conf. | miles | matched |
|---|---|---|---|---|---:|---|
| 67296 | LOVES TRAVEL STOP #340 | Las Vegas, NV | osm_fuel | high | 22.9 | Love's Truck Lanes (brand Love's) |
| 3585 | CHEVRON | Lovelock, NV | osm_fuel | high | 22.4 | Chevron |
| 6058 | MOTOR INN AUTO TRUCK STOP | Mendon, OH | osm_exit | medium | 19.9 | I-75 exit 110 |
| 71124 | LOVES TRAVEL STOP #761 | Jacksonville, FL | osm_fuel | high | 19.6 | Love's |
| 63736 | PLATEAU TRUCK AND AUTO CENTER | Van Horn, TX | osm_exit | medium | 19.5 | I-10 exit 159 |
| 71250 | NAVAJO BLUE TRAVEL CENTER | Flagstaff, AZ | osm_exit | medium | 19.5 | I-40/US-180 exit 219 |
| 72493 | Chiricahua Apache Plaza | Deming, NM | osm_exit | medium | 19.3 | I-10 exit 102 (Akela) |
| 4700 | A C TRUCKSTOP | Laramie, WY | osm_exit | medium | 19.3 | I-80 exit 290 |
| 159 | PILOT TRAVEL CENTER #87 | Jacksonville, FL | osm_exit | medium | 18.7 | I-10 exit 343 (old exit 50) |
| 7534 | TA BALDWIN TRAVEL CENTER | Jacksonville, FL | osm_exit | medium | 18.7 | I-10 exit 343 (old exit 50) |
| 70419 | FUEL AMERICA TRAVEL CENTER | Laredo, TX | osm_exit | medium | 18.7 | I-35 exit 24 |
| 69475 | GUTHRIE TRAVEL CENTER | Guthrie, OK | osm_exit | medium | 18.7 | I-35/SR-66 exit 138C |
| 3464 | BOISE PETROLEUM LLC | Boise, ID | osm_exit | medium | 18.6 | I-84/US-20/US-26/US-30 exit 71 |
| 52656 | TRAVELING TIGER CENTER | Sierra Blanca, TX | osm_exit | medium | 18.5 | I-10 exit 87 |
| 64408 | QUIKTRIP #4058 | San Antonio, TX | osm_fuel | high | 18.2 | QuikTrip |
| 68843 | PILOT #467 | San Antonio, TX | osm_fuel | high | 18.0 | Pilot |
| 66237 | ROUTE 66 TRAVEL CENTER | Albuquerque, NM | osm_fuel | high | 17.9 | Route 66 Travel Center |
| 70284 | ROBINSON CORINTH IRVING | Corinth, ME | osm_exit | medium | 17.8 | I-395/SR-15/SR-9 exit 3 |
| 70964 | SEALSTON DELI | King George, VA | osm_exit | medium | 17.5 | I-95/US-17 exit 130A |
| 6327 | PETRO STOPPING CENTER #312 | Knoxville, TN | osm_fuel | high | 17.3 | Petro |

## Why the others kept their city center

| reason | stations |
|---|---:|
| no exit in address; no OSM site with the store number and the name | 1,808 |
| no exit in address; no store number to confirm a name match | 876 |
| <road> exit <n> not found in OSM within <n> mi; no OSM site with the store number and the name | 132 |
| <road> exit <n> not found in OSM within <n> mi; no store number to confirm a name match | 74 |
| city ambiguous and no exit in address | 65 |
| <road> exit <n> ambiguous: matching exits <n> mi apart; no OSM site with the store number and the name | 19 |
| <road> exit <n> <n> mi from the city, the name is in town and not at the exit; no OSM site with the store number and the name | 15 |
| <road> exit <n> ambiguous: matching exits <n> mi apart; no store number to confirm a name match | 13 |
| exit without a highway in address; no OSM site with the store number and the name | 9 |
| <road> exit <n> <n> mi from the city, the name is in town and not at the exit; no store number to confirm a name match | 3 |
| <road> exit <n> <n> mi from the city and no fuel station with the name next to it; no OSM site with the store number and the name | 3 |
| city ambiguous and <road> exit <n> not found in OSM within <n> mi | 3 |
| exit without a highway in address; no store number to confirm a name match | 2 |
| <road> exit <n> found <n> mi away, but MA renumbered its exits; no store number to confirm a name match | 1 |
| city ambiguous and <road> exit <n> <n> mi from the city and no fuel station with the name next to it | 1 |

## Rules

- Search area: 25 mi around the city center `load_stations` computes.
- `osm_fuel`: OSM `amenity=fuel` whose name / brand / operator matches the OPIS name (brands
  normalised, store numbers removed), within 1.5 mi of the address' exit when that exit
  is found. A site with the OPIS store number in OSM wins; a site with only other store numbers
  is another store and is not taken (high; medium if several sites match or one was set aside).
  Without a usable exit, only the same store number in OSM places a station (high): a name
  alone is not used, because the price file lists several stores of a chain in one town.
- `osm_exit`: the `motorway_junction` with the address' exit number on a way with the address'
  highway (closest to the city center, or the midpoint of the nodes when they are more than
  1.5 mi apart; skipped if the nodes with that number, current `ref` or `old_ref` from
  before a renumbering, are > 5 mi apart). High when number and letter match the current
  ref, all its nodes are within 1.5 mi and it is within 10 mi of the city;
  medium otherwise. Not used beyond 20 mi, nor beyond 5 mi when the chain's
  only store within 3 mi of the city center is in OSM and none is at the exit.
  A state route is read as the US route with the same number ("SR-395" = US 395) only when no
  state route with that number has an exit in the area (medium).
- MA and RI renumbered every exit in 2021 and OSM rarely keeps the old
  number: there an exit matched on its current number must be within 10 mi of the city.
- One OSM fuel station is one store: OPIS stations of different exits never share one. The one
  whose store number OSM carries keeps it; the others are matched again without it.
- Fuel features closer than 0.35 mi count as one site (car and truck canopies).

## Re-run

```
python scripts/build_station_coords.py            # downloads missing states into .cache/osm/
python scripts/build_station_coords.py --offline  # re-match from the cache only
python scripts/build_station_coords.py --refresh  # download every state again
```

Two Overpass requests per state (fuel; exits), sequential, with a pause between states and
back-off on 429/5xx. Then `python manage.py load_stations` to load the result.

## Columns of `data/station_coords.csv`

`opis_id` (the price file's station), `lat` / `lon` (the position), `source` (`osm_fuel`:
its OSM fuel station; `osm_exit`: the exit of its address), `confidence` (`high` / `medium`),
`osm_type_id` (the OSM feature, or the exit nodes joined by `+` for a midpoint),
`matched_label` (that feature's OSM name or exit), `miles_from_city_center` and `reason` (the
rule that placed it). `load_stations` reads `opis_id`, `lat`, `lon` and `source`.

## Licence

Derived from OpenStreetMap data, © OpenStreetMap contributors, available under the
Open Database License (ODbL 1.0, https://opendatacommons.org/licenses/odbl/).
`data/station_coords.csv` is made available under the same licence.
