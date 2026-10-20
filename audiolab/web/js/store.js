// App state + a tiny event bus + the live connection to the server.
import { api, url } from './api.js';

// ---------------------------------------------------------------------------
// Bus. Topics: health, settings, jobs (payload: ids | null), live,
// use-source ({job, task, again}: start something new from an earlier job's source;
// `again`: "Change and start again", with that job's task and options)
// ---------------------------------------------------------------------------
const topics = new Map();

export function on(topic, fn) {
  if (!topics.has(topic)) topics.set(topic, new Set());
  topics.get(topic).add(fn);
  return () => topics.get(topic)?.delete(fn);
}

export function emit(topic, payload) {
  for (const fn of topics.get(topic) || []) {
    try { fn(payload); } catch (e) { console.error(`[${topic}]`, e); }
  }
}

export const store = {
  health: null,
  settings: null,
  jobs: new Map(),
  jobsLoaded: false, // the first GET /api/jobs snapshot has arrived
  live: 'connecting', // connecting | live | reconnecting | polling
};

export const appName = () => store.health?.app || 'AudioLab';
export const aiAvailable = () => !!store.health?.ai?.available;

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------
let pendingIds = new Set();
let flushTimer = 0;

function scheduleJobsEmit(id) {
  if (id) pendingIds.add(id);
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    flushTimer = 0;
    const ids = [...pendingIds];
    pendingIds = new Set();
    emit('jobs', ids);
  }, 60);
}

export function upsertJob(job) {
  if (!job || !job.id) return;
  // Deleted IDs are not reused: a delayed action response or event cannot revive one.
  if (removedIds.has(job.id)) return;
  // An older copy (a cancel's answer arriving after the live "cancelled" event) never wins.
  const cur = store.jobs.get(job.id);
  if (cur && (job.updated_at || 0) < (cur.updated_at || 0)) return;
  store.jobs.set(job.id, job);
  if (snapshotsInFlight) buffered.push(['job', job]);
  scheduleJobsEmit(job.id);
}

export function removeJobs(ids) {
  for (const id of ids) {
    store.jobs.delete(id);
    removedIds.add(id);
  }
  if (snapshotsInFlight) buffered.push(['removed', [...ids]]);
  emit('jobs', ids);
}

// A single-job GET can answer after the job was deleted elsewhere. Its absence from
// the store alone doesn't allow inserting it: not if it was removed, or if a jobs
// snapshot requested after that GET no longer had it.
const removedIds = new Set();
let snapshotsRequested = 0;
let newestSnapshotApplied = 0;

/** Take before fetching one job; pass to mayInsertJob() with the response. */
export const jobsFence = () => snapshotsRequested;

export function mayInsertJob(id, fence) {
  return !store.jobs.has(id) && !removedIds.has(id) && newestSnapshotApplied <= fence;
}

export function jobsSorted() {
  return [...store.jobs.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
}

export const isActive = (job) => job.status === 'running' || job.status === 'queued';
export const isFinished = (job) => ['done', 'failed', 'cancelled'].includes(job.status);
export const activeCount = () => [...store.jobs.values()].filter(isActive).length;

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------
let healthTimer = 0;

export async function loadHealth() {
  store.health = await api('/api/health');
  emit('health');
  clearTimeout(healthTimer);
  // While the AI pack installs, keep health fresh even if SSE is down.
  if (store.health?.ai?.installing) healthTimer = setTimeout(() => loadHealth().catch(() => {}), 3000);
}

// Replay only mutations accepted after each snapshot was requested. A fresh
// snapshot must still be able to remove jobs that disappeared on the server.
let snapshotsInFlight = 0;
let buffered = [];

export async function loadJobs() {
  snapshotsInFlight += 1;
  const seq = ++snapshotsRequested;
  const start = buffered.length;
  try {
    const data = await api('/api/jobs');
    if (seq < newestSnapshotApplied) return;
    store.jobs = new Map((data?.jobs || []).map((j) => [j.id, j]));
    newestSnapshotApplied = seq;
    for (const [kind, payload] of buffered.slice(start)) {
      if (kind === 'job') {
        // Only replay events newer than what the snapshot already contains.
        const cur = store.jobs.get(payload.id);
        if (!cur || (payload.updated_at || 0) >= (cur.updated_at || 0)) store.jobs.set(payload.id, payload);
      }
      else for (const id of payload) store.jobs.delete(id);
    }
    store.jobsLoaded = true;
    emit('jobs', null);
  } finally {
    snapshotsInFlight -= 1;
    if (!snapshotsInFlight) buffered = [];
  }
}

// Keep every choice visible immediately, but send settings requests in invocation
// order. Settling one save removes only its own patch from the optimistic state.
let confirmedSettings = null;
const pendingSettings = [];
let settingsQueue = Promise.resolve();

function publishSettings() {
  store.settings = pendingSettings.reduce((settings, patch) => ({ ...settings, ...patch }), confirmedSettings);
  emit('settings');
}

function queueSettings(request) {
  const result = settingsQueue.then(request);
  settingsQueue = result.catch(() => {});
  return result;
}

export function loadSettings() {
  return queueSettings(async () => {
    confirmedSettings = await api('/api/settings');
    publishSettings();
  });
}

export function saveSettings(patch) {
  if (!pendingSettings.length) confirmedSettings = store.settings;
  patch = { ...patch };
  pendingSettings.push(patch);
  const result = queueSettings(async () => {
    try {
      confirmedSettings = await api('/api/settings', { method: 'PUT', json: patch });
      return confirmedSettings;
    } finally {
      pendingSettings.shift();
      publishSettings();
    }
  });
  publishSettings();
  return result;
}

// ---------------------------------------------------------------------------
// Folded parts ("More options" per task, "Show details" on job pages). Settings →
// "Show all options by default" (stored as ui_mode) decides how they start; what
// the user opens or closes is remembered in this browser, one entry per place.
// ---------------------------------------------------------------------------
const SHOWN_KEY = 'audiolab.ui';
let shown = null; // this page's copy, so a choice sticks even when storage is unavailable

function shownMap() {
  if (!shown) {
    try {
      const v = JSON.parse(localStorage.getItem(SHOWN_KEY) || '{}');
      shown = v && typeof v === 'object' ? v : {};
    } catch { shown = {}; }
  }
  return shown;
}

export const showAllByDefault = () => store.settings?.ui_mode === 'advanced';

/** Whether a folded part ('more.enhance', 'details') is open: remembered, else the default. */
export function isShown(key) {
  const v = shownMap()[key];
  return typeof v === 'boolean' ? v : showAllByDefault();
}

/** Job pages: numbers, decisions and the raw log details instead of plain words only. */
export const showDetails = () => isShown('details');

export function setShown(key, open) {
  shownMap()[key] = !!open;
  try { localStorage.setItem(SHOWN_KEY, JSON.stringify(shown)); } catch { /* this page only */ }
}

/** Forget every remembered choice, so the Settings default applies everywhere again. */
export function forgetShown() {
  shown = {};
  try { localStorage.removeItem(SHOWN_KEY); } catch { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Live updates: SSE with reconnect + polling fallback
// ---------------------------------------------------------------------------
let source = null;
let pollTimer = 0;
let failures = 0;
let reconnectTimer = 0;

function setLive(state) {
  if (store.live === state) return;
  store.live = state;
  emit('live', state);
}

function startPolling() {
  if (pollTimer) return;
  setLive('polling');
  pollTimer = setInterval(() => {
    loadJobs().catch(() => {});
    if (store.health?.ai?.installing) loadHealth().catch(() => {});
  }, 3000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = 0;
}

export function connectEvents() {
  clearTimeout(reconnectTimer);
  if (source) source.close();
  if (typeof EventSource === 'undefined') { startPolling(); return; }
  source = new EventSource(url('/api/events'));
  source.addEventListener('open', () => {
    // Always reconcile: anything that changed between the last snapshot and
    // this subscription would otherwise be missed.
    loadJobs().catch(() => {});
    failures = 0;
    stopPolling();
    setLive('live');
  });
  source.addEventListener('job', (e) => {
    try { upsertJob(JSON.parse(e.data)); } catch { /* ignore malformed */ }
  });
  source.addEventListener('removed', (e) => {
    try { removeJobs(JSON.parse(e.data).ids || []); } catch { /* ignore */ }
  });
  source.addEventListener('system', (e) => {
    try {
      const d = JSON.parse(e.data);
      store.health = { ...(store.health || {}), ...d };
      emit('health');
    } catch { /* ignore */ }
  });
  source.addEventListener('error', () => {
    failures += 1;
    setLive('reconnecting');
    if (failures >= 2) startPolling();
    if (source.readyState === EventSource.CLOSED) {
      reconnectTimer = setTimeout(connectEvents, Math.min(15000, 2000 * failures));
    }
  });
}

// ---------------------------------------------------------------------------
// Labels shared across views
// ---------------------------------------------------------------------------
export function taskLabel(job) {
  switch (job?.task) {
    case 'download': return job?.source?.kind === 'file' ? 'Convert' : 'Save';
    case 'enhance': return 'Enhance';
    case 'separate': return 'Split';
    case 'analyze': return 'Check';
    default: return job?.task || 'Job';
  }
}

/**
 * What a format's (i) card says: the server's facts (Health `formats[].facts`, the one
 * source of this text). `coverOn`: a cover would be embedded now — then a format that
 * can't hold one says so prominently instead of in passing.
 */
export function formatInfo(f, coverOn = false) {
  const noCover = coverOn && f.cover === false;
  return {
    title: f.label,
    lines: (f.facts || []).filter((t) => !(noCover && /^no cover image/i.test(t))),
    note: noCover ? 'No cover art in this format.' : null,
  };
}

export function deviceLabel(device) {
  return { mps: 'Apple GPU', cuda: 'NVIDIA GPU', cpu: 'CPU' }[device] || device || '';
}
