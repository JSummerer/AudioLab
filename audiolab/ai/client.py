"""Cancellable calls to the installed, independently packaged AI helper."""

import json
from pathlib import Path

from ..context import Progress
from ..errors import AudioLabError
from ..media import proc
from . import DEFAULT_MODEL, MODELS
from .managed import managed_ai


def separate_managed(ref: Path, out_dir: Path, model: str, shifts: int, progress: Progress) -> dict:
    manager = managed_ai()
    out_dir = Path(out_dir).resolve()
    result = None
    error = None

    def receive(line: str):
        nonlocal result, error
        if len(line) > 16000:
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return  # library diagnostics may share the stream
        if not isinstance(event, dict):
            return
        if event.get('type') == 'progress':
            pct = float(event.get('pct', 0))
            progress(pct, str(event.get('message') or '')[:500])
        elif event.get('type') == 'result' and event.get('protocol') == 1:
            result = event.get('result')
        elif event.get('type') == 'error':
            error = str(event.get('message') or '')[:1000]

    with manager.lease(progress) as (directory, lock):
        command = [
            str(directory / 'AudioLabAI'),
            '--separate',
            str(Path(ref).resolve()),
            str(out_dir),
            model if model in MODELS else DEFAULT_MODEL,
            str(max(1, min(10, int(shifts)))),
        ]
        completed = proc.run(
            command,
            progress=progress,
            on_line=receive,
            capture_stdout=False,
            env=manager.environment(directory),
            inherited_fds=(lock.fileno,),
            what='AI separation',
        )
    if completed.returncode != 0 or not isinstance(result, dict):
        raise AudioLabError(
            'The AI job could not finish.',
            hints=['Try a shorter recording. If it fails again, repair the AI add-on in Settings.'],
            detail=error or completed.stderr_tail,
        )
    # A damaged/mismatched helper must not send arbitrary files into the export path.
    stems = result.get('stems')
    if not isinstance(stems, dict) or set(stems) != {'vocals', 'drums', 'bass', 'other'}:
        raise AudioLabError('The AI helper returned incomplete results.', fix='retry')
    for name, path in stems.items():
        expected = out_dir / f'{name}.wav'
        if Path(path).resolve() != expected or expected.is_symlink() or not expected.is_file():
            raise AudioLabError('The AI helper returned invalid result files.', fix='retry')
    return result
