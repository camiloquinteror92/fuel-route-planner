// Entry point of the planner page: a guided tour in six steps (steps.js). The server
// only renders the shell; this module calls the public JSON API (the same
// GET /api/route that Postman uses), keeps the trip in the URL (?start&finish&
// start_tank and the truck settings; the step in the #hash) and fills every step
// from the answer, the standard truck's answer for the same trip, /api/about and the
// browser's own timing.

import { createClient, routeUrl, tripQuery } from './api.js';
import { createExplainer } from './explain-ui.js';
import { bind, derive, el, flattenErrors, fmt, plural, present, replace } from './format.js';
import { createMap } from './map.js';
import * as plan from './plan.js';
import { createPlayer } from './player.js';
import { createCombobox } from './places.js';
import { sameSettings, settingsFromQuery } from './settings.js';
import { STEPS, createStepper } from './steps.js';
import { answerText, createTruck, quickTrySettings, renderCompare } from './whatif.js';

const CLIENT_TIMEOUT_MS = 40000;
const FIELD_NAMES = ['start', 'finish', 'start_tank', 'mpg', 'tank_gal', 'safety_reserve_gal', 'consolidate'];
// The API judges the range; the page asks for the tank that gives it.
const FIELD_OF = { max_range_miles: 'tank_gal' };
const WIDE = '(min-width: 960px)';
const PULSE_MS = 1600;

const $ = (selector) => document.querySelector(selector);
const config = JSON.parse($('#page-config').textContent);
const client = createClient(config);
const STANDARD = { start_tank: 'empty', settings: {} };

const state = {
  route: null, // the last answer with a plan: {ok, status, body, serverMs, ...}
  params: null, // {start, finish, start_tank, settings} of that answer
  error: null, // the last failed request: {result, card: 'trip' | 'truck', params, usableRange}
  about: config.about || {},
  baselines: new Map(), // trip -> the standard truck's plan of that trip
  calls: new Map(), // trip -> requests to the routing service so far, for every truck tried
  nextTruck: null, // the truck a new trip uses, when it is not the one on screen ("Plan another trip")
  sayCompare: false, // announce "What changed" when it is ready
};

const inputs = { start: $('#start'), finish: $('#finish') };
const panels = Object.fromEntries(STEPS.map((name) => [name, $(`#panel-${name}`)]));
const buttons = { trip: $('#plan-btn'), truck: $('#truck-apply') };
const boxes = {
  trip: $('#trip-error'), truck: $('#truck-error'), compare: $('#truck-compare'), pending: $('#truck-pending'),
  warnings: $('#route-warnings'), cards: $('#stop-cards'), checks: $('#cost-checks'), bars: $('#cost-bars'),
  json: $('#json-link'), yourTruck: $('#your-truck'), yourTruckLead: $('#your-truck-lead'), stale: $('#stale'),
  player: $('#player'), playNow: $('#play-now'), mapCard: $('.map-card'),
};

function announce(text) {
  $('#live').textContent = text;
}

const isStandard = (params) => params.start_tank !== 'full' && !Object.keys(params.settings || {}).length;
const tripKey = (params) => `${params.start.trim().toLowerCase()}|${params.finish.trim().toLowerCase()}`;
const sameTruck = (a, b) => a.start_tank === b.start_tank && sameSettings(a.settings, b.settings);
const osrmCalls = (body) => (body?.meta?.external_api_services || []).filter((name) => name === 'osrm').length;

function countCalls(params, body) {
  const key = tripKey(params);
  state.calls.set(key, (state.calls.get(key) || 0) + osrmCalls(body));
}

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

// A hint while typing; the API decides (the same rules, server side).
function courtesyHint(value) {
  if (!value || COORDS.test(value)) return '';
  if (value.length < 3 || !/[a-z]/i.test(value)) return 'Use “City, ST”, like “Dallas, TX”.';
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

function showFieldError(apiName, message) {
  const name = FIELD_OF[apiName] || apiName;
  const node = $(`#${name}-error`);
  if (!node) return false;
  node.textContent = apiName === 'max_range_miles' ? `Range on a full tank (tank × miles per gallon): ${message}` : message;
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
  countCalls(params, result.body);
  if (result.ok && result.body) {
    const step = target || (card === 'truck' ? 'truck' : 'route');
    state.route = result;
    state.params = params;
    state.nextTruck = null;
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

// The standard truck's plan of the same trip: "What changed" compares with it and the
// "For Spotter" step shows it. It reuses the road the server just saved, so it costs
// no request to OSRM.
async function rememberBaseline(params, body) {
  const key = tripKey(params);
  if (isStandard(params)) {
    state.baselines.set(key, { status: 'ok', body });
    return;
  }
  const known = state.baselines.get(key);
  if (known && known.status !== 'error') return;
  state.baselines.set(key, { status: 'loading' });
  const r = await client.plan({ start: params.start, finish: params.finish, ...STANDARD });
  countCalls(params, r.body);
  state.baselines.set(key, r.ok && r.body ? { status: 'ok', body: r.body } : { status: 'error' });
  update();
}

// --- drawing ---------------------------------------------------------------------------------

function standardBody() {
  if (!state.params) return null;
  const known = state.baselines.get(tripKey(state.params));
  return known?.status === 'ok' ? known.body : null;
}

function context() {
  const r = state.route;
  const standard = standardBody();
  const tripCalls = state.params ? state.calls.get(tripKey(state.params)) ?? null : null;
  return {
    route: r?.ok ? r.body : null,
    standard,
    about: state.about,
    client: { server_ms: r?.serverMs ?? null },
    derived: derive({ ...state, standard, tripCalls }),
  };
}

function update() {
  bind(document, context());
  updateStrips();
  renderTruck();
  if (state.params) boxes.json.setAttribute('href', routeUrl(config.api.route, { start: state.params.start, finish: state.params.finish, ...STANDARD }));
  // Every value gets its "i" button (parts drawn again get theirs), and an open card
  // shows the new numbers.
  explainer.decorate(document);
  explainer.refresh();
}

function renderPlan() {
  const body = state.route.body;
  plan.renderWarnings(boxes.warnings, body);
  plan.renderStopCards(boxes.cards, body, state.about, { onSelect: (n) => selectStop(n, 'list') });
  plan.renderCost({ checks: boxes.checks, bars: boxes.bars }, body);
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

// The truck the next new trip is planned with: "Plan another trip" chooses the standard
// one; otherwise the one of the plan on screen (changes not yet planned in the Truck
// step stay there), or the one of the link before any plan.
function currentTruck() {
  if (state.nextTruck) return state.nextTruck;
  return state.params ? { start_tank: state.params.start_tank, settings: state.params.settings } : truck.read();
}

// The strips over the steps: "Showing your truck" (in the Trip step: "Your next trip
// uses your truck"), results of an older trip, and "Not planned yet" in the Truck step.
function updateStrips() {
  const step = stepper.current();
  const later = Boolean(state.route) && step !== 'trip';
  const d = derive(state);
  const nextChanged = Boolean(state.route) && !isStandard(currentTruck());
  const onTrip = step === 'trip';
  boxes.yourTruck.hidden = onTrip ? !nextChanged : !(later && d.is_your_truck && step !== 'assignment');
  boxes.yourTruckLead.textContent = onTrip ? 'Your next trip uses your truck' : 'Showing your truck';
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
  const body = state.route?.body;
  boxes.mapCard.classList.toggle('has-trip', !gap && step !== 'trip' && Boolean(body));
  boxes.mapCard.classList.toggle('has-stops', !gap && step !== 'trip' && step !== 'route' && Boolean(body?.fuel_stops?.length));
  if (gap) {
    mapCtl.renderGap(error.result.body, { usableRange: error.usableRange });
    return;
  }
  if (step === 'trip' || !body) mapCtl.showUsa();
  else if (step === 'route') mapCtl.showRouteOnly(body);
  else mapCtl.showStops(body);
}

function onStep(step, previous) {
  if (step !== previous) explainer.close();
  if (previous === 'stops' && step !== 'stops') player.reset();
  if (step !== previous) mapCtl.map?.closePopup();
  boxes.player.hidden = !(step === 'stops' && state.route && window.L);
  showMapFor(step);
  updateStrips();
}

// A locked step was asked for: point at what unlocks it.
function onLocked() {
  buttons.trip.classList.add('is-pulse');
  setTimeout(() => buttons.trip.classList.remove('is-pulse'), PULSE_MS);
  if (!inputs.start.value.trim()) inputs.start.focus();
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

// No truck stop near a road that a full tank covers: plan it leaving full (the road is
// already saved, so this costs no request to the routing service).
function fullTankButton(params, card) {
  return el('button', {
    type: 'button', class: 'btn',
    onclick: () => planTrip({ ...params, start_tank: 'full' }, { card }),
  }, 'Try leaving with a full tank');
}

function renderError() {
  const { result, params, card } = state.error;
  const body = result.body && typeof result.body === 'object' ? result.body : {};
  const code = result.failure || body.error || (result.status ? `http_${result.status}` : 'error');
  const asked = { ...truck.defaults(), ...params.settings };
  const settingsAsked = !isStandard(params);
  // How far a tank goes: the range asked for, less the safety fuel it must keep.
  const safety = Number(asked.safety_reserve_gal) || 0;
  const usable = asked.max_range_miles - safety * asked.mpg;

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
  let action = null;
  if (code === 'no_reachable_fuel_station' && body.gap_end) {
    state.error.usableRange = present(usable) ? usable : null;
    const tank = safety > 0 ? 'The fuel above the safety fuel lasts only' : 'A full tank lasts only';
    lines = [`No truck stop between mile ${fmt.dec1(body.from_mile)} and mile ${fmt.dec1(body.gap_end.mile)} (${fmt.miles(body.gap_miles)}). `
      + `${tank} ${fmt.miles_short(usable)}. The map shows that stretch.`];
  }
  if (code === 'no_fuel_data_on_route') {
    const corridor = asked.corridor_miles ?? state.about.vehicle?.corridor_miles;
    const distance = body.route_distance_miles;
    const ca = derive(state).ca_stations;
    lines = [`The price file has no truck stop within ${fmt.miles_short(corridor)} of this ${fmt.miles(distance)} road, `
      + 'so the fuel cannot be bought or priced.'];
    if (present(ca)) lines.push(`Some states have very few truck stops in the file: California has only ${fmt.int(ca)}, all in its south-east corner.`);
    if (params.start_tank !== 'full' && present(distance) && distance <= usable) {
      lines.push(`A full tank lasts ${fmt.miles_short(usable)}, so leaving with a full tank covers this trip with no stop.`);
      action = fullTankButton(params, card);
    }
  }
  if (!action && (RETRY.has(code) || result.status === 502)) action = retryButton(wait);
  else if (!action && (truckFields || result.status === 422) && settingsAsked) action = standardTruckButton();

  const box = boxes[card];
  // Only the words are the alert: the buttons and the "i" are not read out with it.
  replace(box, el('div', { class: 'error-card' },
    el('div', { role: 'alert' }, el('h3', {}, title), lines.map((line) => el('p', {}, line))),
    action ? el('p', { class: 'error-action' }, action) : null,
    el('p', { class: 'error-code' }, `Error code: ${code}${result.status ? ` (HTTP ${result.status})` : ''}`,
      el('span', { dataset: { explain: 'concept:trip-error-box', explainArg: code } }))));
  box.hidden = false;
  announce(`Could not plan: ${title}`);
  if (card === 'trip' && stepper.current() !== 'trip') stepper.go('trip', { focus: false });
  else showMapFor(stepper.current());
}

// --- actions --------------------------------------------------------------------------------

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

// "Back to the standard truck": every setting cleared (those of a link too). In the Trip
// step it only sets the truck of the next trip; elsewhere the trip on screen (or the one
// that failed) is planned again.
function standardTruck() {
  truck.write({});
  const step = stepper.current();
  if (step === 'trip' && state.route && !state.error) {
    state.nextTruck = { ...STANDARD };
    updateStrips();
    announce('The next trip uses the standard truck.');
    return;
  }
  const trip = state.error?.params || state.params;
  if (!trip) return;
  planTrip({ start: trip.start, finish: trip.finish }, { card: step === 'truck' ? 'truck' : 'trip', target: step === 'trip' ? 'route' : step });
}

// "Plan another trip": empty fields, the standard truck, the cursor in From.
function planAnother() {
  truck.write({});
  state.nextTruck = { ...STANDARD };
  inputs.start.value = '';
  inputs.finish.value = '';
  clearErrors();
  stepper.go('trip');
  updateHints();
  updateStrips();
  inputs.start.focus();
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
  mirror: boxes.playNow,
});
const truck = createTruck({ root: panels.truck, getAbout: () => state.about, onChange: updateStrips, onApply: applyTruck });
truck.renderTries($('#quick-tries'), tryQuick);
const explainer = createExplainer({ getContext: context, toggle: $('#explain-toggle'), announce });
const stepper = createStepper({ nav: $('.stepper-bar'), panels, top: $('#main'), status: $('#step-status'), onChange: onStep, onLocked, announce });
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
  else if (action === 'plan-another') planAnother();
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
