"""Zero-phase biquad filter implementations (RBJ Audio EQ Cookbook).
Filters are designed at g/2 dB so that forward-backward sosfiltfilt achieves
exact target gain with zero phase distortion.
"""

import math

import numpy as np
from scipy.signal import butter, sosfiltfilt


def peak_fq(fc: float, g_db: float, q: float = 1.0, sr: int = 48000) -> np.ndarray:
    """Peaking parametric equalizer SOS designed for zero-phase sosfiltfilt (g/2 design)."""
    A = 10.0 ** ((g_db / 2.0) / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    alpha = math.sin(w0) / (2.0 * q)
    b = np.array([1.0 + alpha * A, -2.0 * math.cos(w0), 1.0 - alpha * A])
    a = np.array([1.0 + alpha / A, -2.0 * math.cos(w0), 1.0 - alpha / A])
    return np.concatenate([b / a[0], a / a[0]]).reshape(1, 6)


def lowshelf_fq(fc: float, g_db: float, s: float = 1.0, sr: int = 48000) -> np.ndarray:
    """Low-shelf equalizer SOS designed for zero-phase sosfiltfilt (g/2 design)."""
    A = 10.0 ** ((g_db / 2.0) / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    alpha = math.sin(w0) / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / s - 1.0) + 2.0)
    two = 2.0 * math.sqrt(A) * alpha
    cw = math.cos(w0)
    b = np.array(
        [
            A * ((A + 1.0) - (A - 1.0) * cw + two),
            2.0 * A * ((A - 1.0) - (A + 1.0) * cw),
            A * ((A + 1.0) - (A - 1.0) * cw - two),
        ]
    )
    a = np.array(
        [(A + 1.0) + (A - 1.0) * cw + two, -2.0 * ((A - 1.0) + (A + 1.0) * cw), (A + 1.0) + (A - 1.0) * cw - two]
    )
    return np.concatenate([b / a[0], a / a[0]]).reshape(1, 6)


def highshelf_fq(fc: float, g_db: float, s: float = 1.0, sr: int = 48000) -> np.ndarray:
    """High-shelf equalizer SOS designed for zero-phase sosfiltfilt (g/2 design)."""
    A = 10.0 ** ((g_db / 2.0) / 40.0)
    w0 = 2.0 * math.pi * fc / sr
    alpha = math.sin(w0) / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / s - 1.0) + 2.0)
    two = 2.0 * math.sqrt(A) * alpha
    cw = math.cos(w0)
    b = np.array(
        [
            A * ((A + 1.0) + (A - 1.0) * cw + two),
            -2.0 * A * ((A - 1.0) + (A + 1.0) * cw),
            A * ((A + 1.0) + (A - 1.0) * cw - two),
        ]
    )
    a = np.array(
        [(A + 1.0) - (A - 1.0) * cw + two, 2.0 * ((A - 1.0) - (A + 1.0) * cw), (A + 1.0) - (A - 1.0) * cw - two]
    )
    return np.concatenate([b / a[0], a / a[0]]).reshape(1, 6)


def highpass_fq(fc: float, sr: int = 48000, order: int = 2) -> np.ndarray:
    """Butterworth highpass SOS for removing subsonic DC rumble."""
    # Order is halved because sosfiltfilt applies forward-backward (doubling effective order)
    sos_order = max(1, order // 2)
    return butter(sos_order, fc, btype='highpass', fs=sr, output='sos')


def filtfilt_stream(
    sos: np.ndarray, x: np.ndarray, block: int = 1 << 22, margin: int = 1 << 16, check_cancel=None
) -> np.ndarray:
    """Zero-phase filtering in bounded blocks with float32 storage.

    Full-length sosfiltfilt upcasts to float64 internally (~3 transient copies
    of the signal) - on a 2 h set that is ~16 GB per filter call. This helper
    runs the exact same zero-phase math inside (block + 2*margin) windows and
    writes float32 results into the output, keeping transients ~30 MB. The
    margin (1.4 s at 48 kHz) absorbs filtfilt edge transients; with the gentle
    audio filters used here (Q <= 1.2, fc >= 25 Hz) the residual seam error is
    far below dither level. `check_cancel` (optional) is called before every block.
    """
    x = np.asarray(x)
    n = x.shape[0]
    out = np.empty(x.shape, dtype=np.float32)
    for s0 in range(0, n, block):
        if check_cancel is not None:
            check_cancel()
        a = max(0, s0 - margin)
        b = min(n, s0 + block + margin)
        y = sosfiltfilt(sos, x[a:b], axis=0)
        stop = min(n, s0 + block)
        out[s0:stop] = y[s0 - a : stop - a]
    return out


def apply_sos_zero_phase(x: np.ndarray, sos: np.ndarray) -> np.ndarray:
    """Apply SOS filter along axis 0 with zero-phase distortion."""
    return sosfiltfilt(sos, x, axis=0)
