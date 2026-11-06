// A disposable render of one passage from a kept original. A full render is a separate,
// explicit action using the same overrides; closing or editing this dialog never saves audio.
import { h, clear, fmtDuration } from './dom.js';
import { api } from './api.js';
import { closeInfo, segmented, switchControl, toast } from './ui.js';
import { sectionPreviewCompare } from './compare.js';
import { upsertJob } from './store.js';
import { go, jobPath, routeStamp } from './router.js';

const MIN_S = 5;
const MAX_S = 30;
const DEFAULT_S = 15;
const POLL_MS = 600;
const CHOICES = [
  { value: 'auto', label: 'Auto' }, { value: 'off', label: 'Off' }, { value: 'strong', label: 'Strong' },
];
let serial = 0;

export function previewUnavailable(entry) {
  if (!entry.original?.kept) return 'Keep the original to preview new settings.';
  const duration = entry.duration_s || entry.source?.duration_s;
  if (Number.isFinite(duration) && duration > 0 && duration < 1) return 'This original is too short for a section preview.';
  const p = entry.processing;
  if (p?.requested?.quality === 'best') return 'Section previews aren’t available with AI stems.';
  if (!p?.resolved?.params || !p.requested?.formats?.length || !['light', 'standard'].includes(p.requested?.quality)) {
    return 'This version predates saved settings for section previews.';
  }
  return '';
}

export function openQuickPreview(entry, comparison, opener) {
  closeInfo();
  const position = comparison?.snapshot?.();
  comparison?.pause();
  const route = routeStamp();
  const id = `quick-preview-${++serial}`;
  const path = `/api/library/${encodeURIComponent(entry.id)}`;
  const duration = entry.duration_s || entry.source?.duration_s;
  const repeat = position?.repeat?.enabled ? position.repeat : null;
  const length = repeat ? Math.max(MIN_S, Math.min(MAX_S, repeat.end - repeat.start)) : DEFAULT_S;
  const from = repeat?.start ?? position?.time ?? 0;
  // Keep a usable section if the playhead is at or very close to the end.
  const start = Number.isFinite(duration) && from > duration - 1 ? Math.max(0, duration - length) : from;
  const saved = entry.processing.requested.advanced || {};
  const light = entry.processing.requested.quality === 'light';
  const modes = { adaptive_lowmid: saved.adaptive_lowmid || 'auto', deess: saved.deess || 'auto' };
  let harmonics = saved.harmonics === 'on';
  let closed = false;
  let applying = false;
  let run = null;        // At most one preview submission, job or poll at a time.
  let ready = null;      // Exact settings of the last successful, still-current preview.
  let player = null;

  const startField = h('input', {
    class: 'field', type: 'number', min: 0, step: 'any', required: true, value: Math.floor(start * 1000) / 1000,
  });
  const lengthField = h('input', {
    class: 'field', type: 'number', min: MIN_S, max: MAX_S, step: 'any', required: true, value: Number(length.toFixed(3)),
  });
  const levelKey = light ? 'target_lufs' : 'bass_db';
  const levelUnit = light ? 'LUFS' : 'dB';
  const levelField = h('input', {
    class: 'field', type: 'number', min: light ? -30 : 0, max: light ? -5 : 6, step: 'any',
    placeholder: Number.isFinite(saved[levelKey]) ? `Saved: ${saved[levelKey]}` : 'Saved: Auto',
    'aria-describedby': `${id}-saved`,
  });
  const hint = h('p', { class: 'small muted' });
  const numeric = (label, input) => h('label', { class: 'quick-preview-number small' }, h('span', null, label), input);
  const mode = (key, label) => h('div', { class: 'quick-preview-choice small' }, h('span', null, label),
    segmented({ label, options: CHOICES, value: modes[key], size: 'sm',
      onChange: (v) => { modes[key] = v; change(); } }));
  const harmonicSwitch = switchControl({ checked: harmonics, label: 'Bass harmonics',
    onChange: (on) => { harmonics = on; change(); } });
  const fields = h('fieldset', { class: 'quick-preview-fields' },
    h('legend', { class: 'sr-only' }, 'Section and settings'),
    h('div', { class: 'quick-preview-range' },
      numeric('Start (seconds)', startField), numeric('Length (seconds)', lengthField)),
    hint,
    h('div', { class: 'quick-preview-settings' },
      !light ? mode('adaptive_lowmid', 'Low-mid control') : null,
      !light ? mode('deess', 'Sharp S sounds') : null,
      numeric(`${light ? 'Loudness target' : 'Bass boost'} (${levelUnit})`, levelField),
      !light ? h('label', { class: 'details-toggle', for: harmonicSwitch.id }, harmonicSwitch, 'Bass harmonics') : null),
    h('p', { class: 'small muted', id: `${id}-saved` }, 'A blank value keeps the saved setting.'));
  const status = h('p', { class: 'small muted', role: 'status' });
  const listen = h('div', { class: 'quick-preview-listen', hidden: true });
  const closeBtn = h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => dismiss(true) }, 'Close');
  const cancelBtn = h('button', { type: 'button', class: 'btn btn-ghost', hidden: true, onclick: stop }, 'Cancel preview');
  const makeBtn = h('button', { type: 'button', class: 'btn btn-primary', onclick: make }, 'Make preview');
  const applyBtn = h('button', { type: 'button', class: 'btn btn-primary', hidden: true, onclick: apply }, 'Apply to full recording');
  const dialog = h('dialog', { class: 'dialog dialog-wide quick-preview', 'aria-labelledby': id },
    h('div', { class: 'dialog-form' },
      h('h2', { id, tabindex: '-1', autofocus: true }, 'Preview changes'),
      h('p', { class: 'small muted' }, 'Try 5–30 seconds from the original. Other settings stay as saved.'),
      fields, status, listen,
      h('div', { class: 'dialog-actions' }, closeBtn, cancelBtn, makeBtn, applyBtn)));

  for (const input of [startField, lengthField, levelField]) input.addEventListener('input', change);
  dialog.addEventListener('cancel', (e) => { e.preventDefault(); if (!applying) dismiss(true); });
  dialog.addEventListener('close', () => dismiss(true));
  document.body.append(dialog);
  updateRange();
  dialog.showModal();
  return () => dismiss(false);

  function updateRange() {
    const start = Number(startField.value);
    const length = Number(lengthField.value);
    const end = Number.isFinite(duration) ? Math.min(duration, start + length) : start + length;
    hint.textContent = startField.value !== '' && lengthField.value !== '' && start >= 0 && length >= MIN_S && length <= MAX_S && end > start
      ? `Section ${fmtDuration(start)}–${fmtDuration(end)}${Number.isFinite(duration) ? ` of ${fmtDuration(duration)}` : ''}.`
      : 'Choose a start and length within this recording.';
  }

  function render() {
    fields.disabled = !!run || applying;
    makeBtn.setAttribute('aria-disabled', String(!!run || applying));
    makeBtn.classList.toggle('btn-key', !!ready);
    makeBtn.classList.toggle('btn-primary', !ready);
    applyBtn.hidden = !ready;
    applyBtn.setAttribute('aria-disabled', String(applying));
    closeBtn.disabled = applying;
    cancelBtn.hidden = !run;
    cancelBtn.setAttribute('aria-disabled', String(!!run?.cancelling));
    listen.hidden = !player;
  }

  function clearPreview() {
    ready = null;
    player?.destroy();
    player = null;
    clear(listen);
  }

  function change() {
    updateRange();
    if (ready) {
      clearPreview();
      status.textContent = 'Settings changed. Make a new preview to apply them.';
      render();
    }
  }

  function request() {
    if (![startField, lengthField, levelField].every((el) => el.reportValidity())) return null;
    const start_s = Number(startField.value);
    const duration_s = Number(lengthField.value);
    if (Number.isFinite(duration) && start_s + 1 > duration + 1e-6) {
      status.textContent = 'Leave at least one second before the end of the original.';
      startField.focus();
      return null;
    }
    const advanced = {};
    if (levelField.value !== '') advanced[levelKey] = Number(levelField.value);
    if (!light) {
      for (const [key, value] of Object.entries(modes)) {
        if (value !== (saved[key] || 'auto')) advanced[key] = value;
      }
      if (harmonics !== (saved.harmonics === 'on')) advanced.harmonics = harmonics ? 'on' : 'off';
    }
    return { start_s, duration_s, advanced };
  }

  async function make() {
    if (closed || run || applying) return;
    const payload = request();
    if (!payload) return;
    clearPreview();
    const work = { controller: new AbortController(), id: null, timer: 0, cancelling: false };
    run = work;
    status.textContent = 'Starting preview…';
    render();
    try {
      // Learn the job ID even if the dialog closes while the POST is in flight.
      const job = await api(`${path}/preview`, { method: 'POST', json: payload });
      if (!job?.id) throw new Error('The app didn’t return a preview job.');
      work.id = job.id;
      if (closed || work.cancelling) cancelJob(work);
      if (!closed) updateJob(work, job, payload.advanced);
    } catch (e) {
      if (!closed) finish(work, e.message);
    }
  }

  function poll(work, advanced) {
    work.timer = setTimeout(async () => {
      try {
        const job = await api(jobPathFor(work), { signal: work.controller.signal });
        if (!closed && run === work) updateJob(work, job, advanced);
      } catch (e) {
        if (closed || run !== work) return;
        if (!e.status) {
          status.textContent = 'Can’t check the preview yet. Trying again…';
          poll(work, advanced);
        } else {
          if (e.status !== 404) cancelJob(work);
          finish(work, e.message);
        }
      }
    }, POLL_MS);
  }

  function updateJob(work, job, advanced) {
    if (job?.id !== work.id) {
      cancelJob(work);
      finish(work, 'The app didn’t return a preview job.');
    } else if (job.status === 'queued' || job.status === 'running') {
      const pct = Number.isFinite(job.progress) ? ` · ${Math.round(job.progress)}%` : '';
      status.textContent = work.cancelling ? 'Cancelling preview…' : job.status === 'queued'
        ? 'Preview queued…' : `${job.message || 'Making preview…'}${pct}`;
      poll(work, advanced);
    } else if (job.status === 'cancelled' || work.cancelling) {
      finish(work, 'Preview cancelled.');
    } else if (job.status === 'done') {
      player = sectionPreviewCompare(job, duration);
      if (!player) { finish(work, 'No comparison was available for this preview.'); return; }
      listen.append(player.el);
      ready = { advanced };
      finish(work, 'Section estimate. The full recording is checked again when saved.');
    } else {
      const message = job.error?.message || 'The preview couldn’t be made.';
      finish(work, [message, job.error?.hints?.[0]].filter(Boolean).join(' '));
    }
  }

  function finish(work, message) {
    if (run !== work) return;
    const focusBack = document.activeElement === cancelBtn;
    run = null;
    status.textContent = message;
    render();
    if (focusBack) makeBtn.focus({ preventScroll: true });
  }

  function jobPathFor(work) { return `/api/jobs/${encodeURIComponent(work.id)}`; }

  function cancelJob(work) {
    if (!work.id || work.cancelSent) return;
    work.cancelSent = true;
    api(`${jobPathFor(work)}/cancel`, { method: 'POST' }).catch((e) => {
      // If the job finished first, polling will discard its result. A connection error
      // instead allows another cancellation attempt while the dialog is still open.
      if (closed || run !== work || e.status === 400) return;
      work.cancelSent = work.cancelling = false;
      status.textContent = e.message;
      render();
    });
  }

  function stop() {
    if (!run || run.cancelling) return;
    run.cancelling = true;
    status.textContent = 'Cancelling preview…';
    render();
    cancelJob(run);
  }

  async function apply() {
    if (closed || run || applying || !ready) return;
    const advanced = ready.advanced;
    applying = true;
    player?.pause();
    status.textContent = 'Starting the full recording…';
    render();
    try {
      const job = await api(`${path}/enhance`, { method: 'POST', json: { advanced } });
      if (!job?.id) throw new Error('The app didn’t return an Enhance job.');
      upsertJob(job);
      if (!closed && route === routeStamp()) {
        dismiss(false);
        go(jobPath(job.id));
      } else {
        toast('Full recording queued.', { kind: 'ok' });
      }
    } catch (e) {
      if (!closed) {
        applying = false;
        status.textContent = e.message;
        render();
      } else {
        toast(e.message, { kind: 'danger' });
      }
    }
  }

  function dismiss(refocus) {
    if (closed) return;
    closed = true;
    if (run) {
      clearTimeout(run.timer);
      run.controller.abort();
      cancelJob(run);
    }
    clearPreview();
    dialog.close();
    dialog.remove();
    if (refocus && opener?.isConnected) opener.focus({ preventScroll: true });
  }
}
