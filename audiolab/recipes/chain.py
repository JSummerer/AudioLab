"""The mastering chain: one parameterised signal path for every recipe.

    [AI stage] → rumble filter → noise reduction → tonal EQ → HF restore →
    sub-bass harmonics → mono bass → compression → loudness → true-peak limiter →
    24-bit dither → master FLAC (48 kHz)

This merges the prototype's Fast Polish and Deep Stem engines (their post
chains were duplicates). The DSP math is the validated prototype math; what a
stage does is decided entirely by `ChainParams` (see recipes.base.resolve).

Quality "light" (`params.volume_only`) runs the same path with every tone and
dynamics stage off: one gain to the target, an inaudible DC offset removed when
present, and the limiter only when a peak would pass the ceiling — at most
LIGHT_MAX_LIMIT_DB of it; beyond that the gain is reduced instead.

Silent input is refused with a plain message before any processing.

Quality gates are enforced, not just reported:
- true-peak ceiling missed after the limiter → corrective gain, with a warning;
- AI reconstruction gate failed (or the AI stage crashed) → the standard
  (non-AI) chain is used instead, with a warning.

Memory: float32 end to end with streamed zero-phase filters, like the
prototype (~3.5 × one full-length stereo array at peak).

Cancellation: every full-length step checks for it block by block (the DSP
helpers take `check_cancel`), so a Cancel takes effect within about a second
whatever the programme length.
"""

import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy.signal import butter

from ..analysis.profile import DC_ALERT, SILENCE_DBFS
from ..context import Progress
from ..dsp.bus import glue_comp, mono_below
from ..dsp.denoise import spectral_gate
from ..dsp.dither import add_24bit_tpdf
from ..dsp.exciter import hf_restore
from ..dsp.filters import filtfilt_stream, highpass_fq, highshelf_fq, lowshelf_fq, peak_fq
from ..dsp.limiter import tp_limit, true_peak_db
from ..dsp.loudness import integrated_lufs, lra, programme_loudness
from ..errors import AudioLabError, Cancelled
from .base import LIGHT_MAX_LIMIT_DB, ChainParams


def _f32(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float32, copy=False)


def subharmonic_reinforce(
    x: np.ndarray, wet: float = 0.3, fc: float = 90.0, sr: int = 48000, check_cancel=None
) -> np.ndarray:
    """Second-order harmonic warmth from the sub band, kept in (fc, 4·fc) so it
    stays out of the mids (prototype algorithm)."""
    if wet <= 0.0:
        return x
    sos_lp = butter(2, fc, btype='lowpass', fs=sr, output='sos')
    hp_lo, hp_hi = fc * 1.1, min(fc * 4.0, 0.45 * sr)
    sos_band = np.concatenate(
        [
            butter(2, hp_lo, btype='highpass', fs=sr, output='sos'),
            butter(2, hp_hi, btype='lowpass', fs=sr, output='sos'),
        ],
        axis=0,
    )
    sub = filtfilt_stream(sos_lp, x, check_cancel=check_cancel)
    sub *= 1.5
    np.tanh(sub, out=sub)
    sub *= 0.7
    harmonics = filtfilt_stream(sos_band, sub, check_cancel=check_cancel)
    del sub
    harmonics *= np.float32(wet)
    x += harmonics
    return _f32(x)


def _stem_eq(bass_db: float, sr: int) -> dict[str, list]:
    """Per-stem zero-phase EQ of the prototype Deep Stem engine."""
    bass = [peak_fq(300.0, -1.0, q=1.0, sr=sr), highshelf_fq(8000.0, -0.5, s=1.0, sr=sr)]
    if bass_db > 0:
        bass.insert(0, lowshelf_fq(70.0, bass_db, s=1.0, sr=sr))
    return {
        'drums': [highshelf_fq(9000.0, -0.75, s=1.0, sr=sr), peak_fq(350.0, -0.5, q=1.0, sr=sr)],
        'bass': bass,
        'vocals': [
            peak_fq(300.0, -0.75, q=1.0, sr=sr),
            peak_fq(4000.0, +0.75, q=0.8, sr=sr),
            highshelf_fq(10000.0, -0.5, s=1.0, sr=sr),
        ],
        'other': [peak_fq(300.0, -0.75, q=1.0, sr=sr), highshelf_fq(9000.0, -0.75, s=1.0, sr=sr)],
        'guitar': [peak_fq(300.0, -0.75, q=1.0, sr=sr)],
        'piano': [peak_fq(300.0, -0.75, q=1.0, sr=sr)],
    }


def _read_stem(path: str, n: int) -> np.ndarray:
    stem, _ = sf.read(path, dtype='float32', always_2d=True)
    if stem.shape[0] > n:
        stem = stem[:n]
    elif stem.shape[0] < n:
        stem = np.pad(stem, ((0, n - stem.shape[0]), (0, 0)))
    return stem


def _read_ref(ref: Path, warnings: list[str] | None = None) -> np.ndarray:
    x, _ = sf.read(str(ref), dtype='float32', always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    x = x[:, :2]
    bad = 0
    for s0 in range(0, len(x), 1 << 20):  # broken float files: NaN/inf samples become silence
        blk = x[s0 : s0 + (1 << 20)]
        mask = ~np.isfinite(blk)
        if mask.any():
            bad += int(mask.sum())
            blk[mask] = 0.0
    if bad and warnings is not None:
        warnings.append(f'The file contained {bad} broken samples (not a number); they were replaced with silence.')
    return x


def _measure(x: np.ndarray, sr: int, tp: float | None = None, check_cancel=None) -> dict[str, float]:
    """Loudness, true peak and LRA; pass `tp` when it is already known (saves the
    4x-oversampled pass)."""
    return {
        'integrated_lufs': round(programme_loudness(x, sr=sr, check_cancel=check_cancel), 2),
        'true_peak_dbtp': round(true_peak_db(x, check_cancel=check_cancel) if tp is None else tp, 2),
        'lra': round(lra(x, sr=sr, check_cancel=check_cancel), 2),
    }


def _write_master(x: np.ndarray, sr: int, path: Path, check_cancel) -> None:
    """24-bit FLAC, written in blocks so a cancel does not wait for the whole encode."""
    with sf.SoundFile(str(path), 'w', sr, x.shape[1], subtype='PCM_24', format='FLAC') as f:
        for s0 in range(0, len(x), 1 << 20):
            check_cancel()
            f.write(x[s0 : s0 + (1 << 20)])


def _ai_stage(ref: Path, params: ChainParams, n: int, sr: int, work_dir: Path, progress: Progress) -> dict[str, Any]:
    """Run separation; returns {'x': array or None, 'report': {...}, 'warning': str|None}."""
    from ..ai.separation import separate

    stems_dir = Path(work_dir) / 'stems'
    sep = separate(ref, stems_dir, model=params.ai_model, progress=progress.sub(0, 85))
    report = {
        'stage': params.ai_stage,
        'model': sep['model'],
        'device': sep['device'],
        'residual_db': sep['residual_db'],
        'gate_passed': sep['gate_passed'],
        'fallback': False,
    }
    if not sep['gate_passed']:
        report['fallback'] = True
        return {
            'x': None,
            'report': report,
            'warning': f'The AI separation was not clean enough (residual {sep["residual_db"]} dB) — '
            f'used the standard chain instead.',
        }
    if params.ai_stage == 'voice_isolate':
        x = _read_stem(sep['stems']['vocals'], n)
        progress(100)
        return {'x': x, 'report': report, 'warning': None}
    eq = _stem_eq(params.bass_db, sr)
    mix = np.zeros((n, 2), dtype=np.float32)
    names = list(sep['stems'])
    for i, name in enumerate(names):
        stem = _read_stem(sep['stems'][name], n)
        for sos in eq.get(name, []):
            stem = filtfilt_stream(sos, stem, check_cancel=progress.check_cancel)
        mix += stem
        del stem
        progress(85 + 15 * (i + 1) / len(names), f'Polishing the {name} stem...')
    return {'x': mix, 'report': report, 'warning': None}


def render(
    ref: Path,
    out_master: Path,
    params: ChainParams,
    profile: dict[str, Any] | None,
    work_dir: Path,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Render the master. Returns the report described in docs/ARCHITECTURE.md (Enhance in detail)."""
    progress = progress or Progress.null()
    t0 = time.time()
    ref, out_master, work_dir = Path(ref), Path(out_master), Path(work_dir)
    out_master.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile = profile or {}
    info = sf.info(str(ref))
    sr, n = info.samplerate, info.frames
    steps: list[str] = []
    warnings: list[str] = []
    ai_report = None

    # --- input + optional AI stage -------------------------------------------
    use_ai = params.ai_stage in ('stem_eq', 'voice_isolate')
    chain_p = progress.sub(55, 100) if use_ai else progress
    progress(1, 'Loading audio...')
    x = _read_ref(ref, warnings)
    input_metrics = _measure(x, sr, check_cancel=progress.check_cancel)
    if n == 0 or input_metrics['true_peak_dbtp'] < SILENCE_DBFS:
        raise AudioLabError(
            'This file is silent — there is nothing to enhance.',
            hints=[
                'Check that you picked the right file (or the right part of a video).',
                'Play it once: if you hear nothing, the recording itself is empty.',
            ],
        )
    if use_ai:
        del x
        x = None
        try:
            res = _ai_stage(ref, params, n, sr, work_dir, progress.sub(2, 55))
            ai_report = res['report']
            if res['warning']:
                warnings.append(res['warning'])
            x = res.pop('x')  # transfer ownership; later DSP can release the obsolete AI buffer
        except Cancelled:
            raise
        except Exception as e:  # MPS/CUDA OOM, missing model, ... → fall back, don't fail the job
            msg = str(e) if isinstance(e, AudioLabError) else f'{type(e).__name__}: {e}'
            warnings.append(f'The AI stage failed ({msg[:160]}) — used the standard chain instead.')
            ai_report = {'stage': params.ai_stage, 'fallback': True, 'error': msg[:300]}
        if x is None:
            x = _read_ref(ref)
            use_ai = False
        elif params.ai_stage == 'stem_eq':
            steps.append(
                'Split the mix into drums, bass, vocals and the rest with AI and polished each part on its own'
            )
        else:
            steps.append('Isolated the voice with AI — music and background sounds removed')
    p = chain_p

    # --- rumble filter ---------------------------------------------------------
    if params.highpass_hz > 0:
        p(5, 'Removing rumble...')
        x = filtfilt_stream(highpass_fq(params.highpass_hz, sr=sr), x, check_cancel=p.check_cancel)
        steps.append(f'Removed inaudible rumble below {params.highpass_hz:.0f} Hz')

    # --- noise reduction -----------------------------------------------------
    if params.denoise in ('light', 'strong'):
        p(8, 'Reducing background noise...')
        x, dn = spectral_gate(x, sr=sr, mode=params.denoise, progress=p.sub(8, 30))
        if dn.get('applied'):
            steps.append(
                f'Reduced steady background noise ({params.denoise}, up to {dn["max_reduction_db"]:.0f} dB in pauses)'
            )

    # --- tonal EQ (the stem-aware AI stage already did its own EQ) -------------
    if not (use_ai and params.ai_stage == 'stem_eq' or params.volume_only):
        p(32, 'Shaping the tone...')
        if params.mud_cut_db < 0:
            x = filtfilt_stream(peak_fq(320.0, params.mud_cut_db, q=1.0, sr=sr), x, check_cancel=p.check_cancel)
            steps.append('Cleaned up muddy low-mids around 320 Hz')
        if params.bass_db > 0:
            x = filtfilt_stream(lowshelf_fq(85.0, params.bass_db, s=1.0, sr=sr), x, check_cancel=p.check_cancel)
            steps.append(f'Added a little bass weight (+{params.bass_db:.1f} dB)')
        if params.presence_db:
            x = filtfilt_stream(peak_fq(3400.0, params.presence_db, q=1.2, sr=sr), x, check_cancel=p.check_cancel)
            steps.append(
                'Made the voice clearer and more present'
                if params.compressor == 'leveler'
                else 'Brought vocals and lead sounds slightly forward'
            )
        if params.air_db:
            x = filtfilt_stream(highshelf_fq(11000.0, params.air_db, s=1.0, sr=sr), x, check_cancel=p.check_cancel)
            steps.append('Added a touch of sparkle on top')

    # --- HF restore (opt-in exciter) -----------------------------------------
    cutoff = (profile.get('bandwidth') or {}).get('cutoff_hz')
    if params.hf_restore and cutoff:
        p(40, 'Re-creating missing highs...')
        x, ex = hf_restore(x, sr, float(cutoff), progress=p.sub(40, 48))
        if ex.get('applied'):
            steps.append(f'Re-created missing highs above {cutoff / 1000:.1f} kHz (subtle exciter)')

    # --- bass: harmonics and mono fold ---------------------------------------
    fold_hz = params.mono_bass_hz or 90.0
    if params.sub_harmonics > 0:
        p(50, 'Reinforcing sub-bass...')
        x = subharmonic_reinforce(x, wet=params.sub_harmonics, fc=fold_hz, sr=sr, check_cancel=p.check_cancel)
        steps.append('Reinforced the sub-bass so it carries on small speakers too')
    if params.mono_bass_hz:
        p(56, 'Centering the bass...')
        x = mono_below(x, fc=params.mono_bass_hz, sr=sr, check_cancel=p.check_cancel)
        steps.append(f'Tightened the bass below {params.mono_bass_hz:.0f} Hz so it hits harder on club and car systems')

    # --- compression -----------------------------------------------------------
    comp_stats: dict[str, Any] = {'engaged': False}
    if params.compressor == 'glue':
        p(62, 'Gluing the mix...')
        x, comp_stats = glue_comp(x, thresh_dbfs=-16.5, ratio=1.5, max_gr_db=1.5, sr=sr, check_cancel=p.check_cancel)
        comp_stats['engaged'] = True
        steps.append(f'Gently glued the mix together (at most {comp_stats["max_gr_db"]:.1f} dB of compression)')
    elif params.compressor == 'leveler':
        p(62, 'Evening out the levels...')
        thresh = integrated_lufs(x, sr=sr, check_cancel=p.check_cancel) + 3.0
        x, comp_stats = glue_comp(
            x,
            thresh_dbfs=thresh,
            ratio=3.0,
            atk_ms=10.0,
            rel_ms=250.0,
            max_gr_db=9.0,
            sr=sr,
            check_cancel=p.check_cancel,
        )
        comp_stats.update(engaged=True, mode='leveler', thresh_dbfs=round(thresh, 1))
        steps.append('Evened out loud and quiet moments of the voice')
    else:
        comp_stats['skip_reason'] = 'off for this recipe'

    # --- light: DC offset (inaudible, but it eats headroom) --------------------
    tp_now = None  # true peak of x when already known
    dc_removed = False
    if params.volume_only:
        tp_now = input_metrics['true_peak_dbtp']
        dc = x.mean(axis=0, dtype=np.float64)
        if float(np.abs(dc).max()) > DC_ALERT:
            x -= dc.astype(np.float32)
            tp_now = true_peak_db(x, check_cancel=p.check_cancel)
            dc_removed = True  # the input measurement no longer describes x
            steps.append('Removed an inaudible DC offset (the waveform sat off-centre and wasted headroom)')

    # --- loudness --------------------------------------------------------------
    p(72, f'Setting loudness to {params.target_lufs:+.1f} LUFS...')
    p.check_cancel()
    # Light changed nothing so far (unless a DC offset went): the input measurement is still valid.
    if params.volume_only and not dc_removed:
        i0 = input_metrics['integrated_lufs']
    else:
        i0 = programme_loudness(x, sr=sr, check_cancel=p.check_cancel)
    gain_db = params.target_lufs - i0
    capped = tp_now is not None and tp_now + gain_db - params.tp_ceiling_db > LIGHT_MAX_LIMIT_DB
    if capped:
        gain_db = params.tp_ceiling_db + LIGHT_MAX_LIMIT_DB - tp_now
        warnings.append(
            f'Stayed at {i0 + gain_db:.1f} LUFS instead of {params.target_lufs:.1f}: getting louder '
            f'would have needed more than {LIGHT_MAX_LIMIT_DB:.0f} dB of peak limiting, which '
            f'would change the sound.'
        )
    if gain_db > 30.0:
        warnings.append(
            f'This recording was extremely quiet — raising it by {gain_db:.0f} dB also raises its '
            f'background noise and hiss.'
        )
    x *= np.float32(10.0 ** (gain_db / 20.0))
    if tp_now is not None:
        tp_now += gain_db

    # --- true-peak limiter + enforced gate ----------------------------------
    if tp_now is not None and tp_now <= params.tp_ceiling_db - 0.05:
        tp_stats = {
            'engaged': False,
            'tp_before': round(tp_now, 3),
            'tp_after': round(tp_now, 3),
            'max_gr_db': 0.0,
            'skip_reason': 'peaks already below the ceiling',
        }
    else:
        p(80, f'Catching peaks (ceiling {params.tp_ceiling_db:.2f} dBTP)...')
        x, tp_stats = tp_limit(x, ceiling_db=params.tp_ceiling_db, sr=sr, check_cancel=p.check_cancel, in_place=True)
    if not tp_stats.get('ceiling_met', True):
        over = float(tp_stats['tp_after']) - params.tp_ceiling_db + 0.05
        x *= np.float32(10.0 ** (-over / 20.0))
        tp_stats['corrective_gain_db'] = round(-over, 2)
        tp_stats['ceiling_met'] = true_peak_db(x, check_cancel=p.check_cancel) <= params.tp_ceiling_db + 0.01
        warnings.append(
            f'Peaks needed more control than the limiter allows, so the whole master was turned '
            f'down {over:.1f} dB to stay distortion-free.'
        )
    if tp_stats.get('engaged'):
        steps.append(
            f'Caught peaks so nothing clips (≤ {params.tp_ceiling_db:.2f} dBTP, '
            f'max {tp_stats.get("max_gr_db", 0):.1f} dB of limiting)'
        )

    # --- write ----------------------------------------------------------------
    p(92, 'Writing the 24-bit master...')
    x = add_24bit_tpdf(x, check_cancel=p.check_cancel)
    tmp = out_master.with_name(out_master.stem + '.partial.flac')
    _write_master(x, sr, tmp, p.check_cancel)
    tmp.replace(out_master)

    p(96, 'Measuring the result...')
    p.check_cancel()
    # The limiter (or, in Light, the prediction) already measured the final true peak.
    known_tp = None if 'corrective_gain_db' in tp_stats else tp_stats['tp_after']
    if params.volume_only and not (tp_stats.get('engaged') or dc_removed) and i0 - abs(gain_db) > -45.0:
        # Pure gain: loudness and LRA move with it exactly. (Only the fixed -70 LUFS gate does
        # not, and blocks near it sit far below the relative gates at this level.)
        final = {
            'integrated_lufs': round(i0 + gain_db, 2),
            'true_peak_dbtp': round(known_tp, 2),
            'lra': input_metrics['lra'],
        }
    else:
        final = _measure(x, sr, tp=known_tp, check_cancel=p.check_cancel)
    delta = final['integrated_lufs'] - input_metrics['integrated_lufs']
    if abs(delta) < 0.3:
        steps.insert(0, f'Kept the loudness where it was ({final["integrated_lufs"]:.1f} LUFS)')
    else:
        steps.insert(
            0,
            f'Made it {abs(delta):.1f} dB {"louder" if delta > 0 else "quieter"} '
            f'({input_metrics["integrated_lufs"]:.1f} → {final["integrated_lufs"]:.1f} LUFS)',
        )
    if abs(final['integrated_lufs'] - params.target_lufs) > 0.6 and not (capped or tp_stats.get('corrective_gain_db')):
        warnings.append(
            f'Ended at {final["integrated_lufs"]:.1f} LUFS instead of {params.target_lufs:.1f} '
            f'(peak limiting took some loudness).'
        )
    p(100, 'Master ready')
    return {
        'integrated_lufs': final['integrated_lufs'],
        'true_peak_dbtp': final['true_peak_dbtp'],
        'lra': final['lra'],
        'input': input_metrics,
        'loudness_gain_db': round(gain_db, 2),
        'compressor': comp_stats,
        'limiter': tp_stats,
        'steps': steps,
        'ai': ai_report,
        'warnings': warnings,
        'params': params.to_dict(),
        'duration_s': round(n / sr, 2),
        'wall_time_s': round(time.time() - t0, 2),
        'master_path': str(out_master),
    }
