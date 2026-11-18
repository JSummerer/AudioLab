// New view — name overrides (title + artist) under More options, used for file
// names and tags. Sent as `options.meta` (docs/API.md). The source card shows the name.
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

/** Only explicit overrides travel with the job. Missing fields keep the source tags. */
export function metaPayload(src) {
  return src?.meta && Object.keys(src.meta).length ? src.meta : null;
}

/** The Name row of More options: "Edit name" (and Undo once renamed), or the editor while editing. */
export function nameTools(src) {
  if (src.editing) return nameEditor(src);
  const renamed = !!metaPayload(src);
  return h('div', { class: 'name-tools' },
    h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm name-edit-btn',
      onclick: () => { src.editing = true; redraw('options'); focusIn('#metaTitle', { select: true }); },
    }, icon('pencil', 14), 'Edit name'),
    renamed && src.kind !== 'job' ? h('button', {
      type: 'button', class: 'btn btn-ghost btn-sm',
      onclick: () => { src.meta = null; redraw('source', 'options', 'action'); focusIn('.name-edit-btn'); },
    }, 'Undo') : null,
    h('p', { class: 'muted small name-note' }, renamed ? 'Your name is used for the files and the song info (tags).'
      : 'Used for the file names and the song info (tags).'));
}

export function nameEditor(src) {
  const cur = shownName(src);
  const draft = src.draft || cur;
  const title = h('input', { class: 'field field-sm', id: 'metaTitle', value: draft.title, autocomplete: 'off', spellcheck: 'false', maxlength: 200 });
  const unknownArtist = src.kind === 'file' || src.kind === 'job';
  let artistEdited = !!draft.artistEdited;
  const artist = h('input', { class: 'field field-sm', id: 'metaArtist', value: draft.artist, autocomplete: 'off', spellcheck: 'false', maxlength: 200,
    placeholder: unknownArtist ? 'Keep source artist' : 'Unknown' });
  const rememberDraft = () => { src.draft = { title: title.value, artist: artist.value, artistEdited }; };
  const updatePlaceholder = () => {
    if (unknownArtist) artist.placeholder = artistEdited || src.meta?.artist === '' ? 'Artist will be cleared' : 'Keep source artist';
  };
  updatePlaceholder();
  const close = () => { src.editing = false; src.draft = null; redraw('source', 'options', 'action'); focusIn('.name-edit-btn'); };
  const save = () => {
    const next = { ...src.meta };
    const defaults = nameDefaults(src);
    const t = title.value.trim();
    const a = artist.value.trim();
    if (t !== cur.title) {
      if (t && t !== defaults.title) next.title = t;
      else delete next.title;
    }
    if (a !== cur.artist || artistEdited) {
      if (unknownArtist || a !== defaults.artist) next.artist = a;
      else delete next.artist;
    }
    src.meta = Object.keys(next).length ? next : null;
    close();
  };
  const clearArtist = unknownArtist ? h('button', {
    type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => {
      artist.value = '';
      artistEdited = true;
      updatePlaceholder();
      rememberDraft();
      artist.focus();
    },
  }, 'Clear artist tag') : null;
  for (const input of [title, artist]) {
    input.addEventListener('input', () => {
      if (input === artist) artistEdited = true;
      updatePlaceholder();
      rememberDraft();
    });
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
      clearArtist,
      h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: close }, 'Cancel')));
}
