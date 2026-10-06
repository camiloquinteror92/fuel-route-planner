// Requirements tab: each requirement of the brief (explicit, and implicit "between
// the lines") with how it is met, the live evidence from THIS page's answers, the
// code that does it and the tests that check it; then the contract checks
// recomputed in the browser and the live edge-case suite.

import { el, fmt, mark, replace, codeLink, testBadge, present } from './format.js';

function line(ok, ...content) {
  return el('li', { class: 'ev' }, mark(ok, ''), el('span', {}, ...content));
}

function info(...content) {
  return el('li', { class: 'ev ev-info' }, el('span', {}, ...content));
}

function pct(stat) {
  return stat ? `p50 ${fmt.ms(stat.p50)} · p95 ${fmt.ms(stat.p95)} (${fmt.int(stat.count)})` : 'no request yet';
}

function callsLabel(n) {
  if (n === 0) return 'cached';
  if (n === 1) return 'ideal';
  if (n <= 3) return 'acceptable';
  return 'over budget';
}

// Live evidence for each requirement id, from the current state.
function evidence(id, ctx) {
  const { body, result, stats, about, checks, derived, casesSummary, onTry } = ctx;
  const byId = Object.fromEntries((checks || []).map((c) => [c.id, c]));
  const need = (text) => [info(el('span', { class: 'muted' }, text))];
  const plan = (fn) => (body ? fn() : need('Plan a trip to see the live evidence.'));
  switch (id) {
    case 'usa_inputs':
      return [
        ...(body ? [info(`start resolved by `, el('code', {}, body.start.geocoder), `, finish by `, el('code', {}, body.finish.geocoder))] : []),
        stats ? info(`${fmt.int(stats.route_requests?.errors_without_external_calls ?? 0)} rejected requests cost no external call (since server start)`) : null,
        el('li', { class: 'ev ev-action' }, el('button', { type: 'button', class: 'btn btn-small', onclick: () => onTry('paris') }, 'Try it: Paris → Austin')),
      ].filter(Boolean);
    case 'route_map':
      return plan(() => [
        line(derived.markers_match, `${fmt.int(derived.route_points)} route points; ${fmt.int(derived.stop_markers)} stop markers for ${fmt.int(body.summary.number_of_stops)} stops`),
        info(el('a', { href: body.map.map_url }, 'map_url of this answer')),
      ]);
    case 'cost_effective_stops':
      return plan(() => [
        byId.optimum_vs_blind ? line(byId.optimum_vs_blind.ok, `optimum ${byId.optimum_vs_blind.actual} vs price-blind ${byId.optimum_vs_blind.expected}`) : null,
        derived.has_savings ? info(`this plan saves ${fmt.money_signed(derived.savings_amount)} (${fmt.pct(derived.savings_pct)}) vs a price-blind driver`) : null,
        present(derived.consolidation_extra_cost) ? info(`consolidation: ${fmt.money_signed(derived.consolidation_extra_cost)} for ${fmt.int(derived.consolidation_fewer_stops)} fewer stops`) : null,
      ].filter(Boolean));
    case 'range_multiple_stops':
      return plan(() => [
        line(derived.stretch_ok, `longest stretch between purchases ${fmt.miles(derived.longest_stretch)} (range ${fmt.miles(body.vehicle.max_range_miles)})`),
        line(derived.tank_ok, `tank between ${fmt.gal(derived.tank_min)} and ${fmt.gal(derived.tank_max)} (capacity ${fmt.gal(body.vehicle.tank_gallons)})`),
        info(`${fmt.int(body.summary.number_of_stops)} stops over ${fmt.miles(body.route.distance_miles)}`),
      ]);
    case 'total_cost_mpg':
      return plan(() => [
        byId.sum_costs ? line(byId.sum_costs.ok, `stop costs add up to ${byId.sum_costs.expected}`) : null,
        byId.every_mile_paid && byId.every_mile_paid.ok !== null
          ? line(byId.every_mile_paid.ok, `${fmt.gal(body.summary.total_gallons_purchased)} bought = ${fmt.miles(body.route.distance_miles)} ÷ ${fmt.num(body.vehicle.miles_per_gallon)} mpg`)
          : info(`${fmt.gal(body.summary.total_gallons_purchased)} bought; ${fmt.gal(derived.fuel_needed_gal)} burned at ${fmt.num(body.vehicle.miles_per_gallon)} mpg`),
      ].filter(Boolean));
    case 'price_file': {
      const data = about.data;
      if (!data) return need('Station data summary not available.');
      return [
        info(`${fmt.int(data.stations)} stations from ${fmt.int(data.price_quotes)} price quotes (${fmt.int(data.stations_with_several_quotes)} stations quoted several times)`),
        info(`${fmt.int(data.geocoded)} geocoded offline, ${fmt.int(data.not_geocoded)} left out`),
      ];
    }
    case 'free_routing_api':
      return [
        body ? info(`this answer: ${derived.services_text}`) : null,
        ...(about.external_services || []).map((s) => info(el('a', { href: s.url, target: '_blank', rel: 'noopener' }, s.name), `: ${s.purpose}`)),
      ].filter(Boolean);
    case 'django_version': {
      const running = about.versions?.django;
      const pinned = about.pinned?.Django;
      return [line(running && pinned ? running === pinned : null, `running ${running || '—'}, pinned ${pinned || '—'}`)];
    }
    case 'fast': {
      const meta = result?.body?.meta;
      return [
        result?.headers?.responseTimeMs !== null && result?.headers?.responseTimeMs !== undefined
          ? info(`this answer: server ${fmt.ms(result.headers.responseTimeMs)}, external ${meta ? fmt.ms(meta.external_api_ms ?? 0) : '—'}, our code ${fmt.ms(derived.our_code_ms)}`)
          : null,
        stats ? info(`since start: cold ${pct(stats.latency_ms?.cold)}; plan cache ${pct(stats.latency_ms?.plan_cache_hit)}`) : null,
      ].filter(Boolean).concat(body || stats ? [] : need('Plan a trip to measure.'));
    }
    case 'few_routing_calls': {
      const meta = result?.body?.meta;
      const ext = stats?.external_api;
      return [
        meta ? line(meta.external_api_calls <= 3, `this answer: ${fmt.int(meta.external_api_calls)} external call(s): ${callsLabel(meta.external_api_calls)}`) : null,
        derived.session_new_trips ? info(`this page: ${fmt.int(derived.session_osrm_calls)} OSRM calls for ${fmt.int(derived.session_new_trips)} new trips`) : null,
        ext ? info(`since start: ${present(ext.osrm_calls_per_routing_request) ? fmt.num(ext.osrm_calls_per_routing_request) : '—'} OSRM calls per routing request, at most ${fmt.int(ext.max_calls_in_one_request)} in one request`) : null,
      ].filter(Boolean).concat(meta || ext ? [] : need('Plan a trip to count.'));
    }
    case 'deliverables': {
      const build = about.build || {};
      return [
        build.repo_url ? info(el('a', { href: build.repo_url, target: '_blank', rel: 'noopener' }, 'GitHub repository'),
          build.commit_short ? ` @ ${build.commit_short}` : '', build.pushed === false ? ' (not pushed yet)' : '', build.dirty ? ' + local changes' : '') : null,
        derived.postman_url ? info(el('a', { href: derived.postman_url, target: '_blank', rel: 'noopener' }, 'postman/collection.json')) : null,
      ].filter(Boolean);
    }
    case 'exact_money':
      return plan(() => ['sum_costs', 'stop_costs', 'sum_gallons'].map((k) => byId[k]).filter(Boolean).map((c) => line(c.ok, c.label)));
    case 'robust_errors': {
      const errors = stats?.route_requests?.errors || {};
      const codes = Object.entries(errors);
      return [
        casesSummary.done ? line(casesSummary.ok === casesSummary.done, `${casesSummary.ok}/${casesSummary.done} edge cases behave as expected`) : info('Run the edge cases below.'),
        codes.length ? info(`errors since start: ${codes.map(([k, v]) => `${k} × ${v}`).join(', ')}`) : null,
      ].filter(Boolean);
    }
    case 'messy_data': {
      const data = about.data;
      if (!data) return need('Station data summary not available.');
      return [
        info(`${fmt.int(data.not_geocoded)} stations without reliable coordinates left out instead of guessed`),
        info(`${fmt.int(data.stations_with_several_quotes)} stations listed several times: priced with the ${about.planner?.price_policy || 'policy'} quote`),
      ];
    }
    case 'production_ready': {
      const p = about.planner || {};
      return [
        info(`plan cache ${present(p.plan_cache_seconds) ? fmt.duration(p.plan_cache_seconds) : '—'}, up to ${present(p.cache_max_entries) ? fmt.int(p.cache_max_entries) : '—'} entries; single flight per route`),
        info(`timeouts ${present(p.http_connect_timeout_seconds) ? `${fmt.num(p.http_connect_timeout_seconds)} s connect / ${fmt.num(p.http_read_timeout_seconds)} s read` : '—'}, ${present(p.http_retries) ? fmt.int(p.http_retries) : '—'} retry`),
        info(`rate limit ${present(p.rate_limit_per_minute) ? fmt.int(p.rate_limit_per_minute) : '—'} requests per minute per IP; Server-Timing on every answer`),
      ];
    }
    case 'tested': {
      const t = about.tests || {};
      if (t.report !== 'found') return need('No test report yet: run pytest.');
      return [
        line(t.failed === 0 && t.errors === 0, `${fmt.int(t.passed)} passed, ${fmt.int(t.failed)} failed, ${fmt.int(t.skipped || 0)} skipped in ${fmt.num(t.duration_seconds)} s`),
        t.stale ? line(false, 'the code changed after this report: run pytest again') : info(`last run ${fmt.ago(t.ran_at)}`),
      ];
    }
    default:
      return [];
  }
}

export function renderHeader(container, ctx) {
  const t = ctx.about.tests || {};
  replace(container,
    el('p', {}, el('strong', {}, ctx.derived.checks_total ? `${ctx.derived.checks_passed}/${ctx.derived.checks_total} contract checks pass for this plan` : 'Plan a trip to run the contract checks.')),
    t.report === 'found'
      ? el('p', {}, `Last pytest run on this machine: ${fmt.int(t.passed)} passed, ${fmt.int(t.failed)} failed${t.errors ? `, ${fmt.int(t.errors)} errors` : ''}, ${fmt.ago(t.ran_at)}.`,
        t.stale ? el('span', { class: 'tag tag-warn' }, 'stale: the code changed after this run') : null)
      : el('p', { class: 'muted' }, 'No test report yet: run pytest, then Refresh.'));
}

export function renderTable(container, ctx) {
  const reqs = ctx.about.requirements;
  if (!Array.isArray(reqs) || !reqs.length) {
    replace(container, el('p', { class: 'muted' }, 'This server does not publish its requirement map (/api/about).'));
    return;
  }
  const rows = reqs.map((r) => el('tr', { class: `req req-${r.kind}` },
    el('th', { scope: 'row', 'data-label': 'Requirement' },
      el('span', { class: `tag tag-${r.kind}` }, r.kind === 'explicit' ? 'asked' : 'between the lines'), el('span', { class: 'brief' }, r.brief)),
    el('td', { 'data-label': 'How', class: 'how' }, r.how),
    el('td', { 'data-label': 'Live evidence' }, el('ul', { class: 'evidence' }, evidence(r.id, ctx))),
    el('td', { 'data-label': 'Code' }, el('ul', { class: 'links' }, (r.sources || []).map((key) => {
      const link = codeLink(ctx.about, key);
      return el('li', {}, link ? el('a', { href: link.url, target: '_blank', rel: 'noopener' }, link.symbol || key) : el('code', {}, key));
    }))),
    el('td', { 'data-label': 'Tests' }, (r.tests || []).length
      ? el('ul', { class: 'links' }, r.tests.map((key) => el('li', {}, testBadge(ctx.about, key, { short: true }))))
      : el('span', { class: 'muted' }, '—')),
  ));
  replace(container, el('div', { class: 'table-wrap' }, el('table', { class: 'data cards req-table' },
    el('caption', { class: 'sr-only' }, 'Requirements of the brief and their evidence'),
    el('thead', {}, el('tr', {}, ['Requirement', 'How', 'Live evidence', 'Code', 'Tests'].map((h) => el('th', { scope: 'col' }, h)))),
    el('tbody', {}, rows))));
}

export function renderChecks(container, checks) {
  if (!checks || !checks.length) {
    replace(container, el('li', { class: 'muted' }, 'Plan a trip to run the checks.'));
    return;
  }
  replace(container, checks.map((c) => el('li', { class: `check ${c.ok === false ? 'is-bad' : ''}` },
    mark(c.ok, c.ok === true ? 'pass' : c.ok === false ? 'FAIL' : 'n/a'),
    el('div', {},
      el('p', { class: 'check-label' }, c.label),
      el('p', { class: 'check-values' }, 'expected ', el('code', {}, c.expected), ' · actual ', el('code', {}, c.actual)),
      c.detail ? el('p', { class: 'muted small' }, c.detail) : null))));
}
