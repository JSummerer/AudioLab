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
from collections.abc import Iterable, Iterator

import numpy as np
from scipy.signal import lfilter

# BS.1770-4 K-weighting filter coefficients at 48 kHz
KB1 = [1.53512485958697, -2.69169618940638, 1.19839281085285]
KA1 = [1.0, -1.69065929318241, 0.73248077421585]
KB2 = [1.0, -2.0, 1.0]
KA2 = [1.0, -1.99004745483398, 0.99007225036621]

_SUPPORTED_SR = 48000
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


def _k_filter_stream(chunks: Iterable[np.ndarray], n_ch: int, check_cancel=None) -> Iterator[np.ndarray]:
    """Yield K-weighted samples in bounded chunks, carrying filter state across
    chunk boundaries so the concatenated stream is identical to a full lfilter
    with zero initial state (matching the historical whole-signal behavior).
    """
    o1 = max(len(KA1), len(KB1)) - 1
    o2 = max(len(KA2), len(KB2)) - 1
    zi1 = np.zeros((o1, n_ch))
    zi2 = np.zeros((o2, n_ch))
    for blk in chunks:
        if check_cancel is not None:
            check_cancel()
        if blk.ndim == 1:
            blk = blk[:, None]
        y, zi1 = lfilter(KB1, KA1, blk, axis=0, zi=zi1)
        y, zi2 = lfilter(KB2, KA2, y, axis=0, zi=zi2)
        yield y


def _k_blocks(
    x: np.ndarray, block_s: float, hop_s: float, sr: int = 48000, check_cancel=None, gain: np.float32 | None = None
) -> list[float]:
    """Apply K-weighting and compute momentary/short-term block loudness."""
    _require_48k(sr)
    b_len = int(block_s * sr)
    h_len = int(hop_s * sr)
    n = x.shape[0]
    if n == 0:
        return []
    n_ch = x.shape[1] if x.ndim > 1 else 1
    if n < b_len:
        return [_block_loudness(next(_k_filter_stream([x if gain is None else x * gain], n_ch, check_cancel)))]

    chunk = max(h_len * 100, b_len + h_len)
    blocks: list[float] = []
    buf = np.empty((0, n_ch), dtype=np.float64)
    buf_start = 0  # global sample index of buf[0]

    chunks = (x[start : start + chunk] for start in range(0, n, chunk))
    if gain is not None:
        chunks = (block * gain for block in chunks)
    for y in _k_filter_stream(chunks, n_ch, check_cancel):
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


def _gated_integrated(blocks: np.ndarray) -> float:
    above_abs = blocks[blocks > _ABS_GATE]
    if above_abs.size == 0:
        return _ABS_GATE
    rel_gate = _lufs(float(_energy(above_abs).mean())) - 10.0
    gated = above_abs[above_abs > rel_gate]
    if gated.size == 0:
        gated = above_abs
    return float(_lufs(float(_energy(gated).mean())))


def integrated_lufs(x: np.ndarray, sr: int = 48000, check_cancel=None) -> float:
    """BS.1770-4 integrated loudness: 400 ms blocks (75 % overlap), absolute gate
    at -70 LUFS, relative gate 10 LU below the energy-mean of the absolutely
    gated blocks, result = energy-mean of the blocks passing both gates."""
    return _gated_integrated(np.array(_k_blocks(x, 0.4, 0.1, sr, check_cancel)))


def programme_loudness(x: np.ndarray, sr: int = 48000, check_cancel=None) -> float:
    """integrated_lufs, also for whisper-quiet programmes that sit entirely below
    the -70 LUFS absolute gate (BS.1770 has no value there; integrated_lufs then
    reports -70): measured with blocks normalised to -6 dBFS peak and shifted back,
    so a gain decision sees the real level instead of the gate. Silence stays -70."""
    i = integrated_lufs(x, sr=sr, check_cancel=check_cancel)
    if i > _ABS_GATE:
        return i
    peak = 0.0
    for start in range(0, len(x), 1 << 20):
        if check_cancel is not None:
            check_cancel()
        block = x[start : start + (1 << 20)]
        peak = max(peak, float(block.max()), -float(block.min()))
    if peak < 1e-9:
        return i
    k = 0.5 / peak
    scaled = _k_blocks(x, 0.4, 0.1, sr, check_cancel, gain=np.float32(k))
    return _gated_integrated(np.array(scaled)) - 20.0 * math.log10(k)


def _gated_range(blocks: np.ndarray) -> float:
    blocks = blocks[blocks > _ABS_GATE]
    if blocks.size == 0:
        return 0.0
    rel_gate = _lufs(float(_energy(blocks).mean())) - 20.0
    g = blocks[blocks > rel_gate]
    if g.size == 0:
        return 0.0
    return float(np.percentile(g, 95) - np.percentile(g, 10))


def lra(x: np.ndarray, sr: int = 48000, check_cancel=None) -> float:
    """EBU Tech 3342 Loudness Range: 3 s short-term blocks (1 s hop), absolute
    gate -70 LUFS, relative gate 20 LU below the energy-mean of those blocks,
    LRA = 95th - 10th percentile of the gated short-term loudness values."""
    return _gated_range(np.array(_k_blocks(x, 3.0, 1.0, sr, check_cancel)))


def stream_programme_levels(
    chunks: Iterable[np.ndarray],
    sr: int = 48000,
    channels: int = 2,
    check_cancel=None,
    bins_out: list[np.ndarray] | None = None,
) -> tuple[float, float]:
    """Programme loudness and LRA from one continuous stream of sample chunks.

    K-weight each sample once, carrying filter state, and retain just ten scalar
    energy bins per second. Both standard measurements use 100 ms-aligned windows.
    `bins_out` (optional) receives those 100 ms energy bins for `programme_density`.
    """
    _require_48k(sr)
    bin_frames = sr // 10
    bin_parts = []
    tail = np.empty(0, dtype=np.float64)
    sample_peak = 0.0

    def track_peak():
        nonlocal sample_peak
        for chunk in chunks:
            if chunk.size:
                sample_peak = max(sample_peak, float(chunk.max()), -float(chunk.min()))
                yield chunk

    for weighted in _k_filter_stream(track_peak(), channels, check_cancel):
        power = np.einsum('ij,ij->i', weighted, weighted)
        if tail.size:
            power = np.concatenate((tail, power))
        end = len(power) // bin_frames * bin_frames
        bin_parts.append(power[:end].reshape(-1, bin_frames).mean(axis=1))
        tail = power[end:].copy()

    if not bin_parts:
        return _ABS_GATE, 0.0
    bins = np.concatenate(bin_parts)
    if bins_out is not None:
        bins_out.append(bins)

    def windows(width: int, hop: int) -> np.ndarray:
        if bins.size < width:
            # The array meter uses one partial window when there isn't a full one.
            frames = bins.size * bin_frames + tail.size
            return np.array([(bins.sum() * bin_frames + tail.sum()) / frames])
        return np.lib.stride_tricks.sliding_window_view(bins, width)[::hop].mean(axis=1)

    integrated = windows(4, 1)
    loudness = _gated_integrated(-0.691 + 10.0 * np.log10(integrated + 1e-24))
    if loudness <= _ABS_GATE and sample_peak >= 1e-9:
        # Scaling these energies is equivalent to re-filtering a peak-normalized copy.
        gain = 0.5 / sample_peak
        normalized = _gated_integrated(-0.691 + 10.0 * np.log10(integrated * gain**2 + 1e-24))
        loudness = normalized - 20.0 * math.log10(gain)
    return loudness, _gated_range(-0.691 + 10.0 * np.log10(windows(30, 10) + 1e-24))


DENSITY_WINDOW_S = 30  # peak-to-loudness windows (15 s hop)
DENSITY_SECTION_S = 180  # loudness-range sections


def programme_density(bins: np.ndarray, peaks: np.ndarray) -> dict[str, float] | None:
    """How dense a long programme is where it plays, rather than across its whole length.

    The whole-file LRA of a DJ set also counts slow level drift between tracks and long
    breakdowns, which reads as concert dynamics. This returns the median sample-peak-to-loudness
    ratio of 30 s windows (quiet windows below -40 LUFS skipped) and the median loudness range of
    3-minute sections (EBU short-term blocks, each section gated on its own). `bins`: K-weighted
    mean power per 100 ms (`stream_programme_levels`); `peaks`: sample peak per 100 ms, same grid.
    None for less than two sections of material."""
    n = min(len(bins), len(peaks))
    if n < 2 * DENSITY_SECTION_S * 10:
        return None
    bins = np.asarray(bins[:n], dtype=np.float64)
    peaks = np.asarray(peaks[:n], dtype=np.float64)
    width = DENSITY_WINDOW_S * 10
    view = np.lib.stride_tricks.sliding_window_view
    loud = -0.691 + 10.0 * np.log10(view(bins, width)[:: width // 2].mean(axis=1) + 1e-24)
    peak_db = 20.0 * np.log10(np.maximum(view(peaks, width)[:: width // 2].max(axis=1), 1e-12))
    plr = (peak_db - loud)[loud > -40.0]
    short = -0.691 + 10.0 * np.log10(view(bins, 30)[::10].mean(axis=1) + 1e-24)
    ranges = []
    for start in range(0, len(short) - DENSITY_SECTION_S + 1, DENSITY_SECTION_S):
        blocks = short[start : start + DENSITY_SECTION_S]
        blocks = blocks[blocks > _ABS_GATE]
        if blocks.size:
            blocks = blocks[blocks > _lufs(float(_energy(blocks).mean())) - 20.0]
        if blocks.size >= DENSITY_SECTION_S // 3:  # a section that is mostly silence says nothing
            ranges.append(float(np.percentile(blocks, 95) - np.percentile(blocks, 10)))
    if plr.size < 4 or len(ranges) < 2:
        return None
    return {
        'peak_to_loudness_db': round(float(np.median(plr)), 1),
        'section_lra': round(float(np.median(ranges)), 1),
        'sections': len(ranges),
    }
