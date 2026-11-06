"""The mastering chain: one parameterised signal path for every recipe.

    [AI stage] → rumble filter → noise reduction → adaptive low-mids → tonal EQ → HF restore →
    sub-bass harmonics → mono bass → compression → de-esser → loudness →
    true-peak limiter → 24-bit dither → master FLAC (48 kHz)

The de-esser follows compression (which lets short S sounds slip under its attack)
and precedes the limiter (so peaks there are not S sounds). With the stem-aware AI
stage it runs on the separated vocals only, so hi-hats never trigger it.

This merges the prototype's Fast Polish and Deep Stem engines (their post
chains were duplicates). The DSP builds on the prototype with regression-tested guards; what a
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

Long renders keep full-rate float32 working arrays in private temporary files.
The same DSP runs in blocks, checking cancellation at block boundaries.
"""

import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np
import soundfile as sf
from scipy.signal import butter

from ..analysis.profile import DC_ALERT, SILENCE_DBFS
from ..context import Progress
from ..dsp.adaptive import control_lowmids
from ..dsp.bus import glue_comp, mono_below
from ..dsp.deess import deess
from ..dsp.denoise import spectral_gate
from ..dsp.dither import add_24bit_tpdf
from ..dsp.exciter import hf_restore
from ..dsp.filters import filtfilt_stream, highpass_fq, highshelf_fq, lowshelf_fq, peak_fq
from ..dsp.limiter import tp_limit, true_peak_db
from ..dsp.loudness import integrated_lufs, lra, programme_loudness
from ..dsp.scratch import DISK_BACKED_AFTER_SECONDS, ScratchBuffers, blocks
from ..errors import AudioLabError, Cancelled
from .base import LIGHT_MAX_LIMIT_DB, STEM_PRESENCE_DB, ChainParams

# Clarity lift: narrow enough to stay out of the S sounds above 5 kHz.
PRESENCE_HZ, PRESENCE_Q = 3000.0, 1.4


def _work_buffer(scratch: ScratchBuffers | None, x: np.ndarray, *keep: np.ndarray) -> np.ndarray | None:
    return scratch.array(x.shape, keep=(x, *keep)) if scratch is not None else None


def _filter(sos, x: np.ndarray, check_cancel, scratch: ScratchBuffers | None, *keep: np.ndarray) -> np.ndarray:
    return filtfilt_stream(sos, x, check_cancel=check_cancel, out=_work_buffer(scratch, x, *keep))


def subharmonic_reinforce(
    x: np.ndarray,
    wet: float = 0.3,
    fc: float = 90.0,
    sr: int = 48000,
    check_cancel=None,
    *,
    scratch: ScratchBuffers | None = None,
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
    sub = _filter(sos_lp, x, check_cancel, scratch)
    for s in blocks(len(x), check_cancel):
        segment = sub[s]
        segment *= 1.5
        np.tanh(segment, out=segment)
        segment *= 0.7
    harmonics = _filter(sos_band, sub, check_cancel, scratch, x)
    for s in blocks(len(x), check_cancel):
        x[s] += harmonics[s] * np.float32(wet)
    return x.astype(np.float32, copy=False)


def _stem_eq(bass_db: float, presence: bool, sr: int) -> dict[str, list]:
    """Per-stem zero-phase EQ of the prototype Deep Stem engine. The vocals' clarity lift is
    narrow at 3 kHz (≤ +0.25 dB from 5 kHz up, where S sounds sit) and only when the recipe
    wants one (`presence`: not on an already bright top end)."""
    bass = [peak_fq(300.0, -1.0, q=1.0, sr=sr), highshelf_fq(8000.0, -0.5, s=1.0, sr=sr)]
    if bass_db > 0:
        bass.insert(0, lowshelf_fq(70.0, bass_db, s=1.0, sr=sr))
    vocals = [peak_fq(300.0, -0.75, q=1.0, sr=sr), highshelf_fq(10000.0, -0.5, s=1.0, sr=sr)]
    if presence:
        vocals.insert(1, peak_fq(PRESENCE_HZ, STEM_PRESENCE_DB, q=PRESENCE_Q, sr=sr))
    return {
        'drums': [highshelf_fq(9000.0, -0.75, s=1.0, sr=sr), peak_fq(350.0, -0.5, q=1.0, sr=sr)],
        'bass': bass,
        'vocals': vocals,
        'other': [peak_fq(300.0, -0.75, q=1.0, sr=sr), highshelf_fq(9000.0, -0.75, s=1.0, sr=sr)],
        'guitar': [peak_fq(300.0, -0.75, q=1.0, sr=sr)],
        'piano': [peak_fq(300.0, -0.75, q=1.0, sr=sr)],
    }


def _read_stem(path: str, n: int, check_cancel, scratch: ScratchBuffers | None, *keep: np.ndarray) -> np.ndarray:
    with sf.SoundFile(path) as file:
        shape = (n, file.channels)
        stem = scratch.array(shape, keep=keep) if scratch is not None else np.empty(shape, dtype=np.float32)
        for s in blocks(n, check_cancel):
            file.read(dtype='float32', always_2d=True, fill_value=0.0, out=stem[s])
    return stem


def _read_ref(
    ref: Path, warnings: list[str] | None = None, *, check_cancel=None, out: np.ndarray | None = None
) -> np.ndarray:
    bad = 0
    with sf.SoundFile(str(ref)) as file:
        x = np.empty((file.frames, 2), dtype=np.float32) if out is None else out
        for s in blocks(len(x), check_cancel):
            x[s] = file.read(s.stop - s.start, dtype='float32', always_2d=True, fill_value=0.0)[:, :2]
            mask = ~np.isfinite(x[s])  # broken float files: NaN/inf samples become silence
            if mask.any():
                bad += int(mask.sum())
                x[s][mask] = 0.0
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


def _ai_stage(
    ref: Path,
    params: ChainParams,
    n: int,
    sr: int,
    work_dir: Path,
    progress: Progress,
    scratch: ScratchBuffers | None = None,
) -> dict[str, Any]:
    """Run separation; returns {'x': array or None, 'report': {...}, 'warning': str|None,
    'deess': the vocals stem's de-esser stats (stem-aware EQ only)}."""
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
            f'used the adaptive chain instead.',
        }
    if params.ai_stage == 'voice_isolate':
        x = _read_stem(sep['stems']['vocals'], n, progress.check_cancel, scratch)
        progress(100)
        return {'x': x, 'report': report, 'warning': None}
    eq = _stem_eq(params.bass_db, params.presence_db > 0, sr)
    mix = scratch.array((n, 2)) if scratch is not None else np.empty((n, 2), dtype=np.float32)
    for s in blocks(n, progress.check_cancel):
        mix[s] = 0.0
    names = list(sep['stems'])
    ds: dict[str, Any] = {'engaged': False, 'skip_reason': 'off'}
    for i, name in enumerate(names):
        stem = _read_stem(sep['stems'][name], n, progress.check_cancel, scratch, mix)
        for sos in eq.get(name, []):
            stem = _filter(sos, stem, progress.check_cancel, scratch, mix)
        if name == 'vocals' and params.deess_db > 0:
            stem, ds = deess(
                stem,
                sr,
                max_gr_db=params.stem_deess_db(),
                mix=False,
                check_cancel=progress.check_cancel,
                band_buffer=_work_buffer(scratch, stem, mix),
            )
        for s in blocks(n, progress.check_cancel):
            mix[s] += stem[s]
        del stem
        progress(85 + 15 * (i + 1) / len(names), f'Polishing the {name} stem...')
    return {'x': mix, 'report': report, 'warning': None, 'deess': ds}


def _deess_step(stats: dict[str, Any], where: str) -> str:
    return (
        f'Softened sharp S sounds {where} (at most {stats["max_gr_db"]:.1f} dB, '
        f'{stats["active_pct"]:.1f} % of the time)'
    )


def render(
    ref: Path,
    out_master: Path,
    params: ChainParams,
    profile: dict[str, Any] | None,
    work_dir: Path,
    progress: Progress | None = None,
    *,
    disk_backed: bool | None = None,
) -> dict[str, Any]:
    """Render the master. Returns the report described in docs/ARCHITECTURE.md (Enhance in detail)."""
    progress = progress or Progress.null()
    ref, out_master, work_dir = Path(ref), Path(out_master), Path(work_dir)
    out_master.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    info = sf.info(str(ref))
    sr, n = info.samplerate, info.frames
    use_scratch = disk_backed if disk_backed is not None else n > DISK_BACKED_AFTER_SECONDS * sr
    with ExitStack() as stack:
        scratch = ScratchBuffers(stack, work_dir, progress.check_cancel) if use_scratch and n > 0 else None
        staging_dir = stack.enter_context(TemporaryDirectory(dir=out_master.parent, prefix='.render-'))
        staged = Path(staging_dir) / 'master.flac'
        report = _render(ref, staged, params, profile or {}, work_dir, progress, sr, n, scratch)
        progress(100, 'Master ready')
        staged.replace(out_master)
    report['master_path'] = str(out_master)
    return report


def _render(
    ref: Path,
    staged: Path,
    params: ChainParams,
    profile: dict[str, Any],
    work_dir: Path,
    progress: Progress,
    sr: int,
    n: int,
    scratch: ScratchBuffers | None,
) -> dict[str, Any]:
    t0 = time.time()
    steps: list[str] = []
    warnings: list[str] = []
    ai_report = None
    deess_stats: dict[str, Any] = {'engaged': False, 'skip_reason': 'off'}
    adaptive_stats: dict[str, Any] = {'engaged': False, 'skip_reason': 'off'}

    # --- input + optional AI stage -------------------------------------------
    use_ai = params.ai_stage in ('stem_eq', 'voice_isolate')
    chain_p = progress.sub(55, 100) if use_ai else progress
    progress(1, 'Loading audio...')
    out = scratch.array((n, 2)) if scratch is not None else None
    x = _read_ref(ref, warnings, check_cancel=progress.check_cancel, out=out)
    del out
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
            res = _ai_stage(ref, params, n, sr, work_dir, progress.sub(2, 55), scratch)
            ai_report = res['report']
            if res['warning']:
                warnings.append(res['warning'])
            x = res.pop('x')  # transfer ownership; later DSP can release the obsolete AI buffer
        except Cancelled:
            raise
        except Exception as e:  # MPS/CUDA OOM, missing model, ... → fall back, don't fail the job
            msg = str(e) if isinstance(e, AudioLabError) else f'{type(e).__name__}: {e}'
            warnings.append(
                'The AI stage failed — used Adaptive processing instead. The full log has technical details.'
            )
            ai_report = {'stage': params.ai_stage, 'fallback': True, 'error': msg[:300]}
        if x is None:
            out = scratch.array((n, 2)) if scratch is not None else None
            x = _read_ref(ref, check_cancel=progress.check_cancel, out=out)
            del out
            use_ai = False
        elif params.ai_stage == 'stem_eq':
            steps.append('Split the mix into drums, bass, vocals and the rest with AI and applied EQ to each part')
            deess_stats = res.get('deess') or deess_stats
            if deess_stats.get('engaged'):
                steps.append(_deess_step(deess_stats, 'in the separated vocals'))
        else:
            steps.append('Used AI to separate the voice from music and background sounds')
    p = chain_p

    # --- rumble filter ---------------------------------------------------------
    if params.highpass_hz > 0:
        p(5, 'Removing rumble...')
        x = _filter(highpass_fq(params.highpass_hz, sr=sr), x, p.check_cancel, scratch)
        steps.append(f'Filtered low frequencies below {params.highpass_hz:.0f} Hz')

    # --- noise reduction -----------------------------------------------------
    if params.denoise in ('light', 'strong'):
        p(8, 'Reducing background noise...')
        x, dn = spectral_gate(x, sr=sr, mode=params.denoise, progress=p.sub(8, 30), out=_work_buffer(scratch, x))
        if dn.get('applied'):
            steps.append(
                f'Reduced steady background noise ({params.denoise}, up to {dn["max_reduction_db"]:.0f} dB in pauses)'
            )

    # --- tonal EQ (the stem-aware AI stage already did its own EQ) -------------
    if not (use_ai and params.ai_stage == 'stem_eq' or params.volume_only):
        p(32, 'Shaping the tone...')
        if params.adaptive_lowmid_db > 0:
            x, adaptive_stats = control_lowmids(
                x, sr, params.adaptive_lowmid_db, check_cancel=p.check_cancel, band_buffer=_work_buffer(scratch, x)
            )
            if adaptive_stats['engaged']:
                steps.append(
                    f'Controlled brief low-mid rises (at most {adaptive_stats["max_gr_db"]:.1f} dB, '
                    f'{adaptive_stats["active_pct"]:.1f} % of the recording)'
                )
            else:
                steps.append('Left the low-mids unchanged — no isolated rises needed control')
        elif params.mud_cut_db < 0:
            x = _filter(peak_fq(320.0, params.mud_cut_db, q=1.0, sr=sr), x, p.check_cancel, scratch)
            steps.append('Reduced low-mids around 320 Hz')
        if params.bass_db > 0:
            x = _filter(lowshelf_fq(85.0, params.bass_db, s=1.0, sr=sr), x, p.check_cancel, scratch)
            steps.append(f'Added a little bass weight (+{params.bass_db:.1f} dB)')
        if params.presence_db:
            x = _filter(peak_fq(PRESENCE_HZ, params.presence_db, q=PRESENCE_Q, sr=sr), x, p.check_cancel, scratch)
            steps.append(f'Adjusted upper mids around 3 kHz ({params.presence_db:+.1f} dB)')
        if params.air_db:
            x = _filter(highshelf_fq(11000.0, params.air_db, s=1.0, sr=sr), x, p.check_cancel, scratch)
            steps.append(f'Adjusted the high frequencies ({params.air_db:+.1f} dB)')
    elif use_ai and params.ai_stage == 'stem_eq':
        adaptive_stats = {'engaged': False, 'skip_reason': 'AI stem EQ already shapes the low-mids'}

    # --- HF restore (opt-in exciter) -----------------------------------------
    cutoff = (profile.get('bandwidth') or {}).get('cutoff_hz')
    if params.hf_restore and cutoff:
        p(40, 'Adding high-frequency harmonics...')
        x, ex = hf_restore(x, sr, float(cutoff), progress=p.sub(40, 48))
        if ex.get('applied'):
            steps.append(f'Added new high-frequency harmonics above {cutoff / 1000:.1f} kHz')

    # --- bass: harmonics and mono fold ---------------------------------------
    fold_hz = params.mono_bass_hz or 90.0
    if params.sub_harmonics > 0:
        p(50, 'Reinforcing sub-bass...')
        x = subharmonic_reinforce(
            x, wet=params.sub_harmonics, fc=fold_hz, sr=sr, check_cancel=p.check_cancel, scratch=scratch
        )
        steps.append('Added harmonics to the deep bass')
    if params.mono_bass_hz:
        p(56, 'Centering the bass...')
        x = mono_below(
            x, fc=params.mono_bass_hz, sr=sr, check_cancel=p.check_cancel, band_buffer=_work_buffer(scratch, x)
        )
        steps.append(f'Made bass below {params.mono_bass_hz:.0f} Hz mono (shared between left and right)')

    # --- compression -----------------------------------------------------------
    comp_stats: dict[str, Any] = {'engaged': False}
    if params.compressor == 'glue':
        p(62, 'Gluing the mix...')
        x, comp_stats = glue_comp(
            x, thresh_dbfs=-16.5, ratio=1.5, max_gr_db=1.5, sr=sr, check_cancel=p.check_cancel, in_place=True
        )
        comp_stats['mode'] = 'glue'
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
            in_place=True,
        )
        comp_stats.update(mode='leveler', thresh_dbfs=round(thresh, 1))
    else:
        comp_stats['skip_reason'] = 'off for this recipe'

    if params.compressor in ('glue', 'leveler'):
        comp_stats['engaged'] = comp_stats['max_gr_db'] > 0
        if comp_stats['engaged']:
            steps.append(f'Compressed louder moments (at most {comp_stats["max_gr_db"]:.1f} dB of reduction)')
        else:
            comp_stats['skip_reason'] = 'no measurable gain reduction'
            steps.append('Compression made no measurable level change')

    # --- de-esser (the stem-aware AI stage already treated the vocals) --------
    if params.deess_db > 0 and not params.volume_only and not (use_ai and params.ai_stage == 'stem_eq'):
        p(66, 'Softening sharp S sounds...')
        mix = params.deess_mix and params.ai_stage != 'voice_isolate'
        x, deess_stats = deess(
            x, sr, max_gr_db=params.deess_db, mix=mix, check_cancel=p.check_cancel, band_buffer=_work_buffer(scratch, x)
        )
        if deess_stats.get('engaged'):
            steps.append(_deess_step(deess_stats, 'and cymbal hits'))

    # --- light: DC offset (inaudible, but it eats headroom) --------------------
    tp_now = None  # true peak of x when already known
    dc_removed = False
    if params.volume_only:
        tp_now = input_metrics['true_peak_dbtp']
        dc = sum(x[s].sum(axis=0, dtype=np.float64) for s in blocks(len(x), p.check_cancel)) / len(x)
        if float(np.abs(dc).max()) > DC_ALERT:
            for s in blocks(len(x), p.check_cancel):
                x[s] -= dc.astype(np.float32)
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
    gain = np.float32(10.0 ** (gain_db / 20.0))
    for s in blocks(len(x), p.check_cancel):
        x[s] *= gain
    if tp_now is not None:
        tp_now += gain_db

    # --- true-peak limiter + enforced gate ----------------------------------
    if tp_now is not None and tp_now <= params.tp_ceiling_db - 0.05:
        tp_stats = {
            'engaged': False,
            'tp_before': round(tp_now, 3),
            'tp_after': round(tp_now, 3),
            'max_gr_db': 0.0,
            'active_pct': 0.0,
            'skip_reason': 'peaks already below the ceiling',
        }
    else:
        p(80, f'Catching peaks (ceiling {params.tp_ceiling_db:.2f} dBTP)...')
        x, tp_stats = tp_limit(
            x, ceiling_db=params.tp_ceiling_db, sr=sr, check_cancel=p.check_cancel, in_place=True, scratch=scratch
        )
    if not tp_stats.get('ceiling_met', True):
        over = float(tp_stats['tp_after']) - params.tp_ceiling_db + 0.05
        gain = np.float32(10.0 ** (-over / 20.0))
        for s in blocks(len(x), p.check_cancel):
            x[s] *= gain
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
    _write_master(x, sr, staged, p.check_cancel)

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
    return {
        'integrated_lufs': final['integrated_lufs'],
        'true_peak_dbtp': final['true_peak_dbtp'],
        'lra': final['lra'],
        'input': input_metrics,
        'loudness_gain_db': round(gain_db, 2),
        'compressor': comp_stats,
        'deess': deess_stats,
        'adaptive_lowmid': adaptive_stats,
        'limiter': tp_stats,
        'steps': steps,
        'ai': ai_report,
        'warnings': warnings,
        'params': params.to_dict(),
        'duration_s': round(n / sr, 2),
        'wall_time_s': round(time.time() - t0, 2),
    }
