"""Loudness measurement: BS.1770-4 integrated loudness (LUFS) and EBU Tech 3342
Loudness Range (LRA).

Gating averages block *energies* (mean square), not dB values, exactly as the
standards (and ffmpeg's libebur128) do. The prototype averaged dB values, which
reads low on dynamic material (Jensen's inequality) - fixed in 2.0.

The in-process K-weighting coefficients are defined for 48 kHz only (guarded).
K-filtering runs in bounded chunks with carried filter state so measuring a
2 h set does not allocate full-length float64 copies. The optional `check_cancel`
callable is called once per chunk (cancellation of long measurements).
"""

import math

import numpy as np
from scipy.signal import lfilter

# BS.1770-4 K-weighting filter coefficients at 48 kHz
KB1 = [1.53512485958697, -2.69169618940638, 1.19839281085285]
KA1 = [1.0, -1.69065929318241, 0.73248077421585]
KB2 = [1.0, -2.0, 1.0]
KA2 = [1.0, -1.99004745483398, 0.99007225036621]

_SUPPORTED_SR = 48000
_CHUNK_S = 10.0  # K-filter chunk size; transients bounded to this span
_ABS_GATE = -70.0


def _require_48k(sr: int):
    if sr != _SUPPORTED_SR:
        raise ValueError(
            f'BS.1770 K-weighting coefficients are defined for {_SUPPORTED_SR} Hz only '
            f'(got {sr} Hz). The ingest stage resamples everything to 48 kHz; '
            f'measure at 48 kHz or resample first.'
        )


def _block_loudness(xk_slice: np.ndarray) -> float:
    """Mean-square channel sum converted to LUFS with BS.1770 constant -0.691."""
    ms = float((xk_slice * xk_slice).mean(axis=0).sum())
    return -0.691 + 10.0 * math.log10(ms + 1e-24)


def _k_filter_stream(x: np.ndarray, chunk: int, check_cancel=None) -> np.ndarray:
    """Yield K-weighted samples in bounded chunks, carrying filter state across
    chunk boundaries so the concatenated stream is identical to a full lfilter
    with zero initial state (matching the historical whole-signal behavior).
    """
    n_ch = x.shape[1] if x.ndim > 1 else 1
    o1 = max(len(KA1), len(KB1)) - 1
    o2 = max(len(KA2), len(KB2)) - 1
    zi1 = np.zeros((o1, n_ch))
    zi2 = np.zeros((o2, n_ch))
    for s0 in range(0, x.shape[0], chunk):
        if check_cancel is not None:
            check_cancel()
        blk = x[s0 : s0 + chunk]
        if blk.ndim == 1:
            blk = blk[:, None]
        y, zi1 = lfilter(KB1, KA1, blk, axis=0, zi=zi1)
        y, zi2 = lfilter(KB2, KA2, y, axis=0, zi=zi2)
        yield y


def _k_blocks(x: np.ndarray, block_s: float, hop_s: float, sr: int = 48000, check_cancel=None) -> list[float]:
    """Apply K-weighting and compute momentary/short-term block loudness."""
    _require_48k(sr)
    b_len = int(block_s * sr)
    h_len = int(hop_s * sr)
    n = x.shape[0]
    if n == 0:
        return []
    if n < b_len:
        return [_block_loudness(next(_k_filter_stream(x, int(_CHUNK_S * sr))))]

    chunk = max(h_len * 100, b_len + h_len)
    blocks: list[float] = []
    buf = np.empty((0, x.shape[1] if x.ndim > 1 else 1), dtype=np.float64)
    buf_start = 0  # global sample index of buf[0]

    for y in _k_filter_stream(x, chunk, check_cancel):
        buf = np.concatenate([buf, y], axis=0)
        # Emit every block whose last sample is available and within the signal
        while True:
            next_start = len(blocks) * h_len  # blocks are emitted in order
            if next_start + b_len > min(buf_start + len(buf), n):
                break
            blocks.append(_block_loudness(buf[next_start - buf_start : next_start - buf_start + b_len]))
            # Trim buffer: keep only what future blocks still need
            keep_from = next_start + h_len - buf_start
            if keep_from > 0:
                buf = buf[keep_from:]
                buf_start += keep_from
    return blocks


def _energy(lufs_values: np.ndarray) -> np.ndarray:
    """Block loudness (LUFS) back to mean-square energy."""
    return 10.0 ** ((lufs_values + 0.691) / 10.0)


def _lufs(mean_energy: float) -> float:
    return -0.691 + 10.0 * math.log10(mean_energy + 1e-24)


def integrated_lufs(x: np.ndarray, sr: int = 48000, check_cancel=None) -> float:
    """BS.1770-4 integrated loudness: 400 ms blocks (75 % overlap), absolute gate
    at -70 LUFS, relative gate 10 LU below the energy-mean of the absolutely
    gated blocks, result = energy-mean of the blocks passing both gates."""
    blocks = np.array(_k_blocks(x, block_s=0.4, hop_s=0.1, sr=sr, check_cancel=check_cancel))
    above_abs = blocks[blocks > _ABS_GATE]
    if above_abs.size == 0:
        return _ABS_GATE
    rel_gate = _lufs(float(_energy(above_abs).mean())) - 10.0
    gated = above_abs[above_abs > rel_gate]
    if gated.size == 0:
        gated = above_abs
    return float(_lufs(float(_energy(gated).mean())))


def programme_loudness(x: np.ndarray, sr: int = 48000, check_cancel=None) -> float:
    """integrated_lufs, also for whisper-quiet programmes that sit entirely below
    the -70 LUFS absolute gate (BS.1770 has no value there; integrated_lufs then
    reports -70): measured on a copy normalised to -6 dBFS peak and shifted back,
    so a gain decision sees the real level instead of the gate. Silence stays -70."""
    i = integrated_lufs(x, sr=sr, check_cancel=check_cancel)
    peak = max(float(x.max()), -float(x.min())) if x.size else 0.0  # no full-length abs() copy
    if i > _ABS_GATE or peak < 1e-9:
        return i
    k = 0.5 / peak
    return integrated_lufs(x * np.float32(k), sr=sr, check_cancel=check_cancel) - 20.0 * math.log10(k)


def lra(x: np.ndarray, sr: int = 48000, check_cancel=None) -> float:
    """EBU Tech 3342 Loudness Range: 3 s short-term blocks (1 s hop), absolute
    gate -70 LUFS, relative gate 20 LU below the energy-mean of those blocks,
    LRA = 95th - 10th percentile of the gated short-term loudness values."""
    blocks = np.array([v for v in _k_blocks(x, 3.0, 1.0, sr, check_cancel) if v > _ABS_GATE])
    if blocks.size == 0:
        return 0.0
    rel_gate = _lufs(float(_energy(blocks).mean())) - 20.0
    g = blocks[blocks > rel_gate]
    if g.size == 0:
        return 0.0
    return float(np.percentile(g, 95) - np.percentile(g, 10))
