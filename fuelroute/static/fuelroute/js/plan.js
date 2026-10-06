// Plan tab and the business result: warnings, the stops table (with "Why" for
// each purchase and totals checked to the cent), the strategy comparison and the
// export actions (link, curl, JSON, GeoJSON, CSV, driver instructions, print).

import { el, fmt, mark, plural, replace, slug } from './format.js';
import { absolute, routeUrl } from './api.js';

const MIN_STOP_DEFAULT_KEY = 'min_stop_gallons';

function consolidationLimits(body, about) {
  const opt = body.pipeline?.optimizer || {};
  return {
    minStop: opt[MIN_STOP_DEFAULT_KEY] ?? about?.vehicle?.min_stop_gallons ?? null,
    maxCost: opt.max_consolidation_cost ?? about?.vehicle?.max_consolidation_cost ?? null,
  };
}

// "and": "a", "a and b", "a, b and c".
function joinAnd(items) {
  if (items.length < 2) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

function reachText(reaches) {
  if (!reaches) return null;
  if (reaches.stop === null || reaches.stop === undefined) return 'enough to finish';
  return `enough to reach stop ${reaches.stop} (mile ${fmt.dec1(reaches.mile)})`;
}

// What the greedy did at this station, before consolidation.
function greedyText(decision, body) {
  const amount = fmt.gal(decision.greedy_gallons);
  if (decision.rule === 'reach_cheaper') {
    return `The greedy bought ${amount} here, just enough to reach a cheaper station at mile ${fmt.dec1(decision.cheaper_station_mile)}.`;
  }
  if (decision.rule === 'fill_up') {
    return `The greedy filled the tank here (${amount}): no cheaper station within ${fmt.miles_round(body.vehicle.max_range_miles)}.`;
  }
  if (decision.rule === 'finish') return `The greedy bought ${amount} here to finish: the cheapest price left.`;
  return `The greedy bought ${amount} here (${decision.rule}).`;
}

// The reason of a stop of the FINAL plan, from the API's decision block: the greedy
// rule that created the stop, what consolidation moved in or out (and what that
// cost), and where the fuel bought here takes the truck in this plan.
export function whyText(stop, body, about) {
  const decision = stop.decision;
  if (!decision) return null;
  if (!decision.reaches) return legacyWhy(decision, body, about);
  const reach = reachText(decision.reaches);
  if (!decision.consolidated) {
    if (decision.rule === 'reach_cheaper') {
      const next = decision.reaches.stop !== null && Math.abs(decision.reaches.mile - decision.cheaper_station_mile) < 0.05;
      return next
        ? `Cheaper station at mile ${fmt.dec1(decision.cheaper_station_mile)} (stop ${decision.reaches.stop}): bought just enough to reach it.`
        : `Cheaper station at mile ${fmt.dec1(decision.cheaper_station_mile)}: bought just enough to reach it; in this plan that is ${reach}.`;
    }
    if (decision.rule === 'fill_up') {
      return `No cheaper station within ${fmt.miles_round(body.vehicle.max_range_miles)}: filled the tank, ${reach}.`;
    }
    if (decision.rule === 'finish') return 'Cheapest price left before the destination: bought just enough to finish.';
    return decision.rule;
  }
  const { minStop } = consolidationLimits(body, about);
  const minimum = minStop !== null ? fmt.gal(minStop) : 'the minimum';
  const moves = [];
  const gone = (decision.moved_in || []).filter((m) => m.from_stop === null || m.from_stop === undefined);
  const kept = (decision.moved_in || []).filter((m) => m.from_stop !== null && m.from_stop !== undefined);
  if (gone.length) {
    moves.push(`moved ${joinAnd(gone.map((m) => `the ${fmt.gal(m.gallons)} planned at mile ${fmt.dec1(m.mile)}`))} here (${gone.length > 1 ? 'those stops are' : 'that stop is'} gone)`);
  }
  for (const m of kept) moves.push(`moved ${fmt.gal(m.gallons)} planned at stop ${m.from_stop} (mile ${fmt.dec1(m.mile)}) here`);
  for (const m of decision.moved_out || []) moves.push(`moved ${fmt.gal(m.gallons)} of it to stop ${m.to_stop} (mile ${fmt.dec1(m.mile)})`);
  const extra = decision.consolidation_extra_cost;
  const cost = extra && Math.abs(extra) >= 0.005 ? `, for ${fmt.money_signed(extra)}` : '';
  const why = moves.length
    ? `To avoid a stop under ${minimum}, consolidation ${joinAnd(moves)}${cost}.`
    : `Consolidation adjusted it to avoid a stop under ${minimum}${cost}.`;
  return `${greedyText(decision, body)} ${why} It buys ${fmt.gal(stop.gallons)}: ${decision.fills_tank ? 'a full tank, ' : ''}${reach}.`;
}

// Answers of a server older than the decision's "reaches" block.
function legacyWhy(decision, body, about) {
  let text;
  if (decision.rule === 'reach_cheaper') {
    text = `Cheaper station at mile ${fmt.dec1(decision.cheaper_station_mile)}: bought just enough to reach it`;
  } else if (decision.rule === 'fill_up') {
    text = `No cheaper station within ${fmt.miles_round(body.vehicle.max_range_miles)}: filled the tank`;
  } else if (decision.rule === 'finish') {
    text = 'Cheapest price left before the destination: bought just enough to finish';
  } else {
    text = decision.rule;
  }
  if (decision.consolidated) {
    const { minStop } = consolidationLimits(body, about);
    text += `. Adjusted to avoid a stop under ${minStop !== null ? fmt.gal(minStop) : 'the minimum'}`;
  }
  return text;
}

export function renderWarnings(container, body) {
  replace(container, (body.warnings || []).map((w) =>
    el('div', { class: 'banner banner-warn', role: 'note' }, el('span', { class: 'banner-icon', 'aria-hidden': 'true' }), el('p', {}, w))));
}

const cents = (v) => Math.round(Number(v) * 100);

export function renderStops(container, body, about, { onStop } = {}) {
  const stops = body.fuel_stops || [];
  if (!stops.length) {
    replace(container, el('div', { class: 'empty-note' },
      el('p', {}, el('strong', {}, 'No fuel stop needed for this trip.')),
      body.summary.note ? el('p', { class: 'muted' }, body.summary.note) : null));
    return;
  }
  const policy = about?.planner?.price_policy;
  let prev = 0;
  const rows = stops.map((s) => {
    const leg = s.mile_marker - prev;
    prev = s.mile_marker;
    const quotes = s.price_quotes;
    const priceTitle = quotes
      ? quotes.count > 1
        ? `${quotes.count} quotes in the file: ${fmt.price_exact(quotes.min)}–${fmt.price_exact(quotes.max)}${policy ? `; using the ${policy}` : ''} (${fmt.price_exact(s.price_per_gallon)})`
        : `One quote in the file: ${fmt.price_exact(s.price_per_gallon)}`
      : null;
    const why = whyText(s, body, about);
    const tr = el('tr', { tabindex: 0, dataset: { stop: s.stop }, title: `Stop ${s.stop}: show it on the map and the profile` },
      el('td', { 'data-label': '#', class: 'num' }, el('span', { class: 'stop-dot' }, String(s.stop))),
      el('td', { 'data-label': 'Station', class: 'station' },
        el('strong', {}, s.name),
        el('small', {}, [s.address, `${s.city}, ${s.state}`].filter(Boolean).join(' · '))),
      el('td', { 'data-label': 'Mile', class: 'num' }, fmt.dec1(s.mile_marker)),
      el('td', { 'data-label': 'Leg', class: 'num' }, fmt.miles(leg)),
      el('td', { 'data-label': 'Off route', class: 'num' }, fmt.miles(s.distance_from_route_miles)),
      el('td', { 'data-label': '$/gal', class: 'num' },
        el('span', { title: priceTitle, class: 'has-tip' }, fmt.price(s.price_per_gallon)),
        quotes && quotes.count > 1 ? el('small', { class: 'quotes' }, `${quotes.count} quotes`) : null),
      el('td', { 'data-label': 'Arrive with', class: `num${s.fuel_on_arrival_gallons === 0 ? ' arrive-empty' : ''}`, title: s.fuel_on_arrival_gallons === 0 ? 'Arrives with an empty tank: the range is used literally, with no safety margin' : null }, fmt.gal(s.fuel_on_arrival_gallons)),
      el('td', { 'data-label': 'Buy', class: 'num' }, fmt.gal(s.gallons)),
      el('td', { 'data-label': 'Cost', class: 'num strong' }, fmt.money(s.cost)),
      el('td', { 'data-label': 'Why', class: 'why' }, why || '—'),
    );
    const activate = () => onStop?.(s.stop, 'table');
    tr.addEventListener('click', activate);
    tr.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        activate();
      }
    });
    return tr;
  });

  // Totals, in integer cents / hundredths of a gallon.
  const costSum = stops.reduce((t, s) => t + cents(s.cost), 0);
  const galSum = stops.reduce((t, s) => t + cents(s.gallons), 0);
  const costOk = costSum === cents(body.summary.total_fuel_cost);
  const galOk = galSum === cents(body.summary.total_gallons_purchased);
  const totals = el('tr', { class: 'totals' },
    el('td', { colspan: 7, 'data-label': 'Totals' },
      el('div', { class: costOk ? '' : 'is-bad' }, mark(costOk, costOk ? 'Σ stop costs = total, to the cent' : `Σ stop costs ${fmt.money(costSum / 100)} ≠ total ${fmt.money(body.summary.total_fuel_cost)}`)),
      el('div', { class: galOk ? '' : 'is-bad' }, mark(galOk, galOk ? 'Σ gallons = total gallons' : `Σ gallons ${fmt.gal(galSum / 100)} ≠ total ${fmt.gal(body.summary.total_gallons_purchased)}`))),
    el('td', { 'data-label': 'Buy', class: 'num strong' }, fmt.gal(body.summary.total_gallons_purchased)),
    el('td', { 'data-label': 'Cost', class: 'num strong' }, fmt.money(body.summary.total_fuel_cost)),
    el('td', {}, ''),
  );

  replace(container, el('div', { class: 'table-wrap' },
    el('table', { class: 'data cards stops-table' },
      el('caption', { class: 'sr-only' }, 'Fuel stops of this plan'),
      el('thead', {}, el('tr', {},
        ['#', 'Station', 'Mile', 'Leg', 'Off route', '$/gal', 'Arrive with', 'Buy', 'Cost', 'Why'].map((h, i) =>
          el('th', { scope: 'col', class: i === 1 || i === 9 ? null : 'num' }, h)))),
      el('tbody', {}, rows),
      el('tfoot', {}, totals))));
}

export function highlightRow(container, n) {
  for (const tr of container.querySelectorAll('tr[data-stop]')) tr.classList.toggle('is-active', tr.dataset.stop === String(n));
}

function strategyRow(label, rule, strategy, base, extra) {
  if (!strategy) {
    return el('tr', { class: 'muted' },
      el('th', { scope: 'row', 'data-label': 'Strategy' }, label),
      el('td', { 'data-label': 'Rule' }, rule || ''),
      el('td', { colspan: 4, 'data-label': 'Result' }, extra || 'not feasible on this route'));
  }
  const diff = base !== null ? strategy.total_fuel_cost - base : null;
  let vs = '—';
  if (diff !== null && Math.abs(diff) >= 0.005) {
    vs = diff > 0 ? `this plan saves ${fmt.money(diff)}${strategy.total_fuel_cost ? ` (${fmt.pct((diff / strategy.total_fuel_cost) * 100)})` : ''}` : `this plan costs ${fmt.money(-diff)} more`;
  } else if (diff !== null) vs = 'same cost';
  return el('tr', {},
    el('th', { scope: 'row', 'data-label': 'Strategy' }, label),
    el('td', { 'data-label': 'Rule', class: 'rule' }, rule || '', extra ? el('small', { class: 'extra' }, extra) : null),
    el('td', { 'data-label': 'Stops', class: 'num' }, strategy.number_of_stops ?? '—'),
    el('td', { 'data-label': 'Gallons', class: 'num' }, fmt.gal(strategy.total_gallons_purchased)),
    el('td', { 'data-label': 'Cost', class: 'num strong' }, fmt.money(strategy.total_fuel_cost)),
    el('td', { 'data-label': 'vs this plan', class: diff > 0 ? 'good' : diff < 0 ? 'bad' : null }, vs));
}

export function renderComparison(container, body) {
  const cmp = body.summary.comparison;
  if (!cmp) {
    replace(container, el('p', { class: 'muted' },
      body.summary.number_of_stops === 0 ? 'No purchase on this trip, so there is nothing to compare.' : 'This server does not send a strategy comparison.'));
    return;
  }
  const base = cmp.optimized?.total_fuel_cost ?? body.summary.total_fuel_cost;
  const before = cmp.optimum_before_consolidation;
  const opt = body.pipeline?.optimizer;
  let consolidationNote = null;
  if (before && cmp.optimized) {
    const extra = opt?.consolidation_extra_cost ?? cmp.optimized.total_fuel_cost - before.total_fuel_cost;
    const fewer = before.number_of_stops - cmp.optimized.number_of_stops;
    consolidationNote = fewer > 0 ? `Consolidation: ${fmt.money_signed(extra)} for ${plural(fewer, 'fewer stop', 'fewer stops')}` : 'Consolidation changed nothing on this trip';
  }
  const avg = cmp.corridor_average;
  const rows = [
    strategyRow(cmp.optimized?.label || 'This plan (optimized, consolidated)', cmp.optimized?.rule, cmp.optimized || {
      number_of_stops: body.summary.number_of_stops, total_gallons_purchased: body.summary.total_gallons_purchased, total_fuel_cost: body.summary.total_fuel_cost,
    }, base),
    strategyRow(before?.label || 'Pure optimum before consolidation', before?.rule, before, base, consolidationNote),
    strategyRow(cmp.price_blind?.label || 'Price-blind driver', cmp.price_blind?.rule, cmp.price_blind, base),
  ];
  if ('quarter_tank' in cmp) {
    rows.push(strategyRow(cmp.quarter_tank?.label || 'Quarter-tank driver', cmp.quarter_tank?.rule, cmp.quarter_tank, base,
      cmp.quarter_tank ? null : 'not available for this route'));
  }
  if (avg) {
    rows.push(strategyRow(avg.label || 'Corridor average price', avg.rule, { ...avg, number_of_stops: null }, base));
  }
  replace(container,
    el('div', { class: 'table-wrap' }, el('table', { class: 'data cards compare-table' },
      el('caption', { class: 'sr-only' }, 'Strategy comparison'),
      el('thead', {}, el('tr', {}, ['Strategy', 'Rule', 'Stops', 'Gallons', 'Cost', 'vs this plan'].map((h, i) =>
        el('th', { scope: 'col', class: i >= 2 && i <= 4 ? 'num' : null }, h)))),
      el('tbody', {}, rows))),
    el('p', { class: 'muted small' }, 'Every strategy uses the same route, the same stations and the same start and end fuel, so they buy the same gallons; only where and how much changes. Negative differences are shown as they are.'));
}

// --- exports -------------------------------------------------------------------------

function download(filename, type, text) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvCell(value) {
  const text = value === null || value === undefined ? '' : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function stopsCsv(body) {
  const header = ['stop', 'opis_id', 'name', 'address', 'city', 'state', 'mile_marker', 'distance_from_route_miles',
    'price_per_gallon', 'fuel_on_arrival_gallons', 'gallons', 'cost', 'decision'];
  const lines = [header.join(',')];
  for (const s of body.fuel_stops) {
    lines.push([s.stop, s.opis_id, s.name, s.address, s.city, s.state, s.mile_marker, s.distance_from_route_miles,
      s.price_per_gallon, s.fuel_on_arrival_gallons, s.gallons, s.cost, s.decision?.rule ?? ''].map(csvCell).join(','));
  }
  lines.push(['total', '', '', '', '', '', body.route.distance_miles, '', body.summary.average_price_paid ?? '', '',
    body.summary.total_gallons_purchased, body.summary.total_fuel_cost, ''].map(csvCell).join(','));
  return `${lines.join('\n')}\n`;
}

export function driverInstructions(body) {
  const lines = [`${body.start.label} → ${body.finish.label}: ${fmt.miles(body.route.distance_miles)}, start tank ${body.vehicle.start_tank}`];
  for (const s of body.fuel_stops) {
    lines.push(`Stop ${s.stop} · mile ${fmt.dec1(s.mile_marker)} · ${s.name}, ${[s.address, `${s.city}, ${s.state}`].filter(Boolean).join(', ')} · buy ${fmt.gal(s.gallons)} @ ${fmt.price(s.price_per_gallon)} = ${fmt.money(s.cost)}`);
  }
  if (!body.fuel_stops.length) lines.push('No fuel stop needed.');
  lines.push(`Total: ${fmt.gal(body.summary.total_gallons_purchased)}, ${fmt.money(body.summary.total_fuel_cost)}`);
  for (const w of body.warnings || []) lines.push(`Note: ${w}`);
  return lines.join('\n');
}

export function setupActions(root, { getState, routeBase, toast }) {
  const current = () => {
    const state = getState();
    return state.route?.ok ? { body: state.route.body, params: state.params } : null;
  };
  const copy = async (text, message) => {
    try {
      await navigator.clipboard.writeText(text);
      toast(message);
    } catch {
      toast('Copy failed: the browser blocked the clipboard.', 'bad');
    }
  };
  const handlers = {
    'copy-link': () => copy(window.location.href, 'Link copied'),
    'copy-curl': () => {
      const c = current();
      if (c) copy(`curl "${absolute(routeUrl(routeBase, c.params))}"`, 'curl command copied');
    },
    'download-geojson': () => {
      const c = current();
      if (c) download(`fuel-route-${slug(c.params.start)}-${slug(c.params.finish)}.geojson`, 'application/geo+json', JSON.stringify(c.body.map.geojson, null, 2));
    },
    'download-csv': () => {
      const c = current();
      if (c) download(`fuel-stops-${slug(c.params.start)}-${slug(c.params.finish)}.csv`, 'text/csv', stopsCsv(c.body));
    },
    'copy-instructions': () => {
      const c = current();
      if (c) copy(driverInstructions(c.body), 'Driver instructions copied');
    },
    print: () => window.print(),
  };
  root.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (button && handlers[button.dataset.action]) handlers[button.dataset.action]();
  });
}

export function updateActionLinks(root, params, routeBase) {
  const json = root.querySelector('[data-open-json]');
  if (json) json.setAttribute('href', routeUrl(routeBase, params));
}
