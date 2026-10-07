// Step "Truck": the settings a newcomer needs (miles per gallon, tank size, safety
// fuel, tank when leaving, skip tiny stops), four one-click tries on the standard
// truck, and "What changed" against the standard truck on the same trip, with why.
//
// People think in miles per gallon and tank size; the API takes the range. The Truck
// step shows the tank and the range it gives (tank x miles per gallon) and sends
// max_range_miles, so a thirstier truck keeps its tank and goes less far.
//
// The road of a trip already planned is saved on the server, so a new truck is
// planned again with 0 requests to OSRM; the page shows the answer's own count.
// The settings without a control (corridor_miles, price_policy) are kept when a link
// brings them, named on the page, and cleared by "Back to the standard truck".

import { el, fmt, hiddenSettingsText, mark, plural, present, replace } from './format.js';
import { HIDDEN, defaultsFrom, onlyChanged, rangeOf, rangeOfTank, tankGallons, tankLimits } from './settings.js';

const $ = (selector, root = document) => root.querySelector(selector);
const NUMBERS = ['mpg', 'tank_gal', 'safety_reserve_gal'];

// One change from the standard truck each. keepTank: the same tank, so a new miles per
// gallon also changes how far a full tank goes (tank × miles per gallon).
export const QUICK_TRIES = [
  { id: 'thirsty', set: { mpg: 8 }, keepTank: true },
  { id: 'safety', set: { safety_reserve_gal: 5 } },
  { id: 'full', set: { start_tank: 'full' } },
  { id: 'merge', set: { consolidate: true } },
];

export function quickTryText(quickTry, defaults) {
  if (quickTry.id === 'thirsty') {
    return `A thirstier truck: ${fmt.num(quickTry.set.mpg)} miles per gallon, same ${fmt.num(tankGallons(defaults))}-gallon tank`;
  }
  if (quickTry.id === 'safety') return `Keep ${plural(quickTry.set.safety_reserve_gal, 'gallon')} of safety fuel`;
  if (quickTry.id === 'merge') return 'Skip tiny stops';
  return 'Leave with a full tank';
}

// {start_tank, settings} of a quick try (settings: only what differs from the defaults).
export function quickTrySettings(quickTry, defaults) {
  const { start_tank: tank = 'empty', ...set } = quickTry.set;
  if (quickTry.keepTank) {
    const gallons = tankGallons(defaults);
    if (present(gallons) && present(set.mpg)) set.max_range_miles = rangeOfTank(gallons, set.mpg);
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

// Why it changed, one sentence per setting that differs (pure: tested in Node). Each
// sentence is true for this trip: it is chosen from the two answers' own numbers.
export function whyChanged(body, base) {
  const v = body.vehicle;
  const w = base.vehicle;
  const s = body.summary;
  const b = base.summary;
  const money = s.total_fuel_cost - b.total_fuel_cost;
  const gallons = s.total_gallons_purchased - b.total_gallons_purchased;
  const stops = s.number_of_stops - b.number_of_stops;
  const out = [];
  if (v.start_tank === 'full' && w.start_tank !== 'full') {
    out.push(`The first tank (${fmt.gal(s.start_fuel_gallons)}) is free, so the bill counts only the fuel bought on the way.`);
  }
  if (v.miles_per_gallon !== w.miles_per_gallon) {
    const word = v.miles_per_gallon < w.miles_per_gallon ? 'worse' : 'better';
    out.push(`Same miles, ${word} mileage: the trip burns ${fmt.gal(s.fuel_used_gallons)} instead of ${fmt.gal(b.fuel_used_gallons)}.`);
  }
  if (Math.abs(v.tank_gallons - w.tank_gallons) >= 0.005) {
    out.push(v.tank_gallons > w.tank_gallons
      ? `A bigger tank (${fmt.gal(v.tank_gallons)}) lasts ${fmt.miles_short(v.max_range_miles)}, so it can carry cheap fuel further.`
      : `A smaller tank (${fmt.gal(v.tank_gallons)}) lasts only ${fmt.miles_short(v.max_range_miles)}, so the truck cannot always wait for the cheapest fuel.`);
  } else if (v.max_range_miles !== w.max_range_miles) {
    out.push(`Same ${fmt.gal(v.tank_gallons)} tank, but it now lasts ${fmt.miles_short(v.max_range_miles)} instead of ${fmt.miles_short(w.max_range_miles)}.`);
  }
  if (Number(v.safety_reserve_gal) !== Number(w.safety_reserve_gal) && Number(v.safety_reserve_gal) > 0) {
    out.push(`Keeping ${fmt.gal(v.safety_reserve_gal)} unused shortens each tank to ${fmt.miles_short(v.usable_range_miles)}`
      + (money >= 0.005 ? ', so some fuel must be bought at pricier truck stops.' : '.'));
  }
  if (v.consolidate === true && w.consolidate !== true) {
    const only = (body.meta?.settings_changed || []).join() === 'consolidate';
    const cost = only && money >= 0.005 ? `, for ${fmt.money(money)} more` : '';
    out.push(stops < 0
      ? `Skipping tiny stops: ${plural(-stops, 'fewer stop')}${cost}.`
      : `Skipping tiny stops: some fuel moved from a tiny stop to a nearby one${cost}.`);
  }
  if (!isFullAgainstEmpty(body, base) && Math.abs(gallons) < 0.005 && Math.abs(money) >= 0.005) {
    out.push('Both trucks buy the same fuel; only where they buy it changes.');
  }
  if (Math.abs(money) < 0.005 && stops === 0 && Math.abs(gallons) < 0.005) {
    out.push('On this trip it changed nothing.');
  }
  return out;
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

// "What changed": the table, why, the honest note for a free full tank, and the requests to OSRM.
export function renderCompare(container, body, baseline) {
  const meta = body.meta || {};
  const calls = meta.external_api_calls;
  const osrm = (meta.external_api_services || []).filter((name) => name === 'osrm').length;
  const callsLine = el('p', { class: 'compare-calls' }, calls === 0
    ? mark(true, 'No new request to the routing service.')
    : present(calls) ? mark(null, `${plural(osrm || calls, 'request')} to the routing service.`) : null);
  const parts = [el('h3', {}, 'What changed, against the standard truck')];
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
    const why = whyChanged(body, baseline.body);
    if (why.length) parts.push(el('h4', { class: 'compare-why-title' }, 'Why'), el('ul', { class: 'compare-why' }, why.map((line) => el('li', {}, line))));
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
  const merge = () => $('#set-consolidate', root);
  const defaultTank = () => tankGallons(defaults);

  function startTank() {
    return $('input[name="start_tank"]:checked', root)?.value === 'full' ? 'full' : 'empty';
  }

  // The values typed, with an empty box meaning the default.
  function typed() {
    const value = (name, fallback) => {
      const text = number(name)?.value ?? '';
      return text === '' ? fallback : text;
    };
    return {
      mpg: value('mpg', defaults.mpg),
      tank: value('tank_gal', defaultTank()),
      safety: value('safety_reserve_gal', defaults.safety_reserve_gal),
    };
  }

  // {start_tank, settings}: what the step asks for (settings = only the changes).
  function read() {
    const { mpg, tank, safety } = typed();
    const range = rangeOfTank(tank, mpg);
    const values = {
      ...hidden,
      mpg,
      // Unreadable: sent as typed, so the server's 400 names it.
      max_range_miles: range ?? tank,
      safety_reserve_gal: safety,
      consolidate: merge()?.checked === true,
    };
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

  function setLimits(name, { min, max, step }) {
    for (const input of [number(name), slider(name)]) {
      if (!input) continue;
      input.min = String(min);
      if (present(max)) input.max = String(max);
      else input.removeAttribute('max');
      input.step = String(step);
    }
  }

  function refresh() {
    const about = getAbout();
    const { mpg, tank } = typed();
    const range = rangeOfTank(tank, mpg);
    setLimits('mpg', rangeOf('mpg', about));
    setLimits('tank_gal', tankLimits(about, mpg));
    setLimits('safety_reserve_gal', rangeOf('safety_reserve_gal', about, { mpg, max_range_miles: range }));
    $('#set-range', root).textContent = present(range) ? fmt.miles_short(range) : '—';
    const asked = read();
    const changed = {
      mpg: 'mpg' in asked.settings,
      tank_gal: present(defaultTank()) && Math.abs(Number(tank) - defaultTank()) >= 0.005,
      safety_reserve_gal: 'safety_reserve_gal' in asked.settings,
      start_tank: asked.start_tank === 'full',
      consolidate: 'consolidate' in asked.settings,
    };
    for (const node of root.querySelectorAll('[data-setting]')) node.classList.toggle('is-changed', Boolean(changed[node.dataset.setting]));
    const words = hiddenSettingsText(hidden);
    const line = $('#hidden-settings', root);
    line.hidden = !words.length;
    line.textContent = words.length ? `This link also changes: ${words.join(', ')}. “Back to the standard truck” clears it.` : '';
  }

  // Put {start_tank, settings} in the controls (missing = the standard truck).
  function write({ start_tank: tank = 'empty', settings = {} } = {}) {
    const radio = $(`input[name="start_tank"][value="${tank === 'full' ? 'full' : 'empty'}"]`, root);
    if (radio) radio.checked = true;
    const mpg = 'mpg' in settings ? settings.mpg : defaults.mpg;
    const range = 'max_range_miles' in settings ? settings.max_range_miles : defaults.max_range_miles;
    const gallons = tankGallons({ mpg, max_range_miles: range });
    const values = {
      mpg,
      tank_gal: present(gallons) ? Math.round(gallons * 100) / 100 : '',
      safety_reserve_gal: 'safety_reserve_gal' in settings ? settings.safety_reserve_gal : defaults.safety_reserve_gal,
    };
    for (const name of NUMBERS) {
      for (const input of [number(name), slider(name)]) if (input) input.value = present(values[name]) ? String(values[name]) : '';
      clearError(name);
    }
    if (merge()) merge().checked = settings.consolidate === true || settings.consolidate === 'true';
    clearError('consolidate');
    hidden = Object.fromEntries(HIDDEN.filter((name) => name in settings).map((name) => [name, settings[name]]));
    refresh();
  }

  function setAbout() {
    const before = read();
    defaults = defaultsFrom(getAbout());
    for (const node of root.querySelectorAll('[data-default]')) {
      const name = node.dataset.default;
      if (name === 'mpg') node.textContent = fmt.num(defaults.mpg);
      else if (name === 'tank_gal') node.textContent = present(defaultTank()) ? `${fmt.num(defaultTank())} gal` : '—';
      else node.textContent = defaults[name] ? fmt.gal(defaults[name]) : 'none';
    }
    write(before);
  }

  // The tries, as buttons; onTry(quickTry) when one is clicked.
  function renderTries(container, onTry) {
    replace(container, QUICK_TRIES.map((quickTry) => el('button', {
      type: 'button', class: 'btn btn-try', dataset: { try: quickTry.id }, onclick: () => onTry(quickTry),
    }, quickTryText(quickTry, defaults))));
  }

  const changed = (name) => {
    clearError(name);
    refresh();
    onChange();
  };
  for (const name of NUMBERS) {
    const box = number(name);
    const range = slider(name);
    box.addEventListener('input', () => {
      if (box.value !== '') range.value = box.value;
      changed(name);
    });
    box.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        onApply();
      }
    });
    range.addEventListener('input', () => {
      box.value = range.value;
      changed(name);
    });
  }
  merge()?.addEventListener('change', () => changed('consolidate'));
  for (const input of root.querySelectorAll('input[name="start_tank"]')) input.addEventListener('change', () => changed('start_tank'));
  $('#truck-apply', root).addEventListener('click', () => onApply());

  setAbout();
  return { read, write, refresh, setAbout, renderTries, defaults: () => defaults, clearError };
}
