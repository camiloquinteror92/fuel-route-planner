// The truck settings of a trip: the optional parameters of /api/route. Pure
// functions, no DOM.
//
// A trip is {start, finish, start_tank, settings}; `settings` holds ONLY the values
// that differ from the server's defaults, so a trip with the standard truck is
// exactly the request Postman sends, and the URL of the page stays short.
//
// `control`: the Truck step has a control for it. The others (corridor_miles,
// price_policy, consolidate) have none, but a link may bring them: they are read
// from the URL, sent to the API as they are (so the page shows the same plan as the
// JSON) and named on the page.
//
// Defaults come from /api/about (what the server uses when a parameter is left out);
// `fallback` is the API contract's default, used only when /api/about does not say.
// The ranges are the API contract's; the server validates them again (400 when out).

export const PRICE_POLICIES = ['median', 'min', 'max'];

const pick = (about, ...path) => path.reduce((value, key) => (value === null || value === undefined ? value : value[key]), about);

// Order = order in URLs.
export const SETTINGS = [
  {
    name: 'mpg', control: true, kind: 'number', min: 3, max: 30, step: 0.5, fallback: 10,
    fromAbout: (about) => pick(about, 'vehicle', 'miles_per_gallon'),
  },
  {
    name: 'max_range_miles', control: true, kind: 'number', min: 100, max: 1500, step: 10, fallback: 500,
    fromAbout: (about) => pick(about, 'vehicle', 'max_range_miles'),
  },
  {
    name: 'corridor_miles', control: false, kind: 'number', min: 1, max: 50, step: 1, fallback: 10,
    fromAbout: (about) => pick(about, 'planner', 'corridor_miles'),
  },
  {
    name: 'safety_reserve_gal', control: true, kind: 'number', min: 0, step: 0.5, fallback: 0,
    fromAbout: (about) => pick(about, 'vehicle', 'safety_reserve_gal'),
  },
  {
    name: 'price_policy', control: false, kind: 'choice', choices: PRICE_POLICIES, fallback: 'median',
    fromAbout: (about) => pick(about, 'planner', 'price_policy'),
  },
  {
    name: 'consolidate', control: false, kind: 'bool', fallback: true,
    fromAbout: (about) => pick(about, 'vehicle', 'consolidate') ?? pick(about, 'planner', 'consolidate'),
  },
];

export const SETTING_NAMES = SETTINGS.map((s) => s.name);
export const CONTROLLED = SETTINGS.filter((s) => s.control).map((s) => s.name);
export const HIDDEN = SETTINGS.filter((s) => !s.control).map((s) => s.name);
const BY_NAME = Object.fromEntries(SETTINGS.map((s) => [s.name, s]));

// The parameter list /api/about publishes (if it describes a setting, it wins).
function published(about, name) {
  const params = pick(about, 'api', 'params');
  return Array.isArray(params) ? params.find((p) => p && p.name === name) : null;
}

function asNumber(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string' || !value.trim()) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function asBool(value) {
  if (typeof value === 'boolean') return value;
  const text = String(value ?? '').trim().toLowerCase();
  if (['true', '1', 'yes', 'on'].includes(text)) return true;
  if (['false', '0', 'no', 'off'].includes(text)) return false;
  return null;
}

// Parse one value; null when it cannot be read as that kind.
export function parseSetting(name, value) {
  const s = BY_NAME[name];
  if (!s || value === null || value === undefined) return null;
  if (s.kind === 'number') return asNumber(value);
  if (s.kind === 'bool') return asBool(value);
  const text = String(value).trim().toLowerCase();
  return s.choices.includes(text) ? text : null;
}

// {name: default} from /api/about, else the contract's defaults.
export function defaultsFrom(about) {
  const out = {};
  for (const s of SETTINGS) {
    const fromList = published(about, s.name);
    const candidates = [fromList?.default, s.fromAbout(about), s.fallback];
    out[s.name] = candidates.map((v) => parseSetting(s.name, v)).find((v) => v !== null);
  }
  return out;
}

export function tankGallons(values) {
  const range = asNumber(values.max_range_miles);
  const mpg = asNumber(values.mpg);
  return range && mpg ? range / mpg : null;
}

// {min, max, step} of a numeric setting: the published ones when /api/about has them.
// The safety fuel must stay below the tank: its largest value is the step below it.
export function rangeOf(name, about, values = {}) {
  const s = BY_NAME[name];
  const p = published(about, name) || {};
  const min = asNumber(p.min_value ?? p.min) ?? s.min;
  let max = asNumber(p.max_value ?? p.max) ?? s.max ?? null;
  if (name === 'safety_reserve_gal') {
    const tank = tankGallons(values);
    if (tank) max = Math.max(min, Math.ceil(tank / s.step) * s.step - s.step);
  }
  return { min, max, step: s.step };
}

function same(name, a, b) {
  if (BY_NAME[name]?.kind === 'number') return asNumber(a) !== null && asNumber(a) === asNumber(b);
  return a === b;
}

// Only what differs from the defaults (values already parsed). Unreadable values are
// kept as typed, so the server's validation answers them with a clear 400.
export function onlyChanged(values, defaults) {
  const out = {};
  for (const name of SETTING_NAMES) {
    if (!(name in values)) continue;
    const raw = values[name];
    if (raw === null || raw === undefined || raw === '') continue;
    const parsed = parseSetting(name, raw);
    if (parsed === null) {
      out[name] = String(raw);
      continue;
    }
    if (!same(name, parsed, defaults[name])) out[name] = parsed;
  }
  return out;
}

// The settings found in a query string (URLSearchParams), as they were written.
export function settingsFromQuery(query, defaults) {
  const values = {};
  for (const name of SETTING_NAMES) if (query.has(name)) values[name] = query.get(name);
  return onlyChanged(values, defaults);
}

export function writeSettings(query, settings = {}) {
  for (const name of SETTING_NAMES) if (name in settings) query.set(name, String(settings[name]));
  return query;
}

export function sameSettings(a = {}, b = {}) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) if (String(a[k]) !== String(b[k])) return false;
  return true;
}
