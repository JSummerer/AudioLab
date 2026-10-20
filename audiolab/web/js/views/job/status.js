// Job page — how the job is going: live progress (steps, meter, message), the
// step-by-step journal, a failure with the fixes it offers, a cancelled job (both with
// "Try again" and "Change and start again"), and the footer (started when, remove, the
// "Show details" spot). Builders take the page context `ctx` ({ onCleanup(fn), live }) so
// timers and polling stop when the page goes away.
import { h, icon, clear, fmtDuration, fmtAgo, clockTime } from '../../dom.js';
import { api } from '../../api.js';
import { store, emit, showDetails, isActive, isFinished } from '../../store.js';
import { meter, setMeter, led, callout, toast, toastError, block } from '../../ui.js';
import { back, go, settingsPath, routeStamp } from '../../router.js';
import { cancelJob, retryJob, removeJob, copyDiagnostics, signInButton } from '../../actions.js';

// ---------------------------------------------------------------------------
// Running / queued
// ---------------------------------------------------------------------------
export function liveView(job, ctx) {
  const stepper = h('ol', { class: 'stepper', 'aria-label': 'Steps' },
    (job.stages || []).map((s) => h('li', { dataset: { key: s.key } }, led('off'), h('span', null, s.label))));
  const pct = h('div', { class: 'live-pct' });
  const m = meter(job.progress, { size: 'lg', idle: job.status === 'queued', label: 'Overall progress' });
  const msg = h('p', { class: 'live-msg', 'aria-live': 'polite' });
  const meta = h('p', { class: 'live-meta' });
  ctx.live = { stepper, pct, m, msg, meta };
  updateLive(job, ctx);
  const ticker = setInterval(() => { const j = store.jobs.get(job.id); if (j && isActive(j)) updateLive(j, ctx); }, 1000);
  ctx.onCleanup(() => clearInterval(ticker));

  const cancel = h('button', { type: 'button', class: 'btn btn-key' }, icon('close', 15), job.status === 'queued' ? 'Remove from queue' : 'Cancel');
  cancel.addEventListener('click', async () => {
    cancel.disabled = true;
    if (!await cancelJob(job, { quiet: true })) cancel.disabled = false;
  });
  return [
    block(null, stepper, h('div', { class: 'live-row' }, pct, m), msg, meta,
      h('div', { class: 'block-actions live-actions' }, cancel, h('span', { class: 'details-slot' }))),
    journalBlock(job, ctx),
  ];
}

export function updateLive(job, ctx) {
  const L = ctx.live;
  if (!L) return;
  const stages = job.stages || [];
  const idx = stages.findIndex((s) => s.key === job.stage);
  [...L.stepper.children].forEach((li, i) => {
    const state = job.status === 'queued' ? 'pending' : i < idx ? 'done' : i === idx ? 'current' : 'pending';
    if (li.dataset.state === state) return;
    li.dataset.state = state;
    li.firstChild.replaceWith(led(state === 'done' ? 'ok' : state === 'current' ? 'on' : 'off', { blink: state === 'current' }));
  });
  setMeter(L.m, job.progress, job.status === 'queued');
  if (job.status === 'queued') {
    L.pct.textContent = job.queue_position ? `#${job.queue_position}` : '…';
    L.msg.textContent = job.queue_position > 1
      ? `Waiting for ${job.queue_position - 1} job${job.queue_position > 2 ? 's' : ''} ahead of it.`
      : 'Starting soon…';
  } else {
    L.pct.textContent = `${Math.round(job.progress || 0)} %`;
    L.msg.textContent = job.message || '';
  }
  const bits = [];
  if (job.started_at && job.status === 'running') bits.push(`Running for ${fmtDuration(Date.now() / 1000 - job.started_at)}`);
  if (job.status === 'running' && job.updated_at) {
    const idle = Date.now() / 1000 - job.updated_at;
    if (idle > 60) bits.push(`no update for ${fmtDuration(idle)} — some steps are quiet, it is still working`);
  }
  L.meta.textContent = bits.join(' · ');
}

// ---------------------------------------------------------------------------
// Journal ("step-by-step log") — polled only while open and the job runs
// ---------------------------------------------------------------------------
export function journalBlock(job, ctx, { open = false } = {}) {
  const list = h('ol', { class: 'journal' });
  const det = h('details', { class: 'block journal-block disclosure', open },
    h('summary', null, isActive(job) ? 'Step-by-step log' : 'Full log'), list);
  let timer = 0;
  const load = async () => {
    try {
      const data = await api(`/api/jobs/${encodeURIComponent(job.id)}/journal`);
      const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
      renderJournal(list, data?.events || []);
      if (nearBottom) list.scrollTop = list.scrollHeight;
    } catch (e) {
      clear(list).append(h('li', { class: 'muted' }, e.message));
    }
  };
  det.addEventListener('toggle', () => {
    clearInterval(timer);
    if (!det.open) return;
    load();
    const j = store.jobs.get(job.id);
    if (j && isActive(j)) timer = setInterval(load, 3000);
  });
  if (open) load();
  ctx.onCleanup(() => clearInterval(timer));
  return det;
}

const KIND_WORDS = {
  info: 'Note', start: 'Start', stage: 'Step', decision: 'Decision', verdict: 'Result',
  warning: 'Warning', error: 'Error', interrupted: 'Stopped', done: 'Done',
};

function renderJournal(list, events) {
  clear(list);
  if (!events.length) { list.append(h('li', { class: 'muted' }, 'No entries yet.')); return; }
  for (const ev of events) {
    const kind = ev.kind || 'info';
    list.append(h('li', { class: `j-${kind}` },
      h('span', { class: 'j-time' }, clockTime(ev.ts)),
      h('span', { class: 'j-kind' }, KIND_WORDS[kind] || kind),
      h('span', { class: 'j-msg' }, ev.message || ''),
      showDetails() && ev.detail ? h('pre', { class: 'j-detail' }, JSON.stringify(ev.detail, null, 2)) : null));
  }
}

// ---------------------------------------------------------------------------
// Failed / cancelled
// ---------------------------------------------------------------------------
export function failedView(job, ctx) {
  const err = job.error || { message: job.message || 'Something went wrong.' };
  const actions = h('div', { class: 'callout-actions' });
  if (err.fix === 'update_downloader' && store.health?.downloader?.update_supported !== false) actions.append(updateDownloaderButton());
  if (err.fix === 'install_ai' && store.health?.ai?.install_supported !== false) actions.append(installAiButton());
  if (err.fix === 'use_cookies') { const b = signInButton(); if (b) actions.append(b); }
  // No fix (`fix: null`): trying the same again fails the same way, so changing something comes first.
  if (!err.fix) actions.append(changeButton(job, true));
  actions.append(retryButton(job, err.fix === 'retry'));
  if (err.fix) actions.append(changeButton(job, false));
  actions.append(h('button', {
    type: 'button', class: 'btn btn-ghost',
    onclick: () => copyDiagnostics(job.id),
  }, icon('clipboard', 15), 'Copy details for a bug report'));
  const hints = (err.hints || []).length
    ? h('div', { class: 'hints' }, h('div', { class: 'hints-title' }, 'What you can try'),
      h('ul', null, err.hints.map((t) => h('li', null, t))))
    : null;
  // The tool's own words behind the message, for the curious ("Show details").
  const detail = showDetails() && err.detail ? h('pre', { class: 'install-log' }, err.detail) : null;
  return [
    callout('danger', 'This didn’t work', err.message, [hints, detail, actions]),
    footer(job),
    journalBlock(job, ctx),
  ];
}

const GENERIC_CANCEL = /^cancel/i; // "Cancelled." / "Cancelled before it started.": the title says that already

export function cancelledView(job, ctx) {
  const said = job.message && !GENERIC_CANCEL.test(job.message) ? job.message : null;
  return [
    callout('info', 'Cancelled',
      said || 'This job was stopped before it finished. Files that were already finished stay in your output folder.',
      h('div', { class: 'callout-actions' }, retryButton(job, true), changeButton(job, false))),
    footer(job),
    journalBlock(job, ctx),
  ];
}

function retryButton(job, primary) {
  const b = h('button', { type: 'button', class: ['btn', primary ? 'btn-primary' : 'btn-key'] }, icon('retry', 15), 'Try again');
  b.addEventListener('click', async () => {
    b.disabled = true;
    if (!await retryJob(job, { quiet: true })) b.disabled = false;
  });
  return b;
}

/** Back to New with this job's source, task and options (tracklist, name…), to fix what went wrong. */
function changeButton(job, primary) {
  return h('button', {
    type: 'button', class: ['btn', primary ? 'btn-primary' : 'btn-key'],
    onclick: () => emit('use-source', { job, task: job.task, again: true }),
  }, icon('pencil', 15), 'Change and start again');
}

function updateDownloaderButton() {
  const b = h('button', { type: 'button', class: 'btn btn-primary' }, icon('download', 15), 'Update the downloader');
  b.addEventListener('click', async () => {
    b.disabled = true;
    b.textContent = 'Updating… (up to a minute)';
    try {
      const res = await api('/api/system/update-downloader', { method: 'POST' });
      b.textContent = res?.ok ? 'Updated' : 'Update failed';
      toast(res?.message || (res?.ok ? `Downloader updated${res.after ? ` to ${res.after}` : ''}. Try again now.` : 'The update did not work.'),
        { kind: res?.ok ? 'ok' : 'danger', timeout: 7000 });
      if (res?.ok) store.health && (store.health.downloader = { ...(store.health.downloader || {}), version: res.after, ok: true });
    } catch (e) {
      b.disabled = false;
      b.textContent = 'Update the downloader';
      toastError(e);
    }
  });
  return b;
}

function installAiButton() {
  const b = h('button', { type: 'button', class: 'btn btn-primary' }, icon('cpu', 15), 'Set up the AI add-on');
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      await api('/api/system/install-ai', { method: 'POST' });
      toast('Installing the AI add-on. This takes a few minutes — progress is shown in Settings.',
        { kind: 'ok', timeout: 8000, action: { label: 'Open Settings', run: () => go(settingsPath('ai'), { focus: false }) } });
    } catch (e) {
      b.disabled = false;
      toastError(e);
    }
  });
  return b;
}

/** When it started, Remove from list (then back to where the user came from), and the
 * "Show details" spot for pages without one of their own. */
export function footer(job) {
  const row = h('div', { class: 'job-footer' });
  if (isFinished(job)) {
    row.append(h('span', { class: 'muted small' }, `Started ${fmtAgo(job.created_at)}`));
    row.append(h('button', {
      type: 'button', class: 'btn btn-ghost btn-quiet btn-sm',
      onclick: async () => {
        const here = routeStamp();
        // Leave this page only if the user is still on it (not after moving on meanwhile).
        if (await removeJob(job) && routeStamp() === here) back();
      },
    }, icon('trash', 15), 'Remove from list'));
  }
  row.append(h('span', { class: 'details-slot' }));
  return row;
}
