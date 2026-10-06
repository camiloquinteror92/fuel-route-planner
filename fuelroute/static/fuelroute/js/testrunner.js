// Tests tab: the pytest suite, run from the page.
//
// GET /api/tests gives the inventory (every test with its one-line docstring) and the
// last run made from the page; "Run all tests" sends POST /api/tests/run, which runs
// pytest on the server in a separate process (no parameters: the client cannot choose
// what runs) and answers with every result, grouped by file. The server only allows it
// from the machine it runs on: anywhere else it answers 403 and the tab says why.
// Without the endpoint (an older server) the tab falls back to /api/about's index.

import { el, fmt, mark, plural, replace, present } from './format.js';
import { CASES } from './cases.js';

const POLL_MS = 3000;
const POLL_LIMIT = 80;
const LOCAL_ONLY = 'Running the tests from the page is only available when you run the project locally (the server accepts it only from its own machine).';

// "test_cache_hit_is_free" -> "Cache hit is free".
export function humanize(name) {
  const text = String(name || '').replace(/\[.*$/, '').replace(/^test_/, '').replace(/_/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : String(name || '');
}

function fileTitle(file) {
  const base = String(file || '').split('/').pop().replace(/^test_/, '').replace(/\.py$/, '').replace(/_/g, ' ');
  return base ? base[0].toUpperCase() + base.slice(1) : file;
}

// Groups as [{file, title, tests: [{name, description, outcome, duration_ms}]}],
// whatever the exact shape the server used (a list, or a {file: tests} object).
function groupsOf(value) {
  if (Array.isArray(value)) {
    return value.filter(Boolean).map((g) => ({
      file: g.file || g.path || g.module || '',
      title: g.title || g.description || null,
      tests: (g.tests || g.cases || []).map((t) => (typeof t === 'string' ? { name: t } : t)),
    }));
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([file, tests]) => ({
      file, title: null, tests: (Array.isArray(tests) ? tests : []).map((t) => (typeof t === 'string' ? { name: t } : t)),
    }));
  }
  return [];
}

// GET /api/tests: {runner: {available, detail, running}, last_run, inventory: {groups}}.
export function normalize(body) {
  if (!body || typeof body !== 'object') return { run: null, inventory: [], runner: null };
  const run = body.last_run ?? body.run ?? null;
  const inv = body.inventory;
  const inventory = groupsOf(Array.isArray(inv) ? inv : inv?.groups ?? inv ?? []);
  const runner = body.runner && typeof body.runner === 'object' ? body.runner : null;
  return { run: run ? { ...run, groups: groupsOf(run.groups) } : null, inventory, runner, running: runner?.running === true };
}

// The run's groups, plus the inventory's tests the run does not have ("not run").
function merged(run, inventory) {
  const byFile = new Map();
  for (const g of inventory) byFile.set(g.file, { ...g, tests: g.tests.map((t) => ({ ...t, outcome: null })) });
  for (const g of run?.groups || []) {
    const known = byFile.get(g.file);
    const described = new Map((known?.tests || []).map((t) => [t.name, t.description]));
    const names = new Set(g.tests.map((t) => t.name));
    const extra = (known?.tests || []).filter((t) => !names.has(t.name) && !g.tests.some((r) => String(r.name).startsWith(`${t.name}[`)));
    byFile.set(g.file, {
      file: g.file, title: g.title || known?.title || null,
      tests: [...g.tests.map((t) => ({ ...t, description: t.description || described.get(String(t.name).replace(/\[.*$/, '')) })), ...extra],
    });
  }
  return [...byFile.values()];
}

// Inventory from /api/about's test index, for a server without /api/tests.
function fromAbout(about) {
  const index = about?.test_index || {};
  const files = new Map();
  for (const [key, entry] of Object.entries(index)) {
    const [file, name] = key.split('::');
    if (!files.has(file)) files.set(file, []);
    files.get(file).push({ name, description: entry.description || null, line: entry.line });
  }
  return [...files.entries()].map(([file, tests]) => ({ file, title: null, tests }));
}

function outcomeMark(outcome) {
  if (outcome === 'passed') return mark(true, 'passed');
  if (outcome === 'failed' || outcome === 'error') return mark(false, outcome);
  if (outcome === 'skipped') return mark(null, 'skipped');
  return mark(null, 'not run');
}

export function createTestRunner(container, { client, getAbout, toast, onDone }) {
  let data = null; // normalized GET /api/tests
  let loaded = false;
  let running = false;
  let started = 0;
  let timer = null;
  let notice = null; // {kind, text}
  let filter = '';
  let onlyFailures = false;
  let unavailable = false; // the server has no /api/tests

  async function load() {
    const r = await client.tests();
    loaded = true;
    if (!r || r.status === 404) {
      unavailable = true;
    } else if (r.status === 403) {
      notice = { kind: 'info', text: r.body?.detail || LOCAL_ONLY };
    } else if (r.ok) {
      data = normalize(r.body);
      if (data.running && !running) {
        notice = { kind: 'warn', text: 'A run is in progress: the results appear here when it ends.' };
        poll(data.run?.ran_at ?? null);
      }
    } else {
      notice = { kind: 'bad', text: `Could not read the test inventory (HTTP ${r.status || r.failure}).` };
    }
    render();
  }

  async function poll(previous, tries = 0) {
    if (tries > POLL_LIMIT) return;
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    const r = await client.tests();
    if (r?.ok) {
      const next = normalize(r.body);
      if (!next.running && next.run && next.run.ran_at !== previous) {
        data = next;
        notice = null;
        render();
        onDone?.();
        return;
      }
    }
    poll(previous, tries + 1);
  }

  async function run() {
    if (running) return;
    running = true;
    notice = null;
    started = performance.now();
    render();
    timer = setInterval(tick, 100);
    const r = await client.runTests();
    running = false;
    clearInterval(timer);
    if (!r || r.status === 404) {
      unavailable = true;
      notice = { kind: 'info', text: 'This server cannot run the tests from the page yet: run pytest in a terminal.' };
    } else if (r.status === 403) {
      notice = { kind: 'info', text: r.body?.detail || LOCAL_ONLY };
    } else if (r.status === 409) {
      notice = { kind: 'warn', text: 'A run is already in progress (from another tab?). The results appear here when it ends.' };
      poll(data?.run?.ran_at ?? null);
    } else if (r.ok && r.body) {
      const fresh = normalize({ last_run: r.body });
      data = { ...(data || { inventory: [], runner: null }), run: fresh.run };
      const s = fresh.run;
      toast?.(`pytest: ${fmt.int(s.passed ?? 0)} passed, ${fmt.int((s.failed ?? 0) + (s.errors ?? 0))} failed`, (s.failed || s.errors) ? 'bad' : 'ok');
      onDone?.();
    } else {
      notice = { kind: 'bad', text: r.body?.detail || `The run failed (HTTP ${r.status || r.failure}).` };
    }
    render();
  }

  function runningText() {
    return `Running pytest… ${fmt.num(Math.round((performance.now() - started) / 100) / 10)} s`;
  }

  // Only the button's timer changes while pytest runs (the filter keeps its focus).
  function tick() {
    const node = container.querySelector('.btn-run .btn-timer');
    if (node) node.textContent = runningText();
  }

  function summary(runData) {
    if (!runData) return null;
    const failed = (runData.failed || 0) + (runData.errors || 0);
    const total = (runData.passed || 0) + failed + (runData.skipped || 0);
    const share = (n) => (total ? `${(n / total) * 100}%` : '0');
    return el('div', { class: 'tests-summary' },
      el('div', { class: 'kpis kpis-perf' },
        el('article', { class: `kpi kpi-small ${failed ? 'is-warn' : 'is-ok'}` }, el('h4', {}, 'Passed'), el('p', { class: 'kpi-value good' }, fmt.int(runData.passed ?? 0)),
          el('p', { class: 'kpi-sub' }, `of ${plural(total, 'test')}`)),
        el('article', { class: 'kpi kpi-small' }, el('h4', {}, 'Failed'), el('p', { class: `kpi-value ${failed ? 'bad' : ''}` }, fmt.int(failed)),
          el('p', { class: 'kpi-sub' }, runData.errors ? `${fmt.int(runData.errors)} errors included` : 'failures and errors')),
        el('article', { class: 'kpi kpi-small' }, el('h4', {}, 'Skipped'), el('p', { class: 'kpi-value' }, fmt.int(runData.skipped ?? 0))),
        el('article', { class: 'kpi kpi-small' }, el('h4', {}, 'Duration'),
          el('p', { class: 'kpi-value' }, present(runData.duration_s) ? `${fmt.num(runData.duration_s)} s` : '—'),
          el('p', { class: 'kpi-sub' }, present(runData.ran_at) ? `${fmt.datetime(runData.ran_at)} (${fmt.ago(runData.ran_at)})` : ''))),
      el('div', { class: 'tests-bar', role: 'img', 'aria-label': `${fmt.int(runData.passed ?? 0)} passed, ${fmt.int(failed)} failed, ${fmt.int(runData.skipped ?? 0)} skipped` },
        el('span', { class: 'tests-bar-pass', css: { width: share(runData.passed || 0) } }),
        el('span', { class: 'tests-bar-fail', css: { width: share(failed) } }),
        el('span', { class: 'tests-bar-skip', css: { width: share(runData.skipped || 0) } })));
  }

  function groupNode(g, runData) {
    const needle = filter.trim().toLowerCase();
    const tests = g.tests.filter((t) => {
      if (onlyFailures && !['failed', 'error'].includes(t.outcome)) return false;
      if (!needle) return true;
      return `${t.name} ${t.description || ''} ${g.file} ${g.title || ''}`.toLowerCase().includes(needle);
    });
    if (!tests.length) return null;
    const failed = g.tests.filter((t) => ['failed', 'error'].includes(t.outcome)).length;
    const passed = g.tests.filter((t) => t.outcome === 'passed').length;
    const ran = g.tests.filter((t) => t.outcome).length;
    const ms = g.tests.reduce((total, t) => total + (Number(t.duration_ms) || 0), 0);
    let badge;
    if (failed) badge = el('span', { class: 'badge badge-bad' }, `${fmt.int(failed)} failing`);
    else if (ran) badge = el('span', { class: 'badge badge-ok' }, `${fmt.int(passed)}/${fmt.int(ran)} passed`);
    else badge = el('span', { class: 'badge badge-na' }, runData ? `${plural(g.tests.length, 'test')}, not in this run` : plural(g.tests.length, 'test'));
    return el('details', { class: `test-group${failed ? ' has-failures' : ''}`, open: Boolean(failed || needle || onlyFailures) },
      el('summary', {},
        el('span', { class: 'test-group-title' }, g.title || fileTitle(g.file)),
        el('code', { class: 'muted' }, g.file),
        el('span', { class: 'test-group-count' }, badge,
          ran && ran < g.tests.length ? el('small', { class: 'muted' }, ` ${fmt.int(g.tests.length - ran)} not run`) : null,
          ms ? el('small', { class: 'muted' }, ` ${fmt.ms(ms)}`) : null)),
      el('ul', { class: 'test-list' }, tests.map((t) => el('li', { class: `test-item${['failed', 'error'].includes(t.outcome) ? ' is-bad' : ''}` },
        outcomeMark(t.outcome),
        el('span', { class: 'test-desc' }, t.description || humanize(t.name),
          el('code', { class: 'test-name' }, t.name)),
        present(t.duration_ms) ? el('span', { class: 'num muted small' }, fmt.ms(t.duration_ms)) : null,
        t.message ? el('pre', { class: 'code test-message' }, t.message) : null))));
  }

  function render() {
    const about = getAbout() || {};
    const runData = data?.run || null;
    const inventory = unavailable || !data ? fromAbout(about) : (data.inventory.length ? data.inventory : fromAbout(about));
    const groups = merged(runData, inventory);
    const count = groups.reduce((total, g) => total + g.tests.length, 0);
    const lastDuration = runData?.duration_s;
    const runner = data?.runner;
    const blocked = runner && runner.available === false;
    const button = el('button', { type: 'button', class: 'btn btn-run', disabled: running || unavailable || blocked, 'aria-busy': String(running), onclick: run },
      running ? el('span', { class: 'btn-timer' }, runningText()) : 'Run all tests');
    const head = el('div', { class: 'tests-head' }, button,
      el('p', { class: 'muted small' },
        running && present(lastDuration) ? `The last run took ${fmt.num(lastDuration)} s. ` : '',
        unavailable
          ? 'This server has no test runner endpoint: run pytest in a terminal. The list below comes from the last report.'
          : `pytest runs in a separate process on the server, with the routing service faked: no network. ${plural(count, 'test')} below, each with what it proves.`,
        runner?.command ? [' ', el('code', {}, runner.command)] : null),
      blocked ? el('p', { class: 'banner banner-info runner-off', role: 'note' }, el('span', { class: 'banner-icon', 'aria-hidden': 'true' }),
        el('span', {}, runner.detail || LOCAL_ONLY)) : null);
    const about_tests = about.tests || {};
    const source = runData?.source === 'report_file'
      ? el('p', { class: 'muted small' }, 'These are the results of the last pytest run from a terminal on this machine; Run all tests runs them again here.')
      : null;
    const tail = runData && runData.exit_code && !((runData.failed || 0) + (runData.errors || 0)) && (runData.output_tail || []).length
      ? el('pre', { class: 'code' }, runData.output_tail.join(String.fromCharCode(10)))
      : null;
    const terminalRun = !runData && about_tests.report === 'found'
      ? el('p', { class: 'muted small' }, `Last run from a terminal on this machine: ${fmt.int(about_tests.passed)} passed, ${fmt.int(about_tests.failed)} failed, ${fmt.ago(about_tests.ran_at)}.`)
      : null;
    const filters = el('div', { class: 'tests-filters' },
      el('label', { for: 'tests-filter', class: 'sr-only' }, 'Find a test'),
      el('input', { id: 'tests-filter', type: 'search', placeholder: 'Find a test: canada, cache, cent…', value: filter, oninput: (event) => { filter = event.target.value; renderGroups(); } }),
      runData && ((runData.failed || 0) + (runData.errors || 0)) > 0
        ? el('label', { class: 'check-inline' }, el('input', { type: 'checkbox', checked: onlyFailures, onchange: (event) => { onlyFailures = event.target.checked; renderGroups(); } }), ' only failures')
        : null);
    const list = el('div', { class: 'test-groups' });
    function renderGroups() {
      const nodes = groups.map((g) => groupNode(g, runData)).filter(Boolean);
      replace(list, nodes.length ? nodes : el('p', { class: 'muted' }, loaded || unavailable ? 'No test matches.' : 'Loading the inventory…'));
    }
    renderGroups();
    const focusFilter = document.activeElement?.id === 'tests-filter';
    replace(container,
      head,
      notice ? el('p', { class: `banner banner-${notice.kind === 'bad' ? 'warn' : notice.kind === 'warn' ? 'warn' : 'info'}`, role: 'status' },
        el('span', { class: 'banner-icon', 'aria-hidden': 'true' }), el('span', {}, notice.text)) : null,
      summary(runData),
      source,
      tail,
      terminalRun,
      filters,
      list,
      el('h3', {}, 'Live edge cases'),
      el('p', {}, `${plural(CASES.length, 'edge case')} run as real requests against this server, each with the answer it must get: `,
        el('a', { href: '#requirements' }, 'Requirements › Edge cases'), '.'));
    if (focusFilter) {
      const input = container.querySelector('#tests-filter');
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }

  return {
    render,
    ensureLoaded() {
      if (!loaded) load();
      else render();
    },
    isRunning: () => running,
  };
}
