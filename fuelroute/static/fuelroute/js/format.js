// Formatting, safe DOM building and data binding shared by every module.
//
// Rule of the page: no number is written by hand. Values come from the API
// response (route), /api/stats (stats), /api/about (about), the browser's own
// measurements (client) or pure functions of those (derived). The template marks
// each value with data-live="<root>.<path>"; bind() fills it, or shows a dash.

const SVG_NS = 'http://www.w3.org/2000/svg';
export const ROOTS = new Set(['route', 'stats', 'about', 'client', 'derived']);
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

const KILO = 1024;

export const fmt = {
  text: (v) => String(v),
  money: (v) => `${v < 0 ? MINUS : ''}$${nf(2, 2).format(Math.abs(v))}`,
  money_signed: (v) => `${v > 0 ? '+' : v < 0 ? MINUS : ''}$${nf(2, 2).format(Math.abs(v))}`,
  // Prices: always three decimals on screen (the exact value goes in a title).
  price: (v) => `$${nf(3, 3).format(v)}`,
  price_exact: (v) => `$${nf(3, 4).format(v)}`,
  gal: (v) => `${nf(2, 2).format(v)} gal`,
  // Distances: always one decimal, so short and long ones line up in a column.
  miles: (v) => `${nf(1, 1).format(v)} mi`,
  // Configured distances (the range, the corridor) are whole numbers: no decimal.
  miles_round: (v) => `${nf(0, 0).format(v)} mi`,
  ms: (v) => `${nf(v >= 100 ? 0 : 1, v >= 100 ? 0 : 1).format(v)} ms`,
  bytes: (v) => {
    if (v < KILO) return `${nf(0, 0).format(v)} B`;
    if (v < KILO * KILO) return `${nf(1, 1).format(v / KILO)} KB`;
    return `${nf(2, 2).format(v / KILO / KILO)} MB`;
  },
  pct: (v) => `${nf(1, 1).format(v)}%`,
  int: (v) => nf(0, 0).format(v),
  dec1: (v) => nf(1, 1).format(v),
  num: (v) => nf(0, 2).format(v),
  ratio: (v) => `${nf(0, v >= 10 ? 0 : 1).format(v)}×`,
  time: (v) => new Date(v).toLocaleTimeString('en-US', { hour12: false }),
  datetime: (v) =>
    new Date(v).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short', hour12: false }),
  date: (v) => new Date(v).toLocaleDateString('en-US', { dateStyle: 'medium' }),
  duration: (seconds) => {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h) return m ? `${h} h ${m} min` : `${h} h`;
    if (m) return `${m} min ${s % 60} s`;
    return `${s} s`;
  },
  ago: (v) => {
    const seconds = (Date.now() - new Date(v).getTime()) / 1000;
    if (!Number.isFinite(seconds)) return DASH;
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
    return `${Math.round(seconds / 86400)} days ago`;
  },
  yesno: (v) => (v ? 'yes' : 'no'),
};

// The count and the word in agreement: one call, two calls, no calls.
export function plural(count, word, many = `${word}s`) {
  return `${nf(0, 0).format(count)} ${Number(count) === 1 ? word : many}`;
}

// Percentiles from few samples say little (p95 of five values is the maximum).
export const MIN_SAMPLES_FOR_P95 = 20;

export function present(value) {
  return value !== null && value !== undefined && value !== '' && !(typeof value === 'number' && !Number.isFinite(value));
}

export function format(kind, value) {
  if (!present(value)) return DASH;
  const fn = fmt[kind] || fmt.text;
  if (fn !== fmt.text && fn !== fmt.yesno && typeof value !== 'number' && !['time', 'datetime', 'date', 'ago'].includes(kind)) {
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
  const node = tag.startsWith('svg:')
    ? document.createElementNS(SVG_NS, tag.slice(4))
    : document.createElement(tag);
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

export function svg(tag, attrs, ...children) {
  return el(`svg:${tag}`, attrs, ...children);
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function replace(node, ...children) {
  clear(node);
  appendChildren(node, children);
  return node;
}

// A check / cross / dot drawn with CSS (never an emoji), always next to a word.
export function mark(ok, words) {
  const kind = ok === true ? 'ok' : ok === false ? 'bad' : 'na';
  const fallback = ok === true ? 'pass' : ok === false ? 'fail' : 'n/a';
  return el('span', { class: `mark mark-${kind}` }, el('span', { class: 'mark-icon', 'aria-hidden': 'true' }), words ?? fallback);
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
    node.classList.toggle('is-missing', !present(value));
  }
  for (const node of root.querySelectorAll('[data-live-if]')) node.hidden = !truthy(lookup(ctx, node.dataset.liveIf));
  for (const node of root.querySelectorAll('[data-live-unless]')) node.hidden = truthy(lookup(ctx, node.dataset.liveUnless));
  for (const node of root.querySelectorAll('[data-href]')) {
    const value = lookup(ctx, node.dataset.href);
    if (value) node.setAttribute('href', value);
    else node.removeAttribute('href');
  }
}

// --- errors, text -------------------------------------------------------------------

// Port of the server's old _messages(): a DRF error dict / list / string as lines.
export function flattenErrors(detail) {
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    return Object.entries(detail).flatMap(([field, value]) =>
      flattenErrors(value).map((line) => (field === 'non_field_errors' ? line : `${field}: ${line}`)),
    );
  }
  if (Array.isArray(detail)) return detail.flatMap(flattenErrors);
  return detail === null || detail === undefined ? [] : [String(detail)];
}

export function slug(text) {
  return String(text || '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

export function sum(values) {
  return values.reduce((total, v) => total + (Number(v) || 0), 0);
}

// Nearest-rank percentile of a list of numbers.
export function percentile(values, p) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = Math.max(1, Math.ceil((p / 100) * sorted.length));
  return sorted[rank - 1];
}

export function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

// --- about helpers --------------------------------------------------------------------

export function codeLink(about, key) {
  const link = about?.code_links?.[key];
  return link?.url ? link : null;
}

// Test index lookup: exact "<path>::<func>" key, else by function name and file.
export function testEntry(about, key) {
  const index = about?.test_index || {};
  if (index[key]) return { key, ...index[key] };
  const [file, func] = String(key).split('::');
  const base = (file || '').split('/').pop();
  for (const [k, v] of Object.entries(index)) {
    const [kFile, kFunc] = k.split('::');
    if (kFunc === func && (!base || kFile.endsWith(base) || kFile.endsWith(base.replace(/\.py$/, '')))) {
      return { key: k, ...v };
    }
  }
  return null;
}

// "12/12 passed", "failing", "not in last run", "missing" for one test function.
export function testBadge(about, key, { short = false } = {}) {
  const entry = testEntry(about, key);
  const func = String(key).split('::').pop();
  const label = short ? func : key;
  if (!entry) return el('span', { class: 'badge badge-bad', title: `${label}: not found in the test index` }, 'missing');
  const report = about?.tests?.report;
  const notOnGitHub = entry.url ? null : el('span', { class: 'tag tag-local', title: 'This test is not on GitHub yet: push to link it.' }, 'not on GitHub yet');
  let badge;
  if (entry.failed) badge = el('span', { class: 'badge badge-bad' }, el('span', { class: 'mark-icon mark-bad-icon', 'aria-hidden': 'true' }), 'failing');
  else if (entry.cases) badge = el('span', { class: 'badge badge-ok' }, `${entry.passed ?? 0}/${entry.cases} passed`);
  else badge = el('span', { class: 'badge badge-na' }, report === 'found' ? 'not in last run' : 'no test report');
  // Short form reads as a sentence: test_cache_hit_is_free -> "cache hit is free".
  const text = short ? func.replace(/^test_/, '').replace(/_/g, ' ') : entry.key;
  const link = entry.url
    ? el('a', { href: entry.url, target: '_blank', rel: 'noopener', class: 'test-link', title: entry.key }, text)
    : el('code', { title: `${entry.key} (line ${entry.line})` }, text);
  return el('span', { class: 'test-ref' }, link, ' ', badge, notOnGitHub ? [' ', notOnGitHub] : null);
}

export function serverTimingDur(headers, name) {
  const entry = (headers?.serverTiming || []).find((e) => e.name === name);
  return entry && Number.isFinite(entry.dur) ? entry.dur : null;
}

// --- derived values -------------------------------------------------------------------
//
// Pure functions of the page state, exposed to the template as derived.*. They
// never introduce a number of their own: everything is arithmetic on API fields,
// /api/stats, /api/about or browser measurements.

const TANK_REASONS = {
  full_tank: 'leaves with a full tank it did not pay for, and buys only what it needs to arrive',
  reserve: 'leaves with the reserve and must arrive with the same amount (borrowed and returned: not a safety margin)',
  first_station_beyond_reserve: 'leaves with enough fuel to reach the first station, and must arrive with the same amount',
  no_station_on_route: 'no station on this route: only a trip the reserve covers works',
};
const CAPPED = '; capped: the last stretch is too long to arrive with that much, and the difference is unpriced';

function sameName(a, b) {
  return String(a || '').trim().toLowerCase() === String(b || '').trim().toLowerCase();
}

export function derive(state, helpers) {
  const { planTrace, priceBlindTrace, summarize } = helpers;
  const about = state.about || {};
  const result = state.route;
  const body = result && result.ok ? result.body : null;
  const d = { has_plan: Boolean(body), no_plan: !body };

  // Browser-side numbers of the last /api/route answer (also for errors).
  const server = result?.headers?.responseTimeMs ?? null;
  const external = result?.body?.meta?.external_api_ms ?? null;
  if (present(server)) {
    d.our_code_ms = present(external) ? Math.max(0, server - external) : server;
    if (present(external) && server > 0) d.external_share_pct = (Math.min(external, server) / server) * 100;
  }
  d.render_ms = serverTimingDur(result?.headers, 'render');

  // Commit, repo, tests. Links go to the newest commit GitHub has (build.linked_commit).
  const build = about.build || {};
  if (build.repo_url) {
    const ref = build.linked_commit || (build.commit ? null : 'main');
    d.commit_url = build.commit && build.pushed ? `${build.repo_url}/commit/${build.commit}` : build.repo_url;
    d.postman_url = ref ? `${build.repo_url}/blob/${ref}/${about.deliverables?.postman_collection || 'postman/collection.json'}` : null;
    d.readme_url = ref ? `${build.repo_url}/blob/${ref}/README.md` : null;
  }
  d.not_pushed = build.pushed === false;
  d.dirty = build.dirty === true;
  d.commits_not_on_github = build.commits_not_on_github || null;
  d.links_on_older_commit = Boolean(build.linked_commit && build.linked_commit !== build.commit);
  d.linked_commit_short = build.linked_commit_short || null;
  d.loom_url = about.deliverables?.loom_url || null;
  d.no_loom = !d.loom_url;
  d.tests_missing = about.tests?.report === 'missing';
  d.tests_stale = about.tests?.stale === true;
  if (about.data?.stations) d.data_geocoded_pct = (about.data.geocoded / about.data.stations) * 100;
  const missingStates = about.data?.states_without_stations;
  const thinnest = about.data?.thinnest_states;
  d.states_without_count = Array.isArray(missingStates) ? missingStates.length : null;
  d.states_without_text = Array.isArray(missingStates) ? missingStates.join(', ') || 'none' : null;
  d.all_states_have_stations = Array.isArray(missingStates) && missingStates.length === 0;
  d.some_states_without = Array.isArray(missingStates) && missingStates.length > 0;
  d.thinnest_text = Array.isArray(thinnest) && thinnest.length
    ? thinnest.map((t) => `${t.state} (${nf(0, 0).format(t.geocoded)})`).join(', ')
    : null;
  const osrmLatency = state.stats?.external_latency_ms?.osrm;
  d.osrm_p95 = osrmLatency && osrmLatency.count >= MIN_SAMPLES_FOR_P95 ? osrmLatency.p95 : null;
  d.osrm_max = osrmLatency ? osrmLatency.max : null;
  d.osrm_samples = osrmLatency ? osrmLatency.count : null;
  d.osrm_few_samples = Boolean(osrmLatency) && osrmLatency.count < MIN_SAMPLES_FOR_P95;

  // Session (this page's own network log).
  const routeLog = (state.session || []).filter((r) => r.kind === 'route');
  d.session_requests = (state.session || []).length;
  const rejected = state.stats?.route_requests?.errors_without_external_calls;
  d.rejected_without_calls_text = present(rejected) ? plural(rejected, 'rejected request') : null;
  d.session_osrm_calls = routeLog.reduce((t, r) => t + (r.osrmCalls || 0), 0);
  d.session_new_trips = routeLog.filter((r) => r.osrmCalls > 0 || r.routeCache === 'miss').length;
  d.session_calls_per_trip = d.session_new_trips ? d.session_osrm_calls / d.session_new_trips : null;

  // Checks.
  if (state.checks && state.checks.length) {
    const s = summarize(state.checks);
    d.checks_passed = s.passed;
    d.checks_total = s.total;
    d.checks_failed = s.failed;
    d.checks_all_pass = s.failed === 0;
  }
  if (!body) return d;

  const summary = body.summary;
  const vehicle = body.vehicle;
  const meta = body.meta || {};
  const pipeline = body.pipeline || null;
  const cmp = summary.comparison || null;
  const distance = body.route.distance_miles;

  d.is_empty = vehicle.start_tank !== 'full';
  d.is_full = vehicle.start_tank === 'full';
  d.has_unpriced = d.is_empty && summary.unpriced_fuel_gallons > 0;
  // "Every mile paid" only when it is true: nothing burned without a station to buy from.
  d.pays_every_mile = d.is_empty && !d.has_unpriced;
  d.full_tank_gallons = d.is_full ? summary.unpriced_fuel_gallons : null;
  d.fuel_needed_gal = distance / vehicle.miles_per_gallon;
  d.cost_per_mile = distance ? summary.total_fuel_cost / distance : null;
  d.has_stops = summary.number_of_stops > 0;
  d.no_stops = summary.number_of_stops === 0;
  d.has_candidates = summary.candidate_stations_on_route > 0;
  d.no_candidates = summary.candidate_stations_on_route === 0;
  const services = [...new Set(meta.external_api_services || [])];
  d.services_text = services.length ? services.join(', ') : 'none';
  d.calls_text = present(meta.external_api_calls)
    ? `${plural(meta.external_api_calls, 'external call')}${services.length ? ` (${services.join(', ')})` : ''}`
    : null;
  d.start_label_differs = !sameName(body.start.label, body.start.query);
  d.finish_label_differs = !sameName(body.finish.label, body.finish.query);
  d.is_plan_hit = meta.plan_cache === 'hit';
  d.is_plan_miss = meta.plan_cache !== 'hit';
  d.has_warnings = (body.warnings || []).length > 0;

  // Comparison.
  d.has_comparison = Boolean(cmp);
  d.no_purchase = !cmp && summary.number_of_stops === 0;
  // Why nothing was bought: a full tank covers the trip, or there was nothing to buy from.
  d.no_purchase_full = d.no_purchase && d.is_full && !d.has_unpriced;
  d.no_purchase_no_station = d.no_purchase && d.no_candidates && !d.no_purchase_full;
  const blind = cmp?.price_blind || null;
  const savings = cmp?.savings_vs_price_blind || null;
  d.has_savings = Boolean(savings);
  d.savings_negative = Boolean(savings) && savings.amount < 0;
  d.savings_not_negative = Boolean(savings) && savings.amount >= 0;
  d.costs_more_amount = d.savings_negative ? -savings.amount : null;
  d.price_blind_infeasible = Boolean(cmp) && !blind;
  d.savings_amount = savings?.amount ?? null;
  d.savings_pct = savings?.percent ?? null;
  d.extra_stops = savings?.extra_stops ?? null;
  d.extra_stops_positive = (savings?.extra_stops ?? 0) > 0;
  d.fewer_stops = (savings?.extra_stops ?? 0) < 0 ? -savings.extra_stops : null;
  d.price_blind_cost = blind?.total_fuel_cost ?? null;
  d.price_blind_stops = blind?.number_of_stops ?? null;
  d.corridor_avg_price = cmp?.corridor_average?.price_per_gallon ?? null;
  d.savings_vs_avg = cmp?.savings_vs_corridor_average?.amount ?? null;

  // Prices on the route (pipeline, or computed from the candidates layer).
  const corridor = pipeline?.corridor || null;
  let prices = corridor?.price_per_gallon || null;
  if (!prices && body.candidates?.rows?.length) {
    const i = body.candidates.fields.indexOf('price_per_gallon');
    const list = body.candidates.rows.map((r) => r[i]);
    prices = { min: Math.min(...list), median: median(list), max: Math.max(...list) };
  }
  if (!d.has_candidates) prices = null;
  d.route_price_min = prices?.min ?? null;
  d.route_price_median = prices?.median ?? null;
  d.route_price_max = prices?.max ?? null;
  d.corridor_candidates = corridor?.candidates ?? summary.candidate_stations_on_route;
  d.corridor_miles = corridor?.corridor_miles ?? about.planner?.corridor_miles ?? null;

  // Tank trace.
  const trace = planTrace(body);
  d.tank_min = trace.min;
  d.tank_max = trace.max;
  d.longest_stretch = trace.longest;
  d.tank_ok = trace.min >= -0.05 && trace.max <= vehicle.tank_gallons + 0.05;
  d.stretch_ok = trace.longest <= vehicle.max_range_miles + 0.05;
  const blindTrace = priceBlindTrace(body);
  d.price_blind_longest = blindTrace ? blindTrace.longest : null;

  const offsets = body.fuel_stops.map((s) => s.distance_from_route_miles);
  d.max_offroute = offsets.length ? Math.max(...offsets) : null;
  d.detour_approx = offsets.length ? sum(offsets) * 2 : null;
  // The range is used literally: how many stops the plan reaches with the tank empty.
  const arrivals = body.fuel_stops.map((s) => s.fuel_on_arrival_gallons);
  d.stops_arriving_empty = arrivals.filter((g) => g === 0).length;
  d.lowest_arrival = arrivals.length ? Math.min(...arrivals) : null;
  d.some_arrive_empty = d.stops_arriving_empty > 0;

  // Map / geometry.
  const features = body.map?.geojson?.features || [];
  d.geojson_features = features.length;
  const line = features.find((f) => f.geometry?.type === 'LineString');
  d.route_points = line ? line.geometry.coordinates.length : null;
  d.stop_markers = features.filter((f) => f.properties?.kind === 'fuel_stop').length;
  d.markers_match = d.stop_markers === summary.number_of_stops;

  // Pipeline (how the cached plan was computed).
  if (pipeline) {
    const timings = pipeline.timings_ms || {};
    d.pipeline_total_ms = sum(Object.values(timings));
    d.pipeline_route_prep_ms = present(timings.routing_ms) ? Math.max(0, timings.routing_ms - (pipeline.external_api_ms || 0)) : null;
    d.pipeline_our_code_ms = Math.max(0, d.pipeline_total_ms - (pipeline.external_api_ms || 0));
    d.routing_from_cache_text = pipeline.routing
      ? pipeline.routing.from_route_cache ? 'reused from the route cache (no call)' : 'fetched from OSRM (one call)'
      : null;
    d.arrival_capped = pipeline.tank?.arrival_capped === true;
    const reason = TANK_REASONS[pipeline.tank?.reason] || pipeline.tank?.reason || null;
    d.tank_reason_text = reason && d.arrival_capped ? `${reason}${CAPPED}` : reason;
    d.pipeline_calls_text = present(pipeline.external_api_calls) ? plural(pipeline.external_api_calls, 'call') : null;
    const opt = pipeline.optimizer || {};
    d.consolidation_fewer_stops = present(opt.stops_before_consolidation) ? opt.stops_before_consolidation - opt.stops : null;
    d.consolidation_extra_cost = opt.consolidation_extra_cost ?? null;
    if (d.is_plan_hit && present(server) && server > 0) d.cached_speedup = d.pipeline_total_ms / server;
    if (corridor && corridor.stations_searched) d.corridor_kept_pct = (corridor.candidates / corridor.stations_searched) * 100;
  }
  return d;
}
