// Formatting, safe DOM building and data binding shared by every module.
//
// Rule of the page: no number is written by hand. Values come from the API
// answer (route), /api/about (about), the browser's own measurements (client) or
// pure functions of those (derived). The template marks each value with
// data-live="<root>.<path>"; bind() fills it, or shows a dash.

import { longestStretch } from './checks.js';

export const ROOTS = new Set(['route', 'about', 'client', 'derived']);
export const DASH = '—';
const MINUS = '−';

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
  ms: (v) => `${nf(v >= 100 ? 0 : 1, v >= 100 ? 0 : 1).format(v)} ms`,
  pct: (v) => `${nf(1, 1).format(v)}%`,
  int: (v) => nf(0, 0).format(v),
  dec1: (v) => nf(1, 1).format(v),
  num: (v) => nf(0, 2).format(v),
};

// The count and the word in agreement: one stop, two stops, no stops.
export function plural(count, word, many = `${word}s`) {
  return `${nf(0, 0).format(count)} ${Number(count) === 1 ? word : many}`;
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
  if (settings.consolidate === false) parts.push('tiny stops kept');
  else if ('consolidate' in settings && settings.consolidate !== true) parts.push(`consolidate ${settings.consolidate}`);
  return parts;
}

// The truck of an answer in words: miles per gallon, miles on a full tank, the safety
// fuel and a full tank at the start when they apply, and the settings of a link.
export function yourTruckText(body, settings = {}) {
  const v = body.vehicle || {};
  const parts = [`${fmt.num(v.miles_per_gallon)} miles per gallon`, `${fmt.miles_short(v.max_range_miles)} on a full tank`];
  if (Number(v.safety_reserve_gal) > 0) parts.push(`safety fuel ${fmt.gal(v.safety_reserve_gal)}`);
  if (v.start_tank === 'full') parts.push('full tank at the start');
  return [...parts, ...hiddenSettingsText(settings)].join(', ');
}

// state: {route: the last good answer's result, about, params}
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
  };
  if (!body) return d;

  const summary = body.summary;
  const vehicle = body.vehicle;
  const meta = body.meta || {};
  const optimizer = body.pipeline?.optimizer || {};
  const corridor = body.pipeline?.corridor || {};
  const cmp = summary.comparison || null;
  const stops = body.fuel_stops || [];
  const distance = body.route.distance_miles;

  // Tank mode and what was paid for.
  d.is_full = vehicle.start_tank === 'full';
  d.has_unpriced = summary.unpriced_fuel_gallons > 0;
  d.pays_every_mile = !d.is_full && !d.has_unpriced
    && Math.abs(summary.end_fuel_gallons - summary.start_fuel_gallons) < 0.005;
  d.safety_on = Number(vehicle.safety_reserve_gal) > 0;
  d.is_your_truck = (meta.settings_changed || []).length > 0 || d.is_full;
  d.your_truck_text = yourTruckText(body, state.params?.settings);

  // The road.
  d.needs_stops = distance > vehicle.usable_range_miles;
  d.fits_one_tank = !d.needs_stops;
  d.has_candidates = (corridor.candidates ?? summary.candidate_stations_on_route) > 0;
  d.no_candidates = !d.has_candidates;
  d.route_price_min = d.has_candidates ? corridor.price_per_gallon?.min ?? null : null;
  d.route_price_max = d.has_candidates ? corridor.price_per_gallon?.max ?? null : null;
  d.has_warnings = (body.warnings || []).length > 0;

  // External calls of THIS answer.
  const services = meta.external_api_services || [];
  const osrm = services.filter((s) => s === 'osrm').length;
  const other = (meta.external_api_calls ?? 0) - osrm;
  d.calls_text = present(meta.external_api_calls) ? plural(meta.external_api_calls, 'request') : null;
  d.calls_plan_hit = meta.plan_cache === 'hit';
  d.calls_route_hit = !d.calls_plan_hit && meta.route_cache === 'hit';
  d.calls_new_trip = !d.calls_plan_hit && !d.calls_route_hit;
  d.calls_osrm_text = plural(osrm, 'request');
  d.calls_other = other > 0;
  d.calls_other_text = other > 0 ? plural(other, 'request') : null;

  // Stops.
  d.has_stops = stops.length > 0;
  d.no_stops = !d.has_stops;
  d.stops_text = plural(summary.number_of_stops, 'stop');
  d.longest_stretch = longestStretch(body);
  d.stops_arriving_empty = stops.filter((s) => s.fuel_on_arrival_gallons === 0).length;
  d.some_arrive_empty = d.stops_arriving_empty > 0 && !d.safety_on;

  // The rule for tiny stops, and what it did on this trip.
  d.min_stop = optimizer.min_stop_gallons ?? about.vehicle?.min_stop_gallons ?? null;
  d.max_fix = optimizer.max_consolidation_cost ?? about.vehicle?.max_consolidation_cost ?? null;
  d.merged_before = optimizer.stops_before_consolidation ?? null;
  d.merged_after = optimizer.stops ?? summary.number_of_stops;
  d.merged_cost = optimizer.consolidation_extra_cost ?? null;
  const movedSomething = Math.abs(d.merged_cost ?? 0) >= 0.005;
  d.merged_changed = present(d.merged_before) && d.merged_before !== d.merged_after;
  d.merged_moved = !d.merged_changed && movedSomething;
  d.merged_unchanged = present(d.merged_before) && !d.merged_changed && !movedSomething;

  // The driver who ignores prices.
  const blind = cmp?.price_blind || null;
  const savings = cmp?.savings_vs_price_blind || null;
  d.no_purchase = !cmp && stops.length === 0;
  d.price_blind_infeasible = Boolean(cmp) && !blind;
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
  return d;
}
