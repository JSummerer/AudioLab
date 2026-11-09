"""Render a short Enhance comparison from the Library's kept original."""

import shutil
from pathlib import Path

import soundfile as sf

from ..context import TaskContext
from ..dsp.limiter import true_peak_db
from ..dsp.loudness import lra, programme_loudness
from ..errors import AudioLabError
from ..media import ffmpeg
from ..previews import make_previews
from ..processing import report_details
from ..recipes.base import ChainParams, preview_params
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
    return {
        'lufs': round(programme_loudness(audio, sr=sr, check_cancel=check_cancel), 2),
        'true_peak_dbtp': round(true_peak_db(audio, check_cancel=check_cancel), 2),
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


def run(ctx: TaskContext, source: dict) -> dict:
    opts = ctx.options
    spec = opts['_preview']
    p = ctx.stage('decode')
    sr = ffmpeg.PCM_RATE
    first = round(spec['start_s'] * sr)
    # Start the input seek on the same resampling grid as compare playback. The selected
    # passage itself can begin on any sample within this larger window.
    context_first = max(0, first - _CONTEXT_S * sr)
    context_first += -context_first % ffmpeg.PCM_GRID
    offset = first - context_first
    requested = round(spec['duration_s'] * sr)
    decode_s = (offset + requested) / sr + _CONTEXT_S
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
    params = preview_params(recipe['key'], ChainParams(**spec['params']), profile, spec['advanced'])
    shift = context_first / sr  # section times are the recording's; this reference starts here
    params.section_eq = [
        {**sec, 'start_s': sec['start_s'] - shift, 'end_s': sec['end_s'] - shift} for sec in params.section_eq
    ]
    master = common.scratch_dir(ctx) / 'preview-master.flac'
    report = render(reference, master, params, profile, common.scratch_dir(ctx), progress=ctx.stage('process'))

    p = ctx.stage('preview')
    out_dir = common.previews_dir(ctx)
    try:
        pv = make_previews(reference, master, out_dir, length / sr, p.check_cancel, start_frame=offset)
        duration_s = pv['duration_s']
        media = {key: pv[key] for key in ('original', 'enhanced')}
        metrics = {
            'before': _comparison_metrics(media['original'], p.check_cancel),
            'after': _comparison_metrics(media['enhanced'], p.check_cancel),
        }
        p(100)
        return {
            'preview': True,
            'headline': 'Preview ready',
            'files': [],
            'previews': [{'key': key, 'label': key.capitalize()} for key in media],
            'excerpt': {'start_s': round(first / sr, 3), 'duration_s': duration_s},
            'recipe': recipe,
            'processing': _excerpt_processing(report, offset / sr, duration_s),
            'metrics': metrics,
            'summary': [_ESTIMATE],
            'warnings': list(report.get('warnings') or []),
            '_media': media,
        }
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
