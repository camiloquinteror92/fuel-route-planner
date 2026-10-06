// "How it scales" tab: measurements, not guesses.
//
// * The benchmark: `python manage.py benchmark` (data/benchmark.json, served by
//   /api/stats as "benchmark"): latency and requests per second of each kind of
//   request, the machine it ran on, the process memory and the number of stations.
// * What each request costs on THIS server since it started (/api/stats) and the
//   current answer, to show where a new trip spends its time: the routing call.
// * The live numbers of the ladder (<span data-scale="...">) come from the same data.

import { el, fmt, plural, replace, present, MIN_SAMPLES_FOR_P95 } from './format.js';

const SCENARIOS = [
  ['plan_cache_hit', 'Repeated trip', 'served from the plan cache'],
  ['what_if_replan', 'What-if', 'planned again on the cached route'],
  ['planning_no_network', 'Our planning alone', 'corridor search, tank rules and optimizer'],
  ['route_preparation', 'Route preparation', 'decoding and preparing what OSRM sends'],
];

export function benchmarkOf(stats) {
  const b = stats?.benchmark;
  return b && typeof b === 'object' && b.results ? b : null;
}

function scenario(b, key) {
  const r = b?.results?.[key];
  return r && typeof r === 'object' ? r : null;
}

// The routing server's latency: this process's calls, else the current plan's call.
function osrmLatency(state) {
  const live = state.stats?.external_latency_ms?.osrm;
  if (live?.count) {
    const p95 = live.count >= MIN_SAMPLES_FOR_P95 ? `, p95 ${fmt.ms(live.p95)}` : '';
    return { ms: live.p50, text: `${fmt.ms(live.p50)} (median of ${plural(live.count, 'call')} since the server started${p95})` };
  }
  const pipeline = state.route?.ok ? state.route.body?.pipeline : null;
  if (pipeline?.external_api_calls && present(pipeline.external_api_ms)) {
    return { ms: pipeline.external_api_ms, text: `${fmt.ms(pipeline.external_api_ms)} (the call behind this plan)` };
  }
  return null;
}

function machineList(b) {
  const m = b.machine || {};
  const mem = b.memory || {};
  const trip = b.trip || {};
  const rows = [
    ['Measured', present(b.measured_at) ? `${fmt.datetime(b.measured_at)} (${fmt.ago(b.measured_at)})` : '—'],
    ['Machine', [m.cpu, present(m.cores) ? plural(m.cores, 'core') : null].filter(Boolean).join(' · ') || '—'],
    ['System', m.os || '—'],
    ['Software', [m.python && `Python ${m.python}`, m.django && `Django ${m.django}`, m.numpy && `numpy ${m.numpy}`].filter(Boolean).join(' · ') || '—'],
    ['Process memory', present(mem.rss_mb) ? `${fmt.num(mem.rss_mb)} MB ${mem.kind === 'peak' ? '(peak)' : '(resident, at the end)'}` : '—'],
    ['Stations loaded', present(b.stations) ? fmt.int(b.stations) : '—'],
    ['Trip', trip.start ? `${trip.start} → ${trip.finish}: ${fmt.miles(trip.distance_miles)}, ${plural(trip.fuel_stops ?? 0, 'stop')}, ${plural(trip.corridor_candidates ?? 0, 'station')} near the route` : '—'],
    ['External calls while measuring', present(b.external_api_calls) ? fmt.int(b.external_api_calls) : '—'],
  ];
  return el('dl', { class: 'bench-machine' }, rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));
}

function benchTable(b) {
  const rows = SCENARIOS.map(([key, label, sub]) => [key, label, sub, scenario(b, key)]).filter(([, , , r]) => r);
  const top = Math.max(1, ...rows.map(([, , , r]) => r.p95_ms || 0));
  return el('div', { class: 'table-wrap' }, el('table', { class: 'data cards compact bench-table' },
    el('caption', { class: 'sr-only' }, 'Benchmark results'),
    el('thead', {}, el('tr', {}, ['Request', 'What it measures', 'Runs', 'p50', 'p95', 'Per second', ''].map((h, i) =>
      el('th', { scope: 'col', class: i >= 2 && i <= 5 ? 'num' : null }, h)))),
    el('tbody', {}, rows.map(([, label, sub, r]) => el('tr', {},
      el('th', { scope: 'row', 'data-label': 'Request' }, label, el('small', { class: 'muted', css: { display: 'block' } }, sub)),
      el('td', { 'data-label': 'What it measures', class: 'small' }, r.description || ''),
      el('td', { 'data-label': 'Runs', class: 'num' }, present(r.iterations) ? fmt.int(r.iterations) : '—'),
      el('td', { 'data-label': 'p50', class: 'num strong' }, present(r.p50_ms) ? fmt.ms(r.p50_ms) : '—'),
      el('td', { 'data-label': 'p95', class: 'num' }, present(r.p95_ms) ? fmt.ms(r.p95_ms) : '—'),
      el('td', { 'data-label': 'Per second', class: 'num' }, present(r.requests_per_second) ? fmt.int(r.requests_per_second) : '—'),
      el('td', { class: 'barcell', 'aria-hidden': 'true' }, el('span', { class: 'hbar', css: { width: `${Math.max(1, ((r.p50_ms || 0) / top) * 100)}%` } })))))));
}

export function renderBench(container, state, { onRefresh } = {}) {
  const b = benchmarkOf(state.stats);
  const refresh = el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: onRefresh }, 'Refresh');
  if (!state.stats) {
    replace(container, el('p', { class: 'muted' }, 'Loading /api/stats…'));
    return;
  }
  if (!b) {
    replace(container, el('p', { class: 'muted' }, 'No benchmark on this server yet: run ', el('code', {}, 'python manage.py benchmark'),
      ' (one routing call the first time, to save the route), then refresh. ', refresh));
    return;
  }
  replace(container, machineList(b), benchTable(b),
    el('p', { class: 'muted small' }, b.method || '', ' ', refresh));
}

function costRow(label, calls, bench, live, top, kind) {
  const value = bench ?? live;
  return el('tr', {},
    el('th', { scope: 'row', 'data-label': 'Request' }, label),
    el('td', { 'data-label': 'External calls', class: 'num' }, calls),
    el('td', { 'data-label': 'Benchmark p50', class: 'num' }, present(bench) ? fmt.ms(bench) : '—'),
    el('td', { 'data-label': 'This server p50', class: 'num' }, present(live) ? fmt.ms(live) : '—'),
    el('td', { class: 'barcell', 'aria-hidden': 'true' }, present(value)
      ? el('span', { class: `hbar ${kind}`, css: { width: `${Math.max(1, (value / (top || 1)) * 100)}%` } }) : null));
}

export function renderCosts(container, state) {
  const b = benchmarkOf(state.stats);
  const lat = state.stats?.latency_ms || {};
  const cached = scenario(b, 'plan_cache_hit')?.p50_ms ?? null;
  const replan = scenario(b, 'what_if_replan')?.p50_ms ?? null;
  const prep = scenario(b, 'route_preparation')?.p50_ms ?? null;
  const osrm = osrmLatency(state);
  const cold = lat.cold?.p50 ?? null;
  // A new trip without a live measurement: the routing call plus our measured part.
  const newTrip = present(osrm?.ms) && present(replan) ? osrm.ms + replan + (prep || 0) : null;
  const top = Math.max(1, ...[cached, replan, cold, newTrip, lat.plan_cache_hit?.p50, lat.route_cache_hit?.p50].filter(present));
  const table = el('div', { class: 'table-wrap' }, el('table', { class: 'data cards compact costs-table' },
    el('caption', { class: 'sr-only' }, 'What each kind of request costs'),
    el('thead', {}, el('tr', {}, ['Request', 'External calls', 'Benchmark p50', 'This server p50', ''].map((h, i) =>
      el('th', { scope: 'col', class: i >= 1 && i <= 3 ? 'num' : null }, h)))),
    el('tbody', {},
      costRow('Repeated trip (plan cache)', '0', cached, lat.plan_cache_hit?.p50 ?? null, top, 'is-cache'),
      costRow('What-if on a planned trip (route cache)', '0', replan, lat.route_cache_hit?.p50 ?? null, top, ''),
      costRow('New trip (one OSRM call)', '1', newTrip, cold, top, 'is-ext'))));
  const share = present(osrm?.ms) && present(cold) && cold > 0 ? Math.min(100, (osrm.ms / cold) * 100) : null;
  const ours = scenario(b, 'planning_no_network')?.p50_ms ?? null;
  replace(container, table,
    el('p', { class: 'small' },
      osrm ? ['One OSRM call: ', el('strong', {}, osrm.text), '. '] : 'Plan a new trip to measure the routing call on this server. ',
      present(ours) ? ['All our planning: ', el('strong', {}, fmt.ms(ours)), ' (benchmark p50). '] : null,
      present(share) ? ['A new trip spends ', el('strong', {}, fmt.pct(share)), ' of its server time waiting for OSRM.'] : null,
      present(osrm?.ms) && present(ours) && ours > 0 ? [' The call is ', el('strong', {}, fmt.ratio(osrm.ms / ours)), ' our whole planning: that is the bottleneck, and why trips and routes are cached.'] : null),
    el('p', { class: 'muted small' }, '“New trip” in the benchmark column is the OSRM call plus the measured what-if and route preparation, since the benchmark never calls the network; “This server” is measured on real requests since it started.'));
}

// The live numbers inside the ladder's prose.
export function fillLadder(root, state) {
  const b = benchmarkOf(state.stats);
  const osrm = osrmLatency(state);
  const replans = state.stats?.latency_ms?.route_cache_hit;
  const liveReplan = replans?.count ? `${fmt.ms(replans.p50)} (median of ${plural(replans.count, 'replan')} on this server)` : null;
  const values = {
    osrm: osrm ? fmt.ms(osrm.ms) : null,
    // Without a benchmark, the replans this server measured (a route cache hit is all our planning).
    full: present(scenario(b, 'planning_no_network')?.p50_ms) ? fmt.ms(scenario(b, 'planning_no_network').p50_ms) : liveReplan,
    whatif: present(scenario(b, 'what_if_replan')?.p50_ms) ? fmt.ms(scenario(b, 'what_if_replan').p50_ms) : liveReplan,
    cached_rps: present(scenario(b, 'plan_cache_hit')?.requests_per_second) ? fmt.int(scenario(b, 'plan_cache_hit').requests_per_second) : null,
    whatif_rps: present(scenario(b, 'what_if_replan')?.requests_per_second) ? fmt.int(scenario(b, 'what_if_replan').requests_per_second) : null,
    stations: present(b?.stations) ? fmt.int(b.stations) : present(state.about?.data?.geocoded) ? fmt.int(state.about.data.geocoded) : null,
    rss: present(b?.memory?.rss_mb) ? `${fmt.num(b.memory.rss_mb)} MB` : null,
  };
  for (const node of root.querySelectorAll('[data-scale]')) {
    const value = values[node.dataset.scale];
    node.textContent = value ?? '—';
    node.classList.toggle('is-missing', !value);
  }
}
