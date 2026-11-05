"""Whole-reference levels from bounded 48 kHz reads, independent of spectral excerpts."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from ..context import Progress
from ..dsp.limiter import TRUE_PEAK_CONTEXT, true_peak_db
from ..dsp.loudness import stream_programme_levels

SILENCE_DBFS = -80.0  # sample peak below this is treated as silence
_SILENCE_PEAK = 10.0 ** (SILENCE_DBFS / 20.0)
_SCAN_BLOCK_S = 10


@dataclass(frozen=True)
class ProgrammeLevels:
    lufs: float
    lra: float
    true_peak_dbtp: float
    clipped_samples_pct: float
    dc_offset: float
    percentiles: dict[str, float]
    audible_span: tuple[int, int] | None


def stereo_samples(x: np.ndarray) -> np.ndarray:
    """Use the same channels and broken-sample handling in excerpts and in the scan."""
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    else:
        x = x[:, :2]
    if not np.isfinite(x).all():
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x


def frame_rms_db(x: np.ndarray, sr: int, frame_s: float = 0.05) -> np.ndarray:
    n = int(frame_s * sr)
    m = len(x) // n
    if m == 0:
        return np.array([-120.0])
    frames = x[: m * n].reshape(m, -1)
    # Accumulate in double precision without a full-size power array. A finite
    # float WAV can be far above full scale; squaring it in float32 can overflow.
    p = np.einsum('ij,ij->i', frames, frames, dtype=np.float64) / frames.shape[1]
    return 10.0 * np.log10(p + 1e-14)


def programme_percentiles(db: np.ndarray) -> dict[str, float]:
    """The 10th percentile and 90th–10th spread of live 50 ms RMS frames."""
    live = db[db > -100.0]
    if live.size < 10:
        return {'noise_floor_db': -120.0, 'snr_db': 120.0}
    nf, loud = np.percentile(live, [10, 90])
    return {'noise_floor_db': round(float(nf), 1), 'snr_db': round(float(loud - nf), 1)}


def flat_top_count(x: np.ndarray, threshold: float, span: tuple[int, int] | None = None) -> int:
    """Count centers of three hot samples, assigning each center to its owning span."""
    start, end = span or (0, len(x))
    start, end = max(1, start), min(len(x) - 1, end)
    if start >= end:
        return 0
    hot = np.abs(x[start - 1 : end + 1]) >= threshold
    return int(np.count_nonzero(hot[:-2] & hot[1:-1] & hot[2:]))


def _windows(
    path: Path, sr: int, frames: int, context: int, progress: Progress
) -> Iterator[tuple[np.ndarray, tuple[int, int], int]]:
    block = _SCAN_BLOCK_S * sr  # aligned to audible 1 s blocks and 50 ms RMS frames
    for start in range(0, frames, block):
        progress.check_cancel()
        stop = min(frames, start + block)
        first = max(0, start - context)
        window, _ = sf.read(path, start=first, stop=min(frames, stop + context), dtype='float32', always_2d=True)
        progress.check_cancel()
        yield stereo_samples(window), (start - first, stop - first), start


def scan_levels(path: Path, sr: int, frames: int, progress: Progress | None = None) -> ProgrammeLevels:
    """Measure complete programme levels with small overlapping reads and a single K-weighting pass."""
    progress = progress or Progress.null()
    rms = []
    tp, sample_peak = -480.0, 0.0
    clipped = 0
    channel_sum = np.zeros(2)
    first_audible = last_audible = None

    def samples():
        nonlocal tp, sample_peak, clipped, first_audible, last_audible
        for window, span, offset in _windows(path, sr, frames, TRUE_PEAK_CONTEXT, progress):
            start, stop = span
            own = window[start:stop]
            channel_sum[:] += own.sum(axis=0, dtype=np.float64)
            rms.append(frame_rms_db(own, sr))
            if window.any():
                tp = max(tp, true_peak_db(window, span=span))
            clipped += flat_top_count(window, 0.999, span)
            for second in range(0, len(own), sr):
                block = own[second : second + sr]
                peak = max(float(block.max()), -float(block.min()))
                sample_peak = max(sample_peak, peak)
                if peak >= _SILENCE_PEAK:
                    if first_audible is None:
                        first_audible = offset + second
                    last_audible = offset + second + len(block)
            progress(90.0 * (offset + len(own)) / frames)
            yield own

    lufs, spread = stream_programme_levels(samples(), sr, check_cancel=progress.check_cancel)
    if sample_peak > 1.41:
        # Preserve the float-file rule: very hot waveforms are checked against their own global peak.
        clipped = sum(
            flat_top_count(x, 0.999 * sample_peak, span) for x, span, _ in _windows(path, sr, frames, 1, progress)
        )
    progress(100)
    return ProgrammeLevels(
        lufs=lufs,
        lra=spread,
        true_peak_dbtp=tp,
        clipped_samples_pct=round(100.0 * clipped / max(1, frames * 2), 4),
        dc_offset=float(np.abs(channel_sum).max() / max(1, frames)),
        percentiles=programme_percentiles(np.concatenate(rms) if rms else np.empty(0)),
        audible_span=None if first_audible is None else (first_audible, last_audible),
    )
