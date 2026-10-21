// Execute the production New-view modules with a small DOM, not a personal browser.
// These checks cover content and option behavior; they make no layout or player-display claims.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

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
  const source = { kind: 'file', file: { name: 'plain.wav', size: 32 } };
  const state = {
    source, task: 'download', download: { format: 'mp3', embed_cover: true },
    enhance: { formats: new Set(['mp3_320', 'wav16']), embed_cover: true, recipe: 'auto', quality: 'standard' },
  };
  const store = { health: { formats: {
    download: [mp3, wav, original], enhance: [{ ...mp3, key: 'mp3_320' }, { ...wav, key: 'wav16' }],
  } } };
  const context = vm.createContext({
    state, store, redrawn,
    h: (tag, attrs, ...children) => new Element(tag, attrs || {}, children),
    icon: () => '', fmtBytes: (size) => `${size} B`,
    localStorage: { getItem: () => null, setItem() {} },
    switchControl: (attrs) => new Element('input', attrs),
    definePart() {}, redraw: (...parts) => redrawn.push(parts),
    isFileLike: () => state.source?.kind === 'file',
  });
  for (const name of ['cover', 'options', 'sleeve']) {
    const path = new URL(`../../audiolab/web/js/views/new/${name}.js`, import.meta.url);
    const original = readFileSync(path, 'utf8');
    const checkout = crlf ? original.replace(/\r?\n/g, '\r\n') : original;
    const source = checkout.replace(/^import .*;\r?\n/gm, '').replace(/^export /gm, '');
    vm.runInContext(source, context, { filename: path.pathname });
  }
  return { state, store, context, redrawn, run: (code) => vm.runInContext(code, context) };
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
