// "Every number explains itself": one card per value and per idea of the planner page.
//
// EXPLAINERS is the single source of truth behind every "i" button of the page. A key
// is the data-live path of a value (route.summary.total_fuel_cost) or concept:<slug>
// for an idea (concept:three-rules). Each entry says, in plain words:
//
//   title   the name of the value or idea
//   what    what it is, and what it is for
//   source  where it comes from: an API field, /api/about, the browser, or derived
//   how     how it is calculated, in general (the formula in words)
//   live    optional (ctx, arg) => line(s): the same formula with THIS trip's numbers
//   why     why it was built this way
//   code    where in the code, as 'path/to/file:function' (a test checks they exist)
//   edge    optional: cases worth knowing
//   see     optional: related keys ("See also")
//
// ctx is the context main.js binds the page with ({route, standard, about, client,
// derived}), so the live lines follow the page's own rule: no number is written by
// hand, every figure is read from the answer, /api/about or the browser. explain()
// turns an entry into the sections the card shows (pure: tested in Node from pytest);
// explain-ui.js draws the buttons and the card.

import { cents, longestStretch, sumCents } from './checks.js';
import { fmt, plural, present } from './format.js';
import { timing } from './player.js';
import { QUICK_TRIES } from './whatif.js';

// The sections every entry must have (fuelroute/tests/test_explain.py checks them).
export const SECTIONS = ['what', 'source', 'how', 'why', 'code'];
export const SECTION_LABELS = {
  what: 'What it is',
  source: 'Where it comes from',
  how: 'How it’s calculated',
  why: 'Why this way',
  code: 'In the code',
  edge: 'Good to know',
};

// --- formatting and reading the context ------------------------------------------------

const PRICE = new Intl.NumberFormat('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 4 });
const EXACT = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const COORD = new Intl.NumberFormat('en-US', { minimumFractionDigits: 5, maximumFractionDigits: 5 });
// A price as the API sends it (four decimals; the page shows three).
const price4 = (v) => `$${PRICE.format(v)}`;
// Money before it is rounded to the cent.
const exact = (v) => `$${EXACT.format(v)}`;
const HALF_HUNDREDTH = 0.005;
const MAX_TERMS = 8; // a longer sum shows its first terms, "…" and the last one
const EARTH_MILES = 3958.7613;

// Thrown by need() when the page has no value yet (no trip planned): the card then
// shows only the general formula.
const MISSING = Symbol('missing');
function need(...values) {
  if (!values.every(present)) throw MISSING;
}

const answer = (c) => c?.route || null;
const veh = (c) => c?.route?.vehicle || {};
const summ = (c) => c?.route?.summary || {};
const metaOf = (c) => c?.route?.meta || {};
const stopsOf = (c) => c?.route?.fuel_stops || [];
const aboutVehicle = (c) => c?.about?.vehicle || {};
const aboutData = (c) => c?.about?.data || {};
const der = (c) => c?.derived || {};
const safetyOf = (c) => Number(veh(c).safety_reserve_gal) || 0;

// The context of the standard truck's answer: the "For Spotter" step reads it.
const standardContext = (c) => ({ ...c, route: c?.standard ?? null, derived: c?.derived?.standard ?? {} });

function sumLine(terms) {
  const shown = terms.length > MAX_TERMS ? [...terms.slice(0, MAX_TERMS - 2), '…', terms[terms.length - 1]] : terms;
  return shown.join(' + ');
}

const GEOCODERS = {
  offline: 'found in the built-in list of US places: no external call',
  coordinates: 'typed as coordinates: no lookup',
  nominatim: 'found by Nominatim, a free place search: one external call, then saved',
};

function placeLine(label, place) {
  need(place?.label);
  const how = GEOCODERS[place.geocoder] || place.geocoder;
  const at = present(place.lat) ? ` at ${COORD.format(place.lat)}, ${COORD.format(place.lon)}` : '';
  return `${label}: you typed “${place.query}”, the server read ${place.label}${at} (${how}).`;
}

function haversine(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_MILES * Math.asin(Math.min(1, Math.sqrt(h)));
}

const RULE_NAMES = { reach_cheaper: 'Cheaper ahead', finish: 'Finish', fill_up: 'Fill up' };

function ruleCounts(stops) {
  const counts = {};
  for (const stop of stops) {
    const rule = stop.decision?.rule;
    if (rule) counts[rule] = (counts[rule] || 0) + 1;
  }
  const parts = Object.keys(RULE_NAMES).filter((rule) => counts[rule]).map((rule) => `${RULE_NAMES[rule]} × ${fmt.int(counts[rule])}`);
  return parts.length ? `Rules used: ${parts.join(', ')}.` : null;
}

// A stop by its number (arg), or the first stop.
function stopOf(c, arg) {
  const stops = stopsOf(c);
  const n = Number(arg);
  return stops.find((s) => s.stop === n) || stops[0] || null;
}

const firstWithRule = (c, rule) => stopsOf(c).find((s) => s.decision?.rule === rule && !s.decision?.consolidated) || null;

// " (the API says X)" when a hand check and the API differ by rounding.
function apiSays(computed, api, format) {
  return Math.abs(computed - api) >= HALF_HUNDREDTH ? ` (the API says ${format(api)}: miles and gallons are rounded)` : '';
}

// How much the truck has when it pulls in: from the previous stop (or the start).
function arrivalLine(c, stop) {
  const stops = stopsOf(c);
  const mpg = veh(c).miles_per_gallon;
  const i = stops.indexOf(stop);
  const prev = i > 0 ? stops[i - 1] : null;
  const leftWith = prev ? prev.fuel_on_arrival_gallons + prev.gallons : summ(c).start_fuel_gallons;
  const fromMile = prev ? prev.mile_marker : 0;
  need(leftWith, fromMile, mpg, stop.mile_marker);
  const calc = Math.max(0, leftWith - (stop.mile_marker - fromMile) / mpg);
  return `Arrives with: ${fmt.gal(leftWith)} when leaving ${prev ? `stop ${prev.stop}` : 'the start'} − `
    + `(${fmt.dec1(stop.mile_marker)} − ${fmt.dec1(fromMile)}) miles ÷ ${fmt.num(mpg)} mpg = ${fmt.gal(calc)}`
    + `${apiSays(calc, stop.fuel_on_arrival_gallons, fmt.gal)}.`;
}

// What the stop buys, by the rule that made it, with this stop's numbers.
function purchaseLine(c, stop) {
  const d = stop.decision || {};
  const mpg = veh(c).miles_per_gallon;
  need(mpg, stop.gallons, stop.fuel_on_arrival_gallons);
  if (d.consolidated) {
    return `Buys ${fmt.gal(stop.gallons)}: the three rules alone would buy ${fmt.gal(d.greedy_gallons)} here, and “Skip tiny stops” moved fuel (see “Why here”).`;
  }
  const arrival = stop.fuel_on_arrival_gallons;
  if (d.rule === 'reach_cheaper' && d.cheaper_station) {
    const cheaper = d.cheaper_station;
    const usable = arrival - safetyOf(c);
    const needed = (cheaper.mile - stop.mile_marker) / mpg;
    const buy = Math.max(0, needed - usable);
    return `Buys (rule “Cheaper ahead”): ${cheaper.city}, ${cheaper.state} sells at ${price4(cheaper.price_per_gallon)} vs ${price4(stop.price_per_gallon)} here. `
      + `Getting there takes (${fmt.dec1(cheaper.mile)} − ${fmt.dec1(stop.mile_marker)}) ÷ ${fmt.num(mpg)} mpg = ${fmt.gal(needed)}; `
      + `the tank has ${fmt.gal(usable)}${safetyOf(c) > 0 ? ' above the safety fuel' : ''}, so it buys ${fmt.gal(needed)} − ${fmt.gal(usable)} = ${fmt.gal(buy)}`
      + `${apiSays(buy, stop.gallons, fmt.gal)}.`;
  }
  if (d.rule === 'finish') {
    const distance = answer(c).route.distance_miles;
    const end = summ(c).end_fuel_gallons;
    const toGo = (distance - stop.mile_marker) / mpg;
    const buy = Math.max(0, toGo + end - arrival);
    return `Buys (rule “Finish”): the rest of the road takes (${fmt.dec1(distance)} − ${fmt.dec1(stop.mile_marker)}) ÷ ${fmt.num(mpg)} mpg = ${fmt.gal(toGo)}, `
      + `plus the ${fmt.gal(end)} it must arrive with, minus the ${fmt.gal(arrival)} it has: ${fmt.gal(buy)}${apiSays(buy, stop.gallons, fmt.gal)}.`;
  }
  if (d.rule === 'fill_up') {
    const tank = veh(c).tank_gallons;
    const buy = Math.max(0, tank - arrival);
    return `Buys (rule “Fill up”): a full tank ${fmt.gal(tank)} − the ${fmt.gal(arrival)} it has = ${fmt.gal(buy)}${apiSays(buy, stop.gallons, fmt.gal)}.`;
  }
  return `Buys ${fmt.gal(stop.gallons)}.`;
}

function costLine(stop) {
  need(stop.gallons, stop.price_per_gallon, stop.cost);
  return `Cost: ${fmt.gal(stop.gallons)} × ${price4(stop.price_per_gallon)} = ${exact(stop.gallons * stop.price_per_gallon)}, `
    + `rounded to the cent: ${fmt.money(stop.cost)}.`;
}

// The detour to a stop, ESTIMATED by the page: the plan does not charge it (README,
// limits). distance_from_route_miles is the straight line from the station to its
// nearest road point, one way; there and back is twice that, burned at this truck's
// miles per gallon and priced at the stop. null when the answer lacks a number.
function detourOf(c, stop) {
  const mpg = veh(c).miles_per_gallon;
  const off = stop?.distance_from_route_miles;
  if (![off, mpg, stop?.price_per_gallon].every(present) || mpg <= 0) return null;
  const gallons = (2 * off) / mpg;
  return { off, miles: 2 * off, gallons, dollars: gallons * stop.price_per_gallon, mpg };
}

function detourLine(c, stop) {
  const d = detourOf(c, stop);
  if (!d) return null;
  if (d.off === 0) return `Detour: ${fmt.miles(d.off)} off the road, so nothing to add.`;
  return `Detour: ≈ ${fmt.miles(d.off)} off the road → about ${fmt.miles(d.miles)} there and back ≈ ${fmt.gal(d.gallons)} `
    + `at this truck’s ${fmt.num(d.mpg)} mpg ≈ ${fmt.money(d.dollars)} at this stop’s price, not in the total (straight-line estimate).`;
}

// Every stop's detour of a plan, added up: Σ 2 × distance ÷ mpg gallons, each priced at
// its own stop. null without stops or when one stop lacks its distance.
function detoursOf(c) {
  const stops = stopsOf(c);
  const each = stops.map((stop) => detourOf(c, stop));
  if (!stops.length || each.includes(null)) return null;
  const add = (field) => each.reduce((total, d) => total + d[field], 0);
  return { stops, miles: add('miles'), gallons: add('gallons'), dollars: add('dollars'), mpg: each[0].mpg };
}

function detoursLine(c) {
  const d = detoursOf(c);
  if (!d) return null;
  return `Not in the total, the detours to the stops (straight-line estimate): 2 × (${sumLine(d.stops.map((s) => fmt.dec1(s.distance_from_route_miles)))}) mi`
    + ` = ${fmt.miles(d.miles)} there and back ÷ ${fmt.num(d.mpg)} mpg ≈ ${fmt.gal(d.gallons)} ≈ ${fmt.money(d.dollars)} at each stop’s price.`;
}

// Which tank sentence of the Route step shows, and why.
function tankSentence(c) {
  const d = der(c);
  if (d.tank_same_back) return 'The page shows “must arrive with … too”: the fuel out and in is the same (within half a hundredth of a gallon), so the bill is the fuel the trip burns.';
  if (d.is_full) return 'The page shows “leaves with a full tank”: that fuel was free and is not in the bill.';
  if (d.tank_arrives_lower) return 'The page shows “arrives with less”: part of the road has no truck stop of the price file, so that fuel has no price.';
  return null;
}

// Why the truck leaves with that much fuel (start_tank=empty). planner._tank_rules takes
// the larger of the start reserve and the miles to the first truck stop + the safety
// fuel, never more than a tank, and lowers it when the last truck stop is far from the
// finish. The API rounds gallons to the hundredth, so miles are compared within that.
function startReasonLine(c, miles, reserve) {
  const mpg = veh(c).miles_per_gallon;
  const safety = safetyOf(c) * mpg;
  const reserveText = fmt.miles_short(reserve);
  if (summ(c).candidate_stations_on_route === 0) {
    return `No truck stop near the road: it leaves with the ${reserveText} start reserve, or the safety fuel if that is larger.`;
  }
  if (Math.abs(miles - reserve) <= HALF_HUNDREDTH * mpg + 1e-9) return `That is the start reserve (${reserveText}).`;
  if (miles > reserve) {
    return safety > 0
      ? `More than the ${reserveText} start reserve: enough to reach the first truck stop near the road with the safety fuel still in the tank (first stop + ${fmt.miles_short(safety)} of safety fuel = ${fmt.miles_short(miles)}).`
      : `More than the ${reserveText} start reserve: enough to reach the first truck stop near the road, about ${fmt.miles_short(miles)} from the start.`;
  }
  return `Less than the ${reserveText} start reserve: the last truck stop is far from the finish, so that is all the truck can still have on arrival (see “Good to know”).`;
}

// One truck setting's range, from /api/about. safety_reserve_gal has no fixed maximum:
// it must stay below the tank, which depends on the range and the miles per gallon.
function limitLine(c, p) {
  const low = present(p.min) ? fmt.num(p.min) : 'no minimum';
  let high = present(p.max) ? fmt.num(p.max) : 'no fixed limit';
  if (p.name === 'safety_reserve_gal' && !present(p.max)) {
    const v = answer(c) ? veh(c) : aboutVehicle(c);
    const tank = v.max_range_miles / v.miles_per_gallon;
    high = present(tank)
      ? `less than the tank (${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.gal(tank)})`
      : 'less than the tank';
  }
  return `${p.name}: ${low} to ${high}, default ${present(p.default) ? fmt.num(p.default) : 'none'}.`;
}

// An error code that is not in /api/about's catalog: the browser's own, or an answer
// that had no API error body (main.js renderError builds http_<status> for it).
function notApiCode(code) {
  if (!code) return null;
  if (code === 'network') return 'network: the browser could not reach the server (no answer at all).';
  if (code === 'timeout') return 'timeout: the browser gave up waiting (its time limit, CLIENT_TIMEOUT_MS in main.js).';
  const status = /^http_(\d+)$/.exec(code)?.[1];
  if (status) return `${code}: the server answered HTTP ${status} without one of the API’s error codes (for example a crash page or a proxy).`;
  return `${code}: not one of the API’s error codes.`;
}

function savingsLines(c) {
  const cmp = summ(c).comparison;
  const blind = cmp?.price_blind;
  const saving = cmp?.savings_vs_price_blind;
  need(blind?.total_fuel_cost, summ(c).total_fuel_cost, saving?.amount);
  return [
    `${fmt.money(blind.total_fuel_cost)} (driver who ignores prices) − ${fmt.money(summ(c).total_fuel_cost)} (this plan) = ${fmt.money(saving.amount)}.`,
    `${fmt.money(saving.amount)} ÷ ${fmt.money(blind.total_fuel_cost)} × 100 = ${fmt.pct(saving.percent)}.`,
  ];
}

// A value of the For Spotter step, read from the standard truck's answer. sameForEvery:
// the value does not depend on the truck (the places, the road), so the title says so.
function withStandard(base, { title, sameForEvery = false } = {}) {
  return {
    ...base,
    title: title || `${base.title} ${sameForEvery ? '(the same for every truck)' : '(standard truck)'}`,
    source: `From the standard truck’s answer (see “Why this table uses the standard truck”). ${base.source}`,
    live: base.live ? (c, arg) => base.live(standardContext(c), arg) : undefined,
    see: [...new Set(['concept:standard-truck-answer', ...(base.see || [])])],
  };
}

// --- shared entries (a value shown in two places, with the same meaning) ------------------

const START_LABEL = {
  title: 'Start',
  what: 'The start of the trip as the server understood it. It is not always the exact text that was typed.',
  source: 'API field start.label of GET /api/route (with start.query, start.lat, start.lon and start.geocoder).',
  how: '“City, ST” → the city in Title Case (each word capitalized) and the state code, from the built-in list of US places; coordinates → the numbers themselves; any other text → the full name Nominatim gives. The green dot on the map is at start.lat, start.lon.',
  live: (c) => placeLine('Start', answer(c)?.start),
  why: 'Saying the match back makes a wrong match visible (a city with the same name in another state, or a street that happens to have that name).',
  code: [
    'fuelroute/services/geocoding.py:geocode', 'fuelroute/services/geocoding.py:Location.to_dict',
    'fuelroute/services/places.py:PlaceIndex.lookup', 'fuelroute/static/fuelroute/js/map.js:drawRoad',
  ],
  edge: 'Python’s Title Case changes inner capitals: “McAllen, TX” is shown as “Mcallen, TX”. A name that exists several times in one state gives the most populated place; type “lat,lon” to be exact.',
  see: ['concept:geocoding', 'concept:route-map-line'],
};

const FINISH_LABEL = {
  ...START_LABEL,
  title: 'Finish',
  what: 'The end of the trip as the server understood it.',
  source: 'API field finish.label of GET /api/route (with finish.query, finish.lat, finish.lon and finish.geocoder).',
  how: 'The same steps as the start. The red dot on the map is at finish.lat, finish.lon. Then the server checks the two ends are not the same place.',
  live: (c) => placeLine('Finish', answer(c)?.finish),
  edge: 'Start and finish less than half a mile apart (SAME_PLACE_MILES) are the same place: 400 same_location (the input is wrong), even written differently (“Austin, TX” and “Austin, Texas”).',
};

const DISTANCE = {
  title: 'Distance',
  what: 'The length of the road the truck drives, from start to finish.',
  source: 'API field route.distance_miles, from OSRM, the free routing service.',
  how: 'OSRM gives the distance of the road it chose in meters; ÷ 1609.344 (meters in a mile), rounded to one decimal.',
  live: (c) => {
    const r = answer(c);
    need(r?.route?.distance_miles);
    const lines = [`${fmt.miles(r.route.distance_miles)} on the road.`];
    if (present(r.start?.lat) && present(r.finish?.lat)) {
      const straight = haversine(r.start, r.finish);
      lines.push(`In a straight line it would be ${fmt.miles(straight)}: the road is ${fmt.pct((r.route.distance_miles / straight - 1) * 100)} longer.`);
    }
    return lines;
  },
  why: 'Fuel is burned per mile of road, so the road distance is needed, not the straight line. OSRM is free, needs no key, and one call gives both the distance and the shape of the road, which is used to give every truck stop a mile marker.',
  code: [
    'fuelroute/services/osrm.py:route_from_payload', 'fuelroute/services/osrm.py:prepare_route',
    'fuelroute/services/planner.py:_plan_trip', 'fuelroute/static/fuelroute/js/format.js:fmt',
  ],
  edge: 'The public OSRM server uses a car profile, not a truck one, and picks its fastest road. The road is saved for a day by its coordinates.',
  see: ['concept:osrm', 'concept:mile-marker', 'concept:drive-duration'],
};

const STOPS_TEXT = {
  title: 'Number of fuel stops',
  what: 'How many times the truck stops to buy fuel, in words (“1 stop”, “3 stops”).',
  source: 'Computed in the browser (format.js derive) from the API field summary.number_of_stops.',
  how: 'plural(number_of_stops, “stop”): the number and the word in agreement. On the server, number_of_stops is the number of stops of the final plan.',
  live: (c) => {
    need(summ(c).number_of_stops);
    return [`number_of_stops = ${fmt.int(summ(c).number_of_stops)} → “${plural(summ(c).number_of_stops, 'stop')}”.`, ruleCounts(stopsOf(c))];
  },
  why: 'The count is a result, not a goal: the plan minimizes cost, not stops, because the model has no cost per stop. The page never writes a number by hand, and a test checks it never says “1 stops”.',
  code: [
    'fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/static/fuelroute/js/format.js:plural',
    'fuelroute/services/planner.py:_build_summary',
  ],
  edge: 'With “Skip tiny stops” off, the count can include very small stops.',
  see: ['concept:three-rules', 'concept:skip-tiny-stops'],
};

const USABLE_RANGE = {
  title: 'Usable range',
  what: 'How far a full tank goes while keeping the safety fuel: the real limit between two purchases, and how far ahead the driver looks in the three rules.',
  source: 'API field vehicle.usable_range_miles.',
  how: 'usable range = range on a full tank − safety fuel × miles per gallon, rounded to one decimal.',
  live: (c) => {
    const v = veh(c);
    need(v.max_range_miles, v.miles_per_gallon, v.usable_range_miles);
    return `${fmt.miles_short(v.max_range_miles)} − ${fmt.gal(safetyOf(c))} × ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.miles_short(v.usable_range_miles)}.`;
  },
  why: 'Safety fuel may never be used, so the plan works with the rest. “Within reach” in the three rules means within this many miles (the optimizer calls it capacity).',
  code: [
    'fuelroute/services/planner.py:PlanSettings.usable_range_miles', 'fuelroute/services/optimizer.py:_usable',
    'fuelroute/services/optimizer.py:_greedy',
  ],
  edge: 'Equal to the range when there is no safety fuel.',
  see: ['route.vehicle.safety_reserve_gal'],
};

const TOTAL_COST = {
  title: 'Total fuel cost',
  what: 'What the fuel for the whole trip costs with this plan, in US dollars: the fuel bought at the stops.',
  source: 'API field summary.total_fuel_cost.',
  how: 'Each stop: gallons (rounded to the hundredth) × price (four decimals), rounded to the cent, half up like a till (Python’s round() would round halves to even). Total = the sum of the stop costs, added with Decimal (exact decimal numbers, not floating point).',
  live: (c) => {
    const stops = stopsOf(c);
    const s = summ(c);
    need(s.total_fuel_cost);
    if (!stops.length) return `No stop, so nothing is bought: ${fmt.money(s.total_fuel_cost)}.`;
    const total = sumCents(stops.map((x) => x.cost));
    const first = stops[0];
    return [
      `${sumLine(stops.map((x) => fmt.money(x.cost)))} = ${fmt.money(total / 100)} (${plural(stops.length, 'stop')}).`,
      total === cents(s.total_fuel_cost)
        ? `The API’s total is ${fmt.money(s.total_fuel_cost)}: the same, to the cent.`
        : `The API’s total is ${fmt.money(s.total_fuel_cost)}: they do NOT match.`,
      `For example stop ${first.stop}: ${fmt.gal(first.gallons)} × ${price4(first.price_per_gallon)} = ${exact(first.gallons * first.price_per_gallon)} → ${fmt.money(first.cost)}.`,
      detoursLine(c),
    ];
  },
  why: 'Rounding each stop first and adding exactly makes the stops on screen add up to the total, to the cent. The fuel already in the tank is not billed: by default the truck returns the tank as it got it, so the bill is the fuel burned.',
  code: [
    'fuelroute/services/planner.py:_money_rows', 'fuelroute/services/planner.py:_money',
    'fuelroute/services/planner.py:_build_summary', 'fuelroute/services/optimizer.py:_to_stops',
    'fuelroute/static/fuelroute/js/plan.js:renderCost', 'fuelroute/tests/test_api.py:test_stop_costs_add_up_to_the_total_to_the_cent',
  ],
  edge: 'Not in the total: the free first tank, with a full tank at the start, and the detours to the stops (“On this trip” estimates them). The cards show prices with three decimals while the cost uses four, so a hand check can be a cent off.',
  see: ['concept:check-costs-add-up-to-the-cent', 'concept:station-price', 'concept:pay-for-every-mile', 'concept:detour-estimate'],
};

const GALLONS_BOUGHT = {
  title: 'Fuel bought',
  what: 'How many gallons the truck buys on the whole trip.',
  source: 'API field summary.total_gallons_purchased.',
  how: 'The sum of every stop’s gallons, each rounded to the hundredth. With the default start (almost empty, arrive with the same) it equals the fuel burned: distance ÷ miles per gallon.',
  live: (c) => {
    const s = summ(c);
    const stops = stopsOf(c);
    need(s.total_gallons_purchased, s.fuel_used_gallons, s.start_fuel_gallons, s.end_fuel_gallons);
    const lines = [];
    if (stops.length) lines.push(`${sumLine(stops.map((x) => fmt.num(x.gallons)))} = ${fmt.gal(s.total_gallons_purchased)}.`);
    lines.push(`Check: burned ${fmt.gal(s.fuel_used_gallons)} + arrives with ${fmt.gal(s.end_fuel_gallons)} − leaves with ${fmt.gal(s.start_fuel_gallons)}`
      + ` = ${fmt.gal(s.fuel_used_gallons + s.end_fuel_gallons - s.start_fuel_gallons)}.`);
    return lines;
  },
  why: 'This is where miles per gallon enters the money: miles become gallons by dividing by mpg. Adding the rounded stop gallons makes the list add up to this total.',
  code: [
    'fuelroute/services/optimizer.py:_to_stops', 'fuelroute/services/planner.py:_money_rows',
    'fuelroute/services/planner.py:_build_summary',
  ],
  edge: 'With a full tank at the start the free tank is not bought, so this is smaller than the fuel burned. A few hundredths of difference come from rounding each stop.',
  see: ['concept:fuel-in-miles-of-range'],
};

const SAVINGS = {
  title: 'You save',
  what: 'How much cheaper the plan is than a driver who ignores prices, on the same trip.',
  source: 'API field summary.comparison.savings_vs_price_blind.amount (read by format.js derive).',
  how: 'saving = the driver’s total − the plan’s total, both exact sums of stop costs rounded to the cent. Percent = saving ÷ the driver’s total × 100, one decimal.',
  live: (c) => savingsLines(c),
  why: 'A saving against a realistic driver is the concrete value of the tool. Both buy the same gallons, so the difference is only where they buy. With default settings it cannot be negative: the three rules are optimal.',
  code: [
    'fuelroute/services/planner.py:_savings', 'fuelroute/services/planner.py:_build_comparison',
    'fuelroute/static/fuelroute/js/format.js:derive',
  ],
  edge: 'Zero when both buy the same gallons at the same truck stops (common on short trips). Negative only with “Skip tiny stops” on.',
  see: ['concept:price-blind-driver', 'concept:three-rules'],
};

const MIN_STOP = {
  title: 'Smallest real stop',
  what: 'The smallest purchase that counts as a real stop when “Skip tiny stops” is on. A stop that would buy less is a “tiny stop”.',
  source: '/api/about → vehicle.min_stop_gallons ← MIN_STOP_GALLONS in the configuration (settings.FUEL_PLANNER).',
  how: 'A configuration value, shown with two decimals. The optimizer uses it only when consolidate=true, in miles: minimum × the miles per gallon of the plan.',
  live: (c) => {
    const v = aboutVehicle(c);
    // The optimizer converts with the mileage of the plan on screen, not the standard one.
    const mpg = veh(c).miles_per_gallon ?? v.miles_per_gallon;
    need(v.min_stop_gallons, mpg);
    return `${fmt.gal(v.min_stop_gallons)} × ${fmt.num(mpg)} mpg = ${fmt.miles_short(v.min_stop_gallons * mpg)} of driving.`;
  },
  why: 'A driver will not pull over for two gallons to save a few cents. The size is a judgment, not math, so it is a setting the page reads from /api/about.',
  code: [
    'config/settings.py:FUEL_PLANNER', 'fuelroute/services/about.py:_vehicle',
    'fuelroute/services/planner.py:PlanSettings.min_stop_gallons', 'fuelroute/services/optimizer.py:plan_fuel_stops',
  ],
  edge: 'With “Skip tiny stops” off it changes nothing; it only names tiny stops in a tip.',
  see: ['concept:skip-tiny-stops'],
};

const MAX_FIX = {
  title: 'Most one fix may cost',
  what: 'The most ONE merge of a tiny stop may add to the fuel bill. A merge that costs more is not done.',
  source: '/api/about → vehicle.max_consolidation_cost ← MAX_CONSOLIDATION_COST in the configuration.',
  how: 'Moving g gallons from a stop priced b to a stop priced a costs g × (a − b). A merge is allowed only if that is at most this limit and the tank stays between empty and full.',
  live: (c) => {
    const v = aboutVehicle(c);
    need(v.max_consolidation_cost);
    const merged = summ(c).tiny_stops_merged;
    return [`Limit: ${fmt.money(v.max_consolidation_cost)} per fix.`,
      merged ? `On this trip all the fixes together added ${fmt.money(merged.extra_cost)}.` : null];
  },
  why: 'It bounds how far the plan may drift from the cheapest one, and the total cost of the merges is reported, not hidden.',
  code: [
    'fuelroute/services/optimizer.py:_consolidate', 'fuelroute/services/optimizer.py:_shift',
    'fuelroute/services/about.py:_vehicle', 'config/settings.py:FUEL_PLANNER',
  ],
  edge: 'The limit is per fix, not per trip: several fixes can add up to more.',
  see: ['concept:skip-tiny-stops'],
};

// --- the explainers ----------------------------------------------------------------------

export const EXPLAINERS = {
  // ----- the page as a whole -----
  'concept:data-live-rule': {
    title: 'Every number on this page',
    what: 'Every number on this page is filled in from the server’s answer; none is typed into the page. Press any “i” to see what a number is, where it comes from and how it is computed.',
    source: 'Five sources: the trip’s answer, the standard truck’s answer for the same trip, the server’s settings (/api/about), your browser’s clock, and small calculations on those.',
    how: 'Each value is a placeholder, <span data-live="root.path">, whose root is route, standard, about, client or derived (the five sources). main.js update() gathers them and format.js bind() fills every placeholder, or shows a dash when the value is missing.',
    live: (c) => {
      const r = answer(c);
      const d = aboutData(c);
      need(c?.about?.data);
      return [
        r ? `route: ${r.start?.label} → ${r.finish?.label}.` : 'route: no trip planned yet.',
        `standard: ${c?.standard ? 'loaded' : 'not loaded'}.`,
        present(d.geocoded) ? `about: ${fmt.int(d.geocoded)} truck stops placed, Django ${c.about?.versions?.django}.` : null,
        present(c?.client?.server_ms) ? `client: the last answer took ${fmt.seconds(c.client.server_ms)} on the server.` : null,
      ];
    },
    why: 'The page cannot contradict the API: every value is read from the API, /api/about or the browser, and tests forbid digits in the page text. A changed server setting changes the figures, not the words of these cards, the four one-click tries or a few template words.',
    code: [
      'fuelroute/static/fuelroute/js/format.js:bind', 'fuelroute/static/fuelroute/js/format.js:lookup',
      'fuelroute/static/fuelroute/js/main.js:context', 'fuelroute/static/fuelroute/js/explain.js:EXPLAINERS',
      'fuelroute/tests/test_web.py:test_page_has_no_hand_written_numbers', 'fuelroute/tests/test_explain.py:test_every_live_value_has_an_explainer',
    ],
    edge: 'about is embedded when the page loads, so its figures are those of that moment.',
    see: ['concept:find-fuel-stops-request', 'concept:pipeline', 'concept:how-it-is-tested'],
  },

  // ----- step 1: the trip -----
  'concept:find-fuel-stops-request': {
    title: 'What “Find fuel stops” does',
    what: 'It asks the server to plan the trip and shows the answer: one read request, GET /api/route, the same one Postman (a tool developers use to call APIs) sends.',
    source: 'The browser (main.js planTrip) calls the public API; the server answers with views.RoutePlanView → planner.plan_trip.',
    how: 'The browser checks both fields, then sends start, finish, start_tank and only the truck settings that differ from the standard truck. A newer click cancels an older request, and the page gives up after forty seconds (CLIENT_TIMEOUT_MS). What the server does is in “The order of work on the server”.',
    live: (c) => {
      const r = answer(c);
      need(r?.start?.query);
      const m = metaOf(c);
      const changed = m.settings_changed || [];
      return [
        `Last request: start “${r.start.query}”, finish “${r.finish.query}”, start_tank=${veh(c).start_tank}${changed.length ? `, changed settings: ${changed.join(', ')}` : ', no other setting'}.`,
        `It made ${plural(m.external_api_calls ?? 0, 'external call')} (plan cache: ${m.plan_cache}, road cache: ${m.route_cache}).`,
      ];
    },
    why: 'The page is only a client of the public API, so what you see is what the JSON says (a test forbids the page from asking for anything more than Postman). Only changed settings are sent, so a standard trip is the short Postman URL.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:planTrip', 'fuelroute/static/fuelroute/js/api.js:request',
      'fuelroute/static/fuelroute/js/api.js:tripQuery', 'fuelroute/static/fuelroute/js/settings.js:onlyChanged',
      'fuelroute/views.py:RoutePlanView', 'fuelroute/services/planner.py:plan_trip',
      'fuelroute/tests/test_web.py:test_the_page_asks_exactly_what_postman_sends',
    ],
    edge: 'request() does not throw for HTTP errors, network failures or timeouts: they come back as an error shown under the form. It throws only when a newer request replaced it. Without JavaScript the form is a plain GET form that reloads the page with the trip in its address.',
    see: ['concept:pipeline', 'concept:page-address-url', 'concept:next-step-lock', 'concept:planning-timer', 'concept:checks-before-routing'],
  },

  'concept:pipeline': {
    title: 'The order of work on the server',
    what: 'What the server does, step by step, for one /api/route request.',
    source: 'services/planner.py, plan_trip and _plan_trip.',
    how: '1) Read both places (offline) and run cheap checks; 2) reuse a saved plan if there is one; 3) get the road (saved, or one OSRM call) and the truck stops near it; 4) set the tank, run the three rules, compare with a driver who ignores prices, and save.',
    live: (c) => {
      const m = metaOf(c);
      need(m.plan_cache);
      return `This answer: plan cache ${m.plan_cache}, road cache ${m.route_cache}, ${plural(summ(c).candidate_stations_on_route, 'truck stop')} near the road, ${plural(summ(c).number_of_stops, 'stop')}.`;
    },
    why: 'The cheapest steps, and those most likely to fail, run first. The only slow step (the call to OSRM) happens only for a valid new trip, and its result is saved apart from the truck settings.',
    code: [
      'fuelroute/services/planner.py:plan_trip', 'fuelroute/services/planner.py:_plan_trip',
      'fuelroute/services/planner.py:_check_endpoints', 'fuelroute/services/osrm.py:get_route',
      'fuelroute/services/stations.py:stations_along_route', 'fuelroute/services/planner.py:_tank_rules',
      'fuelroute/services/optimizer.py:plan_fuel_stops',
    ],
    edge: 'include=details adds the counts and timings of each step to the JSON; the page never asks for it.',
    see: ['concept:checks-before-routing', 'concept:server-timing', 'concept:route-cache-vs-plan-cache'],
  },

  'concept:start-finish-inputs': {
    title: 'From and To',
    what: 'Where the trip starts and ends. They are sent as the start and finish parameters of GET /api/route.',
    source: 'You type them, pick a suggestion, click an example, or they come from the page address (?start=&finish=).',
    how: 'Accepted: “City, ST”, “City, State”, “City ST” or coordinates “lat,lon”, up to two hundred characters. Text shorter than three characters or with no letter is refused with a 400 (the input is wrong). The server turns each place into a point (geocoding).',
    live: (c) => [placeLine('Start', answer(c)?.start), placeLine('Finish', answer(c)?.finish)],
    why: '“City, ST” is found in a built-in list of US places: no external call, under a millisecond, always the same point. Coordinates give an exact point. Other free text costs one call to Nominatim, a free place search.',
    code: [
      'fuelroute/templates/fuelroute/partials/_step_trip.html', 'fuelroute/static/fuelroute/js/main.js:submitTrip',
      'fuelroute/serializers.py:RouteRequestSerializer._validate_location', 'fuelroute/services/geocoding.py:geocode',
    ],
    edge: 'Start and finish less than half a mile apart are the same place (400). Alaska, Hawaii or a US territory: 422 (the input is fine, but that trip cannot be planned). A place outside the USA: 400.',
    see: ['concept:geocoding', 'concept:autocomplete-suggestions', 'concept:courtesy-hint', 'concept:field-errors', 'concept:swap-button', 'concept:planning-timer', 'concept:trip-error-box'],
  },

  'concept:geocoding': {
    title: 'Finding the places (geocoding)',
    what: 'Geocoding = turning a place name into latitude and longitude. It is the first step of every trip.',
    source: 'The server (services/geocoding.py): a built-in list of US places first; Nominatim (a free place search on OpenStreetMap) only for other text.',
    how: 'Cheapest first: “lat,lon” is read as is; “City, ST” comes from the built-in list; a Canadian province, a Mexican state or a US territory after the comma is refused. Anything else is one Nominatim call, limited to the USA. Every point must fall inside an outline of the USA.',
    live: (c) => [placeLine('Start', answer(c)?.start), placeLine('Finish', answer(c)?.finish)],
    why: 'The brief asks for few external calls, so the usual input costs none: the only external call of a new trip is the road. The refusal exists because Nominatim, limited to the USA, answers “Toronto, ON” with a US street named Toronto: the wrong trip would be planned silently.',
    code: [
      'fuelroute/services/geocoding.py:geocode', 'fuelroute/services/geocoding.py:split_city_state',
      'fuelroute/services/geocoding.py:_offline', 'fuelroute/services/geocoding.py:_reject_region_outside_the_states',
      'fuelroute/services/geocoding.py:_nominatim', 'fuelroute/services/places.py:PlaceIndex.lookup',
      'fuelroute/services/usa.py:region_of',
    ],
    edge: 'A name that exists several times in one state (Tennessee has many Antiochs) gives the most populated one. Nominatim is asked at most once per second, its answer is saved, and a match that is only a street is refused.',
    see: ['concept:offline-places-list', 'concept:checks-before-routing'],
  },

  'concept:autocomplete-suggestions': {
    title: 'City suggestions',
    what: 'The list under From and To while you type: US places the planner is sure to understand.',
    source: 'GET /api/places?q=…&limit=…, answered from the same built-in list of US places. No external call.',
    how: 'The browser waits a short pause after each key (a hundred and fifty milliseconds, DEBOUNCE_MS) and needs two letters (MIN_LETTERS). The server reads the text the way lookups do (accents, “st” → saint) and keeps the most populated names that start with it. Text after a comma filters the state.',
    why: 'Picking a suggestion means no typo and no external call. A suggestion’s point is the point the planner will use, so a picked place always plans to the place shown (a test checks every label).',
    code: [
      'fuelroute/static/fuelroute/js/places.js:createCombobox', 'fuelroute/views.py:PlacesView',
      'fuelroute/services/places.py:parse_place_query', 'fuelroute/services/places.py:PlaceIndex.search',
      'fuelroute/tests/test_places.py:test_every_label_plans_to_the_same_point',
    ],
    edge: 'Arrows move, Enter picks without planning, Escape closes. Names are found by binary search (halving a sorted list each step), and a newer key cancels the older request. Suggestions only help: any text you type is still sent, and the API decides.',
    see: ['concept:suggestion-population', 'concept:suggestion-bold-highlight', 'concept:no-price-data-tag'],
  },

  'concept:suggestion-population': {
    title: 'The “people” number in a suggestion',
    what: 'The population of that place. It tells apart places with the same name, and it explains the order of the list.',
    source: '/api/places results[].population, from the built-in list (US Census Gazetteer and GeoNames).',
    how: 'Shown as “N people” when known. A name with no population whose “<name> City” twin in the same state is within six miles (_TWIN_MILES) takes the twin’s population: “New York, NY” shows that of “New York City, NY”.',
    why: 'The list is sorted by population, so the place people mean comes first (Anchorage, AK above Anchorage, KY). The twin rule lets the form the examples use rank first.',
    code: [
      'fuelroute/static/fuelroute/js/places.js:createCombobox', 'fuelroute/services/places.py:PlaceIndex._with_twin_population',
      'fuelroute/services/places.py:PlaceIndex._suggestion',
    ],
    edge: 'Places without a known population show no number.',
  },

  'concept:suggestion-bold-highlight': {
    title: 'The bold part of a suggestion',
    what: 'The part of the name that matches what you typed.',
    source: 'The browser (places.js), from the label and your text.',
    how: 'Your text is read with the server’s rules (normalize_place): accents removed, lower case, “st” → saint, “ft” → fort, “mt” → mount… Then each typed word bolds the start of the matching word of the label: “st lou” bolds Saint and Lou in Saint Louis, MO.',
    why: 'The server matches normalized names, so a plain “contains” highlight would bold nothing for “st lou”. Copying the server’s rules keeps the bold part honest; a test runs this code in Node.',
    code: [
      'fuelroute/static/fuelroute/js/places.js:typedWords', 'fuelroute/static/fuelroute/js/places.js:highlight',
      'fuelroute/services/text.py:normalize_place',
      'fuelroute/tests/test_js_logic.py:test_city_suggestions_bold_what_was_typed_the_way_the_server_reads_it',
    ],
  },

  'concept:no-price-data-tag': {
    title: '“No price data” in a suggestion',
    what: 'A real US place whose trip cannot be priced: Alaska and Hawaii have no truck stop in the price file.',
    source: '/api/places results[].plannable.',
    how: 'plannable = the state is not Alaska or Hawaii (_NO_FUEL_DATA_STATES). When it is false the row is dimmed and tagged.',
    why: 'Hiding Anchorage would look like a bug. Showing it with a warning is honest; planning it anyway gives a clear 422 no_fuel_data_in_region (valid input, but no plan is possible) before any routing call.',
    code: [
      'fuelroute/services/places.py:_NO_FUEL_DATA_STATES', 'fuelroute/services/places.py:PlaceIndex._suggestion',
      'fuelroute/services/planner.py:_check_endpoints', 'fuelroute/services/errors.py:NoFuelDataInRegion',
    ],
  },

  'concept:courtesy-hint': {
    title: 'The grey hint under a field',
    what: '“Use City, ST” appears while you type something that cannot be a place.',
    source: 'The browser (main.js courtesyHint).',
    how: 'Empty text or coordinates: no hint. Fewer than three characters, or no letter: the hint shows. It is checked on every key.',
    why: 'It is the server’s own rule (a 400: the input is wrong), shown early so no request is wasted. It is only a hint: the API still decides.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:courtesyHint', 'fuelroute/static/fuelroute/js/main.js:updateHints',
      'fuelroute/serializers.py:RouteRequestSerializer._validate_location',
    ],
  },

  'concept:field-errors': {
    title: 'Red messages under a field',
    what: 'Why the API refused a value, shown next to the field it belongs to.',
    source: 'The API error body: detail per field (input checks), or field + detail (a place that cannot be found). “Required.” comes from the browser.',
    how: 'Every API error has the shape {error, detail, meta}, plus field when one input is to blame. main.js renderError puts each message under its field and marks the field invalid. Typing in the field hides it.',
    why: 'The page points at the exact field without parsing English text.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:showFieldError', 'fuelroute/static/fuelroute/js/main.js:renderError',
      'fuelroute/services/errors.py:PlannerError', 'fuelroute/views.py:_plan_from',
    ],
    edge: 'Truck setting errors show under the Truck step’s fields; a range error shows under the tank box.',
  },

  'concept:trip-error-box': {
    title: 'Why the trip could not be planned',
    what: 'The red box: a title in plain words, what to do next, and the API’s error code with its HTTP status.',
    source: 'The API error body and status (and the Retry-After header). “network” and “timeout” come from the browser.',
    how: 'Status in plain words: 400 = the input is wrong; 422 = the input is fine but this trip cannot be planned; 429 = too many requests; 502/503 = a service is down or busy. Titles come from a list in main.js (ERROR_TITLES); codes worth retrying get a Retry button.',
    live: (c, code) => {
      const errors = c?.about?.errors || [];
      need(errors.length || null);
      const found = errors.find((e) => e.code === code);
      return [found ? `${found.code} (HTTP ${found.status}): ${found.description}` : notApiCode(code),
        `The API has ${fmt.int(errors.length)} error codes, all listed in /api/about.`];
    },
    why: 'Codes are stable and listed in /api/about, built from the PlannerError classes. Plain words for a user and the raw code for a developer, in the same box.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:renderError', 'fuelroute/static/fuelroute/js/main.js:ERROR_TITLES',
      'fuelroute/static/fuelroute/js/main.js:retryButton', 'fuelroute/static/fuelroute/js/main.js:fullTankButton',
      'fuelroute/services/about.py:error_catalog', 'fuelroute/services/errors.py:PlannerError',
    ],
    edge: 'no_reachable_fuel_station names the stretch with no truck stop, and the map shows it. no_fuel_data_on_route offers “Try leaving with a full tank” when one tank covers the road; that try reuses the saved road, so it costs no OSRM call.',
    see: ['concept:rate-limit-retry', 'concept:checks-before-routing', 'concept:field-errors'],
  },

  'concept:rate-limit-retry': {
    title: 'Too many trips in one minute',
    what: 'A cap on requests to /api/route from one client address per minute (any trip, saved or new): sixty by default (RATE_LIMIT_PER_MINUTE).',
    source: 'A 429 rate_limited answer (too many requests) with a Retry-After header, from middleware.py RateLimitMiddleware.',
    how: 'Every request to /api/route counts, before the plan cache is looked at: new or repeated trips, the page’s quiet standard-truck request and each one-click try. Over the cap: 429, with Retry-After = the seconds left in the minute. The Retry button counts down and turns on at zero.',
    why: 'New trips use free public services (the OSRM demo server, Nominatim) whose fair-use rules are about one request per second; a loop could get the server blocked. The page, /api/places and /api/about never leave the server, so they are not limited.',
    code: [
      'fuelroute/middleware.py:RateLimitMiddleware', 'fuelroute/static/fuelroute/js/main.js:WAIT_TITLES',
      'fuelroute/static/fuelroute/js/main.js:retryButton', 'config/settings.py:RATE_LIMIT_PER_MINUTE',
    ],
    edge: 'A fixed one-minute window, counted in each process’s memory and keyed on the client address (REMOTE_ADDR). Behind a proxy it would need the forwarded address from a trusted proxy; with several workers, Redis. upstream_busy (OSRM asking to slow down) uses the same countdown.',
  },

  'concept:swap-button': {
    title: 'The swap button',
    what: 'The arrows between From and To swap the two fields. They do not plan.',
    source: 'The browser only (main.js, the click handler of the swap button): nothing is sent to the server.',
    how: 'The two values are exchanged (a screen reader hears “Press Find fuel stops to plan the reversed trip”); nothing is sent (a test checks it).',
    why: 'The reversed trip is a new road (the road is saved under start;finish, in that order), so planning it at once would spend a routing call you did not ask for.',
    code: [
      'fuelroute/static/fuelroute/js/main.js', 'fuelroute/services/osrm.py:_cache_key',
      'fuelroute/tests/test_web.py:test_swap_does_not_plan_a_new_trip',
    ],
  },

  'concept:example-chips': {
    title: 'The example trips',
    what: 'One-click sample trips.',
    source: 'Fixed in the template (data-start and data-finish).',
    how: 'A click plans the trip at once with the truck in use, as if you typed it and pressed “Find fuel stops”.',
    why: 'Trips that work, without typing, in “City, ST” (no place search). New York → Los Angeles is the brief’s coast-to-coast case: many stops, and a long last stretch into California (its last fuel stop is in Nevada). Chicago → Houston is medium; Dallas → Austin fits in one tank.',
    code: [
      'fuelroute/templates/fuelroute/partials/_step_trip.html', 'fuelroute/static/fuelroute/js/main.js:planTrip',
      'fuelroute/static/fuelroute/js/main.js:currentTruck',
    ],
    edge: 'If your own truck is on screen, the example uses it (the strip above the steps says so).',
  },

  'concept:page-address-url': {
    title: 'The trip lives in the address',
    what: 'The page address holds the trip, so a link reopens the same plan.',
    source: 'The browser (history.pushState); the server only pre-fills the form from the address.',
    how: 'After a plan the address becomes /api/route/map?start=…&finish=…&start_tank=…[&truck settings]#step. Back and Forward plan the trip in the address again; opening a link plans it on load.',
    why: 'Shareable links and working Back and Forward. Reloading a trip already planned costs no external call (plan cache). The server renders only the page shell: it never plans.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:paramsFromUrl', 'fuelroute/static/fuelroute/js/main.js:hashStep',
      'fuelroute/static/fuelroute/js/settings.js:settingsFromQuery', 'fuelroute/web.py:planner_page',
    ],
  },

  'concept:next-step-lock': {
    title: 'Locked steps',
    what: 'The steps after Trip stay locked until a trip is planned.',
    source: 'The browser (steps.js).',
    how: 'The steps start locked; the first good answer unlocks them. Clicking a locked step says “Plan a trip first” and makes the “Find fuel stops” button pulse.',
    why: 'Steps two to six only show values from an answer: opened empty, they would be a page of dashes.',
    code: ['fuelroute/static/fuelroute/js/steps.js:createStepper', 'fuelroute/static/fuelroute/js/main.js:onLocked'],
  },

  'concept:stale-strip': {
    title: '“You changed the trip”',
    what: 'A warning that the results on screen belong to a different trip than the one in the fields.',
    source: 'The browser (main.js updateStrips) compares the fields with the trip on screen.',
    how: 'Shown on the steps after Trip when From or To (without extra spaces) differs from the trip on screen. Checked on every key, swap and change of step.',
    why: 'Editing never plans by itself (each new trip costs a routing call), so the page must say the numbers are from the previous trip.',
    code: ['fuelroute/static/fuelroute/js/main.js:updateStrips'],
    edge: 'Changing only upper and lower case also counts as a change.',
  },

  'concept:planning-timer': {
    title: 'The “Planning…” timer',
    what: 'A stopwatch of how long the browser has been waiting for the answer.',
    source: 'The browser’s clock (performance.now()).',
    how: '(now − start) ÷ 1000, one decimal, redrawn on every frame until the answer arrives. The page gives up after forty seconds (CLIENT_TIMEOUT_MS), with the error “timeout”.',
    why: 'A new trip waits a second or two for the free OSRM server; a live counter shows the page is working. It includes the network, so it is longer than the server time shown later.',
    code: ['fuelroute/static/fuelroute/js/main.js:setLoading', 'fuelroute/static/fuelroute/js/main.js:planTrip'],
    see: ['client.server_ms'],
  },

  'concept:standard-truck-baseline-request': {
    title: 'The second, quiet request',
    what: 'When the truck on screen is not the standard one, the page also asks the API for the same trip with the standard truck.',
    source: 'The browser (main.js rememberBaseline) → GET /api/route with only start, finish and start_tank=empty.',
    how: 'If the trip on screen already uses the standard truck, its answer is reused (no request). Otherwise one more request is sent, once per trip, and kept in memory under “start|finish”.',
    live: (c) => {
      need(answer(c));
      const s = c?.standard;
      if (!s) return 'The standard truck’s answer for this trip is not loaded (yet).';
      return `The standard truck’s answer is loaded: ${fmt.money(s.summary.total_fuel_cost)}, ${plural(s.summary.number_of_stops, 'stop')}; it made ${plural(s.meta?.external_api_calls ?? 0, 'external call')}.`;
    },
    why: 'The road is already saved on the server (its key is the coordinates, not the truck), so this costs no OSRM call and a few milliseconds. Comparisons always use the API’s own numbers, never numbers made up in the browser.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:rememberBaseline', 'fuelroute/static/fuelroute/js/main.js:standardBody',
      'fuelroute/static/fuelroute/js/main.js:countCalls', 'fuelroute/services/osrm.py:_cache_key',
    ],
    edge: 'If the standard truck cannot make the trip, the comparison says it is not available.',
  },

  'concept:price-file': {
    title: 'The price file',
    what: 'The price table (a CSV file) given with the assignment: OPIS ID (the station’s ID at OPIS, a fuel-price publisher), name, address, city, state, Rack ID (the wholesale terminal behind the price; not used) and Retail Price (the pump price per gallon, used). No coordinates, no dates.',
    source: '/api/about → data.price_file (the file name). manage.py load_stations loads it into the station table once, never during a request.',
    how: 'A station sits at its exact place matched in OpenStreetMap, else at its city center from the built-in place list. Same-name towns far apart in one state: the exit number, the population, then the highway decide. If no clue decides and there is no exact place, it is left out.',
    live: (c) => {
      const d = aboutData(c);
      need(d.stations, d.geocoded);
      const exactText = present(d.exact_positions) ? ` (${fmt.int(d.exact_positions)} of them at their exact place)` : '';
      return [
        `${d.price_file}: ${fmt.int(d.price_quotes)} price quotes for ${fmt.int(d.stations)} US truck stops; ${fmt.int(d.stations_with_several_quotes)} of them are listed more than once.`,
        `${fmt.int(d.geocoded)} placed on the map${exactText}, ${fmt.int(d.not_geocoded)} left out.`,
      ];
    },
    why: 'Placing every station once, when the file is loaded, keeps every request at zero place-search calls. A wrong guess would put a phantom stop hundreds of miles away on other people’s routes; leaving it out only removes one option.',
    code: [
      'fuelroute/services/station_loader.py:parse_price_file', 'fuelroute/services/station_loader.py:load_stations',
      'fuelroute/services/station_loader.py:geocode_rows', 'fuelroute/services/station_loader.py:read_station_coords',
      'fuelroute/management/commands/load_stations.py', 'fuelroute/services/about.py:_data_summary',
    ],
    edge: 'Repeated rows of one OPIS ID are one station, priced with the median; rows outside the US are skipped. Stations kept at their city center can be miles off, so the search around the road is several miles wide. With an empty table /api/route answers station_data_not_loaded.',
    see: ['concept:exact-positions', 'concept:station-price', 'concept:offline-places-list', 'about.data.geocoded'],
  },

  'concept:exact-positions': {
    title: 'Exact truck stop positions',
    what: 'Where each truck stop sits on the map: at its own fuel station in OpenStreetMap (the free, open world map), else at the highway exit its address names, else at its city center.',
    source: 'data/station_coords.csv, built once, offline, by scripts/build_station_coords.py from OpenStreetMap data (its rules and results: data/station_coords_report.md). manage.py load_stations reads it; a request never does.',
    how: 'Matched once, ahead of time: the station’s own fuel station (its brand next to the exit in its address, or its store number), else that exit itself. A doubtful match, like a brand alone or an exit over twenty miles from town, is dropped: the station keeps its city center.',
    live: (c) => {
      const d = aboutData(c);
      need(d.exact_positions, d.stations);
      const stops = stopsOf(c).filter((s) => present(s.distance_from_route_miles));
      const near = stops.filter((s) => s.distance_from_route_miles < 1).length;
      return [
        stops.length ? `Stops less than a mile from the road on this trip (straight line): ${fmt.int(near)} of ${fmt.int(stops.length)}.` : null,
        `In the whole price file: ${fmt.int(d.exact_positions)} of ${fmt.int(d.stations)} US truck stops at their exact place; the others at their city center, or left out.`,
      ];
    },
    why: 'The price file has no coordinates, only a city and an address, often a highway exit (“I-44, EXIT 283”), so a city center can be miles from the stop. Matching once, offline, means no map call during a request; an evaluator needs nothing: the file is in the repository.',
    code: [
      'scripts/build_station_coords.py:main', 'scripts/build_station_coords.py:match_station',
      'scripts/build_station_coords.py:find_exit', 'scripts/build_station_coords.py:match_state',
      'fuelroute/services/station_loader.py:read_station_coords', 'fuelroute/services/station_loader.py:_trusted',
      'fuelroute/services/station_loader.py:load_stations',
      'fuelroute/tests/test_build_station_coords.py:test_without_exit_a_name_alone_is_not_enough',
      'fuelroute/tests/test_station_coords.py:test_an_exact_row_beats_the_city_center',
    ],
    edge: 'About half the stations are exact; the rest keep their city center, an error the search strip around the road mostly absorbs. data/station_coords_report.md lists the largest moves and why each other station kept its city center. To refresh: python scripts/build_station_coords.py, then python manage.py load_stations.',
    see: ['about.data.exact_positions', 'concept:detour-estimate', 'concept:price-file', 'route.vehicle.corridor_miles'],
  },

  'about.data.geocoded': {
    title: 'Truck stops the planner can use',
    what: 'How many truck stops of the price file have a position on the map. Only these can be used in a plan.',
    source: '/api/about → data.geocoded, embedded in the page when it loads (the page does not ask again).',
    how: 'The number of rows of the station table that have a latitude, shown as a whole number.',
    live: (c) => {
      const d = aboutData(c);
      need(d.geocoded, d.stations);
      return [
        `${fmt.int(d.geocoded)} placed of ${fmt.int(d.stations)} US truck stops: ${fmt.int(d.stations)} − ${fmt.int(d.geocoded)} = ${fmt.int(d.not_geocoded)} left out.`,
        present(d.exact_positions)
          ? `Of the placed ones, ${fmt.int(d.exact_positions)} at their exact place and ${fmt.int(d.geocoded - d.exact_positions)} at their city center.`
          : null,
      ];
    },
    why: 'Only stations with a position can be placed near a road, so this is the honest count. Positions are found when the file is loaded, never during a request. With no exact place and an unresolved city, a station stays off the map: better than hundreds of miles away.',
    code: [
      'fuelroute/services/about.py:_data_summary', 'fuelroute/services/about.py:build_about',
      'fuelroute/web.py:planner_page', 'fuelroute/services/station_loader.py:geocode_rows',
      'fuelroute/services/station_loader.py:read_station_coords', 'fuelroute/services/stations.py:get_station_arrays',
    ],
    edge: 'Counted from the database, never typed: with an empty table it shows zero.',
    see: ['about.data.exact_positions', 'concept:price-file'],
  },

  'about.data.exact_positions': {
    title: 'Truck stops at their exact place',
    what: 'How many truck stops of the price file sit at their own fuel station or at the highway exit in their address, not at their city center.',
    source: '/api/about → data.exact_positions, embedded in the page when it loads; data.geocoded_by_source splits it.',
    how: 'Counted in the station table: stations placed by data/station_coords.csv, at their own fuel station in OpenStreetMap (osm_fuel) or at the exit in their address (osm_exit). The others sit at their city center (census or geonames) or are left out.',
    live: (c) => {
      const d = aboutData(c);
      need(d.exact_positions, d.stations);
      const by = d.geocoded_by_source || {};
      return [
        d.stations > 0
          ? `${fmt.int(d.exact_positions)} of ${fmt.int(d.stations)} US truck stops: ${fmt.int(d.exact_positions)} ÷ ${fmt.int(d.stations)} × 100 = ${fmt.pct((d.exact_positions / d.stations) * 100)}.`
          : `${fmt.int(d.exact_positions)} truck stops.`,
        present(by.osm_fuel) || present(by.osm_exit)
          ? `${fmt.int(by.osm_fuel || 0)} at their own fuel station, ${fmt.int(by.osm_exit || 0)} at the exit in their address.`
          : null,
        present(d.geocoded) ? `${fmt.int(d.geocoded - d.exact_positions)} at their city center, ${fmt.int(d.not_geocoded)} left out.` : null,
      ];
    },
    why: 'A count from the database, never typed, so it shows what the server really loaded. Without the positions file it is zero and every stop sits at its city center, as before.',
    code: [
      'fuelroute/services/about.py:_data_summary', 'fuelroute/services/station_loader.py:EXACT_SOURCES',
      'fuelroute/services/station_loader.py:load_stations', 'fuelroute/management/commands/load_stations.py',
      'fuelroute/models.py:FuelStation',
    ],
    edge: 'It counts the whole file, not this trip. python manage.py load_stations --no-coords places every station by its city alone.',
    see: ['concept:exact-positions', 'about.data.geocoded'],
  },

  'about.data.states': {
    title: 'States with fuel prices',
    what: 'How many US states have at least one truck stop in the price file.',
    source: '/api/about → data.states.',
    how: 'The number of different state codes in the station table. Only US states are stored (Canadian rows are skipped when the file is loaded).',
    live: (c) => {
      const d = aboutData(c);
      need(d.states);
      const rows = d.stations_by_state || [];
      // The server sorts by stations in the file; the fewest PLACED need their own sort.
      const fewest = [...rows].sort((a, b) => a.geocoded - b.geocoded || a.state.localeCompare(b.state))
        .slice(0, 3).map((r) => `${r.state} ${fmt.int(r.geocoded)}`).join(', ');
      return [`${fmt.int(d.states)} states.`, fewest ? `Fewest placed truck stops: ${fewest}.` : null];
    },
    why: 'It says how far the data reaches before you type a trip. “None in Alaska or Hawaii” is true for this file: the planner refuses those states before any routing call.',
    code: [
      'fuelroute/services/about.py:_data_summary', 'fuelroute/services/station_loader.py:load_stations',
      'fuelroute/services/planner.py:_check_endpoints',
    ],
    edge: 'The words “mainland” and “none in Alaska or Hawaii” are written in the template; a different price file could make them wrong.',
  },

  'derived.ca_stations': {
    title: 'Truck stops in California',
    what: 'How many usable truck stops the price file has in California. It explains why some trips in California cannot be priced.',
    source: 'Computed in the browser (format.js derive) from /api/about → data.stations_by_state.',
    how: 'Find the row of state CA and take its number of stations with a position. The sentence hides itself when it is zero.',
    live: (c) => {
      const row = (aboutData(c).stations_by_state || []).find((r) => r.state === 'CA');
      need(row);
      return `CA: ${fmt.int(row.stations)} in the file, ${fmt.int(row.geocoded)} with a position.`;
    },
    why: 'California is the biggest gap in the data: a trip from Los Angeles has a long first stretch, and some trips inside California get no_fuel_data_on_route. The count comes from the data, so it cannot go out of date (the words “south-east corner” are written by hand).',
    code: [
      'fuelroute/static/fuelroute/js/format.js:stateStations', 'fuelroute/static/fuelroute/js/format.js:derive',
      'fuelroute/static/fuelroute/js/main.js:renderError', 'fuelroute/services/about.py:_data_summary',
    ],
  },

  'about.vehicle.miles_per_gallon': {
    title: 'Miles per gallon (standard truck)',
    what: 'How many miles the standard truck drives on one gallon. The brief fixes it.',
    source: '/api/about → vehicle.miles_per_gallon ← MILES_PER_GALLON in the configuration (settings.FUEL_PLANNER, an environment variable).',
    how: 'Copied from the configuration, shown with up to two decimals.',
    live: (c) => {
      const v = aboutVehicle(c);
      need(v.miles_per_gallon, v.max_range_miles);
      return `${fmt.num(v.miles_per_gallon)} miles per gallon: the ${fmt.miles_short(v.max_range_miles)} range ÷ ${fmt.num(v.miles_per_gallon)} = a ${fmt.gal(v.tank_gallons)} tank.`;
    },
    why: 'It lives in one place, the configuration, and the page reads it, so the page cannot say one number while the server plans with another. A request can try another value with mpg (the Truck step does).',
    code: [
      'config/settings.py:FUEL_PLANNER', 'fuelroute/services/about.py:_vehicle',
      'fuelroute/services/planner.py:PlanSettings.defaults', 'fuelroute/static/fuelroute/js/format.js:fmt',
    ],
    edge: 'A request with another mpg does not change this value: it is the standard truck’s.',
    see: ['concept:miles-per-gallon', 'about.vehicle.tank_gallons'],
  },

  'about.vehicle.tank_gallons': {
    title: 'Tank size (standard truck)',
    what: 'How many gallons the standard truck’s tank holds.',
    source: '/api/about → vehicle.tank_gallons, computed on the server from two settings.',
    how: 'tank = range on a full tank ÷ miles per gallon, rounded to one decimal.',
    live: (c) => {
      const v = aboutVehicle(c);
      need(v.max_range_miles, v.miles_per_gallon, v.tank_gallons);
      return `${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.gal(v.tank_gallons)}.`;
    },
    why: 'The brief gives a range and a fuel use, not a tank, so the tank is derived from them. People think in tank size, so the Truck step asks for it and sends range = tank × miles per gallon.',
    code: [
      'fuelroute/services/about.py:_vehicle', 'fuelroute/services/planner.py:PlanSettings.tank_gallons',
      'fuelroute/static/fuelroute/js/settings.js:rangeOfTank', 'fuelroute/static/fuelroute/js/settings.js:tankGallons',
    ],
    edge: 'If the configured range ÷ mpg is not round (five hundred ÷ seven, say), /api/about shows one decimal and a route answer two.',
    see: ['concept:tank-size'],
  },

  'about.vehicle.max_range_miles': {
    title: 'Range on a full tank (standard truck)',
    what: 'How far the standard truck drives on one full tank. The brief fixes it.',
    source: '/api/about → vehicle.max_range_miles ← MAX_RANGE_MILES in the configuration.',
    how: 'Copied from the configuration.',
    live: (c) => {
      const v = aboutVehicle(c);
      need(v.max_range_miles, v.tank_gallons, v.miles_per_gallon);
      return `${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg = a ${fmt.gal(v.tank_gallons)} tank.`;
    },
    why: 'It is the plan’s hard limit: no stretch between two purchases may be longer than one tank (less any safety fuel). A request can try another range with max_range_miles.',
    code: [
      'config/settings.py:FUEL_PLANNER', 'fuelroute/services/about.py:_vehicle',
      'fuelroute/services/planner.py:WHAT_IF_RANGES', 'fuelroute/services/planner.py:PlanSettings.usable_range_miles',
    ],
    edge: 'A stretch of road with no truck stop longer than the usable range is a 422 no_reachable_fuel_station (valid input, but this truck cannot make the trip).',
    see: ['route.vehicle.usable_range_miles'],
  },

  'derived.your_truck_text': {
    title: 'Your truck, in words',
    what: 'A one-line description of the truck the numbers on screen were planned with, when it is not the standard truck.',
    source: 'Computed in the browser (format.js yourTruckText) from the vehicle block of the answer on screen, plus settings a link brought.',
    how: 'Joined with commas: miles per gallon; “a N-gallon tank (range)”; the safety fuel if any; “full tank at the start” if start_tank=full; “tiny stops skipped” if consolidate=true; then the settings with no control (search distance, price policy).',
    live: (c) => {
      const v = veh(c);
      need(v.miles_per_gallon, v.tank_gallons, v.max_range_miles);
      return [
        `This answer’s vehicle: ${fmt.num(v.miles_per_gallon)} mpg; tank ${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} = ${fmt.gal(v.tank_gallons)}; safety fuel ${fmt.gal(safetyOf(c))}; start_tank ${v.start_tank}; consolidate ${v.consolidate}.`,
        `Different from the standard truck: ${(metaOf(c).settings_changed || []).join(', ') || 'nothing'}.`,
      ];
    },
    why: 'It reads the settings the server really used (the answer’s vehicle block), not the form, so the words always match the numbers. In the Trip step it warns that your next trip will use this truck.',
    code: [
      'fuelroute/static/fuelroute/js/format.js:yourTruckText', 'fuelroute/static/fuelroute/js/format.js:hiddenSettingsText',
      'fuelroute/static/fuelroute/js/main.js:updateStrips', 'fuelroute/static/fuelroute/js/main.js:currentTruck',
      'fuelroute/services/planner.py:PlanSettings.changed',
    ],
    edge: '“Back to the standard truck” clears every setting, those of a link too. “Plan another trip” also goes back to the standard truck.',
    see: ['concept:standard-truck'],
  },

  // ----- step 2: the route -----
  'route.start.label': START_LABEL,
  'route.finish.label': FINISH_LABEL,
  'route.route.distance_miles': DISTANCE,

  'route.summary.fuel_used_gallons': {
    title: 'Fuel the trip burns',
    what: 'How many gallons the truck burns to drive the whole road.',
    source: 'API field summary.fuel_used_gallons.',
    how: 'fuel burned = distance ÷ miles per gallon, rounded to two decimals (from the unrounded distance).',
    live: (c) => {
      const r = answer(c);
      need(r?.route?.distance_miles, veh(c).miles_per_gallon, summ(c).fuel_used_gallons);
      return `${fmt.miles(r.route.distance_miles)} ÷ ${fmt.num(veh(c).miles_per_gallon)} mpg = ${fmt.gal(summ(c).fuel_used_gallons)}`;
    },
    why: 'The brief gives a fixed miles per gallon, so fuel grows in a straight line with distance. With the default start (almost empty, arrive with the same) the plan buys exactly this much, so it explains the size of the bill.',
    code: ['fuelroute/services/planner.py:_build_summary', 'fuelroute/templates/fuelroute/partials/_step_route.html'],
    edge: 'Dividing the rounded distance by hand can differ by a hundredth of a gallon. Load, hills, idling and speed are not modelled.',
    see: ['route.vehicle.miles_per_gallon', 'concept:fuel-in-miles-of-range'],
  },

  'route.vehicle.miles_per_gallon': {
    title: 'Miles per gallon (this truck)',
    what: 'How many miles the truck of this answer drives on one gallon.',
    source: 'API field vehicle.miles_per_gallon: the request’s mpg, or the default from the configuration.',
    how: 'The request’s mpg (allowed range in /api/about), else the configured default (MILES_PER_GALLON).',
    live: (c) => {
      need(veh(c).miles_per_gallon);
      const changed = (metaOf(c).settings_changed || []).includes('mpg');
      return `${fmt.num(veh(c).miles_per_gallon)} mpg${changed ? ' (changed in the Truck step)' : ' (the default)'}.`;
    },
    why: 'Fixed by the brief, but a parameter so the Truck step can try other trucks. A changed truck reuses the saved road: no OSRM call.',
    code: [
      'fuelroute/services/planner.py:PlanSettings.defaults', 'fuelroute/services/planner.py:PlanSettings.vehicle',
      'config/settings.py:FUEL_PLANNER',
    ],
    edge: 'In the JSON it is miles_per_gallon; in the request it is mpg.',
    see: ['concept:miles-per-gallon'],
  },

  'route.summary.candidate_stations_on_route': {
    title: 'Truck stops near the road',
    what: 'How many truck stops of the price file are close enough to the road to be used.',
    source: 'API field summary.candidate_stations_on_route.',
    how: 'Placed stations live in numpy arrays (numpy: fast maths on arrays of numbers). Three passes: a box around the road; a coarse pass against one road point every twenty miles (COARSE_STEP_MILES); then the exact distance to the nearest one-mile road point, which must be inside the USA.',
    live: (c) => {
      const s = summ(c);
      need(s.candidate_stations_on_route, veh(c).corridor_miles);
      const placed = aboutData(c).geocoded;
      return `${fmt.int(s.candidate_stations_on_route)}${present(placed) ? ` of ${fmt.int(placed)} placed truck stops` : ' truck stops'} are within ${fmt.miles_short(veh(c).corridor_miles)} of this ${fmt.miles(answer(c).route.distance_miles)} road.`;
    },
    why: 'No spatial database is needed: numpy does the exact search on a few hundred stations instead of thousands, in a few tens of milliseconds (about thirty on New York → Los Angeles). A test compares it with a brute-force search (checking every station).',
    code: [
      'fuelroute/services/stations.py:stations_along_route', 'fuelroute/services/stations.py:get_station_arrays',
      'fuelroute/services/geo.py:nearest_on_line', 'fuelroute/services/geo.py:within_distance',
      'fuelroute/tests/test_geo.py:test_corridor_search_matches_brute_force',
    ],
    edge: 'The coarse margin, half that spacing plus a mile, drops no real candidate. None near the road: 422 no_fuel_data_on_route, unless the tank covers the trip. Hundreds of thousands of stations would call for PostGIS (a spatial database); today’s six and a half thousand fit in memory.',
    see: ['route.vehicle.corridor_miles', 'concept:mile-marker'],
  },

  'route.vehicle.corridor_miles': {
    title: 'Search distance from the road (corridor)',
    what: 'The widest straight-line distance from the road at which a truck stop still counts as on the way. The “corridor” is this strip along the road.',
    source: 'API field vehicle.corridor_miles ← CORRIDOR_MILES in the configuration (or the request’s corridor_miles).',
    how: 'A station is kept if its distance to the nearest point of the road is at most this. The page has no control for it: only a link or an API call can change it.',
    live: (c) => {
      need(veh(c).corridor_miles);
      const changed = (metaOf(c).settings_changed || []).includes('corridor_miles');
      return `${fmt.miles_short(veh(c).corridor_miles)}${changed ? ' (from the link)' : ' (the default)'}.`;
    },
    why: 'Many stations still sit at their city center (the file has no coordinates), often miles from the real stop, so this margin keeps most of them in reach.',
    code: [
      'config/settings.py:FUEL_PLANNER', 'fuelroute/services/planner.py:PlanSettings.defaults',
      'fuelroute/services/stations.py:stations_along_route', 'fuelroute/static/fuelroute/js/settings.js:SETTINGS',
    ],
    edge: 'The detour to the station is not charged (the stop’s card estimates it): a station at the edge counts as if it were on the road. A wider strip finds more stations but hides longer detours.',
    see: ['concept:detour-estimate', 'concept:exact-positions'],
  },

  'route.summary.start_fuel_gallons': {
    title: 'Fuel when the truck leaves',
    what: 'The fuel in the tank when the truck leaves.',
    source: 'API field summary.start_fuel_gallons, set by the tank rules (planner._tank_rules).',
    how: 'Almost empty (the default): the larger of the start reserve (fifty miles, START_RESERVE_MILES) and the miles to the first truck stop plus the safety fuel, at most a tank; lowered when the last truck stop is far from the finish. Full: a full tank. Gallons = miles ÷ mpg.',
    live: (c) => {
      const s = summ(c);
      const v = veh(c);
      need(s.start_fuel_gallons, v.miles_per_gallon);
      if (v.start_tank === 'full') return `A full tank: ${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.gal(s.start_fuel_gallons)}, free.`;
      const miles = s.start_fuel_gallons * v.miles_per_gallon;
      const reserve = aboutVehicle(c).start_reserve_miles;
      const lines = [`${fmt.gal(s.start_fuel_gallons)} × ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.miles_short(miles)} of driving.`];
      if (present(reserve)) lines.push(startReasonLine(c, miles, reserve));
      return [...lines, tankSentence(c)];
    },
    why: '“Pay for every mile”: the truck leaves and arrives with the same fuel, so the bill is the fuel the trip burns. It cannot leave with nothing: it must reach a truck stop. The brief does not say how full the tank is, so “full” is offered too.',
    code: [
      'fuelroute/services/planner.py:_tank_rules', 'fuelroute/services/planner.py:_build_summary',
      'config/settings.py:START_RESERVE_MILES',
    ],
    edge: 'Los Angeles: the first truck stop of the file is in Nevada, so the truck leaves with much more, and a warning says so. If the last truck stop is far from the finish, it may leave with less.',
    see: ['concept:pay-for-every-mile', 'concept:tank-when-leaving'],
  },

  'derived.start_fuel_miles': {
    title: 'Starting fuel, in miles',
    what: 'How far the starting fuel takes the truck.',
    source: 'Computed in the browser (format.js derive) from two API fields.',
    how: 'start_fuel_gallons × miles_per_gallon.',
    live: (c) => {
      const s = summ(c);
      need(s.start_fuel_gallons, veh(c).miles_per_gallon);
      return `${fmt.gal(s.start_fuel_gallons)} × ${fmt.num(veh(c).miles_per_gallon)} mpg = ${fmt.miles_short(s.start_fuel_gallons * veh(c).miles_per_gallon)}.`;
    },
    why: 'Gallons alone do not say why that amount; in miles you see it is enough to reach a truck stop. No number is invented: it is arithmetic on two API fields.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/tests/test_js_logic.py:test_derived_values_agree_in_number_and_name_the_truck'],
  },

  'route.summary.end_fuel_gallons': {
    title: 'Fuel when the truck arrives',
    what: 'The fuel left in the tank at the finish, according to the plan.',
    source: 'API field summary.end_fuel_gallons.',
    how: 'The tank rules set the minimum: almost empty → what it left with (unless the last truck stop is far from the finish); full → nothing, or the safety fuel. The last stop buys just enough. With a full tank and no stop needed: tank − fuel burned.',
    live: (c) => {
      const s = summ(c);
      need(s.start_fuel_gallons, s.end_fuel_gallons);
      const same = Math.abs(s.end_fuel_gallons - s.start_fuel_gallons) < HALF_HUNDREDTH;
      return [`Leaves with ${fmt.gal(s.start_fuel_gallons)}, arrives with ${fmt.gal(s.end_fuel_gallons)}: `
        + `${same ? 'the same, so every gallon burned was bought' : `a difference of ${fmt.gal(Math.abs(s.start_fuel_gallons - s.end_fuel_gallons))}`}.`, tankSentence(c)];
    },
    why: 'Showing both numbers proves the books balance: the same fuel out and in means every gallon burned was paid for.',
    code: [
      'fuelroute/services/planner.py:_build_summary', 'fuelroute/services/planner.py:_tank_rules',
      'fuelroute/services/optimizer.py:plan_fuel_stops',
    ],
    edge: 'Lower than the start fuel (almost empty mode): that difference could not be priced, because no truck stop is near the end of the road; a warning says so.',
    see: ['concept:pay-for-every-mile'],
  },

  'route.vehicle.max_range_miles': {
    title: 'Range on a full tank (this truck)',
    what: 'How far the truck of this answer goes on a full tank.',
    source: 'API field vehicle.max_range_miles: the request’s max_range_miles, or MAX_RANGE_MILES.',
    how: 'The request’s max_range_miles, else the configured default (MAX_RANGE_MILES); the tank shown is range ÷ mpg. The page compares the road with the usable range: longer → “must stop on the way”; shorter, but leaving almost empty → “buys the fuel on the way”; nothing to buy → “no stop needed”.',
    live: (c) => {
      const v = veh(c);
      const r = answer(c);
      need(v.max_range_miles, v.tank_gallons, v.usable_range_miles, r?.route?.distance_miles);
      const d = der(c);
      const why = d.trip_over_range ? 'longer than one usable tank, so the truck must stop'
        : d.fits_but_starts_low ? 'one tank would do, but the truck leaves almost empty, so it buys on the way'
          : d.no_stops ? 'the fuel in the tank covers it' : null;
      return [`${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg = a ${fmt.gal(v.tank_gallons)} tank; usable ${fmt.miles_short(v.usable_range_miles)}.`,
        why ? `The road is ${fmt.miles(r.route.distance_miles)}: ${why}.` : null];
    },
    why: 'The range is the plan’s hard limit: no stretch between two purchases is longer than one usable tank.',
    code: ['fuelroute/services/planner.py:PlanSettings', 'config/settings.py:FUEL_PLANNER', 'fuelroute/static/fuelroute/js/format.js:derive'],
    edge: 'With no safety fuel the plan may reach a truck stop with an empty tank: the range is taken literally.',
    see: ['route.vehicle.usable_range_miles'],
  },

  'route.vehicle.safety_reserve_gal': {
    title: 'Safety fuel',
    what: 'Fuel the truck must never go below, at any stop or at the finish.',
    source: 'API field vehicle.safety_reserve_gal (the request’s safety_reserve_gal; none by default).',
    how: 'The optimizer plans with a smaller tank: usable range = range − safety fuel × miles per gallon. It adds the safety fuel back to every fuel level it reports.',
    live: (c) => {
      const v = veh(c);
      need(v.max_range_miles, v.miles_per_gallon, v.usable_range_miles);
      const kept = safetyOf(c) * v.miles_per_gallon;
      return `${fmt.gal(safetyOf(c))} × ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.miles_short(kept)} kept; ${fmt.miles_short(v.max_range_miles)} − ${fmt.miles_short(kept)} = ${fmt.miles_short(v.usable_range_miles)} usable.`;
    },
    why: 'None by default follows the brief literally. Safety fuel is the same problem with a smaller tank, so the same three rules and the same proof still apply (a test checks it).',
    code: [
      'fuelroute/services/planner.py:PlanSettings.check', 'fuelroute/services/optimizer.py:_usable',
      'fuelroute/services/planner.py:_optimize', 'fuelroute/tests/test_optimizer.py:test_safety_reserve_is_the_optimum_of_a_smaller_tank',
    ],
    edge: 'More safety fuel shortens each tank: a gap between truck stops can then become a 422 no_reachable_fuel_station (this truck cannot make the trip).',
    see: ['concept:safety-fuel', 'route.vehicle.usable_range_miles'],
  },

  'route.vehicle.usable_range_miles': USABLE_RANGE,

  'derived.route_price_min': {
    title: 'Cheapest price near the road',
    what: 'The lowest price per gallon among the truck stops near the road.',
    source: 'API field summary.price_per_gallon_on_route.min, read by format.js derive.',
    how: 'The minimum, over the truck stops near the road, of each stop’s price (the median of its quotes by default), rounded to four decimals; the page shows three.',
    live: (c) => {
      const p = summ(c).price_per_gallon_on_route;
      need(p?.min, p?.max);
      return `Cheapest ${price4(p.min)}, dearest ${price4(p.max)}, among ${fmt.int(summ(c).candidate_stations_on_route)} truck stops: ${price4(p.max)} − ${price4(p.min)} = ${price4(p.max - p.min)} a gallon apart.`;
    },
    why: 'The spread between the cheapest and the dearest truck stop is why choosing where to stop saves money.',
    code: [
      'fuelroute/services/planner.py:_price_stats', 'fuelroute/services/planner.py:_build_summary',
      'fuelroute/services/station_loader.py:StationRow.price', 'fuelroute/static/fuelroute/js/format.js:derive',
    ],
    edge: 'The cheapest station may never be used if it is in a bad place on the road (for example just after a fill-up).',
    see: ['concept:station-price'],
  },

  'derived.route_price_max': {
    title: 'Dearest price near the road',
    what: 'The highest price per gallon among the truck stops near the road.',
    source: 'API field summary.price_per_gallon_on_route.max, read by format.js derive.',
    how: 'The maximum, over the truck stops near the road, of each stop’s price, rounded to four decimals; the page shows three.',
    live: (c) => EXPLAINERS['derived.route_price_min'].live(c),
    why: 'With the minimum, it shows the money at stake on this road.',
    code: ['fuelroute/services/planner.py:_price_stats', 'fuelroute/static/fuelroute/js/format.js:derive'],
    edge: 'price_policy=max uses each station’s highest quote, so this changes with that setting.',
    see: ['concept:station-price'],
  },

  'derived.calls_osrm_text': {
    title: 'Requests to OSRM for this answer',
    what: 'How many requests to OSRM (the free routing service) this one answer made, in words.',
    source: 'Computed in the browser from the API field meta.external_api_services (one entry per attempt).',
    how: 'Count the “osrm” entries of meta.external_api_services. Which sentence shows depends on the caches: plan cache hit → “came from memory”; road cache hit → “only the fuel plan was redone”; both missed → a new trip.',
    live: (c) => {
      const m = metaOf(c);
      need(m.plan_cache);
      return `plan cache: ${m.plan_cache}, road cache: ${m.route_cache}, external_api_services = [${(m.external_api_services || []).join(', ')}] → ${der(c).calls_osrm_text} to OSRM.`;
    },
    why: 'The brief asks to call the routing API as little as possible, so every answer reports what it spent.',
    code: [
      'fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/static/fuelroute/js/format.js:plural',
      'fuelroute/services/http.py:ExternalApiClient.get_json', 'fuelroute/services/planner.py:_meta',
    ],
    edge: 'A retry after a failure (one by default, HTTP_RETRIES) counts as a request, so a bad moment of OSRM can show two. It counts this answer only; the For Spotter step adds up every truck tried.',
    see: ['concept:external-calls', 'concept:route-cache-vs-plan-cache', 'concept:osrm'],
  },

  'derived.calls_other_text': {
    title: 'Requests to Nominatim',
    what: 'How many requests this answer made to Nominatim, a free place search, to find a typed place.',
    source: 'Computed in the browser: meta.external_api_calls minus the OSRM entries of meta.external_api_services.',
    how: 'other = external_api_calls − (number of “osrm” entries). Shown only when it is above zero.',
    live: (c) => {
      const m = metaOf(c);
      need(m.external_api_calls);
      const osrm = (m.external_api_services || []).filter((s) => s === 'osrm').length;
      return `${fmt.int(m.external_api_calls)} external − ${fmt.int(osrm)} OSRM = ${fmt.int(m.external_api_calls - osrm)} Nominatim.`;
    },
    why: '“City, ST” never calls Nominatim; free text (a street address) does, once, and the answer is saved. The page names that extra call.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/services/geocoding.py:_ask_nominatim'],
    edge: 'Nominatim answers are saved for a day, so the same trip again shows no Nominatim call.',
    see: ['concept:geocoding'],
  },

  'client.server_ms': {
    title: 'Server time',
    what: 'How long the server took to produce the answer on screen.',
    source: 'The X-Response-Time-ms header of the answer, read by the browser (the “client” root).',
    how: 'The outermost middleware (code that wraps every request) measures the whole request on the server, JSON included, and writes milliseconds in a header. Under a tenth of a second the page says so; otherwise seconds with one decimal.',
    live: (c) => {
      need(c?.client?.server_ms);
      const m = metaOf(c);
      return [`X-Response-Time-ms: ${fmt.dec1(c.client.server_ms)} milliseconds → “${fmt.seconds(c.client.server_ms)}”.`,
        m.plan_cache ? `This answer: plan cache ${m.plan_cache}, road cache ${m.route_cache}.` : null];
    },
    why: 'Measured on the server, it is the number Postman or curl get, and your network does not change it. A new trip is mostly waiting for OSRM; a trip asked again takes a few milliseconds.',
    code: [
      'fuelroute/middleware.py:ResponseTimeMiddleware', 'fuelroute/static/fuelroute/js/api.js:request',
      'fuelroute/static/fuelroute/js/main.js:context', 'fuelroute/tests/test_js_logic.py:test_server_time_is_said_in_seconds',
    ],
    edge: 'It leaves out the network and drawing the page, so the “Planning…” timer is longer. It is the answer on screen (your truck’s), not the standard truck’s.',
    see: ['concept:server-timing', 'concept:planning-timer', 'concept:warm-up-in-memory'],
  },

  'route.warnings': {
    title: 'Good to know',
    what: 'Plain sentences about assumptions or gaps in the data that affect this trip.',
    source: 'API field warnings (a list of sentences).',
    how: 'Three kinds: a place found by Nominatim (it says what the text matched); the tank rules (first truck stop beyond the start reserve, last truck stop too far from the finish, or no truck stop near the road); and a road that runs outside the USA.',
    live: (c) => {
      need(answer(c));
      return `${plural((answer(c).warnings || []).length, 'warning')} on this trip.`;
    },
    why: 'Every assumption the code makes about the data is said out loud, so a surprising number has its reason next to it.',
    code: [
      'fuelroute/services/planner.py:_warnings', 'fuelroute/services/planner.py:_geocoder_warnings',
      'fuelroute/services/planner.py:_tank_rules', 'fuelroute/static/fuelroute/js/plan.js:renderWarnings',
    ],
    edge: 'Most “City, ST” trips in the lower 48 states have none, so the box is hidden.',
    see: ['concept:miles-outside-usa'],
  },

  'concept:pay-for-every-mile': {
    title: 'The tank: pay for every mile',
    what: 'The default assumption (start_tank=empty): the bill is the cost of all the fuel the trip burns.',
    source: 'A server rule (planner._tank_rules). The wording of the two modes is the API’s own (START_TANK_LABELS and START_TANK_HELP).',
    how: 'It leaves with a small reserve (or enough to reach the first truck stop) and must arrive with the same: bought = burned. The page says one of three things: the same fuel out and in; less on arrival (part of the road has no price); a free full tank.',
    live: (c) => {
      const s = summ(c);
      need(s.start_fuel_gallons, s.end_fuel_gallons);
      return [`Leaves with ${fmt.gal(s.start_fuel_gallons)}, arrives with ${fmt.gal(s.end_fuel_gallons)}, burns ${fmt.gal(s.fuel_used_gallons)}, buys ${fmt.gal(s.total_gallons_purchased)}.`, tankSentence(c)];
    },
    why: 'The brief asks for the total money spent on fuel for the trip. Starting full would hide up to a tank of fuel; starting at zero is impossible (the truck must reach a truck stop). Returning the tank as you got it makes trips comparable.',
    code: [
      'fuelroute/services/planner.py:_tank_rules', 'fuelroute/services/planner.py:START_TANK_HELP',
      'fuelroute/static/fuelroute/js/format.js:derive', 'config/settings.py:START_RESERVE_MILES',
    ],
    edge: 'In the API the value is named empty; it means almost empty: fifty miles of fuel by default (START_RESERVE_MILES), or enough to reach the first truck stop.',
    see: ['concept:tank-when-leaving', 'route.summary.start_fuel_gallons', 'concept:check-every-mile-paid'],
  },

  'concept:osrm': {
    title: 'OSRM, the road',
    what: 'OSRM (Open Source Routing Machine) is a free routing engine on OpenStreetMap roads. The app calls its public demo server once per new trip.',
    source: 'An external API at OSRM_URL (router.project-osrm.org by default).',
    how: 'One request: GET /route/v1/driving/{start};{finish}. The answer has the distance, the duration and the road’s shape as an encoded line (the points packed into text). The server decodes it, puts a point every mile, marks the points inside the USA, simplifies it for the map and saves that prepared road.',
    live: (c) => {
      const m = metaOf(c);
      need(m.route_cache);
      return m.route_cache === 'hit' ? 'This answer reused the saved road: no OSRM call.' : `This answer asked OSRM: ${der(c).calls_osrm_text}.`;
    },
    why: 'Free, no key, and one call gives the distance, the time and the road’s shape. Kept cheap and safe: the connection is reused, a slow answer times out and is tried once more, identical requests wait for the first one, and a per-minute limit protects the free server.',
    code: [
      'fuelroute/services/osrm.py:get_route', 'fuelroute/services/osrm.py:route_url',
      'fuelroute/services/osrm.py:decode_polyline', 'fuelroute/services/osrm.py:prepare_route',
      'fuelroute/services/http.py:ExternalApiClient', 'fuelroute/middleware.py:RateLimitMiddleware',
    ],
    edge: 'Car profile, not truck. OSRM busy → 503 upstream_busy; down after the retry → 502 upstream_unavailable; no road → 422 no_route (valid input, but no road joins the places).',
    see: ['concept:route-cache-vs-plan-cache', 'concept:mile-marker', 'concept:trip-error-box'],
  },

  'concept:mile-marker': {
    title: 'Mile marker',
    what: 'How far along the road a point is, in miles from the start. Not the highway’s own mile posts.',
    source: 'The server (osrm.prepare_route and stations.stations_along_route).',
    how: 'The road is resampled: one point about every mile (RESAMPLE_MILES). The markers are scaled so the last one equals the OSRM distance exactly. A truck stop’s mile marker is the marker of the road point closest to it.',
    live: (c) => {
      const stops = stopsOf(c);
      need(stops[0]?.mile_marker);
      return `Stop ${stops[0].stop} is at mile ${fmt.dec1(stops[0].mile_marker)} of ${fmt.miles(answer(c).route.distance_miles)}.`;
    },
    why: 'The optimizer works in one dimension, miles along the road, so each truck stop needs one position on it.',
    code: ['fuelroute/services/osrm.py:prepare_route', 'fuelroute/services/geo.py:resample', 'fuelroute/services/stations.py:stations_along_route'],
    edge: 'Precise to about a mile, plus the error of a station kept at its city center. The detour to the station is not added; the stop’s card estimates it.',
  },

  'concept:external-calls': {
    title: 'External calls',
    what: 'Requests the server makes to outside services (OSRM for the road, Nominatim for free-text places) to answer one trip.',
    source: 'API fields meta.external_api_calls and meta.external_api_services.',
    how: 'A client object is created per request and records the service name on every attempt: external_api_calls = how many, external_api_services = the list. Error answers carry the same meta.',
    live: (c) => {
      const m = metaOf(c);
      need(m.external_api_calls);
      return `This answer: ${plural(m.external_api_calls, 'external call')} [${(m.external_api_services || []).join(', ')}].`;
    },
    why: 'The brief asks to call the routing API as little as possible, so every answer proves how many calls it made. A new trip: one; the same trip again, another truck or an invalid input: none.',
    code: [
      'fuelroute/services/http.py:ExternalApiClient', 'fuelroute/services/planner.py:_meta',
      'fuelroute/views.py:_plan_from', 'fuelroute/tests/test_api.py:test_route_returns_plan_map_and_uses_one_external_call',
    ],
    edge: 'Retries count as calls. The page itself, /api/places and /api/about make none.',
    see: ['concept:route-cache-vs-plan-cache'],
  },

  'concept:route-cache-vs-plan-cache': {
    title: 'Two caches: the road and the plan',
    what: 'A cache is a saved answer reused instead of computing it again. The server keeps two: one for the road (from OSRM) and one for the finished plan.',
    source: 'API fields meta.route_cache and meta.plan_cache (“hit” = reused, “miss” = computed).',
    how: 'Plan cache key: the data version, both ends and every truck setting; kept one hour by default (PLAN_CACHE_SECONDS). Road cache key: both ends only; kept one day (ROUTE_CACHE_SECONDS). One of sixty-four shared locks, picked by hashing the key, makes requests for one road wait for the first.',
    live: (c) => {
      const m = metaOf(c);
      need(m.plan_cache, m.route_cache);
      return `This answer: plan cache ${m.plan_cache}, road cache ${m.route_cache}.`;
    },
    why: 'A changed truck changes the plan but not the road: it misses the plan cache, hits the road cache and costs no call. Keeping roads apart means a burst of new plans never pushes out the road they need. The data version makes old plans stale when prices are reloaded.',
    code: [
      'fuelroute/services/planner.py:_plan_trip', 'fuelroute/services/osrm.py:get_route', 'fuelroute/services/osrm.py:_cache_key',
      'config/settings.py:CACHES', 'fuelroute/tests/test_what_if.py:test_settings_are_in_the_plan_cache_key_not_the_route_cache_key',
      'fuelroute/tests/test_api.py:test_second_request_uses_the_plan_cache',
    ],
    edge: 'Per process, in memory (LocMem): at most five hundred plans and two hundred roads, and a restart empties them. With several workers, Redis would share both caches. Two different roads rarely share a lock, and then only wait for each other.',
  },

  'concept:server-timing': {
    title: 'Where the server time goes',
    what: 'A standard response header, Server-Timing, that splits one request into steps with their milliseconds.',
    source: 'The Server-Timing header (browser DevTools → Network → the /api/route request → Timing).',
    how: 'The planner records each step (geocoding, plan cache, routing, corridor, optimizer, build, comparison) and one entry per external service; the middleware (code that wraps every request) adds render and total.',
    why: 'It shows that a new trip is almost all waiting for OSRM, and that the in-house steps take milliseconds. Errors carry it too.',
    code: [
      'fuelroute/services/planner.py:_Timer', 'fuelroute/views.py:server_timing',
      'fuelroute/middleware.py:ResponseTimeMiddleware', 'fuelroute/renderers.py:TimedJSONRenderer',
      'fuelroute/tests/test_api.py:test_server_timing_header_breaks_down_the_request',
    ],
  },

  'concept:drive-duration': {
    title: 'Driving time (not shown)',
    what: 'OSRM’s estimate of the driving time. It is in the JSON (route.duration_hours) but not on the page.',
    source: 'API field route.duration_hours.',
    how: 'OSRM’s duration in seconds ÷ 3600, rounded to two decimals.',
    live: (c) => {
      need(answer(c)?.route?.duration_hours);
      return `${fmt.num(answer(c).route.duration_hours)} hours of driving, for a car.`;
    },
    why: 'The public OSRM server uses a car profile: no truck speeds, no mandatory breaks, no fuel stops. Showing it would mislead; our own OSRM with a truck profile would fix it.',
    code: ['fuelroute/services/planner.py:_plan_trip', 'fuelroute/services/osrm.py:route_from_payload'],
  },

  'concept:miles-outside-usa': {
    title: 'Road outside the USA',
    what: 'How much of the fastest road runs outside the USA (Detroit → Buffalo goes through Ontario).',
    source: 'API field route.miles_outside_usa.',
    how: 'Each one-mile road point is checked against an outline of the USA; miles outside = points outside × their spacing. A warning is added from one mile up. Truck stops whose nearest road point is outside the USA are not used.',
    live: (c) => {
      need(answer(c)?.route?.miles_outside_usa);
      return `${fmt.miles(answer(c).route.miles_outside_usa)} of this road are outside the USA.`;
    },
    why: 'The price file has no station outside the USA, and a US station reachable only from a Canadian highway would mean crossing the border.',
    code: [
      'fuelroute/services/osrm.py:Route.miles_outside_usa', 'fuelroute/services/usa.py:in_usa',
      'fuelroute/services/planner.py:_warnings', 'fuelroute/tests/test_geo.py:test_corridor_drops_stations_seen_only_from_outside_the_usa',
    ],
  },

  'concept:checks-before-routing': {
    title: 'Checks before the routing call',
    what: 'Cheap checks that run before the routing call is spent.',
    source: 'The server (planner._check_endpoints and geocoding; _check_snapping right after the call).',
    how: 'Before OSRM: outside the USA → 400 (invalid_request for “lat,lon”, location_outside_usa for a place name); the same place → 400 same_location; Alaska or Hawaii → 422 no_fuel_data_in_region; no station data → 503. After OSRM: an end more than five miles from a road (MAX_ROAD_SNAP_MILES) → 422 location_not_near_road.',
    why: 'Invalid trips never use the free routing service, and you get a precise reason: 400 means the input is wrong, 422 that it is fine but the trip cannot be planned.',
    code: [
      'fuelroute/services/planner.py:_check_endpoints', 'fuelroute/services/planner.py:_check_snapping',
      'fuelroute/serializers.py:RouteRequestSerializer._validate_location', 'fuelroute/services/errors.py:PlannerError',
    ],
    edge: 'The road check needs the OSRM answer, so it is the only one that costs a call. 503 station_data_not_loaded means the server is not ready (run load_stations).',
    see: ['concept:trip-error-box'],
  },

  'concept:route-map-line': {
    title: 'The map',
    what: 'The road (blue line), the start (green dot), the finish (red dot) and, from the Fuel stops step on, the stops (numbered circles, in driving order).',
    source: 'API field map.geojson (map data), drawn with Leaflet (a map library) on OpenStreetMap tiles (the map’s background images).',
    how: 'The full OSRM shape is simplified with Ramer–Douglas–Peucker (a standard way to drop the points that do not change a line’s shape; here about fifty meters of tolerance) and rounded to five decimals. The ends are the geocoded places; each stop sits at its station’s position.',
    live: (c) => {
      const line = (answer(c)?.map?.geojson?.features || []).find((f) => f.geometry?.type === 'LineString');
      need(line);
      return `${fmt.int(line.geometry.coordinates.length)} points on the drawn line.`;
    },
    why: 'A coast-to-coast road has tens of thousands of points; simplifying keeps the JSON small with no visible change. The tour shows the road first and the stops next: one idea at a time.',
    code: [
      'fuelroute/services/planner.py:_build_geojson', 'fuelroute/services/geo.py:simplify',
      'fuelroute/static/fuelroute/js/map.js:drawRoad', 'fuelroute/static/fuelroute/js/map.js:showRouteOnly',
      'fuelroute/static/fuelroute/js/main.js:showMapFor',
    ],
    edge: 'A stop marker sits at its station’s exact place, or at its city center when none was confirmed. A stretch with no truck stop is drawn in red with its length. If the map library cannot load, every step still works without a map.',
    see: ['concept:map-in-the-api-answer', 'concept:stop-card'],
  },

  // ----- step 3: fuel stops -----
  'derived.stops_text': STOPS_TEXT,

  'concept:three-rules': {
    title: 'The three rules (the optimizer)',
    what: 'The rules that pick where to stop and how much to buy. An optimizer is code that finds the best plan; this one is “greedy” (it makes the best choice at each stop), and here that is also the best plan overall.',
    source: 'The server: services/optimizer.py (plain Python, no Django).',
    how: '1) A cheaper truck stop within one usable tank → buy just enough to reach the nearest one. 2) Else, the finish within one tank → buy just enough to finish. 3) Else → fill the tank and drive to the cheapest stop within reach.',
    live: (c) => {
      need(answer(c));
      return [ruleCounts(stopsOf(c)), `Looking ahead ${fmt.miles_short(veh(c).usable_range_miles)} among ${fmt.int(summ(c).candidate_stations_on_route)} truck stops near the road.`];
    },
    why: 'This is the classic “gas station problem”: with a price per gallon and no cost per stop, these rules give the cheapest plan. They take O(n log n + n·k) steps (n candidate stops, k within one tank): about a millisecond coast to coast.',
    code: [
      'fuelroute/services/optimizer.py:_greedy', 'fuelroute/services/optimizer.py:plan_fuel_stops',
      'fuelroute/services/planner.py:_optimize', 'fuelroute/tests/test_optimizer.py:test_greedy_matches_exact_dp',
    ],
    edge: 'Optimal only in the model: no cost per stop or detour, no volume discounts (hence “Skip tiny stops”). Exact dynamic programming is far slower, and a linear-programming solver adds a library for the same answer. A binary search (halving a sorted list) finds the last stop within reach.',
    see: ['concept:rule-cheaper-ahead', 'concept:rule-finish', 'concept:rule-fill-up', 'concept:why-rules-are-optimal', 'concept:fuel-in-miles-of-range'],
  },

  'concept:rule-cheaper-ahead': {
    title: 'Rule 1: Cheaper ahead',
    what: 'If a truck stop within one usable tank ahead is cheaper than here, buy only the fuel needed to reach the nearest such stop, and buy the rest there.',
    source: 'API field fuel_stops[].decision.rule = “reach_cheaper”, set by the optimizer; decision.cheaper_station names the stop.',
    how: 'cheaper = the first stop after this one, within this mile + the usable range, with a strictly lower price. buy = (its mile − this mile) ÷ mpg − the fuel in the tank above the safety fuel. If the tank already reaches it, nothing is bought here.',
    live: (c) => {
      const stop = firstWithRule(c, 'reach_cheaper');
      need(stop);
      return [`Stop ${stop.stop}, ${stop.city}, ${stop.state}: ${purchaseLine(c, stop)}`,
        `${fmt.int(stopsOf(c).filter((s) => s.decision?.rule === 'reach_cheaper').length)} of ${plural(stopsOf(c).length, 'stop')} use this rule.`];
    },
    why: 'Any gallon bought here beyond what reaches the cheaper stop would be burned after it, where it costs less. It goes to the NEAREST cheaper stop, not the cheapest: the rules run again there, so the chain still ends at the cheapest fuel.',
    code: [
      'fuelroute/services/optimizer.py:_greedy', 'fuelroute/services/optimizer.py:RULE_REACH_CHEAPER',
      'fuelroute/static/fuelroute/js/plan.js:RULES', 'fuelroute/static/fuelroute/js/plan.js:highlightStop',
    ],
    edge: '“Cheaper” means strictly cheaper. This rule is why many stops are reached with an empty tank: it buys exactly enough to get there.',
    see: ['concept:why-rules-are-optimal', 'concept:fuel-in-miles-of-range'],
  },

  'concept:rule-finish': {
    title: 'Rule 2: Finish',
    what: 'If nothing cheaper is within reach but the finish is, buy just enough to finish.',
    source: 'API field fuel_stops[].decision.rule = “finish”.',
    how: 'Checked only when rule 1 does not apply. needed = (distance − this mile) ÷ mpg + the fuel the truck must arrive with; if that fits in the usable tank, buy needed − the fuel in the tank.',
    live: (c) => {
      const stop = firstWithRule(c, 'finish');
      need(stop);
      return `Stop ${stop.stop}, ${stop.city}, ${stop.state}: ${purchaseLine(c, stop)}`;
    },
    why: 'This is the cheapest price left on the way, so the rest of the trip is bought here; more would be paid fuel that is never burned.',
    code: ['fuelroute/services/optimizer.py:_greedy', 'fuelroute/services/optimizer.py:RULE_FINISH', 'fuelroute/services/planner.py:_tank_rules'],
    edge: '“Just enough to finish” includes the fuel the truck must arrive with, so with the default start the last stop also buys back the start reserve.',
  },

  'concept:rule-fill-up': {
    title: 'Rule 3: Fill up',
    what: 'If nothing within one tank is cheaper and the finish is out of reach, fill the tank here.',
    source: 'API field fuel_stops[].decision.rule = “fill_up”.',
    how: 'buy = a full tank − the fuel in the tank. The truck then drives to the cheapest stop within reach (the farthest on a tie), passing the others. With no stop and no finish within one tank, the trip is impossible: 422 no_reachable_fuel_station (this truck cannot make it).',
    live: (c) => {
      const stop = firstWithRule(c, 'fill_up');
      need(stop);
      return `Stop ${stop.stop}, ${stop.city}, ${stop.state} at ${price4(stop.price_per_gallon)}: ${purchaseLine(c, stop)}`;
    },
    why: 'Nothing cheaper within one tank means this is the best price for the next tankful of miles, so filling up covers as many of them as possible at this price.',
    code: ['fuelroute/services/optimizer.py:_greedy', 'fuelroute/services/optimizer.py:RULE_FILL_UP', 'fuelroute/services/planner.py:_decision'],
    edge: '“Nothing cheaper” means nothing strictly cheaper: a stop with the same price may exist.',
  },

  'concept:why-rules-are-optimal': {
    title: 'Why the three rules give the cheapest plan',
    what: 'Why these simple rules find the lowest possible cost, and the test that checks it.',
    source: 'The argument below, and the test suite.',
    how: 'Two facts. A gallon bought here and burned after a cheaper stop could have been bought there, so buy only enough to reach it. With nothing cheaper within one tank, this price is the cheapest for every mile it can cover, so fill up.',
    why: 'The test: thirty random roads × three ways to start, each a hundred miles, a thirty-mile tank and nine stops at whole miles. Whole miles in means whole miles bought, so an exact search over whole-mile fuel levels (dynamic programming) finds the true minimum; the rules must match it.',
    code: [
      'fuelroute/services/optimizer.py', 'fuelroute/tests/test_optimizer.py:_exact_cost',
      'fuelroute/tests/test_optimizer.py:test_greedy_matches_exact_dp',
      'fuelroute/tests/test_optimizer.py:test_safety_reserve_is_the_optimum_of_a_smaller_tank',
    ],
    edge: 'Optimal only in the model: a price per gallon with no volume discount, free stops, a fixed road and no cost for the detour. The same check runs again with safety fuel. “Skip tiny stops” is not optimal, on purpose.',
    see: ['concept:three-rules', 'concept:how-it-is-tested'],
  },

  'concept:fuel-in-miles-of-range': {
    title: 'Fuel counted in miles',
    what: 'Inside the optimizer, fuel is counted in miles the truck can drive, not in gallons.',
    source: 'The optimizer’s design (services/optimizer.py, “Model” in its docstring).',
    how: 'Tank = the range in miles, and driving d miles burns d; a purchase of m miles is m ÷ mpg gallons. Gallons are made once, at the end.',
    live: (c) => {
      const v = veh(c);
      need(v.max_range_miles, v.tank_gallons, v.miles_per_gallon);
      return `Here: tank ${fmt.miles_short(v.max_range_miles)} = ${fmt.gal(v.tank_gallons)}; one gallon = ${fmt.num(v.miles_per_gallon)} miles.`;
    },
    why: 'It removes the division by mpg from every step, so the rules read as plain distance comparisons.',
    code: ['fuelroute/services/optimizer.py:_to_stops', 'fuelroute/services/optimizer.py:plan_fuel_stops'],
  },

  'concept:stop-card': {
    title: 'This stop, line by line',
    what: 'One fuel stop: where it is, the price, the fuel when it pulls in, what it buys, what that costs, and the rule that made it.',
    source: 'API field fuel_stops[] of the answer on screen (one object per stop).',
    how: 'Mile = distance from the start along the road; arrives with = the fuel when leaving the previous stop − the miles driven since ÷ mpg. Buys = what its rule says. Cost = gallons × price (four decimals), rounded to the cent.',
    live: (c, arg) => {
      const stop = stopOf(c, arg);
      need(stop);
      return [
        `Stop ${stop.stop}: ${stop.name}, ${stop.city}, ${stop.state}, at mile ${fmt.dec1(stop.mile_marker)}, ${price4(stop.price_per_gallon)} a gallon.`,
        arrivalLine(c, stop),
        purchaseLine(c, stop),
        costLine(stop),
        `Leaves with ${fmt.gal(stop.fuel_on_arrival_gallons)} + ${fmt.gal(stop.gallons)} = ${fmt.gal(stop.fuel_on_arrival_gallons + stop.gallons)}.`,
        detourLine(c, stop),
      ];
    },
    why: 'Every number on the card can be checked by hand from the previous stop. The rule tag and “Why here” come from the answer’s decision block: the browser never recomputes the plan.',
    code: [
      'fuelroute/static/fuelroute/js/plan.js:renderStopCards', 'fuelroute/static/fuelroute/js/plan.js:whyText',
      'fuelroute/services/planner.py:_build_stops', 'fuelroute/services/planner.py:_money_rows',
      'fuelroute/services/optimizer.py:_to_stops',
    ],
    edge: 'Hand checks can be a hundredth of a gallon or a cent off: arrivals, gallons and mile markers are rounded, and the card shows the price with three decimals while the cost uses four.',
    see: ['concept:three-rules', 'concept:fuel-on-departure', 'concept:station-price', 'concept:mile-marker', 'concept:detour-estimate'],
  },

  'concept:detour-estimate': {
    title: 'The detour to a stop (an estimate)',
    what: 'The miles a truck drives off the road to reach a stop and get back, and what that fuel would cost. The plan does not charge it; the page only estimates it.',
    source: 'API field fuel_stops[].distance_from_route_miles: the straight line from the station to its nearest road point, one way. The rest is computed in the browser.',
    how: 'there and back = 2 × distance; gallons = that ÷ miles per gallon; dollars = gallons × the stop’s price. The trip’s estimate adds up every stop.',
    live: (c) => {
      const d = detoursOf(c);
      need(d);
      const farthest = d.stops.reduce((a, b) => (b.distance_from_route_miles > a.distance_from_route_miles ? b : a));
      return [detoursLine(c), `Farthest from the road: stop ${farthest.stop}, ${farthest.city}, ${farthest.state}. ${detourLine(c, farthest)}`];
    },
    why: 'The optimizer works in miles along the road, so a detour has no place in its model (README, limits); a stop at its exact place is usually under a mile off anyway. The estimate shows what that leaves out instead of hiding it.',
    code: [
      'fuelroute/services/stations.py:stations_along_route', 'fuelroute/services/geo.py:nearest_on_line',
      'fuelroute/services/planner.py:_build_stops', 'fuelroute/static/fuelroute/js/explain.js:detourOf',
    ],
    edge: 'A straight line is shorter than the real drive (ramps, side roads). Road points are about a mile apart, so a station by the road can show up to half a mile; one at its city center, or at an exit of another highway nearby, several miles.',
    see: ['concept:stop-card', 'route.summary.total_fuel_cost', 'concept:price-blind-driver', 'concept:exact-positions', 'route.vehicle.corridor_miles'],
  },

  'concept:fuel-on-departure': {
    title: 'Fuel when the truck leaves a stop',
    what: 'Arrival + what it buys. It is not printed on the card; you see it on the Tank gauge during Play.',
    source: 'Computed in the browser (player.js) from API fields.',
    how: 'leaves with = fuel_on_arrival_gallons + gallons. Between stops the tank drops by miles ÷ mpg.',
    live: (c) => {
      const stops = stopsOf(c).slice(0, 3);
      need(stops[0]);
      return stops.map((s) => `Stop ${s.stop}: ${fmt.gal(s.fuel_on_arrival_gallons)} + ${fmt.gal(s.gallons)} = ${fmt.gal(s.fuel_on_arrival_gallons + s.gallons)}.`);
    },
    why: 'It makes the plan checkable: leaves with − (miles to the next stop ÷ mpg) = arrives with, at the next stop.',
    code: ['fuelroute/static/fuelroute/js/player.js:arrive', 'fuelroute/services/planner.py:_decision'],
  },

  'concept:skip-tiny-stops': {
    title: 'Skip tiny stops',
    what: 'An optional clean-up after the cheapest plan (off by default): a stop that buys very little is merged into a neighbour stop, or topped up to the minimum, if each fix costs little. In the API it is consolidate=true.',
    source: 'The request setting consolidate (the “Skip tiny stops” switch of the Truck step); its two limits come from /api/about (min_stop_gallons, max_consolidation_cost).',
    how: 'After the three rules, a stop buying less than the minimum moves fuel to or from a neighbour stop, costing gallons moved × the price difference. A move is kept only within the limit, with the tank between empty and full. Moves that remove a stop go first.',
    live: (c) => {
      const v = aboutVehicle(c);
      need(v.min_stop_gallons, v.max_consolidation_cost);
      const merged = summ(c).tiny_stops_merged;
      return [`Minimum stop ${fmt.gal(v.min_stop_gallons)}; at most ${fmt.money(v.max_consolidation_cost)} per fix.`,
        merged ? `On this trip: ${plural(merged.stops_before, 'stop')} became ${plural(summ(c).number_of_stops, 'stop')}, for ${fmt.money(merged.extra_cost)} more.`
          : answer(c) ? 'It is off for the answer on screen.' : null];
    },
    why: 'The pure optimum can say “stop for two gallons because the next stop is two cents cheaper”, which no driver does. Off by default, so the default answer is the exact cheapest plan; when on, the price of convenience is bounded and shown.',
    code: [
      'fuelroute/services/optimizer.py:_consolidate', 'fuelroute/services/optimizer.py:_shift',
      'fuelroute/services/optimizer.py:_move_sources', 'fuelroute/services/optimizer.py:plan_fuel_stops',
      'fuelroute/services/planner.py:_build_summary', 'fuelroute/services/planner.py:_decision',
    ],
    edge: 'The limit is per fix, not per trip: several fixes can add up, and the plan can then cost more than the driver who ignores prices.',
    see: ['derived.min_stop', 'derived.max_fix', 'derived.merged_cost'],
  },

  'derived.merged_cost': {
    title: 'What skipping tiny stops adds',
    what: 'How many dollars more this trip costs because tiny stops were merged, compared with the three rules alone.',
    source: 'API field summary.tiny_stops_merged.extra_cost, read by format.js derive.',
    how: 'extra = the total of the final plan − the total of the plan before merging; each total is a sum of stop costs rounded to the cent.',
    live: (c) => {
      const merged = summ(c).tiny_stops_merged;
      need(merged?.extra_cost, summ(c).total_fuel_cost);
      const total = summ(c).total_fuel_cost;
      return `${fmt.money(total)} now − ${fmt.money(total - merged.extra_cost)} before merging = ${fmt.money(merged.extra_cost)}.`;
    },
    why: 'The cheapest plan stays the reference, and the price of convenience is shown in dollars.',
    code: [
      'fuelroute/services/planner.py:_build_summary', 'fuelroute/services/planner.py:_money_rows',
      'fuelroute/services/optimizer.py:_consolidate', 'fuelroute/static/fuelroute/js/format.js:derive',
    ],
    edge: 'Can be zero (fuel moved between stops with the same price). Can be above the per-fix limit, because the limit is per fix.',
    see: ['concept:skip-tiny-stops'],
  },

  'derived.min_stop': MIN_STOP,
  'derived.max_fix': MAX_FIX,

  'derived.merged_before': {
    title: 'Stops before merging',
    what: 'How many stops the three rules alone make, before tiny stops are merged.',
    source: 'API field summary.tiny_stops_merged.stops_before, read by format.js derive.',
    how: 'The number of purchases of the three rules, before the merging step.',
    live: (c) => {
      const merged = summ(c).tiny_stops_merged;
      need(merged?.stops_before);
      return `${plural(merged.stops_before, 'stop')} → ${plural(summ(c).number_of_stops, 'stop')}.`;
    },
    why: 'It shows exactly what the setting changed on this trip. When the count did not change but money moved, the box says so; when nothing moved, it says it changed nothing.',
    code: ['fuelroute/services/planner.py:_build_summary', 'fuelroute/services/optimizer.py:plan_fuel_stops', 'fuelroute/static/fuelroute/js/format.js:derive'],
    see: ['derived.merged_after'],
  },

  'derived.merged_after': {
    title: 'Stops after merging',
    what: 'How many stops the plan on screen has, after merging tiny stops.',
    source: 'API field summary.number_of_stops, read by format.js derive.',
    how: 'The number of stops of the final plan (the cards below).',
    live: (c) => EXPLAINERS['derived.merged_before'].live(c),
    why: 'The same number as the stop cards, so the box and the list always agree. Never more than before: every merge removes a stop or tops one up.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/services/planner.py:_build_summary'],
  },

  'derived.smallest_purchase': {
    title: 'Smallest purchase',
    what: 'The smallest amount any stop of this plan buys.',
    source: 'Computed in the browser (format.js derive) from fuel_stops[].gallons.',
    how: 'The minimum of every stop’s gallons. The tip shows when “Skip tiny stops” is off and this is below the minimum stop size.',
    live: (c) => {
      const stops = stopsOf(c);
      need(stops[0]);
      const small = stops.reduce((a, b) => (b.gallons < a.gallons ? b : a));
      const min = aboutVehicle(c).min_stop_gallons;
      return `Stop ${small.stop} (${small.city}, ${small.state}) buys ${fmt.gal(small.gallons)}${present(min) ? `; the minimum stop is ${fmt.gal(min)}` : ''}.`;
    },
    why: 'Honest: the cheapest plan can include a stop for a few gallons. The tip names it and says how to trade a few cents for fewer stops.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive'],
    see: ['concept:skip-tiny-stops'],
  },

  'derived.stops_arriving_empty': {
    title: 'Stops reached on empty',
    what: 'How many stops the truck reaches with no fuel left.',
    source: 'Computed in the browser (format.js derive) from fuel_stops[].fuel_on_arrival_gallons.',
    how: 'Count the stops whose arrival is exactly zero (after the API’s rounding to the hundredth). The line shows only when there is no safety fuel.',
    live: (c) => {
      const empty = stopsOf(c).filter((s) => s.fuel_on_arrival_gallons === 0).map((s) => s.stop);
      need(answer(c));
      return empty.length ? `Stops ${empty.join(', ')} arrive with ${fmt.gal(0)}.` : 'No stop arrives empty.';
    },
    why: 'Honest about the model: it uses the whole range, as the brief says. Rule 1 buys exactly enough to reach the cheaper stop, so arriving empty is normal. Safety fuel adds a margin.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/services/optimizer.py:_greedy'],
    edge: 'An arrival of a few thousandths of a gallon rounds to zero and counts as empty.',
    see: ['concept:safety-fuel'],
  },

  'route.summary.number_of_stops': {
    title: 'Number of stops',
    what: 'The number of fuel stops in the final plan.',
    source: 'API field summary.number_of_stops.',
    how: 'The length of the list of stops.',
    live: (c) => {
      need(summ(c).number_of_stops);
      return `${plural(summ(c).number_of_stops, 'stop')} in fuel_stops. ${ruleCounts(stopsOf(c)) || ''}`.trim();
    },
    why: 'One count from the API, used everywhere, so the page cannot disagree with itself.',
    code: ['fuelroute/services/planner.py:_build_summary'],
  },

  'concept:player': {
    title: 'Play the trip',
    what: 'A replay of the plan on the map: a truck drives the road, the tank drains and refills, and the money adds up.',
    source: 'The browser (player.js), using only the numbers of the API answer.',
    how: 'Constant speed along the map line, for ten to fifteen seconds (MIN_SECONDS, MAX_SECONDS): long trips play a little longer, and the stops share at most about a third of the time. Each frame moves at most a tenth of a second, so a hidden tab does not jump.',
    live: (c) => {
      const r = answer(c);
      need(r?.route?.distance_miles);
      const t = timing(r.route.distance_miles, stopsOf(c).length);
      return `${fmt.miles(r.route.distance_miles)} and ${plural(stopsOf(c).length, 'stop')}: about ${fmt.dec1(t.driving + t.perStop * stopsOf(c).length)} seconds.`;
    },
    why: 'A demo must fit in a video, so it is not real driving time. It replays, never recomputes: every arrival and every cost is the API’s.',
    code: [
      'fuelroute/static/fuelroute/js/player.js:timing', 'fuelroute/static/fuelroute/js/player.js:createPlayer',
      'fuelroute/tests/test_js_logic.py:test_play_lasts_ten_to_fifteen_seconds', 'fuelroute/tests/test_web.py:test_play_trip_replays_the_apis_numbers',
    ],
    edge: 'With “reduce motion” turned on in the system, it jumps to the end and lists every purchase.',
    see: ['concept:player-mile-counter', 'concept:player-tank-gauge', 'concept:player-fuel-bought', 'concept:player-event-line'],
  },

  'concept:player-mile-counter': {
    title: 'Mile counter',
    what: 'Where the truck is along the road during Play.',
    source: 'The browser, from route.distance_miles and the stops’ mile markers.',
    how: '“Mile X of D”: X grows at constant speed; the bar has one tick per stop at its mile marker. The truck icon is placed by measuring the map line and scaling it to the API distance.',
    why: 'Scaling the drawn line to the API distance makes the truck reach each stop exactly at its mile marker, so the replay and the cards agree.',
    code: ['fuelroute/static/fuelroute/js/player.js:measureLine', 'fuelroute/static/fuelroute/js/player.js:pointAt'],
  },

  'concept:player-tank-gauge': {
    title: 'Tank gauge',
    what: 'The fuel in the tank during Play, from E (empty) to F (full).',
    source: 'The browser, with the API’s own fuel numbers.',
    how: 'It starts at summary.start_fuel_gallons and, on the road, drops by miles since ÷ mpg. At a stop it jumps to the API’s arrival value, then rises by the gallons bought; at the finish it shows summary.end_fuel_gallons. A mark shows the safety fuel.',
    live: (c) => {
      const s = summ(c);
      need(veh(c).tank_gallons, s.start_fuel_gallons);
      return `Tank ${fmt.gal(veh(c).tank_gallons)}; starts with ${fmt.gal(s.start_fuel_gallons)}, ends with ${fmt.gal(s.end_fuel_gallons)}.`;
    },
    why: 'Only the burn between stops is drawn by the page; every arrival is the API’s, so the gauge cannot drift from the plan.',
    code: ['fuelroute/static/fuelroute/js/player.js:fuelOnTheRoad', 'fuelroute/static/fuelroute/js/player.js:arrive'],
  },

  'concept:player-fuel-bought': {
    title: '“Fuel bought” counter',
    what: 'Money spent so far during Play. It ends on the API’s total, with a check that they match.',
    source: 'The browser, adding the API’s stop costs.',
    how: 'Each stop adds its cost in whole cents (Math.round(cost × 100), the same formula as checks.js). At the end the sum is compared with the total in cents: a check mark if they are equal, a cross if not.',
    live: (c) => EXPLAINERS['concept:check-costs-add-up-to-the-cent'].live(c),
    why: 'Whole cents avoid floating-point errors (in JavaScript 0.1 + 0.2 is not 0.3), so “to the cent” is a real check.',
    code: ['fuelroute/static/fuelroute/js/player.js:arrive', 'fuelroute/static/fuelroute/js/player.js:finish', 'fuelroute/static/fuelroute/js/player.js:cents'],
  },

  'concept:player-event-line': {
    title: 'The line under the map',
    what: 'What is happening now, in words.',
    source: 'The browser, from the answer on screen.',
    how: 'Before Play: what the truck leaves (and must arrive) with. At each stop: “Stop n · City: rule · buys g at price = cost”. At the end: the stops, the gallons and the total.',
    why: 'Each purchase is tied to the rule that created it.',
    code: ['fuelroute/static/fuelroute/js/player.js:idleText', 'fuelroute/static/fuelroute/js/player.js:eventText', 'fuelroute/static/fuelroute/js/player.js:setEvent'],
    edge: 'The price is shown with three decimals and the cost uses four, so gallons × the shown price can differ by a cent.',
  },

  // ----- step 4: cost -----
  'route.summary.total_fuel_cost': TOTAL_COST,
  'route.summary.total_gallons_purchased': GALLONS_BOUGHT,

  'route.summary.average_price_paid': {
    title: 'Average price paid',
    what: 'The price per gallon actually paid over the trip, weighted by how much was bought at each stop.',
    source: 'API field summary.average_price_paid.',
    how: 'total cost ÷ gallons bought, rounded to four decimals; the page shows three.',
    live: (c) => {
      const s = summ(c);
      need(s.total_fuel_cost, s.total_gallons_purchased, s.average_price_paid);
      const prices = stopsOf(c).map((x) => x.price_per_gallon);
      return [`${fmt.money(s.total_fuel_cost)} ÷ ${fmt.gal(s.total_gallons_purchased)} = ${price4(s.total_fuel_cost / s.total_gallons_purchased)} a gallon (the API: ${price4(s.average_price_paid)}).`,
        prices.length ? `The stops’ prices go from ${price4(Math.min(...prices))} to ${price4(Math.max(...prices))}.` : null];
    },
    why: 'A weighted average is the honest “price paid”: a big fill-up at a cheap stop counts more than a small top-up. It is not the plain mean of the station prices.',
    code: ['fuelroute/services/planner.py:_average_price', 'fuelroute/services/planner.py:_build_summary'],
    edge: 'It sits between the cheapest and the dearest stop price, give or take a fraction of a cent: each stop’s cost is rounded to the cent before dividing.',
    see: ['concept:station-price'],
  },

  'concept:check-costs-add-up-to-the-cent': {
    title: 'Check: the stops add up to the total',
    what: 'A live re-check, in your browser, that the stop costs of the API add up exactly to its total.',
    source: 'Computed in the browser (plan.js renderCost) from fuel_stops[].cost and summary.total_fuel_cost.',
    how: 'cents(v) = Math.round(v × 100). It passes if the sum of the stop costs in cents equals the total in cents: a comparison of whole numbers.',
    live: (c) => {
      const stops = stopsOf(c);
      need(stops[0], summ(c).total_fuel_cost);
      const sum = sumCents(stops.map((x) => x.cost));
      const total = cents(summ(c).total_fuel_cost);
      return `Stops: ${fmt.int(sum)} cents; total: ${fmt.int(total)} cents → ${sum === total ? 'equal' : 'NOT equal'}.`;
    },
    why: 'JavaScript numbers cannot add dollars exactly (0.1 + 0.2 is not 0.3); whole cents can. The check runs on every trip, does not trust the server, and is never hidden when it fails. The same rule is a backend test.',
    code: [
      'fuelroute/static/fuelroute/js/plan.js:renderCost', 'fuelroute/static/fuelroute/js/checks.js:cents',
      'fuelroute/static/fuelroute/js/checks.js:sumCents', 'fuelroute/services/planner.py:_money_rows',
      'fuelroute/tests/test_api.py:test_stop_costs_add_up_to_the_total_to_the_cent',
    ],
    see: ['route.summary.total_fuel_cost'],
  },

  'concept:check-every-mile-paid': {
    title: 'Check: every mile is paid for',
    what: 'Proof that the total is the full fuel cost of the trip, not lowered by free fuel in the tank.',
    source: 'Computed in the browser (plan.js renderCost) from start_fuel_gallons, end_fuel_gallons, unpriced_fuel_gallons and vehicle.start_tank.',
    how: 'Shown when the truck did not leave full, no fuel is unpriced, and it arrives with the fuel it left with (within half a hundredth of a gallon).',
    live: (c) => EXPLAINERS['concept:pay-for-every-mile'].live(c),
    why: 'The brief does not say how much fuel the truck starts with. Assuming a free full tank would hide a whole tank of fuel from the bill.',
    code: ['fuelroute/static/fuelroute/js/plan.js:renderCost', 'fuelroute/services/planner.py:_tank_rules', 'fuelroute/static/fuelroute/js/format.js:derive'],
    see: ['concept:pay-for-every-mile'],
  },

  'concept:check-full-tank-free': {
    title: 'Note: the first tank was free',
    what: 'A note that the total is smaller because the truck left full and that tank cost nothing.',
    source: 'API fields vehicle.start_tank and summary.start_fuel_gallons.',
    how: 'Shown when start_tank is full. The free fuel is a full tank: range ÷ miles per gallon.',
    live: (c) => {
      const v = veh(c);
      need(v.max_range_miles, v.miles_per_gallon, summ(c).start_fuel_gallons);
      return `${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg = ${fmt.gal(summ(c).start_fuel_gallons)}, free.`;
    },
    why: 'A free full tank makes any plan look cheaper, so the page says so instead of hiding it.',
    code: ['fuelroute/static/fuelroute/js/plan.js:renderCost', 'fuelroute/services/planner.py:_tank_rules'],
    see: ['concept:tank-when-leaving'],
  },

  'concept:check-unpriced-fuel': {
    title: 'Note: fuel with no price',
    what: 'Fuel the trip burns that is not in the total, because no truck stop of the price file could sell it.',
    source: 'API field summary.unpriced_fuel_gallons.',
    how: 'unpriced = fuel burned − fuel bought, never below zero, rounded to the hundredth. With a full tank, the free tank is taken out first. The note shows when what remains is above zero.',
    live: (c) => {
      const s = summ(c);
      need(s.fuel_used_gallons, s.total_gallons_purchased, s.unpriced_fuel_gallons);
      return `${fmt.gal(s.fuel_used_gallons)} burned − ${fmt.gal(s.total_gallons_purchased)} bought = ${fmt.gal(Math.max(0, s.fuel_used_gallons - s.total_gallons_purchased))} (the API: ${fmt.gal(s.unpriced_fuel_gallons)}).`;
    },
    why: 'If the last truck stop is far from the finish, the truck cannot arrive with what it left with. That fuel has no price, so the page says so instead of inventing one; a matching sentence is in “Good to know”.',
    code: ['fuelroute/static/fuelroute/js/plan.js:renderCost', 'fuelroute/services/planner.py:_build_summary', 'fuelroute/services/planner.py:_tank_rules'],
    edge: 'With a full tank, the API counts the free tank in unpriced_fuel_gallons too; the page subtracts it, so the note only shows a real gap in the data.',
  },

  'concept:price-blind-driver': {
    title: 'The driver who ignores prices',
    what: 'A simulated driver used as a yardstick: it drives until the next truck stop would be out of reach, then fills the tank (or buys just what the rest of the trip needs).',
    source: 'API field summary.comparison.price_blind, computed on the server (optimizer.plan_price_blind) on the same truck stops.',
    how: 'The same truck stops near the road, the same fuel at the start and at the finish, the same safety fuel, priced by the same function as the plan. Stops at the same mile are taken in the order they are stored, never sorted by price.',
    live: (c) => {
      const blind = summ(c).comparison?.price_blind;
      need(blind?.total_fuel_cost);
      const s = summ(c);
      const detours = detoursOf(c);
      return [`Driver who ignores prices: ${fmt.money(blind.total_fuel_cost)}, ${plural(blind.number_of_stops, 'stop')}, ${fmt.gal(blind.total_gallons_purchased)}, ${price4(blind.average_price_paid)} a gallon.`,
        `This plan: ${fmt.money(s.total_fuel_cost)}, ${plural(s.number_of_stops, 'stop')}, ${fmt.gal(s.total_gallons_purchased)}, ${price4(s.average_price_paid)} a gallon.`,
        detours ? `Not in either total, this plan’s detours to its stops: ≈ ${fmt.miles(detours.miles)} there and back ≈ ${fmt.gal(detours.gallons)} ≈ ${fmt.money(detours.dollars)} (straight-line estimate).` : null,
        detours ? 'The answer does not say how far the other driver’s stops are from the road, so its detours are not estimated.' : null];
    },
    why: 'To call a plan “cheapest” you need something to compare it with. The same stops and the same fuel at both ends give the same gallons, so the difference is only where they are bought: a fair test. It is what a driver does with no tool.',
    code: [
      'fuelroute/services/optimizer.py:plan_price_blind', 'fuelroute/services/optimizer.py:_refuel_by_habit',
      'fuelroute/services/planner.py:_build_comparison', 'fuelroute/services/planner.py:_strategy',
      'fuelroute/tests/test_optimizer.py:test_price_blind_driver_buys_the_same_gallons_and_never_beats_the_optimum',
    ],
    edge: 'Hidden when the plan has no stop. Other yardsticks (a quarter-tank driver, the average price near the road) are in the JSON with include=details. Neither total charges the detours to the stops.',
    see: ['concept:cost-bars', 'derived.savings_amount', 'concept:three-rules', 'concept:detour-estimate'],
  },

  'concept:cost-bars': {
    title: 'The two bars',
    what: 'The two totals, side by side.',
    source: 'Computed in the browser (plan.js renderCost) from summary.total_fuel_cost and summary.comparison.price_blind.',
    how: 'Both bars start at zero; the longer one is full width; each width = its cost ÷ the larger cost.',
    live: (c) => {
      const blind = summ(c).comparison?.price_blind;
      need(blind?.total_fuel_cost, summ(c).total_fuel_cost);
      const most = Math.max(blind.total_fuel_cost, summ(c).total_fuel_cost) || 1;
      return [`This plan: ${fmt.money(summ(c).total_fuel_cost)} ÷ ${fmt.money(most)} → ${fmt.pct((summ(c).total_fuel_cost / most) * 100)} of the width.`,
        `Driver who ignores prices: ${fmt.money(blind.total_fuel_cost)} ÷ ${fmt.money(most)} → ${fmt.pct((blind.total_fuel_cost / most) * 100)}.`];
    },
    why: 'Starting at zero keeps the true scale: a saving of a few percent looks like a few percent, not more. Built with plain page elements, no chart library.',
    code: ['fuelroute/static/fuelroute/js/plan.js:renderCost', 'fuelroute/static/fuelroute/js/format.js:el'],
    edge: 'When the saving is small the bars look almost the same: that is on purpose.',
  },

  'derived.savings_amount': SAVINGS,

  'derived.savings_pct': {
    title: 'Saving, in percent',
    what: 'The saving as a percent of what the driver who ignores prices pays.',
    source: 'API field summary.comparison.savings_vs_price_blind.percent, read by format.js derive.',
    how: 'percent = saving ÷ the driver’s total × 100, rounded to one decimal.',
    live: (c) => savingsLines(c)[1],
    why: 'A percent of what a driver with no tool would pay: the usual way to say “X percent cheaper”. It depends only on the price spread between the stops of that road, because both drivers buy the same gallons.',
    code: ['fuelroute/services/planner.py:_savings', 'fuelroute/static/fuelroute/js/format.js:fmt', 'fuelroute/static/fuelroute/js/format.js:derive'],
    see: ['derived.savings_amount'],
  },

  'derived.extra_stops_text': {
    title: 'The trade-off: more stops',
    what: 'How many more stops the plan makes than the driver who ignores prices.',
    source: 'API field summary.comparison.savings_vs_price_blind.extra_stops, put into words by format.js derive.',
    how: 'extra = the plan’s stops − the driver’s stops; shown when above zero.',
    live: (c) => {
      const blind = summ(c).comparison?.price_blind;
      need(blind?.number_of_stops, summ(c).number_of_stops);
      return `${fmt.int(summ(c).number_of_stops)} − ${fmt.int(blind.number_of_stops)} = ${fmt.int(summ(c).number_of_stops - blind.number_of_stops)}.`;
    },
    why: 'The cost model has no cost per stop (time, detour), so the cheapest plan may stop more often. The page names the trade-off instead of hiding it.',
    code: ['fuelroute/services/planner.py:_build_comparison', 'fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/static/fuelroute/js/format.js:plural'],
    see: ['concept:skip-tiny-stops'],
  },

  'derived.fewer_stops_text': {
    title: 'Fewer stops',
    what: 'How many fewer stops the plan makes than the driver who ignores prices.',
    source: 'API field summary.comparison.savings_vs_price_blind.extra_stops, put into words by format.js derive.',
    how: 'When the plan’s stops − the driver’s stops is below zero: that number, without its sign.',
    live: (c) => EXPLAINERS['derived.extra_stops_text'].live(c),
    why: 'The plan can also stop less, for example when it fills up at a cheap stop and goes further. The page says so when it happens.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/services/planner.py:_build_comparison'],
  },

  'derived.costs_more': {
    title: 'The plan costs more',
    what: 'How much more the plan costs than the driver who ignores prices, when that happens.',
    source: 'API field summary.comparison.savings_vs_price_blind.amount, read by format.js derive.',
    how: 'When the saving is below zero: costs more = − saving.',
    live: (c) => savingsLines(c)[0],
    why: 'Possible only with “Skip tiny stops” on, because each merge may add a little. The page tells the truth instead of hiding a bad result.',
    code: ['fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/services/planner.py:_savings', 'fuelroute/services/optimizer.py:_consolidate'],
    see: ['concept:skip-tiny-stops'],
  },

  'concept:station-price': {
    title: 'The price of a truck stop',
    what: 'The price per gallon used for each truck stop.',
    source: 'The price file, loaded into the station table (FuelStation.price) by load_stations.',
    how: 'A stop listed several times (different prices, no date) gets the median of its quotes by default: the middle one, or the mean of the two middle ones, with four decimals. Its lowest and highest quotes are kept too: price_policy=min or max uses them.',
    live: (c) => {
      const p = aboutData(c).price_per_gallon;
      const policy = veh(c).price_policy;
      need(p?.median);
      return [`Whole file: lowest ${price4(p.min)}, median ${price4(p.median)}, highest ${price4(p.max)}.`,
        policy ? `This answer uses price_policy=${policy}.` : null,
        present(aboutData(c).stations_with_several_quotes) ? `${fmt.int(aboutData(c).stations_with_several_quotes)} truck stops are listed more than once.` : null];
    },
    why: 'Without dates there is no “latest” price, and the median is not moved much by one odd quote. The page shows three decimals because US pumps show prices to a tenth of a cent; the math keeps four so the cents stay exact.',
    code: [
      'fuelroute/services/station_loader.py:StationRow.price', 'fuelroute/services/stations.py:StationArrays.priced',
      'fuelroute/models.py:FuelStation', 'fuelroute/services/planner.py:_money_rows',
    ],
    edge: 'The page shows prices with three decimals; the API and the math use four. Prices are a snapshot of the file, not live prices.',
    see: ['concept:price-policy', 'concept:price-file'],
  },

  // ----- step 5: the truck -----
  'about.vehicle.min_stop_gallons': MIN_STOP,
  'about.vehicle.max_consolidation_cost': MAX_FIX,

  'concept:quick-tries': {
    title: 'One-click tries',
    what: 'Four experiments, each changing one thing on the standard truck, on the same trip.',
    source: 'whatif.js QUICK_TRIES (the demo values live in the JavaScript, not in the template); the default tank comes from /api/about.',
    how: 'A thirstier truck: fewer miles per gallon with the same tank, so the range drops (range = tank × mpg). Safety fuel keeps some gallons in the tank everywhere; full tank leaves full; Skip tiny stops sends consolidate=true. Each sends only what differs from the defaults, on the same trip.',
    live: (c) => {
      const v = aboutVehicle(c);
      need(v.tank_gallons);
      const thirsty = QUICK_TRIES.find((t) => t.id === 'thirsty');
      const safety = QUICK_TRIES.find((t) => t.id === 'safety');
      return [`Thirstier: the same ${fmt.gal(v.tank_gallons)} tank × ${fmt.num(thirsty.set.mpg)} mpg = ${fmt.miles_short(v.tank_gallons * thirsty.set.mpg)}.`,
        `Safety fuel: ${fmt.gal(safety.set.safety_reserve_gal)}.`];
    },
    why: 'Each try starts from the standard truck, so “What changed” shows the effect of one change. The road is already saved, so a try costs no OSRM call (a test plans every try with zero external calls).',
    code: [
      'fuelroute/static/fuelroute/js/whatif.js:QUICK_TRIES', 'fuelroute/static/fuelroute/js/whatif.js:quickTrySettings',
      'fuelroute/static/fuelroute/js/main.js:tryQuick', 'fuelroute/tests/test_web.py:test_every_quick_try_is_planned_without_an_external_call',
    ],
    see: ['concept:standard-truck'],
  },

  'concept:defaults-and-limits': {
    title: 'Defaults and limits',
    what: 'The “Default: …” chip of each setting is what the server uses when the parameter is left out; the slider and box limits are what the API accepts.',
    source: '/api/about → api.params (default, min and max of each parameter) ← PlanSettings.defaults() and the serializer’s ranges (WHAT_IF_RANGES).',
    how: 'Defaults: the published default, else the vehicle block of /api/about, else a fallback in settings.js. Limits: miles per gallon from the API; the tank = the range limits ÷ mpg, rounded inward to half a gallon; safety fuel from zero to just under the tank. Recomputed on every edit.',
    live: (c) => {
      const params = c?.about?.api?.params || [];
      need(params.length || null);
      return params.filter((p) => present(p.default) && (present(p.min) || present(p.max))).map((p) => limitLine(c, p));
    },
    why: 'One source of truth: change an environment variable and the page follows. Browser limits are only a convenience: the server checks every value again (a 400, the input is wrong, with the range), so Postman gets the same rules.',
    code: [
      'fuelroute/static/fuelroute/js/settings.js:defaultsFrom', 'fuelroute/static/fuelroute/js/settings.js:rangeOf',
      'fuelroute/static/fuelroute/js/settings.js:tankLimits', 'fuelroute/services/about.py:_params',
      'fuelroute/services/planner.py:WHAT_IF_RANGES', 'fuelroute/serializers.py:SettingFloatField',
    ],
  },

  'concept:miles-per-gallon': {
    title: 'Setting: miles per gallon',
    what: 'How many miles the truck drives on one gallon: the number that turns miles into gallons everywhere.',
    source: 'Default from /api/about; your value is sent as ?mpg= only when it differs.',
    how: 'Fuel burned = distance ÷ mpg. The optimizer counts fuel in miles and converts with gallons = miles ÷ mpg. Changing mpg keeps the tank box, so the range sent becomes tank × mpg.',
    live: (c) => EXPLAINERS['route.summary.fuel_used_gallons'].live(c),
    why: 'The brief fixes the standard value; making it a parameter shows the planner is not hard-coded to one truck.',
    code: [
      'fuelroute/services/planner.py:PlanSettings.defaults', 'fuelroute/services/planner.py:WHAT_IF_RANGES',
      'fuelroute/serializers.py:RouteRequestSerializer', 'fuelroute/static/fuelroute/js/whatif.js:createTruck',
    ],
    edge: 'An empty box means the default. Unreadable text is sent as typed, so the server’s 400 names the field.',
    see: ['concept:defaults-and-limits'],
  },

  'concept:tank-size': {
    title: 'Setting: tank size',
    what: 'How many gallons the tank holds. Only the page asks for it: the API takes the range instead.',
    source: 'Default tank = default range ÷ default mpg (both from /api/about). It is sent as max_range_miles = tank × mpg.',
    how: 'When a plan or a link loads, the box = range ÷ mpg, rounded to the hundredth. Limits: the range limits ÷ mpg, rounded inward to half a gallon.',
    live: (c) => EXPLAINERS['about.vehicle.tank_gallons'].live(c),
    why: 'People think in tank size; the brief and the API speak in range. Keeping the tank when mpg changes makes a thirstier truck go less far, as it should.',
    code: [
      'fuelroute/static/fuelroute/js/settings.js:tankGallons', 'fuelroute/static/fuelroute/js/settings.js:tankLimits',
      'fuelroute/static/fuelroute/js/settings.js:rangeOfTank', 'fuelroute/static/fuelroute/js/whatif.js:createTruck',
      'fuelroute/tests/test_js_logic.py:test_the_tank_controls_turn_a_tank_and_mileage_into_a_range',
    ],
    edge: 'An old version grew the tank when mpg changed; a test now guards against it.',
    see: ['concept:range-on-full-tank'],
  },

  'concept:range-on-full-tank': {
    title: 'Range on a full tank (what is sent)',
    what: 'How far a full tank takes the truck: exactly what the API receives as max_range_miles.',
    source: 'Computed in the browser from the two boxes (tank and miles per gallon).',
    how: 'range = tank × mpg, rounded to the hundredth; a dash if a box cannot be read. In the optimizer this is the tank’s capacity, in miles.',
    live: (c) => {
      const v = veh(c);
      need(v.tank_gallons, v.miles_per_gallon, v.max_range_miles);
      return `Plan on screen: max_range_miles ${fmt.miles_short(v.max_range_miles)}, which is a ${fmt.gal(v.tank_gallons)} tank (${fmt.miles_short(v.max_range_miles)} ÷ ${fmt.num(v.miles_per_gallon)} mpg).`;
    },
    why: 'The formula is shown so you see what is sent; the server checks it again.',
    code: ['fuelroute/static/fuelroute/js/whatif.js:createTruck', 'fuelroute/static/fuelroute/js/settings.js:rangeOfTank', 'fuelroute/services/planner.py:WHAT_IF_RANGES'],
  },

  'concept:safety-fuel': {
    title: 'Setting: safety fuel',
    what: 'Gallons that must still be in the tank when the truck reaches any stop and the finish.',
    source: 'Default from /api/about (none); your value is sent as ?safety_reserve_gal=.',
    how: 'The optimizer solves the same problem with a smaller tank: usable range = range − safety × mpg, start and arrival lowered by as much, and the safety fuel added back to every reported level. It must be less than the tank (else a 400: wrong input).',
    live: (c) => EXPLAINERS['route.vehicle.safety_reserve_gal'].live(c),
    why: 'Real drivers do not plan to arrive empty. Fuel only goes down between purchases, so keeping a reserve is just a smaller tank: the same rules and the same proof.',
    code: [
      'fuelroute/serializers.py:RouteRequestSerializer', 'fuelroute/services/planner.py:PlanSettings.safety_reserve_miles',
      'fuelroute/services/optimizer.py:_usable', 'fuelroute/tests/test_optimizer.py:test_safety_reserve_is_the_optimum_of_a_smaller_tank',
    ],
    edge: 'A stretch with no truck stop longer than the usable range becomes a 422 no_reachable_fuel_station (this truck cannot make the trip).',
    see: ['route.vehicle.usable_range_miles'],
  },

  'concept:tank-when-leaving': {
    title: 'Setting: tank when leaving',
    what: 'How much fuel the truck has at the start, and so what the bill counts.',
    source: 'Labels and help are the API’s own text (START_TANK_LABELS, START_TANK_HELP, in /api/about); the value is ?start_tank=empty|full. The radios belong to the trip form, so they apply to the next trip too.',
    how: 'Almost empty (default): leave with the start reserve, or enough to reach the first truck stop, and arrive with the same: bought = burned. Full: leave with a free full tank; only the fuel bought on the way is priced.',
    live: (c) => {
      const modes = c?.about?.api?.start_tank_modes || [];
      need(modes.length || null);
      return [...modes.map((m) => `${m.value}: “${m.label}”.`), answer(c) ? `The answer on screen: start_tank=${veh(c).start_tank}.` : null];
    },
    why: '“Almost empty” makes the bill the true cost of the miles driven, so trips compare fairly. “Full” is the other reasonable reading of the brief, so it is offered, and the page refuses to compare it dollar for dollar.',
    code: [
      'fuelroute/services/planner.py:START_TANK_LABELS', 'fuelroute/services/planner.py:START_TANK_HELP',
      'fuelroute/services/planner.py:_tank_rules', 'fuelroute/web.py:_start_tank_modes',
      'fuelroute/tests/test_web.py:test_start_tank_labels_are_the_apis',
    ],
    edge: 'In the API the value is named empty; it means almost empty: fifty miles of fuel by default (START_RESERVE_MILES), or enough to reach the first truck stop.',
    see: ['concept:pay-for-every-mile', 'concept:not-comparable-full-tank'],
  },

  'concept:price-policy': {
    title: 'Setting with no control: price policy',
    what: 'Which price to use for a truck stop listed several times: median (default), min or max.',
    source: '?price_policy=median|min|max; only a link or an API call can set it.',
    how: 'median: the stored price; min: its lowest quote; max: its highest. The planner switches which price column it reads, without copying the stations. Only stations with several quotes change.',
    live: (c) => {
      need(veh(c).price_policy);
      return `The answer on screen uses ${veh(c).price_policy}.`;
    },
    why: 'The file repeats some stations with different prices and no date, so there is no “latest” price. The median is robust; min and max give the best and the worst case.',
    code: [
      'fuelroute/services/station_loader.py:StationRow.price', 'fuelroute/services/stations.py:StationArrays.priced',
      'fuelroute/services/stations.py:PRICE_POLICIES', 'fuelroute/static/fuelroute/js/format.js:hiddenSettingsText',
    ],
  },

  'concept:not-planned-yet': {
    title: '“Not planned yet”',
    what: 'Your settings in this step differ from the plan on screen.',
    source: 'The browser (main.js updateStrips).',
    how: 'Shown when a plan exists and the controls differ from its settings. A setting card is highlighted when its value differs from the default.',
    why: 'Nothing is sent until you press “Plan again” (or Enter), so the page says when the screen and the controls disagree.',
    code: ['fuelroute/static/fuelroute/js/main.js:updateStrips', 'fuelroute/static/fuelroute/js/whatif.js:createTruck', 'fuelroute/static/fuelroute/js/settings.js:sameSettings'],
  },

  'concept:standard-truck': {
    title: 'The standard truck',
    what: 'The truck of the brief, on the same trip: the yardstick of “What changed” and of the For Spotter step.',
    source: 'A second GET /api/route with only start, finish and start_tank=empty (no truck settings).',
    how: 'Miles per gallon and range from the configuration (/api/about), no safety fuel, leaves almost empty, tiny stops kept, the default search distance and median prices. The page asks for it once per trip; the server reuses the saved road.',
    live: (c) => {
      const v = aboutVehicle(c);
      need(v.miles_per_gallon);
      const s = c?.standard;
      return [`${fmt.num(v.miles_per_gallon)} mpg, ${fmt.gal(v.tank_gallons)} tank, ${fmt.miles_short(v.max_range_miles)} on a full tank.`,
        s ? `On this trip: ${fmt.money(s.summary.total_fuel_cost)}, ${plural(s.summary.number_of_stops, 'stop')}.` : null];
    },
    why: 'Both plans share the same road, so the table shows only the effect of the truck.',
    code: ['fuelroute/static/fuelroute/js/main.js:rememberBaseline', 'fuelroute/static/fuelroute/js/main.js:standardTruck', 'fuelroute/static/fuelroute/js/main.js:renderTruck', 'fuelroute/services/osrm.py:get_route'],
    edge: 'While it loads the box says so; if it fails, “not available to compare”.',
    see: ['concept:standard-truck-baseline-request'],
  },

  'concept:compare-fuel-cost': {
    title: '“What changed”: fuel cost',
    what: 'The total paid for fuel by each truck on this trip, and the difference.',
    source: 'API field summary.total_fuel_cost of both answers; the difference is computed in the browser (whatif.js compareRows).',
    how: 'difference = yours − standard; “same” under half a cent; red when it costs more, green when less; “not comparable” when yours leaves full and the standard does not.',
    live: (c) => {
      const a = summ(c).total_fuel_cost;
      const b = c?.standard?.summary?.total_fuel_cost;
      need(a, b);
      return `${fmt.money(a)} − ${fmt.money(b)} = ${fmt.money_signed(a - b)}.`;
    },
    why: 'Exact money on both sides, so the difference is real; the half-cent threshold hides floating-point noise.',
    code: ['fuelroute/static/fuelroute/js/whatif.js:compareRows', 'fuelroute/static/fuelroute/js/whatif.js:renderCompare', 'fuelroute/services/planner.py:_money_rows'],
    edge: 'With a full tank the first tank is free, so a lower total is not a saving.',
    see: ['concept:not-comparable-full-tank'],
  },

  'concept:compare-fuel-stops': {
    title: '“What changed”: fuel stops',
    what: 'How many times each truck stops to buy fuel.',
    source: 'API field summary.number_of_stops of both answers.',
    how: 'difference = yours − standard, a whole number: “same”, “+1”, “−2”. Red when there are more stops. Shown even when the totals are not comparable.',
    live: (c) => {
      const a = summ(c).number_of_stops;
      const b = c?.standard?.summary?.number_of_stops;
      need(a, b);
      return `${fmt.int(a)} − ${fmt.int(b)} = ${fmt.int(a - b)}.`;
    },
    why: 'Stops cost a driver time, so fewer stops matter even when dollars barely change.',
    code: ['fuelroute/static/fuelroute/js/whatif.js:compareRows', 'fuelroute/services/planner.py:_build_summary'],
  },

  'concept:compare-fuel-bought': {
    title: '“What changed”: fuel bought',
    what: 'The gallons each truck buys on the way.',
    source: 'API field summary.total_gallons_purchased of both answers.',
    how: 'difference = yours − standard; “same” under half a hundredth of a gallon. Red when more. “not comparable” for a full tank against an almost empty one.',
    live: (c) => {
      const a = summ(c).total_gallons_purchased;
      const b = c?.standard?.summary?.total_gallons_purchased;
      need(a, b);
      return `${fmt.gal(a)} − ${fmt.gal(b)} = ${fmt.num(a - b)} gallons.`;
    },
    why: 'It tells a change that buys more fuel (worse mileage) from one that buys the same fuel at other stops (safety fuel, merging).',
    code: ['fuelroute/static/fuelroute/js/whatif.js:compareRows', 'fuelroute/services/planner.py:_money_rows'],
    edge: 'With an almost empty start, bought = burned, so it changes only with miles per gallon.',
  },

  'concept:not-comparable-full-tank': {
    title: '“Not comparable”',
    what: 'An honesty guard: a free full tank makes a total look cheaper, so the page does not compare it dollar for dollar.',
    source: 'Computed in the browser from both answers (whatif.js).',
    how: 'Not comparable = your truck leaves full and the standard one does not. Then the note compares the average price per gallon of each (total ÷ gallons, from the API).',
    live: (c) => {
      const a = summ(c).average_price_paid;
      const b = c?.standard?.summary?.average_price_paid;
      need(a, b);
      return `Price per gallon: ${price4(a)} (yours) vs ${price4(b)} (standard).`;
    },
    why: 'A free first tank is not a saving. Price per gallon is the fair comparison when one truck got its first tank free.',
    code: ['fuelroute/static/fuelroute/js/whatif.js:compareRows', 'fuelroute/static/fuelroute/js/whatif.js:answerText', 'fuelroute/services/planner.py:_average_price'],
    edge: 'An old version showed the lower total as a saving; a test now guards against it.',
  },

  'concept:why-changed': {
    title: 'The “Why” list',
    what: 'One sentence per setting that changed, chosen from the two answers’ own numbers.',
    source: 'Computed in the browser (whatif.js whyChanged).',
    how: 'One sentence per change, from both answers: a free full tank; another mpg (fuel burned); another tank (how far it lasts); safety fuel (a shorter usable range); Skip tiny stops (stops removed or money moved). Same gallons at another cost: only where they buy changed. Nothing different: “changed nothing”.',
    live: (c) => {
      const a = summ(c).fuel_used_gallons;
      const b = c?.standard?.summary?.fuel_used_gallons;
      need(a, b);
      return `Fuel burned: ${fmt.gal(a)} (yours) vs ${fmt.gal(b)} (standard).`;
    },
    why: 'Every sentence is true for this trip, because it is picked from the numbers, not written in advance (tested in Node).',
    code: ['fuelroute/static/fuelroute/js/whatif.js:whyChanged', 'fuelroute/tests/test_js_logic.py:test_compare_is_honest_with_a_full_tank_and_says_why'],
  },

  'concept:routing-requests-line': {
    title: 'Requests after a truck change',
    what: 'How many calls this answer made to OSRM, the free routing service.',
    source: 'API fields meta.external_api_calls and meta.external_api_services.',
    how: 'No “osrm” entry in meta.external_api_services → “No new request to the routing service” with a check mark; otherwise the number of “osrm” entries (a place search does not count).',
    live: (c) => EXPLAINERS['derived.calls_osrm_text'].live(c),
    why: 'The brief asks to call the routing API as little as possible. The road is saved by its coordinates, so a new truck costs no call, and the page proves it.',
    code: ['fuelroute/static/fuelroute/js/whatif.js:renderCompare', 'fuelroute/services/planner.py:_meta', 'fuelroute/services/osrm.py:get_route'],
    see: ['concept:route-cache-vs-plan-cache'],
  },

  // ----- step 6: for Spotter -----
  'concept:standard-truck-answer': {
    title: 'Why this table uses the standard truck',
    what: 'Every value of this table comes from the standard truck’s answer for the same trip, even if the other steps show your truck.',
    source: 'The “standard” root: the answer of a second GET /api/route with no truck settings (or the answer on screen, when it is already the standard truck).',
    how: 'main.js keeps that answer per trip and passes it to the page as standard; derive() computes derived.standard.* from it.',
    live: (c) => (c?.standard ? `Loaded: ${c.standard.start?.label} → ${c.standard.finish?.label}, ${fmt.money(c.standard.summary?.total_fuel_cost)}.` : (answer(c) ? 'Not loaded yet: the column shows dashes.' : null)),
    why: 'After “a thirstier truck” the table must not print the standard miles per gallon next to a total planned at another mileage. A test forbids route.* values in this step. The extra request costs no OSRM call: the road is already saved.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:rememberBaseline', 'fuelroute/static/fuelroute/js/main.js:standardBody',
      'fuelroute/static/fuelroute/js/main.js:context', 'fuelroute/tests/test_web.py:test_for_spotter_step_always_shows_the_standard_truck',
    ],
    see: ['concept:standard-truck-baseline-request', 'concept:standard-truck'],
  },

  'standard.start.label': withStandard(START_LABEL, { sameForEvery: true }),
  'standard.finish.label': withStandard(FINISH_LABEL, { sameForEvery: true }),
  'standard.route.distance_miles': withStandard(DISTANCE, { sameForEvery: true }),
  'derived.standard.stops_text': withStandard(STOPS_TEXT),
  'derived.standard.savings_amount': withStandard(SAVINGS),
  'standard.vehicle.usable_range_miles': withStandard(USABLE_RANGE, { title: 'Limit between stops (standard truck)' }),
  'standard.summary.total_fuel_cost': withStandard(TOTAL_COST),
  'standard.summary.total_gallons_purchased': withStandard(GALLONS_BOUGHT),

  'derived.standard.longest_stretch': {
    title: 'Longest drive between stops (standard truck)',
    what: 'The longest distance the standard truck drives without buying fuel: from the start to stop 1, between two stops, or from the last stop to the finish.',
    source: 'Computed in the browser (checks.js longestStretch) from the standard truck’s answer.',
    how: 'miles = [0, each stop’s mile marker, the distance]; the answer is the largest gap between two neighbours.',
    live: (c) => {
      const s = c?.standard;
      need(s?.route?.distance_miles);
      const miles = [0, ...(s.fuel_stops || []).map((x) => x.mile_marker), s.route.distance_miles];
      const names = ['the start', ...(s.fuel_stops || []).map((x) => `stop ${x.stop}`), 'the finish'];
      let best = 1;
      for (let i = 2; i < miles.length; i++) if (miles[i] - miles[i - 1] > miles[best] - miles[best - 1]) best = i;
      return `From ${names[best - 1]} (mile ${fmt.dec1(miles[best - 1])}) to ${names[best]} (mile ${fmt.dec1(miles[best])}): ${fmt.miles(longestStretch(s))}; the limit is ${fmt.miles_short(s.vehicle.usable_range_miles)}.`;
    },
    why: 'A visible proof that the range is respected: no stretch is longer than one usable tank.',
    code: ['fuelroute/static/fuelroute/js/checks.js:longestStretch', 'fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/tests/test_js_logic.py:test_longest_stretch'],
    edge: 'The first stretch is driven on the fuel the truck leaves with.',
    see: ['concept:tank-between-empty-and-full'],
  },

  'concept:offline-places-list': {
    title: 'The built-in list of US places',
    what: 'A file shipped with the code (data/us_places.csv.gz): about one hundred ninety thousand US places with their coordinates, from the US Census Gazetteer and GeoNames.',
    source: 'A data file, built by scripts/build_places_dataset.py and loaded once per server process.',
    how: 'Loaded into numpy arrays (fast tables of numbers) when the server starts. lookup(city, state) returns the most populated place with that name in that state. The same list gives truck stops their city center (when no exact place is known) and answers /api/places.',
    why: 'Finding a typed city costs no network call and under a millisecond, so the only external call of a new trip is the road.',
    code: ['fuelroute/services/places.py:get_place_index', 'fuelroute/services/geocoding.py:_offline', 'fuelroute/warmup.py:warm_up'],
    edge: 'Same-name places in one state: the most populated wins; for an exact point type “lat,lon”.',
    see: ['concept:geocoding'],
  },

  'concept:map-in-the-api-answer': {
    title: 'The map in the API answer',
    what: 'Every /api/route answer carries map.geojson (the road, the start, the finish, one point per stop) and map.map_url (a link to this page with the same trip).',
    source: 'API fields map.geojson and map.map_url.',
    how: 'GeoJSON is a standard map-data format. The answer holds a list of shapes (a FeatureCollection): a line for the simplified road, a point for each end with its label, and a point per stop with its name, price, gallons and cost. map_url = /api/route/map?start&finish&start_tank, plus changed settings.',
    why: 'A Postman user gets a map without this page: paste the GeoJSON in any map viewer, or open map_url. While the plan is saved, opening map_url costs no external call.',
    code: ['fuelroute/services/planner.py:_build_geojson', 'fuelroute/services/planner.py:map_path', 'fuelroute/static/fuelroute/js/map.js:createMap'],
    see: ['concept:route-map-line'],
  },

  'concept:tank-between-empty-and-full': {
    title: 'The tank never goes below empty or above full',
    what: 'The rule every plan obeys: the fuel is always between zero (or the safety fuel) and a full tank.',
    source: 'Enforced by the optimizer; shown by the Tank gauge during Play.',
    how: 'The optimizer only drives to stops it can reach with the fuel it has, and never buys more than the free space of the tank. The answer clips each arrival so arrival + purchase never shows above full after rounding.',
    why: 'It is the physical rule of the brief. If a stretch longer than one tank has no truck stop, the API answers 422 no_reachable_fuel_station and names the gap instead of inventing a plan.',
    code: ['fuelroute/services/optimizer.py:plan_fuel_stops', 'fuelroute/services/planner.py:_build_stops', 'fuelroute/services/planner.py:_unreachable', 'fuelroute/tests/test_optimizer.py:test_long_trip_needs_several_stops_and_never_runs_dry'],
    see: ['derived.standard.longest_stretch'],
  },

  'about.data.stations': {
    title: 'US truck stops in the file',
    what: 'The number of US truck stops in the price file: one per OPIS ID.',
    source: '/api/about → data.stations.',
    how: 'The number of rows of the station table. load_stations merges repeated OPIS IDs and keeps only US states (Canadian stops are skipped).',
    live: (c) => EXPLAINERS['concept:price-file'].live(c),
    why: 'OPIS ID is the file’s ID of a truck stop, so repeated rows are the same stop quoted twice: one station, priced with the median. Canadian rows are dropped because the place list, the USA outline and the brief are US-only.',
    code: ['fuelroute/services/about.py:_data_summary', 'fuelroute/services/station_loader.py:load_stations', 'fuelroute/services/station_loader.py:parse_price_file'],
    edge: 'It counts the whole file, not this trip.',
    see: ['concept:price-file'],
  },

  'about.data.not_geocoded': {
    title: 'Truck stops left out',
    what: 'US truck stops left out of every plan because they could not be placed on the map safely.',
    source: '/api/about → data.not_geocoded.',
    how: 'stations − placed stations.',
    live: (c) => EXPLAINERS['about.data.geocoded'].live(c),
    why: 'Leaving a station out is safer than placing it hundreds of miles from where it is.',
    code: [
      'fuelroute/services/about.py:_data_summary', 'fuelroute/services/station_loader.py:geocode_rows',
      'fuelroute/services/station_loader.py:read_station_coords',
    ],
    edge: 'They have no exact place, and a city found in neither list or with same-name towns in one state (more than twenty-five miles apart) that no clue could resolve.',
  },

  'derived.trip_osrm_text': {
    title: 'Requests to OSRM for this trip',
    what: 'How many requests this page caused to OSRM for this trip, counting every truck tried.',
    source: 'Computed in the browser from meta.external_api_services of every answer for this trip (the standard truck’s included).',
    how: 'main.js keeps a running total per trip (“start|finish”): after each /api/route answer it adds the number of “osrm” entries.',
    live: (c) => {
      need(der(c).trip_osrm_text);
      return `So far: ${der(c).trip_osrm_text}${der(c).trip_osrm_none ? ' (the road was already saved on the server)' : ''}.`;
    },
    why: 'The proof uses the API’s own counter, not a sentence typed by hand. A new trip costs one call; a repeat, another truck or the standard truck’s answer cost none.',
    code: [
      'fuelroute/static/fuelroute/js/main.js:countCalls', 'fuelroute/static/fuelroute/js/main.js:osrmCalls',
      'fuelroute/static/fuelroute/js/format.js:derive', 'fuelroute/services/http.py:ExternalApiClient.get_json',
      'fuelroute/tests/test_api.py:test_route_returns_plan_map_and_uses_one_external_call',
    ],
    edge: 'It lives in the page’s memory: a reload starts again from zero. A retry after an OSRM failure counts. Nominatim calls are not in this count.',
    see: ['concept:route-cache-vs-plan-cache'],
  },

  'concept:warm-up-in-memory': {
    title: 'Loaded in memory once',
    what: 'Work done once when the server process starts, so the first user does not pay for it.',
    source: 'The server (config/wsgi.py runs warm_up()).',
    how: 'It loads the places list, builds the suggestion index, loads the outline of the USA and the station arrays (numpy: fast tables of numbers), and runs numpy once on coast-to-coast sizes. The station arrays reload by themselves when the data version (row count + highest id) changes.',
    why: 'Everything a request needs, except the road, is already in memory: a saved trip answers in a few milliseconds, and finding the stations near a road takes tens of milliseconds.',
    code: ['fuelroute/warmup.py:warm_up', 'config/wsgi.py', 'fuelroute/services/stations.py:get_station_arrays', 'fuelroute/services/stations.py:data_version'],
    edge: 'Each request reads the version with one small query (row count and highest id). load_stations replaces every row with new ids, so a reload is always noticed; a price edited in place from a shell would not be, until a restart.',
    see: ['client.server_ms', 'concept:pipeline'],
  },

  'about.versions.django': {
    title: 'Django version',
    what: 'The Django version the server is running right now.',
    source: '/api/about → versions.django.',
    how: 'django.get_version(), read when the server runs.',
    live: (c) => {
      const v = c?.about?.versions;
      need(v?.django);
      return `Django ${v.django}, Django REST framework ${v.djangorestframework}, Python ${v.python}.`;
    },
    why: 'Read from the running code, so the page cannot claim a version that is not installed.',
    code: ['fuelroute/services/about.py:_versions'],
    see: ['concept:how-django-is-used'],
  },

  'concept:how-django-is-used': {
    title: 'How Django is used',
    what: 'One request’s path: the URL → RoutePlanView (a Django REST framework view, GET and POST) → RouteRequestSerializer (checks every input; a 400 names the field) → planner.plan_trip (the services) → the JSON renderer, wrapped by middlewares (code around every request: timing, gzip, rate limit).',
    source: 'fuelroute/views.py, serializers.py, renderers.py and config/settings.py (MIDDLEWARE, CACHES); requirements.txt pins the versions.',
    how: 'manage.py load_stations loads the price file once into the FuelStation model (SQLite). Two in-memory caches (LocMem) keep plans and roads. The services take plain values, not requests, and the optimizer is pure Python with no Django at all.',
    live: (c) => EXPLAINERS['about.versions.django'].live(c),
    why: 'Django REST framework gives input checks, one error shape everywhere and a browsable API in debug, with little code. Keeping the logic out of the views lets it be tested directly, without HTTP.',
    code: [
      'fuelroute/urls.py', 'fuelroute/views.py:RoutePlanView', 'fuelroute/serializers.py:RouteRequestSerializer',
      'fuelroute/renderers.py:TimedJSONRenderer', 'config/settings.py:MIDDLEWARE', 'config/settings.py:CACHES',
      'fuelroute/models.py:FuelStation', 'fuelroute/management/commands/load_stations.py', 'requirements.txt',
    ],
    edge: 'The exact versions are pinned in requirements.txt and read at run time by /api/about, so this page shows what really runs.',
    see: ['about.versions.django', 'concept:pipeline'],
  },

  'concept:how-it-is-tested': {
    title: 'How it is tested',
    what: 'More than nine hundred pytest tests that run in seconds with no network: the one function that does HTTP fails in every test, and OSRM is replaced by a fake that counts the calls.',
    source: 'The fuelroute/tests folder; run it with python -m pytest.',
    how: 'The optimizer is refereed by an exact method on ninety small random roads, the corridor search by brute force, and the API is checked for one call per new trip and cents that add up. The page’s JavaScript runs in Node from pytest, and every card here is checked.',
    why: 'Each test pins a claim the page or the README makes, so a claim cannot drift from the code. Pure functions (no screen, no network) make the logic testable in milliseconds.',
    code: [
      'fuelroute/tests/test_optimizer.py:test_greedy_matches_exact_dp', 'fuelroute/tests/test_geo.py:test_corridor_search_matches_brute_force',
      'fuelroute/tests/test_api.py:test_route_returns_plan_map_and_uses_one_external_call', 'fuelroute/tests/test_js_logic.py:run_js',
      'fuelroute/tests/test_web.py:test_page_has_no_hand_written_numbers', 'fuelroute/tests/test_explain.py:test_every_live_value_has_an_explainer',
      'fuelroute/tests/test_what_if.py:test_plans_match_the_golden_file', 'fuelroute/tests/conftest.py:clean_state',
    ],
    edge: 'The Node tests are skipped when Node.js is not installed. A golden file (answers saved before the truck settings existed) keeps the default plans unchanged.',
    see: ['concept:why-rules-are-optimal', 'concept:data-live-rule'],
  },

  'concept:json-link': {
    title: 'This trip as JSON',
    what: 'A link to the raw API answer for this trip, with the standard truck.',
    source: 'The browser builds the link (main.js update).',
    how: '/api/route?start=…&finish=…&start_tank=empty, with the trip as it was typed.',
    why: 'It shows the page is only a client of the public API: the same request Postman sends. Opening it while the plan is saved costs no external call.',
    code: ['fuelroute/static/fuelroute/js/main.js:update', 'fuelroute/static/fuelroute/js/api.js:routeUrl', 'fuelroute/tests/test_web.py:test_the_page_asks_exactly_what_postman_sends'],
  },

  'concept:footer-credits': {
    title: 'Credits',
    what: 'Where each data source comes from, and the required attributions.',
    source: 'Template text; the GitHub link comes from /api/about (deliverables.repo_url).',
    how: 'Fixed text. “CC BY 4.0” is marked as a literal, because the page rule forbids digits in the template text.',
    why: 'GeoNames (CC BY 4.0) and OpenStreetMap require attribution; the exact truck stop positions are OpenStreetMap data too (ODbL). The OSRM demo server and Nominatim are free and need no key.',
    code: ['fuelroute/templates/fuelroute/partials/_footer.html', 'fuelroute/services/about.py:_deliverables'],
  },
};

// --- turning an entry into a card ----------------------------------------------------------

export function isConcept(key) {
  return String(key).startsWith('concept:');
}

// The accessible name of the "i" button of an entry.
export function ariaLabel(key) {
  const entry = EXPLAINERS[key];
  if (!entry) return null;
  return `Explain: ${entry.title}`;
}

// The first sentence of "what": the short tooltip of the button.
export function summary(key) {
  const what = EXPLAINERS[key]?.what || '';
  const end = what.search(/\.(\s|$)/);
  return end < 0 ? what : what.slice(0, end + 1);
}

// 'fuelroute/services/planner.py:_build_summary' -> {file, symbol}.
export function codeRef(ref) {
  const at = ref.indexOf(':');
  return at < 0 ? { file: ref, symbol: null } : { file: ref.slice(0, at), symbol: ref.slice(at + 1) };
}

// The sections of a card: {key, kind, title, what, source, how, live, why, code, edge,
// see, error}. live: this trip's lines (empty before a plan, or when the value does not
// apply); error: a live line that failed (a bug: the tests require none).
export function explain(key, ctx = {}, arg = null) {
  const entry = EXPLAINERS[key];
  if (!entry) return null;
  let live = [];
  let error = null;
  if (entry.live) {
    try {
      const out = entry.live(ctx || {}, arg);
      live = (Array.isArray(out) ? out : [out]).filter((line) => typeof line === 'string' && line.trim() !== '');
    } catch (err) {
      if (err !== MISSING) error = String(err?.message || err);
      live = [];
    }
  }
  return {
    key,
    kind: isConcept(key) ? 'concept' : 'value',
    title: entry.title,
    what: entry.what,
    source: entry.source,
    how: entry.how,
    live,
    why: entry.why,
    code: [...entry.code],
    edge: entry.edge || null,
    see: (entry.see || []).filter((other) => other !== key && other in EXPLAINERS),
    error,
  };
}
