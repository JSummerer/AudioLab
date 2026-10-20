// New (#/new): source → task → options → one button. The parts live next to this
// file (source, names, options, start) and share ./state.js. What is loaded here
// stays when the user visits other views; paste-anywhere, drag & drop (files and
// links), deep links, "do more with this" and "change and start again" all land here
// through the functions exported below.
import { h, icon } from '../../dom.js';
import { store, on } from '../../store.js';
import { closeInfo } from '../../ui.js';
import { state, refs, redraw, TASKS } from './state.js';
import { loadUrl, setFiles, useJobSource, normalizeUrl } from './source.js';
import './options.js';
import './start.js';
import './sleeve.js';

export { loadUrl, setFiles, useJobSource, normalizeUrl };

let lastDefaults = {};

export const newView = {
  el: null,
  heading: null,

  mount(el) {
    this.el = el;
    const input = h('input', {
      id: 'urlInput', class: 'source-input', type: 'text', inputmode: 'url', autocomplete: 'off',
      autocapitalize: 'off', spellcheck: 'false', placeholder: 'Paste an audio or video link…',
      'aria-label': 'Link to a video, song or playlist',
    });
    const form = h('form', { class: 'source-form', novalidate: true },
      h('div', { class: 'source-field' },
        icon('link', 20, 'source-field-ico'),
        input,
        h('button', { type: 'submit', class: 'btn btn-key' }, 'Load')));
    form.addEventListener('submit', (e) => { e.preventDefault(); loadUrl(input.value); });
    input.addEventListener('paste', () => setTimeout(() => { if (input.value.trim()) loadUrl(input.value); }, 0));
    // A link dropped on the field replaces what's in it and loads (other text is inserted as usual).
    input.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      const link = `${dt?.getData('text/uri-list') || ''}\n${dt?.getData('text/plain') || ''}`
        .split(/\r?\n/).map((l) => l.trim()).find((l) => l && !l.startsWith('#'));
      if (!link || /\s/.test(link) || !/^https?:\/\//i.test(link) || !normalizeUrl(link)) return;
      e.preventDefault();
      loadUrl(link);
    });

    Object.assign(refs, {
      input,
      source: h('div', { class: 'source-slot', 'aria-live': 'polite' }),
      tiles: h('div', { class: 'tiles', role: 'group', 'aria-label': 'What do you want to do?' }),
      options: h('div', { class: 'options' }),
      action: h('div', { class: 'action-row', tabindex: '-1' }),
      sleeve: h('div', { class: 'sleeve' }),
    });
    this.heading = h('h1', { class: 'page-title', id: 'newTitle', tabindex: '-1' }, 'Paste a link or drop a file.');

    // The form sits on the sheet; the page title already says what the link field is for. Beside
    // it (under it when narrow), the sleeve: what is being made, and the start row.
    el.append(h('div', { class: 'page page-new' },
      h('header', { class: 'page-head' },
        h('div', { class: 'page-head-text' },
          this.heading,
          h('p', { class: 'page-sub' }, 'Save it, make it sound better, split it into stems, or check how good it really is.'))),
      h('div', { class: 'create' },
        h('div', { class: 'create-main' },
          h('section', { class: 'step', 'aria-label': 'Source' },
            form,
            h('div', { class: 'source-alt' },
              h('button', { type: 'button', class: 'btn btn-ghost', onclick: () => document.getElementById('fileInput').click() },
                icon('upload', 16), 'Choose files'),
              h('span', { class: 'source-alt-note' }, 'or drop them anywhere on this window')),
            refs.source),
          h('section', { class: 'step' },
            h('h2', { class: 'step-label' }, 'What do you want to do?'),
            refs.tiles),
          refs.options),
        h('aside', { class: 'sleeve-panel', 'aria-label': 'What you get' }, refs.sleeve, refs.action))));

    on('health', () => redraw());
    on('settings', () => { applyDefaults(); redraw(); });
    applyDefaults();
    redraw();
  },

  show() {},
  hide() { closeInfo(); },
};

/** Pre-select a task (deep links). Unknown keys are ignored. */
export function setTask(task) {
  if (!TASKS.some((t) => t.key === task) || state.task === task) return;
  state.task = task;
  redraw();
}

/** Bring the link field into view and focus it (after a paste anywhere). */
export function focusLinkField() {
  refs.input?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  refs.input?.focus({ preventScroll: true });
}

// Settings → Defaults feed the options, but only when they change (a pick made
// here survives unrelated settings saves).
function applyDefaults() {
  const s = store.settings;
  if (!s) return;
  const next = {
    download_format: s.download_format,
    enhance_formats: (s.enhance_formats || []).join(','),
    separate_format: s.separate_format,
    embed_cover: s.embed_cover,
  };
  if (next.download_format && next.download_format !== lastDefaults.download_format) state.download.format = next.download_format;
  if (next.enhance_formats && next.enhance_formats !== lastDefaults.enhance_formats) state.enhance.formats = new Set(s.enhance_formats);
  if (next.separate_format && next.separate_format !== lastDefaults.separate_format) state.separate.format = next.separate_format;
  if (next.embed_cover != null && next.embed_cover !== lastDefaults.embed_cover) {
    state.download.embed_cover = !!next.embed_cover;
    state.enhance.embed_cover = !!next.embed_cover;
  }
  lastDefaults = next;
}
