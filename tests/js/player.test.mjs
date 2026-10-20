// Exercise actual production ES modules with finite DOM/audio fakes; no browser or npm dependencies.
import assert from 'node:assert/strict';
import test from 'node:test';
import { stemRows, trackRows } from '../../audiolab/web/js/player.js';

class FakeNode {
  constructor(tag = '', text = '') {
    this.tagName = tag;
    this.children = [];
    this.attributes = new Map();
    this.handlers = new Map();
    this.className = '';
    this.textContent = text;
    this.hidden = false;
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, on) => {
        const classes = new Set(this.className.split(/\s+/).filter(Boolean));
        if (on) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(' ');
      },
    };
  }
  setAttribute(key, value) { this.attributes.set(key, value); if (key === 'hidden') this.hidden = true; }
  getAttribute(key) { return this.attributes.get(key); }
  append(...children) { this.children.push(...children); }
  get firstChild() { return this.children[0] || null; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener(kind, handler) {
    if (!this.handlers.has(kind)) this.handlers.set(kind, []);
    this.handlers.get(kind).push(handler);
  }
  click() { return this.handlers.get('click')[0](); }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) {
    const match = find(child, predicate);
    if (match) return match;
  }
  return null;
}

function setup(t, kind) {
  const names = ['Node', 'document', 'Audio', 'localStorage', 'location'];
  const original = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  t.after(() => {
    for (const [name, descriptor] of original) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const attempts = [];
  globalThis.Node = FakeNode;
  globalThis.document = {
    createElement: (tag) => new FakeNode(tag),
    createTextNode: (text) => new FakeNode('', text),
  };
  globalThis.location = { origin: 'http://127.0.0.1:19999' };
  globalThis.localStorage = { getItem: () => null };
  globalThis.Audio = class {
    constructor() {
      this.handlers = new Map();
      this.paused = false;
      this.removed = false;
      this.pending = new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject; });
      attempts.push(this);
    }
    play() { return this.pending; }
    pause() { this.paused = true; }
    removeAttribute(key) { if (key === 'src') this.removed = true; }
    addEventListener(kind, handler) { this.handlers.set(kind, handler); }
    fire(kind) { this.handlers.get(kind)?.(); }
  };
  const files = [
    { id: 'f0', label: 'Alpha', name: 'alpha.wav' },
    { id: 'f1', label: 'Beta', name: 'beta.wav' },
  ];
  const player = kind === 'tracks' ? trackRows('test', files) : stemRows('test', [
    { key: 'stem:vocals', label: 'Alpha', file_id: 'f0' },
    { key: 'stem:drums', label: 'Beta', file_id: 'f1' },
  ], files);
  const rows = player.el.children.map((el) => ({
    el,
    button: find(el, (node) => node.tagName === 'button'),
    notice: find(el, (node) => node.getAttribute('role') === 'status'),
    download: find(el, (node) => node.tagName === 'a'),
  }));
  return { player, rows, attempts };
}

function playing(row, expected) {
  assert.equal(row.el.classList.contains('is-playing'), expected);
  assert.match(row.button.getAttribute('aria-label'), expected ? /^Stop / : /^Play /);
}

for (const kind of ['tracks', 'stems']) {
  test(`${kind}: rejected play clears row state, announces failure and retains download`, async (t) => {
    const { rows: [row], attempts } = setup(t, kind);
    const pending = row.button.click();
    playing(row, true);
    attempts[0].reject(new Error('unsupported media'));
    await pending;
    playing(row, false);
    assert.equal(row.notice.hidden, false);
    assert.match(row.notice.textContent, /couldn’t be played.*downloading/);
    assert.equal(row.notice.getAttribute('aria-live'), 'polite');
    assert.ok(row.download.getAttribute('href'));
    assert.equal(attempts[0].paused, true);
    assert.equal(attempts[0].removed, true);
  });

  test(`${kind}: an older rejected play does not stop the newer row`, async (t) => {
    const { rows: [a, b], attempts } = setup(t, kind);
    const first = a.button.click();
    const second = b.button.click();
    attempts[1].resolve();
    await second;
    attempts[0].reject(new Error('superseded'));
    await first;
    playing(a, false);
    playing(b, true);
    assert.equal(a.notice.hidden, true);
    assert.equal(b.notice.hidden, true);
    assert.equal(attempts[1].paused, false);
  });

  test(`${kind}: A → B → A ignores the first A rejection and stale media events`, async (t) => {
    const { rows: [a, b], attempts } = setup(t, kind);
    const first = a.button.click();
    const second = b.button.click();
    const third = a.button.click();
    attempts[2].resolve();
    await third;
    attempts[0].reject(new Error('old Alpha'));
    attempts[1].reject(new Error('old Beta'));
    await Promise.all([first, second]);
    attempts[0].fire('error');
    attempts[1].fire('ended');
    playing(a, true);
    playing(b, false);
    assert.equal(a.notice.hidden, true);
    assert.equal(b.notice.hidden, true);
  });

  test(`${kind}: stop invalidates a pending attempt and permits retry`, async (t) => {
    const { rows: [row], attempts } = setup(t, kind);
    const pending = row.button.click();
    await row.button.click();
    playing(row, false);
    attempts[0].reject(new Error('stopped'));
    await pending;
    attempts[0].fire('error');
    assert.equal(row.notice.hidden, true);
    const retry = row.button.click();
    attempts[1].resolve();
    await retry;
    playing(row, true);
  });

  test(`${kind}: destroy invalidates pending play, errors and later clicks`, async (t) => {
    const { player, rows: [row], attempts } = setup(t, kind);
    const pending = row.button.click();
    player.destroy();
    playing(row, false);
    attempts[0].reject(new Error('destroyed'));
    await pending;
    attempts[0].fire('error');
    await row.button.click();
    assert.equal(row.notice.hidden, true);
    assert.equal(attempts.length, 1);
  });

  test(`${kind}: media error after success clears state, and a retry clears the notice`, async (t) => {
    const { rows: [row], attempts } = setup(t, kind);
    const pending = row.button.click();
    attempts[0].resolve();
    await pending;
    attempts[0].fire('error');
    playing(row, false);
    assert.equal(row.notice.hidden, false);
    const retry = row.button.click();
    assert.equal(row.notice.hidden, true);
    attempts[1].resolve();
    await retry;
    playing(row, true);
    attempts[1].fire('ended');
    playing(row, false);
    assert.equal(row.notice.hidden, true);
  });
}
