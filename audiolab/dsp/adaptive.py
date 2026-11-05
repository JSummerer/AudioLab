"""Conservative, stereo-linked control of temporary low-mid rises.

This is a relative detector, not a target spectrum or a diagnosis of bad sound.
Every 100 ms of the recording is compared with its own surrounding 20 seconds.
Only isolated rises lasting 0.3–3 seconds qualify: short attacks and sustained
tonal contrasts are deliberately left alone. Quiet/uncertain regions also pass.

The zero-phase band is subtracted with a smooth, bounded gain envelope. Where
that envelope is zero the input is untouched. Both channels use the same gain;
antiphase content counts by channel energy, never by summing L and R. A small
whole-band energy budget further limits cumulative changes. These are engineering
guardrails, not listening evidence or a claim to identify musical intent.

One full-length float32 band buffer; analysis and gain arrays run at 10 Hz.
"""

from typing import Any

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter

from .filters import filtfilt_stream

BAND_HZ = (180.0, 480.0)
HOP_S = 0.1
REFERENCE_S = 20.0
MARGIN_DB = 6.0
MAX_BAND_CHANGE_DB = 0.25
_MAX_EVENT_HOPS = 30
_MAX_DIP_HOPS = 2
_BLOCK = 1 << 20


def _powers(x: np.ndarray, hop: int, check) -> np.ndarray:
    """Channel-mean energy, including the final partial frame."""
    n = (len(x) + hop - 1) // hop
    out = np.empty(n, dtype=np.float64)
    step = max(hop, _BLOCK // hop * hop)
    for start in range(0, len(x), step):
        check()
        a = x[start : start + step].astype(np.float64)
        if a.ndim == 1:
            a = a[:, None]
        full = len(a) // hop
        if full:
            b = a[: full * hop].reshape(full, hop, a.shape[1])
            out[start // hop : start // hop + full] = np.mean(b * b, axis=(1, 2))
        if len(a) % hop:
            b = a[full * hop :]
            out[start // hop + full] = np.mean(b * b)
    return out


def _runs(mask: np.ndarray):
    edges = np.flatnonzero(np.diff(np.r_[False, mask, False].astype(np.int8)))
    return zip(edges[::2], edges[1::2], strict=True)


def _event_edge(share, audible, candidate, edge, step, threshold):
    """Find the last high window past a candidate edge, counting only bracketed dips."""
    last_high = edge
    for offset in range(1, _MAX_EVENT_HOPS + _MAX_DIP_HOPS + 1):
        i = edge + step * offset
        if not 0 <= i < len(share):
            break
        if candidate[i] or (audible[i] and share[i] > threshold):
            last_high = i
        elif abs(i - last_high) > _MAX_DIP_HOPS:
            break
    return last_high


def _reference(share: np.ndarray, audible: np.ndarray, check) -> np.ndarray:
    """Local audible median, evaluated each second with bounded working memory."""
    radius = round(REFERENCE_S / HOP_S / 2)
    anchors = np.unique(np.r_[np.arange(0, len(share), 10), len(share) - 1])
    values = np.empty(len(anchors))
    for j, i in enumerate(anchors):
        if j % 100 == 0:
            check()
        lo, hi = max(0, i - radius), min(len(share), i + radius + 1)
        local = share[lo:hi][audible[lo:hi]]
        # No confident neighbourhood: 0 dB (the whole signal) prevents a correction.
        values[j] = np.median(local) if len(local) >= 30 else 0.0
    return np.interp(np.arange(len(share)), anchors, values)


def control_lowmids(
    x: np.ndarray, sr: int = 48000, max_gr_db: float = 1.5, check_cancel=None
) -> tuple[np.ndarray, dict[str, Any]]:
    """Modify x in place; return it and a bounded, JSON-safe account of actual changes."""
    check = check_cancel or (lambda: None)
    check()
    cap = min(3.0, max(0.0, float(max_gr_db)))
    stats: dict[str, Any] = {
        'engaged': False,
        'band_hz': list(BAND_HZ),
        'analysis_hop_s': HOP_S,
        'reference_window_s': REFERENCE_S,
        'margin_db': MARGIN_DB,
        'cap_db': cap,
    }
    if cap <= 0 or len(x) < 4 * sr or sr <= BAND_HZ[1] * 3:
        return x, {**stats, 'skip_reason': 'off' if cap <= 0 else 'too little context'}
    hop = max(1, round(sr * HOP_S))
    full_power = _powers(x, hop, check)
    level = 10.0 * np.log10(full_power + 1e-24)
    audible = level > max(-65.0, float(np.percentile(level, 95)) - 35.0)
    stats['windows_total'] = len(level)
    if audible.sum() < 40:
        return x, {**stats, 'skip_reason': 'too little audible context'}
    band = filtfilt_stream(butter(2, BAND_HZ, btype='bandpass', fs=sr, output='sos'), x, check_cancel=check)
    band_power = _powers(band, hop, check)
    share = 10.0 * np.log10((band_power + 1e-24) / (full_power + 1e-24))
    reference = _reference(share, audible, check)
    excess = share - reference - MARGIN_DB
    # A relatively large rise of a negligible band is still negligible.
    candidate = audible & (share > -10.0) & (excess > 0)
    eligible = np.zeros(len(level), dtype=bool)
    sustained = np.zeros(len(level), dtype=bool)
    for j, (start, stop) in enumerate(_runs(candidate)):
        if j % 128 == 0:
            check()
        lo, hi = start, stop
        run_hops = stop - start
        if run_hops <= _MAX_EVENT_HOPS:
            # A moving median can catch up with a sustained rise. Pin the reference
            # and allow brief dips for the duration check only.
            threshold = reference[start] + MARGIN_DB
            lo = _event_edge(share, audible, candidate, start, -1, threshold)
            hi = _event_edge(share, audible, candidate, stop - 1, 1, threshold) + 1
        if 3 <= run_hops and hi - lo <= _MAX_EVENT_HOPS and hi < len(share):
            eligible[start:stop] = True
        elif hi - lo > _MAX_EVENT_HOPS:
            sustained[start:stop] = True
    stats.update(
        candidate_pct=round(100.0 * float(candidate.mean()), 2),
        sustained_pct=round(100.0 * float(sustained.mean()), 2),
    )
    gr = np.where(eligible, np.clip(excess * 0.5, 0.0, cap), 0.0)
    gr = uniform_filter1d(gr, 3, mode='nearest')  # 300 ms, no abrupt gain switches
    gr[~audible] = 0.0
    energy = float(band_power.sum())

    def band_change(gain):
        kept = float(np.sum(band_power * 10.0 ** (-gain / 10.0)))
        return float(10.0 * np.log10(max(1e-24, kept / energy))) if energy else 0.0

    if band_change(gr) < -MAX_BAND_CHANGE_DB:
        lo, hi = 0.0, 1.0
        for _ in range(12):
            check()
            mid = (lo + hi) / 2
            if band_change(gr * mid) < -MAX_BAND_CHANGE_DB:
                hi = mid
            else:
                lo = mid
        gr *= lo
    if gr.max(initial=0.0) < 0.1:
        return x, {**stats, 'skip_reason': 'no isolated low-mid rises needing control'}

    cut = 1.0 - 10.0 ** (-gr / 20.0)
    centres = np.minimum((np.arange(len(gr)) + 0.5) * hop, len(x) - 1)
    for start in range(0, len(x), _BLOCK):
        check()
        stop = min(len(x), start + _BLOCK)
        c = np.interp(np.arange(start, stop), centres, cut).astype(np.float32)
        x[start:stop] -= band[start:stop] * (c[:, None] if x.ndim > 1 else c)

    events = [
        {
            'start_s': round(float(a * HOP_S), 2),
            'end_s': round(float(min(len(x) / sr, b * HOP_S)), 2),
            'max_gr_db': round(float(gr[a:b].max()), 2),
        }
        for a, b in _runs(gr >= 0.1)
    ]
    events = sorted(sorted(events, key=lambda e: e['max_gr_db'], reverse=True)[:12], key=lambda e: e['start_s'])
    return x, {
        **stats,
        'engaged': True,
        'max_gr_db': round(float(gr.max()), 2),
        'active_pct': round(100.0 * float((gr >= 0.1).mean()), 2),
        'band_change_db': round(band_change(gr), 3),
        'events': events,
    }
