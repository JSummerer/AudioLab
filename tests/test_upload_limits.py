"""Bound the multipart stream before it can spool excessive temporary data."""

import asyncio
import dataclasses
import io
import random
import struct
import wave
import zlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

from audiolab.api import jobs, security
from audiolab.api.app import create_app
from audiolab.jobs import JobManager


def test_non_upload_requests_do_not_evaluate_upload_configuration(monkeypatch):
    monkeypatch.setattr(security, 'LIMITS', SimpleNamespace(max_upload_mb=float('nan')))
    client = TestClient(create_app(local=True), base_url='http://localhost')
    try:
        assert client.get('/api/ping').status_code == 200
        assert client.get('/').status_code == 200
    finally:
        client.close()


@pytest.mark.parametrize('declared_length', [False, True])
@pytest.mark.parametrize('size', [2047, 2048, 2049])
def test_request_budget_at_boundary(monkeypatch, declared_length, size):
    monkeypatch.setattr(security, 'LIMITS', SimpleNamespace(max_upload_mb=1024 / 1024**2))
    monkeypatch.setattr(security, 'COVER_MAX_MB', 0)
    monkeypatch.setattr(security, 'MULTIPART_OVERHEAD_BYTES', 1024)
    app = FastAPI()

    @app.post('/api/jobs')
    async def consume(request: security.Request):
        await request.body()
        return {'ok': True}

    # A raw-body consumer does not translate parser exceptions; the form route does.
    # This boundary check uses a receiver which deliberately handles the parser's error.
    @app.exception_handler(security.MultiPartException)
    async def parser_error(request, exc):
        return security.JSONResponse({'detail': exc.message}, status_code=400)

    security.install_guard(app, local=True, token=None)
    headers = {'host': 'localhost'}
    if declared_length:
        headers['content-length'] = str(size)
    with TestClient(app, base_url='http://localhost') as client:
        response = client.post('/api/jobs', content=iter([b'x' * size]), headers=headers)
    assert response.status_code == (200 if size <= 2048 else 413)


def test_stream_overflow_stops_before_parser_and_closes_spool(monkeypatch):
    monkeypatch.setattr(security, 'LIMITS', SimpleNamespace(max_upload_mb=1024 / 1024**2))
    monkeypatch.setattr(security, 'COVER_MAX_MB', 0)
    monkeypatch.setattr(security, 'MULTIPART_OVERHEAD_BYTES', 1024)
    app = FastAPI()
    files = []
    forwarded = 0
    write = StarletteUploadFile.write

    async def record_write(self, data):
        nonlocal forwarded
        if self not in files:
            files.append(self)
        forwarded += len(data)
        await write(self, data)

    monkeypatch.setattr(StarletteUploadFile, 'write', record_write)

    @app.post('/api/jobs')
    async def consume(file: UploadFile = File(...)):
        pytest.fail('An oversized multipart stream must never reach the route')

    security.install_guard(app, local=True, token=None)
    prefix = (
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="tiny.wav"\r\n'
        b'Content-Type: audio/wav\r\n\r\n'
    )
    chunks = [prefix + b'a' * 512, b'b' * 1024, b'c' * 1024, b'never-read\r\n--boundary--\r\n']
    messages = []
    consumed = 0

    async def receive():
        nonlocal consumed
        chunk = chunks[consumed]
        consumed += 1
        return {'type': 'http.request', 'body': chunk, 'more_body': consumed < len(chunks)}

    async def send(message):
        messages.append(message)

    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'http_version': '1.1',
        'method': 'POST',
        'scheme': 'http',
        'path': '/api/jobs',
        'raw_path': b'/api/jobs',
        'query_string': b'',
        'headers': [(b'host', b'localhost'), (b'content-type', b'multipart/form-data; boundary=boundary')],
        'client': ('127.0.0.1', 10000),
        'server': ('localhost', 80),
    }
    asyncio.run(app(scope, receive, send))
    assert messages[0]['status'] == 413
    assert consumed == 3  # no further receive after detecting the oversized chunk
    assert forwarded == 1536  # offending chunk was never given to multipart parsing
    assert files and all(f.file.closed for f in files)


def _wav():
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\0' * 800)
    return audio.getvalue()


def _png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    # A generated image just over the old 1 MiB request-overhead allowance.
    pixels = random.Random(7).randbytes(600 * 600 * 3)
    rows = b''.join(b'\0' + pixels[i : i + 1800] for i in range(0, len(pixels), 1800))
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 600, 600, 8, 2, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(rows))
        + chunk(b'IEND', b'')
    )


def _multipart(parts):
    body = bytearray()
    for name, filename, content in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        body.extend(f'--boundary\r\n{disposition}\r\n\r\n'.encode() + content + b'\r\n')
    return bytes(body) + b'--boundary--\r\n'


@pytest.mark.parametrize('chunked', [False, True])
@pytest.mark.parametrize('case', ['valid_cover', 'audio_limit', 'cover_limit', 'extra_parts'])
def test_real_upload_contract(monkeypatch, tmp_path, chunked, case):
    limits = dataclasses.replace(jobs.LIMITS, max_upload_mb=1024 / 1024**2)
    monkeypatch.setattr(jobs, 'LIMITS', limits)
    monkeypatch.setattr(security, 'LIMITS', limits)
    parts = [('task', None, b'download')]
    if case == 'audio_limit':
        parts.append(('file', 'large.wav', b'x' * 1025))
    else:
        parts.append(('file', 'small.wav', _wav()))
    if case == 'valid_cover':
        parts.append(('cover', 'cover.png', _png()))
    elif case == 'cover_limit':
        monkeypatch.setattr(jobs, 'COVER_MAX_MB', 1024 / 1024**2)
        monkeypatch.setattr(security, 'COVER_MAX_MB', 1024 / 1024**2)
        parts.append(('cover', 'cover.png', b'x' * 1025))
    elif case == 'extra_parts':
        monkeypatch.setattr(security, 'COVER_MAX_MB', 0)
        monkeypatch.setattr(security, 'MULTIPART_OVERHEAD_BYTES', 1024)
        parts.extend([('unused', 'extra.bin', b'x' * 600)] * 3)
    body = _multipart(parts)
    manager = JobManager(lambda job, ctx: {}, lambda job: [], root=tmp_path / 'jobs')
    # No lifespan or workers: exercise parsing, validation and submission only.
    client = TestClient(create_app(local=True, manager=manager), base_url='http://localhost')
    try:
        response = client.post(
            '/api/jobs',
            content=iter(body[i : i + 1024] for i in range(0, len(body), 1024)) if chunked else body,
            headers={'content-type': 'multipart/form-data; boundary=boundary'},
        )
    finally:
        client.close()
    if case == 'valid_cover':
        assert response.status_code == 200, response.text
        assert len(response.json()['jobs']) == 1
    else:
        assert response.status_code == 413, response.text
        assert manager.list() == []
        assert not list((tmp_path / 'jobs').glob('*/input/*'))
