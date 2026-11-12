import test from 'node:test';
import assert from 'node:assert/strict';
import { auditionRequests } from '../../audiolab/web/js/adjust-audition.js';
import { auditionPower, loadAuditionSpectrum } from '../../audiolab/web/js/audition-spectrum.js';
import { basisMessage } from '../../audiolab/web/js/views/adjust.js';

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const nextTurn = () => new Promise((resolve) => setImmediate(resolve));

test('leaving while submitting releases the late audition, without displaying it', async () => {
  const submission = deferred();
  const states = [];
  const calls = [];
  const requests = auditionRequests('/api/library/entry', {
    onState: (state) => states.push(state), delay: async () => {},
    request: async (path, options) => {
      const method = options?.method ?? 'GET';
      calls.push([method, path]);
      if (path.endsWith('/preview')) return submission.promise;
      return { id: 'discarded', status: 'cancelled' };
    },
  });
  const started = requests.start({ start_s: 0, duration_s: 5, advanced: {} });
  requests.dispose();
  submission.resolve({ id: 'discarded', status: 'queued' });
  await started;
  await nextTurn();
  assert.deepEqual(calls, [
    ['POST', '/api/library/entry/preview'],
    ['POST', '/api/jobs/discarded/cancel'],
    ['GET', '/api/jobs/discarded'],
    ['DELETE', '/api/jobs/discarded'],
  ]);
  assert.deepEqual(states, [{ busy: true, message: 'Starting audition…' }]);
});

test('late completion for stale settings cannot replace the new audition', async () => {
  const firstPoll = deferred();
  const sentPoll = deferred();
  const states = [];
  const deleted = [];
  let submissions = 0;
  const requests = auditionRequests('/api/library/entry', {
    onState: (state) => states.push(state), delay: async () => {},
    request: async (path, options) => {
      if (path.endsWith('/preview')) {
        submissions++;
        assert.equal(options.json.paired, true);
        assert.equal(options.json.advanced.target_lufs, submissions === 1 ? -15 : -17);
        return { id: submissions === 1 ? 'old' : 'new', status: submissions === 1 ? 'queued' : 'done' };
      }
      if (options?.method === 'DELETE') { deleted.push(path); return {}; }
      if (options?.signal) { sentPoll.resolve(); return firstPoll.promise; }
      return { id: 'old', status: 'done' };
    },
  });
  const earlier = requests.start({ advanced: { target_lufs: -15 } });
  await sentPoll.promise;
  requests.reset('Settings changed.');
  await requests.start({ advanced: { target_lufs: -17 } });
  firstPoll.resolve({ id: 'old', status: 'done' });
  await earlier;
  assert.deepEqual(states.filter((state) => state.job).map(({ job }) => job.id), ['new']);
  assert.deepEqual(deleted, ['/api/jobs/old']);
  requests.dispose();
  await nextTurn();
  assert.deepEqual(deleted, ['/api/jobs/old', '/api/jobs/new']);
});

test('a transient poll error retries the audition without resubmitting', async () => {
  let submits = 0;
  let polls = 0;
  const states = [];
  const requests = auditionRequests('/api/library/entry', {
    onState: (state) => states.push(state), delay: async () => {},
    request: async (path, options) => {
      if (path.endsWith('/preview')) {
        submits++;
        return { id: 'work', status: 'queued', queue_position: 2 };
      }
      if (options?.method === 'DELETE') return {};
      if (++polls === 1) throw Object.assign(new Error('Temporary error'), { status: 0 });
      return { id: 'work', status: 'done' };
    },
  });
  await requests.start({ start_s: 0, duration_s: 5, advanced: {} });
  assert.equal(submits, 1);
  assert.equal(polls, 2);
  assert.ok(states.some((state) => state.message?.includes('#2 in line')));
  assert.ok(states.some((state) => state.message?.includes('Trying again')));
  assert.equal(states.at(-1).job.id, 'work');
  requests.dispose();
});

test('opposing stereo has the same mean power as in-phase stereo', () => {
  const frames = 48000;
  const peakBin = 128;
  const inPhase = new Float32Array(2 * frames);
  const outOfPhase = new Float32Array(2 * frames);
  for (let i = 0; i < frames; i++) {
    const sample = 0.25 * Math.sin(2 * Math.PI * peakBin * i / 4096);
    inPhase[i * 2] = inPhase[i * 2 + 1] = outOfPhase[i * 2] = sample;
    outOfPhase[i * 2 + 1] = -sample;
  }
  const result = auditionPower(outOfPhase);
  assert.deepEqual(result, auditionPower(inPhase));
  assert.equal(result.indexOf(Math.max(...result)), peakBin);
  assert.ok(Math.abs(result.reduce((sum, value) => sum + value, 0) - 0.25 ** 2 / 2) < 1e-6);
  assert.ok(auditionPower(new Float32Array(frames * 2)).every((value) => value === 0));
});

test('spectrum ignores trailing PCM beyond the exact crop', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => {
    const padded = new Float32Array(3840 * 2);
    padded.fill(1, 1921 * 2);
    return { ok: true, arrayBuffer: async () => padded.buffer };
  });
  const oldLocation = globalThis.location;
  globalThis.location = { origin: 'http://localhost' };
  try {
    const job = { id: 'audition', result: { excerpt: { frames: 1921, sample_rate_hz: 48000 } } };
    assert.ok((await loadAuditionSpectrum(job, 'saved')).every((value) => value === 0));
  } finally {
    globalThis.location = oldLocation;
  }
});

test('an omitted whole-master correction is part of the stated audition basis', () => {
  const result = (calibration) => ({ comparison_basis: 'retained_gain', calibration });
  const plain = basisMessage(result({ gain: 'retained', retained_detectors: [] }), false);
  assert.doesNotMatch(plain, /after limiting/);
  for (const gain of ['retained', 'retained_target_delta', 'shared_retained_estimate']) {
    const message = basisMessage(result({ gain, retained_detectors: [], omitted_post_limiter_gain_db: -1.405 }), false);
    assert.match(message, /turned down 1\.4 dB after limiting.*Both sides leave that out/);
    assert.match(message, /edited recording may need a different correction/);
  }
  assert.match(basisMessage(result({ gain: 'retained_target_delta', omitted_post_limiter_gain_db: -2 }), true),
    /peak cap\..*turned down 2\.0 dB/);
});
