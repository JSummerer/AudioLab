"""Continuous level measurements agree with the array meters and cover excerpt gaps."""

import json
from dataclasses import asdict

import numpy as np
import pytest
import soundfile as sf

from audiolab.analysis import levels, profile
from audiolab.dsp.limiter import true_peak_db
from audiolab.dsp.loudness import lra, programme_loudness, stream_programme_levels

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


def test_whole_file_array_path_does_not_rescan(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, 'scan_levels', lambda *args: pytest.fail('The samples are already in memory.'))
    path = tmp_path / 'short.wav'
    sf.write(path, _tone(1.0), SR, subtype='FLOAT')

    report, _, _ = profile.analyze_file(path)

    assert report['analysis_basis'] == report['level_analysis_basis'] == 'whole file'


def test_extreme_finite_float_audio_keeps_level_readings_json_safe(tmp_path):
    x = _tone(4) * np.float32(1e20)
    path = tmp_path / 'above-full-scale.wav'
    sf.write(path, x, SR, subtype='FLOAT')

    with np.errstate(over='raise', invalid='raise'):
        scan = levels.scan_levels(path, SR, len(x))

    json.dumps(asdict(scan), allow_nan=False)
    assert scan.percentiles['noise_floor_db'] == pytest.approx(377.0, abs=0.1)
    assert scan.percentiles['snr_db'] == pytest.approx(0.0, abs=0.1)
