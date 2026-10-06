// City suggestions for the Start and Finish fields: an ARIA 1.2 combobox over
// GET /api/places, the server's offline index of US places (0 external calls).
//
// Typing waits a moment before asking; arrow keys move, Enter picks, Escape closes,
// the mouse works too. The part already typed is shown in bold. Suggestions only
// help: "lat,lon" and any free text are still accepted, and the API decides.

import { el, fmt, replace } from './format.js';

const DEBOUNCE_MS = 150;
const LIMIT = 8;
const MIN_LETTERS = 2;
const COORDS = /^\s*-?\d+(\.\d+)?\s*[, ]\s*-?\d+(\.\d+)?\s*$/;

// Lower case, no accents, single spaces: "Cañon City" and "canon city" compare equal.
function fold(text) {
  return String(text).normalize('NFD').replace(/\p{M}/gu, '').toLowerCase().replace(/\s+/g, ' ');
}

// How many characters at the start of `label` spell what was typed (0 if they do not):
// "chi, il" highlights "Chi" of "Chicago, IL".
export function typedPrefixLength(label, typed) {
  const want = fold(String(typed).split(',')[0]).trimStart();
  if (!want) return 0;
  let got = '';
  for (let i = 0; i < label.length; i++) {
    got += fold(label[i]);
    if (got === want) return i + 1;
    if (!want.startsWith(got)) return 0;
  }
  return 0;
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
      const cut = typedPrefixLength(place.label, typed);
      return el('li', {
        id: optionId(i), role: 'option', class: 'combo-option', 'aria-selected': 'false',
        onmousedown: (event) => event.preventDefault(), // keep the focus in the input
        onclick: () => choose(i),
        onmousemove: () => { if (active !== i) setActive(i); },
      },
      el('span', { class: 'combo-label' }, cut ? [el('strong', {}, place.label.slice(0, cut)), place.label.slice(cut)] : place.label),
      place.population ? el('small', { class: 'combo-meta' }, `pop. ${fmt.int(place.population)}`) : null);
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
