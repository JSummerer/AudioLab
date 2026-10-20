"""Pinned release source acquisition, with fake transport and temporary files only."""

import hashlib
import importlib.util
import io
import socket
import ssl
import urllib.error
from concurrent.futures import Future
from pathlib import Path

import pytest


@pytest.fixture
def sources(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'packaging' / 'macos' / 'third_party.py'
    spec = importlib.util.spec_from_file_location('audiolab_packaging_sources', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = {
        'filename': 'original.tar.gz',
        'url': 'https://sources.example.test/original.tar.gz',
        'sha256': hashlib.sha256(b'original source').hexdigest(),
    }
    monkeypatch.setattr(module, 'artifacts', lambda groups: [row])
    sleeps = []
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    return module, row, sleeps


@pytest.mark.parametrize(
    'error',
    [
        urllib.error.URLError(TimeoutError('connect timed out')),
        ConnectionResetError('connection reset'),
        urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, 'temporary DNS failure')),
        urllib.error.HTTPError('https://sources.example.test', 503, 'unavailable', {}, None),
        urllib.error.HTTPError('https://sources.example.test', 429, 'too many requests', {}, None),
    ],
)
def test_temporary_download_failure_retries_same_pinned_source(sources, monkeypatch, tmp_path, capsys, error):
    module, row, sleeps = sources
    requests = []

    def open_source(request, timeout):
        requests.append((request.full_url, timeout))
        if len(requests) == 1:
            raise error
        return io.BytesIO(b'original source')

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_source)
    assert module.fetch('all', tmp_path) == [row]
    assert requests == [(row['url'], 120)] * 2
    assert sleeps == [1]
    assert (tmp_path / row['filename']).read_bytes() == b'original source'
    assert not list(tmp_path.glob('*.partial'))
    output = capsys.readouterr().out
    assert row['filename'] in output and row['url'] in output and 'attempt 1/3' in output


def test_interrupted_transfer_restarts_partial_file(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise TimeoutError('read timed out')
            return super().read(size)

    replies = iter([Interrupted(b'incomplete untrusted bytes'), io.BytesIO(b'original source')])
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: next(replies))
    module.fetch('all', tmp_path)
    assert sleeps == [1]
    assert (tmp_path / row['filename']).read_bytes() == b'original source'


def test_exhausted_retries_identify_failed_artifact_and_preserve_cause(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    calls = []
    error = urllib.error.URLError(TimeoutError('connect timed out'))

    def fail(*args, **kwargs):
        calls.append(None)
        raise error

    monkeypatch.setattr(module.urllib.request, 'urlopen', fail)
    with pytest.raises(RuntimeError) as caught:
        module.fetch('all', tmp_path)
    assert len(calls) == 3 and sleeps == [1, 2]
    assert caught.value.__cause__ is error
    assert row['filename'] in str(caught.value) and row['url'] in str(caught.value)
    assert 'attempt 3/3' in str(caught.value)
    assert not (tmp_path / row['filename']).exists()


@pytest.mark.parametrize(
    'error',
    [
        urllib.error.HTTPError('https://sources.example.test', 404, 'not found', {}, None),
        urllib.error.URLError(ssl.SSLCertVerificationError('certificate verification failed')),
        urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, 'host does not exist')),
    ],
)
def test_permanent_download_failure_does_not_retry(sources, monkeypatch, tmp_path, error):
    module, row, sleeps = sources
    calls = []

    def fail(*args, **kwargs):
        calls.append(None)
        raise error

    monkeypatch.setattr(module.urllib.request, 'urlopen', fail)
    with pytest.raises(RuntimeError, match='Source download failed') as caught:
        module.fetch('all', tmp_path)
    assert len(calls) == 1 and sleeps == []
    assert row['url'] in str(caught.value)
    assert not (tmp_path / row['filename']).exists()


def test_download_checksum_mismatch_is_retained_and_never_retried(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    calls = []

    def corrupt(*args, **kwargs):
        calls.append(None)
        return io.BytesIO(b'wrong source')

    monkeypatch.setattr(module.urllib.request, 'urlopen', corrupt)
    with pytest.raises(RuntimeError, match='Source checksum failed'):
        module.fetch('all', tmp_path)
    assert len(calls) == 1 and sleeps == []
    assert not (tmp_path / row['filename']).exists()
    assert (tmp_path / (row['filename'] + '.partial')).read_bytes() == b'wrong source'


@pytest.mark.parametrize('content', [b'original source', b'corrupt cached source'])
def test_cached_archive_is_verified_without_download(sources, monkeypatch, tmp_path, content):
    module, row, sleeps = sources
    target = tmp_path / row['filename']
    target.write_bytes(content)

    def unexpected(*args, **kwargs):
        pytest.fail('Cached archives must not trigger a network request')

    monkeypatch.setattr(module.urllib.request, 'urlopen', unexpected)
    if content == b'original source':
        assert module.fetch('all', tmp_path) == [row]
    else:
        with pytest.raises(RuntimeError, match='Source checksum failed'):
            module.fetch('all', tmp_path)
    assert target.read_bytes() == content and sleeps == []


def test_terminal_failure_cancels_queued_sources(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    futures = []
    monkeypatch.setattr(module, 'artifacts', lambda groups: [dict(row, filename=f'{i}.tar.gz') for i in range(10)])

    class Executor:
        def __init__(self, max_workers):
            assert max_workers == 4

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, function, row):
            future = Future()
            if not futures:
                future.set_exception(RuntimeError('Source download failed: first.tar.gz'))
            futures.append(future)
            return future

    monkeypatch.setattr(module, 'ThreadPoolExecutor', Executor)
    with pytest.raises(RuntimeError, match='first.tar.gz'):
        module.fetch('all', tmp_path)
    assert len(futures) == 10
    assert all(future.cancelled() for future in futures[1:])
    assert sleeps == []
