// Section EQ (`advanced.section_eq`, docs/API.md): EQ bands for parts of a recording, each part
// From–To in the recording's own time, faded in and out at its edges. Shared by Enhance →
// Fine-tuning and the Library's section preview, and checked here the way the server checks it.
// With a tone timeline at hand, a section can ask for a suggestion; the app never picks the parts itself.
import { h, icon, fmtDuration, parseClock, clockText } from './dom.js';
import { eqField, parseEq, formatEq, eqSummary, sameEq } from './eq.js';

export const MAX_SECTIONS = 8;
export const MIN_SECTION_S = 10;
export const DEFAULT_FADE_S = 5;
const FADE = [0.5, 60];
const SUGGEST_FROM_DB = 1;     // a band this far from the typical balance gets a suggestion
const SUGGEST_GAIN = [-2, 1];  // half the difference, kept to this
const NO_BOOST_HZ = 6000;      // highs and top end: never boosted (that adds hiss, not detail)
const SUGGEST_Q = 0.8;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

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
 * at least 10 s and longer than two fades, a fade of 0.5–60 s (blank = 5), inside the recording when
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
    const fadeText = String(row.fade ?? '').trim().replace(',', '.');
    const fade = fadeText === '' ? DEFAULT_FADE_S : Number(fadeText);
    const eq = parseEq(row.text);
    if (!Number.isFinite(start)) return fail('Enter From like 40:00 or 1:02:03.');
    if (!Number.isFinite(end)) return fail('Enter To like 45:00 or 1:02:03.');
    if (end <= start) return fail('To must be after From.');
    if (end - start < MIN_SECTION_S) return fail(`A section needs at least ${MIN_SECTION_S} seconds.`);
    if (!(fade >= FADE[0] && fade <= FADE[1])) return fail('Fade must be 0.5 to 60 seconds.');
    if (end - start <= 2 * fade) return fail(`Make it longer than two fades (${2 * fade} seconds).`);
    if (Number(duration) > 0 && end > duration + 0.5) return fail(`The recording ends at ${fmtDuration(duration)}.`);
    if (eq.errors.length) return fail('Fix its EQ first. The problems are listed under it.');
    if (!eq.bands.length) return fail('Add EQ bands, or remove this section.');
    ok.push({ index, section: { start_s: start, end_s: end, fade_s: fade, bands: eq.bands } });
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
  return sa.every((s, i) => clockText(s.start_s) === clockText(sb[i].start_s) && clockText(s.end_s) === clockText(sb[i].end_s)
    && Number(s.fade_s ?? DEFAULT_FADE_S) === Number(sb[i].fade_s ?? DEFAULT_FADE_S) && sameEq(s.bands, sb[i].bands));
}

/** Sections on one line, for settings lists: "40:00–45:00 (14 kHz −2.0 dB Q0.7); …". */
export function sectionSummary(sections) {
  return (Array.isArray(sections) ? sections : [])
    .map((s) => `${fmtDuration(s.start_s)}–${fmtDuration(s.end_s)} (${eqSummary(s.bands)})`).join('; ');
}

/**
 * "Suggest from the chart" for start…end seconds: per band, the part's mean difference from the
 * recording's typical balance (stretches weighted by how much of them lies inside; quiet ones left out).
 * A band at least 1 dB off gets a bell at its geometric centre with half the opposite gain (−2…+1 dB,
 * never a boost from 6 kHz up), Q 0.8. → { bands, differs }, or null when the chart has no reading there.
 */
export function suggestBands(tl, start, end) {
  const step = Number(tl?.step_s);
  const bands = tl?.bands || [];
  if (!(step > 0) || !bands.length || !(end > start)) return null;
  const sums = bands.map(() => 0);
  const weights = bands.map(() => 0);
  (tl.values || []).forEach((row, i) => {
    const inside = Math.min(end, (i + 1) * step) - Math.max(start, i * step);
    if (!row || inside <= 0) return;
    bands.forEach((_, j) => {
      if (Number.isFinite(row[j])) { sums[j] += row[j] * inside; weights[j] += inside; }
    });
  });
  if (!weights.some(Boolean)) return null;
  let differs = false;
  const out = [];
  bands.forEach((b, j) => {
    const mean = weights[j] ? sums[j] / weights[j] : 0;
    const [lo, hi] = (b.hz || []).map(Number);
    if (Math.abs(mean) < SUGGEST_FROM_DB || !(lo > 0 && hi > 0)) return;
    differs = true;
    const hz = Number(Math.sqrt(lo * hi).toPrecision(2)); // 49 Hz, 320 Hz, 3.5 kHz
    const top = hz >= NO_BOOST_HZ ? 0 : SUGGEST_GAIN[1];
    const db = Math.round(Math.min(top, Math.max(SUGGEST_GAIN[0], -mean / 2)) * 10) / 10;
    if (db) out.push({ hz, db, q: SUGGEST_Q });
  });
  return { bands: out, differs };
}

let uid = 0;

/**
 * The editor: per section From, To, Fade, its EQ text (with the presets) and Remove; then Add section.
 * `rows` is edited in place. `onInput(rows, result)` after every edit (`result`: checkSections).
 * `range()` → { start, end } in seconds adds "Use preview range" to each section (the preview dialog).
 * `timeline()` → the recording's tone timeline or null adds "Suggest from the chart" while it has one.
 * Returns { el, read() → checkSections result, sync() (the timeline arrived) }.
 */
export function sectionEqField({ rows, duration = null, onInput, range = null, timeline = null }) {
  const base = `sec-${++uid}`;
  const list = h('div', { class: 'sec-list' });
  const addBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: add }, icon('plus', 14), 'Add section');
  const note = h('p', { class: 'small muted sec-note', 'aria-live': 'polite' });
  let errorEls = [];
  let suggestBtns = [];

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

  function timeInput(row, key, placeholder, cls = null) {
    const input = h('input', { class: ['field', 'field-sm', cls], type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder });
    input.value = row[key];
    input.addEventListener('input', () => { row[key] = input.value; update(true); });
    return input;
  }

  function rowEl(row, i) {
    const from = timeInput(row, 'from', '40:00');
    const to = timeInput(row, 'to', '45:00');
    const fade = timeInput(row, 'fade', String(DEFAULT_FADE_S), 'sec-fade');
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
        update(true);
      },
    }, 'Use preview range') : null;
    const eq = eqField({ text: row.text, label: `EQ for section ${i + 1}`, onInput: ({ text }) => { row.text = text; update(true); } });
    const suggestNote = timeline ? h('p', { class: 'small muted sec-suggest', 'aria-live': 'polite' }) : null;
    const suggest = timeline ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', hidden: !timeline(),
      onclick: () => { suggestNote.textContent = suggestInto(row, eq); },
    }, 'Suggest from the chart') : null;
    if (suggest) suggestBtns.push(suggest);
    const labelled = (text, input, unit = null) => h('label', { class: 'sec-time small' }, h('span', null, text), input, unit);
    return h('fieldset', { class: 'sec-row' },
      h('legend', { class: 'sec-legend' }, `Section ${i + 1}`),
      h('div', { class: 'sec-times' },
        labelled('From', from), labelled('To', to), labelled('Fade', fade, 's'), useRange, suggest,
        h('button', {
          type: 'button', class: 'btn btn-ghost btn-quiet btn-sm', 'aria-label': `Remove section ${i + 1}`, onclick: () => remove(i),
        }, icon('trash', 14), 'Remove')),
      error, suggestNote, eq.el);
  }

  /** Fill the row's EQ from the chart (the user asked); returns what to say. */
  function suggestInto(row, eq) {
    const start = parseClock(row.from);
    const end = parseClock(row.to);
    if (!(end > start)) return 'Enter From and To first.';
    const s = suggestBands(timeline(), start, end);
    if (!s) return 'The chart has no reading for this part.';
    if (!s.bands.length) {
      return s.differs ? 'Only its highs are lower than usual, and boosting them isn’t suggested.'
        : 'This part is close to the recording’s typical balance.';
    }
    row.text = formatEq(s.bands);
    eq.set(row.text);
    return 'Suggested: half of this part’s difference from the recording’s typical balance.';
  }

  function render() {
    errorEls = [];
    suggestBtns = [];
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
    sync: () => { for (const b of suggestBtns) b.hidden = !timeline?.(); },
  };
}
