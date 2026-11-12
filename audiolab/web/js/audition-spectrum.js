// Spectrum of one exact audition crop, as delivered by the aligned PCM endpoint.
// Average one-sided power per FFT bin, using both channels' energy.
import { h, fmtNum } from './dom.js';
import { url } from './api.js';

const N = 4096;
const HOP = N / 2;
const RATE = 48000;
const GRID = 1920;
const CHUNK = 250 * GRID;
const MAX_FRAMES = RATE * 30;
const HANN = Float64Array.from({ length: N }, (_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / N));
const FLOOR = -110;
const SVG = 'http://www.w3.org/2000/svg';

const reversed = Uint16Array.from({ length: N }, (_, i) => {
  let result = 0;
  for (let bits = N; bits > 1; bits >>= 1, i >>= 1) result = (result << 1) | (i & 1);
  return result;
});
const twiddleR = Float64Array.from({ length: HOP }, (_, i) => Math.cos(-2 * Math.PI * i / N));
const twiddleI = Float64Array.from({ length: HOP }, (_, i) => Math.sin(-2 * Math.PI * i / N));

function transform(re, im) {
  for (let width = 2; width <= N; width *= 2) {
    const half = width / 2;
    const step = N / width;
    for (let offset = 0; offset < N; offset += width) {
      for (let i = 0; i < half; i++) {
        const a = offset + i;
        const b = a + half;
        const w = i * step;
        const r = re[b] * twiddleR[w] - im[b] * twiddleI[w];
        const j = re[b] * twiddleI[w] + im[b] * twiddleR[w];
        re[b] = re[a] - r;
        im[b] = im[a] - j;
        re[a] += r;
        im[a] += j;
      }
    }
  }
}

/** Mean two-channel power, with a window centred on each hop and the edges zero padded. */
export function auditionPower(pcm) {
  const frames = pcm.length / 2;
  const re = new Float64Array(N);
  const im = new Float64Array(N);
  const power = new Float64Array(HOP + 1);
  let weights = 0;
  for (let start = -HOP; start < frames; start += HOP) {
    const lo = Math.max(0, -start);
    const hi = Math.min(N, frames - start);
    for (let i = lo; i < hi; i++) weights += HANN[i] ** 2;
    for (let channel = 0; channel < 2; channel++) {
      re.fill(0);
      im.fill(0);
      for (let i = lo; i < hi; i++) re[reversed[i]] = pcm[(start + i) * 2 + channel] * HANN[i];
      transform(re, im);
      for (let i = 0; i <= HOP; i++) power[i] += re[i] ** 2 + im[i] ** 2;
    }
  }
  if (weights) {
    // Combine channel powers by their mean, with double-sided energy folded into positive bins.
    for (let i = 0; i <= HOP; i++) power[i] /= N * weights * (i === 0 || i === HOP ? 2 : 1);
  }
  return power;
}

export function auditionPcmUrl(jobId, key, offset, frames) {
  return url(`/api/jobs/${encodeURIComponent(jobId)}/pcm/${encodeURIComponent(key)}`, { offset, frames });
}

export async function loadAuditionSpectrum(job, key, signal) {
  const { frames, sample_rate_hz } = job.result?.excerpt || {};
  if (sample_rate_hz !== RATE || !Number.isInteger(frames) || frames < 1 || frames > MAX_FRAMES) {
    throw new Error('The exact audition bounds aren’t available.');
  }
  const pcm = new Float32Array(frames * 2);
  for (let offset = 0; offset < frames; offset += CHUNK) {
    const needed = Math.min(CHUNK, frames - offset);
    const request = Math.ceil(needed / GRID) * GRID;
    const response = await fetch(auditionPcmUrl(job.id, key, offset, request), { signal });
    if (!response.ok) throw new Error('Couldn’t read the audition audio for the spectrum.');
    const chunk = new Float32Array(await response.arrayBuffer());
    if (chunk.length < needed * 2 || chunk.length % 2) throw new Error('The audition audio is incomplete.');
    pcm.set(chunk.subarray(0, needed * 2), offset * 2);
  }
  return auditionPower(pcm);
}

function svg(tag, attrs, label = null) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  if (label != null) el.textContent = label;
  return el;
}

/** Full FFT resolution on a logarithmic frequency axis; the visual floor is fixed for comparisons. */
export function spectrumPlot(power) {
  const w = 720;
  const height = 155;
  const left = 44;
  const right = w - 18;
  const top = 12;
  const bottom = height - 24;
  const df = RATE / N;
  const x = (hz) => left + (right - left) * Math.log(hz / df) / Math.log(N / 2);
  const y = (db) => top + (bottom - top) * Math.min(1, Math.max(0, db / FLOOR));
  const db = (value) => 10 * Math.log10(value);
  let peak = 1;
  const points = [];
  for (let i = 1; i < power.length; i++) {
    points.push(`${x(i * df).toFixed(2)},${y(db(power[i])).toFixed(2)}`);
    if (power[i] > power[peak]) peak = i;
  }
  const axis = svg('g', { class: 'adjust-spectrum-axis' });
  for (const value of [0, -30, -60, -90]) {
    axis.append(svg('line', { x1: left, x2: right, y1: y(value), y2: y(value) }),
      svg('text', { x: left - 6, y: y(value) + 4, 'text-anchor': 'end' }, value));
  }
  for (const hz of [20, 100, 1000, 10000]) {
    axis.append(svg('line', { x1: x(hz), x2: x(hz), y1: top, y2: bottom }),
      svg('text', { x: x(hz), y: height - 5, 'text-anchor': 'middle' }, hz < 1000 ? `${hz} Hz` : `${hz / 1000} kHz`));
  }
  const chart = svg('svg', { viewBox: `0 0 ${w} ${height}`, 'aria-hidden': 'true', class: 'adjust-spectrum-plot' });
  chart.append(axis, svg('polyline', { points: points.join(' '), class: 'adjust-spectrum-curve' }));
  const level = db(power[peak]);
  const peakText = level < FLOOR ? 'All displayed bins are below −110 dBFS.'
    : `Strongest bin at ${Math.round(peak * df).toLocaleString()} Hz, ${fmtNum(level)} dBFS.`;
  return h('div', null, chart, h('p', { class: 'small muted' },
    `Mean stereo power in dBFS per bin · 12 Hz–24 kHz on a log scale · 11.72 Hz bin spacing, Hann window. ${peakText}`));
}
