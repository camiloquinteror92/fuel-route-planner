// Fuel profile: one SVG, two bands sharing the mile axis.
//   top:    price of every station within the corridor (candidates layer), the
//           plan's stops, the price-blind driver's stops and the corridor average;
//   bottom: fuel in the tank, recomputed in the browser from the stops (solid:
//           this plan, dashed: the price-blind driver), with the tank capacity.
// A crosshair (mouse and touch) reads both bands; clicking a stop syncs the map
// and the stops table. Points where the tank audit fails are marked in red.

import { el, fmt, svg, clear } from './format.js';
import { fuelAt, planTrace, priceBlindTrace } from './checks.js';

const W = 1000;
const H = 320;
const LEFT = 64;
const RIGHT = 16;
const PRICE_TOP = 26;
const PRICE_BOTTOM = 132;
const TANK_TOP = 168;
const TANK_BOTTOM = 284;
const AXIS_Y = 300;

function niceStep(span, target) {
  const raw = span / target;
  const power = 10 ** Math.floor(Math.log10(raw));
  const unit = raw / power;
  const nice = unit < 1.5 ? 1 : unit < 3.5 ? 2 : unit < 7.5 ? 5 : 10;
  return nice * power;
}

function rowsOf(candidates) {
  if (!candidates || !Array.isArray(candidates.rows)) return [];
  const f = candidates.fields;
  return candidates.rows.map((row) => Object.fromEntries(f.map((name, i) => [name, row[i]])));
}

export function createProfile(svgNode, tipNode, { onStop } = {}) {
  let current = null;

  function render(body, { about } = {}) {
    clear(svgNode);
    const distance = body.route.distance_miles;
    const capacity = body.vehicle.tank_gallons;
    const stops = body.fuel_stops || [];
    const comparison = body.summary.comparison || null;
    const blindStops = comparison?.price_blind?.stops || [];
    const candidates = rowsOf(body.candidates);
    const trace = planTrace(body);
    const blindTrace = priceBlindTrace(body);
    const corridorMiles = body.pipeline?.corridor?.corridor_miles ?? about?.planner?.corridor_miles;

    const x = (mile) => LEFT + (Math.max(0, Math.min(distance, mile)) / (distance || 1)) * (W - LEFT - RIGHT);
    const prices = [...candidates.map((c) => c.price_per_gallon), ...stops.map((s) => s.price_per_gallon), ...blindStops.map((s) => s.price_per_gallon)];
    const hasPrices = prices.length > 0;
    const pMin = hasPrices ? Math.min(...prices) : 0;
    const pMax = hasPrices ? Math.max(...prices) : 1;
    const pad = (pMax - pMin) * 0.08 || 0.05;
    const yPrice = (p) => PRICE_BOTTOM - ((p - (pMin - pad)) / (pMax + pad - (pMin - pad))) * (PRICE_BOTTOM - PRICE_TOP);
    const yTank = (g) => TANK_BOTTOM - (g / (capacity || 1)) * (TANK_BOTTOM - TANK_TOP);

    const root = svg('g', {});
    svgNode.append(root);

    // Axes and grid.
    const step = niceStep(distance || 1, 6);
    for (let mile = 0; mile <= distance + 1e-9; mile += step) {
      root.append(svg('line', { x1: x(mile), x2: x(mile), y1: PRICE_TOP, y2: TANK_BOTTOM, class: 'pf-grid' }));
      root.append(svg('text', { x: x(mile), y: AXIS_Y + 12, class: 'pf-tick', 'text-anchor': 'middle' }, fmt.int(mile)));
    }
    root.append(svg('text', { x: W - RIGHT, y: AXIS_Y + 12, class: 'pf-tick pf-tick-end', 'text-anchor': 'end' }, `${fmt.int(distance)} mi`));
    root.append(svg('line', { x1: LEFT, x2: W - RIGHT, y1: TANK_BOTTOM, y2: TANK_BOTTOM, class: 'pf-axis' }));

    // Price band.
    root.append(svg('text', { x: LEFT, y: PRICE_TOP - 10, class: 'pf-band-title' },
      hasPrices ? `Price at stations within ${fmt.miles(corridorMiles)} ($/gal)` : 'Price of the plan stops ($/gal)'));
    if (hasPrices) {
      for (const p of [pMin, pMax]) {
        root.append(svg('text', { x: LEFT - 6, y: yPrice(p) + 4, class: 'pf-tick', 'text-anchor': 'end' }, fmt.price(p)));
        root.append(svg('line', { x1: LEFT, x2: W - RIGHT, y1: yPrice(p), y2: yPrice(p), class: 'pf-grid pf-grid-h' }));
      }
      for (const c of candidates) {
        root.append(svg('circle', { cx: x(c.mile_marker), cy: yPrice(c.price_per_gallon), r: 2.4, class: 'pf-cand' }));
      }
      const avg = comparison?.corridor_average?.price_per_gallon;
      if (avg) {
        root.append(svg('line', { x1: LEFT, x2: W - RIGHT, y1: yPrice(avg), y2: yPrice(avg), class: 'pf-avg' }));
        root.append(svg('text', { x: W - RIGHT, y: yPrice(avg) - 4, class: 'pf-avg-label', 'text-anchor': 'end' }, `corridor average ${fmt.price(avg)}`));
      }
      for (const s of blindStops) {
        const cx = x(s.mile_marker);
        const cy = yPrice(s.price_per_gallon);
        root.append(svg('rect', { x: cx - 5, y: cy - 5, width: 10, height: 10, class: 'pf-pb' }));
      }
    }

    // Tank band.
    root.append(svg('text', { x: LEFT, y: TANK_TOP - 10, class: 'pf-band-title' }, 'Fuel in tank (gal)'));
    for (const g of [0, capacity]) {
      root.append(svg('text', { x: LEFT - 6, y: yTank(g) + 4, class: 'pf-tick', 'text-anchor': 'end' }, fmt.int(g)));
    }
    root.append(svg('line', { x1: LEFT, x2: W - RIGHT, y1: yTank(capacity), y2: yTank(capacity), class: 'pf-cap' }));
    root.append(svg('text', { x: W - RIGHT, y: yTank(capacity) - 4, class: 'pf-cap-label', 'text-anchor': 'end' }, `capacity ${fmt.gal(capacity)}`));
    const fraction = about?.planner?.quarter_tank_fraction;
    if (comparison?.quarter_tank && fraction) {
      root.append(svg('line', { x1: LEFT, x2: W - RIGHT, y1: yTank(capacity * fraction), y2: yTank(capacity * fraction), class: 'pf-quarter' }));
      root.append(svg('text', { x: W - RIGHT, y: yTank(capacity * fraction) - 4, class: 'pf-cap-label', 'text-anchor': 'end' }, `quarter-tank driver refuels below ${fmt.pct(fraction * 100)}`));
    }
    const pointsOf = (t) => t.points.map((p) => `${x(p.mile).toFixed(1)},${yTank(p.fuel).toFixed(1)}`).join(' ');
    if (blindTrace) root.append(svg('polyline', { points: pointsOf(blindTrace), class: 'pf-pbline' }));
    root.append(svg('polyline', { points: pointsOf(trace), class: 'pf-plan' }));

    // Audit: red marks where the recomputed tank leaves [0, capacity].
    for (const p of trace.points) {
      if (p.fuel < -0.05 || p.fuel > capacity + 0.05) {
        root.append(svg('circle', { cx: x(p.mile), cy: yTank(Math.max(0, Math.min(capacity, p.fuel))), r: 6, class: 'pf-bad' },
          svg('title', {}, `Tank audit failed at mile ${fmt.dec1(p.mile)}: ${fmt.gal(p.fuel)}`)));
      }
    }

    // Plan stops (price band), focusable.
    for (const s of stops) {
      const g = svg('g', { class: 'pf-stop', tabindex: 0, role: 'button', 'aria-label': `Stop ${s.stop}: ${s.name}, mile ${fmt.dec1(s.mile_marker)}, ${fmt.price(s.price_per_gallon)} per gallon` });
      const cx = x(s.mile_marker);
      const cy = hasPrices ? yPrice(s.price_per_gallon) : (PRICE_TOP + PRICE_BOTTOM) / 2;
      g.append(svg('circle', { cx, cy, r: 9 }), svg('text', { x: cx, y: cy + 3.5, 'text-anchor': 'middle' }, String(s.stop)));
      g.addEventListener('click', () => onStop?.(s.stop, 'profile'));
      g.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onStop?.(s.stop, 'profile');
        }
      });
      root.append(g);
    }

    const cross = svg('line', { x1: 0, x2: 0, y1: PRICE_TOP, y2: TANK_BOTTOM, class: 'pf-cross', visibility: 'hidden' });
    const dot = svg('circle', { cx: 0, cy: 0, r: 4, class: 'pf-cross-dot', visibility: 'hidden' });
    root.append(cross, dot);
    current = { body, distance, x, yTank, trace, blindTrace, candidates, cross, dot };
  }

  function readout(clientX) {
    if (!current) return;
    const rect = svgNode.getBoundingClientRect();
    const sx = ((clientX - rect.left) / rect.width) * W;
    const mile = Math.max(0, Math.min(current.distance, ((sx - LEFT) / (W - LEFT - RIGHT)) * current.distance));
    const fuel = fuelAt(current.trace, mile);
    const blind = current.blindTrace ? fuelAt(current.blindTrace, mile) : null;
    let nearest = null;
    for (const c of current.candidates) {
      if (!nearest || Math.abs(c.mile_marker - mile) < Math.abs(nearest.mile_marker - mile)) nearest = c;
    }
    const cx = current.x(mile);
    current.cross.setAttribute('x1', cx);
    current.cross.setAttribute('x2', cx);
    current.cross.setAttribute('visibility', 'visible');
    current.dot.setAttribute('cx', cx);
    current.dot.setAttribute('cy', current.yTank(fuel));
    current.dot.setAttribute('visibility', 'visible');
    clear(tipNode);
    tipNode.append(
      el('strong', {}, `Mile ${fmt.dec1(mile)}`),
      el('span', {}, `Plan: ${fmt.gal(fuel)}`),
      blind !== null ? el('span', {}, `Price-blind: ${fmt.gal(blind)}`) : null,
      nearest ? el('span', {}, `Nearest station: ${nearest.name} ${fmt.price(nearest.price_per_gallon)} (mile ${fmt.dec1(nearest.mile_marker)})`) : null,
    );
    tipNode.hidden = false;
    const left = ((cx / W) * rect.width);
    const width = tipNode.offsetWidth;
    tipNode.style.setProperty('left', `${Math.max(0, Math.min(rect.width - width, left - width / 2))}px`);
  }

  function hide() {
    if (!current) return;
    current.cross.setAttribute('visibility', 'hidden');
    current.dot.setAttribute('visibility', 'hidden');
    tipNode.hidden = true;
  }

  svgNode.addEventListener('pointermove', (event) => readout(event.clientX));
  svgNode.addEventListener('pointerdown', (event) => readout(event.clientX));
  svgNode.addEventListener('pointerleave', hide);

  function highlightStop(n) {
    for (const g of svgNode.querySelectorAll('.pf-stop')) g.classList.toggle('is-active', g.querySelector('text')?.textContent === String(n));
  }

  return { render, highlightStop, clear: () => { clear(svgNode); current = null; tipNode.hidden = true; } };
}
