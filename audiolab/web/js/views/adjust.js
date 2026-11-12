// An optional workspace for a saved Enhance version. Drafts live in this tab.
// Auditions are short-lived jobs. Only Apply queues a new full-length export.
import { h, icon, clear, clockText, fmtNum, parseClock } from '../dom.js';
import { api } from '../api.js';
import { segmented, switchControl, toast } from '../ui.js';
import { adjustPath, go, jobPath } from '../router.js';
import { upsertJob } from '../store.js';
import { formatEq, sameEq } from '../eq.js';
import { graphicalEqField } from '../eq-editor.js';
import {
  checkSections, dragSectionEdge, parseFade, sameSections, sectionRows, sectionFromAudition, sectionFullStrength,
  DEFAULT_FADE_S, MAX_SECTIONS,
} from '../section-eq.js';
import { toneChart, toneObservation } from '../tone-chart.js';
import { adjustmentOverview } from '../adjust-overview.js';
import { auditionRequests, pairedAudition } from '../adjust-audition.js';
import { PROCESSING_LABELS } from './new/state.js';

const MIN = 5;
const MAX = 30;
const DEFAULT = 15;
const MODES = [
  { value: 'auto', label: 'Auto' }, { value: 'off', label: 'Off' }, { value: 'strong', label: 'Strong' },
];
const GLUE = [
  { value: 'auto', label: 'Auto' }, { value: 'on', label: 'On' }, { value: 'off', label: 'Off' },
];
const numbers = [
  ['target_lufs', 'Loudness target (LUFS)', -30, -5],
  ['tp_ceiling_db', 'Peak ceiling (dBTP)', -3, -0.1],
  ['bass_db', 'Bass boost (dB)', 0, 6],
];
const drafts = new Map();
const seeds = new Map();
const durationOf = (entry) => entry.duration_s || entry.source?.duration_s;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const libraryPath = (id) => `#/library/${encodeURIComponent(id)}`;
const entryPath = (id) => `/api/library/${encodeURIComponent(id)}`;
const button = (label, onclick, style = 'key') => h('button', {
  type: 'button', class: `btn btn-${style} btn-sm`, onclick,
}, label);
const control = (label, input) => h('label', { class: 'adjust-control small' }, h('span', null, label), input);
const timeInput = (value, oninput) => h('input', {
  class: 'field field-sm', type: 'text', autocomplete: 'off', spellcheck: 'false', value, oninput,
});
let viewLoad = null;
let workspace = null;
let viewId = null;
let body = null;
let subtitle = null;

export function adjustUnavailable(entry) {
  if (entry.task !== 'enhance') return 'Adjust is available for saved Enhance versions.';
  if (!entry.original?.kept) return 'Adjust needs the kept original.';
  const duration = durationOf(entry);
  if (Number.isFinite(duration) && duration > 0 && duration < 1) return 'This original is shorter than one second.';
  const { requested, resolved } = entry.processing || {};
  if (requested?.quality === 'best') return 'Adjust is unavailable for versions made with AI stems.';
  if (!resolved?.params || !requested?.formats?.length || !['light', 'standard'].includes(requested?.quality)) {
    return 'This version predates the saved settings used by Adjust.';
  }
  const { params } = resolved;
  if (!requested.recipe || !resolved.recipe?.key || !Number.isFinite(params.target_lufs)
    || !Number.isFinite(params.tp_ceiling_db) || params.volume_only !== (requested.quality === 'light')
    || (params.ai_stage && params.ai_stage !== 'none')) {
    return 'This version is missing the processing details used by Adjust.';
  }
  return '';
}

export function adjustLink(entry, snapshot) {
  return h('a', {
    class: 'btn btn-key btn-sm', href: adjustPath(entry.id),
    onclick: (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      const state = snapshot?.();
      if (state) seeds.set(entry.id, state);
    },
  }, icon('pencil', 14), 'Adjust');
}

export const adjustView = {
  el: null, heading: null, fallback: '#/library', scrollPerArg: true,

  mount(el) {
    this.el = el;
    this.heading = h('h1', { class: 'page-title', id: 'adjustTitle', tabindex: '-1' }, 'Adjust');
    subtitle = h('p', { class: 'page-sub' });
    body = h('div');
    el.append(h('div', { class: 'page adjust-page' },
      h('header', { class: 'page-head' }, h('div', { class: 'page-head-text' }, this.heading, subtitle)), body));
  },

  async show(id) {
    if (viewId === id) return;
    this.hide();
    viewId = id;
    subtitle.textContent = '';
    clear(body).append(h('p', { class: 'small muted', role: 'status' }, 'Loading saved settings…'));
    const controller = new AbortController();
    viewLoad = controller;
    const showError = (message) => {
      clear(body).append(h('p', { role: 'alert' }, message), h('a', { href: libraryPath(id) }, 'Back to Library'));
    };
    try {
      const entry = await api(entryPath(id), { signal: controller.signal });
      if (viewLoad !== controller) return;
      if (entry?.id !== id) { showError('This version couldn’t be loaded.'); return; }
      const reason = adjustUnavailable(entry);
      if (reason) { showError(reason); return; }
      const mode = PROCESSING_LABELS[entry.processing.requested.quality];
      subtitle.textContent = [entry.title, entry.artist, mode].filter(Boolean).join(' · ');
      workspace = createWorkspace(entry, controller.signal);
      clear(body).append(workspace.el);
    } catch (e) {
      if (viewLoad === controller) showError(e.message);
    }
  },

  hide() {
    viewLoad?.abort();
    viewLoad = null;
    workspace?.dispose();
    workspace = null;
    viewId = null;
    clear(body);
  },
};

function draftFor(entry) {
  const seed = seeds.get(entry.id);
  seeds.delete(entry.id);
  const duration = durationOf(entry);
  const { requested, resolved } = entry.processing;
  const saved = requested.advanced || {};
  const savedBands = resolved.params.eq_bands || saved.eq || [];
  const savedSections = resolved.params.section_eq || saved.section_eq || [];
  const identity = JSON.stringify([entry.original.key, requested, resolved]);
  let draft = drafts.get(entry.id);
  if (draft?.identity === identity) return draft;
  const repeat = seed?.repeat?.enabled ? seed.repeat : null;
  const length = repeat ? clamp(repeat.end - repeat.start, MIN, MAX) : DEFAULT;
  const startAt = repeat?.start ?? seed?.time ?? entry.excerpt?.start_s ?? 0;
  const start = Number.isFinite(duration) && startAt > duration - 1 ? Math.max(0, duration - length) : startAt;
  const rows = sectionRows(savedSections);
  draft = {
    identity, from: clockText(start), length: String(length), scope: 'global', nextId: rows.length,
    bands: savedBands, values: {}, graphs: new Map(), applying: false,
    sections: rows.map((row, i) => ({
      id: String(i), ...row, bands: savedSections[i].bands, savedBands: savedSections[i].bands,
    })),
  };
  drafts.set(entry.id, draft);
  return draft;
}

function readBounds(section) {
  return {
    start_s: parseClock(section.from), end_s: parseClock(section.to),
    fade_s: parseFade(section.fade),
  };
}

function createWorkspace(entry, signal) {
  const draft = draftFor(entry);
  const { requested, resolved } = entry.processing;
  const { params } = resolved;
  const saved = requested.advanced || {};
  const savedBands = params.eq_bands || saved.eq || [];
  const savedSections = params.section_eq || saved.section_eq || [];
  const light = requested.quality === 'light';
  const missingBandwidth = !light && params.hf_restore && !entry.job_exists
    && (!resolved.bandwidth || (resolved.bandwidth.is_lossy_cutoff && !resolved.bandwidth.cutoff_hz));
  const duration = durationOf(entry);
  const path = entryPath(entry.id);
  let disposed = false;
  let busy = false;
  let audition = null;
  let tone = null;
  let readingTone = false;
  const graphs = new Map();
  const listen = h('div', { class: 'adjust-listen', hidden: true });
  const status = h('p', { class: 'small muted adjust-status', role: 'status' });
  const auditionIssue = h('p', { class: 'small adjust-errors adjust-error-text', hidden: true });
  const applyStatus = h('p', { class: 'small muted', role: 'status' });
  const auditionNote = h('p', { class: 'small muted' });
  const scopeNote = h('p', { class: 'small muted', role: 'status' });
  const sectionErrors = h('ul', { class: 'small adjust-errors', hidden: true });
  const settingsErrors = h('ul', { class: 'small adjust-errors', hidden: true });
  const timing = h('div', { class: 'adjust-section-edit', hidden: true });
  const toneResult = h('p', { class: 'small muted', role: 'status' });
  const tonePlot = h('div', { hidden: true });
  const graphPanel = h('div', { class: 'adjust-graph-panel' });
  const selector = h('select', { class: 'field field-sm', onchange: (e) => pickScope(e.target.value) });
  const startInput = timeInput(draft.from, () => { draft.from = startInput.value; changed(true); });
  const lengthInput = timeInput(draft.length, () => { draft.length = lengthInput.value; changed(true); });
  const renderBtn = button('Render audition', renderAudition, 'primary');
  const cancelBtn = button('Cancel audition', () => {
    clearAudition('Audition cancelled.');
    renderBtn.focus({ preventScroll: true });
  }, 'ghost');
  const applyBtn = button('Apply to full recording', apply);
  const addSectionBtn = button('Make section from audition', addSection, 'ghost');
  const toneBtn = button('Read historical tone chart', inspectTone);
  const keep = h('a', { class: 'btn btn-ghost btn-sm', href: libraryPath(entry.id) }, 'Keep existing');
  const exportLink = entry.compare_file_id ? h('a', { class: 'btn btn-key btn-sm', href: libraryPath(entry.id) },
    icon('compare', 14), 'Compare actual export') : null;

  const overview = duration > 0 ? adjustmentOverview(duration, {
    onPassage: dragPassage, onSection: dragSection, onPick: pickScope, sectionControls: !light,
  }) : null;
  if (overview && entry.has_overview) {
    api(`${path}/overview`, { signal }).then((data) => { if (!disposed) overview.overview(data); }).catch(() => {});
  }

  const markers = (entry.markers || []).filter((m) => Number.isFinite(m.start_s));
  const bookmarks = markers.length > 1 ? h('select', { class: 'field field-sm', onchange: (e) => {
    const n = e.target.value;
    if (n !== '') {
      setPassage(markers[Number(n)].start_s, readRange()?.length || DEFAULT);
      e.target.value = '';
    }
  } }, h('option', { value: '' }, 'Choose a marker'),
  markers.map((m, i) => h('option', { value: i }, `${clockText(m.start_s)} · ${m.title || 'Marker'}`))) : null;

  const settings = makeSettings();
  const renderLine = h('div', { class: 'adjust-row' }, renderBtn, cancelBtn);
  const actions = h('div', { class: 'adjust-row' }, applyBtn, keep);
  const passage = h('section', { class: 'adjust-block', 'aria-labelledby': 'adjustPassageTitle' },
    h('h2', { id: 'adjustPassageTitle' }, 'Passage'),
    overview?.el,
    h('div', { class: 'adjust-row' }, control('Start', startInput), control('Length (seconds)', lengthInput),
      bookmarks ? control('Jump to', bookmarks) : null),
    auditionNote,
    entry.has_tone_timeline ? h('details', { class: 'disclosure' }, h('summary', null, 'Historical tone readings'),
      toneBtn, tonePlot, toneResult) : null);
  const graph = light ? null : h('section', { class: 'adjust-block', 'aria-labelledby': 'adjustEqTitle' },
    h('h2', { id: 'adjustEqTitle' }, 'Equalizer'),
    h('div', { class: 'adjust-row' }, control('Edit', selector), addSectionBtn),
    timing, scopeNote, sectionErrors, graphPanel);
  const el = h('div', { class: 'adjust-workspace' },
    exportLink ? h('div', { class: 'adjust-row adjust-export-link' }, exportLink) : null,
    passage,
    graph,
    h('details', { class: 'disclosure adjust-block', open: light }, h('summary', null, light ? 'Loudness settings' : 'More settings'), settings),
    h('section', { class: 'adjust-block', 'aria-labelledby': 'adjustAuditionTitle' },
      h('h2', { id: 'adjustAuditionTitle' }, 'Audition'),
      renderLine, auditionIssue, status, listen),
    h('section', { class: 'adjust-block', 'aria-labelledby': 'adjustSaveTitle' },
      h('h2', { id: 'adjustSaveTitle' }, 'Save a version'),
      h('p', { class: 'small muted' }, 'Apply analyzes the full original again and verifies a new export. Automatic decisions may change. Earlier versions stay in the Library.'),
      actions, applyStatus));

  const requests = auditionRequests(path, { onState: (state) => {
    busy = state.busy;
    if (state.job) {
      audition = pairedAudition(state.job, entry);
      if (!audition) { clearAudition('The app couldn’t open the audition.'); return; }
      clear(listen).append(audition.el);
      listen.hidden = false;
      status.textContent = basisMessage(state.job.result, light);
    } else {
      status.textContent = state.message;
    }
    update();
  } });

  if (!light) {
    syncScopeOptions();
    pickScope(draft.scope);
  }
  update();

  return {
    el, update,
    dispose() {
      disposed = true;
      for (const [id, graph] of graphs) {
        draft.graphs.set(id, { snapshot: graph.snapshot(), invalid: !!graph.read().errors.length });
      }
      audition?.destroy();
      requests.dispose();
    },
  };

  function makeSettings() {
    const wrap = h('div', { class: 'adjust-settings' });
    for (const [key, label, min, max] of numbers) {
      if (light && key === 'bass_db') continue;
      const input = h('input', {
        class: 'field field-sm adjust-level', type: 'number', step: 'any', min, max,
        placeholder: Number.isFinite(params[key]) ? String(params[key]) : 'Saved value',
      });
      const value = draft.values[key];
      input.value = value === undefined ? (Number.isFinite(saved[key]) ? String(saved[key]) : '') : value ?? '';
      input.setAttribute('aria-invalid', String(draft.values[key] === null || !input.validity.valid));
      input.addEventListener('input', () => {
        draft.values[key] = input.validity.badInput ? null : input.value;
        input.setAttribute('aria-invalid', String(!input.validity.valid));
        changed();
      });
      wrap.append(control(label, input));
    }
    wrap.append(h('p', { class: 'small muted' }, 'Blank numbers use the saved values.'));
    if (!light) {
      for (const [key, label, choices] of [
        ['adaptive_lowmid', 'Low-mid control', MODES], ['deess', 'Sharp S sounds', MODES], ['glue', 'Compression', GLUE],
      ]) {
        const pick = segmented({ label, value: draft.values[key] ?? saved[key] ?? 'auto', options: choices, size: 'sm',
          onChange: (value) => { draft.values[key] = value; changed(); } });
        wrap.append(h('div', { class: 'adjust-row small' }, h('span', null, label), pick));
      }
      const sw = switchControl({ checked: (draft.values.harmonics ?? saved.harmonics) === 'on', label: 'Bass harmonics',
        onChange: (on) => { draft.values.harmonics = on ? 'on' : 'off'; changed(); } });
      wrap.append(h('label', { class: 'details-toggle', for: sw.id }, sw, 'Bass harmonics'));
      if (saved.glue && !resolved.auto_compressor) {
        wrap.append(h('p', { class: 'small muted' }, 'Auto compression on this version requires Apply to the full recording.'));
      }
      wrap.append(h('p', { class: 'small muted' }, 'The graph’s static context shows filters from the saved settings. Render an audition to hear changes to these controls.'));
    }
    wrap.append(settingsErrors);
    return wrap;
  }

  function readRange() {
    const start = parseClock(draft.from);
    const length = parseClock(draft.length);
    if (!Number.isFinite(start) || !(length >= MIN && length <= MAX)) return null;
    const end = duration > 0 ? Math.min(duration, start + length) : start + length;
    if (end - start < 1 - 1e-8) return null;
    return { start, end, length };
  }

  function setPassage(start, length) {
    const maxStart = duration > 0 ? Math.max(0, Math.floor((duration - 1) * 1000) / 1000) : Infinity;
    const nextFrom = clockText(clamp(start, 0, maxStart));
    const nextLength = String(Number(length.toFixed(3)));
    if (nextFrom === draft.from && nextLength === draft.length) return;
    startInput.value = draft.from = nextFrom;
    lengthInput.value = draft.length = nextLength;
    changed(true);
  }

  function dragPassage(edge, start, end, delta) {
    const min = Math.min(MIN, duration);
    if (edge === 'start') {
      const next = clamp(start + delta, Math.max(0, end - MAX), end - min);
      setPassage(next, Math.max(MIN, end - next));
    } else if (edge === 'end') {
      const next = clamp(end + delta, start + min, Math.min(duration, start + MAX));
      setPassage(start, Math.max(MIN, next - start));
    } else {
      setPassage(clamp(start + delta, 0, duration - (end - start)), readRange()?.length || DEFAULT);
    }
  }

  function dragSection(id, edge, at) {
    const section = draft.sections.find((part) => part.id === id);
    if (!section || !Number.isFinite(at)) return;
    const others = draft.sections.filter((part) => part !== section).map(readBounds);
    const next = dragSectionEdge(readBounds(section), others, duration, edge, at);
    const key = edge === 'start' ? 'from' : 'to';
    if (next == null || clockText(next) === section[key]) return;
    section[key] = clockText(next);
    const input = timing.querySelector(`[data-bound="${key}"]`);
    if (input) input.value = section[key];
    changed();
  }

  function syncScopeOptions() {
    clear(selector).append(h('option', { value: 'global' }, 'Global EQ'),
      draft.sections.map((part, index) => h('option', { value: part.id }, `Section ${index + 1}`)));
    selector.value = draft.scope;
  }

  function context(part) {
    return { params, bands: part ? draft.bands : [] };
  }

  function pickScope(id) {
    const part = draft.sections.find((item) => item.id === id);
    if (id !== 'global' && !part) id = 'global';
    draft.scope = selector.value = id;
    toneResult.textContent = '';
    if (!graphs.has(id)) {
      const instance = graphicalEqField({
        bands: part?.bands ?? draft.bands, savedBands: part?.savedBands ?? savedBands,
        draftState: draft.graphs.get(id)?.snapshot,
        context: context(part),
        label: part ? 'Section EQ' : 'Global EQ',
        onInput: (bands) => {
          if (part) part.bands = bands;
          else draft.bands = bands;
          changed();
        },
      });
      graphs.set(id, instance);
      graphPanel.append(instance.el);
    }
    for (const [key, instance] of graphs) instance.el.hidden = key !== id;
    graphs.get(id).setContext?.(context(part));
    timing.hidden = !part;
    clear(timing);
    if (part) {
      const parts = [
        ['from', 'Section start'], ['to', 'Section end'], ['fade', 'Fade at each edge (seconds)'],
      ].map(([key, label]) => {
        const input = timeInput(part[key], () => { part[key] = input.value; changed(); });
        input.dataset.bound = key;
        if (key === 'fade') input.placeholder = String(DEFAULT_FADE_S);
        return control(label, input);
      });
      timing.append(h('div', { class: 'adjust-row' }, parts,
        button('Remove section', () => {
          draft.sections.splice(draft.sections.indexOf(part), 1);
          graphs.get(id)?.el.remove();
          graphs.delete(id);
          draft.graphs.delete(id);
          syncScopeOptions();
          pickScope('global');
          selector.focus();
          changed();
        }, 'ghost')));
    }
    update();
  }

  function addSection() {
    const range = readRange();
    if (!range) return;
    const bounds = draft.sections.map(readBounds);
    if (bounds.some((part) => !Number.isFinite(part.start_s) || !(part.end_s > part.start_s))) {
      scopeNote.textContent = 'Fix the existing section bounds before adding a section.';
      return;
    }
    const { section, message } = sectionFromAudition(range, duration, bounds);
    if (!section) { scopeNote.textContent = message; return; }
    const part = { id: String(draft.nextId++), ...sectionRows([section])[0], bands: [], savedBands: [] };
    draft.sections.push(part);
    syncScopeOptions();
    pickScope(part.id);
    scopeNote.textContent = message;
    selector.focus();
  }

  function settingsSnapshot() {
    const advanced = {};
    const invalidSettings = [];
    const invalidEq = [];
    for (const [key, label, min, max] of numbers) {
      if (light && key === 'bass_db') continue;
      const raw = draft.values[key];
      if (raw === undefined || (typeof raw === 'string' && raw.trim() === '')) continue;
      const value = raw == null ? NaN : Number(raw);
      if (!(value >= min && value <= max)) invalidSettings.push(`${label} must be between ${min} and ${max}.`);
      else if (value !== saved[key]) advanced[key] = value;
    }
    if (!light) {
      for (const key of ['adaptive_lowmid', 'deess', 'glue', 'harmonics']) {
        const defaultMode = key === 'harmonics' ? 'off' : 'auto';
        const next = draft.values[key];
        if (next != null && next !== (saved[key] ?? defaultMode)) advanced[key] = key === 'glue' && next === 'auto' ? null : next;
      }
      if (!sameEq(draft.bands, savedBands)) advanced.eq = draft.bands;
      const enabled = draft.sections.filter((part) => part.bands.length);
      const result = checkSections(enabled.map((part) => ({ ...part, text: formatEq(part.bands) })), duration);
      for (const { index, message } of result.errors) {
        const number = draft.sections.indexOf(enabled[index]) + 1;
        invalidEq.push(`Section ${number}. ${message}`);
      }
      if (!sameSections(result.sections, savedSections)) advanced.section_eq = result.sections;
      for (const [i, id] of ['global', ...draft.sections.map((part) => part.id)].entries()) {
        const instance = graphs.get(id);
        const invalid = instance ? instance.read().errors.length : draft.graphs.get(id)?.invalid;
        if (invalid) invalidEq.push(i === 0 ? 'Fix the invalid values in Global EQ.' : `Fix the invalid EQ values in Section ${i}.`);
      }
    }
    return { advanced, errors: [...invalidSettings, ...invalidEq], invalidSettings, invalidEq };
  }

  function update() {
    if (disposed) return;
    const range = readRange();
    const { advanced, errors, invalidSettings, invalidEq } = settingsSnapshot();
    const fullOnly = advanced.glue === null && !resolved.auto_compressor;
    const section = draft.sections.find((part) => part.id === draft.scope);
    auditionNote.textContent = range ? `Audition ${clockText(range.start)}–${clockText(range.end)}${range.end < range.start + range.length ? ' · ends at the recording boundary' : ''}.`
      : 'Choose a start and a length from 5 to 30 seconds, with at least one second of audio.';
    if (section) {
      const bounds = readBounds(section);
      const full = sectionFullStrength(bounds);
      scopeNote.textContent = Number.isFinite(full.start) && Number.isFinite(full.end) && full.end >= full.start
        ? `Full strength ${clockText(full.start)}–${clockText(full.end)}. Fades are inside the section.${section.bands.length ? '' : ' Add a band to enable this section.'}`
        : 'Enter section bounds and fades.';
    } else {
      scopeNote.textContent = 'Global EQ applies throughout the recording.';
    }
    clear(sectionErrors).append(...invalidEq.map((message) => h('li', null, message)));
    sectionErrors.hidden = invalidEq.length === 0;
    clear(settingsErrors).append(...invalidSettings.map((message) => h('li', null, message)));
    settingsErrors.hidden = invalidSettings.length === 0;
    auditionIssue.textContent = missingBandwidth
      ? 'Audition needs the bandwidth measurements for this version’s high-frequency restoration. Apply analyzes the original again.'
      : fullOnly ? 'Auto compression requires Apply to the full recording for this version.' : '';
    auditionIssue.hidden = !(missingBandwidth || fullOnly);
    overview?.update(range, draft.sections.map((part) => ({ id: part.id, bounds: readBounds(part) })), draft.scope);
    addSectionBtn.disabled = !(range && duration > 0) || draft.sections.length >= MAX_SECTIONS;
    renderBtn.disabled = !range || !!errors.length || missingBandwidth || fullOnly || busy || draft.applying;
    renderBtn.textContent = audition ? 'Render again' : 'Render audition';
    cancelBtn.hidden = !busy;
    applyBtn.disabled = !!errors.length || !Object.keys(advanced).length || draft.applying;
  }

  function clearAudition(message = '') {
    audition?.destroy();
    audition = null;
    clear(listen);
    listen.hidden = true;
    requests.reset(message);
  }

  function changed(rangeChanged = false) {
    if (rangeChanged || draft.scope !== 'global') toneResult.textContent = '';
    if (audition || busy) clearAudition('Passage or settings changed. Render a new audition to hear them.');
    else update();
  }

  function renderAudition() {
    const range = readRange();
    const { advanced, errors } = settingsSnapshot();
    if (renderBtn.disabled || !range || errors.length) return;
    clearAudition();
    requests.start({ start_s: range.start, duration_s: range.length, advanced });
  }

  async function apply() {
    const { advanced, errors } = settingsSnapshot();
    if (draft.applying || errors.length || !Object.keys(advanced).length) return;
    draft.applying = true;
    clearAudition();
    applyStatus.textContent = 'Starting the full recording…';
    try {
      const job = await api(`${path}/enhance`, { method: 'POST', json: { advanced } });
      if (!job?.id) throw new Error('The app didn’t return an Enhance job.');
      upsertJob(job);
      if (!disposed) go(jobPath(job.id));
      else toast('Full recording queued.', { kind: 'ok' });
    } catch (e) {
      if (!disposed) applyStatus.textContent = [e.message, e.hints?.[0]].filter(Boolean).join(' ');
      else toast(e.message, { kind: 'danger' });
    } finally {
      draft.applying = false;
      workspace?.update();
    }
  }

  async function inspectTone() {
    if (readingTone) return;
    readingTone = true;
    toneBtn.disabled = true;
    try {
      tone ||= await api(`${path}/tone-timeline`, { signal });
      if (disposed) return;
      if (tonePlot.hidden) {
        const chart = toneChart(tone, { compact: true, markers, onPick: (seconds) => setPassage(seconds, readRange()?.length || DEFAULT) });
        if (chart) {
          tonePlot.append(chart);
          tonePlot.hidden = false;
        }
      }
      const part = draft.sections.find((section) => section.id === draft.scope);
      const bounds = part ? readBounds(part) : null;
      const range = bounds ? { start: bounds.start_s, end: bounds.end_s } : readRange();
      if (!(range?.end > range?.start)) { toneResult.textContent = 'Choose a valid range first.'; return; }
      const observation = toneObservation(tone, range.start, range.end, duration);
      toneResult.textContent = [`${part ? 'EQ section' : 'Audition'} ${clockText(range.start)}–${clockText(range.end)}.`,
        observation.basis, observation.message,
        ...observation.differences.map(({ label, db }) => `${label} ${db > 0 ? '+' : ''}${db.toFixed(1)} dB.`),
      ].filter(Boolean).join(' ');
    } catch (e) {
      if (!disposed) toneResult.textContent = e.message;
    } finally {
      readingTone = false;
      toneBtn.disabled = false;
    }
  }
}

export function basisMessage(result, light) {
  const omitted = result.calibration?.omitted_post_limiter_gain_db;
  const correction = Number.isFinite(omitted) && omitted < 0
    ? ` The saved version was turned down ${fmtNum(-omitted, 1)} dB after limiting to meet its peak ceiling. Both sides leave that out. The edited recording may need a different correction, so Apply can change the levels and the difference.`
    : '';
  if (light && result.comparison_basis === 'retained_gain') {
    return `Passage estimate using saved whole-recording levels and peak cap. Apply checks the full recording again.${correction}`;
  }
  switch (result.calibration?.gain) {
    case 'retained':
      return `Passage estimate using saved loudness gain. Dynamics and peaks can differ in the full recording.${correction}`;
    case 'retained_target_delta':
      return `Passage estimate using saved loudness gain plus the target change. Full-recording peaks can differ.${correction}`;
    case 'shared_retained_estimate':
      return `Passage estimate using shared saved gain. Edits can change gain and dynamics in the full recording.${correction}`;
    default:
      return 'Passage estimate using gain from the passage and its surrounding audio. Full-recording processing can differ.';
  }
}
