// City suggestions for the Start and Finish fields: an ARIA 1.2 combobox over
// GET /api/places, the server's offline index of US places (0 external calls).
//
// Typing waits a moment before asking; arrow keys move, Enter picks, Escape closes,
// the mouse works too. The part already typed is shown in bold, read the way the
// server reads it ("st lou" is "Saint Lou…"). A place with no fuel prices (Alaska,
// Hawaii) is shown dimmed with "no price data": choosing it gets the API's 422.
// Suggestions only help: "lat,lon" and any free text are still accepted, and the API
// decides.

import { el, fmt, replace } from './format.js';

const DEBOUNCE_MS = 150;
const LIMIT = 8;
const MIN_LETTERS = 2;
const COORDS = /^\s*-?\d+(\.\d+)?\s*[, ]\s*-?\d+(\.\d+)?\s*$/;

// Lower case, no accents, single spaces: "Cañon City" and "canon city" compare equal.
function fold(text) {
  return String(text).normalize('NFD').replace(/\p{M}/gu, '').toLowerCase().replace(/\s+/g, ' ');
}

// The words typed, as the server compares names (services/text.py, normalize_place):
// lower case, no accents or punctuation, a leading "The" dropped, a leading compass
// letter spelled out and "St", "Ste", "Ft", "Mt", "Pt" expanded ("st lou" -> saint, lou).
const TOKENS = { st: 'saint', ste: 'sainte', ft: 'fort', mt: 'mount', pt: 'point' };
const DIRECTIONS = { n: 'north', s: 'south', e: 'east', w: 'west' };

export function typedWords(typed) {
  let words = fold(String(typed).split(',')[0]).replace(/'/g, '').replace(/[^a-z0-9]+/g, ' ').trim().split(' ').filter(Boolean);
  if (words.length > 1 && words[0] === 'the') words = words.slice(1);
  if (words.length > 1 && DIRECTIONS[words[0]]) words = [DIRECTIONS[words[0]], ...words.slice(1)];
  return words.map((word) => TOKENS[word] ?? word);
}

// How many characters at the start of `word` spell `want` (letters and digits, folded).
function spelled(word, want) {
  let got = '';
  for (let i = 0; i < word.length; i++) {
    const c = fold(word[i]).replace(/[^a-z0-9]/g, '');
    if (!c) continue;
    got += c;
    if (got === want) return i + 1;
    if (!want.startsWith(got)) return 0;
  }
  return 0;
}

// The label cut into [text, bold] pieces: in each of its first words, the part that
// spells the word typed at that place ("st lou" -> **Saint** **Lou**is, MO).
export function highlight(label, typed) {
  const want = typedWords(typed);
  const comma = label.lastIndexOf(', ');
  const name = comma >= 0 ? label.slice(0, comma) : label;
  const pieces = [];
  let w = 0;
  for (const part of name.split(/([\s-]+)/)) {
    if (!part) continue;
    if (/^[\s-]+$/.test(part)) {
      pieces.push([part, false]);
      continue;
    }
    const cut = w < want.length ? spelled(part, want[w]) : 0;
    if (cut) pieces.push([part.slice(0, cut), true], [part.slice(cut), false]);
    else pieces.push([part, false]);
    w = cut && cut === part.length ? w + 1 : want.length; // a word only partly typed ends the match
  }
  if (comma >= 0) pieces.push([label.slice(comma), false]);
  return pieces.filter(([text]) => text);
}

function letters(text) {
  return (String(text).match(/\p{L}/gu) || []).length;
}

export function createCombobox(input, listbox, { fetchPlaces, announce = () => {}, onPick = () => {} }) {
  const cache = new Map();
  let items = [];
  let active = -1;
  let timer = null;
  let controller = null;
  let disabled = false; // the server has no /api/places: stop asking
  let picking = false;
  let shownFor = '';

  function optionId(i) {
    return `${listbox.id}-opt-${i}`;
  }

  function close() {
    listbox.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  }

  function setActive(i) {
    active = i;
    for (const [n, node] of [...listbox.children].entries()) {
      const on = n === i;
      node.setAttribute('aria-selected', String(on));
      node.classList.toggle('is-active', on);
      if (on) node.scrollIntoView({ block: 'nearest' });
    }
    if (i >= 0) input.setAttribute('aria-activedescendant', optionId(i));
    else input.removeAttribute('aria-activedescendant');
  }

  function choose(i) {
    const place = items[i];
    if (!place) return;
    picking = true;
    input.value = place.label;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    picking = false;
    close();
    onPick(place);
    announce(`${place.label} selected.`);
  }

  function render(typed) {
    shownFor = typed;
    replace(listbox, items.map((place, i) => {
      const priced = place.plannable !== false; // false in Alaska and Hawaii: no station of the file there
      return el('li', {
        id: optionId(i), role: 'option', class: `combo-option${priced ? '' : ' is-unpriced'}`, 'aria-selected': 'false',
        title: priced ? null : 'The price file has no station in this state: a trip to or from here cannot be priced.',
        onmousedown: (event) => event.preventDefault(), // keep the focus in the input
        onclick: () => choose(i),
        onmousemove: () => { if (active !== i) setActive(i); },
      },
      el('span', { class: 'combo-label' }, highlight(place.label, typed).map(([text, bold]) => (bold ? el('strong', {}, text) : text))),
      priced ? null : el('span', { class: 'combo-tag' }, 'no price data'),
      place.population ? el('small', { class: 'combo-meta' }, `${fmt.int(place.population)} people`) : null);
    }));
    if (!items.length) {
      close();
      return;
    }
    listbox.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    setActive(-1);
  }

  async function lookup(typed) {
    const key = fold(typed).trim();
    if (cache.has(key)) {
      items = cache.get(key);
      render(typed);
      announce(items.length ? `${items.length} suggestions. Use the arrow keys to choose.` : '');
      return;
    }
    if (controller) controller.abort('superseded');
    const current = new AbortController();
    controller = current;
    let r;
    try {
      r = await fetchPlaces(typed, { limit: LIMIT, signal: current.signal });
    } catch {
      return; // a newer keystroke replaced it
    }
    if (controller !== current) return;
    controller = null;
    if (!r) return;
    if (r.status === 404) {
      disabled = true; // an older server: free text still works
      return;
    }
    if (!r.ok || !Array.isArray(r.body?.results)) {
      close();
      return;
    }
    items = r.body.results.filter((p) => p && typeof p.label === 'string');
    cache.set(key, items);
    if (fold(input.value).trim() !== key) return; // the text changed while waiting
    render(typed);
    announce(items.length ? `${items.length} suggestions. Use the arrow keys to choose.` : '');
  }

  input.addEventListener('input', () => {
    if (picking) return;
    clearTimeout(timer);
    const typed = input.value;
    if (disabled || COORDS.test(typed) || letters(typed) < MIN_LETTERS) {
      close();
      return;
    }
    timer = setTimeout(() => lookup(typed), DEBOUNCE_MS);
  });

  input.addEventListener('keydown', (event) => {
    const open = !listbox.hidden && items.length > 0;
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (!open) {
          if (items.length && fold(shownFor).trim() === fold(input.value).trim()) render(input.value);
          else if (!disabled && letters(input.value) >= MIN_LETTERS) lookup(input.value);
          return;
        }
        setActive(active + 1 >= items.length ? 0 : active + 1);
        break;
      case 'ArrowUp':
        if (!open) return;
        event.preventDefault();
        setActive(active <= 0 ? items.length - 1 : active - 1);
        break;
      case 'Enter':
        if (open && active >= 0) {
          event.preventDefault(); // pick, do not submit yet
          choose(active);
        } else {
          close();
        }
        break;
      case 'Escape':
        if (open) {
          event.preventDefault();
          close();
        }
        break;
      case 'Tab':
        close();
        break;
      default:
    }
  });

  input.addEventListener('blur', () => setTimeout(close, 0));

  return { close };
}
