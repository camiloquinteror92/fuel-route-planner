# Loom script (under 5 minutes)

About 560 spoken words: at a calm 140 words per minute that is ~4:00, which leaves
~50 s for clicks and for the edge-case run. Rehearse once with a stopwatch.

The script has no fixed numbers on purpose: read them from the screen ("about ...").
Every number the page shows comes from the API answer, `/api/stats`, `/api/about` or
the browser's own measurements, so they will differ a little between takes.

Before recording:

1. `python manage.py runserver`, then `pytest` once in another terminal (the
   Requirements tab shows its results). **Do not edit code while the server runs**:
   the auto-reloader restarts the process and clears the in-process cache and counters.
2. Postman with `postman/collection.json` imported. Do **not** send request 1 before
   recording: the first send should show the one routing call.
3. Browser tab ready (empty), DevTools closed.
4. VS Code with `fuelroute/services/optimizer.py` (at `_greedy`) and
   `fuelroute/services/planner.py` (at `plan_trip`).

---

## 0:00 - 0:15 Introduction

> Hi, I'm Camilo. This is my solution for the fuel route assessment: a Django 6.1 and
> Django REST Framework API. You give it a start and a finish in the USA; it returns the
> route, the cheapest places to buy fuel and the total cost. I'll show the API in Postman,
> then the page I built on top of the same API, then a little code.

## 0:15 - 1:15 Postman: the API

**Request 1: New York to Los Angeles.** Send. Scroll to `summary`, `fuel_stops`, `meta`.

> The answer has the route, each stop with its price, gallons and cost, and the total.
> The gallons equal the distance divided by ten miles per gallon: every mile is paid for.
> In meta: one external call, to OSRM, for the route.

Open the **Headers** tab of the response.

> Every answer has a Server-Timing header: almost all of this time is the OSRM call;
> our own code is a small slice of it.

**Send again.**

> Same request: zero external calls, a few milliseconds. It comes from the plan cache.

Copy `map.map_url`.

## 1:15 - 2:20 The page: the result

Paste `map_url` in the browser.

> The page is a client of the same public API. The server renders only the shell; the
> JavaScript calls the same endpoint Postman just called, so this is a cache hit: zero calls.

Point at the KPIs.

> Total cost, and what this plan saves against a driver who ignores prices. It's honest:
> the cheapest plan makes more stops than that driver, and it says how many.

Map: point at the coloured dots, click a numbered stop.

> These are all the stations within ten miles of the route, coloured by price for this trip.
> Each stop says why it was chosen: here, a cheaper station was within reach, so it bought
> just enough to get there.

Fuel profile: move the mouse over it.

> Top: prices along the route. Bottom: fuel in the tank, recomputed in the browser from the
> stops. It never goes below empty or above full. The dashed line is the price-blind driver.

## 2:20 - 3:00 Performance

Click **Performance**, then **Send again (plan cache)**.

> Each request is measured: server time, external calls, browser round trip, and where the
> server time went. This card shows the plan was computed earlier with one OSRM call, and is
> now served from the cache many times faster. Below: everything this page sent, and the
> server's own counters since it started.

## 3:00 - 3:50 Requirements

Click **Requirements**, then **Run all** in "Edge cases" (it keeps running while you talk).

> Each requirement of the brief, the explicit ones and the ones between the lines, with how
> it's met, live evidence, links to the code and the result of its tests in the last pytest run.
> These contract checks are recomputed in the browser in integer cents: the costs add up, each
> cost is gallons times price, the tank stays within bounds, the call budget holds.
> And these are real requests running now: outside the USA, Alaska, the same place written
> twice, a gap longer than the range, a typo in a parameter, broken JSON. The rejected ones
> cost zero external calls.

Wait for the summary line ("... edge cases behave as expected").

## 3:50 - 4:30 Code

**optimizer.py** (`_greedy`)

> This is the classic gas station problem. At each station: if a cheaper one is within range,
> buy just enough to reach it; else, if the destination is in range, buy just enough to finish;
> otherwise fill up. A test checks it against an exact dynamic program on random routes,
> and the consolidation step removes tiny stops for at most a dollar each.

**planner.py** (`plan_trip`)

> One request: offline geocoding, cheap checks before spending the call, the plan cache,
> one OSRM call, numpy to find the stations near the route, the optimizer, and money in Decimal.

## 4:30 - 4:45 Wrap-up

> One routing call per new trip and zero for repeats, every number explained on the page, and
> the README covers the assumptions and the data gaps. Thanks for watching.

---

## If something goes wrong while recording

- **OSRM is slow or returns 502/503:** it is a free demo server. The API already retried once;
  the page shows a Retry button. Wait a few seconds and send again.
- **"Run all" stops with a rate-limit countdown:** the server allows a fixed number of requests
  per minute per client; wait for the countdown and press Run all again.
- **The numbers are a bit different from a previous take:** say "about"; they are live.
- **Port already in use:** `python manage.py runserver 8010` and change `baseUrl` in Postman.
