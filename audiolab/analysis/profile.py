"""Audio profile: the measurements every other decision builds on.

Fast by design: long files are measured on a few spread-out excerpts
(see `load_samples`), short files completely. Everything here is plain
numpy/scipy (no ffmpeg), 48 kHz in, float32.

Reported (see `profile`): loudness (BS.1770 integrated LUFS, true peak, LRA),
flat-top clipping, real audio bandwidth (lossy-cutoff detection), octave band
balance and low-mid "mud", programme level percentiles, stereo width / mono / antiphase bass,
DC offset, a musical key estimate, the club-dynamics style used by the `club`
recipe, and novice-friendly alerts. Silent material gets one plain alert
instead of meaningless numbers.
"""

import math
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

from ..dsp.limiter import true_peak_db
from ..dsp.loudness import lra, programme_loudness
from .key import estimate_key

OCTAVE_CENTERS = [31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0]
_WELCH_NFFT = 8192
_FALLBACK_SECONDS = 180  # contiguous material anchored at a detected audible block
SILENCE_DBFS = -80.0  # sample peak below this = silent (nothing a listener could hear)
DC_ALERT = 0.01  # |mean| above -40 dBFS = a real DC offset


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
def _slice_plan(duration_s: float) -> tuple[list[float], float, str]:
    """(relative positions, slice length s, basis text) for a programme length."""
    if duration_s <= 180:
        return [], 0.0, 'whole file'
    if duration_s <= 20 * 60:
        return [0.15, 0.50, 0.85], 30.0, '3 × 30 s excerpts at 15 / 50 / 85 % of the file'
    pos = [0.10, 0.30, 0.50, 0.70, 0.90]
    return pos, 24.0, '5 × 24 s excerpts spread across the file'


def load_samples(path: Path, span: tuple[int, int] | None = None) -> tuple[np.ndarray, int, str]:
    """Read the analysis material: the whole file when short, else spread excerpts.
    `span` (start, stop frames) limits both to that part of the file.

    Returns (x (N, 2) float32, sr, basis text)."""
    info = sf.info(str(path))
    sr = info.samplerate
    first, last = span or (0, info.frames)
    frames = last - first
    positions, slice_s, basis = _slice_plan(frames / sr)
    if not positions:
        x, _ = sf.read(str(path), start=first, stop=last, dtype='float32', always_2d=True)
    else:
        n = int(slice_s * sr)
        parts = []
        for p in positions:
            start = first + min(max(0, int(p * frames) - n // 2), max(0, frames - n))
            part, _ = sf.read(str(path), start=start, stop=start + n, dtype='float32', always_2d=True)
            parts.append(part)
        x = np.concatenate(parts, axis=0)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    elif x.shape[1] > 2:
        x = x[:, :2]
    if not np.isfinite(x).all():  # broken float files: NaN/inf samples count as silence
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return x, sr, basis


# ---------------------------------------------------------------------------
# Spectrum helpers
# ---------------------------------------------------------------------------
def average_psd(
    x: np.ndarray, sr: int, nfft: int = _WELCH_NFFT, max_frames: int = 400
) -> tuple[np.ndarray, np.ndarray]:
    """Welch-averaged power spectrum of the mid signal (mean over up to max_frames windows)."""
    mono = x.mean(axis=1) if x.ndim > 1 else x
    nfft = min(nfft, 1 << int(math.log2(max(256, len(mono)))))
    hop = nfft // 2
    starts = np.arange(0, max(1, len(mono) - nfft + 1), hop)
    if len(starts) > max_frames:
        starts = starts[np.linspace(0, len(starts) - 1, max_frames).astype(int)]
    win = np.hanning(nfft).astype(np.float32)
    acc = np.zeros(nfft // 2 + 1, dtype=np.float64)
    for s0 in starts:
        seg = mono[s0 : s0 + nfft]
        if len(seg) < nfft:
            seg = np.pad(seg, (0, nfft - len(seg)))
        acc += np.abs(np.fft.rfft(seg * win)) ** 2
    psd = acc / max(1, len(starts)) / (nfft * float((win**2).sum()))
    return np.fft.rfftfreq(nfft, 1.0 / sr), psd


def octave_bands_db(freqs: np.ndarray, psd: np.ndarray) -> dict[str, float]:
    """Energy per true octave band (fc/√2 … fc·√2), dB."""
    out = {}
    for fc in OCTAVE_CENTERS:
        lo, hi = fc / math.sqrt(2.0), fc * math.sqrt(2.0)
        idx = (freqs >= lo) & (freqs < hi)
        e = float(psd[idx].sum()) if idx.any() else 0.0
        out[str(int(fc)) if fc >= 100 else str(fc)] = round(10.0 * math.log10(e + 1e-24), 2)
    return out


def detect_bandwidth(x: np.ndarray, sr: int = 48000, source_rate: int | None = None) -> dict[str, Any]:
    """Where does the audio content actually stop?

    Looks for a brick-wall "cliff" above 4 kHz in the averaged spectrum (what
    lossy encoders leave behind) and otherwise reports the natural roll-off.
    Wording never claims provenance: a brick wall comes from lossy compression
    or a low sample rate somewhere along the chain — we cannot tell which.
    Exceptions: when the source file's own rate (`source_rate`) caps the range,
    that is stated plainly; and a spectrum that is just a few tones (a test
    tone, a hum) has no range to judge (`judged: False`).
    """
    freqs, psd = average_psd(x, sr)
    return _bandwidth_from_psd(freqs, psd, sr, source_rate)


def _bandwidth_from_psd(freqs: np.ndarray, psd: np.ndarray, sr: int, source_rate: int | None = None) -> dict[str, Any]:
    """Bandwidth verdict from an existing Welch spectrum (same rules as detect_bandwidth)."""
    db = 10.0 * np.log10(psd + 1e-20)
    band = (freqs > 100.0) & (freqs < min(20000.0, sr / 2))
    if not band.any() or float((db[band] > db.max() - 50.0).mean()) < 0.01:
        return {
            'cutoff_hz': None,
            'is_lossy_cutoff': False,
            'cliff_db': 0.0,
            'full_range': False,
            'judged': False,
            'verdict': 'Only a few single tones — too little sound to judge the frequency range.',
        }
    df = freqs[1] - freqs[0]
    k = max(1, int(round(150.0 / df)))  # ~150 Hz smoothing
    # Edge-padded smoothing (zero padding would inject '0 dB' at the spectrum edges)
    smooth = np.convolve(np.pad(db, (k // 2, k - 1 - k // 2), mode='edge'), np.ones(k) / k, mode='valid')
    ref_idx = (freqs >= 1000) & (freqs <= 4000)
    ref = float(smooth[ref_idx].mean()) if ref_idx.any() else float(smooth.max())

    # Cliff search: drop between the 600 Hz below and the 600 Hz above each bin.
    w = max(1, int(round(600.0 / df)))
    best_drop, best_f = 0.0, None
    nyq_guard = 0.475 * sr
    for i in range(int(4000 / df), len(freqs) - w):
        if freqs[i] > nyq_guard:
            break
        below = smooth[i - w : i].mean()
        above = smooth[i : i + w].mean()
        drop = below - above
        # Must be a real cliff in content that is clearly audible relative to the mids.
        if drop > best_drop and below > ref - 60.0:
            best_drop, best_f = drop, float(freqs[i])

    if best_f is not None and best_drop >= 18.0:
        cutoff = best_f
    else:
        # Natural roll-off: highest frequency still within 60 dB of the mid level.
        audible = np.nonzero(smooth > ref - 60.0)[0]
        cutoff = float(freqs[audible[-1]]) if len(audible) else float(sr / 2)
        best_drop = 0.0

    is_lossy = bool(best_drop >= 18.0 and cutoff <= 19500.0)
    full = cutoff >= 19500.0
    khz = cutoff / 1000.0
    if source_rate and source_rate < 39000 and cutoff >= 0.9 * source_rate / 2:
        # The cliff is the file's own sample rate (the analysis copy's resampler puts it a bit higher).
        cutoff, is_lossy, full = source_rate / 2.0, True, False
        verdict = (
            f'The file is recorded at {source_rate / 1000:g} kHz, so it cannot hold anything above '
            f'{cutoff / 1000:.1f} kHz — the very top end is missing.'
        )
    elif is_lossy:
        verdict = (
            f'Content stops sharply at {khz:.1f} kHz — typical of audio that was compressed '
            f'(MP3/AAC/streaming) or recorded at a low sample rate somewhere along the way; '
            f'the very top end is missing.'
        )
    elif full:
        verdict = 'Full frequency range (content reaches about 20 kHz).'
    else:
        verdict = f'High frequencies fade out naturally around {khz:.1f} kHz.'
    return {
        'cutoff_hz': int(round(cutoff)),
        'is_lossy_cutoff': is_lossy,
        'cliff_db': round(float(best_drop), 1),
        'full_range': bool(full),
        'judged': True,
        'verdict': verdict,
    }


# ---------------------------------------------------------------------------
# Level / stereo helpers
# ---------------------------------------------------------------------------
def frame_rms_db(x: np.ndarray, sr: int, frame_s: float = 0.05) -> np.ndarray:
    mono_power = (x.astype(np.float32) ** 2).mean(axis=1) if x.ndim > 1 else x.astype(np.float32) ** 2
    n = int(frame_s * sr)
    m = len(mono_power) // n
    if m == 0:
        return np.array([-120.0])
    p = mono_power[: m * n].reshape(m, n).mean(axis=1)
    return 10.0 * np.log10(p + 1e-14)


def noise_floor(x: np.ndarray, sr: int) -> dict[str, float]:
    """Programme-level percentiles, with legacy result names kept for compatibility.

    `noise_floor_db` is the 10th percentile of live 50 ms RMS frames (digital
    silence excluded); `snr_db` is the 90th-to-10th percentile distance. Neither
    statistic distinguishes wanted content from noise or diagnoses background noise.
    """
    db = frame_rms_db(x, sr)
    live = db[db > -100.0]
    if live.size < 10:
        return {'noise_floor_db': -120.0, 'snr_db': 120.0}
    nf = float(np.percentile(live, 10))
    loud = float(np.percentile(live, 90))
    return {'noise_floor_db': round(nf, 1), 'snr_db': round(loud - nf, 1)}


def flat_top_clipping(x: np.ndarray, threshold: float = 0.999) -> float:
    """Share of samples (%) sitting in runs of ≥3 consecutive near-full-scale samples.

    Float files peaking far above full scale (> +3 dBFS) are not clipped — they
    still hold the whole waveform — so there the runs are counted at their own
    peak. Decoded lossy files of clipped masters overshoot 0 dBFS only a little,
    so up to +3 dBFS full scale stays the reference."""
    peak = float(np.abs(x).max(initial=0.0))
    ref = peak if peak > 1.41 else 1.0
    hot = np.abs(x) >= threshold * ref
    if not hot.any():
        return 0.0
    run = hot[2:] & hot[1:-1] & hot[:-2]
    return round(100.0 * float(run.sum()) / max(1, hot.size), 4)


def stereo_info(x: np.ndarray, sr: int) -> dict[str, Any]:
    if x.ndim < 2 or x.shape[1] < 2:
        return {'width': 0.0, 'is_mono': True, 'antiphase_bass': False, 'bass_correlation': 1.0}
    mid = (x[:, 0] + x[:, 1]) * 0.5
    side = (x[:, 0] - x[:, 1]) * 0.5
    em, es = float((mid**2).mean()) + 1e-20, float((side**2).mean()) + 1e-20
    width_db = 10.0 * math.log10(es / em)
    sos = butter(4, 120.0, 'lowpass', fs=sr, output='sos')
    low = sosfiltfilt(sos, x, axis=0)
    left, right = low[:, 0], low[:, 1]
    denom = math.sqrt(float((left**2).sum()) * float((right**2).sum())) + 1e-20
    corr = float((left * right).sum()) / denom
    return {
        'width_db': round(width_db, 1),
        'is_mono': width_db < -40.0,
        'antiphase_bass': corr < 0.0,
        'bass_correlation': round(corr, 2),
    }


# ---------------------------------------------------------------------------
# Dynamics style (the validated ClubReady anchor rules, used by the club recipe)
# ---------------------------------------------------------------------------
def dynamics_style(lufs: float, lra_val: float) -> dict[str, Any]:
    if lra_val >= 6.5 or lufs <= -14.8:
        return {
            'type': 'concert',
            'name': 'Wide dynamics (live / orchestral)',
            'rule': f'LRA ≥ 6.5 or LUFS ≤ −14.8 (measured LRA {lra_val:.1f}, {lufs:.1f} LUFS)',
            'recommended_target_lufs': round(max(-17.0, min(-14.2, lufs + 0.5)), 1),
            'recommended_glue_comp': False,
            'recommended_bass_punch_db': 0.5,
        }
    if lra_val <= 3.2 or lufs >= -11.2:
        return {
            'type': 'festival',
            'name': 'Dense peak-time / festival',
            'rule': f'LRA ≤ 3.2 or LUFS ≥ −11.2 (measured LRA {lra_val:.1f}, {lufs:.1f} LUFS)',
            'recommended_target_lufs': round(max(-11.5, min(-9.8, lufs)), 1),
            'recommended_glue_comp': False,
            'recommended_bass_punch_db': 1.5,
        }
    return {
        'type': 'club',
        'name': 'Club / DJ mix dynamics',
        'rule': f'LRA between 3.2 and 6.5 (measured LRA {lra_val:.1f}, {lufs:.1f} LUFS)',
        'recommended_target_lufs': round(max(-13.5, min(-11.8, lufs + 0.2)), 1),
        'recommended_glue_comp': True,
        'recommended_bass_punch_db': 1.5,
    }


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def _silent_profile(sr: int, duration_s: float, basis: str, channels: int | None) -> dict[str, Any]:
    msg = 'This file is silent — there is nothing to measure or enhance.'
    return {
        'duration_s': round(float(duration_s), 2),
        'samplerate': sr,
        'channels': channels,
        'analysis_basis': basis,
        'is_silent': True,
        'integrated_lufs': -70.0,
        'true_peak_dbtp': -120.0,
        'lra': 0.0,
        'is_clipping': False,
        'clipped_samples_pct': 0.0,
        'dc_offset': 0.0,
        'bandwidth': {
            'cutoff_hz': None,
            'is_lossy_cutoff': False,
            'cliff_db': 0.0,
            'full_range': False,
            'judged': False,
            'verdict': 'No audible content.',
        },
        'bands_db': {str(int(fc)) if fc >= 100 else str(fc): -120.0 for fc in OCTAVE_CENTERS},
        'mud_excess_db': 0.0,
        'has_mud_excess': False,
        'noise_floor_db': -120.0,
        'snr_db': 0.0,
        'stereo': {'width_db': 0.0, 'is_mono': True, 'antiphase_bass': False, 'bass_correlation': 1.0},
        'dynamics_style': dynamics_style(-70.0, 0.0),
        'key': None,
        'alerts': [{'level': 'danger', 'title': 'No sound', 'message': msg}],
    }


def profile(
    x: np.ndarray,
    sr: int,
    duration_s: float,
    basis: str,
    channels: int | None = None,
    source_rate: int | None = None,
    check_cancel=None,
) -> dict[str, Any]:
    """`channels` / `source_rate`: the source file's own format (reported as `channels` and
    `samplerate`); x itself is the 48 kHz stereo analysis copy. `check_cancel` (optional)
    is called between the measurements."""
    channels = channels if channels is not None else (x.shape[1] if x.ndim > 1 else 1)
    if x.shape[0] == 0 or float(np.abs(x).max(initial=0.0)) < 10.0 ** (SILENCE_DBFS / 20.0):
        return _silent_profile(source_rate or sr, duration_s, basis, channels)
    check = check_cancel or (lambda: None)
    lufs = programme_loudness(x, sr=sr, check_cancel=check_cancel)
    lra_val = lra(x, sr=sr, check_cancel=check_cancel)
    tp = true_peak_db(x, check_cancel=check_cancel)
    clip_pct = flat_top_clipping(x)
    dc = float(np.abs(x.mean(axis=0)).max())
    check()
    freqs, psd = average_psd(x, sr)
    bands = octave_bands_db(freqs, psd)
    bandwidth = _bandwidth_from_psd(freqs, psd, sr, source_rate)
    mud_est = (bands['250'] + bands['500']) / 2.0
    broadband = sum(bands.values()) / len(bands)
    mud_excess = round(mud_est - broadband, 2)
    check()
    nf = noise_floor(x, sr)
    stereo = stereo_info(x, sr)
    style = dynamics_style(lufs, lra_val)
    check()
    key = estimate_key(x, sr, lufs)

    alerts: list[dict[str, str]] = []
    if clip_pct > 0.01:
        alerts.append(
            {
                'level': 'danger',
                'title': 'The recording is clipped',
                'message': f'{clip_pct:.2f} % of samples are flat-topped at full scale. '
                'That distortion is baked in and can only be softened, not undone.',
            }
        )
    if tp > -0.5:
        alerts.append(
            {
                'level': 'warning',
                'title': 'Peaks are too hot',
                'message': f'Peaks reach {tp:+.1f} dBTP — above the −1 dB safety margin, so they may '
                'distort on some speakers or after conversion to MP3/AAC.',
            }
        )
    else:
        alerts.append(
            {'level': 'ok', 'title': 'Peaks are safe', 'message': f'Highest peak {tp:+.1f} dBTP — no digital overs.'}
        )
    if dc > DC_ALERT:
        alerts.append(
            {
                'level': 'info',
                'title': 'Off-centre waveform (DC offset)',
                'message': f'The waveform is shifted {100 * dc:.1f} % off centre. You cannot hear it, '
                'but it wastes headroom; Enhance removes it.',
            }
        )
    if lufs < -20.0:
        alerts.append(
            {
                'level': 'info',
                'title': 'Quite quiet',
                'message': f'Average loudness is {lufs:.1f} LUFS — noticeably quieter than most music (−14 … −8 LUFS).',
            }
        )
    elif lufs > -7.5:
        alerts.append(
            {
                'level': 'info',
                'title': 'Very loud master',
                'message': f'Average loudness is {lufs:.1f} LUFS — heavily compressed; there is little room for punch.',
            }
        )
    alerts.append(
        {
            'level': 'warning' if bandwidth['is_lossy_cutoff'] else 'ok',
            'title': 'Missing top end' if bandwidth['is_lossy_cutoff'] else 'Frequency range',
            'message': bandwidth['verdict'],
        }
    )
    if mud_excess > 1.0:
        alerts.append(
            {
                'level': 'info',
                'title': 'A bit boomy / boxy',
                'message': f'Low-mids (250–500 Hz) sit {mud_excess:+.1f} dB above the overall balance.',
            }
        )
    if stereo['antiphase_bass']:
        alerts.append(
            {
                'level': 'warning',
                'title': 'Bass is out of phase',
                'message': 'Left and right bass partly cancel each other; it may sound thin on mono speakers.',
            }
        )
    if stereo['is_mono']:
        alerts.append({'level': 'info', 'title': 'Mono recording', 'message': 'Left and right are identical (mono).'})

    return {
        'duration_s': round(float(duration_s), 2),
        'samplerate': source_rate or sr,
        'channels': channels,
        'analysis_basis': basis,
        'is_silent': False,
        'integrated_lufs': round(lufs, 2),
        'true_peak_dbtp': round(tp, 2),
        'lra': round(lra_val, 2),
        'is_clipping': bool(clip_pct > 0.01),
        'clipped_samples_pct': clip_pct,
        'dc_offset': round(dc, 4),
        'bandwidth': bandwidth,
        'bands_db': bands,
        'mud_excess_db': mud_excess,
        'has_mud_excess': bool(mud_excess > 1.0),
        'noise_floor_db': nf['noise_floor_db'],
        'snr_db': nf['snr_db'],
        'stereo': stereo,
        'dynamics_style': style,
        'key': key,
        'alerts': alerts,
    }


def _audible_span(path: Path, check_cancel=None) -> tuple[int, int] | None:
    """Outer bounds of audible 1 s blocks, by a streamed peak scan.

    There may be silent gaps between these bounds. None means silence throughout.
    """
    thr = 10.0 ** (SILENCE_DBFS / 20.0)
    sr = sf.info(str(path)).samplerate
    first = last = None
    pos = 0
    for blk in sf.blocks(str(path), blocksize=sr, dtype='float32', always_2d=True):
        if check_cancel:
            check_cancel()
        blk = np.nan_to_num(blk, nan=0.0, posinf=0.0, neginf=0.0)
        if blk.size and max(float(blk.max()), -float(blk.min())) >= thr:
            first = pos if first is None else first
            last = pos + len(blk)
        pos += len(blk)
    return None if first is None else (first, last)


def analyze_file(
    path: Path, progress=None, source_rate: int | None = None, source_channels: int | None = None
) -> tuple[dict[str, Any], np.ndarray, int]:
    """Profile a (48 kHz reference) file. Returns (profile, samples, sr); the samples
    are handed on to analysis.classify.audio_features to avoid a second read.
    Pass the original file's sample rate / channel count (media.ffmpeg.probe_media)
    so the report shows the source's format, not the working copy's."""
    if progress is not None:
        progress(5, 'Reading audio for analysis...')
    info = sf.info(str(path))
    x, sr, basis = load_samples(Path(path))
    if progress is not None:
        progress(40, 'Measuring loudness, peaks and frequency range...')
    check = progress.check_cancel if progress is not None else None
    fmt = {'channels': source_channels or info.channels, 'source_rate': source_rate, 'check_cancel': check}
    rep = profile(x, sr, info.duration, basis, **fmt)
    if rep['is_silent'] and basis != 'whole file':
        # The sampled spots were silent: check the whole file before saying so, and
        # if there is sound somewhere, include a block that actually contains it.
        span = _audible_span(Path(path), check_cancel=check)
        if span is not None:
            first, last = span
            stop = min(last, first + _FALLBACK_SECONDS * sr)
            x, sr, _ = load_samples(Path(path), (first, stop))
            basis = (
                f'{(stop - first) / sr:g} s contiguous excerpt starting at {first / sr:g} s '
                '(anchored at the first block with detected sound)'
            )
            rep = profile(x, sr, info.duration, basis, **fmt)
        else:
            rep['analysis_basis'] = 'whole-file silence confirmed by a streamed peak scan'
    if progress is not None:
        progress(100, 'Analysis done')
    return rep, x, sr
