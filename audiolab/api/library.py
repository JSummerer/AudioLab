"""Library routes: the finished results grouped by recording (or one entry), their files (players and
downloads, with Range; decoded chunks for the compare player), removing entries and dropping kept
originals (one, or all) — see docs/API.md and library.py."""

import math
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from .. import library, processing, tasks
from ..jobs import new_job_id
from ..util import link_or_copy
from .jobs import serve_file, serve_pcm
from .state import State

router = APIRouter()


@router.get('/api/library')
def list_library(state: State):
    groups = library.groups(lambda job_id: state.manager.get(job_id) is not None)
    return {'groups': groups, 'count': sum(len(g['entries']) for g in groups)}


@router.get('/api/library/{entry_id}')
def get_entry(state: State, entry_id: str):
    entry = _entry(entry_id)
    return library.public(entry, state.manager.get(entry.get('job_id') or '') is not None)


def _entry(entry_id: str) -> dict:
    entry = library.get(entry_id)
    if entry is None:
        raise HTTPException(404, 'This result is no longer in the Library.')
    return entry


def _file(entry: dict, file_id: str) -> Path:
    p = library.file_path(entry, file_id)
    if p is None:
        raise HTTPException(404, 'File not found.')
    if not p.is_file():
        raise HTTPException(410, 'This file was moved or deleted.')
    return p


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _overrides(body: Any, is_preview: bool) -> dict:
    from ..tasks import enhance

    allowed = {'advanced', 'start_s', 'duration_s'} if is_preview else {'advanced'}
    if not isinstance(body, dict) or body.keys() - allowed or not isinstance(body.get('advanced', {}), dict):
        raise HTTPException(400, 'Send the changed settings as an advanced options object.')
    advanced = body.get('advanced', {})
    for key, value in advanced.items():
        if key in enhance.RANGES:
            valid = _finite(value)
        elif key in enhance.CHOICES:
            valid = value in enhance.CHOICES[key]
        elif key == 'sample_rate':
            valid = _finite(value) and value in enhance.SAMPLE_RATES
        elif key == 'model' and not is_preview:
            from ..ai import MODELS

            valid = isinstance(value, str) and (value == 'auto' or value in MODELS)
        else:
            valid = False
        if not valid:
            raise HTTPException(400, 'One of the changed settings is invalid.')
    return advanced


def _preview_spec(state: State, entry: dict, body: dict, options: dict, overrides: dict) -> dict:
    from ..recipes import RECIPES
    from ..tasks import preview

    start, length = body.get('start_s'), body.get('duration_s')
    if not _finite(start) or not 0 <= start < 1e9:
        raise HTTPException(400, 'Choose a valid preview start time.')
    if not _finite(length) or not preview.MIN_DURATION_S <= length <= preview.MAX_DURATION_S:
        raise HTTPException(400, 'Choose a preview length between 5 and 30 seconds.')
    duration = entry.get('duration_s')
    if _finite(duration) and duration > 0 and start + preview.MIN_AVAILABLE_S > duration + 1e-6:
        raise HTTPException(400, 'Choose a start time with at least one second of audio left.')
    details = entry['processing'].get('resolved') or {}
    params = details.get('params') or {}
    recipe = RECIPES.get((details.get('recipe') or {}).get('key'))
    if (
        not recipe
        or not {'target_lufs', 'tp_ceiling_db', 'volume_only'} <= params.keys()
        or params.get('volume_only') != (options['quality'] == 'light')
        or params.get('ai_stage', 'none') != 'none'
    ):
        raise HTTPException(400, 'This version has no saved settings for a short preview.')

    bandwidth = processing.preview_bandwidth(details.get('bandwidth'))
    if bandwidth is None:
        old_job = state.manager.get(entry.get('job_id') or '')
        if old_job:
            bandwidth = processing.preview_bandwidth(((old_job.result or {}).get('analysis') or {}).get('bandwidth'))
    changes = {key: options['advanced'][key] for key in overrides if key in options['advanced']}
    restore = changes.get('hf_restore', 'on' if params.get('hf_restore') else 'off')
    if options['quality'] != 'light' and restore == 'on':
        old_override = (entry['processing']['requested'].get('advanced') or {}).get('hf_restore')
        # An earlier request to restore highs that resolved to off found no lossy cutoff.
        if bandwidth is None and old_override == 'on' and params.get('hf_restore') is False:
            bandwidth = {'is_lossy_cutoff': False}
        if bandwidth is None or (bandwidth.get('is_lossy_cutoff') and not bandwidth.get('cutoff_hz')):
            raise HTTPException(400, 'This version has no saved bandwidth for a Restore highs preview.')
    return {
        'start_s': start,
        'duration_s': length,
        'recipe': {'key': recipe.key, 'name': recipe.name},
        'params': params,
        'bandwidth': bandwidth or {},
        'advanced': changes,
    }


def _enhance_original(state: State, entry_id: str, body: Any, is_preview: bool):
    entry = _entry(entry_id)
    original = library.file_path(entry, 'original')
    if original is None or not original.is_file():
        raise HTTPException(410, 'The kept original is no longer available.')
    saved = (entry.get('processing') or {}).get('requested') or {}
    if entry.get('task') != 'enhance' or not all(saved.get(k) for k in ('recipe', 'quality', 'formats')):
        raise HTTPException(400, 'This version has no saved Enhance settings.')
    overrides = _overrides(body, is_preview)
    if is_preview and saved['quality'] == 'best':
        raise HTTPException(400, 'Short previews are available for Volume only and Adaptive processing.')
    source = {
        'kind': 'file',
        'name': entry['original'].get('name') or f'{entry["title"]}{original.suffix}',
        'title': entry['title'],
        'duration_s': entry.get('duration_s'),
        'size_bytes': entry['original'].get('size_bytes'),
    }
    opts = tasks.validate(
        'enhance',
        {
            **saved,
            'advanced': {**(saved.get('advanced') or {}), **overrides},
            'meta': {'title': entry['title'], 'artist': entry.get('artist') or ''},
        },
        source,
    )
    if is_preview:
        opts['_preview'] = _preview_spec(state, entry, body, opts, overrides)
    else:
        source.update(
            _library_group=entry.get('group'),
            _library_source=entry.get('source'),
            _markers=entry.get('markers') or [],
            _markers_source=entry.get('markers_source'),
        )

    manager = state.manager
    job_id = new_job_id()
    try:
        # Retain an independent link for the queued attempt and its retries. Library cleanup
        # can unlink the original before the worker gets to this job.
        source['path'] = str(link_or_copy(original, manager.input_dir(job_id) / f'original{original.suffix}'))
        title = f'{entry["title"]} (Preview)' if is_preview else entry['title']
        manager.submit('enhance', source, opts, title=title, job_id=job_id)
    except FileNotFoundError:
        shutil.rmtree(manager.job_dir(job_id), ignore_errors=True)
        raise HTTPException(410, 'The kept original is no longer available.') from None
    except Exception:
        shutil.rmtree(manager.job_dir(job_id), ignore_errors=True)
        raise
    return manager.public(job_id)


@router.post('/api/library/{entry_id}/preview')
def preview_entry(state: State, entry_id: str, body: Any = Body(None)):
    return _enhance_original(state, entry_id, body, is_preview=True)


@router.post('/api/library/{entry_id}/enhance')
def enhance_entry(state: State, entry_id: str, body: Any = Body(None)):
    return _enhance_original(state, entry_id, body, is_preview=False)


@router.get('/api/library/{entry_id}/files/{file_id}')
def library_file(entry_id: str, file_id: str, download: bool = False):
    entry = _entry(entry_id)
    p = _file(entry, file_id)
    name = next((f.get('name') for f in library.public(entry)['files'] if f['id'] == file_id), None) or p.name
    return serve_file(p, name if download else None)


@router.get('/api/library/{entry_id}/pcm/{file_id}')
def library_pcm(state: State, entry_id: str, file_id: str, offset: int = 0, frames: int = 0):
    return serve_pcm(_file(_entry(entry_id), file_id), offset, frames, state.pcm_stop)


@router.post('/api/library/{entry_id}/reveal')
def reveal_entry(state: State, entry_id: str):
    entry = library.get(entry_id)
    if entry is None:
        raise HTTPException(404, 'This result is no longer in the Library.')
    return state.reveal(library.folder(entry))


@router.delete('/api/library/originals')
def clear_originals(state: State):
    state.forget('storage')
    return library.clear_originals()


@router.delete('/api/library/{entry_id}/original')
def drop_original(state: State, entry_id: str):
    dropped = library.drop_original(entry_id)
    if dropped is None:
        raise HTTPException(404, 'This result is no longer in the Library.')
    state.forget('storage')
    return dropped


@router.delete('/api/library/{entry_id}')
def remove_entry(state: State, entry_id: str):
    removed = library.remove(entry_id)
    if removed is None:
        raise HTTPException(404, 'This result is no longer in the Library.')
    state.forget('storage')
    return removed
