// Entry point of the planner page. The server only renders the shell; this module
// calls the public JSON API (the same GET /api/route that Postman uses), keeps
// the trip in the URL (?start&finish&start_tank, #tab) and renders every part of
// the page from the answer, /api/about, /api/stats and the browser's timings.

import { createClient, onLog, session, clearSession, routeUrl } from './api.js';
import { bind, derive, el, fmt, flattenErrors, plural, replace, present } from './format.js';
import { runChecks, planTrace, priceBlindTrace, summarize } from './checks.js';
import { createMap } from './map.js';
import { createProfile } from './profile.js';
import * as plan from './plan.js';
import * as requirements from './requirements.js';
import * as perf from './performance.js';
import * as how from './how.js';
import * as apitab from './apitab.js';
import { createCases } from './cases.js';

const CLIENT_TIMEOUT_MS = 40000;
const TOAST_MS = 2600;
const TABS = ['plan', 'requirements', 'performance', 'how', 'api'];
const DERIVE_HELPERS = { planTrace, priceBlindTrace, summarize };

const $ = (selector) => document.querySelector(selector);
const config = JSON.parse($('#page-config').textContent);
const client = createClient(config);
const routeBase = config.api.route;

const state = {
  route: null, // last /api/route result: {ok, status, body, headers, client, ...}
  params: null, // {start, finish, start_tank} of that result
  stats: null,
  about: config.about || {},
  session,
  checks: [],
};

// --- elements ---------------------------------------------------------------------------

const form = $('#trip-form');
const inputs = { start: $('#start'), finish: $('#finish') };
const planButton = $('#plan-btn');
const planTimer = planButton.querySelector('[data-timer]');
const planLabel = planButton.querySelector('.btn-label');
const live = $('#live');
const errorBox = $('#error');
const staleNote = $('#stale');
const hero = $('#empty');
const results = $('#results');
const profileCard = $('#profile-card');
const toasts = $('#toasts');
const boxes = {
  warnings: $('#warnings'),
  stops: $('#stops-table'),
  compare: $('#comparison-table'),
  actions: $('#plan-actions'),
  reqHead: $('#req-head'),
  reqTable: $('#req-table'),
  checks: $('#checks-list'),
  cases: $('#cases'),
  perfRequest: $('#perf-request'),
  bench: $('#perf-bench'),
  session: $('#perf-session'),
  stats: $('#perf-stats'),
  funnel: $('#funnel'),
  states: $('#state-bars'),
  timeline: $('#timeline'),
  historySummary: $('#history-summary'),
  inventory: $('#test-inventory'),
  apiRequest: $('#api-request'),
  apiResponse: $('#api-response'),
  apiReference: $('#api-reference'),
  apiErrors: $('#api-errors'),
};

function toast(message, kind = 'ok') {
  const node = el('div', { class: `toast toast-${kind}`, role: 'status' }, message);
  toasts.append(node);
  setTimeout(() => node.remove(), TOAST_MS);
}

function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// --- form and URL ---------------------------------------------------------------------------

function readForm() {
  const tank = form.querySelector('input[name="start_tank"]:checked');
  return { start: inputs.start.value.trim(), finish: inputs.finish.value.trim(), start_tank: tank && tank.value === 'full' ? 'full' : 'empty' };
}

function setForm(params) {
  inputs.start.value = params.start || '';
  inputs.finish.value = params.finish || '';
  const tank = form.querySelector(`input[name="start_tank"][value="${params.start_tank === 'full' ? 'full' : 'empty'}"]`);
  if (tank) tank.checked = true;
  updateButton();
}

function paramsFromUrl() {
  const q = new URLSearchParams(window.location.search);
  const start = (q.get('start') || '').trim();
  const finish = (q.get('finish') || '').trim();
  if (!start || !finish) return null;
  return { start, finish, start_tank: q.get('start_tank') === 'full' ? 'full' : 'empty' };
}

function sameParams(a, b) {
  if (!a || !b) return !a && !b;
  return a.start === b.start && a.finish === b.finish && a.start_tank === b.start_tank;
}

function pageUrl(params) {
  const q = new URLSearchParams({ start: params.start, finish: params.finish, start_tank: params.start_tank });
  return `${window.location.pathname}?${q}${window.location.hash}`;
}

const COORDS = /^\s*-?\d+(\.\d+)?\s*[, ]\s*-?\d+(\.\d+)?\s*$/;
const STATE_AFTER_COMMA = /,\s*([A-Za-z]{2})\s*$/;

// A hint while typing; the API decides (the same rules, server side).
function courtesyHint(value) {
  if (!value) return '';
  if (COORDS.test(value)) return '';
  if (value.length < 3 || !/[a-z]/i.test(value)) return 'Use “City, ST” or “lat,lon”.';
  const codes = state.about?.api?.state_codes;
  const tail = value.match(STATE_AFTER_COMMA);
  if (tail && Array.isArray(codes) && !codes.includes(tail[1].toUpperCase())) {
    return `“${tail[1].toUpperCase()}” is not a US state: trips start and end in the USA.`;
  }
  return '';
}

// The button stays enabled (a disabled button does not say why); a submit with a
// missing field shows "Required." under it instead.
function updateButton() {
  planButton.setAttribute('aria-busy', String(document.body.classList.contains('is-loading')));
  for (const name of ['start', 'finish']) {
    const hint = $(`#${name}-hint`);
    const text = courtesyHint(inputs[name].value.trim());
    hint.textContent = text;
    hint.hidden = !text;
  }
}

// Results that no longer match the form (edited, swapped, other tank) are marked,
// not replanned: a new trip costs a routing call, so only "Plan route" sends one.
function markStale() {
  const stale = Boolean(state.params) && !sameParams(readForm(), state.params);
  staleNote.hidden = !stale;
  results.classList.toggle('is-stale', stale);
  profileCard.classList.toggle('is-stale', stale);
}

function clearFieldErrors() {
  for (const name of ['start', 'finish', 'start_tank']) {
    const node = $(`#${name}-error`);
    if (!node) continue;
    node.textContent = '';
    node.hidden = true;
  }
  for (const input of Object.values(inputs)) input.removeAttribute('aria-invalid');
  form.querySelector('.segmented')?.classList.remove('is-invalid');
}

function showFieldError(name, message) {
  const node = $(`#${name}-error`);
  if (!node) return false;
  node.textContent = message;
  node.hidden = false;
  const hint = $(`#${name}-hint`);
  if (hint) hint.hidden = true; // the server's message says it better
  if (inputs[name]) inputs[name].setAttribute('aria-invalid', 'true');
  if (name === 'start_tank') form.querySelector('.segmented')?.classList.add('is-invalid');
  return true;
}

// --- loading --------------------------------------------------------------------------------

let loadingFrame = null;
function setLoading(on, params) {
  document.body.classList.toggle('is-loading', on);
  results.setAttribute('aria-busy', String(on));
  cancelAnimationFrame(loadingFrame);
  if (on) {
    const started = performance.now();
    planLabel.hidden = true;
    planTimer.hidden = false;
    const tick = () => {
      planTimer.textContent = `Planning… ${fmt.ms(performance.now() - started)}`;
      loadingFrame = requestAnimationFrame(tick);
    };
    tick();
    live.textContent = `Planning ${params.start} → ${params.finish}`;
  } else {
    planLabel.hidden = false;
    planTimer.hidden = true;
  }
  updateButton();
}

// --- planning ---------------------------------------------------------------------------------

let controller = null;

function includeList() {
  return (state.about?.api?.include_values || []).includes('candidates') ? ['candidates'] : [];
}

async function planTrip(params, { history = 'push', origin = 'user', method = 'GET' } = {}) {
  if (controller) controller.abort('superseded');
  const current = new AbortController();
  controller = current;
  const timeout = setTimeout(() => current.abort('timeout'), CLIENT_TIMEOUT_MS);
  clearFieldErrors();
  setForm(params);
  if (history === 'push' && !sameParams(params, paramsFromUrl())) window.history.pushState({ params }, '', pageUrl(params));
  else if (history !== 'none') window.history.replaceState({ params }, '', pageUrl(params));
  setLoading(true, params);
  let result;
  try {
    result = method === 'POST'
      ? await client.planPost(params, { signal: current.signal, origin })
      : await client.plan(params, { include: includeList(), signal: current.signal, origin });
  } catch {
    return; // replaced by a newer request
  } finally {
    clearTimeout(timeout);
    if (controller === current) setLoading(false, params);
  }
  if (controller !== current) return;
  controller = null;
  state.route = result;
  state.params = params;
  markStale();
  if (result.ok && result.body) {
    state.checks = runChecks(result.body, state.about);
    renderPlan();
    const s = result.body.summary;
    document.title = `${result.body.start.label} → ${result.body.finish.label} · Fuel Route Planner`;
    live.textContent = `Planned ${result.body.start.label} → ${result.body.finish.label}: ${plural(s.number_of_stops, 'stop')}, ${fmt.money(s.total_fuel_cost)}.`;
  } else {
    state.checks = [];
    document.title = `${params.start} → ${params.finish} · Fuel Route Planner`;
    renderError(result);
  }
  renderAll({ api: true });
  refreshStats();
  // On a phone the form fills the screen: bring what was asked for into view.
  if ((origin === 'user' || origin === 'retry') && window.matchMedia('(max-width: 759.98px)').matches) {
    (result.ok ? results : errorBox).scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
  }
}

async function refreshStats() {
  const r = await client.stats();
  if (r && r.ok) {
    state.stats = r.body;
    renderAll();
  }
}

async function refreshAbout() {
  const r = await client.about();
  if (r && r.ok) {
    state.about = r.body;
    if (state.route?.ok) state.checks = runChecks(state.route.body, state.about);
    renderAll({ api: true });
    toast('Refreshed from /api/about');
  } else {
    toast('Could not refresh /api/about', 'bad');
  }
}

// --- rendering ---------------------------------------------------------------------------------

function context() {
  const r = state.route;
  const body = r && r.ok ? r.body : null;
  const clientCtx = r
    ? {
        server_ms: r.headers.responseTimeMs,
        round_trip_ms: r.client.roundTripMs,
        ttfb_ms: r.client.ttfbMs,
        transfer_bytes: r.client.transferBytes,
        encoded_bytes: r.client.encodedBytes,
        decoded_bytes: r.client.decodedBytes,
        parse_ms: r.client.parseMs,
      }
    : {};
  return { route: body, stats: state.stats, about: state.about, client: clientCtx, derived: derive(state, DERIVE_HELPERS) };
}

function selectStop(n, source) {
  if (source !== 'map') mapCtl.highlightStop(n);
  profile.highlightStop(n);
  plan.highlightRow(boxes.stops, n);
}

function renderPlan() {
  const body = state.route.body;
  hero.hidden = true;
  errorBox.hidden = true;
  results.hidden = false;
  profileCard.hidden = false;
  plan.renderWarnings(boxes.warnings, body);
  plan.renderStops(boxes.stops, body, state.about, { onStop: selectStop });
  plan.renderComparison(boxes.compare, body);
  plan.updateActionLinks(boxes.actions, state.params, routeBase);
  mapCtl.render(body, { about: state.about });
  profile.render(body, { about: state.about });
  const calls = body.meta?.external_api_calls ?? 0;
  const badge = $('[data-perf-badge]');
  badge.classList.toggle('is-zero', calls === 0);
  badge.classList.toggle('is-one', calls === 1);
  badge.classList.toggle('is-many', calls > 1);
}

const ERROR_TITLES = {
  invalid_request: 'Check the inputs',
  location_not_found: 'Place not found',
  location_outside_usa: 'Place outside the USA',
  same_location: 'Start and finish are the same place',
  no_fuel_data_in_region: 'No fuel price data in that region',
  location_not_near_road: 'Point too far from a road',
  no_route: 'No drivable route between these points',
  no_fuel_data_on_route: 'No priced station along this route',
  no_reachable_fuel_station: 'A stretch without stations is longer than the range',
  upstream_unavailable: 'The public routing server failed',
  upstream_busy: 'The routing server is busy',
  station_data_not_loaded: 'Station data not loaded',
  station_data_changed: 'Station data changed during the request',
  rate_limited: 'Too many requests',
  parse_error: 'Malformed JSON',
  not_found: 'Not found',
  method_not_allowed: 'Method not allowed',
  unsupported_media_type: 'Unsupported media type',
  network: 'Cannot reach the API',
  timeout: 'The API did not answer in time',
};

let retryTimer = null;

function retryControls(seconds) {
  const label = el('span', { class: 'muted' });
  const button = el('button', { type: 'button', class: 'btn', onclick: () => planTrip(state.params, { history: 'none', origin: 'retry' }) }, 'Retry');
  clearInterval(retryTimer);
  if (seconds > 0) {
    let left = seconds;
    button.disabled = true;
    const tick = () => {
      if (left <= 0) {
        button.disabled = false;
        label.textContent = 'You can retry now.';
        clearInterval(retryTimer);
        return;
      }
      label.textContent = `Retry in ${left} s`;
      left -= 1;
    };
    tick();
    retryTimer = setInterval(tick, 1000);
  }
  return el('p', { class: 'error-actions' }, button, ' ', label);
}

function renderError(result) {
  hero.hidden = true;
  results.hidden = true;
  profileCard.hidden = true;
  profile.clear();
  const body = result.body && typeof result.body === 'object' ? result.body : {};
  const code = result.failure || body.error || (result.status ? `http_${result.status}` : 'error');
  const meta = body.meta;
  const lines = result.failure === 'network'
    ? [`Cannot reach the API at ${window.location.origin}. Is the server running?`]
    : result.failure === 'timeout'
      ? [`No answer within ${fmt.int(CLIENT_TIMEOUT_MS / 1000)} s.`]
      : flattenErrors(body.detail ?? result.text);
  const extra = [];
  const maxRange = state.about?.vehicle?.max_range_miles;

  // Inline errors under the inputs.
  if (code === 'invalid_request' && body.detail && typeof body.detail === 'object' && !Array.isArray(body.detail)) {
    for (const [field, messages] of Object.entries(body.detail)) showFieldError(field, flattenErrors(messages).join(' '));
  }
  if (body.field) showFieldError(body.field, body.detail);
  if (code === 'same_location') {
    showFieldError('start', 'Same place as the finish.');
    showFieldError('finish', 'Same place as the start.');
  }

  if (code === 'no_reachable_fuel_station' && body.gap_end) {
    mapCtl.renderGap(body, { maxRange });
    extra.push(el('p', {}, `No station between mile ${fmt.dec1(body.from_mile)} and mile ${fmt.dec1(body.gap_end.mile)} (${fmt.miles(body.gap_miles)}), more than the ${fmt.miles_round(maxRange)} range. The map shows the stretch.`));
  } else {
    mapCtl.showUsa();
  }
  if (code === 'no_fuel_data_on_route' && present(body.route_distance_miles) && present(maxRange)
    && body.route_distance_miles <= maxRange && state.params?.start_tank !== 'full') {
    extra.push(el('p', {}, `This trip is ${fmt.miles(body.route_distance_miles)}, within the ${fmt.miles_round(maxRange)} range: a full tank needs no stop. `,
      el('button', { type: 'button', class: 'btn', onclick: () => planTrip({ ...state.params, start_tank: 'full' }) }, 'Try with a full tank')));
  }
  const retryAfter = Number(result.headers.retryAfter);
  if ([429, 503].includes(result.status) && result.headers.retryAfter) extra.push(retryControls(Number.isFinite(retryAfter) ? retryAfter : 0));
  else if (result.status === 502 || result.failure) extra.push(retryControls(0));

  const title = ERROR_TITLES[code] || 'Could not plan this route';
  live.textContent = `Could not plan: ${title}.`;
  replace(errorBox,
    el('div', { class: 'error-card', role: 'alert' },
      el('h2', {}, title),
      el('p', { class: 'error-code' }, result.status ? `HTTP ${result.status} · ` : '', el('code', {}, code)),
      lines.length ? el('ul', {}, lines.map((l) => el('li', {}, l))) : null,
      ...extra,
      meta
        ? el('p', { class: 'error-calls' }, el('span', { class: `badge ${meta.external_api_calls === 0 ? 'badge-ok' : 'badge-na'}` },
          `External calls spent: ${fmt.int(meta.external_api_calls)}`),
        meta.external_api_calls === 0 ? '' : ` (${(meta.external_api_services || []).join(', ')})`)
        : null));
  errorBox.hidden = false;
}

function renderEmpty() {
  hero.hidden = false;
  results.hidden = true;
  errorBox.hidden = true;
  profileCard.hidden = true;
  profile.clear();
  mapCtl.showUsa();
}

let pendingRender = null;
function renderAll({ api = false } = {}) {
  const ctx = context();
  bind(document, ctx);
  how.decorate(document, state.about);
  requirements.renderHeader(boxes.reqHead, ctx);
  requirements.renderTable(boxes.reqTable, { ...ctx, body: ctx.route, result: state.route, checks: state.checks, casesSummary: cases.summary(), onTry: tryCase });
  requirements.renderChecks(boxes.checks, state.checks);
  cases.render(boxes.cases);
  perf.renderRequest(boxes.perfRequest, state);
  bench.render();
  perf.renderSession(boxes.session, state, { onClear: () => clearSession() });
  perf.renderStats(boxes.stats, state.stats, { onRefresh: refreshStats });
  how.renderFunnel(boxes.funnel, ctx.route);
  how.renderStates(boxes.states, state.about);
  how.renderTimeline(boxes.timeline, state.about, boxes.historySummary);
  how.renderTestInventory(boxes.inventory, state.about);
  const checksBadge = $('[data-checks-badge]');
  checksBadge.classList.toggle('is-bad', (ctx.derived.checks_failed || 0) > 0);
  if (api) {
    apitab.renderRequest(boxes.apiRequest, state, { routeBase, toast, onPost: () => planTrip(state.params, { history: 'none', origin: 'user (POST)', method: 'POST' }) });
    apitab.renderResponse(boxes.apiResponse, state);
    apitab.renderReference(boxes.apiReference, state.about);
  }
  apitab.renderErrors(boxes.apiErrors, state.about, { onTry: tryCase, results: cases.results });
}

function scheduleRender() {
  if (pendingRender) return;
  pendingRender = requestAnimationFrame(() => {
    pendingRender = null;
    renderAll();
  });
}

async function tryCase(id) {
  const r = await cases.run(id);
  if (!r) return;
  const body = r.body && typeof r.body === 'object' ? r.body : {};
  const calls = body.meta?.external_api_calls;
  toast(`HTTP ${r.status}${body.error ? ` ${body.error}` : ''}${present(calls) ? ` · ${plural(calls, 'external call')}` : ''}`, r.ok || r.status < 500 ? 'ok' : 'bad');
}

// --- tabs ------------------------------------------------------------------------------------

const tablist = $('[role="tablist"]');

function selectTab(name, { focus = false, scroll = false, updateHash = true } = {}) {
  if (!TABS.includes(name)) return;
  const previous = TABS.find((t) => $(`#tab-${t}`).getAttribute('aria-selected') === 'true');
  for (const t of TABS) {
    const on = t === name;
    const tab = $(`#tab-${t}`);
    tab.setAttribute('aria-selected', String(on));
    tab.tabIndex = on ? 0 : -1;
    $(`#panel-${t}`).hidden = !on;
  }
  if (focus) $(`#tab-${name}`).focus();
  if (updateHash) window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}#${name}`);
  if (scroll) tablist.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
  if (name === 'performance' && previous !== 'performance') refreshStats();
  if (name !== 'plan') mapCtl.invalidate();
}

// On a narrow screen the tabs scroll sideways: a fade at the edge says there is more.
function setupTabsOverflow() {
  const update = () => {
    const more = tablist.scrollWidth - tablist.clientWidth - tablist.scrollLeft > 1;
    tablist.classList.toggle('has-more', more);
  };
  tablist.addEventListener('scroll', update, { passive: true });
  new ResizeObserver(update).observe(tablist);
  update();
}

tablist.addEventListener('click', (event) => {
  const tab = event.target.closest('[role="tab"]');
  if (tab) selectTab(tab.id.replace('tab-', ''));
});
tablist.addEventListener('keydown', (event) => {
  const index = TABS.findIndex((t) => $(`#tab-${t}`) === document.activeElement);
  if (index < 0) return;
  const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: TABS.length - 1 };
  if (!(event.key in moves)) return;
  event.preventDefault();
  selectTab(TABS[(moves[event.key] + TABS.length) % TABS.length], { focus: true });
});
window.addEventListener('hashchange', () => {
  const name = window.location.hash.slice(1);
  if (TABS.includes(name)) selectTab(name, { scroll: true, updateHash: false });
});

// --- events ------------------------------------------------------------------------------------

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const params = readForm();
  if (!params.start || !params.finish) {
    clearFieldErrors();
    if (!params.start) showFieldError('start', 'Required.');
    if (!params.finish) showFieldError('finish', 'Required.');
    return;
  }
  planTrip(params, { history: 'push' });
});
for (const input of Object.values(inputs)) {
  input.addEventListener('input', () => {
    input.removeAttribute('aria-invalid');
    const node = $(`#${input.id}-error`);
    if (node) node.hidden = true;
    updateButton();
    markStale();
  });
}
for (const radio of form.querySelectorAll('input[name="start_tank"]')) radio.addEventListener('change', markStale);
// Swap only swaps: the reversed trip is a new trip (a routing call), sent by "Plan route".
$('#swap').addEventListener('click', () => {
  const start = inputs.start.value;
  inputs.start.value = inputs.finish.value;
  inputs.finish.value = start;
  updateButton();
  markStale();
  if (state.params) live.textContent = 'Start and finish swapped. Press Plan route to plan the reversed trip.';
});
for (const chip of document.querySelectorAll('.chip[data-start]')) {
  chip.addEventListener('click', () => {
    planTrip({ start: chip.dataset.start, finish: chip.dataset.finish, start_tank: chip.dataset.tank === 'full' ? 'full' : 'empty' }, { history: 'push' });
  });
}
$('#send-again').addEventListener('click', () => {
  if (state.params) planTrip(state.params, { history: 'none', origin: 'cache demo' });
});
$('#other-tank').addEventListener('click', () => {
  if (!state.params) return;
  planTrip({ ...state.params, start_tank: state.params.start_tank === 'full' ? 'empty' : 'full' }, { history: 'push', origin: 'cache demo' });
});
$('#about-refresh').addEventListener('click', refreshAbout);
window.addEventListener('popstate', () => {
  const params = paramsFromUrl();
  if (sameParams(params, state.params)) return;
  if (!params) {
    state.route = null;
    state.params = null;
    state.checks = [];
    setForm({ start: '', finish: '', start_tank: 'empty' });
    renderEmpty();
    renderAll({ api: true });
    return;
  }
  planTrip(params, { history: 'none', origin: 'history' });
});

// --- start ---------------------------------------------------------------------------------------

// The map library comes from a CDN: if it did not load, the page still works as tables.
function noMap(container) {
  replace(container, el('p', { class: 'map-missing' }, 'The map library could not be loaded (no network to its CDN?). The plan, the profile and every table below still work.'));
  const nothing = () => {};
  return { render: nothing, renderGap: nothing, showUsa: nothing, highlightStop: nothing, reframe: nothing, invalidate: nothing };
}
const why = (stop, body) => plan.whyText(stop, body, state.about);
const mapCtl = window.L ? createMap($('#map'), { onStop: selectStop, why }) : noMap($('#map'));
const profile = createProfile($('#profile'), $('#profile-tip'), { onStop: selectStop, why });
let casesWereRunning = false;
const cases = createCases({
  routeBase,
  getAbout: () => state.about,
  onOpen: (params) => {
    selectTab('plan');
    planTrip(params, { history: 'push' });
  },
  onChange: () => {
    const running = cases.isRunning();
    if (casesWereRunning && !running) refreshStats();
    casesWereRunning = running;
    scheduleRender();
  },
});
const bench = perf.createBenchmark(boxes.bench, {
  getState: () => state,
  client,
  routeUrlFor: (params) => routeUrl(routeBase, params),
  onDone: refreshStats,
});
plan.setupActions(boxes.actions, { getState: () => state, routeBase, toast });
onLog(scheduleRender);

const hashTab = window.location.hash.slice(1);
selectTab(TABS.includes(hashTab) ? hashTab : 'plan', { updateHash: false });
setForm(config.initial || {});
setupTabsOverflow();
const initial = paramsFromUrl();
renderEmpty();
renderAll({ api: true });
document.documentElement.classList.add('js-ready');
if (initial) planTrip(initial, { history: 'replace', origin: 'page load' });
