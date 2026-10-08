// The "i" buttons and the card they open (the words live in explain.js).
//
// decorate(root) puts a small "i" button after every value (each [data-live] that has
// an explainer, once per paragraph) and inside every [data-explain="key"] element of
// the templates and of the parts drawn by JavaScript (stop cards, checks, "What
// changed", Play). It can run again at any time: it never marks an element twice.
// Where the buttons go is decided by planButtons(), a pure function tested in Node.
//
// A button opens one shared card (role="dialog", not modal) next to it, or as a sheet
// at the bottom of a phone. The card shows at once what the value is, this trip's
// numbers and why; where it comes from, the code and "Good to know" are folded, and
// open by themselves when the header switch "Explain every number" is on (remembered
// in this browser only). The card reads the page context when it opens, and refresh()
// redraws it after a new answer, keeping the focus where it was. Escape closes it and
// gives the focus back to its button; Shift+Tab before its first control does the
// same, and Tab after its last one goes on to what follows the button, so the keyboard
// order follows the page. A click or a tap outside, or the focus leaving it, closes
// it. "See also" opens a related card, and Back returns. On a wide screen the card
// follows a button that stays put while the page scrolls (the sticky map column).

import { EXPLAINERS, SECTION_LABELS, ariaLabel, codeRef, explain, summary } from './explain.js';
import { el, replace } from './format.js';

const STORE_KEY = 'fuelroute.explain-all';
const SHEET = '(max-width: 639.98px)';
const GAP = 8; // between the button and the card, and from the window's edges
// The block a value belongs to: one button per key per block.
const BLOCKS = 'p, li, td, th, dd, dt, h2, h3, h4, legend, caption';
// Sections folded until "Explain every number" is on (what, how and why always show).
const FOLDED = new Set(['source', 'code', 'edge']);
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]';

// On screen: in the page and not hidden (display: none has no boxes).
const visible = (node) => Boolean(node?.isConnected) && node.getClientRects().length > 0;

// Where the buttons go (pure: tested in Node). items, in page order, one per
// [data-live] or [data-explain] element: {live, explain, noExplain, done, block, arg}.
// Returns [{index, key, arg, where}]: one button per key per block, 'inside' a
// data-explain element, 'after' a value. A value marked data-no-explain, an element
// already done and a key without an explainer get none.
export function planButtons(items, isKnown = (key) => Object.hasOwn(EXPLAINERS, String(key))) {
  const used = new Map(); // block -> keys with a button
  const plan = [];
  items.forEach((item, index) => {
    if (item.done) return;
    const host = item.explain !== null && item.explain !== undefined;
    const key = host ? item.explain : item.live;
    if ((!host && item.noExplain) || !isKnown(key)) return;
    const keys = used.get(item.block) ?? new Set();
    if (keys.has(key)) return;
    keys.add(key);
    used.set(item.block, keys);
    plan.push({ index, key, arg: item.arg ?? null, where: host ? 'inside' : 'after' });
  });
  return plan;
}

function readStore() {
  try {
    return window.localStorage.getItem(STORE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeStore(on) {
  try {
    window.localStorage.setItem(STORE_KEY, on ? '1' : '0');
  } catch {
    // private window or blocked storage: the switch still works for this visit
  }
}

function marker(key, arg) {
  const dataset = { explainKey: key };
  if (arg !== undefined && arg !== null && arg !== '') dataset.explainArg = String(arg);
  return el('button', {
    type: 'button',
    class: 'explain-btn',
    'aria-label': dataset.explainArg ? `${ariaLabel(key)} (${dataset.explainArg})` : ariaLabel(key),
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
    'aria-controls': 'explain-card',
    title: `${EXPLAINERS[key].title}: ${summary(key)}`,
    dataset,
  });
}

function codeItem(ref) {
  const { file, symbol } = codeRef(ref);
  return el('li', {}, el('code', {}, file), symbol ? [' → ', el('code', { class: 'explain-symbol' }, symbol)] : null);
}

// A button inside a sticky (or fixed) box stays put while the page scrolls.
function inStickyBox(node) {
  for (let n = node?.parentElement; n && n !== document.body; n = n.parentElement) {
    const { position } = window.getComputedStyle(n);
    if (position === 'sticky' || position === 'fixed') return true;
  }
  return false;
}

// getContext(): the page context ({route, standard, about, client, derived}).
export function createExplainer({ getContext, toggle = null, announce = () => {} }) {
  const card = el('div', {
    id: 'explain-card', class: 'explain-card', role: 'dialog', 'aria-modal': 'false', 'aria-labelledby': 'explain-title',
    tabindex: '-1', hidden: true,
  });
  document.body.append(card);
  let current = null; // {trigger, key, arg, back: [{key, arg}], opened: Set of folded sections opened, sticky}
  let scrollFrame = 0;

  function section(name, ...content) {
    return el('section', { class: `explain-section explain-${name}` }, el('h3', {}, SECTION_LABELS[name]), ...content);
  }

  // A folded section: open when "Explain every number" is on, or once opened by hand.
  function fold(name, ...content) {
    const all = document.body.classList.contains('explain-all');
    const node = el('details', { class: `explain-section explain-${name}`, dataset: { section: name }, open: all || current.opened.has(name) },
      el('summary', {}, SECTION_LABELS[name]), ...content);
    node.addEventListener('toggle', () => {
      if (!current) return;
      if (node.open) current.opened.add(name);
      else current.opened.delete(name);
    });
    return node;
  }

  function render() {
    const { key, arg, back } = current;
    const data = explain(key, getContext(), arg);
    const how = data.live.length
      ? [el('div', { class: 'explain-live' }, el('p', { class: 'explain-tag' }, 'On this trip'), data.live.map((line) => el('p', {}, line))),
        el('p', { class: 'explain-general' }, el('span', { class: 'explain-tag' }, 'In general: '), data.how)]
      : [el('p', {}, data.how)];
    const folded = (name, ...content) => (FOLDED.has(name) ? fold(name, ...content) : section(name, ...content));
    replace(card,
      el('div', { class: 'explain-head' },
        back.length ? el('button', { type: 'button', class: 'explain-icon', 'data-explain-back': '', 'aria-label': 'Back to the previous explanation' }, '←') : null,
        el('h2', { id: 'explain-title' }, data.title),
        el('button', { type: 'button', class: 'explain-icon', 'data-explain-close': '', 'aria-label': 'Close' }, '×')),
      el('div', { class: 'explain-body' },
        section('what', el('p', {}, data.what)),
        section('how', ...how),
        section('why', el('p', {}, data.why)),
        folded('source', el('p', {}, data.source)),
        folded('code', el('ul', { class: 'explain-refs' }, data.code.map(codeItem))),
        data.edge ? folded('edge', el('p', {}, data.edge)) : null,
        data.see.length ? el('div', { class: 'explain-see' }, el('span', { class: 'explain-tag' }, 'See also'),
          data.see.map((other) => el('button', { type: 'button', class: 'explain-chip', dataset: { explainSee: other } }, EXPLAINERS[other].title))) : null));
  }

  // What has the focus inside the card, as a selector to find its twin after a redraw:
  // null when the focus is elsewhere, '' for the card itself.
  function focusInCard() {
    const active = document.activeElement;
    if (!active || !card.contains(active)) return null;
    if (active.matches('[data-explain-close]')) return '[data-explain-close]';
    if (active.matches('[data-explain-back]')) return '[data-explain-back]';
    if (active.dataset?.explainSee) return `[data-explain-see="${CSS.escape(active.dataset.explainSee)}"]`;
    const folded = active.matches('summary') ? active.closest('details')?.dataset.section : null;
    return folded ? `details[data-section="${folded}"] > summary` : '';
  }

  function place() {
    const sheet = window.matchMedia(SHEET).matches;
    card.classList.toggle('is-sheet', sheet);
    if (sheet || !visible(current?.trigger)) {
      card.style.left = '';
      card.style.top = '';
      return;
    }
    // The visible area without the scrollbar (window.innerWidth includes it).
    const viewWidth = document.documentElement.clientWidth;
    const viewHeight = document.documentElement.clientHeight;
    const r = current.trigger.getBoundingClientRect();
    const width = card.offsetWidth;
    const height = card.offsetHeight;
    const left = Math.min(Math.max(GAP, r.left + r.width / 2 - width / 2), viewWidth - width - GAP);
    // Below the button, else above it, else as low as the window allows (the card is
    // never taller than the window: max-height in planner.css).
    let top = r.bottom + GAP;
    if (top + height > viewHeight) top = r.top - GAP - height >= GAP ? r.top - GAP - height : viewHeight - height - GAP;
    card.style.left = `${Math.max(GAP, left) + window.scrollX}px`;
    card.style.top = `${Math.max(GAP, top) + window.scrollY}px`;
  }

  function show(key, arg) {
    current = { ...current, key, arg };
    render();
    card.hidden = false;
    card.scrollTop = 0;
    place();
    card.focus({ preventScroll: true });
  }

  function open(trigger) {
    if (current?.trigger === trigger) {
      close({ focus: true });
      return;
    }
    close();
    current = { trigger, back: [], opened: new Set(), sticky: inStickyBox(trigger) };
    trigger.setAttribute('aria-expanded', 'true');
    show(trigger.dataset.explainKey, trigger.dataset.explainArg ?? null);
  }

  function close({ focus = false } = {}) {
    if (!current) return;
    const { trigger } = current;
    current = null;
    card.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (focus && visible(trigger)) trigger.focus({ preventScroll: true });
  }

  // The first control after `node` in the page's tab order (outside the card).
  function nextTabbable(node) {
    return [...document.querySelectorAll(FOCUSABLE)].find((other) => !card.contains(other) && other.tabIndex >= 0 && visible(other)
      && (node.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_FOLLOWING)) || null;
  }

  // A new answer: the open card shows its numbers and keeps the focus where it was. If
  // its button was drawn again, or is now hidden (data-live-if), it follows the twin
  // that is on screen, or closes.
  function refresh() {
    if (!current) return;
    if (!visible(current.trigger)) {
      const { explainKey: key, explainArg: arg } = current.trigger.dataset;
      const again = [...document.querySelectorAll('.explain-btn')]
        .find((b) => b.dataset.explainKey === key && b.dataset.explainArg === arg && visible(b));
      if (!again) {
        close();
        return;
      }
      current.trigger.setAttribute('aria-expanded', 'false');
      current.trigger = again;
      current.sticky = inStickyBox(again);
      again.setAttribute('aria-expanded', 'true');
    }
    const focus = focusInCard();
    render();
    place();
    if (focus !== null) ((focus && card.querySelector(focus)) || card).focus({ preventScroll: true });
  }

  function decorate(root = document) {
    const nodes = [...root.querySelectorAll('[data-live], [data-explain]')];
    const items = nodes.map((node) => ({
      live: node.dataset.live ?? null,
      explain: node.dataset.explain ?? null,
      noExplain: 'noExplain' in node.dataset,
      done: Boolean(node.dataset.explainDone),
      block: node.closest(BLOCKS) || node.parentElement,
      arg: node.dataset.explainArg ?? null,
    }));
    for (const node of nodes) node.dataset.explainDone = '1';
    for (const { index, key, arg, where } of planButtons(items)) {
      const button = marker(key, arg);
      if (where === 'inside') nodes[index].append(button);
      else nodes[index].after(button);
    }
  }

  function setAll(on) {
    document.body.classList.toggle('explain-all', on);
    toggle?.setAttribute('aria-pressed', String(on));
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest?.('.explain-btn');
    if (button) {
      event.preventDefault();
      open(button);
      return;
    }
    if (!current || !card.contains(event.target)) return;
    if (event.target.closest('[data-explain-close]')) close({ focus: true });
    else if (event.target.closest('[data-explain-back]')) {
      const previous = current.back.pop();
      if (previous) show(previous.key, previous.arg);
    } else {
      const see = event.target.closest('[data-explain-see]');
      if (see) {
        current.back.push({ key: current.key, arg: current.arg });
        show(see.dataset.explainSee, null);
      }
    }
  });
  // A click or a tap outside the card closes it (its own button toggles it).
  document.addEventListener('pointerdown', (event) => {
    if (current && !card.contains(event.target) && !current.trigger.contains(event.target)) close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && current) {
      event.preventDefault();
      close({ focus: true });
    }
  });
  // The card is the last element of the page: without this, Shift+Tab from its first
  // control would jump to the footer, and Tab from its last one out of the page.
  card.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' || !current) return;
    const controls = [...card.querySelectorAll(FOCUSABLE)].filter((node) => node.tabIndex >= 0 && visible(node));
    const active = document.activeElement;
    if (event.shiftKey && (active === card || active === controls[0])) {
      event.preventDefault();
      close({ focus: true });
    } else if (!event.shiftKey && (!controls.length || active === controls[controls.length - 1])) {
      event.preventDefault();
      const { trigger } = current;
      close();
      (nextTabbable(trigger) || trigger).focus();
    }
  });
  card.addEventListener('focusout', (event) => {
    const next = event.relatedTarget;
    if (current && next && !card.contains(next) && next !== current.trigger) close();
  });
  window.addEventListener('resize', () => current && place());
  // A button in the sticky map column stays put while the page scrolls: the card follows.
  window.addEventListener('scroll', () => {
    if (!current?.sticky || scrollFrame) return;
    scrollFrame = window.requestAnimationFrame(() => {
      scrollFrame = 0;
      if (current) place();
    });
  }, { passive: true });

  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener('click', () => {
      const on = toggle.getAttribute('aria-pressed') !== 'true';
      setAll(on);
      writeStore(on);
      announce(on
        ? 'Every number now has a visible i button, and its card opens every section: what it is, where it comes from, how it is calculated, why, and the code.'
        : 'The i buttons are back to subtle, and the cards fold where a value comes from, the code and good to know.');
    });
    setAll(readStore());
  }

  return { decorate, refresh, close, open, isOpen: () => Boolean(current) };
}
