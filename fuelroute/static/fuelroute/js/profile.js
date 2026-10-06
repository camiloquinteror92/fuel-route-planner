// Fuel profile: one SVG, two bands sharing the mile axis.
//   top:    price of every station within the corridor (candidates layer), the
//           plan's stops, the price-blind driver's stops and the corridor average;
//   bottom: fuel in the tank, recomputed in the browser from the stops (solid:
//           this plan, dashed: the price-blind driver), with the tank capacity.
// It is drawn in real pixels: the viewBox is the container's own size, measured on
// every render and again whenever the container is resized, so text and circles are
// never stretched. The reference lines are named in the legend (HTML), not on the
// chart, so their labels cannot cover the data. A crosshair (mouse and touch) reads
// both bands; near a plan stop it describes that stop. Clicking a stop syncs the map
// and the stops table. Points where the tank audit fails are marked in red.

import { el, fmt, svg, clear } from './format.js';
import { fuelAt, planTrace, priceBlindTrace } from './checks.js';

const COMPACT_BELOW = 560; // px: fewer ticks, narrower axis
const STOP_HIT_PX = 12; // the crosshair "touches" a plan stop within this distance

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

function layout(width, height) {
  const compact = width < COMPACT_BELOW;
  const left = compact ? 50 : 64;
  const right = compact ? 10 : 16;
  const priceTop = 26;
  const axisY = height - 18;
  const tankBottom = height - 34;
  const usable = tankBottom - priceTop;
  const priceBottom = priceTop + usable * 0.4;
  const tankTop = priceBottom + usable * 0.17;
  return { compact, width, height, left, right, priceTop, priceBottom, tankTop, tankBottom, axisY };
}

export function createProfile(svgNode, tipNode, { onStop, why } = {}) {
  let current = null;
  let last = null; // {body, about} of the last render, to redraw on resize
  let highlighted = null;

  function render(body, { about } = {}) {
    last = { body, about };
    clear(svgNode);
    const width = Math.round(svgNode.clientWidth);
    const height = Math.round(svgNode.clientHeight);
    if (!width || !height) {
      current = null;
      return; // hidden: drawn when it gets a size (ResizeObserver below)
    }
    const L = layout(width, height);
    svgNode.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const distance = body.route.distance_miles;
    const capacity = body.vehicle.tank_gallons;
    const stops = body.fuel_stops || [];
    const comparison = body.summary.comparison || null;
    const blindStops = comparison?.price_blind?.stops || [];
    const candidates = rowsOf(body.candidates);
    const trace = planTrace(body);
    const blindTrace = priceBlindTrace(body);
    const corridorMiles = body.pipeline?.corridor?.corridor_miles ?? about?.planner?.corridor_miles;

    const plotWidth = width - L.left - L.right;
    const x = (mile) => L.left + (Math.max(0, Math.min(distance, mile)) / (distance || 1)) * plotWidth;
    const prices = [...candidates.map((c) => c.price_per_gallon), ...stops.map((s) => s.price_per_gallon), ...blindStops.map((s) => s.price_per_gallon)];
    const hasPrices = prices.length > 0;
    const pMin = hasPrices ? Math.min(...prices) : 0;
    const pMax = hasPrices ? Math.max(...prices) : 1;
    const pad = (pMax - pMin) * 0.08 || 0.05;
    const yPrice = (p) => L.priceBottom - ((p - (pMin - pad)) / (pMax + pad - (pMin - pad))) * (L.priceBottom - L.priceTop);
    const yTank = (g) => L.tankBottom - (g / (capacity || 1)) * (L.tankBottom - L.tankTop);

    const root = svg('g', {});
    svgNode.append(root);

    // Mile axis: a few round ticks, never on top of the end label.
    const step = niceStep(distance || 1, L.compact ? 3 : 6);
    const endLabel = `${fmt.int(distance)} mi`;
    const endRoom = endLabel.length * 7 + 8;
    for (let mile = 0; mile <= distance + 1e-9; mile += step) {
      root.append(svg('line', { x1: x(mile), x2: x(mile), y1: L.priceTop, y2: L.tankBottom, class: 'pf-grid' }));
      if (x(mile) < width - L.right - endRoom) {
        root.append(svg('text', { x: x(mile), y: L.axisY + 10, class: 'pf-tick', 'text-anchor': 'middle' }, fmt.int(mile)));
      }
    }
    root.append(svg('text', { x: width - L.right, y: L.axisY + 10, class: 'pf-tick pf-tick-end', 'text-anchor': 'end' }, endLabel));
    root.append(svg('line', { x1: L.left, x2: width - L.right, y1: L.tankBottom, y2: L.tankBottom, class: 'pf-axis' }));

    // Price band.
    root.append(svg('text', { x: L.left, y: L.priceTop - 10, class: 'pf-band-title' },
      hasPrices ? `Price at stations within ${fmt.miles_round(corridorMiles)} ($/gal)` : 'Price of the plan stops ($/gal)'));
    if (hasPrices) {
      for (const p of [pMin, pMax]) {
        root.append(svg('text', { x: L.left - 6, y: yPrice(p) + 4, class: 'pf-tick', 'text-anchor': 'end' }, fmt.price(p)));
        root.append(svg('line', { x1: L.left, x2: width - L.right, y1: yPrice(p), y2: yPrice(p), class: 'pf-grid pf-grid-h' }));
      }
      for (const c of candidates) {
        root.append(svg('circle', { cx: x(c.mile_marker), cy: yPrice(c.price_per_gallon), r: 2.4, class: 'pf-cand' }));
      }
      const avg = comparison?.corridor_average?.price_per_gallon;
      if (avg) root.append(svg('line', { x1: L.left, x2: width - L.right, y1: yPrice(avg), y2: yPrice(avg), class: 'pf-avg' }));
      for (const s of blindStops) {
        const cx = x(s.mile_marker);
        const cy = yPrice(s.price_per_gallon);
        root.append(svg('rect', { x: cx - 5, y: cy - 5, width: 10, height: 10, class: 'pf-pb' }));
      }
    }

    // Tank band: 0 and the capacity on the axis; the capacity line is in the legend.
    root.append(svg('text', { x: L.left, y: L.tankTop - 10, class: 'pf-band-title' }, 'Fuel in tank (gal)'));
    for (const g of [0, capacity]) {
      root.append(svg('text', { x: L.left - 6, y: yTank(g) + 4, class: 'pf-tick', 'text-anchor': 'end' }, fmt.int(g)));
    }
    root.append(svg('line', { x1: L.left, x2: width - L.right, y1: yTank(capacity), y2: yTank(capacity), class: 'pf-cap' }));
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
      const g = svg('g', {
        class: `pf-stop${highlighted === s.stop ? ' is-active' : ''}`, tabindex: 0, role: 'button',
        'aria-label': `Stop ${s.stop}: ${s.name}, mile ${fmt.dec1(s.mile_marker)}, ${fmt.price(s.price_per_gallon)} per gallon, buys ${fmt.gal(s.gallons)}`,
        'data-stop': s.stop,
      });
      const cx = x(s.mile_marker);
      const cy = hasPrices ? yPrice(s.price_per_gallon) : (L.priceTop + L.priceBottom) / 2;
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

    const cross = svg('line', { x1: 0, x2: 0, y1: L.priceTop, y2: L.tankBottom, class: 'pf-cross', visibility: 'hidden' });
    const dot = svg('circle', { cx: 0, cy: 0, r: 4, class: 'pf-cross-dot', visibility: 'hidden' });
    root.append(cross, dot);
    current = { body, distance, x, L, plotWidth, yTank, trace, blindTrace, candidates, stops, cross, dot };
  }

  // What is under the crosshair: a plan stop when one is within a few pixels (the
  // stops are what the user came for), else the nearest corridor station, the
  // cheapest one when several share that mile (city-level coordinates).
  function underCursor(mile, px) {
    let stop = null;
    for (const s of current.stops) {
      const gap = Math.abs(current.x(s.mile_marker) - px);
      if (gap <= STOP_HIT_PX && (!stop || gap < Math.abs(current.x(stop.mile_marker) - px))) stop = s;
    }
    if (stop) return { stop };
    let nearest = null;
    for (const c of current.candidates) {
      const gap = Math.abs(c.mile_marker - mile);
      const best = nearest ? Math.abs(nearest.mile_marker - mile) : Infinity;
      if (gap < best - 1e-9 || (Math.abs(gap - best) <= 1e-9 && c.price_per_gallon < nearest.price_per_gallon)) nearest = c;
    }
    return { nearest };
  }

  function readout(clientX) {
    if (!current) return;
    const rect = svgNode.getBoundingClientRect();
    const px = Math.max(current.L.left, Math.min(current.L.left + current.plotWidth, clientX - rect.left));
    const mile = ((px - current.L.left) / (current.plotWidth || 1)) * current.distance;
    const { stop, nearest } = underCursor(mile, px);
    const at = stop ? stop.mile_marker : mile;
    const cx = current.x(at);
    const fuel = fuelAt(current.trace, at);
    const blind = current.blindTrace ? fuelAt(current.blindTrace, at) : null;
    current.cross.setAttribute('x1', cx);
    current.cross.setAttribute('x2', cx);
    current.cross.setAttribute('visibility', 'visible');
    current.dot.setAttribute('cx', cx);
    current.dot.setAttribute('cy', current.yTank(stop ? stop.fuel_on_arrival_gallons + stop.gallons : fuel));
    current.dot.setAttribute('visibility', 'visible');
    clear(tipNode);
    if (stop) {
      const reason = why ? why(stop, current.body) : null;
      tipNode.append(
        el('strong', {}, `Stop ${stop.stop} · mile ${fmt.dec1(stop.mile_marker)}`),
        el('span', {}, `${stop.name}, ${stop.city}, ${stop.state}`),
        el('span', {}, `${fmt.price(stop.price_per_gallon)}/gal · arrives with ${fmt.gal(stop.fuel_on_arrival_gallons)} · buys ${fmt.gal(stop.gallons)} = ${fmt.money(stop.cost)}`),
        reason ? el('span', { class: 'tip-why' }, reason) : null,
      );
    } else {
      tipNode.append(
        el('strong', {}, `Mile ${fmt.dec1(at)}`),
        el('span', {}, `Plan: ${fmt.gal(fuel)} in the tank`),
        blind !== null ? el('span', {}, `Price-blind driver: ${fmt.gal(blind)}`) : null,
        nearest ? el('span', {}, `Nearest station: ${nearest.name} ${fmt.price(nearest.price_per_gallon)} (mile ${fmt.dec1(nearest.mile_marker)})`) : null,
      );
    }
    tipNode.hidden = false;
    const tipWidth = tipNode.offsetWidth;
    tipNode.style.setProperty('left', `${Math.max(0, Math.min(rect.width - tipWidth, cx - tipWidth / 2))}px`);
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

  // Redraw in real pixels when the box changes size (window resize, phone rotation,
  // the card becoming visible).
  let drawnSize = '';
  let frame = null;
  new ResizeObserver(() => {
    const size = `${Math.round(svgNode.clientWidth)}x${Math.round(svgNode.clientHeight)}`;
    if (!last || size === drawnSize || !svgNode.clientWidth) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      drawnSize = size;
      tipNode.hidden = true;
      render(last.body, { about: last.about });
    });
  }).observe(svgNode);

  function draw(body, opts) {
    drawnSize = `${Math.round(svgNode.clientWidth)}x${Math.round(svgNode.clientHeight)}`;
    render(body, opts);
  }

  function highlightStop(n) {
    highlighted = n;
    for (const g of svgNode.querySelectorAll('.pf-stop')) g.classList.toggle('is-active', g.dataset.stop === String(n));
  }

  function clearAll() {
    clear(svgNode);
    current = null;
    last = null;
    highlighted = null;
    tipNode.hidden = true;
  }

  return { render: draw, highlightStop, clear: clearAll };
}
