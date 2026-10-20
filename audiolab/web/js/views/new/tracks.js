// New view — tracks: Save and Enhance of one link or file can cut it into one file per
// track, by the link's chapters (or the tracklist in its description) or by a tracklist
// the user pastes. An earlier job's link is looked up again for its chapters (source.js),
// and its pasted tracklist comes along (setTracklist). The pasted text is the thing to edit: the server checks it while
// they type (POST /api/tracklist, the strict parser the job uses), the recognised
// tracks and the problems per line show right under it, and it is sent as `tracklist`.
import { h, icon, append, clear, fmtDuration } from '../../dom.js';
import { api } from '../../api.js';
import { switchControl } from '../../ui.js';
import { state, redraw } from './state.js';

const FROM = {
  chapters: 'Found in the video’s chapters.',
  description: 'Found in the tracklist in the description.',
  pasted: 'From your tracklist.',
};
const EXAMPLE = '0:00 Artist – First track\n4:12 Artist – Second track\n1:02:30 Artist – Last track';
const MAX_CHARS = 50000;
const CHECK_DELAY = 400;

let ui = null; // the shown block's live parts { head, status, ta, out }, updated in place while typing
let timer = 0;
let ctl = null;

/** Save and Enhance of one link or one file can be split. */
export function tracksOffered() {
  return ['download', 'enhance'].includes(state.task) && ['url', 'file', 'job'].includes(state.source?.kind);
}

const pasted = (src) => (src?.tracklist?.text.trim() ? src.tracklist : null);

/** What the job would cut by: a pasted list once it checks out, else the link's own tracks. */
export function knownTracks() {
  const src = state.source;
  const tl = pasted(src);
  if (tl) return tl.tracks?.length && !tl.errors.length ? { tracks: tl.tracks, from: 'pasted' } : null;
  const own = linkTracks(src);
  return own.length >= 2 ? { tracks: own, from: src.probe.tracks_source } : null;
}

/** A link's own tracks (chapters, description): a pasted link, or an earlier job's once looked up. */
const linkTracks = (src) => (['url', 'job'].includes(src?.kind) ? src.probe?.tracks || [] : []);

/** Put a tracklist (an earlier job's) into the box of `src`, the current source, and check it. */
export function setTracklist(src, text) {
  src.tracklist = { text, tracks: null, errors: [], pending: false, failed: null };
  if (text.trim()) check(src);
}

const problems = (n) => `${n} problem${n === 1 ? '' : 's'}`;

/** Why Start has to wait for the pasted tracklist, or null. */
export function tracksProblem() {
  const tl = tracksOffered() && pasted(state.source);
  if (!tl) return null;
  if (tl.errors.length) return `Fix the tracklist first: ${problems(tl.errors.length)}, listed under it.`;
  if (tl.pending) return 'Checking your tracklist…';
  // Only a successful check of the current text lets it through.
  if (tl.failed) return 'The tracklist couldn’t be checked — try again under it.';
  return null;
}

/** `tracklist` and `split_tracks` for the job (docs/API.md "Task options"). */
export function tracksOptions() {
  if (!tracksOffered()) return {};
  const src = state.source;
  const o = {};
  if (pasted(src)) o.tracklist = src.tracklist.text;
  // An earlier link job's choice stands while its link is still being looked up (the job finds the chapters itself).
  const lookingUp = src.kind === 'job' && src.job?.source?.kind === 'url' && !src.probe;
  if (src.splitTracks && (knownTracks() || lookingUp)) o.split_tracks = true;
  return o;
}

/** A local file's length from its header (for "after the end" checks), when the browser can read it. */
export function measureFile(src) {
  const audio = document.createElement('audio');
  const objectUrl = URL.createObjectURL(src.file);
  const done = () => URL.revokeObjectURL(objectUrl);
  audio.preload = 'metadata';
  audio.addEventListener('loadedmetadata', () => {
    if (Number.isFinite(audio.duration) && audio.duration > 0) {
      src.duration_s = audio.duration;
      if (pasted(src)) check(src);
    }
    audio.removeAttribute('src');
    done();
  }, { once: true });
  audio.addEventListener('error', done, { once: true });
  audio.src = objectUrl;
}

function lengthOf(src) {
  const s = src.kind === 'url' ? src.probe?.duration_s : src.kind === 'file' ? src.duration_s : src.job?.source?.duration_s;
  return s > 0 ? s : undefined;
}

// ---------------------------------------------------------------------------
// The block: the split switch (once there are tracks), then "Paste a tracklist"
// ---------------------------------------------------------------------------
export function tracksBlock() {
  ui = null;
  if (!tracksOffered()) return null;
  const src = state.source;
  const tl = (src.tracklist ||= { text: '', tracks: null, errors: [], pending: false, failed: null });
  const ownTracks = linkTracks(src).length >= 2;
  const open = src.pasteOpen ?? !!tl.text.trim();
  const ta = h('textarea', {
    id: 'tracklistText', class: 'field tl-text', rows: 6, maxlength: MAX_CHARS,
    spellcheck: 'false', autocomplete: 'off', placeholder: EXAMPLE,
    'aria-label': 'Tracklist', 'aria-describedby': 'tracklistHelp tracklistErrors',
  });
  ta.value = tl.text;
  ta.addEventListener('input', () => onInput(src, ta.value));
  ui = { head: headPart(src), status: h('span', { class: 'more-status tl-status', role: 'status' }), ta, out: h('div', { class: 'tl-out' }) };
  const panel = h('div', { class: 'tl-panel', id: 'tracklistPanel', hidden: !open },
    h('p', { class: 'muted small', id: 'tracklistHelp' },
      `One track per line, with its start time${ownTracks ? ' — used instead of the chapters' : ''}.`),
    ta, ui.out);
  const toggle = h('button', { type: 'button', class: 'more-toggle', 'aria-expanded': String(open), 'aria-controls': panel.id },
    h('span', { class: 'more-mark', 'aria-hidden': 'true' }), ownTracks ? 'Use my own tracklist' : 'Paste a tracklist');
  toggle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    src.pasteOpen = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
    if (!panel.hidden) ta.focus();
  });
  fillOut(src);
  return h('div', { class: 'opt' },
    h('div', { class: 'opt-label' }, 'Tracks'),
    h('div', { class: 'tracks-opt' }, ui.head, h('div', { class: 'more-head' }, toggle, ui.status), panel));
}

function headPart(src) {
  const known = knownTracks();
  if (!known) return h('p', { class: 'muted small' }, 'Have a tracklist with times? Paste it to cut the recording into one file per track.');
  const n = known.tracks.length;
  const title = `Split into ${n} track${n === 1 ? '' : 's'}`;
  const sw = switchControl({
    id: 'splitTracks', checked: !!src.splitTracks, label: title,
    onChange: (v) => { src.splitTracks = v; redraw('action'); },
  });
  const what = state.task === 'enhance'
    ? 'One file per track, cut from the finished result — the full mix is saved too.'
    : 'One file per track — the full recording is saved too.';
  sw.setAttribute('aria-describedby', 'splitTracksHelp');
  return h('div', { class: 'tracks-head' },
    h('label', { class: 'switch-row', for: 'splitTracks' },
      sw,
      h('span', { class: 'switch-row-label' }, title),
      h('span', { class: 'switch-row-help', id: 'splitTracksHelp' }, [FROM[known.from], what].filter(Boolean).join(' '))),
    // A pasted list shows its tracks under the box instead.
    known.from === 'pasted' ? null : h('details', { class: 'tracks-list disclosure' },
      h('summary', null, `Show the ${n} tracks`),
      h('ol', null, known.tracks.map((t, i) => h('li', null,
        h('span', { class: 'tracks-n' }, String(t.n ?? i + 1).padStart(2, '0')),
        h('span', { class: 'tracks-title' }, t.title || `Track ${t.n ?? i + 1}`),
        h('span', { class: 'tracks-time' }, fmtDuration(t.start_s)))))));
}

/** Under the box: the problems per line, then the recognised tracks. */
function fillOut(src) {
  const tl = src.tracklist;
  const n = tl.tracks?.length || 0;
  const text = tl.text.trim();
  // The status only changes with an answer (not "checking" at every pause in typing).
  if (!text || tl.failed) ui.status.textContent = '';
  else if (!tl.pending) ui.status.textContent = tl.errors.length ? problems(tl.errors.length) : `${n} track${n === 1 ? '' : 's'} recognised`;
  ui.status.classList.toggle('is-stale', !!tl.pending);
  ui.ta.setAttribute('aria-invalid', String(!!(text && tl.errors.length)));
  append(clear(ui.out), [
    text && tl.failed ? h('p', { class: 'muted small' }, `Couldn’t check the tracklist (${tl.failed}). `,
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => check(src) }, icon('retry', 14), 'Check again')) : null,
    text && tl.errors.length ? h('ul', { class: 'tl-errors', id: 'tracklistErrors' },
      tl.errors.map((e) => h('li', null, h('strong', null, `Line ${e.line}: `), e.message))) : null,
    text && n ? h('ol', { class: 'tl-preview', 'aria-label': 'Recognised tracks' }, tl.tracks.map((t) => h('li', null,
      h('span', { class: 'tracks-n' }, String(t.n).padStart(2, '0')),
      h('span', { class: 'tracks-time' }, fmtDuration(t.start_s)),
      h('span', { class: 'tracks-title' }, t.artist ? `${t.artist} – ${t.title}` : t.title)))) : null]);
}

/** After an answer: the switch (tracks may have appeared or changed), the list, Start. */
function refresh(src) {
  if (state.source !== src) return;
  if (ui?.head.isConnected) {
    const focused = ui.head.contains(document.activeElement);
    const head = headPart(src);
    ui.head.replaceWith(head);
    ui.head = head;
    if (focused) document.getElementById('splitTracks')?.focus({ preventScroll: true });
    if (ui.out.contains(document.activeElement)) ui.ta.focus({ preventScroll: true }); // "Check again" goes away
    fillOut(src);
  }
  redraw('action');
}

function onInput(src, text) {
  const tl = src.tracklist;
  tl.text = text;
  clearTimeout(timer);
  ctl?.abort();
  if (!text.trim()) {
    Object.assign(tl, { tracks: null, errors: [], pending: false, failed: null });
    refresh(src);
    return;
  }
  if (!tl.pending) {
    tl.pending = true;
    ui?.status.classList.add('is-stale');
    redraw('action');
  }
  timer = setTimeout(() => check(src), CHECK_DELAY);
}

async function check(src) {
  // A source that is no longer shown (its file's length arrived late): leave the current one's check alone.
  if (state.source !== src) return;
  const tl = src.tracklist;
  const text = tl.text;
  ctl?.abort();
  const mine = ctl = new AbortController();
  if (!tl.pending) {
    tl.pending = true;
    ui?.status.classList.add('is-stale');
    redraw('action');
  }
  try {
    const res = await api('/api/tracklist', { method: 'POST', json: { text, duration_s: lengthOf(src) }, signal: mine.signal });
    if (tl.text !== text) return;
    Object.assign(tl, { tracks: res?.tracks || [], errors: res?.errors || [], pending: false, failed: null });
    // Pasting a list is asking to split by it — unless the user already chose.
    if (src.splitTracks === undefined && tl.tracks.length && !tl.errors.length) src.splitTracks = true;
  } catch (e) {
    if (e.name === 'AbortError' || tl.text !== text) return;
    Object.assign(tl, { tracks: null, errors: [], pending: false, failed: e.message });
  }
  refresh(src);
}
