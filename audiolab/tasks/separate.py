"""Separate ("Split"): AI stem separation — vocals + instrumental (karaoke) or
four stems (vocals, drums, bass, other)."""

import math
from pathlib import Path
from typing import Any

from ..context import TaskContext
from ..errors import AudioLabError, ToolMissingError
from ..media import export
from ..runtime import is_packaged
from ..settings import SETTINGS
from ..util import sanitize_filename
from . import common

MODES = {'karaoke': 'Vocals + instrumental', 'stems4': '4 stems'}
STEM_LABELS = {
    'vocals': 'Vocals',
    'instrumental': 'Instrumental',
    'drums': 'Drums',
    'bass': 'Bass',
    'other': 'Other (melody, keys, synths)',
}


SAFE_PEAK = 10 ** (-0.3 / 20)  # −0.3 dBFS


def _scaled(src: Path, gain: float, dst: Path) -> Path:
    """Stream `src` × `gain` into a float WAV (RF64 when large)."""
    import soundfile as sf

    info = sf.info(str(src))
    with sf.SoundFile(
        str(dst), 'w', samplerate=info.samplerate, channels=info.channels, subtype='FLOAT', format='RF64'
    ) as out:
        for blk in sf.blocks(str(src), blocksize=1 << 18, dtype='float32', always_2d=True):
            out.write(blk * gain)
    return dst


def validate(options: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    if not common.ai_device():
        if is_packaged():
            raise ToolMissingError(
                'Split is not included in this core app.',
                hints=['This download supports Convert, Save, Check, and Light/Standard Enhance.'],
            )
        raise ToolMissingError(
            'Splitting audio needs the AI features, which are not installed yet.',
            hints=['Open Settings and click "Enable AI features" (one-time download).'],
            fix='install_ai',
        )
    mode = str(options.get('mode') or 'karaoke')
    if mode not in MODES:
        raise AudioLabError(f'Unknown split mode "{mode}".')
    fmt = str(options.get('format') or SETTINGS.get().separate_format)
    if fmt not in export.SEPARATE_FORMATS:
        fmt = 'wav16'
    from ..ai import MODELS

    model = str(options.get('model') or 'auto')
    if model not in MODELS:
        model = 'htdemucs'
    return {'mode': mode, 'format': fmt, 'model': model, 'save_cover': bool(options.get('save_cover'))}


def stages(options: dict[str, Any], source: dict[str, Any]):
    return [
        ('acquire', 'Getting audio', 15 if source.get('kind') == 'url' else 3),
        ('decode', 'Preparing', 5),
        ('separate', 'Separating with AI', 70),
        ('export', 'Saving stems', 10),
    ]


def run(ctx: TaskContext, source: dict[str, Any]) -> dict[str, Any]:
    from ..ai.separation import mix_stems, separate, stems_peak

    opts = ctx.options
    acq = common.acquire(ctx, source, ctx.stage('acquire'), task='separate', quality='best')
    common.decode(ctx, acq, ctx.stage('decode'))
    ref = common.reference_path(ctx)

    p = ctx.stage('separate', 'Loading the AI model…')
    sep = separate(ref, common.scratch_dir(ctx) / 'stems', model=opts['model'], progress=p)
    stems: dict[str, Path] = {k: Path(v) for k, v in sep['stems'].items()}
    if not sep.get('gate_passed', True):
        ctx.note(
            'warning', f'Separation check: the stems do not add up perfectly ({sep.get("residual_db")} dB residual).'
        )

    if opts['mode'] == 'karaoke':
        rest = [v for k, v in stems.items() if k != 'vocals']
        instrumental = mix_stems(rest, common.scratch_dir(ctx) / 'instrumental.wav')
        wanted = {'vocals': stems['vocals'], 'instrumental': instrumental}
    else:
        wanted = {k: stems[k] for k in ('vocals', 'drums', 'bass', 'other') if k in stems}

    # Stems are float and may peak above 0 dBFS (parts can cancel each other in the mix).
    # One shared gain for all stems keeps them clip-free AND still summing to the mix.
    peak = stems_peak(list(wanted.values()))
    if peak > SAFE_PEAK:
        gain = SAFE_PEAK / peak
        ctx.note(
            'decision',
            f'Turned all stems down by {20 * math.log10(1 / gain):.1f} dB together '
            f'so none of them clips (they still add up to the original).',
        )
        wanted = {k: _scaled(v, gain, common.scratch_dir(ctx) / f'safe_{k}.wav') for k, v in wanted.items()}

    p = ctx.stage('export', 'Saving the stems…')
    title = sanitize_filename(acq.title)
    folder = common.output_folder(f'{title} (Stems)', parent=common.output_root(acq))
    outputs = []
    for i, (name, path) in enumerate(wanted.items()):
        sub = p.sub(100 * i / len(wanted), 100 * (i + 1) / len(wanted))
        out = export.export(
            path,
            folder,
            f'{title} - {STEM_LABELS[name].split(" (")[0]}',
            [opts['format']],
            tags={
                'title': f'{acq.title} ({STEM_LABELS[name].split(" (")[0]})',
                **({'artist': acq.artist} if acq.artist else {}),
            },
            progress=sub,
        )
        for o in out:
            o['label'] = STEM_LABELS[name]
            o['stem'] = name
        outputs += out
    files = common.file_entries(outputs)
    media, previews = {}, []
    for f, o in zip(files, outputs):
        key = f'stem:{o["stem"]}'
        media[key] = f['path'] if f['playable'] else str(wanted[o['stem']])
        previews.append({'key': key, 'label': o['label'], 'file_id': f['id']})
    stems_saved = len(files)
    files += common.cover_file(ctx, source, acq, folder, start=len(files))

    device = str(sep.get('device', '')).upper()
    summary = [
        f'Split into {", ".join(STEM_LABELS[k].split(" (")[0].lower() for k in wanted)} '
        f'with AI ({sep.get("model")} on {device}).',
        f'Saved {stems_saved} files to {folder}.',
    ]
    warnings = []
    if not sep.get('gate_passed', True):
        warnings.append('Quality check: some parts may bleed between stems on this recording.')
    return {
        'headline': f'Split into {stems_saved} stems',
        'output_dir': str(folder),
        'files': files,
        'cover_source': common.cover_source(source),
        'previews': previews,
        'summary': summary,
        'warnings': warnings,
        '_media': media,
        '_library': common.library_hints(acq),  # no original kept: nothing compares stems with it
    }
