// New view — shared state and constants. The parts (source card, tasks,
// options, start row) redraw each other through `redraw()` instead of importing in
// a circle: each part registers its renderer with `definePart()`.
import { store } from '../../store.js';
import { fmtBytes } from '../../dom.js';

export const MAX_FILES = 50;
// Light only sets the level, so only these fine-tuning keys apply to it.
export const LIGHT_KEYS = ['target_lufs', 'tp_ceiling_db', 'sample_rate'];
export const RECIPE_LABELS = { club: 'DJ set', live: 'Concert', music: 'Song', voice: 'Podcast / voice' };

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

/** The fine-tuning values that apply to the chosen quality (sent with the job). */
export function appliedAdvanced() {
  const e = state.enhance;
  return Object.entries(e.advanced).filter(([k]) => e.quality !== 'light' || LIGHT_KEYS.includes(k));
}

export const isFileLike = () => state.source?.kind === 'file' || state.source?.kind === 'files'
  || (state.source?.kind === 'job' && state.source.job?.source?.kind === 'file');

/** The source is a link (its audio is the site's lossy stream): one, a playlist, or an earlier job's. */
export const isLinkSource = () => ['url', 'url-error', 'playlist'].includes(state.source?.kind)
  || (state.source?.kind === 'job' && state.source.job?.source?.kind === 'url');

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
