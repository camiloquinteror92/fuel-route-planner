// "How it works" tab: the pipeline of the current answer (numbers bound from
// route.pipeline in the template; the corridor funnel drawn here), the data that
// was loaded (/api/about data), and how the project was built (commit history and
// the test inventory from /api/about).

import { el, fmt, replace, codeLink, testBadge, present } from './format.js';

// Every element with data-code="<key>" links to that symbol on GitHub; every
// element with data-test="<path>::<func>" shows the test and its last result.
export function decorate(root, about) {
  for (const a of root.querySelectorAll('[data-code]')) {
    const link = codeLink(about, a.dataset.code);
    if (link) {
      a.setAttribute('href', link.url);
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener');
      a.setAttribute('title', `${link.path}${link.start_line ? ` lines ${link.start_line}–${link.end_line}` : ''}`);
      a.hidden = false;
    } else {
      a.removeAttribute('href');
      a.hidden = true;
    }
  }
  for (const node of root.querySelectorAll('[data-test]')) replace(node, testBadge(about, node.dataset.test, { short: true }));
}

export function renderFunnel(container, body) {
  const c = body?.pipeline?.corridor;
  if (!c) {
    replace(container, el('p', { class: 'muted' }, 'Plan a trip to see how many stations each pass keeps.'));
    return;
  }
  const steps = [
    ['stations searched', c.stations_searched],
    ['inside the route’s bounding box', c.in_bounding_box],
    ['after the coarse pass', c.after_coarse_pass],
    [`within ${fmt.miles(c.corridor_miles)} of the route`, c.within_corridor],
    ['candidates (inside the USA)', c.candidates],
  ];
  const top = Math.max(1, c.stations_searched || 0);
  replace(container,
    el('ol', { class: 'funnel' }, steps.map(([label, value]) => el('li', {},
      el('span', { class: 'funnel-label' }, label),
      el('span', { class: 'funnel-track' }, el('span', { class: 'funnel-bar', css: { width: `${Math.max(0.6, ((value || 0) / top) * 100)}%` } })),
      el('span', { class: 'num' }, present(value) ? fmt.int(value) : '—')))),
    c.dropped_outside_usa ? el('p', { class: 'muted small' }, `${fmt.int(c.dropped_outside_usa)} stations near a stretch outside the USA were dropped.`) : null);
}

export function renderStates(container, about) {
  const rows = about?.data?.stations_by_state;
  if (!Array.isArray(rows) || !rows.length) {
    replace(container, el('p', { class: 'muted' }, 'Per-state counts not available.'));
    return;
  }
  const top = Math.max(1, ...rows.map((r) => r.stations));
  replace(container, el('ol', { class: 'state-bars' }, rows.map((r) => el('li', { title: `${r.state}: ${r.stations} stations, ${r.geocoded} geocoded` },
    el('span', { class: 'state-code' }, r.state),
    el('span', { class: 'funnel-track' },
      el('span', { class: 'funnel-bar', css: { width: `${Math.max(0.6, (r.stations / top) * 100)}%` } }),
      el('span', { class: 'funnel-bar funnel-bar-geo', css: { width: `${Math.max(0, (r.geocoded / top) * 100)}%` } })),
    el('span', { class: 'num' }, fmt.int(r.stations))))));
}

const TYPE_LABEL = { feat: 'feature', fix: 'fix', docs: 'docs', test: 'test', refactor: 'refactor', chore: 'chore', perf: 'perf' };

export function renderTimeline(container, about) {
  const history = about?.build?.history;
  if (!Array.isArray(history) || !history.length) {
    replace(container, el('p', { class: 'muted' }, 'Commit history not available on this server.'));
    return;
  }
  replace(container, el('ol', { class: 'timeline' }, history.map((c) => el('li', {},
    el('time', { datetime: c.date }, fmt.date(c.date)),
    el('span', { class: `tag tag-type tag-${c.type || 'other'}` }, TYPE_LABEL[c.type] || c.type || 'other'),
    c.url ? el('a', { href: c.url, target: '_blank', rel: 'noopener' }, c.subject) : el('span', {}, c.subject),
    el('code', { class: 'muted' }, c.commit_short)))));
}

export function renderTestInventory(container, about) {
  const index = about?.test_index;
  if (!index || !Object.keys(index).length) {
    replace(container, el('p', { class: 'muted' }, 'Test index not available on this server.'));
    return;
  }
  const files = new Map();
  for (const [key, entry] of Object.entries(index)) {
    const path = entry.path || key.split('::')[0];
    if (!files.has(path)) files.set(path, []);
    files.get(path).push([key, entry]);
  }
  replace(container, el('div', { class: 'inventory' }, [...files.entries()].map(([path, tests]) => {
    const cases = tests.reduce((t, [, e]) => t + (e.cases || 0), 0);
    const failed = tests.reduce((t, [, e]) => t + (e.failed || 0), 0);
    return el('details', { class: 'inventory-file' },
      el('summary', {}, el('code', {}, path), ' ',
        el('span', { class: 'muted' }, `${fmt.int(tests.length)} test functions${cases ? `, ${fmt.int(cases)} cases` : ''}`),
        failed ? el('span', { class: 'badge badge-bad' }, `${fmt.int(failed)} failing`) : null),
      el('ul', {}, tests.map(([key]) => el('li', {}, testBadge(about, key, { short: true })))));
  })));
}
