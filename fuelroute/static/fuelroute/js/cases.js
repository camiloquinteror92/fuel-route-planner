// Edge cases, run live against this server: each one is a real request with the
// answer the API promises (status, error code, warning, external-call budget)
// and the pytest test that checks the same behaviour offline.
//
// "Run all" is sequential and waits between cases that may reach the public
// routing server (its fair-use policy is about one request per second); a 429
// stops the run and shows the server's Retry-After.

import { el, fmt, mark, replace, testBadge } from './format.js';
import { request, routeUrl, tripLabel } from './api.js';

const ROUTE_SPACING_MS = 1100;
const T = 'fuelroute/tests/test_api.py::';

export const CASES = [
  {
    id: 'ny_la', label: 'New York, NY → Los Angeles, CA', proves: 'A coast-to-coast trip: several stops, at most one routing call.',
    params: { start: 'New York, NY', finish: 'Los Angeles, CA', start_tank: 'empty' },
    expected: { status: 200, max_calls: 1 }, may_route: true, test_id: `${T}test_route_returns_plan_map_and_uses_one_external_call`,
  },
  {
    id: 'la_ny', label: 'Los Angeles, CA → New York, NY', proves: 'The first station is beyond the reserve: planned, with a warning.',
    params: { start: 'Los Angeles, CA', finish: 'New York, NY', start_tank: 'empty' },
    expected: { status: 200, warning_contains: 'first station', max_calls: 1 }, may_route: true,
    test_id: `${T}test_first_station_beyond_the_reserve_is_planned_not_rejected`,
  },
  {
    id: 'detroit_buffalo', label: 'Detroit, MI → Buffalo, NY', proves: 'The fastest road crosses Canada: the miles abroad are reported.',
    params: { start: 'Detroit, MI', finish: 'Buffalo, NY', start_tank: 'full' },
    expected: { status: 200, warning_contains: 'outside the USA', max_calls: 1 }, may_route: true,
    test_id: `${T}test_route_through_canada_warns_and_reports_the_miles`,
  },
  {
    id: 'seattle_la', label: 'Seattle, WA → Los Angeles, CA', proves: 'A stretch without stations longer than the range: 422 that says where.',
    params: { start: 'Seattle, WA', finish: 'Los Angeles, CA', start_tank: 'empty' },
    expected: { status: 422, error: 'no_reachable_fuel_station', max_calls: 1 }, may_route: true,
    test_id: `${T}test_gap_longer_than_the_range_is_422_and_says_where`,
  },
  {
    id: 'la_sf_empty', label: 'Los Angeles, CA → San Francisco, CA (empty)', proves: 'No station of the file along a trip that needs fuel.',
    params: { start: 'Los Angeles, CA', finish: 'San Francisco, CA', start_tank: 'empty' },
    expected: { status: 422, error: 'no_fuel_data_on_route', max_calls: 1 }, may_route: true,
    test_id: `${T}test_trip_needing_fuel_with_no_station_on_the_route_is_422`,
  },
  {
    id: 'la_sf_full', label: 'Los Angeles, CA → San Francisco, CA (full)', proves: 'Same trip on a full tank: no stop, route from the cache.',
    params: { start: 'Los Angeles, CA', finish: 'San Francisco, CA', start_tank: 'full' },
    expected: { status: 200, stops: 0, max_calls: 0 }, may_route: true, note: 'after the previous case',
    test_id: `${T}test_short_trip_with_full_tank_has_no_stops`,
  },
  {
    id: 'dallas_austin', label: 'Dallas, TX → Austin, TX (full)', proves: 'A short trip on a full tank needs no stop.',
    params: { start: 'Dallas, TX', finish: 'Austin, TX', start_tank: 'full' },
    expected: { status: 200, stops: 0, max_calls: 1 }, may_route: true, test_id: `${T}test_short_trip_with_full_tank_has_no_stops`,
  },
  {
    id: 'paris', label: 'Paris coordinates → Austin, TX', proves: 'A point outside the USA is rejected before any call.',
    params: { start: '48.8566,2.3522', finish: 'Austin, TX', start_tank: 'empty' },
    expected: { status: 400, error: 'invalid_request', max_calls: 0 }, test_id: `${T}test_points_outside_the_usa_are_400_without_calls`,
  },
  {
    id: 'anchorage', label: 'Anchorage, AK → Seattle, WA', proves: 'No price data in Alaska: 422 before routing.',
    params: { start: 'Anchorage, AK', finish: 'Seattle, WA', start_tank: 'empty' },
    expected: { status: 422, error: 'no_fuel_data_in_region', max_calls: 0 }, test_id: `${T}test_alaska_and_hawaii_are_422_before_routing`,
  },
  {
    id: 'same_place', label: 'Austin, TX → Austin, Texas', proves: 'The same place written two ways is caught before routing.',
    params: { start: 'Austin, TX', finish: 'Austin, Texas', start_tank: 'empty' },
    expected: { status: 400, error: 'same_location', max_calls: 0 }, test_id: `${T}test_same_place_written_differently_is_400_without_routing`,
  },
  {
    id: 'typo_param', label: 'Typo: start_tnak=full', proves: 'An unknown parameter is an error, not silently ignored.',
    query: { start: 'Chicago, IL', finish: 'Houston, TX', start_tnak: 'full' },
    expected: { status: 400, error: 'invalid_request', max_calls: 0 }, test_id: `${T}test_invalid_input_returns_400`,
  },
  {
    id: 'garbage', label: 'start=???', proves: 'Text that cannot be a place is rejected without geocoding.',
    query: { start: '???', finish: 'Houston, TX' },
    expected: { status: 400, error: 'invalid_request', max_calls: 0 }, test_id: `${T}test_invalid_input_returns_400`,
  },
  {
    id: 'bad_include', label: 'include=foo', proves: 'The optional include list only accepts known values.',
    query: { start: 'Chicago, IL', finish: 'Houston, TX', include: 'foo' },
    expected: { status: 400, error: 'invalid_request', detail_has: 'include', max_calls: 0 }, test_id: `${T}test_unknown_include_value_is_400`,
  },
  {
    id: 'post_json', label: 'POST JSON Chicago, IL → Houston, TX', proves: 'The same endpoint accepts a JSON body.',
    method: 'POST', body: { start: 'Chicago, IL', finish: 'Houston, TX' },
    expected: { status: 200, max_calls: 1 }, may_route: true, test_id: `${T}test_post_json_and_full_tank_mode`,
  },
  {
    id: 'broken_json', label: 'POST broken JSON', proves: 'Malformed JSON gets the same error format.',
    method: 'POST', raw: '{not json', contentType: 'application/json',
    expected: { status: 400, error: 'parse_error' }, test_id: `${T}test_drf_errors_use_the_same_format`,
  },
  {
    id: 'wrong_type', label: 'POST text/plain', proves: 'A wrong Content-Type is a 415 with the same format.',
    method: 'POST', raw: 'start=Chicago', contentType: 'text/plain',
    expected: { status: 415, error: 'unsupported_media_type' }, test_id: `${T}test_drf_errors_use_the_same_format`,
  },
  {
    id: 'wrong_method', label: 'PUT /api/route', proves: 'A wrong method is a 405 with the same format.',
    method: 'PUT', body: {},
    expected: { status: 405, error: 'method_not_allowed' }, test_id: `${T}test_drf_errors_use_the_same_format`,
  },
  {
    id: 'trailing_slash', label: '/api/route/ (trailing slash)', proves: 'The slash is optional; the first case is served from the plan cache.',
    params: { start: 'New York, NY', finish: 'Los Angeles, CA', start_tank: 'empty' }, slash: true,
    expected: { status: 200, plan_cache: 'hit', max_calls: 0 }, note: 'after the first case', test_id: `${T}test_trailing_slash_and_unknown_paths`,
  },
  {
    id: 'not_found', label: '/api/nope', proves: 'Unknown paths under /api/ answer a JSON 404.',
    path: 'nope', expected: { status: 404, error: 'not_found' }, test_id: `${T}test_trailing_slash_and_unknown_paths`,
  },
];

// Which case shows each error code (for "Try it" in the error catalog).
export const CASE_FOR_ERROR = {
  invalid_request: 'paris',
  same_location: 'same_place',
  no_fuel_data_in_region: 'anchorage',
  no_reachable_fuel_station: 'seattle_la',
  no_fuel_data_on_route: 'la_sf_empty',
  parse_error: 'broken_json',
  unsupported_media_type: 'wrong_type',
  method_not_allowed: 'wrong_method',
  not_found: 'not_found',
};

export function expectedText(expected) {
  const parts = [String(expected.status)];
  if (expected.error) parts.push(expected.error);
  if (expected.warning_contains) parts.push(`warning “${expected.warning_contains}”`);
  if (expected.detail_has) parts.push(`detail.${expected.detail_has}`);
  if (expected.stops !== undefined) parts.push(`${expected.stops === 0 ? 'no' : expected.stops} stops`);
  if (expected.plan_cache) parts.push(`plan cache ${expected.plan_cache}`);
  if (expected.max_calls !== undefined) parts.push(expected.max_calls === 0 ? 'no external call' : `≤ ${expected.max_calls} external call`);
  return parts.join(' · ');
}

export function evaluate(c, r) {
  const e = c.expected;
  const body = r.body && typeof r.body === 'object' ? r.body : {};
  const problems = [];
  if (r.status !== e.status) problems.push(`status ${r.status || 'none'}, expected ${e.status}`);
  if (e.error && body.error !== e.error) problems.push(`error ${body.error || 'none'}, expected ${e.error}`);
  if (e.warning_contains && !(body.warnings || []).some((w) => w.includes(e.warning_contains))) problems.push(`no warning about “${e.warning_contains}”`);
  if (e.detail_has && !(body.detail && typeof body.detail === 'object' && e.detail_has in body.detail)) problems.push(`detail has no “${e.detail_has}”`);
  if (e.stops !== undefined && body.summary?.number_of_stops !== e.stops) problems.push(`stops ${body.summary?.number_of_stops ?? 'none'}`);
  if (e.plan_cache && body.meta?.plan_cache !== e.plan_cache) problems.push(`plan cache ${body.meta?.plan_cache ?? 'none'}`);
  if (e.max_calls !== undefined) {
    const calls = body.meta?.external_api_calls;
    if (calls === undefined || calls === null) problems.push('no meta.external_api_calls');
    else if (calls > e.max_calls) problems.push(`${calls} external call(s)`);
  }
  return { ok: problems.length === 0, problems };
}

function requestOf(c, routeBase) {
  const base = c.slash ? `${routeBase}/` : routeBase;
  if (c.path) return { method: 'GET', url: routeBase.replace(/route\/?$/, c.path) };
  if (c.method === 'POST' || c.method === 'PUT') {
    return c.raw !== undefined
      ? { method: c.method, url: base, rawBody: c.raw, contentType: c.contentType }
      : { method: c.method, url: base, body: c.body };
  }
  if (c.params) return { method: 'GET', url: routeUrl(base, c.params) };
  return { method: 'GET', url: `${base}?${new URLSearchParams(c.query)}` };
}

export function curlOf(c, routeBase) {
  const r = requestOf(c, routeBase);
  const url = new URL(r.url, window.location.href).href;
  if (r.method === 'GET') return `curl "${url}"`;
  const data = r.rawBody !== undefined ? r.rawBody : JSON.stringify(r.body);
  return `curl -X ${r.method} "${url}" -H "Content-Type: ${r.contentType || 'application/json'}" -d '${data}'`;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createCases({ routeBase, getAbout, onOpen, onChange }) {
  const results = new Map();
  let running = false;
  let lastRoute = 0;
  let retryUntil = 0;
  let countdown = null;

  async function run(c) {
    if (c.may_route) {
      const since = performance.now() - lastRoute;
      if (since < ROUTE_SPACING_MS) await wait(ROUTE_SPACING_MS - since);
      lastRoute = performance.now();
    }
    results.set(c.id, { running: true });
    onChange();
    const spec = requestOf(c, routeBase);
    const r = await request(spec.method, spec.url, {
      body: spec.body, rawBody: spec.rawBody, contentType: spec.contentType, origin: 'edge case',
      trip: c.params ? tripLabel(c.params) : c.label,
    });
    const verdict = evaluate(c, r);
    results.set(c.id, { r, ...verdict, rateLimited: r.status === 429 });
    if (c.may_route) lastRoute = performance.now();
    onChange();
    return r;
  }

  function startCountdown(seconds) {
    retryUntil = Date.now() + seconds * 1000;
    clearInterval(countdown);
    countdown = setInterval(() => {
      if (Date.now() >= retryUntil) clearInterval(countdown);
      onChange();
    }, 1000);
  }

  async function runAll() {
    if (running) return;
    running = true;
    onChange();
    try {
      for (const c of CASES) {
        const r = await run(c);
        if (r.status === 429) {
          startCountdown(Number(r.headers.retryAfter) || 0);
          break;
        }
      }
    } finally {
      running = false;
      onChange();
    }
  }

  async function runOne(id) {
    const c = CASES.find((x) => x.id === id);
    if (!c || running) return null;
    running = true;
    try {
      const r = await run(c);
      if (r.status === 429) startCountdown(Number(r.headers.retryAfter) || 0);
      return r;
    } finally {
      running = false;
      onChange();
    }
  }

  function summary() {
    const done = CASES.filter((c) => results.get(c.id)?.r);
    return { done: done.length, total: CASES.length, ok: done.filter((c) => results.get(c.id).ok).length };
  }

  function render(container) {
    const about = getAbout();
    const s = summary();
    const waitSeconds = Math.max(0, Math.ceil((retryUntil - Date.now()) / 1000));
    const head = el('div', { class: 'cases-head' },
      el('button', { type: 'button', class: 'btn', disabled: running || waitSeconds > 0, onclick: runAll },
        running ? 'Running…' : waitSeconds > 0 ? `Rate limited: retry in ${waitSeconds} s` : 'Run all'),
      el('p', { class: 'muted' },
        s.done ? `${s.ok}/${s.done} edge cases behave as expected${s.done < s.total ? ` (${s.total - s.done} not run yet)` : ''}.` : `${CASES.length} cases, run one by one or all in order.`,
        ' Cases that may reach the routing server are spaced to respect its fair-use policy.'));
    const rows = CASES.map((c) => {
      const res = results.get(c.id);
      const r = res?.r;
      const body = r?.body && typeof r.body === 'object' ? r.body : {};
      let actual = '—';
      if (res?.running) actual = 'running…';
      else if (r) {
        actual = [r.status || r.failure, body.error, body.meta ? `${body.meta.external_api_calls} call(s)` : null,
          r.headers.responseTimeMs !== null ? fmt.ms(r.headers.responseTimeMs) : null].filter((x) => x !== null && x !== undefined).join(' · ');
      }
      return el('tr', {},
        el('th', { scope: 'row', 'data-label': 'Case' }, c.label, c.note ? el('small', { class: 'muted' }, ` (${c.note})`) : null),
        el('td', { 'data-label': 'What it proves' }, c.proves),
        el('td', { 'data-label': 'Expected' }, el('code', {}, expectedText(c.expected))),
        el('td', { 'data-label': 'Actual' }, actual, res && res.problems?.length ? el('small', { class: 'bad' }, res.problems.join('; ')) : null),
        el('td', { 'data-label': 'Result' }, r ? mark(res.ok, res.ok ? 'as expected' : 'unexpected') : mark(null, 'not run')),
        el('td', { 'data-label': 'Actions', class: 'actions-cell' },
          el('button', { type: 'button', class: 'btn btn-small', disabled: running || waitSeconds > 0, onclick: () => runOne(c.id) }, 'Run'),
          c.params ? el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: () => onOpen(c.params) }, 'Open in planner') : null),
        el('td', { 'data-label': 'Test' }, testBadge(about, c.test_id, { short: true })),
      );
    });
    replace(container, head, el('div', { class: 'table-wrap' }, el('table', { class: 'data cards cases-table' },
      el('caption', { class: 'sr-only' }, 'Edge cases'),
      el('thead', {}, el('tr', {}, ['Case', 'What it proves', 'Expected', 'Actual', 'Result', '', 'Test'].map((h) => el('th', { scope: 'col' }, h)))),
      el('tbody', {}, rows))));
  }

  return { run: runOne, runAll, render, results, summary, isRunning: () => running };
}
