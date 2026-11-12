"""Whole-reference levels and stereo measurements from bounded 48 kHz reads."""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

from ..context import Progress
from ..dsp.limiter import TRUE_PEAK_CONTEXT, true_peak_db
from ..dsp.loudness import programme_density, stream_programme_levels
from .tone import block_bands, tone_timeline

SILENCE_DBFS = -80.0  # sample peak below this is treated as silence
_SILENCE_PEAK = 10.0 ** (SILENCE_DBFS / 20.0)
_SCAN_BLOCK_S = 10
_BASS_FOLD_HZ = 90.0  # Club mono-bass crossover
_BASS_CORRELATION_WINDOW_S = 0.5
_BASS_MIN_POWER = 10.0 ** (-80.0 / 10.0)  # -80 dBFS RMS
_BASS_MIN_SHARE = 10.0 ** (-35.0 / 10.0)  # -35 dB relative to local broadband power
OVERVIEW_MAX_BINS = 512


@dataclass(frozen=True)
class ProgrammeLevels:
    lufs: float
    lra: float
    true_peak_dbtp: float
    clipped_samples_pct: float
    dc_offset: float
    percentiles: dict[str, float]
    audible_span: tuple[int, int] | None
    stereo: dict[str, float | bool | None]
    density: dict[str, float] | None = None  # programme_density (long recordings)
    tone_timeline: dict | None = None  # analysis.tone: how the tonal balance moves (chart data)
    overview: dict | None = None


class LevelOverview:
    """Maximum absolute sample across both channels in consecutive, continuous bins."""

    def __init__(self, frames: int, sr: int):
        self._frames = frames
        self._sr = sr
        self._size = max(1, sr // 10, (frames + OVERVIEW_MAX_BINS - 1) // OVERVIEW_MAX_BINS)
        self._peaks = [0.0] * ((frames + self._size - 1) // self._size)

    def add(self, x: np.ndarray, offset: int, check_cancel) -> None:
        limit = offset + len(x)
        pos = offset
        while pos < limit:
            check_cancel()
            # The scan reads 10 s chunks that need not align with the overview bins.
            index = pos // self._size
            end = min(limit, (index + 1) * self._size)
            chunk = x[pos - offset : end - offset]
            self._peaks[index] = max(self._peaks[index], float(chunk.max()), -float(chunk.min()))
            pos = end

    def result(self) -> dict:
        return {
            'basis': 'original_sample_peak',
            'duration_s': self._frames / self._sr,
            'bin_duration_s': self._size / self._sr,
            'peaks': [round(peak, 6) for peak in self._peaks],
        }


def stereo_samples(x: np.ndarray) -> np.ndarray:
    """Use the same channels and broken-sample handling in excerpts and in the scan."""
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    else:
        x = x[:, :2]
    if not np.isfinite(x).all():
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x


def _correlation(energy: np.ndarray) -> float:
    left, right, cross = energy
    denominator = math.sqrt(left) * math.sqrt(right)
    return max(-1.0, min(1.0, float(cross) / denominator)) if denominator else 0.0


class _StereoScan:
    """Accumulate stereo energy and keep the lowest meaningful local bass correlation."""

    def __init__(self, sr: int):
        # Two causal passes have the crossover's squared low-pass magnitude. Carry the state
        # between reads. The phase delay is shared by both channels, and the local windows overlap.
        sos = butter(2, _BASS_FOLD_HZ, 'lowpass', fs=sr, output='sos')
        self._sos = np.tile(sos, (2, 1))
        self._state = np.zeros((len(self._sos), 2, 2))
        self._hop = max(1, round(sr * _BASS_CORRELATION_WINDOW_S / 2))
        self._frames = 0
        self._mid = self._side = 0.0
        self._bass = np.zeros(3)
        self._previous: tuple[np.ndarray, float, int] | None = None
        self._lowest: float | None = None

    def _consider(self, bass: np.ndarray, broadband: float, frames: int) -> None:
        if (bass[0] + bass[1]) / 2 < max(frames * _BASS_MIN_POWER, broadband * _BASS_MIN_SHARE):
            return  # inaudible bass or low-pass leakage from much louder higher frequencies
        corr = _correlation(bass)
        self._lowest = corr if self._lowest is None else min(self._lowest, corr)

    def add(self, x: np.ndarray) -> None:
        low, self._state = sosfilt(self._sos, x, axis=0, zi=self._state)
        self._frames += len(x)
        for start in range(0, len(x), self._hop):
            stop = start + self._hop
            left, right = x[start:stop].T.astype(np.float64)
            mid, side = (left + right) * 0.5, (left - right) * 0.5
            mid_energy, side_energy = float(mid @ mid), float(side @ side)
            self._mid += mid_energy
            self._side += side_energy
            left, right = low[start:stop].T
            bass = np.array([left @ left, right @ right, left @ right])
            self._bass += bass
            broadband = mid_energy + side_energy
            if self._previous is not None:
                prev_bass, prev_broadband, prev_frames = self._previous
                self._consider(bass + prev_bass, broadband + prev_broadband, len(left) + prev_frames)
            self._previous = bass, broadband, len(left)

    def result(self) -> dict[str, float | bool | None]:
        if self._previous is not None and self._frames <= self._hop:
            self._consider(*self._previous)
        floor = max(1, self._frames) * 1e-20
        width_db = 10.0 * math.log10((self._side + floor) / (self._mid + floor))
        return {
            'width_db': round(width_db, 1),
            'is_mono': width_db < -40.0,
            'antiphase_bass': self._lowest is not None and self._lowest < 0.0,
            'bass_correlation': round(_correlation(self._bass), 2),
            'lowest_bass_correlation': None if self._lowest is None else round(self._lowest, 2),
        }


def stereo_info(x: np.ndarray, sr: int, check_cancel=None) -> dict[str, float | bool | None]:
    """Measure an already loaded recording with the same bounded blocks as the file scan."""
    stereo = _StereoScan(sr)
    block = _SCAN_BLOCK_S * sr
    for start in range(0, len(x), block):
        if check_cancel is not None:
            check_cancel()
        own = x[start : start + block]
        stereo.add(stereo_samples(own[:, None] if own.ndim == 1 else own))
    return stereo.result()


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


def scan_levels(
    path: Path, sr: int, frames: int, progress: Progress | None = None, *, include_overview: bool = False
) -> ProgrammeLevels:
    """Measure complete programme levels and stereo with small overlapping reads."""
    progress = progress or Progress.null()
    rms, bin_peaks, bins, band_blocks = [], [], [], []
    tp, sample_peak = -480.0, 0.0
    clipped = 0
    channel_sum = np.zeros(2)
    first_audible = last_audible = None
    stereo = _StereoScan(sr)
    overview = LevelOverview(frames, sr) if include_overview else None

    def samples():
        nonlocal tp, sample_peak, clipped, first_audible, last_audible
        for window, span, offset in _windows(path, sr, frames, TRUE_PEAK_CONTEXT, progress):
            start, stop = span
            own = window[start:stop]
            if overview is not None:
                overview.add(own, offset, progress.check_cancel)
            stereo.add(own)
            channel_sum[:] += own.sum(axis=0, dtype=np.float64)
            rms.append(frame_rms_db(own, sr))
            bin_peaks.append(_bin_peaks(own, sr // 10))
            band_blocks.append(block_bands(own, sr))
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

    lufs, spread = stream_programme_levels(samples(), sr, check_cancel=progress.check_cancel, bins_out=bins)
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
        stereo=stereo.result(),
        density=programme_density(bins[0], np.concatenate(bin_peaks)) if bins and bin_peaks else None,
        tone_timeline=tone_timeline(np.array(band_blocks), _SCAN_BLOCK_S) if band_blocks else None,
        overview=overview.result() if overview is not None else None,
    )


def _bin_peaks(x: np.ndarray, size: int) -> np.ndarray:
    """Sample peak per `size` frames (both channels); a short last bin counts on its own."""
    full = len(x) // size * size
    peaks = np.abs(x[:full]).reshape(-1, size * x.shape[1]).max(axis=1) if full else np.empty(0, np.float32)
    if full < len(x):
        peaks = np.append(peaks, np.abs(x[full:]).max())
    return peaks
