// Custom EQ: up to eight peaking bands, written as text with one band per line
// ("160 Hz −3.3 dB Q 2.9"). Used by Enhance → Fine-tuning and the Library's section
// preview; the server applies the bands after the tone shaping, before compression, loudness and peak control (docs/API.md).
import { h } from './dom.js';

export const EQ_MAX_BANDS = 8;
const HZ = [20, 20000];
const DB = [-6, 6];
const Q = [0.3, 8];
const EXAMPLE = '160 Hz −3.3 dB Q 2.9';

/** Starting points from a sound engineer's curves; a preset adds its bands to the list. */
export const EQ_PRESETS = [
  {
    label: 'Sub focus',
    desc: 'Tightens the lowest rumble and focuses the kick’s sub around 45 Hz.',
    bands: [{ hz: 31, db: -1, q: 3.1 }, { hz: 45, db: 1, q: 3.1 }],
  },
  {
    label: 'Clean upper bass',
    desc: 'Cuts boom around 160 Hz while keeping warmth at 280 Hz.',
    bands: [{ hz: 160, db: -3.3, q: 2.9 }, { hz: 280, db: 1.5, q: 2 }],
  },
];

const NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
const FREQ_WORDS = new Set(['f', 'fc', 'freq', 'frequency']);
const GAIN_WORDS = new Set(['g', 'gain']);
const TYPE_WORDS = new Set(['pk', 'peq', 'peak', 'peaking', 'bell']); // "PK 160 Hz …" reads like a plain line
// Equalizer APO: "Filter 3: ON PK Fc 160 Hz Gain -3.3 dB Q 2.90" (and "Preamp: -4 dB", which the loudness step replaces).
const APO_FILTER = /^filter\s*\d*\s*:\s*(\S+)\s+([a-z]+)(.*)$/i;
const round = (v) => Math.round(v * 1000) / 1000;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/**
 * Text → { bands: [{ hz, db, q }], errors: [{ line, message }] }. Blank lines and lines starting
 * with # are skipped; so are APO "Preamp:" lines and filters switched OFF. Values out of range are
 * reported per line, never clamped. Bands with 0 dB do nothing and are left out.
 */
export function parseEq(text) {
  const bands = [];
  const errors = [];
  String(text ?? '').split(/\r?\n/).forEach((raw, i) => {
    const line = i + 1;
    let rest = raw.trim();
    if (!rest || rest.startsWith('#') || /^preamp\s*:/i.test(rest)) return;
    if (/^filter\s*\d*\s*:/i.test(rest)) {
      const m = rest.match(APO_FILTER);
      if (!m || !['on', 'off'].includes(m[1].toLowerCase())) { errors.push({ line, message: 'Can’t read this filter line.' }); return; }
      if (m[1].toLowerCase() === 'off') return;
      if (!TYPE_WORDS.has(m[2].toLowerCase())) { errors.push({ line, message: 'Only peaking (PK) bands are supported.' }); return; }
      rest = m[3];
    }
    const result = readBand(rest);
    if (result.error) errors.push({ line, message: result.error });
    else if (result.band.db === 0) { /* no change: nothing to send */ }
    else if (bands.length >= EQ_MAX_BANDS) errors.push({ line, message: `Up to ${EQ_MAX_BANDS} bands.` });
    else bands.push(result.band);
  });
  return { bands, errors };
}

function tokens(text) {
  return text
    .replace(/[−‒–‐]/g, '-')  // typographic minus signs and dashes
    .replace(/(\d),(\d)/g, '$1.$2')              // a decimal comma: 2,5 kHz
    .replace(/[,;:=]/g, ' ')                      // separators: "160 Hz, −3 dB; Q=2"
    .replace(/([+-])\s+(?=[\d.])/g, '$1')         // "- 3 dB"
    .replace(/(\d)([a-z])/gi, '$1 $2')            // "2.5k", "-3dB"
    .replace(/([a-z])([+\-.\d])/gi, '$1 $2')      // "Q2.9"
    .split(/\s+/).filter(Boolean);
}

function readBand(text) {
  const band = {};
  let expect = null; // the value a keyword (Fc, Gain, Q) announced
  const toks = tokens(text);
  for (let i = 0; i < toks.length; i++) {
    const word = toks[i].toLowerCase();
    if (FREQ_WORDS.has(word)) { expect = 'hz'; continue; }
    if (GAIN_WORDS.has(word)) { expect = 'db'; continue; }
    if (word === 'q') { expect = 'q'; continue; }
    if (TYPE_WORDS.has(word) && i === 0) continue;
    if (!NUMBER.test(word)) return { error: `Can’t read “${toks[i]}”.` };
    let value = Number(word);
    let key;
    const unit = toks[i + 1]?.toLowerCase();
    if (unit === 'hz') { key = 'hz'; i++; }
    else if (unit === 'k' || unit === 'khz') { key = 'hz'; value *= 1000; i++; }
    else if (unit === 'db') { key = 'db'; i++; }
    else if (expect) key = expect;
    else if (/^[+-]/.test(word)) key = 'db'; // a signed number is a gain
    else if (band.hz == null) key = 'hz';
    else if (band.db == null) key = 'db';
    else return { error: 'Write the width as Q, like Q 1.4.' };
    expect = null;
    if (band[key] != null) return { error: 'Use one band per line.' };
    band[key] = round(value);
  }
  if (band.hz == null) return { error: 'Add a frequency, like 160 Hz.' };
  if (band.db == null) return { error: 'Add a gain in dB, like −3 dB.' };
  if (band.q == null) return { error: 'Add a width as Q, like Q 1.4.' };
  if (band.hz < HZ[0] || band.hz > HZ[1]) return { error: 'Frequency must be 20 Hz to 20 kHz.' };
  if (band.db < DB[0] || band.db > DB[1]) return { error: 'Gain must be −6 to +6 dB.' };
  if (band.q < Q[0] || band.q > Q[1]) return { error: 'Q must be 0.3 to 8.' };
  return { band: { hz: band.hz, db: band.db, q: band.q } };
}

// --- Display ---------------------------------------------------------------------
const digits = (v, max, min = 0) => {
  const s = String(Number(Number(v).toFixed(max)));
  return min && !s.includes('.') ? `${s}.${'0'.repeat(min)}` : s;
};

function bandText(b, compact = false) {
  const hz = Number(b.hz) >= 1000 ? `${digits(b.hz / 1000, 3)} kHz` : `${digits(b.hz, 2)} Hz`;
  const db = Number(b.db);
  const gain = `${db > 0 ? '+' : db < 0 ? '−' : ''}${digits(Math.abs(db), 2, 1)} dB`;
  return `${hz} ${gain} ${compact ? 'Q' : 'Q '}${digits(b.q, 2, 1)}`;
}

const list = (bands) => (Array.isArray(bands) ? bands : []);

/** Bands → text for the box, one per line: "160 Hz −3.3 dB Q 2.9" (parseEq reads it back). */
export function formatEq(bands) {
  return list(bands).map((b) => bandText(b)).join('\n');
}

/** Bands on one line, for settings lists: "160 Hz −3.3 dB Q2.9 · 280 Hz +1.5 dB Q2.0". */
export function eqSummary(bands) {
  return list(bands).map((b) => bandText(b, true)).join(' · ');
}

/** The same bands as far as anyone can see (order aside). */
export function sameEq(a, b) {
  const key = (bands) => list(bands).map((x) => bandText(x)).sort().join('\n');
  return key(a) === key(b);
}

// --- Editor ------------------------------------------------------------------------
let uid = 0;

/**
 * The bands as text, the presets, and the problems per line under it.
 * `onInput({ text, bands, errors })` runs after every edit (typing or a preset).
 * Returns { el, textarea, read() → parseEq result, set(text) }.
 */
export function eqField({ id, text = '', label = 'Custom EQ', onInput }) {
  const base = id || `eq-${++uid}`;
  const ta = h('textarea', {
    id: base, class: 'field eq-text', rows: 3, spellcheck: 'false', autocomplete: 'off',
    placeholder: EXAMPLE, 'aria-label': label, 'aria-describedby': `${base}-note ${base}-errors`,
  });
  ta.value = text;
  const note = h('p', { class: 'small muted eq-note', id: `${base}-note`, 'aria-live': 'polite' });
  const out = h('div', { class: 'eq-out' });
  const presets = h('div', { class: 'eq-presets' }, h('span', { class: 'small muted' }, 'Presets'),
    EQ_PRESETS.map((p) => h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm', title: p.desc, onclick: () => addPreset(p),
    }, p.label)));

  function show({ bands, errors }) {
    ta.setAttribute('aria-invalid', String(errors.length > 0));
    ta.setCustomValidity(errors.length ? `Line ${errors[0].line}: ${errors[0].message}` : '');
    note.textContent = errors.length ? plural(errors.length, 'problem') : bands.length ? plural(bands.length, 'band') : '';
    while (out.firstChild) out.removeChild(out.firstChild);
    if (errors.length) {
      out.append(h('ul', { class: 'line-errors', id: `${base}-errors` },
        errors.map((e) => h('li', null, h('strong', null, `Line ${e.line}: `), e.message))));
    }
  }

  function update() {
    const result = parseEq(ta.value);
    show(result);
    onInput?.({ text: ta.value, ...result });
    return result;
  }

  function addPreset(p) {
    const have = new Set(parseEq(ta.value).bands.map((b) => bandText(b)));
    const lines = p.bands.map((b) => bandText(b)).filter((line) => !have.has(line));
    if (lines.length) ta.value = [ta.value.replace(/\s+$/, ''), ...lines].filter(Boolean).join('\n');
    const result = update();
    if (!result.errors.length) note.textContent = lines.length ? `${p.label}: ${p.desc}` : `${p.label} is already in the list.`;
  }

  ta.addEventListener('input', update);
  show(parseEq(text));
  return {
    el: h('div', { class: 'eq-edit' }, ta, presets, note, out), textarea: ta, read: () => parseEq(ta.value),
    set: (text) => { ta.value = text; return update(); }, // new text from outside (a suggestion), as if typed
  };
}
