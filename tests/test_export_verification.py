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


def test_the_limit_a_saved_file_is_checked_against():
    from audiolab.media import export

    assert export.peak_limit('mp3_320', -1.0) == export.peak_limit('opus_160', -1.5) == export.LOSSY_LIMIT_DBTP
    assert export.peak_limit('aac_256', 0.0) == 0.0  # never stricter than the user's own ceiling
    assert export.peak_limit('alac16', -1.0) == export.peak_limit('wav24', -1.0) == -1.0  # lossless, ALAC in .m4a too


def test_a_lossy_export_uses_the_headroom_up_to_the_limit_and_is_turned_down_once_above_it(tmp_path, monkeypatch):
    from audiolab.dsp.limiter import tp_limit
    from audiolab.media import export

    def limited_master(name, ceiling_db):  # loud and limited, pop-like: encoding lifts its peaks
        t = np.arange(4 * SR) / SR
        noise = np.random.default_rng(1).standard_normal(len(t))
        kick = np.sin(2 * np.pi * 55 * t) * np.exp(-(t % 0.5) * 12)
        chord = sum(np.sin(2 * np.pi * f * k * t) / k for f in (220, 277.2, 329.6) for k in range(1, 30)) * 0.08
        mix = kick + 0.3 * noise * np.exp(-(t % 0.25) * 40) + chord
        mix *= 2 / np.abs(mix).max()
        limited, _ = tp_limit(np.column_stack((mix, np.roll(mix, 7))), ceiling_db=ceiling_db, sr=SR)
        sf.write(tmp_path / name, limited.astype(np.float32), SR, subtype='PCM_24')
        return tmp_path / name, verification.measure_export(tmp_path / name, ceiling_dbtp=0)['true_peak_dbtp']

    def check(path, progress, limit, duration_s):
        return verification.measure_export(path, ceiling_dbtp=limit, progress=progress, duration_s=duration_s)

    limit = export.LOSSY_LIMIT_DBTP
    # Over the master's ceiling, under the limit: the MP3 is saved as encoded; the WAV is held to the ceiling.
    master, ceiling = limited_master('quiet.flac', -2.0)  # the master meets its ceiling exactly
    out = tmp_path / 'room'
    outputs = export.export(master, out, 'Song', ['wav24', 'mp3_320'], check=check, ceiling_dbtp=ceiling)
    made = {o['format']: o for o in outputs}
    mp3, wav = made['mp3_320']['verification'], made['wav24']['verification']
    assert made['mp3_320']['trim_db'] == 0.0 and mp3['status'] == 'verified' and mp3['ceiling_dbtp'] == limit
    assert ceiling < mp3['true_peak_dbtp'] <= limit
    assert made['wav24']['trim_db'] == 0.0 and wav['status'] == 'verified' and wav['ceiling_dbtp'] == ceiling

    # Over the limit: one re-encode turned down by the excess plus the margin, if that stays within the bound.
    master, _ = limited_master('hot.flac', -0.3)
    made = {}
    for bound in (0.0, export.TRIM_MAX_DB):  # 0: every needed turn-down is beyond it, so kept as encoded
        monkeypatch.setattr(export, 'TRIM_MAX_DB', bound)
        out = tmp_path / f'bound {bound}'
        (made[bound],) = export.export(master, out, 'Song', ['mp3_320'], check=check, ceiling_dbtp=-1.0)
        assert [p.name for p in out.iterdir()] == ['Song.mp3']  # no second file, no staging
    over, fixed = made[0.0], made[2.0]
    assert over['verification']['ceiling_met'] is False and over['trim_db'] == 0.0
    assert fixed['trim_db'] == round(over['verification']['true_peak_dbtp'] - limit + export.TRIM_MARGIN_DB, 2)
    assert fixed['verification']['status'] == 'verified' and fixed['verification']['true_peak_dbtp'] <= limit
    assert fixed['verification'] == check(
        fixed['path'], Progress.null(), limit, 4.0
    )  # the reading is of the saved file

    # Cancelled while the turned-down version is checked: nothing is saved and nothing is left behind.
    calls = []

    def cancel_on_recheck(path, progress, limit, duration_s):
        calls.append(path)
        if len(calls) == 2:
            raise Cancelled()
        return check(path, progress, limit, duration_s)

    out = tmp_path / 'cancelled'
    with pytest.raises(Cancelled):
        export.export(master, out, 'Song', ['mp3_320'], check=cancel_on_recheck, ceiling_dbtp=-1.0)
    assert len(calls) == 2 and list(out.iterdir()) == []
