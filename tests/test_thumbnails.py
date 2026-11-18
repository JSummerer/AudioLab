"""Small fake transfers exercise thumbnail budgets and real job cancellation, without network."""

import io
import struct
import threading
import zlib

import pytest

from audiolab.context import Progress, TaskContext
from audiolab.errors import Cancelled, DownloadError
from audiolab.media import export, youtube
from audiolab.tasks import common


def tiny_png():
    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))

    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
        + chunk(b'tEXt', b'Comment\0' + b'small generated test picture' * 3)
        + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * 2) * 2))
        + chunk(b'IEND', b'')
    )


class Response:
    def __init__(self, chunks, length=None, after_read=lambda: None):
        self.chunks = list(chunks)
        self.headers = {} if length is None else {'Content-Length': length}
        self.after_read = after_read
        self.read_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size):
        raise AssertionError('HTTP streams should use read1, not fill a buffer across raw reads')

    def read1(self, size):
        self.read_calls += 1
        chunk = self.chunks.pop(0) if self.chunks else b''
        if len(chunk) > size:
            self.chunks.insert(0, chunk[size:])
            chunk = chunk[:size]
        self.after_read()
        return chunk


def connect(monkeypatch, response):
    calls = []

    def open_url(request, timeout):
        calls.append((request.full_url, timeout))
        return response

    monkeypatch.setattr(youtube.urllib.request, 'urlopen', open_url)
    return calls


@pytest.mark.parametrize('length', [None, '1', 'invalid'])
def test_thumbnail_counts_actual_bytes_and_accepts_cap_boundary(tmp_path, monkeypatch, length):
    picture = tiny_png()
    monkeypatch.setattr(youtube, '_THUMBNAIL_MAX_BYTES', len(picture))
    response = Response([picture[:80], picture[80:]], length=length)
    calls = connect(monkeypatch, response)
    result = youtube.fetch_thumbnail('https://example.test/cover.png', tmp_path / 'cover.src')
    assert result.read_bytes() == picture
    assert export.check_picture(result) == ('png', 2, 2)
    assert calls == [('https://example.test/cover.png', youtube._THUMBNAIL_SOCKET_TIMEOUT_S)]
    assert response.closed


@pytest.mark.parametrize('length', [None, '1'])
def test_thumbnail_rejects_actual_overflow_and_removes_only_its_file(tmp_path, monkeypatch, length):
    picture = tiny_png()
    monkeypatch.setattr(youtube, '_THUMBNAIL_MAX_BYTES', len(picture))
    response = Response([picture, b'!'], length=length)
    connect(monkeypatch, response)
    existing = tmp_path / 'cover.src'
    existing.write_bytes(b'user file')
    with pytest.raises(DownloadError, match='larger than 15 MB'):
        youtube.fetch_thumbnail('https://example.test/cover', existing)
    assert existing.read_bytes() == b'user file'
    assert list(tmp_path.iterdir()) == [existing]
    assert response.closed


def test_thumbnail_rejects_oversized_header_before_reading(tmp_path, monkeypatch):
    response = Response([], length=str(youtube._THUMBNAIL_MAX_BYTES + 1))
    connect(monkeypatch, response)
    with pytest.raises(DownloadError, match='larger than 15 MB'):
        youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src')
    assert response.read_calls == 0
    assert not list(tmp_path.iterdir())


def test_thumbnail_deadline_after_a_read_cleans_up(tmp_path, monkeypatch):
    now = [0]
    monkeypatch.setattr(youtube.time, 'monotonic', lambda: now[0])
    response = Response([tiny_png()], after_read=lambda: now.__setitem__(0, youtube._THUMBNAIL_TIMEOUT_S))
    connect(monkeypatch, response)
    with pytest.raises(DownloadError, match='too long'):
        youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src')
    assert response.read_calls == 1
    assert response.closed
    assert not list(tmp_path.iterdir())


def test_thumbnail_deadline_includes_connection_time(tmp_path, monkeypatch):
    now = [0]
    monkeypatch.setattr(youtube.time, 'monotonic', lambda: now[0])
    response = Response([tiny_png()])

    def open_url(*args, **kwargs):
        now[0] = youtube._THUMBNAIL_TIMEOUT_S
        return response

    monkeypatch.setattr(youtube.urllib.request, 'urlopen', open_url)
    with pytest.raises(DownloadError, match='too long'):
        youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src')
    assert not response.read_calls
    assert not list(tmp_path.iterdir())


def test_thumbnail_cancelled_before_connection(tmp_path, monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    calls = connect(monkeypatch, Response([]))
    with pytest.raises(Cancelled):
        youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src', Progress.null(cancelled))
    assert not calls
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('entrypoint', ['direct', 'best_effort', 'cover_art', 'cover_file'])
def test_thumbnail_midstream_cancel_propagates_and_cleans_up(tmp_path, monkeypatch, entrypoint):
    ctx = TaskContext('test', tmp_path / 'work', {'save_cover': True, 'embed_cover': True}, [])
    response = Response([b'x' * 100, b'y' * 100], after_read=ctx.request_cancel)
    connect(monkeypatch, response)
    source = {'thumbnail': 'https://example.test/cover'}
    acquired = common.Acquired(tmp_path / 'source.wav', 'Test', thumbnail=source['thumbnail'])
    with pytest.raises(Cancelled):
        if entrypoint == 'direct':
            youtube.fetch_thumbnail(source['thumbnail'], ctx.work_dir / 'cover.src', ctx.progress)
        elif entrypoint == 'best_effort':
            youtube.download_thumbnail(source['thumbnail'], ctx.work_dir / 'cover.jpg', ctx.progress)
        elif entrypoint == 'cover_art':
            common.cover_art(ctx, acquired)
        else:
            common.cover_file(ctx, source, acquired, tmp_path / 'out', 0)
    assert response.read_calls == 1
    assert response.closed
    assert not [p for p in tmp_path.rglob('*') if p.is_file()]


@pytest.mark.parametrize('body', [b'', b'not a picture' * 15])
def test_thumbnail_invalid_picture_is_cleaned(tmp_path, monkeypatch, body):
    connect(monkeypatch, Response([body]))
    with pytest.raises(DownloadError):
        youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src')
    assert not list(tmp_path.iterdir())


def test_thumbnail_network_failure_is_plain_and_cleaned(tmp_path, monkeypatch):
    def fail():
        raise OSError('connection lost')

    connect(monkeypatch, Response([b'x' * 100], after_read=fail))
    with pytest.raises(DownloadError, match='Could not get the cover image'):
        youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src')
    assert not list(tmp_path.iterdir())


def test_thumbnail_fallback_read_is_one_byte(tmp_path, monkeypatch):
    picture = tiny_png()

    class WithoutRead1(Response):
        read1 = None

        def read(self, size):
            assert size == 1
            return Response.read1(self, size)

    response = WithoutRead1([picture])
    connect(monkeypatch, response)
    result = youtube.fetch_thumbnail('https://example.test/cover', tmp_path / 'cover.src')
    assert result.read_bytes() == picture


def test_best_effort_cancellation_after_conversion_is_not_swallowed(tmp_path, monkeypatch):
    ctx = TaskContext('test', tmp_path / 'work', {}, [])
    connect(monkeypatch, Response([tiny_png()]))

    def convert(*args, **kwargs):
        ctx.request_cancel()
        return tmp_path / 'unused.jpg'

    monkeypatch.setattr(export, 'cover_jpeg', convert)
    with pytest.raises(Cancelled):
        youtube.download_thumbnail('https://example.test/cover', ctx.work_dir / 'cover.jpg', ctx.progress)
    assert not list(ctx.work_dir.iterdir())


def test_save_cover_passes_valid_small_thumbnail_to_output(tmp_path, monkeypatch):
    picture = tiny_png()
    connect(monkeypatch, Response([picture]))
    entry = common.save_cover({'thumbnail': 'https://example.test/cover'}, 'Test', tmp_path / 'out', 3)
    assert entry['id'] == 'f3'
    assert (tmp_path / 'out' / 'Test - cover.png').read_bytes() == picture


def test_thumbnail_success_preserves_an_existing_destination(tmp_path, monkeypatch):
    picture = tiny_png()
    monkeypatch.setattr(youtube.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(picture))
    existing = tmp_path / 'cover.src'
    existing.write_bytes(b'user file')
    result = youtube.fetch_thumbnail('https://example.test/cover', existing)
    assert result != existing
    assert existing.read_bytes() == b'user file'
    assert result.read_bytes() == picture
