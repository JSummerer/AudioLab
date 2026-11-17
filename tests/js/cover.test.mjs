// Execute the production New-view modules with a small DOM, not a personal browser.
// These checks cover content and option behavior; they make no layout or player-display claims.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import { fmtDuration } from '../../audiolab/web/js/dom.js';
import { PROCESSING_LABELS, LIGHT_KEYS, enhanceAIChoice } from '../../audiolab/web/js/views/new/state.js';
import { RECIPE_LABELS, ENHANCE_PRESETS } from '../../audiolab/web/js/store.js';
import { parseEq } from '../../audiolab/web/js/eq.js';
import { checkSections } from '../../audiolab/web/js/section-eq.js';

class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tag = tag;
    Object.assign(this, attrs);
    this.children = children.flat(Infinity).filter((c) => c != null);
  }
  append(...kids) { this.children.push(...kids.flat(Infinity).filter((c) => c != null)); }
  addEventListener(type, fn) { ((this.listeners ||= {})[type] ||= []).push(fn); }
  fire(type) { for (const fn of this.listeners?.[type] || []) fn({ preventDefault() {} }); }
  focus() {}
  setAttribute(key, value) { this[key] = value; }
  get classList() {
    const add = (c) => { this.class = [this.class, c].flat().filter(Boolean); };
    return { add, toggle: (c, on) => { if (on) add(c); } };
  }
}
const findIn = (node, pred) => (node instanceof Element
  ? (pred(node) ? node : node.children.map((c) => findIn(c, pred)).find(Boolean)) : undefined);
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
    state, store, redrawn, PROCESSING_LABELS, RECIPE_LABELS, enhanceAIChoice, ENHANCE_PRESETS,
    refs: { input: { value: '' }, options: { querySelector: () => null } }, AbortController, URL, setTimeout, fmtDuration,
    focusIn() {}, focusStart() {}, autoTask() {},
    segmented: (attrs) => new Element('div', attrs), isShown: () => false, setShown() {}, formatInfo: () => null,
    clear: (el) => { el.children = []; return el; },
    append: (el, kids) => { el.children.push(...kids.flat(Infinity).filter((c) => c != null)); return el; },
    isLinkSource: () => state.source?.kind === 'url',
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
  for (const name of ['cover', 'names', 'tracks', 'options', 'source', 'sleeve', 'start', 'index']) {
    const path = new URL(`../../audiolab/web/js/views/new/${name}.js`, import.meta.url);
    const original = readFileSync(path, 'utf8');
    const checkout = crlf ? original.replace(/\r?\n/g, '\r\n') : original;
    const source = checkout.replace(/^import .*;\r?\n/gm, '').replace(/^export \{.*\};\r?\n/gm, '').replace(/^export /gm, '');
    vm.runInContext(source, context, { filename: path.pathname });
  }
  return { state, store, context, redrawn, requests, run: (code) => vm.runInContext(code, context) };
}

test('the separate image is off by default and does not control embedding', () => {
  const s = setup();
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('coverOptions(state.source, "download")'))), {});
  const [own, separate] = s.run('coverTools(state.source, "download")');
  assert.match(text(own), /Use my own image/);
  assert.match(text(separate), /Also save a separate image/);
  const sw = separate.children[0];
  assert.equal(sw.checked, false);
  sw.onChange(true);
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('coverOptions(state.source, "download")'))), { save_cover: true });
  assert.equal(s.state.download.embed_cover, true);
  assert.equal(s.run('coverTools(state.source, "separate")')[0], null, 'Split takes no picture of your own');
});

test('Reset under More options resets the options, never the name the page shows and sends', () => {
  const s = setup();
  // "Enhance it"/"Change and start again" from a job that was given the title "Concert".
  s.run(`useJobSource({ id: 'j1', task: 'download', title: 'Original', status: 'done',
    source: { kind: 'file', name: 'Original.wav' }, options: { format: 'mp3', meta: { title: 'Concert' } } }, 'download')`);
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('metaPayload(state.source)'))), { title: 'Concert' });
  s.state.download.embed_cover = false; // one option changed
  const el = s.run('const el = h("div"); renderDownloadOptions(el); el');
  const reset = findIn(el, (n) => n.tag === 'button' && n.children.includes('Reset'));
  reset.fire('click');
  assert.equal(s.state.download.embed_cover, true, 'the option is back to its default');
  assert.equal(s.run('shownName(state.source).title'), 'Concert');
  assert.deepEqual(JSON.parse(JSON.stringify(s.run('metaPayload(state.source)'))), { title: 'Concert' }, 'still sent');
});

test('a link from another page only fills the link field: nothing is looked up before Load', () => {
  const s = setup();
  s.run('offerLink("https://example.com/watch?v=stub")');
  assert.deepEqual(s.requests, [], 'no /api/probe request');
  assert.equal(s.run('refs.input.value'), 'https://example.com/watch?v=stub');
  assert.equal(s.state.source.kind, 'offered');
  s.run('pasteLink("https://example.com/watch?v=pasted")'); // a paste into the waiting field
  assert.deepEqual(s.requests, [], 'a paste does not look it up either');
  assert.equal(s.state.source.url, 'https://example.com/watch?v=pasted');
  s.run('loadUrl(refs.input.value)'); // what Load does
  assert.deepEqual(s.requests.map((r) => r.path), ['/api/probe'], 'Load looks it up');
  // main.js hands a deep link to offerLink, never to loadUrl.
  const main = readFileSync(new URL('../../audiolab/web/js/main.js', import.meta.url), 'utf8');
  assert.match(main, /offerLink\(deepLink\.link\)/);
  assert.doesNotMatch(main, /loadUrl\(deepLink/);
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

test('local name edits keep untouched tags, support explicit artist clearing, Cancel and Undo', () => {
  const s = setup();
  const editor = () => s.run('nameEditor(state.source)');
  const button = (el, label) => findIn(el, (n) => n.tag === 'button' && n.children.includes(label));
  const payload = () => JSON.parse(JSON.stringify(s.run('metaPayload(state.source)')));
  button(editor(), 'Save name').onclick();
  assert.equal(payload(), null);
  let form = editor();
  findIn(form, (n) => n.id === 'metaTitle').value = 'Concert';
  button(form, 'Save name').onclick();
  assert.deepEqual(payload(), { title: 'Concert' });
  form = editor();
  button(form, 'Clear artist tag').onclick();
  button(form, 'Cancel').onclick();
  assert.deepEqual(payload(), { title: 'Concert' });
  form = editor();
  button(form, 'Clear artist tag').onclick();
  button(form, 'Save name').onclick();
  assert.deepEqual(payload(), { title: 'Concert', artist: '' });
  button(s.run('nameTools(state.source)'), 'Undo').onclick();
  assert.equal(payload(), null);
  form = editor();
  findIn(form, (n) => n.id === 'metaArtist').value = 'Singer';
  button(form, 'Save name').onclick();
  assert.deepEqual(payload(), { artist: 'Singer' });
});

test('typing replaces the pending or loaded source and requires Load; ordinary paste still probes', async () => {
  const s = setup();
  let finish;
  s.context.api = () => new Promise((resolve) => { finish = resolve; });
  s.run('loadUrl("https://example.com/first")');
  s.run('editLink("https://example.com/second")');
  finish({ title: 'Stale answer' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.state.source.kind, 'offered');
  assert.match(s.run('blockedReason()'), /Press Load/);
  s.run('pasteLink("https://example.com/third")');
  finish({ title: 'Third' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(s.state.source.kind, 'url');
  s.run('editLink("https://example.com/fourth")');
  assert.equal(s.state.source.url, 'https://example.com/fourth');
  assert.match(s.run('blockedReason()'), /Press Load/);
  s.run('editLink("")');
  assert.equal(s.state.source, null);
});

test('format defaults ignore past browser storage, keep changes in this session, and follow an explicit Settings change', () => {
  const s = setup();
  s.context.localStorage.getItem = () => { throw new Error('must not read last started formats'); };
  s.store.settings = { download_format: 'mp3', enhance_formats: ['mp3_320'], separate_format: 'wav16' };
  s.state.separate = {};
  s.run('applyDefaults()');
  assert.deepEqual([...s.state.enhance.formats], ['mp3_320']);
  s.state.enhance.formats.add('wav16');
  s.store.settings.theme = 'dark';
  s.run('applyDefaults()');
  assert.deepEqual([...s.state.enhance.formats], ['mp3_320', 'wav16']);
  s.store.settings.enhance_formats = ['wav16'];
  s.run('applyDefaults()');
  assert.deepEqual([...s.state.enhance.formats], ['wav16']);
});

test('Split uses server labels for common and additional formats', () => {
  const s = setup();
  s.store.health.formats.separate = [{ key: 'wav16', label: 'Server WAV label' }, { key: 'wav24', label: 'Server 24-bit' }];
  assert.deepEqual([...s.run('separateFormatOptions()')].map((f) => f.label), ['Server WAV label', 'Server 24-bit']);
});

test('submission errors stay at Start on redraw, clear on retry, and belong only to their source', async () => {
  const s = setup();
  Object.assign(s.context, {
    FormData, document: { activeElement: null }, currentRoute: () => ({ name: 'new' }), upsertJob() {},
    shortFolder: () => '', callout: (level, title, help) => new Element('div', {}, [title, help]),
  });
  const row = new Element('div');
  row.contains = () => false;
  s.context.refs.action = row;
  s.state.source = { kind: 'url', url: 'https://example.com/recording', probe: { title: 'Recording' } };
  s.context.api = async () => { throw Object.assign(new Error('Could not start'), { hints: ['Select another folder.'] }); };
  await s.run('submit()');
  s.run('renderAction()');
  assert.match(text(findIn(row, (n) => n.id === 'submitError')), /Could not start Select another folder/);
  let finish;
  s.context.api = () => new Promise((resolve) => { finish = resolve; });
  const retry = s.run('submit()');
  assert.equal(s.state.source.submitError, undefined);
  s.run('editLink("https://example.com/other")');
  finish({ jobs: [] });
  await retry;
  s.run('renderAction()');
  assert.equal(findIn(row, (n) => n.id === 'submitError'), undefined);
});

test('Enhance AI wording follows server recipe stages and distinguishes Auto', () => {
  const s = setup();
  s.state.task = 'enhance';
  s.state.enhance.quality = 'best';
  s.store.health.recipes = [
    { key: 'music', ai_stage: 'stem_eq' }, { key: 'voice', ai_stage: 'voice_isolate' },
    { key: 'live', ai_stage: 'none' },
  ];
  for (const [recipe, label] of [['music', 'Balance parts separately'], ['voice', 'Isolate voice'], ['auto', 'AI when supported']]) {
    s.state.enhance.recipe = recipe;
    assert.match(s.run('plan()[0]'), new RegExp(label));
  }
  assert.equal(enhanceAIChoice('live', s.store.health.recipes), null);
  assert.match(enhanceAIChoice('auto', s.store.health.recipes).desc, /depends on the detected audio type/);
});
