// Library (#/library[/<entry id>]): finished results grouped by recording, newest
// first. Each recording is a sleeve row (cover, title, credits), then its versions (Saved,
// Enhanced, Stems…): a head line with the full-length comparison with the kept original,
// Show in Finder, Open job and Remove (never deletes files), then the files — play,
// download, or "Moved or deleted" when a file is gone. A kept original is AudioLab's own
// copy, shared by the versions of one recording, and can be deleted per recording here or
// all at once in Settings → Storage.
// One player is open at a time. The list is fetched on show and when a job finishes;
// cards are kept and only rebuilt when their data changes, so a player keeps going
// through a reload. #/library/<id> scrolls to that version and opens its comparison.
import { h, icon, clear, append, fmtBytes, fmtDuration, fmtAgo } from '../dom.js';
import { api } from '../api.js';
import { store, on } from '../store.js';
import { toast, toastError, callout, confirmDialog } from '../ui.js';
import { jobPath, currentRoute } from '../router.js';
import { libraryCompare, plainPlayer, libraryFileUrl, codecName } from '../compare.js';

const TASK_ICON = { download: 'download', enhance: 'sparkle', separate: 'layers' };
const REVEAL_LABEL = /Mac/i.test(navigator.platform || navigator.userAgent) ? 'Show in Finder' : 'Show in folder';
const FLASH_MS = 1800;
const libPath = (id, what = '') => `/api/library/${encodeURIComponent(id)}${what ? `/${what}` : ''}`;

let refs = null;
let scroller = null;
let visible = false;
let groups = null;   // the last GET /api/library (null until the first answer)
let loadSeq = 0;
let reloadTimer = 0;
let doneKey = '';    // finished jobs with a Library entry: the list reloads when this changes
let target = null;   // #/library/<id> still to scroll to
let player = null;   // the open player: { entryId, what: 'compare' | file id, fileId, title, el, stage, pause, destroy }
let uid = 0;
const groupEls = new Map(); // group key → { el, head, list, titleId, sig }
const cards = new Map();    // entry id → { el, entry, sig, stage, toggles: Map(what → button) }

export const libraryView = {
  el: null,
  heading: null,

  mount(el) {
    this.el = el;
    scroller = el.closest('.content');
    refs = {
      title: h('h1', { class: 'page-title', id: 'libraryTitle', tabindex: '-1' }, 'Library'),
      sub: h('p', { class: 'page-sub' }),
      reassure: h('p', { class: 'lib-reassure', hidden: true }, 'Removing one here never deletes your files.'),
      error: h('div', { hidden: true }),
      empty: h('p', { class: 'filter-empty', hidden: true },
        'Finished results from Save, Enhance and Split show up here. ', h('a', { href: '#/new' }, 'Start something new')),
      list: h('div', { class: 'lib-groups' }),
    };
    this.heading = refs.title;
    el.append(h('div', { class: 'page' },
      h('header', { class: 'page-head' }, h('div', { class: 'page-head-text' }, refs.title, refs.sub, refs.reassure)),
      refs.error, refs.empty, refs.list));
    on('jobs', onJobs);
  },

  show(arg) {
    visible = true;
    target = arg || null;
    // A known list can be scrolled now; otherwise once it has loaded.
    if (target && !(groups && revealTarget())) {
      scroller.scrollTop = 0;
      refs.title.focus({ preventScroll: true });
    }
    load();
    return !!arg;
  },

  hide() {
    visible = false;
    target = null;
    clearTimeout(reloadTimer);
    closePlayer();
  },
};

// A job that finishes (or a finished one removed from the list) changes the Library.
function onJobs() {
  const key = [...store.jobs.values()]
    .filter((j) => j.status === 'done' && j.result?.library_id).map((j) => j.id).sort().join(' ');
  if (key === doneKey) return;
  doneKey = key;
  if (!visible) return;
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(load, 300);
}

async function load() {
  const seq = ++loadSeq;
  try {
    const data = await api('/api/library');
    if (seq !== loadSeq || !visible) return;
    groups = data?.groups || [];
    refs.error.hidden = true;
  } catch (e) {
    if (seq !== loadSeq || !visible) return;
    if (groups) toast(e.message, { kind: 'danger' });
    else {
      const retry = h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: () => load() }, icon('retry', 14), 'Try again');
      clear(refs.error).append(callout('danger', 'The Library couldn’t be loaded', e.message, h('div', { class: 'callout-actions' }, retry)));
      refs.error.hidden = false;
      refs.sub.textContent = '';
      refs.reassure.hidden = true;
      return;
    }
  }
  render();
  if (target) revealTarget(true);
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------
function render() {
  const list = groups || [];
  const count = list.reduce((n, g) => n + g.entries.length, 0);
  refs.sub.textContent = !count ? 'Your finished results and their versions.'
    : `${count} result${count === 1 ? '' : 's'} from ${list.length} recording${list.length === 1 ? '' : 's'}.`;
  refs.reassure.hidden = !count;
  refs.empty.hidden = count > 0;

  const ids = new Set(list.flatMap((g) => g.entries.map((e) => e.id)));
  for (const [id, c] of cards) {
    if (ids.has(id)) continue;
    if (player?.entryId === id) closePlayer();
    c.el.remove();
    cards.delete(id);
  }
  const els = list.map((g) => groupFor(g).el);
  const keys = new Set(list.map((g) => g.key));
  for (const [key, grp] of groupEls) {
    if (!keys.has(key)) { grp.el.remove(); groupEls.delete(key); }
  }
  syncChildren(refs.list, els);
}

/** Put `els` into `parent` in order, moving only what is out of place (moving a node drops its focus). */
function syncChildren(parent, els) {
  els.forEach((el, i) => {
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
  });
  while (parent.children.length > els.length) parent.lastElementChild.remove();
}

function groupFor(g) {
  let grp = groupEls.get(g.key);
  if (!grp) {
    const titleId = `lib-g${++uid}`;
    const head = h('div');
    const list = h('ol', { class: 'lib-entries' });
    grp = { el: h('section', { class: 'block lib-group', 'aria-labelledby': titleId }, head, list), head, list, titleId, sig: null };
    groupEls.set(g.key, grp);
  }
  const kept = g.entries.find((e) => e.original?.kept) || null;
  const cover = coverOf(g);
  const sig = JSON.stringify([g.title, g.artist, g.source, g.latest_at, g.entries.length, cover, kept?.id, kept?.original]);
  if (sig !== grp.sig) {
    grp.sig = sig;
    append(clear(grp.head), [groupHead(g, grp.titleId, cover), kept ? originalRow(g, kept) : null]);
  }
  syncChildren(grp.list, g.entries.map((e) => cardFor(e).el));
  return grp;
}

/** The recording's cover: AudioLab's own saved copy when a version has one, else the link's thumbnail. */
function coverOf(g) {
  for (const e of g.entries) {
    const f = (e.files || []).find((x) => x.kind === 'cover' && x.exists);
    if (f) return libraryFileUrl(e.id, f.id);
  }
  return g.source?.thumbnail || null;
}

/** The sleeve row: cover (or a quiet square with the task's icon), title, one credits line. */
function groupHead(g, titleId, cover) {
  const src = g.source || {};
  const thumb = h('div', { class: 'job-thumb lib-cover' });
  const fallback = () => icon(TASK_ICON[g.entries[0]?.task] || 'music', 24);
  if (cover) {
    const img = h('img', { src: cover, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', () => clear(thumb).append(fallback()));
    thumb.append(img);
  } else {
    thumb.append(fallback());
  }
  const duration = src.duration_s || g.entries[0]?.duration_s;
  const meta = [g.artist, sourceText(src), fmtDuration(duration), dateText(g.latest_at)].filter(Boolean).join(' · ');
  return h('header', { class: 'lib-head' },
    thumb,
    h('div', { class: 'lib-head-text' },
      h('h2', { class: 'lib-title', id: titleId, tabindex: '-1' }, g.title || 'Untitled'),
      h('p', { class: 'lib-meta', title: src.kind === 'url' ? src.url : src.name || null }, meta)));
}

function sourceText(src) {
  if (src?.kind !== 'url' || !src.url) return 'File';
  try { return new URL(src.url).hostname.replace(/^www\./, ''); } catch { return 'Link'; }
}

/** The kept original of a recording, one quiet line: shared by its versions, AudioLab's own copy. */
function originalRow(g, entry) {
  const o = entry.original;
  const file = (entry.files || []).find((f) => f.id === 'original');
  const facts = [codecName(o.codec, o.format) || (o.format || '').toUpperCase(), fmtBytes(o.size_bytes)]
    .filter(Boolean).map((t) => t.replace(/ /g, '\u00a0')); // "29.3 MB" never breaks
  const what = ['Original kept for comparing', ...facts].join(' · ');
  return h('div', { class: 'lib-original' },
    h('p', { class: 'lib-original-text' },
      h('strong', null, what), ` — in ${store.health?.app || 'AudioLab'}’s own data, not your output folder.`),
    h('div', { class: 'lib-original-actions' },
      h('a', {
        class: 'btn btn-ghost btn-sm', href: libraryFileUrl(entry.id, 'original', { download: true }),
        download: file?.name || true, 'aria-label': 'Download the original copy',
      }, icon('download', 14), 'Download'),
      h('button', {
        type: 'button', class: 'btn btn-ghost btn-danger-text btn-sm', onclick: () => deleteOriginal(g, entry),
      }, icon('trash', 14), 'Delete original copy')));
}

// ---------------------------------------------------------------------------
// One version
// ---------------------------------------------------------------------------
function cardFor(entry) {
  const sig = JSON.stringify(entry);
  let c = cards.get(entry.id);
  if (c && c.sig === sig) return c;
  if (!c) {
    c = { el: h('li', { class: 'lib-entry', id: `lib-${entry.id}`, tabindex: '-1' }), toggles: new Map() };
    cards.set(entry.id, c);
  }
  c.sig = sig;
  c.entry = entry;
  fillCard(c, entry);
  return c;
}

function fillCard(c, entry) {
  // The open player stays when its files are still there (it is moved, not rebuilt).
  const keep = player?.entryId === entry.id && playerStillFits(entry);
  if (player?.entryId === entry.id && !keep) closePlayer();
  const focused = c.el.contains(document.activeElement) ? document.activeElement : null;
  const focusIndex = focused ? [...c.el.querySelectorAll('a, button, summary')].indexOf(focused) : -1;

  const titleId = `${c.el.id}-title`;
  c.el.setAttribute('aria-labelledby', titleId);
  c.toggles = new Map();
  const files = (entry.files || []).filter((f) => f.kind !== 'original' && f.kind !== 'track');
  const tracks = (entry.files || []).filter((f) => f.kind === 'track').sort((a, b) => (a.track ?? 0) - (b.track ?? 0));
  c.stage = h('div', { class: 'lib-stage', id: `${c.el.id}-player`, hidden: true });
  const when = dateText(entry.created_at, true);
  append(clear(c.el), [
    h('div', { class: 'lib-entry-head' },
      h('div', { class: 'lib-entry-line' },
        h('h3', { class: 'lib-entry-title', id: titleId }, versionTitle(entry)),
        when ? h('span', { class: 'lib-when' }, ` · ${when}`) : null),
      entryActions(c, entry)),
    files.length ? h('ul', { class: 'files' }, files.map((f) => fileRow(c, entry, f))) : null,
    tracks.length ? h('details', {
      class: 'disclosure lib-tracks', open: keep && tracks.some((t) => t.id === player.what),
    }, h('summary', null, `Tracks (${tracks.length})`), h('ul', { class: 'files' }, tracks.map((f) => fileRow(c, entry, f)))) : null,
    c.stage]);
  if (keep) attachPlayer(c);
  updateToggles();
  if (focusIndex >= 0) {
    const targets = [...c.el.querySelectorAll('a, button, summary')];
    (targets[focusIndex] || c.el).focus({ preventScroll: true });
  }
}

function taskLabel(entry) {
  if (entry.task === 'download') return entry.source?.kind === 'file' ? 'Converted' : 'Saved';
  return { enhance: 'Enhanced', separate: 'Stems' }[entry.task] || entry.task;
}

/** "Enhanced as Song / music" with the task's word marked; a headline without one ("Volume evened out") gets it in front. */
function versionTitle(entry) {
  const text = entry.headline || entry.title || '';
  const verb = /^(Saved|Converted|Enhanced|Split)\b/.exec(text)?.[0];
  if (verb) return [h('span', { class: 'lib-task' }, verb), text.slice(verb.length)];
  return [h('span', { class: 'lib-task' }, taskLabel(entry)), text ? ` · ${text}` : ''];
}

function fileRow(c, entry, f) {
  const label = f.label || f.stem || f.name;
  let actions;
  if (!f.exists) {
    actions = h('span', { class: 'lib-missing' }, icon('alert', 14), 'Moved or deleted');
  } else {
    const play = f.playable ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm lib-play', 'aria-controls': c.stage.id,
      dataset: { label }, onclick: () => togglePlayer(c, f.id),
    }) : null;
    if (play) {
      c.toggles.set(f.id, play);
      playLabel(play, false);
    }
    actions = [play, h('a', {
      class: 'btn btn-ghost btn-sm', href: libraryFileUrl(entry.id, f.id, { download: true }), download: f.name,
      'aria-label': `Download ${label}`,
    }, icon('download', 14), h('span', { class: 'dl-text' }, 'Download'))];
  }
  return h('li', { class: ['file-row', !f.exists && 'is-missing'] },
    f.kind === 'cover' && f.exists ? coverThumb(libraryFileUrl(entry.id, f.id))
      : icon(f.kind === 'stem' ? 'layers' : f.kind === 'cover' ? 'image' : 'music', 18, 'file-ico'),
    h('div', { class: 'file-text' },
      h('strong', null, label),
      // Narrow windows show the size after the name instead of in its own column.
      h('span', { class: 'file-name', title: f.path || f.name }, f.name,
        f.exists && f.size_bytes ? h('span', { class: 'file-size-inline' }, ` · ${fmtBytes(f.size_bytes)}`) : null)),
    h('span', { class: 'file-size' }, f.exists ? fmtBytes(f.size_bytes) : ''),
    h('div', { class: 'lib-file-actions' }, actions));
}

/** A saved cover's picture in place of the icon (the icon when it won't load). */
function coverThumb(src) {
  const box = h('span', { class: 'file-thumb' });
  const img = h('img', { src, alt: '', loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', () => clear(box).append(icon('image', 18, 'file-ico')));
  box.append(img);
  return box;
}

function entryActions(c, entry) {
  const btns = [];
  if (compareFile(entry)) {
    const b = h('button', {
      type: 'button', class: 'btn btn-key btn-sm', 'aria-expanded': 'false', 'aria-controls': c.stage.id,
      onclick: (e) => togglePlayer(c, 'compare', e),
    }, icon('compare', 15), 'Compare with the original');
    c.toggles.set('compare', b);
    btns.push(b);
  }
  if (store.health?.local) {
    btns.push(h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => revealEntry(entry) }, icon('folder', 15), REVEAL_LABEL));
  }
  if (entry.job_exists) btns.push(h('a', { class: 'btn btn-ghost btn-sm', href: jobPath(entry.job_id) }, icon('arrow', 15), 'Open job'));
  btns.push(h('button', {
    type: 'button', class: 'btn btn-ghost btn-quiet btn-sm', title: 'Your files stay where they are',
    onclick: () => removeEntry(entry),
  }, icon('trash', 15), 'Remove from Library'));
  return h('div', { class: 'lib-actions' }, btns);
}

/** The result the kept original is compared with: the server's pick (lossless first). */
function compareFile(entry) {
  if (!entry.original?.kept || !entry.compare_file_id) return null;
  return (entry.files || []).find((f) => f.id === entry.compare_file_id) || null;
}

// ---------------------------------------------------------------------------
// The player (one at a time)
// ---------------------------------------------------------------------------
function togglePlayer(c, what, event) {
  const same = player?.entryId === c.entry.id && player.what === what;
  // Only full-length comparisons share a transport, and only inside the server's
  // recording group. Plain files may be stems/tracks with a different timeline.
  const group = c.entry.group;
  const state = !same && what === 'compare' && player?.what === 'compare'
    && typeof group === 'string' && group && player.group === group ? player.snapshot?.() : null;
  closePlayer();
  if (same) return;
  const entry = c.entry;
  if (what === 'compare') {
    const result = compareFile(entry);
    if (!result) return;
    const p = libraryCompare(entry);
    player = { ...p, entryId: entry.id, what, group, fileId: result.id, title: 'Comparing with the original' };
    attachPlayer(c);
    // Route reveals also open players; they retain the position but never start sound.
    p.restore?.(state, { resume: event?.isTrusted === true });
  } else {
    const file = (entry.files || []).find((f) => f.id === what);
    if (!file) return;
    const p = plainPlayer(entry, file);
    player = { ...p, entryId: entry.id, what, fileId: file.id, title: file.label || file.name };
    attachPlayer(c);
    p.play(); // still inside the click: Safari only lets a user gesture start sound
  }
  updateToggles();
}

function attachPlayer(c) {
  const close = h('button', {
    type: 'button', class: 'icon-btn icon-btn-sm', 'aria-label': 'Close the player', title: 'Close',
    onclick: () => {
      const toggle = c.toggles.get(player?.what);
      closePlayer();
      (toggle || c.el).focus({ preventScroll: true });
    },
  }, icon('close', 15));
  clear(c.stage).append(h('div', { class: 'lib-stage-head' }, h('strong', null, player.title), close), player.el);
  c.stage.hidden = false;
  player.stage = c.stage;
}

function closePlayer() {
  if (!player) return;
  const { stage } = player;
  player.destroy();
  player = null;
  if (stage) { clear(stage); stage.hidden = true; }
  updateToggles();
}

function playerStillFits(entry) {
  if (player.what === 'compare') return compareFile(entry)?.id === player.fileId;
  return (entry.files || []).some((f) => f.id === player.what && f.exists);
}

function updateToggles() {
  for (const c of cards.values()) {
    for (const [what, b] of c.toggles) {
      const on = !!player && player.entryId === c.entry.id && player.what === what;
      if (what === 'compare') b.setAttribute('aria-expanded', String(on));
      else if (b.classList.contains('is-on') !== on) playLabel(b, on);
    }
  }
}

/** A file's Play button says what it does: "Play", or "Stop" while its player is open. */
function playLabel(b, on) {
  b.classList.toggle('is-on', on);
  b.setAttribute('aria-label', `${on ? 'Stop' : 'Play'} ${b.dataset.label}`);
  clear(b).append(icon(on ? 'stop' : 'play', 14), on ? 'Stop' : 'Play');
}

// ---------------------------------------------------------------------------
// #/library/<id>
// ---------------------------------------------------------------------------
/** Scroll to the linked version, highlight it, open its comparison. `final`: the list is fresh. */
function revealTarget(final = false) {
  const id = target;
  const route = currentRoute();
  if (!visible || route?.name !== 'library' || route.arg !== id) { target = null; return false; }
  const c = cards.get(id);
  if (!c) {
    if (final) {
      target = null;
      toast('That result is no longer in the Library.', { kind: 'info' });
    }
    return false;
  }
  target = null;
  c.el.scrollIntoView({ block: 'start' });
  c.el.focus({ preventScroll: true });
  c.el.classList.remove('is-flash');
  void c.el.offsetWidth; // restart the highlight
  c.el.classList.add('is-flash');
  setTimeout(() => c.el.classList.remove('is-flash'), FLASH_MS);
  if (compareFile(c.entry) && !(player?.entryId === id && player.what === 'compare')) togglePlayer(c, 'compare');
  return true;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function revealEntry(entry) {
  try {
    const res = await api(libPath(entry.id, 'reveal'), { method: 'POST' });
    if (res?.fallback === 'output') toast('That folder was moved or deleted — opened the output folder instead.', { kind: 'info', timeout: 7000 });
  } catch (e) {
    toastError(e);
  }
}

async function removeEntry(entry) {
  const group = (groups || []).find((g) => g.entries.some((e) => e.id === entry.id));
  const lastOriginal = entry.original?.kept && !group?.entries.some((e) => e.id !== entry.id && e.original?.kept);
  const ok = await confirmDialog({
    title: 'Remove from the Library?',
    message: `“${entry.title}” (${taskLabel(entry)}) is taken off this list. Your files stay where they are.${
      lastOriginal ? ' The kept copy of the original goes with it.' : ''}`,
    confirm: 'Remove',
  });
  if (!ok) return;
  try {
    await api(libPath(entry.id), { method: 'DELETE' });
    toast('Removed from the Library. Your files stay where they are.', { kind: 'ok' });
  } catch (e) {
    toast(e.message, { kind: 'danger' });
  }
  refs.title.focus({ preventScroll: true });
  await load();
}

async function deleteOriginal(g, entry) {
  const n = g.entries.length;
  const ok = await confirmDialog({
    title: 'Delete the original copy?',
    // No size here: a copy can share its disk space with the job's upload, so the toast says what was freed.
    message: `This affects ${n > 1 ? `all ${n} versions` : 'every version'} of “${g.title}”: `
      + 'they can no longer be compared with the original. Your own files stay where they are.',
    confirm: 'Delete copy',
  });
  if (!ok) return;
  try {
    const res = await api(libPath(entry.id, 'original'), { method: 'DELETE' });
    toast(res?.freed_bytes ? `Original copy deleted — ${fmtBytes(res.freed_bytes)} freed.` : 'Original copy deleted.', { kind: 'ok' });
  } catch (e) {
    toast(e.message, { kind: 'danger' });
  }
  // The row with the button goes away: keep keyboard focus on the page, then on the recording.
  refs.title.focus({ preventScroll: true });
  await load();
  document.getElementById(groupEls.get(g.key)?.titleId)?.focus({ preventScroll: true });
}

// ---------------------------------------------------------------------------
// Dates
// ---------------------------------------------------------------------------
/** "Today" / "Yesterday" / "6 Oct" (with the year when it isn't this one); `time` adds the clock time or "12 min ago". */
function dateText(ts, time = false) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
  if (days === 0 && time) return fmtAgo(ts);
  const day = days === 0 ? 'Today' : days === 1 ? 'Yesterday'
    : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' });
  return time ? `${day}, ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : day;
}

function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}
