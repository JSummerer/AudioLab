import assert from 'node:assert/strict';
import test from 'node:test';
import { graphicalEqField } from '../../audiolab/web/js/eq-editor.js';

class Element {
  constructor(tag, text = '') {
    this.tag = tag;
    this.textContent = text;
    this.children = [];
    this.attrs = new Map();
    this.listeners = new Map();
    this.style = {};
    this.classList = { toggle() {} };
  }
  get firstChild() { return this.children[0]; }
  setAttribute(key, value) { this.attrs.set(key, String(value)); }
  setCustomValidity(message) { this.validationMessage = message; }
  append(...children) { this.children.push(...children); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  addEventListener(kind, callback) { this.listeners.set(kind, callback); }
  focus() { document.activeElement = this; }
  getBoundingClientRect() { return { width: 600, height: 200 }; }
  setPointerCapture(id) { this.pointer = id; }
  hasPointerCapture(id) { return this.pointer === id; }
  releasePointerCapture() { this.pointer = null; }
  fire(kind, data = {}) { this.listeners.get(kind)?.({ target: this, preventDefault() {}, ...data }); }
}

function setup(t) {
  globalThis.Node = Element;
  globalThis.document = {
    createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
    createTextNode: (text) => new Element('', text),
  };
  t.after(() => { delete globalThis.Node; delete globalThis.document; });
}

const find = (node, match) => match(node) ? node : node.children.map((child) => find(child, match)).find(Boolean);
const named = (editor, label) => find(editor.el, (node) => node.attrs.get('aria-label') === label);
const click = (editor, label) => {
  const target = find(editor.el, (node) => node.tag === 'button' && node.children[0]?.textContent === label);
  assert.equal(target.disabled, false, `${label} is enabled`);
  target.fire('click');
};
const enter = (element, value) => { element.value = value; element.fire('input'); };
const saved = [{ hz: 1000, db: 1, q: 1 }];

test('graph, numeric input and text share normalized EQ; bypass, undo and reset survive navigation', (t) => {
  setup(t);
  let emitted;
  let editor = graphicalEqField({ bands: saved, savedBands: saved, onInput: (bands, { errors }) => { emitted = { bands, errors }; } });
  const initial = named(editor, 'Band 1, 1000 Hz, +1.00 dB, Q 1');
  initial.fire('keydown', { key: 'ArrowUp' });
  assert.deepEqual(emitted, { bands: [{ ...saved[0], db: 1.1 }], errors: [] });
  assert.equal(named(editor, 'Band 1 Gain (dB)').value, '1.1');
  const freq = named(editor, 'Band 1 Frequency (Hz)');
  enter(freq, '1275.05');
  freq.fire('blur');
  assert.equal(freq.value, '1275.1');
  const text = named(editor, 'Custom EQ as text');
  assert.match(text.value, /^1.2751 kHz \+1.1 dB/);
  enter(text, 'Filter: ON PK Fc 2.6785 kHz Gain -1.125 dB Q 2.125');
  const changed = [{ hz: 2678.5, db: -1.13, q: 2.13 }];
  assert.deepEqual(editor.read().bands, changed);
  assert.equal(named(editor, 'Band 1 Width (Q)').value, '2.13');
  click(editor, 'Bypass EQ');
  assert.deepEqual(editor.read().bands, []);
  editor = graphicalEqField({ draftState: editor.snapshot(), savedBands: saved });
  assert.deepEqual(editor.read().bands, []);
  click(editor, 'Undo EQ edit');
  assert.deepEqual(editor.read().bands, changed);
  click(editor, 'Reset EQ to saved');
  assert.deepEqual(editor.read().bands, saved);
});

test('a drag creates one undo step and invalid numeric or text drafts remain reviewable', (t) => {
  setup(t);
  let editor = graphicalEqField({ bands: saved, savedBands: saved });
  const handle = named(editor, 'Band 1, 1000 Hz, +1.00 dB, Q 1');
  handle.fire('pointerdown', { pointerId: 1, button: 0, clientX: 300, clientY: 80 });
  handle.fire('pointermove', { pointerId: 1, clientX: 330, clientY: 70 });
  handle.fire('pointermove', { pointerId: 1, clientX: 340, clientY: 60 });
  handle.fire('pointerup', { pointerId: 1 });
  assert.deepEqual(editor.read().bands, [{ hz: 1584.9, db: 2.8, q: 1 }]);
  click(editor, 'Undo EQ edit');
  assert.deepEqual(editor.read().bands, saved);
  enter(named(editor, 'Band 1 Gain (dB)'), '7');
  assert.equal(find(editor.el, (node) => node.tag === 'button' && node.children[0]?.textContent === 'Sub focus').disabled, true);
  editor = graphicalEqField({ draftState: editor.snapshot(), savedBands: saved });
  assert.deepEqual(editor.read().bands, saved);
  assert.match(editor.read().errors[0].message, /Gain must be/);
  assert.equal(named(editor, 'Band 1 Gain (dB)').value, '7');
  enter(named(editor, 'Band 1 Gain (dB)'), '   ');
  assert.match(editor.read().errors[0].message, /Gain must be/);
  click(editor, 'Undo EQ edit');
  assert.deepEqual(editor.read(), { bands: saved, errors: [] });
  enter(named(editor, 'Custom EQ as text'), 'unfinished');
  editor = graphicalEqField({ draftState: editor.snapshot(), savedBands: saved });
  assert.equal(named(editor, 'Custom EQ as text').value, 'unfinished');
  assert.ok(editor.read().errors.length);
  assert.deepEqual(editor.read().bands, saved);
});

test('a cut-only curve summarizes its flat skirt as 0.0 dB, not −0.0', (t) => {
  setup(t);
  const editor = graphicalEqField({ bands: [{ hz: 160, db: -3.31, q: 2.865 }] });
  const note = find(editor.el, (node) => node.textContent.startsWith('Your EQ'));
  assert.equal(note.textContent, 'Your EQ · −3.3 to 0.0 dB');
});
