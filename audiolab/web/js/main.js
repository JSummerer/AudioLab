// Boot + global wiring: the theme, the sidebar (collapse, AI status,
// running-job badge), the router and its views, drag & drop (files and links),
// paste-anywhere, deep links (?url=…&task=…: the link waits in the field for Load), the
// notice banner (no ffmpeg, an unavailable output folder) and the live connection.
import { IS_MAC, h, icon, clear, hydrateIcons } from './dom.js';
import { onAuthRequired, setToken } from './api.js';
import { store, on, loadHealth, loadSettings, loadJobs, connectEvents, activeCount, deviceLabel, appName } from './store.js';
import { toastError, tokenDialog, led } from './ui.js';
import { register, start, go, onRoute, currentRoute, settingsPath } from './router.js';
import { newView, loadUrl, offerLink, setFiles, setTask, useJobSource, normalizeUrl, focusLinkField } from './views/new/index.js';
import { activityView } from './views/activity.js';
import { jobView } from './views/job/index.js';
import { libraryView } from './views/library.js';
import { adjustView } from './views/adjust.js';
import { settingsView } from './views/settings.js';

// Remove query parameters from the address bar (no reload, no history entry).
function dropParams(...keys) {
  const params = new URLSearchParams(location.search);
  if (!keys.some((k) => params.has(k))) return;
  keys.forEach((k) => params.delete(k));
  history.replaceState(history.state, '', location.pathname + (params.toString() ? `?${params}` : '') + location.hash);
}

// A token passed in the address (?token=…) is remembered, then removed from the URL.
const startToken = new URLSearchParams(location.search).get('token');
if (startToken) setToken(startToken);
dropParams('token');

// Deep links (?url=…&task=…, docs/API.md) open New with the link in the field. It is not looked
// up until the user presses Load: any website can send the browser to this address.
const TASK_ALIASES = { save: 'download', convert: 'download', split: 'separate', check: 'analyze' };
const deepLink = (() => {
  const params = new URLSearchParams(location.search);
  const link = (params.get('url') || '').trim();
  const raw = (params.get('task') || '').trim().toLowerCase();
  dropParams('url', 'task');
  return link || raw ? { link, task: TASK_ALIASES[raw] || raw } : null;
})();

hydrateIcons();

const scroller = document.getElementById('main');

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function applyTheme() {
  const t = store.settings?.theme;
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

on('settings', applyTheme);

// ---------------------------------------------------------------------------
// Sidebar: AI chip, running-job badge, current page
// ---------------------------------------------------------------------------
const aiChip = document.getElementById('aiChip');
function renderAiChip() {
  const ai = store.health?.ai || {};
  let state = 'off';
  // Not a fault: the add-on is optional.
  let word = ai.install_supported === false ? 'AI add-on unavailable' : 'AI add-on not installed';
  let device = '';
  if (ai.available) { state = 'ok'; word = 'AI ready'; device = ai.device ? deviceLabel(ai.device) : ''; }
  else if (ai.installing) { state = 'on'; word = 'Installing AI…'; }
  const text = device ? `${word} · ${device}` : word;
  // Narrow windows keep the word and drop the device (base.css).
  clear(aiChip).append(led(state, { blink: !!ai.installing }), h('span', { class: 'status-chip-text' },
    word, device ? h('span', { class: 'status-chip-device' }, ` · ${device}`) : null));
  const tip = ai.available ? 'AI features are ready' : (ai.reason || 'AI features are not installed — click to set them up');
  // Collapsed, the chip is just its LED, so its text moves into the tooltip.
  aiChip.title = !isCollapsed() ? tip : ai.available ? text : `${text}\n${tip}`;
  aiChip.setAttribute('aria-label', `${text} — AI settings`);
}
on('health', renderAiChip);
aiChip.addEventListener('click', () => go(settingsPath('ai'), { focus: false }));

const badge = document.getElementById('activityBadge');
function renderCounts() {
  const n = activeCount();
  badge.hidden = n === 0;
  badge.textContent = String(n);
  document.title = n ? `(${n}) ${appName()}` : appName();
}
on('jobs', renderCounts);

// Brand comes from the server (health.app) so a rename needs no UI change.
on('health', () => {
  const name = appName();
  for (const el of document.querySelectorAll('.brand-name')) el.textContent = name;
  document.querySelector('.brand')?.setAttribute('aria-label', `${name} — new job`);
  renderCounts();
});

// Job pages belong to Activity and Adjust belongs to the Library.
const NAV_OF = { job: 'activity', adjust: 'library' };
onRoute(({ name }) => {
  const active = NAV_OF[name] || name;
  for (const a of document.querySelectorAll('.nav-item')) {
    if (a.dataset.route === active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
});

// The skip link would otherwise be read as a route.
document.getElementById('skipLink').addEventListener('click', (e) => {
  e.preventDefault();
  const r = currentRoute();
  const view = { new: newView, activity: activityView, job: jobView, library: libraryView,
    adjust: adjustView, settings: settingsView }[r?.name];
  (view?.heading || scroller).focus();
});

// ---------------------------------------------------------------------------
// Sidebar: collapse to an icon rail (remembered in this browser; index.html applies
// it before first paint). Below 1000 px it starts collapsed, so the page keeps its room;
// the toggle still opens it for the moment, without changing what is remembered.
// Narrow windows show the tab bar instead, without a toggle.
// ---------------------------------------------------------------------------
const SIDEBAR_KEY = 'audiolab.sidebar';
const sidebarToggle = document.getElementById('sidebarToggle');
const narrow = matchMedia('(max-width: 759px)');
const compact = matchMedia('(max-width: 999px)');
function isCollapsed() { return document.documentElement.dataset.sidebar === 'collapsed'; }
function rememberedCollapsed() {
  try { return localStorage.getItem(SIDEBAR_KEY) === 'collapsed'; } catch { return false; }
}

function applySidebar(collapsed) {
  if (collapsed) document.documentElement.dataset.sidebar = 'collapsed';
  else delete document.documentElement.dataset.sidebar;
  const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
  sidebarToggle.setAttribute('aria-label', label);
  sidebarToggle.title = `${label} (${IS_MAC ? '⌘\\' : 'Ctrl+\\'})`;
  // Icons only: the hidden labels stay the links' names, the tooltips show them.
  for (const a of document.querySelectorAll('.nav-item')) {
    if (collapsed) a.title = a.querySelector('.nav-label').textContent;
    else a.removeAttribute('title');
  }
  if (store.health) renderAiChip();
}

function toggleSidebar() {
  const collapsed = !isCollapsed();
  applySidebar(collapsed);
  if (compact.matches) return; // a compact window: for now only, not remembered
  try { localStorage.setItem(SIDEBAR_KEY, collapsed ? 'collapsed' : 'expanded'); } catch { /* this page only */ }
}

applySidebar(compact.matches || isCollapsed());
compact.addEventListener('change', () => applySidebar(compact.matches || rememberedCollapsed()));
sidebarToggle.addEventListener('click', toggleSidebar);
// ⌘\ (Ctrl+\ elsewhere). Alt/Shift aren't refused: some layouts need them for "\".
document.addEventListener('keydown', (e) => {
  if (e.key !== '\\' && e.code !== 'Backslash') return;
  if (!(IS_MAC ? e.metaKey : e.ctrlKey) || e.repeat || narrow.matches) return;
  if (isTextField(e.target) || document.querySelector('dialog[open]')) return;
  e.preventDefault();
  toggleSidebar();
});

// ---------------------------------------------------------------------------
// Into New: "do more with this", files (picker, drag & drop), pasted links
// ---------------------------------------------------------------------------
function openNew() {
  if (currentRoute()?.name !== 'new') go('#/new', { focus: false });
}

on('use-source', ({ job, task, again = false, file = null }) => {
  openNew();
  scroller.scrollTop = 0;
  useJobSource(job, task, { again, file });
});

const fileInput = document.getElementById('fileInput');
fileInput.addEventListener('change', () => {
  if (fileInput.files?.length) { openNew(); setFiles(fileInput.files); }
  fileInput.value = '';
});

// A dragged link (from the address bar or a web page) is loaded like a pasted one. Without
// preventDefault the window would follow it and replace the app with that page, with no way back.
// The overlay shows for files and for links from outside the app ("Drop to load this link");
// plain dragged text may still be a link, but that is only known on drop, so it gets no overlay.
const overlay = document.getElementById('dropOverlay');
const overlayTitle = document.getElementById('dropTitle');
const overlayNote = document.getElementById('dropNote');
const FILE_TEXT = [overlayTitle.textContent, overlayNote.textContent];
let dragDepth = 0;
let innerDrag = false; // a drag that started on this page (a cover, a link): not something to load
document.addEventListener('dragstart', () => { innerDrag = true; });
document.addEventListener('dragend', () => { innerDrag = false; });
const dragTypes = (e) => [...(e.dataTransfer?.types || [])];
const hasFiles = (e) => dragTypes(e).includes('Files');
const hasText = (e) => dragTypes(e).some((t) => t === 'text/uri-list' || t === 'text/plain');
const hasLink = (e) => !hasFiles(e) && dragTypes(e).includes('text/uri-list');
// Text fields take dropped text themselves (the link field, the tracklist).
const ownDrop = (e) => !hasFiles(e) && isTextField(e.target);
// What the overlay is shown for: files, or a link from outside this page.
const overlayFor = (e) => !innerDrag && (hasFiles(e) || hasLink(e));
window.addEventListener('dragenter', (e) => {
  if (hasText(e) && !hasFiles(e) && !ownDrop(e)) e.preventDefault();
  if (!overlayFor(e)) return;
  e.preventDefault();
  if (!dragDepth) {
    const link = !hasFiles(e);
    overlayTitle.textContent = link ? 'Drop to load this link' : FILE_TEXT[0];
    overlayNote.textContent = link ? 'It opens in New, as if you had pasted it.' : FILE_TEXT[1];
  }
  dragDepth += 1;
  overlay.hidden = false;
});
window.addEventListener('dragover', (e) => { if ((hasFiles(e) || hasText(e)) && !ownDrop(e)) e.preventDefault(); });
window.addEventListener('dragleave', (e) => {
  if (!overlayFor(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) overlay.hidden = true;
});
window.addEventListener('drop', (e) => {
  dragDepth = 0;
  overlay.hidden = true;
  if (!(hasFiles(e) || hasText(e)) || ownDrop(e)) return;
  e.preventDefault();
  if (innerDrag) return; // dropped back onto the page it came from
  if (!mounted || document.querySelector('dialog[open]')) return;
  const files = [...(e.dataTransfer.files || [])];
  if (files.length) {
    openNew();
    setFiles(files);
    return;
  }
  // The first link of a uri-list (lines starting with # are comments), else the plain text.
  const uri = (e.dataTransfer.getData('text/uri-list') || '').split(/\r?\n/).map((l) => l.trim()).find((l) => l && !l.startsWith('#'));
  const text = uri || (e.dataTransfer.getData('text/plain') || '').trim();
  if (!looksLikeLink(text)) return; // other dropped text: ignored
  openNew();
  loadUrl(text);
  focusLinkField();
});

// Paste a link anywhere (no clipboard permission needed: it's the paste event).
const TEXT_TYPES = new Set(['', 'text', 'search', 'url', 'email', 'password', 'number', 'tel']);
function isTextField(el) {
  if (!el || el === document.body || el.nodeType !== 1) return false;
  if (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  return el.tagName === 'INPUT' && TEXT_TYPES.has((el.getAttribute('type') || '').toLowerCase());
}
// A link, not just any word with a dot in it ("notes.txt").
const looksLikeLink = (text) => !!text && !/\s/.test(text)
  && /^(https?:\/\/|www\.|([\w-]+\.)+[a-z]{2,}\/)/i.test(text) && !!normalizeUrl(text);
document.addEventListener('paste', (e) => {
  if (!mounted || isTextField(e.target) || isTextField(document.activeElement) || document.querySelector('dialog[open]')) return;
  const text = (e.clipboardData?.getData('text') || '').trim();
  if (!looksLikeLink(text)) return;
  e.preventDefault();
  openNew();
  loadUrl(text);
  focusLinkField();
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
const banner = document.getElementById('banner');
let bannerKey = '';
function showBanner(message, actionLabel, action) {
  bannerKey = `${message}|${actionLabel}`;
  clear(banner).append(icon('alert', 18), h('span', null, message),
    actionLabel ? h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: action }, actionLabel) : null);
  banner.hidden = false;
}

/** After each health update: what stops jobs from working, until it's fixed. */
function syncBanner() {
  if (!mounted) return; // boot shows its own errors
  const hth = store.health || {};
  const ff = hth.ffmpeg;
  let next = null;
  if (ff && ff.ok === false) {
    next = [ff.error || 'The audio engine (ffmpeg) is missing, so nothing can be processed.', 'Details', () => go(settingsPath('ffmpeg'))];
  } else if (hth.output_dir_ok === false) {
    next = [`The output folder isn’t available: ${hth.output_dir || ''}.`, 'Change folder', () => go(settingsPath('output'))];
  }
  if (!next) { banner.hidden = true; bannerKey = ''; return; }
  if (banner.hidden || bannerKey !== `${next[0]}|${next[1]}`) showBanner(...next);
}
on('health', syncBanner);
// A drive plugged back in while the app was in the background: look again on return.
window.addEventListener('focus', () => { if (mounted && store.health?.output_dir_ok === false) loadHealth().catch(() => {}); });

let mounted = false;
onAuthRequired(() => tokenDialog(() => boot()));

function mountViews() {
  const views = {
    new: [newView, 'viewNew'], activity: [activityView, 'viewActivity'], job: [jobView, 'viewJob'],
    library: [libraryView, 'viewLibrary'], adjust: [adjustView, 'viewAdjust'], settings: [settingsView, 'viewSettings'],
  };
  for (const [name, [view, id]] of Object.entries(views)) {
    view.mount(document.getElementById(id));
    register(name, view);
  }
  if (deepLink) {
    history.replaceState(history.state, '', '#/new');
    if (deepLink.task) setTask(deepLink.task);
    if (deepLink.link) offerLink(deepLink.link); // waits in the field until the user presses Load
  }
  start(scroller);
}

async function boot() {
  try {
    await Promise.all([loadHealth(), loadSettings()]);
  } catch (e) {
    if (e.status === 401) return; // token dialog is open
    showBanner(e.message || 'Can’t reach the app.', 'Reload', () => location.reload());
    return;
  }
  banner.hidden = true;
  applyTheme();
  if (!mounted) {
    mounted = true;
    mountViews();
  }
  syncBanner();
  try { await loadJobs(); } catch (e) { toastError(e); }
  connectEvents();
}

on('live', (state) => {
  document.body.dataset.live = state;
});

boot();
