"""Neural source separation with Demucs (drums / bass / other / vocals).

Windowed for bounded memory: the reference is read in ~2 min windows, each
window is resampled to the model rate (htdemucs is trained at 44.1 kHz — the
prototype fed it 48 kHz directly, which detunes everything the model hears by
~9 %), separated, resampled back, crossfaded with the previous window and
streamed to disk as float32 WAV per stem (stems are intermediates: opposing
stems may legitimately exceed ±1.0 while their sum is fine, so they must not
be clipped by an integer format). Peak RAM stays O(window).

A reconstruction gate (sum of stems vs input, worst window, excluding window
edges) tells callers whether the separation is trustworthy.
"""

import math
import os
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .. import runtime
from ..context import Progress
from ..errors import AudioLabError, ToolMissingError
from . import DEFAULT_MODEL, MODELS, ai_status

WINDOW_S = 120.0  # separation window (>= 2x the demucs internal segment);
# cancellation is checked per ~8 s model segment via apply_model's callback
WINDOW_OVERLAP_S = 12.0  # crossfade region between windows
GATE_DB = -28.0  # reconstruction residual must be at or below this


def _load_model(name: str, progress: Progress):
    if runtime.is_ai_worker():
        from .worker import load_local_model

        progress(1, f'Loading the installed AI model ({MODELS[name]["label"]})…')
        return load_local_model(name)
    os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')
    try:
        from huggingface_hub.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
    except Exception:
        pass
    from demucs.pretrained import get_model

    progress(
        1,
        f'Loading the AI model ({MODELS.get(name, {}).get("label", name)}) — '
        f'the first run downloads it (~80–300 MB)...',
    )
    try:
        model = get_model(name)
    except Exception as e:
        raise AudioLabError(
            'Could not load the AI separation model.',
            hints=[
                'The first use downloads the model — check your internet connection.',
                f'Details: {type(e).__name__}: {str(e)[:200]}',
            ],
            fix='retry',
        ) from e
    return model


def _float_wav(path: Path, frames: int, sr: int, channels: int = 2) -> sf.SoundFile:
    """Writer for an unclipped float32 intermediate; RF64 when it could pass 4 GB."""
    big = frames * channels * 4 > 3_900_000_000
    return sf.SoundFile(
        str(path), 'w', samplerate=sr, channels=channels, subtype='FLOAT', format='RF64' if big else 'WAV'
    )


def stems_peak(paths: list[Path]) -> float:
    """Highest absolute sample over all files (streamed), linear."""
    peak = 0.0
    for p in paths:
        for blk in sf.blocks(str(p), blocksize=1 << 18, dtype='float32', always_2d=True):
            if blk.size:
                peak = max(peak, float(np.abs(blk).max()))
    return peak


def separate(
    ref: Path, out_dir: Path, model: str = DEFAULT_MODEL, shifts: int = 1, progress: Progress | None = None
) -> dict[str, Any]:
    """Separate `ref` into stems written to `out_dir/<stem>.wav` (float32, ref rate).

    Returns {'stems': {name: path}, 'residual_db', 'gate_passed', 'device', 'model',
             'sources', 'wall_time_s'}."""
    progress = progress or Progress.null()
    if runtime.is_packaged() and not runtime.is_ai_worker():
        from .client import separate_managed

        return separate_managed(ref, out_dir, model, shifts, progress)
    status = ai_status()
    if not status.get('available'):
        raise ToolMissingError(
            'AI features are not installed.',
            hints=['Enable the AI pack in Settings (one-time download).'],
            fix='install_ai',
        )
    import torch
    from demucs.apply import apply_model

    t0 = time.time()
    info = sf.info(str(ref))
    sr, n_total = info.samplerate, info.frames

    # Global normalisation stats (streamed; Demucs expects roughly unit-variance input)
    mean_acc = np.zeros(2, dtype=np.float64)
    sq_acc = np.zeros(2, dtype=np.float64)
    for blk in sf.blocks(str(ref), blocksize=1 << 20, dtype='float32', always_2d=True):
        blk = blk[:, :2] if blk.shape[1] >= 2 else np.repeat(blk, 2, axis=1)
        blk = np.nan_to_num(blk, nan=0.0, posinf=0.0, neginf=0.0)
        mean_acc += blk.sum(axis=0, dtype=np.float64)
        sq_acc += (blk.astype(np.float64) ** 2).sum(axis=0)
    ref_mean = float(mean_acc.mean() / max(1, n_total))
    if n_total == 0 or sq_acc.mean() / n_total < 1e-16:  # digital silence (below -160 dB RMS)
        raise AudioLabError(
            'This file is silent — there is nothing to split.',
            hints=['Check that you picked the right file (or the right part of a video).'],
        )
    ref_std = max(1e-4, math.sqrt(max(1e-12, sq_acc.mean() / max(1, n_total) - ref_mean**2)))

    model_name = model if model in MODELS else DEFAULT_MODEL
    device = status['device']
    net = _load_model(model_name, progress)
    net.to(device).eval()
    order: list[str] = list(getattr(net, 'sources', ['drums', 'bass', 'other', 'vocals']))
    model_sr = int(getattr(net, 'samplerate', 44100))

    ratio = Fraction(model_sr, sr)
    up, down = ratio.numerator, ratio.denominator  # 147/160 for 48k -> 44.1k
    seg_s = float(getattr(net, 'segment', None) or getattr(net, 'max_allowed_segment', 8.0))
    win = max(int(WINDOW_S * sr), 2 * int(seg_s * sr))
    ov = min(int(WINDOW_OVERLAP_S * sr), win // 4)
    hop = win - ov
    edge = int(0.5 * sr)  # excluded from the gate at window edges

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem_paths = {name: out_dir / f'{name}.wav' for name in order}
    writers: dict[str, sf.SoundFile] = {}
    fade = np.linspace(0.0, 1.0, ov, dtype=np.float32)[:, None] if ov else None
    prev_tail = None
    written = 0
    worst_db = -120.0
    sep_progress = progress.sub(3, 100)
    try:
        for name, p in stem_paths.items():
            writers[name] = _float_wav(p, n_total, sr)
        start = 0
        while start < n_total:
            stop = min(n_total, start + win)
            seg, _ = sf.read(str(ref), start=start, stop=stop, dtype='float32', always_2d=True)
            seg = seg[:, :2] if seg.shape[1] >= 2 else np.repeat(seg, 2, axis=1)
            seg = np.nan_to_num(seg, nan=0.0, posinf=0.0, neginf=0.0)
            seg_len = seg.shape[0]
            wav = seg.T  # (2, L) at sr
            wav_m = resample_poly(wav, up, down, axis=1).astype(np.float32) if up != down else wav
            wav_t = torch.from_numpy(np.ascontiguousarray((wav_m - ref_mean) / ref_std)).unsqueeze(0)
            win_len_m = max(1, wav_m.shape[1])
            frac0, frac1 = start / n_total, stop / n_total

            def on_segment(d, _len=win_len_m, _a=frac0, _b=frac1):
                # Called by demucs before/after every ~8 s model segment: a cheap,
                # frequent cancellation point plus progress inside the window.
                models = max(1, int(d.get('models', 1)))
                inner = (int(d.get('model_idx_in_bag', 0)) + min(1.0, d.get('segment_offset', 0) / _len)) / models
                sep_progress(
                    100.0 * (_a + (_b - _a) * inner),
                    f'AI separation on {device.upper()}: {100.0 * (_a + (_b - _a) * inner):.0f} %',
                )

            with torch.no_grad():
                src = apply_model(
                    net,
                    wav_t.to(device),
                    device=device,
                    shifts=max(1, int(shifts)),
                    split=True,
                    overlap=0.25,
                    progress=False,
                    callback=on_segment,
                )[0]
            # Denormalise; the mean (≈ DC) is shared out so the stems still sum to the input.
            src = (src.float().cpu().numpy() * ref_std + ref_mean / len(order)).astype(np.float32)
            del wav_t
            if up != down:
                src = resample_poly(src, down, up, axis=2).astype(np.float32)
            if src.shape[2] >= seg_len:
                src = src[:, :, :seg_len]
            else:
                src = np.pad(src, ((0, 0), (0, 0), (0, seg_len - src.shape[2])))

            last = stop >= n_total
            if prev_tail is not None:
                for k, name in enumerate(order):
                    writers[name].write(prev_tail[k] * (1.0 - fade) + src[k][:, :ov].T * fade)
                written += ov
                mid_from = ov
            else:
                mid_from = 0
            mid_to = seg_len - (0 if last else ov)
            for k, name in enumerate(order):
                writers[name].write(src[k][:, mid_from:mid_to].T)
            written += max(0, mid_to - mid_from)

            # Reconstruction gate on the window interior (resampling edges excluded)
            a = 0 if start == 0 else min(edge, seg_len // 4)
            b = seg_len if last else max(a + 1, seg_len - min(edge, seg_len // 4))
            resid = src[:, :, a:b].sum(axis=0) - wav[:, a:b]
            e_in = float((wav[:, a:b].astype(np.float64) ** 2).mean()) + 1e-12
            e_res = float((resid.astype(np.float64) ** 2).mean()) + 1e-12
            if e_in > 1e-9:  # ignore silent windows
                worst_db = max(worst_db, 10.0 * math.log10(e_res / e_in))

            prev_tail = None if last else src[:, :, seg_len - ov :].transpose(0, 2, 1).copy()
            sep_progress(100.0 * stop / n_total, f'AI separation on {device.upper()}: {100.0 * stop / n_total:.0f} %')
            if last:
                break
            start += hop
    finally:
        for w in writers.values():
            w.close()
        del net
        try:
            if device == 'mps':
                torch.mps.empty_cache()
            elif device == 'cuda':
                torch.cuda.empty_cache()
        except Exception:
            pass

    if abs(written - n_total) > 1024:
        raise AudioLabError(f'AI separation produced the wrong length ({written} vs {n_total} samples).')
    worst_db = round(worst_db, 2)
    return {
        'stems': {k: str(v) for k, v in stem_paths.items()},
        'sources': order,
        'residual_db': worst_db,
        'gate_passed': worst_db <= GATE_DB,
        'device': device,
        'model': model_name,
        'wall_time_s': round(time.time() - t0, 1),
    }


def mix_stems(paths: list[Path], out: Path, gains: list[float] | None = None) -> Path:
    """Sum stems (same length/rate) into one float32 WAV, streamed block by block —
    e.g. drums + bass + other = instrumental. Not clipped: the caller decides the
    final gain (see stems_peak)."""
    paths = [Path(p) for p in paths]
    if not paths:
        raise ValueError('no stems to mix')
    gains = gains or [1.0] * len(paths)
    info = sf.info(str(paths[0]))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    readers = [sf.SoundFile(str(p)) for p in paths]
    try:
        with _float_wav(out, info.frames, info.samplerate, info.channels) as w:
            while True:
                blocks = [r.read(1 << 18, dtype='float32', always_2d=True) for r in readers]
                n = min(b.shape[0] for b in blocks)
                if n == 0:
                    break
                acc = np.zeros((n, blocks[0].shape[1]), dtype=np.float32)
                for b, g in zip(blocks, gains):
                    acc += b[:n] * np.float32(g)
                w.write(acc)
    finally:
        for r in readers:
            r.close()
    return out
