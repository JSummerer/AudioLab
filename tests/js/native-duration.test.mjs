import assert from 'node:assert/strict';
import test from 'node:test';

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

test('native duration updates only the displayed direct pick and rechecks its tracklist', async (t) => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  t.after(() => {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  });
  const replies = new Map();
  globalThis.window = {
    pywebview: { api: { duration: (_, selection) => new Promise((resolve) => replies.set(selection, resolve)) } },
  };
  const checked = [];
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/tracklist');
    const request = JSON.parse(options.body);
    checked.push(request);
    const outside = request.duration_s && request.duration_s < 650;
    return { ok: true, text: async () => JSON.stringify({ tracks: [], errors: outside ? ['After the end'] : [] }) };
  };
  const { state } = await import('../../audiolab/web/js/views/new/state.js');
  const { measureFile, setTracklist, tracksProblem, tracksUpFront } = await import('../../audiolab/web/js/views/new/tracks.js');
  const file = (selection, can_measure_duration = true) => ({
    kind: 'file', file: { selection, can_measure_duration },
  });

  const abandoned = state.source = file('old');
  measureFile(abandoned);
  const folderFile = state.source = file('folder', false);
  measureFile(folderFile);
  assert.deepEqual([...replies.keys()], ['old']);

  const current = state.source = file('current');
  measureFile(current);
  replies.get('old')(800);
  await flush();
  assert.equal(abandoned.duration_s, undefined);
  assert.equal(tracksUpFront(), false);

  setTracklist(current, '0:00 Opening\n10:50 Encore');
  await flush();
  assert.equal(tracksProblem(), null);
  replies.get('current')(620);
  await flush();
  assert.equal(current.duration_s, 620);
  assert.deepEqual(checked.map((r) => r.duration_s), [undefined, 620]);
  assert.match(tracksProblem(), /Fix the tracklist/);

  const long = state.source = file('long');
  measureFile(long);
  replies.get('long')(900);
  await flush();
  assert.equal(tracksUpFront(), true);

  const unknown = state.source = file('unknown');
  measureFile(unknown);
  replies.get('unknown')(null);
  await flush();
  assert.equal(unknown.duration_s, undefined);
  assert.equal(tracksUpFront(), false);
});
