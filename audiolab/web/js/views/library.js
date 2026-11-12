// Library (#/library[/<entry id>]): finished results grouped by recording, newest first or
// largest first, searchable by title and artist. Each recording is a sleeve row (cover, title,
// credits), then its versions (Saved, Enhanced, Stems…): a head line with the full-length
// comparison with the kept original, Adjust and Compare versions, the rest (Show in
// Finder, Open job, Keep only, Remove — files stay unless the user opts in to the Trash) in a ⋯ menu,
// then the files — play, download, or "Moved or deleted" when a file is gone. A kept original is
// AudioLab's own copy, shared by the versions of one recording, and can be deleted per recording
// here or all at once in Settings → Storage.
// One player is open at a time. The list is fetched on show and when a job finishes;
// cards are kept and only rebuilt when their data changes, so a player keeps going
// through a reload. #/library/<id> scrolls to that version and opens its comparison.
import { h, icon, clear, append, fmtBytes, fmtDuration, fmtAgo, fmtNum, fmtSigned, shortFolder } from '../dom.js';
import { api } from '../api.js';
import { store, on, upsertJob } from '../store.js';
import { toast, toastError, callout, confirmDialog, segmented, switchControl, menuButton, closeMenu } from '../ui.js';
import { go, jobPath, currentRoute } from '../router.js';
import {
  libraryCompare, plainPlayer, libraryFileUrl, codecName, versionCompare, versionFile, versionLoudness, loudnessSpread,
  streamLine,
} from '../compare.js';
import { adjustLink, adjustUnavailable } from './adjust.js';
import { PROCESSING_LABELS, LIGHT_KEYS } from './new/state.js';
import { eqSummary } from '../eq.js';
import { sectionSummary } from '../section-eq.js';

const TASK_ICON = { download: 'download', enhance: 'sparkle', separate: 'layers', import: 'file' };
const IS_MAC = /Mac/i.test(navigator.platform || navigator.userAgent);
const REVEAL_LABEL = IS_MAC ? 'Show in Finder' : 'Show in folder';
const FLASH_MS = 1800;
const CLOCK_MS = 30000; // "5 min ago" is refreshed this often while the Library shows
const libPath = (id, what = '') => `/api/library/${encodeURIComponent(id)}${what ? `/${what}` : ''}`;
const SORT_KEY = 'audiolab.librarySort';
const SORTS = [{ value: 'newest', label: 'Newest' }, { value: 'largest', label: 'Largest' }];

let refs = null;
let scroller = null;
let visible = false;
let groups = null;   // the last GET /api/library (null until the first answer)
let loadSeq = 0;
let reloadTimer = 0;
let doneKey = '';    // finished jobs with a Library entry: the list reloads when this changes
let target = null;   // #/library/<id> still to scroll to
let player = null;   // the open player: { entryId, what: 'compare' | 'versions' | file id, fileId, title, el, stage, pause, destroy }
let clock = 0;       // the timer that refreshes relative times
let uid = 0;
let query = '';      // the search text (title or artist)
let sort = 'newest'; // newest | largest, remembered in this browser
try { if (localStorage.getItem(SORT_KEY) === 'largest') sort = 'largest'; } catch { /* storage unavailable */ }
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
      search: h('input', {
        type: 'search', class: 'field field-sm lib-search', placeholder: 'Search title or artist', 'aria-label': 'Search the Library',
        autocomplete: 'off', spellcheck: 'false',
      }),
      noMatch: h('p', { class: 'filter-empty', hidden: true }),
      addFile: h('button', { type: 'button', class: 'btn btn-key btn-sm', hidden: true, onclick: () => importDialog() },
        icon('plus', 15), 'Add a file…'),
      error: h('div', { hidden: true }),
      empty: h('p', { class: 'filter-empty', hidden: true },
        'Finished results from Save, Enhance and Split show up here. ', h('a', { href: '#/new' }, 'Start something new')),
      list: h('div', { class: 'lib-groups' }),
    };
    this.heading = refs.title;
    refs.search.addEventListener('input', () => { query = refs.search.value; render(); });
    refs.tools = h('div', { class: 'lib-tools', hidden: true }, refs.search,
      segmented({
        label: 'Sort', size: 'sm', value: sort, options: SORTS,
        onChange: (v) => {
          sort = v;
          try { localStorage.setItem(SORT_KEY, v); } catch { /* this page only */ }
          render();
        },
      }));
    el.append(h('div', { class: 'page' },
      h('header', { class: 'page-head' }, h('div', { class: 'page-head-text' }, refs.title, refs.sub), refs.addFile),
      refs.tools, refs.error, refs.empty, refs.noMatch, refs.list));
    on('jobs', onJobs);
  },

  show(arg) {
    visible = true;
    refreshTimes();
    clearInterval(clock);
    clock = setInterval(refreshTimes, CLOCK_MS);
    target = arg || null;
    if (target && query) { query = ''; refs.search.value = ''; } // a linked version is never hidden by a search
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
    clearInterval(clock);
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
      refs.tools.hidden = true;
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
  const all = groups || [];
  const count = all.reduce((n, g) => n + g.entries.length, 0);
  refs.sub.textContent = !count ? 'Your finished results and their versions.' : totalsText(all, count);
  refs.tools.hidden = !count;
  refs.empty.hidden = count > 0;
  refs.addFile.hidden = !store.health?.local; // the file is read where it is: only on this computer
  const list = shownGroups(all);
  refs.noMatch.hidden = !count || list.length > 0;
  refs.noMatch.textContent = `Nothing matches “${query.trim()}”.`;
  // A player in a recording the search hides would keep playing out of sight.
  if (player && !list.some((g) => g.entries.some((e) => e.id === player.entryId))) closePlayer();

  const ids = new Set(all.flatMap((g) => g.entries.map((e) => e.id)));
  for (const [id, c] of cards) {
    if (ids.has(id)) continue;
    if (player?.entryId === id) closePlayer();
    c.el.remove();
    cards.delete(id);
  }
  const els = list.map((g) => groupFor(g).el);
  const keys = new Set(all.map((g) => g.key));
  for (const [key, grp] of groupEls) {
    if (!keys.has(key)) { grp.el.remove(); groupEls.delete(key); }
  }
  syncChildren(refs.list, els);
  refreshTimes();
}

/** The bytes of these recordings' existing files. */
const filesSize = (list) => list.flatMap((g) => g.entries).flatMap((e) => e.files || [])
  .filter((f) => f.kind !== 'original' && f.exists && f.size_bytes > 0).reduce((n, f) => n + f.size_bytes, 0);

/** The bytes of their kept originals, each distinct one once (`original.key`; without it, one per recording). */
function originalsSize(list) {
  const seen = new Map();
  for (const g of list) {
    for (const o of g.entries.map((e) => e.original).filter((x) => x?.kept)) {
      const id = o.key ? `key:${o.key}` : `group:${g.key}`;
      if (!seen.has(id)) seen.set(id, o.size_bytes || 0);
    }
  }
  return [...seen.values()].reduce((n, b) => n + b, 0);
}

/** "14 results · 9.2 GB of files · kept originals 1.1 GB" for the whole Library. */
function totalsText(all, count) {
  const [files, originals] = [filesSize(all), originalsSize(all)];
  return [`${count} result${count === 1 ? '' : 's'}`, files ? `${fmtBytes(files)} of files` : null,
    originals ? `kept originals ${fmtBytes(originals)}` : null].filter(Boolean).join(' · ');
}

/** The recordings to show: those matching the search (title or artist), newest first or largest first. */
function shownGroups(all) {
  const q = query.trim().toLowerCase();
  const list = q ? all.filter((g) => [g.title, g.artist].some((t) => String(t || '').toLowerCase().includes(q))) : [...all];
  if (sort === 'largest') {
    const size = new Map(list.map((g) => [g, filesSize([g]) + originalsSize([g])]));
    list.sort((a, b) => size.get(b) - size.get(a)); // stable: equal sizes keep newest first
  }
  return list;
}

/** Cards are only rebuilt when their data changes, so "5 min ago" is brought up to date here. */
function refreshTimes() {
  for (const c of cards.values()) {
    if (c.when) c.when.textContent = ` · ${dateText(c.entry.created_at, true)}`;
  }
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
  // Enhance versions with a result to play can be compared with each other (two or more).
  const versions = g.entries.filter((e) => ['enhance', 'import'].includes(e.task) && versionFile(e, enhanceFormats()));
  const peers = versions.length >= 2 ? versions : null;
  const hint = peers ? loudnessHint(peers) : '';
  // The date ("Today") is part of it, so the head is redrawn when the day changes.
  const local = !!store.health?.local;
  const sig = JSON.stringify([g.title, g.artist, g.source, g.latest_at, dateText(g.latest_at), g.entries.length, cover, kept?.id, kept?.original, hint, local]);
  if (sig !== grp.sig) {
    grp.sig = sig;
    append(clear(grp.head), [groupHead(g, grp.titleId, cover, local), kept ? originalRow(g, kept) : null,
      hint ? h('p', { class: 'lib-hint' }, hint) : null]);
  }
  const several = g.entries.length >= 2; // "Keep only this version"
  syncChildren(grp.list, g.entries.map((e) => cardFor(e, peers?.includes(e) ? peers : null, several).el));
  return grp;
}

const enhanceFormats = () => store.health?.formats?.enhance || [];

/** Versions more than 1 dB apart: louder tends to win a quick listen, so point to the matched comparison. */
function loudnessHint(versions) {
  const spread = loudnessSpread(versions.map((e) => versionLoudness(e, versionFile(e, enhanceFormats()))));
  return spread ? `These versions differ in loudness (${fmtNum(spread.low)} to ${fmtNum(spread.high)} LUFS). `
    + 'Louder often sounds better at first, so use Compare versions to hear them at matched volume.' : '';
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
function groupHead(g, titleId, cover, local) {
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
      h('p', { class: 'lib-meta', title: src.kind === 'url' ? src.url : src.name || null }, meta),
      local ? h('button', { type: 'button', class: 'btn btn-ghost btn-sm lib-add-version', onclick: () => importDialog(g) },
        icon('plus', 14), 'Add a version from a file…') : null));
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
/**
 * A version's card; `peers`: the versions of its recording it can be compared with (itself included),
 * or null; `several`: its recording has other versions too (for "Keep only this version").
 */
function cardFor(entry, peers = null, several = false) {
  const sig = JSON.stringify([entry, peers?.map((e) => e.id), several]);
  let c = cards.get(entry.id);
  if (c) Object.assign(c, { peers, several }); // the latest copies, for the picker
  if (c && c.sig === sig) return c;
  if (!c) {
    c = { el: h('li', { class: 'lib-entry', id: `lib-${entry.id}`, tabindex: '-1' }), toggles: new Map(), peers, several };
    cards.set(entry.id, c);
  }
  c.sig = sig;
  c.entry = entry;
  fillCard(c, entry);
  return c;
}

function fillCard(c, entry) {
  // The open player stays when its files are still there (it is moved, not rebuilt).
  const keep = player?.entryId === entry.id && playerStillFits(c, entry);
  if (player?.entryId === entry.id && !keep) closePlayer();
  // Focus comes back to the same control; an item of the ⋯ menu counts as its button (the new menu is closed).
  const controls = () => [...c.el.querySelectorAll('a, button, summary')].filter((n) => !n.closest('.menu'));
  const focused = c.el.contains(document.activeElement) ? document.activeElement : null;
  const inMenu = !!focused?.closest('.menu');
  if (inMenu) closeMenu();
  const focusIndex = focused ? controls().indexOf(inMenu ? focused.closest('.menu-wrap').querySelector('button') : focused) : -1;

  const titleId = `${c.el.id}-title`;
  c.el.setAttribute('aria-labelledby', titleId);
  c.toggles = new Map();
  const files = (entry.files || []).filter((f) => f.kind !== 'original' && f.kind !== 'track');
  const tracks = (entry.files || []).filter((f) => f.kind === 'track').sort((a, b) => (a.track ?? 0) - (b.track ?? 0));
  c.processing = processingDetails(entry, files, c.processing?.open);
  c.stage = h('div', { class: 'lib-stage', id: `${c.el.id}-player`, hidden: true });
  const when = dateText(entry.created_at, true);
  c.when = when ? h('span', { class: 'lib-when' }, ` · ${when}`) : null;
  const facts = versionFacts(entry);
  const stream = streamLine(entry.stream);
  append(clear(c.el), [
    h('div', { class: 'lib-entry-head' },
      h('div', { class: 'lib-entry-line' },
        h('h3', { class: 'lib-entry-title', id: titleId }, versionTitle(entry)),
        c.when,
        facts ? h('p', { class: 'lib-version' }, facts) : null,
        stream ? h('p', { class: 'lib-version' }, `Source stream: ${stream}`) : null),
      entryActions(c, entry)),
    files.length ? h('ul', { class: 'files' }, files.map((f) => fileRow(c, entry, f))) : null,
    c.processing,
    tracks.length ? h('details', {
      class: 'disclosure lib-tracks', open: keep && tracks.some((t) => t.id === player.what),
    }, h('summary', null, `Tracks (${tracks.length})`), h('ul', { class: 'files' }, tracks.map((f) => fileRow(c, entry, f)))) : null,
    c.stage]);
  if (keep) attachPlayer(c);
  updateToggles();
  if (focusIndex >= 0) {
    (controls()[focusIndex] || c.el).focus({ preventScroll: true });
  }
}

const OVERRIDES = {
  target_lufs: ['Loudness target', ' LUFS'], tp_ceiling_db: ['Peak ceiling', ' dBTP'], bass_db: ['Bass punch', ' dB'],
  glue: ['Compression'], hf_restore: ['Brighten muffled highs'], mono_bass: ['Mono bass'], denoise: ['Noise reduction'],
  deess: ['Sharp S sounds'], harmonics: ['Bass harmonics'], adaptive_lowmid: ['Adaptive low-mids'],
  sample_rate: ['Sample rate', ' Hz'], model: ['AI model'], eq: ['Custom EQ', '', eqSummary],
  section_eq: ['Section EQ', '', sectionSummary],
};

function processingDetails(entry, files, open) {
  const checked = files.filter((f) => f.verification);
  const p = entry.processing;
  if (!p && !checked.length) return null;
  const requested = p?.requested || {};
  const resolved = p?.resolved || {};
  const applied = p?.applied || {};
  const recipe = requested.recipe === 'auto' ? 'Automatic' : resolved.recipe?.name;
  const mode = PROCESSING_LABELS[requested.quality];
  const formats = (requested.formats || []).map((key) =>
    store.health?.formats?.enhance?.find((f) => f.key === key)?.label || files.find((f) => f.format === key)?.label || key);
  const selected = [recipe, mode, ...formats].filter(Boolean).join(' · ');
  const overrides = Object.entries(OVERRIDES).flatMap(([key, [label, unit = '', format]]) => {
    const value = requested.advanced?.[key];
    if (value == null || (Array.isArray(value) && !value.length)) return [];
    return [[label, format ? format(value) : `${value}${unit}`]];
  });
  for (const [key, label] of [['split_tracks', 'Split into tracks'], ['embed_cover', 'Embed cover'], ['save_cover', 'Save cover image']]) {
    if (typeof requested[key] === 'boolean') overrides.push([label, requested[key] ? 'On' : 'Off']);
  }
  const steps = [...(applied.steps || [])];
  if (applied.ai?.fallback) steps.push('The AI stage could not be used. Adaptive processing was applied.');
  const adaptive = applied.adaptive_lowmid;
  if (adaptive?.engaged === false && adaptive.skip_reason && !['off', 'no isolated low-mid rises needing control'].includes(adaptive.skip_reason)) {
    steps.push(`Adaptive low-mid control stayed off: ${adaptive.skip_reason}.`);
  }
  const measured = [
    [applied.loudness_gain_db, 'Loudness gain before peak limiting', ' dB', fmtSigned],
    [applied.limiter?.corrective_gain_db, 'Extra gain reduction to protect peaks', ' dB', fmtSigned],
    [adaptive?.engaged ? adaptive.active_pct : null, 'Time with active low-mid control', '%', fmtNum],
  ].filter(([v]) => Number.isFinite(v));
  const section = (label, ...body) => h('section', null, h('h4', null, label), ...body);
  const anyWarning = checked.some((f) => f.verification.status === 'warning');
  const anyUnverified = checked.some((f) => f.verification.status === 'unverified');
  const checkNote = anyWarning ? ' · Export warning' : anyUnverified ? ' · Export check unavailable' : '';
  return h('details', { class: 'disclosure lib-processing', open },
    h('summary', null, `Processing details${checkNote}`),
    h('div', { class: 'lib-processing-body small' },
      steps.length || measured.length ? section('What changed',
        steps.length ? h('ul', { class: 'plain-list' }, steps.map((step) => h('li', null, step))) : null,
        measured.length ? h('dl', { class: 'facts' }, measured.map(([v, label, unit, fmt]) =>
          [h('dt', null, label), h('dd', { class: 'num' }, `${fmt(v, 2)}${unit}`)])) : null) : null,
      processingMeasurements(p?.metrics, section),
      checked.length ? section('Export checks',
        h('ul', { class: 'plain-list' }, checked.map((f) => exportCheck(f)))) : null,
      selected || overrides.length ? section('Requested settings',
        selected ? h('p', null, selected) : null,
        overrides.length ? h('dl', { class: 'facts' }, overrides.map(([label, value]) =>
          [h('dt', null, label), h('dd', null, value)])) : null) : null,
      resolved.decisions?.length ? section('Settings used and why',
        h('ul', { class: 'plain-list' }, resolved.decisions.map((d) =>
          h('li', null, h('strong', null, `${d.label}: ${d.value}`), d.why ? ` — ${d.why}` : null)))) : null));
}

function processingMeasurements(metrics, section) {
  const b = metrics?.before || {};
  const a = metrics?.after || {};
  const rows = [
    ['lufs', 'Loudness', 'LUFS'], ['true_peak_dbtp', 'True peak', 'dBTP'], ['lra', 'Loudness range', 'LU'],
  ].filter(([key]) => Number.isFinite(b[key]) || Number.isFinite(a[key]));
  return rows.length ? section('Original and master',
    h('p', { class: 'muted' }, 'The master is the audio before it is saved in your selected formats.'),
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Measurement'), h('th', { scope: 'col' }, 'Original'), h('th', { scope: 'col' }, 'Master'))),
      h('tbody', null, rows.map(([key, label, unit]) => h('tr', null,
        h('th', { scope: 'row' }, `${label} (${unit})`), h('td', { class: 'num' }, fmtNum(b[key])),
        h('td', { class: 'num' }, fmtNum(a[key]))))))) : null;
}

function exportCheck(file) {
  const check = file.verification;
  const status = check.status === 'unverified' ? 'Could not check the saved file'
    : check.ceiling_met === false ? 'Peak above the ceiling'
      : check.status === 'verified' ? 'Peak within the ceiling' : 'Review the saved file';
  const measurements = [
    Number.isFinite(check.integrated_lufs) ? `${fmtNum(check.integrated_lufs, 2)} LUFS` : null,
    Number.isFinite(check.true_peak_dbtp) ? `${fmtNum(check.true_peak_dbtp, 2)} dBTP` : null,
    Number.isFinite(check.ceiling_dbtp) ? `ceiling ${fmtNum(check.ceiling_dbtp, 2)} dBTP` : null,
  ].filter(Boolean);
  return h('li', null, h('strong', null, file.label || file.name || 'Export'), ` — ${status}`,
    measurements.length ? ` (${measurements.join(' · ')})` : null);
}

function taskLabel(entry) {
  if (entry.task === 'download') return entry.source?.kind === 'file' ? 'Converted' : 'Saved';
  return { enhance: 'Enhanced', separate: 'Stems', import: 'Imported' }[entry.task] || entry.task;
}

/** "Enhanced as Song / music" with the task's word marked; a headline without one ("Volume evened out") gets it in front. */
function versionTitle(entry) {
  if (entry.task === 'import') return [h('span', { class: 'lib-task' }, 'Imported'), ' file'];
  const text = entry.headline || entry.title || '';
  const verb = /^(Saved|Converted|Enhanced|Split)\b/.exec(text)?.[0];
  if (verb) return [h('span', { class: 'lib-task' }, verb), text.slice(verb.length)];
  return [h('span', { class: 'lib-task' }, taskLabel(entry)), text ? ` · ${text}` : ''];
}

// What a version is, so two "Enhanced as DJ set" versions can be told apart: the main settings
// it used, then which settings were the user's (requested) rather than automatic.
const COMPRESSION = { off: 'compression off', glue: 'compression on', leveler: 'leveler on' };
const OWN_SETTINGS = {
  target_lufs: 'loudness', tp_ceiling_db: 'peak ceiling', bass_db: 'bass', harmonics: 'bass harmonics',
  adaptive_lowmid: 'low-mids', glue: 'compression', hf_restore: 'highs', denoise: 'noise reduction',
  mono_bass: 'mono bass', deess: 'S sounds', sample_rate: 'sample rate', eq: 'EQ', section_eq: 'section EQ',
  model: 'AI model',
};
const shortNum = (v) => String(Number(Number(v).toFixed(1))).replace('-', '−');

function versionFacts(entry) {
  if (entry.task === 'import') {
    // A file the user brought in: what was measured over the whole file.
    const m = entry.measured || {};
    const lufs = Number.isFinite(m.integrated_lufs) ? m.integrated_lufs : entry.loudness?.result_lufs;
    return [Number.isFinite(lufs) ? `${shortNum(lufs)} LUFS` : null,
      Number.isFinite(m.true_peak_dbtp) ? `true peak ${fmtSigned(m.true_peak_dbtp, 1)} dBTP` : null].filter(Boolean).join(' · ');
  }
  const p = entry.processing;
  if (entry.task !== 'enhance' || !p) return '';
  const params = p.resolved?.params || {};
  const full = !params.volume_only; // Volume only sets the level and nothing else
  const rate = Number(params.sample_rate);
  const facts = [
    Number.isFinite(params.target_lufs) ? `${shortNum(params.target_lufs)} LUFS` : null,
    full ? COMPRESSION[params.compressor] : null,
    full && Array.isArray(params.eq_bands) && params.eq_bands.length ? 'custom EQ' : null,
    rate > 0 && rate !== 48000 ? `${shortNum(rate / 1000)} kHz` : null,
  ];
  if (p.requested) {
    const light = p.requested.quality === 'light';
    const own = Object.entries(p.requested.advanced || {})
      .filter(([key, v]) => v != null && !(Array.isArray(v) && !v.length) && (!light || LIGHT_KEYS.includes(key)))
      .map(([key]) => OWN_SETTINGS[key] || key.replace(/_/g, ' '));
    facts.push(own.length ? `your settings: ${own.join(', ')}` : 'Auto settings');
  }
  return facts.filter(Boolean).join(' · ');
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
  if (!adjustUnavailable(entry)) {
    btns.push(adjustLink(entry, () => player?.entryId === entry.id && player.what === 'compare' ? player.snapshot?.() : null));
  }
  if (c.peers) {
    const b = h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', 'aria-expanded': 'false', 'aria-controls': c.stage.id,
      onclick: (e) => togglePlayer(c, 'versions', e),
    }, icon('compare', 15), 'Compare versions');
    c.toggles.set('versions', b);
    btns.push(b);
  }
  // The rarer actions: one menu per version.
  btns.push(menuButton({ label: 'More actions', items: [
    store.health?.local ? { label: REVEAL_LABEL, icon: 'folder', onSelect: () => revealEntry(entry) } : null,
    entry.job_exists ? { label: 'Open job', icon: 'arrow', onSelect: () => go(jobPath(entry.job_id)) } : null,
    c.several ? { label: 'Keep only this version', icon: 'check', onSelect: () => keepOnly(entry) } : null,
    { label: 'Remove from Library', icon: 'trash', onSelect: () => removeEntry(entry) },
  ] }));
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
    const start = entry.excerpt?.start_s;  // the loud passage the job page starts at, kept with the entry
    const p = libraryCompare(entry, {
      startAt: start || 0,
      note: start ? `Starts at a loud passage (${fmtDuration(start)}) — the whole recording is here.` : null,
    });
    player = { ...p, entryId: entry.id, what, group, fileId: result.id, title: 'Comparing with the original' };
    attachPlayer(c);
    // Route reveals also open players; they retain the position but never start sound.
    p.restore?.(state, { resume: event?.isTrusted === true });
  } else if (what === 'versions') {
    if (!openVersions(c)) return;
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

// ---------------------------------------------------------------------------
// Two versions of one recording, at matched volume: the older one first. The version the
// row belongs to is compared with the newest other one, or the one picked above the player.
// ---------------------------------------------------------------------------
function openVersions(c, otherId = null) {
  const others = (c.peers || []).filter((e) => e.id !== c.entry.id); // newest first, like the list
  const other = others.find((e) => e.id === otherId) || others[0];
  if (!other) return false;
  // Older first; at the same time, the one listed lower (the list is newest first) counts as older.
  const [older, newer] = [c.entry, other].sort((x, y) =>
    (x.created_at || 0) - (y.created_at || 0) || c.peers.indexOf(y) - c.peers.indexOf(x));
  const side = (entry, label, name = `The ${label.toLowerCase()} version`) => ({ entry, file: versionFile(entry, enhanceFormats()), label, name });
  // An Enhance version against a file the user added: AudioLab's first, "Your file" second, whatever their dates.
  const mine = [c.entry, other].filter((e) => e.task === 'import');
  const [a, b] = mine.length === 1
    ? [side([c.entry, other].find((e) => e.task !== 'import'), 'Enhanced'), side(mine[0], 'Your file', 'Your file')]
    : [side(older, 'Older'), side(newer, 'Newer')];
  if (!a.file || !b.file) return false;
  const start = b.entry.excerpt?.start_s ?? a.entry.excerpt?.start_s; // an imported file has no loud passage of its own
  const legend = (s) => [`${s.label} (${dateText(s.entry.created_at, true) || 'no date'}): `, versionFacts(s.entry) || s.file.label || s.file.name];
  const p = versionCompare(a, b, {
    startAt: start || 0,
    note: [...legend(a), h('br'), ...legend(b), start ? ` · Starts at a loud passage (${fmtDuration(start)}).` : ''],
  });
  player = { ...p, entryId: c.entry.id, what: 'versions', otherId: other.id, fileIds: [a.file.id, b.file.id], title: 'Comparing two versions' };
  attachPlayer(c);
  return true;
}

/** "Compare with" above the player, once there is more than one other version to pick. */
function versionPicker(c) {
  const others = (c.peers || []).filter((e) => e.id !== c.entry.id);
  if (others.length < 2) return null;
  const pick = segmented({
    label: 'Compare with', size: 'sm', value: player.otherId,
    options: others.map((e) => ({ value: e.id, label: dateText(e.created_at, true) || 'Undated', desc: versionFacts(e) || null })),
    onChange: (id) => {
      const state = player?.snapshot?.();
      closePlayer();
      if (!openVersions(c, id)) { updateToggles(); return; }
      // A new pick in the same place: keep the position, and keep playing (this is a click).
      player.restore?.(state && { time: state.time, playing: state.playing, matchVolume: state.matchVolume }, { resume: true });
      updateToggles();
      c.stage.querySelector('.lib-version-pick input:checked')?.focus({ preventScroll: true });
    },
  });
  return h('div', { class: 'lib-version-pick small' }, h('span', null, 'Compare with'), pick);
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
  append(clear(c.stage), [h('div', { class: 'lib-stage-head' }, h('strong', null, player.title), close),
    player.what === 'versions' ? versionPicker(c) : null, player.el]);
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

function playerStillFits(c, entry) {
  if (player.what === 'compare') return compareFile(entry)?.id === player.fileId;
  if (player.what === 'versions') {
    const other = c.peers?.find((e) => e.id === player.otherId);
    const ids = other ? [entry, other].map((e) => versionFile(e, enhanceFormats())?.id) : [];
    return ids.every(Boolean) && ids.every((id) => player.fileIds.includes(id));
  }
  return (entry.files || []).some((f) => f.id === player.what && f.exists);
}

function updateToggles() {
  for (const c of cards.values()) {
    for (const [what, b] of c.toggles) {
      const on = !!player && player.entryId === c.entry.id && player.what === what;
      if (what === 'compare' || what === 'versions') b.setAttribute('aria-expanded', String(on));
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

// ---------------------------------------------------------------------------
// Removing: the Library forgets entries; their files stay unless the user opts in to the Trash
// (off by default, and never a permanent delete). An imported file can go too: the switch names its folder.
// ---------------------------------------------------------------------------
/** An entry's files that are still there and could go to the Trash (the kept original is AudioLab's own). */
const trashable = (entry) => (entry.files || []).filter((f) => f.exists && f.kind !== 'original');

/** Where an imported file is, for reading: "~/Music/Masters" (no raw path). */
const importFolder = (entry) => shortFolder(String(trashable(entry)[0]?.path || '').replace(/[\\/][^\\/]*$/, ''));

/** The opt-in under a confirmation: off by default. `on()` reads it after the dialog closes. */
function trashSwitch(label, help) {
  const sw = switchControl({ checked: false, label, onChange: () => {} });
  const helpId = `${sw.id}-help`;
  sw.setAttribute('aria-describedby', helpId);
  return {
    el: h('label', { class: 'switch-row lib-trash', for: sw.id }, sw,
      h('span', { class: 'switch-row-label' }, label), h('span', { class: 'switch-row-help', id: helpId }, help)),
    on: () => sw.checked,
  };
}

/** What the Trash did, as a toast line; files it couldn't move get their own warning. */
function trashResult(res, done) {
  const moved = res?.trashed?.length || 0;
  const failed = res?.trash_failed || [];
  toast(`${done}${moved ? ` ${moved} file${moved === 1 ? '' : 's'} moved to the Trash.` : ''}`, { kind: 'ok' });
  if (failed.length) {
    const names = failed.slice(0, 3).join(', ') + (failed.length > 3 ? ` and ${failed.length - 3} more` : '');
    toast(`Couldn’t move ${failed.length === 1 ? 'this file' : `${failed.length} files`} to the Trash, so ${failed.length === 1 ? 'it stays' : 'they stay'} where ${failed.length === 1 ? 'it is' : 'they are'}: ${names}.`,
      { kind: 'danger', timeout: 9000 });
  }
}

async function removeEntry(entry) {
  const group = (groups || []).find((g) => g.entries.some((e) => e.id === entry.id));
  const lastOriginal = entry.original?.kept && !group?.entries.some((e) => e.id !== entry.id && e.original?.kept);
  const imported = entry.task === 'import';
  const folder = imported ? importFolder(entry) : '';
  let trash = null;
  if (trashable(entry).length) {
    trash = imported
      ? trashSwitch(`Also move the file to the Trash${folder ? ` (it’s in ${folder}, where you added it from)` : ''}`, 'You can put it back from the Trash.')
      : trashSwitch('Also move its files to the Trash', 'You can put them back from the Trash.');
  }
  const ok = await confirmDialog({
    title: 'Remove from the Library?',
    message: `“${entry.title}” (${taskLabel(entry)}) is taken off this list.${lastOriginal ? ' The kept copy of the original goes with it.' : ''}${
      !trash ? '' : imported ? ' The file stays where it is unless you choose otherwise.' : ' Its files stay where they are unless you choose otherwise.'}`,
    confirm: 'Remove', extra: trash?.el,
  });
  if (!ok) return;
  const toTrash = !!trash?.on();
  try {
    const res = await api(`${libPath(entry.id)}${toTrash ? '?trash_files=1' : ''}`, { method: 'DELETE' });
    trashResult(res, toTrash ? 'Removed from the Library.' : 'Removed from the Library. Your files stay where they are.');
  } catch (e) {
    toastError(e); // 409: the Trash isn't available here, nothing was removed
  }
  refs.title.focus({ preventScroll: true });
  await load();
}

async function keepOnly(entry) {
  const group = (groups || []).find((g) => g.entries.some((e) => e.id === entry.id));
  const others = (group?.entries || []).filter((e) => e.id !== entry.id);
  if (!others.length) return;
  const n = others.length;
  const imports = others.some((e) => e.task === 'import' && trashable(e).length);
  const trash = others.some((e) => trashable(e).length) ? trashSwitch('Also move their files to the Trash',
    `You can put them back from the Trash.${imports ? ' This includes files you added from other folders.' : ''}`) : null;
  const ok = await confirmDialog({
    title: 'Keep only this version?',
    message: `The other ${n === 1 ? 'version' : `${n} versions`} of “${group.title || entry.title}” ${n === 1 ? 'is' : 'are'} taken off this list. `
      + (trash ? 'Their files stay where they are unless you choose otherwise.' : 'Their files stay where they are.'),
    confirm: n === 1 ? 'Remove the other' : `Remove ${n} others`, extra: trash?.el,
  });
  if (!ok) return;
  try {
    const res = await api(libPath(entry.id, 'keep-only'), { method: 'POST', json: { trash_files: !!trash?.on() } });
    const removed = res?.removed ?? n;
    trashResult(res, `Kept this version. ${removed} other${removed === 1 ? '' : 's'} removed from the Library.`);
  } catch (e) {
    toastError(e);
  }
  document.getElementById(cards.get(entry.id)?.el.id)?.focus({ preventScroll: true });
  await load();
}

/**
 * "Add a file…" / "Add a version from a file…": an audio file already on this computer (a master
 * from a DAW, say) becomes a Library entry where it is (no copy). `group`: add it to that recording.
 */
function importDialog(group = null) {
  const id = `lib-import-${++uid}`;
  const path = h('input', {
    class: 'field', id: `${id}-path`, type: 'text', required: true, autocomplete: 'off', spellcheck: 'false',
    placeholder: IS_MAC ? '/Users/you/Music/Master.wav' : 'C:\\Users\\you\\Music\\Master.wav', 'aria-describedby': `${id}-help ${id}-error`,
  });
  const title = h('input', { class: 'field', id: `${id}-title`, type: 'text', autocomplete: 'off', placeholder: 'From its tags or file name' });
  const error = h('p', { class: 'lib-import-error small', id: `${id}-error` });
  const addBtn = h('button', { type: 'submit', class: 'btn btn-primary', value: 'ok' }, 'Add to Library');
  const form = h('form', { class: 'dialog-form', method: 'dialog' },
    h('h2', { id }, group ? 'Add a version from a file' : 'Add a file'),
    h('p', { class: 'muted' }, group
      ? `Adds an audio file you already have to “${group.title || 'this recording'}”, so you can play it and compare it with the other versions. The file stays where it is.`
      : 'Adds an audio file you already have, such as a master from your DAW, so you can play it and compare it. The file stays where it is.'),
    h('label', { class: 'field-label', for: path.id }, 'File path'), path,
    h('p', { class: 'muted small', id: `${id}-help` }, IS_MAC
      ? 'Paste the file’s path. In Finder: right-click the file with ⌥ held → Copy as Pathname.'
      : 'Paste the file’s full path.'),
    h('label', { class: 'field-label', for: title.id }, 'Title (optional)'), title,
    error,
    h('div', { class: 'dialog-actions' },
      h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => dialog.close() }, 'Cancel'), addBtn));
  const dialog = h('dialog', { class: 'dialog lib-import', 'aria-labelledby': id }, form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const filePath = path.value.trim().replace(/^(["'])(.*)\1$/, '$2'); // "Copy as path" on Windows adds quotes
    if (!filePath) { path.focus(); return; }
    const body = { path: filePath };
    if (title.value.trim()) body.title = title.value.trim();
    if (group) body.group = group.key;
    addBtn.disabled = true;
    error.textContent = '';
    try {
      const job = await api('/api/library/import', { method: 'POST', json: body });
      if (!job?.id) throw new Error('The app didn’t return an import job.');
      upsertJob(job); // the Library reloads when it is done
      dialog.close();
      toast(`Adding “${body.title || filePath.split(/[\\/]/).pop()}” to the Library…`, {
        action: { label: 'Show', run: () => go(jobPath(job.id)) },
      });
    } catch (err) {
      addBtn.disabled = false;
      error.textContent = [err.message, err.hints?.[0]].filter(Boolean).join(' ');
      path.focus();
    }
  });
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  path.focus();
  return dialog;
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
