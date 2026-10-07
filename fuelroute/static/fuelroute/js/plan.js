// The plan, in plain words: the reason of every stop ("Why here"), the list of
// stops, the bill with its checks, and the comparison with a driver who ignores
// prices. Every number is read from the API answer.

import { el, fmt, joinAnd, mark, plural, replace } from './format.js';
import { cents, sumCents } from './checks.js';

// The three rules of the algorithm, by the name the API gives them (decision.rule).
export const RULES = { reach_cheaper: 'Cheaper ahead', finish: 'Finish', fill_up: 'Fill up' };
const TAGS = { reach_cheaper: 'tag-cheaper', finish: 'tag-finish', fill_up: 'tag-fill' };
const HALF_CENT = 0.005;
const SAME_MILE = 0.05; // mile markers have one decimal

export function ruleOf(stop) {
  return stop?.decision?.rule ?? null;
}

function usableRange(body) {
  return fmt.miles_short(body.vehicle?.usable_range_miles ?? body.vehicle?.max_range_miles);
}

function place(station) {
  return station?.city ? `${station.city}, ${station.state}` : `mile ${fmt.dec1(station.mile)}`;
}

// "stop 2, Toledo, OH" when the cheaper station is the next stop, else "Toledo, OH (mile 562.0)".
function cheaperPlace(d, body) {
  const c = d.cheaper_station;
  const r = d.reaches;
  const next = r && r.stop !== null && r.stop !== undefined && Math.abs(r.mile - c.mile) <= SAME_MILE
    ? (body.fuel_stops || [])[r.stop - 1] : null;
  return next ? `stop ${next.stop}, ${place(c)}` : `${place(c)} (mile ${fmt.dec1(c.mile)})`;
}

// Why the truck stops here, from the answer's decision block (pure: tested in Node).
// A stop of the three rules gets its rule in one or two sentences. A stop that "Skip
// tiny stops" changed gets what the rules alone do here, which stop it skipped (by
// name) or which fuel moved, and what the change cost.
export function whyText(stop, body, about) {
  const d = stop.decision;
  if (!d) return null;
  const range = usableRange(body);
  if (!d.consolidated) {
    if (d.rule === 'reach_cheaper' && d.cheaper_station) {
      return `Fuel is cheaper at ${cheaperPlace(d, body)}: ${fmt.price(d.cheaper_station.price_per_gallon)} vs `
        + `${fmt.price(stop.price_per_gallon)} here. Buy just enough to get there.`;
    }
    if (d.rule === 'fill_up') return `Nothing within ${range} is cheaper. Fill the tank.`;
    if (d.rule === 'finish') return 'No cheaper fuel before the finish. Buy just enough to finish.';
    return null;
  }

  const minStop = about?.vehicle?.min_stop_gallons ?? null;
  const tinyHere = minStop !== null && d.greedy_gallons < minStop - HALF_CENT;
  let alone = null;
  if (d.rule === 'reach_cheaper' && d.cheaper_station) {
    alone = `buy ${fmt.gal(d.greedy_gallons)} here, to reach cheaper fuel in ${place(d.cheaper_station)}`;
  } else if (d.rule === 'fill_up') {
    alone = `fill the tank here (${fmt.gal(d.greedy_gallons)}): nothing within ${range} is cheaper`;
  } else if (d.rule === 'finish') {
    alone = `buy ${fmt.gal(d.greedy_gallons)} here, just enough to finish`;
  }
  const parts = [];
  if (alone) parts.push(tinyHere ? `The three rules alone would ${alone}: a tiny stop.` : `The three rules alone ${alone}.`);

  const movedIn = d.moved_in || [];
  const gone = movedIn.filter((m) => m.from_stop === null || m.from_stop === undefined);
  const where = (m) => `${place(m)} (mile ${fmt.dec1(m.mile)})`;
  if (gone.length === 1) {
    parts.push(tinyHere
      ? `So it also buys here the ${fmt.gal(gone[0].gallons)} planned in ${where(gone[0])}, and that stop is skipped.`
      : `It also buys here the ${fmt.gal(gone[0].gallons)} the rules planned in ${where(gone[0])}, a tiny stop that is skipped.`);
  } else if (gone.length > 1) {
    const list = joinAnd(gone.map((m) => `${fmt.gal(m.gallons)} in ${where(m)}`));
    parts.push(`${tinyHere ? 'So it' : 'It'} also buys here the fuel the rules planned at other stops (${list}), and those stops are skipped.`);
  }
  for (const m of movedIn.filter((x) => !gone.includes(x))) {
    parts.push(`It also buys here ${fmt.gal(m.gallons)} that stop ${m.from_stop} would have bought, so no stop is tiny.`);
  }
  for (const m of d.moved_out || []) {
    const where = m.to_stop !== null && m.to_stop !== undefined ? `stop ${m.to_stop}` : `mile ${fmt.dec1(m.mile)}`;
    parts.push(`${fmt.gal(m.gallons)} of it is bought at ${where} instead, so no stop is tiny.`);
  }
  if (!movedIn.length && !(d.moved_out || []).length) {
    parts.push(`Adjusted so no stop buys less than ${minStop !== null ? fmt.gal(minStop) : 'the minimum'}.`);
  }
  const extra = d.consolidation_extra_cost;
  if (extra !== null && extra !== undefined && Math.abs(extra) >= HALF_CENT) parts.push(`Cost of the change: ${fmt.money_signed(extra)}.`);
  return parts.join(' ');
}

export function renderWarnings(container, body) {
  replace(container, (body.warnings || []).map((w) => el('li', {}, w)));
}

function tag(rule) {
  return el('span', { class: `tag ${TAGS[rule] || ''}` }, RULES[rule] || rule);
}

// The stops as cards: where (town and truck stop name), the price, what the truck
// arrives with and buys, the rule (and "Tiny stop skipped") and why. onSelect(n) when
// a card is chosen.
export function renderStopCards(container, body, about, { onSelect } = {}) {
  replace(container, (body.fuel_stops || []).map((s) => {
    const why = whyText(s, body, about);
    const button = el('button', { type: 'button', class: 'stop-btn', 'aria-describedby': `why-${s.stop}` },
      el('span', { class: 'stop-num', 'aria-hidden': 'true' }, String(s.stop)),
      el('span', { class: 'stop-main' },
        el('span', { class: 'stop-place' }, el('span', { class: 'sr-only' }, `Stop ${s.stop}: `),
          el('strong', {}, `${s.city}, ${s.state}`), ` · mile ${fmt.dec1(s.mile_marker)}`),
        el('span', { class: 'stop-name' }, s.name),
        el('span', { class: 'stop-tags' }, tag(ruleOf(s)), s.decision?.consolidated ? el('span', { class: 'tag tag-merged' }, 'Tiny stop skipped') : null),
        el('span', { class: 'stop-line' },
          `${fmt.price(s.price_per_gallon)}/gal · arrives with ${fmt.gal(s.fuel_on_arrival_gallons)} · buys ${fmt.gal(s.gallons)} · `,
          el('strong', {}, fmt.money(s.cost)))));
    button.addEventListener('click', () => onSelect?.(s.stop));
    return el('li', { class: 'stop-card', dataset: { stop: s.stop } },
      button,
      why ? el('p', { class: 'stop-why', id: `why-${s.stop}` }, el('strong', {}, 'Why here: '), why) : null);
  }));
}

// Light up a stop's card and the rule it follows (and the tiny-stop box if a merge
// changed it); n = null clears. scroll: bring the card into view (desktop only).
export function highlightStop({ cards, rules, body }, n, { scroll = false } = {}) {
  const stop = (body?.fuel_stops || []).find((s) => s.stop === n) || null;
  for (const card of cards.querySelectorAll('.stop-card')) {
    const on = stop !== null && card.dataset.stop === String(n);
    card.classList.toggle('is-current', on);
    card.querySelector('.stop-btn')?.setAttribute('aria-pressed', String(on));
    if (on && scroll) card.scrollIntoView({ block: 'nearest', behavior: 'auto' });
  }
  for (const rule of rules.querySelectorAll('[data-rule]')) {
    const name = rule.dataset.rule;
    rule.classList.toggle('is-active', Boolean(stop) && (name === ruleOf(stop) || (name === 'merged' && stop.decision?.consolidated === true)));
  }
}

// The bill's checks (in integer cents, never hidden when they fail) and the bars
// against a driver who ignores prices.
export function renderCost({ checks, bars }, body) {
  const s = body.summary;
  const stops = body.fuel_stops || [];
  const items = [];
  if (stops.length) {
    const sum = sumCents(stops.map((x) => x.cost));
    const total = cents(s.total_fuel_cost);
    if (sum === total) {
      items.push(mark(true, stops.length === 1
        ? 'The cost of the stop is this total, to the cent.'
        : `The ${fmt.int(stops.length)} stop costs add up to this total, to the cent.`));
    } else {
      items.push(mark(false, `The stop costs add up to ${fmt.money(sum / 100)}, not ${fmt.money(s.total_fuel_cost)}.`));
    }
  }
  const empty = body.vehicle?.start_tank !== 'full';
  const unpriced = Number(s.unpriced_fuel_gallons) > 0;
  if (empty && !unpriced && Math.abs(s.end_fuel_gallons - s.start_fuel_gallons) < HALF_CENT) {
    items.push(mark(true, `Every mile is paid for: the truck leaves with ${fmt.gal(s.start_fuel_gallons)} and arrives with the same `
      + `${fmt.gal(s.end_fuel_gallons)}, so the fuel it buys is the fuel the trip burns.`));
  }
  if (!empty) {
    items.push(mark(null, `The truck left with a full tank. Those ${fmt.gal(s.start_fuel_gallons)} were free and are not in the total.`));
  }
  if (unpriced) {
    items.push(mark(null, `${fmt.gal(s.unpriced_fuel_gallons)} were burned where the price file has no truck stop, so they have no price. See “Good to know” in the Route step.`));
  }
  replace(checks, items.map((item) => el('li', {}, item)));

  const blind = s.comparison?.price_blind;
  if (!blind || !stops.length) {
    replace(bars);
    return;
  }
  const most = Math.max(s.total_fuel_cost, blind.total_fuel_cost) || 1;
  const bar = (kind, label, cost, count) => el('div', { class: `bar bar-${kind}` },
    el('p', { class: 'bar-label' }, el('span', {}, label), el('span', { class: 'bar-value' }, `${fmt.money(cost)} · ${plural(count, 'stop')}`)),
    el('div', { class: 'bar-track' }, el('span', { class: 'bar-fill', css: { width: `${(cost / most) * 100}%` } })));
  replace(bars,
    bar('plan', 'This plan', s.total_fuel_cost, s.number_of_stops),
    bar('blind', 'Driver who ignores prices', blind.total_fuel_cost, blind.number_of_stops));
}
