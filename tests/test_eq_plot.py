"""Compare the browser's plotted magnitude with the real mastering coefficients."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import sosfreqz

from audiolab.dsp.filters import highpass_fq, highshelf_fq, lowshelf_fq, peak_fq
from audiolab.recipes.base import eq_bands, section_eq

ROOT = Path(__file__).resolve().parent.parent
RATE = 48000


def _node(source, payload):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node 22+ is needed for production JavaScript ES modules')
    version = subprocess.run([node, '--version'], capture_output=True, text=True, check=True, timeout=10)
    if int(version.stdout.split('.')[0].lstrip('v')) < 22:
        pytest.skip('Node 22+ is needed for production JavaScript ES modules')
    result = subprocess.run(
        [node, '--input-type=module', '-e', source],
        input=json.dumps(payload),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _band(hz, db, q):
    return {'hz': hz, 'db': db, 'q': q}


def _coefficients(bands):
    return [peak_fq(b['hz'], b['db'], b['q'], sr=RATE) for b in bands]


def _zero_phase_db(filters, frequencies):
    _, complex_response = sosfreqz(np.vstack(filters), worN=frequencies, fs=RATE)
    return 40 * np.log10(np.abs(complex_response))


def test_plotted_response_matches_server_cascade_including_narrow_skirts_and_fixed_filters():
    scenarios = [
        {'bands': [_band(20, 6, 8), _band(20000, -6, 8), _band(1255.1, 0.01, 0.3)], 'context': {}},
        {'bands': [_band(19770.1, 6, 8)] * 7 + [_band(19825, 6, 8)], 'context': {}},
        {
            'bands': [_band(250.4, -5.5, 0.3), _band(330.5, 3.3, 2.5), _band(330.5, -1.1, 8)],
            'context': {
                'params': {'highpass_hz': 45, 'mud_cut_db': -1.1, 'bass_db': 2, 'presence_db': 0.7, 'air_db': -0.8},
                'bands': [_band(22.1, 6, 8), _band(12000.1, 2, 8)],
            },
        },
    ]
    actual = _node(
        """
        import { readFileSync } from 'node:fs';
        import { responseDb, responseFilters, responseFrequencies } from './audiolab/web/js/eq-response.js';
        const data = JSON.parse(readFileSync(0, 'utf8')).map(({ bands, context }) => {
          const { user, fixed } = responseFilters(bands, context);
          return responseFrequencies([...bands, ...(context.bands || [])])
            .map((hz) => [hz, responseDb(user, hz), responseDb([...user, ...fixed], hz)]);
        });
        process.stdout.write(JSON.stringify(data));
        """,
        scenarios,
    )
    for data, scenario in zip(actual, scenarios):
        bands, context = scenario['bands'], scenario['context']
        frequencies, own, combined = np.array(data).T
        assert {b['hz'] for b in bands} <= set(frequencies)
        user = _coefficients(bands)
        fixed = []
        if context:
            fixed = _coefficients(context['bands']) + [
                highpass_fq(45, sr=RATE),
                peak_fq(320, -1.1, sr=RATE),
                lowshelf_fq(85, 2, sr=RATE),
                peak_fq(3000, 0.7, 1.4, sr=RATE),
                highshelf_fq(11000, -0.8, sr=RATE),
            ]
        dense = np.geomspace(20, 20000, 24001)
        for plotted, coefficients in [(own, user), (combined, user + fixed)]:
            np.testing.assert_allclose(plotted, _zero_phase_db(coefficients, frequencies), rtol=0, atol=1e-7)
            interpolated = np.interp(np.log(dense), np.log(frequencies), plotted)
            np.testing.assert_allclose(interpolated, _zero_phase_db(coefficients, dense), rtol=0, atol=0.05)


def test_section_and_eq_normalization_agrees_between_javascript_and_python():
    raw = [_band(1000.05, -1.125, 2.125), _band(2678.5, 0.005, 8), _band(80, -0.004, 1)]
    sections = [
        {'start_s': 1.001, 'end_s': 11.001, 'fade_s': 5, 'bands': raw},
        {'start_s': 11.0007, 'end_s': 21.001, 'fade_s': 5.004, 'bands': raw},
    ]
    actual = _node(
        r"""
        import { readFileSync } from 'node:fs';
        import { parseEq } from './audiolab/web/js/eq.js';
        import { checkSections } from './audiolab/web/js/section-eq.js';
        const { raw, sections } = JSON.parse(readFileSync(0, 'utf8'));
        const toText = (bands) => bands.map((b) => `${b.hz} Hz ${b.db} dB Q ${b.q}`).join('\n');
        const checked = checkSections(sections.map((s) => ({
          from: s.start_s, to: s.end_s, fade: s.fade_s, text: toText(s.bands),
        })));
        process.stdout.write(JSON.stringify({ bands: parseEq(toText(raw)).bands, sections: checked.sections }));
        """,
        {'raw': raw, 'sections': sections},
    )
    assert actual == {'bands': eq_bands(raw), 'sections': section_eq(sections)}
