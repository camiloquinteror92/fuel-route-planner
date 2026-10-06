// API tab: the request behind what the page shows (GET, curl, POST body), the
// raw answer (status, headers, collapsible JSON), the reference published by
// /api/about (parameters, endpoints) and the error catalog with live "Try it".

import { el, fmt, plural, replace, present } from './format.js';
import { absolute, routeUrl, tripBody } from './api.js';
import { CASE_FOR_ERROR } from './cases.js';

const COLLAPSE_OVER = 20;

function copyButton(text, toast, label = 'Copy') {
  return el('button', {
    type: 'button', class: 'btn btn-small btn-ghost',
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast('Copied');
      } catch {
        toast('Copy failed: the browser blocked the clipboard.', 'bad');
      }
    },
  }, label);
}

function scalar(value) {
  if (value === null) return el('span', { class: 'j-null' }, 'null');
  if (typeof value === 'string') return el('span', { class: 'j-str' }, JSON.stringify(value));
  if (typeof value === 'number') return el('span', { class: 'j-num' }, String(value));
  if (typeof value === 'boolean') return el('span', { class: 'j-bool' }, String(value));
  return el('span', {}, String(value));
}

// Collapsible JSON tree. Big arrays (route coordinates, candidate rows) start as
// "[n items] show" so the DOM stays small until asked.
function tree(value, key, depth, path) {
  const label = key !== null ? el('span', { class: 'j-key' }, `${JSON.stringify(key)}: `) : null;
  if (value === null || typeof value !== 'object') return el('div', { class: 'j-row' }, label, scalar(value));
  const isArray = Array.isArray(value);
  const entries = isArray ? value.map((v, i) => [i, v]) : Object.entries(value);
  const open = isArray ? '[' : '{';
  const close = isArray ? ']' : '}';
  const size = isArray ? plural(entries.length, 'item') : plural(entries.length, 'key');
  const big = isArray && (entries.length > COLLAPSE_OVER || path.endsWith('candidates.rows'));
  const details = el('details', { class: 'j-node' });
  if (depth < 2 && !big) details.open = true;
  const summary = el('summary', {}, label, el('span', { class: 'j-brace' }, open), el('span', { class: 'j-size muted' }, ` ${size} `), el('span', { class: 'j-brace' }, close));
  details.append(summary);
  const fill = () => {
    if (details.dataset.filled) return;
    details.dataset.filled = '1';
    const children = el('div', { class: 'j-children' }, entries.map(([k, v]) => tree(v, isArray ? null : k, depth + 1, `${path}.${k}`)));
    details.append(children);
  };
  if (details.open) fill();
  details.addEventListener('toggle', () => {
    if (details.open) fill();
  });
  return details;
}

export function renderRequest(container, state, { routeBase, toast, onPost }) {
  if (!state.params) {
    replace(container, el('p', { class: 'muted' }, 'Plan a trip: this section shows the exact request behind it, ready for Postman or curl.'));
    return;
  }
  const getUrl = absolute(routeUrl(routeBase, state.params));
  const curl = `curl "${getUrl}"`;
  const post = JSON.stringify(tripBody(state.params), null, 2);
  const about = state.about || {};
  const build = about.build || {};
  const ref = build.linked_commit || (build.commit ? null : 'main');
  replace(container,
    el('div', { class: 'req-block' }, el('h4', {}, 'GET'), el('pre', { class: 'code' }, getUrl), copyButton(getUrl, toast, 'Copy URL'),
      el('a', { class: 'btn btn-small btn-ghost', href: getUrl, target: '_blank', rel: 'noopener' }, 'Open')),
    el('div', { class: 'req-block' }, el('h4', {}, 'curl'), el('pre', { class: 'code' }, curl), copyButton(curl, toast, 'Copy curl')),
    el('div', { class: 'req-block' }, el('h4', {}, `POST ${routeBase} (Content-Type: application/json)`), el('pre', { class: 'code' }, post), copyButton(post, toast, 'Copy body'),
      el('button', { type: 'button', class: 'btn btn-small', onclick: onPost }, 'Send as POST')),
    el('p', { class: 'muted small' }, 'The page itself adds ', el('code', {}, 'include=candidates'),
      ' to draw the stations near the route; it shares the plan cache, so it never costs another routing call. ',
      build.repo_url && ref ? el('a', { href: `${build.repo_url}/blob/${ref}/postman/collection.json`, target: '_blank', rel: 'noopener' }, 'Postman collection') : null));
}

export function renderResponse(container, state) {
  const r = state.route;
  if (!r) {
    replace(container, el('p', { class: 'muted' }, 'No answer yet.'));
    return;
  }
  const headers = Object.entries(r.headers.raw || {}).filter(([, v]) => present(v));
  replace(container,
    el('p', { class: 'status-line' }, el('code', {}, `${r.method} ${new URL(r.url).pathname}${new URL(r.url).search}`), ' → ',
      el('strong', { class: r.ok ? 'good' : 'bad' }, r.status ? `HTTP ${r.status}` : r.failure || 'no answer'),
      ` · ${fmt.ms(r.client.roundTripMs)} round trip`),
    headers.length ? el('dl', { class: 'headers' }, headers.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, el('code', {}, v))])) : null,
    r.body !== null && typeof r.body === 'object'
      ? el('div', { class: 'json-view' }, tree(r.body, null, 0, ''))
      : el('pre', { class: 'code' }, r.text || r.message || ''));
}

export function renderReference(container, about) {
  const params = about?.api?.params;
  const endpoints = about?.endpoints;
  const include = about?.api?.include_values;
  replace(container,
    el('h4', {}, 'Parameters of /api/route'),
    Array.isArray(params) && params.length
      ? el('div', { class: 'table-wrap' }, el('table', { class: 'data cards compact' },
        el('thead', {}, el('tr', {}, ['Name', 'Required', 'Values', 'Description'].map((h) => el('th', { scope: 'col' }, h)))),
        el('tbody', {}, params.map((p) => el('tr', {},
          el('td', { 'data-label': 'Name' }, el('code', {}, p.name)),
          el('td', { 'data-label': 'Required' }, p.required ? 'yes' : 'no'),
          el('td', { 'data-label': 'Values' }, p.name === 'include' && include ? include.join(', ') : Array.isArray(p.choices) ? p.choices.join(' | ') : '—'),
          el('td', { 'data-label': 'Description' }, p.help_text || ''))))))
      : el('p', { class: 'muted' }, 'Not published by this server.'),
    el('h4', {}, 'Endpoints'),
    Array.isArray(endpoints) && endpoints.length
      ? el('div', { class: 'table-wrap' }, el('table', { class: 'data cards compact' },
        el('thead', {}, el('tr', {}, ['Method', 'Path', 'Description'].map((h) => el('th', { scope: 'col' }, h)))),
        el('tbody', {}, endpoints.map((e) => el('tr', {},
          el('td', { 'data-label': 'Method' }, el('code', {}, e.method)),
          el('td', { 'data-label': 'Path' }, el('code', {}, e.path)),
          el('td', { 'data-label': 'Description' }, e.description))))))
      : el('p', { class: 'muted' }, 'Not published by this server.'));
}

export function renderErrors(container, about, { onTry, results }) {
  const errors = about?.errors;
  if (!Array.isArray(errors) || !errors.length) {
    replace(container, el('p', { class: 'muted' }, 'Not published by this server.'));
    return;
  }
  replace(container, el('div', { class: 'table-wrap' }, el('table', { class: 'data cards compact errors-table' },
    el('thead', {}, el('tr', {}, ['Status', 'Code', 'When', 'Live'].map((h) => el('th', { scope: 'col' }, h)))),
    el('tbody', {}, [...errors].sort((a, b) => a.status - b.status || a.code.localeCompare(b.code)).map((e) => {
      const caseId = CASE_FOR_ERROR[e.code];
      const res = caseId ? results.get(caseId) : null;
      const calls = res?.r?.body?.meta?.external_api_calls;
      return el('tr', {},
        el('td', { 'data-label': 'Status', class: 'num' }, String(e.status)),
        el('td', { 'data-label': 'Code' }, el('code', {}, e.code)),
        el('td', { 'data-label': 'When' }, e.description),
        el('td', { 'data-label': 'Live' }, caseId
          ? [el('button', { type: 'button', class: 'btn btn-small btn-ghost', onclick: () => onTry(caseId) }, 'Try it'),
            res?.r ? el('small', { class: res.ok ? 'good' : 'bad' }, ` HTTP ${res.r.status}${present(calls) ? `, ${plural(calls, 'external call')}` : ''}`) : null]
          : el('span', { class: 'muted' }, '—')));
    })))));
}
