// The AI add-on row of Settings → AI add-on, rendered from the production module
// (see settings-harness.mjs). Lifecycle controls and Storage refresh timing, not layout.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import test from 'node:test';
import { setup as setupSettings, text } from './settings-harness.mjs';

function setup(ai, options) {
  const s = setupSettings({ packaged: true, ai, ffmpeg: { ok: true } }, options);
  return {
    ...s,
    button: s.find,
    render: (next) => { s.context.store.health.ai = next; s.run('renderAi()'); },
  };
}
const managed = (patch = {}) => ({
  managed: true, install_supported: true, available: false, installing: false,
  state: 'missing', target_version: 'ai-2', ...patch,
});

test('AI completion refreshes Storage once after caches expire, not on download progress', () => {
  const s = setup(managed());
  assert.equal(s.scheduled.length, 0);
  s.render(managed({ installing: true, state: 'downloading', downloaded_bytes: 10 }));
  s.render(managed({ installing: true, state: 'downloading', downloaded_bytes: 100 }));
  s.render(managed({ installing: true, state: 'checking' }));
  assert.equal(s.scheduled.length, 0);
  const ready = managed({ available: true, version: 'ai-2', state: 'ready', removable: true, install_ok: true });
  s.render(ready);
  assert.equal(s.scheduled.length, 1);
  assert.equal(s.scheduled[0].delay, 6000);
  s.render(ready);
  assert.equal(s.scheduled.length, 1);
});

test('a pending Storage refresh survives leaving and reopening Settings inside the cache lifetime', async () => {
  const s = setup(managed({ installing: true, state: 'checking' }));
  let serial = 0;
  const timers = new Map();
  s.context.setTimeout = (callback, delay) => { const id = ++serial; timers.set(id, { callback, delay }); return id; };
  s.context.clearTimeout = (id) => timers.delete(id);
  s.context.closeInfo = () => {};
  let bytes = 10;
  s.context.api = async () => ({ jobs_bytes: 0, models_bytes: bytes });
  vm.runInNewContext(`
    scroller = { addEventListener() {}, removeEventListener() {} };
    spy = () => {};
    renderAll = () => renderAi();
    renderStorageInfo = () => {};
  `, s.context);
  const terminal = managed({ state: 'partial', removable: true, install_ok: false });
  s.render(terminal);
  assert.equal(timers.size, 1);
  vm.runInNewContext('settingsView.hide()', s.context);
  assert.equal(timers.size, 0);
  vm.runInNewContext('settingsView.show()', s.context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInNewContext('storage.data.models_bytes', s.context), 10);
  assert.equal(timers.size, 1, 'cached immediate response still has a delayed refresh');
  const [id, timer] = [...timers][0];
  s.render(terminal);
  assert.equal([...timers][0][0], id, 'unchanged health must not postpone the refresh');
  assert.equal(timer.delay, 6000);
  bytes = 100; // both server caches have now expired
  timers.delete(id);
  await timer.callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInNewContext('storage.data.models_bytes', s.context), 100);
  assert.equal(vm.runInNewContext('storage.refreshPending', s.context), false);
  assert.equal(timers.size, 0);
});

test('Storage refresh covers cancellation and a failed repair between health polls', () => {
  for (const terminal of [
    managed({ state: 'partial', removable: true, install_ok: false }),
    managed({ available: true, state: 'ready', version: 'ai-2', removable: true, install_ok: false }),
  ]) {
    const s = setup(managed({ installing: true, state: 'checking' }));
    s.render(terminal);
    assert.equal(s.scheduled.length, 1);
    s.render(terminal);
    assert.equal(s.scheduled.length, 1);
  }
});

test('Settings harness accepts Windows checkout line endings', () => {
  const s = setup(managed(), { crlf: true });
  assert.equal(text(s.button('enableAi')), 'Enable AI features');
});

test('before install: one size (the download) and the existing install endpoint', async () => {
  const s = setup(managed({ download_bytes: 2048, target_installed_bytes: 8192, installed_bytes: 512 }));
  assert.equal(text(s.button('enableAi')), 'Enable AI features');
  assert.match(s.text(), /Download: 2.0 KB\./);
  assert.doesNotMatch(s.text(), /8\.0 KB|512 B/);
  await s.button('enableAi').onclick();
  assert.equal(s.requests[0].path, '/api/system/install-ai');
  assert.equal(s.requests[0].method, 'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(s.requests[0].json)), {});
});

test('a working install offers no repair or install, only removal, and says its disk use', () => {
  const s = setup(managed({ available: true, state: 'ready', version: 'ai-2', removable: true, installed_bytes: 4096, download_bytes: 2048 }));
  assert.equal(s.button('enableAi'), null);
  assert.equal(s.button('repairAi'), null);
  assert.ok(s.button('removeAi'));
  assert.match(s.text(), /Uses 4\.0 KB on disk\./);
  assert.doesNotMatch(s.text(), /Download:/);
});

test('repair in progress shows cancel even if the previous runtime remains available', async () => {
  const s = setup(managed({ available: true, state: 'checking', installing: true, removable: true }));
  assert.match(s.text(), /Checking the AI add-on/);
  assert.equal(s.button('removeAi'), null);
  await s.button('cancelAi').onclick();
  assert.equal(s.requests[0].path, '/api/system/cancel-ai');
  assert.equal(s.requests[0].method, 'POST');
});

test('partial download resumes and incompatible installation updates', async () => {
  const s = setup(managed({ state: 'partial', downloaded_bytes: 1024, removable: true }));
  assert.equal(text(s.button('enableAi')), 'Resume AI download');
  s.render(managed({ state: 'incompatible', version: 'ai-1', removable: true }));
  assert.equal(text(s.button('enableAi')), 'Update AI add-on');
  await s.button('enableAi').onclick();
  assert.equal(s.requests[0].json.repair, undefined);
});

test('broken install exposes explicit repair; failed repair log remains visible for working AI', async () => {
  const s = setup(managed({ state: 'broken', version: 'ai-2', removable: true }));
  assert.equal(text(s.button('enableAi')), 'Repair AI add-on');
  await s.button('enableAi').onclick();
  assert.equal(s.requests[0].json.repair, true);
  s.render(managed({ available: true, state: 'ready', install_ok: false, install_log: 'Connection interrupted' }));
  assert.match(s.text(), /last install did not finish/);
  assert.match(s.text(), /Connection interrupted/);
});

test('owned files can be removed without a catalog and cancellation of confirmation preserves them', async () => {
  const s = setup(managed({ install_supported: false, state: 'unsupported', removable: true }));
  assert.equal(s.button('enableAi'), null);
  s.context.confirmDialog = async () => false;
  await s.button('removeAi').onclick();
  assert.equal(s.requests.length, 0);
  s.context.confirmDialog = async () => true;
  await s.button('removeAi').onclick();
  assert.equal(s.requests[0].path, '/api/system/ai');
  assert.equal(s.requests[0].method, 'DELETE');
});

test('source installations never expose managed removal or cancellation', () => {
  const s = setup({ managed: false, install_supported: true, available: false, removable: true });
  assert.ok(s.button('enableAi'));
  assert.equal(s.button('removeAi'), null);
  s.render({ install_supported: true, installing: true });
  assert.equal(s.button('cancelAi'), null);
});

test('a busy rejection is reported and controls become available for retry', async () => {
  const s = setup(managed());
  const busy = new Error('The AI pack is in use.');
  s.context.api = async () => { throw busy; };
  await s.button('enableAi').onclick();
  assert.equal(s.errors[0], busy);
  assert.equal(s.button('enableAi').disabled, false);
});

test('concurrent clicks are blocked across health renders until the request finishes', async () => {
  const s = setup(managed());
  let finish;
  let calls = 0;
  s.context.api = () => { calls += 1; return new Promise((resolve) => { finish = resolve; }); };
  const pending = s.button('enableAi').onclick();
  assert.equal(s.button('enableAi').disabled, true);
  s.render(managed());
  await s.button('enableAi').onclick();
  assert.equal(calls, 1);
  finish();
  await pending;
  assert.equal(s.button('enableAi').disabled, false);
});
