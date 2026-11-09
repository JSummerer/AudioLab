// The Custom EQ text parser and formatter (no DOM needed).
import assert from 'node:assert/strict';
import test from 'node:test';
import { parseEq, formatEq, eqSummary, sameEq, EQ_PRESETS, EQ_MAX_BANDS } from '../../audiolab/web/js/eq.js';

const band = (hz, db, q) => ({ hz, db, q });
const one = (text) => {
  const { bands, errors } = parseEq(text);
  assert.deepEqual(errors, [], `no errors for ${JSON.stringify(text)}`);
  assert.equal(bands.length, 1);
  return bands[0];
};

test('plain bands accept units, signs, minus signs and the usual ways of writing Q', () => {
  for (const text of ['160 Hz -3.3 dB Q 2.9', '160Hz −3.3dB Q2.9', '160 hz, -3.3 db, q=2.9', '160 -3.3 Q2.9',
    'Q 2.9 -3.3 dB 160 Hz', '160 Hz; – 3.3 dB; Q: 2.9', 'PK 160 Hz -3.3 dB Q 2.9', 'Fc 160 Gain -3.3 Q 2.9']) {
    assert.deepEqual(one(text), band(160, -3.3, 2.9), text);
  }
  assert.deepEqual(one('2.5k +1 dB Q=1.4'), band(2500, 1, 1.4));
  assert.deepEqual(one('2.5 kHz 1.5 dB Q .7'), band(2500, 1.5, 0.7));
  assert.deepEqual(one('1.1 kHz +2 Q 1'), band(1100, 2, 1));
  assert.deepEqual(one('2,5 kHz -1,5 dB Q 1,4'), band(2500, -1.5, 1.4), 'a decimal comma between digits');
});

test('Equalizer APO lines: peaking filters are read, OFF filters and Preamp are skipped, shelves are reported', () => {
  const { bands, errors } = parseEq([
    'Preamp: -4.1 dB',
    'Filter: ON PK Fc 160 Hz Gain -3.3 dB Q 2.9',
    'Filter 2: ON PK Fc 45 Hz Gain 1.0 dB Q 3.10',
    'Filter 3: OFF PK Fc 1000 Hz Gain 4 dB Q 1',
    'Filter 4: ON LSC Fc 105 Hz Gain 3 dB',
  ].join('\n'));
  assert.deepEqual(bands, [band(160, -3.3, 2.9), band(45, 1, 3.1)]);
  assert.deepEqual(errors, [{ line: 5, message: 'Only peaking (PK) bands are supported.' }]);
});

test('blank lines and # comments are skipped; problems name their line and nothing is clamped', () => {
  const { bands, errors } = parseEq([
    '# my curve',
    '',
    '31 Hz -1 dB Q 3.1',
    '10 Hz -1 dB Q 1',
    '25 kHz -1 dB Q 1',
    '100 Hz +7 dB Q 1',
    '100 Hz -1 dB Q 9',
    '100 Hz -1 dB Q 0.2',
    '100 Hz -1 dB',
    '100 Hz Q 1',
    '-1 dB Q 1',
    '160 Hz -3 dB BW 0.5',
    '160 Hz -3 dB 2.9',
    '160 Hz 200 Hz -3 dB Q 1',
  ].join('\n'));
  assert.deepEqual(bands, [band(31, -1, 3.1)]);
  assert.deepEqual(errors, [
    { line: 4, message: 'Frequency must be 20 Hz to 20 kHz.' },
    { line: 5, message: 'Frequency must be 20 Hz to 20 kHz.' },
    { line: 6, message: 'Gain must be −6 to +6 dB.' },
    { line: 7, message: 'Q must be 0.3 to 8.' },
    { line: 8, message: 'Q must be 0.3 to 8.' },
    { line: 9, message: 'Add a width as Q, like Q 1.4.' },
    { line: 10, message: 'Add a gain in dB, like −3 dB.' },
    { line: 11, message: 'Add a frequency, like 160 Hz.' },
    { line: 12, message: 'Can’t read “BW”.' },
    { line: 13, message: 'Write the width as Q, like Q 1.4.' },
    { line: 14, message: 'Use one band per line.' },
  ]);
});

test('the limits themselves are allowed, 0 dB bands are left out and a ninth band is reported', () => {
  assert.deepEqual(parseEq('20 Hz -6 dB Q 0.3\n20 kHz +6 dB Q 8').bands, [band(20, -6, 0.3), band(20000, 6, 8)]);
  assert.deepEqual(parseEq('100 Hz 0 dB Q 1'), { bands: [], errors: [] });
  const lines = Array.from({ length: EQ_MAX_BANDS + 1 }, (_, i) => `${100 + i * 100} Hz -1 dB Q 1`);
  const { bands, errors } = parseEq(lines.join('\n'));
  assert.equal(bands.length, EQ_MAX_BANDS);
  assert.deepEqual(errors, [{ line: EQ_MAX_BANDS + 1, message: 'Up to 8 bands.' }]);
  assert.deepEqual(parseEq(''), { bands: [], errors: [] });
  assert.deepEqual(parseEq(null), { bands: [], errors: [] });
});

test('the formatter uses real minus signs, reads back exactly, and the presets are valid', () => {
  const upper = EQ_PRESETS.find((p) => p.label === 'Clean upper bass');
  assert.equal(formatEq(upper.bands), '160 Hz −3.3 dB Q 2.9\n280 Hz +1.5 dB Q 2.0');
  assert.equal(eqSummary(upper.bands), '160 Hz −3.3 dB Q2.9 · 280 Hz +1.5 dB Q2.0');
  const sub = EQ_PRESETS.find((p) => p.label === 'Sub focus');
  assert.deepEqual(sub.bands, [band(31, -1, 3.1), band(45, 1, 3.1)]);
  for (const p of EQ_PRESETS) {
    assert.ok(p.desc);
    assert.deepEqual(parseEq(formatEq(p.bands)), { bands: p.bands, errors: [] });
  }
  const odd = [band(2456, -2.25, 1.41), band(31.5, 0.5, 4)];
  assert.equal(formatEq(odd), '2.456 kHz −2.25 dB Q 1.41\n31.5 Hz +0.5 dB Q 4.0');
  assert.deepEqual(parseEq(formatEq(odd)).bands, odd);
  assert.equal(formatEq([]), '');
  assert.equal(eqSummary(undefined), '');
});

test('sameEq ignores order and number types but not values', () => {
  const a = [band(160, -3.3, 2.9), band(280, 1.5, 2)];
  assert.ok(sameEq(a, [band('280', '1.5', '2'), band(160, -3.3, 2.9)]));
  assert.ok(sameEq([], undefined));
  assert.ok(!sameEq(a, a.slice(0, 1)));
  assert.ok(!sameEq(a, [band(160, -3.2, 2.9), band(280, 1.5, 2)]));
});
