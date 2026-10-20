"""The optional runtime boundary: protocol, confinement and inherited job leases."""

import json
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from audiolab import runtime, system
from audiolab.ai import client, worker
from audiolab.context import Progress
from audiolab.errors import AudioLabError, Cancelled
from audiolab.media import proc


@pytest.fixture
def installed(tmp_path, monkeypatch):
    class Manager:
        @contextmanager
        def lease(self, progress):
            yield tmp_path, SimpleNamespace(fileno=42)

        def environment(self, directory):
            return {'OWNED_AI': str(directory)}

    monkeypatch.setattr(client, 'managed_ai', Manager)
    return tmp_path


def test_managed_separation_uses_helper_and_validates_result(installed, monkeypatch):
    out = installed / 'job' / 'stems'
    out.mkdir(parents=True)
    stems = {}
    for name in ('vocals', 'drums', 'bass', 'other'):
        path = out / f'{name}.wav'
        path.write_bytes(b'fixture')
        stems[name] = str(path)

    def run(command, **kwargs):
        assert command == [
            str(installed / 'AudioLabAI'),
            '--separate',
            str(installed / 'input.wav'),
            str(out),
            'htdemucs',
            '1',
        ]
        assert kwargs['inherited_fds'] == (42,)
        assert not kwargs['capture_stdout']
        kwargs['on_line']('some library diagnostic')
        kwargs['on_line'](json.dumps({'type': 'progress', 'pct': 50, 'message': 'Separating'}))
        kwargs['on_line'](json.dumps({'type': 'result', 'protocol': 1, 'result': {'stems': stems}}))
        return proc.ProcResult(0, '', '')

    monkeypatch.setattr(proc, 'run', run)
    assert client.separate_managed(installed / 'input.wav', out, 'htdemucs', 1, Progress.null())['stems'] == stems


def test_managed_separation_rejects_outside_files(installed, monkeypatch):
    outside = installed / 'keep.wav'
    outside.write_bytes(b'user file')

    def run(command, **kwargs):
        kwargs['on_line'](
            json.dumps(
                {
                    'type': 'result',
                    'protocol': 1,
                    'result': {
                        'stems': dict.fromkeys(('vocals', 'drums', 'bass', 'other'), str(outside)),
                    },
                }
            )
        )
        return proc.ProcResult(0, '', '')

    monkeypatch.setattr(proc, 'run', run)
    with pytest.raises(AudioLabError, match='invalid result files'):
        client.separate_managed(outside, installed / 'out', 'htdemucs', 1, Progress.null())
    assert outside.read_bytes() == b'user file'


def test_managed_separation_preserves_cancellation(installed, monkeypatch):
    def cancelled(*args, **kwargs):
        raise Cancelled()

    monkeypatch.setattr(proc, 'run', cancelled)
    with pytest.raises(Cancelled):
        client.separate_managed(installed / 'input.wav', installed / 'out', 'htdemucs', 1, Progress.null())


def test_worker_uses_local_weights_and_retains_bag_settings(tmp_path, monkeypatch):
    definitions = {'models': ['955717e8'], 'weights': [[1, 2, 3, 4]], 'segment': 7.8}
    (tmp_path / 'htdemucs.yaml').write_text('fixture')
    (tmp_path / '955717e8.safetensors').write_bytes(b'fixture')
    monkeypatch.setenv('AUDIOLAB_AI_MODELS_DIR', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'yaml', SimpleNamespace(safe_load=lambda text: definitions))
    loaded = []

    class Bag:
        def __init__(self, models, weights, segment):
            assert models == ['model'] and weights == definitions['weights'] and segment == 7.8

        def eval(self):
            return self

    monkeypatch.setitem(sys.modules, 'demucs.apply', SimpleNamespace(BagOfModels=Bag))
    monkeypatch.setitem(
        sys.modules, 'demucs.hf', SimpleNamespace(load_safetensors_model=lambda path: loaded.append(path) or 'model')
    )
    assert isinstance(worker.load_local_model('htdemucs'), Bag)
    assert loaded == [tmp_path / '955717e8.safetensors']
    (tmp_path / '955717e8.safetensors').unlink()
    with pytest.raises(ValueError, match='missing'):
        worker.load_local_model('htdemucs')


def test_main_app_never_takes_worker_role_from_environment(monkeypatch):
    monkeypatch.setattr(sys, '_audiolab_ai_worker', False, raising=False)
    monkeypatch.setenv('AUDIOLAB_AI_WORKER', '1')
    assert not runtime.is_ai_worker()


def test_process_runner_keeps_inherited_lease_after_parent_releases(tmp_path):
    try:
        system.inherited_descriptors((1,))
    except RuntimeError:
        pytest.skip('The initial managed helper target is POSIX.')
    lock = system.InstanceLock(tmp_path / 'operation.lock')
    assert lock.acquire()
    ready = tmp_path / 'ready'
    script = tmp_path / 'child.py'
    script.write_text(
        'import fcntl, pathlib, sys, time\n'
        'pathlib.Path(sys.argv[2]).touch()\n'
        'time.sleep(0.3)\n'
        'with open(sys.argv[1], "a+") as other:\n'
        '    try:\n'
        '        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
        '    except BlockingIOError:\n'
        '        print("lease retained")\n'
        '    else:\n'
        '        raise RuntimeError("Lease was lost")\n'
    )

    def release_parent():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        lock.release()

    thread = threading.Thread(target=release_parent)
    thread.start()
    try:
        result = proc.run(
            [sys.executable, str(script), str(lock.path), str(ready)], inherited_fds=(lock.fileno,), timeout_s=5
        )
    finally:
        thread.join(6)
        lock.release()
    assert result.returncode == 0 and result.stdout == 'lease retained'
    assert lock.acquire()
    lock.release()
