// Step "Truck": the four settings a newcomer needs (miles per gallon, miles on a full
// tank, safety fuel, tank when leaving), three one-click tries, and "What changed"
// against the standard truck on the same trip.
//
// The road of a trip already planned is saved on the server, so a new truck is
// planned again with 0 requests to OSRM; the page shows the answer's own count.
// The settings without a control (corridor_miles, price_policy, consolidate) are
// kept when a link brings them, named on the page, and cleared by "Back to the
// standard truck".

import { el, fmt, hiddenSettingsText, mark, plural, present, replace } from './format.js';
import { CONTROLLED, HIDDEN, defaultsFrom, onlyChanged, rangeOf, tankGallons } from './settings.js';

const $ = (selector, root = document) => root.querySelector(selector);

// One change from the standard truck each. keepTank: the same tank, so a new miles per
// gallon also changes how far a full tank goes (tank × miles per gallon).
export const QUICK_TRIES = [
  { id: 'thirsty', set: { mpg: 8 }, keepTank: true },
  { id: 'safety', set: { safety_reserve_gal: 5 } },
  { id: 'full', set: { start_tank: 'full' } },
];

export function quickTryText(quickTry, defaults) {
  if (quickTry.id === 'thirsty') {
    return `A thirstier truck: ${fmt.num(quickTry.set.mpg)} miles per gallon, same ${fmt.num(tankGallons(defaults))}-gallon tank`;
  }
  if (quickTry.id === 'safety') return `Keep ${plural(quickTry.set.safety_reserve_gal, 'gallon')} of safety fuel`;
  return 'Start with a full tank';
}

// {start_tank, settings} of a quick try (settings: only what differs from the defaults).
export function quickTrySettings(quickTry, defaults) {
  const { start_tank: tank = 'empty', ...set } = quickTry.set;
  if (quickTry.keepTank) {
    const tank = tankGallons(defaults);
    if (present(tank) && present(set.mpg)) set.max_range_miles = Math.round(tank * set.mpg * 100) / 100;
  }
  return { start_tank: tank, settings: onlyChanged(set, defaults) };
}

function isFullAgainstEmpty(body, base) {
  return body?.vehicle?.start_tank === 'full' && base?.vehicle?.start_tank !== 'full';
}

function signedInt(diff) {
  return `${diff > 0 ? '+' : '−'}${fmt.int(Math.abs(diff))}`;
}

// The standard truck against yours, on the same trip (pure: tested in Node). With a
// full tank against paying for every mile the totals are not comparable dollar for
// dollar (the first tank is free): only the price per gallon is.
export function compareRows(body, base) {
  const s = body.summary;
  const b = base.summary;
  const notComparable = isFullAgainstEmpty(body, base);
  const money = s.total_fuel_cost - b.total_fuel_cost;
  const gallons = s.total_gallons_purchased - b.total_gallons_purchased;
  const stops = s.number_of_stops - b.number_of_stops;
  return {
    notComparable,
    rows: [
      {
        label: 'Fuel cost', standard: fmt.money(b.total_fuel_cost), yours: fmt.money(s.total_fuel_cost),
        diff: notComparable ? 'not comparable' : Math.abs(money) < 0.005 ? 'same' : fmt.money_signed(money), worse: !notComparable && money >= 0.005,
      },
      {
        label: 'Fuel stops', standard: fmt.int(b.number_of_stops), yours: fmt.int(s.number_of_stops),
        diff: stops === 0 ? 'same' : signedInt(stops), worse: stops > 0,
      },
      {
        label: 'Fuel bought', standard: fmt.gal(b.total_gallons_purchased), yours: fmt.gal(s.total_gallons_purchased),
        diff: notComparable ? 'not comparable' : Math.abs(gallons) < 0.005 ? 'same' : `${gallons > 0 ? '+' : '−'}${fmt.gal(Math.abs(gallons))}`,
        worse: !notComparable && gallons >= 0.005,
      },
    ],
    perGallon: notComparable && present(s.average_price_paid) && present(b.average_price_paid)
      ? `Price per gallon: ${fmt.price(s.average_price_paid)} (yours) vs ${fmt.price(b.average_price_paid)} (standard).`
      : null,
  };
}

// The same comparison in one sentence (said to screen readers after a new truck).
export function answerText(body, base) {
  const s = body.summary;
  const head = `Your truck: ${fmt.money(s.total_fuel_cost)}, ${plural(s.number_of_stops, 'stop')}`;
  if (!base) return `${head}.`;
  const { notComparable, rows, perGallon } = compareRows(body, base);
  if (notComparable) return `${head}. Not comparable dollar for dollar: with a full tank, the first tank is free. ${perGallon || ''}`.trim();
  const cost = rows[0].diff === 'same' ? 'the same cost as the standard truck' : `${rows[0].diff} against the standard truck`;
  return `${head}: ${cost}.`;
}

// "What changed": the table, the honest note for a free full tank, and the requests to OSRM.
export function renderCompare(container, body, baseline) {
  const meta = body.meta || {};
  const calls = meta.external_api_calls;
  const osrm = (meta.external_api_services || []).filter((name) => name === 'osrm').length;
  const callsLine = el('p', { class: 'compare-calls' }, calls === 0
    ? mark(true, 'No new request to OSRM.')
    : present(calls) ? mark(null, `${plural(osrm || calls, 'request')} to OSRM.`) : null);
  const parts = [el('h3', {}, 'What changed')];
  if (baseline?.status === 'ok') {
    const { notComparable, rows, perGallon } = compareRows(body, baseline.body);
    parts.push(el('table', { class: 'compare-table' },
      el('thead', {}, el('tr', {}, el('th', { scope: 'col' }, el('span', { class: 'sr-only' }, 'What')),
        el('th', { scope: 'col' }, 'Standard truck'), el('th', { scope: 'col' }, 'Your truck'), el('th', { scope: 'col' }, 'Difference'))),
      el('tbody', {}, rows.map((row) => el('tr', {},
        el('th', { scope: 'row' }, row.label),
        el('td', {}, row.standard),
        el('td', {}, el('strong', {}, row.yours)),
        el('td', { class: row.diff === 'same' || row.diff === 'not comparable' ? 'diff' : `diff ${row.worse ? 'is-worse' : 'is-better'}` }, row.diff))))));
    if (notComparable) parts.push(el('p', { class: 'small' }, 'Not comparable dollar for dollar: with a full tank, the first tank is free. ', perGallon));
  } else if (baseline?.status === 'loading') {
    parts.push(el('p', { class: 'muted small' }, 'Getting the standard truck’s plan to compare…'));
  } else if (baseline?.status === 'error') {
    parts.push(el('p', { class: 'muted small' }, 'The standard truck’s plan is not available to compare.'));
  }
  parts.push(callsLine);
  replace(container, ...parts);
}

export function createTruck({ root, getAbout, onChange, onApply }) {
  let defaults = defaultsFrom(getAbout());
  let hidden = {}; // settings a link brought that have no control here
  const number = (name) => $(`#set-${name}`, root);
  const slider = (name) => $(`#set-${name}-range`, root);

  function startTank() {
    return $('input[name="start_tank"]:checked', root)?.value === 'full' ? 'full' : 'empty';
  }

  // {start_tank, settings}: what the step asks for (settings = only the changes).
  function read() {
    const values = { ...hidden };
    for (const name of CONTROLLED) values[name] = number(name)?.value ?? '';
    return { start_tank: startTank(), settings: onlyChanged(values, defaults) };
  }

  // The API's message under a field belongs to the value it judged: editing hides it.
  function clearError(name) {
    const node = $(`#${name}-error`, root);
    if (node) {
      node.hidden = true;
      node.textContent = '';
    }
    number(name)?.removeAttribute('aria-invalid');
  }

  function refresh() {
    const current = {};
    for (const name of CONTROLLED) current[name] = number(name)?.value || defaults[name];
    for (const name of CONTROLLED) {
      const { min, max, step } = rangeOf(name, getAbout(), { ...defaults, ...current });
      for (const input of [number(name), slider(name)]) {
        if (!input) continue;
        input.min = String(min);
        if (present(max)) input.max = String(max);
        input.step = String(step);
      }
    }
    const tank = tankGallons(current);
    $('#set-tank', root).textContent = present(tank) ? fmt.gal(tank) : '—';
    const asked = read();
    for (const node of root.querySelectorAll('[data-setting]')) {
      const name = node.dataset.setting;
      node.classList.toggle('is-changed', name === 'start_tank' ? asked.start_tank === 'full' : name in asked.settings);
    }
    const words = hiddenSettingsText(hidden);
    const line = $('#hidden-settings', root);
    line.hidden = !words.length;
    line.textContent = words.length ? `This link also changes: ${words.join(', ')}. “Back to the standard truck” clears it.` : '';
  }

  // Put {start_tank, settings} in the controls (missing = the standard truck).
  function write({ start_tank: tank = 'empty', settings = {} } = {}) {
    const radio = $(`input[name="start_tank"][value="${tank === 'full' ? 'full' : 'empty'}"]`, root);
    if (radio) radio.checked = true;
    for (const name of CONTROLLED) {
      const value = name in settings ? settings[name] : defaults[name];
      for (const input of [number(name), slider(name)]) if (input) input.value = present(value) ? String(value) : '';
      clearError(name);
    }
    hidden = Object.fromEntries(HIDDEN.filter((name) => name in settings).map((name) => [name, settings[name]]));
    refresh();
  }

  function setAbout() {
    const before = read();
    defaults = defaultsFrom(getAbout());
    for (const node of root.querySelectorAll('[data-default]')) {
      const name = node.dataset.default;
      const value = defaults[name];
      if (name === 'mpg') node.textContent = fmt.num(value);
      else if (name === 'max_range_miles') node.textContent = fmt.miles_short(value);
      else node.textContent = value ? fmt.gal(value) : 'none';
    }
    write(before);
  }

  // The three tries, as buttons; onTry(quickTry) when one is clicked.
  function renderTries(container, onTry) {
    replace(container, QUICK_TRIES.map((quickTry) => el('button', {
      type: 'button', class: 'btn btn-try', dataset: { try: quickTry.id }, onclick: () => onTry(quickTry),
    }, quickTryText(quickTry, defaults))));
  }

  for (const name of CONTROLLED) {
    const box = number(name);
    const range = slider(name);
    box.addEventListener('input', () => {
      if (box.value !== '') range.value = box.value;
      clearError(name);
      refresh();
      onChange();
    });
    box.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        onApply();
      }
    });
    range.addEventListener('input', () => {
      box.value = range.value;
      clearError(name);
      refresh();
      onChange();
    });
  }
  for (const input of root.querySelectorAll('input[name="start_tank"]')) {
    input.addEventListener('change', () => {
      clearError('start_tank');
      refresh();
      onChange();
    });
  }
  $('#truck-apply', root).addEventListener('click', () => onApply());

  setAbout();
  return { read, write, refresh, setAbout, renderTries, defaults: () => defaults, clearError };
}
