// New view — the source: a pasted link (looked up via /api/probe), a playlist to
// pick from, one file, several files (one job each), or an earlier job's source
// ("do more with this"). Renders the card under the link field, with the name
// (names.js) and cover image (cover.js) tools for one link or file.
import { h, icon, clear, fmtDuration, fmtBytes, confidenceWords } from '../../dom.js';
import { api } from '../../api.js';
import { appName } from '../../store.js';
import { toast, callout, meter, setMeter, led } from '../../ui.js';
import { signInButton } from '../../actions.js';
import { state, refs, redraw, definePart, isFileLike, focusIn, focusStart, isMediaFile, tooBig, MAX_FILES } from './state.js';
import { shownName, metaPayload, nameTools, nameEditor } from './names.js';
import { coverTools, syncCoverPreview, setCover, isImageFile, takesCover } from './cover.js';
import { measureFile, setTracklist } from './tracks.js';

let probeCtl = null;

definePart('source', renderSource);

export function normalizeUrl(text) {
  let t = (text || '').trim();
  if (!t) return null;
  if (!/^[a-z]+:\/\//i.test(t) && /^([\w-]+\.)+[a-z]{2,}(\/|$)/i.test(t)) t = `https://${t}`;
  try {
    const u = new URL(t);
    if ((u.protocol === 'http:' || u.protocol === 'https:') && u.hostname.includes('.')) return u.href;
  } catch { /* not a URL */ }
  return null;
}

export function loadUrl(text) {
  probeCtl?.abort();
  probeCtl = null;
  const link = normalizeUrl(text);
  if (refs.input && refs.input.value !== text) refs.input.value = text;
  if (!link) {
    state.source = text.trim() ? { kind: 'invalid', text } : null;
    redraw();
    return;
  }
  if (refs.input) refs.input.value = link;
  probeCtl = new AbortController();
  const ctl = probeCtl;
  state.source = { kind: 'probing', url: link };
  redraw();
  api('/api/probe', { method: 'POST', json: { url: link }, signal: ctl.signal })
    .then((probe) => {
      if (ctl.signal.aborted) return;
      if (probe?.kind === 'playlist') {
        const entries = probe.entries || [];
        state.source = { kind: 'playlist', url: link, probe, selected: new Set(entries.map((e) => e.url)) };
        if (state.task === 'analyze') state.task = 'download';
      } else {
        state.source = { kind: 'url', url: link, probe: probe || {} };
      }
      redraw();
    })
    .catch((e) => {
      if (e.name === 'AbortError' || ctl.signal.aborted) return;
      state.source = { kind: 'url-error', url: link, error: e.message, status: e.status, hints: e.hints || [], fix: e.fix };
      redraw();
    });
}

function setFile(file) {
  if (!file) return;
  probeCtl?.abort();
  if (refs.input) refs.input.value = '';
  state.source = { kind: 'file', file };
  measureFile(state.source);
  if (state.download.format === 'original' || state.download.format === 'video') state.download.format = 'mp3';
  redraw();
  focusStart();
}

/** Several files: one job each. Adds to an existing list, otherwise starts one. */
export function setFiles(list) {
  const picked = [...(list || [])].filter(Boolean);
  if (!picked.length) return;
  // One picture (dropped or chosen): the cover of the link or file that is loaded.
  if (picked.length === 1 && isImageFile(picked[0])) {
    if (!takesCover(state.source)) toast('Load a link or a file first, then add the picture as its cover.', { kind: 'info' });
    else if (setCover(state.source, picked[0])) toast('Added as the cover image.', { kind: 'ok' });
    return;
  }
  const adding = state.source?.kind === 'files';
  if (picked.length === 1 && !adding) {
    if (isMediaFile(picked[0])) setFile(picked[0]);
    else toast(`“${picked[0].name}” isn’t an audio or video file ${appName()} can read.`, { kind: 'warning' });
    return;
  }
  if (adding && state.busy) { toast('Wait until these files are uploaded, then add more.', { kind: 'warning' }); return; }
  const media = picked.filter(isMediaFile);
  const skipped = picked.length - media.length;
  const items = adding ? [...state.source.items] : [];
  const key = (f) => `${f.name}|${f.size}|${f.lastModified}`;
  const known = new Set(items.map((it) => key(it.file)));
  let over = 0;
  let big = 0;
  for (const file of media) {
    if (known.has(key(file))) continue;
    if (tooBig(file)) { big += 1; continue; }
    if (items.length >= MAX_FILES) { over += 1; continue; }
    known.add(key(file));
    items.push({ file, status: 'ready', pct: 0, error: null, jobId: null });
  }
  const plural = (n) => `${n} file${n === 1 ? '' : 's'}`;
  if (over) toast(`You can add up to ${MAX_FILES} files at a time. Start these first, then add the rest.`, { kind: 'warning', timeout: 8000 });
  else if (big) toast(`Left out ${plural(big)} ${tooBig(media.find(tooBig))}.`, { kind: 'warning', timeout: 8000 });
  else if (skipped) toast(`Left out ${plural(skipped)} that ${skipped === 1 ? 'isn’t' : 'aren’t'} audio or video.`, { kind: 'info' });
  if (!items.length) {
    if (!adding && !big) toast('None of these look like audio or video files.', { kind: 'warning' });
    return;
  }
  if (items.length === 1 && !adding) { setFile(items[0].file); return; }
  probeCtl?.abort();
  if (refs.input) refs.input.value = '';
  state.source = { kind: 'files', items };
  if (['original', 'video'].includes(state.download.format)) state.download.format = 'mp3';
  redraw();
  focusStart();
}

/**
 * Start from an earlier job's source ("Do more with this"). Its own name, tracklist and
 * split choice come along; `again` ("Change and start again" on a failed or cancelled job)
 * also takes its task's options, so only what went wrong needs changing.
 */
export function useJobSource(job, task, { again = false } = {}) {
  probeCtl?.abort();
  probeCtl = null;
  if (refs.input) refs.input.value = job.source?.kind === 'url' ? (job.source.url || '') : '';
  const o = job.options || {};
  // The earlier job's own name overrides carry over (its source keeps the original name).
  const carried = o.meta && Object.keys(o.meta).length ? { ...o.meta } : null;
  const src = { kind: 'job', job, meta: carried, artistCleared: carried?.artist === '' };
  if (typeof o.split_tracks === 'boolean') src.splitTracks = o.split_tracks;
  state.source = src;
  if (task) state.task = task;
  if (again) applyOptions(job.task, o);
  if (isFileLike() && ['original', 'video'].includes(state.download.format)) state.download.format = 'mp3';
  if (typeof o.tracklist === 'string') setTracklist(src, o.tracklist);
  if (job.source?.kind === 'url' && job.source.url) lookUpJobLink(src, job.source.url);
  redraw();
  setTimeout(focusStart, 0);
}

/** A job's (normalised) options back into the choices on New; keys an older job lacks stay as they are. */
function applyOptions(task, o) {
  const pick = (target, keys) => keys.forEach((k) => { if (o[k] != null) target[k] = o[k]; });
  if (task === 'download') pick(state.download, ['format', 'embed_cover']);
  else if (task === 'separate') pick(state.separate, ['mode', 'format', 'model']);
  else if (task === 'enhance') {
    const e = state.enhance;
    pick(e, ['recipe', 'quality', 'embed_cover']);
    if (Array.isArray(o.formats) && o.formats.length) e.formats = new Set(o.formats);
    if (o.advanced && typeof o.advanced === 'object') e.advanced = { ...o.advanced };
  }
}

/** An earlier link job: look the link up again in the background (as pasting does) for its
 * chapters or the tracklist in its description. Nothing is said when that doesn't work. */
function lookUpJobLink(src, link) {
  const ctl = probeCtl = new AbortController();
  api('/api/probe', { method: 'POST', json: { url: link }, signal: ctl.signal })
    .then((probe) => {
      if (ctl.signal.aborted || state.source !== src || probe?.kind !== 'single') return;
      src.probe = probe;
      redraw('options', 'action');
    })
    .catch(() => {});
}

export function clearSource() {
  probeCtl?.abort();
  state.source = null;
  refs.input.value = '';
  redraw();
  refs.input.focus();
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------
function sourceCard({ thumb, iconName, title, meta, extra, removable = true, rename = null, cover = null }) {
  const thumbBox = h('div', { class: 'source-thumb' });
  if (thumb) {
    const img = h('img', { src: thumb, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', () => { clear(thumbBox).append(icon(iconName, 26)); });
    thumbBox.append(img);
  } else {
    thumbBox.append(icon(iconName, 26));
  }
  const editing = !!rename?.editing;
  // The name and cover tools: one row under the text (under the whole card when narrow).
  const tools = [rename && !editing ? nameTools(rename) : null, cover ? coverTools(cover) : null].flat().filter(Boolean);
  return h('div', { class: ['source-card', editing && 'is-editing'] },
    thumbBox,
    h('div', { class: 'source-text' },
      h('div', { class: 'source-title' }, title),
      meta ? h('div', { class: 'source-meta' }, meta) : null,
      extra),
    removable ? h('button', { type: 'button', class: 'icon-btn source-remove', 'aria-label': 'Remove this source', onclick: clearSource }, icon('close', 16)) : null,
    tools.length ? h('div', { class: 'source-tools' }, tools) : null,
    editing ? nameEditor(rename) : null);
}

function guessLine(guess) {
  if (!guess || !guess.type || guess.type === 'unknown') return null;
  const reasons = (guess.reasons || []).join(' · ');
  return h('div', { class: 'guess' },
    h('span', null, 'Looks like: ', h('strong', null, guess.label || guess.type)),
    h('span', { class: 'guess-conf' }, ` · ${confidenceWords(guess.confidence, guess.confidence_label)}`),
    reasons ? h('div', { class: 'guess-reasons' }, reasons) : null);
}

const metaJoin = (...parts) => parts.filter(Boolean).join(' · ');

function fileIcon(name = '', type = '') {
  if (type.startsWith('video') || /\.(mp4|mkv|mov|webm|avi)$/i.test(name)) return 'video';
  return 'music';
}

function renderSource() {
  syncCoverPreview();
  const src = state.source;
  // Don't rebuild under someone typing a name (health/settings events re-render).
  if (src?.editing && refs.source.contains(document.activeElement) && document.activeElement.closest('.name-edit')) return;
  const el = clear(refs.source);
  if (!src) return;
  switch (src.kind) {
    case 'invalid':
      el.append(callout('warning', 'That doesn’t look like a link', 'Paste the full address from your browser, for example https://www.youtube.com/watch?v=…'));
      break;
    case 'probing':
      el.append(sourceCard({ iconName: 'link', title: 'Looking up the link…', meta: src.url, extra: meter(0, { idle: true, label: 'Looking up' }) }));
      break;
    case 'url-error':
      el.append(callout('danger', 'Couldn’t read this link', src.error, [
        src.hints?.length ? h('ul', { class: 'plain-list muted' }, src.hints.map((t) => h('li', null, t))) : null,
        h('div', { class: 'callout-actions' },
          src.fix === 'use_cookies' ? signInButton({ small: true }) : null,
          h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: () => loadUrl(src.url) }, icon('retry', 14), 'Try again'),
          src.fix === 'use_cookies' ? null : h('span', { class: 'muted small' }, 'You can still start a job — it will explain what went wrong.'))]));
      break;
    case 'url': {
      const p = src.probe;
      const name = shownName(src);
      el.append(sourceCard({
        thumb: p.thumbnail, iconName: 'video', title: name.title || src.url,
        meta: metaJoin(name.artist, fmtDuration(p.duration_s), p.is_live ? 'Live stream' : null),
        extra: guessLine(p.guess), rename: src, cover: src,
      }));
      if (p.is_live) el.append(callout('warning', 'This is a live stream', 'Live streams can only be saved after they end.'));
      break;
    }
    case 'playlist':
      el.append(playlistCard(src));
      break;
    case 'file': {
      const renamed = !!metaPayload(src);
      const name = shownName(src);
      el.append(sourceCard({
        iconName: fileIcon(src.file.name, src.file.type), title: renamed ? name.title : src.file.name,
        meta: metaJoin(renamed ? name.artist : null, renamed ? src.file.name : 'File on this computer', fmtBytes(src.file.size)),
        rename: src, cover: src,
      }));
      break;
    }
    case 'files':
      el.append(filesList(src));
      break;
    case 'job': {
      const j = src.job;
      const name = shownName(src);
      el.append(sourceCard({
        thumb: j.source?.thumbnail, iconName: j.source?.kind === 'file' ? 'music' : 'video', title: name.title,
        meta: metaJoin(name.artist, 'From an earlier job', j.source?.kind === 'file' ? j.source?.name : null, fmtDuration(j.source?.duration_s)),
        extra: guessLine(j.result?.classification), rename: src, cover: src,
      }));
      break;
    }
    default:
      break;
  }
}

// ---------------------------------------------------------------------------
// Several files: a compact list, one job per file
// ---------------------------------------------------------------------------
function filesList(src) {
  const items = src.items;
  const total = items.reduce((n, it) => n + (it.file.size || 0), 0);
  const busy = state.busy;
  return h('div', { class: 'mf' },
    h('div', { class: 'mf-head' },
      h('div', { class: 'job-thumb mf-ico' }, icon('layers', 20)),
      h('div', { class: 'mf-text' },
        h('div', { class: 'source-title' }, `${items.length} files`),
        h('div', { class: 'source-meta' }, metaJoin('On this computer', fmtBytes(total), 'one job per file'))),
      h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm', disabled: busy,
        onclick: () => document.getElementById('fileInput').click(),
      }, icon('plus', 14), 'Add more'),
      h('button', { type: 'button', class: 'icon-btn source-remove', 'aria-label': 'Remove all files', disabled: busy, onclick: clearSource }, icon('close', 16))),
    h('ul', { class: 'mf-list', 'aria-label': 'Files' }, items.map((it) => fileItemRow(src, it))));
}

function fileItemRow(src, it) {
  const ui = {
    led: led('off'),
    status: h('span', { class: 'mf-status' }),
    error: h('div', { class: 'mf-error' }),
    meter: meter(0, { label: `Upload of ${it.file.name}` }),
    remove: h('button', {
      type: 'button', class: 'icon-btn icon-btn-sm mf-remove', 'aria-label': `Remove ${it.file.name}`, title: 'Remove',
      onclick: () => removeItem(src, it),
    }, icon('close', 14)),
  };
  ui.row = h('li', { class: 'mf-item' },
    ui.led,
    h('div', { class: 'mf-main' }, h('span', { class: 'mf-name', title: it.file.name }, it.file.name), ui.error, ui.meter),
    ui.status, ui.remove);
  it.ui = ui;
  updateItem(it);
  return ui.row;
}

/** Refresh one file's row (status LED, upload meter, error). */
export function updateItem(it) {
  const ui = it.ui;
  if (!ui) return;
  ui.row.dataset.status = it.status;
  const next = led({ uploading: 'on', done: 'ok', error: 'bad' }[it.status] || 'off', { blink: it.status === 'uploading' });
  ui.led.replaceWith(next);
  ui.led = next;
  ui.status.textContent = it.status === 'uploading' ? `${Math.round(it.pct)} %`
    : it.status === 'done' ? 'Added' : it.status === 'error' ? 'Didn’t work' : fmtBytes(it.file.size);
  ui.error.textContent = it.status === 'error' ? (it.error || '') : '';
  ui.meter.hidden = it.status !== 'uploading';
  if (it.status === 'uploading') setMeter(ui.meter, it.pct);
  ui.remove.hidden = state.busy;
}

function removeItem(src, it) {
  if (state.busy) return;
  const i = src.items.indexOf(it);
  src.items = src.items.filter((x) => x !== it);
  if (!src.items.length) { clearSource(); return; }
  if (src.items.length === 1) {
    // Back to the single-file card (reusing its upload when it has one).
    const last = src.items[0];
    state.source = { kind: 'file', file: last.file, ...(last.jobId ? { reuseJobId: last.jobId } : {}) };
    measureFile(state.source);
    redraw();
    focusIn('.source-remove');
    return;
  }
  redraw();
  const rows = refs.source.querySelectorAll('.mf-remove');
  (rows[Math.min(i, rows.length - 1)] || refs.source.querySelector('.source-remove'))?.focus({ preventScroll: true });
}

// ---------------------------------------------------------------------------
// Playlist: pick the items to process
// ---------------------------------------------------------------------------
function playlistCard(src) {
  const p = src.probe;
  const entries = p.entries || [];
  const count = h('span', { class: 'pl-count' });
  const updateCount = () => { count.textContent = `${src.selected.size} of ${entries.length} selected`; };
  updateCount();
  const boxes = [];
  const list = h('ul', { class: 'pl-list' }, entries.map((e, i) => {
    const id = `pl-${i}`;
    const cb = h('input', { type: 'checkbox', id, checked: src.selected.has(e.url) });
    cb.addEventListener('change', () => {
      if (cb.checked) src.selected.add(e.url); else src.selected.delete(e.url);
      updateCount();
      redraw('action');
    });
    boxes.push([cb, e.url]);
    return h('li', null, h('label', { for: id },
      cb,
      h('span', { class: 'pl-title' }, e.title || e.url),
      h('span', { class: 'pl-dur' }, fmtDuration(e.duration_s))));
  }));
  const setAll = (on) => {
    src.selected = new Set(on ? entries.map((e) => e.url) : []);
    boxes.forEach(([cb]) => { cb.checked = on; });
    updateCount();
    redraw('action');
  };
  return h('div', { class: 'playlist' },
    sourceCard({
      iconName: 'layers', title: p.title || 'Playlist',
      meta: metaJoin(p.uploader, `Playlist · ${p.count ?? entries.length} items`),
      extra: p.truncated ? h('div', { class: 'muted small' }, `Showing the first ${entries.length} items.`) : null,
    }),
    h('div', { class: 'pl-bar' },
      count,
      h('div', { class: 'pl-bar-actions' },
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => setAll(true) }, 'Select all'),
        h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => setAll(false) }, 'Select none'))),
    list);
}
