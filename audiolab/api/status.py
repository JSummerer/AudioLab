"""App-level routes: ping, health, settings, storage use, the diagnostics report, the
downloader update, the AI-pack install and opening the output folder — see docs/API.md."""

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from .. import ai, diagnostics, library
from ..errors import AudioLabError
from ..media import youtube
from ..settings import SETTINGS
from .state import AppState, State

router = APIRouter()


# ---------------------------------------------------------------- health
@router.get('/api/ping')
def ping():
    return {'ok': True}


@router.get('/api/health')
async def health(state: State):
    return await run_in_threadpool(state.health_data)


# -------------------------------------------------------------- settings
@router.get('/api/settings')
def get_settings():
    return SETTINGS.public()


@router.put('/api/settings')
def put_settings(patch: dict[str, Any] = Body(...)):
    patch = dict(patch or {})
    for key in SETTINGS.locked():  # nothing here may touch the old output folder: it can be unreachable
        patch.pop(key, None)
    if 'output_dir' in patch and patch['output_dir']:  # '' = back to the default folder
        target = Path(str(patch['output_dir']).strip()).expanduser()
        if not target.is_absolute():  # it would depend on the folder AudioLab was started from
            raise AudioLabError('Enter a full folder path, for example ~/Music/AudioLab.')
        try:
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target, prefix='.audiolab-', delete=True):
                pass
        except OSError as e:
            raise AudioLabError(
                f'AudioLab cannot write to {target}: {e.strerror or e}.',
                hints=['Pick a folder inside your home folder, e.g. ~/Music/AudioLab.'],
            ) from e
        patch['output_dir'] = str(target)
    try:
        SETTINGS.update(patch)
    except ValueError as e:
        raise AudioLabError(str(e)) from None
    return SETTINGS.public()


@router.post('/api/reveal-output')
def reveal_output(state: State):
    return state.reveal()


# --------------------------------------------------------------- storage
def _dir_bytes(path: Path, seen: set | None = None) -> int:
    """Bytes used below `path`; hard-linked files (re-runs share their input) count once."""
    seen = set() if seen is None else seen
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        total += _dir_bytes(Path(e.path), seen)
                    elif e.is_file(follow_symlinks=False):
                        # DirEntry's cached Windows metadata omits hard-link identity.
                        st = os.stat(e.path, follow_symlinks=False)
                        if st.st_nlink > 1:
                            if (st.st_dev, st.st_ino) in seen:
                                continue
                            seen.add((st.st_dev, st.st_ino))
                        total += st.st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _storage_data(state: AppState) -> dict[str, Any]:
    mgr = state.manager
    finished = {j['id'] for j in mgr.list() if j.get('status') in ('done', 'failed', 'cancelled')}
    total = finished_bytes = count = 0
    seen: set = set()  # a file is counted once: a kept original that is also a job's input counts as original
    originals = library.originals_usage(seen)
    jobs_dir = mgr.root
    if jobs_dir.is_dir():
        for entry in os.scandir(jobs_dir):
            if entry.is_dir(follow_symlinks=False):
                size = _dir_bytes(Path(entry.path), seen)
                total += size
                count += 1
                if entry.name in finished:
                    finished_bytes += size
    return {
        'jobs_bytes': total,
        'job_count': count,
        'finished_bytes': finished_bytes,
        'finished_count': len(finished),
        'models_bytes': _dir_bytes(ai.model_cache_dir()),
        'library_count': library.count(),
        'originals_bytes': originals['bytes'],
        'originals_count': originals['count'],
    }


@router.get('/api/storage')
async def storage(state: State):
    return await run_in_threadpool(state.cached, 'storage', lambda: _storage_data(state), 5)


# ----------------------------------------------------------- diagnostics
@router.get('/api/diagnostics', response_class=PlainTextResponse)
async def get_diagnostics(state: State, job: str | None = None):
    mgr = state.manager

    def build() -> str:
        one = journal = None
        if job:
            found = state.job_or_404(job)
            one, journal = mgr.public(found.id), mgr.journal(found.id)
        return diagnostics.build_report(state.health_data(), mgr.list(), one, journal or (), diagnostics.log_file())

    return PlainTextResponse(await run_in_threadpool(build))


# ----------------------------------------------------------- maintenance
@router.post('/api/system/update-downloader')
async def update_downloader(state: State):
    result = await run_in_threadpool(youtube.update_ytdlp)
    state.forget('ytdlp')
    state.system_event()
    return result


@router.post('/api/system/install-ai')
def install_ai(state: State):
    if ai.ai_status().get('available'):
        return {'ok': True, 'message': 'AI features are already installed.'}

    def done(ok: bool):
        ai.ai_status(refresh=True)
        state.system_event()

    started = state.installer.start(done)
    state.system_event()
    return {'ok': True, 'message': 'Installing…' if started else 'Already installing…'}
