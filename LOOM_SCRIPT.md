# Loom script (under 5 minutes)

About 560 spoken words: at a calm 145 words per minute that is ~3:50, which leaves
~70 s for clicks, page loads, Play trip and the test run. Rehearse once with a stopwatch.

The script has no fixed numbers on purpose: read them from the screen ("about ...").
Every number the page shows comes from the API answer, `/api/stats`, `/api/about` or
the browser's own measurements, so they will differ a little between takes.

Before recording:

1. Push first (`git push`), so the page's code and test links open the right lines on
   GitHub and the footer no longer says "not pushed yet". Set `LOOM_URL` after
   uploading if you record a second take.
2. `python manage.py runserver` (if port 8000 is taken, for example by another project's
   Docker backend: `python manage.py runserver 8001` and change `baseUrl` in Postman),
   then `pytest` once in another terminal (the Requirements tab shows its results).
   **Do not edit code while the server runs**: the auto-reloader restarts the process
   and clears the in-process caches and counters.
3. Warm the routes the video uses, so the public routing server cannot slow it down: open
   the page, Requirements tab, **Run all** in "Edge cases" once (up to six routing
   calls), then reload. Routes stay cached for a day, plans for an hour.
4. Postman with `postman/collection.json` imported. Do **not** send request 1 before
   recording: the first send should show the one routing call (if it was warmed in step 3,
   say "zero calls: this server already routed it today").
5. Browser tab ready (empty), DevTools closed (the edge cases' intentional 4xx answers
   show in red there), window at least 1280 px wide.
6. VS Code with `fuelroute/services/optimizer.py` at `_greedy`.

---

## 0:00 - 0:15 Introduction

> Hi, I'm Camilo. This is my solution for the fuel route assessment: a Django 6.1 and
> Django REST Framework API. You give it a start and a finish in the USA; it returns the
> route, the cheapest places to buy fuel and the total cost. I'll show the API in Postman,
> then the page I built on top of the same API, then a little code.

## 0:15 - 0:55 Postman: the API

**Request 1: New York to Los Angeles.** Send. Scroll to `summary`, `fuel_stops`, `meta`.

> Each stop has its price, gallons and cost, and the total. Every mile is paid for, and in
> meta: one external call, to OSRM. Server-Timing shows almost all the time is that call.

**Send again.**

> Zero external calls, a few milliseconds: the plan cache.

**Request 11: Toronto, ON.** Send.

> A Canadian city is rejected before any call, not even a geocoding one.

Copy `map.map_url` from request 1.

## 0:55 - 1:45 The page: the result and Play trip

Paste `map_url` in the browser.

> The page is a client of the same public API, so this is a cache hit: zero calls.

Point at the KPIs, then click stop 1 on the map.

> Total cost, and what this plan saves against a driver who ignores prices; it is honest
> about the extra stops. Each stop explains itself: why the greedy stopped here, and what the
> consolidation of tiny stops moved here.

Under the map: **4×**, then **Play trip** (let it run while you talk).

> Play trip replays the plan: the tank goes down, refills at each stop, and the money adds
> up in whole cents until it lands exactly on the API's total.

## 1:45 - 2:35 What if…

Scroll to **Interview questions**. Click the **8 mpg** question, then the **25 mi** one.

> These are the questions an interviewer asks. Eight miles per gallon with the same tank:
> the cost jumps, and the badge says zero external calls, because only the plan changed,
> not the route. Stations up to twenty-five miles away: it looks a bit cheaper, but the
> answer counts the longer detours, and then it costs more. That is why ten miles is the
> default.

Point at the effect box at the top of **What if…**.

> Every setting is in the address, so this link reopens the same plan.

## 2:35 - 3:05 Requirements

Click **Requirements**.

> Each requirement of the brief, the explicit ones and the ones between the lines, with live
> evidence, links to the code on GitHub and its tests. These contract checks are recomputed
> in the browser in integer cents. And the edge cases are real requests: Toronto, Alaska, a
> gap longer than the range, broken JSON.

Click **Run** on one edge case (for example Anchorage, Alaska).

## 3:05 - 3:40 Tests

Click **Tests**, then **Run all tests** (it runs while you talk).

> The whole suite runs from the page, with the routing service faked, so no network. Every
> test says in one sentence what it proves; the optimizer is checked against an exact
> dynamic program on random routes. It only runs on my own machine: the server refuses
> anyone else.

Wait for the green summary ("... of ... tests").

## 3:40 - 4:10 How it scales

Click **How it scales**.

> Measured, not guessed: a repeated trip and a what-if cost milliseconds; a new trip waits
> for the public routing server, many times longer than all our own code. So the next steps
> are Redis to share the caches, then our own OSRM, each with the number that triggers it.

## 4:10 - 4:40 Code

**optimizer.py** (`_greedy`)

> The classic gas station problem: if a cheaper station is within range, buy just enough to
> reach it; else, if the destination is in range, buy just enough to finish; otherwise fill
> up. Every mile is driven on the cheapest fuel that could supply it.

## 4:40 - 4:55 Wrap-up

> One routing call per new trip, zero for repeats and what-ifs, every number explained on the
> page, and the README covers the assumptions and the data gaps. Thanks for watching.

---

## If something goes wrong while recording

- **OSRM is slow or returns 502/503:** it is a free demo server. The API already retried once;
  the page shows a Retry button. Wait a few seconds and send again.
- **"Run all" stops with a rate-limit countdown:** the server allows a fixed number of requests
  per minute per client to `/api/route`; wait for the countdown and press Run all again.
- **Play trip does not move:** the browser pauses animations in a hidden tab; keep the tab in
  front. With "reduce motion" on it jumps straight to the end.
- **The Tests tab says the runner is not available:** the page must be opened on the same
  machine (`127.0.0.1` or `localhost`) and the server started with `manage.py runserver`.
- **The numbers are a bit different from a previous take:** say "about"; they are live.
- **Code links say "not on GitHub yet":** the latest commits are not pushed; push and reload.
- **Port already in use:** `python manage.py runserver 8001` and change `baseUrl` in Postman.
