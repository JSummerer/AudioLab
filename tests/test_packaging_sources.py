"""Pinned release source acquisition, with fake transport and temporary files only."""

import gzip
import hashlib
import importlib.util
import io
import socket
import ssl
import tarfile
import urllib.error
from concurrent.futures import Future
from pathlib import Path

import pytest


@pytest.fixture
def sources(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'packaging' / 'macos' / 'third_party.py'
    monkeypatch.syspath_prepend(str(path.parent))
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


def _tar_bytes(entries, mtime):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for info, content in entries:
            info.mtime = mtime
            info.pax_headers = {**info.pax_headers, 'mtime': f'{mtime}.125'}
            info.uid, info.gid = 501, 20
            info.uname, info.gname = 'source-owner', 'source-group'
            info.size = len(content) if info.isfile() else 0
            archive.addfile(info, io.BytesIO(content) if info.isfile() else None)
    return gzip.compress(output.getvalue(), mtime=mtime)


def _example_archive(mtime, change=None):
    source = tarfile.TarInfo('renamed.py' if change == 'path' else 'source.py')
    source.mode = 0o755 if change == 'mode' else 0o644
    other = tarfile.TarInfo('other.py')
    link = tarfile.TarInfo('shortcut')
    link.type = tarfile.SYMTYPE
    link.linkname = 'other.py' if change == 'link' else 'source.py'
    return _tar_bytes(
        [(source, b'changed' if change == 'content' else b'source bytes'), (other, b'other bytes'), (link, b'')],
        mtime,
    )


def _normalized_pin(module, row, tmp_path, raw):
    original = tmp_path / 'pin-input.tar.gz'
    canonical = tmp_path / 'pin-output.tar.gz'
    original.write_bytes(raw)
    module.normalize(original, canonical)
    row.update(
        normalization='gitiles-tar-v1',
        acquisition_sha256=hashlib.sha256(raw).hexdigest(),
        sha256=module.digest(canonical),
    )
    return canonical.read_bytes()


def test_explicit_normalization_accepts_timestamp_changes_under_exact_final_hash(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    raw = _example_archive(1)
    expected = _normalized_pin(module, row, tmp_path, raw)
    regenerated = _example_archive(999)
    assert hashlib.sha256(regenerated).hexdigest() != row['acquisition_sha256']
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(regenerated))
    cache = tmp_path / 'cache'
    module.fetch('all', cache)
    assert (cache / row['filename']).read_bytes() == expected
    assert list(cache.iterdir()) == [cache / row['filename']]
    assert sleeps == []


@pytest.mark.parametrize('change', ['content', 'mode', 'path', 'link'])
def test_normalization_keeps_changed_source_identity_as_hard_hash_failure(sources, monkeypatch, tmp_path, change):
    module, row, sleeps = sources
    _normalized_pin(module, row, tmp_path, _example_archive(1))
    changed = _example_archive(999, change)
    calls = []

    def open_changed(*args, **kwargs):
        calls.append(None)
        return io.BytesIO(changed)

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_changed)
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='Source checksum failed'):
        module.fetch('all', cache)
    assert calls == [None] and sleeps == []
    assert not (cache / row['filename']).exists()
    assert (cache / (row['filename'] + '.partial')).read_bytes() == changed


@pytest.mark.parametrize('invalid', ['traversal', 'drive', 'duplicate', 'special', 'escaping-link', 'composed-link'])
def test_unsafe_normalized_archive_is_rejected_without_retry(sources, monkeypatch, tmp_path, invalid):
    module, row, sleeps = sources
    _normalized_pin(module, row, tmp_path, _example_archive(1))
    info = tarfile.TarInfo('../outside' if invalid == 'traversal' else 'C:/outside' if invalid == 'drive' else 'file')
    entries = [(info, b'bytes')]
    if invalid == 'duplicate':
        entries.append((tarfile.TarInfo('file'), b'duplicate'))
    elif invalid == 'special':
        info.type = tarfile.CHRTYPE
    elif invalid in ('escaping-link', 'composed-link'):
        info.type = tarfile.SYMTYPE
        info.linkname = '../outside' if invalid == 'escaping-link' else '.'
        if invalid == 'composed-link':
            link = tarfile.TarInfo('second-link')
            link.type, link.linkname = tarfile.SYMTYPE, 'file/../outside'
            entries.append((link, b''))
    unsafe = _tar_bytes(entries, 999)
    calls = []

    def open_unsafe(*args, **kwargs):
        calls.append(None)
        return io.BytesIO(unsafe)

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_unsafe)
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='Source normalization failed'):
        module.fetch('all', cache)
    assert calls == [None] and sleeps == []
    assert not (cache / row['filename']).exists()
    assert (cache / (row['filename'] + '.partial')).read_bytes() == unsafe


def test_unknown_normalization_fails_before_network(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    row['normalization'] = 'unreviewed-transform'

    def unexpected(*args, **kwargs):
        pytest.fail('Unknown normalization must not trigger a network request')

    monkeypatch.setattr(module.urllib.request, 'urlopen', unexpected)
    with pytest.raises(ValueError, match='Unknown source normalization'):
        module.fetch('all', tmp_path)
    assert sleeps == []


@pytest.mark.parametrize('raw', [b'not a gzip archive', _example_archive(1)[:50]])
def test_malformed_normalized_archive_is_a_contextual_permanent_failure(sources, monkeypatch, tmp_path, raw):
    module, row, sleeps = sources
    _normalized_pin(module, row, tmp_path, _example_archive(1))
    calls = []

    def open_malformed(*args, **kwargs):
        calls.append(None)
        return io.BytesIO(raw)

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_malformed)
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='Source normalization failed') as caught:
        module.fetch('all', cache)
    assert row['filename'] in str(caught.value) and row['url'] in str(caught.value)
    assert calls == [None] and sleeps == []
    assert not (cache / row['filename']).exists()


def test_normalized_cache_is_verified_without_migrating_raw_or_corrupt_cache(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    raw = _example_archive(1)
    _normalized_pin(module, row, tmp_path, raw)
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / row['filename']).write_bytes(raw)

    def unexpected(*args, **kwargs):
        pytest.fail('Existing cache must pass its final pinned byte hash directly')

    monkeypatch.setattr(module.urllib.request, 'urlopen', unexpected)
    monkeypatch.setattr(module, 'normalize', unexpected)
    with pytest.raises(RuntimeError, match='Source checksum failed'):
        module.fetch('all', cache)
    assert (cache / row['filename']).read_bytes() == raw and sleeps == []


def _mode_archive(mode, member_type=tarfile.REGTYPE):
    output = io.BytesIO()
    source = tarfile.TarInfo('source.py')
    source.type = member_type
    source.size = len(b'source bytes') if source.isfile() else 0
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        archive.addfile(source, io.BytesIO(b'source bytes') if source.isfile() else None)
    # Python's TAR writer masks redundant mode type bits. Set the raw header as
    # Gitiles does, and repair its checksum so the validator sees the real bits.
    raw = bytearray(output.getvalue())
    raw[100:108] = f'{mode:07o}\0'.encode()
    raw[148:156] = b' ' * 8
    raw[148:156] = f'{sum(raw[:512]):06o}\0 '.encode()
    return gzip.compress(raw, mtime=1)


@pytest.mark.parametrize('mode,member_type', [(0o100755, tarfile.REGTYPE), (0o040755, tarfile.DIRTYPE)])
def test_gitiles_redundant_mode_type_bits_keep_type_and_permissions(sources, tmp_path, mode, member_type):
    module, row, sleeps = sources
    raw = _mode_archive(mode, member_type)
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        assert archive.getmembers()[0].mode == mode
    canonical = _normalized_pin(module, row, tmp_path, raw)
    with tarfile.open(fileobj=io.BytesIO(canonical), mode='r:gz') as archive:
        member = archive.getmembers()[0]
        assert member.type == member_type and member.mode == 0o755
        if member.isfile():
            assert archive.extractfile(member).read() == b'source bytes'


@pytest.mark.parametrize(
    'key,value',
    [('SCHILY.xattr.user.test', 'attribute'), ('GNU.sparse.map', '0,5'), ('custom.metadata', 'custom')],
)
def test_normalization_rejects_unreviewed_pax_metadata(sources, monkeypatch, tmp_path, key, value):
    module, row, sleeps = sources
    _normalized_pin(module, row, tmp_path, _example_archive(1))
    info = tarfile.TarInfo('source.py')
    info.pax_headers[key] = value
    raw = _tar_bytes([(info, b'bytes')], 999)
    calls = []

    def open_metadata(*args, **kwargs):
        calls.append(None)
        return io.BytesIO(raw)

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_metadata)
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='Source normalization failed.*Unsupported PAX metadata'):
        module.fetch('all', cache)
    assert calls == [None] and sleeps == []
    assert not (cache / row['filename']).exists()
    assert (cache / (row['filename'] + '.partial')).read_bytes() == raw


@pytest.mark.parametrize(
    'mode,member_type',
    [(0o040644, tarfile.REGTYPE), (0o100755, tarfile.DIRTYPE), (0o400644, tarfile.REGTYPE)],
)
def test_normalization_rejects_contradictory_or_unknown_mode_bits(sources, monkeypatch, tmp_path, mode, member_type):
    module, row, sleeps = sources
    _normalized_pin(module, row, tmp_path, _example_archive(1))
    raw = _mode_archive(mode, member_type)
    calls = []

    def open_mode(*args, **kwargs):
        calls.append(None)
        return io.BytesIO(raw)

    monkeypatch.setattr(module.urllib.request, 'urlopen', open_mode)
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='Source normalization failed.*Unexpected file mode bits'):
        module.fetch('all', cache)
    assert calls == [None] and sleeps == []
    assert not (cache / row['filename']).exists()
    assert (cache / (row['filename'] + '.partial')).read_bytes() == raw


def test_unmarked_archive_is_not_normalized(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources

    def unexpected(*args, **kwargs):
        pytest.fail('Unmarked archive must retain the raw byte hash contract')

    monkeypatch.setattr(module, 'normalize', unexpected)
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'original source'))
    module.fetch('all', tmp_path)
    assert (tmp_path / row['filename']).read_bytes() == b'original source'
    assert sleeps == []


def test_source_companion_includes_the_normalizer(sources, monkeypatch, tmp_path):
    module, row, sleeps = sources
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / row['filename']).write_bytes(b'original source')
    monkeypatch.setattr(module, 'fetch', lambda groups, directory: [row])
    output = tmp_path / 'companion.tar.gz'
    module.companion('all', cache, output)
    with tarfile.open(output, 'r:gz') as archive:
        assert 'packaging/macos/source_archive.py' in archive.getnames()
