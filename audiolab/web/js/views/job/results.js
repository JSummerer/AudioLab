// Job page — results of a finished job, per task. Listening and the files come first: the
// player right under the sleeve (compare, stems, plain), then the files as a tracklist with
// their downloads ("Save cover to folder" adds the cover as one more; a file the user moved
// or deleted is marked, without download or player), then what was done and detected, and
// "do more with this". Players register their cleanup on the page context `ctx`.
import { h, icon, clear, fmtBytes, fmtDuration, confidenceWords, shortFolder } from '../../dom.js';
import { api, url, fileUrl, zipUrl, mediaUrl } from '../../api.js';
import { store, emit, showDetails, upsertJob } from '../../store.js';
import { callout, toast, toastError, block } from '../../ui.js';
import { go, jobPath, settingsPath } from '../../router.js';
import { stemRows, filePlayer, trackRows } from '../../player.js';
import { comparePlayer, libraryCompare } from '../../compare.js';
import { footer, journalBlock } from './status.js';
import {
  summaryBlock, musicFacts, metricsBlock, sourceLine, decisionsBlock, spectrogramBlock, analyzeResult,
} from './readings.js';

export const RECIPE_SHORT = { club: 'DJ set', live: 'Concert', music: 'Song', voice: 'Podcast / voice' };
const isTrack = (f) => f.group === 'tracks';
const exists = (f) => f.exists !== false;
const REVEAL_LABEL = /Mac/i.test(navigator.platform || navigator.userAgent) ? 'Show in Finder' : 'Show in folder';

export function doneView(job, ctx) {
  const r = job.result || {};
  const out = [];
  if (job.task === 'analyze') out.push(...analyzeResult(job, r), warningsCallout(job, r));
  else out.push(listenBlock(job, r, ctx), ...filesBlocks(job, r, ctx), warningsCallout(job, r), ...aboutBlocks(job, r));
  out.push(nextBlock(job));
  out.push(footer(job));
  out.push(journalBlock(job, ctx));
  return out.filter(Boolean);
}

function warningsCallout(job, r) {
  // A silent file's Check already says so in one clear state.
  if (!(r.warnings || []).length || (job.task === 'analyze' && r.analysis?.is_silent)) return null;
  return callout('warning', 'Worth knowing', null, h('ul', { class: 'plain-list' }, r.warnings.map((w) => h('li', null, w))));
}

/** The player right under the sleeve; its heading is for screen readers (the sleeve says what it is). */
function listenBlock(job, r, ctx) {
  if (job.task === 'enhance') return compareBlock(job, r, ctx);
  if (job.task === 'separate') return stemsBlock(job, r, ctx);
  return plainBlock(job, r);
}

function listenSection(title, ...children) {
  return h('section', { class: 'block job-listen' }, h('h2', { class: 'sr-only' }, title), ...children);
}

function aboutBlocks(job, r) {
  if (job.task === 'enhance') return enhanceAbout(job, r);
  // A Save's first line ("Saved as MP3 · 320 kbps — 10.9 MB.") is what the sleeve and the files say already.
  return [summaryBlock(r, 'What we did', { skip: job.task === 'download' ? 1 : 0 })];
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------
function filesBlocks(job, r, ctx) {
  const all = r.files || [];
  const files = all.filter((f) => !isTrack(f));
  const tracks = all.filter(isTrack).sort((a, b) => (a.track ?? 0) - (b.track ?? 0));
  const local = !!store.health?.local;
  const zip = all.filter(exists).length >= 2
    ? h('a', {
      class: ['btn', 'btn-sm', local ? 'btn-ghost' : 'btn-key'], href: zipUrl(job.id),
      download: true, title: 'All files of this job in one ZIP',
    }, icon('archive', 15), 'Download all (ZIP)')
    : null;
  const library = r.library_id
    ? h('a', { class: 'btn btn-ghost btn-sm', href: `#/library/${encodeURIComponent(r.library_id)}` }, icon('arrow', 15), 'Open in Library')
    : null;
  const list = h('ul', { class: 'files' }, files.map((f) => fileRow(job, f)));
  // Where the files are (short: ~/Music/…) and what to do with all of them, under the list.
  const foot = h('div', { class: 'files-foot' },
    r.output_dir ? h('p', { class: 'files-where' }, 'Saved in ', h('span', { title: r.output_dir }, shortFolder(r.output_dir))) : null,
    h('div', { class: 'files-actions' }, local && r.output_dir ? revealButton(job) : null, zip, coverButton(job, r, list), library));
  const out = [];
  if (!all.length) out.push(block('Your files', h('p', { class: 'muted' }, 'No files were produced.'), foot));
  else if (files.length) out.push(h('section', { class: 'block job-files' }, h('h2', { class: 'block-title' }, 'Your files'), list, foot));
  if (tracks.length) {
    const t = trackRows(job.id, tracks.filter(exists));
    const gone = tracks.filter((f) => !exists(f));
    ctx.onCleanup(() => t.destroy());
    out.push(h('details', { class: 'block tracks-block job-files', open: true },
      h('summary', null, h('span', { class: 'block-title' }, `Tracks (${tracks.length})`)),
      job.options?.tracklist || r.tracks_dir
        ? h('p', { class: 'tracks-where' },
          job.options?.tracklist ? 'Cut by your tracklist. ' : null,
          r.tracks_dir ? ['In ', h('span', { title: r.tracks_dir }, shortFolder(r.tracks_dir))] : null)
        : null,
      t.el,
      gone.length ? h('ul', { class: 'files' }, gone.map((f) => fileRow(job, f))) : null,
      // Only tracks (no main file listed): the ZIP + folder buttons live here.
      files.length ? null : foot));
  }
  return out;
}

/** One file with its download — or, moved or deleted since, marked as such (as in the Library). */
function fileRow(job, f) {
  const there = exists(f);
  return h('li', { class: ['file-row', !there && 'is-missing'], dataset: { file: f.id } },
    f.kind === 'cover' ? coverThumb(mediaUrl(job.id, `file:${f.id}`), there)
      : icon(f.kind === 'video' ? 'video' : f.kind === 'report' ? 'file' : 'music', 18, 'file-ico'),
    h('div', { class: 'file-text' },
      h('strong', null, f.label || f.format || f.name),
      // Narrow windows show the size after the name instead of in its own column.
      h('span', { class: 'file-name', title: f.name }, f.name,
        there && f.size_bytes ? h('span', { class: 'file-size-inline' }, ` · ${fmtBytes(f.size_bytes)}`) : null)),
    h('span', { class: 'file-size' }, there ? fmtBytes(f.size_bytes) : ''),
    there
      ? h('a', { class: 'btn btn-ghost btn-sm', href: fileUrl(job.id, f.id), download: f.name, 'aria-label': `Download ${f.label || f.name}` }, icon('download', 14), h('span', { class: 'dl-text' }, 'Download'))
      : h('span', { class: 'lib-missing' }, icon('alert', 14), 'Moved or deleted'));
}

/** A cover file's picture in place of the icon (the icon when it is gone or won't load). */
function coverThumb(src, exists = true) {
  if (!exists) return icon('image', 18, 'file-ico');
  const box = h('span', { class: 'file-thumb' });
  const img = h('img', { src, alt: '', loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', () => clear(box).append(icon('image', 18, 'file-ico')));
  box.append(img);
  return box;
}

async function revealJob(job) {
  try {
    const res = await api(`/api/jobs/${encodeURIComponent(job.id)}/reveal`, { method: 'POST' });
    if (res?.fallback === 'output') toast('That folder was moved or deleted — opened the output folder instead.', { kind: 'info', timeout: 7000 });
  } catch (e) {
    toastError(e);
  }
}

function revealButton(job) {
  return h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => revealJob(job) }, icon('folder', 15), REVEAL_LABEL);
}

/** "Save cover to folder" (POST /api/jobs/{id}/cover) while the job has a cover and no cover file
 * yet: the picture lands next to the other files and joins `list` here. Older results lack
 * `cover_source`: their own image or link thumbnail counts. */
function coverButton(job, r, list) {
  const source = 'cover_source' in r ? r.cover_source : job.source?.cover_name || job.source?.thumbnail;
  const saved = (r.files || []).some((f) => f.kind === 'cover' && f.exists);
  if (!source || saved || !r.output_dir) return null;
  const text = h('span', null, 'Save cover to folder');
  const b = h('button', { type: 'button', class: 'btn btn-ghost btn-sm' }, icon('image', 15), text);
  b.addEventListener('click', async () => {
    if (b.disabled) return;
    b.disabled = true;
    text.textContent = 'Saving the cover…';
    try {
      const f = await api(`/api/jobs/${encodeURIComponent(job.id)}/cover`, { method: 'POST' });
      const row = fileRow(job, f);
      const old = [...list.children].find((li) => li.dataset.file === f.id);
      if (old) old.replaceWith(row); else list.append(row);
      b.remove();
      row.querySelector('a')?.focus({ preventScroll: true }); // the button is gone: onto the new file
      const reveal = store.health?.local ? { label: REVEAL_LABEL, run: () => revealJob(job) } : null;
      toast(`Cover saved to the folder as “${f.name}”.`, { kind: 'ok', action: reveal, timeout: 7000 });
    } catch (e) {
      b.disabled = false;
      text.textContent = 'Save cover to folder';
      if (e.status !== 401) toastError(e);
    }
  });
  return b;
}

function nextBlock(job) {
  const choices = [
    ['enhance', 'sparkle', 'Enhance it'],
    ['separate', 'layers', 'Split it'],
    ['analyze', 'gauge', 'Check it'],
    ['download', 'download', job.source?.kind === 'file' ? 'Convert it' : 'Save it'],
  ].filter(([t]) => t !== job.task);
  // After a Check, enhancing is the one obvious next step.
  return block('Do more with this', h('div', { class: 'next-actions' }, choices.map(([task, ico, label]) => h('button', {
    type: 'button', class: ['btn', job.task === 'analyze' && task === 'enhance' ? 'btn-primary' : 'btn-key', 'btn-sm'],
    onclick: () => emit('use-source', { job, task }),
  }, icon(ico, 15), label))));
}

// ---------------------------------------------------------------------------
// Save / convert
// ---------------------------------------------------------------------------
function plainBlock(job, r) {
  const playable = (r.files || []).find((f) => !isTrack(f) && exists(f) && f.playable !== false && (f.kind === 'audio' || f.kind === 'video'));
  return playable ? listenSection(playable.kind === 'video' ? 'Watch' : 'Listen', filePlayer(job.id, playable)) : null;
}

// ---------------------------------------------------------------------------
// Enhance
// ---------------------------------------------------------------------------
function enhanceAbout(job, r) {
  const out = [summaryBlock(r), classificationBlock(job, r)];
  const m = metricsBlock(r);
  if (m) {
    const src = showDetails() ? sourceLine(r.analysis) : '';
    if (src) m.append(h('p', { class: 'muted small metrics-note' }, `Source: ${src}`));
    out.push(m);
  }
  if (showDetails() && (r.decisions || []).length) out.push(decisionsBlock(r));
  if (showDetails() && r.spectrogram) out.push(spectrogramBlock(job, r));
  return out;
}

/**
 * "Listen and compare": the whole song against its kept original (the Library entry, fetched
 * first and kept on `ctx` for rebuilds of the same result), starting at the excerpt's loud
 * passage; without a kept original the job's own excerpts, in song time. Both in the same
 * compare player. (Open in Library is with the files.)
 */
function compareBlock(job, r, ctx) {
  const excerpts = ['original', 'enhanced'].every((key) => (r.previews || []).some((p) => p.key === key));
  if (!r.library_id && !excerpts) return null;
  const stage = h('div', { class: 'job-player' });
  let player = null;
  let gone = false;
  ctx.onCleanup(() => { gone = true; player?.destroy(); });
  const show = (entry, missing = false) => {
    if (gone) return;
    player = fullCompare(r, entry) || (excerpts ? excerptCompare(job, r, entry, missing) : null);
    if (player) clear(stage).append(player.el);
    else clear(stage).append(h('p', { class: 'muted' }, 'There is nothing to compare: the original wasn’t kept.'));
  };
  if (!r.library_id) show(null, true);
  else if (ctx.entry) show(ctx.entry.value, ctx.entry.missing);
  else {
    api(`/api/library/${encodeURIComponent(r.library_id)}`).then(
      (entry) => { if (!gone) ctx.entry = { value: entry }; show(entry); },
      (e) => {
        if (!gone && e.status === 404) ctx.entry = { value: null, missing: true };
        show(null, e.status === 404);
      });
  }
  return listenSection('Listen and compare', stage);
}

function fullCompare(r, entry) {
  if (!entry?.original?.kept || !entry.compare_file_id) return null;
  const start = r.excerpt?.start_s;
  return libraryCompare(entry, {
    startAt: start || 0,
    note: start ? `Starts at a loud passage (${fmtDuration(start)}) — the whole song is here.` : null,
  });
}

/** The job's own before/after excerpts. Before 2.4 they were volume-matched and had no start. */
function excerptCompare(job, r, entry, missing) {
  const side = (key, label) => ({
    label, url: (offset, frames) => url(`/api/jobs/${encodeURIComponent(job.id)}/pcm/${key}`, { offset, frames }),
  });
  const sides = [side('original', 'Original'), side('enhanced', 'Enhanced')];
  const ex = r.excerpt;
  const song = r.analysis?.duration_s || job.source?.duration_s;
  if (!ex) {
    return comparePlayer({ sides, length: Math.min(45, song || 45), loudness: null, note: '45-second excerpt, volumes matched.' });
  }
  const where = `${fmtDuration(ex.start_s)}–${fmtDuration(ex.start_s + ex.duration_s)}${song ? ` of ${fmtDuration(song)}` : ''}`;
  const why = missing ? ['this result is no longer in the Library.']
    : !entry ? ['the whole song couldn’t be loaded.']
      : entry.original?.kept ? ['the result files were moved or deleted.']
        : ['the original wasn’t kept or was deleted — ', h('a', { href: settingsPath('storage') }, 'Settings → Storage'), '.'];
  return comparePlayer({
    sides, songOffset: ex.start_s, length: ex.duration_s, songDuration: song, startAt: ex.start_s,
    loudness: { original_lufs: r.metrics?.before?.lufs, result_lufs: r.metrics?.after?.lufs },
    note: h('span', null, `Only an excerpt (${where}): `, ...why),
  });
}

function classificationBlock(job, r) {
  const c = r.classification;
  const recipe = r.recipe || {};
  const picked = job.options?.recipe && job.options.recipe !== 'auto';
  const lines = [];
  if (c && c.type && c.type !== 'unknown') {
    lines.push(h('p', { class: 'class-head' },
      'Detected: ', h('strong', null, c.label || c.type),
      h('span', { class: 'muted' }, ` (${confidenceWords(c.confidence, c.confidence_label)})`)));
    if ((c.reasons || []).length) lines.push(h('p', { class: 'reasons' }, `Because: ${c.reasons.join(' · ')}`));
  } else if (c) {
    lines.push(h('p', { class: 'class-head' }, 'We weren’t sure what kind of audio this is, so we used gentle settings.'));
  }
  const facts = musicFacts(r.analysis, c);
  if (facts) lines.push(facts);
  lines.push(h('p', { class: 'muted small' },
    picked ? `You chose the ${recipe.name || RECIPE_SHORT[job.options.recipe] || job.options.recipe} settings.`
      : `Treated with the ${recipe.name || 'automatic'} settings.`));
  const others = (store.health?.recipes?.length ? store.health.recipes : Object.keys(RECIPE_SHORT).map((key) => ({ key, name: RECIPE_SHORT[key] })))
    .filter((x) => x.key !== recipe.key);
  const redo = h('div', { class: 'redo' },
    h('span', { class: 'muted small' }, picked ? 'Try another:' : 'Wrong? Redo as'),
    others.map((x) => h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: (e) => redoAs(job, x, e.currentTarget) }, RECIPE_SHORT[x.key] || x.name)));
  return block('What it is', ...lines, redo);
}

async function redoAs(job, recipe, btn) {
  btn.disabled = true;
  try {
    const form = new FormData();
    form.append('task', 'enhance');
    form.append('options', JSON.stringify({ ...(job.options || {}), recipe: recipe.key }));
    form.append('source_job', job.id);
    const res = await api('/api/jobs', { method: 'POST', form });
    (res?.jobs || []).forEach(upsertJob);
    toast(`Redoing as ${RECIPE_SHORT[recipe.key] || recipe.name}.`, { kind: 'ok' });
    if (res?.jobs?.[0]) go(jobPath(res.jobs[0].id));
  } catch (e) {
    btn.disabled = false;
    toastError(e);
  }
}

// ---------------------------------------------------------------------------
// Split
// ---------------------------------------------------------------------------
function stemsBlock(job, r, ctx) {
  // Not the stems whose file was moved or deleted (their row under "Your files" says so).
  const gone = new Set((r.files || []).filter((f) => !exists(f)).map((f) => f.id));
  const stems = (r.previews || []).filter((p) => p.key.startsWith('stem:') && !gone.has(p.file_id));
  if (!stems.length) return null;
  const s = stemRows(job.id, stems, r.files || []);
  ctx.onCleanup(() => s.destroy());
  return listenSection('Your stems', s.el);
}
