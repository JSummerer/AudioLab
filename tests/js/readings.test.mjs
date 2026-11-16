// Peak words of the Check report and the Enhance page (no DOM needed).
import assert from 'node:assert/strict';
import test from 'node:test';
import { peakVerdict } from '../../audiolab/web/js/views/job/readings.js';

test('a lossy file is judged by its limit; only a peak over full scale is called clipping', () => {
  const word = (tp, limit) => peakVerdict(tp, limit).word;
  assert.equal(word(-0.3, -0.05), 'Safe', 'an Enhance MP3 at -0.3 dBTP is fine');
  assert.equal(word(-0.02, -0.05), 'No headroom');
  assert.equal(word(-0.3), 'Little headroom', 'a lossless file keeps the 1 dB margin');
  assert.equal(word(-0.05), 'No headroom', 'under full scale is not clipping');
  assert.equal(word(0.3), 'Clipping');
  assert.equal(word(0.3, -0.05), 'Clipping');
  assert.match(peakVerdict(-0.02, -0.05).note, /almost at full scale/, 'over the limit, still under full scale');
  assert.match(peakVerdict(0.3).note, /above full scale/);
});
