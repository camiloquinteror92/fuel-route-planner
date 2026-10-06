// The route map (Leaflet + OpenStreetMap tiles). Layers, all from the API answer:
// route line, start / finish, the plan's numbered stops, every station within the
// corridor (include=candidates, grouped by their city-level coordinates and
// coloured by price tercile of THIS trip), the price-blind driver's stops and,
// for a 422 no_reachable_fuel_station, the stretch with no station.
// Popups are built with DOM nodes, never HTML strings.

import { el, fmt } from './format.js';

const USA_CENTER = [39.5, -98.35];
const USA_ZOOM = 4;

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function reducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function rowsOf(candidates) {
  if (!candidates || !Array.isArray(candidates.rows)) return [];
  const f = candidates.fields;
  return candidates.rows.map((row) => Object.fromEntries(f.map((name, i) => [name, row[i]])));
}

// Price cut-offs at one and two thirds of this trip's candidate prices.
export function terciles(prices) {
  if (!prices.length) return null;
  const sorted = [...prices].sort((a, b) => a - b);
  const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
  return { min: sorted[0], t1: at(1 / 3), t2: at(2 / 3), max: sorted[sorted.length - 1] };
}

export function tercileOf(price, t) {
  if (!t) return 'mid';
  if (price <= t.t1) return 'cheap';
  if (price <= t.t2) return 'mid';
  return 'dear';
}

function stopPopup(stop, whyText) {
  const quotes = stop.price_quotes;
  const query = [stop.name, stop.address, stop.city, stop.state].filter(Boolean).join(', ');
  return el('div', { class: 'popup' },
    el('p', { class: 'popup-title' }, `Stop ${stop.stop}: `, el('strong', {}, stop.name)),
    el('p', { class: 'popup-sub' }, [stop.address, `${stop.city}, ${stop.state}`].filter(Boolean).join(' · ')),
    el('dl', { class: 'popup-dl' },
      el('dt', {}, 'Mile'), el('dd', {}, fmt.miles(stop.mile_marker)),
      el('dt', {}, 'Price'), el('dd', {}, `${fmt.price(stop.price_per_gallon)}/gal`,
        quotes && quotes.count > 1 ? el('small', {}, ` (${quotes.count} quotes, ${fmt.price(quotes.min)}–${fmt.price(quotes.max)})`) : null),
      el('dt', {}, 'Arrive with'), el('dd', {}, fmt.gal(stop.fuel_on_arrival_gallons)),
      el('dt', {}, 'Buy'), el('dd', {}, fmt.gal(stop.gallons)),
      el('dt', {}, 'Cost'), el('dd', {}, fmt.money(stop.cost)),
    ),
    whyText ? el('p', { class: 'popup-why' }, whyText) : null,
    el('a', { href: `https://www.openstreetmap.org/search?query=${encodeURIComponent(query)}`, target: '_blank', rel: 'noopener' }, 'Find on OpenStreetMap'),
  );
}

export function createMap(container, { onStop, why } = {}) {
  const map = L.map(container, { preferCanvas: true, scrollWheelZoom: false }).setView(USA_CENTER, USA_ZOOM);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · routing <a href="https://project-osrm.org/">OSRM</a>',
  }).addTo(map);
  // Wheel zoom only after the user clicks the map, so the page scrolls normally.
  map.on('click', () => map.scrollWheelZoom.enable());
  container.addEventListener('mouseleave', () => map.scrollWheelZoom.disable());

  let layers = [];
  let controls = [];
  let stopMarkers = new Map();
  let pendingBounds = null;

  const fit = () => {
    if (container.clientWidth === 0 || container.clientHeight === 0) return;
    map.invalidateSize();
    if (pendingBounds) {
      map.fitBounds(pendingBounds, { padding: [30, 30], animate: false });
      pendingBounds = null;
    }
  };
  new ResizeObserver(fit).observe(container);

  function reset() {
    layers.forEach((layer) => map.removeLayer(layer));
    controls.forEach((control) => map.removeControl(control));
    layers = [];
    controls = [];
    stopMarkers = new Map();
  }

  function add(layer) {
    layers.push(layer);
    return layer;
  }

  function fitTo(bounds) {
    pendingBounds = bounds;
    fit();
  }

  function legend(t, corridorMiles, count) {
    const control = L.control({ position: 'bottomright' });
    control.onAdd = () => {
      const row = (kind, label, range) =>
        el('li', {}, el('span', { class: `swatch swatch-${kind}`, 'aria-hidden': 'true' }), el('span', {}, label), el('span', { class: 'legend-range' }, range));
      return el('div', { class: 'map-legend' },
        el('p', { class: 'legend-title' }, `Price at the ${fmt.int(count)} stations within ${fmt.miles(corridorMiles)}`),
        el('ul', {},
          row('cheap', 'cheapest third', `${fmt.price(t.min)}–${fmt.price(t.t1)}`),
          row('mid', 'middle third', `${fmt.price(t.t1)}–${fmt.price(t.t2)}`),
          row('dear', 'priciest third', `${fmt.price(t.t2)}–${fmt.price(t.max)}`),
          el('li', {}, el('span', { class: 'swatch swatch-stop', 'aria-hidden': 'true' }), el('span', {}, 'plan stop')),
          el('li', {}, el('span', { class: 'swatch swatch-blind', 'aria-hidden': 'true' }), el('span', {}, 'price-blind stop')),
        ),
      );
    };
    controls.push(control);
    control.addTo(map);
  }

  function render(body, { about } = {}) {
    reset();
    const features = body.map?.geojson?.features || [];
    const line = features.find((f) => f.geometry?.type === 'LineString');
    const overlays = {};

    const routeLayer = add(L.geoJSON(line ? [line] : [], { style: { color: token('--route'), weight: 4, opacity: 0.85 } }));
    routeLayer.addTo(map);
    overlays.Route = routeLayer;

    const endpoints = add(L.layerGroup());
    for (const f of features) {
      const kind = f.properties?.kind;
      if (kind !== 'start' && kind !== 'finish') continue;
      const [lon, lat] = f.geometry.coordinates;
      L.circleMarker([lat, lon], {
        radius: 8, color: '#ffffff', weight: 2, fillOpacity: 1,
        fillColor: kind === 'start' ? token('--ok') : token('--danger'),
      })
        .bindPopup(el('div', { class: 'popup' }, el('strong', {}, kind === 'start' ? 'Start' : 'Finish'), ': ', f.properties.label))
        .addTo(endpoints);
    }
    endpoints.addTo(map);

    // Stations within the corridor (opt-in candidates layer).
    const comparison = body.summary?.comparison;
    const blindIds = new Set((comparison?.price_blind?.stops || []).map((s) => s.opis_id));
    const candidates = rowsOf(body.candidates);
    const corridorMiles = body.pipeline?.corridor?.corridor_miles ?? about?.planner?.corridor_miles;
    if (candidates.length) {
      const t = terciles(candidates.map((c) => c.price_per_gallon));
      const groups = new Map();
      for (const c of candidates) {
        const key = `${c.lat},${c.lon}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(c);
      }
      const layer = add(L.layerGroup());
      for (const group of groups.values()) {
        group.sort((a, b) => a.price_per_gallon - b.price_per_gallon);
        const best = group[0];
        const marker = L.circleMarker([best.lat, best.lon], {
          radius: 4 + Math.sqrt(group.length) * 1.6,
          color: '#ffffff', weight: 1, fillOpacity: 0.9,
          fillColor: token(`--${tercileOf(best.price_per_gallon, t)}`),
        });
        marker.bindPopup(() => el('div', { class: 'popup popup-list' },
          el('p', { class: 'popup-title' }, `${best.city}, ${best.state}: `, el('strong', {}, `${group.length} station${group.length > 1 ? 's' : ''}`)),
          el('p', { class: 'popup-sub' }, 'City-level coordinates, so they share one point.'),
          el('ol', {}, group.map((c) => el('li', {},
            el('span', { class: `swatch swatch-${tercileOf(c.price_per_gallon, t)}`, 'aria-hidden': 'true' }),
            el('strong', {}, fmt.price(c.price_per_gallon)), ' ', c.name,
            el('small', {}, ` · mile ${fmt.dec1(c.mile_marker)} · ${fmt.dec1(c.distance_from_route_miles)} mi off route`),
            c.stop ? el('span', { class: 'tag tag-stop' }, `Plan stop #${c.stop}`) : null,
            blindIds.has(c.opis_id) ? el('span', { class: 'tag tag-blind' }, 'Price-blind stop') : null,
          ))),
        ), { maxWidth: 340 });
        marker.addTo(layer);
      }
      layer.addTo(map);
      overlays[`Stations within ${fmt.miles(corridorMiles)} (${fmt.int(candidates.length)})`] = layer;
      if (t) legend(t, corridorMiles, candidates.length);
    }

    // Price-blind driver's stops: hollow squares, off by default.
    if (comparison?.price_blind?.stops?.length) {
      const layer = add(L.layerGroup());
      for (const s of comparison.price_blind.stops) {
        L.marker([s.lat, s.lon], {
          icon: L.divIcon({ className: 'pb-marker', iconSize: [14, 14], iconAnchor: [7, 7] }),
          keyboard: false,
          title: `Price-blind stop at mile ${fmt.dec1(s.mile_marker)}`,
        })
          .bindPopup(el('div', { class: 'popup' },
            el('p', { class: 'popup-title' }, el('strong', {}, 'Price-blind driver'), ` at mile ${fmt.dec1(s.mile_marker)}`),
            el('p', {}, `${fmt.gal(s.gallons)} at ${fmt.price(s.price_per_gallon)} = ${fmt.money(s.cost)}`)))
          .addTo(layer);
      }
      overlays['Price-blind driver stops'] = layer;
    }

    // The plan's stops: numbered markers, on top.
    const stopsLayer = add(L.layerGroup());
    for (const s of body.fuel_stops || []) {
      const icon = L.divIcon({
        className: 'stop-marker',
        html: el('span', {}, String(s.stop)),
        iconSize: [28, 28],
        iconAnchor: [14, 14],
        popupAnchor: [0, -12],
      });
      const marker = L.marker([s.lat, s.lon], { icon, zIndexOffset: 1000, title: `Stop ${s.stop}: ${s.name}`, alt: `Stop ${s.stop}` });
      marker.bindPopup(stopPopup(s, why ? why(s, body) : null));
      marker.on('click', () => onStop?.(s.stop, 'map'));
      marker.addTo(stopsLayer);
      stopMarkers.set(s.stop, marker);
    }
    stopsLayer.addTo(map);
    overlays['Plan fuel stops'] = stopsLayer;

    const control = L.control.layers(null, overlays, { collapsed: window.matchMedia('(max-width: 760px)').matches, position: 'topright' });
    controls.push(control);
    control.addTo(map);

    if (line) fitTo(routeLayer.getBounds());
  }

  // 422 no_reachable_fuel_station: show where the stretch without stations is.
  function renderGap(body, { maxRange } = {}) {
    reset();
    const a = body.gap_start;
    const b = body.gap_end;
    if (!a || !b) return;
    const layer = add(L.layerGroup());
    const danger = token('--danger');
    L.polyline([[a.lat, a.lon], [b.lat, b.lon]], { color: danger, weight: 4, dashArray: '8 8' }).addTo(layer);
    for (const [point, label] of [[a, 'Last station before the gap'], [b, body.next_station_mile === null ? 'Destination' : 'Next station']]) {
      L.circleMarker([point.lat, point.lon], { radius: 8, color: '#ffffff', weight: 2, fillColor: danger, fillOpacity: 1 })
        .bindPopup(el('div', { class: 'popup' }, el('strong', {}, label), ` at mile ${fmt.dec1(point.mile)}`))
        .addTo(layer);
    }
    L.tooltip({ permanent: true, direction: 'center', className: 'gap-label' })
      .setLatLng([(a.lat + b.lat) / 2, (a.lon + b.lon) / 2])
      .setContent(el('span', {}, `No station for ${fmt.miles(body.gap_miles)}${maxRange ? ` (range ${fmt.miles(maxRange)})` : ''}`))
      .addTo(layer);
    layer.addTo(map);
    fitTo(L.latLngBounds([[a.lat, a.lon], [b.lat, b.lon]]));
  }

  function showUsa() {
    reset();
    map.setView(USA_CENTER, USA_ZOOM);
  }

  function highlightStop(n) {
    const marker = stopMarkers.get(n);
    if (!marker) return;
    const rect = container.getBoundingClientRect();
    const reduce = reducedMotion();
    if (rect.bottom < 0 || rect.top > window.innerHeight) {
      container.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
    }
    map.flyTo(marker.getLatLng(), Math.max(map.getZoom(), 9), { animate: !reduce, duration: reduce ? 0 : 0.6 });
    marker.openPopup();
  }

  return { map, render, renderGap, showUsa, highlightStop, invalidate: fit };
}
