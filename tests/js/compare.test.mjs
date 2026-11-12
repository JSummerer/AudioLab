// Production comparison and Library modules, with finite DOM/Web Audio/HTTP fakes.
// No browser, network, personal audio, or npm dependency is used by these regressions.
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  comparePlayer, libraryCompare, sectionPreviewCompare, versionCompare, versionFile, versionLoudness, loudnessSpread,
} from '../../audiolab/web/js/compare.js';
import { openQuickPreview } from '../../audiolab/web/js/quick-preview.js';

class FakeNode {
  constructor(tag = '', text = '') {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.handlers = new Map();
    this.className = '';
    this._text = text;
    this.hidden = false;
    this.checked = false;
    this.disabled = false;
    if (tag === 'input') this.value = '';
    this.dataset = {};
    this.style = { setProperty() {} };
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, on) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (on) names.add(name); else names.delete(name);
        this.className = [...names].join(' ');
      },
      add: (name) => this.classList.toggle(name, true),
      remove: (name) => this.classList.toggle(name, false),
    };
  }
  setAttribute(key, value) {
    this.attributes.set(key, value);
    if (['id', 'value', 'min', 'max', 'type'].includes(key)) this[key] = value;
    if (['hidden', 'checked', 'disabled'].includes(key)) this[key] = true;
  }
  getAttribute(key) { return this.attributes.get(key); }
  removeAttribute(key) { this.attributes.delete(key); }
  append(...children) {
    for (let child of children) {
      if (!(child instanceof FakeNode)) child = new FakeNode('', String(child));
      child.remove();
      child.parentNode = this;
      this.children.push(child);
    }
  }
  insertBefore(child, before) {
    child.remove();
    child.parentNode = this;
    const index = this.children.indexOf(before);
    this.children.splice(index < 0 ? this.children.length : index, 0, child);
  }
  get firstChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children.at(-1) || null; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(text) { this._text = text; this.children = []; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
  remove() { this.parentNode?.removeChild(this); }
  contains(node) { return this === node || this.children.some((c) => c.contains(node)); }
  matches(selector) {
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    return this.tagName === selector;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map((s) => s.trim());
    return this.children.flatMap((c) => [
      ...(selectors.some((s) => c.matches(s)) ? [c] : []), ...c.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector); }
  addEventListener(kind, handler) {
    if (!this.handlers.has(kind)) this.handlers.set(kind, []);
    this.handlers.get(kind).push(handler);
  }
  fire(kind, values = {}) {
    const event = { target: this, preventDefault() {}, ...values };
    for (const handler of this.handlers.get(kind) || []) handler(event);
  }
  click(values) { this.fire('click', values); }
  focus() { document.activeElement = this; }
  get isConnected() { return document.body.contains(this); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  setCustomValidity(message) { this.validationMessage = message; }
  reportValidity() { return !this.validationMessage; }
  scrollIntoView() {}
  canPlayType() { return 'probably'; }
  play() { this.played = true; return Promise.resolve(); }
  pause() { this.paused = true; }
  load() { this.loaded = true; }
}

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const find = (root, predicate) => predicate(root) ? root : root.children.map((c) => find(c, predicate)).find(Boolean);
const button = (root, label) => find(root, (n) => n.tagName === 'button' && n.getAttribute('aria-label') === label);
const toggle = (root, label) => find(root, (n) => n.getAttribute('role') === 'switch' && n.getAttribute('aria-label') === label);
const seekTo = (root, value) => {
  const seek = root.querySelector('.ab-seek');
  seek.value = value;
  seek.fire('change');
};
function markRepeat(root, start, end) {
  for (const [point, value] of [['start', start], ['end', end]]) {
    seekTo(root, value);
    find(root, (n) => n.tagName === 'button' && n.textContent === `Set ${point}`).click();
  }
  return toggle(root, 'Repeat');
}
function enableRepeat(input) {
  assert.equal(input.disabled, false);
  input.checked = true;
  input.fire('change');
}

function setup(t) {
  const names = ['Node', 'document', 'window', 'navigator', 'location', 'localStorage', 'fetch',
    'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'];
  const original = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  t.after(() => {
    for (const [name, descriptor] of original) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const env = { contexts: [], requests: [], timers: new Map(), mode: 'ok', library: null, totalFrames: 20 * 48000 };
  let serial = 0;
  const parameter = () => ({ value: 1, events: [],
    cancelScheduledValues() {},
    setValueAtTime(v, when) { this.value = v; this.events.push(['set', v, when]); },
    setTargetAtTime(v) { this.value = v; },
    linearRampToValueAtTime(v, when) { this.value = v; this.events.push(['ramp', v, when]); },
  });
  class FakeContext {
    constructor() { this.currentTime = 0; this.sampleRate = 48000; this.state = 'suspended';
      this.sources = []; this.gains = []; this.suspends = 0; env.contexts.push(this); }
    createGain() {
      const node = { gain: parameter(),
        connect(destination) { this.destination = destination; },
        disconnect() { this.destination = null; } };
      this.gains.push(node);
      return node;
    }
    createBuffer(channels, length) { return { length, getChannelData: () => new Float32Array(length) }; }
    createBufferSource() {
      const source = { starts: [], stops: [], connect(node) { this.connectedTo = node; }, disconnect() {},
        start(at) { this.starts.push(at); }, stop(at) { this.stops.push(at); } };
      this.sources.push(source);
      return source;
    }
    resume() { this.state = 'running'; return Promise.resolve(); }
    suspend() { this.state = 'suspended'; this.suspends++; return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
  }
  const body = new FakeNode('body');
  Object.assign(globalThis, { Node: FakeNode, document: { activeElement: null, body,
    createElement: (tag) => new FakeNode(tag), createElementNS: (ns, tag) => new FakeNode(tag),
    createTextNode: (text) => new FakeNode('', text),
    getElementById: (id) => find(body, (n) => n.id === id) || null },
  window: { AudioContext: FakeContext }, location: { origin: 'http://127.0.0.1:19999' },
  localStorage: { getItem: () => null } });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { platform: 'Test' } });
  globalThis.setTimeout = (fn, delay) => { const id = ++serial; env.timers.set(id, { fn, delay }); return id; };
  globalThis.setInterval = (fn, delay) => { const id = ++serial; env.timers.set(id, { fn, delay, repeat: true }); return id; };
  globalThis.clearTimeout = globalThis.clearInterval = (id) => env.timers.delete(id);
  env.runTimers = (maxDelay) => {
    for (const [id, timer] of [...env.timers]) {
      if (timer.delay > maxDelay || !env.timers.has(id)) continue;
      if (!timer.repeat) env.timers.delete(id);
      timer.fn();
    }
  };
  env.pcm = (request) => {
    const total = request.path.endsWith('/result') ? (env.resultFrames ?? env.totalFrames) : env.totalFrames;
    const frames = Math.max(0, Math.min(request.frames, total - request.offset));
    return { ok: true, arrayBuffer: async () => new Float32Array(frames * 2).buffer };
  };
  globalThis.fetch = async (path, options = {}) => {
    if (path === '/api/library') return { ok: true, text: async () => JSON.stringify(env.library) };
    const url = new URL(path, location.origin);
    const request = { path: url.pathname, offset: Number(url.searchParams.get('offset')),
      frames: Number(url.searchParams.get('frames')), signal: options.signal };
    env.requests.push(request);
    if (env.mode === 'deferred') return new Promise((resolve) => { request.resolve = () => resolve(env.pcm(request)); });
    if (env.mode === 'gone' && request.path.endsWith('result')) {
      return { ok: false, status: 404, json: async () => ({ detail: 'Preview not available.' }) };
    }
    if (env.mode === 'failure' && request.path.endsWith('result')) {
      return { ok: false, status: 500, json: async () => ({ detail: 'Generated transient error.' }) };
    }
    return env.pcm(request);
  };
  env.create = (extra = {}) => {
    const player = comparePlayer({ length: 20,
      loudness: { original_lufs: -20, result_lufs: -14 },
      sides: ['original', 'result'].map((label) => ({ label,
        url: (offset, frames) => `/pcm/${label}?offset=${offset}&frames=${frames}` })), ...extra });
    t.after(() => player.destroy());
    return player;
  };
  return env;
}

test('recoverable PCM errors suspend output, retain position and can retry', async (t) => {
  const env = setup(t);
  const player = env.create({ startAt: 8 });
  env.mode = 'failure';
  button(player.el, 'Play').click();
  await flush();
  assert.equal(player.snapshot().time, 8);
  assert.equal(player.snapshot().playing, false);
  assert.ok(button(player.el, 'Play'));
  const retry = find(player.el, (n) => n.tagName === 'button' && n.textContent.includes('Try again'));
  assert.ok(retry);
  env.runTimers(100);
  assert.equal(env.contexts[0].state, 'suspended');
  env.mode = 'ok';
  retry.click();
  await flush();
  assert.equal(player.snapshot().playing, true);
  assert.equal(env.contexts[0].state, 'running');
  assert.equal(env.requests.at(-1).offset, 10 * 48000);
});

test('a side that is gone ends the player with the default or a caller-supplied message', async (t) => {
  const env = setup(t);
  env.mode = 'gone';
  const library = env.create();
  button(library.el, 'Play').click();
  await flush();
  assert.match(library.el.textContent, /The result couldn’t be loaded\. It may have been moved or deleted\./);
  const audition = env.create({ missing: 'This audition is no longer available. Render it again.' });
  button(audition.el, 'Play').click();
  await flush();
  assert.match(audition.el.textContent, /This audition is no longer available\. Render it again\./);
  assert.doesNotMatch(audition.el.textContent, /moved or deleted/);
});

test('retry before the fade settles is not suspended by the old pause', async (t) => {
  const env = setup(t);
  const player = env.create();
  env.mode = 'failure';
  button(player.el, 'Play').click();
  await flush();
  env.mode = 'ok';
  button(player.el, 'Play').click();
  await flush();
  env.runTimers(100);
  assert.equal(env.contexts[0].state, 'running');
  assert.equal(env.contexts[0].suspends, 0);
});

test('transport restore keeps time, original/result and volume choice while paused', (t) => {
  const env = setup(t);
  const first = env.create();
  const seek = first.el.querySelector('.ab-seek');
  seek.value = 12.125;
  seek.fire('change');
  find(first.el, (n) => n.getAttribute('role') === 'radio').click();
  const match = find(first.el, (n) => n.getAttribute('role') === 'switch');
  match.checked = false;
  match.fire('change');
  const state = first.snapshot();
  first.destroy();
  const second = env.create();
  second.restore(state, { resume: true });
  assert.deepEqual(second.snapshot(), { time: 12.125, playing: false, side: 'original', matchVolume: false });
  assert.equal(second.el.querySelector('.ab-seek').value, 12.125);
  assert.equal(find(second.el, (n) => n.getAttribute('role') === 'switch').checked, false);
  assert.equal(env.contexts.length, 0);
});

test('restore clamps a shorter result at its end without restarting, and rejects invalid time', (t) => {
  const env = setup(t);
  const player = env.create({ length: 4 });
  player.restore({ time: 12, playing: true, side: 'original', matchVolume: false }, { resume: true });
  assert.equal(player.snapshot().time, 4);
  assert.equal(player.snapshot().playing, false);
  assert.equal(env.contexts.length, 0);
  player.restore({ time: Infinity, playing: true }, { resume: true });
  assert.equal(player.snapshot().time, 4);
  player.restore({ time: -5, playing: false });
  assert.equal(player.snapshot().time, 0);
});

test('switching during a pending seek drops old fetches and closes the old context', async (t) => {
  const env = setup(t);
  env.mode = 'deferred';
  const first = env.create();
  button(first.el, 'Play').click();
  const seek = first.el.querySelector('.ab-seek');
  seek.value = 9.12;
  seek.fire('keydown', { key: 'ArrowRight' }); // pending key seek: moves to 14.12 immediately
  const state = first.snapshot();
  assert.equal(state.time, 14.12);
  first.destroy();
  const second = env.create();
  second.restore(state, { resume: true });
  assert.equal(env.contexts[0].state, 'closed');
  assert.ok(env.requests.slice(0, 2).every((r) => r.signal.aborted));
  env.requests.slice(0, 2).forEach((r) => r.resolve()); // ignore abort deliberately
  await flush();
  assert.equal(env.contexts[0].sources.length, 0);
  assert.equal(env.contexts[1].sources.length, 0);
  env.requests.slice(2, 4).forEach((r) => r.resolve());
  await flush();
  assert.equal(env.contexts[1].sources.length, 2);
  assert.equal(second.snapshot().time, 14.12);
});

test('a snapshot taken after audio ends cannot revive playback in the next version', async (t) => {
  const env = setup(t);
  env.totalFrames = 48000;
  const first = env.create({ length: 1 });
  button(first.el, 'Play').click();
  await flush();
  env.contexts[0].currentTime = 2; // after the real end; the next UI tick has not run
  const state = first.snapshot();
  assert.equal(state.playing, false);
  assert.equal(state.time, 1);
  const second = env.create();
  second.restore(state, { resume: true });
  assert.equal(second.snapshot().time, 1);
  assert.equal(second.snapshot().playing, false);
});

test('a short repeat schedules both sides at exactly the same boundaries and reuses PCM through wraps', async (t) => {
  const env = setup(t);
  const player = env.create();
  const repeat = markRepeat(player.el, 3.16, 6.36);
  enableRepeat(repeat);
  assert.equal(player.snapshot().time, 3.16);
  button(player.el, 'Play').click();
  await flush();
  const ctx = env.contexts[0];
  for (let i = 1; i <= 51; i++) {
    ctx.currentTime = i / 5;
    env.runTimers(200);
    await flush();
  }
  assert.deepEqual(env.requests.map(({ offset, frames }) => [offset, frames]),
    [[151680, 96000], [151680, 96000], [247680, 57600], [247680, 57600]]);
  assert.ok(ctx.sources.length >= 16); // at least four passes of both chunks
  for (let i = 0; i < ctx.sources.length; i += 2) {
    const [a, b] = ctx.sources.slice(i, i + 2);
    const phase = i / 2;
    const expected = ctx.sources[0].starts[0] + Math.floor(phase / 2) * 3.2 + (phase % 2 ? 2 : 0);
    assert.equal(a.starts[0], b.starts[0]);
    assert.ok(Math.abs(a.starts[0] - expected) < 1e-10);
    assert.equal(a.buffer.length, phase % 2 ? 57600 : 96000);
    assert.equal(b.buffer.length, a.buffer.length);
  }
  assert.equal(player.snapshot().time, 3.71);
  player.pause();
  env.runTimers(200);
  assert.equal(ctx.state, 'suspended');
  assert.ok(ctx.sources.at(-1).stops.length);
  assert.deepEqual(player.snapshot().repeat, { start: 3.16, end: 6.36, enabled: true });
});

test('repeat fades match on both sides and only touch the edges of the selected range', async (t) => {
  const env = setup(t);
  for (const { end, fades } of [
    { end: 3.24, fades: [[true, true], [true, true]] }, // both edges in one chunk
    { end: 5.2, fades: [[true, false], [false, true], [true, false]] }, // split after 2 s, then wrap
  ]) {
    const player = env.create();
    enableRepeat(markRepeat(player.el, 2, end));
    button(player.el, 'Play').click();
    await flush();
    const { sources, gains } = env.contexts.at(-1);
    fades.forEach(([fadeIn, fadeOut], i) => {
      const pair = sources.slice(i * 2, i * 2 + 2);
      assert.equal(pair.length, 2);
      assert.equal(pair[0].starts[0], pair[1].starts[0]);
      for (const [side, source] of pair.entries()) {
        assert.equal(source.connectedTo.destination, gains[side + 1]);
        const expected = [];
        const rampFrames = 576; // 12 ms at 48 kHz
        if (fadeIn) expected.push(['set', 0, 0], ['ramp', 1, rampFrames]);
        if (fadeOut) expected.push(['set', 1, source.buffer.length - rampFrames], ['ramp', 0, source.buffer.length]);
        const events = source.connectedTo.gain.events.map(([event, value, time]) =>
          [event, value, Math.round((time - source.starts[0]) * 48000)]);
        assert.deepEqual(events, expected);
      }
    });
    sources[0].onended();
    assert.equal(sources[0].connectedTo.destination, null);
    player.destroy();
  }
});

test('long repeats stop fetching at the selected end and a seek outside leaves repeat mode', async (t) => {
  const env = setup(t);
  const player = env.create();
  const repeat = markRepeat(player.el, 2, 14.32);
  enableRepeat(repeat);
  button(player.el, 'Play').click();
  await flush();
  const ctx = env.contexts[0];
  for (const seconds of [6.2, 9, 13]) {
    ctx.currentTime = seconds;
    env.runTimers(200);
    await flush();
  }
  const offsets = env.requests.filter((r) => r.path.endsWith('/original')).map(({ offset, frames }) => [offset, frames]);
  assert.deepEqual(offsets, [[96000, 96000], [192000, 384000], [576000, 111360], [96000, 384000]]);
  assert.ok(Math.abs(ctx.sources[6].starts[0] - (ctx.sources[0].starts[0] + 12.32)) < 1e-10);
  seekTo(player.el, 18);
  await flush();
  assert.equal(repeat.checked, false);
  assert.equal(env.requests.findLast((r) => r.path.endsWith('/original') && r.offset === 18 * 48000).frames, 96000);
  assert.ok(ctx.sources[6].stops.length);
});

test('the timeline can mark the final grid point and seeking to it leaves repeat mode', async (t) => {
  const env = setup(t);
  const player = env.create();
  const repeat = markRepeat(player.el, 18, 20);
  enableRepeat(repeat);
  assert.deepEqual(player.snapshot().repeat, { start: 18, end: 20, enabled: true });
  button(player.el, 'Play').click();
  await flush();
  seekTo(player.el, 20);
  await flush();
  assert.equal(repeat.checked, false);
  assert.equal(env.requests.at(-1).offset, 19.96 * 48000);
  const ctx = env.contexts[0];
  ctx.currentTime = 1;
  env.runTimers(200);
  assert.equal(player.snapshot().time, 20);
  assert.equal(player.snapshot().playing, false);
});

test('repeat rejects a short range and stops repeating if one side reaches an earlier end', async (t) => {
  const env = setup(t);
  const player = env.create();
  assert.equal(markRepeat(player.el, 2, 2.96).disabled, true);
  const repeat = markRepeat(player.el, 2, 3.6);
  env.resultFrames = 3 * 48000;
  enableRepeat(repeat);
  button(player.el, 'Play').click();
  await flush();
  assert.equal(repeat.checked, false);
  assert.equal(repeat.disabled, true);
  assert.match(player.el.querySelector('.cmp-repeat-note').textContent, /One version ends before this range/);
  assert.equal(player.snapshot().playing, false);
  assert.equal(env.requests.length, 2);
  assert.equal(env.contexts[0].sources.length, 0);
});

test('pending repeat audio is discarded after an in-range seek, pause and destroy', async (t) => {
  const env = setup(t);
  const player = env.create();
  env.mode = 'deferred';
  enableRepeat(markRepeat(player.el, 2, 6));
  button(player.el, 'Play').click();
  seekTo(player.el, 4.4);
  assert.equal(toggle(player.el, 'Repeat').checked, true);
  const [first, seek] = [env.requests.slice(0, 2), env.requests.slice(2, 4)];
  assert.ok(first.every((r) => r.signal.aborted));
  first.forEach((r) => r.resolve());
  await flush();
  assert.equal(env.contexts[0].sources.length, 0);
  seek.forEach((r) => r.resolve());
  await flush();
  assert.equal(env.contexts[0].sources.length, 2);
  const next = env.requests.slice(4);
  assert.equal(next.length, 2);
  player.pause();
  player.destroy();
  assert.ok(next.every((r) => r.signal.aborted));
  next.forEach((r) => r.resolve());
  await flush();
  assert.equal(env.contexts[0].sources.length, 2);
  assert.equal(env.contexts[0].state, 'closed');
});

test('a repeat can retry a transient read error without losing its range', async (t) => {
  const env = setup(t);
  const player = env.create();
  const repeat = markRepeat(player.el, 2, 5.2);
  enableRepeat(repeat);
  env.mode = 'failure';
  button(player.el, 'Play').click();
  await flush();
  assert.equal(player.snapshot().playing, false);
  assert.equal(repeat.checked, true);
  assert.equal(env.contexts[0].sources.length, 0);
  env.mode = 'ok';
  find(player.el, (n) => n.tagName === 'button' && n.textContent.includes('Try again')).click();
  await flush();
  assert.equal(player.snapshot().playing, true);
  assert.deepEqual(player.snapshot().repeat, { start: 2, end: 5.2, enabled: true });
});

let librarySerial = 0;
async function librarySetup(t, entries, moreGroups = []) {
  const env = setup(t);
  env.library = { groups: [{ key: 'file:test', title: 'Generated fixture', entries }, ...moreGroups], count: entries.length };
  const { libraryView } = await import(`../../audiolab/web/js/views/library.js?test=${++librarySerial}`);
  const stage = new FakeNode('main');
  stage.className = 'content';
  document.body.append(stage);
  libraryView.mount(stage);
  libraryView.show();
  await flush();
  t.after(() => libraryView.hide());
  const card = (id) => stage.querySelector(`#lib-${id}`);
  const open = (id, trusted = true) => find(card(id), (n) => n.tagName === 'button'
    && n.textContent.includes('Compare with the original')).click({ isTrusted: trusted });
  const seek = (id, seconds) => { const slider = card(id).querySelector('.ab-seek'); slider.value = seconds; slider.fire('change'); };
  return { ...env, card, open, seek, view: libraryView };
}

const entry = (id, group = 'file:test', duration_s = 20) => ({ id, group, duration_s,
  task: 'enhance', title: 'Generated fixture', source: { kind: 'file', name: 'fixture.wav' },
  original: { kept: true }, loudness: { original_lufs: -20, result_lufs: -14 }, compare_file_id: 'f0',
  files: [{ id: 'f0', name: 'fixture.wav', label: 'WAV', kind: 'enhanced', mime: 'audio/wav', playable: true, exists: true }] });

test('a Library reduction cue keeps the selected side and repeats around the peak, not its time-window boundary', (t) => {
  setup(t);
  const result = entry('changes', 'file:changes', 60);
  result.processing = { applied: { adaptive_lowmid: {
    events: [{ start_s: 4, end_s: 5, max_gr_db: 4 }],
    timeline: { duration_s: 60, bins: [{ start_s: 20, end_s: 30, peak_s: 27.44, max_gr_db: 1.7 }] },
  } } };
  const player = libraryCompare(result);
  t.after(() => player.destroy());
  find(player.el, (n) => n.getAttribute('role') === 'radio').click();
  const marker = player.el.querySelector('.cmp-reduction-marker');
  assert.match(marker.getAttribute('aria-label'), /0:27: Low-mid control, 1.7 dB reduction/);
  marker.click();
  assert.equal(player.snapshot().side, 'original');
  assert.equal(player.snapshot().playing, false);
  const repeat = player.snapshot().repeat;
  assert.equal(repeat.enabled, true);
  assert.ok(Math.abs((repeat.start + repeat.end) / 2 - 27.44) < 0.041);
  assert.equal(player.el.querySelectorAll('.cmp-reduction-marker').length, 1);
});

const previewEntry = () => ({
  ...entry('preview-source', 'file:preview-source', 60),
  processing: { requested: { quality: 'standard', formats: ['wav'], advanced: { deess: 'off' } },
    resolved: { params: { target_lufs: -14 } } },
});
const previewJob = (id = 'section') => ({
  id, status: 'done', source: { duration_s: 60 }, result: {
    preview: true, excerpt: { start_s: 31, duration_s: 8 },
    processing: { applied: { limiter: {
      timeline: { duration_s: 8, bins: [{ start_s: 0, end_s: 4, peak_s: 3, max_gr_db: 1.1 }] },
    } } },
  },
});
const jsonResponse = (data) => ({ ok: true, text: async () => JSON.stringify(data) });
const named = (root, text) => find(root, (n) => n.tagName === 'button' && n.textContent === text);
// A labelled field of the preview dialog: <label><span>Label</span><input></label>.
const field = (root, label) => find(root, (n) => n.tagName === 'label' && n.children[0]?.textContent === label)?.children[1];
const type = (input, value) => { input.value = value; input.fire('input'); };
const pick = (root, group, value) => {
  const radios = find(root, (n) => n.getAttribute('role') === 'radiogroup' && n.getAttribute('aria-label') === group);
  const radio = find(radios, (n) => n.tagName === 'input' && n.value === value);
  radio.checked = true;
  radio.fire('change');
};

test('a preview reduction cue uses source time while requesting excerpt-relative PCM', async (t) => {
  const env = setup(t);
  const preview = sectionPreviewCompare(previewJob());
  t.after(() => preview.destroy());
  const marker = preview.el.querySelector('.cmp-reduction-marker');
  assert.match(marker.getAttribute('aria-label'), /0:34: Peak limiting, 1.1 dB reduction/);
  marker.click();
  button(preview.el, 'Play').click();
  await flush();
  assert.deepEqual(env.requests.slice(0, 2).map((r) => r.path),
    ['/api/jobs/section/pcm/original', '/api/jobs/section/pcm/enhanced']);
  assert.ok(env.requests[0].offset < 48000, 'play starts roughly 2.5 seconds before the 3-second excerpt peak');
});

test('an unchanged preview plays the saved export and edits clear it before a section estimate', async (t) => {
  const env = setup(t);
  const current = env.create({ length: 60 });
  current.restore({ time: 32, repeat: { start: 31, end: 39, enabled: true } });
  const requests = [];
  const savedSummary = 'Playing the saved WAV version with its full-recording processing. No new processing was applied.';
  let basis;
  globalThis.fetch = async (path, options) => {
    const payload = options?.body && JSON.parse(options.body);
    requests.push([path, options?.method, payload]);
    if (path.endsWith('/preview')) {
      basis = Object.keys(payload.advanced).length ? 'section_estimate' : 'saved_export';
      return jsonResponse({ id: 'section', status: 'queued' });
    }
    if (path === '/api/jobs/section') {
      const job = previewJob();
      job.result.preview_basis = basis;
      if (basis === 'saved_export') job.result.summary = [savedSummary];
      return jsonResponse(job);
    }
    assert.fail(`Unexpected request: ${path}`);
  };
  const dismiss = openQuickPreview(previewEntry(), current);
  t.after(dismiss);
  const dialog = document.body.querySelector('dialog');
  const status = find(dialog, (n) => n.getAttribute('role') === 'status');
  const make = named(dialog, 'Make preview');
  make.click();
  make.click();
  await flush();
  assert.deepEqual(requests, [['/api/library/preview-source/preview', 'POST',
    { start_s: 31, duration_s: 8, advanced: {} }]]);
  assert.match(status.textContent, /Preview queued/);
  env.runTimers(600);
  await flush();
  assert.ok(dialog.querySelector('.ab'));
  assert.equal(status.textContent, savedSummary);
  const apply = named(dialog, 'Apply to full recording');
  assert.equal(apply.hidden, false);
  type(field(dialog, 'Bass boost (dB)'), '2');
  assert.equal(apply.hidden, true);
  assert.equal(dialog.querySelector('.ab'), null);
  assert.match(status.textContent, /Settings changed/);
  apply.click();
  assert.equal(requests.length, 2);
  make.click();
  await flush();
  assert.deepEqual(requests.at(-1)[2].advanced, { bass_db: 2 });
  env.runTimers(600);
  await flush();
  assert.ok(dialog.querySelector('.ab'));
  assert.match(status.textContent, /Section estimate/);
});

test('the preview job page shows whether it played a saved export or estimated a section', async (t) => {
  setup(t);
  const { doneView } = await import('../../audiolab/web/js/views/job/results.js');
  const ctx = { onCleanup: (fn) => t.after(fn) };
  const job = previewJob();
  job.result.preview_basis = 'saved_export';
  job.result.summary = ['Playing the saved WAV version with its full-recording processing.'];
  assert.ok(doneView(job, ctx).some((node) => node.textContent === job.result.summary[0]));

  job.result.preview_basis = 'section_estimate';
  delete job.result.summary;
  assert.ok(doneView(job, ctx).some((node) => node.textContent.startsWith('Section estimate.')));
});

test('a section preview takes clock times, loudness, compression and a custom EQ, sent only when changed', async (t) => {
  const env = setup(t);
  const requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push([path, options?.body && JSON.parse(options.body)]);
    if (path.endsWith('/preview')) return jsonResponse({ id: 'section', status: 'queued' });
    if (path === '/api/jobs/section') return jsonResponse(previewJob());
    if (path.endsWith('/enhance')) return new Promise(() => {}); // stays pending: no navigation in this test
    assert.fail(`Unexpected request: ${path}`);
  };
  const saved = previewEntry();
  saved.processing.requested.advanced.eq = [{ hz: 160, db: -3.3, q: 2.9 }];
  const dismiss = openQuickPreview(saved, null);
  t.after(dismiss);
  const dialog = document.body.querySelector('dialog');
  const make = async () => {
    named(dialog, 'Make preview').click();
    await flush();
    env.runTimers(600);
    await flush();
    return requests.filter(([path]) => path.endsWith('/preview')).at(-1)[1];
  };
  const eqText = find(dialog, (n) => n.tagName === 'textarea');
  assert.equal(eqText.value, '160 Hz −3.3 dB Q 2.9');
  assert.equal(field(dialog, 'Start').value, '0:00');
  assert.deepEqual(await make(), { start_s: 0, duration_s: 15, advanced: {} }, 'the saved EQ is not sent again');

  type(field(dialog, 'Start'), '0:20');
  type(field(dialog, 'Length (seconds)'), '0:10');
  type(field(dialog, 'Loudness (LUFS)'), '-12');
  pick(dialog, 'Compression', 'on');
  named(dialog, 'Sub focus').click();
  assert.equal(eqText.value, '160 Hz −3.3 dB Q 2.9\n31 Hz −1.0 dB Q 3.1\n45 Hz +1.0 dB Q 3.1');
  const changed = await make();
  assert.deepEqual(changed, { start_s: 20, duration_s: 10, advanced: { target_lufs: -12, glue: 'on',
    eq: [{ hz: 160, db: -3.3, q: 2.9 }, { hz: 31, db: -1, q: 3.1 }, { hz: 45, db: 1, q: 3.1 }] } });
  named(dialog, 'Apply to full recording').click();
  await flush();
  assert.deepEqual(requests.at(-1), ['/api/library/preview-source/enhance', { advanced: changed.advanced }]);
});

test('a section preview clears a saved EQ with [] and waits for valid times and EQ text', async (t) => {
  setup(t);
  const bodies = [];
  globalThis.fetch = async (path, options) => {
    bodies.push(JSON.parse(options.body));
    return jsonResponse({ id: 'section', status: 'queued' });
  };
  const saved = previewEntry();
  Object.assign(saved.processing.requested.advanced, { eq: [{ hz: 160, db: -3.3, q: 2.9 }], glue: 'off' });
  saved.duration_s = 10000; // a long set: times past an hour
  const dismiss = openQuickPreview(saved, null);
  t.after(dismiss);
  const dialog = document.body.querySelector('dialog');
  const status = find(dialog, (n) => n.getAttribute('role') === 'status');
  const radios = find(dialog, (n) => n.getAttribute('role') === 'radiogroup' && n.getAttribute('aria-label') === 'Compression');
  assert.deepEqual(radios.querySelectorAll('input').map((n) => n.value), ['on', 'off'], 'no Auto once a version saved one');
  const eqText = find(dialog, (n) => n.tagName === 'textarea');
  type(eqText, '160 Hz -3.3 dB');
  named(dialog, 'Make preview').click();
  await flush();
  assert.equal(bodies.length, 0);
  assert.match(status.textContent, /Fix the custom EQ first/);
  assert.match(find(dialog, (n) => n.tagName === 'ul').textContent, /Line 1: Add a width as Q/);
  type(eqText, '');
  type(field(dialog, 'Start'), '1:75');
  named(dialog, 'Make preview').click();
  await flush();
  assert.equal(bodies.length, 0, 'an unreadable start is reported by the field');
  type(field(dialog, 'Start'), '1:02:03.5');
  assert.match(find(dialog, (n) => n.getAttribute('id')?.endsWith('-range')).textContent, /Section 1:02:04–1:02:19/);
  named(dialog, 'Make preview').click();
  await flush();
  assert.deepEqual(bodies, [{ start_s: 3723.5, duration_s: 15, advanced: { eq: [] } }]);
});

test('closing a starting preview cancels its eventual job without polling or showing its result', async (t) => {
  setup(t);
  let submit;
  const requests = [];
  globalThis.fetch = (path) => {
    requests.push(path);
    if (path.endsWith('/preview')) return new Promise((resolve) => { submit = resolve; });
    assert.match(path, /\/cancel$/);
    return Promise.resolve(jsonResponse({ id: 'section', status: 'cancelled' }));
  };
  const dismiss = openQuickPreview(previewEntry());
  const dialog = document.body.querySelector('dialog');
  named(dialog, 'Make preview').click();
  dismiss();
  submit(jsonResponse({ id: 'section', status: 'queued' }));
  await flush();
  assert.deepEqual(requests, ['/api/library/preview-source/preview', '/api/jobs/section/cancel']);
  assert.equal(document.body.querySelector('dialog'), null);
});

test('a failed preview shows its recovery hint and never offers Apply', async (t) => {
  setup(t);
  globalThis.fetch = async () => jsonResponse({ id: 'section', status: 'failed',
    error: { message: 'Could not align this preview.', hints: ['Try a WAV or FLAC original.'] } });
  const dismiss = openQuickPreview(previewEntry());
  t.after(dismiss);
  const dialog = document.body.querySelector('dialog');
  named(dialog, 'Make preview').click();
  await flush();
  assert.match(find(dialog, (n) => n.getAttribute('role') === 'status').textContent,
    /Could not align this preview\. Try a WAV or FLAC original\./);
  assert.equal(named(dialog, 'Apply to full recording').hidden, true);
  assert.equal(dialog.querySelector('.ab'), null);
});

test('cancelling a preview that finishes before cancellation does not expose Apply', async (t) => {
  const env = setup(t);
  let cancelCalls = 0;
  globalThis.fetch = async (path) => {
    if (path.endsWith('/preview')) return jsonResponse({ id: 'section', status: 'running' });
    if (path.endsWith('/cancel')) {
      cancelCalls++;
      return { ok: false, status: 400, text: async () => '{}' };
    }
    assert.equal(path, '/api/jobs/section');
    return jsonResponse(previewJob());
  };
  const dismiss = openQuickPreview(previewEntry());
  t.after(dismiss);
  const dialog = document.body.querySelector('dialog');
  named(dialog, 'Make preview').click();
  await flush();
  const cancel = named(dialog, 'Cancel preview');
  cancel.click();
  cancel.click();
  env.runTimers(600);
  await flush();
  assert.equal(cancelCalls, 1);
  assert.equal(named(dialog, 'Apply to full recording').hidden, true);
  assert.equal(dialog.querySelector('.ab'), null);
  assert.match(find(dialog, (n) => n.getAttribute('role') === 'status').textContent, /Preview cancelled/);
});

test('Library matches volume using the chosen export, including when its peak check warns', async (t) => {
  const env = setup(t);
  const result = entry('level');
  result.files.push({ id: 'other', label: 'MP3', verification: { status: 'verified', integrated_lufs: -10 } });
  result.files[0].verification = { status: 'warning', integrated_lufs: -18 };
  const player = libraryCompare(result);
  t.after(() => player.destroy());
  button(player.el, 'Play').click();
  await flush();
  const resultGain = env.contexts[0].gains[2].gain;
  assert.ok(Math.abs(resultGain.value - 10 ** (-2 / 20)) < 1e-10);
  assert.match(player.el.querySelector('.cmp-volume-note').textContent, /2\.0 dB louder/);
  assert.ok(env.requests.some((r) => r.path.endsWith('/f0')));
});

test('a new export with no reliable loudness cannot use the legacy master level', async (t) => {
  const env = setup(t);
  for (const verification of [null, { status: 'unverified', integrated_lufs: null },
    { status: 'verified', integrated_lufs: null }]) {
    const result = entry('unverified');
    result.files[0].verification = verification;
    const player = libraryCompare(result);
    const match = toggle(player.el, 'Match volume');
    assert.equal(match.disabled, true);
    assert.equal(match.checked, false);
    assert.match(player.el.querySelector('.cmp-volume-note').textContent, /needs a loudness measurement/);
    button(player.el, 'Play').click();
    await flush();
    assert.equal(env.contexts.at(-1).gains[2].gain.value, 1);
    player.destroy();
  }
  const legacy = libraryCompare(entry('legacy'));
  t.after(() => legacy.destroy());
  assert.equal(toggle(legacy.el, 'Match volume').checked, true);
  assert.match(legacy.el.querySelector('.cmp-volume-note').textContent, /6\.0 dB louder/);
});

test('Library lists a custom EQ on one line and keeps "min ago" current while it shows', async (t) => {
  const created = Math.floor(Date.now() / 1000) - 5;
  if (new Date(created * 1000).toDateString() !== new Date().toDateString()) { t.skip('just after midnight'); return; }
  const result = entry('eq');
  result.created_at = created;
  result.processing = { requested: { quality: 'standard', formats: ['wav'], advanced: {
    hf_restore: 'on', glue: 'off', eq: [{ hz: 160, db: -3.3, q: 2.9 }, { hz: 280, db: 1.5, q: 2 }],
  } } };
  const cleared = entry('cleared');
  cleared.processing = { requested: { quality: 'standard', formats: ['wav'], advanced: { eq: [] } } };
  const env = await librarySetup(t, [result, cleared]);
  const facts = (id) => Object.fromEntries(env.card(id).querySelectorAll('dt').map((dt) => [dt.textContent,
    dt.parentNode.children[dt.parentNode.children.indexOf(dt) + 1].textContent]));
  assert.equal(facts('eq')['Custom EQ'], '160 Hz −3.3 dB Q2.9 · 280 Hz +1.5 dB Q2.0');
  assert.equal(facts('eq')['Brighten muffled highs'], 'on');
  assert.equal(facts('cleared')['Custom EQ'], undefined, 'a cleared EQ is not listed');
  const when = env.card('eq').querySelector('.lib-when');
  assert.equal(when.textContent, ' · just now');
  t.mock.method(Date, 'now', () => (created + 125) * 1000);
  env.runTimers(30000);
  assert.equal(when.textContent, ' · 2 min ago');
});

test('each Enhance version says what it is: loudness, compression, EQ, rate and whose settings', async (t) => {
  const band = [{ hz: 160, db: -3.3, q: 2.9 }];
  const version = (id, processing) => Object.assign(entry(id), { processing });
  const env = await librarySetup(t, [
    version('tuned', { requested: { quality: 'standard', advanced: { target_lufs: -13.5, eq: band } },
      resolved: { params: { target_lufs: -13.5, compressor: 'glue', eq_bands: band, sample_rate: 44100 } } }),
    version('auto', { requested: { quality: 'standard', advanced: {} },
      resolved: { params: { target_lufs: -14, compressor: 'off', eq_bands: [], sample_rate: 48000 } } }),
    version('volume', { requested: { quality: 'light', advanced: { target_lufs: -12, eq: band, glue: 'on' } },
      resolved: { params: { target_lufs: -12, compressor: 'off', volume_only: true, eq_bands: [] } } }),
    version('cleared', { requested: { quality: 'standard', advanced: { eq: [] } } }),
    entry('older'),
  ]);
  const line = (id) => env.card(id).querySelector('.lib-version')?.textContent;
  assert.equal(line('tuned'), '−13.5 LUFS · compression on · custom EQ · 44.1 kHz · your settings: loudness, EQ');
  assert.equal(line('auto'), '−14 LUFS · compression off · Auto settings');
  assert.equal(line('volume'), '−12 LUFS · your settings: loudness', 'Volume only lists only what it uses');
  assert.equal(line('cleared'), 'Auto settings', 'a version without resolved settings still says whose they were');
  assert.equal(line('older'), undefined, 'an entry from before saved settings has no line');
});

test('a version is compared by the server’s pick, else its first lossless result still there', () => {
  const file = (id, format, extra = {}) => ({ id, format, kind: 'enhanced', exists: true, ...extra });
  const formats = [{ key: 'mp3_320', kind: 'lossy' }, { key: 'flac24', kind: 'lossless' }];
  const mp3 = file('m', 'mp3_320');
  const flac = file('f', 'flac24');
  assert.equal(versionFile({ compare_file_id: 'm', files: [flac, mp3] }, formats), mp3);
  assert.equal(versionFile({ compare_file_id: null, files: [mp3, flac] }, formats), flac, 'no kept original: lossless first');
  assert.equal(versionFile({ compare_file_id: null, files: [mp3, flac] }), mp3, 'without Health: the first one');
  assert.equal(versionFile({ compare_file_id: 'f', files: [mp3, { ...flac, exists: false }] }, formats), mp3, 'a moved file is skipped');
  assert.equal(versionFile({ files: [file('o', 'wav16', { kind: 'original' }), file('c', null, { kind: 'cover' })] }), null);
  assert.equal(versionFile({}), null);
});

test('version loudness uses export measurements or legacy estimates; a spread counts above 1 dB', () => {
  const entry = { loudness: { result_lufs: -14.2 } };
  assert.equal(versionLoudness(entry, { verification: { status: 'verified', integrated_lufs: -13.5 } }), -13.5);
  assert.equal(versionLoudness(entry, { verification: { status: 'warning', integrated_lufs: -13.9 } }), -13.9);
  assert.equal(versionLoudness(entry, { verification: { status: 'unverified', integrated_lufs: null } }), null);
  assert.equal(versionLoudness(entry, { verification: { status: 'verified', integrated_lufs: null } }), null);
  assert.equal(versionLoudness(entry, { verification: null }), null);
  assert.equal(versionLoudness(entry, {}), -14.2);
  assert.equal(versionLoudness({}, null), null);
  assert.deepEqual(loudnessSpread([-13.5, -15.4, -14]), { low: -15.4, high: -13.5 });
  assert.equal(loudnessSpread([-14.5, -13.5]), null, 'exactly 1 dB is not enough');
  assert.equal(loudnessSpread([-15.4, null]), null);
  assert.equal(loudnessSpread([]), null);
});

test('two versions play side by side from their own files, the louder one turned down', async (t) => {
  const env = setup(t);
  const version = (id, lufs, label) => ({ entry: { id, duration_s: 20, loudness: { result_lufs: lufs } },
    file: { id: `f-${id}`, kind: 'enhanced', exists: true }, label, name: `The ${label.toLowerCase()} version` });
  const newer = version('new', -14, 'Newer');
  newer.file.verification = { status: 'warning', integrated_lufs: -13.5 };
  const player = versionCompare(version('old', -15.4, 'Older'), newer);
  t.after(() => player.destroy());
  assert.equal(player.el.getAttribute('aria-label'), 'Compare the older version with the newer version');
  assert.deepEqual(player.el.querySelectorAll('.ab-key-label').map((n) => n.textContent), ['Older', 'Newer']);
  assert.match(player.el.querySelector('.cmp-volume-note').textContent, /^The newer version is 1\.9 dB louder, turned down to match\.$/);
  button(player.el, 'Play').click();
  await flush();
  assert.deepEqual([...new Set(env.requests.map((r) => r.path))].sort(), ['/api/library/new/pcm/f-new', '/api/library/old/pcm/f-old']);
  assert.ok(Math.abs(env.contexts[0].gains[2].gain.value - 10 ** (-1.9 / 20)) < 1e-9);

  const unknown = versionCompare(version('a', -15, 'Older'), { ...version('b', null, 'Newer'), entry: { id: 'b', duration_s: 20 } });
  t.after(() => unknown.destroy());
  const match = toggle(unknown.el, 'Match volume');
  assert.equal(match.disabled, true);
  assert.match(unknown.el.querySelector('.cmp-volume-note').textContent, /needs a loudness measurement for both versions/);
});

const enhanced = (id, created_at, lufs) => Object.assign(entry(id), { created_at, loudness: { result_lufs: lufs } });
const compareVersions = (card) => find(card, (n) => n.tagName === 'button' && n.textContent.includes('Compare versions'));

test('Library offers Compare versions within a recording and says when the versions differ in loudness', async (t) => {
  const now = Math.floor(Date.now() / 1000);
  const saved = Object.assign(entry('saved'), { task: 'download', files: [] });
  const env = await librarySetup(t, [enhanced('new', now - 60, -13.5), enhanced('old', now - 7200, -15.4), saved]);
  const hint = document.body.querySelector('.lib-hint');
  assert.equal(hint.textContent, 'These versions differ in loudness (−15.4 to −13.5 LUFS). '
    + 'Louder often sounds better at first, so use Compare versions to hear them at matched volume.');
  assert.ok(compareVersions(env.card('new')));
  assert.equal(compareVersions(env.card('saved')), undefined, 'only Enhance versions with a result');
  const open = compareVersions(env.card('old'));
  open.click({ isTrusted: true });
  const stage = env.card('old').querySelector('.lib-stage');
  assert.equal(open.getAttribute('aria-expanded'), 'true');
  assert.equal(stage.hidden, false);
  assert.match(stage.textContent, /Comparing two versions/);
  assert.equal(stage.querySelector('.lib-version-pick'), null, 'one other version: nothing to pick');
  assert.equal(stage.children.some((n) => n.tagName === '' && n.textContent === 'null'), false, 'no stray text for the missing picker');
  assert.deepEqual(stage.querySelectorAll('.ab-key-label').map((n) => n.textContent), ['Older', 'Newer']);
  assert.match(stage.querySelector('.cmp-note').textContent, /^Older \(.+\): .*Newer \(1 min ago\): /);
  open.click({ isTrusted: true });
  assert.equal(open.getAttribute('aria-expanded'), 'false');
  assert.equal(stage.hidden, true);
});

test('an unverified Library export disables matching and suppresses the loudness hint', async (t) => {
  const known = enhanced('known', 2, -16);
  const unknown = enhanced('unknown', 1, -12);
  unknown.files[0].verification = { status: 'unverified', integrated_lufs: null };
  const env = await librarySetup(t, [known, unknown]);
  assert.equal(document.body.querySelector('.lib-hint'), null);
  const card = env.card('unknown');
  compareVersions(card).click({ isTrusted: true });
  const match = toggle(card, 'Match volume');
  assert.equal(match.disabled, true);
  assert.equal(match.checked, false);
  assert.match(card.querySelector('.cmp-volume-note').textContent, /needs a loudness measurement for both versions/);
});

test('with three versions a picker chooses the other one; versions within 1 dB get no loudness line', async (t) => {
  const now = Math.floor(Date.now() / 1000);
  const env = await librarySetup(t, [enhanced('c', now - 60, -14), enhanced('b', now - 120, -14.5), enhanced('a', now - 180, -13.6)]);
  assert.equal(document.body.querySelector('.lib-hint'), null);
  compareVersions(env.card('b')).click({ isTrusted: true });
  const stage = env.card('b').querySelector('.lib-stage');
  const radios = stage.querySelector('.lib-version-pick').querySelectorAll('input');
  assert.deepEqual(radios.map((r) => [r.value, r.checked]), [['c', true], ['a', false]], 'the newest other one first');
  env.requests.length = 0;
  radios[1].checked = true;
  radios[1].fire('change');
  const keys = env.card('b').querySelector('.lib-stage').querySelectorAll('.ab-key-label').map((n) => n.textContent);
  assert.deepEqual(keys, ['Older', 'Newer']);
  button(env.card('b').querySelector('.lib-stage'), 'Play').click();
  await flush();
  assert.deepEqual([...new Set(env.requests.map((r) => r.path))].sort(), ['/api/library/a/pcm/f0', '/api/library/b/pcm/f0']);
});

// --- Round 4: section EQ and the tone chart in the preview; Trash, Keep only and import in the Library ---
const sectionBand = { hz: 14000, db: -2, q: 0.7 };

test('the preview sends section EQ only when changed, takes the preview range, and [] once cleared', async (t) => {
  const env = setup(t);
  const requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push([path, options?.body && JSON.parse(options.body)]);
    if (path.endsWith('/preview')) return jsonResponse({ id: 'section', status: 'queued' });
    if (path === '/api/jobs/section') return jsonResponse(previewJob());
    assert.fail(`Unexpected request: ${path}`);
  };
  const saved = previewEntry();
  saved.processing.requested.advanced.section_eq = [{ start_s: 20, end_s: 40, fade_s: 5, bands: [sectionBand] }];
  const dismiss = openQuickPreview(saved, null);
  t.after(dismiss);
  const dialog = document.body.querySelector('dialog');
  const make = async () => {
    named(dialog, 'Make preview').click();
    await flush();
    env.runTimers(600);
    await flush();
    return requests.filter(([path]) => path.endsWith('/preview')).at(-1)[1];
  };
  assert.deepEqual((await make()).advanced, {}, 'the saved sections are not sent again');
  named(dialog, 'Add section').click();
  type(field(dialog, 'Start'), '0:42');
  type(field(dialog, 'Length (seconds)'), '15');
  dialog.querySelectorAll('button').filter((b) => b.textContent === 'Use preview range')[1].click();
  const status = find(dialog, (n) => n.getAttribute('role') === 'status');
  named(dialog, 'Make preview').click();
  await flush();
  assert.match(status.textContent, /Fix the section EQ first/, 'a section without bands waits');
  type(dialog.querySelectorAll('textarea').at(-1), '300 Hz +1 dB Q 1');
  assert.deepEqual((await make()).advanced, { section_eq: [
    { start_s: 20, end_s: 40, fade_s: 5, bands: [sectionBand] },
    { start_s: 42, end_s: 57, fade_s: 5, bands: [{ hz: 300, db: 1, q: 1 }] }] });
  button(dialog, 'Remove section 1').click();
  button(dialog, 'Remove section 1').click();
  assert.deepEqual((await make()).advanced, { section_eq: [] });
});

test('the preview fetches the tone chart; a picked point sets the start and the chapter is named', async (t) => {
  setup(t);
  const saved = { ...previewEntry(), has_tone_timeline: true, markers: [{ n: 1, title: 'Intro', start_s: 0 }, { n: 2, title: 'Drop', start_s: 25 }] };
  const tl = { step_s: 10, values: [[0, 0, 0], [1, -1, 2], null, [0.5, 0, -2]],
    bands: [{ key: 'deep_bass', label: 'Deep bass' }, { key: 'mids', label: 'Mids' }, { key: 'top_end', label: 'Top end' }] };
  const fetched = [];
  globalThis.fetch = async (path) => { fetched.push(path); return jsonResponse(tl); };
  const dismiss = openQuickPreview(saved, null);
  t.after(dismiss);
  await flush();
  assert.deepEqual(fetched, ['/api/library/preview-source/tone-timeline']);
  const dialog = document.body.querySelector('dialog');
  const chart = find(dialog, (n) => n.getAttribute('role') === 'slider');
  const lines = (n) => [n, ...n.children.flatMap(lines)].filter((x) => x.getAttribute?.('class') === 'tone-marker');
  assert.equal(lines(dialog).length, 1, 'a line at the second chapter');
  chart.fire('keydown', { key: 'End' });
  assert.equal(chart.getAttribute('aria-valuetext'), '0:30 · Drop · Deep bass +0.5 dB · Mids 0.0 dB · Top end −2.0 dB');
  chart.fire('keydown', { key: 'ArrowLeft' });
  assert.equal(chart.getAttribute('aria-valuetext'), '0:20 · Intro · no usable mid-signal reading');
  chart.fire('keydown', { key: 'Enter' });
  assert.equal(field(dialog, 'Start').value, '0:20');
  assert.match(find(dialog, (n) => n.getAttribute('id')?.endsWith('-range')).textContent, /Section 0:20–0:35/);
});

/** Toasts need their root and a frame callback. */
function toastRoot(t) {
  const root = new FakeNode('div');
  root.id = 'toasts';
  document.body.append(root);
  const raf = Object.getOwnPropertyDescriptor(globalThis, 'requestAnimationFrame');
  globalThis.requestAnimationFrame = (fn) => fn();
  t.after(() => { root.remove(); if (raf) Object.defineProperty(globalThis, 'requestAnimationFrame', raf); else delete globalThis.requestAnimationFrame; });
  return root;
}
/** Answer the Library's write requests (the list itself still comes from the harness). */
function libraryWrites(answer) {
  const calls = [];
  const base = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    if (!['DELETE', 'POST'].includes(options.method)) return base(path, options);
    calls.push([path, options.method, options.body ? JSON.parse(options.body) : null]);
    return jsonResponse(answer(path));
  };
  return calls;
}
const confirmIn = (title) => find(document.body, (n) => n.tagName === 'dialog' && n.textContent.includes(title));
const answerDialog = (dlg, value) => { dlg.returnValue = value; dlg.fire('close'); };

test('the Library totals its files, searches title and artist, sorts by size; rare actions sit in a menu', async (t) => {
  const GB = 1024 ** 3;
  const small = { ...entry('small'), original: { kept: true, size_bytes: GB / 2 }, files: [
    { id: 'f0', name: 'small.wav', kind: 'enhanced', exists: true, size_bytes: GB }, { id: 'f1', name: 'gone.wav', kind: 'enhanced', exists: false, size_bytes: 5 * GB }] };
  // Distinct kept originals count once each (by key); `small` has no key: one per recording.
  const kept = (key) => ({ kept: true, key, size_bytes: GB / 4 });
  const big = { ...entry('big', 'file:big'), original: kept('b1'), files: [{ id: 'f0', name: 'big.wav', kind: 'enhanced', exists: true, size_bytes: 3 * GB }] };
  const bigSets = [big, { ...entry('big2', 'file:big'), original: kept('b1') }, { ...entry('big3', 'file:big'), original: kept('b2') }];
  const env = await librarySetup(t, [small], [{ key: 'file:big', title: 'Big set', artist: 'DJ Test', entries: bigSets }]);
  Object.assign(document, { addEventListener() {}, removeEventListener() {} });
  const page = document.body;
  const titles = () => page.querySelectorAll('.lib-title').map((n) => n.textContent);
  assert.equal(page.querySelector('.page-sub').textContent, '4 results · 4.0 GB of files · kept originals 1.0 GB');
  assert.deepEqual(titles(), ['Generated fixture', 'Big set']);
  const largest = find(page, (n) => n.tagName === 'input' && n.value === 'largest');
  largest.checked = true;
  largest.fire('change');
  assert.deepEqual(titles(), ['Big set', 'Generated fixture']);
  const search = find(page, (n) => n.getAttribute('aria-label') === 'Search the Library');
  search.value = 'dj test';
  search.fire('input');
  assert.deepEqual(titles(), ['Big set']);
  search.value = 'nothing';
  search.fire('input');
  assert.equal(page.querySelectorAll('.filter-empty').find((n) => !n.hidden).textContent, 'Nothing matches “nothing”.');
  search.value = '';
  search.fire('input');

  const more = button(env.card('big'), 'More actions');
  assert.equal(more.getAttribute('aria-haspopup'), 'menu');
  more.click();
  const menu = find(env.card('big'), (n) => n.getAttribute('role') === 'menu');
  assert.equal(menu.hidden, false);
  assert.equal(document.activeElement.textContent, 'Keep only this version', 'focus moves into the menu');
  menu.fire('keydown', { key: 'Escape', stopPropagation() {} });
  assert.equal(menu.hidden, true);
  assert.equal(more.getAttribute('aria-expanded'), 'false');
  assert.equal(document.activeElement, more, 'Esc returns focus to the button');
  more.click();
  big.headline = 'Enhanced again'; // a reload rebuilds the card while its menu has focus
  env.view.show();
  await flush();
  assert.equal(document.activeElement, button(env.card('big'), 'More actions'), 'focus lands on the new ⋯ button');
});

test('Remove and Keep only ask first; files go to the Trash only when the user opts in', async (t) => {
  const env = await librarySetup(t, [entry('one'), entry('two')]);
  const toasts = toastRoot(t);
  const calls = libraryWrites((path) => (path.endsWith('/keep-only') ? { kept: 'one', removed: 1, trashed: [], trash_failed: [] }
    : { deleted: 'x', trashed: ['fixture.wav'], trash_failed: ['cover.jpg'] }));
  named(env.card('one'), 'Remove from Library').click();
  let dlg = confirmIn('Remove from the Library?');
  const trash = find(dlg, (n) => n.getAttribute('role') === 'switch');
  assert.equal(trash.getAttribute('aria-label'), 'Also move its files to the Trash');
  assert.equal(trash.checked, false, 'off by default');
  answerDialog(dlg, 'ok');
  await flush();
  assert.deepEqual(calls.at(-1), ['/api/library/one', 'DELETE', null]);
  named(env.card('two'), 'Remove from Library').click();
  dlg = confirmIn('Remove from the Library?');
  find(dlg, (n) => n.getAttribute('role') === 'switch').checked = true;
  answerDialog(dlg, 'ok');
  await flush();
  assert.deepEqual(calls.at(-1), ['/api/library/two?trash_files=1', 'DELETE', null]);
  assert.match(toasts.textContent, /Removed from the Library\. 1 file moved to the Trash\./);
  assert.match(toasts.textContent, /Couldn’t move this file to the Trash, so it stays where it is: cover\.jpg\./);
  named(env.card('one'), 'Remove from Library').click();
  answerDialog(confirmIn('Remove from the Library?'), 'cancel');
  await flush();
  assert.equal(calls.length, 2, 'Cancel sends nothing');
  named(env.card('one'), 'Keep only this version').click();
  dlg = confirmIn('Keep only this version?');
  assert.equal(find(dlg, (n) => n.getAttribute('role') === 'switch').getAttribute('aria-label'), 'Also move their files to the Trash');
  answerDialog(dlg, 'ok');
  await flush();
  assert.deepEqual(calls.at(-1), ['/api/library/one/keep-only', 'POST', { trash_files: false }]);
});

test('a file can be added as a version; imported entries say so and can be compared', async (t) => {
  const { store } = await import('../../audiolab/web/js/store.js');
  const health = store.health;
  store.health = { local: true };
  t.after(() => { store.health = health; });
  const imported = { ...entry('master'), task: 'import', compare_file_id: null, original: { kept: false },
    loudness: { original_lufs: null, result_lufs: -14.2 }, measured: { integrated_lufs: -14.2, true_peak_dbtp: -1, duration_s: 20 },
    files: [{ id: 'f0', name: 'Master.wav', label: 'WAV', kind: 'imported', exists: true, playable: true, path: '/Users/me/Music/Master.wav' }] };
  const env = await librarySetup(t, [imported, entry('enhanced')]);
  toastRoot(t);
  const calls = libraryWrites(() => ({ id: 'j_import', task: 'import', status: 'queued' }));
  const card = env.card('master');
  assert.match(card.querySelector('.lib-entry-title').textContent, /^Imported file$/);
  assert.equal(card.querySelector('.lib-version').textContent, '−14.2 LUFS · true peak −1.0 dBTP');
  assert.ok(find(card, (n) => n.tagName === 'button' && n.textContent.includes('Compare versions')));
  assert.equal(find(card, (n) => n.tagName === 'button' && n.textContent.includes('Compare with the original')), undefined);
  named(card, 'Remove from Library').click();
  const removal = confirmIn('Remove from the Library?');
  const trash = find(removal, (n) => n.getAttribute('role') === 'switch');
  assert.equal(trash.getAttribute('aria-label'), 'Also move the file to the Trash (it’s in ~/Music, where you added it from)');
  assert.equal(trash.checked, false);
  answerDialog(removal, 'cancel');

  find(document.body, (n) => n.tagName === 'button' && n.textContent.includes('Add a version from a file')).click();
  const dlg = confirmIn('Add a version from a file');
  find(dlg, (n) => n.id?.endsWith('-path')).value = '"/Users/me/Music/Master v2.wav"';
  find(dlg, (n) => n.id?.endsWith('-title')).value = '  Master v2 ';
  find(dlg, (n) => n.tagName === 'form').fire('submit');
  await flush();
  assert.deepEqual(calls, [['/api/library/import', 'POST', { path: '/Users/me/Music/Master v2.wav', title: 'Master v2', group: 'file:test' }]]);
  assert.equal(store.jobs.get('j_import')?.task, 'import', 'the job is tracked like any other');
  assert.equal(dlg.open, false);
});

test('Library same-recording click preserves playback, side and match choice; unrelated groups reset', async (t) => {
  const env = await librarySetup(t, [entry('one'), entry('two'), entry('other', 'file:other')]);
  env.open('one');
  env.seek('one', 8);
  find(env.card('one'), (n) => n.getAttribute('role') === 'radio').click();
  const match = find(env.card('one'), (n) => n.getAttribute('role') === 'switch');
  match.checked = false;
  match.fire('change');
  button(env.card('one'), 'Play').click();
  await flush();
  env.open('two');
  await flush();
  assert.equal(env.card('two').querySelector('.ab-seek').value, 8);
  assert.ok(button(env.card('two'), 'Pause'));
  assert.equal(find(env.card('two'), (n) => n.getAttribute('role') === 'radio').getAttribute('aria-checked'), 'true');
  assert.equal(find(env.card('two'), (n) => n.getAttribute('role') === 'switch').checked, false);
  assert.equal(env.contexts[0].state, 'closed');
  env.open('other');
  assert.equal(env.card('other').querySelector('.ab-seek').value, 0);
  assert.ok(button(env.card('other'), 'Play'));
  assert.equal(find(env.card('other'), (n) => n.getAttribute('role') === 'switch').checked, true);
});

test('Library programmatic opens retain position but do not resume sound', async (t) => {
  const env = await librarySetup(t, [entry('one'), entry('two')]);
  env.open('one');
  env.seek('one', 6);
  button(env.card('one'), 'Play').click();
  await flush();
  env.open('two', false);
  assert.equal(env.card('two').querySelector('.ab-seek').value, 6);
  assert.ok(button(env.card('two'), 'Play'));
  assert.equal(env.contexts.length, 1);
  assert.equal(env.contexts[0].state, 'closed');
});

test('Library repeat range follows versions of the same recording when it fits', async (t) => {
  const env = await librarySetup(t, [entry('one'), entry('two'), entry('shorter', 'file:test', 4)]);
  env.open('one');
  enableRepeat(markRepeat(env.card('one'), 2, 6));
  button(env.card('one'), 'Play').click();
  await flush();
  env.open('two');
  await flush();
  assert.equal(toggle(env.card('two'), 'Repeat').checked, true);
  assert.ok(button(env.card('two'), 'Pause'));
  assert.match(env.card('two').querySelector('.cmp-repeat-note').textContent, /Start 0:02\.00 · End 0:06\.00/);
  env.open('shorter', false);
  assert.equal(toggle(env.card('shorter'), 'Repeat').checked, false);
  assert.equal(toggle(env.card('shorter'), 'Repeat').disabled, true);
  assert.ok(button(env.card('shorter'), 'Play'));
});

test('an unavailable match preserves the user’s volume preference across Library versions', async (t) => {
  const first = entry('one');
  const unavailable = entry('unavailable');
  unavailable.files[0].verification = { status: 'unverified', integrated_lufs: null };
  const env = await librarySetup(t, [first, unavailable, entry('two')]);
  env.open('one');
  const match = toggle(env.card('one'), 'Match volume');
  match.checked = false;
  match.fire('change');
  button(env.card('one'), 'Play').click();
  await flush();
  env.open('unavailable');
  await flush();
  const disabled = toggle(env.card('unavailable'), 'Match volume');
  assert.equal(disabled.disabled, true);
  assert.equal(disabled.checked, false);
  assert.equal(env.contexts.at(-1).gains[2].gain.value, 1);
  env.open('two');
  await flush();
  const next = toggle(env.card('two'), 'Match volume');
  assert.equal(next.disabled, false);
  assert.equal(next.checked, false);
  assert.equal(env.contexts.at(-1).gains[2].gain.value, 1);
});

test('Library plain files and missing group identity never inherit comparison transport', async (t) => {
  const unknown = entry('unknown');
  delete unknown.group;
  const env = await librarySetup(t, [entry('one'), entry('two'), unknown]);
  env.open('one');
  env.seek('one', 7);
  env.card('one').querySelector('.lib-play').click();
  assert.equal(env.card('one').querySelector('.cmp-plain').querySelector('audio').played, true);
  env.open('two');
  assert.equal(env.card('two').querySelector('.ab-seek').value, 0);
  env.seek('two', 9);
  env.open('unknown');
  assert.equal(env.card('unknown').querySelector('.ab-seek').value, 0);
});

test('Library closing the comparison discards continuity state', async (t) => {
  const env = await librarySetup(t, [entry('one'), entry('two')]);
  env.open('one');
  env.seek('one', 5);
  env.open('one'); // the same toggle closes
  env.open('two');
  assert.equal(env.card('two').querySelector('.ab-seek').value, 0);
});
