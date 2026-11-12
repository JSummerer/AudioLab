// Section EQ (`advanced.section_eq`, docs/API.md): EQ bands for parts of a recording, each part
// From–To in the recording's own time, faded in and out at its edges. Shared by Enhance →
// Fine-tuning and the Library's section preview, and checked here the way the server checks it.
import { h, icon, fmtDuration, fmtSigned, parseClock, clockText } from './dom.js';
import { eqField, parseEq, formatEq, eqSummary, sameEq, scaledUnits } from './eq.js';
import { toneObservation } from './tone-chart.js';

export const MAX_SECTIONS = 8;
export const MIN_SECTION_S = 10;
export const DEFAULT_FADE_S = 5;
const FADE = [0.5, 60];
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const toMs = (value) => scaledUnits(value, 1000);

export function parseFade(raw) {
  const text = String(raw ?? '').trim().replace(',', '.');
  return text === '' ? DEFAULT_FADE_S : /^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text) ? Number(text) : NaN;
}

/** Saved sections → editable rows `{ from, to, fade, text }` (a default fade stays blank). */
export function sectionRows(sections) {
  return (Array.isArray(sections) ? sections : []).map((s) => ({
    from: clockText(s.start_s), to: clockText(s.end_s),
    fade: s.fade_s == null || Number(s.fade_s) === DEFAULT_FADE_S ? '' : String(s.fade_s),
    text: formatEq(s.bands),
  }));
}

/**
 * Rows → `{ sections, errors: [{ index, message }] }`, as the server will judge them: readable times,
 * at least 10 s and at least two fades, a fade of 0.5–60 s (blank = 5), inside the recording when
 * its `duration` is known, EQ text that reads, no overlaps, at most 8. `sections` (sorted) only
 * when there are no errors.
 */
export function checkSections(rows, duration = null) {
  const errors = [];
  const ok = [];
  rows.forEach((row, index) => {
    const fail = (message) => { errors.push({ index, message }); };
    if (index >= MAX_SECTIONS) return fail(`Up to ${MAX_SECTIONS} sections.`);
    const start = parseClock(row.from);
    const end = parseClock(row.to);
    const fade = parseFade(row.fade);
    const eq = parseEq(row.text);
    if (!(start >= 0 && start < 1e9)) return fail('Enter From like 40:00 or 1:02:03.');
    if (!(end >= 0 && end < 1e9)) return fail('Enter To like 45:00 or 1:02:03.');
    if (!(fade >= FADE[0] && fade <= FADE[1])) return fail('Fade must be 0.5 to 60 seconds.');
    const startMs = toMs(start);
    const endMs = toMs(end);
    const fadeMs = scaledUnits(fade, 100) * 10;
    if (endMs <= startMs) return fail('To must be after From.');
    if (endMs - startMs < toMs(MIN_SECTION_S)) return fail(`A section needs at least ${MIN_SECTION_S} seconds.`);
    if (endMs - startMs < 2 * fadeMs) return fail(`Allow at least ${2 * fadeMs / 1000} seconds for both fades.`);
    if (Number(duration) > 0 && endMs > toMs(duration)) return fail(`The recording ends at ${clockText(duration)}.`);
    if (eq.errors.length) return fail('Fix its EQ first. The problems are listed under it.');
    if (!eq.bands.length) return fail('Add EQ bands, or remove this section.');
    ok.push({ index, section: { start_s: startMs / 1000, end_s: endMs / 1000, fade_s: fadeMs / 1000, bands: eq.bands } });
  });
  ok.sort((a, b) => a.section.start_s - b.section.start_s);
  for (let i = 1; i < ok.length; i++) {
    if (ok[i].section.start_s < ok[i - 1].section.end_s) {
      errors.push({ index: ok[i].index, message: `Overlaps the section from ${clockText(ok[i - 1].section.start_s)}.` });
    }
  }
  errors.sort((a, b) => a.index - b.index);
  return { sections: errors.length ? [] : ok.map((x) => x.section), errors };
}

/** The same sections as far as anyone can see (times to the millisecond, fades, bands). */
export function sameSections(a, b) {
  const list = (x) => (Array.isArray(x) ? x : []);
  if (list(a).length !== list(b).length) return false;
  const sorted = (x) => [...list(x)].sort((p, q) => p.start_s - q.start_s);
  const [sa, sb] = [sorted(a), sorted(b)];
  const fadeUnits = (s) => scaledUnits(s.fade_s ?? DEFAULT_FADE_S, 100);
  return sa.every((s, i) => toMs(s.start_s) === toMs(sb[i].start_s) && toMs(s.end_s) === toMs(sb[i].end_s)
    && fadeUnits(s) === fadeUnits(sb[i]) && sameEq(s.bands, sb[i].bands));
}

/** Sections on one line, for settings lists: "40:00–45:00 (14 kHz −2.0 dB Q0.7); …". */
export function sectionSummary(sections) {
  return (Array.isArray(sections) ? sections : [])
    .map((s) => `${fmtDuration(s.start_s)}–${fmtDuration(s.end_s)} (${eqSummary(s.bands)})`).join('; ');
}

/** Full-strength interval between the section's two inside fades (can be a single point). */
export function sectionFullStrength(section) {
  const fade = section.fade_s ?? DEFAULT_FADE_S;
  if (!(fade >= FADE[0] && fade <= FADE[1]) || !(section.start_s >= 0 && section.end_s > section.start_s)) {
    return { start: NaN, end: NaN };
  }
  const startMs = toMs(section.start_s);
  const endMs = toMs(section.end_s);
  const fadeMs = scaledUnits(fade, 100) * 10;
  if (endMs - startMs < Math.max(toMs(MIN_SECTION_S), 2 * fadeMs)) return { start: NaN, end: NaN };
  return { start: (startMs + fadeMs) / 1000, end: (endMs - fadeMs) / 1000 };
}

/**
 * Where a dragged edge may go: at least 10 s and both fades (in the validator's hundredths of a second,
 * so a 5.005 s fade counts as 5.01) from the other edge, between the neighbours and the recording's
 * edges. Null when the section has no room for that edge.
 */
export function dragSectionEdge(bounds, others, duration, edge, at) {
  const fade = bounds.fade_s ?? DEFAULT_FADE_S;
  if (!(fade >= FADE[0] && fade <= FADE[1])) return null;
  const min = Math.max(MIN_SECTION_S, 2 * scaledUnits(fade, 100) / 100);
  const left = Math.max(0, ...others.map((b) => b.end_s).filter((end) => end <= bounds.start_s));
  const right = Math.min(duration, ...others.map((b) => b.start_s).filter((start) => start >= bounds.end_s));
  const [lo, hi] = edge === 'start' ? [left, bounds.end_s - min] : [bounds.start_s + min, right];
  if (!(lo <= hi) || !Number.isFinite(at)) return null;
  return Math.max(lo, Math.min(hi, at));
}

/** Place a new section with the smallest allowed fades and the least additional processing scope. */
export function sectionFromAudition({ start, end }, duration, sections = []) {
  const fail = (message) => ({ section: null, message });
  if (sections.length >= MAX_SECTIONS) return fail(`Up to ${MAX_SECTIONS} sections.`);
  if (![start, end, duration].every(Number.isFinite) || start < 0 || end <= start || end > duration) {
    return fail('Choose an audition within the recording first.');
  }
  const startMs = toMs(start);
  const endMs = Math.min(toMs(end), Math.floor(duration * 1000 + 1e-7));
  if (endMs <= startMs) return fail('Choose a longer audition first.');
  let left = 0;
  let right = Math.floor(duration * 1000 + 1e-7);
  for (const s of sections) {
    const from = toMs(s.start_s);
    const to = toMs(s.end_s);
    if (!(from >= 0 && to > from)) return fail('Finish entering the existing section bounds first.');
    if (to <= startMs) left = Math.max(left, to);
    else if (from >= endMs) right = Math.min(right, from);
    else return fail('This audition overlaps an existing section. Select that section to edit it.');
  }
  const minimumMs = toMs(MIN_SECTION_S);
  if (right - left < minimumMs) return fail(`There are fewer than ${MIN_SECTION_S} free seconds around this audition.`);
  const fadeMs = toMs(FADE[0]);
  const paddedStart = Math.max(left, startMs - fadeMs);
  const paddedEnd = Math.min(right, endMs + fadeMs);
  const width = Math.max(minimumMs, paddedEnd - paddedStart);
  const extra = width - (paddedEnd - paddedStart);
  const from = Math.min(right - width, Math.max(left, paddedStart - Math.floor(extra / 2)));
  const to = from + width;
  const section = { start_s: from / 1000, end_s: to / 1000, fade_s: FADE[0], bands: [] };
  const full = sectionFullStrength(section);
  const scope = `Section ${clockText(section.start_s)}–${clockText(section.end_s)} with ${FADE[0]} s fades inside its edges.`;
  const extent = `Full strength ${clockText(full.start)}–${clockText(full.end)}.`;
  const limits = from + fadeMs > startMs || to - fadeMs < endMs ? ' Part of the audition lies in a fade.' : '';
  return { section, message: `${scope} ${extent}${limits}` };
}

let uid = 0;

/**
 * The editor: per section From, To, Fade, its EQ text (with the presets) and Remove; then Add section.
 * `rows` is edited in place. `onInput(rows, result)` after every edit (`result`: checkSections).
 * `range()` → { start, end } in seconds adds "Use preview range" to each section (the preview dialog).
 * `timeline()` → the recording's tone timeline or null adds observations while readings are available.
 * Returns { el, read() → checkSections result, sync() (the timeline arrived) }.
 */
export function sectionEqField({ rows, duration = null, onInput, range = null, timeline = null }) {
  const base = `sec-${++uid}`;
  const list = h('div', { class: 'sec-list' });
  const addBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: add }, icon('plus', 14), 'Add section');
  const note = h('p', { class: 'small muted sec-note', 'aria-live': 'polite' });
  let errorEls = [];
  let toneBtns = [];

  function add() {
    if (rows.length >= MAX_SECTIONS) return;
    rows.push({ from: '', to: '', fade: '', text: '' });
    render();
    update(true);
    list.lastElementChild?.querySelector('input')?.focus();
  }

  function remove(i) {
    rows.splice(i, 1);
    render();
    update(true);
    addBtn.focus();
  }

  function timeInput(row, key, placeholder, clearObservation, cls = null) {
    const input = h('input', { class: ['field', 'field-sm', cls], type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder });
    input.value = row[key];
    input.addEventListener('input', () => { row[key] = input.value; clearObservation(); update(true); });
    return input;
  }

  function rowEl(row, i) {
    const toneNote = timeline ? h('p', { class: 'small muted sec-suggest', 'aria-live': 'polite' }) : null;
    const clearObservation = () => { if (toneNote) toneNote.textContent = ''; };
    const from = timeInput(row, 'from', '40:00', clearObservation);
    const to = timeInput(row, 'to', '45:00', clearObservation);
    const fade = timeInput(row, 'fade', String(DEFAULT_FADE_S), clearObservation, 'sec-fade');
    const error = h('p', { class: 'sec-error small', id: `${base}-${i}-error` });
    errorEls[i] = error;
    for (const input of [from, to, fade]) input.setAttribute('aria-describedby', error.id);
    const useRange = range ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm',
      onclick: () => {
        const r = range();
        if (!r) return;
        row.from = from.value = clockText(r.start);
        row.to = to.value = clockText(r.end);
        clearObservation();
        update(true);
      },
    }, 'Use preview range') : null;
    const eq = eqField({ text: row.text, label: `EQ for section ${i + 1}`, onInput: ({ text }) => { row.text = text; update(true); } });
    const observation = timeline ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', hidden: !timeline(),
      onclick: () => {
        const result = toneObservation(timeline(), parseClock(row.from), parseClock(row.to), duration);
        const readings = result.differences.map(({ label, db }) => `${label} ${fmtSigned(db, 1)} dB`).join(' · ');
        toneNote.textContent = [result.basis, readings, result.message].filter(Boolean).join(' ');
      },
    }, 'Read tone chart') : null;
    if (observation) toneBtns.push(observation);
    const labelled = (text, input, unit = null) => h('label', { class: 'sec-time small' }, h('span', null, text), input, unit);
    return h('fieldset', { class: 'sec-row' },
      h('legend', { class: 'sec-legend' }, `Section ${i + 1}`),
      h('div', { class: 'sec-times' },
        labelled('From', from), labelled('To', to), labelled('Fade', fade, 's'), useRange, observation,
        h('button', {
          type: 'button', class: 'btn btn-ghost btn-quiet btn-sm', 'aria-label': `Remove section ${i + 1}`, onclick: () => remove(i),
        }, icon('trash', 14), 'Remove')),
      error, toneNote, eq.el);
  }

  function render() {
    errorEls = [];
    toneBtns = [];
    while (list.firstChild) list.removeChild(list.firstChild);
    rows.forEach((row, i) => list.append(rowEl(row, i)));
    addBtn.disabled = rows.length >= MAX_SECTIONS;
  }

  function update(fire) {
    const result = checkSections(rows, duration);
    errorEls.forEach((el, i) => { el.textContent = result.errors.find((e) => e.index === i)?.message || ''; });
    note.textContent = !rows.length ? '' : result.errors.length ? plural(result.errors.length, 'problem')
      : plural(result.sections.length, 'section');
    addBtn.disabled = rows.length >= MAX_SECTIONS;
    if (fire) onInput?.(rows, result);
    return result;
  }

  render();
  update(false);
  return {
    el: h('div', { class: 'sec-edit' }, list, h('div', { class: 'sec-foot' }, addBtn, note)),
    read: () => checkSections(rows, duration),
    sync: () => { for (const b of toneBtns) b.hidden = !timeline?.(); },
  };
}
