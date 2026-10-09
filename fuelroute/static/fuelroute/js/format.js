// Formatting, safe DOM building and data binding shared by every module.
//
// Rule of the page: no number is written by hand. Values come from the API
// answer (route), the standard truck's answer for the same trip (standard), /api/about
// (about), the browser's own measurements (client) or pure functions of those
// (derived). The template marks each value with data-live="<root>.<path>"; bind()
// fills it, or shows a dash.

import { longestStretch } from './checks.js';

export const ROOTS = new Set(['route', 'standard', 'about', 'client', 'derived']);
export const DASH = '—';
const MINUS = '−';
const TENTH_OF_A_SECOND_MS = 100;

const cache = new Map();
function nf(min, max) {
  const key = `${min}:${max}`;
  if (!cache.has(key)) {
    cache.set(key, new Intl.NumberFormat('en-US', { minimumFractionDigits: min, maximumFractionDigits: max }));
  }
  return cache.get(key);
}

export const fmt = {
  text: (v) => String(v),
  money: (v) => `${v < 0 ? MINUS : ''}$${nf(2, 2).format(Math.abs(v))}`,
  money_signed: (v) => `${v > 0 ? '+' : v < 0 ? MINUS : ''}$${nf(2, 2).format(Math.abs(v))}`,
  // Prices: always three decimals on screen.
  price: (v) => `$${nf(3, 3).format(v)}`,
  gal: (v) => `${nf(2, 2).format(v)} gal`,
  // Distances along the road: always one decimal, so they line up.
  miles: (v) => `${nf(1, 1).format(v)} mi`,
  // Settings (the range, the corridor): no decimal unless the value has one.
  miles_short: (v) => `${nf(0, 1).format(v)} mi`,
  // A server time in milliseconds, said in seconds (people do not think in ms).
  seconds: (v) => (v < TENTH_OF_A_SECOND_MS ? 'under a tenth of a second' : `${nf(1, 1).format(v / 1000)} seconds`),
  pct: (v) => `${nf(1, 1).format(v)}%`,
  int: (v) => nf(0, 0).format(v),
  dec1: (v) => nf(1, 1).format(v),
  num: (v) => nf(0, 2).format(v),
};

// The count and the word in agreement: one stop, two stops, no stops.
export function plural(count, word, many = `${word}s`) {
  return `${nf(0, 0).format(count)} ${Number(count) === 1 ? word : many}`;
}

// Where a stop sits on the map, by the API field fuel_stops[].position (planner.py
// STOP_POSITIONS): exact (its own truck stop, or the highway exit in its address, both
// matched in OpenStreetMap) or its city center. null for an answer without the field.
export const POSITIONS = {
  fuel_station: { exact: true, text: 'Exact position: its own truck stop on OpenStreetMap' },
  highway_exit: { exact: true, text: 'Exact position: the highway exit in its address (OpenStreetMap)' },
  city_center: { exact: false, text: 'Approximate position: its city center (no exact match)' },
};

export function positionOf(stop) {
  return POSITIONS[stop?.position] || null;
}

// "a", "a and b", "a, b and c".
export function joinAnd(items) {
  if (items.length < 2) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

export function present(value) {
  return value !== null && value !== undefined && value !== '' && !(typeof value === 'number' && !Number.isFinite(value));
}

export function format(kind, value) {
  if (!present(value)) return DASH;
  const fn = fmt[kind] || fmt.text;
  if (fn !== fmt.text && typeof value !== 'number') {
    const number = Number(value);
    return Number.isFinite(number) ? fn(number) : String(value);
  }
  return fn(value);
}

export function truthy(value) {
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'number') return Number.isFinite(value) && value !== 0;
  return Boolean(value);
}

// --- DOM ------------------------------------------------------------------------

function appendChildren(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
}

// el('a', {href, class, text, dataset, css, onclick, 'aria-label'}, ...children)
// Text is always inserted as text (never parsed as HTML).
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'text') node.textContent = String(value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'css') for (const [prop, v] of Object.entries(value)) node.style.setProperty(prop, v);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  appendChildren(node, children);
  return node;
}

export function replace(node, ...children) {
  while (node.firstChild) node.removeChild(node.firstChild);
  appendChildren(node, children);
  return node;
}

// A check / cross / dot drawn with CSS (never an emoji), always next to words.
export function mark(ok, words) {
  const kind = ok === true ? 'ok' : ok === false ? 'bad' : 'na';
  return el('span', { class: `mark mark-${kind}` }, el('span', { class: 'mark-icon', 'aria-hidden': 'true' }), el('span', {}, words));
}

// --- data binding -----------------------------------------------------------------

export function lookup(ctx, path) {
  const parts = String(path).split('.');
  if (!ROOTS.has(parts[0])) throw new Error(`data-live root not allowed: ${path}`);
  let value = ctx;
  for (const part of parts) {
    if (value === null || value === undefined) return undefined;
    value = value[part];
  }
  return value;
}

export function bind(root, ctx) {
  for (const node of root.querySelectorAll('[data-live]')) {
    const value = lookup(ctx, node.dataset.live);
    node.textContent = present(value) ? format(node.dataset.format || 'text', value) : DASH;
  }
  for (const node of root.querySelectorAll('[data-live-if]')) node.hidden = !truthy(lookup(ctx, node.dataset.liveIf));
  for (const node of root.querySelectorAll('[data-href]')) {
    const value = lookup(ctx, node.dataset.href);
    if (value) node.setAttribute('href', value);
    else node.removeAttribute('href');
  }
}

// A DRF error dict / list / string as lines ("mpg: Ensure this value is ...").
export function flattenErrors(detail) {
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    return Object.entries(detail).flatMap(([field, value]) =>
      flattenErrors(value).map((line) => (field === 'non_field_errors' ? line : `${field}: ${line}`)),
    );
  }
  if (Array.isArray(detail)) return detail.flatMap(flattenErrors);
  return detail === null || detail === undefined ? [] : [String(detail)];
}

// --- derived values -------------------------------------------------------------------
//
// Pure functions of the page state, exposed to the template as derived.*. They never
// introduce a number of their own: everything is arithmetic on API fields, /api/about
// or browser measurements.

// The truck settings without a control in the Truck step, in words (a link may bring them).
export function hiddenSettingsText(settings = {}) {
  const parts = [];
  if ('corridor_miles' in settings) parts.push(`stations up to ${format('miles_short', settings.corridor_miles)} from the road`);
  if (settings.price_policy === 'max') parts.push('the highest price of each station');
  else if (settings.price_policy === 'min') parts.push('the lowest price of each station');
  else if ('price_policy' in settings) parts.push(`price_policy ${settings.price_policy}`);
  return parts;
}

// The truck of an answer in words: miles per gallon, the tank and its range, the safety
// fuel, a full tank at the start and skipped tiny stops when they apply, and the
// settings of a link.
export function yourTruckText(body, settings = {}) {
  const v = body.vehicle || {};
  const parts = [
    `${fmt.num(v.miles_per_gallon)} miles per gallon`,
    `a ${fmt.num(v.tank_gallons)}-gallon tank (${fmt.miles_short(v.max_range_miles)})`,
  ];
  if (Number(v.safety_reserve_gal) > 0) parts.push(`safety fuel ${fmt.gal(v.safety_reserve_gal)}`);
  if (v.start_tank === 'full') parts.push('full tank at the start');
  if (v.consolidate === true) parts.push('tiny stops skipped');
  return [...parts, ...hiddenSettingsText(settings)].join(', ');
}

function stateStations(about, code) {
  const row = (about.data?.stations_by_state || []).find((r) => r.state === code);
  return row ? row.geocoded : null;
}

// state: {route: the last good answer's result, about, params, standard: the standard
// truck's answer body for the same trip (or null), tripCalls: requests to the routing
// service for this trip so far (or null)}
export function derive(state) {
  const about = state.about || {};
  const deliverables = about.deliverables || {};
  const result = state.route;
  const body = result && result.ok ? result.body : null;
  const d = {
    has_plan: Boolean(body),
    repo_url: deliverables.repo_url || null,
    postman_url: deliverables.postman_url || null,
    loom_url: deliverables.loom_url || null,
    ca_stations: stateStations(about, 'CA'),
  };
  if (!body) return d;

  const summary = body.summary;
  const vehicle = body.vehicle;
  const meta = body.meta || {};
  const cmp = summary.comparison || null;
  const stops = body.fuel_stops || [];
  const distance = body.route.distance_miles;

  // The tank: what the truck leaves and arrives with.
  d.is_full = vehicle.start_tank === 'full';
  d.has_unpriced = summary.unpriced_fuel_gallons > 0;
  d.tank_same_back = !d.is_full && !d.has_unpriced
    && Math.abs(summary.end_fuel_gallons - summary.start_fuel_gallons) < 0.005;
  d.tank_arrives_lower = !d.is_full && !d.tank_same_back;
  d.start_fuel_miles = summary.start_fuel_gallons * vehicle.miles_per_gallon;
  d.safety_on = Number(vehicle.safety_reserve_gal) > 0;
  d.is_your_truck = (meta.settings_changed || []).length > 0 || d.is_full;
  d.your_truck_text = yourTruckText(body, state.params?.settings);

  // The road, and why it needs stops.
  d.has_stops = stops.length > 0;
  d.no_stops = !d.has_stops;
  d.trip_over_range = distance > vehicle.usable_range_miles + 0.05;
  d.fits_but_starts_low = !d.trip_over_range && d.has_stops;
  d.has_candidates = summary.candidate_stations_on_route > 0;
  d.no_candidates = !d.has_candidates;
  d.route_price_min = summary.price_per_gallon_on_route?.min ?? null;
  d.route_price_max = summary.price_per_gallon_on_route?.max ?? null;
  d.has_warnings = (body.warnings || []).length > 0;

  // External calls of THIS answer.
  const services = meta.external_api_services || [];
  const osrm = services.filter((s) => s === 'osrm').length;
  const other = (meta.external_api_calls ?? 0) - osrm;
  d.calls_plan_hit = meta.plan_cache === 'hit';
  d.calls_route_hit = !d.calls_plan_hit && meta.route_cache === 'hit';
  d.calls_new_trip = !d.calls_plan_hit && !d.calls_route_hit;
  d.calls_osrm_text = plural(osrm, 'request');
  d.calls_other = other > 0;
  d.calls_other_text = other > 0 ? plural(other, 'request') : null;
  d.trip_osrm_text = present(state.tripCalls) ? plural(state.tripCalls, 'request') : null;
  d.trip_osrm_none = state.tripCalls === 0;

  // Stops.
  d.stops_text = plural(summary.number_of_stops, 'stop');
  d.longest_stretch = longestStretch(body);
  d.stops_arriving_empty = stops.filter((s) => s.fuel_on_arrival_gallons === 0).length;
  d.some_arrive_empty = d.stops_arriving_empty > 0 && !d.safety_on;
  // Where the stops sit on the map: "12 of 19 stops" at their exact position.
  const exactStops = stops.filter((s) => positionOf(s)?.exact).length;
  d.has_positions = d.has_stops && stops.every((s) => positionOf(s) !== null);
  d.exact_stops_text = d.has_positions ? `${nf(0, 0).format(exactStops)} of ${plural(stops.length, 'stop')}` : null;
  d.some_approx = d.has_positions && exactStops < stops.length;

  // Tiny stops: merged ("Skip tiny stops" on), or named when the rules alone make some.
  const merged = summary.tiny_stops_merged || null;
  d.min_stop = about.vehicle?.min_stop_gallons ?? null;
  d.max_fix = about.vehicle?.max_consolidation_cost ?? null;
  d.merge_on = vehicle.consolidate === true && d.has_stops && Boolean(merged);
  d.merged_before = merged?.stops_before ?? null;
  d.merged_after = summary.number_of_stops;
  d.merged_cost = merged?.extra_cost ?? null;
  const movedSomething = Math.abs(d.merged_cost ?? 0) >= 0.005;
  d.merged_changed = d.merge_on && d.merged_before !== d.merged_after;
  d.merged_moved = d.merge_on && !d.merged_changed && movedSomething;
  d.merged_unchanged = d.merge_on && !d.merged_changed && !movedSomething;
  d.smallest_purchase = d.has_stops ? Math.min(...stops.map((s) => s.gallons)) : null;
  d.has_tiny_stops = !d.merge_on && d.has_stops && present(d.min_stop) && d.smallest_purchase < d.min_stop;

  // The driver who ignores prices.
  const blind = cmp?.price_blind || null;
  const savings = cmp?.savings_vs_price_blind || null;
  d.no_purchase = !cmp && stops.length === 0;
  d.price_blind_infeasible = Boolean(cmp) && !blind;
  d.has_price_blind = Boolean(blind) && d.has_stops;
  // Each stop's gallons are rounded to the hundredth: the totals may differ by that much.
  const rounding = 0.005 * (stops.length + (blind?.number_of_stops ?? 0)) + 1e-9;
  d.same_gallons = d.has_price_blind
    && Math.abs(blind.total_gallons_purchased - summary.total_gallons_purchased) <= rounding;
  d.price_blind_cost = blind?.total_fuel_cost ?? null;
  d.price_blind_stops = blind?.number_of_stops ?? null;
  d.savings_amount = savings?.amount ?? null;
  d.savings_pct = savings?.percent ?? null;
  d.savings_not_negative = Boolean(savings) && savings.amount >= 0;
  d.savings_negative = Boolean(savings) && savings.amount < 0;
  d.costs_more = d.savings_negative ? -savings.amount : null;
  const extra = savings?.extra_stops ?? 0;
  d.extra_stops_positive = extra > 0;
  d.extra_stops_text = extra > 0 ? plural(extra, 'more stop') : null;
  d.fewer_stops = extra < 0;
  d.fewer_stops_text = extra < 0 ? plural(-extra, 'fewer stop') : null;

  // The same values for the standard truck (the "For Spotter" step).
  d.standard = state.standard
    ? derive({ route: { ok: true, body: state.standard }, about, params: { settings: {} }, tripCalls: state.tripCalls })
    : null;
  return d;
}
