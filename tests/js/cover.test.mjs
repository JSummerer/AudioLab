// Execute the production New-view modules with a small DOM, not a personal browser.
// These checks cover content and option behavior; they make no layout or player-display claims.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { fmtDuration } from '../../audiolab/web/js/dom.js';
import { PROCESSING_LABELS, LIGHT_KEYS } from '../../audiolab/web/js/views/new/state.js';
import { parseEq } from '../../audiolab/web/js/eq.js';
import { checkSections } from '../../audiolab/web/js/section-eq.js';

class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag;
    Object.assign(this, attrs);
    this.children = children.flat(Infinity).filter((c) => c != null);
  }
  addEventListener() {}
  setAttribute(key, value) { this[key] = value; }
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ');
  return node instanceof Element ? node.textContent ?? node.children.map(text).join(' ') : String(node ?? '');
}
const mp3 = { key: 'mp3', label: 'MP3', description: 'For everyday use.', cover: true };
const wav = { key: 'wav', label: 'WAV', description: 'For editing.', cover: false };
const original = { key: 'original', label: 'Original', cover: false };

function setup({ crlf = false } = {}) {
  const redrawn = [];
  const requests = [];
  const source = { kind: 'file', file: { name: 'plain.wav', size: 32 } };
  const state = {
    source, task: 'download', download: { format: 'mp3', embed_cover: true },
    enhance: { formats: new Set(['mp3_320', 'wav16']), embed_cover: true, recipe: 'auto', quality: 'standard', advanced: {} },
  };
  const store = { health: { formats: {
    download: [mp3, wav, original], enhance: [{ ...mp3, key: 'mp3_320' }, { ...wav, key: 'wav16' }],
  } } };
  const context = vm.createContext({
    state, store, redrawn, PROCESSING_LABELS,
    refs: { input: { value: '' } }, AbortController, setTimeout, fmtDuration,
    focusStart() {},
    api: async (path, { json }) => { requests.push({ path, json }); },
    h: (tag, attrs, ...children) => new Element(tag, attrs || {}, children),
    icon: () => '', fmtBytes: (size) => `${size} B`,
    localStorage: { getItem: () => null, setItem() {} },
    switchControl: (attrs) => new Element('input', attrs),
    definePart() {}, redraw: (...parts) => redrawn.push(parts),
    isFileLike: () => state.source?.kind === 'file' || state.source?.job?.source?.kind === 'file',
    parseEq, checkSections, // state.js's appliedAdvanced, for this sandbox's state:
    appliedAdvanced: () => Object.entries(state.enhance.advanced)
      .filter(([k]) => state.enhance.quality !== 'light' || LIGHT_KEYS.includes(k)),
  });
  for (const name of ['cover', 'names', 'tracks', 'options', 'source', 'sleeve']) {
    const path = new URL(`../../audiolab/web/js/views/new/${name}.js`, import.meta.url);
    const original = readFileSync(path, 'utf8');
    const checkout = crlf ? original.replace(/\r?\n/g, '\r\n') : original;
    const source = checkout.replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
    vm.runInContext(source, context, { filename: path.pathname });
  }
  return { state, store, context, redrawn, requests, run: (code) => vm.runInContext(code, context) };
}

test('the separate image is off by default and does not control embedding', () => {
  const s = setup();
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('coverOptions(state.source, "download")'))), {});
  const tools = s.run('coverTools(state.source)');
  assert.match(text(tools), /Also save a separate image/);
  assert.match(text(tools), /artwork inside supported audio files/);
  assert.equal(tools[0].children[1].children[0].checked, false);
  tools[0].children[1].children[0].onChange(true);
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('coverOptions(state.source, "download")'))), { save_cover: true });
  assert.equal(s.state.download.embed_cover, true);
});

test('a plain source summary requests available art without claiming that it has a picture', () => {
  const s = setup();
  assert.equal(s.run('plan()[1]'), 'Cover art inside audio when available');
  s.state.download.embed_cover = false;
  assert.equal(s.run('plan()[1]'), null);
});

test('an explicit or reused own image is summarized despite embed_cover being off', () => {
  const s = setup();
  s.state.download.embed_cover = false;
  s.state.source.cover = { file: { name: 'own.png', size: 24 }, url: 'blob:synthetic' };
  assert.equal(s.run('plan()[1]'), 'Your image inside supported audio files');
  s.state.source = { kind: 'job', job: { source: { cover_name: 'own.png' } } };
  assert.equal(s.run('plan()[1]'), 'Your image inside supported audio files');
});

test('unsupported and unknown download formats never promise embedded artwork', () => {
  const s = setup();
  s.state.source.cover = {};
  for (const format of ['wav', 'original', 'video', 'unavailable']) {
    s.state.download.format = format;
    assert.equal(s.run('plan()[1]'), null, format);
  }
});

test('Enhance scopes own artwork to supported outputs and respects the flag without an own image', () => {
  const s = setup();
  s.state.task = 'enhance';
  s.state.enhance.embed_cover = false;
  s.state.source.cover = {};
  assert.equal(s.run('plan()[2]'), 'Your image inside supported audio files');
  s.state.enhance.formats = new Set(['wav16']);
  assert.equal(s.run('plan()[2]'), null);
  s.state.enhance.formats = new Set(['mp3_320']);
  delete s.state.source.cover;
  assert.equal(s.run('plan()[2]'), null);
  s.state.enhance.embed_cover = true;
  assert.equal(s.run('plan()[2]'), 'Cover art inside audio when available');
});

test('the Enhance summary counts fine-tuning changes, with unreadable EQ text, and only what Volume only uses', () => {
  const s = setup();
  s.state.task = 'enhance';
  const tuned = () => s.run('plan()[3]');
  assert.equal(tuned(), null);
  s.state.enhance.advanced = { target_lufs: -12, glue: 'on' };
  assert.equal(tuned(), 'Fine-tuning: 2 changes');
  s.state.enhance.eqText = '160 Hz -3 dB';
  assert.equal(tuned(), 'Fine-tuning: 3 changes', 'unreadable EQ text counts as a change');
  s.state.enhance.sectionRows = [{ from: '1:00', to: '1:05', fade: '', text: '' }];
  assert.equal(tuned(), 'Fine-tuning: 4 changes', 'an unfinished section counts too');
  assert.match(s.run('sectionProblem()'), /^Fix the section EQ first: 1 problem, listed under it\.$/);
  s.state.enhance.quality = 'light';
  assert.equal(tuned(), 'Fine-tuning: 1 change', 'Volume only uses the loudness target, not compression or EQ');
  assert.equal(s.run('sectionProblem()'), null);
});

test('own-image help names supported outputs and never promises art in an unsupported output', () => {
  const s = setup();
  s.state.source.cover = {};
  s.context.chosen = [wav];
  const row = s.run('coverRow(state.download, () => chosen)');
  assert.equal(row.children[0].checked, true);
  assert.equal(row.children[0].disabled, true);
  assert.match(text(row), /Choose a supported audio format to embed your image/);
  assert.match(text(row), /AudioLab adds no cover art to WAV/);
  assert.doesNotMatch(text(row), /always added|image is embedded in WAV/);
  s.context.chosen = [mp3, wav];
  s.run('syncCover()');
  assert.match(text(row), /Your image is embedded in MP3/);
  assert.match(text(row), /AudioLab adds no cover art to WAV/);
});

test('the embedding switch refreshes the sleeve without changing the sidecar preference', () => {
  const s = setup();
  s.context.chosen = [mp3];
  const row = s.run('coverRow(state.download, () => chosen)');
  assert.match(text(row), /Embed cover art/);
  row.children[0].onChange(false);
  assert.equal(s.state.download.embed_cover, false);
  assert.deepEqual(s.redrawn.at(-1), ['sleeve']);
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('coverOptions(state.source, "download")'))), {});
});

test('cover controls and summaries also execute with Windows checkout line endings', () => {
  const s = setup({ crlf: true });
  assert.equal(s.run('plan()[1]'), 'Cover art inside audio when available');
});

test('checking an export uses its file metadata and reusing the original still looks up the link', () => {
  const s = setup();
  const url = 'https://example.com/recording';
  s.context.job = {
    id: 'previous', title: 'Source recording',
    source: { kind: 'url', url, duration_s: 100, thumbnail: 'old.jpg', cover_name: 'own.jpg' },
    options: { meta: { title: 'Source rename', artist: 'Source artist' }, split_tracks: true, tracklist: '0:00 Source track' },
  };
  s.context.file = { id: 'f2', name: 'Recording (Enhanced).wav' };

  s.run('useJobSource(job, "analyze", { file })');
  assert.deepEqual(s.requests, []);
  assert.equal(s.state.source.job.id, 'previous');
  assert.equal(s.state.source.sourceFile.id, 'f2');
  assert.equal(s.run('loaded().title'), 'Recording (Enhanced)');
  assert.equal(s.run('loaded().meta'), 'Recording (Enhanced).wav');
  assert.equal(s.run('loaded().art'), undefined);
  assert.equal(s.run('sourceDuration()'), null);

  s.state.task = 'enhance';
  assert.equal(s.run('tracksOffered()'), true);
  assert.equal(s.run('Object.keys(tracksOptions()).length'), 0);
  assert.equal(s.run('hasOwnCover()'), false);
  s.run('setTracklist(state.source, "0:00 File track")');
  assert.equal(s.requests[0].path, '/api/tracklist');
  assert.equal(s.requests[0].json.text, '0:00 File track');
  assert.equal(s.requests[0].json.duration_s, undefined);

  s.run('useJobSource(job, "enhance")');
  assert.equal(s.run('refs.input.value'), url);
  assert.equal(s.state.source.sourceFile, undefined);
  assert.equal(s.run('loaded().title'), 'Source rename');
  assert.equal(s.run('sourceDuration()'), 100);
  assert.equal(s.requests[1].json.text, '0:00 Source track');
  assert.equal(s.requests[2].path, '/api/probe');
  assert.equal(s.requests[2].json.url, url);

  s.run('useJobSource(job, "analyze", { file: { ...file, duration_s: 32 } })');
  assert.equal(s.requests.length, 3);
  assert.equal(s.run('sourceDuration()'), 32);
  assert.match(s.run('loaded().meta'), /0:32$/);
});
