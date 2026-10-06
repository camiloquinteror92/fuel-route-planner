# Loom script (under 5 minutes)

About 500 spoken words: at a calm 140 words per minute that is ~3:35, which leaves
~80 s for clicks, page loads and the edge-case run. Rehearse once with a stopwatch.

The script has no fixed numbers on purpose: read them from the screen ("about ...").
Every number the page shows comes from the API answer, `/api/stats`, `/api/about` or
the browser's own measurements, so they will differ a little between takes.

Before recording:

1. Push first (`git push`), so the page's code and test links open the right lines on
   GitHub and the footer no longer says "not pushed yet". Set `LOOM_URL` after
   uploading if you record a second take.
2. `python manage.py runserver`, then `pytest` once in another terminal (the
   Requirements tab shows its results). **Do not edit code while the server runs**:
   the auto-reloader restarts the process and clears the in-process cache and counters.
3. Postman with `postman/collection.json` imported. Do **not** send request 1 before
   recording: the first send should show the one routing call.
4. Browser tab ready (empty), DevTools closed, window at least 1280 px wide.
5. VS Code with `fuelroute/services/optimizer.py` at `_greedy`.

---

## 0:00 - 0:15 Introduction

> Hi, I'm Camilo. This is my solution for the fuel route assessment: a Django 6.1 and
> Django REST Framework API. You give it a start and a finish in the USA; it returns the
> route, the cheapest places to buy fuel and the total cost. I'll show the API in Postman,
> then the page I built on top of the same API, then a little code.

## 0:15 - 1:05 Postman: the API

**Request 1: New York to Los Angeles.** Send. Scroll to `summary`, `fuel_stops`, `meta`.

> Each stop has its price, gallons and cost, and the total. The gallons equal the distance
> divided by ten miles per gallon: every mile is paid for. In meta: one external call, to OSRM.

Open the **Headers** tab: point at `Server-Timing` and `Content-Encoding: gzip`.

> Server-Timing breaks the time down: almost all of it is the OSRM call.

**Send again.**

> Zero external calls, a few milliseconds: the plan cache.

**Request 11: Toronto, ON.** Send.

> A Canadian city is rejected as outside the USA before any call, not even a geocoding one.

Copy `map.map_url` from request 1.

## 1:05 - 2:10 The page: the result

Paste `map_url` in the browser.

> The page is a client of the same public API, so this is a cache hit: zero calls.

Point at the KPIs.

> Total cost, and what this plan saves against a driver who ignores prices. It's honest:
> the cheapest plan makes more stops than that driver, and it says how many.

Map: point at the coloured dots, click stop 1.

> These are the stations within ten miles of the route, coloured by price. Stop 1 explains
> itself: the greedy bought a couple of gallons to reach a cheaper station, but a stop that small is
> silly, so consolidation merged that station's purchase here, for a few cents, and the
> fuel now reaches stop 2.

Fuel profile: move the mouse over a stop, then over the tank line.

> Prices along the route, and the tank recomputed in the browser from the stops: never
> below empty or above full. The range is used literally, and the page says so.

## 2:10 - 2:45 Performance

Click **Performance**, then **Send again (plan cache)**.

> Each request is measured: server time, external calls, round trip, and gzip: about a
> third of the JSON travels. This card: computed once with one OSRM call, now served from
> the cache many times faster. Below, the server's own counters: OSRM calls per routing
> request.

## 2:45 - 3:35 Requirements

Click **Requirements**, then **Run all** in "Edge cases" (it keeps running while you talk).

> Each requirement of the brief, the explicit ones and the ones between the lines, with live
> evidence, links to the code on GitHub and its tests in the last pytest run. These contract
> checks are recomputed in the browser in integer cents. And these are real requests running
> now: Toronto, Alaska, a typo in a state, a gap longer than the range, broken JSON. The
> rejected ones cost zero external calls.

Wait for the summary line ("... edge cases behave as expected").

## 3:35 - 4:15 How it works

Click **How it works**. Scroll through the pipeline, then to "Known limits" and "How it was built".

> The pipeline of this exact answer: offline geocoding, one routing call, the corridor
> funnel in numpy, the tank rules, the optimizer. The known limits are stated with live
> numbers, like stops reached with an empty tank. And how it was built, with the commit
> history read from git.

## 4:15 - 4:45 Code

**optimizer.py** (`_greedy`)

> The classic gas station problem: if a cheaper station is within range, buy just enough to
> reach it; else, if the destination is in range, buy just enough to finish; otherwise fill
> up. A test checks it against an exact dynamic program on random routes.

## 4:45 - 4:55 Wrap-up

> One routing call per new trip and zero for repeats, every number explained on the page, and
> the README covers the assumptions and the data gaps. Thanks for watching.

---

## If something goes wrong while recording

- **OSRM is slow or returns 502/503:** it is a free demo server. The API already retried once;
  the page shows a Retry button. Wait a few seconds and send again.
- **"Run all" stops with a rate-limit countdown:** the server allows a fixed number of requests
  per minute per client to `/api/route`; wait for the countdown and press Run all again.
- **The numbers are a bit different from a previous take:** say "about"; they are live.
- **Code links say "not on GitHub yet":** the latest commits are not pushed; push and reload.
- **Port already in use:** `python manage.py runserver 8010` and change `baseUrl` in Postman.
