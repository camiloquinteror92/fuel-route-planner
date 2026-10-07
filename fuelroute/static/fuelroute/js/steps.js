// The guided tour: six steps, one card at a time. The step lives in the address
// hash (#trip, #route, #stops, #cost, #truck, #assignment), so a link opens the same
// step; the trip lives in the query string (main.js). Steps after "trip" stay locked
// until a plan exists. A line under the steps says where you are and what comes next
// ("Step 3 of 6: Fuel stops · Next: Cost"), and why a locked step does not open.

export const STEPS = ['trip', 'route', 'stops', 'cost', 'truck', 'assignment'];
const LOCKED_TITLE = 'Plan a trip first.';
const LOCKED_TEXT = 'Plan a trip first: type a start and a finish, then press “Find fuel stops”.';
const LOCKED_SECONDS = 5;

// nav: the <nav> with one button[data-step] per step; panels: {name: <section>};
// top: the element a new step scrolls back to; status: the line under the steps.
// onChange(name, previous) runs after every change of step; onLocked() when a locked
// step is asked for.
export function createStepper({ nav, panels, top = null, status = null, onChange, onLocked, announce = () => {} }) {
  let currentStep = 'trip';
  let unlocked = false;
  let lockedTimer = null;
  const buttons = Object.fromEntries(STEPS.map((name) => [name, nav.querySelector(`[data-step="${name}"]`)]));
  const label = (name) => buttons[name]?.querySelector('.step-label')?.textContent.trim() || name;

  // The step a hash names; an unknown or old one opens the route or the form.
  function fromHash(hash = window.location.hash) {
    const name = String(hash || '').replace(/^#/, '');
    if (STEPS.includes(name) && (unlocked || name === 'trip')) return name;
    return unlocked ? 'route' : 'trip';
  }

  function where() {
    const index = STEPS.indexOf(currentStep);
    const next = STEPS[index + 1];
    return `Step ${index + 1} of ${STEPS.length}: ${label(currentStep)}${next ? ` · Next: ${label(next)}` : ''}`;
  }

  function showStatus(text, kind = 'where') {
    if (!status) return;
    status.textContent = text;
    status.dataset.kind = kind;
  }

  function paint() {
    const index = STEPS.indexOf(currentStep);
    STEPS.forEach((name, i) => {
      const button = buttons[name];
      const locked = !unlocked && name !== 'trip';
      if (name === currentStep) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
      if (locked) {
        button.setAttribute('aria-disabled', 'true');
        button.title = LOCKED_TITLE;
      } else {
        button.removeAttribute('aria-disabled');
        button.removeAttribute('title');
      }
      button.classList.toggle('is-done', unlocked && i < index);
      panels[name].hidden = name !== currentStep;
    });
    for (const next of document.querySelectorAll('[data-go]')) {
      const locked = !unlocked && next.dataset.go !== 'trip';
      if (locked) next.setAttribute('aria-disabled', 'true');
      else next.removeAttribute('aria-disabled');
    }
    document.body.className = document.body.className.replace(/\bstep-\w+\b/g, '').trim();
    document.body.classList.add(`step-${currentStep}`);
    if (!lockedTimer) showStatus(where());
    // On a phone the bar scrolls sideways: keep the current step in view.
    const list = buttons[currentStep].closest('ol');
    const item = buttons[currentStep].parentElement;
    if (list && (item.offsetLeft < list.scrollLeft || item.offsetLeft + item.offsetWidth > list.scrollLeft + list.clientWidth)) {
      list.scrollLeft = item.offsetLeft - list.clientWidth / 3;
    }
  }

  // A locked step was asked for: say why, visibly, for a few seconds.
  function locked() {
    clearTimeout(lockedTimer);
    showStatus(LOCKED_TEXT, 'locked');
    announce(LOCKED_TEXT);
    onLocked?.();
    lockedTimer = setTimeout(() => {
      lockedTimer = null;
      showStatus(where());
    }, LOCKED_SECONDS * 1000);
  }

  // Show a step. focus: move the focus to its title (and say it), as a page change does.
  function go(name, { focus = true, updateHash = true } = {}) {
    if (!STEPS.includes(name)) name = fromHash('');
    if (!unlocked && name !== 'trip') {
      locked();
      return currentStep;
    }
    const previous = currentStep;
    currentStep = name;
    paint();
    if (updateHash && window.location.hash !== `#${name}`) {
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}#${name}`);
    }
    onChange?.(name, previous);
    if (focus) {
      const title = panels[name].querySelector('h2');
      title?.focus({ preventScroll: true });
      backToTop();
      announce(`Step ${STEPS.indexOf(name) + 1} of ${STEPS.length}: ${title?.textContent.trim() || name}`);
    }
    return name;
  }

  // A new card starts at its top: when the page is scrolled past the tour, go back to it
  // (just under the steps bar, which stays on screen).
  function backToTop() {
    if (!top) return;
    const bar = nav.getBoundingClientRect().bottom;
    const y = top.getBoundingClientRect().top;
    if (y >= bar) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollBy({ top: y - bar - 8, behavior: reduce ? 'auto' : 'smooth' });
  }

  function unlock(on = true) {
    unlocked = on;
    paint();
  }

  nav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-step]');
    if (button) go(button.dataset.step);
  });
  document.addEventListener('click', (event) => {
    const next = event.target.closest('[data-go]');
    if (next) go(next.dataset.go);
  });
  // A hash typed or followed (back / forward): show that step, and name it in the address.
  const follow = () => {
    const name = fromHash();
    if (name !== currentStep || window.location.hash !== `#${name}`) go(name, { focus: false });
  };
  window.addEventListener('hashchange', follow);
  window.addEventListener('popstate', follow);

  paint();
  return { go, unlock, current: () => currentStep, fromHash, isUnlocked: () => unlocked };
}
