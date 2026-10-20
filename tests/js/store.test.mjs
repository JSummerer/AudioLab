// Run the production ES modules with controlled HTTP, events and timers. Node 22+,
// no server, browser, build step or npm dependencies.
import assert from 'node:assert/strict';
import { setImmediate as nextTurn } from 'node:timers/promises';
import test from 'node:test';

let imports = 0;

async function setup(t, { events = false, actions = false } = {}) {
  const names = ['fetch', 'localStorage', 'location', 'EventSource',
    'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'];
  const original = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  t.after(() => {
    for (const [name, descriptor] of original) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const requests = [];
  const timers = new Map();
  const intervals = new Map();
  let timerId = 0;
  let source;
  globalThis.fetch = (path, options) => new Promise((resolve, reject) => {
    requests.push({ path, options, resolve, reject });
  });
  globalThis.localStorage = { getItem: () => null };
  globalThis.location = { origin: 'http://127.0.0.1:19999' };
  globalThis.setTimeout = (fn) => { timers.set(++timerId, fn); return timerId; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  globalThis.setInterval = (fn) => { intervals.set(++timerId, fn); return timerId; };
  globalThis.clearInterval = (id) => intervals.delete(id);
  if (events) {
    globalThis.EventSource = class {
      static CLOSED = 2;
      constructor() { this.handlers = new Map(); source = this; }
      addEventListener(kind, handler) { this.handlers.set(kind, handler); }
      close() {}
      send(kind, data) { this.handlers.get(kind)?.({ data: JSON.stringify(data) }); }
    };
  } else delete globalThis.EventSource;
  // The action integration check shares the ordinary store import used by actions.js.
  const state = actions ? await import('../../audiolab/web/js/store.js')
    : await import(`../../audiolab/web/js/store.js?test=${++imports}`);
  return {
    ...state, requests,
    actions: actions ? await import('../../audiolab/web/js/actions.js') : null,
    poll: () => { for (const fn of intervals.values()) fn(); },
    event: (kind, data) => source.send(kind, data),
  };
}

function answer(request, data, status = 200) {
  assert.ok(request, 'the expected request has started');
  request.resolve({ status, ok: status >= 200 && status < 300, text: async () => JSON.stringify(data) });
}

const job = (id, status = 'queued', updated_at = 10) => ({ id, status, updated_at });

test('overlapping fallback polls ignore an older snapshot that finishes last', async (t) => {
  const s = await setup(t);
  let snapshots = 0;
  s.on('jobs', (ids) => { if (ids === null) snapshots += 1; });
  s.connectEvents();
  assert.equal(s.store.live, 'polling');
  s.poll();
  s.poll();
  assert.equal(s.requests.length, 2);
  answer(s.requests[1], { jobs: [job('a', 'done', 20)] });
  await nextTurn();
  answer(s.requests[0], { jobs: [job('a', 'running', 10)] });
  await nextTurn();
  assert.equal(s.store.jobs.get('a').status, 'done');
  assert.equal(s.store.jobsLoaded, true);
  assert.equal(snapshots, 1);
});

test('a pending snapshot preserves direct removal, creation and newer update', async (t) => {
  const s = await setup(t);
  s.upsertJob(job('removed', 'done', 20));
  s.upsertJob(job('updated', 'running', 10));
  const snapshot = s.loadJobs();
  s.removeJobs(['removed']);
  s.upsertJob(job('added', 'queued', 30));
  s.upsertJob(job('updated', 'done', 40));
  answer(s.requests[0], { jobs: [job('removed', 'done', 20), job('updated', 'running', 10)] });
  await snapshot;
  assert.equal(s.store.jobs.has('removed'), false);
  assert.equal(s.store.jobs.get('added').status, 'queued');
  assert.equal(s.store.jobs.get('updated').status, 'done');
});

test('SSE mutations share snapshot replay and never regress a newer snapshot job', async (t) => {
  const s = await setup(t, { events: true });
  s.connectEvents();
  s.event('open');
  assert.equal(s.requests.length, 1);
  s.event('removed', { ids: ['removed'] });
  s.event('job', job('added', 'running', 30));
  s.event('job', job('newer-snapshot', 'running', 30));
  answer(s.requests[0], { jobs: [job('removed'), job('newer-snapshot', 'done', 40)] });
  await nextTurn();
  assert.equal(s.store.jobs.has('removed'), false);
  assert.equal(s.store.jobs.get('added').status, 'running');
  assert.equal(s.store.jobs.get('newer-snapshot').status, 'done');
});

test('a fresh snapshot may remove a row mutated before its own request', async (t) => {
  const s = await setup(t);
  const older = s.loadJobs();
  s.upsertJob(job('remote-removed', 'done', 30));
  const fresh = s.loadJobs();
  answer(s.requests[1], { jobs: [] });
  await fresh;
  assert.equal(s.store.jobs.has('remote-removed'), false);
  answer(s.requests[0], { jobs: [job('remote-removed', 'running', 10)] });
  await older;
  assert.equal(s.store.jobs.has('remote-removed'), false);
});

test('an ignored stale upsert cannot resurrect a row missing from a fresh snapshot', async (t) => {
  const s = await setup(t);
  s.upsertJob(job('a', 'done', 30));
  const snapshot = s.loadJobs();
  s.upsertJob(job('a', 'running', 20));
  answer(s.requests[0], { jobs: [] });
  await snapshot;
  assert.equal(s.store.jobs.has('a'), false);
});

test('failed snapshots release their mutations and leave later reconciliation usable', async (t) => {
  const s = await setup(t);
  const fence = s.jobsFence();
  const failed = s.loadJobs();
  const rejected = assert.rejects(failed, /synthetic rejection/);
  s.upsertJob(job('a', 'done', 30));
  answer(s.requests[0], { detail: 'synthetic rejection' }, 400);
  await rejected;
  assert.equal(s.store.jobsLoaded, false);
  assert.equal(s.mayInsertJob('unseen', fence), true);
  const fresh = s.loadJobs();
  answer(s.requests[1], { jobs: [] });
  await fresh;
  assert.equal(s.store.jobs.has('a'), false);
  assert.equal(s.store.jobsLoaded, true);
  assert.equal(s.mayInsertJob('unseen', fence), false);
});

test('a newer failed snapshot does not suppress an older successful request', async (t) => {
  const s = await setup(t);
  const older = s.loadJobs();
  const newer = s.loadJobs();
  const rejected = assert.rejects(newer, /synthetic rejection/);
  answer(s.requests[1], { detail: 'synthetic rejection' }, 400);
  await rejected;
  answer(s.requests[0], { jobs: [job('a', 'done', 30)] });
  await older;
  assert.equal(s.store.jobs.get('a').status, 'done');
  assert.equal(s.store.jobsLoaded, true);
});

test('single-job fences still reject deleted jobs and jobs absent from a later applied snapshot', async (t) => {
  const s = await setup(t);
  const before = s.jobsFence();
  assert.equal(s.mayInsertJob('a', before), true);
  const snapshot = s.loadJobs();
  const during = s.jobsFence();
  s.removeJobs(['removed']);
  answer(s.requests[0], { jobs: [] });
  await snapshot;
  assert.equal(s.mayInsertJob('removed', during), false);
  assert.equal(s.mayInsertJob('a', before), false);
  assert.equal(s.mayInsertJob('a', during), true);
  s.upsertJob(job('a'));
  assert.equal(s.mayInsertJob('a', during), false);
});

test('delayed job actions cannot restore removed IDs or replay them into an empty snapshot', async (t) => {
  const s = await setup(t, { events: true, actions: true });
  s.connectEvents();
  for (const action of ['cancelJob', 'retryJob']) {
    for (const removal of ['local', 'SSE']) {
      const id = `${action}-${removal}`;
      const original = job(id, action === 'cancelJob' ? 'running' : 'failed', 10);
      s.upsertJob(original);
      const pending = s.actions[action](original, { quiet: true });
      const reply = s.requests.at(-1);
      assert.equal(reply.path, `/api/jobs/${id}/${action === 'cancelJob' ? 'cancel' : 'retry'}`);
      assert.equal(reply.options.method, 'POST');
      // The job has finished while the action's earlier response is delayed.
      s.event('job', job(id, action === 'cancelJob' ? 'cancelled' : 'done', 30));
      const snapshot = s.loadJobs();
      const listReply = s.requests.at(-1);
      if (removal === 'local') {
        const deleted = s.actions.removeJob(s.store.jobs.get(id));
        const deleteReply = s.requests.at(-1);
        assert.equal(deleteReply.options.method, 'DELETE');
        answer(deleteReply, { deleted: id });
        assert.equal(await deleted, true);
      } else s.event('removed', { ids: [id] });
      assert.equal(s.store.jobs.has(id), false, `${id}: removal takes effect`);
      answer(reply, job(id, action === 'cancelJob' ? 'cancelled' : 'queued', removal === 'local' ? 20 : 40));
      assert.equal(await pending, true, `${id}: the accepted action still resolves successfully`);
      assert.equal(s.store.jobs.has(id), false, `${id}: a delayed action cannot revive a removed ID`);
      answer(listReply, { jobs: [] });
      await snapshot;
      assert.equal(s.store.jobs.has(id), false, `${id}: rejected replies cannot enter snapshot replay`);
      s.event('job', job(id, 'done', 50));
      assert.equal(s.store.jobs.has(id), false, `${id}: late live events cannot revive a removed ID`);
    }
  }
  const created = job('new-job', 'queued', 10);
  s.upsertJob(created);
  assert.deepEqual(s.store.jobs.get(created.id), created);
  s.event('job', job(created.id, 'running', 20));
  assert.equal(s.store.jobs.get(created.id).status, 'running');
  const failed = job('valid-retry', 'failed', 10);
  s.upsertJob(failed);
  const retried = s.actions.retryJob(failed, { quiet: true });
  answer(s.requests.at(-1), job(failed.id, 'queued', 20));
  assert.equal(await retried, true);
  assert.equal(s.store.jobs.get(failed.id).status, 'queued');
});

for (const sameKey of [false, true]) {
  for (const firstSucceeds of [false, true]) {
    for (const secondSucceeds of [false, true]) {
      const outcome = (ok) => ok ? 'success' : 'failure';
      test(`settings ${sameKey ? 'same' : 'different'} keys: ${outcome(firstSucceeds)} then ${outcome(secondSucceeds)}`, async (t) => {
        const s = await setup(t);
        let server = { theme: 'auto', keep_originals: true };
        s.store.settings = { ...server };
        const firstPatch = { theme: 'dark' };
        const secondPatch = sameKey ? { theme: 'light' } : { keep_originals: false };
        const first = s.saveSettings(firstPatch);
        const second = s.saveSettings(secondPatch);
        const settled = Promise.allSettled([first, second]);
        assert.deepEqual(s.store.settings, { ...server, ...firstPatch, ...secondPatch });
        await nextTurn();
        assert.equal(s.requests.length, 1, 'only the first PUT may start');
        assert.equal(s.requests[0].options.method, 'PUT');
        assert.deepEqual(JSON.parse(s.requests[0].options.body), firstPatch);
        if (firstSucceeds) server = { ...server, ...firstPatch };
        answer(s.requests[0], firstSucceeds ? server : { detail: 'synthetic rejection' }, firstSucceeds ? 200 : 400);
        await nextTurn();
        assert.equal(s.requests.length, 2, 'the second PUT starts after the first settles');
        assert.deepEqual(JSON.parse(s.requests[1].options.body), secondPatch);
        assert.deepEqual(s.store.settings, { ...server, ...secondPatch }, 'later intent stays visible');
        if (secondSucceeds) server = { ...server, ...secondPatch };
        answer(s.requests[1], secondSucceeds ? server : { detail: 'synthetic rejection' }, secondSucceeds ? 200 : 400);
        const results = await settled;
        assert.deepEqual(results.map((r) => r.status), [firstSucceeds, secondSucceeds].map((ok) => ok ? 'fulfilled' : 'rejected'));
        assert.deepEqual(s.store.settings, server, 'client and ordered server agree after all saves settle');
        if (firstSucceeds) assert.deepEqual(results[0].value, { theme: 'dark', keep_originals: true });
      });
    }
  }
}

test('a new settings save still succeeds after the queue has rejected and drained', async (t) => {
  const s = await setup(t);
  s.store.settings = { theme: 'auto' };
  const failed = s.saveSettings({ theme: 'dark' });
  const rejected = assert.rejects(failed, /Can’t reach the app/);
  await nextTurn();
  s.requests[0].reject(new Error('offline'));
  await rejected;
  assert.deepEqual(s.store.settings, { theme: 'auto' });
  const saved = s.saveSettings({ theme: 'light' });
  assert.deepEqual(s.store.settings, { theme: 'light' });
  await nextTurn();
  answer(s.requests[1], { theme: 'light' });
  await saved;
  assert.deepEqual(s.store.settings, { theme: 'light' });
});

test('settings loads and saves keep invocation order without hiding pending choices', async (t) => {
  const s = await setup(t);
  s.store.settings = { theme: 'auto', keep_originals: true };
  const first = s.saveSettings({ theme: 'dark' });
  const loaded = s.loadSettings();
  const second = s.saveSettings({ keep_originals: false });
  await nextTurn();
  assert.equal(s.requests.length, 1);
  answer(s.requests[0], { theme: 'dark', keep_originals: true });
  await first;
  await nextTurn();
  assert.equal(s.requests.length, 2);
  assert.equal(s.requests[1].options.method, 'GET');
  assert.deepEqual(s.store.settings, { theme: 'dark', keep_originals: false });
  answer(s.requests[1], { theme: 'dark', keep_originals: true, ui_mode: 'advanced' });
  await loaded;
  await nextTurn();
  assert.equal(s.requests.length, 3);
  assert.deepEqual(s.store.settings, { theme: 'dark', keep_originals: false, ui_mode: 'advanced' });
  answer(s.requests[2], { theme: 'dark', keep_originals: false, ui_mode: 'advanced' });
  await second;
  assert.deepEqual(s.store.settings, { theme: 'dark', keep_originals: false, ui_mode: 'advanced' });
});

test('a settings save remains optimistic while the preceding load finishes', async (t) => {
  const s = await setup(t);
  const loaded = s.loadSettings();
  const saved = s.saveSettings({ theme: 'dark' });
  assert.deepEqual(s.store.settings, { theme: 'dark' });
  await nextTurn();
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0].options.method, 'GET');
  answer(s.requests[0], { theme: 'auto', keep_originals: true });
  await loaded;
  await nextTurn();
  assert.deepEqual(s.store.settings, { theme: 'dark', keep_originals: true });
  answer(s.requests[1], { theme: 'dark', keep_originals: true });
  await saved;
  assert.deepEqual(s.store.settings, { theme: 'dark', keep_originals: true });
});

test('a failed settings load does not block a queued save', async (t) => {
  const s = await setup(t);
  s.store.settings = { theme: 'auto' };
  const loaded = s.loadSettings();
  const rejected = assert.rejects(loaded, /synthetic rejection/);
  const saved = s.saveSettings({ theme: 'dark' });
  await nextTurn();
  answer(s.requests[0], { detail: 'synthetic rejection' }, 400);
  await rejected;
  await nextTurn();
  assert.deepEqual(s.store.settings, { theme: 'dark' });
  answer(s.requests[1], { theme: 'dark' });
  await saved;
  assert.deepEqual(s.store.settings, { theme: 'dark' });
});
