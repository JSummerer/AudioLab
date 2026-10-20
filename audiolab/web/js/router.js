// Tiny hash router: #/new · #/activity · #/job/<id> · #/library[/<entry id>] · #/settings[/<section>].
//
// Views stay mounted; the router shows one at a time and calls its hooks:
//   view = { el, heading, show(arg, { same }), hide(), scrollPerArg?, fallback? }
// `arg` is the part after the view name (a job id, a Library entry, a settings section); `same` is
// true when only that part changed. `show` returns true when it placed the scroll
// position and focus itself; otherwise the router restores the view's own scroll
// position and focuses its heading. `scrollPerArg` keeps one position per arg (a
// job page per job) and, like a browser, restores it only on Back/Forward — a fresh
// visit starts at the top. A view with `fallback` needs an arg and goes there without one.
//
// Every history entry carries its depth in history.state, so the in-page Back
// button knows whether there is an app page to go back to (the desktop window has
// no browser buttons or address bar).

const DEFAULT = '#/new';
const views = new Map();
const scrollTops = new Map();
const listeners = new Set();
let scroller = null;
let current = null; // { name, arg, key, view }
let lastDepth = 0;
let stamp = 0; // bumps on every route change

const depth = () => (Number.isInteger(history.state?.depth) ? history.state.depth : null);

export const jobPath = (id) => `#/job/${encodeURIComponent(id)}`;
export const settingsPath = (section) => (section ? `#/settings/${encodeURIComponent(section)}` : '#/settings');

export function register(name, view) { views.set(name, view); }

/** Called after every route change with { name, arg }. */
export function onRoute(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Changes whenever the route does: lets an async action check the user is still where it started. */
export const routeStamp = () => stamp;

/** The route on screen: { name, arg } (null before start). */
export const currentRoute = () => (current ? { name: current.name, arg: current.arg } : null);

/** Navigate. `replace` swaps the current history entry; `focus: false` leaves focus to the caller. */
export function go(path, { replace = false, focus = true } = {}) {
  const d = depth() ?? 0;
  if (path !== location.hash) {
    if (replace) history.replaceState({ ...history.state, depth: d }, '', path);
    else history.pushState({ depth: d + 1 }, '', path);
  }
  // Same route again (e.g. the AI chip while Settings already shows that spot): let the view re-act.
  apply({ focus, traversal: false, again: true });
}

/** The in-page Back button: the previous app page, else `fallback`. */
export function back(fallback = '#/activity') {
  if ((depth() ?? 0) > 0) history.back();
  else go(fallback, { replace: true });
}

export function start(scrollEl) {
  scroller = scrollEl;
  if (depth() == null) history.replaceState({ ...history.state, depth: 0 }, '');
  // Back/forward, plain links and typed hashes. Chrome fires both events for a link
  // click; apply() is a no-op when nothing changed.
  window.addEventListener('popstate', onBrowserNav);
  window.addEventListener('hashchange', onBrowserNav);
  apply({ focus: false, traversal: true });
}

function onBrowserNav() {
  // A link click makes an entry without our depth: stamp it one deeper than where we were.
  // An entry that already has one was reached with Back/Forward.
  const traversal = depth() != null;
  if (!traversal) history.replaceState({ ...history.state, depth: lastDepth + 1 }, '');
  apply({ traversal });
}

function parse(hash) {
  const [name = '', raw = ''] = hash.replace(/^#\/?/, '').split('/');
  let arg = raw;
  try { arg = decodeURIComponent(raw); } catch { /* keep it raw */ }
  return { name: name || 'new', arg: arg || null };
}

function apply({ focus = true, traversal = false, again = false } = {}) {
  lastDepth = depth() ?? 0;
  const { name, arg } = parse(location.hash);
  const view = views.get(name);
  if (!view || (view.fallback && !arg)) {
    go(view?.fallback || DEFAULT, { replace: true, focus });
    return;
  }
  const key = view.scrollPerArg && arg ? `${name}/${arg}` : name;
  const same = current?.view === view;
  if (same && current.arg === arg) {
    if (again) view.show(arg, { same: true });
    return;
  }

  if (current && current.key !== key) scrollTops.set(current.key, scroller.scrollTop);
  if (current && !same) {
    current.view.el.hidden = true;
    current.view.hide?.();
  }
  const keyChanged = current?.key !== key;
  stamp += 1;
  current = { name, arg, key, view };
  view.el.hidden = false;
  const placed = view.show(arg, { same }) === true;
  if (!placed && keyChanged) {
    scroller.scrollTop = view.scrollPerArg && !traversal ? 0 : scrollTops.get(key) || 0;
    if (focus) view.heading?.focus({ preventScroll: true });
  }
  for (const fn of listeners) {
    try { fn({ name, arg }); } catch (e) { console.error('[route]', e); }
  }
}
