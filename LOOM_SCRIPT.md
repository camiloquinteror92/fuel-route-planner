# Loom script (under 5 minutes)

About 600 spoken words: at a calm 140 words per minute that is ~4:20, which leaves
~40 s for clicks. Rehearse once with a stopwatch.

Before recording:

1. Start the server: `python manage.py runserver`. **Do not edit code while it runs**:
   the auto-reloader restarts the process and clears the in-process cache, so a
   repeated trip would call OSRM again.
2. Optional: send request 1 once before recording, for a fast demo. Restarting the
   server clears the cache.
3. Postman with `postman/collection.json` imported.
4. VS Code with these tabs, in this order: `fuelroute/services/planner.py`,
   `fuelroute/services/optimizer.py`, `fuelroute/services/stations.py`,
   `fuelroute/services/station_loader.py`, `fuelroute/tests/test_api.py`.
5. A terminal in the project folder where `pytest` has ALREADY run (show the result,
   do not wait for it).

---

## 0:00 - 0:20 Introduction

> Hi, I'm Camilo. This is my solution for the fuel route assessment: a Django 6.1
> and Django REST Framework API. You give it a start and a finish in the USA, and it
> returns the route, the cheapest places to buy fuel, and the total fuel cost.

## 0:20 - 2:00 Demo in Postman

**Request 1: New York to Los Angeles.** Click Send, scroll slowly.

> The route is about 2,800 miles. Twelve stops, 281 gallons, about 855 dollars.
> 281 gallons is the distance divided by 10 miles per gallon: every mile is paid for.
> Each stop has the station, price, mile marker, gallons and cost, and the costs add up to the cent.
> In meta: one external call, to OSRM, for the route.

Click Send again.

> The same request again comes from the cache: zero external calls, about 5 milliseconds.

**Map.** Copy `map_url`, open it in the browser, click a stop.

> This is the map from the response: Leaflet with OpenStreetMap tiles. The numbers are the stops.

**Request 2: Chicago to Houston (POST).**

> The endpoint also accepts POST with JSON. About 1,080 miles, five stops, around 323 dollars.

**Request 3: Los Angeles to New York.** Scroll to `warnings`.

> The price file has only eight stations in California, the first one on this route is at mile 251.
> So the truck leaves with enough fuel to reach it, and must arrive with the same amount.
> The response says it in warnings, and every mile is still paid for.

**Request 4: invalid.**

> A point outside the USA, here Paris, is a 400 with a clear message and zero external calls.

## 2:00 - 4:30 Code

**planner.py**

> This is one request. First geocoding: "City, ST" is resolved offline, from Census and GeoNames data, so zero API calls.
> Then cheap checks before spending the routing call: same place, Alaska or Hawaii.
> Then one call to OSRM. I ask for polyline6, which is smaller than GeoJSON, and I cache the prepared route and the final plan.

**optimizer.py** (show `_greedy`)

> This is the classic gas station problem. At each station: if a cheaper one is within range, buy just enough to reach it.
> If not, and the destination is in range, buy just enough to finish. Otherwise fill up and go to the cheapest station in range.
> A test compares it with an exact dynamic programming solution on 90 random routes.

Scroll to `_consolidate`.

> The pure optimum sometimes stops for two gallons to save a cent. So I fix stops under 10 gallons with a neighbour,
> preferring to remove a stop, and only if it costs at most one dollar. New York to Los Angeles goes from 18 stops to 12 for about a dollar fifty.

**stations.py**

> To find stations near the route I resample it every mile and use numpy with 3D unit vectors.
> Bounding box, coarse pass, exact pass: coast to coast it checks 5,000 stations in about 20 milliseconds.

**station_loader.py**

> The price file has no coordinates and repeats some stations with different prices.
> I use the median quote, and the city's coordinates from the offline table.
> When a city name exists twice in a state, the interstate exit in the address tells which one; if it is unclear, I leave the station out instead of guessing.

**test_api.py + terminal**

> The tests fake the network: a new trip makes exactly one external call, a repeat and the map make zero,
> and they cover every edge case and error code. 265 tests in about three seconds.

## 4:30 - 4:50 Wrap-up

> One routing call per new trip, zero for repeats. About a second for a new coast-to-coast trip, mostly the routing API.
> The README explains the assumptions, the edge cases and the data gaps. Thank you for watching.

---

## If something goes wrong while recording

- **OSRM is slow or returns 502/503:** it is a free demo server. The API already retried once; wait a few seconds and send again.
- **The numbers are a bit different:** say "about".
- **Port already in use:** `python manage.py runserver 8010` and change `baseUrl` in Postman.
