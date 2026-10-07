// "Play the trip": the plan replayed on the map, with the API's own numbers.
//
// A truck drives the road (the answer's map line) at a constant speed. The tank
// gauge drains at the plan's miles per gallon; at every stop the truck waits a
// moment, the stop lights up (on the map, in the list and the rule it follows), the
// tank rises by the gallons bought and "Fuel bought" adds the stop's cost. Arrival
// fuel is the API's fuel_on_arrival_gallons, and the cost counter adds each stop's
// cost in integer cents, so it ends EXACTLY on the API's total_fuel_cost (and says
// so). The whole replay takes about ten to fifteen seconds, whatever the trip. With
// prefers-reduced-motion the final state is shown at once, without animation.

import { el, fmt, mark, plural, replace, present } from './format.js';
import { RULES, ruleOf } from './plan.js';

const MIN_SECONDS = 10;
const MAX_SECONDS = 15;
const LONG_TRIP_MILES = 1200; // beyond it, a trip plays a little longer, up to MAX_SECONDS
const MILES_PER_EXTRA_SECOND = 600;
const STOP_SECONDS = 0.7; // the most a stop waits; with many stops they share a third of the time
const STOP_SHARE = 0.35;
const GAIN_VISIBLE_SECONDS = 1.6;
const EARTH_MILES = 3958.7613;

const cents = (value) => Math.round(Number(value) * 100);

function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function haversine(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad;
  const dLon = (b[1] - a[1]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_MILES * Math.asin(Math.min(1, Math.sqrt(h)));
}

// The route line with a mile for every vertex, scaled to the API's distance so the
// stops' mile markers land where the API put them.
export function measureLine(coordinates, distance) {
  const points = coordinates.map(([lon, lat]) => [lat, lon]);
  const miles = [0];
  for (let i = 1; i < points.length; i++) miles.push(miles[i - 1] + haversine(points[i - 1], points[i]));
  const total = miles[miles.length - 1] || 1;
  const scale = present(distance) && distance > 0 ? distance / total : 1;
  return { points, miles: miles.map((m) => m * scale) };
}

// [lat, lon] at a mile, and the index of the last vertex before it.
export function pointAt(line, mile) {
  const { points, miles } = line;
  if (mile <= 0) return { at: points[0], index: 0 };
  const last = points.length - 1;
  if (mile >= miles[last]) return { at: points[last], index: last };
  let lo = 0;
  let hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (miles[mid] <= mile) lo = mid;
    else hi = mid;
  }
  const span = miles[hi] - miles[lo] || 1;
  const t = (mile - miles[lo]) / span;
  return { at: [points[lo][0] + (points[hi][0] - points[lo][0]) * t, points[lo][1] + (points[hi][1] - points[lo][1]) * t], index: lo };
}

// How long the replay lasts: {seconds driving, seconds waiting at each stop}.
export function timing(distance, stopCount) {
  const total = Math.min(MAX_SECONDS, MIN_SECONDS + Math.max(0, distance - LONG_TRIP_MILES) / MILES_PER_EXTRA_SECOND);
  const perStop = stopCount ? Math.min(STOP_SECONDS, (total * STOP_SHARE) / stopCount) : 0;
  return { driving: total - perStop * stopCount, perStop };
}

const TRUCK_SVG = 'M2 5h13v9H2zM15 8h4.5l3.5 3.5V14h-8zM5 17.2a2 2 0 1 0 0-.1zM18 17.2a2 2 0 1 0 0-.1z';

function truckIcon() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 20');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', TRUCK_SVG);
  svg.append(path);
  return el('span', { class: 'truck-body' }, svg);
}

export function createPlayer(container, { getMap, markStop, clearMarks, stopLatLng, onStop, announce, reframe }) {
  let trip = null; // everything derived from the answer
  let s = null; // the playback state
  let frame = null;
  let lastTime = null;
  let layers = [];
  const nodes = {};

  // --- DOM ----------------------------------------------------------------------------
  function build() {
    nodes.play = el('button', { type: 'button', class: 'btn btn-play', onclick: toggle });
    nodes.restart = el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: restart }, 'Restart');
    nodes.fill = el('span', { class: 'progress-fill' });
    nodes.head = el('span', { class: 'progress-head' });
    nodes.ticks = el('span', { class: 'progress-ticks' });
    nodes.track = el('div', { class: 'progress-track', role: 'progressbar', 'aria-label': 'Miles driven', 'aria-valuemin': '0' }, nodes.fill, nodes.ticks, nodes.head);
    nodes.mile = el('strong', {});
    nodes.of = el('span', {});
    nodes.gaugeFill = el('span', { class: 'gauge-fill' });
    nodes.reserve = el('span', { class: 'gauge-reserve', title: 'Safety fuel' });
    nodes.gaugeTrack = el('div', { class: 'gauge-track', role: 'meter', 'aria-label': 'Fuel in the tank', 'aria-valuemin': '0' },
      el('span', { class: 'gauge-e', 'aria-hidden': 'true' }, 'E'), nodes.gaugeFill, nodes.reserve, el('span', { class: 'gauge-f', 'aria-hidden': 'true' }, 'F'));
    nodes.gaugeValue = el('span', { class: 'gauge-value' });
    nodes.cost = el('strong', { class: 'cost-value' });
    nodes.costOf = el('span', { class: 'muted' });
    nodes.event = el('p', { class: 'player-event' });
    replace(container,
      el('div', { class: 'player-row' },
        el('div', { class: 'player-controls' }, nodes.play, nodes.restart),
        el('div', { class: 'player-progress' }, nodes.track,
          el('p', { class: 'progress-label' }, 'Mile ', nodes.mile, ' ', nodes.of))),
      el('div', { class: 'player-row player-readouts' },
        el('div', { class: 'gauge' }, el('span', { class: 'gauge-title' }, 'Tank'), nodes.gaugeTrack, nodes.gaugeValue),
        el('div', { class: 'cost' }, el('span', { class: 'gauge-title' }, 'Fuel bought'), nodes.cost, nodes.costOf)),
      nodes.event);
  }

  function playLabel() {
    const phase = s?.phase;
    const label = phase === 'playing' ? 'Pause' : phase === 'paused' ? 'Resume' : phase === 'done' ? 'Play again' : 'Play the trip';
    replace(nodes.play, el('span', { class: `play-icon ${phase === 'playing' ? 'is-pause' : 'is-play'}`, 'aria-hidden': 'true' }), label);
    nodes.restart.hidden = phase === 'idle';
  }

  // --- layers -------------------------------------------------------------------------
  function removeLayers() {
    const map = getMap();
    if (map) layers.forEach((layer) => map.removeLayer(layer));
    layers = [];
    s?.timers?.forEach(clearTimeout);
  }

  function addLayers() {
    const map = getMap();
    if (!map || !window.L) return;
    s.trail = L.polyline([trip.line.points[0], trip.line.points[0]], { color: token('--trail-line'), weight: 5, opacity: 0.9, interactive: false });
    s.truck = L.marker(trip.line.points[0], {
      icon: L.divIcon({ className: 'truck-marker', html: truckIcon(), iconSize: [34, 34], iconAnchor: [17, 17] }),
      interactive: false, keyboard: false, zIndexOffset: 2000,
    });
    layers = [s.trail, s.truck];
    layers.forEach((layer) => layer.addTo(map));
  }

  // --- state ----------------------------------------------------------------------------
  function initialState() {
    const start = trip.startFuel;
    return {
      phase: 'idle', mile: 0, fuel: start, departFuel: start, departMile: 0, costCents: 0, shownCents: null,
      next: 0, atStop: null, vertex: 0, timers: [], trail: null, truck: null,
    };
  }

  function idleText() {
    return trip.stops.length ? `Press Play: ${plural(trip.stops.length, 'stop')} on the way.` : 'Press Play: no stop needed on this trip.';
  }

  // A new answer: ready to play, nothing on the map yet.
  function load(body) {
    reset();
    const line = (body?.map?.geojson?.features || []).find((f) => f.geometry?.type === 'LineString');
    if (!line || !body.route?.distance_miles) {
      trip = null;
      return;
    }
    const distance = body.route.distance_miles;
    const v = body.vehicle || {};
    const stops = body.fuel_stops || [];
    const { driving, perStop } = timing(distance, stops.length);
    trip = {
      body, distance, stops, perStop,
      mpg: v.miles_per_gallon,
      capacity: v.tank_gallons ?? (v.max_range_miles && v.miles_per_gallon ? v.max_range_miles / v.miles_per_gallon : null),
      reserve: Number(v.safety_reserve_gal) || 0,
      startFuel: Number(body.summary?.start_fuel_gallons) || 0,
      endFuel: body.summary?.end_fuel_gallons,
      totalCents: cents(body.summary?.total_fuel_cost ?? 0),
      line: measureLine(line.geometry.coordinates, distance),
      milesPerSecond: distance / driving,
    };
    if (!nodes.play) build();
    replace(nodes.ticks, stops.map((stop) => el('span', {
      class: 'progress-tick', title: `Stop ${stop.stop}`, css: { left: `${(stop.mile_marker / distance) * 100}%` },
    })));
    nodes.track.setAttribute('aria-valuemax', String(distance));
    nodes.gaugeTrack.setAttribute('aria-valuemax', String(trip.capacity ?? 0));
    nodes.reserve.hidden = !(trip.reserve > 0 && trip.capacity);
    if (trip.reserve > 0 && trip.capacity) nodes.reserve.style.setProperty('left', `${(trip.reserve / trip.capacity) * 100}%`);
    nodes.of.textContent = `of ${fmt.miles(distance)}`;
    nodes.costOf.textContent = ` of ${fmt.money(trip.totalCents / 100)}`;
    s = initialState();
    nodes.event.textContent = idleText();
    draw();
    playLabel();
  }

  // Back to the start, with nothing on the map (leaving the step, or a new answer).
  function reset() {
    stop();
    removeLayers();
    clearMarks?.();
    if (!trip) return;
    s = initialState();
    nodes.event.textContent = idleText();
    draw();
    playLabel();
  }

  // --- the clock ------------------------------------------------------------------------
  function loop(now) {
    if (!s || s.phase !== 'playing') return;
    const dt = lastTime === null ? 0 : Math.min(0.1, (now - lastTime) / 1000); // a hidden tab does not jump
    lastTime = now;
    advance(dt);
    draw();
    if (s.phase === 'playing') frame = requestAnimationFrame(loop);
  }

  function stop() {
    cancelAnimationFrame(frame);
    frame = null;
    lastTime = null;
  }

  function fuelOnTheRoad(mile) {
    return s.departFuel - (mile - s.departMile) / trip.mpg;
  }

  function advance(dt) {
    let left = dt;
    while (left > 0 && s.phase === 'playing') {
      if (s.atStop) {
        const used = Math.min(left, s.atStop.remaining);
        s.atStop.remaining -= used;
        left -= used;
        const t = trip.perStop ? 1 - s.atStop.remaining / trip.perStop : 1;
        const ease = 1 - (1 - t) ** 3;
        s.fuel = s.atStop.fuelFrom + (s.atStop.fuelTo - s.atStop.fuelFrom) * ease;
        s.shownCents = Math.round(s.atStop.costFrom + (s.atStop.costTo - s.atStop.costFrom) * ease);
        if (s.atStop.remaining <= 0) depart();
        continue;
      }
      const next = trip.stops[s.next];
      const target = next ? next.mile_marker : trip.distance;
      const need = (target - s.mile) / trip.milesPerSecond;
      if (left >= need) {
        left -= need;
        s.mile = target;
        if (next) arrive(next);
        else finish();
      } else {
        s.mile += trip.milesPerSecond * left;
        left = 0;
        s.fuel = fuelOnTheRoad(s.mile);
      }
    }
  }

  function eventText(stop) {
    return [el('strong', {}, `Stop ${stop.stop} · ${stop.city}: ${RULES[ruleOf(stop)] || ruleOf(stop)}`),
      ` · buys ${fmt.gal(stop.gallons)} at ${fmt.price(stop.price_per_gallon)} = ${fmt.money(stop.cost)}`];
  }

  function arrive(stop) {
    const arrival = present(stop.fuel_on_arrival_gallons) ? stop.fuel_on_arrival_gallons : fuelOnTheRoad(stop.mile_marker);
    s.fuel = arrival;
    s.atStop = {
      stop, remaining: trip.perStop,
      fuelFrom: arrival, fuelTo: arrival + stop.gallons,
      costFrom: s.costCents, costTo: s.costCents + cents(stop.cost),
    };
    markStop?.(stop.stop, 'current');
    onStop?.(stop.stop);
    gain(stop);
    nodes.event.replaceChildren(...eventText(stop));
    announce?.(`Stop ${stop.stop}, ${stop.city}: arrives with ${fmt.gal(arrival)}, buys ${fmt.gal(stop.gallons)} for ${fmt.money(stop.cost)}.`);
  }

  function depart() {
    const { stop, fuelTo, costTo } = s.atStop;
    s.fuel = fuelTo;
    s.costCents = costTo;
    s.shownCents = null;
    s.departFuel = fuelTo;
    s.departMile = stop.mile_marker;
    s.atStop = null;
    s.next += 1;
    markStop?.(stop.stop, 'visited');
  }

  function finish() {
    s.mile = trip.distance;
    s.fuel = present(trip.endFuel) ? trip.endFuel : fuelOnTheRoad(trip.distance);
    s.phase = 'done';
    s.exact = s.costCents === trip.totalCents;
    s.costCents = trip.totalCents;
    onStop?.(null);
    nodes.event.replaceChildren(
      el('strong', {}, 'Arrived. '),
      `${plural(trip.stops.length, 'stop')}, ${fmt.gal(trip.body.summary?.total_gallons_purchased ?? 0)} bought for ${fmt.money(trip.totalCents / 100)}. `,
      mark(s.exact, s.exact ? 'The stops add up to the total, to the cent.' : 'The stops do not add up to the total.'));
    announce?.(`Arrived after ${fmt.miles(trip.distance)}: ${plural(trip.stops.length, 'stop')}, ${fmt.money(trip.totalCents / 100)} in total.`);
    playLabel();
  }

  function gain(stop) {
    const map = getMap();
    const at = stopLatLng?.(stop.stop) || [stop.lat, stop.lon];
    if (!map || !window.L) return;
    const tip = L.tooltip({ permanent: true, direction: 'top', className: 'gain-tip', offset: [0, -16], interactive: false })
      .setLatLng(at)
      .setContent(el('span', {}, `+${fmt.gal(stop.gallons)} · +${fmt.money(stop.cost)}`))
      .addTo(map);
    layers.push(tip);
    s.timers.push(setTimeout(() => {
      map.removeLayer(tip);
      layers = layers.filter((layer) => layer !== tip);
    }, GAIN_VISIBLE_SECONDS * 1000));
  }

  // --- drawing --------------------------------------------------------------------------
  function draw() {
    if (!trip || !s) return;
    const { at, index } = pointAt(trip.line, s.mile);
    if (s.trail) {
      const pts = s.trail.getLatLngs();
      pts.pop(); // the moving head
      while (s.vertex < index) {
        s.vertex += 1;
        pts.push(L.latLng(trip.line.points[s.vertex]));
      }
      pts.push(L.latLng(at));
      s.trail.redraw();
    }
    if (s.truck) {
      s.truck.setLatLng(at);
      const ahead = pointAt(trip.line, Math.min(trip.distance, s.mile + 5)).at;
      s.truck.getElement()?.classList.toggle('is-west', ahead[1] < at[1] - 1e-6);
    }
    const share = trip.distance ? s.mile / trip.distance : 0;
    nodes.fill.style.setProperty('width', `${share * 100}%`);
    nodes.head.style.setProperty('left', `${share * 100}%`);
    nodes.track.setAttribute('aria-valuenow', s.mile.toFixed(1));
    nodes.mile.textContent = fmt.dec1(s.mile);
    const capacity = trip.capacity || 1;
    const level = Math.max(0, Math.min(1, s.fuel / capacity));
    nodes.gaugeFill.style.setProperty('width', `${level * 100}%`);
    nodes.gaugeFill.classList.toggle('is-low', level < 0.25);
    nodes.gaugeFill.classList.toggle('is-mid', level >= 0.25 && level < 0.5);
    nodes.gaugeTrack.setAttribute('aria-valuenow', Math.max(0, s.fuel).toFixed(2));
    nodes.gaugeValue.textContent = `${fmt.dec1(Math.max(0, s.fuel))} / ${fmt.dec1(trip.capacity ?? 0)} gal`;
    nodes.cost.textContent = fmt.money((s.shownCents ?? s.costCents) / 100);
    container.classList.toggle('is-playing', s.phase === 'playing');
  }

  // --- controls -------------------------------------------------------------------------
  function play() {
    if (!trip) return;
    if (s.phase === 'done') {
      restart();
      return;
    }
    if (s.phase === 'idle') {
      clearMarks?.();
      reframe?.();
      if (!s.trail) addLayers();
    }
    if (reducedMotion()) {
      jumpToEnd();
      return;
    }
    s.phase = 'playing';
    lastTime = null;
    playLabel();
    frame = requestAnimationFrame(loop);
  }

  function pause() {
    if (s?.phase !== 'playing') return;
    s.phase = 'paused';
    stop();
    draw();
    playLabel();
  }

  function toggle() {
    if (!s) return;
    if (s.phase === 'playing') pause();
    else play();
  }

  function restart() {
    if (!trip) return;
    reset();
    play();
  }

  // Reduced motion: the final state, with every purchase listed.
  function jumpToEnd() {
    s.phase = 'playing';
    for (const stop of trip.stops) {
      markStop?.(stop.stop, 'visited');
      s.costCents += cents(stop.cost);
    }
    s.next = trip.stops.length;
    finish();
    nodes.event.append(el('span', { class: 'player-list' }, trip.stops.map((stop) => ` · Stop ${stop.stop}: +${fmt.gal(stop.gallons)} · +${fmt.money(stop.cost)}`)));
    draw();
  }

  return { load, reset, play, pause, restart, isPlaying: () => s?.phase === 'playing' };
}
