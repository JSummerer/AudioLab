"""Stationary-noise reduction (spectral gate) for speech and quiet recordings.

Memory-bounded: the STFT runs over ~20 s chunks with a frame-aligned margin,
so a full-length complex spectrogram is never materialised. Chunk boundaries
sit on the global hop grid, so interior frames are identical to whole-signal
processing and no crossfade is needed.

Method: a per-bin noise power profile is estimated from the quietest frames of
the whole programme; each STFT bin then gets a Wiener-like gain
    g = max(floor, 1 - over * noise / power)
smoothed over time and frequency (reduces "musical noise"). One mask computed
on the mid signal is applied to both channels, so the stereo image is kept.
"""

import math
from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter

NFFT = 2048
HOP = 512  # 75 % overlap; hann analysis + synthesis
_WOLA_NORM = 1.5  # sum of hann^2 at 75 % overlap
MODES = {'light': 8.0, 'strong': 15.0}  # max reduction in dB


def _frames(x: np.ndarray, win: np.ndarray) -> np.ndarray:
    """(n_frames, NFFT) windowed frames of a 1-D signal whose length fits the grid."""
    n_frames = 1 + (len(x) - NFFT) // HOP
    idx = np.arange(NFFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    return x[idx] * win


def _stft(x: np.ndarray, win: np.ndarray) -> np.ndarray:
    return np.fft.rfft(_frames(x, win), axis=1).astype(np.complex64)


def _istft(spec: np.ndarray, win: np.ndarray, length: int) -> np.ndarray:
    frames = np.fft.irfft(spec, n=NFFT, axis=1).astype(np.float32) * win
    out = np.zeros(length, dtype=np.float32)
    for i in range(frames.shape[0]):
        out[i * HOP : i * HOP + NFFT] += frames[i]
    return out / _WOLA_NORM


def estimate_noise_profile(
    x: np.ndarray, chunk_s: float = 20.0, sr: int = 48000, quiet_pct: float = 15.0
) -> np.ndarray | None:
    """Per-bin noise power (mid signal) from the quietest frames of each chunk."""
    mono = x.mean(axis=1) if x.ndim > 1 else x
    win = np.hanning(NFFT).astype(np.float32)
    chunk = max(NFFT * 4, int(chunk_s * sr) // HOP * HOP)
    profiles = []
    for s0 in range(0, len(mono) - NFFT, chunk):
        seg = mono[s0 : s0 + chunk + NFFT]
        if len(seg) < NFFT * 4:
            continue
        p = np.abs(_stft(seg, win)) ** 2
        e = p.sum(axis=1)
        live = e > 1e-14  # ignore digital silence
        if live.sum() < 8:
            continue
        thr = np.percentile(e[live], quiet_pct)
        quiet = p[live & (e <= thr)]
        if len(quiet):
            profiles.append(quiet.mean(axis=0))
    if not profiles:
        return None
    # Lower quartile across chunks: robust against chunks with no real pauses.
    return np.percentile(np.stack(profiles), 25, axis=0).astype(np.float32)


def spectral_gate(
    x: np.ndarray, sr: int = 48000, mode: str = 'light', progress=None, chunk_s: float = 20.0
) -> tuple[np.ndarray, dict[str, Any]]:
    """Reduce stationary background noise (hiss, hum beds, room tone).

    Returns (y, stats). y is float32 with the input's shape.
    """
    reduction_db = MODES.get(mode, MODES['light'])
    squeeze = x.ndim == 1
    xx = x[:, None] if squeeze else x
    xx = xx.astype(np.float32, copy=False)
    n, n_ch = xx.shape

    if n < NFFT * 4:
        return x, {'applied': False, 'reason': 'too short'}

    noise = estimate_noise_profile(xx, chunk_s=chunk_s, sr=sr)
    if noise is None:
        return x, {'applied': False, 'reason': 'no usable noise estimate'}

    floor = np.float32(10.0 ** (-reduction_db / 20.0))
    over = np.float32(1.5)  # over-subtraction factor
    win = np.hanning(NFFT).astype(np.float32)
    out = np.empty_like(xx)
    chunk = max(NFFT * 8, int(chunk_s * sr) // HOP * HOP)
    margin = 4 * NFFT  # frame-aligned (multiple of HOP)
    mean_gain = []

    for s0 in range(0, n, chunk):
        # Segment with a margin on both sides (zero-padded at the file edges).
        lo, hi = s0 - margin, min(n, s0 + chunk) + margin
        seg = np.zeros((hi - lo, n_ch), dtype=np.float32)
        src_lo, src_hi = max(0, lo), min(n, hi)
        seg[src_lo - lo : src_hi - lo] = xx[src_lo:src_hi]
        # Samples after the last full frame (< HOP) fall inside the discarded margin.
        mid = seg.mean(axis=1)
        spec_mid = _stft(mid, win)
        power = np.abs(spec_mid) ** 2
        gain = 1.0 - over * noise[None, :] / (power + 1e-20)
        gain = np.clip(gain, floor, 1.0).astype(np.float32)
        gain = uniform_filter(gain, size=(3, 5), mode='nearest')  # time x frequency smoothing
        mean_gain.append(float(gain.mean()))
        for c in range(n_ch):
            y = _istft(_stft(seg[:, c], win) * gain, win, len(seg))
            out[s0 : min(n, s0 + chunk), c] = y[margin : margin + min(n, s0 + chunk) - s0]
        if progress is not None:
            progress(100.0 * min(n, s0 + chunk) / n)

    avg_red = -20.0 * math.log10(max(1e-6, float(np.mean(mean_gain))))
    stats = {
        'applied': True,
        'mode': mode,
        'max_reduction_db': reduction_db,
        'avg_reduction_db': round(avg_red, 2),
        'noise_floor_db': round(
            10.0 * math.log10(2.0 * float(noise.sum()) / (NFFT**2 * float((win**2).mean())) + 1e-24), 1
        ),
    }
    return (out[:, 0] if squeeze else out), stats
