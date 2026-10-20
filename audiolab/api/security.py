"""Request guard: local-mode browser safety (Host/Origin checks), the optional access
token and the upload size cap, applied to every request before it reaches a route."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import APP_NAME
from ..settings import LIMITS

LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '[::1]', '::1'}
COVER_MAX_MB = 15
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


def _hostname(hostport: str) -> str:
    hostport = (hostport or '').strip().lower()
    if hostport.startswith('['):
        return hostport[: hostport.find(']') + 1]
    return hostport.rsplit(':', 1)[0] if hostport.count(':') == 1 else hostport


def is_loopback(host: str) -> bool:
    """True for a loopback address or name, with or without a port ('127.0.0.1', 'localhost:8765', '[::1]:80')."""
    return _hostname(host) in LOOPBACK_HOSTS


def install_guard(app: FastAPI, local: bool, token: str | None) -> None:
    """Register the guard before routes parse or spool request bodies."""
    app.add_middleware(_Guard, local=local, token=token)


class _Guard:
    def __init__(self, app: ASGIApp, local: bool, token: str | None):
        self.app = app
        self.local = local
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        refusal = None
        if self.local and not self.token:
            # Without a token, refuse what a malicious web page could do to a local AudioLab:
            # DNS rebinding (a foreign name pointing at 127.0.0.1) and cross-site requests (CSRF).
            # (With a token both are impossible anyway — and reverse proxies keep working.)
            host = request.headers.get('host', '')
            if not is_loopback(host):
                refusal = JSONResponse(
                    {'detail': f'Open {APP_NAME} via http://127.0.0.1 or http://localhost.'}, status_code=403
                )
            origin = request.headers.get('origin')
            if origin and path.startswith('/api') and origin.lower() != f'{request.url.scheme}://{host}'.lower():
                refusal = JSONResponse({'detail': 'Requests from other websites are not allowed.'}, status_code=403)
        if self.token and path.startswith('/api') and path != '/api/ping':
            given = request.headers.get('x-audiolab-token') or request.query_params.get('token')
            if given != self.token:
                refusal = JSONResponse({'detail': 'Access token required.'}, status_code=401)
        if refusal is not None:
            await refusal(scope, receive, send)
            return

        upload = request.method == 'POST' and path == '/api/jobs'
        limit = 0
        too_large = JSONResponse(
            {'detail': 'The upload request is too large. Send one audio file and one cover image at a time.'},
            status_code=413,
        )
        if upload:
            limit = int((LIMITS.max_upload_mb + COVER_MAX_MB) * 1024 * 1024) + MULTIPART_OVERHEAD_BYTES
            # Content-Length is only an early optimization; chunked bodies get the same bound below.
            try:
                length = int(request.headers.get('content-length') or 0)
            except ValueError:
                length = 0
            if length > limit:
                await too_large(scope, receive, send)
                return

        received = 0
        exceeded = False
        refused = False

        async def bounded_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if upload and message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > limit:
                    exceeded = True
                    # Supported older Starlette versions close multipart temp files specifically
                    # on MultiPartException. Do not forward the offending bytes to the parser.
                    raise MultiPartException('Upload request limit exceeded.')
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal refused
            if exceeded:
                # request.form() translates MultiPartException into 400. The route parses its
                # entire form before responding, so replace that response with our precise 413.
                if not refused:
                    refused = True
                    await too_large(scope, receive, send)
                return
            if message['type'] == 'http.response.start' and not path.startswith('/api'):
                MutableHeaders(scope=message)['Cache-Control'] = 'no-cache'
            await send(message)

        await self.app(scope, bounded_receive, guarded_send)
