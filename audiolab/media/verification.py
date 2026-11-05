"""Measure the audio decoded from a finished export in one FFmpeg pass."""

import re
from pathlib import Path
from typing import Literal, TypedDict

from ..context import Progress
from ..errors import AudioLabError
from .ffmpeg import run_ffmpeg

PEAK_TOLERANCE_DB = 0.05
_NUMBER = r'[+-]?\d+(?:\.\d+)?'
_LOUDNESS = re.compile(rf'^\s+I: +({_NUMBER}) LUFS\s*\n\s+Threshold: +({_NUMBER}) LUFS$', re.MULTILINE)

# Measure loudness at the original rate and take the higher of the original and reconstructed peaks.
# The explicit resampler drains buffered samples at EOF. ebur128's internal peak meter can miss the
# tail of the file, and integer resampling would clip peaks before we could measure them.
_METER = (
    '[0:a:0]asplit[native][peak];'
    '[native]ebur128=framelog=verbose,astats@native=measure_perchannel=none:measure_overall=Peak_level[loudness];'
    '[peak]aresample=192000:osf=dbl,astats@reconstructed=measure_perchannel=none:measure_overall=Peak_level[truepeak]'
)


class ExportMeasurement(TypedDict):
    status: Literal['verified', 'warning']
    integrated_lufs: float | None
    true_peak_dbtp: float | None
    ceiling_dbtp: float
    ceiling_met: bool
    tolerance_db: float
    message: str | None


def _read_peak(stderr: str, meter: str) -> float | None:
    match = re.search(rf'^\[astats@{meter} @[^\n]+\] Peak level dB: ({_NUMBER}|-inf)$', stderr, re.MULTILINE)
    return float(match[1]) if match else None


def measure_export(
    path: Path, *, ceiling_dbtp: float, progress: Progress | None = None, duration_s: float | None = None
) -> ExportMeasurement:
    """Measure the first audio stream to EOF. Raises AudioLabError if the meter cannot verify it.

    Loudness is gated integrated LUFS. True peak is an estimate at 192 kHz, with a small tolerance
    against the configured ceiling. An absent loudness reading or a peak of digital silence is null.
    """
    result = run_ffmpeg(
        [
            '-loglevel',
            'info',
            '-xerror',
            '-abort_on',
            'empty_output_stream',
            '-i',
            str(path),
            '-filter_complex',
            _METER,
            '-map',
            '[loudness]',
            '-map',
            '[truepeak]',
            '-c:a',
            'pcm_f32le',
            '-f',
            'null',
            '-',
        ],
        progress=progress,
        duration_s=duration_s,
        what='export verification',
    )
    stderr = result.stderr_tail
    loudness = _LOUDNESS.search(stderr)
    native_peak = _read_peak(stderr, 'native')
    reconstructed_peak = _read_peak(stderr, 'reconstructed')
    if loudness is None or native_peak is None or reconstructed_peak is None:
        raise AudioLabError('The audio meter did not return a complete measurement.', detail=stderr)

    lufs, threshold = map(float, loudness.groups())
    # ebur128 prints its gate floor and an unset threshold if no block passes (silence, very quiet
    # audio, or no complete 400 ms block).
    integrated_lufs = None if lufs == -70.0 and threshold == 0.0 else lufs
    peak = max(native_peak, reconstructed_peak)
    true_peak_dbtp = None if peak == float('-inf') else round(peak, 2)
    ceiling_met = true_peak_dbtp is None or true_peak_dbtp <= round(ceiling_dbtp + PEAK_TOLERANCE_DB, 2)
    message = None
    if not ceiling_met:
        message = (
            f'{path.name} peaks at {true_peak_dbtp:+.2f} dBTP after export, above the {ceiling_dbtp:+.2f} dBTP ceiling.'
        )
    return {
        'status': 'verified' if ceiling_met else 'warning',
        'integrated_lufs': integrated_lufs,
        'true_peak_dbtp': true_peak_dbtp,
        'ceiling_dbtp': ceiling_dbtp,
        'ceiling_met': ceiling_met,
        'tolerance_db': PEAK_TOLERANCE_DB,
        'message': message,
    }
