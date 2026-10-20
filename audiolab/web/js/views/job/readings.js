// Job page — readings: measurements turned into plain words, or numbers with
// "Show details" on; shared by Enhance results and the Check report, which lives here too.
import { h, icon, fmtDuration, fmtNum, fmtSigned, confidenceWords } from '../../dom.js';
import { mediaUrl } from '../../api.js';
import { showDetails } from '../../store.js';
import { block } from '../../ui.js';

export function loudnessWord(l) {
  if (l == null) return '–';
  if (l >= -9) return 'Very loud';
  if (l >= -13) return 'Loud';
  if (l >= -18) return 'Normal';
  if (l >= -24) return 'Quiet';
  return 'Very quiet';
}

export function peakVerdict(tp) {
  if (tp == null) return { word: '–', level: 'info' };
  if (tp > -0.1) return { word: 'Clipping', level: 'danger' };
  if (tp > -1.0) return { word: 'Little headroom', level: 'warning' };
  return { word: 'Safe', level: 'ok' };
}

export function dynamicsWord(lra) {
  if (lra == null) return '–';
  if (lra < 3) return 'Very even';
  if (lra < 6) return 'Even';
  if (lra < 10) return 'Balanced';
  if (lra < 15) return 'Wide';
  return 'Very wide';
}

/** What was done, in plain lines. Not where the files went ("Saved 2 files to /Users/…"): the files
 * section says that. `skip`: leading lines the page shows elsewhere. */
export function summaryBlock(r, title = 'What we did', { skip = 0 } = {}) {
  const dirs = [r.tracks_dir, r.output_dir].filter(Boolean);
  const lines = (r.summary || []).slice(skip)
    .filter((s) => !/^Saved( \d+ files?)? to /.test(s))
    .map((s) => dirs.reduce((t, d) => t.replace(` — saved to ${d}.`, '.'), s));
  if (!lines.length) return null;
  return block(title, h('ul', { class: 'summary' }, lines.map((s) => h('li', null, icon('check', 15), h('span', null, s)))));
}

/** A section whose title row holds the "Show details" switch (the job page puts it into `.details-slot`). */
export function detailsBlock(title, ...children) {
  return h('section', { class: 'block' },
    h('div', { class: 'block-head' }, h('h2', { class: 'block-title' }, title), h('span', { class: 'details-slot' })),
    ...children);
}

/** "Key A minor · 8A · Tempo ≈ 126 BPM" for music (both are estimates). */
export function musicFacts(a, c) {
  const key = keyText(a?.key);
  const bpm = a?.bpm ?? c?.bpm;
  if (!key && !bpm) return null;
  return h('p', { class: 'music-facts' },
    key ? h('span', null, h('span', { class: 'muted' }, 'Key '), h('strong', null, key)) : null,
    bpm ? h('span', null, h('span', { class: 'muted' }, 'Tempo '), h('strong', null, `≈ ${Math.round(bpm)} BPM`)) : null,
    h('span', { class: 'muted small' }, key && bpm ? 'estimates' : 'estimate'));
}

function keyText(k) {
  if (!k || !k.name) return '';
  return k.camelot ? `${k.name} · ${k.camelot}` : k.name;
}

// ---------------------------------------------------------------------------
// Enhance: before → after
// ---------------------------------------------------------------------------
export function metricsBlock(r) {
  const b = r.metrics?.before;
  const a = r.metrics?.after;
  if (!b || !a) return null;
  if (!showDetails()) {
    const row = (label, before, after) => h('div', { class: 'ba-row' },
      h('span', { class: 'ba-label' }, label),
      h('span', { class: 'ba-before' }, before), icon('arrow', 14, 'ba-arrow'), h('strong', { class: 'ba-after' }, after));
    return detailsBlock('Before → after', h('div', { class: 'ba-rows' },
      row('Volume', loudnessWord(b.lufs), loudnessWord(a.lufs)),
      row('Peaks', peakVerdict(b.true_peak_dbtp).word, peakVerdict(a.true_peak_dbtp).word),
      row('Dynamics', dynamicsWord(b.lra), dynamicsWord(a.lra))));
  }
  const tr = (label, unit, kb, digits = 1) => h('tr', null,
    h('th', { scope: 'row' }, label, h('span', { class: 'muted small' }, ` ${unit}`)),
    h('td', { class: 'num' }, fmtNum(b[kb], digits)),
    h('td', { class: 'num' }, fmtNum(a[kb], digits)),
    h('td', { class: 'num muted' }, b[kb] != null && a[kb] != null ? fmtSigned(a[kb] - b[kb]) : '–'));
  return detailsBlock('Measurements',
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'Before'), h('th', null, 'After'), h('th', null, 'Change'))),
      h('tbody', null,
        tr('Integrated loudness', 'LUFS', 'lufs'),
        tr('True peak', 'dBTP', 'true_peak_dbtp'),
        tr('Loudness range', 'LU', 'lra'))));
}

const CODEC_NAMES = {
  mp3: 'MP3', aac: 'AAC', alac: 'Apple Lossless', flac: 'FLAC', opus: 'Opus', vorbis: 'Ogg Vorbis',
  wmav2: 'WMA', ac3: 'AC-3', eac3: 'E-AC-3', pcm_s16le: 'PCM 16-bit', pcm_s24le: 'PCM 24-bit',
  pcm_s32le: 'PCM 32-bit', pcm_f32le: 'PCM 32-bit float', pcm_s16be: 'PCM 16-bit', pcm_s24be: 'PCM 24-bit',
};

/** "MP3 · 128 kbps · 44.1 kHz · stereo" — the source's own format (details). */
export function sourceLine(a) {
  if (!a) return '';
  const codec = a.source_audio_codec ? (CODEC_NAMES[a.source_audio_codec] || a.source_audio_codec.toUpperCase()) : null;
  const ch = a.channels === 1 ? 'mono' : a.channels === 2 ? 'stereo' : a.channels ? `${a.channels} channels` : null;
  return [codec, a.source_bitrate_kbps ? `${Math.round(a.source_bitrate_kbps)} kbps` : null,
    a.samplerate ? `${fmtNum(a.samplerate / 1000, 1).replace('.0', '')} kHz` : null, ch].filter(Boolean).join(' · ');
}

export function decisionsBlock(r) {
  return block('Decisions',
    h('table', { class: 'data decisions' },
      h('thead', null, h('tr', null, h('th', null, 'Setting'), h('th', null, 'Value'), h('th', null, 'Why'))),
      h('tbody', null, r.decisions.map((d) => h('tr', null,
        h('th', { scope: 'row' }, d.label, h('span', { class: ['src-tag', `src-${d.source}`] }, d.source)),
        h('td', { class: 'num' }, d.value),
        h('td', { class: 'muted' }, d.why || ''))))));
}

export function spectrogramBlock(job, r) {
  return block('Spectrogram',
    h('img', { class: 'spectrogram', src: mediaUrl(job.id, r.spectrogram), alt: 'Spectrogram of the audio', loading: 'lazy' }),
    h('p', { class: 'muted small' }, 'Time runs left to right, pitch bottom to top, brightness is energy. A sharp flat ceiling usually means the audio was compressed (MP3/AAC) at some point.'));
}

// ---------------------------------------------------------------------------
// Check: the quality report — the verdicts as a list (label · word · what it means),
// the findings as rows
// ---------------------------------------------------------------------------
/** `value`: the number behind the word, with "Show details" on (`always`: also without). */
function verdict(label, word, sub, level, value, always = false) {
  return h('div', { class: ['verdict', level && `verdict-${level}`] },
    h('dt', { class: 'verdict-label' }, label),
    h('dd', { class: 'verdict-word' }, word),
    h('dd', { class: 'verdict-sub' }, sub || '',
      value && (always || showDetails()) ? h('span', { class: 'verdict-value num' }, value) : null));
}

export function analyzeResult(job, r) {
  const a = r.analysis || {};
  const c = r.classification;
  const bw = a.bandwidth || {};
  const pk = peakVerdict(a.true_peak_dbtp);
  if (a.is_silent) return silentResult(a);
  const cutoff = bw.cutoff_hz;
  const judged = bw.judged !== false;   // pure tones / near-silence: no meaningful cutoff
  const full = judged && (cutoff == null || cutoff >= 19500);
  const bpm = a.bpm ?? c?.bpm;
  const key = keyText(a.key);
  const conf = a.key?.confidence;
  const verdicts = h('dl', { class: 'verdicts' },
    verdict('Volume', loudnessWord(a.integrated_lufs), 'How loud it sounds overall', null,
      a.integrated_lufs != null ? `${fmtNum(a.integrated_lufs)} LUFS` : null),
    verdict('Peaks', pk.word,
      pk.level === 'danger' ? 'The loudest moments go over the limit and can distort'
        : pk.level === 'warning' ? 'Very close to the limit' : 'No distortion from overs', pk.level,
      a.true_peak_dbtp != null ? `${fmtSigned(a.true_peak_dbtp)} dBTP` : null),
    verdict('Dynamics', dynamicsWord(a.lra), 'Gap between quiet and loud parts', null,
      a.lra != null ? `${fmtNum(a.lra)} LU` : null),
    !judged
      ? verdict('Frequency range', 'Can’t tell', bw.verdict || 'Not enough different sounds in it to judge the range', null)
      : verdict('Frequency range', full ? 'Full range' : `Stops at ${fmtNum(cutoff / 1000, 1)} kHz`,
        full ? 'The highs are all there' : 'The very top end is missing — see below', bw.is_lossy_cutoff ? 'warning' : full ? 'ok' : null,
        cutoff ? `${Math.round(cutoff)} Hz` : null),
    verdict('What it is', c?.label || 'Unknown',
      c ? `${confidenceWords(c.confidence, c.confidence_label)}${(c.reasons || []).length ? ` — ${c.reasons.slice(0, 2).join(', ')}` : ''}` : null,
      null, bpm ? `≈ ${Math.round(bpm)} BPM` : null, true),
    key ? verdict('Key', key, conf != null && conf < 0.4 ? 'rough estimate' : 'estimate', null,
      conf != null ? `${Math.round(conf * 100)} % confidence` : null) : null);
  const out = [detailsBlock('Quality check', verdicts)];
  const alerts = (a.alerts || []).filter((x) => x.level !== 'ok');
  if (alerts.length) out.push(block('Findings', h('ul', { class: 'findings' }, alerts.map(findingRow))));
  const s = summaryBlock(r, 'Summary');
  if (s) out.push(s);
  if (r.spectrogram) out.push(spectrogramBlock(job, r));
  if (showDetails()) out.push(factsBlock(a));
  return out;
}

function findingRow(x) {
  const level = x.level === 'danger' ? 'danger' : x.level === 'warning' ? 'warning' : 'info';
  return h('li', { class: ['finding', `finding-${level}`] },
    icon(level === 'info' ? 'info' : 'alert', 16, 'finding-ico'),
    h('div', { class: 'finding-text' }, h('strong', null, x.title), x.message ? h('p', { class: 'muted' }, x.message) : null));
}

function factsBlock(a) {
  return block('Details', h('dl', { class: 'facts' },
    fact('Length', fmtDuration(a.duration_s)),
    fact('Source', sourceLine(a)),
    fact('Measured on', a.analysis_basis)));
}

/** Nothing to measure: one clear state instead of a list of "–". */
function silentResult(a) {
  const out = [detailsBlock('Quality check',
    h('div', { class: 'silent-state' },
      h('div', { class: 'empty-meter', 'aria-hidden': 'true' }, ...Array.from({ length: 9 }, () => h('i', { style: { '--i': 0 } }))),
      h('strong', null, 'Silent file'),
      h('p', { class: 'muted' }, `There’s no sound in it${a.duration_s ? ` — ${fmtDuration(a.duration_s)} of silence` : ''}, so there’s nothing to measure. Check that it’s the right file or that the recording worked.`)))];
  if (showDetails()) out.push(factsBlock(a));
  return out;
}

function fact(label, value) {
  if (value == null || value === '') return null;
  return [h('dt', null, label), h('dd', null, String(value))];
}
