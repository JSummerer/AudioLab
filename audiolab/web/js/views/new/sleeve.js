// New view — the sleeve: what is being made, beside the form (under it in narrow windows).
// The picture (the link's thumbnail, your own cover, or an empty sleeve), the title and
// one line about it, what you get, then the start row (start.js renders into refs.action).
// It follows the source and the choices: redraw() runs it after 'source' and 'action'.
import { h, icon, clear, fmtDuration, fmtBytes } from '../../dom.js';
import { store } from '../../store.js';
import { state, refs, definePart, isFileLike, RECIPE_LABELS } from './state.js';
import { shownName } from './names.js';
import { downloadChoices, enhanceFormatsInOrder } from './options.js';
import { embeddedCoverNote } from './cover.js';
import { TASK_ICON } from '../job/index.js';

definePart('sleeve', renderSleeve);

const TASK_WORD = { download: 'Save', enhance: 'Enhance', separate: 'Split', analyze: 'Check' };
const QUALITY = { light: 'Light', standard: 'Standard', best: 'Best' };
const SPLIT_MODE = { karaoke: 'Vocals + instrumental', stems4: '4 stems' };

const join = (...parts) => parts.filter(Boolean).join(' · ');

/** What is loaded: { title, meta, art (picture URL), iconName, empty, busy }. */
function loaded() {
  const src = state.source;
  const own = src?.cover?.url; // a picture the user added as the cover
  switch (src?.kind) {
    case 'probing': return { title: 'Looking up the link…', meta: src.url, busy: true };
    case 'url-error': return { title: 'Couldn’t read this link', meta: src.url, iconName: 'link' };
    case 'playlist': {
      const n = src.probe?.entries?.length || 0;
      return { title: src.probe?.title || 'Playlist', meta: `${src.selected.size} of ${n} selected`, art: src.probe?.thumbnail, iconName: 'video' };
    }
    case 'files': {
      const total = src.items.reduce((n, it) => n + (it.file.size || 0), 0);
      return { title: `${src.items.length} files`, meta: join(fmtBytes(total), 'one job each'), iconName: 'layers' };
    }
    case 'url': {
      const name = shownName(src);
      return { title: name.title || src.url, meta: join(name.artist, fmtDuration(src.probe?.duration_s)), art: own || src.probe?.thumbnail, iconName: 'video' };
    }
    case 'file': {
      const name = shownName(src);
      return { title: src.meta ? name.title : src.file.name, meta: join(src.meta ? name.artist : null, fmtBytes(src.file.size)), art: own, iconName: 'music' };
    }
    case 'job': {
      const name = shownName(src);
      const j = src.job;
      return { title: name.title, meta: join(name.artist, fmtDuration(j.source?.duration_s)), art: own || j.source?.thumbnail, iconName: 'music' };
    }
    default: return { empty: true };
  }
}

/** What you get, in one or two short lines. */
function plan() {
  switch (state.task) {
    case 'download': {
      const d = state.download;
      const f = downloadChoices().find((c) => c.key === d.format);
      return [join(f?.label || d.format.toUpperCase(), f?.description?.replace(/\.$/, '')),
        embeddedCoverNote([f], d.embed_cover)];
    }
    case 'enhance': {
      const e = state.enhance;
      const all = store.health?.formats?.enhance || [];
      const keys = enhanceFormatsInOrder(e.formats);
      const selected = keys.map((k) => all.find((f) => f.key === k));
      const formats = selected.map((f, i) => f?.label || keys[i]);
      return [join(e.recipe === 'auto' ? 'Type detected' : RECIPE_LABELS[e.recipe], `${QUALITY[e.quality]} quality`),
        formats.join(', ') || null, embeddedCoverNote(selected, e.embed_cover)];
    }
    case 'separate': {
      const s = state.separate;
      const f = (store.health?.formats?.separate || []).find((x) => x.key === s.format);
      return [join(SPLIT_MODE[s.mode], f?.label)];
    }
    case 'analyze': return ['Loudness, peaks, dynamics and real quality'];
    default: return [];
  }
}

function art(info) {
  const box = h('div', { class: ['sleeve-cover', !info.art && 'is-empty'] });
  const fallback = () => clear(box).append(icon(info.iconName || TASK_ICON[state.task] || 'music', 28));
  if (info.art) {
    const img = h('img', { src: info.art, alt: '', decoding: 'async', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', () => { box.classList.add('is-empty'); fallback(); });
    box.append(img);
  } else {
    fallback();
  }
  return h('div', { class: 'sleeve-art', 'aria-hidden': 'true' }, h('span', { class: 'sleeve-record' }), box);
}

function renderSleeve() {
  const el = refs.sleeve;
  if (!el) return;
  const info = loaded();
  const task = state.task === 'download' && isFileLike() ? 'Convert' : TASK_WORD[state.task];
  el.classList.toggle('is-empty', !!info.empty);
  el.classList.toggle('is-busy', !!(info.busy || state.busy));
  clear(el).append(
    art(info),
    h('div', { class: 'sleeve-text' },
      h('p', { class: 'sleeve-eyebrow' }, task),
      h('p', { class: 'sleeve-title' }, info.empty ? 'Nothing loaded yet' : info.title),
      h('p', { class: 'sleeve-meta' }, info.empty ? 'Its cover and name show here.' : info.meta || ''),
      h('ul', { class: 'sleeve-plan' }, plan().filter(Boolean).map((line) => h('li', null, line)))));
}
