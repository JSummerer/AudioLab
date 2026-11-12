// Section EQ: the client checks match the server's main rules (docs/API.md `section_eq`).
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  checkSections, dragSectionEdge, parseFade, sectionRows, sameSections, sectionFromAudition, sectionFullStrength,
} from '../../audiolab/web/js/section-eq.js';
import { clockText } from '../../audiolab/web/js/dom.js';

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

test('exactly ten seconds with five-second fades is valid, including touching sections and millisecond round trips', () => {
  const { sections, errors } = checkSections([row('0:01.001', '0:11.001'), row('11.0007', '21.001', '14 kHz -2 dB Q 0.7', '5.004')], 21.001);
  assert.deepEqual(errors, []);
  assert.deepEqual(sections, [
    { start_s: 1.001, end_s: 11.001, fade_s: 5, bands: [band] },
    { start_s: 11.001, end_s: 21.001, fade_s: 5, bands: [band] },
  ]);
  assert.deepEqual(checkSections(sectionRows(sections), 21.001).sections, sections);
  assert.deepEqual(sectionFullStrength(sections[0]), { start: 6.001, end: 6.001 });
  assert.equal(checkSections([row('0', '10', '14 kHz -2 dB Q 0.7', '5.005')]).errors.length, 1);
  assert.equal(checkSections([row('0', '10.001')], 10).errors.length, 1);
});

test('create a section around a short audition with explicit scope and boundary fades', () => {
  const inner = sectionFromAudition({ start: 15, end: 20 }, 40);
  assert.deepEqual(inner.section, { start_s: 12.5, end_s: 22.5, fade_s: 0.5, bands: [] });
  assert.match(inner.message, /Section 0:12\.5–0:22\.5.*Full strength 0:13–0:22/);
  assert.doesNotMatch(inner.message, /Part of the audition/);
  const start = sectionFromAudition({ start: 0, end: 5 }, 40);
  assert.deepEqual(start.section, { start_s: 0, end_s: 10, fade_s: 0.5, bands: [] });
  assert.match(start.message, /Part of the audition lies in a fade/);
  const end = sectionFromAudition({ start: 35, end: 40 }, 40);
  assert.deepEqual(end.section, { start_s: 30, end_s: 40, fade_s: 0.5, bands: [] });
  assert.match(end.message, /Part of the audition lies in a fade/);
  const neighbors = [{ start_s: 0, end_s: 10 }, { start_s: 20, end_s: 40 }];
  const touching = sectionFromAudition({ start: 10, end: 15 }, 40, neighbors);
  assert.deepEqual(touching.section, { start_s: 10, end_s: 20, fade_s: 0.5, bands: [] });
  assert.match(touching.message, /Part of the audition lies in a fade/);
  assert.equal(sectionFromAudition({ start: 8, end: 15 }, 40, neighbors).section, null);
  assert.equal(sectionFromAudition({ start: 2, end: 7 }, 9.999).section, null);
});

test('dragging an edge stops where the validator still accepts the section, using its fade units', () => {
  const bounds = { start_s: 0, end_s: 20, fade_s: 5.005 };
  const end = dragSectionEdge(bounds, [], 30, 'end', 3);
  assert.equal(clockText(end), '0:10.02');
  const row = { from: '0', to: clockText(end), fade: '5.005', text: '1000 Hz +1 dB Q 1' };
  assert.deepEqual(checkSections([row], 30).errors, []);
  const neighbours = [{ start_s: 25, end_s: 40, fade_s: 0.5 }];
  assert.equal(dragSectionEdge(bounds, neighbours, 60, 'end', 31), 25);
  assert.equal(dragSectionEdge({ start_s: 12, end_s: 22, fade_s: 0.5 }, [{ start_s: 0, end_s: 10, fade_s: 0.5 }], 60, 'start', 0), 10);
  assert.equal(dragSectionEdge({ start_s: 0, end_s: 10, fade_s: 5 }, [], 9, 'end', 12), null);
  assert.equal(dragSectionEdge({ start_s: 0, end_s: 20, fade_s: -2 }, [], 30, 'end', 15), null);
  assert.equal(Number.isNaN(parseFade('1e1')), true);
  assert.equal(Number.isNaN(sectionFullStrength({ start_s: 10, end_s: 20, fade_s: -2 }).start), true);
  assert.equal(Number.isNaN(sectionFullStrength({ start_s: 5, end_s: 8, fade_s: 0.5 }).start), true);
});
