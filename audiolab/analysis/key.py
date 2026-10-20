"""Musical key estimate (e.g. "A minor · 8A") for tonal music.

Method: mono downmix resampled to 8 kHz → long-window STFT (2 s, so semitones
are resolved down to ~55 Hz) → spectral peaks between 55 Hz and 2 kHz folded
into a 12-bin chroma, corrected for tuning (recordings are rarely exactly at
A = 440 Hz) → Pearson correlation with the 24 rotated Krumhansl–Kessler key
profiles. Confidence comes from the margin between the best and the
runner-up key. Works on the profile's bounded excerpts, so a 2 h set costs
the same as a song.

Returns None rather than guessing for speech, near-silence, noise, a single
sustained note and material without a clear tonal centre. Relative keys (C major / A minor)
share all their notes, so they are the typical confusion; the margin then
stays small and the confidence low.
"""

import math
from fractions import Fraction
from typing import Any

import numpy as np
from scipy.signal import resample_poly

from .classify import tonal_persistence

_FS = 8000
_NFFT = 16384
_HOP = 8192
_FMIN, _FMAX = 55.0, 2000.0
_MAX_FRAMES = 600  # ~10 min of 1 s hops; the profile excerpts are shorter anyway

# Krumhansl & Kessler (1982) probe-tone profiles, tonic first.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NAMES_MAJOR = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
_NAMES_MINOR = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'G#', 'A', 'Bb', 'B']


def camelot(tonic: int, minor: bool) -> str:
    """Camelot wheel code: C major = 8B, A minor = 8A, one step = a fifth."""
    num = ((5 if minor else 8) + 7 * tonic) % 12 or 12
    return f'{num}{"A" if minor else "B"}'


def chroma(mono: np.ndarray, sr: int) -> np.ndarray | None:
    """Tuning-corrected 12-bin pitch-class energy (C = 0), or None if there is
    too little tonal material."""
    ratio = Fraction(_FS, int(sr)).limit_denominator(1000)
    y = resample_poly(mono.astype(np.float32), ratio.numerator, ratio.denominator).astype(np.float32)
    if len(y) < _NFFT:
        return None
    starts = np.arange(0, len(y) - _NFFT + 1, _HOP)
    if len(starts) > _MAX_FRAMES:
        starts = starts[np.linspace(0, len(starts) - 1, _MAX_FRAMES).astype(int)]
    freqs = np.fft.rfftfreq(_NFFT, 1.0 / _FS)
    band = np.nonzero((freqs >= _FMIN) & (freqs <= _FMAX))[0]
    win = np.hanning(_NFFT).astype(np.float32)
    peak_f, peak_w = [], []
    for s0 in starts:
        mag = np.abs(np.fft.rfft(y[s0 : s0 + _NFFT] * win))[band[0] - 1 : band[-1] + 2]
        c = mag[1:-1]
        # Local maxima clearly above the frame's typical level = tonal partials.
        is_peak = (c > mag[:-2]) & (c >= mag[2:]) & (c > 4.0 * np.median(c) + 1e-9)
        if not is_peak.any():
            continue
        w = c[is_peak]
        peak_f.append(freqs[band][is_peak])
        peak_w.append(w / w.sum())  # every frame counts the same
    if len(peak_f) < 4:
        return None
    f, w = np.concatenate(peak_f), np.concatenate(peak_w)
    midi = 69.0 + 12.0 * np.log2(f / 440.0)
    # Tuning: circular mean of the peaks' offset from the equal-tempered grid.
    dev = midi - np.round(midi)
    tuning = float(np.angle((w * np.exp(2j * np.pi * dev)).sum()) / (2 * np.pi))
    pc = np.round(midi - tuning).astype(int) % 12
    return np.bincount(pc, weights=w, minlength=12)


def estimate_key(x: np.ndarray, sr: int, lufs: float | None = None) -> dict[str, Any] | None:
    """{'name': 'A minor', 'camelot': '8A', 'confidence': 0..1} or None."""
    if lufs is not None and lufs < -50.0:
        return None
    mono = (x.mean(axis=1) if x.ndim > 1 else x).astype(np.float32)
    if mono.size == 0 or not np.isfinite(mono).all():
        return None
    # Speech harmonics glide and noise has no stable partials: no key to find.
    tp = tonal_persistence(mono, sr, blocks=4)
    if tp is None or tp < 0.27:
        return None
    c = chroma(mono, sr)
    if c is None or c.sum() <= 0:
        return None
    c = c / c.sum()
    if float(c.std() / c.mean()) < 0.25:  # (almost) flat chroma: atonal / noisy
        return None
    # One sustained note (a test tone, a drone): its own harmonics read as a major chord,
    # but a key needs several notes — music peaks at ≈ 15–25 % per pitch class, a tone at 45 %+.
    if float(c.max()) > 0.4 or int((c >= 0.05).sum()) < 4:
        return None
    scores = []
    for minor, prof in ((False, _MAJOR), (True, _MINOR)):
        for tonic in range(12):
            scores.append((float(np.corrcoef(c, np.roll(prof, tonic))[0, 1]), tonic, minor))
    scores.sort(reverse=True)
    (r1, tonic, minor), (r2, _, _) = scores[0], scores[1]
    if not math.isfinite(r1) or r1 < 0.5:
        return None
    conf = float(np.clip((r1 - r2) / 0.15, 0.0, 1.0) * np.clip((r1 - 0.5) / 0.3, 0.0, 1.0))
    if conf < 0.15:
        return None
    name = (_NAMES_MINOR if minor else _NAMES_MAJOR)[tonic]
    return {
        'name': f'{name} {"minor" if minor else "major"}',
        'camelot': camelot(tonic, minor),
        'confidence': round(conf, 2),
    }
