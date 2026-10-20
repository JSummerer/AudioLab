"""Task registry. A task module provides:

    validate(options, source) -> normalised options   (raise AudioLabError for bad input;
                                                         call tasks.validate(), which adds `meta`)
    stages(options, source)   -> [(key, label, weight), ...]
    run(ctx, source)          -> Result dict (see docs/API.md); internal keys start with '_'

Modules are imported lazily so e.g. a plain download never loads the DSP/AI stack.
"""

import importlib
from typing import Any

TASKS = ('download', 'enhance', 'separate', 'analyze')
LABELS = {'download': 'Save', 'enhance': 'Enhance', 'separate': 'Split', 'analyze': 'Check'}


def get(task: str):
    if task not in TASKS:
        from ..errors import AudioLabError

        raise AudioLabError(f'Unknown task "{task}".')
    return importlib.import_module(f'{__name__}.{task}')


def clean_meta(raw: Any) -> dict[str, str]:
    """User overrides for names/tags: {"title": "...", "artist": "..."} (any task)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ('title', 'artist'):
        val = raw.get(key)
        if isinstance(val, str) and (val.strip() or key == 'artist'):
            out[key] = ' '.join(val.split())[:200]  # artist '' = "no artist"
    return out


def validate(task: str, options: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Task-specific validation plus the options every task understands (`meta`)."""
    norm = get(task).validate(options, source)
    meta = clean_meta(options.get('meta'))
    if meta:
        norm['meta'] = meta
    return norm


def stages_for(job) -> list:
    return list(get(job.task).stages(job.options, job.source))


def run_job(job, ctx) -> dict[str, Any]:
    return get(job.task).run(ctx, job.source)
