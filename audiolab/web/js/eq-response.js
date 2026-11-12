// Magnitude for the plot only. Match audiolab/dsp/filters.py and the fixed stages in recipes/chain.py.
// These filters run at the mastering rate, independently of the chosen export sample rate.
import { EQ_FIELDS } from './eq.js';

export const EQ_SAMPLE_RATE = 48000;
const angle = (hz) => 2 * Math.PI * hz / EQ_SAMPLE_RATE;

function bell({ hz, db, q }) {
  const gain = 10 ** (db / 80); // the DSP's half-gain design
  const center = angle(hz);
  const alpha = Math.sin(center) / (2 * q);
  const mid = -2 * Math.cos(center);
  return [1 + alpha * gain, mid, 1 - alpha * gain, 1 + alpha / gain, mid, 1 - alpha / gain];
}

function shelf(hz, db, high) {
  const gain = 10 ** (db / 80);
  const center = angle(hz);
  const cosine = Math.cos(center);
  const sum = gain + 1;
  const difference = gain - 1;
  const twice = Math.sqrt(2 * gain) * Math.sin(center); // RBJ shelf with slope 1
  if (high) {
    return [gain * (sum + difference * cosine + twice), -2 * gain * (difference + sum * cosine),
      gain * (sum + difference * cosine - twice), sum - difference * cosine + twice,
      2 * (difference - sum * cosine), sum - difference * cosine - twice];
  }
  return [gain * (sum - difference * cosine + twice), 2 * gain * (difference - sum * cosine),
    gain * (sum - difference * cosine - twice), sum + difference * cosine + twice,
    -2 * (difference + sum * cosine), sum + difference * cosine - twice];
}

function highpass(hz) {
  const center = angle(hz);
  const cosine = Math.cos(center);
  const alpha = Math.sin(center) / Math.SQRT2; // order-2 Butterworth
  return [(1 + cosine) / 2, -(1 + cosine), (1 + cosine) / 2, 1 + alpha, -2 * cosine, 1 - alpha];
}

export function responseFilters(bands, context = {}) {
  const params = context.params || {};
  const fixed = [];
  if (params.highpass_hz > 0) fixed.push(highpass(params.highpass_hz));
  if (!(params.adaptive_lowmid_db > 0) && params.mud_cut_db < 0) fixed.push(bell({ hz: 320, db: params.mud_cut_db, q: 1 }));
  if (params.bass_db > 0) fixed.push(shelf(85, params.bass_db, false));
  if (params.presence_db) fixed.push(bell({ hz: 3000, db: params.presence_db, q: 1.4 }));
  if (params.air_db) fixed.push(shelf(11000, params.air_db, true));
  return { user: bands.map(bell), fixed: [...fixed, ...(context.bands || []).map(bell)] };
}

/** Square one-pass magnitude to get the forward-backward response. Evaluate the SOS on the unit circle. */
export function responseDb(filters, hz) {
  const w = angle(hz);
  const sin = Math.sin(w);
  const cos = Math.cos(w);
  return filters.reduce((db, [b0, b1, b2, a0, a1, a2]) => {
    const br = (b0 + b2) * cos + b1;
    const bi = (b0 - b2) * sin;
    const ar = (a0 + a2) * cos + a1;
    const ai = (a0 - a2) * sin;
    return db + 20 * Math.log10((br * br + bi * bi) / (ar * ar + ai * ai));
  }, 0);
}

const { min, max } = EQ_FIELDS.hz;
const LOG_RANGE = Math.log(max / min);
export const eqX = (hz) => Math.log(hz / min) / LOG_RANGE;
export const eqFrequency = (x) => x <= 0 ? min : x >= 1 ? max : min * Math.exp(LOG_RANGE * x);

const grid = Array.from({ length: 769 }, (_, i) => eqFrequency(i / 768));

/** Include exact band centers and add samples through their skirts on the bilinear-warped axis. */
export function responseFrequencies(bands) {
  const points = new Set(grid);
  for (const { hz, q } of bands) {
    points.add(hz);
    const center = Math.tan(angle(hz) / 2);
    for (let i = -64; i <= 64; i++) {
      if (i === 0) continue;
      const point = Math.atan(center * Math.exp(i / (24 * q))) * EQ_SAMPLE_RATE / Math.PI;
      if (point > min && point < max) points.add(point);
    }
  }
  return [...points].sort((a, b) => a - b);
}
