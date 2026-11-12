"""Render a short Enhance comparison from the Library's kept original."""

import shutil
from pathlib import Path

import soundfile as sf

from ..analysis.levels import SILENCE_DBFS
from ..context import TaskContext
from ..dsp.limiter import true_peak_db
from ..dsp.loudness import lra, programme_loudness
from ..errors import AudioLabError
from ..media import export, ffmpeg
from ..previews import make_previews
from ..processing import report_details
from ..recipes.base import ChainParams, preview_params
from ..recipes.calibration import for_pair
from ..recipes.chain import render
from ..resources import preflight
from . import common

MIN_DURATION_S = 5
MAX_DURATION_S = 30
MIN_AVAILABLE_S = 1
_CONTEXT_S = 20
_ESTIMATE = 'This is a short preview. Levels and processing may differ when AudioLab uses the whole recording.'


def stages():
    return [
        ('decode', 'Reading the selected passage', 15),
        ('process', 'Enhancing the passage', 70),
        ('preview', 'Making the comparison', 15),
    ]


def _comparison_metrics(path: str, check_cancel) -> dict:
    audio, sr = sf.read(path, dtype='float32', always_2d=True)
    peak = round(true_peak_db(audio, check_cancel=check_cancel), 2)
    return {
        'lufs': None if peak < SILENCE_DBFS else round(programme_loudness(audio, sr=sr, check_cancel=check_cancel), 2),
        'true_peak_dbtp': peak,
        'lra': round(lra(audio, sr=sr, check_cancel=check_cancel), 2),
    }


def _excerpt_processing(report: dict, offset_s: float, duration_s: float) -> dict:
    """Keep renderer measurements, but express visible timeline highlights in excerpt time."""
    details = report_details(report)
    stop_s = offset_s + duration_s
    for stage in details['applied'].values():
        if not isinstance(stage, dict):
            continue
        if timeline := stage.get('timeline'):
            timeline['duration_s'] = duration_s
            timeline['bins'] = [
                {
                    **item,
                    'start_s': max(0.0, item['start_s'] - offset_s),
                    'end_s': min(duration_s, item['end_s'] - offset_s),
                    'peak_s': item['peak_s'] - offset_s,
                }
                for item in timeline['bins']
                if offset_s <= item['peak_s'] < stop_s
            ]
        if 'events' in stage:
            stage['events'] = [
                {**item, 'start_s': item['start_s'] - offset_s, 'end_s': item['end_s'] - offset_s}
                for item in stage['events']
                if offset_s <= item['start_s'] < item['end_s'] <= stop_s
            ]
    return details


def _render_section(reference, master, params, profile, work, progress, calibration, context_first):
    # Section times are the recording's; this reference starts at context_first.
    shift = context_first / ffmpeg.PCM_RATE
    params.section_eq = [
        {**sec, 'start_s': sec['start_s'] - shift, 'end_s': sec['end_s'] - shift} for sec in params.section_eq
    ]
    return render(reference, master, params, profile, work, progress=progress, calibration=calibration)


def run(ctx: TaskContext, source: dict) -> dict:
    opts = ctx.options
    spec = opts['_preview']
    p = ctx.stage('decode')
    sr = ffmpeg.PCM_RATE
    first = round(spec['start_s'] * sr)
    saved_export = spec.get('saved_export')
    paired = spec.get('paired', False)
    # Start the input seek on the same resampling grid as compare playback. The selected
    # passage itself can begin on any sample within this larger window.
    context_first = max(0, first - _CONTEXT_S * sr)
    context_first += -context_first % ffmpeg.PCM_GRID
    if saved_export:
        context_first = first - first % ffmpeg.PCM_GRID
    offset = first - context_first
    requested = round(spec['duration_s'] * sr)
    decode_s = (offset + requested) / sr + (0 if saved_export else _CONTEXT_S)
    ctx.note('decision', 'Resource check passed', preflight(decode_s, ctx.work_dir, 'enhance', opts['quality']))
    reference = common.reference_path(ctx)
    decoded = ffmpeg.decode_reference(
        Path(source['path']), reference, progress=p, excerpt=(context_first / sr, decode_s)
    )
    length = min(requested, decoded['frames'] - offset)
    if length < MIN_AVAILABLE_S * sr:
        raise AudioLabError('Choose a start time with at least one second of audio left.')
    profile = {'bandwidth': spec['bandwidth']}
    recipe = spec['recipe']
    saved = ChainParams(**spec['params'])
    params = (
        ChainParams(**spec['draft_params'])
        if 'draft_params' in spec
        else preview_params(recipe['key'], ChainParams(**spec['params']), profile, spec['advanced'])
    )
    baseline_master = baseline_report = basis = None
    if saved_export:
        master = common.scratch_dir(ctx) / 'saved-export.wav'
        path = Path(saved_export['path'])
        if not path.is_file():
            raise AudioLabError('The saved version was moved or deleted. Choose another version to preview.')
        decoded_export = ffmpeg.decode_reference(
            path,
            master,
            progress=ctx.stage('process', 'Reading the saved version'),
            excerpt=(context_first / sr, decode_s),
        )
        length = min(length, decoded_export['frames'] - offset)
        if length < MIN_AVAILABLE_S * sr:
            raise AudioLabError('Choose a start time with at least one second of saved audio left.')
        report = {'params': spec['params'], **spec['applied']}
        processing_offset = first / sr  # saved timelines describe the whole recording
        label = export.FORMATS[saved_export['format']].label
        summary = (
            f'Playing the saved {label} version with its full-recording processing. No new processing was applied.'
        )
    else:
        work = common.scratch_dir(ctx)
        master = work / 'preview-master.flac'
        p = ctx.stage('process')
        saved_cal, draft_cal, basis = for_pair(saved, params, spec.get('calibration'), sr)
        identical = saved == params
        if paired and not identical:
            baseline_master = work / 'baseline-master.flac'
            baseline_report = _render_section(
                reference, baseline_master, saved, profile, work, p.sub(0, 50), saved_cal, context_first
            )
        report = _render_section(
            reference, master, params, profile, work, p.sub(50, 100) if baseline_master else p, draft_cal, context_first
        )
        if paired and identical:
            baseline_master, baseline_report = master, report
        processing_offset = offset / sr
        summary = _ESTIMATE

    p = ctx.stage('preview')
    out_dir = common.previews_dir(ctx)
    try:
        pv = make_previews(
            reference, master, out_dir, length / sr, p.check_cancel, start_frame=offset, saved_master=baseline_master
        )
        duration_s = pv['frames'] / sr if paired else pv['duration_s']
        labels = {'original': 'Original', 'enhanced': 'Edited settings' if paired else 'Enhanced'}
        if paired:
            labels['saved'] = 'Saved settings (re-rendered)'
        media = {key: pv[key] for key in labels}
        metrics = {
            name: _comparison_metrics(media[key], p.check_cancel)
            for key, name in (('original', 'before'), ('enhanced', 'after'), ('saved', 'saved'))
            if key in media
        }
        excerpt = {'start_s': round(first / sr, 3), 'duration_s': duration_s}
        if paired:
            excerpt = {
                'start_s': first / sr,
                'duration_s': pv['frames'] / sr,
                'start_frame': first,
                'frames': pv['frames'],
                'sample_rate_hz': sr,
            }
        if not saved_export and 'draft_params' in spec:
            # Keep settings in recording time. Only the measurement timeline is crop-relative.
            report = {**report, 'params': spec['draft_params']}
        details = _excerpt_processing(report, processing_offset, duration_s)
        p(100)
        return {
            'preview': True,
            'preview_basis': 'saved_export' if saved_export else 'section_estimate',
            'headline': 'Preview ready',
            'files': [],
            'previews': [{'key': key, 'label': label} for key, label in labels.items()],
            'excerpt': excerpt,
            'recipe': recipe,
            'processing': details,
            **(
                {
                    'comparison_basis': 'section_gain' if basis['gain'] == 'section' else 'retained_gain',
                    'calibration': basis,
                    'baseline_processing': _excerpt_processing(
                        {**baseline_report, 'params': spec['params']}, processing_offset, duration_s
                    ),
                    'shared_gain_db': pv['shared_gain_db'],
                }
                if paired
                else {}
            ),
            'metrics': metrics,
            'summary': [summary],
            'warnings': list(report.get('warnings') or []),
            '_media': media,
        }
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
