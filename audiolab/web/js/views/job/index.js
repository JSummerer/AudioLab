// Job page (#/job/<id>), the old side sheet as a page: Back (the desktop window has
// no browser buttons), the sleeve header (the cover, what the job made, the title, one
// credits line with the status), then whatever fits the job — live progress, a failure
// with its fixes, or the results. "Show details" (numbers, decisions and log details
// instead of plain words only; remembered) sits next to what it changes: the page's
// builders mark that spot with `.details-slot`. Copes with a reload straight onto a job
// URL (looks the job up itself), unknown ids and jobs removed while open. Leaving the
// page stops its players, timers and polling.
import { h, icon, clear, fmtDuration } from '../../dom.js';
import { api, mediaUrl } from '../../api.js';
import { store, on, isActive, isFinished, upsertJob, taskLabel, jobsFence, mayInsertJob, showDetails, setShown } from '../../store.js';
import { meter, statusLed, statusWord, callout, switchControl } from '../../ui.js';
import { back, go } from '../../router.js';
import { liveView, updateLive, failedView, cancelledView } from './status.js';
import { doneView, RECIPE_SHORT } from './results.js';

export const TASK_ICON = { download: 'download', enhance: 'sparkle', separate: 'layers', analyze: 'gauge' };

/** A job's cover: the server's own saved copy first, else the link's thumbnail; null when it has none. */
export function coverUrl(job) {
  const saved = (job.result?.files || []).find((f) => f.kind === 'cover' && f.exists !== false);
  if (saved) return mediaUrl(job.id, `file:${saved.id}`);
  return job.source?.thumbnail || null;
}

let refs = null;
// { id, key, dataKey, seen, lookup: 'idle' | 'loading' | 'done' | 'missing' | 'error', error, ctx }
let page = null;

export const jobView = {
  el: null,
  heading: null,
  scrollPerArg: true,
  fallback: '#/activity',

  mount(el) {
    this.el = el;
    refs = {
      cover: h('div', { class: 'job-cover', hidden: true }),
      eyebrow: h('p', { class: 'job-eyebrow' }),
      title: h('h1', { class: 'page-title', id: 'jobTitle', tabindex: '-1' }),
      sub: h('p', { class: 'job-sub-line' }),
      body: h('div', { class: 'job-body' }),
      details: switchControl({
        id: 'jobDetails',
        onChange: (v) => { setShown('details', v); if (page) { page.key = null; render({ anchor: true }); } },
      }),
      coverKey: null,
      headKey: null,
    };
    refs.detailsToggle = h('label', { class: 'details-toggle', for: 'jobDetails' }, refs.details, 'Show details');
    refs.bar = h('div', { class: 'job-bar' },
      h('button', { type: 'button', class: 'btn btn-ghost btn-quiet btn-sm', onclick: () => back() },
        icon('chevron', 16, 'ico-flip'), 'Back'));
    refs.head = h('header', { class: 'job-head' },
      refs.cover, h('div', { class: 'job-head-text' }, refs.eyebrow, refs.title, refs.sub));
    this.heading = refs.title;
    el.append(h('div', { class: 'page' }, refs.bar, refs.head, refs.body));
    on('jobs', (ids) => { if (page && (!ids || ids.includes(page.id))) render(); });
  },

  show(id) {
    refs.details.checked = showDetails(); // Settings may have changed the default meanwhile
    if (page?.id !== id) {
      teardown();
      page = { id, key: null, dataKey: null, seen: false, lookup: 'idle', error: '', ctx: newContext() };
      const cached = store.jobs.get(id);
      if (cached && isFinished(cached)) refresh(id);
    }
    render();
  },

  hide() {
    teardown();
    page = null;
  },
};

/** Cleanups registered by the builders (players, tickers, journal polling). `entry`: the
 * Library entry the compare player was built from, kept while the job's files stay the
 * same, so "Show details" rebuilds the page at once, without the player's box collapsing. */
function newContext() {
  const fns = [];
  return {
    live: null,
    entry: undefined,
    onCleanup: (fn) => fns.push(fn),
    cleanup() {
      fns.splice(0).forEach((fn) => { try { fn(); } catch { /* ignore */ } });
      this.live = null;
    },
  };
}

function teardown() {
  page?.ctx.cleanup();
  // Plain <audio>/<video> elements aren't tracked by the players: pause them too.
  refs?.body.querySelectorAll('audio, video').forEach((m) => m.pause());
}

function resetBody() {
  teardown();
  return clear(refs.body);
}

/** `anchor`: keep "Show details" where it is on screen (and its focus) while the page is rebuilt under it. */
function render({ anchor = false } = {}) {
  if (!page) return;
  const job = store.jobs.get(page.id);
  if (!job) { renderMissing(); return; }
  page.seen = true;
  refs.detailsToggle.hidden = false;
  renderHead(job);
  // Files moved or deleted since lose Download, Listen and their place in the ZIP. Not every file:
  // the cover saved from this page joins the list by itself, without stopping the players.
  const gone = (job.result?.files || []).filter((f) => f.exists === false).map((f) => f.id).join(',');
  const dataKey = `${job.status}|${job.result ? 1 : 0}|${gone}`;
  const key = `${dataKey}|${showDetails() ? 1 : 0}`;
  if (key === page.key) {
    if (isActive(job)) updateLive(job, page.ctx);
    return;
  }
  page.key = key;
  if (dataKey !== page.dataKey) {
    page.dataKey = dataKey;
    page.ctx.entry = undefined;
  }
  const scroller = document.getElementById('main');
  const top = anchor && refs.detailsToggle.isConnected ? refs.detailsToggle.getBoundingClientRect().top : null;
  const focused = document.activeElement === refs.details;
  const body = resetBody();
  if (isActive(job)) body.append(...liveView(job, page.ctx));
  else if (job.status === 'failed') body.append(...failedView(job, page.ctx));
  else if (job.status === 'cancelled') body.append(...cancelledView(job, page.ctx));
  else if (job.status === 'done') body.append(...doneView(job, page.ctx));
  (body.querySelector('.details-slot') || refs.bar).append(refs.detailsToggle);
  if (top != null && scroller) scroller.scrollTop += refs.detailsToggle.getBoundingClientRect().top - top;
  if (focused) refs.details.focus({ preventScroll: true });
}

// ---------------------------------------------------------------------------
// The sleeve: cover, what the job made, title, credits
// ---------------------------------------------------------------------------
function renderHead(job) {
  const src = job.source || {};
  const r = job.result || {};
  const duration = src.duration_s || r.analysis?.duration_s;
  const artist = job.options?.meta?.artist;
  const eyebrow = eyebrowText(job);
  // Rebuilt only when something in it changes: live updates would restart the status light's blink.
  const headKey = JSON.stringify([job.status, job.title, eyebrow, artist, src.url, src.name, duration]);
  renderCover(job);
  if (headKey === refs.headKey) return;
  refs.headKey = headKey;
  refs.title.textContent = job.title || 'Untitled';
  refs.eyebrow.textContent = eyebrow;
  refs.eyebrow.hidden = !eyebrow;
  const parts = [];
  if (artist) parts.push(h('span', null, artist));
  else if (src.kind === 'url' && src.url) parts.push(h('span', null, hostOf(src.url)));
  else if (src.kind === 'file' && src.name) parts.push(h('span', { class: 'job-file-name', title: src.name }, src.name));
  if (duration) parts.push(h('span', { class: 'num' }, fmtDuration(duration)));
  parts.push(h('span', { class: 'job-status' }, statusLed(job), statusWord(job)));
  if (src.kind === 'url' && src.url) {
    parts.push(h('a', { class: 'job-link', href: src.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open source', icon('external', 13)));
  }
  clear(refs.sub).append(...parts.flatMap((p, i) => (i ? [h('span', { class: 'job-dot', 'aria-hidden': 'true' }, '·'), p] : [p])));
}

/** The sleeve's picture: a finished job always has one (its cover, or a quiet square with the
 * task's icon); while running or after a failure only when there is a real cover. */
function renderCover(job) {
  const src = coverUrl(job);
  const shown = job.status === 'done' || !!src;
  refs.head.classList.toggle('has-cover', shown);
  refs.cover.hidden = !shown;
  const key = shown ? `${src || ''}|${job.task}` : null;
  if (key === refs.coverKey) return;
  refs.coverKey = key;
  const empty = () => {
    refs.cover.classList.add('is-empty');
    clear(refs.cover).append(icon(TASK_ICON[job.task] || 'music', 32));
  };
  if (!shown) clear(refs.cover);
  else if (!src) empty();
  else {
    const img = h('img', { src, alt: '', decoding: 'async', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', empty);
    refs.cover.classList.remove('is-empty');
    clear(refs.cover).append(img);
  }
}

/** What the job made, above the title ("Enhanced · Song", "Saved as MP3"); the task until it is done. */
function eyebrowText(job) {
  if (job.status !== 'done') return taskLabel(job);
  const r = job.result || {};
  const files = r.files || [];
  const tracks = files.filter((f) => f.group === 'tracks').length;
  const main = files.find((f) => f.group !== 'tracks' && f.kind !== 'cover' && f.kind !== 'report');
  let what;
  switch (job.task) {
    case 'enhance':
      what = [job.options?.quality === 'light' ? 'Volume evened out' : 'Enhanced', RECIPE_SHORT[r.recipe?.key] || r.recipe?.name];
      break;
    case 'download': {
      const format = (main?.label || '').split(' · ')[0].replace(/^Original \((.+)\)$/, '$1 (original)');
      what = [format ? `${job.source?.kind === 'file' ? 'Converted to' : 'Saved as'} ${format}` : taskLabel(job)];
      break;
    }
    case 'separate': {
      const n = (r.previews || []).filter((p) => p.key.startsWith('stem:')).length;
      what = [n ? `Split into ${n} stems` : 'Split'];
      break;
    }
    case 'analyze': what = ['Quality check']; break;
    default: what = [taskLabel(job)];
  }
  if (tracks) what.push(`${tracks} tracks`);
  return what.filter(Boolean).join(' · ');
}

function hostOf(link) {
  try { return new URL(link).hostname.replace(/^www\./, ''); } catch { return 'Link'; }
}

/** A finished job from the cache: its files may have been moved or deleted since, so ask again. */
async function refresh(id) {
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (job?.id === id && store.jobs.has(id)) upsertJob(job); // not back once it was removed meanwhile
  } catch { /* the cached copy stays */ }
}

// ---------------------------------------------------------------------------
// Not in the store: still loading, unknown id, or removed while open
// ---------------------------------------------------------------------------
async function lookup(id) {
  const asked = page; // a new visit or another job makes this answer stale
  asked.lookup = 'loading';
  const fence = jobsFence();
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (page !== asked) return;
    asked.lookup = 'done';
    // Not when live events brought a newer copy, or the job was removed meanwhile.
    if (job?.id === id && mayInsertJob(id, fence)) { upsertJob(job); return; } // `jobs` re-renders
  } catch (e) {
    if (page !== asked) return;
    asked.lookup = e.status === 404 ? 'missing' : 'error';
    asked.error = e.message;
  }
  render();
}

function renderMissing() {
  if (!page.seen && page.lookup === 'idle') lookup(page.id);
  const state = page.seen ? 'removed' : page.lookup === 'done' ? 'missing' : page.lookup;
  const key = `missing|${state}`;
  if (page.key === key) return;
  page.key = key;
  const body = resetBody();
  clear(refs.sub);
  refs.headKey = null;
  refs.eyebrow.hidden = true;
  renderCover({ status: state, task: null, source: {} });
  refs.detailsToggle.hidden = true;
  if (state === 'loading') {
    refs.title.textContent = 'Loading…';
    body.append(h('div', { class: 'job-loading' }, meter(0, { idle: true, label: 'Loading the job' })));
    return;
  }
  const ways = h('div', { class: 'callout-actions' },
    h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: () => go('#/activity') }, icon('activity', 15), 'Go to Activity'),
    h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => go('#/new') }, icon('plus', 15), 'Start something new'));
  if (state === 'error') {
    refs.title.textContent = 'Couldn’t load this job';
    const retry = h('button', {
      type: 'button', class: 'btn btn-primary btn-sm',
      onclick: () => { page.lookup = 'idle'; page.key = null; render(); },
    }, icon('retry', 15), 'Try again');
    ways.prepend(retry);
    body.append(callout('danger', null, page.error || 'Something went wrong.', ways));
    return;
  }
  refs.title.textContent = state === 'removed' ? 'Removed' : 'Job not found';
  body.append(callout('info',
    state === 'removed' ? 'This job was removed from the list.' : 'This job isn’t in the list.',
    state === 'removed' ? 'Its files are still in your output folder.'
      : 'It may have been removed or cleared. Files it made are still in your output folder.',
    ways));
}
