"""De-esser: softens sharp S sounds and the hardest cymbal hits without dulling the track.

Split band: a zero-phase band-pass takes 4.5-9.5 kHz (where S sounds sit, whatever the
voice); the output is x - band * (1 - g), so it is exactly the input wherever g = 1.

Detection runs at a 1 kHz control rate, stereo-linked: the band's share of the whole
signal (5 ms RMS) against this track's own typical share (its median over the audible
time, which stays below the S sounds even when they fill a third of a vocal). Only moments
where the band jumps clearly above that are turned down, so a bright track keeps its
brightness and only its spikes are softened. Strong (more than 4 dB allowed) also starts
acting a little earlier. The gain is held and smoothed symmetrically (zero-phase,
like every other stage here): it starts a few milliseconds before a spike and eases out after.

Safety budget: the threshold is calibrated by measurement (synthetic S bursts, a few songs,
DJ sets and a concert), not by ear, so the result is bounded whatever it finds. No spike
loses more than `max_gr_db` (3-4 dB, well short of a lisp). On a mix the band's overall
energy may drop by at most MIX_TONE_DB per 3 dB allowed (Strong: twice), so the track's tone
stays put; if the threshold would take more, it is raised until only the biggest spikes are
left. A voice alone (speech, Best's separated vocals) has no cymbals to dull: there the band
is mostly S sounds, so only the per-spike cap applies.

Memory: one band copy of the input; everything else is control-rate or block-sized.
`check_cancel` (optional) is called between blocks.
"""

from typing import Any

import numpy as np
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import butter

from .filters import filtfilt_stream

BAND_HZ = (4500.0, 9500.0)
_CTRL_SR = 1000  # control rate (Hz)
_BLOCK = 1 << 20
BASE_PCT = 50  # the track's typical band share
MARGIN_DB, STRONG_MARGIN_DB = 10.0, 7.0  # a spike = the band's share this far above it
RATIO = 3.0  # above the threshold, 2/3 of the excess is taken off
HOLD_MS, SMOOTH_MS = 12, 12
QUIET_DB = 35.0  # moments this far below the loud parts are not judged (pauses, fades)
MIX_TONE_DB = 0.3  # most the band's overall energy may drop on a mix, per 3 dB allowed


def _ctrl_power(a: np.ndarray, decim: int, n_ctrl: int, check) -> np.ndarray:
    """Mean power per control period, stereo-linked, computed in blocks."""
    out = np.empty(n_ctrl, dtype=np.float64)
    step = (_BLOCK // decim) * decim
    for s0 in range(0, n_ctrl * decim, step):
        check()
        blk = a[s0 : min(s0 + step, n_ctrl * decim)].astype(np.float64)
        blk = blk.reshape(-1, decim, blk.shape[1]) if blk.ndim > 1 else blk.reshape(-1, decim, 1)
        out[s0 // decim : s0 // decim + len(blk)] = (blk * blk).mean(axis=(1, 2))
    return out


def deess(
    x: np.ndarray, sr: int = 48000, max_gr_db: float = 3.0, mix: bool = True, check_cancel=None
) -> tuple[np.ndarray, dict[str, Any]]:
    """Soften S-sound spikes in place (x is modified and returned); `mix=False` for a voice alone.
    Stats: engaged, max_gr_db, active_pct (share of the time turned down by more than 0.5 dB),
    band_change_db (the band's overall energy change), budget_db (how far the safety budget
    raised the threshold) - or a skip_reason."""
    check = check_cancel or (lambda: None)
    decim = max(1, sr // _CTRL_SR)
    n_ctrl = x.shape[0] // decim
    if max_gr_db <= 0 or n_ctrl < 200 or BAND_HZ[1] >= 0.45 * sr:
        return x, {'engaged': False, 'skip_reason': 'too short' if n_ctrl < 200 else 'off'}

    sos = butter(2, BAND_HZ, btype='bandpass', fs=sr, output='sos')
    band = filtfilt_stream(sos, x, check_cancel=check_cancel)
    k = max(1, int(0.005 * _CTRL_SR))
    # Running means can dip a hair below zero on digital silence.
    pb = np.maximum(uniform_filter1d(_ctrl_power(band, decim, n_ctrl, check), k, mode='nearest'), 0.0)
    pf = np.maximum(uniform_filter1d(_ctrl_power(x, decim, n_ctrl, check), k, mode='nearest'), 0.0)
    level = 10.0 * np.log10(pf + 1e-20)
    audible = level > np.percentile(level, 95) - QUIET_DB
    if audible.sum() < 200:
        return x, {'engaged': False, 'skip_reason': 'too little sound to judge'}
    share = 10.0 * np.log10((pb + 1e-20) / (pf + 1e-20))
    e_band = float(pb[audible].sum())

    def shape(threshold: float) -> np.ndarray:
        g = np.clip((share - threshold) * (1.0 - 1.0 / RATIO), 0.0, max_gr_db)
        g[~audible] = 0.0
        return uniform_filter1d(maximum_filter1d(g, HOLD_MS, mode='nearest'), SMOOTH_MS, mode='nearest')

    def band_change_db(g: np.ndarray) -> float:
        kept = float((pb * 10.0 ** (-g / 10.0))[audible].sum())
        return 10.0 * np.log10(kept / e_band) if e_band > 0 else 0.0

    def within(g: np.ndarray) -> bool:
        return not mix or band_change_db(g) >= -MIX_TONE_DB * max_gr_db / 3.0

    margin = STRONG_MARGIN_DB if max_gr_db > 4.0 else MARGIN_DB
    base = float(np.percentile(share[audible], BASE_PCT)) + margin
    threshold, gr = base, shape(base)
    if not within(gr):  # raise the threshold until only the biggest spikes are left
        lo, hi = base, float(share[audible].max()) + 1.0  # at hi nothing is touched: always within
        for _ in range(12):
            check()
            mid = 0.5 * (lo + hi)
            lo, hi = (lo, mid) if within(shape(mid)) else (mid, hi)
        threshold, gr = hi, shape(hi)
    if not (gr > 0.1).any():
        return x, {'engaged': False, 'skip_reason': 'no sharp S sounds found'}

    cut = 1.0 - 10.0 ** (-gr / 20.0)  # share of the band to take away, per control period
    centres = (np.arange(n_ctrl) + 0.5) * decim
    for s0 in range(0, x.shape[0], _BLOCK):
        check()
        s1 = min(x.shape[0], s0 + _BLOCK)
        c = np.interp(np.arange(s0, s1), centres, cut).astype(np.float32)
        x[s0:s1] -= band[s0:s1] * (c[:, None] if x.ndim > 1 else c)
    del band
    return x, {
        'engaged': True,
        'max_gr_db': round(float(gr.max()), 2),
        'active_pct': round(100.0 * float((gr > 0.5).mean()), 2),
        'band_change_db': round(band_change_db(gr), 2),
        'threshold_db': round(threshold, 1),
        'budget_db': round(threshold - base, 1),
    }
