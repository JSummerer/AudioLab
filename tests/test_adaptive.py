"""Numerical guardrails for adaptive low-mids; these are not listening judgments."""

import json

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import butter

from audiolab.dsp.adaptive import BAND_HZ, MAX_BAND_CHANGE_DB, control_lowmids
from audiolab.dsp.filters import filtfilt_stream
from audiolab.errors import Cancelled
from audiolab.recipes import resolve
from audiolab.recipes.base import ChainParams
from audiolab.recipes.chain import render

SR = 12000  # enough for this band's guards, keeping numerical fixtures small


def fixture(duration=20, event=(9, 10), sr=SR, *, dip=None):
    t = np.arange(int(duration * sr)) / sr
    x = 0.1 * np.sin(2 * np.pi * 1000 * t) + 0.01 * np.sin(2 * np.pi * 300 * t)
    if event:
        rise = (t >= event[0]) & (t < event[1])
        if dip:
            rise &= (t < dip[0]) | (t >= dip[1])
        x += 0.2 * np.sin(2 * np.pi * 300 * t) * rise
    return np.column_stack([x, -x]).astype(np.float32)


@pytest.mark.parametrize('event', [None, (9, 9.1), (9, 13), (9, 19), (0, 20)])
def test_steady_tone_attacks_and_sustained_contrasts_are_unchanged(event):
    x = fixture(event=event)
    y, stats = control_lowmids(x.copy(), SR)
    assert not stats['engaged']
    np.testing.assert_array_equal(x, y)


@pytest.mark.parametrize('dip', [(11, 11.1), (11, 11.2), (10.97, 11.07), (10.97, 11.17)])
@pytest.mark.parametrize('quiet', [False, True], ids=['tonal-dip', 'quiet-dip'])
def test_short_dips_do_not_turn_sustained_contrast_into_isolated_events(dip, quiet):
    x = fixture(event=(9, 13), dip=dip)
    if quiet:
        x[round(dip[0] * SR) : round(dip[1] * SR)] = 0
    y, stats = control_lowmids(x.copy(), SR)
    assert not stats['engaged']
    assert stats['sustained_pct'] == stats['candidate_pct'] > 0
    np.testing.assert_array_equal(x, y)


def test_relative_control_is_bounded_local_and_stereo_linked():
    x = fixture()
    y, stats = control_lowmids(x.copy(), SR)
    assert stats['engaged'] and 0.1 < stats['max_gr_db'] <= 1.5
    assert 0 < stats['active_pct'] < 10
    assert stats['band_change_db'] >= -MAX_BAND_CHANGE_DB - 0.001
    assert stats['windows_total'] == 200
    json.dumps(stats, allow_nan=False)
    np.testing.assert_array_equal(y[: 8 * SR], x[: 8 * SR])
    np.testing.assert_array_equal(y[11 * SR :], x[11 * SR :])
    np.testing.assert_array_equal(y[:, 0], -y[:, 1])
    # Check the audio, not just the detector's modeled energy budget.
    band = butter(2, BAND_HZ, btype='bandpass', fs=SR, output='sos')
    before = filtfilt_stream(band, x)
    after = filtfilt_stream(band, y)
    change = 10 * np.log10(np.mean(after**2) / np.mean(before**2))
    assert -MAX_BAND_CHANGE_DB - 0.02 <= change < -0.1
    quiet, quiet_stats = control_lowmids(x.copy() * 0.1, SR)
    assert quiet_stats['events'] == stats['events']
    np.testing.assert_allclose(quiet, y * 0.1, atol=2e-8)


def test_whole_recording_and_bypass():
    x = fixture(duration=140, event=(130, 131), sr=SR)
    _, stats = control_lowmids(x.copy(), SR)
    assert stats['engaged'] and stats['events'][0]['start_s'] >= 129
    unchanged, off = control_lowmids(x, SR, max_gr_db=0)
    assert unchanged is x and off['skip_reason'] == 'off'
    silent = np.zeros((SR * 5, 2), dtype=np.float32)
    assert not control_lowmids(silent, SR)[1]['engaged']


def test_broadband_level_change_is_not_a_tonal_event():
    x = fixture(event=None)
    x[9 * SR : 10 * SR] *= 4
    y, stats = control_lowmids(x.copy(), SR)
    assert not stats['engaged']
    np.testing.assert_array_equal(y, x)


def test_cancellation_during_analysis_and_processing():
    for cancel_at in (1, 3, 10):
        calls = 0

        def check(cancel_at=cancel_at):
            nonlocal calls
            calls += 1
            if calls == cancel_at:
                raise Cancelled()

        with pytest.raises(Cancelled):
            control_lowmids(fixture(), SR, check_cancel=check)


def test_cancellation_after_first_output_block():
    x = fixture(duration=100)
    original_event = x[9 * SR : 10 * SR].copy()

    def check():
        if not np.array_equal(original_event, x[9 * SR : 10 * SR]):
            raise Cancelled()

    with pytest.raises(Cancelled):
        control_lowmids(x, SR, check_cancel=check)


@pytest.mark.parametrize(
    'duration,event', [(3.99, (1, 2)), (4, (3.5, 4)), (20, (0, 1)), (20, (19, 20)), (40, (12.37, 22.37))]
)
def test_uncertain_edges_and_off_grid_sustained_change_pass(duration, event):
    x = fixture(duration=duration, event=event)
    y, stats = control_lowmids(x.copy(), SR)
    assert not stats['engaged']
    np.testing.assert_array_equal(x, y)


def test_quiet_context_is_preserved_not_normalized_for_detection():
    x = fixture() * 0.001
    y, stats = control_lowmids(x.copy(), SR)
    assert stats['skip_reason'] == 'too little audible context'
    np.testing.assert_array_equal(x, y)


def test_recipe_bypasses_and_harmonics_are_independent():
    profile = {'integrated_lufs': -14, 'has_mud_excess': True}
    for recipe in ('club', 'music', 'live', 'voice'):
        p, _ = resolve(recipe, profile)
        assert p.mud_cut_db == 0 and p.adaptive_lowmid_db == 1.5
        assert p.sub_harmonics == 0
        p, _ = resolve(recipe, profile, advanced={'bass_db': 0, 'harmonics': 'on', 'adaptive_lowmid': 'strong'})
        assert p.sub_harmonics == 0.25 and p.bass_db == 0 and p.adaptive_lowmid_db == 3
        p, _ = resolve(recipe, profile, advanced={'bass_db': 3, 'harmonics': 'off', 'adaptive_lowmid': 'off'})
        assert p.sub_harmonics == 0 and p.bass_db == 3 and p.adaptive_lowmid_db == 0
        p, _ = resolve(recipe, profile, quality='light', advanced={'adaptive_lowmid': 'strong', 'harmonics': 'on'})
        assert p.adaptive_lowmid_db == 0 and p.sub_harmonics == 0


def test_ai_stem_eq_skips_duplicate_control_and_fallback_uses_it(tmp_path, monkeypatch):
    import audiolab.recipes.chain as chain

    x = fixture(sr=48000)
    source = tmp_path / 'source.wav'
    sf.write(source, x, 48000, subtype='FLOAT')

    def ai_stage(*args, **kwargs):
        return {'x': x.copy(), 'report': {'stage': 'stem_eq', 'fallback': False}, 'warning': None}

    monkeypatch.setattr(chain, '_ai_stage', ai_stage)
    p = ChainParams(highpass_hz=0, ai_stage='stem_eq', adaptive_lowmid_db=1.5, target_lufs=-20)
    rep = render(source, tmp_path / 'ai.flac', p, {}, tmp_path / 'work')
    assert not rep['adaptive_lowmid']['engaged']
    assert 'AI stem EQ' in rep['adaptive_lowmid']['skip_reason']

    def fallback(*args, **kwargs):
        raise RuntimeError('simulated failure')

    monkeypatch.setattr(chain, '_ai_stage', fallback)
    rep = render(source, tmp_path / 'fallback.flac', p, {}, tmp_path / 'work2')
    assert rep['ai']['fallback'] and rep['adaptive_lowmid']['engaged']
