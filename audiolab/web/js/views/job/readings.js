// Job page — readings: measurements turned into plain words, or numbers with
// "Show details" on; shared by Enhance results and the Check report, which lives here too.
import { h, icon, fmtDuration, fmtNum, fmtSigned, confidenceWords } from '../../dom.js';
import { mediaUrl } from '../../api.js';
import { showDetails } from '../../store.js';
import { block } from '../../ui.js';
import { toneChart } from '../../tone-chart.js';
import { audioLine, streamLine } from '../../compare.js';

export function trackCheckNeedsAttention(file) {
  const status = file.verification?.status;
  return status === 'warning' || status === 'unverified';
}

export function loudnessWord(l) {
  if (l == null) return '–';
  if (l >= -9) return 'Very loud';
  if (l >= -13) return 'Loud';
  if (l >= -18) return 'Moderate';
  if (l >= -24) return 'Quiet';
  return 'Very quiet';
}

/** Peaks in a word. `limit` (`analysis.peak_limit_dbtp`, lossy files only): encoding has already used the
 * master's headroom, so only peaks above it count; otherwise use the display thresholds below. */
export function peakVerdict(tp, limit = null) {
  if (tp == null) return { word: '–', level: 'info', note: '' };
  if (tp > 0) return { word: 'Above full scale', level: 'danger', note: 'Measured true peak exceeds 0 dBTP' };
  if (tp > (limit ?? -0.1)) return { word: 'No headroom', level: 'danger', note: 'The loudest moments are almost at full scale' };
  if (limit == null && tp > -1.0) return { word: 'Little headroom', level: 'warning', note: 'Very close to the limit' };
  return { word: 'Within limit', level: 'ok', note: 'Measured true peak is within the check limit' };
}

export function dynamicsWord(lra) {
  if (lra == null) return '–';
  if (lra < 3) return 'Very even';
  if (lra < 6) return 'Even';
  if (lra < 10) return 'Moderate';
  if (lra < 15) return 'Wide';
  return 'Very wide';
}

// The one convention for server summary lines: a plain sentence, optionally followed by its details
// in trailing brackets — "Made it louder (+8.2 dB: −25.2 → −17.0 LUFS)."
const TRAILING_DETAILS = /\s*\([^()]*\)\s*([.!?]?)\s*$/;

/** A summary line without "Show details": its trailing bracket goes, nothing else is changed. */
export function plainLine(s) {
  return s.replace(TRAILING_DETAILS, '$1').trim() || s;
}

/** What was done, in plain lines (the server's own lines with "Show details"). Not where the files went
 * ("Saved 2 files to /Users/…"): the files section says that. `skip`: leading lines the page shows elsewhere. */
export function summaryBlock(r, title = 'What we did', { skip = 0 } = {}) {
  const dirs = [r.tracks_dir, r.output_dir].filter(Boolean);
  const details = showDetails();
  const lines = (r.summary || []).slice(skip)
    .filter((s) => !/^Saved( \d+ files?)? to /.test(s))
    .map((s) => dirs.reduce((t, d) => t.replace(` — saved to ${d}.`, '.'), s))
    .map((s) => (details ? s : plainLine(s)));
  if (!lines.length) return null;
  return block(title, h('ul', { class: 'summary' }, lines.map((s) => h('li', null, icon('check', 15), h('span', null, s)))));
}

/** A section whose title row holds the "Show details" control (the job page puts it into `.details-slot`). */
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
    return detailsBlock('Original → master', h('div', { class: 'ba-rows' },
      row('Volume', loudnessWord(b.lufs), loudnessWord(a.lufs)),
      row('Peaks', peakVerdict(b.true_peak_dbtp, r.analysis?.peak_limit_dbtp).word, peakVerdict(a.true_peak_dbtp).word),
      row('Dynamics', dynamicsWord(b.lra), dynamicsWord(a.lra))));
  }
  const tr = (label, unit, kb, digits = 1) => h('tr', null,
    h('th', { scope: 'row' }, label, h('span', { class: 'muted small' }, ` ${unit}`)),
    h('td', { class: 'num' }, fmtNum(b[kb], digits)),
    h('td', { class: 'num' }, fmtNum(a[kb], digits)),
    h('td', { class: 'num muted' }, b[kb] != null && a[kb] != null ? fmtSigned(a[kb] - b[kb]) : '–'));
  return detailsBlock('Master measurements',
    h('table', { class: 'data' },
      h('thead', null, h('tr', null, h('th', null, ''), h('th', null, 'Original'), h('th', null, 'Master'), h('th', null, 'Change'))),
      h('tbody', null,
        tr('Integrated loudness', 'LUFS', 'lufs'),
        tr('True peak', 'dBTP', 'true_peak_dbtp'),
        tr('Loudness range', 'LU', 'lra'))));
}

/** "MP3 · 128 kbps · 44.1 kHz · stereo" — the source's own format (details). */
export function sourceLine(a) {
  return a ? audioLine({ codec: a.source_audio_codec, kbps: a.source_bitrate_kbps, rate: a.samplerate, channels: a.channels }) : '';
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

/** "Source stream: Opus · 99 kbps · 48 kHz · stereo": what the site delivered (link jobs), or null. */
export function streamNote(r) {
  const line = streamLine(r?.stream);
  return line ? h('p', { class: 'muted small metrics-note' }, `Source stream: ${line}`) : null;
}

/** How the tonal balance moves over the recording (recordings over 3 minutes have a timeline), with its chapters. */
export function toneBlock(r) {
  const chart = toneChart(r?.analysis?.tone_timeline, { markers: r?.markers });
  return chart ? block('Tone over time', chart) : null;
}

export function spectrogramBlock(job, r) {
  return block('Spectrogram',
    h('img', { class: 'spectrogram', src: mediaUrl(job.id, r.spectrogram), alt: 'Spectrogram of the audio', loading: 'lazy' }),
    h('p', { class: 'muted small' }, 'Time runs left to right, frequency bottom to top. Brighter areas have more energy.'));
}

// ---------------------------------------------------------------------------
// Check: the measurement report (label · word · reading),
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
  const pk = peakVerdict(a.true_peak_dbtp, a.peak_limit_dbtp);
  if (a.is_silent) return silentResult(a);
  const cutoff = bw.cutoff_hz;
  const judged = bw.judged !== false;   // pure tones / near-silence: no meaningful cutoff
  const full = judged && (bw.full_range ?? (cutoff != null && cutoff >= 19500));
  const bpm = a.bpm ?? c?.bpm;
  const key = keyText(a.key);
  const conf = a.key?.confidence;
  const verdicts = h('dl', { class: 'verdicts' },
    verdict('Volume', a.loudness_label ?? loudnessWord(a.integrated_lufs), 'How loud it sounds overall', null,
      a.integrated_lufs != null ? `${fmtNum(a.integrated_lufs)} LUFS` : null, true),
    verdict('Peaks', pk.word, pk.note, pk.level,
      a.true_peak_dbtp != null ? `${fmtSigned(a.true_peak_dbtp)} dBTP` : null),
    verdict('Loudness range', a.loudness_range_label ?? dynamicsWord(a.lra), 'Difference between quieter and louder passages', null,
      a.lra != null ? `${fmtNum(a.lra)} LU` : null, true),
    !judged || cutoff == null
      ? verdict('Frequency range', 'Can’t tell', bw.verdict || 'No frequency range estimate', null)
      : verdict('Frequency range', full ? 'Reaches about 20 kHz' : `About ${fmtNum(cutoff / 1000, 1)} kHz`,
        bw.is_lossy_cutoff ? 'Sharp cutoff' : 'Estimated upper end', bw.is_lossy_cutoff ? 'warning' : null,
        `${Math.round(cutoff)} Hz`),
    verdict('Likely content type', c?.label || 'Unknown',
      c ? `${confidenceWords(c.confidence, c.confidence_label)}${(c.reasons || []).length ? ` — ${c.reasons.slice(0, 2).join(', ')}` : ''}` : null,
      null, bpm ? `≈ ${Math.round(bpm)} BPM` : null, true),
    key ? verdict('Key', key, conf != null && conf < 0.4 ? 'rough estimate' : 'estimate', null,
      conf != null ? `${Math.round(conf * 100)} % confidence` : null) : null);
  const out = [detailsBlock('Measurements', verdicts, streamNote(r))];
  const alerts = (a.alerts || []).filter((x) => x.level !== 'ok');
  if (alerts.length) out.push(block('Findings', h('ul', { class: 'findings' }, alerts.map(findingRow))));
  const s = summaryBlock(r, 'Summary');
  if (s) out.push(s);
  const tone = toneBlock(r);
  if (tone) out.push(tone);
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
    fact('Levels measured on', a.level_analysis_basis || a.analysis_basis),
    fact('Spectrum and key measured on', a.analysis_basis),
    fact('Stereo measured on', a.stereo_basis || a.analysis_basis)));
}

/** Nothing to measure: one clear state instead of a list of "–". */
function silentResult(a) {
  const out = [detailsBlock('Measurements',
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
