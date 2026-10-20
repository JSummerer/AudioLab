"""Analyze ("Check quality"): loudness, peaks, dynamics, real bandwidth, content
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
        loud = 'very loud' if lufs > -9 else 'loud' if lufs > -12 else 'normal' if lufs > -17 else 'quiet'
        out.append(f'Overall volume is {loud} ({lufs:.1f} LUFS).')
    if analysis.get('is_clipping'):
        out.append('The recording is clipped — some loud peaks are flattened, and that distortion cannot be undone.')
    elif tp is not None and tp > -0.5:
        out.append('Peaks go above the safe limit — it may distort on some speakers.')
    elif tp is not None:
        out.append('Peaks stay below the safe limit.')
    if lra is not None and (analysis.get('duration_s') or 0) >= 15:  # LRA needs more than a few seconds
        out.append(
            'Very even volume (heavily compressed).'
            if lra < 3
            else 'Wide dynamics — quiet and loud parts differ a lot.'
            if lra > 9
            else 'Natural, balanced dynamics.'
        )
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
        'warnings': [a['message'] for a in analysis.get('alerts', []) if a.get('level') in ('warning', 'danger')],
        '_media': media,
    }
