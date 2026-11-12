import { clear, fmtSigned, h, icon } from './dom.js';
import { canonicalEq, EQ_FIELDS, EQ_MAX_BANDS, eqField, formatEq, normalizeEqBand } from './eq.js';
import { EQ_SAMPLE_RATE, eqFrequency, eqX, responseDb, responseFilters, responseFrequencies } from './eq-response.js';

const SVG = 'http://www.w3.org/2000/svg';
const W = 1000;
const H = 240;
const TICKS = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];
const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
const yAt = (db, range) => (1 - db / range) / 2;
const pct = (x) => `${100 * x}%`;
const fields = Object.keys(EQ_FIELDS);
let uid = 0;

function svg(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}

const button = (name, onclick) => h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick }, name);
const sameOrder = (a, b) => a.length === b.length && a.every((band, i) => fields.every((key) => band[key] === b[i][key]));
const copyDraft = ({ bands, bypassed }) => ({ bands: bands.map((band) => ({ ...band })), bypassed });

/** Editor for one scope. `onInput(effectiveBands, { errors })` receives exactly what audition/Apply can send. */
export function graphicalEqField({ bands = [], savedBands = [], context = {}, label = 'Custom EQ', onInput, draftState } = {}) {
  const id = `graph-eq-${++uid}`;
  const saved = canonicalEq(savedBands);
  // Neutral rows and bypassed bands remain editable here; they are omitted from effective settings.
  let draft = draftState ? copyDraft(draftState) : { bands: canonicalEq(bands), bypassed: false };
  let errors = [];
  let errorSource = null;
  let lastSource = null;
  const history = draftState?.history?.map(copyDraft) || [];
  let rows = [];
  let drag = null;
  let dbRange = 9;
  const read = () => ({ bands: draft.bypassed ? [] : canonicalEq(draft.bands), errors: [...errors] });
  const announce = () => { const current = read(); onInput?.(current.bands, { errors: current.errors }); };

  const errorNote = h('p', { class: 'eq-editor-error small', role: 'status' });
  const note = h('p', { class: 'small muted eq-editor-note', id: `${id}-instructions` },
    `Full-strength response at ${EQ_SAMPLE_RATE / 1000} kHz. Fades and dynamics change the audition. `,
    'Drag bands or use the arrow keys to move them. Use the fields for exact values.');
  const text = eqField({ id: `${id}-text`, text: draftState?.text ?? formatEq(draft.bands), label: `${label} as text`, onInput: editText });
  text.textarea.addEventListener('blur', endSession);
  const textPanel = h('details', { class: 'eq-editor-text', open: draftState?.textOpen },
    h('summary', null, 'EQ text or APO filters'), text.el);
  const numbers = h('div', { class: 'eq-editor-bands' });
  const path = svg('path', { class: 'eq-plot-response' });
  const contextPath = svg('path', { class: 'eq-plot-context' });
  const plot = svg('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', focusable: 'false' });
  plot.append(...TICKS.map((hz) => gridLine(eqX(hz), 0, eqX(hz), 1)),
    ...[0, 0.25, 0.5, 0.75, 1].map((y) => gridLine(0, y, 1, y)), contextPath, path);
  const xLabels = h('div', { class: 'eq-plot-x', 'aria-hidden': 'true' }, TICKS.map((hz, i) => h('span', {
    class: [i === 0 && 'is-first', i === TICKS.length - 1 && 'is-last', i % 2 && 'is-minor'], style: { left: pct(eqX(hz)) },
  }, hz < 1000 ? `${hz}` : `${hz / 1000}k`)), h('span', { class: 'eq-plot-x-unit' }, 'Hz'));
  const yLabels = h('div', { class: 'eq-plot-y', 'aria-hidden': 'true' });
  const handles = h('div', { class: 'eq-plot-handles' });
  const plotArea = h('div', { class: 'eq-plot-area' }, plot, handles);
  const graph = h('div', { class: 'eq-plot' }, yLabels, plotArea, xLabels);
  const responseNote = h('span', { class: 'small' });
  const contextLabel = h('span', { class: 'eq-editor-context-label small', hidden: true });
  const showContext = h('input', { type: 'checkbox', onchange: draw });
  const contextChoice = h('label', { class: 'small eq-editor-choice' }, showContext, 'Show static context');
  const add = button('Add band', () => {
    if (draft.bands.length >= EQ_MAX_BANDS) return;
    change({ ...draft, bands: [...draft.bands, { hz: 1000, db: 0, q: 1 }] });
    rows.at(-1).inputs.db.focus();
  });
  const bypass = button('Bypass EQ', () => change({ ...draft, bypassed: !draft.bypassed }));
  const undo = button('Undo EQ edit', () => {
    if (errors.length) change(draft, { remember: false });
    else if (history.length) change(history.pop(), { remember: false });
  });
  const reset = button('Reset EQ to saved', () => change({ bands: saved, bypassed: false }));

  function gridLine(x1, y1, x2, y2) {
    return svg('line', { class: y1 === 0.5 ? 'eq-plot-zero' : 'eq-plot-grid', x1: x1 * W, x2: x2 * W, y1: y1 * H, y2: y2 * H });
  }

  function endSession() { lastSource = null; }

  function invalid(nextErrors, source) {
    errors = nextErrors;
    errorSource = source;
    updateControls();
    announce();
  }

  function change(next, { source = null, remember = true } = {}) {
    const edited = !sameOrder(next.bands, draft.bands) || next.bypassed !== draft.bypassed;
    if (edited && remember && (!source || source !== lastSource)) {
      history.push(draft);
      if (history.length > 32) history.shift();
    }
    lastSource = source;
    draft = next;
    errors = [];
    errorSource = null;
    if (source !== text.textarea) text.set(formatEq(draft.bands), { notify: false });
    renderRows(source);
    updateControls();
    draw();
    announce();
  }

  function editText({ bands: parsed, errors: problems }) {
    if (problems.length) invalid(problems, text.textarea);
    else change({ ...draft, bands: parsed }, { source: text.textarea });
  }

  function readNumbers() {
    const parsed = rows.map(({ inputs }) => normalizeEqBand(Object.fromEntries(fields.map((key) => [key,
      inputs[key].validity?.badInput || inputs[key].value.trim() === '' ? NaN : Number(inputs[key].value)]))));
    return { parsed, problems: parsed.flatMap((row, index) => row.error ? [{ index, key: row.key, message: row.error }] : []) };
  }

  function editNumber(source) {
    const { parsed, problems } = readNumbers();
    if (problems.length) invalid(problems, source);
    else change({ ...draft, bands: parsed.map((row) => row.band) }, { source });
  }

  function alterBand(index, changes, source = null) {
    const result = normalizeEqBand({ ...draft.bands[index], ...changes });
    if (!result.band || errors.length) return;
    if (fields.every((key) => result.band[key] === draft.bands[index][key])) return;
    const next = draft.bands.map((band, i) => i === index ? result.band : band);
    change({ ...draft, bands: next }, { source });
  }

  function makeRow(index) {
    const name = `Band ${index + 1}`;
    const inputs = Object.fromEntries(fields.map((key) => {
      const field = EQ_FIELDS[key];
      const input = h('input', {
        class: 'field field-sm', type: 'number', min: field.min, max: field.max, step: 1 / field.scale,
        'aria-label': `${name} ${field.label}`, 'aria-describedby': `${id}-validation`,
        oninput: () => editNumber(input),
        onblur: () => { endSession(); if (!errors.length) input.value = String(draft.bands[index]?.[key] ?? ''); },
      });
      return [key, input];
    }));
    const handle = h('button', {
      type: 'button', class: 'eq-plot-handle', 'aria-describedby': `${id}-instructions`,
      onclick: (event) => { if (event.detail === 0) inputs.hz.focus(); },
      onkeydown: (event) => moveByKey(event, index, handle),
      onblur: endSession,
      onpointerdown: (event) => beginDrag(event, index, handle),
      onpointermove: (event) => moveDrag(event, handle),
      onpointerup: (event) => endDrag(event, handle),
      onpointercancel: (event) => endDrag(event, handle),
      onlostpointercapture: (event) => endDrag(event, handle),
    }, index + 1);
    const remove = button('Remove', () => {
      change({ ...draft, bands: draft.bands.filter((_, i) => i !== index) });
      (rows[Math.min(index, rows.length - 1)]?.inputs.hz || add).focus();
    });
    remove.setAttribute('aria-label', `Remove ${name.toLowerCase()}`);
    const el = h('fieldset', { class: 'eq-editor-band' }, h('legend', { class: 'sr-only' }, name),
      h('span', { class: 'eq-editor-band-name small', 'aria-hidden': 'true' }, name),
      fields.map((key) => h('label', { class: 'small' }, EQ_FIELDS[key].label, inputs[key])), remove);
    return { el, handle, inputs, remove };
  }

  function renderRows(source) {
    if (rows.length !== draft.bands.length) {
      rows = draft.bands.map((_, i) => makeRow(i));
      clear(numbers);
      clear(handles);
      numbers.append(...rows.map((row) => row.el));
      handles.append(...rows.map((row) => row.handle));
    }
    rows.forEach(({ inputs }, i) => {
      for (const key of fields) if (inputs[key] !== source) inputs[key].value = String(draft.bands[i][key]);
    });
  }

  function updateControls() {
    const blocked = errors.length > 0;
    const textInvalid = blocked && errorSource === text.textarea;
    const numericInvalid = blocked && !textInvalid;
    rows.forEach(({ inputs, handle, remove }, index) => {
      handle.disabled = blocked;
      remove.disabled = blocked;
      for (const key of fields) {
        const problem = errors.find((e) => e.index === index && e.key === key);
        inputs[key].disabled = textInvalid;
        inputs[key].setAttribute('aria-invalid', String(Boolean(problem)));
      }
    });
    text.setDisabled(numericInvalid);
    add.disabled = blocked || draft.bands.length >= EQ_MAX_BANDS;
    bypass.disabled = blocked || !draft.bypassed && !draft.bands.some((band) => band.db);
    bypass.setAttribute('aria-pressed', String(draft.bypassed));
    undo.disabled = !blocked && history.length === 0;
    reset.disabled = !blocked && !draft.bypassed && sameOrder(draft.bands, saved);
    const first = errors[0];
    errorNote.textContent = numericInvalid ? `Band ${first.index + 1}. ${first.message}` : '';
  }

  function moveByKey(event, index, source) {
    const band = draft.bands[index];
    const step = event.shiftKey ? 5 : 1;
    const values = {
      ArrowLeft: { hz: eqFrequency(eqX(band.hz) - step / 192) },
      ArrowRight: { hz: eqFrequency(eqX(band.hz) + step / 192) },
      ArrowDown: { db: clamp(band.db - step / 10, EQ_FIELDS.db.min, EQ_FIELDS.db.max) },
      ArrowUp: { db: clamp(band.db + step / 10, EQ_FIELDS.db.min, EQ_FIELDS.db.max) },
    };
    if (!Object.hasOwn(values, event.key)) return;
    event.preventDefault();
    alterBand(index, values[event.key], source);
  }

  function beginDrag(event, index, handle) {
    if (event.button !== 0 || drag || errors.length) return;
    const { width, height } = plotArea.getBoundingClientRect();
    if (!width || !height) return;
    handle.focus({ preventScroll: true });
    endSession();
    drag = { id: event.pointerId, index, band: draft.bands[index], x: event.clientX, y: event.clientY, width, height, range: dbRange };
    handle.setPointerCapture(event.pointerId);
  }

  function moveDrag(event, handle) {
    if (drag?.id !== event.pointerId || !handle.hasPointerCapture(event.pointerId)) return;
    const dx = (event.clientX - drag.x) / drag.width;
    const dy = (event.clientY - drag.y) / drag.height;
    alterBand(drag.index, {
      hz: eqFrequency(eqX(drag.band.hz) + dx),
      db: clamp(drag.band.db - 2 * drag.range * dy, EQ_FIELDS.db.min, EQ_FIELDS.db.max),
    }, drag);
  }

  function endDrag(event, handle) {
    if (drag?.id !== event.pointerId) return;
    drag = null;
    endSession();
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
  }

  function draw() {
    const effective = read().bands;
    const other = canonicalEq(context.bands);
    const { user, fixed } = responseFilters(effective, { ...context, bands: other });
    contextChoice.hidden = fixed.length === 0;
    const showFixed = showContext.checked && fixed.length > 0;
    contextLabel.hidden = !showFixed;
    const contextNames = [fixed.length > other.length && 'saved static filters', other.length && 'global EQ'].filter(Boolean);
    contextLabel.textContent = `With ${contextNames.join(' and ')}`;
    contextPath.style.display = showFixed ? '' : 'none';
    const frequencies = responseFrequencies([...effective, ...(showFixed ? other : [])]);
    const own = frequencies.map((hz) => responseDb(user, hz));
    const combined = showFixed ? frequencies.map((hz, i) => own[i] + responseDb(fixed, hz)) : [];
    const peak = Math.max(...own.map(Math.abs), ...combined.map(Math.abs));
    dbRange = Math.max(9, Math.ceil((peak + 0.1) / 3) * 3);
    for (const [element, values] of [[path, own], [contextPath, combined]]) {
      element.setAttribute('d', values.map((value, i) => `${i ? 'L' : 'M'}${(W * eqX(frequencies[i])).toFixed(3)} ${(H * yAt(value, dbRange)).toFixed(3)}`).join(''));
    }
    clear(yLabels);
    yLabels.append(...[dbRange, 0, -dbRange].map((db) => h('span', { style: { top: pct(yAt(db, dbRange)) } }, `${fmtSigned(db, 0)} dB`)));
    const tenths = (value) => Math.round(value * 10) / 10 || 0; // a −0.04 dB skirt reads 0.0, not −0.0
    const min = fmtSigned(tenths(Math.min(...own)), 1);
    const max = fmtSigned(tenths(Math.max(...own)), 1);
    responseNote.textContent = draft.bypassed ? 'Your EQ · bypassed for audition and Apply'
      : `Your EQ · ${min === max ? min : `${min} to ${max}`} dB`;
    rows.forEach(({ handle }, i) => {
      const { hz, db, q } = draft.bands[i];
      handle.style.left = pct(eqX(hz));
      handle.style.top = pct(yAt(db, dbRange));
      handle.classList.toggle('is-inactive', draft.bypassed || db === 0);
      handle.setAttribute('aria-label', `Band ${i + 1}, ${hz} Hz, ${fmtSigned(db, 2)} dB, Q ${q}`);
    });
  }

  errorNote.id = `${id}-validation`;
  renderRows();
  if (draftState?.numbers) {
    rows.forEach(({ inputs }, i) => { for (const key of fields) inputs[key].value = draftState.numbers[i][key]; });
    errors = readNumbers().problems;
  } else {
    errors = text.read().errors;
    if (errors.length) errorSource = text.textarea;
  }
  updateControls();
  draw();
  return {
    el: h('div', { class: 'eq-editor', 'aria-label': label, role: 'group' },
      h('div', { class: 'eq-editor-toolbar' }, add, bypass, undo, reset),
      graph, h('div', { class: 'eq-editor-legend' }, responseNote, contextLabel, contextChoice), note,
      numbers, errorNote, textPanel),
    read,
    setContext(next) { context = next || {}; draw(); },
    snapshot() {
      const numbers = errors.length && errorSource !== text.textarea
        ? rows.map(({ inputs }) => Object.fromEntries(fields.map((key) => [key, inputs[key].value]))) : null;
      return { ...copyDraft(draft), history: history.map(copyDraft), text: text.textarea.value, textOpen: textPanel.open, numbers };
    },
  };
}
