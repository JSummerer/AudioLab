"""Tone timeline (chart data, no verdict) and section EQ (user-chosen parts of a recording)."""

import json

import numpy as np
import pytest

from audiolab.analysis import tone
from audiolab.recipes.base import resolve, section_eq
from audiolab.recipes.chain import apply_section_eq

SR = 48000


def _blocks(minutes, rng, boost=()):
    base = np.array([4.0, 6.0, 5.0, 8.0, 3.0, 1.0, 0.3])
    rows = base * 10 ** rng.normal(0.0, 0.03, (int(minutes * 6), len(base)))
    for start, end, band, db in boost:
        rows[start // 10 : end // 10, band] *= 10 ** (db / 10)
    return rows


def test_timeline_shows_a_brighter_first_part_and_skips_pauses():
    rng = np.random.default_rng(1)
    blocks = _blocks(30, rng, [(0, 600, 6, 4.0)])
    blocks[120:126] *= 1e-4  # minute 20 is a near-silent break
    tl = tone.tone_timeline(blocks)
    assert tl['step_s'] == 60 and len(tl['values']) == 30 and [b['key'] for b in tl['bands']][-1] == 'top_end'
    first, later = tl['values'][3], tl['values'][25]
    assert first[6] == pytest.approx(3.8, abs=0.4) and abs(later[6]) < 0.3
    assert tl['values'][20] is None
    json.dumps(tl, allow_nan=False)


def test_timeline_stays_small_for_very_long_files_and_needs_two_stretches():
    rng = np.random.default_rng(2)
    tl = tone.tone_timeline(_blocks(20 * 60, rng))  # 20 hours
    assert len(tl['values']) <= tone.MAX_POINTS and tl['step_s'] > 60
    assert tone.tone_timeline(_blocks(1, rng)) is None
    assert tone.tone_timeline(np.zeros((30, 7))) is None


def test_block_bands_reads_where_the_energy_is():
    t = np.arange(SR * 10) / SR
    low = np.column_stack([np.sin(2 * np.pi * 50 * t)] * 2).astype(np.float32)
    bands = tone.block_bands(low, SR)
    assert bands.argmax() == 0 and bands[0] > 100 * bands[1:].sum()
    assert not tone.block_bands(low[:100], SR).any()


def test_section_eq_validation():
    band = [{'hz': 14000, 'db': -2, 'q': 0.7}]
    ok = section_eq(
        [{'start_s': 600, 'end_s': 900, 'bands': band}, {'start_s': 0, 'end_s': 300, 'fade_s': 99, 'bands': band}]
    )
    assert [s['start_s'] for s in ok] == [0.0, 600.0] and ok[0]['fade_s'] == 60.0 and ok[1]['fade_s'] == 5.0
    assert section_eq([{'start_s': 0, 'end_s': 300, 'bands': [{'hz': 100, 'db': 0, 'q': 1}]}]) == []
    assert section_eq([]) == []
    bad = [
        [{'start_s': 0, 'end_s': 300, 'bands': band}, {'start_s': 299, 'end_s': 400, 'bands': band}],  # overlap
        [{'start_s': 0, 'end_s': 8, 'bands': band}],  # under 10 s
        [{'start_s': 0, 'end_s': 30, 'fade_s': 20, 'bands': band}],  # not longer than two fades
        [{'start_s': -1, 'end_s': 30, 'bands': band}],
        [{'start_s': 0, 'end_s': 30, 'bands': band, 'other': 1}],
        [{'start_s': 0, 'end_s': 10**400, 'bands': band}],
        'all of it',
    ]
    for value in bad:
        assert section_eq(value) is None, value


def test_section_eq_is_resolved_and_ignored_by_volume_only():
    sections = [{'start_s': 0.0, 'end_s': 2400.0, 'fade_s': 5.0, 'bands': [{'hz': 14000.0, 'db': -2.0, 'q': 0.7}]}]
    p, decisions = resolve('club', {'integrated_lufs': -14.0, 'lra': 5.0}, advanced={'section_eq': sections})
    assert p.section_eq == sections
    (d,) = [d for d in decisions if d['key'] == 'section_eq']
    assert d['value'] == '0:00–40:00: 14000 Hz −2.0 dB Q0.7'
    p, _ = resolve('club', {'integrated_lufs': -14.0}, quality='light', advanced={'section_eq': sections})
    assert p.section_eq == []


def test_section_eq_changes_only_its_part_with_smooth_edges():
    t = np.arange(SR * 10) / SR
    x = np.column_stack([0.1 * np.sin(2 * np.pi * 1000 * t)] * 2).astype(np.float32)
    dry = x.copy()
    sec = [{'start_s': 3.0, 'end_s': 7.0, 'fade_s': 0.5, 'bands': [{'hz': 1000.0, 'db': 6.0, 'q': 1.0}]}]
    assert apply_section_eq(x, sec, SR) == 1
    assert np.array_equal(x[: 3 * SR], dry[: 3 * SR]) and np.array_equal(x[7 * SR :], dry[7 * SR :])
    inside = x[4 * SR : 6 * SR, 0]
    gain = 20 * np.log10(np.sqrt(np.mean(inside**2)) / np.sqrt(np.mean(dry[4 * SR : 6 * SR, 0] ** 2)))
    assert gain == pytest.approx(6.0, abs=0.1)
    # 10 ms gain steps across the fade rise steadily from 0 to +6 dB: a fade, not a click.
    win = SR // 100
    seg = slice(int(2.9 * SR), int(3.6 * SR))
    rms = lambda a: np.sqrt(np.mean(a[seg, 0].reshape(-1, win) ** 2, axis=1))  # noqa: E731
    steps = 20 * np.log10(rms(x) / rms(dry))
    assert abs(steps[0]) < 0.01 and steps[-1] == pytest.approx(6.0, abs=0.15)
    assert np.all(np.diff(steps) > -0.01) and np.max(np.diff(steps)) < 0.3


def test_section_eq_on_a_preview_excerpt_touches_only_the_overlap():
    x = np.full((SR * 4, 2), 0.1, dtype=np.float32) * np.sin(2 * np.pi * 1000 * np.arange(SR * 4) / SR)[:, None]
    dry = x.copy()
    shifted = [{'start_s': -10.0, 'end_s': 2.0, 'fade_s': 1.0, 'bands': [{'hz': 1000.0, 'db': -6.0, 'q': 1.0}]}]
    assert apply_section_eq(x, shifted, SR) == 1
    assert np.array_equal(x[2 * SR :], dry[2 * SR :]) and not np.array_equal(x[: SR // 2], dry[: SR // 2])
    assert apply_section_eq(x, [{**shifted[0], 'start_s': 10.0, 'end_s': 30.0}], SR) == 0
