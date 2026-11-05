// Production comparison and Library modules, with finite DOM/Web Audio/HTTP fakes.
// No browser, network, personal audio, or npm dependency is used by these regressions.
import assert from 'node:assert/strict';
import test from 'node:test';
import { comparePlayer, libraryCompare } from '../../audiolab/web/js/compare.js';

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
    createElement: (tag) => new FakeNode(tag), createTextNode: (text) => new FakeNode('', text),
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
async function librarySetup(t, entries) {
  const env = setup(t);
  env.library = { groups: [{ key: 'file:test', title: 'Generated fixture', entries }], count: entries.length };
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
  return { ...env, card, open, seek };
}

const entry = (id, group = 'file:test', duration_s = 20) => ({ id, group, duration_s,
  task: 'enhance', title: 'Generated fixture', source: { kind: 'file', name: 'fixture.wav' },
  original: { kept: true }, loudness: { original_lufs: -20, result_lufs: -14 }, compare_file_id: 'f0',
  files: [{ id: 'f0', name: 'fixture.wav', label: 'WAV', kind: 'enhanced', mime: 'audio/wav', playable: true, exists: true }] });

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
