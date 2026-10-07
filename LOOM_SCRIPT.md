# Loom script (4:30)

Three parts, timed: **Postman 1:05, the page 1:50, the code 1:10**, plus a short intro and close. About 470 spoken
words: at a calm 140 words per minute that is 3:20, which leaves about a minute for clicks, sends and Play. Rehearse
with a stopwatch until it ends before 4:30.

Numbers change a little between days: read them from the screen and say "about".

## Before recording

1. Start the server without the auto-reloader: `python manage.py runserver --noreload` (port 8000 busy?
   `python manage.py runserver 8001 --noreload` and change `baseUrl` in Postman). Do not edit code while it runs.
2. Warm up: on the page, plan **New York → Los Angeles** once, to be sure the free routing server answers today. Then
   restart the server (Ctrl+C and the same command), so the first send in the video shows its one request.
3. Postman with `postman/collection.json` imported, request 1 open.
4. Browser window at least 1280 px wide, empty tab.
5. VS Code with five tabs, in this order: `fuelroute/views.py` (`RoutePlanView`), `fuelroute/services/planner.py`
   (`plan_trip`), `fuelroute/services/osrm.py` (`get_route`), `fuelroute/services/stations.py`
   (`stations_along_route`), `fuelroute/services/optimizer.py` (`_greedy`).

---

## 0:00 Intro (10 s)

> Hi, I'm Camilo. This Django API takes a start and a finish in the USA and returns the route, the cheapest places
> to buy fuel and the total cost.

## 0:10 Postman (1:05)

**Request 1, New York → Los Angeles.** Send. Show `fuel_stops` (one `decision`), `summary`, `meta`.

> About eighteen stops, each with its price, gallons and cost, and a decision that says which rule chose it. The
> total is about 854 dollars. Meta says one external call: the road, from OSRM, a free routing service.

**Send again.**

> Zero calls, a few milliseconds: the same trip is answered from memory.

**Request 3, a thirstier truck.** Send.

> Another truck on the same trip: eight miles per gallon, same tank. The plan changes, but the road was saved, so
> again zero calls.

**Request 7, Toronto.** Send.

> A city in Canada is turned down before any call.

Copy `map.map_url` from request 1.

## 1:15 The page (1:50)

Paste `map_url` in the browser. It opens at the **Route** step.

> The page uses the same API. About 2,800 miles, about 281 gallons burned. The truck leaves with five gallons, just
> enough to reach a truck stop, and must arrive with five too, so the bill is exactly the fuel the trip burns. Prices
> near the road go from about 2.80 to 4.30 a gallon: that is why it pays to choose where to stop.

Click **Next: where to buy fuel**.

> At every truck stop the driver looks one full tank ahead and follows three rules. One: if fuel is cheaper within
> reach, buy just enough to get there. Two: if not, and the finish is within reach, buy just enough to finish. Three:
> otherwise, fill the tank. So each gallon comes from the cheapest stop that can reach it; a test compares this with
> an exact method.

Click stop 3 (**Youngstown**, Cheaper ahead), then stop 10 (**Waco**, Fill up).

> Youngstown: fuel is cheaper in Toledo, so it buys just enough to get there. Waco, Nebraska: the cheapest price on
> the road, so it fills the tank.

Press **Play the trip** under the map (about 12 seconds; talk over it).

> Play shows the tank emptying and refilling, and the money adding up to the exact total.

Click **Next: the bill**.

> About 854 dollars, and the stops add up to it to the cent. A driver who ignores prices buys the same gallons and
> pays about 87 dollars more. The trade-off is more, smaller stops; the Truck step can merge them for about a dollar
> and a half.

## 3:05 The code (1:10)

VS Code, one tab after the other, following one request.

`views.py`, `RoutePlanView`:

> The view validates the input with a serializer and calls one function, plan_trip.

`planner.py`, `plan_trip`:

> Places are found in an offline index, and cheap checks run before any network call. A finished plan is cached by
> its coordinates and truck settings.

`osrm.py`, `get_route`:

> The only external call. The road is cached by coordinates alone, with a lock per key, so a repeat or another truck
> never calls OSRM again.

`stations.py`, `stations_along_route`:

> The stations within ten miles of the road, with numpy: a bounding box, a coarse pass, then an exact one. About
> thirty milliseconds coast to coast.

`optimizer.py`, `_greedy`:

> And the algorithm: these three branches are the three rules of the page. Money is added in Decimal, so the stops
> always add up to the total.

## 4:15 Close (10 s)

> One routing call per new trip, none for a repeat or another truck. The README has the assumptions, the data gaps
> and how it would scale. Thanks for watching.

---

## If something goes wrong

- **OSRM is slow or answers 502/503:** it is a free demo server. The API already retried once; wait a few seconds and
  send again (the page has a Retry button).
- **The first send shows 0 calls:** the server already planned that trip (no restart after the warm-up). Say "zero,
  because this server planned it already; a new trip makes one".
- **Play does not move:** keep the tab in front. With "reduce motion" on, it jumps to the end.
- **Port already in use:** `python manage.py runserver 8001 --noreload` and change `baseUrl`.
