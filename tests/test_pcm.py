"""The compare players' PCM decoding: edge cases of WebM's seek correction, and stopping on quit."""

import threading

import numpy as np
import pytest

from audiolab.errors import Cancelled
from audiolab.media import ffmpeg, proc
from audiolab.media.ffmpeg import run_ffmpeg


@pytest.mark.parametrize(
    'name,graph',
    [
        # too short for the first passage it measures at (1 s + 0.4 s)
        ('short.webm', 'anoisesrc=d=1.2:c=pink:r=48000:a=0.3'),
        # silent where it measures, and ends before the next place
        ('quiet.webm', 'anoisesrc=d=3:c=pink:r=48000:a=0.3,volume=0:enable=lt(t\\,1.5)'),
    ],
)
def test_webm_without_a_measurable_passage_still_joins_exactly(name, graph, tmp_path, monkeypatch):
    """No seek correction can be measured: chunks are decoded from the start instead (exact), not by an
    uncorrected seek (1 ms off)."""
    monkeypatch.setattr(proc, '_POLL_S', 0.01)
    src = tmp_path / name
    run_ffmpeg(['-f', 'lavfi', '-i', graph, '-ac', '2', '-c:a', 'libopus', '-b:a', '128k', str(src)])
    g = ffmpeg.PCM_GRID
    pcm = lambda offset, frames: np.frombuffer(ffmpeg.decode_pcm(src, offset, frames), np.float32).reshape(-1, 2)
    full = pcm(0, 100 * g)
    chunk = pcm(20 * g, 5 * g)  # 0.8 s: past the 0.48 s pre-roll, so it would seek
    assert ffmpeg._seek_shift(src, None) is None
    assert chunk.shape == (5 * g, 2) and np.abs(chunk - full[20 * g : 25 * g]).max() < 1e-6


def test_quitting_stops_running_decodes(tmp_path, monkeypatch):
    src = tmp_path / 'a.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'anoisesrc=d=2:r=48000', '-ac', '2', str(src)])
    started = threading.Event()
    real_run = proc.run

    def slow_run(cmd, progress=None, **kw):  # the decode is "running" until quitting cancels it
        started.set()
        while not progress.cancelled:
            threading.Event().wait(0.01)
        progress.check_cancel()
        return real_run(cmd, progress=progress, **kw)

    monkeypatch.setattr(proc, 'run', slow_run)
    errors, stop = [], threading.Event()  # AppState.pcm_stop, set by AppState.shutdown
    t = threading.Thread(target=lambda: _catch(errors, lambda: ffmpeg.decode_pcm(src, 0, ffmpeg.PCM_GRID, stop)))
    t.start()
    assert started.wait(5)
    stop.set()
    t.join(5)
    assert not t.is_alive() and errors and isinstance(errors[0], Cancelled)


def _catch(errors, fn):
    try:
        fn()
    except Exception as e:
        errors.append(e)


def test_quitting_stops_the_webm_seek_measurement(tmp_path, monkeypatch):
    """The first seek into a WebM measures its seek correction first (several decodes): quitting stops that too."""
    src = tmp_path / 'a.webm'
    run_ffmpeg(['-f', 'lavfi', '-i', 'anoisesrc=d=3:r=48000', '-ac', '2', '-c:a', 'libopus', str(src)])
    stop, runs = threading.Event(), []
    stop.set()  # quitting already

    def run(cmd, progress=None, **kw):
        runs.append(cmd)
        progress.check_cancel()
        raise AssertionError('a decode ran although AudioLab is quitting')

    monkeypatch.setattr(proc, 'run', run)
    with pytest.raises(Cancelled):
        ffmpeg.decode_pcm(src, 50 * ffmpeg.PCM_GRID, ffmpeg.PCM_GRID, stop)  # 2 s: by a seek
    assert len(runs) == 1
