"""Triangular Probability Density Function (TPDF) dither for audio bit-depth conversion.
Eliminates harmonic quantization distortion and grain without coloring the frequency response.

All generators work in fixed-size chunks so peak memory stays bounded on
multi-hour sets (a naive full-length float64 implementation transiently
allocates ~2x the signal per random array).
"""

import numpy as np

_CHUNK = 4 << 20  # 4M samples per chunk (~16 MB per temporary at float32 stereo)


def quantize_to_16bit(x: np.ndarray, dither: bool = True, seed: int | None = 42) -> np.ndarray:
    """Quantize floating-point audio in [-1.0, 1.0] to 16-bit integer PCM with TPDF dither.

    Processes in chunks so transient allocations are bounded regardless of
    programme length (2 h stereo previously peaked ~18 GB here).
    """
    n = x.shape[0]
    out = np.empty(n, dtype=np.int16) if x.ndim == 1 else np.empty((n, x.shape[1]), dtype=np.int16)
    rng = np.random.default_rng(seed)
    for s0 in range(0, n, _CHUNK):
        blk = x[s0 : s0 + _CHUNK]
        q = blk * 32767.0
        if dither:
            r1 = rng.random(blk.shape, dtype=np.float64)
            r2 = rng.random(blk.shape, dtype=np.float64)
            q = q + (r1 - r2)
        out[s0 : s0 + blk.shape[0]] = np.round(np.clip(q, -32768.0, 32767.0)).astype(np.int16)
    return out


def add_24bit_tpdf(x: np.ndarray, seed: int | None = 42, check_cancel=None) -> np.ndarray:
    """Add subtle TPDF dither at the 24-bit LSB level before writing to PCM_24.

    Chunked and in-place-equivalent: returns the input array modified with
    dither, keeping transients bounded on long programmes. `check_cancel`
    (optional) is called before every chunk.
    """
    rng = np.random.default_rng(seed)
    lsb = 1.0 / (2.0**23)
    n = x.shape[0]
    for s0 in range(0, n, _CHUNK):
        if check_cancel is not None:
            check_cancel()
        blk = x[s0 : s0 + _CHUNK]
        r1 = rng.random(blk.shape, dtype=np.float32)
        r2 = rng.random(blk.shape, dtype=np.float32)
        blk += ((r1.astype(np.float64) + r2.astype(np.float64) - 1.0) * lsb).astype(np.float32)
        np.clip(blk, -1.0, 1.0, out=blk)
    return x
