"""True-peak limiter with 4x oversampling, max-pooling peak detection,
min-hold plateau lookahead, and edge-replicated smoothing to avoid boundary dips.

The gain moves on a 6 ms hold + 5 ms smoothing scale: the prototype's 2 ms + 0.5 ms
recovered within ~2.5 ms of every over, an audio-rate gain wobble that spreads grit
around cymbals and S sounds (~12 dB more energy 150-1500 Hz around a 7 kHz tone
under limited kicks) for ~0.03 dB more loudness on dense material.

The 4x oversampling is the expensive part (~2.5 s per pass on 10 min of stereo),
so each limiter pass oversamples once: the same pass yields the needed gain and
the true peak it was measured from. The optional `check_cancel` callable is
called once per oversampled chunk.
"""

import math
from typing import Any

import numpy as np
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import resample_poly

from .scratch import ScratchBuffers, blocks
from .timeline import ReductionTimeline

TRUE_PEAK_CONTEXT = 8192


def true_peak_db(
    x: np.ndarray, os: int = 4, chunk: int = 1 << 20, check_cancel=None, *, span: tuple[int, int] | None = None
) -> float:
    """4x-oversampled true peak (dBTP), computed in chunks.

    Each chunk is oversampled with `ctx` samples of real context on both sides,
    but only the chunk's OWN samples are measured: the context edges are where
    resample_poly assumes zeros beyond the slice and rings, which used to read
    up to ~1 dB high and made the chain over-limit. `span` limits measurement to
    a region of x while the surrounding samples still provide context."""
    n = x.shape[0]
    start, stop = span or (0, n)
    peak = 0.0
    for s0 in range(start, stop, chunk):
        if check_cancel is not None:
            check_cancel()
        L = min(chunk, stop - s0)
        a = max(0, s0 - TRUE_PEAK_CONTEXT)
        b = min(n, s0 + L + TRUE_PEAK_CONTEXT)
        y = resample_poly(x[a:b], os, 1, axis=0)
        own = y[(s0 - a) * os : (s0 - a + L) * os]
        if own.size:
            peak = max(peak, float(np.abs(own).max()))
        del y, own
    return 20.0 * math.log10(peak + 1e-24)


def tp_need_gain(
    x: np.ndarray,
    ceiling_db: float,
    os: int = 4,
    chunk: int = 1 << 20,
    return_peak: bool = False,
    check_cancel=None,
    *,
    out: np.ndarray | None = None,
):
    """Per-sample gain keeping the 4x-oversampled peak under the ceiling.
    Max-pools the oversampled points per base sample so intersample peaks are
    caught; like true_peak_db, only each chunk's own samples are used.
    With return_peak: (gain, true peak dBTP of x) from the same pass."""
    n = x.shape[0]
    ctx = TRUE_PEAK_CONTEXT
    over = 10.0 ** (ceiling_db / 20.0)
    g = np.empty(n, dtype=np.float32) if out is None else out
    peak = 0.0
    for s0 in range(0, n, chunk):
        if check_cancel is not None:
            check_cancel()
        a = max(0, s0 - ctx)
        b = min(n, s0 + chunk + ctx)
        L = min(chunk, n - s0)
        y = resample_poly(x[a:b], os, 1, axis=0)
        own = y[(s0 - a) * os : (s0 - a + L) * os]
        mag = np.abs(own)
        if mag.ndim > 1:  # elementwise maxima: numpy's reduce over a short axis is ~10x slower
            mag = np.maximum.reduce([mag[:, c] for c in range(mag.shape[1])])
        mag = mag[: L * os]
        pooled = np.maximum.reduce([mag[i::os] for i in range(os)])
        if pooled.size:
            peak = max(peak, float(pooled.max()))
        need = np.minimum(1.0, over / (pooled + 1e-12))
        g[s0 : s0 + len(need)] = need
        del y, own, mag, pooled, need
    return (g, 20.0 * math.log10(peak + 1e-24)) if return_peak else g


def _filter_blocks(fn, g: np.ndarray, out: np.ndarray, margin: int, check_cancel) -> None:
    """Apply a short 1-D scipy.ndimage filter in blocks with `margin` samples of real
    context: identical result, but ndimage's float64 line buffer stays block-sized
    instead of two full-length copies (~2.8 GB on a 1 h programme)."""
    n = len(g)
    for s in blocks(n, check_cancel, size=1 << 22):
        a, b = max(0, s.start - margin), min(n, s.stop + margin)
        out[s] = fn(g[a:b])[s.start - a : s.stop - a]


def tp_limit(
    x: np.ndarray,
    ceiling_db: float = -1.25,
    max_extra_gr_db: float = 4.0,
    lookahead_ms: float = 6.0,
    smooth_ms: float = 5.0,
    sr: int = 48000,
    max_passes: int = 3,
    check_cancel=None,
    in_place: bool = False,
    *,
    scratch: ScratchBuffers | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Calibrated true-peak safety limiter:
    Calculates needed gain -> min-hold plateau (`lookahead_ms`, centred) -> edge-replicated
    smoothing (`smooth_ms`, never longer than the hold: a wider average would lift the gain
    at the peak itself). Repeats until under ceiling. `in_place=True` limits x itself (saves
    a full copy on long programmes when the caller no longer needs the input).
    `active_pct`: the share of samples turned down by more than 0.5 dB.
    """
    shape = (len(x),)
    gain_buffer = scratch.array(shape) if scratch is not None else None
    g, tp_now = tp_need_gain(x, ceiling_db, return_peak=True, check_cancel=check_cancel, out=gain_buffer)
    info = {'engaged': False, 'tp_before': round(tp_now, 3)}
    if tp_now <= ceiling_db:
        info['tp_after'] = info['tp_before']
        info['max_gr_db'] = 0.0
        info['active_pct'] = 0.0
        return x, info

    x_out = x if in_place else x.copy()
    floor = np.float32(10.0 ** (-max_extra_gr_db / 20.0))
    L = max(1, int(lookahead_ms / 1000.0 * sr))
    k = max(1, min(L, int(smooth_ms / 1000.0 * sr)))
    filtered = scratch.array(shape, keep=(g,)) if scratch is not None else np.empty_like(g)
    g_total = scratch.array(shape, keep=(g, filtered)) if scratch is not None else np.empty_like(g)
    for s in blocks(len(x), check_cancel):
        g_total[s] = 1.0

    for it in range(max_passes):
        if check_cancel is not None:
            check_cancel()
        info[f'pass{it + 1}_tp_before'] = round(tp_now, 3)
        if tp_now <= ceiling_db + 0.005:  # converged (a hair over is measurement noise, see below)
            break
        for s in blocks(len(g), check_cancel):
            np.maximum(g[s], floor, out=g[s])
        # Lookahead plateau to catch attack
        _filter_blocks(lambda b: minimum_filter1d(b, size=L, mode='nearest'), g, filtered, L, check_cancel)
        # Edge-replicated box smoothing (no zero-pad head/tail drops); a running mean,
        # so no full-length FFT temporaries on long programmes
        _filter_blocks(lambda b: uniform_filter1d(b, size=k, mode='nearest'), filtered, g, k, check_cancel)

        for s in blocks(len(g), check_cancel):
            gain = g[s]
            np.maximum(gain, floor, out=gain)
            x_out[s] *= gain[:, None] if x_out.ndim > 1 else gain
            g_total[s] *= gain

        # Gain for the next pass and the true peak after this one: one oversampling.
        _, tp_now = tp_need_gain(x_out, ceiling_db, return_peak=True, check_cancel=check_cancel, out=g)

    tp_final = tp_now
    info['engaged'] = True
    info['tp_after'] = round(tp_final, 3)
    # 0.01 dB tolerance: the 4x max-pool TP measurement itself is only exact to
    # ~0.01 dB; a floating-point hair above the ceiling is not a real miss.
    info['ceiling_met'] = tp_final <= ceiling_db + 0.01
    half_db = np.float32(10.0 ** (-0.5 / 20.0))
    active, lowest = 0, 1.0
    timeline = ReductionTimeline(len(x), sr)
    for s in blocks(len(g_total), check_cancel):
        gain = g_total[s]
        lowest = min(lowest, float(gain.min()))
        active += int(np.count_nonzero(gain < half_db))
        timeline.add(-20.0 * np.log10(np.maximum(gain, 1e-6)), start_frame=s.start)
    info['max_gr_db'] = round(-20.0 * math.log10(max(1e-6, lowest)), 2)
    info['active_pct'] = round(100.0 * active / max(1, len(g_total)), 2)
    info['timeline'] = timeline.result()
    if not info['ceiling_met']:
        info['warning'] = (
            f'True peak {tp_final:.2f} dBTP still above ceiling after {max_passes} limiter passes '
            f'(gain reduction capped at {max_extra_gr_db} dB).'
        )
    return x_out, info
