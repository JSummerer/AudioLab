// A disposable render of one passage from a kept original. A full render is a separate,
// explicit action using the same overrides; closing or editing this dialog never saves audio.
import { h, clear, fmtDuration, parseClock, clockText } from './dom.js';
import { api } from './api.js';
import { closeInfo, segmented, switchControl, toast } from './ui.js';
import { sectionPreviewCompare } from './compare.js';
import { upsertJob } from './store.js';
import { go, jobPath, routeStamp } from './router.js';
import { eqField, formatEq, sameEq } from './eq.js';
import { sectionEqField, sectionRows, sameSections } from './section-eq.js';
import { toneChart } from './tone-chart.js';

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
  const savedEq = Array.isArray(saved.eq) ? saved.eq : [];
  const savedSections = Array.isArray(saved.section_eq) ? saved.section_eq : [];
  // Compression only takes on/off: once a version saved one, "Auto" can't be asked for again.
  const glueChoices = [{ value: 'on', label: 'On' }, { value: 'off', label: 'Off' }];
  if (!saved.glue) glueChoices.unshift({ value: 'auto', label: 'Auto' });
  const modes = { adaptive_lowmid: saved.adaptive_lowmid || 'auto', deess: saved.deess || 'auto', glue: saved.glue || 'auto' };
  let harmonics = saved.harmonics === 'on';
  let closed = false;
  let applying = false;
  let run = null;        // At most one preview submission, job or poll at a time.
  let ready = null;      // Exact settings of the last successful, still-current preview.
  let player = null;
  let timeline = null;   // the tone timeline, fetched once the dialog is open (the list doesn't carry it)

  // Text fields, so a time can be typed as 14:00 or 1:02:03 as well as in seconds (checked in validateRange).
  const timeField = (value) => h('input', {
    class: 'field', type: 'text', required: true, autocomplete: 'off', spellcheck: 'false', value,
    'aria-describedby': `${id}-range`,
  });
  const startField = timeField(clockText(start));
  const lengthField = timeField(Number(length.toFixed(3)));
  const levelField = (key, min, max, step) => h('input', {
    class: 'field', type: 'number', min, max, step,
    placeholder: Number.isFinite(saved[key]) ? `Saved: ${saved[key]}` : 'Saved: Auto',
    'aria-describedby': `${id}-saved`,
  });
  // Volume only previews keep to the loudness target.
  const levels = [['target_lufs', 'Loudness (LUFS)', levelField('target_lufs', -30, -5, 0.5)]];
  if (!light) levels.push(['bass_db', 'Bass boost (dB)', levelField('bass_db', 0, 6, 'any')]);
  const hint = h('p', { class: 'small muted', id: `${id}-range` });
  const numeric = (label, input) => h('label', { class: 'quick-preview-number small' }, h('span', null, label), input);
  const mode = (key, label, options = CHOICES) => h('div', { class: 'quick-preview-choice small' }, h('span', null, label),
    segmented({ label, options, value: modes[key], size: 'sm',
      onChange: (v) => { modes[key] = v; change(); } }));
  const harmonicSwitch = switchControl({ checked: harmonics, label: 'Bass harmonics',
    onChange: (on) => { harmonics = on; change(); } });
  const eq = light ? null : eqField({ text: formatEq(savedEq), label: 'Custom EQ', onInput: change });
  // Sections in the recording's own time; "Use preview range" takes the Start and Length above.
  const sections = light ? null : sectionEqField({
    rows: sectionRows(savedSections), duration, onInput: change, timeline: () => timeline,
    range: () => {
      const [s, l] = validateRange();
      return Number.isFinite(s) && l > 0 ? { start: s, end: Number.isFinite(duration) ? Math.min(duration, s + l) : s + l } : null;
    },
  });
  // Where the balance moves (with the chapters); a point picked on it becomes the Start. No chart when it can't load.
  const tone = h('div', { hidden: true });
  if (entry.has_tone_timeline) {
    api(`${path}/tone-timeline`).then((tl) => {
      const chart = closed ? null : toneChart(tl, { compact: true, markers: entry.markers, onPick: pickStart });
      if (!chart) return;
      timeline = tl;
      tone.append(chart);
      tone.hidden = false;
      sections?.sync();
    }).catch(() => {});
  }
  const fields = h('fieldset', { class: 'quick-preview-fields' },
    h('legend', { class: 'sr-only' }, 'Section and settings'),
    h('div', { class: 'quick-preview-range' },
      numeric('Start', startField), numeric('Length (seconds)', lengthField)),
    hint,
    tone,
    h('div', { class: 'quick-preview-settings' },
      !light ? mode('adaptive_lowmid', 'Low-mid control') : null,
      !light ? mode('deess', 'Sharp S sounds') : null,
      !light ? mode('glue', 'Compression', glueChoices) : null,
      levels.map(([, label, input]) => numeric(label, input)),
      !light ? h('label', { class: 'details-toggle', for: harmonicSwitch.id }, harmonicSwitch, 'Bass harmonics') : null,
      eq ? h('div', { class: 'quick-preview-eq small' }, h('span', null, 'Custom EQ'), eq.el) : null,
      sections ? h('div', { class: 'quick-preview-eq small' }, h('span', null, 'Section EQ'), sections.el) : null),
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

  const inputs = [startField, lengthField, ...levels.map(([, , input]) => input)];
  for (const input of inputs) input.addEventListener('input', change);
  dialog.addEventListener('cancel', (e) => { e.preventDefault(); if (!applying) dismiss(true); });
  dialog.addEventListener('close', () => dismiss(true));
  document.body.append(dialog);
  updateRange();
  dialog.showModal();
  return () => dismiss(false);

  function pickStart(t) {
    if (run || applying) return;
    startField.value = clockText(t);
    change();
  }

  /** The typed start and length in seconds; marks a field invalid (reportValidity) when it can't be used. */
  function validateRange() {
    const start = parseClock(startField.value);
    const length = parseClock(lengthField.value);
    startField.setCustomValidity(Number.isFinite(start) ? '' : 'Enter a start like 14:00, 1:02:03 or in seconds.');
    lengthField.setCustomValidity(length >= MIN_S && length <= MAX_S ? '' : `Choose ${MIN_S}–${MAX_S} seconds.`);
    return [start, length];
  }

  function updateRange() {
    const [start, length] = validateRange();
    const end = Number.isFinite(duration) ? Math.min(duration, start + length) : start + length;
    hint.textContent = Number.isFinite(start) && length >= MIN_S && length <= MAX_S && end > start
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
    const [start_s, duration_s] = validateRange();
    if (!inputs.every((el) => el.reportValidity())) return null;
    if (Number.isFinite(duration) && start_s + 1 > duration + 1e-6) {
      status.textContent = 'Leave at least one second before the end of the original.';
      startField.focus();
      return null;
    }
    const custom = eq?.read();
    if (custom?.errors.length) {
      status.textContent = 'Fix the custom EQ first. The problems are listed under it.';
      eq.textarea.focus();
      return null;
    }
    const parts = sections?.read();
    if (parts?.errors.length) {
      status.textContent = 'Fix the section EQ first. The problems are listed under it.';
      return null;
    }
    const advanced = {};
    for (const [key, , input] of levels) if (input.value !== '') advanced[key] = Number(input.value);
    if (!light) {
      // Only what differs from the saved version; "Auto" for compression keeps it.
      for (const [key, value] of Object.entries(modes)) {
        if (value !== (saved[key] || 'auto')) advanced[key] = value;
      }
      if (harmonics !== (saved.harmonics === 'on')) advanced.harmonics = harmonics ? 'on' : 'off';
      // [] clears a saved EQ or saved sections.
      if (!sameEq(custom.bands, savedEq)) advanced.eq = custom.bands;
      if (!sameSections(parts.sections, savedSections)) advanced.section_eq = parts.sections;
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
      finish(work, job.result?.preview_basis === 'saved_export'
        ? job.result.summary?.[0] || 'Playing the saved version. No new processing was applied.'
        : 'Section estimate. The full recording is checked again when saved.');
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
