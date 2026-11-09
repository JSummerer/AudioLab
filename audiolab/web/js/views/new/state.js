// New view — shared state and constants. The parts (source card, tasks,
// options, start row) redraw each other through `redraw()` instead of importing in
// a circle: each part registers its renderer with `definePart()`.
import { store } from '../../store.js';
import { fmtBytes } from '../../dom.js';

export const MAX_FILES = 50;
// Volume only sets the level, so only these fine-tuning keys apply to it.
export const LIGHT_KEYS = ['target_lufs', 'tp_ceiling_db', 'sample_rate'];
export const RECIPE_LABELS = { club: 'DJ set', live: 'Concert', music: 'Song', voice: 'Podcast / voice' };
// The labels describe what runs; the API keeps the original quality keys.
export const PROCESSING_LABELS = { light: 'Volume only', standard: 'Adaptive', best: 'AI stems' };

export const TASKS = [
  { key: 'download' }, // Save or Convert: see options.js taskText()
  { key: 'enhance', title: 'Enhance', desc: 'Better sound, automatically' },
  { key: 'separate', title: 'Split', desc: 'Vocals and instruments · AI' },
  { key: 'analyze', title: 'Check', desc: 'Loudness and real quality' },
];

/**
 * source: null | { kind: 'invalid' | 'probing' | 'url-error' | 'url' | 'playlist' | 'file' | 'files' | 'job', … }
 * One link or file also carries what the user added to it: `meta` (names.js), `cover`
 * (cover.js), `tracklist` + `splitTracks` (tracks.js).
 * The per-task option objects survive switching tasks and leaving the view.
 */
export const state = {
  source: null,
  task: 'download',
  download: { format: 'mp3', embed_cover: true },
  enhance: { recipe: 'auto', quality: 'standard', formats: new Set(['mp3_320', 'wav16']), embed_cover: true, advanced: {} },
  separate: { mode: 'karaoke', format: 'wav16', model: 'auto' },
  busy: false,
  uploadPct: null,
  batch: null,
};

/** DOM slots filled by the parts: input, source, tiles, options, action, sleeve, uploadMeter. */
export const refs = {};

const PARTS = ['source', 'tiles', 'options', 'action', 'sleeve'];
const renderers = {};

export function definePart(name, render) { renderers[name] = render; }

/** Redraw the named parts (all of them when none are named). The sleeve sums up the source
 *  and the choices, so it follows 'source' and 'action'. */
export function redraw(...names) {
  for (const name of names.length ? names : PARTS) renderers[name]?.();
  if (names.length && !names.includes('sleeve') && (names.includes('source') || names.includes('action'))) renderers.sleeve?.();
}

// The formats last started per task, kept in this browser with the Settings default they were
// chosen over: once Settings → Defaults change, those apply again.
const FORMATS_KEY = 'audiolab.formats';
const DEFAULT_SETTING = { download: 'download_format', enhance: 'enhance_formats', separate: 'separate_format' };
const settingDefault = (task) => JSON.stringify(store.settings?.[DEFAULT_SETTING[task]] ?? null);

export function rememberFormats(task, options) {
  const value = task === 'enhance' ? options.formats : options.format;
  if (!DEFAULT_SETTING[task] || value == null) return;
  try {
    const all = JSON.parse(localStorage.getItem(FORMATS_KEY) || '{}') || {};
    all[task] = { value, base: settingDefault(task) };
    localStorage.setItem(FORMATS_KEY, JSON.stringify(all));
  } catch { /* this page only */ }
}

/** The formats last started for `task` (a key, or keys for Enhance), unless Settings → Defaults changed since. */
export function rememberedFormats(task) {
  try {
    const last = JSON.parse(localStorage.getItem(FORMATS_KEY) || '{}')?.[task];
    return last && last.base === settingDefault(task) ? last.value : null;
  } catch { return null; }
}

/** The fine-tuning values that apply to the chosen processing mode (sent with the job). */
export function appliedAdvanced() {
  const e = state.enhance;
  return Object.entries(e.advanced).filter(([k]) => e.quality !== 'light' || LIGHT_KEYS.includes(k));
}

export const isFileLike = () => state.source?.kind === 'file' || state.source?.kind === 'files'
  || (state.source?.kind === 'job' && (!!state.source.sourceFile || state.source.job?.source?.kind === 'file'));

/** The source is a link (its audio is the site's lossy stream): one, a playlist, or an earlier job's. */
export const isLinkSource = () => ['url', 'url-error', 'playlist'].includes(state.source?.kind)
  || (state.source?.kind === 'job' && !state.source.sourceFile && state.source.job?.source?.kind === 'url');

/** A file the server accepts as audio or video: by its extension, from Health (loaded before the views mount). */
export function isMediaFile(f) {
  const name = (f?.name || '').toLowerCase();
  const dot = name.lastIndexOf('.');
  return dot > 0 && (store.health?.upload_extensions || []).includes(name.slice(dot));
}

/** Why `file` can't be uploaded because of its size ("larger than the 4 GB limit"), or null. */
export function tooBig(file) {
  const mb = store.health?.limits?.max_upload_mb;
  return mb && file?.size > mb * 1024 * 1024 ? `larger than the ${fmtBytes(mb * 1024 * 1024)} limit` : null;
}

export function focusIn(selector, { select = false } = {}) {
  setTimeout(() => {
    const el = refs.source?.querySelector(selector);
    el?.focus({ preventScroll: true });
    if (select) el?.select?.();
  }, 0);
}

export function focusStart() {
  refs.action?.querySelector('.btn-primary')?.focus({ preventScroll: true });
}
