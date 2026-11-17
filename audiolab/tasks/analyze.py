"""Analyze ("Check"): loudness, peaks, dynamics, real bandwidth, content
type, BPM, musical key and a spectrogram — measured, explained, nothing changed."""

import json
from typing import Any

from ..analysis import classify
from ..analysis import profile as prof
from ..context import TaskContext
from ..media import ffmpeg
from ..util import human_duration
from . import common

STAGES = [('acquire', 'Getting audio', 45), ('decode', 'Preparing', 20), ('analyze', 'Measuring', 35)]


def validate(options: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return {}


def stages(options: dict[str, Any], source: dict[str, Any]):
    return STAGES


def describe(analysis: dict[str, Any], kind: dict[str, Any]) -> list[str]:
    """Plain-language findings for novices."""
    if analysis.get('is_silent'):
        return ['This file is silent — there is nothing to measure.']
    out = []
    if kind:
        out.append(f'Sounds like: {kind["label"]} ({kind.get("confidence_label", "medium")} confidence).')
    lufs, tp, lra = analysis.get('integrated_lufs'), analysis.get('true_peak_dbtp'), analysis.get('lra')
    if lufs is not None:
        label = analysis.get('loudness_label') or prof.loudness_label(lufs)
        out.append(f'Overall loudness is {label.lower()} ({lufs:.1f} LUFS).')
    if analysis.get('is_clipping'):
        out.append('The peak detector found runs of samples near its reference level.')
    if tp is not None:
        limit = analysis.get('peak_limit_dbtp')
        relationship = 'above' if prof.peaks_too_hot(tp, limit) else 'within'
        out.append(f'Peaks are {relationship} the check limit ({tp:+.2f} dBTP).')
    if lra is not None and (analysis.get('duration_s') or 0) >= 15:  # LRA needs more than a few seconds
        label = analysis.get('loudness_range_label') or prof.loudness_range_label(lra)
        out.append(f'Loudness range is {label.lower()} ({lra:.1f} LU).')
    bw = analysis.get('bandwidth') or {}
    if bw.get('verdict'):
        out.append(bw['verdict'])
    key = analysis.get('key')
    if key:
        out.append(
            f'Key (estimate): {key["name"]} · {key["camelot"]} (Camelot)'
            + (' — low confidence.' if key.get('confidence', 0) < 0.4 else '.')
        )
    return out


def run(ctx: TaskContext, source: dict[str, Any]) -> dict[str, Any]:
    acq = common.acquire(ctx, source, ctx.stage('acquire'), task='analyze')
    common.decode(ctx, acq, ctx.stage('decode'))
    ref = common.reference_path(ctx)

    p = ctx.stage('analyze', 'Measuring loudness, peaks and frequency range…')
    analysis, x, sr = prof.analyze_file(ref, progress=p.sub(0, 60), **common.source_format(acq))
    features = classify.audio_features(x, sr, check_cancel=p.check_cancel, known_lra=analysis.get('lra'))
    kind = classify.classify(features, acq.duration_s or analysis.get('duration_s') or 0, acq.metadata)
    analysis['bpm'] = kind.get('bpm')
    common.describe_source(analysis, acq)
    if kind.get('type') == 'speech':
        analysis['key'] = None
    p(70, 'Drawing the spectrogram…')
    spectro = common.previews_dir(ctx) / 'spectrogram.png'
    media = {}
    try:
        ffmpeg.render_spectrogram(acq.path, spectro, progress=p.sub(70, 100))
        media['spectrogram'] = str(spectro)
    except Exception as e:  # a missing picture must not fail the analysis
        ctx.note('warning', f'Spectrogram skipped: {e}')

    summary = describe(analysis, kind)
    report = common.previews_dir(ctx) / 'report.json'
    report.write_text(
        json.dumps(
            {
                'title': acq.title,
                'source': acq.url or acq.path.name,
                'analysis': analysis,
                'classification': kind,
                'summary': summary,
            },
            indent=2,
            default=str,
        ),
        encoding='utf-8',
    )
    files = common.file_entries([{'path': str(report), 'label': 'Report (JSON)', 'format': 'json', 'kind': 'report'}])
    return {
        'headline': f'{"Silent" if analysis.get("is_silent") else kind["label"]} · {human_duration(acq.duration_s or 0)}',
        'output_dir': None,
        'files': files,
        'previews': [],
        'summary': summary,
        'classification': kind,
        'analysis': analysis,
        'spectrogram': 'spectrogram' if 'spectrogram' in media else None,
        'markers': common.chapters(acq),
        **common.link_result(acq),
        'warnings': [a['message'] for a in analysis.get('alerts', []) if a.get('level') in ('warning', 'danger')],
        '_media': media,
    }
