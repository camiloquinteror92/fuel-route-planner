# Loom script (5 minutes)

Before recording:

1. Start the server: `python manage.py runserver`.
2. Run request 1 (New York -> Los Angeles) once **before** recording if you want a fast demo, or record the first call to show the real ~1.5 s. Restarting the server clears the cache.
3. Open Postman with the collection `postman/collection.json` imported.
4. Open VS Code with the project. Have these files in tabs, in this order:
   `fuelroute/services/planner.py`, `fuelroute/services/optimizer.py`, `fuelroute/services/stations.py`, `fuelroute/services/station_loader.py`, `fuelroute/tests/test_api.py`.
5. Have a terminal ready in the project folder.

---

## 0:00 - 0:20 Introduction

> Hi, I'm Camilo. This is my solution for the fuel route assessment.
> It is a Django 6.1 and Django REST Framework API.
> You give it a start and a finish in the USA.
> It returns the route, the cheapest places to buy fuel, and the total fuel cost.
> I will show it working in Postman, and then walk through the code.

## 0:20 - 1:50 Demo in Postman

**Request 1: New York to Los Angeles.**

> This is a GET to /api/route with start "New York, NY" and finish "Los Angeles, CA".

Click Send. Scroll slowly.

> The route is about 2,800 miles.
> Here is the summary: 12 fuel stops, 281 gallons, and the total cost is about 854 dollars.
> 281 gallons is exactly the distance divided by 10 miles per gallon. Every mile is paid for.
> Each stop has the station name, address, price, the mile marker on the route, how many gallons to buy, and the cost.
> Here is the map: a GeoJSON FeatureCollection with the route line and the stops.

Scroll to `meta`.

> In meta you can see this request made one external API call, to OSRM, for the route.

Click Send again.

> Now I send the same request again. It comes from the cache. Zero external calls, and look at the header X-Response-Time-ms: about 5 milliseconds.

**Map.** Copy `map_url` from the response and open it in the browser.

> This is the map URL from the response. It is a Leaflet page with OpenStreetMap tiles.
> The numbered points are the fuel stops. If I click one, I see the price, gallons and cost.
> This page also uses the cache, so it does not call the routing API again.

Go back to Postman.

**Request 2: Chicago to Houston (POST).**

> The same endpoint also accepts POST with JSON. Chicago to Houston: about 1,080 miles, 6 stops, around 317 dollars.

**Request 3: Dallas to Austin with start_tank=full.**

> I added a start_tank option. By default the truck leaves almost empty, so the total is the real cost of the trip.
> With start_tank equal to full, the truck leaves with 500 miles of fuel. This trip is 196 miles, so there are no stops.

**Request 4: invalid.**

> If a point is outside the USA, here Paris, the API returns 400 with a clear message, and it does not call any external service.

## 1:50 - 4:30 Code overview

**planner.py** (open it)

> This is the flow of one request.
> First, geocoding. "City, ST" is resolved offline from a table of US places. So it costs zero API calls.
> Second, one call to OSRM for the route. I ask for polyline6 because it is much smaller than GeoJSON, so the call is faster. The route and the final plan are cached.
> Third, I find the stations near the route.
> Fourth, the optimizer decides where to stop and how much to buy.
> At the end I build the JSON and the GeoJSON for the map.

**station_loader.py** (open it)

> The price file has no coordinates, and some stations appear several times.
> The load_stations command runs once. It merges duplicates by OPIS ID, and keeps the lowest price.
> Then it gives each station the coordinates of its city, from the US Census Gazetteer and GeoNames. Both are free and offline.
> 99.8 percent of the US stations get coordinates. There are no geocoding API calls at request time.

**stations.py** (open it)

> To find stations near the route, I resample the route every mile.
> I use numpy and 3D unit vectors: the nearest point is the largest dot product.
> First a bounding box, then a coarse pass, then the exact nearest point.
> Coast to coast it checks about 5,000 stations in about 20 milliseconds, and keeps the ones within 10 miles of the route.

**optimizer.py** (open it, show `_greedy`)

> This is the classic gas station problem.
> At each station: if there is a cheaper station within range, I buy just enough fuel to reach it.
> If not, and the destination is within range, I buy just enough to finish.
> If not, I fill the tank and go to the cheapest station in range.
> This greedy is optimal. I have a test that compares it with an exact dynamic programming solution on random routes.

Scroll to `_consolidate`.

> The pure optimum sometimes stops to buy one gallon to save a fraction of a cent.
> So I merge stops under 10 gallons into the previous or next stop, when the tank allows it.
> On New York to Los Angeles this goes from 17 stops to 12, for 90 cents more.

**tests** (open `test_api.py`, then run `pytest` in the terminal)

> The tests mock OSRM and check that a new trip makes exactly one external call, and the repeated trip and the map make zero.
> They also cover validation, the 502 when OSRM is down, and the 422 when there is no reachable station.

Run `pytest`.

> 125 tests, about 2 seconds.

## 4:30 - 5:00 Wrap-up

> To summarize: one routing call per new trip, zero for repeated trips.
> About one and a half seconds for a new coast-to-coast trip, and that is mostly the routing API. A few milliseconds from the cache.
> The README explains the assumptions, the measured times and the limitations. The main limitation is that station locations are at city level.
> Thank you for watching.

---

## If something goes wrong while recording

- **OSRM is slow or returns 502:** it is a free demo server. Wait a few seconds and send again.
- **The numbers are a bit different:** that is fine. Say "about".
- **Port already in use:** `python manage.py runserver 8001` and change `baseUrl` in Postman.
