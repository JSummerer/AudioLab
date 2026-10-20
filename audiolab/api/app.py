"""The FastAPI application: lifespan (logging, job workers, shutdown), error format,
the route modules and the static web UI."""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import APP_NAME, __version__, ai, diagnostics
from ..errors import AudioLabError, storage_error
from ..jobs import JobManager
from ..settings import SETTINGS
from . import events, jobs, library, status
from .security import install_guard
from .state import AppState

WEB_DIR = Path(__file__).resolve().parent.parent / 'web'
log = logging.getLogger('audiolab')


@asynccontextmanager
async def _lifespan(app: FastAPI):
    state: AppState = app.state.audiolab
    state.hub.loop = asyncio.get_running_loop()
    diagnostics.setup_logging()
    log.info(
        '%s %s starting (local=%s, token=%s, data=%s)',
        APP_NAME,
        __version__,
        state.local,
        bool(state.token),
        SETTINGS.data_dir,
    )
    unsubscribe = state.manager.subscribe(state.hub.publish)
    state.manager.start()
    threading.Thread(target=ai.ai_status, daemon=True).start()  # warm the (slow) torch import
    yield
    unsubscribe()
    state.shutdown()


async def _audiolab_error(request: Request, exc: AudioLabError):
    if exc.detail:  # the raw text behind it goes to the log (in the system report), not to the page
        log.info('%s %s: %s (%s)', request.method, request.url.path, exc, exc.detail)
    return JSONResponse({'detail': str(exc), 'hints': exc.hints, 'fix': exc.fix}, status_code=400)


async def _unexpected_error(request: Request, exc: Exception):
    """Anything a route didn't turn into a plain message: a full disk or a folder AudioLab may not
    write to in plain words, everything else as a short JSON 500 — the traceback goes to the log."""
    friendly = storage_error(exc) if isinstance(exc, OSError) else None
    if friendly is not None:
        log.warning('%s %s: %s (%s)', request.method, request.url.path, friendly, friendly.detail)
        body = {'detail': str(friendly), 'hints': friendly.hints, 'fix': None}
    else:
        log.error('%s %s failed', request.method, request.url.path, exc_info=exc)
        body = {
            'detail': f'Something went wrong in {APP_NAME}.',
            'hints': [
                'Try again.',
                'If it keeps happening, report it: Settings → About → "Copy system report" copies what is '
                'needed for a bug report.',
            ],
            'fix': None,
        }
    return JSONResponse(body, status_code=500)


def create_app(local: bool = True, token: str | None = None, manager: JobManager | None = None) -> FastAPI:
    """`local`: runs on this computer (folders can be opened, browser-safety checks when there
    is no token). `token`: required on /api requests when set. `manager`: inject a JobManager
    (tests); by default one is created for the data folder."""
    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        lifespan=_lifespan,
        docs_url='/api/docs',
        openapi_url='/api/openapi.json',
        redoc_url=None,
    )
    app.state.audiolab = AppState(local, token, manager)
    install_guard(app, local, token)
    app.add_exception_handler(AudioLabError, _audiolab_error)
    app.add_exception_handler(OSError, _unexpected_error)  # answered here, not re-raised to the server
    app.add_exception_handler(Exception, _unexpected_error)  # last resort (Starlette re-raises it after answering)
    for module in (status, jobs, library, events):
        app.include_router(module.router)
    if WEB_DIR.is_dir():
        app.mount('/', StaticFiles(directory=str(WEB_DIR), html=True), name='web')  # last: catches everything else
    return app
