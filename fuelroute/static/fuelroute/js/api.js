// The page's only way to talk to the server: the same public JSON endpoints that
// Postman and curl use. The page asks GET /api/route with exactly the parameters a
// Postman request would carry: start, finish, start_tank and the truck settings that
// differ from the standard truck.

import { writeSettings } from './settings.js';

// request('GET', url) -> {ok, status, body, text, url, serverMs, retryAfter, failure}
// Never throws for HTTP or network errors (status 0 + failure: 'network' | 'timeout');
// throws only when the request was aborted because a newer one replaced it.
export async function request(method, url, { signal } = {}) {
  const target = absolute(url);
  let response;
  let text = '';
  try {
    response = await fetch(target, { method, headers: { Accept: 'application/json' }, signal, cache: 'no-store', credentials: 'same-origin' });
    text = await response.text();
  } catch (error) {
    if (signal?.aborted && signal.reason !== 'timeout') throw error;
    return {
      ok: false, status: 0, body: null, text: '', url: target, serverMs: null, retryAfter: null,
      failure: signal?.aborted ? 'timeout' : 'network',
    };
  }
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  const serverMs = response.headers.get('X-Response-Time-ms');
  return {
    ok: response.ok,
    status: response.status,
    body,
    text,
    url: target,
    serverMs: serverMs !== null && serverMs !== '' ? Number(serverMs) : null,
    retryAfter: response.headers.get('Retry-After'),
    failure: null,
  };
}

// start, finish, start_tank and the truck settings that differ from the standard truck.
export function tripQuery(params) {
  const query = new URLSearchParams();
  query.set('start', params.start);
  query.set('finish', params.finish);
  query.set('start_tank', params.start_tank === 'full' ? 'full' : 'empty');
  return writeSettings(query, params.settings);
}

// /api/route?start&finish&start_tank[&settings]: the exact request Postman sends.
export function routeUrl(base, params) {
  return `${base}?${tripQuery(params)}`;
}

export function absolute(url) {
  return new URL(url, window.location.href).href;
}

export function createClient(config) {
  const endpoints = config.api || {};
  return {
    plan(params, { signal } = {}) {
      return request('GET', routeUrl(endpoints.route, params), { signal });
    },
    about() {
      return endpoints.about ? request('GET', endpoints.about) : Promise.resolve(null);
    },
    // City suggestions from the server's built-in list of US places (0 external calls).
    places(q, { limit, signal } = {}) {
      if (!endpoints.places) return Promise.resolve(null);
      const query = new URLSearchParams({ q });
      if (limit) query.set('limit', String(limit));
      return request('GET', `${endpoints.places}?${query}`, { signal });
    },
  };
}
