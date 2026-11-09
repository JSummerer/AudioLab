"""Import (task `import`): add an audio file from this computer to the Library as it is — a master
made elsewhere, to compare with AudioLab's versions. The file stays where it is (nothing is copied or
changed); it is decoded once to measure its loudness and true peak over the whole recording (and, for
long ones, how its tonal balance moves). Started only by POST /api/library/import."""

from pathlib import Path
from typing import Any

from ..analysis import profile as prof
from ..context import TaskContext
from ..errors import AudioLabError
from ..util import human_duration
from . import common

STAGES = [('acquire', 'Reading file', 5), ('decode', 'Preparing', 35), ('analyze', 'Measuring', 60)]


def validate(options: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    if source.get('kind') != 'file' or not source.get('path'):
        raise AudioLabError('Choose an audio file on this computer to add to the Library.')
    return {}


def stages(options: dict[str, Any], source: dict[str, Any]):
    return STAGES


def run(ctx: TaskContext, source: dict[str, Any]) -> dict[str, Any]:
    path = Path(source['path'])
    if not path.is_file():
        raise AudioLabError(
            f"This file isn't there any more: {path}.",
            hints=['It was moved, renamed or deleted, or its drive is not connected. Import it again.'],
        )
    # Measured like Check (the preflight's estimate fits); nothing is written to the output folder.
    acq = common.acquire(ctx, source, ctx.stage('acquire'), task='analyze')
    common.decode(ctx, acq, ctx.stage('decode'))
    p = ctx.stage('analyze', 'Measuring loudness and peaks…')
    analysis, x, _ = prof.analyze_file(common.reference_path(ctx), progress=p, **common.source_format(acq))
    del x
    common.describe_source(analysis, acq)
    lufs, peak = analysis.get('integrated_lufs'), analysis.get('true_peak_dbtp')
    duration = analysis.get('duration_s') or acq.duration_s
    if analysis.get('is_silent'):
        lufs = peak = None  # the meters' floor is not a reading

    ext = path.suffix[1:].lower()
    files = common.file_entries([{'path': str(path), 'label': f'Imported ({ext.upper()})', 'format': ext}])
    summary = [f'Added to the Library: {path.name} — it stays where it is: {path.parent}.']
    if lufs is not None and peak is not None:
        summary.append(f'Loudness {lufs:.1f} LUFS, peaks {peak:+.1f} dBTP ({human_duration(duration or 0)}).')
    else:
        summary.append('This file is silent — there is no loudness to measure.')
    return {
        'headline': f'Imported · {lufs:.1f} LUFS' if lufs is not None else 'Imported',
        'output_dir': str(path.parent),
        'files': files,
        'previews': [{'key': 'file:f0', 'label': 'Play'}] if files[0]['playable'] else [],
        'summary': summary,
        'metrics': {'before': {}, 'after': {'lufs': lufs, 'true_peak_dbtp': peak, 'lra': analysis.get('lra')}},
        'measured': {'integrated_lufs': lufs, 'true_peak_dbtp': peak, 'duration_s': duration},
        'analysis': analysis,
        'warnings': [],
        '_library': common.library_hints(acq),
    }
