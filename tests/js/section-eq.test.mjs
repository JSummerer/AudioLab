// Section EQ: the client checks match the server's main rules (docs/API.md `section_eq`).
import assert from 'node:assert/strict';
import test from 'node:test';
import { checkSections, sectionRows, sameSections, suggestBands } from '../../audiolab/web/js/section-eq.js';

const row = (from, to, text = '14 kHz -2 dB Q 0.7', fade = '') => ({ from, to, fade, text });
const band = { hz: 14000, db: -2, q: 0.7 };

test('valid sections come out sorted in seconds; problems are named per section and block the rest', () => {
  assert.deepEqual(checkSections([row('1:20:00', '1:30:00'), row('40:00', '45:00', '160 Hz −3 dB Q 2', '2,5')], 9632), {
    errors: [],
    sections: [{ start_s: 2400, end_s: 2700, fade_s: 2.5, bands: [{ hz: 160, db: -3, q: 2 }] },
      { start_s: 4800, end_s: 5400, fade_s: 5, bands: [band] }],
  });
  const { sections, errors } = checkSections([
    row('3:00', '3:09'), row('10:00', '20:00'), row('19:00', '25:00'), row('30:00', '31:00', ''), row('2:39:00', '2:41:00'),
  ], 9632);
  assert.deepEqual(sections, []);
  assert.deepEqual(errors.map((e) => [e.index, e.message]), [
    [0, 'A section needs at least 10 seconds.'],
    [2, 'Overlaps the section from 10:00.'],
    [3, 'Add EQ bands, or remove this section.'],
    [4, 'The recording ends at 2:40:32.'],
  ]);
});

test('saved sections become rows that check back to the same sections', () => {
  const saved = [{ start_s: 2400, end_s: 2700, fade_s: 5, bands: [band] }, { start_s: 4800, end_s: 5400, fade_s: 10, bands: [band] }];
  const rows = sectionRows(saved);
  assert.deepEqual(rows[0], { from: '40:00', to: '45:00', fade: '', text: '14 kHz −2.0 dB Q 0.7' });
  assert.ok(sameSections(checkSections(rows).sections, saved));
  assert.ok(!sameSections(saved.slice(1), saved));
});

test('a suggestion is half the part’s difference from the typical balance, cuts up to 2 dB, never boosts the highs', () => {
  const hz = [[30, 80], [80, 200], [200, 500], [500, 2000], [2000, 6000], [6000, 12000], [12000, 20000]];
  const tl = { step_s: 60, bands: hz.map((r, i) => ({ key: `b${i}`, hz: r })), values: [
    [0, 0, 0, 0, 0, 0, 0], [3, 0.5, -3, 0, 0, -4, 1.5], null, [3, 0.5, -3, 0, 0, -4, 0.5], [0, 0, 0, 0, 0, -4, 0]] };
  assert.deepEqual(suggestBands(tl, 60, 240), { differs: true, bands: [
    { hz: 49, db: -1.5, q: 0.8 }, { hz: 320, db: 1, q: 0.8 }, { hz: 15000, db: -0.5, q: 0.8 }] });
  assert.deepEqual(suggestBands(tl, 240, 300), { differs: true, bands: [] }, 'only lower highs: nothing to boost');
  assert.deepEqual(suggestBands(tl, 0, 60), { differs: false, bands: [] });
  assert.equal(suggestBands(tl, 125, 175), null, 'a quiet stretch has no reading');
});
