// New view — name overrides (title + artist) on the source card, used for file
// names and tags. Sent as `options.meta` (docs/API.md).
import { h, icon } from '../../dom.js';
import { redraw, focusIn } from './state.js';

function nameDefaults(src) {
  switch (src?.kind) {
    case 'url': return { title: src.probe?.title || '', artist: src.probe?.artist || src.probe?.uploader || '' };
    case 'file': return { title: src.file.name.replace(/\.[^.]+$/, ''), artist: '' };
    case 'job': return { title: src.job.title || '', artist: '' };
    default: return { title: '', artist: '' };
  }
}

/** What the card shows: the user's name where set, else the source's. An emptied artist stays empty. */
export function shownName(src) {
  const d = nameDefaults(src);
  return { title: src.meta?.title || d.title, artist: typeof src.meta?.artist === 'string' ? src.meta.artist : d.artist };
}

/**
 * `options.meta`: only fields the user actually changed (a reused job re-sends its own).
 * `artist: ""` clears the artist; a title can't be emptied (blank = keep the original).
 * An uploaded file sends both once the editor was saved: the server names it by the file's
 * own tags, which the card (file name, no artist) doesn't know, so "unchanged" means nothing.
 */
export function metaPayload(src) {
  if (!src?.meta) return null;
  const d = nameDefaults(src);
  const out = {};
  const title = (src.meta.title || '').trim();
  if (src.kind === 'file') return { title: title || d.title, artist: (src.meta.artist || '').trim() };
  if (title && (src.kind === 'job' || title !== d.title)) out.title = title;
  if (typeof src.meta.artist === 'string') {
    const artist = src.meta.artist.trim();
    // A reused job's source artist is unknown here: send it when set, or when it was erased on purpose.
    if (src.kind === 'job' ? (artist || src.artistCleared) : artist !== d.artist) out.artist = artist;
  }
  return Object.keys(out).length ? out : null;
}

export function nameTools(src) {
  const renamed = !!metaPayload(src);
  return h('div', { class: 'name-tools' },
    h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm name-edit-btn',
      onclick: () => { src.editing = true; redraw('source'); focusIn('#metaTitle', { select: true }); },
    }, icon('pencil', 14), 'Edit name'),
    renamed ? h('span', { class: 'muted small' }, 'Your name is used for the files and tags.') : null,
    renamed && src.kind !== 'job' ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm',
      onclick: () => { src.meta = null; redraw('source', 'action'); focusIn('.name-edit-btn'); },
    }, 'Undo') : null);
}

export function nameEditor(src) {
  const cur = shownName(src);
  const draft = src.draft || cur;
  const title = h('input', { class: 'field field-sm', id: 'metaTitle', value: draft.title, autocomplete: 'off', spellcheck: 'false', maxlength: 200 });
  const artist = h('input', { class: 'field field-sm', id: 'metaArtist', value: draft.artist, autocomplete: 'off', spellcheck: 'false', maxlength: 200, placeholder: 'Unknown' });
  const close = () => { src.editing = false; src.draft = null; redraw('source', 'action'); focusIn('.name-edit-btn'); };
  const save = () => {
    const next = { title: title.value.trim(), artist: artist.value.trim() };
    if (src.kind === 'job') {
      // Compare with what was shown (it may include the earlier job's own name).
      const before = shownName(src);
      if (next.title !== before.title || next.artist !== before.artist) {
        src.artistCleared = !next.artist && (!!before.artist || !!src.artistCleared);
        src.meta = next;
      }
    } else if (src.kind === 'file') {
      src.meta = next; // sent as it is (see metaPayload)
    } else {
      const d = nameDefaults(src);
      const same = (next.title || d.title) === d.title && next.artist === d.artist;
      src.meta = same ? null : next;
    }
    close();
  };
  for (const input of [title, artist]) {
    input.addEventListener('input', () => { src.draft = { title: title.value, artist: artist.value }; });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); save(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    });
  }
  return h('div', { class: 'name-edit' },
    h('div', { class: 'name-fields' },
      h('label', { class: 'name-field', for: 'metaTitle' }, h('span', { class: 'name-field-label' }, 'Title'), title),
      h('label', { class: 'name-field', for: 'metaArtist' }, h('span', { class: 'name-field-label' }, 'Artist'), artist)),
    h('div', { class: 'name-actions' },
      h('button', { type: 'button', class: 'btn btn-key btn-sm', onclick: save }, icon('check', 14), 'Save name'),
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: close }, 'Cancel'),
      h('span', { class: 'muted small' }, 'Used for the file names and the song info (tags).')));
}
