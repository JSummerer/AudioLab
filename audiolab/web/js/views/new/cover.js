// New view — the cover image on the source card (docs/API.md "Cover image"): pick a
// picture of your own for Save and Enhance, and "Also save the cover as a picture"
// (`save_cover`: the job puts it next to its files — no download, no Save dialog).
// The own picture is sent as the `cover` part of POST /api/jobs and is always
// embedded. One picture dropped anywhere lands here too (setFiles in source.js).
import { h, icon, fmtBytes } from '../../dom.js';
import { toast, switchControl } from '../../ui.js';
import { state, redraw, focusIn } from './state.js';

const MAX_MB = 15;
const TYPES = /^image\/(jpeg|png|webp)$/;
const EXT = /\.(jpe?g|png|webp)$/i;
const COVER_TASKS = ['download', 'enhance']; // the server refuses a cover for the other tasks
const SAVE_TASKS = ['download', 'enhance', 'separate']; // the tasks that write files
const SAVE_KEY = 'audiolab.saveCover';

// "Also save the cover as a picture": the last choice, remembered in this browser (default off).
let saveCover = false;
try { saveCover = localStorage.getItem(SAVE_KEY) === '1'; } catch { /* storage unavailable */ }

function setSaveCover(on) {
  saveCover = on;
  try { localStorage.setItem(SAVE_KEY, on ? '1' : '0'); } catch { /* this page only */ }
}

export const isImageFile = (f) => TYPES.test(f?.type || '') || EXT.test(f?.name || '');

/** One link or one file takes an own cover (playlist items would all get the same one). */
export const takesCover = (src) => ['url', 'file', 'job'].includes(src?.kind);

/** An own picture will be embedded: chosen here, or carried over from the earlier job. */
export const hasOwnCover = (src = state.source) => takesCover(src) && !!(src.cover || src.job?.source?.cover_name);

/** The `save_cover` option for a job of `task` from this source ({} when it doesn't apply). */
export function coverOptions(src, task) {
  return saveCover && takesCover(src) && SAVE_TASKS.includes(task) ? { save_cover: true } : {};
}

/** The picture to send with a job of `task`, as [file, name], or null. */
export function coverUpload(src, task) {
  if (!src?.cover || !takesCover(src) || !COVER_TASKS.includes(task)) return null;
  const f = src.cover.file;
  // The server goes by the extension: a picture without one gets a name that has it.
  return [f, EXT.test(f.name) ? f.name : `cover.${f.type === 'image/jpeg' ? 'jpg' : f.type.slice(6)}`];
}

/** Use `file` as the source's cover. Says why not (toast) and returns false when it can't be one. */
export function setCover(src, file) {
  if (!isImageFile(file)) {
    toast('Use a JPG, PNG or WebP picture as the cover.', { kind: 'warning' });
    return false;
  }
  if (file.size > MAX_MB * 1024 * 1024) {
    toast(`That picture is larger than ${MAX_MB} MB. Use a smaller one.`, { kind: 'warning' });
    return false;
  }
  src.cover = { file, url: URL.createObjectURL(file) };
  redraw('source', 'options');
  return true;
}

function removeCover(src) {
  src.cover = null;
  // Reusing the earlier upload (source_job) would bring its cover along: upload the file again.
  if (src.reuseHasCover) delete src.reuseJobId;
  redraw('source', 'options');
  focusIn('.cover-pick');
}

// The preview's object URL lives until its picture is replaced or removed, or the
// source changes: renderSource() calls this each time.
let previewUrl = null;
export function syncCoverPreview() {
  const live = state.source?.cover?.url || null;
  if (previewUrl && previewUrl !== live) URL.revokeObjectURL(previewUrl);
  previewUrl = live;
}

// ---------------------------------------------------------------------------
// On the card: "Use my own image" (+ preview, Remove) and "Also save the cover as a picture"
// ---------------------------------------------------------------------------
export function coverTools(src) {
  if (!takesCover(src)) return null;
  const input = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp', hidden: true });
  input.addEventListener('change', () => {
    const file = input.files?.[0];
    input.value = '';
    if (file && setCover(src, file)) focusIn('.cover-remove');
  });
  const pick = h('button', { type: 'button', class: 'btn btn-ghost btn-sm cover-pick', onclick: () => input.click() },
    icon('image', 14), src.cover ? 'Use another image' : 'Use my own image');
  const save = h('label', { class: 'details-toggle cover-save' },
    switchControl({ checked: saveCover, onChange: setSaveCover }), 'Also save the cover as a picture');
  const inherited = !src.cover && src.job?.source?.cover_name;
  return [
    h('div', { class: 'cover-tools' }, pick, save, input),
    src.cover ? h('div', { class: 'cover-own' },
      h('img', { class: 'cover-preview', src: src.cover.url, alt: '' }),
      h('div', { class: 'cover-own-text' },
        h('strong', { class: 'small' }, 'Your cover image'),
        h('span', { class: 'muted small cover-own-name', title: src.cover.file.name },
          `${src.cover.file.name} · ${fmtBytes(src.cover.file.size)} · added when saving or enhancing`)),
      h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm cover-remove', 'aria-label': 'Remove your cover image',
        onclick: () => removeCover(src),
      }, 'Remove'))
      : null,
    inherited ? h('p', { class: 'muted small cover-own-note' }, `Uses your cover image from the earlier job (${inherited}).`) : null,
  ];
}
