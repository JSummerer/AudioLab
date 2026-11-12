"""Continuous level measurements agree with the array meters and cover excerpt gaps."""

import json
import threading
from dataclasses import asdict

import numpy as np
import pytest
import soundfile as sf
from fastapi.encoders import jsonable_encoder

from audiolab.analysis import levels, profile
from audiolab.context import Progress
from audiolab.dsp.bus import mono_below
from audiolab.dsp.limiter import true_peak_db
from audiolab.dsp.loudness import lra, programme_loudness, stream_programme_levels
from audiolab.errors import Cancelled
from audiolab.recipes import resolve

SR = 48000


def _tone(seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.1 * np.sin(2 * np.pi * 997 * t)).astype(np.float32)


@pytest.mark.parametrize(('seconds', 'gain'), [(0.37, 1), (0.43, 1), (7.23, 1), (7.23, 0.001)])
def test_streamed_gates_match_array_meter_across_partial_reads(seconds, gain):
    tone = _tone(seconds) * np.float32(gain)
    tone[len(tone) // 3 : 2 * len(tone) // 3] *= 0.3
    x = np.column_stack((tone, tone * 0.8))
    chunks = (x[start : start + 13717] for start in range(0, len(x), 13717))

    loudness, spread = stream_programme_levels(chunks)

    assert loudness == pytest.approx(programme_loudness(x), abs=1e-5)
    assert spread == pytest.approx(lra(x), abs=1e-5)


@pytest.mark.parametrize('peak', [1.0, 1.8])
def test_full_scan_keeps_peak_and_flat_top_context_at_a_read_boundary(tmp_path, monkeypatch, peak):
    monkeypatch.setattr(levels, '_SCAN_BLOCK_S', 1)
    x = _tone(3.1) + np.float32(0.02)
    x[SR - 2 : SR + 3] = peak
    x[2 * SR - 2 : 2 * SR + 2] = [peak, -peak, -peak, peak]
    x[17] = np.nan
    path = tmp_path / 'reference.wav'
    sf.write(path, x, SR, subtype='FLOAT')
    stereo = np.repeat(np.nan_to_num(x)[:, None], 2, axis=1)

    scan = levels.scan_levels(path, SR, len(x))

    assert scan.true_peak_dbtp == pytest.approx(true_peak_db(stereo), abs=1e-6)
    assert scan.clipped_samples_pct == profile.flat_top_clipping(stereo) > 0
    assert scan.dc_offset == pytest.approx(float(np.abs(stereo.mean(axis=0)).max()), abs=1e-6)
    assert scan.percentiles == profile.noise_floor(stereo, SR)


def test_report_uses_whole_file_levels_when_spectral_excerpts_miss_the_loud_passage(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, '_slice_plan', lambda duration: ([0.15, 0.85], 0.5, 'two excerpts'))
    x = _tone(9.1)
    x[3 * SR : 6 * SR] *= 6
    x[4 * SR : 4 * SR + 200] = 1
    path = tmp_path / 'changing.wav'
    sf.write(path, x, SR, subtype='FLOAT')
    stereo = np.repeat(x[:, None], 2, axis=1)

    report, excerpts, _ = profile.analyze_file(path)

    assert report['analysis_basis'] == 'two excerpts'
    assert report['level_analysis_basis'] == 'whole file'
    assert np.abs(excerpts).max() <= 0.1
    assert report['integrated_lufs'] == round(programme_loudness(stereo), 2)
    assert report['lra'] == round(lra(stereo), 2)
    assert report['true_peak_dbtp'] == round(true_peak_db(stereo), 2)
    assert report['dynamics_style'] == profile.dynamics_style(programme_loudness(stereo), lra(stereo))
    assert report['is_clipping']
    assert 'Peaks are too hot' in {alert['title'] for alert in report['alerts']}


@pytest.mark.parametrize('side_gain', [0.0, 0.02], ids=['mono-excerpts', 'stereo-excerpts'])
def test_club_auto_preserves_local_antiphase_bass_missed_by_excerpts(tmp_path, monkeypatch, side_gain):
    monkeypatch.setattr(levels, '_SCAN_BLOCK_S', 1)
    monkeypatch.setattr(profile, '_slice_plan', lambda duration: ([0.15, 0.85], 1.0, 'two excerpts'))
    t = np.arange(8 * SR) / SR
    fundamental = np.sin(2 * np.pi * 45 * t)
    bass = 0.1 * fundamental
    mids = 0.1 * np.sin(2 * np.pi * 997 * t)
    side = side_gain * np.sin(2 * np.pi * 1300 * t)
    # A short polarity change spans two file reads, outside both spectral excerpts.
    antiphase = (t >= 3.74) & (t < 4.26)
    audio = np.column_stack((mids + bass + side, mids + np.where(antiphase, -bass, bass) - side)).astype(np.float32)
    path = tmp_path / 'local-antiphase.wav'
    sf.write(path, audio, SR, subtype='FLOAT')

    report, excerpts, _ = profile.analyze_file(path)

    sampled_stereo = profile.stereo_info(excerpts, SR)
    assert sampled_stereo['is_mono'] is (side_gain == 0)
    assert sampled_stereo['bass_correlation'] == 1.0 and not sampled_stereo['antiphase_bass']
    if side_gain:
        assert resolve('club', {'stereo': sampled_stereo})[0].mono_bass_hz == 90.0
    assert report['analysis_basis'] == 'two excerpts' and report['stereo_basis'] == 'whole file'
    stereo = report['stereo']
    assert stereo == profile.stereo_info(audio, SR)
    assert jsonable_encoder(report)['stereo'] == stereo
    assert not stereo['is_mono'] and stereo['antiphase_bass']
    assert stereo['bass_correlation'] > 0.8 and stereo['lowest_bass_correlation'] < -0.9
    assert any(alert['title'] == 'Bass is out of phase' for alert in report['alerts'])
    auto, decisions = resolve('club', report)
    assert auto.mono_bass_hz is None
    assert any(d['key'] == 'mono_bass' and 'measured passage' in d['why'] for d in decisions)
    forced, decisions = resolve('club', report, advanced={'mono_bass': 'on'})
    assert forced.mono_bass_hz == 90.0
    assert any(d['key'] == 'mono_bass' and 'folding may thin' in d['why'] for d in decisions)
    folded = mono_below(audio, fc=forced.mono_bass_hz)
    inside = (t >= 3.9) & (t < 4.1)
    ratio = (folded[inside, 0] @ fundamental[inside]) / (audio[inside, 0] @ fundamental[inside])
    assert 20 * np.log10(abs(ratio)) == pytest.approx(-24.6, abs=0.1)


def test_in_phase_stereo_bass_keeps_club_auto_fold_and_mono_skips_it():
    t = np.arange(SR) / SR
    bass = 0.1 * np.sin(2 * np.pi * 45 * t)
    side = 0.1 * np.sin(2 * np.pi * 1300 * t)
    stereo = profile.stereo_info(np.column_stack((bass + side, bass - side)).astype(np.float32), SR)
    assert stereo['width_db'] == 0.0 and not stereo['is_mono'] and not stereo['antiphase_bass']
    assert stereo['bass_correlation'] == stereo['lowest_bass_correlation'] == 1.0
    assert jsonable_encoder(stereo) == stereo
    assert resolve('club', {'stereo': stereo})[0].mono_bass_hz == 90.0
    mono = profile.stereo_info(bass.astype(np.float32), SR)
    assert mono['is_mono'] and not mono['antiphase_bass']
    assert resolve('club', {'stereo': mono})[0].mono_bass_hz is None


@pytest.mark.parametrize(('bass_gain', 'mid_gain'), [(1e-3, 0.2), (2e-6, 0.0)])
def test_negligible_antiphase_bass_does_not_trigger_a_warning(bass_gain, mid_gain):
    t = np.arange(SR) / SR
    bass = bass_gain * np.sin(2 * np.pi * 45 * t)
    mids = mid_gain * np.sin(2 * np.pi * 1300 * t)
    stereo = profile.stereo_info(np.column_stack((mids + bass, mids - bass)).astype(np.float32), SR)
    assert stereo['bass_correlation'] < -0.5
    assert stereo['lowest_bass_correlation'] is None and not stereo['antiphase_bass']


def test_whole_stereo_scan_cancels_between_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(levels, '_SCAN_BLOCK_S', 1)
    path = tmp_path / 'stereo.wav'
    sf.write(path, _tone(3), SR, subtype='FLOAT')
    cancel = threading.Event()
    original = levels._StereoScan.add
    calls = []

    def cancel_after_first_block(self, x):
        original(self, x)
        calls.append(len(x))
        cancel.set()

    monkeypatch.setattr(levels._StereoScan, 'add', cancel_after_first_block)
    with pytest.raises(Cancelled):
        levels.scan_levels(path, SR, 3 * SR, Progress.null(cancel))
    assert calls == [SR]


def test_whole_file_array_path_does_not_rescan(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, 'scan_levels', lambda *args: pytest.fail('The samples are already in memory.'))
    path = tmp_path / 'short.wav'
    sf.write(path, _tone(1.0), SR, subtype='FLOAT')

    report, _, _ = profile.analyze_file(path)

    assert report['analysis_basis'] == report['level_analysis_basis'] == 'whole file'
    assert report['stereo_basis'] == 'whole file'


def test_extreme_finite_float_audio_keeps_level_readings_json_safe(tmp_path):
    x = _tone(4) * np.float32(1e20)
    path = tmp_path / 'above-full-scale.wav'
    sf.write(path, x, SR, subtype='FLOAT')

    with np.errstate(over='raise', invalid='raise'):
        scan = levels.scan_levels(path, SR, len(x))

    json.dumps(asdict(scan), allow_nan=False)
    assert scan.percentiles['noise_floor_db'] == pytest.approx(377.0, abs=0.1)
    assert scan.percentiles['snr_db'] == pytest.approx(0.0, abs=0.1)
    assert scan.stereo['is_mono'] and not scan.stereo['antiphase_bass']


def _energy(lufs):
    return 10.0 ** ((np.asarray(lufs, dtype=np.float64) + 0.691) / 10.0)


def test_density_ignores_slow_drift_between_sections():
    """A long set whose level steps between tracks has a wide whole-file range but dense sections."""
    from audiolab.dsp import loudness

    rng = np.random.default_rng(1)
    steps = np.repeat(np.tile([-13.0, -19.0], 5), 1800)  # 30 min of 100 ms bins, a level step every 3 min
    bins = _energy(steps + rng.normal(0.0, 0.5, steps.size))
    peaks = 10.0 ** ((steps + 10.0) / 20.0)
    density = loudness.programme_density(bins, peaks)
    whole = loudness._gated_range(
        -0.691 + 10 * np.log10(np.lib.stride_tricks.sliding_window_view(bins, 30)[::10].mean(axis=1))
    )
    assert whole > 5.5 and density['section_lra'] < 2.0
    assert density['peak_to_loudness_db'] == pytest.approx(10.0, abs=0.6)
    assert loudness.programme_density(bins[:3000], peaks[:3000]) is None  # under two sections


@pytest.mark.parametrize(
    ('plr', 'section', 'lufs', 'kind'),
    [
        (10.9, 6.1, -15.9, 'club'),
        (10.3, 4.7, -12.3, 'club'),
        (13.0, 7.2, -13.8, 'concert'),
        (7.5, 2.5, -13.0, 'festival'),
        (10.0, 4.0, -10.5, 'festival'),
    ],
)
def test_long_recordings_judge_density_not_absolute_loudness(plr, section, lufs, kind):
    """Measured: two DJ sets and a 76-minute orchestral concert; then two dense cases."""
    style = profile.dynamics_style(lufs, 7.5, {'peak_to_loudness_db': plr, 'section_lra': section, 'sections': 9})
    assert style['type'] == kind
    assert profile.dynamics_style(-15.9, 7.5)['type'] == 'concert'  # short pieces keep the original rule
    if kind == 'club':
        assert style['recommended_glue_comp'] and style['recommended_target_lufs'] >= -13.5


def test_scan_measures_density_on_the_level_grid(tmp_path, monkeypatch):
    from audiolab.dsp import loudness

    monkeypatch.setattr(loudness, 'DENSITY_SECTION_S', 6)
    monkeypatch.setattr(loudness, 'DENSITY_WINDOW_S', 3)
    monkeypatch.setattr(levels, '_SCAN_BLOCK_S', 1)
    x = _tone(24.05)
    path = tmp_path / 'steady.wav'
    sf.write(path, x, SR, subtype='FLOAT')
    scan = levels.scan_levels(path, SR, len(x))
    crest = 20 * np.log10(0.1) - programme_loudness(np.column_stack((x, x)))
    assert scan.density['section_lra'] < 0.5
    assert scan.density['peak_to_loudness_db'] == pytest.approx(crest, abs=0.3)


def test_scan_reports_the_tone_timeline_on_its_own_grid(tmp_path, monkeypatch):
    from audiolab.analysis import tone

    monkeypatch.setattr(levels, '_SCAN_BLOCK_S', 1)
    monkeypatch.setattr(tone, 'STEP_S', 2)
    x = _tone(6.0)
    path = tmp_path / 'steady.wav'
    sf.write(path, x, SR, subtype='FLOAT')
    timeline = levels.scan_levels(path, SR, len(x)).tone_timeline
    assert timeline['step_s'] == 2 and len(timeline['values']) == 3
    assert all(abs(v) < 0.2 for row in timeline['values'] for v in row)  # a steady tone: typical everywhere


def test_original_overview_keeps_brief_opposing_stereo_peaks_between_scan_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(levels, '_SCAN_BLOCK_S', 1)
    monkeypatch.setattr(levels, 'OVERVIEW_MAX_BINS', 8)
    x = np.zeros((6 * SR + 3, 2), dtype=np.float32)
    x[SR - 1] = (0.4, -0.4)
    x[SR + 1] = (-0.9, 0.9)
    x[-1] = (0, -0.6)
    path = tmp_path / 'opposing-peaks.wav'
    sf.write(path, x, SR, subtype='FLOAT')
    overview = levels.scan_levels(path, SR, len(x), include_overview=True).overview
    assert overview['duration_s'] == len(x) / SR
    assert overview['bin_duration_s'] == 36001 / SR
    assert overview['peaks'] == [0, 0.9, 0, 0, 0, 0, 0, 0.6]
