// The page's only way to talk to the server: the same public JSON endpoints that
// Postman and curl use. Every request is measured (Resource Timing, with a
// performance.now() fallback), its Server-Timing header is parsed, and it is
// appended to this session's network log (kept in memory only).

export const session = [];
const listeners = new Set();

export function onLog(listener) {
  listeners.add(listener);
}

export function clearSession() {
  session.length = 0;
  listeners.forEach((fn) => fn(session));
}

try {
  performance.setResourceTimingBufferSize(1000);
  performance.addEventListener('resourcetimingbufferfull', () => performance.clearResourceTimings());
} catch {
  // Old browsers: the fallback timings are used.
}

export function parseServerTiming(header) {
  if (!header) return [];
  return header
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [name, ...params] = part.split(';').map((s) => s.trim());
      const entry = { name, dur: null, desc: null };
      for (const param of params) {
        const eq = param.indexOf('=');
        if (eq < 0) continue;
        const key = param.slice(0, eq).trim().toLowerCase();
        let value = param.slice(eq + 1).trim();
        if (value.startsWith('"') && value.endsWith('"')) value = value.slice(1, -1);
        if (key === 'dur') entry.dur = Number(value);
        else if (key === 'desc') entry.desc = value;
      }
      return entry;
    });
}

async function resourceEntry(url, since) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const entries = performance.getEntriesByName(url, 'resource').filter((e) => e.startTime >= since - 1);
    if (entries.length) return entries[entries.length - 1];
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return null;
}

function kindOf(pathname) {
  if (/\/api\/route\/?$/.test(pathname)) return 'route';
  if (/\/api\/stats\/?$/.test(pathname)) return 'stats';
  if (/\/api\/about\/?$/.test(pathname)) return 'about';
  return 'other';
}

function record(result) {
  const meta = result.body && typeof result.body === 'object' ? result.body.meta : null;
  const services = meta?.external_api_services || [];
  const url = new URL(result.url);
  session.unshift({
    at: result.at,
    method: result.method,
    path: url.pathname,
    query: url.search,
    kind: kindOf(url.pathname),
    trip: result.trip,
    origin: result.origin,
    status: result.status,
    error: result.failure || result.body?.error || null,
    calls: meta ? meta.external_api_calls : null,
    osrmCalls: services.filter((s) => s === 'osrm').length,
    planCache: meta?.plan_cache ?? null,
    routeCache: meta?.route_cache ?? null,
    serverMs: result.headers.responseTimeMs,
    roundTripMs: result.client.roundTripMs,
    bytes: result.client.transferBytes ?? result.client.decodedBytes,
  });
  listeners.forEach((fn) => fn(session));
}

function emptyHeaders() {
  return { responseTimeMs: null, serverTiming: [], retryAfter: null, contentType: null, contentLength: null, contentEncoding: null };
}

// request('GET', url) -> {ok, status, body, text, url, method, headers, client, ...}
// Never throws for HTTP or network errors (status 0 + failure); throws only when
// the request was aborted because a newer one replaced it.
export async function request(method, url, options = {}) {
  const { body, rawBody, contentType, signal, origin = 'user', trip = '' } = options;
  const absolute = new URL(url, window.location.href).href;
  const init = { method, headers: { Accept: 'application/json' }, signal, cache: 'no-store', credentials: 'same-origin' };
  if (rawBody !== undefined) {
    init.body = rawBody;
    init.headers['Content-Type'] = contentType || 'application/json';
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers['Content-Type'] = 'application/json';
  }
  const at = new Date();
  const started = performance.now();
  let response;
  let text = '';
  try {
    response = await fetch(absolute, init);
    text = await response.text();
  } catch (error) {
    if (signal?.aborted && signal.reason !== 'timeout') throw error;
    const failed = {
      ok: false, status: 0, body: null, text: '', url: absolute, method, at, origin, trip, requestBody: init.body ?? null,
      failure: signal?.aborted ? 'timeout' : 'network', message: String(error?.message || error),
      headers: emptyHeaders(),
      client: { roundTripMs: performance.now() - started, ttfbMs: null, transferBytes: null, encodedBytes: null, decodedBytes: null, parseMs: null, source: 'performance.now' },
    };
    record(failed);
    return failed;
  }
  const ended = performance.now();
  const parseStart = performance.now();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  const parseMs = performance.now() - parseStart;
  const entry = await resourceEntry(absolute, started);
  const h = response.headers;
  const serverMs = h.get('X-Response-Time-ms');
  const result = {
    ok: response.ok,
    status: response.status,
    body: parsed,
    text,
    url: absolute,
    method,
    at,
    origin,
    trip,
    requestBody: init.body ?? null,
    headers: {
      responseTimeMs: serverMs !== null && serverMs !== '' ? Number(serverMs) : null,
      serverTiming: parseServerTiming(h.get('Server-Timing')),
      retryAfter: h.get('Retry-After'),
      contentType: h.get('Content-Type'),
      contentLength: h.get('Content-Length'),
      contentEncoding: h.get('Content-Encoding'),
      raw: {
        'Content-Type': h.get('Content-Type'), 'Content-Encoding': h.get('Content-Encoding'), 'X-Response-Time-ms': serverMs,
        'Server-Timing': h.get('Server-Timing'), 'Retry-After': h.get('Retry-After'),
      },
    },
    client: entry
      ? {
          roundTripMs: entry.duration,
          ttfbMs: entry.responseStart > 0 ? entry.responseStart - entry.startTime : null,
          // transferSize: what travelled, headers included; encodedBodySize: the body as
          // sent (gzip); decodedBodySize: the body after decompression.
          transferBytes: entry.transferSize || null,
          encodedBytes: entry.encodedBodySize || null,
          decodedBytes: entry.decodedBodySize || new Blob([text]).size,
          parseMs,
          source: 'resource-timing',
        }
      : { roundTripMs: ended - started, ttfbMs: null, transferBytes: null, encodedBytes: null, decodedBytes: new Blob([text]).size, parseMs, source: 'performance.now' },
  };
  record(result);
  return result;
}

export function tripLabel(params) {
  return `${params.start} → ${params.finish}${params.start_tank === 'full' ? ' (full tank)' : ''}`;
}

// /api/route?start&finish&start_tank[&include]: the exact request Postman sends
// (include only adds the map layer of nearby stations; it shares the plan cache).
export function routeUrl(base, params, include = []) {
  const query = new URLSearchParams();
  query.set('start', params.start);
  query.set('finish', params.finish);
  query.set('start_tank', params.start_tank === 'full' ? 'full' : 'empty');
  if (include.length) query.set('include', include.join(','));
  return `${base}?${query}`;
}

export function absolute(url) {
  return new URL(url, window.location.href).href;
}

export function createClient(config) {
  const endpoints = config.api || {};
  return {
    endpoints,
    plan(params, { include = [], signal, origin = 'user' } = {}) {
      return request('GET', routeUrl(endpoints.route, params, include), { signal, origin, trip: tripLabel(params) });
    },
    planPost(params, { signal, origin = 'user' } = {}) {
      const body = { start: params.start, finish: params.finish, start_tank: params.start_tank === 'full' ? 'full' : 'empty' };
      return request('POST', endpoints.route, { body, signal, origin, trip: tripLabel(params) });
    },
    stats() {
      return endpoints.stats ? request('GET', endpoints.stats, { origin: 'page' }) : Promise.resolve(null);
    },
    about() {
      return endpoints.about ? request('GET', endpoints.about, { origin: 'page' }) : Promise.resolve(null);
    },
    // N sequential GETs of the same URL (no include: Postman's request). Stops at
    // the first answer that needed an external call or was rate limited.
    async benchmark(url, n, onProgress, signal) {
      try {
        performance.clearResourceTimings();
      } catch {
        // ignore
      }
      const results = [];
      let stopped = null;
      for (let i = 0; i < n; i++) {
        if (signal?.aborted) {
          stopped = { reason: 'cancelled' };
          break;
        }
        const r = await request('GET', url, { origin: 'benchmark', trip: 'benchmark' });
        results.push(r);
        onProgress?.(i + 1, n, r);
        if (r.status === 429) {
          stopped = { reason: 'rate_limited', retryAfter: Number(r.headers.retryAfter) || null };
          break;
        }
        if (!r.ok) {
          stopped = { reason: 'error', status: r.status };
          break;
        }
        if ((r.body?.meta?.external_api_calls || 0) > 0) {
          stopped = { reason: 'external_call' };
          break;
        }
      }
      return { results, stopped };
    },
  };
}
