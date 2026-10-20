"""HTTP API: REST + Server-Sent Events + the web UI (contract: docs/API.md).

    app.py       create_app(), lifespan, error format, static UI
    security.py  Host/Origin checks, access token, upload size cap
    state.py     per-app runtime state (`app.state.audiolab`)
    jobs.py      /api/probe, /api/jobs…
    library.py   /api/library… (entries, files, PCM chunks, removal)
    status.py    ping, health, settings, storage, diagnostics, maintenance
    events.py    /api/events (SSE)

`audiolab.desktop` runs `create_app()` with uvicorn.
"""

from .app import create_app
from .security import is_loopback

__all__ = ['create_app', 'is_loopback']
