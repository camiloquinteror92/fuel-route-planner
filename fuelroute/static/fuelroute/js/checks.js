// Contract checks: invariants of a /api/route answer, recomputed in the browser.
//
// Pure functions (no DOM). Money is checked in integer arithmetic: gallons as
// hundredths, prices as ten-thousandths, costs as cents, rounding half up, the
// same rule the server applies with Decimal. Each check returns
// {id, label, ok: true | false | null, expected, actual, detail}; null = not
// applicable to this answer. A failure is reported, never hidden.

const cents = (value) => Math.round(Number(value) * 100);
const e4 = (value) => Math.round(Number(value) * 10000);

// round(n / d) half up, for integers (n >= 0, d > 0).
function divHalfUp(n, d) {
  return n >= 0 ? Math.floor((2 * n + d) / (2 * d)) : -Math.floor((-2 * n + d) / (2 * d));
}

const money = (c) => `$${(c / 100).toFixed(2)}`;
const gallons = (c) => `${(c / 100).toFixed(2)} gal`;

function result(id, label, ok, expected, actual, detail = '') {
  return { id, label, ok, expected, actual, detail };
}

// Fuel in the tank along the route, from the answer itself:
// [{mile, fuel, kind: 'start' | 'arrive' | 'depart' | 'end', stop}], plus min,
// max and the longest stretch between purchases.
// `anchor`: each arrival is the API's own fuel_on_arrival_gallons (and the end its
// end_fuel_gallons), so the rounding of the mile markers does not pile up over a long
// trip (a 62.5-gal tank once showed 62.53). Without it everything is recomputed from
// the start fuel, the legs and the purchases: the contract check compares the two.
export function tankTrace({ distance, mpg, startFuel, stops, endFuel = null, anchor = false }) {
  const points = [{ mile: 0, fuel: startFuel, kind: 'start', stop: null }];
  let fuel = startFuel;
  let prev = 0;
  let longest = 0;
  for (const s of stops) {
    fuel -= (s.mile_marker - prev) / mpg;
    const api = s.fuel_on_arrival_gallons;
    if (anchor && Number.isFinite(api)) fuel = api;
    points.push({ mile: s.mile_marker, fuel, kind: 'arrive', stop: s.stop ?? null, api });
    longest = Math.max(longest, s.mile_marker - prev);
    fuel += s.gallons;
    points.push({ mile: s.mile_marker, fuel, kind: 'depart', stop: s.stop ?? null });
    prev = s.mile_marker;
  }
  fuel -= (distance - prev) / mpg;
  if (anchor && Number.isFinite(endFuel)) fuel = endFuel;
  longest = Math.max(longest, distance - prev);
  points.push({ mile: distance, fuel, kind: 'end', stop: null });
  const levels = points.map((p) => p.fuel);
  return { points, min: Math.min(...levels), max: Math.max(...levels), longest, end: fuel };
}

// Fuel at a given mile (linear between the points of a trace).
export function fuelAt(trace, mile) {
  const pts = trace.points;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    if (a.mile === b.mile) continue;
    if (mile >= a.mile && mile <= b.mile) return a.fuel + ((b.fuel - a.fuel) * (mile - a.mile)) / (b.mile - a.mile);
  }
  return pts.length ? pts[pts.length - 1].fuel : null;
}

// The plan's tank: the API's own levels by default (what the page draws and states).
export function planTrace(body, { anchor = true } = {}) {
  return tankTrace({
    distance: body.route.distance_miles,
    mpg: body.vehicle.miles_per_gallon,
    startFuel: body.summary.start_fuel_gallons,
    endFuel: body.summary.end_fuel_gallons,
    stops: body.fuel_stops,
    anchor,
  });
}

export function priceBlindTrace(body) {
  const blind = body.summary?.comparison?.price_blind;
  if (!blind || !Array.isArray(blind.stops)) return null;
  return tankTrace({
    distance: body.route.distance_miles,
    mpg: body.vehicle.miles_per_gallon,
    startFuel: body.summary.start_fuel_gallons,
    stops: blind.stops,
    anchor: true,
  });
}

export function runChecks(body, about = {}) {
  const stops = body.fuel_stops || [];
  const summary = body.summary || {};
  const vehicle = body.vehicle || {};
  const distance = body.route?.distance_miles ?? 0;
  const mpg = vehicle.miles_per_gallon;
  const capacity = vehicle.tank_gallons;
  const checks = [];

  // 1. Stop costs add up to the total, to the cent.
  const costSum = stops.reduce((t, s) => t + cents(s.cost), 0);
  const total = cents(summary.total_fuel_cost);
  checks.push(result('sum_costs', 'Stop costs add up to the total', costSum === total, money(total), money(costSum),
    'Sum of the rounded stop costs, in cents.'));

  // 2. Each stop cost = gallons x price, rounded half up to the cent.
  const wrong = [];
  for (const s of stops) {
    const expected = divHalfUp(cents(s.gallons) * e4(s.price_per_gallon), 10000);
    if (expected !== cents(s.cost)) wrong.push(`stop ${s.stop}: ${money(expected)} expected, ${money(cents(s.cost))} shown`);
  }
  checks.push(result('stop_costs', 'Each stop cost = gallons × price, rounded to the cent',
    stops.length ? wrong.length === 0 : null,
    'round(gallons × price, cents)', stops.length ? (wrong.length ? `${wrong.length} wrong` : 'all match') : 'no stops',
    wrong.join('; ')));

  // 3. Stop gallons add up to the total gallons.
  const galSum = stops.reduce((t, s) => t + cents(s.gallons), 0);
  const galTotal = cents(summary.total_gallons_purchased);
  checks.push(result('sum_gallons', 'Stop gallons add up to the total gallons', galSum === galTotal,
    gallons(galTotal), gallons(galSum)));

  // 4. Fuel balance: start + bought - burned = end (rounding tolerance per stop).
  const burned = Math.round((distance * 100) / mpg);
  const balance = cents(summary.start_fuel_gallons) + galTotal - burned;
  const tolerance = 1 + 0.5 * stops.length;
  checks.push(result('balance', 'Fuel balance: start + bought − burned = end',
    Math.abs(balance - cents(summary.end_fuel_gallons)) <= tolerance,
    gallons(cents(summary.end_fuel_gallons)), gallons(balance),
    `Burned = ${distance} mi ÷ ${mpg} mpg. Tolerance: rounding of each stop to the hundredth of a gallon.`));

  // 5. start_tank=empty with nothing unpriced: gallons bought = miles / mpg.
  const empty = vehicle.start_tank !== 'full';
  const unpriced = Number(summary.unpriced_fuel_gallons) > 0;
  checks.push(result('every_mile_paid', 'Every mile is paid for (start tank empty)',
    empty && !unpriced ? Math.abs(galTotal - burned) <= tolerance : null,
    empty && !unpriced ? gallons(burned) : 'not applicable', gallons(galTotal),
    !empty ? 'Start tank full: the fuel in the tank at departure is not bought.'
      : unpriced ? 'Part of the fuel burned is not priced (see warnings).' : `${distance} mi ÷ ${mpg} mpg`));

  // 6. Tank between empty and full (the API's levels: arrival, arrival + purchase, end);
  // arrivals recomputed from the start fuel, the legs and the purchases match them.
  const trace = planTrace(body);
  const recomputed = planTrace(body, { anchor: false });
  const slack = 0.05; // the mile markers are rounded to a tenth of a mile
  const mismatches = [];
  let k = 0;
  for (const p of recomputed.points) {
    if (p.kind !== 'arrive') continue;
    k += 1;
    const allowed = Math.max(slack, 0.01 + 0.005 * k);
    if (Math.abs(p.fuel - p.api) > allowed) mismatches.push(`stop ${p.stop}: ${p.fuel.toFixed(2)} vs ${p.api}`);
  }
  const inRange = cents(trace.min) >= 0 && cents(trace.max) <= cents(capacity);
  checks.push(result('tank_bounds', 'Tank stays between empty and full; arrivals match',
    inRange && mismatches.length === 0,
    `between 0 and ${capacity} gal`, `${trace.min.toFixed(2)} to ${trace.max.toFixed(2)} gal`,
    mismatches.length ? `Arrival differs: ${mismatches.join('; ')}` : 'Levels of the answer; arrivals also recomputed mile by mile from the stops.'));

  // 7. Stops in route order and inside the corridor.
  const corridor = body.pipeline?.corridor?.corridor_miles ?? about?.planner?.corridor_miles;
  let ordered = true;
  for (let i = 1; i < stops.length; i++) if (stops[i].mile_marker < stops[i - 1].mile_marker) ordered = false;
  const farthest = stops.reduce((m, s) => Math.max(m, s.distance_from_route_miles), 0);
  const inside = corridor === undefined || corridor === null ? null : farthest <= corridor;
  checks.push(result('order_corridor', 'Stops in route order, within the corridor',
    stops.length ? ordered && inside !== false : null,
    corridor !== undefined && corridor !== null ? `ordered, ≤ ${corridor} mi off the route` : 'ordered',
    stops.length ? `${ordered ? 'ordered' : 'NOT ordered'}, farthest ${farthest} mi` : 'no stops'));

  // 8. External calls within the budget of the brief.
  const meta = body.meta || {};
  const services = meta.external_api_services || [];
  const osrm = services.filter((s) => s === 'osrm').length;
  const nominatim = services.filter((s) => s === 'nominatim').length;
  const retries = Number(about?.planner?.http_retries) || 0;
  const planHit = meta.plan_cache === 'hit';
  const okCalls = planHit ? meta.external_api_calls === 0 : osrm <= 1 + retries && meta.external_api_calls === osrm + nominatim;
  checks.push(result('call_budget', 'External calls within budget', okCalls,
    planHit ? 'none (plan cache hit)' : `one OSRM call${retries ? ` (+ up to ${retries} retry)` : ''}, plus one Nominatim call per free-text place`,
    `${meta.external_api_calls} (${services.join(', ') || 'none'})`,
    osrm > 1 ? 'A retried call after a transient failure counts as a call.' : ''));

  // 9. The pure optimum never costs more than the price-blind driver.
  const cmp = summary.comparison;
  const before = cmp?.optimum_before_consolidation;
  const blind = cmp?.price_blind;
  checks.push(result('optimum_vs_blind', 'Optimum never costs more than a price-blind driver',
    before && blind ? cents(before.total_fuel_cost) <= cents(blind.total_fuel_cost) : null,
    blind ? `≤ ${money(cents(blind.total_fuel_cost))}` : 'not applicable',
    before ? money(cents(before.total_fuel_cost)) : 'no comparison',
    !cmp ? 'No purchase on this trip, or the server does not send the comparison.' : !blind ? 'A price-blind driver cannot finish this route.' : 'Same stations, same start and end fuel, greedy optimum before consolidation.'));

  // 10. The safety reserve (a what-if setting): never arrive anywhere with less.
  const reserve = Number(vehicle.safety_reserve_gal) || 0;
  const arrivals = [...stops.map((s) => s.fuel_on_arrival_gallons), summary.end_fuel_gallons].filter(Number.isFinite);
  const lowest = arrivals.length ? Math.min(...arrivals) : null;
  checks.push(result('safety_reserve', 'Never below the safety reserve',
    reserve > 0 && lowest !== null ? cents(lowest) >= cents(reserve) : null,
    reserve > 0 ? `at least ${gallons(cents(reserve))} on arrival anywhere` : 'not applicable',
    lowest !== null ? `lowest arrival ${gallons(cents(lowest))}` : 'no arrival',
    reserve > 0 ? 'Every stop and the destination, from the answer.' : 'No safety reserve in this plan (a what-if setting).'));

  return checks;
}

export function summarize(checks) {
  const applicable = checks.filter((c) => c.ok !== null);
  return { passed: applicable.filter((c) => c.ok).length, total: applicable.length, failed: applicable.filter((c) => !c.ok).length };
}
