// Entry point of the planner page: a guided tour in six steps (steps.js). The server
// only renders the shell; this module calls the public JSON API (the same
// GET /api/route that Postman uses), keeps the trip in the URL (?start&finish&
// start_tank and the truck settings; the step in the #hash) and fills every step
// from the answer, /api/about and the browser's own timing.

import { createClient, routeUrl, tripQuery } from './api.js';
import { bind, derive, el, flattenErrors, fmt, plural, present, replace } from './format.js';
import { createMap } from './map.js';
import * as plan from './plan.js';
import { createPlayer } from './player.js';
import { createCombobox } from './places.js';
import { SETTING_NAMES, sameSettings, settingsFromQuery } from './settings.js';
import { STEPS, createStepper } from './steps.js';
import { answerText, createTruck, quickTrySettings, renderCompare } from './whatif.js';

const CLIENT_TIMEOUT_MS = 40000;
const FIELD_NAMES = ['start', 'finish', 'start_tank', ...SETTING_NAMES];
const WIDE = '(min-width: 960px)';

const $ = (selector) => document.querySelector(selector);
const config = JSON.parse($('#page-config').textContent);
const client = createClient(config);

const state = {
  route: null, // the last answer with a plan: {ok, status, body, serverMs, ...}
  params: null, // {start, finish, start_tank, settings} of that answer
  error: null, // the last failed request: {result, card: 'trip' | 'truck', params, usableRange}
  about: config.about || {},
  baselines: new Map(), // trip -> the standard truck's plan of that trip, to compare
  sayCompare: false, // announce "What changed" when it is ready
};

const inputs = { start: $('#start'), finish: $('#finish') };
const panels = Object.fromEntries(STEPS.map((name) => [name, $(`#panel-${name}`)]));
const buttons = { trip: $('#plan-btn'), truck: $('#truck-apply') };
const boxes = {
  trip: $('#trip-error'), truck: $('#truck-error'), compare: $('#truck-compare'), pending: $('#truck-pending'),
  warnings: $('#route-warnings'), cards: $('#stop-cards'), checks: $('#cost-checks'), bars: $('#cost-bars'),
  json: $('#json-link'), yourTruck: $('#your-truck'), stale: $('#stale'), player: $('#player'),
};

function announce(text) {
  $('#live').textContent = text;
}

const isStandard = (params) => params.start_tank !== 'full' && !Object.keys(params.settings || {}).length;
const tripKey = (params) => `${params.start.trim().toLowerCase()}|${params.finish.trim().toLowerCase()}`;
const sameTruck = (a, b) => a.start_tank === b.start_tank && sameSettings(a.settings, b.settings);

// --- the form and the address -----------------------------------------------------------

function setForm(params, { places = true } = {}) {
  if (places) {
    inputs.start.value = params.start || '';
    inputs.finish.value = params.finish || '';
  }
  truck.write({ start_tank: params.start_tank, settings: params.settings || {} });
  updateHints();
}

function paramsFromUrl() {
  const q = new URLSearchParams(window.location.search);
  const start = (q.get('start') || '').trim();
  const finish = (q.get('finish') || '').trim();
  if (!start || !finish) return null;
  return { start, finish, start_tank: q.get('start_tank') === 'full' ? 'full' : 'empty', settings: settingsFromQuery(q, truck.defaults()) };
}

// The step a link asks for (a link without one opens the route).
function hashStep() {
  const name = window.location.hash.slice(1);
  return STEPS.includes(name) ? name : 'route';
}

const COORDS = /^\s*-?\d+(\.\d+)?\s*[, ]\s*-?\d+(\.\d+)?\s*$/;
const STATE_AFTER_COMMA = /,\s*([A-Za-z]{2})\s*$/;

// A hint while typing; the API decides (the same rules, server side).
function courtesyHint(value) {
  if (!value || COORDS.test(value)) return '';
  if (value.length < 3 || !/[a-z]/i.test(value)) return 'Use “City, ST”, like “Dallas, TX”.';
  const codes = state.about?.api?.state_codes;
  const tail = value.match(STATE_AFTER_COMMA);
  if (tail && Array.isArray(codes) && !codes.includes(tail[1].toUpperCase())) {
    return `“${tail[1].toUpperCase()}” is not a US state. Start and finish must be in the USA.`;
  }
  return '';
}

function updateHints() {
  for (const name of ['start', 'finish']) {
    const hint = $(`#${name}-hint`);
    hint.textContent = courtesyHint(inputs[name].value.trim());
    hint.hidden = !hint.textContent;
  }
}

function fieldInput(name) {
  return inputs[name] || $(`#set-${name}`);
}

function showFieldError(name, message) {
  const node = $(`#${name}-error`);
  if (!node) return false;
  node.textContent = message;
  node.hidden = false;
  if (inputs[name]) $(`#${name}-hint`).hidden = true; // the server's message says it better
  fieldInput(name)?.setAttribute('aria-invalid', 'true');
  return true;
}

function clearErrors() {
  for (const name of FIELD_NAMES) {
    const node = $(`#${name}-error`);
    if (!node) continue;
    node.textContent = '';
    node.hidden = true;
    fieldInput(name)?.removeAttribute('aria-invalid');
  }
  clearInterval(retryTimer);
  state.error = null;
  boxes.trip.hidden = true;
  boxes.truck.hidden = true;
}

// --- planning ------------------------------------------------------------------------------

let controller = null;
let loadingFrame = null;

function setLoading(card, on) {
  document.body.classList.toggle('is-loading', on);
  cancelAnimationFrame(loadingFrame);
  for (const [name, button] of Object.entries(buttons)) {
    const busy = on && name === card;
    button.setAttribute('aria-busy', String(busy));
    button.querySelector('.btn-label').hidden = busy;
    button.querySelector('[data-timer]').hidden = !busy;
  }
  if (!on) return;
  const timer = buttons[card].querySelector('[data-timer]');
  const started = performance.now();
  const tick = () => {
    timer.textContent = `Planning… ${fmt.dec1((performance.now() - started) / 1000)} s`;
    loadingFrame = requestAnimationFrame(tick);
  };
  tick();
}

// card: where the loading and an error show ('trip' or 'truck'); target: the step to
// show after a plan (from the Truck step the user stays there).
async function planTrip(params, { card = 'trip', target = null, history = 'push', focus = card !== 'truck' } = {}) {
  if (controller) controller.abort('superseded');
  const current = new AbortController();
  controller = current;
  const timeout = setTimeout(() => current.abort('timeout'), CLIENT_TIMEOUT_MS);
  params = { start: params.start, finish: params.finish, start_tank: params.start_tank === 'full' ? 'full' : 'empty', settings: params.settings || {} };
  clearErrors();
  setLoading(card, true);
  announce(`Planning ${params.start} → ${params.finish}…`);
  let result;
  try {
    result = await client.plan(params, { signal: current.signal });
  } catch {
    return; // replaced by a newer request
  } finally {
    clearTimeout(timeout);
    if (controller === current) setLoading(card, false);
  }
  if (controller !== current) return;
  controller = null;
  if (result.ok && result.body) {
    const step = target || (card === 'truck' ? 'truck' : 'route');
    state.route = result;
    state.params = params;
    const url = `${window.location.pathname}?${tripQuery(params)}#${step}`;
    if (history === 'push') window.history.pushState({ params }, '', url);
    else if (history === 'replace') window.history.replaceState({ params }, '', url);
    if (card !== 'truck') setForm(params);
    else truck.write(params);
    rememberBaseline(params, result.body);
    renderPlan();
    stepper.unlock();
    stepper.go(step, { focus });
    const s = result.body.summary;
    if (card === 'truck') state.sayCompare = true;
    else announce(`Planned ${result.body.start.label} → ${result.body.finish.label}: ${plural(s.number_of_stops, 'stop')}, ${fmt.money(s.total_fuel_cost)}.`);
  } else {
    state.error = { result, params, card: card === 'truck' && state.route ? 'truck' : 'trip' };
    renderError();
  }
  update();
  // From the Truck step, the answer ("What changed" or the error) shows under the buttons.
  if (card === 'truck') (state.error ? boxes.truck : boxes.compare).scrollIntoView({ block: 'nearest' });
}

// The standard truck's plan of the same trip, to compare a new truck with. It reuses the
// road the server just saved, so it costs no request to OSRM.
async function rememberBaseline(params, body) {
  const key = tripKey(params);
  if (isStandard(params)) {
    state.baselines.set(key, { status: 'ok', body });
    return;
  }
  const known = state.baselines.get(key);
  if (known && known.status !== 'error') return;
  state.baselines.set(key, { status: 'loading' });
  const r = await client.plan({ start: params.start, finish: params.finish, start_tank: 'empty', settings: {} });
  state.baselines.set(key, r.ok && r.body ? { status: 'ok', body: r.body } : { status: 'error' });
  renderTruck();
}

// --- drawing ---------------------------------------------------------------------------------

function context() {
  const r = state.route;
  return { route: r?.ok ? r.body : null, about: state.about, client: { server_ms: r?.serverMs ?? null }, derived: derive(state) };
}

function update() {
  bind(document, context());
  updateStrips();
  renderTruck();
}

function renderPlan() {
  const body = state.route.body;
  plan.renderWarnings(boxes.warnings, body);
  plan.renderStopCards(boxes.cards, body, state.about, { onSelect: (n) => selectStop(n, 'list') });
  plan.renderCost({ checks: boxes.checks, bars: boxes.bars }, body);
  boxes.json.setAttribute('href', routeUrl(config.api.route, state.params));
  player.load(body);
  document.title = `${body.start.label} → ${body.finish.label} · Fuel Route Planner`;
}

// A stop chosen in the list ('list'), on the map ('map') or reached by Play ('play').
function selectStop(n, source) {
  const wide = window.matchMedia(WIDE).matches;
  plan.highlightStop({ cards: boxes.cards, rules: panels.stops, body: state.route?.body }, n, { scroll: wide && source !== 'list' });
  if (source === 'list') mapCtl.focusStop(n);
  else if (source === 'map') mapCtl.selectStop(n);
}

// The strips over the steps: "Showing your truck", results of an older trip, and
// "Not planned yet" in the Truck step.
function updateStrips() {
  const later = Boolean(state.route) && stepper.current() !== 'trip';
  const d = derive(state);
  boxes.yourTruck.hidden = !(later && d.is_your_truck);
  const stale = Boolean(state.params) && (inputs.start.value.trim() !== state.params.start || inputs.finish.value.trim() !== state.params.finish);
  boxes.stale.hidden = !(later && stale);
  boxes.pending.hidden = !(state.params && !sameTruck(truck.read(), state.params));
}

function renderTruck() {
  const show = Boolean(state.route) && !isStandard(state.params);
  boxes.compare.hidden = !show;
  if (!show) return;
  const baseline = state.baselines.get(tripKey(state.params));
  renderCompare(boxes.compare, state.route.body, baseline);
  if (state.sayCompare && baseline?.status !== 'loading') {
    state.sayCompare = false;
    announce(answerText(state.route.body, baseline?.status === 'ok' ? baseline.body : null));
  }
}

// The map of each step: the USA, the road alone, or the road with the stops (or the
// stretch without a truck stop, next to the error that names it).
function showMapFor(step) {
  const error = state.error;
  const gap = Boolean(error?.usableRange) && error.card === step;
  document.body.classList.toggle('has-gap', gap);
  if (gap) {
    mapCtl.renderGap(error.result.body, { usableRange: error.usableRange });
    return;
  }
  const body = state.route?.body;
  if (step === 'trip' || !body) mapCtl.showUsa();
  else if (step === 'route') mapCtl.showRouteOnly(body);
  else mapCtl.showStops(body);
}

function onStep(step, previous) {
  if (previous === 'stops' && step !== 'stops') player.reset();
  if (step !== previous) mapCtl.map?.closePopup();
  boxes.player.hidden = !(step === 'stops' && state.route && window.L);
  showMapFor(step);
  updateStrips();
}

// --- errors ----------------------------------------------------------------------------------

const ERROR_TITLES = {
  invalid_request: 'Please check the places',
  location_not_found: 'We could not find that place',
  location_outside_usa: 'That place is outside the USA',
  same_location: 'Start and finish are the same place',
  no_fuel_data_in_region: 'No fuel prices there (Alaska, Hawaii and US territories are not in the price file)',
  location_not_near_road: 'That point is too far from a road',
  no_route: 'There is no road between these two places',
  no_fuel_data_on_route: 'No truck stop from the price file is near this road',
  no_reachable_fuel_station: 'Part of this road has no truck stop for too long',
  upstream_unavailable: 'The free routing service did not answer. Please try again.',
  station_data_not_loaded: 'Fuel prices are not loaded yet (run load_stations).',
  station_data_changed: 'The fuel prices were just reloaded. Please try again.',
  network: 'Cannot reach the server. Is it running?',
  timeout: 'The server took too long to answer.',
};
const WAIT_TITLES = {
  upstream_busy: (s) => `The free routing service is busy. Try again in ${s} s.`,
  rate_limited: (s) => `Too many trips in one minute. Try again in ${s} s.`,
};
const RETRY = new Set(['upstream_unavailable', 'upstream_busy', 'rate_limited', 'station_data_changed', 'network', 'timeout']);

let retryTimer = null;

function retryButton(seconds) {
  const button = el('button', { type: 'button', class: 'btn', onclick: () => planTrip(state.error.params, { card: state.error.card }) }, 'Retry');
  clearInterval(retryTimer);
  let left = Number.isFinite(seconds) ? Math.ceil(seconds) : 0;
  const tick = () => {
    button.disabled = left > 0;
    button.textContent = left > 0 ? `Retry in ${left} s` : 'Retry';
    if (left <= 0) clearInterval(retryTimer);
    left -= 1;
  };
  tick();
  if (left >= 0) retryTimer = setInterval(tick, 1000);
  return button;
}

function standardTruckButton() {
  return el('button', { type: 'button', class: 'btn', dataset: { action: 'standard-truck' } }, 'Back to the standard truck');
}

function renderError() {
  const { result, params, card } = state.error;
  const body = result.body && typeof result.body === 'object' ? result.body : {};
  const code = result.failure || body.error || (result.status ? `http_${result.status}` : 'error');
  const asked = { ...truck.defaults(), ...params.settings };
  const settingsAsked = !isStandard(params);

  // Messages under the fields the API named.
  const fields = [];
  if (code === 'invalid_request' && body.detail && typeof body.detail === 'object' && !Array.isArray(body.detail)) {
    for (const [field, messages] of Object.entries(body.detail)) if (showFieldError(field, flattenErrors(messages).join(' '))) fields.push(field);
  }
  if (body.field && showFieldError(body.field, String(body.detail))) fields.push(body.field);
  if (code === 'same_location') {
    showFieldError('start', 'Same place as the finish.');
    showFieldError('finish', 'Same place as the start.');
  }
  const truckFields = fields.length > 0 && fields.every((f) => !(f in inputs));

  let title = ERROR_TITLES[code] || 'Could not plan this trip';
  let lines = flattenErrors(body.detail);
  const wait = Number(result.retryAfter);
  if (WAIT_TITLES[code]) title = WAIT_TITLES[code](Number.isFinite(wait) ? wait : '—');
  if (truckFields) title = 'Please check the truck';
  if (result.status === 422 && settingsAsked) title = 'This truck cannot make the trip.';
  if (code === 'no_reachable_fuel_station' && body.gap_end) {
    // How far a tank goes: the range asked for, less the safety fuel it must keep.
    const usable = asked.max_range_miles - (Number(asked.safety_reserve_gal) || 0) * asked.mpg;
    state.error.usableRange = present(usable) ? usable : null;
    lines = [`No truck stop between mile ${fmt.dec1(body.from_mile)} and mile ${fmt.dec1(body.gap_end.mile)} (${fmt.miles(body.gap_miles)}). `
      + `A full tank only lasts ${fmt.miles_short(usable)}. The map shows that stretch.`];
  }
  let action = null;
  if (RETRY.has(code) || result.status === 502) action = retryButton(wait);
  else if ((truckFields || result.status === 422) && settingsAsked) action = standardTruckButton();

  const box = boxes[card];
  replace(box, el('div', { class: 'error-card', role: 'alert' },
    el('h3', {}, title),
    lines.map((line) => el('p', {}, line)),
    action ? el('p', { class: 'error-action' }, action) : null,
    el('p', { class: 'error-code' }, `Error code: ${code}${result.status ? ` (HTTP ${result.status})` : ''}`)));
  box.hidden = false;
  announce(`Could not plan: ${title}`);
  if (card === 'trip' && stepper.current() !== 'trip') stepper.go('trip', { focus: false });
  else showMapFor(stepper.current());
}

// --- actions --------------------------------------------------------------------------------

// The truck a new trip is planned with: the one of the plan on screen (changes not yet
// planned in the Truck step stay there), or the one of the link before any plan.
function currentTruck() {
  return state.params ? { start_tank: state.params.start_tank, settings: state.params.settings } : truck.read();
}

function submitTrip() {
  const start = inputs.start.value.trim();
  const finish = inputs.finish.value.trim();
  if (!start || !finish) {
    clearErrors();
    if (!start) showFieldError('start', 'Required.');
    if (!finish) showFieldError('finish', 'Required.');
    (start ? inputs.finish : inputs.start).focus();
    return;
  }
  planTrip({ start, finish, ...currentTruck() }, { card: 'trip' });
}

// "Plan again" (and Enter in a setting): the same trip with the truck of the step.
function applyTruck() {
  if (!state.params) return;
  planTrip({ start: state.params.start, finish: state.params.finish, ...truck.read() }, { card: 'truck' });
}

// "Back to the standard truck": every setting cleared (those of a link too), and the trip
// planned again (the one that failed, if the button sits in an error).
function standardTruck() {
  truck.write({});
  const trip = state.error?.params || state.params;
  if (!trip) return;
  const step = stepper.current();
  planTrip({ start: trip.start, finish: trip.finish }, { card: step === 'truck' ? 'truck' : 'trip', target: step === 'trip' ? 'route' : step });
}

function tryQuick(quickTry) {
  if (!state.params) return;
  const asked = quickTrySettings(quickTry, truck.defaults());
  truck.write(asked);
  planTrip({ start: state.params.start, finish: state.params.finish, ...asked }, { card: 'truck' });
}

// --- start ------------------------------------------------------------------------------------

// The map library comes from a CDN: if it did not load, the page still works without a map.
function noMap(container) {
  replace(container, el('p', { class: 'map-missing' }, 'The map could not be loaded (no network to its CDN?). Every step still works.'));
  const nothing = () => {};
  return {
    map: null, showRouteOnly: nothing, showStops: nothing, renderGap: nothing, showUsa: nothing, focusStop: nothing, selectStop: nothing,
    reframe: nothing, invalidate: nothing, markStop: nothing, clearMarks: nothing, stopLatLng: () => null,
  };
}

const mapCtl = window.L
  ? createMap($('#map'), { onStop: (n) => selectStop(n, 'map'), why: (stop, body) => plan.whyText(stop, body, state.about) })
  : noMap($('#map'));
const player = createPlayer(boxes.player, {
  getMap: () => mapCtl.map,
  markStop: (n, kind) => mapCtl.markStop(n, kind),
  clearMarks: () => mapCtl.clearMarks(),
  stopLatLng: (n) => mapCtl.stopLatLng(n),
  onStop: (n) => selectStop(n, 'play'),
  announce,
  reframe: () => {
    mapCtl.map?.closePopup();
    mapCtl.reframe();
  },
});
const truck = createTruck({ root: panels.truck, getAbout: () => state.about, onChange: updateStrips, onApply: applyTruck });
truck.renderTries($('#quick-tries'), tryQuick);
const stepper = createStepper({ nav: $('.stepper-bar'), panels, top: $('#main'), onChange: onStep, announce });
for (const name of ['start', 'finish']) {
  createCombobox(inputs[name], $(`#${name}-listbox`), {
    fetchPlaces: (q, options) => client.places(q, options),
    announce,
    onPick: () => {
      if (name === 'start' && !inputs.finish.value.trim()) inputs.finish.focus();
    },
  });
}

$('#trip-form').addEventListener('submit', (event) => {
  event.preventDefault();
  submitTrip();
});
for (const input of Object.values(inputs)) {
  input.addEventListener('input', () => {
    input.removeAttribute('aria-invalid');
    $(`#${input.id}-error`).hidden = true;
    updateHints();
    updateStrips();
  });
}
// Swap only swaps: the reversed trip is a new trip (a request to OSRM), sent by "Find fuel stops".
$('#swap').addEventListener('click', () => {
  const start = inputs.start.value;
  inputs.start.value = inputs.finish.value;
  inputs.finish.value = start;
  updateHints();
  updateStrips();
  announce('From and To swapped. Press Find fuel stops to plan the reversed trip.');
});
for (const chip of document.querySelectorAll('.chip[data-start]')) {
  chip.addEventListener('click', () => {
    planTrip({ start: chip.dataset.start, finish: chip.dataset.finish, ...currentTruck() }, { card: 'trip' });
  });
}
document.addEventListener('click', (event) => {
  const action = event.target.closest('[data-action]')?.dataset.action;
  if (action === 'standard-truck') standardTruck();
  else if (action === 'plan-another') {
    stepper.go('trip');
    inputs.start.focus();
    inputs.start.select();
  }
});
window.addEventListener('popstate', () => {
  const params = paramsFromUrl();
  if (!params || (state.params && tripQuery(params).toString() === tripQuery(state.params).toString())) return;
  setForm(params);
  planTrip(params, { history: 'none', target: hashStep() });
});

// The map column is as tall as the window below the header and the steps bar.
const measure = () => {
  document.documentElement.style.setProperty('--header-h', `${$('.top').offsetHeight}px`);
  document.documentElement.style.setProperty('--stepper-h', `${$('.stepper-bar').offsetHeight}px`);
};
new ResizeObserver(measure).observe(document.body);

const initial = paramsFromUrl();
setForm(initial || { ...config.initial, settings: settingsFromQuery(new URLSearchParams(window.location.search), truck.defaults()) });
update();
stepper.go('trip', { focus: false, updateHash: Boolean(window.location.hash) && !initial });
document.documentElement.classList.add('js-ready');
if (initial) planTrip(initial, { history: 'replace', target: hashStep(), focus: false });
