"""Generated comparisons with measurements taken before playback volume matching."""

from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from audiolab.dsp.loudness import programme_loudness
from audiolab.recipes.base import LIGHT_MAX_LIMIT_DB, ChainParams, eq_bands, section_eq
from audiolab.recipes.calibration import AuditionCalibration, for_pair
from audiolab.recipes.chain import render

SR = 48000
CROP = slice(5 * SR, 10 * SR)
BAND = {'hz': 1000, 'db': 3, 'q': 0.8}
TWO_LSB_24 = 2 * 2**-23  # the dither RNG can differ between full and cropped renders


def _source(tmp_path, samples):
    audio = np.column_stack((samples, samples)) if samples.ndim == 1 else samples
    full, context = tmp_path / 'full.wav', tmp_path / 'context.wav'
    sf.write(full, audio, SR, subtype='FLOAT')
    sf.write(context, audio[: 30 * SR], SR, subtype='FLOAT')
    return full, context


def _render(tmp_path, path, params, name, calibration=None):
    output = tmp_path / f'{name}.flac'
    report = render(path, output, params, {}, tmp_path, calibration=calibration)
    audio, _ = sf.read(output, start=CROP.start, stop=CROP.stop, always_2d=True)
    return audio, report


def _errors(actual, reference):
    def rms(audio):
        return np.sqrt(np.mean(audio**2))

    scale = rms(reference) / rms(actual)
    level_error = programme_loudness(actual, SR) - programme_loudness(reference, SR)
    raw_error = 20 * np.log10(rms(actual - reference) / rms(reference))
    aligned_error = 20 * np.log10(rms(actual * scale - reference) / rms(reference))
    return level_error, raw_error, aligned_error


@pytest.mark.parametrize('section_edit', [False, True], ids=['global-eq', 'outside-audition'])
def test_eq_drafts_keep_level_error_visible_after_gain_alignment(tmp_path, section_edit):
    seconds = 36 if section_edit else 32
    t = np.arange(seconds * SR) / SR
    full, context = _source(tmp_path, 0.02 * (np.sin(2 * np.pi * 1000 * t) + 0.2 * np.sin(2 * np.pi * 3000 * t)))
    saved = ChainParams(highpass_hz=0, target_lufs=-20)
    if section_edit:
        draft = replace(saved, section_eq=[{'start_s': 20, 'end_s': 35, 'fade_s': 0.5, 'bands': [BAND]}])
    else:
        draft = replace(saved, eq_bands=[BAND])
    saved_full, saved_report = _render(tmp_path, full, saved, 'saved-full')
    edited_full, edited_report = _render(tmp_path, full, draft, 'edited-full')
    saved_cal, draft_cal, basis = for_pair(saved, draft, saved_report['calibration'], SR)
    saved_audition, _ = _render(tmp_path, context, saved, 'saved-audition', saved_cal)
    edited_audition, draft_report = _render(tmp_path, context, draft, 'edited-audition', draft_cal)
    assert basis['gain'] == 'shared_retained_estimate'
    np.testing.assert_allclose(saved_audition, saved_full, rtol=0, atol=TWO_LSB_24)
    assert draft_report['loudness_gain_db'] == saved_report['loudness_gain_db']
    assert not (edited_report['limiter']['engaged'] or draft_report['limiter']['engaged'])
    level, raw, aligned = _errors(edited_audition, edited_full)
    if section_edit:
        np.testing.assert_array_equal(saved_audition, edited_audition)
        assert level > 1 and raw > -20
    else:
        assert level == pytest.approx(2.85, abs=0.05)
        assert raw == pytest.approx(-8.2, abs=0.2)
    assert aligned < -100


def test_target_only_reuses_whole_gain_for_quiet_passage(tmp_path):
    t = np.arange(38 * SR) / SR
    full, context = _source(tmp_path, np.where(t < 32, 0.01, 0.12) * np.sin(2 * np.pi * 440 * t))
    saved = ChainParams(highpass_hz=0, target_lufs=-20)
    draft = replace(saved, target_lufs=-17)
    _, full_report = _render(tmp_path, full, saved, 'saved-full')
    reference, _ = _render(tmp_path, full, draft, 'edited-full')
    baseline, edited, basis = for_pair(saved, draft, full_report['calibration'], SR)
    assert basis['gain'] == 'retained_target_delta'
    assert edited.pre_limiter_gain_db == baseline.pre_limiter_gain_db + 3
    audition, result = _render(tmp_path, context, draft, 'retained', edited)
    legacy, _, legacy_basis = for_pair(saved, draft, None, SR)
    local, _ = _render(tmp_path, context, draft, 'local', legacy)
    assert legacy_basis['gain'] == 'section'
    assert result['limiter']['max_gr_db'] == 0
    assert not any('peak limiting' in warning for warning in result['warnings']), result['warnings']
    retained_errors = _errors(audition, reference)
    assert abs(retained_errors[0]) < 0.001 and max(retained_errors[1:]) < -100, retained_errors
    np.testing.assert_allclose(audition, reference, rtol=0, atol=TWO_LSB_24)
    local_level, local_raw, local_aligned = _errors(local, reference)
    assert local_level > 15 and local_raw > 10 and local_aligned < -85


def test_active_voice_calibration_respects_upstream_dependencies(tmp_path):
    t = np.arange(35 * SR) / SR
    # Lower-level passages set the median, loud syllables exercise the leveler,
    # and short out-of-band spikes exercise de-essing and low-mid control.
    x = (0.03 + 0.25 * (t % 4 < 0.05)) * np.sin(2 * np.pi * 1100 * t)
    x += 0.12 * np.sin(2 * np.pi * 6300 * t) * ((t > 6) & (t < 6.16))
    x += 0.15 * np.sin(2 * np.pi * 320 * t) * ((t > 7) & (t < 7.6))
    full, context = _source(tmp_path, x)
    saved = ChainParams(highpass_hz=0, compressor='leveler', deess_db=4, deess_mix=False, adaptive_lowmid_db=1.5)
    draft = replace(saved, target_lufs=saved.target_lufs - 2)
    _, report = _render(tmp_path, full, saved, 'saved-full')
    assert all(report[stage]['engaged'] for stage in ('compressor', 'deess', 'adaptive_lowmid'))
    calibration = report['calibration']
    assert {'leveler_threshold_dbfs', 'deess', 'lowmid'} <= calibration.keys()
    saved_cal, draft_cal, basis = for_pair(saved, draft, calibration, SR)
    assert saved_cal.deess == draft_cal.deess == calibration['deess']
    assert set(basis['retained_detectors']) == {'leveler_threshold_dbfs', 'deess', 'lowmid'}
    full_audio, full_edited = _render(tmp_path, full, draft, 'edited-full')
    audition_audio, audition = _render(tmp_path, context, draft, 'edited-audition', draft_cal)
    assert audition['compressor']['thresh_dbfs'] == report['compressor']['thresh_dbfs']
    assert audition['deess']['threshold_db'] == report['deess']['threshold_db']
    assert all(audition[stage]['engaged'] for stage in ('compressor', 'deess', 'adaptive_lowmid'))
    assert audition['limiter']['engaged'] and full_edited['limiter']['engaged']
    errors = _errors(audition_audio, full_audio)
    assert abs(errors[0]) < 0.05 and max(errors[1:]) < -45, errors
    for stage in ('compressor', 'deess', 'adaptive_lowmid', 'limiter'):
        assert audition[stage]['max_gr_db'] == pytest.approx(full_edited[stage]['max_gr_db'], abs=0.05), stage
    saved_cal, draft_cal, basis = for_pair(saved, replace(saved, eq_bands=[BAND]), calibration, SR)
    assert saved_cal.deess is draft_cal.deess is None
    assert saved_cal.leveler_threshold_dbfs is draft_cal.leveler_threshold_dbfs is None
    assert basis['retained_detectors'] == ['lowmid']
    _, _, basis = for_pair(saved, replace(saved, denoise='strong'), calibration, SR)
    assert basis['retained_detectors'] == []
    _, _, basis = for_pair(saved, replace(saved, deess_db=6), calibration, SR)
    assert set(basis['retained_detectors']) == {'leveler_threshold_dbfs', 'lowmid'}


def test_volume_only_uses_complete_peak_cap_and_channel_dc(tmp_path):
    t = np.arange(34 * SR) / SR
    # The deciding crest and the larger DC offset are beyond the bounded context.
    wave = np.where((t >= 32) & (t < 32.04), 0.75, 0.02) * np.sin(2 * np.pi * 440 * t)
    bias = np.where(t < 30, 0.009, 0.05)
    full, context = _source(tmp_path, np.column_stack((wave + bias, wave - bias)))
    saved = ChainParams(highpass_hz=0, volume_only=True, target_lufs=-15)
    draft = replace(saved, target_lufs=-8)
    reference, saved_report = _render(tmp_path, full, saved, 'saved-full')
    assert saved_report['limiter']['engaged']
    volume = saved_report['calibration']['volume']
    assert volume['dc'] == pytest.approx([0.01382, -0.01382], abs=0.00005)
    cap = saved.tp_ceiling_db + LIGHT_MAX_LIMIT_DB - volume['peak_dbtp']
    assert saved_report['loudness_gain_db'] == round(cap, 2)
    saved_cal, draft_cal, _ = for_pair(saved, draft, saved_report['calibration'], SR)
    saved_audio, baseline = _render(tmp_path, context, saved, 'saved-audition', saved_cal)
    edited_audio, edited = _render(tmp_path, context, draft, 'edited-audition', draft_cal)
    assert baseline['limiter']['engaged'] is edited['limiter']['engaged'] is False
    assert baseline['loudness_gain_db'] == edited['loudness_gain_db'] == round(cap, 2)
    np.testing.assert_array_equal(saved_audio, edited_audio)
    assert np.max(np.abs(saved_audio - reference)) < 1e-6
    tighter = replace(draft, tp_ceiling_db=-3)
    _, tighter_cal, _ = for_pair(saved, tighter, saved_report['calibration'], SR)
    _, tighter_report = _render(tmp_path, context, tighter, 'tighter-audition', tighter_cal)
    assert tighter_report['loudness_gain_db'] == pytest.approx(edited['loudness_gain_db'] - 2, abs=0.01)


def test_post_limiter_correction_is_applied_once(tmp_path, monkeypatch):
    t = np.arange(10 * SR) / SR
    full, _ = _source(tmp_path, 0.05 * np.sin(2 * np.pi * 1000 * t))
    params = ChainParams(highpass_hz=0)

    # Inject a reported ceiling miss so the fallback is repeatable across platforms.
    def limited(x, **_kwargs):
        return x, {'engaged': True, 'ceiling_met': False, 'tp_after': 0.0, 'max_gr_db': 0.1}

    monkeypatch.setattr('audiolab.recipes.chain.tp_limit', limited)
    corrected, report = _render(tmp_path, full, params, 'corrected')
    stored = {**report['calibration'], 'post_limiter_gain_db': -0.5}
    _, same, _ = for_pair(params, params, stored, SR)
    replay, replay_report = _render(tmp_path, full, params, 'same', same)
    assert same.post_limiter_gain_db == -0.5
    assert replay_report['limiter']['corrective_gain_db'] == -1.05
    np.testing.assert_array_equal(corrected, replay)
    _, edited, _ = for_pair(params, replace(params, target_lufs=-15), stored, SR)
    assert edited.post_limiter_gain_db is None

    # The stronger retained attenuation also works when there is no new miss.
    def unchanged(x, **_kwargs):
        return x, {'engaged': False, 'ceiling_met': True, 'tp_after': -10.0}

    monkeypatch.setattr('audiolab.recipes.chain.tp_limit', unchanged)
    replay, replay_report = _render(tmp_path, full, params, 'saved-correction', same)
    dry, _ = _render(tmp_path, full, params, 'no-correction', AuditionCalibration())
    assert replay_report['limiter']['corrective_gain_db'] == -0.5
    assert _errors(replay, dry)[0] == pytest.approx(-0.5, abs=0.001)


def test_omitted_post_limiter_correction_is_declared(tmp_path):
    t = np.arange(40 * SR) / SR
    x = 0.01 * np.sin(2 * np.pi * 440 * t)
    # One 2 ms click beyond the 30 s context needs more than the limiter's 12 dB: whole-master correction.
    click = (t >= 35) & (t < 35.002)
    x[click] = 0.99 * np.sign(np.sin(2 * np.pi * 1000 * t[click]))
    full, context = _source(tmp_path, x)
    saved = ChainParams(highpass_hz=0, target_lufs=-27, tp_ceiling_db=-1)
    saved_full, report = _render(tmp_path, full, saved, 'saved-full')
    correction = report['calibration']['post_limiter_gain_db']
    assert correction == pytest.approx(-1.405, abs=0.05)
    _, same, basis = for_pair(saved, replace(saved, sample_rate=44100), report['calibration'], SR)
    assert same.post_limiter_gain_db == correction and 'omitted_post_limiter_gain_db' not in basis

    draft = replace(saved, tp_ceiling_db=-2)
    saved_cal, draft_cal, basis = for_pair(saved, draft, report['calibration'], SR)
    assert basis['gain'] == 'retained' and basis['omitted_post_limiter_gain_db'] == correction
    edited_full, _ = _render(tmp_path, full, draft, 'edited-full')
    saved_side, _ = _render(tmp_path, context, saved, 'saved-audition', saved_cal)
    edited_side, edited = _render(tmp_path, context, draft, 'edited-audition', draft_cal)
    # The declared limitation: neither side has a correction, so the pair shows no difference
    # while the full renders differ by 1 LU, and gain alignment hides the level error.
    np.testing.assert_array_equal(saved_side, edited_side)
    assert programme_loudness(edited_full, SR) - programme_loudness(saved_full, SR) == pytest.approx(-1, abs=0.02)
    level, raw, aligned = _errors(saved_side, saved_full)
    assert level == pytest.approx(-correction, abs=0.02) and raw > -20 and aligned < -100
    # Lowering the target far enough removes the edited full render's need for a correction. The
    # omitted saved value therefore cannot establish that both sides are louder than their full renders.
    quieter = replace(saved, target_lufs=-30)
    _, quieter_cal, quieter_basis = for_pair(saved, quieter, report['calibration'], SR)
    quieter_full, quieter_report = _render(tmp_path, full, quieter, 'quieter-full')
    quieter_side, _ = _render(tmp_path, context, quieter, 'quieter-audition', quieter_cal)
    assert quieter_basis['omitted_post_limiter_gain_db'] == correction
    assert quieter_report['calibration']['post_limiter_gain_db'] == 0
    assert _errors(quieter_side, quieter_full)[0] == pytest.approx(0, abs=0.02)
    # The passage is quieter than the target by design; that is not attributed to limiting.
    assert not edited['limiter']['engaged'] and not edited['warnings']
    # An uncalibrated silent passage also leaves gain at 0 dB without blaming peak limiting.
    silent_path = tmp_path / 'silent-context.wav'
    sf.write(silent_path, np.zeros((10 * SR, 2), dtype=np.float32), SR, subtype='FLOAT')
    _, silent_report = _render(tmp_path, silent_path, saved, 'silent-audition', AuditionCalibration())
    assert not silent_report['limiter']['engaged'] and not silent_report['warnings']


def test_eq_normalization_round_trips_touching_minimum_sections():
    bands = [{'hz': 441.25, 'db': -1.125, 'q': 2.125}]
    canonical = [{'hz': 441.3, 'db': -1.13, 'q': 2.13}]
    assert eq_bands(bands) == eq_bands(canonical) == canonical
    first = {'start_s': 2.208, 'end_s': 12.208, 'fade_s': 5, 'bands': bands}
    second = {'start_s': 12.208, 'end_s': 22.208, 'fade_s': 5, 'bands': bands}
    assert section_eq([second, first]) == [{**section, 'bands': canonical} for section in (first, second)]
    assert section_eq([{**first, 'end_s': 12.207, 'fade_s': 0.5}]) is None
    assert section_eq([first, {**second, 'start_s': 12.207}]) is None
