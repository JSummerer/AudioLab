"""Bus processing: Sub-bass mono folding and master glue compressor.

mono_below: 2nd-order Butterworth doubled by sosfiltfilt = 4th-order
Linkwitz-Riley; the high band is derived by subtraction (x - low), so the
recombination is exactly flat for any filter phase.

glue_comp: the detector and gain application work in bounded windows. Only
the 1 kHz controls span the programme. Their attack/release state carries
across windows, and the 2 ms box smoothing reads real neighbouring gains.

Both take an optional `check_cancel` callable, called between their full-length steps.
"""

import math
from typing import Any

import numpy as np
from scipy.signal import iirfilter, oaconvolve

from .filters import filtfilt_stream
from .scratch import BLOCK_FRAMES, blocks
from .timeline import ReductionTimeline

_CTRL_SR = 1000  # control-rate ballistics (Hz)


def mono_below(
    x: np.ndarray,
    fc: float = 90.0,
    sr: int = 48000,
    check_cancel=None,
    *,
    band_buffer: np.ndarray | None = None,
) -> np.ndarray:
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
    low = filtfilt_stream(sos, x, check_cancel=check_cancel, out=band_buffer)
    for s in blocks(len(x), check_cancel):
        band = low[s]
        band[:] = (x[s] - band) + band.mean(axis=1, keepdims=True)
    return low


def glue_comp(
    x: np.ndarray,
    thresh_dbfs: float = -16.5,
    ratio: float = 1.5,
    atk_ms: float = 30.0,
    rel_ms: float = 300.0,
    max_gr_db: float = 1.5,
    sr: int = 48000,
    check_cancel=None,
    *,
    in_place: bool = False,
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

    # 50 ms Hanning window power detection
    win = int(0.05 * sr)
    w = np.hanning(win).astype(np.float32)
    w /= w.sum()

    threshold = np.float32(10.0 ** (thresh_dbfs / 20.0))
    slope = np.float32(1.0 - 1.0 / ratio)
    floor_gain = np.float32(10.0 ** (-max_gr_db / 20.0))
    decim = max(1, sr // _CTRL_SR)
    n_ctrl = n_samples // decim
    detect_until = n_ctrl * decim if n_ctrl else n_samples
    step = max(decim, BLOCK_FRAMES // decim * decim)
    gain = np.empty(n_ctrl or n_samples, dtype=np.float32)
    for s in blocks(detect_until, check_cancel, size=step):
        lo = max(0, s.start - win // 2)
        hi = min(n_samples, s.stop + win // 2)
        samples = x[lo:hi]
        power = (samples * samples).mean(axis=1)
        mean_power = oaconvolve(power, w, mode='same')[s.start - lo : s.stop - lo]
        env = np.sqrt(np.maximum(mean_power, 0.0) + np.float32(1e-24))
        gr = np.ones_like(env)
        above = env > threshold
        gr[above] = (threshold / env[above]) ** slope
        np.maximum(gr, floor_gain, out=gr)
        if n_ctrl:
            gain[s.start // decim : s.stop // decim] = gr.reshape(-1, decim).mean(axis=1)
        else:
            gain[:] = gr

    # The detector consumed the original signal, so it is safe to reuse x for output.
    if n_ctrl > 0:
        a_atk = math.exp(-1.0 / max(1e-3, atk_ms))
        a_rel = math.exp(-1.0 / max(1e-3, rel_ms))
        prev = 1.0
        for s in blocks(n_ctrl, check_cancel, size=8192):
            for i in range(s.start, s.stop):
                a = a_atk if gain[i] < prev else a_rel
                prev = a * prev + (1.0 - a) * gain[i]
                gain[i] = prev

    out = x if in_place else np.empty_like(x)
    k = max(1, int(0.002 * sr))
    box = np.ones(k, dtype=np.float32) / k
    min_db, active_sum, active_count, above_count = float('inf'), 0.0, 0, 0
    timeline = ReductionTimeline(n_samples, sr)
    for s in blocks(n_samples, check_cancel):
        # Zero-order hold, replicating the first and last gains at the file edges.
        indices = np.arange(s.start - k // 2, s.stop + (k - 1) // 2)
        if n_ctrl:
            indices //= decim
        expanded = gain[np.clip(indices, 0, len(gain) - 1)]
        smoothed = oaconvolve(expanded, box, mode='valid')
        np.maximum(smoothed, floor_gain, out=smoothed)
        out[s] = x[s] * smoothed[:, None]
        gr_db = 20.0 * np.log10(smoothed.astype(np.float64) + 1e-12)
        timeline.add(-gr_db, start_frame=s.start)
        min_db = min(min_db, float(gr_db.min()))
        active = gr_db[gr_db < -0.01]
        active_sum += float(active.sum())
        active_count += len(active)
        above_count += int(np.count_nonzero(gr_db < -0.5))

    stats = {
        'max_gr_db': round(-min_db, 2),
        'mean_gr_db_active': round(-active_sum / active_count, 3) if active_count else 0.0,
        'pct_time_above_0p5db_gr': round(100.0 * above_count / n_samples, 2),
        'timeline': timeline.result(),
    }
    if squeeze:
        out = out[:, 0]
    return out, stats
