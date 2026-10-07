# Loom script (under 5 minutes)

About 525 spoken words: at a calm 140 words per minute that is under 4 minutes, which leaves
time for clicks and Play trip. Rehearse once with a stopwatch.

Numbers change a little between takes: read them from the screen and say "about".

## Before recording

1. Start the server without the auto-reloader: `python manage.py runserver --noreload`
   (port 8000 busy? `python manage.py runserver 8001 --noreload` and change `baseUrl` in
   Postman). Do not edit code while it runs.
2. Warm up: on the page, plan **New York → Los Angeles** and **Chicago → Houston** once, to be
   sure the free routing server answers today. Then restart the server (Ctrl+C and the same
   command), so the first send in the video shows its one request.
3. Postman with `postman/collection.json` imported, request 1 open.
4. Browser window at least 1280 px wide, empty tab. VS Code with
   `fuelroute/services/optimizer.py` open at `_greedy`.

---

## 0:00 Intro

> Hi, I'm Camilo. This is my solution to the fuel route assignment: a Django 6.1 API. You give
> it a start and a finish in the USA, and it returns the route, the cheapest places to buy fuel
> and the total cost. First the API in Postman, then a page that walks through one trip, then
> the code.

## 0:15 Postman

**Request 1, New York → Los Angeles.** Send. Show `fuel_stops` (one `decision`), `summary`, `meta`.

> About twelve stops, each with its price, gallons and cost, and a decision that says which
> rule chose it. The total is about 855 dollars. And meta says one external call: the road,
> from OSRM, a free routing service.

**Send again.**

> Zero calls, a few milliseconds: the same trip is answered from memory.

**Request 7, Toronto.** Send.

> A city in Canada is turned down before any call.

Copy `map.map_url` from request 1.

## 0:55 The page: Route

Paste `map_url` in the browser. It opens at the **Route** step.

> The page is a client of the same API. About 2,800 miles, about 281 gallons burned, and about
> 458 truck stops near the road. Their prices go from about 2.80 to 4.30 a gallon: that gap is
> why it pays to choose where to stop. This trip was asked before, so no request to OSRM.

Click **Next: where to buy fuel**.

## 1:25 Fuel stops

> At every truck stop the driver looks one full tank ahead and follows three rules. One: if
> fuel is cheaper further on, within reach, buy just enough to get there. Two: if not, and the
> finish is within reach, buy just enough to finish. Three: otherwise, fill the tank. So every
> gallon is bought at the cheapest station that could supply it, and a test checks that
> against an exact method on random roads.

Click a **Cheaper ahead** stop (Youngstown), then a **Fill up** stop (Waco).

> Youngstown: fuel is cheaper in Toledo, so it buys just enough to get there. Waco, Nebraska:
> the cheapest price on the road, so it fills the tank.

Click stop 1 (**Merged**) and point at the "On this trip" line.

> One more rule: a tiny stop is merged with a nearby one, if that costs at most a dollar. On
> this trip, eighteen stops became twelve, for about a dollar fifty.

Press **Play the trip** under the map.

> Play shows the tank emptying and refilling, and the money adding up to the exact total.

## 2:30 Cost

Click **Next: the bill**.

> About 855 dollars, and the check: the stop costs add up to this total, to the cent. A driver
> who ignores prices pays about 941. We save about 85 dollars, nine percent. The honest
> trade-off: six more stops, because buying just enough to reach cheaper fuel means more,
> smaller stops.

## 3:05 Truck

Click **Next: try another truck**, then the quick try **A thirstier truck**.

> Eight miles per gallon, same tank. What changed: about 220 dollars more, and no new request
> to OSRM. The road was already saved; only the plan changed.

Click **Keep 5 gallons of safety fuel**.

> With five gallons of safety fuel, the truck never reaches a station on empty, for a couple of
> dollars more.

## 3:40 Assignment

Click **Next: the assignment**. Scroll the table.

> Each point of the assignment, how it is met, and the proof on this trip: one request to the
> routing API, the total for about 281 gallons, and the latest Django.

## 4:05 Code

VS Code, `optimizer.py`, `_greedy`.

> The algorithm is these three branches: cheaper ahead, finish, fill up. The same three rules as
> the page. Then `_consolidate` merges the tiny stops, never more than a dollar each.

## 4:40 Close

> One routing call per new trip, none for a repeat or another truck, and every number on the
> page comes from the API answer. The README has the assumptions, the data gaps and how it
> would scale. Thanks for watching.

---

## If something goes wrong

- **OSRM is slow or answers 502/503:** it is a free demo server. The API already retried
  once; wait a few seconds and send again (the page has a Retry button).
- **The first send shows 0 calls:** the server already planned that trip (no restart after
  the warm-up). Say "zero, because this server planned it already; a new trip makes one".
- **Play does not move:** keep the tab in front. With "reduce motion" on, it jumps to the end.
- **Port already in use:** `python manage.py runserver 8001 --noreload` and change `baseUrl`.
