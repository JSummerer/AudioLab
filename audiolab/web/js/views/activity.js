// Activity (#/activity): every job in one list — "In progress" on top, then finished jobs
// grouped by day. Filters (All · Running · Done · Didn't finish) narrow the list; a job's
// row opens its page. Rows are kept and updated in place so focus and running meters
// survive the frequent live updates; nothing ticks while the view is hidden.
import { h, icon, clear, fmtAgo } from '../dom.js';
import { store, on, jobsSorted, isActive, isFinished, taskLabel } from '../store.js';
import { meter, setMeter, statusLed } from '../ui.js';
import { go, jobPath } from '../router.js';
import { cancelJob, retryJob, removeJob, clearFinished } from '../actions.js';
import { TASK_ICON, coverUrl } from './job/index.js';
const STALE_AFTER_S = 60;
const DAY_MS = 86400000;
const FILTERS = [
  { key: 'all', label: 'All', test: () => true, empty: '' },
  { key: 'running', label: 'Running', test: isActive, empty: 'Nothing is running right now.' },
  { key: 'done', label: 'Done', test: (j) => j.status === 'done', empty: 'Nothing has finished yet.' },
  // Failed and cancelled (or not started) jobs, both offer "Try again".
  { key: 'failed', label: 'Didn’t finish', test: (j) => j.status === 'failed' || j.status === 'cancelled', empty: 'Every job finished.' },
];

let refs = null;
let filter = 'all';
let visible = false;
let ticker = 0;
const cards = new Map();  // job id → { el, job, refs }
const groups = new Map(); // group key → { el, title, list }

export const activityView = {
  el: null,
  heading: null,

  mount(el) {
    this.el = el;
    refs = {
      title: h('h1', { class: 'page-title', id: 'activityTitle', tabindex: '-1' }, 'Activity'),
      sub: h('p', { class: 'page-sub' }),
      clear: h('button', { type: 'button', class: 'btn btn-ghost btn-quiet btn-sm', onclick: onClearFinished }, icon('trash', 14), 'Clear finished'),
      filters: h('div', { class: 'filters', role: 'group', 'aria-label': 'Show' }),
      groups: h('div', { class: 'job-groups' }),
      noMatch: h('p', { class: 'filter-empty' }),
      empty: h('div', { class: 'activity-empty' },
        h('div', { class: 'empty-meter', 'aria-hidden': 'true' }, ...Array.from({ length: 9 }, (_, i) => h('i', { style: { '--i': i } }))),
        h('p', null, h('strong', null, 'Nothing here yet.')),
        h('p', { class: 'muted' }, 'Jobs you start appear here with their progress. Finished jobs are cleared after a while — your files stay in the output folder.'),
        h('button', { type: 'button', class: 'btn btn-primary', onclick: () => go('#/new') }, icon('plus', 16), 'Start something')),
    };
    this.heading = refs.title;
    refs.filterBtns = FILTERS.map((f) => {
      const n = h('span', { class: 'filter-n' });
      const b = h('button', { type: 'button', class: 'filter', 'aria-pressed': String(f.key === filter) }, f.label, n);
      b.addEventListener('click', () => { filter = f.key; render(); });
      refs.filters.append(b);
      return { f, b, n };
    });
    el.append(h('div', { class: 'page' },
      h('header', { class: 'page-head' },
        h('div', { class: 'page-head-text' }, refs.title, refs.sub),
        h('div', { class: 'page-actions' }, refs.clear)),
      refs.filters, refs.groups, refs.noMatch, refs.empty));
    on('jobs', () => { if (visible) render(); });
  },

  show() {
    visible = true;
    render(true);
    // Relative times and "no update for a while" notes.
    clearInterval(ticker);
    ticker = setInterval(() => render(true), 15000);
  },

  hide() {
    visible = false;
    clearInterval(ticker);
  },
};

async function onClearFinished() {
  refs.clear.disabled = true;
  await clearFinished();
  refs.clear.disabled = false;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------
function render(tick = false) {
  const all = jobsSorted();
  const loading = !store.jobsLoaded && !all.length;
  const running = all.filter((j) => j.status === 'running').length;
  const waiting = all.filter((j) => j.status === 'queued').length;
  const finished = all.filter(isFinished).length;
  refs.sub.textContent = loading ? 'Loading…'
    : [running && `${running} running`, waiting && `${waiting} waiting`, finished && `${finished} finished`].filter(Boolean).join(' · ')
      || 'Your jobs and their results.';
  refs.clear.hidden = !finished;
  refs.empty.hidden = loading || all.length > 0;
  refs.filters.hidden = !all.length;
  for (const { f, b, n } of refs.filterBtns) {
    b.setAttribute('aria-pressed', String(f.key === filter));
    const count = f.key === 'all' ? 0 : all.filter(f.test).length;
    n.textContent = count ? String(count) : '';
  }

  const active = FILTERS.find((f) => f.key === filter);
  const shown = all.filter(active.test);
  refs.noMatch.hidden = !all.length || shown.length > 0;
  refs.noMatch.textContent = active.empty;

  const ids = new Set(shown.map((j) => j.id));
  for (const [id, c] of cards) {
    if (!ids.has(id)) { c.el.remove(); cards.delete(id); }
  }
  const grouped = groupJobs(shown);
  const groupEls = grouped.map((g) => {
    let grp = groups.get(g.key);
    if (!grp) {
      const title = h('h2', { class: 'group-title' });
      const list = h('ol', { class: 'job-list' });
      grp = { el: h('section', { class: 'job-group' }, title, list), title, list };
      groups.set(g.key, grp);
    }
    grp.title.textContent = g.title;
    grp.list.setAttribute('aria-label', g.title);
    syncChildren(grp.list, g.jobs.map((job) => cardFor(job, tick).el));
    return grp.el;
  });
  const keys = new Set(grouped.map((g) => g.key));
  for (const [key, grp] of groups) {
    if (!keys.has(key)) { grp.el.remove(); groups.delete(key); }
  }
  syncChildren(refs.groups, groupEls);
}

/** Put `els` into `parent` in order, moving only what is out of place (moving a node drops its focus). */
function syncChildren(parent, els) {
  els.forEach((el, i) => {
    if (parent.children[i] !== el) parent.insertBefore(el, parent.children[i] || null);
  });
  while (parent.children.length > els.length) parent.lastElementChild.remove();
}

function finishedTime(job) {
  return job.finished_at || job.updated_at || job.created_at || 0;
}

function groupJobs(jobs) {
  const out = [];
  const inProgress = jobs.filter(isActive).sort((a, b) =>
    (a.status === b.status ? 0 : a.status === 'running' ? -1 : 1)
    || (a.queue_position ?? 0) - (b.queue_position ?? 0)
    || (b.created_at || 0) - (a.created_at || 0));
  if (inProgress.length) out.push({ key: 'active', title: 'In progress', jobs: inProgress });
  const done = jobs.filter((j) => !isActive(j)).sort((a, b) => finishedTime(b) - finishedTime(a));
  for (const job of done) {
    const day = startOfDay(finishedTime(job) * 1000);
    const key = `day-${day}`;
    let g = out[out.length - 1];
    if (g?.key !== key) { g = { key, title: dayTitle(day), jobs: [] }; out.push(g); }
    g.jobs.push(job);
  }
  return out;
}

function startOfDay(ms) {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function dayTitle(day) {
  const ago = Math.round((startOfDay(Date.now()) - day) / DAY_MS);
  if (ago === 0) return 'Today';
  if (ago === 1) return 'Yesterday';
  const d = new Date(day);
  if (ago < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'long', year: sameYear ? undefined : 'numeric' });
}

/** "just now" / "12 min ago" today, the clock time on earlier days. */
function whenText(ts) {
  if (!ts) return '';
  if (startOfDay(ts * 1000) === startOfDay(Date.now())) return fmtAgo(ts);
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// ---------------------------------------------------------------------------
// Rows (the `job-card` class is the 2.3 name)
// ---------------------------------------------------------------------------
function cardFor(job, tick) {
  let c = cards.get(job.id);
  if (!c) {
    c = { el: h('li', { class: 'job-card' }), job, refs: null };
    fill(c, job);
    cards.set(job.id, c);
  } else if (c.job !== job || tick) {
    updateCard(c, job);
  }
  return c;
}

/** "Enhance · 2 files · 12 min ago": the task in words, then how it went. */
function subtitle(job) {
  return `${taskLabel(job)} · ${statusText(job)}`;
}

function statusText(job) {
  switch (job.status) {
    case 'queued': return job.queue_position ? `Waiting · #${job.queue_position} in line` : 'Waiting';
    case 'running': {
      const stage = (job.stages || []).find((s) => s.key === job.stage);
      return `${stage ? stage.label : 'Working'} · ${Math.round(job.progress || 0)} %`;
    }
    case 'done': {
      const when = whenText(finishedTime(job));
      if (job.task === 'analyze') return `Report · ${when}`;
      const files = job.result?.files || [];
      const tracks = files.filter((f) => f.group === 'tracks').length;
      const n = files.length - tracks;
      const what = [`${n} file${n === 1 ? '' : 's'}`, tracks && `${tracks} tracks`].filter(Boolean).join(' + ');
      return `${what} · ${when}`;
    }
    case 'failed': return `Didn’t work · ${whenText(finishedTime(job))}`;
    case 'cancelled': return `Cancelled · ${whenText(finishedTime(job))}`;
    default: return job.status;
  }
}

function lineText(job) {
  if (job.status === 'failed') return job.error?.message || job.message || '';
  if (job.status === 'running') {
    // Not when it only repeats the line above ("Enhancing… 62 %" under "Enhance · Enhancing · 62 %").
    const stage = (job.stages || []).find((s) => s.key === job.stage);
    const words = (job.message || '').replace(/\.\.\.|…|\d+(\.\d+)?\s*%/g, '').trim().toLowerCase();
    return stage && words === stage.label.toLowerCase() ? '' : job.message || '';
  }
  // "Not started — AudioLab was closed…", not a plain "Cancelled." (the line above says that)
  if (job.status === 'cancelled' && job.message && !/^cancel/i.test(job.message)) return job.message;
  return '';
}

function staleText(job) {
  if (job.status !== 'running' || !job.updated_at) return '';
  const idle = Date.now() / 1000 - job.updated_at;
  if (idle < STALE_AFTER_S) return '';
  const mins = Math.round(idle / 60);
  return `No update for ${mins < 2 ? 'a minute' : `${mins} min`} — long steps can be quiet.`;
}

function actionButtons(job) {
  const btns = [];
  if (isActive(job)) btns.push(['close', 'Cancel', () => cancelJob(job)]);
  // Try again in words: it is the row's one action that does something new.
  if (job.status === 'failed' || job.status === 'cancelled') btns.push(['retry', 'Try again', () => retryJob(job), true]);
  if (isFinished(job)) btns.push(['trash', 'Remove from list', () => removeJob(job)]);
  return btns.map(([ico, label, fn, words]) => h('button', {
    type: 'button', class: words ? 'btn btn-ghost btn-quiet btn-sm job-retry' : 'icon-btn icon-btn-sm',
    'aria-label': `${label}: ${job.title}`, title: label, onclick: fn,
  }, icon(ico, 15), words ? h('span', { class: 'job-retry-text' }, label) : null));
}

function updateCard(c, job) {
  // Cheap in-place update while a job runs; a full refill when its status changes.
  if (c.job.status === job.status && job.status === 'running' && c.refs) {
    // A link's real title arrives once the job has looked it up.
    if (c.job.title !== job.title) {
      c.refs.link.textContent = job.title || 'Untitled';
      c.refs.meter.setAttribute('aria-label', `Progress of ${job.title}`);
      c.refs.buttons.forEach((b) => b.setAttribute('aria-label', `${b.title}: ${job.title}`));
    }
    c.refs.sub.textContent = subtitle(job);
    c.refs.line.textContent = lineText(job);
    c.refs.stale.textContent = staleText(job);
    setMeter(c.refs.meter, job.progress);
    c.job = job;
    return;
  }
  fill(c, job);
  c.job = job;
}

function fill(c, job) {
  // Keep focus on the same control when a card is rebuilt under it.
  const focused = c.el.contains(document.activeElement) ? document.activeElement : null;
  const focusIndex = focused ? [...c.el.querySelectorAll('a, button')].indexOf(focused) : -1;
  const el = clear(c.el);
  el.dataset.status = job.status;
  const thumb = h('div', { class: 'job-thumb' });
  const cover = coverUrl(job);
  if (cover) {
    const img = h('img', { src: cover, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', () => { clear(thumb).append(icon(TASK_ICON[job.task] || 'music', 20)); });
    thumb.append(img);
  } else {
    thumb.append(icon(TASK_ICON[job.task] || 'music', 20));
  }
  const sub = h('span', { class: 'job-sub' }, subtitle(job));
  const line = h('div', { class: 'job-line' }, lineText(job));
  const stale = h('div', { class: 'job-stale' }, staleText(job));
  const m = isActive(job) ? meter(job.progress, { idle: job.status === 'queued', label: `Progress of ${job.title}` }) : null;
  // The title link covers the whole row (::after); the action buttons sit above it.
  const link = h('a', { class: 'job-open', href: jobPath(job.id) }, job.title || 'Untitled');
  const buttons = actionButtons(job);
  el.append(
    thumb,
    h('div', { class: 'job-main' },
      h('span', { class: 'job-title' }, link),
      h('div', { class: 'job-meta' }, statusLed(job), sub),
      m, line, stale),
    h('div', { class: 'job-actions' }, buttons),
    icon('chevron', 16, 'job-go'));
  c.refs = { sub, line, stale, meter: m, link, buttons };
  if (focusIndex >= 0) {
    const targets = [...el.querySelectorAll('a, button')];
    (targets[focusIndex] || link).focus({ preventScroll: true });
  }
}
