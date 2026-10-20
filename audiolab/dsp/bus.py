"""Bus processing: Sub-bass mono folding and master glue compressor.

mono_below: 2nd-order Butterworth doubled by sosfiltfilt = 4th-order
Linkwitz-Riley; the high band is derived by subtraction (x - low), so the
recombination is exactly flat for any filter phase.

glue_comp: kept float32 end-to-end with bounded temporaries (the float64
implementation transiently allocated ~4x the signal on long sets). Ballistic
expansion uses a zero-order hold at the 1 kHz control rate followed by the
existing 2 ms box smoothing, which masks the stairsteps.

Both take an optional `check_cancel` callable, called between their full-length steps.
"""

import math
from typing import Any

import numpy as np
from scipy.signal import iirfilter, oaconvolve

from .filters import filtfilt_stream

_CTRL_SR = 1000  # control-rate ballistics (Hz)


def mono_below(x: np.ndarray, fc: float = 90.0, sr: int = 48000, check_cancel=None) -> np.ndarray:
    """Sum sub-frequencies below fc (default 90Hz) to mono using zero-phase Linkwitz-Riley crossover.
    Input: stereo audio array of shape (N, 2).
    Leaves stereo width above fc intact while solidly centering sub-bass for club rigs.
    """
    if x.ndim == 1 or x.shape[1] < 2:
        return x
    # 2nd order Butterworth lowpass doubled by zero-phase filtering gives a
    # 4th order Linkwitz-Riley response; high = x - low keeps the recombination
    # exactly flat (amplitude-complementary by construction).
    sos = iirfilter(2, fc, btype='lowpass', ftype='butter', fs=sr, output='sos')
    low = filtfilt_stream(sos, x, check_cancel=check_cancel)
    high = x - low
    low_mono = low.mean(axis=1, keepdims=True)
    return (high + low_mono).astype(np.float32, copy=False)


def glue_comp(
    x: np.ndarray,
    thresh_dbfs: float = -16.5,
    ratio: float = 1.5,
    atk_ms: float = 30.0,
    rel_ms: float = 300.0,
    max_gr_db: float = 1.5,
    sr: int = 48000,
    check_cancel=None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Master bus glue compressor with smooth 1 kHz control-rate ballistics and hard GR clamping.
    Formula: (T / env) ** (1 - 1/ratio) prevents peak explosion.
    Uses edge-padded smoothing to prevent boundary artifacts.
    """
    if x.ndim == 1:
        x = x[:, None]
        squeeze = True
    else:
        squeeze = False
    x = x.astype(np.float32, copy=False)
    n_samples = x.shape[0]
    check = check_cancel or (lambda: None)

    # 50 ms Hanning window power detection
    win = int(0.05 * sr)
    w = np.hanning(win).astype(np.float32)
    w /= w.sum()

    power = (x * x).mean(axis=1)
    check()
    env = np.sqrt(oaconvolve(power, w, mode='same') + np.float32(1e-24))
    del power
    check()

    # Gain calculation (static curve)
    T = np.float32(10.0 ** (thresh_dbfs / 20.0))
    gr = np.ones_like(env)
    overshoot = env > T
    gr[overshoot] = (T / env[overshoot]) ** np.float32(1.0 - 1.0 / ratio)
    del env, overshoot

    # Hard clamp to maximum allowed gain reduction
    floor_gain = np.float32(10.0 ** (-max_gr_db / 20.0))
    gr = np.maximum(gr, floor_gain)

    # Decimate to ~1 kHz control rate for smooth analog-style ballistics
    decim = max(1, sr // _CTRL_SR)
    n_ctrl = len(gr) // decim
    check()
    if n_ctrl > 0:
        gr_ctrl = gr[: n_ctrl * decim].reshape(n_ctrl, decim).mean(axis=1)
        a_atk = math.exp(-1.0 / max(1e-3, atk_ms))
        a_rel = math.exp(-1.0 / max(1e-3, rel_ms))
        out_ctrl = np.empty_like(gr_ctrl)
        prev = 1.0
        for i in range(n_ctrl):
            a = a_atk if gr_ctrl[i] < prev else a_rel
            prev = a * prev + (1.0 - a) * gr_ctrl[i]
            out_ctrl[i] = prev
        # Zero-order hold back to sample rate (bounded, stays float32); the
        # 2 ms box smoothing below removes the stairsteps.
        g = np.repeat(out_ctrl.astype(np.float32), decim)
        g = g[:n_samples]
    else:
        g = gr

    if len(g) < n_samples:
        g = np.pad(g, (0, n_samples - len(g)), mode='edge')
    g = g[:n_samples]

    # Light edge-padded 2ms box smoothing
    check()
    k = max(1, int(0.002 * sr))
    pad_len = k // 2
    g_padded = np.pad(g, pad_len, mode='edge')
    g_smoothed = oaconvolve(g_padded, np.ones(k, dtype=np.float32) / k, mode='valid')[:n_samples]
    g_smoothed = np.maximum(g_smoothed, floor_gain)
    del g, g_padded, gr
    check()

    gr_db = 20.0 * np.log10(g_smoothed.astype(np.float64) + 1e-12)
    stats = {
        'max_gr_db': round(float(-gr_db.min()), 2),
        'mean_gr_db_active': round(float(-gr_db[gr_db < -0.01].mean()), 3) if (gr_db < -0.01).any() else 0.0,
        'pct_time_above_0p5db_gr': round(float((gr_db < -0.5).mean() * 100.0), 2),
    }
    del gr_db
    check()

    out = x * g_smoothed[:, None]
    if squeeze:
        out = out[:, 0]
    return out, stats
