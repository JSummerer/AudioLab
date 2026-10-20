// New view — the start row: one button that says what will happen, the reason
// when it can't start yet, upload progress, and the submit itself (link, playlist
// items, an earlier job's source, one upload, or several uploads one after another).
// A started job opens its page; several jobs open Activity — unless the user has
// moved on to another view meanwhile, then a toast offers the way there. Links go with
// their title and length, so a job started long after the lookup still knows them.
// An earlier job that was removed meanwhile: its link is sent instead, or its file is asked for again.
import { h, icon, clear, shortFolder } from '../../dom.js';
import { api, upload } from '../../api.js';
import { store, upsertJob } from '../../store.js';
import { toast, toastError, meter, setMeter } from '../../ui.js';
import { go, jobPath, settingsPath, currentRoute } from '../../router.js';
import { state, refs, redraw, definePart, isFileLike, appliedAdvanced, tooBig } from './state.js';
import { metaPayload } from './names.js';
import { updateItem, clearSource } from './source.js';
import { taskBlock, downloadChoices, enhanceFormatsInOrder } from './options.js';
import { tracksOptions, tracksProblem } from './tracks.js';
import { coverUpload, coverOptions } from './cover.js';

// Reusing an earlier upload failed because that job (404) or its file (410) is gone: upload again.
const REUPLOAD_STATUS = new Set([404, 410]);

definePart('action', renderAction);

function selectedCount() {
  const src = state.source;
  return src?.kind === 'playlist' ? src.selected.size : src?.kind === 'files' ? src.items.length : 1;
}

function primaryLabel() {
  const kind = state.source?.kind;
  const many = kind === 'playlist' || kind === 'files';
  const n = selectedCount();
  const what = kind === 'files' ? 'files' : 'items';
  switch (state.task) {
    case 'download': {
      const fmt = state.download.format;
      const label = downloadChoices().find((f) => f.key === fmt)?.label || fmt.toUpperCase();
      if (isFileLike()) return many ? `Convert ${n} files to ${label}` : `Convert to ${label}`;
      if (fmt === 'video') return many ? `Save ${n} videos` : 'Save video';
      if (fmt === 'original') return many ? `Save ${n} items` : 'Save original audio';
      return many ? `Save ${n} items as ${label}` : `Save as ${label}`;
    }
    case 'enhance':
      if (state.enhance.quality === 'light') return many ? `Even out ${n} ${what}` : 'Fix the volume';
      return many ? `Enhance ${n} ${what}` : 'Enhance';
    case 'separate': return many ? `Split ${n} ${what}` : 'Split into stems';
    case 'analyze': return many ? `Check ${n} ${what}` : 'Check quality';
    default: return 'Start';
  }
}

function blockedReason() {
  const src = state.source;
  if (!src || src.kind === 'invalid') return 'Paste a link or choose a file first.';
  if (src.kind === 'probing') return 'Looking up the link…';
  if (src.kind === 'playlist' && !src.selected.size) return 'Select at least one item.';
  if (src.kind === 'file' && !src.reuseJobId && tooBig(src.file)) return `This file is ${tooBig(src.file)}.`;
  const block = taskBlock(state.task);
  if (block) return block.reason;
  if (state.task === 'enhance' && !state.enhance.formats.size) return 'Pick at least one format to save.';
  return tracksProblem();
}

function hintText() {
  if (state.task === 'analyze') return state.source?.kind === 'files' ? 'The reports appear in Activity.' : 'Nothing is saved — you get a report.';
  const out = store.health?.output_dir;
  return out ? `Saved to ${shortFolder(out)}` : '';
}

function busyLabel() {
  const b = state.batch;
  if (b) return `${b.reusing ? 'Starting' : 'Uploading'} ${b.index} of ${b.count}…`;
  if (state.uploadPct != null) return `Uploading… ${Math.round(state.uploadPct)} %`;
  return 'Starting…';
}

function renderAction() {
  // Keep keyboard focus in the row when the button is rebuilt (busy → done).
  const hadFocus = refs.action.contains(document.activeElement);
  const el = clear(refs.action);
  const reason = blockedReason();
  const btn = h('button', {
    type: 'button', class: 'btn btn-primary btn-xl', disabled: !!reason || state.busy,
    'aria-describedby': 'actionHint',
  }, state.busy ? busyLabel() : primaryLabel());
  btn.addEventListener('click', submit);
  const why = reason && !state.busy;
  const hint = h('div', { class: ['action-hint', !why && 'is-where'], id: 'actionHint', title: store.health?.output_dir || null }, why ? reason : hintText());
  el.append(btn);
  if (state.batch) {
    el.append(h('button', {
      type: 'button', class: 'btn btn-key', disabled: state.batch.ctl.signal.aborted,
      onclick: () => state.batch?.ctl.abort(),
    }, icon('close', 15), 'Stop'));
  }
  el.append(hint);
  if (hadFocus) (el.querySelector('button:not(:disabled)') || el).focus({ preventScroll: true });
  if (state.busy && state.uploadPct != null) {
    refs.uploadMeter = meter(state.uploadPct, { label: 'Upload' });
    el.append(h('div', { class: 'action-upload' }, refs.uploadMeter));
  }
}

function buildOptions() {
  const o = taskOptions();
  const meta = metaPayload(state.source);
  if (meta) o.meta = meta;
  return Object.assign(o, tracksOptions(), coverOptions(state.source, state.task));
}

function taskOptions() {
  switch (state.task) {
    case 'download':
      return { format: state.download.format, embed_cover: !!state.download.embed_cover };
    case 'enhance': {
      const e = state.enhance;
      // In the order shown: tracks are cut from the first one.
      const o = { recipe: e.recipe, quality: e.quality, formats: enhanceFormatsInOrder(e.formats), embed_cover: !!e.embed_cover };
      const adv = appliedAdvanced();
      if (adv.length) o.advanced = Object.fromEntries(adv);
      return o;
    }
    case 'separate': {
      const s = state.separate;
      return { mode: s.mode, format: s.format, model: s.model };
    }
    default:
      return {};
  }
}

// ---------------------------------------------------------------------------
// Links as `urls` items (docs/API.md "Links"): with what the lookup said about them
// ---------------------------------------------------------------------------
function linkItem(url, { title, duration_s: duration } = {}) {
  return { url, title: title || undefined, duration_s: duration > 0 ? duration : undefined };
}

/** The ticked playlist items, in the playlist's order. */
const playlistItems = (src) => (src.probe.entries || []).filter((e) => src.selected.has(e.url)).map((e) => linkItem(e.url, e));

const jobLink = (job) => [linkItem(job.source.url, job.source)];

// ---------------------------------------------------------------------------
// Where to go once jobs exist: follow them only while New still shows the source
// they came from; otherwise the user moved on, so a toast offers the way there.
// ---------------------------------------------------------------------------
const stillHere = (src) => currentRoute()?.name === 'new' && state.source === src;
const jobsWord = (n) => `${n} job${n === 1 ? '' : 's'}`;

function openStarted(jobs, src) {
  if (jobs.length === 1) {
    if (stillHere(src)) go(jobPath(jobs[0].id));
    else toast(`Started “${jobs[0].title || 'your job'}”.`, { kind: 'ok', action: { label: 'Open', run: () => go(jobPath(jobs[0].id)) } });
  } else if (jobs.length > 1) {
    const follow = stillHere(src);
    toast(`Added ${jobsWord(jobs.length)} to Activity.`, { kind: 'ok', action: follow ? null : { label: 'Open Activity', run: () => go('#/activity') } });
    if (follow) go('#/activity');
  }
}

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------
async function submit() {
  if (blockedReason() || state.busy) return;
  const src = state.source;
  const task = state.task;
  const options = buildOptions();
  if (src.kind === 'files') await submitFiles(src, task, options);
  else await submitOne(src, task, options, coverUpload(src, task));
}

/** Whether the view still shows what was submitted (source, task, options and cover). */
const unchanged = (src, task, options, cover) => state.source === src && state.task === task
  && JSON.stringify(buildOptions()) === JSON.stringify(options) && coverUpload(src, task)?.[0] === cover?.[0];

/** Sends a submit again another way (as an upload, or the link): only as clicked, while the view still shows it. */
function sendAgain(src, task, options, cover) {
  if (!unchanged(src, task, options, cover)) {
    toast('The earlier job is gone, so nothing was started.', { kind: 'warning' });
    return null;
  }
  state.busy = false;
  return submitOne(src, task, options, cover);
}

/** One link, playlist, file or earlier job's source, with the task, options and cover chosen on Start. */
async function submitOne(src, task, options, cover) {
  state.busy = true;
  redraw('action');
  try {
    let res;
    const form = new FormData();
    form.append('task', task);
    form.append('options', JSON.stringify(options));
    if (cover) form.append('cover', ...cover);
    if (src.kind === 'file' && !src.reuseJobId) {
      form.append('file', src.file, src.file.name);
      state.uploadPct = 0;
      redraw('action');
      res = await upload('/api/jobs', form, (f) => {
        state.uploadPct = f * 100;
        if (refs.uploadMeter) setMeter(refs.uploadMeter, state.uploadPct);
        const b = refs.action.querySelector('.btn-primary');
        if (b) b.textContent = `Uploading… ${Math.round(state.uploadPct)} %`;
      });
    } else {
      if (src.kind === 'file') form.append('source_job', src.reuseJobId);
      else if (src.kind === 'job' && !src.jobGone) form.append('source_job', src.job.id);
      else if (src.kind === 'job') form.append('urls', JSON.stringify(jobLink(src.job)));
      else if (src.kind === 'playlist') {
        form.append('urls', JSON.stringify(playlistItems(src)));
        if (src.probe?.title) form.append('playlist_title', src.probe.title);
      } else if (src.probe?.title) form.append('urls', JSON.stringify([linkItem(src.url, src.probe)]));
      else form.append('url', src.url);
      try {
        res = await api('/api/jobs', { method: 'POST', form });
      } catch (e) {
        // The earlier job holding the uploaded file was removed: upload again.
        if (src.kind === 'file' && REUPLOAD_STATUS.has(e.status)) {
          delete src.reuseJobId;
          return await sendAgain(src, task, options, cover);
        }
        // The earlier job (or its file) is gone: a link is sent as such, a file has to be added again.
        if (src.kind === 'job' && !src.jobGone && REUPLOAD_STATUS.has(e.status)) {
          if (src.job.source?.kind === 'url' && src.job.source.url) {
            src.jobGone = true;
            return await sendAgain(src, task, options, cover);
          }
          toast(e.status === 404 ? 'The earlier job is gone — add the file again.' : e.message, { kind: 'warning', timeout: 9000 });
          if (state.source === src) clearSource();
          return;
        }
        throw e;
      }
    }
    const jobs = res?.jobs || [];
    jobs.forEach(upsertJob);
    if (src.kind === 'file' && jobs[0]) {
      // Reusing that job brings its cover along: not once the picture was removed or swapped meanwhile.
      if (cover && src.cover?.file !== cover[0]) delete src.reuseJobId;
      else {
        src.reuseJobId = jobs[0].id;
        src.reuseHasCover = !!cover;
      }
    }
    openStarted(jobs, src);
  } catch (e) {
    if (e.status !== 401) {
      const signIn = e.fix === 'use_cookies' && store.health?.local
        ? { label: 'Use my browser’s sign-in…', run: () => go(settingsPath('cookies'), { focus: false }) } : null;
      toastError(e, { action: signIn, timeout: 9000 });
    }
  } finally {
    state.busy = false;
    state.uploadPct = null;
    refs.uploadMeter = null;
    redraw('action');
  }
}

/** Several files: upload one after another, one job each, with per-file progress. */
async function submitFiles(src, task, options) {
  const items = [...src.items];
  const batch = { ctl: new AbortController(), index: 0, count: items.length };
  state.busy = true;
  state.batch = batch;
  const live = () => state.source === src;
  const refresh = () => { if (live()) { redraw('action'); items.forEach(updateItem); } };
  for (const it of items) { if (it.status !== 'done') { it.status = 'ready'; it.error = null; } }
  refresh();
  let added = 0;
  let failed = 0;
  for (const it of items) {
    if (batch.ctl.signal.aborted) break;
    batch.index += 1;
    batch.reusing = !!it.jobId;
    it.status = 'uploading';
    it.pct = 0;
    refresh();
    try {
      if (!it.jobId && tooBig(it.file)) throw new Error(`This file is ${tooBig(it.file)}.`);
      let res = null;
      if (it.jobId) {
        const form = new FormData();
        form.append('task', task);
        form.append('options', JSON.stringify(options));
        form.append('source_job', it.jobId);
        try {
          res = await api('/api/jobs', { method: 'POST', form, signal: batch.ctl.signal });
        } catch (e) {
          // The earlier job holding the upload was removed: upload again.
          if (!REUPLOAD_STATUS.has(e.status)) throw e;
          it.jobId = null;
        }
      }
      if (!res) {
        const form = new FormData();
        form.append('task', task);
        form.append('options', JSON.stringify(options));
        form.append('file', it.file, it.file.name);
        res = await upload('/api/jobs', form, (f) => {
          it.pct = f * 100;
          if (live()) updateItem(it);
        }, batch.ctl.signal);
      }
      const job = res?.jobs?.[0];
      (res?.jobs || []).forEach(upsertJob);
      it.jobId = job?.id || it.jobId;
      it.status = 'done';
      added += 1;
    } catch (e) {
      if (e.name === 'AbortError') { it.status = it.jobId ? 'done' : 'ready'; break; }
      it.status = 'error';
      it.error = e.hints?.[0] ? `${e.message} ${e.hints[0]}` : e.message;
      failed += 1;
      if (e.status === 401 || e.status === 0) break;
    } finally {
      if (live()) updateItem(it);
    }
  }
  state.busy = false;
  state.batch = null;
  if (live()) redraw();
  else redraw('action'); // another source was loaded meanwhile: still drop "Stop" and re-enable the button
  const toActivity = { label: 'Open Activity', run: () => go('#/activity') };
  if (batch.ctl.signal.aborted) toast(`Stopped. Added ${jobsWord(added)} to Activity.`, { kind: 'info', action: added ? toActivity : null });
  else if (failed) toast(`Added ${jobsWord(added)}. ${failed} file${failed === 1 ? '' : 's'} didn’t work — see the list.`, { kind: 'danger', timeout: 9000 });
  else if (added) {
    const follow = stillHere(src);
    toast(`Added ${jobsWord(added)} to Activity.`, { kind: 'ok', action: follow ? null : toActivity });
    if (follow) go('#/activity');
  }
}
