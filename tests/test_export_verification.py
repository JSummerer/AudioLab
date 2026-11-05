"""Generated audio exercises the meter on finished files and at the end of the stream."""

import threading

import numpy as np
import pytest
import soundfile as sf

from audiolab.context import Progress
from audiolab.dsp.loudness import integrated_lufs
from audiolab.errors import AudioLabError, Cancelled
from audiolab.media import verification
from audiolab.media.export import transcode
from audiolab.media.proc import ProcResult

SR = 48000


@pytest.mark.parametrize('sample_rate', [44100, 96000])
def test_measures_the_encoded_audio_at_its_export_rate(tmp_path, sample_rate):
    tone = 0.25 * np.cos(2 * np.pi * 1000 * np.arange(SR) / SR)
    stereo = np.column_stack((tone, tone)).astype(np.float32)
    master = tmp_path / 'master.flac'
    sf.write(master, stereo, SR, subtype='PCM_24')
    output = transcode(master, tmp_path / 'export', 'wav24', sample_rate=sample_rate)
    assert sf.info(output).samplerate == sample_rate

    reading = verification.measure_export(output, ceiling_dbtp=-1.25)

    assert reading['status'] == 'verified'
    assert reading['integrated_lufs'] == pytest.approx(integrated_lufs(stereo), abs=0.12)
    assert reading['true_peak_dbtp'] == pytest.approx(-12.04, abs=0.07)
    assert reading['ceiling_met'] is True
    assert reading['message'] is None


def test_reconstruction_catches_a_peak_in_the_last_four_samples(tmp_path):
    wave = np.zeros(SR)
    wave[-4:] = 1.3 * np.cos(np.pi / 2 * np.arange(4) + np.pi / 4)
    assert np.abs(wave).max() < 10 ** (-0.5 / 20)  # sample peaks alone would pass
    output = tmp_path / 'tail.wav'
    sf.write(output, wave, SR, subtype='PCM_24')

    reading = verification.measure_export(output, ceiling_dbtp=-0.5)

    assert reading['status'] == 'warning'
    assert reading['true_peak_dbtp'] > 0.0  # the meter must not clip the reconstructed peak itself
    assert reading['ceiling_met'] is False
    assert output.name in reading['message']


@pytest.mark.parametrize(('peak', 'expected'), [(-1.20, True), (-1.19, False)])
def test_ceiling_uses_the_reported_tolerance(tmp_path, peak, expected):
    output = tmp_path / 'near-ceiling.wav'
    sf.write(output, np.full(SR, 10 ** (peak / 20)), SR, subtype='PCM_24')

    reading = verification.measure_export(output, ceiling_dbtp=-1.25)

    assert reading['true_peak_dbtp'] == pytest.approx(peak, abs=0.005)
    assert reading['tolerance_db'] == 0.05
    assert reading['ceiling_met'] is expected


@pytest.mark.parametrize(('gain', 'frames', 'expected_peak'), [(0, SR, None), (1e-5, SR, -100.0), (0.25, 4800, -12.04)])
def test_below_gate_or_short_exports_have_no_integrated_reading(tmp_path, gain, frames, expected_peak):
    output = tmp_path / 'quiet.wav'
    sf.write(output, gain * np.cos(2 * np.pi * 1000 * np.arange(frames) / SR), SR, subtype='PCM_24')

    reading = verification.measure_export(output, ceiling_dbtp=-1.25)

    assert reading['status'] == 'verified'
    assert reading['integrated_lufs'] is None
    assert reading['true_peak_dbtp'] == (pytest.approx(expected_peak, abs=0.12) if expected_peak is not None else None)
    assert reading['ceiling_met'] is True


def test_empty_audio_is_not_verified(tmp_path):
    output = tmp_path / 'empty.wav'
    sf.write(output, np.zeros(0), SR)
    with pytest.raises(AudioLabError):
        verification.measure_export(output, ceiling_dbtp=-1.25)


def test_missing_meter_summary_is_not_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, 'run_ffmpeg', lambda *args, **kwargs: ProcResult(0, '', 'No meter output.'))
    with pytest.raises(AudioLabError, match='complete measurement'):
        verification.measure_export(tmp_path / 'export.wav', ceiling_dbtp=-1.25)


def test_cancellation_is_propagated(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        verification.measure_export(tmp_path / 'export.wav', ceiling_dbtp=-1.25, progress=Progress.null(cancel))
