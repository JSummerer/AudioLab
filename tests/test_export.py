"""Artwork fallback preserves generated audio and journals only successful recovery."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audiolab.context import TaskContext
from audiolab.errors import AudioLabError, Cancelled
from audiolab.media import export, ffmpeg


def test_corrupt_prepared_cover_warns_after_audio_is_saved(tmp_path):
    """An internal prepared image can fail after upload validation; keep the usable audio."""
    src = tmp_path / 'tone.wav'
    sf.write(src, np.sin(2 * np.pi * 440 * np.arange(4410) / 44100) * 0.1, 44100)
    cover = tmp_path / 'damaged-prepared.jpg'
    cover.write_bytes(b'not a valid jpeg')
    notes = []
    ctx = TaskContext('art', tmp_path / 'job', {}, [], emit_note=lambda *note: notes.append(note))

    path = export.transcode(src, tmp_path / 'saved', 'mp3', cover=cover, progress=ctx.progress)

    assert path.is_file() and path.stat().st_size > 0
    info = ffmpeg.probe_media(path)
    assert info['audio_codec'] == 'mp3' and not info['has_cover']
    assert len(notes) == 1
    assert notes[0] == (
        'warning',
        f'Saved {path.name} without cover art. The picture could not be added.',
        None,
    )


@pytest.mark.parametrize('error', [AudioLabError('Audio export failed.'), Cancelled()])
def test_failed_or_cancelled_export_does_not_report_audio_as_saved(tmp_path, monkeypatch, error):
    cover = tmp_path / 'prepared.jpg'
    cover.write_bytes(b'fake prepared image')
    notes, calls = [], []
    ctx = TaskContext('art', tmp_path / 'job', {}, [], emit_note=lambda *note: notes.append(note))
    monkeypatch.setattr(export, '_check_encoder', lambda spec: None)
    monkeypatch.setattr(export, 'probe_media', lambda src: {})

    def fail(args, **kwargs):
        calls.append(args)
        raise error

    monkeypatch.setattr(export, 'run_ffmpeg', fail)
    with pytest.raises(type(error)):
        export.transcode(tmp_path / 'source.wav', tmp_path / 'saved', 'mp3', cover=cover, progress=ctx.progress)
    assert len(calls) == (1 if isinstance(error, Cancelled) else 2)
    assert notes == [] and not (tmp_path / 'saved.mp3').exists()
    assert not list(tmp_path.glob('*.partial.mp3'))


@pytest.mark.parametrize('fmt, cover_on', [('mp3', True), ('mp3', False), ('wav', True)])
def test_success_without_fallback_emits_no_missing_art_warning(tmp_path, monkeypatch, fmt, cover_on):
    cover = tmp_path / 'prepared.jpg'
    cover.write_bytes(b'fake prepared image')
    notes, calls = [], []
    ctx = TaskContext('art', tmp_path / 'job', {}, [], emit_note=lambda *note: notes.append(note))
    monkeypatch.setattr(export, '_check_encoder', lambda spec: None)
    monkeypatch.setattr(export, 'probe_media', lambda src: {})

    def succeed(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b'fake encoded output')

    monkeypatch.setattr(export, 'run_ffmpeg', succeed)
    path = export.transcode(
        tmp_path / 'source.wav', tmp_path / 'saved', fmt, cover=cover if cover_on else None, progress=ctx.progress
    )
    assert path.is_file() and len(calls) == 1
    assert ('1:v:0' in calls[0]) == (fmt == 'mp3' and cover_on)
    assert notes == []


@pytest.mark.parametrize(('fmt', 'hours', 'rate', 'big'), [('wav16', 2.7, None, False), ('wav24', 2.7, 96000, True)])
def test_wav_over_4_gb_is_written_as_rf64_with_a_warning(tmp_path, monkeypatch, fmt, hours, rate, big):
    """A plain RIFF header cannot count 4 GB: a 2 h 40 min WAV at 96 kHz/24-bit (5.5 GB) needs RF64."""
    notes, calls = [], []
    ctx = TaskContext('wav', tmp_path / 'job', {}, [], emit_note=lambda *note: notes.append(note))
    monkeypatch.setattr(export, '_check_encoder', lambda spec: None)
    monkeypatch.setattr(export, 'probe_media', lambda src: {'channels': 2, 'sample_rate': 48000})

    def succeed(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b'fake encoded output')

    monkeypatch.setattr(export, 'run_ffmpeg', succeed)
    export.transcode(
        tmp_path / 'source.wav', tmp_path / 'set', fmt, progress=ctx.progress, duration_s=hours * 3600, sample_rate=rate
    )
    assert ('-rf64' in calls[0]) == big
    assert [n[0] for n in notes] == (['warning'] if big else [])
