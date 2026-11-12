// A routed workspace owns at most one audition. Leaving or editing disposes the
// player and any spectrum fetch, and releases the preview job's temporary files.
import { h, clear, clockText, fmtNum } from './dom.js';
import { api } from './api.js';
import { segmented } from './ui.js';
import { comparePlayer } from './compare.js';
import { auditionPcmUrl, loadAuditionSpectrum, spectrumPlot } from './audition-spectrum.js';

const POLL_MS = 550;
const RELEASE_POLLS = 12;
const LABELS = { saved: 'Saved settings (re-rendered)', original: 'Original', enhanced: 'Edited settings' };
const jobPath = (id) => `/api/jobs/${encodeURIComponent(id)}`;
const terminal = (job) => ['done', 'cancelled', 'failed'].includes(job?.status);
const wait = () => new Promise((resolve) => setTimeout(resolve, POLL_MS));

/** Kept separate from the DOM so navigation and submission races can be exercised directly. */
export function auditionRequests(path, { onState, request = api, delay = wait } = {}) {
  let current = null;
  let disposed = false;

  function release(work) {
    if (!work.id || work.releasing) return;
    work.releasing = true;
    const job = jobPath(work.id);
    void (async () => {
      try {
        if (!work.terminal) {
          await request(`${job}/cancel`, { method: 'POST' }).catch(() => {});
          for (let i = 0; i < RELEASE_POLLS; i++) {
            if (terminal(await request(job))) break;
            await delay();
          }
        }
        await request(job, { method: 'DELETE' });
      } catch { /* Standard job retention also removes a preview when the server can't be reached. */ }
    })();
  }

  function reset(message = '') {
    const work = current;
    current = null;
    if (work) {
      work.controller.abort();
      release(work);
    }
    if (!disposed) onState({ message, busy: false });
  }

  async function start(payload) {
    if (disposed || current) return;
    const work = { id: null, terminal: false, releasing: false, controller: new AbortController() };
    current = work;
    onState({ message: 'Starting audition…', busy: true });
    try {
      // Keep the POST alive until its ID is known, even if the view goes away.
      let job = await request(`${path}/preview`, { method: 'POST', json: { ...payload, paired: true } });
      if (!job?.id) throw new Error('The app didn’t return an audition job.');
      work.id = job.id;
      work.terminal = terminal(job);
      let pollFailures = 0;
      while (current === work && !work.terminal) {
        if (job?.id !== work.id) throw new Error('The app returned a different audition job.');
        const pct = Number.isFinite(job.progress) ? ` · ${Math.round(job.progress)}%` : '';
        const position = Number.isInteger(job.queue_position) ? ` · #${job.queue_position} in line` : '';
        onState({
          busy: true,
          message: job.status === 'queued' ? `Audition queued${position}…` : `${job.message || 'Rendering audition…'}${pct}`,
        });
        await delay();
        if (current !== work) break;
        try {
          job = await request(jobPath(work.id), { signal: work.controller.signal });
          pollFailures = 0;
        } catch (e) {
          if (current !== work) break;
          if (e.status !== 0 || ++pollFailures > 3) throw e;
          onState({ busy: true, message: 'Can’t check the audition yet. Trying again…' });
          await delay();
          continue;
        }
        if (job?.id !== work.id) throw new Error('The app returned a different audition job.');
        work.terminal = terminal(job);
      }
      if (current !== work) {
        release(work);
        return;
      }
      if (job.status === 'done') {
        onState({ job, busy: false });
        return;
      }
      const message = job.status === 'cancelled' ? 'Audition cancelled.'
        : [job.error?.message || 'The audition couldn’t be rendered.', job.error?.hints?.[0]].filter(Boolean).join(' ');
      reset(message);
    } catch (e) {
      if (current === work) reset([e.message, e.hints?.[0]].filter(Boolean).join(' '));
    }
  }

  return { start, reset, dispose() { disposed = true; reset(); } };
}

const lufs = (metrics, key) => metrics?.[{ saved: 'saved', original: 'before', enhanced: 'after' }[key]]?.lufs;

/** Reuse the synchronized two-side player, swapping its reference while keeping position. */
export function pairedAudition(job, entry) {
  const r = job.result;
  const ex = r?.excerpt;
  const media = new Set((r?.previews || []).map(({ key }) => key));
  if (!r?.preview || !ex || !(ex.duration_s > 0) || !Number.isFinite(ex.start_s)
    || !Object.keys(LABELS).every((key) => media.has(key))) return null;
  let reference = 'saved';
  let player = null;
  let loading = null;
  let disposed = false;
  const spectra = new Map();
  const transport = h('div');
  const matchNote = h('p', { class: 'small muted' });
  const spectrum = h('div', { class: 'adjust-spectrum', 'aria-busy': 'false' });
  const spectrumStatus = h('p', { class: 'small muted', role: 'status' });
  const spectrumSource = h('select', { class: 'field field-sm' },
    Object.entries(LABELS).map(([key, label]) => h('option', { value: key }, label)));
  spectrumSource.value = 'enhanced';
  const measure = h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: inspect }, 'Measure spectrum');
  const compare = (key) => ({
    name: LABELS[key], label: LABELS[key], url: (offset, frames) => auditionPcmUrl(job.id, key, offset, frames),
  });

  function showPlayer(resume = false) {
    const snapshot = player?.snapshot?.();
    player?.destroy();
    player = comparePlayer({
      title: `Compare ${LABELS[reference].toLowerCase()} with edited settings`,
      sides: [compare(reference), compare('enhanced')],
      songOffset: ex.start_s,
      length: ex.duration_s,
      songDuration: entry.duration_s || entry.source?.duration_s,
      markers: entry.markers,
      loudness: { original_lufs: lufs(r.metrics, reference), result_lufs: lufs(r.metrics, 'enhanced') },
      reductions: r.processing?.applied,
      reductionOffset: ex.start_s,
      showMatchUnavailable: true,
      missing: 'This audition is no longer available, for example after AudioLab restarted. Render it again.',
    });
    clear(transport).append(player.el);
    player.restore?.(snapshot, { resume });
    describeGain();
  }

  function describeGain() {
    const gain = Number.isFinite(r.shared_gain_db) ? `Shared headroom gain ${fmtNum(r.shared_gain_db, 2)} dB.` : '';
    const before = lufs(r.metrics, reference);
    const after = lufs(r.metrics, 'enhanced');
    let match = 'Volume matching is unavailable for this pair.';
    if (Number.isFinite(before) && Number.isFinite(after)) {
      const diff = after - before;
      match = diff === 0 ? 'Volume matching adds 0 dB to each side.'
        : `With Match volume on, ${LABELS[diff > 0 ? 'enhanced' : reference]} is turned down ${fmtNum(Math.abs(diff), 2)} dB.`;
    }
    matchNote.textContent = ['Spectrum uses the audition audio before playback volume matching.', gain, match].filter(Boolean).join(' ');
  }

  const ref = segmented({
    label: 'Reference audio', value: reference, size: 'sm',
    options: [{ value: 'saved', label: LABELS.saved }, { value: 'original', label: LABELS.original }],
    onChange: (next) => { reference = next; showPlayer(true); },
  });

  function showSpectrum() {
    clear(spectrum);
    const data = spectra.get(spectrumSource.value);
    if (!data) return;
    spectrum.append(h('h3', null, LABELS[spectrumSource.value]), spectrumPlot(data));
  }

  spectrumSource.addEventListener('change', () => {
    loading?.abort();
    loading = null;
    measure.disabled = false;
    spectrum.setAttribute('aria-busy', 'false');
    spectrumStatus.textContent = '';
    showSpectrum();
  });

  async function inspect() {
    if (disposed || loading) return;
    const key = spectrumSource.value;
    if (spectra.has(key)) { showSpectrum(); return; }
    const controller = new AbortController();
    loading = controller;
    measure.disabled = true;
    spectrum.setAttribute('aria-busy', 'true');
    spectrumStatus.textContent = 'Reading the audition audio…';
    try {
      const data = await loadAuditionSpectrum(job, key, controller.signal);
      if (disposed || loading !== controller) return;
      spectra.set(key, data);
      showSpectrum();
      spectrumStatus.textContent = 'Spectrum ready.';
    } catch (e) {
      if (!disposed && loading === controller) spectrumStatus.textContent = e.message;
    } finally {
      if (loading === controller) {
        loading = null;
        measure.disabled = false;
        spectrum.setAttribute('aria-busy', 'false');
      }
    }
  }

  showPlayer();
  const el = h('div', { class: 'adjust-audition' },
    h('div', { class: 'adjust-row small' }, h('span', null, 'Reference'), ref),
    transport,
    h('details', { class: 'disclosure' }, h('summary', null, 'Passage spectrum'),
      h('p', { class: 'small muted' }, `Aligned audition from ${clockText(ex.start_s)} to ${clockText(ex.start_s + ex.duration_s)}.`),
      h('div', { class: 'adjust-row' }, h('label', { class: 'adjust-control small' }, h('span', null, 'Audio'), spectrumSource), measure),
      matchNote, spectrumStatus, spectrum));
  return {
    el,
    destroy() {
      disposed = true;
      loading?.abort();
      loading = null;
      spectra.clear();
      player?.destroy();
    },
    pause: () => player?.pause(),
  };
}
