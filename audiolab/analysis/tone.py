"""How a recording's tonal balance moves over time, for a chart: show it, don't judge it.

The profile's spectrum comes from a few excerpts, so a set whose first part is brighter than the rest
looks normal there. During the whole-file level scan, each 10 s block also yields the energy of seven
bands of its mid signal (a few FFT frames). Per stretch (60 s, longer for very long files) each band's
share of that stretch's own total is compared with its median share over the recording, so loudness
drops out and 0 dB is the recording's typical balance. Tested on two DJ sets and a concert: changes of
2–5 dB lasting many minutes are common in all of them, so this reports no verdict — a change is often
the music itself, and the user previews a part to judge it.
"""

import math
from typing import Any

import numpy as np

BANDS = (
    ('deep_bass', 'Deep bass', 30, 80),
    ('bass', 'Bass', 80, 200),
    ('low_mids', 'Low mids', 200, 500),
    ('mids', 'Mids', 500, 2000),
    ('upper_mids', 'Upper mids', 2000, 6000),
    ('highs', 'Highs', 6000, 12000),
    ('top_end', 'Top end', 12000, 20000),
)
BLOCK_S = 10  # levels._SCAN_BLOCK_S
STEP_S = 60
MAX_POINTS = 600
QUIET_DB = 20.0  # stretches this far below the typical one are pauses or fades: no reading
_NFFT = 8192
_FRAMES = 8


def block_bands(x: np.ndarray, sr: int) -> np.ndarray:
    """Energy of BANDS in one block's mid signal from _FRAMES Hann-windowed FFT frames (zeros when the
    block is shorter than one frame)."""
    mid = x.mean(axis=1, dtype=np.float64) if x.ndim > 1 else x.astype(np.float64)
    out = np.zeros(len(BANDS))
    if len(mid) < _NFFT:
        return out
    freqs = np.fft.rfftfreq(_NFFT, 1.0 / sr)
    win = np.hanning(_NFFT)
    power = np.zeros(len(freqs))
    for start in np.linspace(0, len(mid) - _NFFT, _FRAMES).astype(int):
        power += np.abs(np.fft.rfft(mid[start : start + _NFFT] * win)) ** 2
    for i, (_, _, lo, hi) in enumerate(BANDS):
        out[i] = power[(freqs >= lo) & (freqs < hi)].sum()
    return out


def tone_timeline(blocks: np.ndarray, block_s: int = BLOCK_S) -> dict[str, Any] | None:
    """The chart data (docs/API.md `tone_timeline`) from block_bands rows, or None for fewer than two
    stretches or no audible material."""
    blocks = np.nan_to_num(np.asarray(blocks, dtype=np.float64), nan=0.0, posinf=0.0)
    if blocks.ndim != 2 or len(blocks) == 0:
        return None
    per = max(STEP_S // block_s, math.ceil(len(blocks) / MAX_POINTS))
    steps = len(blocks) // per + (1 if len(blocks) % per >= per / 2 else 0)
    if steps < 2:
        return None
    sums = np.array([blocks[i * per : (i + 1) * per].sum(axis=0) for i in range(steps)])
    total = sums.sum(axis=1)
    if not (total > 0).any():
        return None
    level = 10.0 * np.log10(total + 1e-300)
    audible = (total > 0) & (level > np.median(level[total > 0]) - QUIET_DB)
    with np.errstate(divide='ignore', invalid='ignore'):
        shares = 10.0 * np.log10(sums / total[:, None] + 1e-12)
    reference = np.median(shares[audible], axis=0)
    values = []
    for i in range(steps):
        row = shares[i] - reference
        values.append([round(float(v), 1) for v in row] if audible[i] and np.isfinite(row).all() else None)
    return {
        'step_s': per * block_s,
        'bands': [{'key': key, 'label': label, 'hz': [lo, hi]} for key, label, lo, hi in BANDS],
        'values': values,
        'basis': 'mid-signal band shares per stretch against the recording’s median balance (whole file)',
    }
