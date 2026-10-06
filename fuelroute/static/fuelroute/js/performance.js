// Performance tab. Only measured numbers:
//   this request  - X-Response-Time-ms, Server-Timing and meta.timings_ms from the
//                   server; round trip, TTFB and size from the browser's Resource
//                   Timing; the cached computation from the answer's pipeline;
//   cache demo    - the same request again (plan cache), the other tank mode
//                   (route cache) and a sequential benchmark;
//   this session  - every request this page made;
//   since start   - /api/stats, the server's own per-process counters.

import { el, fmt, mark, plural, replace, present, sum, percentile, median, codeLink, svg, serverTimingDur, MIN_SAMPLES_FOR_P95 } from './format.js';

const GROUP_LABEL = { ext: 'external service', cache: 'cache', ours: 'our code', skipped: 'other' };
let zoomOurs = false;

function segmentsOf(meta, headers, serverMs, { pipelineMode = false } = {}) {
  const t = meta.timings_ms || {};
  const ext = meta.external_api_ms || 0;
  const services = meta.external_api_services || [];
  const nominatim = services.includes('nominatim');
  const osrm = services.includes('osrm');
  const list = [];
  const push = (key, label, ms, group, code) => {
    if (present(ms)) list.push({ key, label, ms: Math.max(0, ms), group, code });
  };
  push('geocoding', nominatim ? 'geocoding (incl. Nominatim)' : 'geocoding (offline)', t.geocoding_ms, nominatim ? 'ext' : 'ours', 'geocoding.geocode');
  push('plan_cache', 'plan cache lookup', t.plan_cache_ms, 'cache', 'planner.plan_trip');
  if (present(t.routing_ms)) {
    if (osrm && !nominatim && ext > 0) {
      push('osrm', 'OSRM routing call', Math.min(ext, t.routing_ms), 'ext', 'osrm.get_route');
      push('route_prep', 'route decode + resample', t.routing_ms - Math.min(ext, t.routing_ms), 'ours', 'osrm.prepare_route');
    } else if (osrm) {
      push('routing', 'routing (incl. OSRM)', t.routing_ms, 'ext', 'osrm.get_route');
    } else {
      push('route_cache', 'route from the route cache', t.routing_ms, 'cache', 'osrm.get_route');
    }
  }
  push('corridor', 'stations near the route', t.corridor_ms, 'ours', 'stations.stations_along_route');
  push('optimizer', 'optimizer', t.optimizer_ms, 'ours', 'optimizer.greedy');
  push('build', 'response build', t.response_build_ms, 'ours', 'planner.build_stops');
  push('comparison', 'strategy comparison', t.comparison_ms, 'ours', 'planner.comparison');
  push('candidates', 'map layer (only this page asks for it)', t.candidates_ms, 'ours', 'planner.plan_trip');
  if (!pipelineMode) {
    push('render', 'JSON render', serverTimingDur(headers, 'render'), 'ours', 'middleware.response_time');
    if (present(serverMs)) {
      const other = serverMs - sum(list.map((s) => s.ms));
      list.push({ key: 'other', label: 'other (framework, validation, middleware)', ms: Math.max(0, other), group: 'skipped', code: null });
    }
  }
  return list;
}

function bar(segments, about, { gray = false, zoom = false, label } = {}) {
  const shown = zoom ? segments.filter((s) => s.group !== 'ext') : segments;
  const total = sum(shown.map((s) => s.ms)) || 1;
  const track = el('div', { class: `stack ${gray ? 'stack-gray' : ''}`, role: 'img', 'aria-label': label || 'Time per stage' },
    shown.filter((s) => s.ms > 0).map((s) => {
      const link = s.code ? codeLink(about, s.code) : null;
      const attrs = { class: `seg seg-${s.group}`, title: `${s.label}: ${fmt.ms(s.ms)}`, css: { 'flex-grow': String(s.ms / total) } };
      return link ? el('a', { ...attrs, href: link.url, target: '_blank', rel: 'noopener' }) : el('span', attrs);
    }));
  const legend = el('ul', { class: 'stack-legend' }, shown.map((s) => {
    const link = s.code ? codeLink(about, s.code) : null;
    return el('li', {},
      el('span', { class: `swatch seg-${gray ? 'skipped' : s.group}`, 'aria-hidden': 'true' }),
      link ? el('a', { href: link.url, target: '_blank', rel: 'noopener' }, s.label) : el('span', {}, s.label),
      el('span', { class: 'num' }, fmt.ms(s.ms)),
      el('span', { class: 'num muted' }, fmt.pct((s.ms / total) * 100)));
  }));
  return el('div', { class: 'stack-wrap' }, track, legend);
}

function card(title, value, sub, cls = '') {
  return el('article', { class: `kpi kpi-small ${cls}` }, el('h4', {}, title), el('p', { class: 'kpi-value' }, value), sub ? el('p', { class: 'kpi-sub' }, sub) : null);
}

export function renderRequest(container, state) {
  const result = state.route;
  if (!result) {
    replace(container, el('p', { class: 'muted' }, 'Plan a trip: this section measures that request.'));
    return;
  }
  const about = state.about || {};
  const meta = result.body?.meta || {};
  const server = result.headers.responseTimeMs;
  const ext = meta.external_api_ms ?? null;
  const c = result.client;
  const network = present(server) && present(c.roundTripMs) ? Math.max(0, c.roundTripMs - server) : null;
  const calls = meta.external_api_calls;
  const cards = el('div', { class: 'kpis kpis-perf' },
    card('Server', present(server) ? fmt.ms(server) : '—', 'X-Response-Time-ms header'),
    card('External API', present(ext) ? fmt.ms(ext) : '—',
      present(calls) ? `${plural(calls, 'call')}${(meta.external_api_services || []).length ? ` to ${[...new Set(meta.external_api_services)].join(', ')}` : ''}` : 'no meta in this answer',
      calls === 0 ? 'is-ok' : calls > 1 ? 'is-warn' : ''),
    card('Our code', present(server) ? fmt.ms(Math.max(0, server - (ext || 0))) : '—', 'server − external'),
    card('Browser round trip', fmt.ms(c.roundTripMs), c.source === 'resource-timing' ? 'Resource Timing' : 'performance.now()'),
    card('Network + framework', present(network) ? fmt.ms(network) : '—', 'approx: round trip − server'),
    card('Transfer (incl. headers)', present(c.transferBytes) ? fmt.bytes(c.transferBytes) : '—',
      [
        present(c.encodedBytes) ? `body ${fmt.bytes(c.encodedBytes)} as sent (${result.headers.contentEncoding || 'not compressed'})` : null,
        present(c.decodedBytes) ? `${fmt.bytes(c.decodedBytes)} decoded` : null,
        present(c.parseMs) ? `JSON.parse ${fmt.ms(c.parseMs)}` : null,
      ].filter(Boolean).join(' · ') || null),
    card('TTFB', present(c.ttfbMs) ? fmt.ms(c.ttfbMs) : '—', 'request start → first byte'),
  );

  const segments = segmentsOf(meta, result.headers, server);
  const extMs = sum(segments.filter((s) => s.group === 'ext').map((s) => s.ms));
  const children = [cards];
  if (segments.length) {
    const toggle = el('div', { class: 'seg-toggle', role: 'group', 'aria-label': 'Bar scale' },
      el('button', { type: 'button', class: `btn btn-small ${zoomOurs ? 'btn-ghost' : ''}`, 'aria-pressed': String(!zoomOurs), onclick: () => { zoomOurs = false; renderRequest(container, state); } }, 'linear'),
      el('button', { type: 'button', class: `btn btn-small ${zoomOurs ? '' : 'btn-ghost'}`, 'aria-pressed': String(zoomOurs), onclick: () => { zoomOurs = true; renderRequest(container, state); } }, 'zoom to our code'));
    children.push(el('div', { class: 'stack-head' },
      el('h4', {}, 'Where the server time went'), toggle),
    bar(segments, about, { zoom: zoomOurs, label: 'Server time per stage of this request' }),
    present(server) && server > 0 ? el('p', { class: 'muted small' }, `External service share: ${fmt.pct((extMs / server) * 100)} of the server time. Click a segment to open its code.`) : null);
  }

  const body = result.body;
  const pipeline = body?.pipeline;
  if (result.ok && meta.plan_cache === 'hit' && pipeline) {
    const computedMs = sum(Object.values(pipeline.timings_ms || {}));
    const ratio = present(server) && server > 0 ? computedMs / server : null;
    children.push(el('div', { class: 'cached-card' },
      el('h4', {}, 'Served from the plan cache'),
      el('p', {}, `Computed at ${fmt.time(pipeline.computed_at)} in ${fmt.ms(computedMs)} with ${plural(pipeline.external_api_calls, 'external call')} → now ${fmt.ms(server)} with ${fmt.int(meta.external_api_calls)}`,
        ratio ? el('strong', {}, ` (${fmt.ratio(ratio)} faster)`) : null, '.'),
      bar(segmentsOf({ ...pipeline, timings_ms: pipeline.timings_ms }, null, null, { pipelineMode: true }), about, { gray: true, label: 'How long the cached plan took to compute' })));
  }
  children.push(el('p', { class: 'muted small' }, 'Every API answer carries a Server-Timing header: DevTools › Network › Timing shows the same breakdown.'));
  replace(container, ...children);
}

// --- benchmark ----------------------------------------------------------------------

function sparkline(series) {
  const w = 320;
  const h = 64;
  const all = series.flatMap((s) => s.values).filter(present);
  if (!all.length) return null;
  const max = Math.max(...all) || 1;
  const n = Math.max(...series.map((s) => s.values.length));
  const xs = (i) => (n > 1 ? (i / (n - 1)) * (w - 8) + 4 : w / 2);
  return svg('svg', { viewBox: `0 0 ${w} ${h}`, class: 'sparkline', role: 'img', 'aria-label': 'Benchmark timings per request' },
    series.map((s) => svg('polyline', {
      class: `spark-${s.cls}`,
      points: s.values.map((v, i) => `${xs(i).toFixed(1)},${(h - 4 - (v / max) * (h - 8)).toFixed(1)}`).join(' '),
    })));
}

// p95 only with enough samples: with fewer it is just the maximum again.
function p95Cell(value, count) {
  return count >= MIN_SAMPLES_FOR_P95
    ? el('td', { class: 'num' }, fmt.ms(value))
    : el('td', { class: 'num muted', title: `A percentile needs at least ${MIN_SAMPLES_FOR_P95} requests; this has ${count}.` }, '—');
}

function statRow(label, values) {
  return el('tr', {}, el('th', { scope: 'row' }, label),
    el('td', { class: 'num' }, fmt.ms(Math.min(...values))),
    el('td', { class: 'num' }, fmt.ms(percentile(values, 50))),
    p95Cell(percentile(values, 95), values.length),
    el('td', { class: 'num' }, fmt.ms(Math.max(...values))));
}

export function createBenchmark(container, { getState, client, routeUrlFor, onDone }) {
  let running = false;
  let report = null;
  let progress = null;
  let chosen = null;

  function render() {
    const state = getState();
    const can = Boolean(state.route?.ok) && !running;
    const select = el('select', { id: 'bench-n', 'aria-label': 'Number of requests', onchange: () => { chosen = select.value; } },
      [5, 10, 20].map((n) => el('option', { value: n, selected: String(n) === chosen }, `${n} requests`)));
    const children = [el('div', { class: 'bench-controls' },
      select,
      el('button', { type: 'button', class: 'btn', disabled: !can, onclick: () => start(Number(select.value)) }, running ? `Running ${progress || ''}` : 'Benchmark'),
      el('span', { class: 'muted small' }, 'Sequential GETs of this trip without include (the request Postman sends). Stops at the first answer that needs an external call or is rate limited.'))];
    if (report) {
      const ok = report.results.filter((r) => r.ok);
      if (report.stopped) {
        const why = report.stopped.reason === 'rate_limited'
          ? `stopped: rate limited${report.stopped.retryAfter ? `, retry in ${report.stopped.retryAfter} s` : ''}`
          : report.stopped.reason === 'external_call' ? 'stopped: an answer needed an external call (the plan was not cached)' : `stopped: ${report.stopped.reason}`;
        children.push(el('p', { class: 'banner banner-warn' }, el('span', { class: 'banner-icon', 'aria-hidden': 'true' }), el('span', {}, why)));
      }
      if (ok.length) {
        const server = ok.map((r) => r.headers.responseTimeMs).filter(present);
        const trip = ok.map((r) => r.client.roundTripMs);
        const hits = ok.filter((r) => r.body?.meta?.plan_cache === 'hit').length;
        children.push(el('div', { class: 'bench-result' },
          el('table', { class: 'data compact' },
            el('caption', {}, hits === ok.length
              ? `${plural(ok.length, 'answer')}, all from the plan cache`
              : `${plural(ok.length, 'answer')}, ${fmt.int(hits)} of them from the plan cache`),
            el('thead', {}, el('tr', {}, ['', 'min', 'p50', 'p95', 'max'].map((h) => el('th', { scope: 'col', class: h ? 'num' : null }, h)))),
            el('tbody', {}, server.length ? statRow('server', server) : null, statRow('round trip', trip))),
          el('figure', { class: 'spark-fig' }, sparkline([{ cls: 'trip', values: trip }, { cls: 'server', values: server }]),
            el('figcaption', { class: 'muted small' }, el('span', { class: 'swatch spark-trip-sw' }), ' round trip ', el('span', { class: 'swatch spark-server-sw' }), ' server'))));
      }
    }
    replace(container, ...children);
  }

  async function start(n) {
    const state = getState();
    if (!state.route?.ok || running) return;
    running = true;
    report = null;
    progress = `0/${n}`;
    render();
    report = await client.benchmark(routeUrlFor(state.params), n, (i, total) => {
      progress = `${i}/${total}`;
      render();
    });
    running = false;
    progress = null;
    render();
    onDone?.();
  }

  return { render };
}

// --- session log ------------------------------------------------------------------------

export function renderSession(container, state, { onClear } = {}) {
  const all = state.session || [];
  // City suggestions are one request per pause in typing: counted, not listed.
  const lookups = all.filter((r) => r.kind === 'places');
  const log = all.filter((r) => r.kind !== 'places');
  const lookupMs = lookups.map((r) => r.serverMs).filter(present);
  const routeRows = log.filter((r) => r.kind === 'route');
  const newTrips = routeRows.filter((r) => r.osrmCalls > 0 || r.routeCache === 'miss').length;
  const osrm = routeRows.reduce((t, r) => t + (r.osrmCalls || 0), 0);
  const head = el('div', { class: 'session-head' },
    el('p', {}, el('strong', {}, `New trips: ${fmt.int(newTrips)} · OSRM calls: ${fmt.int(osrm)}`),
      newTrips ? el('span', { class: 'muted' }, ` (${fmt.num(osrm / newTrips)} per new trip)`) : null,
      el('span', { class: 'muted' }, ` · ${fmt.int(log.length)} requests from this page, kept in memory only.`),
      lookups.length ? el('span', { class: 'muted' }, ` Plus ${plural(lookups.length, 'city suggestion lookup')} to /api/places (not listed${lookupMs.length ? `; server median ${fmt.ms(median(lookupMs))}` : ''}, 0 external calls).`) : null),
    el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: onClear, disabled: !all.length }, 'Clear'));
  if (!log.length) {
    replace(container, head, el('p', { class: 'muted' }, 'No request yet.'));
    return;
  }
  const rows = log.map((r) => el('tr', { class: r.status >= 400 || r.status === 0 ? 'is-error' : '' },
    el('td', { 'data-label': 'Time', class: 'num' }, fmt.time(r.at)),
    el('td', { 'data-label': 'Endpoint' }, el('code', {}, `${r.method} ${r.path}`)),
    el('td', { 'data-label': 'Trip' }, r.trip || '—'),
    el('td', { 'data-label': 'Origin' }, r.origin),
    el('td', { 'data-label': 'Status', class: 'num' }, r.status || r.error, r.error && r.status ? el('small', {}, ` ${r.error}`) : null),
    el('td', { 'data-label': 'External calls', class: 'num' }, present(r.calls) ? fmt.int(r.calls) : '—'),
    el('td', { 'data-label': 'Cache' }, r.planCache ? `plan ${r.planCache} · route ${r.routeCache ?? '—'}` : '—'),
    el('td', { 'data-label': 'Server', class: 'num' }, present(r.serverMs) ? fmt.ms(r.serverMs) : '—'),
    el('td', { 'data-label': 'Round trip', class: 'num' }, present(r.roundTripMs) ? fmt.ms(r.roundTripMs) : '—'),
    el('td', { 'data-label': 'Size', class: 'num' }, present(r.bytes) ? fmt.bytes(r.bytes) : '—')));
  replace(container, head, el('div', { class: 'table-wrap table-scroll' }, el('table', { class: 'data cards compact session-table' },
    el('caption', { class: 'sr-only' }, 'Requests made by this page'),
    el('thead', {}, el('tr', {}, ['Time', 'Endpoint', 'Trip', 'Origin', 'Status', 'External calls', 'Cache', 'Server', 'Round trip', 'Size']
      .map((h, i) => el('th', { scope: 'col', class: [0, 4, 5, 7, 8, 9].includes(i) ? 'num' : null }, h)))),
    el('tbody', {}, rows))));
}

// --- since server start -------------------------------------------------------------------

// The bar shows the p95 when there are enough samples, else the maximum.
function barValue(p) {
  return p.count >= MIN_SAMPLES_FOR_P95 ? p.p95 : p.max;
}

function pctRow(label, p, max) {
  if (!p) return el('tr', {}, el('th', { scope: 'row' }, label), el('td', { colspan: 5, class: 'muted' }, 'no request yet'));
  return el('tr', {},
    el('th', { scope: 'row' }, label),
    el('td', { class: 'num' }, fmt.int(p.count)),
    el('td', { class: 'num' }, fmt.ms(p.p50)),
    p95Cell(p.p95, p.count),
    el('td', { class: 'num' }, fmt.ms(p.max)),
    el('td', { class: 'barcell' }, el('span', { class: 'hbar', title: fmt.ms(barValue(p)), css: { width: `${Math.max(1, (barValue(p) / (max || 1)) * 100)}%` } })));
}

export function renderStats(container, stats, { onRefresh } = {}) {
  const refresh = el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: onRefresh }, 'Refresh');
  if (!stats) {
    replace(container, el('p', { class: 'muted' }, 'This server does not publish /api/stats.'), refresh);
    return;
  }
  const p = stats.process || {};
  const rr = stats.route_requests || {};
  const ext = stats.external_api || {};
  const lat = stats.latency_ms || {};
  const outcomes = ['cold', 'nominatim_call', 'route_cache_hit', 'plan_cache_hit', 'error'];
  const extLat = stats.external_latency_ms || {};
  const maxBar = Math.max(0, ...[...outcomes.map((k) => lat[k]), extLat.osrm, extLat.nominatim].filter(Boolean).map(barValue));
  const kv = (obj) => Object.entries(obj || {}).map(([k, v]) => `${k} ${fmt.int(v)}`).join(' · ') || 'none';
  replace(container,
    el('div', { class: 'stats-head' },
      el('p', {}, `Process ${p.pid ?? '—'} up for ${present(p.uptime_seconds) ? fmt.duration(p.uptime_seconds) : '—'}`,
        p.warm_up ? ` · warm-up ${fmt.ms(p.warm_up.ms)} (${fmt.int(p.warm_up.places)} places, ${fmt.int(p.warm_up.stations)} stations pre-loaded)` : ''),
      refresh),
    el('div', { class: 'kpis kpis-perf' },
      card('Route requests', fmt.int(rr.total ?? 0), `by status: ${kv(rr.by_status)}`),
      card('By outcome', kv(rr.by_outcome), 'cold = routing call · nominatim_call = free text geocoded · cache hits · errors'),
      card('External calls', fmt.int(ext.calls ?? 0), `${kv(ext.by_service)} · ${fmt.ms(ext.total_ms ?? 0)} in total`),
      card('OSRM calls per routing request', present(ext.osrm_calls_per_routing_request) ? fmt.num(ext.osrm_calls_per_routing_request) : '—',
        present(ext.max_osrm_calls_in_one_request)
          ? `at most ${plural(ext.max_osrm_calls_in_one_request, 'OSRM call')} in one request${(ext.max_calls_in_one_request ?? 0) > ext.max_osrm_calls_in_one_request ? ` (${plural(ext.max_calls_in_one_request, 'external call')} with geocoding)` : ''}`
          : `at most ${plural(ext.max_calls_in_one_request ?? 0, 'external call')} in one request`),
      card('Errors', kv(rr.errors), `${fmt.int(rr.errors_without_external_calls ?? 0)} of them cost no external call`)),
    el('h4', {}, `Latency by outcome (last ${fmt.int(lat.window ?? 0)} requests, server time)`),
    el('div', { class: 'table-wrap' }, el('table', { class: 'data compact latency-table' },
      el('thead', {}, el('tr', {}, ['Outcome', 'Requests', 'p50', 'p95', 'max', 'Relative'].map((h, i) => el('th', { scope: 'col', class: i > 0 && i < 5 ? 'num' : null }, h)))),
      el('tbody', {},
        pctRow('cold: the routing call', lat.cold, maxBar),
        pctRow('free text: a Nominatim call (+ routing)', lat.nominatim_call, maxBar),
        pctRow('route cache hit', lat.route_cache_hit, maxBar),
        pctRow('plan cache hit', lat.plan_cache_hit, maxBar),
        pctRow('error', lat.error, maxBar),
        pctRow('OSRM call alone', extLat.osrm, maxBar),
        extLat.nominatim ? pctRow('Nominatim call alone', extLat.nominatim, maxBar) : null))),
    el('p', { class: 'muted small' }, `p95 is shown from ${MIN_SAMPLES_FOR_P95} requests on; the bar is the p95, or the maximum below that.`),
    el('h4', {}, 'Recent route requests (no locations are kept)'),
    (stats.recent || []).length
      ? el('div', { class: 'table-wrap table-scroll' }, el('table', { class: 'data compact cards' },
        el('thead', {}, el('tr', {}, ['Time', 'Method', 'Status', 'Outcome', 'External calls', 'Server'].map((h) => el('th', { scope: 'col' }, h)))),
        el('tbody', {}, stats.recent.map((r) => el('tr', { class: r.status >= 400 ? 'is-error' : '' },
          el('td', { 'data-label': 'Time' }, fmt.time(r.at)), el('td', { 'data-label': 'Method' }, r.method),
          el('td', { 'data-label': 'Status' }, String(r.status), r.error ? el('small', {}, ` ${r.error}`) : null),
          el('td', { 'data-label': 'Outcome' }, r.outcome), el('td', { 'data-label': 'External calls', class: 'num' }, fmt.int(r.external_api_calls)),
          el('td', { 'data-label': 'Server', class: 'num' }, fmt.ms(r.ms)))))))
      : el('p', { class: 'muted' }, 'No route request since the server started.'),
    el('p', { class: 'muted small' }, mark(null, ''), ' Per server process: a restart, or the development auto-reloader after a code change, resets these counters and the caches.'));
}
