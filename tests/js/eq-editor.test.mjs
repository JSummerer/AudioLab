import assert from 'node:assert/strict';
import test from 'node:test';
import { snapshotDraft, restoreDraft } from '../../audiolab/web/js/adjust-draft.js';
import { adjustView } from '../../audiolab/web/js/views/adjust.js';
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

test('Adjust limits raw EQ text, preset additions and invalid numeric rows to what a draft can save', (t) => {
  setup(t);
  const editor = graphicalEqField({ bands: saved, savedBands: saved });
  const gain = named(editor, 'Band 1 Gain (dB)');
  enter(gain, '9'.repeat(129));
  assert.equal(gain.value, '9'.repeat(128));
  assert.equal(editor.snapshot().numbers[0].db, gain.value);
  enter(gain, '1');
  const raw = named(editor, 'Custom EQ as text');
  assert.equal(raw.attrs.get('maxlength'), '4096');
  const full = '#' + 'x'.repeat(4095);
  enter(raw, full);
  click(editor, 'Sub focus');
  assert.equal(editor.snapshot().text, full, 'a preset cannot overflow the draft or overwrite existing text');
  assert.ok(find(editor.el, (node) => node.textContent.includes('Shorten the EQ text')));
});

test('a cut-only curve summarizes its flat skirt as 0.0 dB, not −0.0', (t) => {
  setup(t);
  const editor = graphicalEqField({ bands: [{ hz: 160, db: -3.31, q: 2.865 }] });
  const note = find(editor.el, (node) => node.textContent.startsWith('Your EQ'));
  assert.equal(note.textContent, 'Your EQ · −3.3 to 0.0 dB');
});

test('reopening a durable Adjust snapshot restores invalid EQ text and settings without Undo history or job state', (t) => {
  setup(t);
  const editor = graphicalEqField({ bands: saved, savedBands: saved });
  named(editor, 'Band 1, 1000 Hz, +1.00 dB, Q 1').fire('keydown', { key: 'ArrowUp' });
  enter(named(editor, 'Custom EQ as text'), 'unfinished');
  const base = () => ({ bands: saved, sections: [], graphs: new Map(), applying: false });
  const draft = { ...base(), from: '0:', length: '12', values: { bass_db: '-' }, scope: 'global', applying: true };
  const data = JSON.parse(JSON.stringify(snapshotDraft(draft, new Map([['global', editor]]))));
  const restored = base();
  restoreDraft(restored, data);
  assert.equal(restored.from, '0:');
  assert.deepEqual(restored.values, { bass_db: '-' });
  assert.equal(restored.applying, false);
  assert.equal(restored.graphs.get('global').invalid, true);
  assert.deepEqual(restored.bands, [{ hz: 1000, db: 1.1, q: 1 }]);
  const reopened = graphicalEqField({ draftState: restored.graphs.get('global').snapshot, savedBands: saved });
  assert.equal(named(reopened, 'Custom EQ as text').value, 'unfinished');
  assert.ok(reopened.read().errors.length);
  assert.deepEqual(reopened.snapshot().history, []);
});

test('re-entering Adjust follows another window’s saved or discarded revision while preserving local conflicts', async (t) => {
  setup(t);
  globalThis.window = new EventTarget();
  t.after(() => { adjustView.hide(); delete globalThis.window; });
  const id = 'draft-route-test';
  const data = (value) => ({ from: '0:00', length: '15', scope: 'global', sections: [], values: { target_lufs: value }, graphs: {} });
  let record = { basis: 'source', revision: 0, data: null };
  const writes = [];
  let readFails = false;
  t.mock.method(globalThis, 'fetch', async (path, opts) => {
    if (opts.method === 'GET' && readFails) return Response.json({ detail: 'Offline' }, { status: 503 });
    if (opts.method === 'GET') return Response.json({
      id, task: 'enhance', original: { kept: true, key: 'source' }, adjust_draft: record, adjust_quality: 'light',
      processing: { requested: { recipe: 'music', quality: 'light', formats: ['wav16'] },
        resolved: { recipe: { key: 'music' }, params: { volume_only: true, target_lufs: -14, tp_ceiling_db: -1 } } },
    });
    assert.equal(path, `/api/library/${id}/adjust-draft`);
    const edit = JSON.parse(opts.body);
    writes.push({ ...edit, keepalive: opts.keepalive });
    if (edit.revision !== record.revision) return Response.json({ detail: 'Draft changed.' }, { status: 409 });
    record = { ...edit, revision: edit.revision + 1 };
    return Response.json({ revision: record.revision });
  });
  const root = new Element('main');
  adjustView.mount(root);
  const number = () => find(root, (n) => n.className?.includes('adjust-level'));
  const reopen = async () => { adjustView.hide(); await adjustView.show(id); };
  const next = () => new Promise((resolve) => setImmediate(resolve));
  await adjustView.show(id);
  assert.equal(number().attrs.get('maxlength'), '128');
  assert.equal(find(root, (n) => n.attrs.get('spellcheck') === 'false').attrs.get('maxlength'), '128');
  enter(number(), '-12');
  adjustView.hide(); // normal navigation flushes the first window's edit
  await next();
  assert.deepEqual(writes.map((w) => [w.revision, w.keepalive]), [[0, false]]);
  record = { ...record, revision: 2, data: data('-18') }; // the other window saved
  await adjustView.show(id);
  assert.equal(number().value, '-18');
  record = { ...record, revision: 3, data: null }; // the other window discarded its draft
  await reopen();
  assert.equal(number().value, '');
  assert.equal(number().attrs.get('placeholder'), '-14');

  enter(number(), '-11');
  record = { ...record, revision: 4, data: data('-20') }; // a race with our pending write
  window.dispatchEvent(new Event('pagehide'));
  await next();
  assert.deepEqual(writes.map((w) => [w.revision, w.keepalive]), [[0, false], [3, true]]);
  await reopen();
  assert.equal(number().value, '-11');
  const loadSaved = find(root, (n) => n.tag === 'button' && n.children[0]?.textContent === 'Load saved draft');
  assert.equal(loadSaved.hidden, false);
  assert.equal(record.data.values.target_lufs, '-20');

  // The real confirmation runs before the workspace replaces its cached draft.
  document.body = new Element('body');
  const create = document.createElement;
  let answer = 'cancel';
  document.createElement = (tag) => {
    const el = create(tag);
    if (tag === 'dialog') {
      el.remove = () => document.body.removeChild(el);
      el.showModal = () => { el.returnValue = answer; el.fire('close'); };
    }
    return el;
  };
  loadSaved.fire('click');
  await next();
  assert.equal(number().value, '-11');
  answer = 'ok';
  readFails = true;
  loadSaved.fire('click');
  await next();
  assert.equal(number().value, '-11', 'a failed read leaves the local draft available');
  readFails = false;
  loadSaved.fire('click');
  await next();
  assert.equal(number().value, '-20');
  enter(number(), '-10');
  adjustView.hide();
  await next();
  assert.equal(record.revision, 5);
  assert.equal(record.data.values.target_lufs, '-10');

  await adjustView.show(id);
  enter(number(), '-9');
  record = { basis: 'different-original', revision: 0, data: null };
  await reopen();
  assert.equal(number().value, '-9', 'retain a conflicting local edit against its prior basis for review');
  const apply = find(root, (n) => n.tag === 'button' && n.children[0]?.textContent === 'Apply to full recording');
  assert.equal(apply.disabled, true);
});
