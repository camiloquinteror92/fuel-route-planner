// "Play trip": the plan replayed on the map, with the API's own numbers.
//
// A truck drives the route line (the answer's GeoJSON) at a constant speed, so each
// leg takes a time proportional to its miles. The tank gauge drains at the plan's
// mpg; at every stop the truck waits about a second, the stop lights up on the map,
// in the stops table and on the fuel profile, the tank rises by the gallons bought
// and "+gal · +$" appears. Arrival fuel is the API's fuel_on_arrival_gallons and the
// cost counter adds each stop's cost in integer cents, so it ends EXACTLY on the
// API's total_fuel_cost (and says so). Pause, resume, 1×/2×/4×, restart. With
// prefers-reduced-motion the final state is shown at once, without animation.

import { el, fmt, mark, plural, replace, present } from './format.js';

const MILES_PER_SECOND = 110; // at 1×, clamped below so short and long trips both read well
const MIN_SECONDS = 8;
const MAX_SECONDS = 28;
const STOP_PAUSE_SECONDS = 1;
const GAIN_VISIBLE_SECONDS = 1.8;
const SPEEDS = [1, 2, 4];
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

export function createPlayer(container, { getMap, markStop, clearMarks, stopLatLng, onStop, onMile, announce, reframe }) {
  let trip = null; // everything derived from the answer
  let s = null; // the playback state
  let speed = 1;
  let frame = null;
  let lastTime = null;
  let layers = [];
  const nodes = {};

  // --- DOM ----------------------------------------------------------------------------
  function build() {
    nodes.play = el('button', { type: 'button', class: 'btn btn-play', onclick: toggle });
    nodes.restart = el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: restart, title: 'Restart the trip from the start' }, 'Restart');
    nodes.speeds = SPEEDS.map((n) => el('button', {
      type: 'button', class: 'btn btn-small btn-ghost speed-btn', 'aria-pressed': String(n === speed),
      onclick: () => setSpeed(n), title: `Play at ${n}× speed`,
    }, `${n}×`));
    nodes.fill = el('span', { class: 'progress-fill' });
    nodes.head = el('span', { class: 'progress-head' });
    nodes.ticks = el('span', { class: 'progress-ticks' });
    nodes.track = el('div', { class: 'progress-track', role: 'progressbar', 'aria-label': 'Trip progress', 'aria-valuemin': '0' }, nodes.fill, nodes.ticks, nodes.head);
    nodes.mile = el('strong', {});
    nodes.of = el('span', {});
    nodes.stopText = el('span', { class: 'muted' });
    nodes.gaugeFill = el('span', { class: 'gauge-fill' });
    nodes.reserve = el('span', { class: 'gauge-reserve', title: 'Safety reserve' });
    nodes.gaugeTrack = el('div', { class: 'gauge-track', role: 'meter', 'aria-label': 'Fuel in the tank', 'aria-valuemin': '0' },
      el('span', { class: 'gauge-e', 'aria-hidden': 'true' }, 'E'), nodes.gaugeFill, nodes.reserve, el('span', { class: 'gauge-f', 'aria-hidden': 'true' }, 'F'));
    nodes.gaugeValue = el('span', { class: 'gauge-value' });
    nodes.cost = el('strong', { class: 'cost-value' });
    nodes.costOf = el('small', { class: 'muted' });
    nodes.event = el('p', { class: 'player-event' });
    replace(container,
      el('div', { class: 'player-row' },
        el('div', { class: 'player-controls' }, nodes.play,
          el('div', { class: 'speed', role: 'group', 'aria-label': 'Playback speed' }, nodes.speeds), nodes.restart),
        el('div', { class: 'player-progress' }, nodes.track,
          el('p', { class: 'progress-label' }, 'Mile ', nodes.mile, ' ', nodes.of, ' ', nodes.stopText))),
      el('div', { class: 'player-row player-readouts' },
        el('div', { class: 'gauge' }, el('span', { class: 'gauge-title' }, 'Tank'), nodes.gaugeTrack, nodes.gaugeValue),
        el('div', { class: 'cost' }, el('span', { class: 'gauge-title' }, 'Fuel bought'), nodes.cost, ' ', nodes.costOf),
        nodes.event));
  }

  function playLabel() {
    const phase = s?.phase;
    const label = phase === 'playing' ? 'Pause' : phase === 'paused' ? 'Resume' : phase === 'done' ? 'Play again' : 'Play trip';
    replace(nodes.play, el('span', { class: `play-icon ${phase === 'playing' ? 'is-pause' : 'is-play'}`, 'aria-hidden': 'true' }), label);
    nodes.play.setAttribute('aria-label', `${label} (animation of the plan on the map)`);
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
      phase: 'idle', mile: 0, fuel: start, departFuel: start, departMile: 0, costCents: 0,
      next: 0, atStop: null, vertex: 0, timers: [], trail: null, truck: null,
    };
  }

  function load(body) {
    stop();
    removeLayers();
    clearMarks?.();
    const line = (body?.map?.geojson?.features || []).find((f) => f.geometry?.type === 'LineString');
    if (!line || !body.route?.distance_miles) {
      trip = null;
      s = null;
      container.hidden = true;
      return;
    }
    const distance = body.route.distance_miles;
    const v = body.vehicle || {};
    const mpg = v.miles_per_gallon ?? v.mpg;
    const capacity = v.tank_gallons ?? (v.max_range_miles && mpg ? v.max_range_miles / mpg : null);
    const seconds = Math.min(MAX_SECONDS, Math.max(MIN_SECONDS, distance / MILES_PER_SECOND));
    trip = {
      body, distance, mpg, capacity,
      reserve: Number(v.safety_reserve_gal) || 0,
      startFuel: Number(body.summary?.start_fuel_gallons) || 0,
      endFuel: body.summary?.end_fuel_gallons,
      totalCents: cents(body.summary?.total_fuel_cost ?? 0),
      stops: body.fuel_stops || [],
      line: measureLine(line.geometry.coordinates, distance),
      milesPerSecond: distance / seconds,
    };
    if (!nodes.play) build();
    container.hidden = false;
    replace(nodes.ticks, trip.stops.map((stop) => el('span', {
      class: 'progress-tick', title: `Stop ${stop.stop} at mile ${fmt.dec1(stop.mile_marker)}`,
      css: { left: `${(stop.mile_marker / distance) * 100}%` },
    })));
    nodes.track.setAttribute('aria-valuemax', String(distance));
    nodes.gaugeTrack.setAttribute('aria-valuemax', String(capacity ?? 0));
    nodes.reserve.hidden = !(trip.reserve > 0 && capacity);
    if (trip.reserve > 0 && capacity) nodes.reserve.style.setProperty('left', `${(trip.reserve / capacity) * 100}%`);
    nodes.of.textContent = `of ${fmt.miles(distance)}`;
    nodes.costOf.textContent = `of ${fmt.money(trip.totalCents / 100)}`;
    s = initialState();
    nodes.event.textContent = trip.stops.length
      ? `Press Play: ${plural(trip.stops.length, 'stop')} on the way.`
      : 'Press Play: no stop needed on this trip.';
    draw();
    playLabel();
  }

  function unload() {
    stop();
    removeLayers();
    clearMarks?.();
    trip = null;
    s = null;
    container.hidden = true;
  }

  // --- the clock ------------------------------------------------------------------------
  function loop(now) {
    if (!s || s.phase !== 'playing') return;
    const dt = lastTime === null ? 0 : Math.min(0.1, (now - lastTime) / 1000); // a hidden tab does not jump
    lastTime = now;
    advance(dt * speed);
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
        const t = 1 - s.atStop.remaining / STOP_PAUSE_SECONDS;
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

  function arrive(stop) {
    const arrival = present(stop.fuel_on_arrival_gallons) ? stop.fuel_on_arrival_gallons : fuelOnTheRoad(stop.mile_marker);
    s.fuel = arrival;
    s.atStop = {
      stop, remaining: STOP_PAUSE_SECONDS,
      fuelFrom: arrival, fuelTo: arrival + stop.gallons,
      costFrom: s.costCents, costTo: s.costCents + cents(stop.cost),
    };
    markStop?.(stop.stop, 'current');
    onStop?.(stop.stop);
    gain(stop);
    nodes.event.replaceChildren(el('strong', {}, `Stop ${stop.stop}`), ` · ${stop.name}: +${fmt.gal(stop.gallons)} · +${fmt.money(stop.cost)} at ${fmt.price(stop.price_per_gallon)}/gal`);
    announce?.(`Stop ${stop.stop}, mile ${fmt.dec1(stop.mile_marker)}: arrives with ${fmt.gal(arrival)}, buys ${fmt.gal(stop.gallons)} for ${fmt.money(stop.cost)}.`);
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
      `${plural(trip.stops.length, 'stop')}, ${fmt.gal(trip.body.summary?.total_gallons_purchased ?? 0)} bought for ${fmt.money(trip.totalCents / 100)} `,
      mark(s.exact, s.exact ? 'the sum of the stops = the API total, to the cent' : 'the sum of the stops differs from the API total'));
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
    }, (GAIN_VISIBLE_SECONDS * 1000) / speed));
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
      const node = s.truck.getElement();
      if (node) node.classList.toggle('is-west', ahead[1] < at[1] - 1e-6);
    }
    onMile?.(s.phase === 'idle' ? null : s.mile);
    const share = trip.distance ? s.mile / trip.distance : 0;
    nodes.fill.style.setProperty('width', `${share * 100}%`);
    nodes.head.style.setProperty('left', `${share * 100}%`);
    nodes.track.setAttribute('aria-valuenow', s.mile.toFixed(1));
    nodes.mile.textContent = fmt.dec1(s.mile);
    const passed = s.next + (s.atStop ? 1 : 0);
    nodes.stopText.textContent = trip.stops.length ? `· stop ${fmt.int(Math.min(passed, trip.stops.length))} of ${fmt.int(trip.stops.length)}` : '· no stop needed';
    const capacity = trip.capacity || 1;
    const level = Math.max(0, Math.min(1, s.fuel / capacity));
    nodes.gaugeFill.style.setProperty('width', `${level * 100}%`);
    nodes.gaugeFill.classList.toggle('is-low', level < 0.25);
    nodes.gaugeFill.classList.toggle('is-mid', level >= 0.25 && level < 0.5);
    nodes.gaugeTrack.setAttribute('aria-valuenow', Math.max(0, s.fuel).toFixed(2));
    nodes.gaugeValue.textContent = `${fmt.dec1(Math.max(0, s.fuel))} / ${fmt.dec1(trip.capacity ?? 0)} gal`;
    nodes.cost.textContent = fmt.money((s.shownCents ?? s.costCents) / 100);
    container.classList.toggle('is-playing', s.phase === 'playing');
    container.classList.toggle('is-done', s.phase === 'done');
  }

  // --- controls -------------------------------------------------------------------------
  function play() {
    if (!trip) return;
    if (s.phase === 'done') {
      restart();
      return;
    }
    if (s.phase === 'idle') {
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
    stop();
    removeLayers();
    clearMarks?.();
    s = initialState();
    addLayers();
    nodes.event.textContent = '';
    draw();
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

  function setSpeed(n) {
    speed = n;
    nodes.speeds.forEach((button, i) => button.setAttribute('aria-pressed', String(SPEEDS[i] === n)));
  }

  return { load, unload, play, pause, restart, isPlaying: () => s?.phase === 'playing' };
}
