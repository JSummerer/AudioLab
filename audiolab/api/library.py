"""Library routes: the finished results grouped by recording (or one entry), their files (players and
downloads, with Range; decoded chunks for the compare player), importing a file from this computer,
removing entries (optionally moving their files to the Trash), "keep only this version" and dropping
kept originals (one, or all) — see docs/API.md and library.py."""

import math
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from .. import library, processing, tasks
from ..errors import AudioLabError
from ..jobs import new_job_id
from ..media import export, ffmpeg
from ..settings import SETTINGS
from ..util import link_or_copy
from .jobs import _cover_dir, serve_file, serve_pcm
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


@router.get('/api/library/{entry_id}/tone-timeline')
def get_tone_timeline(entry_id: str):
    timeline = _entry(entry_id).get('tone_timeline')
    if not timeline:
        raise HTTPException(404, 'This version has no tone timeline.')
    return timeline


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
        elif key == 'eq':
            valid = enhance.eq_bands(value) is not None
        elif key == 'section_eq':
            from ..recipes.base import section_eq

            valid = section_eq(value) is not None
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
            raise HTTPException(400, 'This version has no saved bandwidth for a Brighten muffled highs preview.')
    return {
        'start_s': start,
        'duration_s': length,
        'recipe': {'key': recipe.key, 'name': recipe.name},
        'params': params,
        'bandwidth': bandwidth or {},
        'advanced': changes,
    }


def _entry_covers(entry: dict, folder: Path) -> dict[str, str]:
    """The cover this version already has, as source keys for an Apply job: `_cover`, the picture
    embedded in one of its files (so `embed_cover: false` without an own image stays without), and
    `_cover_original`, its full-size saved cover file for `save_cover`. The kept original is bare
    audio, so Apply would otherwise lose the cover."""
    keys: dict[str, str] = {}
    folder.mkdir(parents=True, exist_ok=True)
    for f in entry.get('files') or []:
        path = library.file_path(entry, f.get('id') or '')
        if path is None or not path.is_file():
            continue
        spec = export.FORMATS.get(f.get('format') or '')
        try:
            if f.get('kind') == 'cover':
                keys.setdefault('_cover_original', str(link_or_copy(path, folder / f'saved{path.suffix.lower()}')))
            elif '_cover' not in keys and spec and spec.cover and ffmpeg.probe_media(path).get('has_cover'):
                keys['_cover'] = str(export.cover_jpeg(path, folder / 'cover.jpg'))
        except (AudioLabError, OSError):  # a broken picture must not cost the new version
            continue
    return keys


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
        if not is_preview:
            source.update(_entry_covers(entry, _cover_dir(manager, job_id)))
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
def remove_entry(state: State, entry_id: str, trash_files: bool = False):
    try:
        removed = library.remove(entry_id, trash_files=trash_files)
    except AudioLabError as e:  # this system has no Trash: nothing was removed
        return _no_trash(e)
    if removed is None:
        raise HTTPException(404, 'This result is no longer in the Library.')
    state.forget('storage')
    return removed


@router.post('/api/library/{entry_id}/keep-only')
def keep_only(state: State, entry_id: str, body: Any = Body(None)):
    body = {} if body is None else body
    if not isinstance(body, dict) or body.keys() - {'trash_files'} or type(body.get('trash_files', False)) is not bool:
        raise HTTPException(400, 'Send {"trash_files": true} or {"trash_files": false}.')
    try:
        kept = library.keep_only(entry_id, trash_files=body.get('trash_files', False))
    except AudioLabError as e:
        return _no_trash(e)
    if kept is None:
        raise HTTPException(404, 'This result is no longer in the Library.')
    state.forget('storage')
    return kept


def _no_trash(e: AudioLabError) -> JSONResponse:
    return JSONResponse({'detail': str(e), 'hints': e.hints, 'fix': e.fix}, status_code=409)


@router.post('/api/library/import')
def import_file(state: State, body: Any = Body(None)):
    """Queue an `import` job for an audio file on this computer: it is measured where it is (no copy)
    and becomes a Library entry (tasks/importing.py)."""
    if not state.local:
        raise HTTPException(403, 'Files can only be imported when AudioLab runs on this computer.')
    if not isinstance(body, dict) or body.keys() - {'path', 'group', 'title'}:
        raise HTTPException(400, 'Send the path of an audio file to import.')
    raw, group, title = body.get('path'), body.get('group'), body.get('title')
    if not isinstance(raw, str) or not raw.strip() or '\0' in raw:
        raise HTTPException(400, 'Send the path of an audio file to import.')
    if group is not None and (not isinstance(group, str) or not group.strip() or len(group) > 1000):
        raise HTTPException(400, 'The recording to add this version to is not valid.')
    if title is not None and not isinstance(title, str):
        raise HTTPException(400, 'The title must be text.')
    path = Path(os.path.realpath(Path(raw.strip()).expanduser()))  # resolved: Trash later refuses a changed link
    _importable(path)
    title = ' '.join((title or '').split())[:200]
    source: dict[str, Any] = {
        'kind': 'file',
        'name': path.name,
        'path': str(path),
        'size_bytes': path.stat().st_size,
        'title': title or path.stem,
    }
    if group:
        source['_library_group'] = group.strip()
    opts = tasks.validate('import', {'meta': {'title': title}} if title else {}, source)
    job = state.manager.submit('import', source, opts, title=title or path.stem)
    return state.manager.public(job.id)


def _importable(path: Path) -> None:
    """400 (AudioLabError, with hints) unless `path` is a non-empty regular file with sound, outside
    AudioLab's own data folder (its kept originals and job files are not the user's to import)."""
    try:
        st = path.stat()
    except OSError:
        raise AudioLabError(
            f"This file isn't there: {path}.", hints=['Check the path, or choose the file again.']
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise AudioLabError(f'This is not a file: {path}.', hints=['Choose an audio file, not a folder.'])
    if st.st_size == 0:
        raise AudioLabError('This file is empty (0 bytes).', hints=['Choose another file.'])
    if Path(os.path.realpath(path)).is_relative_to(os.path.realpath(SETTINGS.data_dir)):
        raise AudioLabError(
            "This file belongs to AudioLab's own data folder.", hints=['Choose a file from your music folders.']
        )
    if not ffmpeg.probe_media(path).get('has_audio'):
        raise AudioLabError(
            "AudioLab can't find any audio in this file — it may be damaged, or it is not an audio file.",
            hints=['Check that it plays in a media player.'],
        )
