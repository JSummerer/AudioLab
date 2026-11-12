// The tone timeline (analysis `tone_timeline`, docs/API.md): how the tonal balance moves over a
// recording, one line per band in dB around the recording's typical balance (0). Plain SVG for
// the lines, HTML for the labels, the crosshair and its readout. Shared by the job page (Check,
// Enhance) and, small, by the Library's section preview, where picking a point sets the start.
// The source's chapters (`markers`) are thin lines, and the readout names the one under the crosshair.
import { h, fmtDuration, fmtSigned } from './dom.js';
import { chips } from './ui.js';

const SVG = 'http://www.w3.org/2000/svg';
const W = 1000;   // viewBox: the SVG stretches to its box, the lines keep their width (non-scaling stroke)
const H = 100;
const PAD = 4;    // viewBox units kept free above the top and below the bottom value
const TICK_STEPS = [60, 120, 300, 600, 900, 1200, 1800, 3600, 5400, 7200, 10800];
const MAX_MARKERS = 60; // chapter lines drawn at most (more are thinned evenly)
export const TONE_DEFAULT = ['deep_bass', 'mids', 'top_end'];
const TONE_BASIS = 'Sampled mid-signal band shares relative to the recording’s median balance';

/** Summarize whole audible bins within a selection. Relative shares alone cannot yield corrective EQ. */
export function toneObservation(tl, start, end, duration = null) {
  const step = Number(tl?.step_s);
  const bands = tl?.bands || [];
  const values = tl?.values || [];
  const basis = `${TONE_BASIS} in ${step} s bins.`;
  if (!(step > 0) || !bands.length || !values.length) {
    return { status: 'insufficient', differences: [], message: 'No tone-over-time readings are available.' };
  }
  if (!(Number.isFinite(start) && Number.isFinite(end) && end > start)) {
    return { status: 'insufficient', differences: [], basis, message: 'Choose the start and end first.' };
  }
  // Older timelines can end with a partial bin. Only a known file length or a later bin confirms its coverage.
  const confirmedEnd = Number.isFinite(duration) && duration > 0 ? duration : (values.length - 1) * step;
  const first = Math.max(0, Math.ceil(start / step));
  const stop = Math.min(values.length, Math.floor(Math.min(end, confirmedEnd) / step));
  const complete = values.slice(first, Math.max(first, stop));
  const audible = complete.filter((row) => bands.every((_, j) => Number.isFinite(row?.[j])));
  if (!audible.length) {
    const message = complete.length ? 'No complete audible tone reading within this range.'
      : `This range contains no complete ${step} s tone bin.`;
    return { status: 'insufficient', differences: [], basis, message };
  }
  const differences = bands.map((band, j) => ({
    label: band.label || band.key,
    db: audible.reduce((sum, row) => sum + row[j], 0) / audible.length,
  }));
  const coverage = `Mean of ${audible.length} of ${complete.length} complete ${step} s bins in this range with audible readings.`;
  return { status: 'no_candidate', differences, basis, message: `${coverage} No EQ proposed from relative shares.` };
}

const bandIndex = (tl, key) => (tl?.bands || []).findIndex((b) => b.key === key);
const color = (key) => `var(--tone-${String(key).replace(/_/g, '-')}, var(--ink-3))`;

/** { step, n, end }: values[i] is at i × step; the chart spans 0…end (the last point's time). */
export function toneTimes(tl) {
  const step = Number(tl?.step_s) > 0 ? Number(tl.step_s) : 60;
  const n = tl?.values?.length || 0;
  return { step, n, end: Math.max(step, (n - 1) * step) };
}

/** The y range in dB for the shown bands: symmetric around 0, at least ±3, in steps of 3 dB. */
export function toneRange(tl, keys) {
  const cols = keys.map((k) => bandIndex(tl, k)).filter((i) => i >= 0);
  let max = 0;
  for (const row of tl?.values || []) {
    if (!row) continue;
    for (const i of cols) if (Number.isFinite(row[i])) max = Math.max(max, Math.abs(row[i]));
  }
  return Math.max(3, Math.ceil(max / 3) * 3);
}

const xAt = (t, end) => Math.round((t / end) * W * 10) / 10;
const yAt = (v, range) => Math.round((H / 2 - (v / range) * (H / 2 - PAD)) * 10) / 10;

/** One band as SVG path data in viewBox units: a new stroke after every gap (null); a lone point is a dot. */
export function tonePath(tl, key, range) {
  const col = bandIndex(tl, key);
  const { step, end } = toneTimes(tl);
  if (col < 0) return '';
  const runs = [];
  let run = null;
  (tl.values || []).forEach((row, i) => {
    const v = row?.[col];
    if (!Number.isFinite(v)) { run = null; return; }
    if (!run) runs.push(run = []);
    run.push(`${xAt(i * step, end)} ${yAt(v, range)}`);
  });
  return runs.map((r) => (r.length === 1 ? `M${r[0]}h0.01` : `M${r.join('L')}`)).join('');
}

/** Time ticks for the x axis: a round step with at most six intervals. */
export function timeTicks(end) {
  const step = TICK_STEPS.find((s) => end / s <= 6) || Math.ceil(end / 6 / 3600) * 3600;
  const ticks = [];
  for (let t = 0; t <= end + 1e-6; t += step) ticks.push(t);
  return ticks;
}

/** An axis label: m:ss, or h:mm once the recording reaches an hour. */
export function axisTime(t, end) {
  if (end < 3600) return fmtDuration(t);
  const m = Math.round(t / 60);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`;
}

/** The chapter playing at `t` seconds: the one with the latest start at or before it (its `end_s` may be missing). */
export function chapterAt(markers, t) {
  let found = null;
  for (const m of Array.isArray(markers) ? markers : []) {
    if (Number(m?.start_s) <= t + 1e-6 && (!found || m.start_s >= found.start_s)) found = m;
  }
  return found;
}

/**
 * "12:00 · Intro · Deep bass −0.4 dB · Top end +3.1 dB" for point `i` (a band without a value there is left
 * out; the chapter only with `markers`).
 */
export function toneReadout(tl, i, keys, markers = null) {
  const { step } = toneTimes(tl);
  const row = tl?.values?.[i];
  const parts = keys.map((k) => {
    const col = bandIndex(tl, k);
    const v = row?.[col];
    return col >= 0 && Number.isFinite(v) ? `${tl.bands[col].label} ${fmtSigned(v, 1)} dB` : null;
  }).filter(Boolean);
  const chapter = chapterAt(markers, i * step)?.title || null;
  return [fmtDuration(i * step), chapter, ...(parts.length ? parts : ['no usable mid-signal reading'])].filter(Boolean).join(' · ');
}

/** The chapter starts to draw inside the chart (not at 0), every k-th once there are more than MAX_MARKERS. */
export function markerTimes(markers, end) {
  const starts = (Array.isArray(markers) ? markers : []).map((m) => Number(m?.start_s)).filter((t) => t > 0 && t <= end);
  const k = Math.ceil(starts.length / MAX_MARKERS);
  return starts.filter((_, i) => i % k === 0);
}

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/**
 * The chart, or null without a timeline. `compact`: the small version (the three first bands, a legend
 * instead of the band picker). `onPick(seconds)`: a click, Enter or Space on a point picks its time.
 * `markers`: the source's chapters ([{ title, start_s }]).
 */
export function toneChart(tl, { compact = false, onPick = null, markers = null } = {}) {
  if (!tl?.values?.length || !tl.bands?.length) return null;
  const { step, n, end } = toneTimes(tl);
  let shown = new Set(TONE_DEFAULT.filter((k) => bandIndex(tl, k) >= 0));
  if (!shown.size) shown = new Set([tl.bands[0].key]);
  let index = -1;     // the point under the crosshair (-1: none)
  let range = 3;

  const lines = svg('g');
  const plot = svg('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', focusable: 'false' });
  plot.append(svg('line', { class: 'tone-grid', x1: 0, x2: W, y1: PAD, y2: PAD }),
    svg('line', { class: 'tone-zero', x1: 0, x2: W, y1: H / 2, y2: H / 2 }),
    svg('line', { class: 'tone-grid', x1: 0, x2: W, y1: H - PAD, y2: H - PAD }),
    ...markerTimes(markers, end).map((t) => svg('line', { class: 'tone-marker', x1: xAt(t, end), x2: xAt(t, end), y1: 0, y2: H })),
    lines);
  const yLabels = h('div', { class: 'tone-y', 'aria-hidden': 'true' });
  const cursor = h('span', { class: 'tone-cursor', hidden: true });
  const dots = h('span', { class: 'tone-dots' });
  const area = h('div', {
    class: ['tone-area', onPick && 'is-pickable'], tabindex: '0', role: 'slider',
    'aria-label': onPick ? 'Tone over time. Enter picks the start' : 'Tone over time',
    'aria-valuemin': 0, 'aria-valuemax': Math.round(end),
  }, plot, cursor, dots);
  const xLabels = h('div', { class: 'tone-x', 'aria-hidden': 'true' }, timeTicks(end).map((t) => h('span', {
    class: ['tone-tick', t === 0 && 'is-first', t >= end - 1e-6 && 'is-last'], style: { left: `${(t / end) * 100}%` },
  }, axisTime(t, end))));
  const readout = h('p', { class: 'tone-readout', 'aria-live': 'polite' });
  const note = h('figcaption', { class: 'tone-note' }, `${TONE_BASIS} in ${step} s bins. `,
    onPick ? 'Pick a point to start the audition there.' : 'Listen to a passage to judge its tone.');
  const options = tl.bands.map((b) => ({
    value: b.key, label: [h('i', { class: 'tone-swatch', style: { '--c': color(b.key) }, 'aria-hidden': 'true' }), b.label],
  }));
  const picker = compact ? h('p', { class: 'tone-legend' }, options.filter((o) => shown.has(o.value)).map((o) => h('span', null, o.label)))
    : chips({ label: 'Bands', size: 'sm', values: shown, options, onChange: (next) => { shown = next; draw(); } });

  function draw() {
    const keys = [...shown];
    range = toneRange(tl, keys);
    while (lines.firstChild) lines.removeChild(lines.firstChild);
    for (const key of keys) {
      const d = tonePath(tl, key, range);
      if (d) lines.append(svg('path', { class: 'tone-line', d, style: `stroke: ${color(key)}` }));
    }
    yLabels.textContent = '';
    yLabels.append(...[[range, PAD], [0, H / 2], [-range, H - PAD]].map(([v, y]) =>
      h('span', { style: { top: `${y}%` } }, v ? `${fmtSigned(v, 0)} dB` : '0')));
    point(index);
  }

  function point(i) {
    index = Math.max(-1, Math.min(n - 1, i));
    const on = index >= 0;
    cursor.hidden = !on;
    dots.textContent = '';
    if (!on) { readout.textContent = ''; area.removeAttribute('aria-valuenow'); area.removeAttribute('aria-valuetext'); return; }
    const left = `${(index * step / end) * 100}%`;
    cursor.style.left = left;
    const keys = [...shown];
    for (const key of keys) {
      const v = tl.values[index]?.[bandIndex(tl, key)];
      if (Number.isFinite(v)) dots.append(h('i', { class: 'tone-dot', style: { left, top: `${yAt(v, range)}%`, '--c': color(key) } }));
    }
    const text = toneReadout(tl, index, keys, markers);
    readout.textContent = text;
    area.setAttribute('aria-valuenow', String(Math.round(index * step)));
    area.setAttribute('aria-valuetext', text);
  }

  const nearest = (e) => {
    const r = area.getBoundingClientRect();
    return r.width ? Math.round(((e.clientX - r.left) / r.width) * end / step) : 0;
  };
  area.addEventListener('pointermove', (e) => point(nearest(e)));
  area.addEventListener('pointerleave', () => { if (document.activeElement !== area) point(-1); });
  area.addEventListener('focus', () => { if (index < 0) point(0); });
  area.addEventListener('blur', () => point(-1));
  area.addEventListener('click', (e) => {
    point(nearest(e));
    if (onPick && index >= 0) onPick(index * step);
  });
  area.addEventListener('keydown', (e) => {
    const moves = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, PageUp: 10, PageDown: -10 };
    if (e.key in moves) point(Math.max(0, index) + moves[e.key]);
    else if (e.key === 'Home') point(0);
    else if (e.key === 'End') point(n - 1);
    else if ((e.key === 'Enter' || e.key === ' ') && onPick && index >= 0) onPick(index * step);
    else return;
    e.preventDefault();
  });

  draw();
  return h('figure', { class: ['tone-chart', compact && 'is-compact'] },
    h('div', { class: 'tone-plot' }, yLabels, area, xLabels), readout, note, picker);
}
