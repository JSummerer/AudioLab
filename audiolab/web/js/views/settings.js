// Settings (#/settings[/<section>]): one column of sections (rows with hairlines: label
// and help left, the control right) under a sticky strip of section tabs that follows
// the scroll position. The route's section may also name a spot inside a section that
// other views link to (#/settings/ai, #/settings/cookies; #/settings/output opens the
// folder field): the page scrolls there and flashes it. Changes save immediately; a
// small "Saved ✓" next to the setting confirms it. Paths show short, the full one on hover.
import { h, icon, clear, append, shortFolder, fmtBytes, fmtDurationWords } from '../dom.js';
import { api } from '../api.js';
import { store, on, saveSettings, loadHealth, deviceLabel, appName, isFinished, forgetShown, formatInfo } from '../store.js';
import { segmented, chips, switchControl, led, toast, toastError, callout, copyText, confirmDialog, closeInfo } from '../ui.js';
import { go, settingsPath } from '../router.js';
import { clearFinished, copyDiagnostics } from '../actions.js';

const SECTIONS = [
  ['files', 'Files'], ['defaults', 'Defaults'], ['look', 'Look'], ['downloads', 'Downloads & sign-in'],
  ['system', 'AI & system'], ['storage', 'Storage'], ['about', 'About'],
];
// Spots inside a section → the section holding them.
const SPOTS = { ai: 'system', cookies: 'downloads', output: 'files', shortcut: 'downloads' };
const STALE_DOWNLOADER_DAYS = 60;
const SIGN_IN_BROWSERS = [
  ['', 'Off'], ['firefox', 'Firefox'], ['chrome', 'Chrome'], ['edge', 'Edge'], ['brave', 'Brave'],
  ['safari', 'Safari'], ['chromium', 'Chromium'], ['opera', 'Opera'], ['vivaldi', 'Vivaldi'],
];
const STORAGE_CACHE_MS = 6000; // /api/storage is cached for ~5 s on the server

let refs = null;
let scroller = null;
let visible = false;
let saving = 0;       // own saves don't rebuild the controls (keeps keyboard focus)
let flashing = null;  // { key, until }: a section or spot to highlight, survives rebuilds
let spyFrame = 0;
let spyPausedUntil = 0;
let defaultsWaiting = false; // Defaults were drawn before /api/health brought the formats
let folderDraft = null; // the output folder field's text while it is open (null = closed)
const storage = { data: null, finished: null, key: null, timer: 0 };

export const settingsView = {
  el: null,
  heading: null,

  mount(el) {
    this.el = el;
    scroller = el.closest('.content');
    refs = {
      title: h('h1', { class: 'page-title', id: 'settingsTitle', tabindex: '-1' }, 'Settings'),
      sub: h('p', { class: 'page-sub' }),
      nav: h('nav', { class: 'settings-nav', 'aria-label': 'Settings sections' }),
      links: {},
      sections: {},
      storageInfo: h('div', { class: 'storage-info', hidden: true, tabindex: '-1' }),
      live: h('p', { class: 'sr-only', role: 'status' }),
    };
    this.heading = refs.title;
    const list = h('div', { class: 'settings-sections' });
    for (const [key, label] of SECTIONS) {
      const a = h('a', { href: settingsPath(key) }, label);
      a.addEventListener('click', (e) => { e.preventDefault(); go(settingsPath(key), { replace: true, focus: false }); });
      refs.links[key] = a;
      refs.nav.append(a);
      const body = h('div', { class: 'set-rows' });
      const sec = h('section', { class: 'block', id: `settings-${key}`, tabindex: '-1', 'aria-labelledby': `settings-${key}-title` },
        h('h2', { class: 'block-title', id: `settings-${key}-title` }, label), body);
      refs.sections[key] = { el: sec, body };
      list.append(sec);
    }
    el.append(h('div', { class: 'page' },
      h('header', { class: 'page-head' }, h('div', { class: 'page-head-text' }, refs.title, refs.sub)),
      h('div', { class: 'settings-layout' }, refs.nav, list),
      refs.live));

    on('health', () => {
      if (!visible) return;
      if (folderDraft == null) renderFiles(); // never redraw the folder field under the user's typing
      if (defaultsWaiting) renderDefaults();
      renderDownloads();
      renderSystem();
      renderAbout();
    });
    on('settings', () => { if (visible && !saving) { renderDefaults(); renderLook(); renderDownloads(); renderStorage(); } });
    on('jobs', () => { if (visible) renderStorageInfo(); });
  },

  show(section, { same = false } = {}) {
    if (!same) {
      visible = true;
      renderAll();
      loadHealth().catch(() => {});
      loadStorage();
      scroller.addEventListener('scroll', onScroll, { passive: true });
      spy();
    }
    return section ? reveal(section, { smooth: same, flash: !same || !!SPOTS[section] }) : false;
  },

  hide() {
    visible = false;
    folderDraft = null;
    closeInfo();
    clearTimeout(storage.timer);
    scroller.removeEventListener('scroll', onScroll);
  },
};

function renderAll() {
  refs.sub.textContent = `${appName()} ${store.health?.version || ''}`.trim();
  renderFiles();
  renderDefaults();
  renderLook();
  renderDownloads();
  renderSystem();
  renderStorage();
  renderAbout();
}

// ---------------------------------------------------------------------------
// Section nav: jump to a section, follow the scroll position
// ---------------------------------------------------------------------------
function reveal(key, { smooth, flash }) {
  const sectionKey = SPOTS[key] || key;
  const sec = refs.sections[sectionKey];
  if (!sec) return false;
  if (key === 'output') openFolderField(); // "Change folder" in the banner: land in the field
  const spot = SPOTS[key] ? sec.el.querySelector(`[data-spot="${key}"]`) : null;
  setCurrent(sectionKey);
  spyPausedUntil = Date.now() + 900; // let a smooth scroll land before following it again
  (spot || sec.el).scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: spot ? 'center' : 'start' });
  ((spot || sec.el).querySelector('[data-autofocus]') || sec.el).focus({ preventScroll: true });
  if (flash) {
    flashing = { key: spot ? key : sectionKey, until: Date.now() + 1800 };
    applyFlash();
  }
  return true;
}

function applyFlash() {
  if (!flashing || Date.now() > flashing.until) return;
  const el = refs.sections[flashing.key]?.el || refs.sections[SPOTS[flashing.key]]?.el.querySelector(`[data-spot="${flashing.key}"]`);
  if (!el || el.classList.contains('is-flash')) return;
  el.classList.add('is-flash');
  setTimeout(() => el.classList.remove('is-flash'), Math.max(0, flashing.until - Date.now()));
}

function onScroll() {
  if (!spyFrame) spyFrame = requestAnimationFrame(spy);
}

function spy() {
  spyFrame = 0;
  if (!visible || Date.now() < spyPausedUntil) return;
  const line = scroller.getBoundingClientRect().top + 110;
  let key = SECTIONS[0][0];
  for (const [k] of SECTIONS) if (refs.sections[k].el.getBoundingClientRect().top <= line) key = k;
  if (scroller.scrollTop > 0 && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4) key = SECTIONS.at(-1)[0];
  setCurrent(key);
}

function setCurrent(key) {
  for (const [k, a] of Object.entries(refs.links)) {
    if (k === key) a.setAttribute('aria-current', 'true');
    else a.removeAttribute('aria-current');
  }
  // Keep the current entry visible in the scrolling strip.
  const nav = refs.nav;
  const a = refs.links[key];
  if (nav.scrollWidth > nav.clientWidth && (a.offsetLeft < nav.scrollLeft || a.offsetLeft + a.offsetWidth > nav.scrollLeft + nav.clientWidth)) {
    nav.scrollTo({ left: Math.max(0, a.offsetLeft - 16), behavior: 'smooth' });
  }
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------
/** Rebuild a section's content, keeping keyboard focus on the same control (by id). */
function fill(key, ...nodes) {
  const body = refs.sections[key].body;
  const focusedId = body.contains(document.activeElement) ? document.activeElement.id : '';
  append(clear(body), nodes);
  if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
  applyFlash();
}

/** Label + help + control. `setting` names the settings key it changes (for "Saved ✓").
 *  A switch stays on the right of its words even on a phone; other controls go under them. */
function row(label, help, control, { stack = false, setting = null } = {}) {
  const isSwitch = control?.classList?.contains('switch');
  return h('div', { class: ['adv-row', stack && 'adv-row-stack', isSwitch && 'adv-row-switch'], dataset: setting ? { setting } : null },
    h('div', { class: 'adv-text' }, h('div', { class: 'adv-label' }, label), help ? h('div', { class: 'adv-help' }, help) : null),
    h('div', { class: 'adv-control' }, control));
}

/** A part the app relies on: its name, then its state after a light (ok, bad, dim, on = working). */
function statusRow(state, title, detail, action, ...more) {
  return h('div', { class: ['adv-row', 'sys-row'] },
    h('div', { class: 'adv-text' },
      h('div', { class: 'adv-label' }, title),
      h('div', { class: 'adv-help sys-state' }, led(state, { blink: state === 'on' }), h('span', null, detail)),
      ...more),
    action ? h('div', { class: 'adv-control' }, action) : null);
}

/** Save one change; on failure say why (with the server's hint) and redraw the controls. Returns whether it worked.
 *  `mark: false` leaves "Saved ✓" to the caller (when it redraws the setting first). */
async function save(patch, { quiet = false, mark = true } = {}) {
  saving += 1;
  try {
    await saveSettings(patch);
    if (mark) savedMark(Object.keys(patch)[0]);
    return true;
  } catch (e) {
    if (!quiet) toastError(e);
    renderDefaults();
    renderLook();
    renderDownloads();
    renderStorage();
    if (quiet) throw e;
    return false;
  } finally {
    saving -= 1;
  }
}

/** "Saved ✓" after the label of the setting that changed; it fades after a moment. */
function savedMark(key) {
  const label = settingsView.el.querySelector(`[data-setting="${key}"] :is(.adv-label, .field-label)`);
  if (!label) return;
  label.querySelector('.saved-mark')?.remove();
  const mark = h('span', { class: 'saved-mark', 'aria-hidden': 'true' }, 'Saved', icon('check', 13));
  label.append(mark);
  // Timers, not a keyframe animation: reduced motion would cut that to nothing.
  setTimeout(() => mark.classList.add('is-out'), 1200);
  setTimeout(() => mark.remove(), 1500);
  refs.live.textContent = '';
  setTimeout(() => { refs.live.textContent = `${label.firstChild.textContent} saved.`; }, 60);
}

// ---------------------------------------------------------------------------
// Files: the output folder, shown short; "Change" opens a field with the full path
// ---------------------------------------------------------------------------
function renderFiles() {
  const s = store.settings || {};
  const locked = (s.locked || []).includes('output_dir');
  const dir = s.output_dir || store.health?.output_dir || '';
  if (locked) folderDraft = null;
  const help = h('p', { class: 'adv-help', id: 'outputDirHelp' },
    'Finished files are saved here, one folder per job when there are several files.',
    locked ? ' Set by the server’s configuration, so it can’t be changed here.' : null);
  fill('files', h('div', { class: 'folder', dataset: { spot: 'output', setting: 'output_dir' } },
    h('label', { class: 'adv-label', for: 'outputDir' }, 'Output folder'),
    folderDraft == null ? folderLine(dir, locked) : folderField(),
    help));
}

function folderLine(dir, locked) {
  const missing = store.health?.output_dir_ok === false && dir === store.health?.output_dir;
  const open = store.health?.local
    ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', id: 'openOutputDir',
      onclick: async () => { try { await api('/api/reveal-output', { method: 'POST' }); } catch (e) { toastError(e); } },
    }, icon('folder', 14), 'Open folder')
    : null;
  const change = locked ? null : h('button', {
    type: 'button', class: 'btn btn-ghost btn-sm', id: 'changeOutputDir', 'aria-label': 'Change the output folder',
    onclick: () => { openFolderField(); document.getElementById('outputDir')?.focus(); },
  }, icon('pencil', 14), 'Change');
  return h('div', { class: 'folder-line' },
    icon('folder', 16, 'folder-ico'),
    h('span', { class: 'folder-path', title: dir || null }, shortFolder(dir) || 'Not set'),
    missing ? h('span', { class: 'lib-missing' }, icon('alert', 13), 'Not available') : null,
    open || change ? h('span', { class: 'folder-actions' }, open, change) : null);
}

/** Opens the folder field with the full path (unless it is open already or set by the server). */
function openFolderField() {
  const s = store.settings || {};
  if (folderDraft != null || (s.locked || []).includes('output_dir')) return;
  folderDraft = s.output_dir || '';
  renderFiles();
}

function closeFolderField() {
  folderDraft = null;
  renderFiles();
  document.getElementById('changeOutputDir')?.focus({ preventScroll: true });
}

function folderField() {
  const input = h('input', {
    class: 'field', id: 'outputDir', value: folderDraft, spellcheck: 'false', autocomplete: 'off',
    'aria-describedby': 'outputDirError outputDirHelp', dataset: { autofocus: '' },
  });
  const saveBtn = h('button', { type: 'button', class: 'btn btn-key' }, 'Save');
  const cancel = h('button', { type: 'button', class: 'btn btn-ghost btn-quiet', onclick: closeFolderField }, 'Cancel');
  // Why the folder couldn't be saved: under the field, with what the user typed kept in it.
  const problem = h('div', { id: 'outputDirError', 'aria-live': 'polite' });
  input.addEventListener('input', () => {
    folderDraft = input.value;
    clear(problem);
    input.removeAttribute('aria-invalid');
  });
  const submit = async () => {
    const value = input.value.trim();
    if (saveBtn.disabled || !value) return;
    if (value === (store.settings?.output_dir || '')) { closeFolderField(); return; }
    saveBtn.disabled = true;
    try {
      await save({ output_dir: value }, { quiet: true, mark: false });
    } catch (e) {
      saveBtn.disabled = false;
      input.setAttribute('aria-invalid', 'true');
      clear(problem).append(callout('danger', null, e.message,
        e.hints?.length ? h('ul', { class: 'plain-list' }, e.hints.map((t) => h('li', null, t))) : null));
      return;
    }
    // The banner about an unavailable folder goes away once it's fine. Reloaded while the
    // field is still open, so its redraw doesn't take the "Saved ✓" along.
    await loadHealth().catch(() => {});
    closeFolderField();
    savedMark('output_dir');
  };
  saveBtn.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
    if (e.key === 'Escape') { e.preventDefault(); closeFolderField(); }
  });
  return [h('div', { class: 'field-row folder-edit' }, input, saveBtn, cancel), problem];
}

/** A path shown short, the full one on hover and to copy. `id` keeps focus on its button across redraws. */
function pathValue(p, what, id) {
  return h('span', { class: 'path-value' },
    h('span', { class: 'path-text', title: p }, shortFolder(p)),
    h('button', {
      type: 'button', class: 'icon-btn icon-btn-sm', id, 'aria-label': `Copy the ${what} path`, title: 'Copy the full path',
      onclick: () => copyText(p, { done: 'Path copied', title: `The ${what} path` }),
    }, icon('clipboard', 14)));
}

// ---------------------------------------------------------------------------
// Defaults and look
// ---------------------------------------------------------------------------
function renderDefaults() {
  const s = store.settings || {};
  const fm = store.health?.formats || {};
  // Each format has an (i) card with the server's facts; the cover line follows the setting below.
  // Pills say the picked format's one-line description under them; the checklist has its labels.
  const choice = (f, coverOn) => ({ value: f.key, label: f.label, info: () => formatInfo(f, coverOn && store.settings?.embed_cover !== false) });
  const pill = (f, coverOn) => ({ ...choice(f, coverOn), desc: f.description || null });
  defaultsWaiting = !fm.download;
  const loading = defaultsWaiting ? h('p', { class: 'muted small' }, 'Loading formats…') : null;
  fill('defaults',
    row('Save format', 'What “Save” uses unless you pick something else.',
      loading || segmented({
        label: 'Save format', size: 'sm', value: s.download_format,
        options: (fm.download || []).filter((f) => f.key !== 'video').map((f) => pill(f, true)),
        onChange: (v) => save({ download_format: v }),
      }), { stack: true, setting: 'download_format' }),
    row('Enhance saves', 'Formats ticked for every Enhance job.',
      loading || chips({
        label: 'Enhance formats', size: 'sm', values: new Set(s.enhance_formats || []),
        options: (fm.enhance || []).map((f) => choice(f, true)),
        // In the order shown, not the order clicked.
        onChange: (next) => {
          if (next.size) save({ enhance_formats: (fm.enhance || []).map((f) => f.key).filter((k) => next.has(k)) });
          else { toast('Keep at least one format.', { kind: 'warning' }); renderDefaults(); }
        },
      }), { stack: true, setting: 'enhance_formats' }),
    row('Split format', 'File type for separated stems.',
      loading || segmented({
        label: 'Split format', size: 'sm', value: s.separate_format,
        options: (fm.separate || []).map((f) => pill(f, false)),
        onChange: (v) => save({ separate_format: v }),
      }), { stack: true, setting: 'separate_format' }),
    row('Cover art', 'Puts the video thumbnail (or a file’s own picture) into saved and enhanced songs as album art.',
      switchControl({ checked: s.embed_cover, label: 'Cover art', onChange: (v) => save({ embed_cover: v }) }), { setting: 'embed_cover' }));
}

function renderLook() {
  const s = store.settings || {};
  fill('look',
    row('Theme', null,
      segmented({
        label: 'Theme', size: 'sm', value: s.theme || 'auto',
        options: [{ value: 'auto', label: 'Automatic' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }],
        onChange: (v) => save({ theme: v }),
      }), { setting: 'theme' }),
    // Stored as ui_mode ('advanced' = on). Changing it also forgets what was opened or
    // closed one by one, so the new default shows everywhere.
    row('Show all options by default', 'Opens “More options” for every task and shows the details (measurements, decisions, full log) on job pages. Each can still be opened or closed on its own.',
      switchControl({
        id: 'showAllOptions', checked: s.ui_mode === 'advanced', label: 'Show all options by default',
        onChange: (v) => { forgetShown(); save({ ui_mode: v ? 'advanced' : 'simple' }); },
      }), { setting: 'ui_mode' }));
}

// ---------------------------------------------------------------------------
// Downloads & sign-in: the downloader, browser sign-in, the bookmarklet
// ---------------------------------------------------------------------------
function renderDownloads() {
  const dl = store.health?.downloader || {};
  const age = dl.age_days;
  const stale = dl.ok && age != null && age > STALE_DOWNLOADER_DAYS;
  const updateBtn = h('button', { type: 'button', class: 'btn btn-key btn-sm', id: 'updateDownloader' }, icon('retry', 14), 'Update downloader');
  updateBtn.addEventListener('click', async () => {
    updateBtn.disabled = true;
    updateBtn.textContent = 'Updating…';
    try {
      const res = await api('/api/system/update-downloader', { method: 'POST' });
      toast(res?.message || (res?.ok ? 'Downloader is up to date.' : 'The update did not work.'), { kind: res?.ok ? 'ok' : 'danger', timeout: 6000 });
      await loadHealth();
    } catch (e) {
      toastError(e);
      updateBtn.disabled = false;
      updateBtn.textContent = 'Update downloader';
    }
  });
  fill('downloads',
    statusRow(dl.ok ? 'ok' : 'bad', 'Downloader (yt-dlp)', dl.ok ? `Version ${dl.version || '?'} — update it if downloads start failing` : 'Not available', updateBtn,
      stale ? h('div', { class: 'adv-help nudge' },
        `This version is ${Math.round(age)} days old. Sites like YouTube change often — updating takes a moment.`) : null),
    row('Fix the downloader automatically', 'When a download fails because the site changed, update the downloader and try once more.',
      switchControl({
        id: 'autoUpdateDownloader',
        checked: (store.settings || {}).auto_update_downloader !== false, label: 'Fix the downloader automatically',
        onChange: (v) => save({ auto_update_downloader: v }),
      }), { setting: 'auto_update_downloader' }),
    signInRow(),
    bookmarklet());
}

function signInRow() {
  const s = store.settings || {};
  if (!store.health?.local || !('cookies_browser' in s)) return null;
  const locked = (s.locked || []).includes('cookies_browser');
  const select = h('select', {
    class: 'field field-select', id: 'cookiesBrowser', 'aria-label': 'YouTube sign-in', disabled: locked,
    dataset: { autofocus: '' },
  }, SIGN_IN_BROWSERS.map(([value, label]) => h('option', { value, selected: (s.cookies_browser || '') === value }, label)));
  select.addEventListener('change', () => save({ cookies_browser: select.value }));
  return h('div', { dataset: { spot: 'cookies' } },
    row('YouTube sign-in',
      'Only needed for age-restricted videos or when YouTube asks to confirm you’re not a bot. Uses your browser’s existing sign-in on this computer; nothing is sent anywhere else. Firefox works most reliably.',
      select, { setting: 'cookies_browser' }));
}

function bookmarkletHref() {
  // Never put the access token in here: the bookmarklet runs inside the visited page,
  // which could read it. The AudioLab tab it opens has its own stored token (or asks).
  const base = new URL('./', location.href).href.replace(/#.*$/, '');
  const prefix = `${base}?url=`;
  // The whole script is percent-encoded: browsers decode javascript: URLs before running them.
  const code = `void(window.open(${JSON.stringify(prefix)}+encodeURIComponent(location.href),'_blank'))`;
  return `javascript:${encodeURIComponent(code)}`;
}

function bookmarklet() {
  const href = bookmarkletHref();
  const name = `Send to ${appName()}`;
  const link = h('a', { class: 'bookmarklet', href, draggable: 'true', title: 'Drag this to your bookmarks bar' }, icon('bookmark', 15), name);
  link.addEventListener('click', (e) => {
    e.preventDefault();
    toast('Drag the button to your bookmarks bar, then click it on a YouTube page.', { kind: 'info' });
  });
  const copy = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', id: 'copyBookmarklet' }, icon('clipboard', 14), 'Copy as text');
  copy.addEventListener('click', () => copyText(href, { done: 'Copied — paste it as the address of a new bookmark.', title: name }));
  return h('div', { dataset: { spot: 'shortcut' } },
    row(name, 'Drag this to your bookmarks bar. On a YouTube page, click it to open the video here.',
      h('div', { class: 'bm-row' }, link, copy)));
}

// ---------------------------------------------------------------------------
// AI & system
// ---------------------------------------------------------------------------
function renderSystem() {
  const ff = store.health?.ffmpeg || {};
  const ai = store.health?.ai || {};
  let aiAction = null;
  let aiDetail;
  if (ai.available) {
    aiDetail = `Ready${ai.device ? ` · runs on ${deviceLabel(ai.device)}` : ''}${ai.device === 'cpu' ? ' (works, but slowly)' : ''}`;
  } else if (ai.installing) {
    aiDetail = 'Installing… this can take a few minutes.';
  } else {
    aiDetail = ai.reason || 'Not installed. Needed for Split and for Enhance → Best.';
    // The one main action on this page when it's there (the AI status in the sidebar links here).
    aiAction = h('button', { type: 'button', class: 'btn btn-primary btn-sm', id: 'enableAi' }, icon('download', 14), 'Enable AI features');
    aiAction.addEventListener('click', async () => {
      aiAction.disabled = true;
      try {
        await api('/api/system/install-ai', { method: 'POST' });
        toast('Installing the AI add-on…', { kind: 'ok' });
        await loadHealth();
      } catch (e) {
        aiAction.disabled = false;
        toastError(e);
      }
    });
  }
  fill('system',
    !ff.ok && ff.error ? callout('danger', 'Audio engine missing', ff.error) : null,
    h('div', { dataset: { spot: 'ai' } },
      // Not installed is a choice, not a fault: a dim light, not a red one.
      statusRow(ai.available ? 'ok' : ai.installing ? 'on' : 'dim', 'AI add-on', aiDetail, aiAction,
        !ai.available && !ai.installing ? h('div', { class: 'adv-help' }, 'Adds stem splitting and the “Best” quality for Enhance. It is a one-time download of about 1–2 GB and runs entirely on this computer.') : null),
      ai.installing || (ai.install_log && !ai.available) ? h('pre', { class: 'install-log' }, ai.install_log || 'Starting…') : null),
    statusRow(ff.ok ? 'ok' : 'bad', 'Audio engine (ffmpeg)', ff.ok ? `Version ${ff.version || '?'} · ${ff.source === 'bundled' ? 'built in' : 'installed on this computer'}` : (ff.error || 'Not found')));
}

// ---------------------------------------------------------------------------
// Storage used by the app itself (not the output files)
// ---------------------------------------------------------------------------
function renderStorage() {
  const s = store.settings || {};
  fill('storage',
    refs.storageInfo,
    row('Keep work files', 'Keeps the big in-between files (decoded audio, raw stems) after a job finishes. Only useful for troubleshooting — uses a lot of disk.',
      switchControl({ checked: s.keep_work_files, label: 'Keep work files', onChange: (v) => save({ keep_work_files: v }) }), { setting: 'keep_work_files' }),
    row('Keep originals for comparing', `Keeps the downloaded or uploaded original of Enhance results in ${appName()}’s own data, so the Library can compare them at full length (about 1 MB per minute for links). Turning it off doesn’t delete the ones already kept.`,
      switchControl({ checked: s.keep_originals !== false, label: 'Keep originals for comparing', onChange: (v) => save({ keep_originals: v }) }), { setting: 'keep_originals' }));
}

async function loadStorage() {
  let data = null;
  try { data = await api('/api/storage'); } catch { data = null; } // older server: no line
  storage.data = data;
  storage.key = null;
  if (visible) renderStorageInfo();
}

function renderStorageInfo() {
  const el = refs.storageInfo;
  const d = storage.data;
  if (!d || d.jobs_bytes == null) { el.hidden = true; return; }
  const finished = [...store.jobs.values()].filter(isFinished).length;
  // Jobs finished or were cleared elsewhere: the sizes are stale, fetch them again.
  if (storage.finished != null && storage.finished !== finished) {
    clearTimeout(storage.timer);
    storage.timer = setTimeout(loadStorage, STORAGE_CACHE_MS);
  }
  storage.finished = finished;
  // `jobs` fires many times a second while a job runs: only rebuild on a real change,
  // so the button isn't swapped out under a click.
  const key = [d.jobs_bytes, d.finished_bytes, d.models_bytes, d.originals_bytes, d.originals_count, finished].join('|');
  if (key === storage.key && !el.hidden) return;
  storage.key = key;
  const btn = h('button', { type: 'button', class: 'btn btn-ghost btn-danger-text btn-sm', disabled: !finished }, icon('trash', 14), 'Clear finished jobs');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const ok = await clearFinished();
    // The server caches these numbers for a few seconds: show the expected result now.
    if (ok) storage.data = { ...d, jobs_bytes: Math.max(0, d.jobs_bytes - (d.finished_bytes || 0)), finished_bytes: 0 };
    storage.finished = null;
    storage.key = null; // rebuild even if clearing failed (re-enables the button)
    renderStorageInfo();
    clearTimeout(storage.timer);
    storage.timer = setTimeout(loadStorage, STORAGE_CACHE_MS);
    // The button is now disabled (nothing left to clear): keep focus nearby.
    el.focus({ preventScroll: true });
  });
  const extra = [];
  if (finished && d.finished_bytes) extra.push(`Clearing frees about ${fmtBytes(d.finished_bytes)}.`);
  if (d.models_bytes) extra.push(`AI models: ${fmtBytes(d.models_bytes)}.`);
  el.hidden = false;
  clear(el).append(row(
    `${appName()}’s working data: ${fmtBytes(d.jobs_bytes)} · ${finished} finished job${finished === 1 ? '' : 's'}`,
    ['Logs, previews and copies kept for each job. Your files in the output folder aren’t counted and are never deleted.', ...extra].join(' '),
    btn), originalsRow(d));
}

/** The originals kept for the Library's compare player, with "Delete kept originals". */
function originalsRow(d) {
  if (d.originals_bytes == null) return null; // older server
  const n = d.originals_count || 0;
  const del = h('button', { type: 'button', class: 'btn btn-ghost btn-danger-text btn-sm', disabled: !n }, icon('trash', 14), 'Delete kept originals');
  del.addEventListener('click', async () => {
    const ok = await confirmDialog({
      title: 'Delete the kept originals?',
      // "Up to": a copy of an upload shares its disk space with the job's file while that job is kept.
      message: `This frees up to ${fmtBytes(d.originals_bytes)}. Library results can no longer be compared with their originals. Your output files aren’t touched.`,
      confirm: 'Delete originals',
    });
    if (!ok) return;
    del.disabled = true;
    try {
      const res = await api('/api/library/originals', { method: 'DELETE' });
      toast(`Deleted ${res?.deleted ?? n} kept original${(res?.deleted ?? n) === 1 ? '' : 's'} — ${fmtBytes(res?.freed_bytes ?? 0)} freed.`, { kind: 'ok' });
    } catch (e) {
      toastError(e);
    }
    await loadStorage(); // rebuilds the row (and re-enables the button if that didn't work)
    refs.storageInfo.focus({ preventScroll: true });
  });
  return row(
    `Kept originals: ${n ? `${fmtBytes(d.originals_bytes)} · ${n} file${n === 1 ? '' : 's'}` : 'none'}`,
    `Copies of the originals of Enhance results, so the Library can compare them at full length. In ${appName()}’s own data — your output files aren’t touched.`,
    del);
}

// ---------------------------------------------------------------------------
// About: version, folders, limits, the system report
// ---------------------------------------------------------------------------
function renderAbout() {
  const hth = store.health || {};
  fill('about',
    h('dl', { class: 'facts' },
      [h('dt', null, 'Version'), h('dd', null, `${appName()} ${hth.version || ''}`)],
      hth.output_dir ? [h('dt', null, 'Output folder'), h('dd', null, pathValue(hth.output_dir, 'output folder', 'copyOutputDir'))] : null,
      hth.data_dir ? [h('dt', null, 'Work folder'), h('dd', null, pathValue(hth.data_dir, 'work folder', 'copyDataDir'))] : null,
      hth.limits?.max_duration_s ? [h('dt', null, 'Longest allowed'), h('dd', null, durationLimit(hth.limits.max_duration_s))] : null),
    h('div', { class: 'block-actions' },
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', id: 'copyReport', onclick: () => copyDiagnostics() }, icon('clipboard', 15), 'Copy system report'),
      h('span', { class: 'muted small' }, 'For bug reports: versions, tools and recent log lines. No tokens.')));
}

/** "8 hours", "30 min", "1 h 30 min". */
function durationLimit(s) {
  const hours = s / 3600;
  if (Number.isInteger(hours)) return `${hours} hour${hours === 1 ? '' : 's'}`;
  return fmtDurationWords(s);
}
