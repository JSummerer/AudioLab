"""Library routes: the finished results grouped by recording (or one entry), their files (players and
downloads, with Range; decoded chunks for the compare player), removing entries and dropping kept
originals (one, or all) — see docs/API.md and library.py."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import library
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
