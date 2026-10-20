"""High-frequency restoration for band-limited (lossy) sources — an honest exciter.

Lossy encoders throw away everything above a cutoff (often 16 kHz). This does
NOT recover the original highs; it synthesises plausible "air" from the octave
just below the cutoff: band-pass [cutoff/2, cutoff] → full-wave rectification
(creates harmonics at 2f) → band-limit to [cutoff, min(2·cutoff, 0.45·sr)] →
mix in, scaled so the new band sits `level_db` below the source octave.
Opt-in only: it invents content, which is a matter of taste.

Two block passes with filter margins (measure, then apply), so memory stays
bounded on multi-hour programmes and no full-length temporaries are created.
"""

import math
from typing import Any

import numpy as np
from scipy.signal import butter, sosfiltfilt

_BLOCK = 1 << 21
_MARGIN = 1 << 14


def _design(cutoff_hz: float, sr: int):
    lo = max(1000.0, cutoff_hz / 2.0)
    hi_top = min(2.0 * cutoff_hz, 0.45 * sr)
    src_band = np.concatenate(
        [butter(2, lo, 'highpass', fs=sr, output='sos'), butter(2, cutoff_hz * 0.98, 'lowpass', fs=sr, output='sos')]
    )
    new_band = np.concatenate(
        [butter(4, cutoff_hz, 'highpass', fs=sr, output='sos'), butter(2, hi_top, 'lowpass', fs=sr, output='sos')]
    )
    return src_band, new_band


def _blocks(n: int):
    for s0 in range(0, n, _BLOCK):
        a, b = max(0, s0 - _MARGIN), min(n, s0 + _BLOCK + _MARGIN)
        yield s0, min(n, s0 + _BLOCK), a, b


def hf_restore(
    x: np.ndarray, sr: int, cutoff_hz: float, level_db: float = -14.0, progress=None
) -> tuple[np.ndarray, dict[str, Any]]:
    """Add synthesized content above `cutoff_hz`. Modifies and returns x (float32)."""
    if not cutoff_hz or cutoff_hz < 8000.0 or cutoff_hz > 0.44 * sr:
        return x, {'applied': False, 'reason': f'cutoff {cutoff_hz} Hz out of range'}
    x = x.astype(np.float32, copy=False)
    n = x.shape[0]
    src_band, new_band = _design(float(cutoff_hz), sr)

    def generated(seg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        band = sosfiltfilt(src_band, seg, axis=0)
        gen = sosfiltfilt(new_band, np.abs(band), axis=0)
        return band, gen

    # Pass 1: energy of the source octave vs the raw generated band.
    e_src = e_gen = 0.0
    for s0, s1, a, b in _blocks(n):
        band, gen = generated(x[a:b])
        e_src += float(np.square(band[s0 - a : s1 - a]).sum())
        e_gen += float(np.square(gen[s0 - a : s1 - a]).sum())
        if progress is not None:
            progress(50.0 * s1 / n)
    if e_gen <= 1e-20 or e_src <= 1e-20:
        return x, {'applied': False, 'reason': 'no content below the cutoff to work from'}
    gain = math.sqrt(e_src / e_gen) * 10.0 ** (level_db / 20.0)

    # Pass 2: add the scaled band in place, front to back. The left margin of
    # each block must see the ORIGINAL samples, so keep the unmodified tail of
    # the previous block around.
    orig_tail = None
    for s0, s1, a, b in _blocks(n):
        seg = x[a:b].copy()
        if orig_tail is not None and s0 > a:
            seg[: s0 - a] = orig_tail[-(s0 - a) :]
        orig_tail = x[max(0, s1 - _MARGIN) : s1].copy()
        _, gen = generated(seg)
        x[s0:s1] += (gain * gen[s0 - a : s1 - a]).astype(np.float32)
        if progress is not None:
            progress(50.0 + 50.0 * s1 / n)
    return x, {
        'applied': True,
        'cutoff_hz': round(float(cutoff_hz)),
        'level_db': level_db,
        'gain_db': round(20.0 * math.log10(gain), 2),
    }
