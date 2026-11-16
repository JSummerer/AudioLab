"""Triangular Probability Density Function (TPDF) dither for audio bit-depth conversion.
Eliminates harmonic quantization distortion and grain without coloring the frequency response.

All generators work in fixed-size chunks so peak memory stays bounded on
multi-hour sets (a naive full-length float64 implementation transiently
allocates ~2x the signal per random array).
"""

import numpy as np

_CHUNK = 4 << 20  # 4M samples per chunk (~16 MB per temporary at float32 stereo)


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
