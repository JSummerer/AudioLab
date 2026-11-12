// The tone timeline chart's helpers (no DOM needed).
import assert from 'node:assert/strict';
import test from 'node:test';
import { toneRange, tonePath, timeTicks, axisTime, toneReadout, toneObservation } from '../../audiolab/web/js/tone-chart.js';

const bands = [{ key: 'deep_bass', label: 'Deep bass' }, { key: 'mids', label: 'Mids' }, { key: 'top_end', label: 'Top end' }];
const timeline = (values, step_s = 60) => ({ step_s, bands, values });

test('the y range fits the shown bands; a band path breaks at gaps and draws a lone point as a dot', () => {
  const tl = timeline([[3, 0, 0], [-3, 7.2, 0], null, [0, 0, 0], [1.5, null, 0]]);
  assert.equal(toneRange(tl, ['deep_bass']), 3);
  assert.equal(toneRange(tl, ['deep_bass', 'mids']), 9, '7.2 dB rounds up to ±9');
  // 0…240 s → x 0…1000; y = 50 − v / 3 × 46.
  assert.equal(tonePath(tl, 'deep_bass', 3), 'M0 4L250 96M750 50L1000 27');
  assert.equal(tonePath(tl, 'top_end', 3), 'M0 50L250 50M750 50L1000 50');
  assert.match(tonePath(timeline([null, [0, 1, 2], null]), 'top_end', 3), /^M500 19\.3h0\.01$/);
});

test('axis times switch to h:mm from an hour on; the readout names the time and each band', () => {
  assert.deepEqual(timeTicks(1500), [0, 300, 600, 900, 1200, 1500]);
  assert.equal(axisTime(720, 1500), '12:00');
  assert.equal(axisTime(5400, 9632), '1:30');
  const tl = timeline([[0.4, -0.2, 3.1], null, [null, 1, null]], 360);
  assert.equal(toneReadout(tl, 0, ['deep_bass', 'top_end']), '0:00 · Deep bass +0.4 dB · Top end +3.1 dB');
  assert.equal(toneReadout(tl, 1, ['deep_bass']), '6:00 · no usable mid-signal reading');
});

test('relative tone shares cannot recommend cuts or boosts in unchanged bands', () => {
  const bandList = ['Deep bass', 'Bass', 'Low mids', 'Mids', 'Upper mids', 'Highs', 'Top end'].map((label) => ({ label }));
  const relative = (reference, current) => {
    const shares = (energy) => energy.map((e) => 10 * Math.log10(e / energy.reduce((sum, v) => sum + v)));
    return shares(current).map((db, i) => db - shares(reference)[i]);
  };
  const reference = [0.46, 0.09, 0.09, 0.09, 0.09, 0.09, 0.09];
  const bassRises = relative(reference, reference.map((v, i) => i === 0 ? v * 10 ** 0.3 : v));
  assert.deepEqual(bassRises.map((db) => Number(db.toFixed(1))), [1.4, -1.6, -1.6, -1.6, -1.6, -1.6, -1.6]);
  const lowerFalls = relative([1, 1, 1, 1, 1, 1, 1], [0, 1, 2, 3, 4, 5, 6].map((i) => i < 4 ? 10 ** -0.2 : 1));
  assert.deepEqual(lowerFalls.map(Math.round), [-1, -1, -1, -1, 1, 1, 1]);
  for (const values of [bassRises, lowerFalls]) {
    const tl = { bands: bandList, step_s: 60, values: [Array(7).fill(0), values, null] };
    const report = toneObservation(tl, 60, 120, 180);
    assert.equal(report.status, 'no_candidate');
    assert.deepEqual(report.differences.map((point) => point.db), values);
    assert.match(report.basis, /Sampled mid-signal band shares/);
    assert.match(report.message, /No EQ proposed/);
    assert.equal(toneObservation(tl, 80, 85, 180).status, 'insufficient', 'a brief event in a coarse bin cannot be inspected here');
    assert.equal(toneObservation(tl, 120, 180, 180).status, 'insufficient', 'a quiet or canceled bin lacks data');
  }
});

test('tone observations state observed coverage and exclude a possibly partial final bin', () => {
  const tl = timeline([[2, 0, 0], null, [1, 0, 0]]);
  const partial = toneObservation(tl, 0, 180, 150);
  assert.match(partial.message, /1 of 2 complete 60 s bins/);
  assert.equal(partial.differences[0].db, 2);
  assert.equal(toneObservation(tl, 120, 180).status, 'insufficient');
  assert.equal(toneObservation(tl, 120, 180, 180).status, 'no_candidate');
});
