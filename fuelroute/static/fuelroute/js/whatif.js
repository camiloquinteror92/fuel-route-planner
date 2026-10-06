// "What if…" panel: the plan's settings (start tank, fuel economy, range, corridor,
// safety reserve, price of stations quoted several times, consolidation of tiny
// stops), the effect of the current settings against the assignment defaults, and a
// list of interview questions that apply a setting and replan.
//
// A trip already planned keeps its route in the server's route cache, so a what-if
// is planned again with 0 external calls; the panel shows the meta that proves it.
// Every number it shows is read from the API answers (the plan and the default plan
// of the same trip) or computed from them.

import { el, fmt, mark, plural, replace, present } from './format.js';
import {
  PRICE_POLICIES, SETTINGS, SETTING_NAMES, START_TANK_MODES,
  changedNames, defaultsFrom, effectiveSettings, onlyChanged, rangeOf, tankGallons, tankMode,
} from './settings.js';

const $ = (selector, root = document) => root.querySelector(selector);

// Each question changes one thing from the assignment defaults, on the trip in the form.
export const QUESTIONS = [
  { id: 'mpg', set: { mpg: 8 }, text: (d, s) => `What if the truck does ${fmt.num(s.mpg)} mpg instead of ${fmt.num(d.mpg)}?` },
  { id: 'short_range', set: { max_range_miles: 300 }, text: (d, s) => `What if a full tank only lasts ${fmt.miles_round(s.max_range_miles)}?` },
  {
    id: 'reserve', set: { safety_reserve_gal: 5 },
    text: (d, s) => `What if the driver never wants to arrive anywhere with less than ${fmt.int(s.safety_reserve_gal)} gallons?`,
  },
  { id: 'full_tank', set: { start_tank: 'full' }, text: () => 'What if the truck starts with a full tank?' },
  { id: 'no_merge', set: { consolidate: false }, text: () => 'What if we keep every tiny stop instead of merging it?' },
  { id: 'wide', set: { corridor_miles: 25 }, text: (d, s) => `What if the driver accepts stations up to ${fmt.miles_round(s.corridor_miles)} off the route?` },
  { id: 'worst_price', set: { price_policy: 'max' }, text: () => 'What if every station charges the highest price it was quoted?' },
  { id: 'best_price', set: { price_policy: 'min' }, text: () => 'What if every station charges the lowest price it was quoted?' },
];

export function tripKey(params) {
  return `${String(params?.start || '').trim().toLowerCase()}|${String(params?.finish || '').trim().toLowerCase()}`;
}

export function isDefaultTrip(params) {
  return params.start_tank !== 'full' && !Object.keys(params.settings || {}).length;
}

// The question whose change is exactly this trip's (start tank + settings), if any.
export function questionOf(params) {
  const settings = params.settings || {};
  return QUESTIONS.find((q) => {
    const { start_tank: tank = 'empty', ...rest } = q.set;
    if ((params.start_tank === 'full' ? 'full' : 'empty') !== tank) return false;
    const keys = new Set([...Object.keys(rest), ...Object.keys(settings)]);
    return [...keys].every((k) => String(rest[k]) === String(settings[k]));
  }) || null;
}

const POLICY_LABEL = Object.fromEntries(PRICE_POLICIES.map((p) => [p.value, p.label]));

function defaultText(name, value) {
  if (!present(value)) return '—';
  if (name === 'mpg') return `${fmt.num(value)} mpg`;
  if (name === 'max_range_miles' || name === 'corridor_miles') return fmt.miles_round(value);
  if (name === 'safety_reserve_gal') return value ? fmt.gal(value) : 'none';
  if (name === 'price_policy') return POLICY_LABEL[value] || value;
  if (name === 'consolidate') return value ? 'on' : 'off';
  if (name === 'start_tank') return tankMode(value).label;
  return String(value);
}

// What {start_tank, settings} changes, in words ("full tank, … mpg, … range"); empty when nothing.
export function changeSummary({ start_tank: tank, settings = {} }) {
  const parts = [];
  if (tank === 'full') parts.push('full tank');
  for (const name of SETTING_NAMES) {
    if (!(name in settings)) continue;
    const value = settings[name];
    if (typeof value === 'string' && name !== 'price_policy') parts.push(`${name} ${value}`); // unreadable: the API will say why
    else if (name === 'mpg') parts.push(`${fmt.num(value)} mpg`);
    else if (name === 'max_range_miles') parts.push(`${fmt.miles_round(value)} range`);
    else if (name === 'corridor_miles') parts.push(`stations within ${fmt.miles_round(value)}`);
    else if (name === 'safety_reserve_gal') parts.push(`reserve ${fmt.gal(value)}`);
    else if (name === 'price_policy') parts.push(`${(POLICY_LABEL[value] || value).toLowerCase()} price`);
    else if (name === 'consolidate') parts.push(value === false ? 'tiny stops kept' : 'tiny stops merged');
  }
  return parts.join(', ');
}

// One chip per effective setting of a plan, the changed ones marked.
export function renderSettingsLine(container, body, params, about) {
  if (!body) {
    replace(container);
    return;
  }
  const defaults = defaultsFrom(about);
  const eff = effectiveSettings(body, defaults);
  const changed = new Set(changedNames(body, params));
  const chip = (name, text) => el('li', {
    class: `chip-setting${changed.has(name) ? ' is-changed' : ''}`,
    title: changed.has(name) ? `Changed: the assignment default is ${defaultText(name, defaults[name])}` : 'Assignment default',
  }, text);
  replace(container,
    el('span', { class: 'settings-title' }, changed.size ? 'What if:' : 'Settings:'),
    el('ul', { class: 'settings-chips' },
      chip('start_tank', body.vehicle?.start_tank_label || tankMode(eff.start_tank).label),
      chip('mpg', `${fmt.num(eff.mpg)} mpg`),
      chip('max_range_miles', `${fmt.miles_round(eff.max_range_miles)} range`),
      present(eff.tank_gallons) ? el('li', { class: 'chip-setting is-derived', title: 'range ÷ mpg' }, `${fmt.gal(eff.tank_gallons)} tank`) : null,
      chip('corridor_miles', `stations within ${fmt.miles_round(eff.corridor_miles)}`),
      chip('price_policy', `${(POLICY_LABEL[eff.price_policy] || eff.price_policy || '—').toLowerCase()} price`),
      chip('consolidate', eff.consolidate === false ? 'tiny stops kept' : 'tiny stops merged'),
      chip('safety_reserve_gal', eff.safety_reserve_gal ? `reserve ${fmt.gal(eff.safety_reserve_gal)}` : 'no reserve')));
}

function delta(value, base, format, { goodWhenLower = true } = {}) {
  if (!present(value) || !present(base)) return null;
  const diff = value - base;
  if (Math.abs(diff) < 0.005) return el('span', { class: 'delta' }, 'same');
  const better = goodWhenLower ? diff < 0 : diff > 0;
  const share = base ? Math.abs(diff / base) * 100 : 0;
  const pct = share >= 0.05 ? ` (${diff > 0 ? '+' : '−'}${fmt.pct(share)})` : '';
  return el('span', { class: `delta ${better ? 'is-better' : 'is-worse'}` }, `${diff > 0 ? '+' : '−'}${format(Math.abs(diff))}${pct}`);
}

function plainDelta(value, base) {
  if (!present(value) || !present(base)) return null;
  const diff = value - base;
  if (!diff) return el('span', { class: 'delta' }, 'same');
  return el('span', { class: `delta ${diff < 0 ? 'is-better' : 'is-worse'}` }, `${diff > 0 ? '+' : '−'}${fmt.int(Math.abs(diff))}`);
}

// A short answer line: cost, stops and the change against the defaults.
export function answerText(body, base) {
  const s = body.summary;
  const parts = [`${fmt.money(s.total_fuel_cost)}, ${plural(s.number_of_stops, 'stop')}`];
  if (base) {
    const diff = s.total_fuel_cost - base.summary.total_fuel_cost;
    parts.push(Math.abs(diff) < 0.005 ? 'same cost as the defaults' : `${fmt.money_signed(diff)} vs the defaults`);
  }
  return parts.join(' · ');
}

export function createWhatIf({ root, effectRoot, questionsRoot, getAbout, onChange, onApply, onReset, onAsk }) {
  let defaults = defaultsFrom(getAbout());
  const numbers = SETTINGS.filter((s) => s.kind === 'number').map((s) => s.name);
  const number = (name) => $(`#set-${name}`, root);
  const slider = (name) => $(`#set-${name}-range`, root);

  function values() {
    const out = {};
    for (const name of numbers) out[name] = number(name)?.value ?? '';
    out.price_policy = $('input[name="price_policy"]:checked', root)?.value ?? defaults.price_policy;
    const box = $('#set-consolidate', root);
    out.consolidate = box ? box.checked : defaults.consolidate;
    return out;
  }

  function startTank() {
    return $('input[name="start_tank"]:checked', root)?.value === 'full' ? 'full' : 'empty';
  }

  // {start_tank, settings}: what the panel asks for (settings = only the changes).
  function read() {
    return { start_tank: startTank(), settings: onlyChanged(values(), defaults) };
  }

  function refresh() {
    const current = values();
    // Range limits, the reserve's depending on the tank.
    for (const name of numbers) {
      const { min, max, step } = rangeOf(name, getAbout(), { ...defaults, ...current });
      for (const input of [number(name), slider(name)]) {
        if (!input) continue;
        input.min = String(min);
        if (present(max)) input.max = String(max);
        input.step = String(step);
      }
    }
    const tank = tankGallons({ mpg: current.mpg || defaults.mpg, max_range_miles: current.max_range_miles || defaults.max_range_miles });
    const tankNode = $('#set-tank', root);
    if (tankNode) tankNode.textContent = present(tank) ? fmt.gal(tank) : '—';
    const changed = read();
    for (const node of root.querySelectorAll('[data-setting]')) {
      const name = node.dataset.setting;
      const on = name === 'start_tank' ? changed.start_tank === 'full' : name in changed.settings;
      node.classList.toggle('is-changed', on);
    }
    const count = Object.keys(changed.settings).length + (changed.start_tank === 'full' ? 1 : 0);
    const counter = $('#whatif-count', root);
    if (counter) counter.textContent = count ? `${plural(count, 'change')} from the assignment defaults` : 'Assignment defaults';
    $('#whatif-reset', root)?.toggleAttribute('disabled', count === 0);
  }

  // Put {start_tank, settings} in the controls (missing = the default).
  function write({ start_tank: tank = 'empty', settings = {} } = {}) {
    const radio = $(`input[name="start_tank"][value="${tank === 'full' ? 'full' : 'empty'}"]`, root);
    if (radio) radio.checked = true;
    for (const name of numbers) {
      const value = name in settings ? settings[name] : defaults[name];
      for (const input of [number(name), slider(name)]) if (input) input.value = present(value) ? String(value) : '';
    }
    const policy = settings.price_policy ?? defaults.price_policy;
    const policyRadio = $(`input[name="price_policy"][value="${policy}"]`, root);
    if (policyRadio) policyRadio.checked = true;
    const box = $('#set-consolidate', root);
    if (box) box.checked = (settings.consolidate ?? defaults.consolidate) !== false;
    refresh();
  }

  function setAbout() {
    const before = read();
    defaults = defaultsFrom(getAbout());
    for (const node of root.querySelectorAll('[data-default]')) node.textContent = defaultText(node.dataset.default, defaults[node.dataset.default]);
    write(before);
  }

  // --- events -------------------------------------------------------------------------
  for (const name of numbers) {
    const box = number(name);
    const range = slider(name);
    box?.addEventListener('input', () => {
      if (range && box.value !== '') range.value = box.value;
      refresh();
      onChange();
    });
    box?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        onApply();
      }
    });
    range?.addEventListener('input', () => {
      if (box) box.value = range.value;
      refresh();
      onChange();
    });
  }
  for (const input of root.querySelectorAll('input[name="start_tank"], input[name="price_policy"], #set-consolidate')) {
    input.addEventListener('change', () => {
      refresh();
      onChange();
    });
  }
  $('#whatif-apply', root)?.addEventListener('click', () => onApply());
  $('#whatif-reset', root)?.addEventListener('click', () => {
    write({});
    onReset();
  });

  // --- effect of the current settings ---------------------------------------------------
  function renderEffect(state, baseline) {
    const r = state.route;
    if (!r || !state.params) {
      replace(effectRoot, el('p', { class: 'muted small' }, 'Plan a trip, then change a setting or click a question: the effect against the assignment defaults shows here.'));
      return;
    }
    if (!r.ok) {
      replace(effectRoot, el('p', { class: 'effect-none' }, mark(false, 'No plan with these settings:'), ' ', r.body?.error ? el('code', {}, r.body.error) : `HTTP ${r.status || '—'}`,
        el('span', { class: 'muted small' }, ' (the message is above the map).')));
      return;
    }
    const body = r.body;
    const meta = body.meta || {};
    const calls = meta.external_api_calls;
    const callsLine = el('p', { class: 'effect-calls' },
      mark(calls === 0 ? true : null, present(calls) ? plural(calls, 'external call') : 'external calls not reported'),
      ' ', el('span', { class: 'muted small' },
        meta.plan_cache === 'hit' ? 'plan cache hit'
          : meta.route_cache === 'hit' ? 'route cache hit: planned again on the cached route'
            : meta.route_cache === 'miss' ? 'new trip: one routing call' : ''));
    if (isDefaultTrip(state.params)) {
      replace(effectRoot, el('p', { class: 'small' }, el('strong', {}, 'Assignment defaults. '),
        el('span', { class: 'muted' }, 'Change a setting and replan, or click a question below: the change in cost and stops shows here.')), callsLine);
      return;
    }
    const base = baseline?.status === 'ok' ? baseline.body : null;
    const s = body.summary;
    const b = base?.summary;
    const row = (label, value, d, baseText) => el('div', { class: 'effect-row' },
      el('dt', {}, label), el('dd', {}, el('strong', {}, value), ' ', d, baseText ? el('small', { class: 'muted' }, ` defaults ${baseText}`) : null));
    const changed = changedNames(body, state.params);
    replace(effectRoot,
      el('dl', { class: 'effect' },
        row('Total fuel cost', fmt.money(s.total_fuel_cost), delta(s.total_fuel_cost, b?.total_fuel_cost, fmt.money), b ? fmt.money(b.total_fuel_cost) : null),
        row('Fuel stops', fmt.int(s.number_of_stops), plainDelta(s.number_of_stops, b?.number_of_stops), b ? fmt.int(b.number_of_stops) : null),
        row('Gallons bought', fmt.gal(s.total_gallons_purchased), delta(s.total_gallons_purchased, b?.total_gallons_purchased, fmt.gal), b ? fmt.gal(b.total_gallons_purchased) : null)),
      baseline?.status === 'loading' ? el('p', { class: 'muted small' }, 'Asking the API for the default plan of this trip to compare…') : null,
      baseline?.status === 'error' ? el('p', { class: 'muted small' }, 'The default plan of this trip is not available: ', el('code', {}, baseline.error || 'error'), '.') : null,
      changed.length ? el('p', { class: 'small' }, 'Changed: ', changed.map((name, i) => [i ? ', ' : '', el('code', {}, name)])) : null,
      callsLine);
  }

  // --- interview questions ------------------------------------------------------------
  // answerFor(question) -> the answer text on the current trip, or null.
  function renderQuestions(state, answerFor) {
    const current = state.route?.ok && state.params ? questionOf(state.params) : null;
    const busy = document.body.classList.contains('is-loading');
    replace(questionsRoot, QUESTIONS.map((q) => {
      const values = { ...defaults, ...q.set };
      const answer = answerFor(q);
      return el('li', { class: `question${current === q ? ' is-current' : ''}` },
        el('button', { type: 'button', class: 'question-btn', disabled: busy, onclick: () => onAsk(q) }, q.text(defaults, values)),
        answer ? el('p', { class: 'question-answer' }, el('span', { class: 'arrow', 'aria-hidden': 'true' }, '→ '), answer) : null);
    }));
  }

  setAbout();
  return { read, write, refresh, setAbout, renderEffect, renderQuestions, defaults: () => defaults, names: SETTING_NAMES, modes: START_TANK_MODES };
}
