// The route map (Leaflet + OpenStreetMap tiles), drawn from the API answer: the road
// with its start and finish, then the numbered fuel stops, and for a 422
// no_reachable_fuel_station the stretch with no truck stop. Popups are built with
// DOM nodes, never HTML strings.
//
// Framing: the trip stays framed when the box changes size (window resize, phone
// rotation, a step that shows the map again) until the user pans or zooms; "Whole
// trip" frames it again.

import { el, fmt } from './format.js';

const USA_CENTER = [39.5, -98.35];
const USA_ZOOM = 4;
const PHONE = '(max-width: 959.98px)';
// Zoom step of the fit only (the +/- buttons keep the map's half steps).
const FIT_SNAP = 0.1;
const EDGE = 28;

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function stopPopup(stop, why) {
  return el('div', { class: 'popup' },
    el('p', { class: 'popup-title' }, `Stop ${stop.stop}: `, el('strong', {}, stop.name)),
    el('p', {}, `${stop.city}, ${stop.state} · mile ${fmt.dec1(stop.mile_marker)}`),
    el('p', {}, `${fmt.price(stop.price_per_gallon)}/gal · buys ${fmt.gal(stop.gallons)} · ${fmt.money(stop.cost)}`),
    why ? el('p', { class: 'popup-why' }, why) : null,
  );
}

export function createMap(container, { onStop, why } = {}) {
  // Half-step zoom: a coast-to-coast trip fills a phone-width map instead of a third of it.
  const map = L.map(container, { preferCanvas: true, scrollWheelZoom: false, zoomSnap: 0.5 }).setView(USA_CENTER, USA_ZOOM);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · road by <a href="https://project-osrm.org/">OSRM</a>',
  }).addTo(map);
  // Wheel zoom only after the user clicks the map, so the page scrolls normally.
  map.on('click', () => map.scrollWheelZoom.enable());
  container.addEventListener('mouseleave', () => map.scrollWheelZoom.disable());

  let shown = null; // the answer whose road is drawn
  let roadLayers = [];
  let stopsLayer = null;
  let gapLayer = null;
  let stopMarkers = new Map();
  let lastBounds = null; // what the current view should show
  let userMoved = false; // the user panned or zoomed: stop re-framing
  let framing = false; // our own fitBounds / setView is running

  function frame() {
    if (container.clientWidth === 0 || container.clientHeight === 0) return;
    map.invalidateSize();
    if (!lastBounds || userMoved) return;
    framing = true;
    const snap = map.options.zoomSnap;
    map.options.zoomSnap = FIT_SNAP;
    try {
      // Room on the left for the zoom buttons and "Whole trip".
      map.fitBounds(lastBounds, { paddingTopLeft: [EDGE + 36, EDGE], paddingBottomRight: [EDGE, EDGE], animate: false });
    } finally {
      map.options.zoomSnap = snap;
      framing = false;
    }
  }
  new ResizeObserver(frame).observe(container);
  for (const event of ['dragstart', 'zoomstart']) {
    map.on(event, () => {
      if (!framing) userMoved = true;
    });
  }

  function fitTo(bounds) {
    lastBounds = bounds;
    userMoved = false;
    container.classList.add('has-trip'); // shows "Whole trip"
    frame();
  }

  // "Show the whole trip" again (after a pan, a zoom or a stop).
  function reframe() {
    userMoved = false;
    frame();
  }

  const whole = L.control({ position: 'topleft' });
  whole.onAdd = () => {
    const button = el('button', { type: 'button', class: 'map-reframe', title: 'Show the whole trip' }, 'Whole trip');
    L.DomEvent.disableClickPropagation(button);
    button.addEventListener('click', reframe);
    return button;
  };
  whole.addTo(map);

  function clearGap() {
    if (gapLayer) map.removeLayer(gapLayer);
    gapLayer = null;
  }

  function clearRoad() {
    roadLayers.forEach((layer) => map.removeLayer(layer));
    if (stopsLayer) map.removeLayer(stopsLayer);
    roadLayers = [];
    stopsLayer = null;
    stopMarkers = new Map();
    shown = null;
  }

  // The road, start and finish of an answer (drawn once per answer), framed when new.
  function drawRoad(body) {
    clearGap();
    if (shown === body) return;
    clearRoad();
    shown = body;
    const features = body.map?.geojson?.features || [];
    const line = features.find((f) => f.geometry?.type === 'LineString');
    const road = L.geoJSON(line ? [line] : [], { style: { color: token('--route'), weight: 5, opacity: 0.85 } }).addTo(map);
    roadLayers.push(road);
    for (const f of features) {
      const kind = f.properties?.kind;
      if (kind !== 'start' && kind !== 'finish') continue;
      const [lon, lat] = f.geometry.coordinates;
      const marker = L.circleMarker([lat, lon], {
        radius: 8, color: '#ffffff', weight: 2, fillOpacity: 1,
        fillColor: kind === 'start' ? token('--ok') : token('--danger'),
      }).bindPopup(el('div', { class: 'popup' }, el('strong', {}, kind === 'start' ? 'Start' : 'Finish'), ': ', f.properties.label));
      marker.addTo(map);
      roadLayers.push(marker);
    }
    // The numbered stops, built now and shown by showStops().
    stopsLayer = L.layerGroup();
    for (const s of body.fuel_stops || []) {
      const icon = L.divIcon({
        className: 'stop-marker',
        html: el('span', {}, String(s.stop)),
        iconSize: [28, 28],
        iconAnchor: [14, 14],
        popupAnchor: [0, -12],
      });
      const marker = L.marker([s.lat, s.lon], { icon, zIndexOffset: 1000, title: `Stop ${s.stop}: ${s.city}, ${s.state}`, alt: `Stop ${s.stop}` });
      marker.bindPopup(stopPopup(s, why ? why(s, body) : null), { maxWidth: 300 });
      marker.on('click', () => onStop?.(s.stop, 'map'));
      marker.addTo(stopsLayer);
      stopMarkers.set(s.stop, marker);
    }
    if (line) fitTo(road.getBounds());
  }

  // Step "Route": only the road.
  function showRouteOnly(body) {
    drawRoad(body);
    if (stopsLayer && map.hasLayer(stopsLayer)) map.removeLayer(stopsLayer);
    map.closePopup();
  }

  // Steps after "Route": the road and the stops.
  function showStops(body) {
    drawRoad(body);
    if (stopsLayer && !map.hasLayer(stopsLayer)) stopsLayer.addTo(map);
  }

  // 422 no_reachable_fuel_station: where the stretch without a truck stop is.
  function renderGap(body, { usableRange } = {}) {
    clearRoad();
    clearGap();
    const a = body.gap_start;
    const b = body.gap_end;
    if (!a || !b) return;
    gapLayer = L.layerGroup();
    const danger = token('--danger');
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: danger, weight: 5, dashArray: '8 8' }).addTo(gapLayer);
    for (const [point, label] of [[a, 'Last truck stop before the gap'], [b, body.next_station_mile === null ? 'Finish' : 'Next truck stop']]) {
      L.circleMarker([point.lat, point.lon], { radius: 8, color: '#ffffff', weight: 2, fillColor: danger, fillOpacity: 1 })
        .bindPopup(el('div', { class: 'popup' }, el('strong', {}, label), ` at mile ${fmt.dec1(point.mile)}`))
        .addTo(gapLayer);
    }
    L.tooltip({ permanent: true, direction: 'center', className: 'gap-label' })
      .setLatLng([(a.lat + b.lat) / 2, (a.lon + b.lon) / 2])
      .setContent(el('span', {}, `No truck stop for ${fmt.miles(body.gap_miles)}${usableRange ? ` (a tank lasts ${fmt.miles_short(usableRange)})` : ''}`))
      .addTo(gapLayer);
    gapLayer.addTo(map);
    fitTo(L.latLngBounds([[a.lat, a.lon], [b.lat, b.lon]]));
  }

  function showUsa() {
    clearRoad();
    clearGap();
    lastBounds = null;
    container.classList.remove('has-trip');
    framing = true;
    try {
      map.setView(USA_CENTER, USA_ZOOM);
    } finally {
      framing = false;
    }
  }

  // A stop chosen in the list: light it up, open its popup and bring it into view.
  function focusStop(n) {
    const marker = stopMarkers.get(n);
    if (!marker) return;
    selectStop(n);
    if (window.matchMedia(PHONE).matches) {
      const rect = container.getBoundingClientRect();
      if (rect.bottom < 60 || rect.top > window.innerHeight) {
        container.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
      }
    }
    userMoved = true; // the user asked for this stop: a resize must not undo it
    marker.openPopup();
  }

  // The selected stop (a ring around its number).
  function selectStop(n) {
    for (const [k, marker] of stopMarkers) marker.getElement()?.classList.toggle('is-selected', k === n);
  }

  // Play trip: the stop the truck is at ('current') and those it has passed ('visited').
  function markStop(n, kind) {
    const node = stopMarkers.get(n)?.getElement();
    if (!node) return;
    node.classList.toggle('is-current', kind === 'current');
    node.classList.toggle('is-visited', kind === 'visited');
  }

  function clearMarks() {
    for (const marker of stopMarkers.values()) marker.getElement()?.classList.remove('is-current', 'is-visited', 'is-selected');
  }

  function stopLatLng(n) {
    const marker = stopMarkers.get(n);
    return marker ? marker.getLatLng() : null;
  }

  return {
    map, showRouteOnly, showStops, renderGap, showUsa, focusStop, selectStop, reframe, invalidate: frame, markStop, clearMarks, stopLatLng,
  };
}
