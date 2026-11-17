// Peak words of the Check report and the Enhance page (no DOM needed).
import assert from 'node:assert/strict';
import test from 'node:test';
import { peakVerdict } from '../../audiolab/web/js/views/job/readings.js';

test('a lossy file is judged by its limit; an over-range peak is named for the measurement', () => {
  const word = (tp, limit) => peakVerdict(tp, limit).word;
  assert.equal(word(-0.3, -0.05), 'Within limit', 'an Enhance MP3 at -0.3 dBTP is within the check limit');
  assert.equal(word(-0.02, -0.05), 'No headroom');
  assert.equal(word(-0.3), 'Little headroom', 'a lossless file keeps the 1 dB margin');
  assert.equal(word(-0.05), 'No headroom', 'under full scale is not clipping');
  assert.equal(word(0.3), 'Above full scale');
  assert.equal(word(0.3, -0.05), 'Above full scale');
  assert.match(peakVerdict(-0.02, -0.05).note, /almost at full scale/, 'over the limit, still under full scale');
  assert.match(peakVerdict(0.3).note, /exceeds 0 dBTP/);
});
