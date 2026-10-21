"""Managed add-on lifecycle using tiny archives and a local HTTP fixture, never real models."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from audiolab import system
from audiolab.ai import managed
from audiolab.context import Progress
from audiolab.errors import AudioLabError, Cancelled

URL = 'https://github.com/JSummerer/AudioLab/releases/download/ai-1.0.0/AudioLabAI.tar.gz'
MODELS = ['htdemucs', 'htdemucs_ft']


def archive_bytes(entries=None):
    entries = (
        entries
        if entries is not None
        else [
            ('AudioLabAI', b'fake executable', tarfile.REGTYPE),
            ('pack.json', json.dumps({'version': '1.0.0', 'protocol': 1}).encode(), tarfile.REGTYPE),
            ('models/weights.safetensors', b'offline weights', tarfile.REGTYPE),
            ('_internal/lib.dylib', b'private runtime', tarfile.REGTYPE),
        ]
    )
    target = io.BytesIO()
    installed = 0
    with tarfile.open(fileobj=target, mode='w:gz') as tar:
        for name, data, kind in entries:
            item = tarfile.TarInfo(name)
            item.type, item.mode = kind, 0o755 if name == 'AudioLabAI' else 0o644
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                item.linkname = data.decode()
            elif kind == tarfile.REGTYPE:
                item.size = len(data)
                installed += len(data)
            tar.addfile(item, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return target.getvalue(), installed


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(system, 'managed_ai_supported', lambda: True)
    blob, installed = archive_bytes()
    catalog = {
        'schema': 1,
        'protocol': 1,
        'version': '1.0.0',
        'platform': 'macos-arm64',
        'url': URL,
        'sha256': hashlib.sha256(blob).hexdigest(),
        'download_bytes': len(blob),
        'installed_bytes': installed,
        'executable': 'AudioLabAI',
        'models': MODELS,
    }
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(catalog))
    manager = managed.ManagedAI(tmp_path / 'data', path)
    calls = []

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        assert command[1:] == ['--probe']
        assert Path(command[0]).is_file()
        assert (Path(command[0]).parent / 'models' / 'weights.safetensors').is_file()
        assert kwargs['inherited_fds'] and kwargs['timeout_s'] == 90
        kwargs['progress'].check_cancel()
        kwargs['on_line'](
            json.dumps(
                {
                    'type': 'probe',
                    'protocol': 1,
                    'version': manager.catalog['version'],
                    'available': True,
                    'device': 'cpu',
                    'torch_version': '2.fixture',
                    'demucs_version': '4.fixture',
                    'models': MODELS,
                }
            )
        )
        return SimpleNamespace(returncode=0, stderr_tail='')

    monkeypatch.setattr(managed.proc, 'run', probe)
    return SimpleNamespace(manager=manager, blob=blob, catalog=catalog, path=path, calls=calls, probe=probe)


@pytest.fixture
def server():
    state = SimpleNamespace(blob=b'', requests=[], mode='normal')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw_range = self.headers.get('Range')
            state.requests.append(raw_range)
            start = int(raw_range.removeprefix('bytes=').removesuffix('-')) if raw_range else 0
            if state.mode == 'ignore_range':
                start = 0
            payload = state.blob[start:]
            self.send_response(206 if start else 200)
            self.send_header('Content-Length', len(payload))
            if start:
                self.send_header(
                    'Content-Range',
                    f'bytes {start + (1 if state.mode == "bad_range" else 0)}-{len(state.blob) - 1}/{len(state.blob)}',
                )
            self.end_headers()
            if state.mode == 'truncated':
                payload = payload[: len(payload) // 2]
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=lambda: http.serve_forever(poll_interval=0.02), daemon=True)
    worker.start()

    @contextmanager
    def open_local(request):
        local = urllib.request.Request(
            f'http://127.0.0.1:{http.server_port}/asset', headers=dict(request.header_items())
        )
        with urllib.request.urlopen(local, timeout=3) as response:
            response.geturl = lambda: URL
            yield response

    state.open = open_local
    yield state
    http.shutdown()
    http.server_close()
    worker.join(timeout=2)


def connect(setup, server):
    server.blob = setup.blob
    setup.manager._open = server.open


def use_archive(setup, server, entries):
    setup.blob, installed = archive_bytes(entries)
    setup.manager.catalog.update(
        sha256=hashlib.sha256(setup.blob).hexdigest(), download_bytes=len(setup.blob), installed_bytes=installed
    )
    connect(setup, server)


def finish(manager, repair=False):
    done = []
    assert manager.start(done.append, repair=repair)
    manager._thread.join(timeout=5)
    assert not manager.running
    assert done == [manager.ok]
    return manager.ok


def partial(setup, count):
    manager = setup.manager
    manager._root(create=True)
    (manager.root / 'download.part').write_bytes(setup.blob[:count])
    manager._write_json(
        manager.root / 'download.json',
        {key: manager.catalog[key] for key in ('url', 'sha256', 'download_bytes')},
    )


def test_install_probe_ready_noop_restart_and_offline_environment(setup, server, monkeypatch):
    connect(setup, server)
    monkeypatch.setenv('PYTHONPATH', '/unrelated/modules')
    monkeypatch.setenv('TORCH_HOME', '/shared/torch')
    monkeypatch.setenv('DYLD_INSERT_LIBRARIES', '/unrelated/library')
    assert finish(setup.manager)
    state = setup.manager.status()
    assert state['available'] and state['state'] == 'ready'
    assert state['managed'] and state['version'] == '1.0.0'
    assert state['target_installed_bytes'] == setup.catalog['installed_bytes']
    assert state['removable'] and not (setup.manager.root / 'download.part').exists()
    env = setup.calls[0][1]['env']
    assert 'PYTHONPATH' not in env and 'DYLD_INSERT_LIBRARIES' not in env
    for name in ['TORCH_HOME', 'HF_HOME', 'XDG_CACHE_HOME', 'TMPDIR', 'TEMP', 'TMP']:
        assert Path(env[name]).is_relative_to(setup.manager.root)
    assert os.environ['TORCH_HOME'] == '/shared/torch'
    assert finish(setup.manager)  # already ready: no second download
    assert len(server.requests) == 1
    restarted = managed.ManagedAI(setup.manager.data_dir, setup.path)
    assert restarted.status()['available']
    probes = len(setup.calls)
    restarted.status()
    restarted.status()
    assert len(setup.calls) == probes  # health polls do not repeatedly load both models
    with restarted.lease(Progress.null()) as (directory, lock):
        assert directory == restarted.root / '1.0.0' and lock.fileno >= 0
        assert restarted.status()['available']
        assert restarted.status()['state'] == 'busy'


@pytest.mark.parametrize('mode', ['normal', 'ignore_range'])
def test_resumes_only_at_correct_offset_or_restarts_if_server_ignores_range(setup, server, mode):
    connect(setup, server)
    server.mode = mode
    partial(setup, 79)
    assert setup.manager.status()['state'] == 'partial'
    assert finish(setup.manager)
    assert server.requests == ['bytes=79-']


def test_rejects_incorrect_content_range_without_activation(setup, server):
    connect(setup, server)
    server.mode = 'bad_range'
    partial(setup, 79)
    assert not finish(setup.manager)
    assert (setup.manager.root / 'download.part').read_bytes() == setup.blob[:79]
    assert not (setup.manager.root / 'active.json').exists()
    assert not setup.calls


def test_truncated_download_is_retryable_and_resume_metadata_is_pinned(setup, server):
    connect(setup, server)
    server.mode = 'truncated'
    assert not finish(setup.manager)
    size = (setup.manager.root / 'download.part').stat().st_size
    assert 0 < size < len(setup.blob)
    server.mode = 'normal'
    assert finish(setup.manager)
    assert server.requests == [None, f'bytes={size}-']


def test_complete_archive_can_resume_without_network_and_hash_corruption_redownloads(setup, server):
    connect(setup, server)
    partial(setup, len(setup.blob))
    data = bytearray((setup.manager.root / 'download.part').read_bytes())
    data[10] ^= 1
    (setup.manager.root / 'download.part').write_bytes(data)
    assert not finish(setup.manager)
    assert not (setup.manager.root / 'download.part').exists()
    assert not setup.calls
    assert finish(setup.manager)
    assert server.requests == [None]
    assert setup.manager.remove()['state'] == 'missing'
    partial(setup, len(setup.blob))
    assert finish(setup.manager)
    assert server.requests == [None]


def test_cancel_preserves_only_resumable_download_and_retry_succeeds(setup, server, monkeypatch):
    connect(setup, server)
    monkeypatch.setattr(managed, '_BLOCK', 32)
    original = setup.manager._open

    @contextmanager
    def cancel_after_read(request):
        with original(request) as response:
            read = response.read

            def read_and_cancel(count):
                result = read(count)
                setup.manager.cancel()
                return result

            response.read = read_and_cancel
            yield response

    setup.manager._open = cancel_after_read
    assert not finish(setup.manager)
    assert (setup.manager.root / 'download.part').stat().st_size == 32
    assert not (setup.manager.root / 'active.json').exists()
    assert setup.manager.status()['state'] == 'partial'
    setup.manager._open = original
    assert finish(setup.manager)
    assert server.requests == [None, 'bytes=32-']


@pytest.mark.parametrize(
    'name,kind,data',
    [
        ('../escape', tarfile.REGTYPE, b'bad'),
        ('/absolute', tarfile.REGTYPE, b'bad'),
        ('models/../../escape', tarfile.REGTYPE, b'bad'),
        ('models\\escape', tarfile.REGTYPE, b'bad'),
        ('models//escape', tarfile.REGTYPE, b'bad'),
        ('C:/escape', tarfile.REGTYPE, b'bad'),
        ('models/link', tarfile.SYMTYPE, b'../../outside'),
        ('models/link', tarfile.LNKTYPE, b'../../outside'),
        ('models/pipe', tarfile.FIFOTYPE, b''),
        ('models/device', tarfile.CHRTYPE, b''),
    ],
)
def test_unsafe_archives_never_activate_or_touch_outside(setup, server, name, kind, data):
    blob, installed = archive_bytes([('AudioLabAI', b'fake', tarfile.REGTYPE), (name, data, kind)])
    setup.manager.catalog.update(
        sha256=hashlib.sha256(blob).hexdigest(), download_bytes=len(blob), installed_bytes=installed
    )
    setup.blob = blob
    connect(setup, server)
    outside = setup.manager.data_dir / 'escape'
    outside.parent.mkdir()
    outside.write_text('untouched')
    assert not finish(setup.manager)
    assert outside.read_text() == 'untouched'
    assert not (setup.manager.root / 'active.json').exists() and not setup.calls


@pytest.mark.parametrize('other', ['AudioLabAI', 'audiolabAI'])
def test_duplicate_archive_members_are_rejected(setup, server, other):
    blob, installed = archive_bytes([('AudioLabAI', b'first', tarfile.REGTYPE), (other, b'second', tarfile.REGTYPE)])
    setup.manager.catalog.update(
        sha256=hashlib.sha256(blob).hexdigest(), download_bytes=len(blob), installed_bytes=installed
    )
    setup.blob = blob
    connect(setup, server)
    assert not finish(setup.manager)
    assert not setup.calls


@pytest.mark.parametrize('forward_reference', [False, True])
def test_direct_regular_file_hardlinks_preserve_aliases_and_count_unique_storage(setup, server, forward_reference):
    files = [
        ('AudioLabAI', b'fake executable', tarfile.REGTYPE),
        ('models/weights.safetensors', b'offline weights', tarfile.REGTYPE),
        ('_internal/lib.dylib', b'private runtime', tarfile.REGTYPE),
    ]
    links = [
        ('_internal/alias.dylib', b'_internal/lib.dylib', tarfile.LNKTYPE),
        ('models/alias.safetensors', b'models/weights.safetensors', tarfile.LNKTYPE),
        ('AudioLabAI-alias', b'AudioLabAI', tarfile.LNKTYPE),
    ]
    use_archive(setup, server, links + files if forward_reference else files + links)
    assert finish(setup.manager)
    assert setup.manager.status()['available']
    directory = setup.manager.root / '1.0.0'
    for alias, target, _ in links:
        left, right = (directory / alias).stat(), (directory / target.decode()).stat()
        assert (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
        assert left.st_nlink == 2
        assert not (directory / alias).is_symlink()
    metadata_bytes = sum(
        path.stat().st_size
        for path in [
            setup.manager.root / managed._OWNER_FILE,
            setup.manager.root / 'active.json',
            directory / '.managed-receipt.json',
        ]
    )
    assert setup.manager.storage_bytes() == setup.manager.catalog['installed_bytes'] + metadata_bytes
    assert setup.manager.remove()['state'] == 'missing'
    assert not setup.manager.root.exists()


@pytest.mark.parametrize(
    'target',
    [
        '/outside',
        '../outside',
        '_internal/../../outside',
        'C:/outside',
        'models\\weights.safetensors',
        'models//weights.safetensors',
        './models/weights.safetensors',
        'models/weights.safetensors/',
        'missing',
        'models',
        '_internal/alias.dylib',
        '_internal/LIB.dylib',
    ],
)
def test_hardlinks_reject_unsafe_missing_directory_self_and_case_alias_targets(setup, server, tmp_path, target):
    use_archive(
        setup,
        server,
        [
            ('AudioLabAI', b'fake executable', tarfile.REGTYPE),
            ('models', b'', tarfile.DIRTYPE),
            ('models/weights.safetensors', b'offline weights', tarfile.REGTYPE),
            ('_internal/lib.dylib', b'private runtime', tarfile.REGTYPE),
            ('_internal/alias.dylib', target.encode(), tarfile.LNKTYPE),
        ],
    )
    sentinel = tmp_path / 'outside'
    sentinel.write_bytes(b'never link outside the archive')
    assert not finish(setup.manager)
    assert not (setup.manager.root / 'active.json').exists()
    assert not setup.calls
    assert sentinel.read_bytes() == b'never link outside the archive'


@pytest.mark.parametrize('forward_reference', [False, True])
def test_hardlink_chains_are_rejected_before_any_link_is_created(setup, server, forward_reference):
    files = [
        ('AudioLabAI', b'fake executable', tarfile.REGTYPE),
        ('models/weights.safetensors', b'offline weights', tarfile.REGTYPE),
    ]
    links = [
        ('models/first', b'models/weights.safetensors', tarfile.LNKTYPE),
        ('models/second', b'models/first', tarfile.LNKTYPE),
    ]
    use_archive(setup, server, links + files if forward_reference else files + links)
    assert not finish(setup.manager)
    assert not setup.calls
    assert 'regular archive file' in '\n'.join(setup.manager.log)


@pytest.mark.parametrize('alias', ['models/alias', 'models/ALIAS'])
def test_duplicate_hardlink_destinations_are_rejected(setup, server, alias):
    use_archive(
        setup,
        server,
        [
            ('AudioLabAI', b'fake executable', tarfile.REGTYPE),
            ('models/weights.safetensors', b'offline weights', tarfile.REGTYPE),
            ('models/alias', b'models/weights.safetensors', tarfile.LNKTYPE),
            (alias, b'models/weights.safetensors', tarfile.LNKTYPE),
        ],
    )
    assert not finish(setup.manager)
    assert not setup.calls
    assert 'duplicate' in '\n'.join(setup.manager.log)


def test_hardlinks_cannot_adopt_preexisting_staging_files(setup, tmp_path):
    blob, installed = archive_bytes(
        [('AudioLabAI', b'fake', tarfile.REGTYPE), ('alias', b'not-in-archive', tarfile.LNKTYPE)]
    )
    setup.manager.catalog['installed_bytes'] = installed
    archive = tmp_path / 'archive.tar.gz'
    archive.write_bytes(blob)
    staged = setup.manager.root / 'staging' / 'direct-test'
    setup.manager._directory(staged, create=True)
    sentinel = staged / 'not-in-archive'
    sentinel.write_bytes(b'not owned by this archive')
    with pytest.raises(AudioLabError, match='regular archive file'):
        setup.manager._extract(archive, staged, Progress.null())
    assert not (staged / 'alias').exists()
    assert sentinel.read_bytes() == b'not owned by this archive'


def test_hardlink_with_nonzero_payload_is_rejected(setup, server):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode='w:gz') as tar:
        regular = tarfile.TarInfo('AudioLabAI')
        regular.size = 4
        tar.addfile(regular, io.BytesIO(b'fake'))
        link = tarfile.TarInfo('alias')
        link.type, link.linkname, link.size = tarfile.LNKTYPE, 'AudioLabAI', 1
        tar.addfile(link, io.BytesIO(b'x'))
    setup.blob = target.getvalue()
    setup.manager.catalog.update(
        sha256=hashlib.sha256(setup.blob).hexdigest(), download_bytes=len(setup.blob), installed_bytes=4
    )
    connect(setup, server)
    assert not finish(setup.manager)
    assert 'unexpected file data' in '\n'.join(setup.manager.log)
    assert not setup.calls


def test_hardlinks_count_against_the_archive_entry_limit(setup, server, monkeypatch):
    use_archive(
        setup,
        server,
        [
            ('AudioLabAI', b'fake executable', tarfile.REGTYPE),
            ('models/weights.safetensors', b'offline weights', tarfile.REGTYPE),
            ('models/alias', b'models/weights.safetensors', tarfile.LNKTYPE),
        ],
    )
    monkeypatch.setattr(managed, '_MAX_FILES', 2)
    assert not finish(setup.manager)
    assert not setup.calls
    assert 'too many files' in '\n'.join(setup.manager.log)


@pytest.mark.parametrize('size_delta', [-1, 1])
def test_exact_unpacked_size_is_required(setup, server, size_delta):
    connect(setup, server)
    setup.manager.catalog['installed_bytes'] += size_delta
    assert not finish(setup.manager)
    assert not setup.calls


def test_file_count_and_disk_space_limits_prevent_activation(setup, server, monkeypatch):
    connect(setup, server)
    monkeypatch.setattr(managed, '_MAX_FILES', 2)
    assert not finish(setup.manager)
    assert 'too many files' in '\n'.join(setup.manager.log)
    assert not setup.calls
    monkeypatch.setattr(managed.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1))
    assert not finish(setup.manager)
    assert 'free space' in '\n'.join(setup.manager.log)


@pytest.mark.parametrize('repair', [False, True])
def test_interrupted_extraction_is_reclaimed_before_retry_disk_preflight(setup, server, monkeypatch, repair):
    connect(setup, server)
    assert finish(setup.manager)
    previous = setup.manager.root / '1.0.0'
    receipt = (setup.manager.root / 'active.json').read_bytes()
    if not repair:
        setup.manager.catalog = {**setup.manager.catalog, 'version': '2.0.0'}
    count = len(setup.blob) // 2
    partial(setup, count)
    abandoned = setup.manager.root / 'staging' / f'install-{"a" * 32}'
    setup.manager._directory(abandoned, create=True)
    (abandoned / 'unpacked').write_bytes(b'interrupted extraction')
    needed = (
        len(setup.blob)
        - count
        + setup.manager.catalog['installed_bytes']
        + max(64 * 1024**2, setup.manager.catalog['installed_bytes'] // 10)
    )
    observed_staging = []

    def free_after_reclaim(path):
        assert path == setup.manager.root
        assert (previous / 'AudioLabAI').read_bytes() == b'fake executable'
        assert (setup.manager.root / 'active.json').read_bytes() == receipt
        assert (setup.manager.root / 'download.part').read_bytes() == setup.blob[:count]
        observed_staging.append(abandoned.exists())
        return SimpleNamespace(free=needed - int(abandoned.exists()))

    monkeypatch.setattr(managed.shutil, 'disk_usage', free_after_reclaim)
    assert finish(setup.manager, repair=repair), '\n'.join(setup.manager.log)
    assert observed_staging == [False]
    assert server.requests == [None, f'bytes={count}-']
    assert setup.manager.status()['available']


def test_retry_recovers_activation_before_reclaiming_staging(setup, server, monkeypatch):
    connect(setup, server)
    assert finish(setup.manager)
    receipt = (setup.manager.root / 'active.json').read_bytes()
    replace = managed.system.replace_file

    def stop_after_backup(source, destination, *args, **kwargs):
        replace(source, destination, *args, **kwargs)
        if Path(destination).name.startswith('previous-'):
            raise OSError('simulated interruption after backup')

    monkeypatch.setattr(managed.system, 'replace_file', stop_after_backup)
    assert not finish(setup.manager, repair=True)
    assert not (setup.manager.root / '1.0.0').exists()
    assert list((setup.manager.root / 'staging').glob('previous-*'))
    monkeypatch.setattr(managed.system, 'replace_file', replace)
    checked = []

    def insufficient_space_after_recovery(path):
        assert (path / '1.0.0' / 'AudioLabAI').read_bytes() == b'fake executable'
        assert (path / 'active.json').read_bytes() == receipt
        assert not (path / 'activation.json').exists()
        assert not list((path / 'staging').iterdir())
        assert (path / 'download.part').read_bytes() == setup.blob
        checked.append(True)
        return SimpleNamespace(free=1)

    monkeypatch.setattr(managed.shutil, 'disk_usage', insufficient_space_after_recovery)
    assert not finish(setup.manager, repair=True)
    assert checked == [True]
    assert 'free space' in '\n'.join(setup.manager.log)
    assert (setup.manager.root / 'active.json').read_bytes() == receipt
    assert setup.manager.status()['available']


def test_failed_probe_preserves_previous_version_and_success_replaces_it(setup, server, monkeypatch):
    connect(setup, server)
    assert finish(setup.manager)
    previous = (setup.manager.root / 'active.json').read_bytes()
    setup.manager.catalog = {**setup.manager.catalog, 'version': '2.0.0'}
    assert setup.manager.status()['state'] == 'incompatible'
    monkeypatch.setattr(
        managed.proc, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr_tail='broken')
    )
    assert not finish(setup.manager)
    assert (setup.manager.root / '1.0.0' / 'AudioLabAI').exists()
    assert (setup.manager.root / 'active.json').read_bytes() == previous
    monkeypatch.setattr(managed.proc, 'run', setup.probe)
    assert finish(setup.manager)
    assert not (setup.manager.root / '1.0.0').exists()
    assert setup.manager.status()['version'] == '2.0.0'


def test_same_version_repair_uses_stage_and_can_repair_missing_helper(setup, server):
    connect(setup, server)
    assert finish(setup.manager)
    (setup.manager.root / '1.0.0' / 'AudioLabAI').unlink()
    assert setup.manager.status()['state'] == 'broken'
    assert finish(setup.manager, repair=True)
    assert setup.manager.status()['available']
    assert not (setup.manager.root / 'activation.json').exists()
    assert not (setup.manager.root / 'staging').exists()


def test_cancel_after_probe_preserves_existing_healthy_version(setup, server, monkeypatch):
    connect(setup, server)
    assert finish(setup.manager)
    receipt = (setup.manager.root / 'active.json').read_bytes()

    def cancel_at_probe(*args, **kwargs):
        result = setup.probe(*args, **kwargs)
        setup.manager.cancel()
        return result

    monkeypatch.setattr(managed.proc, 'run', cancel_at_probe)
    assert not finish(setup.manager, repair=True)
    assert (setup.manager.root / 'active.json').read_bytes() == receipt
    assert setup.manager.status()['available']


@pytest.mark.parametrize('problem', ['protocol', 'version', 'models', 'available', 'device'])
def test_probe_protocol_and_selected_models_are_required_before_activation(setup, server, monkeypatch, problem):
    connect(setup, server)

    def invalid_probe(command, **kwargs):
        value = {
            'protocol': 1,
            'version': '1.0.0',
            'models': MODELS,
            'available': True,
            'device': 'cpu',
        }
        value[problem] = None
        kwargs['on_line'](json.dumps(value))
        return SimpleNamespace(returncode=0, stderr_tail='')

    monkeypatch.setattr(managed.proc, 'run', invalid_probe)
    assert not finish(setup.manager)
    assert not (setup.manager.root / 'active.json').exists()


@pytest.mark.parametrize('crash_at', ['before_move', 'after_backup', 'after_move', 'after_receipt'])
def test_interrupted_same_version_repair_recovers_on_restart(setup, server, monkeypatch, crash_at):
    connect(setup, server)
    assert finish(setup.manager)
    original = (setup.manager.root / '1.0.0' / '.managed-receipt.json').read_bytes()
    replace = managed.system.replace_file

    def crash(source, destination, *args, **kwargs):
        is_backup = Path(destination).name.startswith('previous-')
        is_move = Path(destination) == setup.manager.root / '1.0.0'
        is_receipt = Path(destination) == setup.manager.root / 'active.json'
        if crash_at == 'before_move' and is_backup:
            raise OSError('simulated interruption')
        replace(source, destination, *args, **kwargs)
        if (crash_at == 'after_backup' and is_backup) or (crash_at == 'after_move' and is_move):
            raise OSError('simulated interruption')
        if crash_at == 'after_receipt' and is_receipt:
            raise OSError('simulated interruption')

    monkeypatch.setattr(managed.system, 'replace_file', crash)
    assert not finish(setup.manager, repair=True)
    monkeypatch.setattr(managed.system, 'replace_file', replace)
    restarted = managed.ManagedAI(setup.manager.data_dir, setup.path)
    assert restarted.status()['available']
    marker = (setup.manager.root / '1.0.0' / '.managed-receipt.json').read_bytes()
    assert (marker != original) == (crash_at == 'after_receipt')
    assert not (setup.manager.root / 'activation.json').exists()
    assert not list((setup.manager.root / 'staging').glob('previous-*'))


def test_removal_is_owned_only_including_partial_downloads_and_nested_links(setup, server, tmp_path):
    connect(setup, server)
    outside = tmp_path / 'shared-model-cache'
    outside.mkdir()
    sentinel = outside / 'real-audio.wav'
    sentinel.write_bytes(b'user audio')
    assert finish(setup.manager)
    (setup.manager.root / 'cache' / 'nested-link').symlink_to(outside, target_is_directory=True)
    (setup.manager.data_dir / 'library').mkdir()
    library = setup.manager.data_dir / 'library' / 'entry.json'
    library.write_text('kept original')
    stable_lock = setup.manager.data_dir / '.ai-operation.lock'
    inode = stable_lock.stat().st_ino
    assert setup.manager.remove()['state'] == 'missing'
    assert not setup.manager.root.exists()
    assert sentinel.read_bytes() == b'user audio' and library.read_text() == 'kept original'
    assert stable_lock.stat().st_ino == inode
    partial(setup, 40)
    assert setup.manager.remove()['state'] == 'missing'
    assert not setup.manager.root.exists()


@pytest.mark.parametrize('location', ['root', 'version', 'partial', 'lock'])
def test_symlink_or_hardlink_mutation_boundaries_are_refused(setup, server, tmp_path, location):
    connect(setup, server)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_text('must survive')
    setup.manager.data_dir.mkdir()
    if location == 'root':
        setup.manager.root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(AudioLabError, match='link'):
            setup.manager.start(lambda ok: None)
        with pytest.raises(AudioLabError, match='link'):
            setup.manager.remove()
    elif location == 'version':
        setup.manager._root(create=True)
        (setup.manager.root / '1.0.0').symlink_to(outside, target_is_directory=True)
        assert not finish(setup.manager)
        with pytest.raises(AudioLabError, match='link'):
            setup.manager.remove()
    elif location == 'partial':
        setup.manager._root(create=True)
        os.link(sentinel, setup.manager.root / 'download.part')
        assert not finish(setup.manager)
        setup.manager.remove()
    else:
        (setup.manager.data_dir / '.ai-operation.lock').symlink_to(sentinel)
        with pytest.raises(AudioLabError, match='link'):
            setup.manager.start(lambda ok: None)
    assert sentinel.read_text() == 'must survive'


def test_operation_exclusion_and_cancellable_lease(setup, server):
    connect(setup, server)
    assert finish(setup.manager)
    lock = system.InstanceLock(setup.manager.data_dir / '.ai-operation.lock')
    assert lock.acquire()
    event = threading.Event()
    result = []

    def wait_for_lease():
        try:
            with setup.manager.lease(Progress.null(event)):
                result.append('unexpected use')
        except Cancelled:
            result.append('cancelled')

    try:
        with pytest.raises(AudioLabError, match='in use'):
            setup.manager.start(lambda ok: None)
        with pytest.raises(AudioLabError, match='in use'):
            setup.manager.remove()
        thread = threading.Thread(target=wait_for_lease)
        thread.start()
        event.set()
        thread.join(timeout=2)
        assert result == ['cancelled']
        assert setup.manager.status()['state'] == 'busy'
    finally:
        lock.release()
    assert setup.manager.status()['available']


def test_separate_process_holds_same_stable_lock(setup, server):
    connect(setup, server)
    script = (
        'import sys; from pathlib import Path; from audiolab.system import InstanceLock; '
        'lock=InstanceLock(Path(sys.argv[1])); assert lock.acquire(); '
        'print("locked", flush=True); sys.stdin.readline()'
    )
    child = subprocess.Popen(
        [sys.executable, '-c', script, str(setup.manager.data_dir / '.ai-operation.lock')],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(AudioLabError, match='in use'):
            setup.manager.start(lambda ok: None)
        with pytest.raises(AudioLabError, match='in use'):
            setup.manager.remove()
    finally:
        child.communicate('\n', timeout=5)
    assert finish(setup.manager)


@pytest.mark.skipif(os.name != 'posix', reason='Managed helper descriptor inheritance is a POSIX delivery boundary.')
def test_inherited_lock_survives_abrupt_parent_exit(setup, tmp_path):
    stop = tmp_path / 'stop-helper'
    lockpath = setup.manager.data_dir / '.ai-operation.lock'
    helper = (
        'import os,sys,time; from pathlib import Path; '
        'assert os.fstat(int(sys.argv[1])); '
        'stop=Path(sys.argv[2]); '
        '\nwhile not stop.exists(): time.sleep(.02)\n'
    )
    parent = (
        'import os,subprocess,sys; from pathlib import Path; '
        'from audiolab.system import InstanceLock,inherited_descriptors; '
        'lock=InstanceLock(Path(sys.argv[1])); assert lock.acquire(); '
        'subprocess.Popen([sys.executable,"-c",sys.argv[3],str(lock.fileno),sys.argv[2]], '
        'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**inherited_descriptors((lock.fileno,))); '
        'os._exit(0)'
    )
    owner = subprocess.Popen([sys.executable, '-c', parent, str(lockpath), str(stop), helper])
    lock = system.InstanceLock(lockpath)
    try:
        assert owner.wait(timeout=5) == 0
        assert not lock.acquire()  # parent is gone; the still-running helper owns the inherited description
    finally:
        stop.touch()
        owner.wait(timeout=5)
    deadline = time.monotonic() + 5
    while not lock.acquire():
        assert time.monotonic() < deadline, 'The test helper did not release its inherited lock.'
        time.sleep(0.02)
    lock.release()


@pytest.mark.parametrize(
    'field,value',
    [
        ('url', 'http://github.com/JSummerer/AudioLab/releases/download/v1/AI.tar.gz'),
        ('url', 'https://example.org/JSummerer/AudioLab/releases/download/v1/AI.tar.gz'),
        ('url', 'https://github.com/other/repo/releases/download/v1/AI.tar.gz'),
        ('url', 'https://github.com/JSummerer/AudioLab/releases/download/../AI.tar.gz'),
        ('version', '../escape'),
        ('version', 'cache'),
        ('version', 'active.json'),
        ('url', ['https://invalid']),
        ('download_bytes', True),
        ('installed_bytes', -1),
        ('sha256', 'bad'),
        ('models', ['htdemucs']),
        ('executable', '../helper'),
        ('protocol', 2),
        ('schema', True),
    ],
)
def test_invalid_catalog_disables_install_without_network(setup, field, value):
    setup.path.write_text(json.dumps({**setup.catalog, field: value}))
    manager = managed.ManagedAI(setup.manager.data_dir, setup.path)
    assert not manager.status()['install_supported']
    with pytest.raises(AudioLabError):
        manager.start(lambda ok: None)
    assert not manager.root.exists()


def test_missing_catalog_partial_removal_and_cached_storage_scan(setup, server, monkeypatch):
    missing = managed.ManagedAI(setup.manager.data_dir, setup.path.with_name('absent.json'))
    assert missing.status()['state'] == 'unsupported'
    missing._root(create=True)
    (missing.root / 'download.part').write_bytes(b'interrupted bytes')
    assert missing.status()['removable']
    assert missing.remove()['state'] == 'unsupported'
    connect(setup, server)
    assert finish(setup.manager)
    size = setup.manager.storage_bytes()
    monkeypatch.setattr(setup.manager, '_owned_bytes', lambda: pytest.fail('Repeated storage scan on health polling'))
    assert setup.manager.storage_bytes() == size
    assert setup.manager.status()['installed_bytes'] == size


def test_transport_redirect_cannot_escape_official_https_downloads():
    for url in ['http://github.com/file', 'https://malicious.invalid/file', 'file:///tmp/asset']:
        with pytest.raises(ValueError, match='HTTPS'):
            managed._ReleaseRedirect().redirect_request(None, None, 302, '', {}, url)


def test_existing_nonempty_unowned_root_is_never_adopted_or_removed(setup):
    setup.manager.root.mkdir(parents=True)
    sentinel = setup.manager.root / 'user-recording.wav'
    sentinel.write_bytes(b'user audio')
    (setup.manager.root / 'active.json').write_text(json.dumps({'catalog': setup.catalog}))
    for action in [setup.manager.remove, setup.manager._cleanup, lambda: setup.manager.start(lambda ok: None)]:
        with pytest.raises(AudioLabError, match='not owned'):
            action()
    state = setup.manager.status()
    assert state['state'] == 'broken' and not state['removable'] and not state['available']
    assert setup.manager.storage_bytes() == 0
    assert sentinel.read_bytes() == b'user audio'
    assert not (setup.manager.root / managed._OWNER_FILE).exists()


@pytest.mark.parametrize('kind', ['wrong_owner', 'partial', 'link', 'hardlink'])
def test_invalid_ownership_marker_preserves_every_file(setup, tmp_path, kind):
    setup.manager.root.mkdir(parents=True)
    outside = tmp_path / 'outside-marker'
    outside.write_text(json.dumps(managed._OWNER))
    marker = setup.manager.root / managed._OWNER_FILE
    if kind == 'wrong_owner':
        marker.write_text(json.dumps({'schema': 1, 'owner': 'unrelated'}))
    elif kind == 'partial':
        marker.write_text('{"schema":')
    elif kind == 'link':
        marker.symlink_to(outside)
    else:
        os.link(outside, marker)
    sentinel = setup.manager.root / 'sentinel.wav'
    sentinel.write_bytes(b'keep')
    with pytest.raises(AudioLabError, match='ownership marker'):
        setup.manager.remove()
    with pytest.raises(AudioLabError, match='ownership marker'):
        setup.manager.start(lambda ok: None)
    assert not setup.manager.status()['removable']
    assert sentinel.read_bytes() == b'keep'
    assert outside.read_text() == json.dumps(managed._OWNER)


def test_interrupted_ownership_creation_leaves_unclaimed_files_untouched(setup):
    script = (
        'import os,sys; from pathlib import Path; from audiolab.ai.managed import ManagedAI; '
        'from audiolab import system; '
        'system.replace_file=lambda *args,**kwargs: os._exit(91); '
        'ManagedAI(Path(sys.argv[1]),Path(sys.argv[2]))._root(create=True)'
    )
    child = subprocess.run([sys.executable, '-c', script, str(setup.manager.data_dir), str(setup.path)], timeout=5)
    assert child.returncode == 91
    leftovers = {path.name: path.read_bytes() for path in setup.manager.root.iterdir()}
    assert len(leftovers) == 1 and not (setup.manager.root / managed._OWNER_FILE).exists()
    with pytest.raises(AudioLabError, match='not owned'):
        setup.manager.remove()
    with pytest.raises(AudioLabError, match='not owned'):
        setup.manager.start(lambda ok: None)
    assert {path.name: path.read_bytes() for path in setup.manager.root.iterdir()} == leftovers
    assert not setup.manager.status()['removable']


@pytest.mark.parametrize('relationship', ['equal', 'descendant', 'ancestor', 'symlink_descendant'])
def test_existing_output_environment_overlap_blocks_all_managed_mutation(
    setup, server, monkeypatch, tmp_path, relationship
):
    connect(setup, server)
    assert finish(setup.manager)
    outputs = {
        'equal': setup.manager.root,
        'descendant': setup.manager.root / 'finished',
        'ancestor': setup.manager.data_dir,
        'symlink_descendant': setup.manager.root / 'finished',
    }
    output = outputs[relationship]
    output.mkdir(exist_ok=True)
    sentinel = output / 'user-audio.wav'
    sentinel.write_bytes(b'never remove output')
    if relationship == 'symlink_descendant':
        link = tmp_path / 'output-link'
        link.symlink_to(output, target_is_directory=True)
        output = link
    monkeypatch.setenv('AUDIOLAB_OUTPUT_DIR', str(output))
    state = setup.manager.status()
    assert not state['available'] and not state['removable'] and 'overlaps' in state['reason']
    for action in [setup.manager.remove, setup.manager._cleanup, lambda: setup.manager.start(lambda ok: None)]:
        with pytest.raises(AudioLabError, match='overlaps'):
            action()
    with pytest.raises(AudioLabError, match='overlaps'), setup.manager.lease(Progress.null()):
        pytest.fail('Output-overlapping AI use was allowed')
    assert sentinel.read_bytes() == b'never remove output'
    assert (setup.manager.root / managed._OWNER_FILE).is_file()


def test_output_changed_during_install_blocks_activation_and_cleanup(setup, server, monkeypatch):
    connect(setup, server)
    assert finish(setup.manager)
    receipt = (setup.manager.root / 'active.json').read_bytes()
    output = setup.manager.root / 'finished'

    def new_output_after_probe(*args, **kwargs):
        result = setup.probe(*args, **kwargs)
        output.mkdir()
        (output / 'recording.wav').write_bytes(b'keep output')
        monkeypatch.setenv('AUDIOLAB_OUTPUT_DIR', str(output))
        return result

    monkeypatch.setattr(managed.proc, 'run', new_output_after_probe)
    assert not finish(setup.manager, repair=True)
    assert (setup.manager.root / 'active.json').read_bytes() == receipt
    assert (output / 'recording.wav').read_bytes() == b'keep output'
    assert 'overlaps' in '\n'.join(setup.manager.log)
