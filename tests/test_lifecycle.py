"""Bounded lifecycle barriers, cancellable Library preparation and owned process teardown."""

import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audiolab import library, system, util
from audiolab.api import create_app
from audiolab.errors import AudioLabError, Cancelled
from audiolab.jobs import CANCELLED, DONE, FAILED, RUNNING, JobManager
from audiolab.settings import SETTINGS

STAGES = [('a', 'Working', 1)]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition not reached')


def manager_at(tmp_path, runner=lambda job, ctx: {}):
    return JobManager(runner, lambda job: STAGES, root=tmp_path / 'jobs', workers=1)


@pytest.mark.parametrize('status', [DONE, FAILED, CANCELLED])
@pytest.mark.parametrize('remove', ['delete', 'clear_finished'])
def test_final_persistence_serializes_removal(tmp_path, monkeypatch, status, remove):
    manager = manager_at(tmp_path)
    job = manager.submit('analyze', {}, {})
    job.status = RUNNING
    entered, release, attempting, removed = (threading.Event() for _ in range(4))
    events = []
    manager.subscribe(lambda kind, payload: events.append((kind, payload)))
    journal = manager._journal

    def paused_journal(job_id, kind, *args, **kwargs):
        if kind == status:
            entered.set()
            assert release.wait(5)
        journal(job_id, kind, *args, **kwargs)

    def remove_job():
        attempting.set()
        if remove == 'delete':
            assert manager.delete(job.id)
        else:
            assert manager.clear_finished() == [job.id]
        removed.set()

    monkeypatch.setattr(manager, '_journal', paused_journal)
    worker = threading.Thread(target=manager._finish, args=(job, status, 'Settled'))
    remover = threading.Thread(target=remove_job)
    worker.start()
    try:
        assert entered.wait(5)
        remover.start()
        assert attempting.wait(5)
        assert not removed.wait(0.05)  # removal cannot observe a half-persisted terminal state
    finally:
        release.set()
        worker.join(5)
        if remover.ident:
            remover.join(5)
    assert removed.is_set() and not manager.job_dir(job.id).exists()
    assert [(kind, data.get('status')) for kind, data in events] == [('job', status), ('removed', None)]
    manager._emit_job(job, force=True)  # a retained object is no longer eligible to emit
    again = manager_at(tmp_path)
    again._load()
    assert again.get(job.id) is None
    assert len(events) == 2


@pytest.mark.parametrize('late_error', [False, True])
def test_forced_shutdown_fences_late_worker_callbacks(tmp_path, late_error):
    entered, release = threading.Event(), threading.Event()
    contexts = []

    def runner(job, ctx):
        contexts.append(ctx)
        entered.set()
        assert release.wait(5)
        ctx.note('warning', 'Late note')
        ctx._shared.emit(99, 'Late state')  # an already queued progress callback
        if late_error:
            raise AudioLabError('Late failure')
        return {'headline': 'Late result'}

    manager = manager_at(tmp_path, runner)
    manager.start()
    job = manager.submit('analyze', {}, {})
    try:
        assert entered.wait(5)
        manager.shutdown(timeout=0)
        assert manager.get(job.id).status == CANCELLED
        assert manager.delete(job.id)
        events = []
        manager.subscribe(lambda kind, payload: events.append((kind, payload)))
        release.set()
        for thread in manager._threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in manager._threads)
        assert not manager.job_dir(job.id).exists() and events == []
        again = manager_at(tmp_path)
        again._load()
        assert again.get(job.id) is None
    finally:
        release.set()
        manager.shutdown()


def result_runner(original, outputs):
    def runner(job, ctx):
        ctx.stage('a')(100)
        output = SETTINGS.output_dir / f'{job.id}.wav'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'tiny generated result')
        outputs.append(output)
        return {
            'headline': 'Done',
            'files': [{'id': 'audio', 'path': str(output)}],
            '_library': {'original': str(original)},
        }

    return runner


@pytest.mark.parametrize('phase', ['hash', 'copy', 'cleanup'])
def test_cancel_during_library_preparation_is_effective(tmp_path, monkeypatch, phase):
    original = tmp_path / 'source.wav'
    original.write_bytes(b'generated' * 150_000)  # two streamed blocks, no real audio
    outputs = []
    entered, release = threading.Event(), threading.Event()
    first = True
    if phase == 'hash':
        sha1 = library.hashlib.sha1

        class PausedHash:
            def __init__(self):
                self.inner = sha1()

            def update(self, block):
                nonlocal first
                self.inner.update(block)
                if first:
                    first = False
                    entered.set()
                    assert release.wait(5)

            def hexdigest(self):
                return self.inner.hexdigest()

        monkeypatch.setattr(library.hashlib, 'sha1', PausedHash)
    elif phase == 'copy':

        def no_link(*args):
            raise OSError(errno.EXDEV, 'different volume')

        monkeypatch.setattr(util.os, 'link', no_link)
        copy = library.link_or_copy

        def paused_copy(src, dst, check_cancel):
            calls = 0

            def check():
                nonlocal calls, first
                calls += 1
                if calls == 2 and first:  # inside fallback copy, after reading the first block
                    first = False
                    entered.set()
                    assert release.wait(5)
                check_cancel()

            return copy(src, dst, check)

        monkeypatch.setattr(library, 'link_or_copy', paused_copy)
    else:
        rmtree = shutil.rmtree

        def paused_cleanup(path, *args, **kwargs):
            nonlocal first
            if Path(path).name == 'scratch' and first:
                first = False
                entered.set()
                assert release.wait(5)
            return rmtree(path, *args, **kwargs)

        monkeypatch.setattr('audiolab.jobs.shutil.rmtree', paused_cleanup)
    manager = manager_at(tmp_path, result_runner(original, outputs))
    # No app lifespan: no AI warmup, keep-awake process, listener or real server.
    client = TestClient(create_app(local=False, token='t', manager=manager))
    manager.on_running_changed = None
    manager.on_finished = None
    manager.start()
    job = manager.submit('enhance', {'kind': 'file', 'path': str(original)}, {})
    try:
        assert entered.wait(5)
        assert manager.public(job.id)['message'] == 'Finishing…'
        response = client.post(f'/api/jobs/{job.id}/cancel', headers={'X-AudioLab-Token': 't'})
        assert response.status_code == 200 and response.json()['message'] == 'Cancelling…'
        release.set()
        wait_for(lambda: manager.get(job.id).status == CANCELLED)
        assert library.get(job.id) is None and list(library.originals_dir().iterdir()) == []
        assert original.is_file() and all(path.is_file() for path in outputs)
        manager.retry(job.id)
        wait_for(lambda: manager.get(job.id).status == DONE)
        assert library.get(job.id)['original'] and all(path.is_file() for path in outputs)
    finally:
        release.set()
        manager.shutdown()
        client.close()


@pytest.mark.parametrize('action', ['cancel', 'shutdown'])
def test_publication_cutoff_serializes_cancel_and_shutdown(tmp_path, monkeypatch, action):
    original = tmp_path / 'source.wav'
    original.write_bytes(b'tiny original')
    outputs, contexts, responses = [], [], []
    entered, release, attempted, returned = (threading.Event() for _ in range(4))
    runner = result_runner(original, outputs)

    def run(job, ctx):
        contexts.append(ctx)
        return runner(job, ctx)

    write = library._write

    def paused_write(entry):
        entered.set()
        assert release.wait(5)
        write(entry)

    monkeypatch.setattr(library, '_write', paused_write)
    manager = manager_at(tmp_path, run)
    client = TestClient(create_app(local=False, token='t', manager=manager))
    manager.on_running_changed = None
    manager.on_finished = None
    manager.start()
    job = manager.submit('enhance', {'kind': 'file', 'path': str(original)}, {})

    def act():
        attempted.set()
        if action == 'cancel':
            responses.append(client.post(f'/api/jobs/{job.id}/cancel', headers={'X-AudioLab-Token': 't'}))
        else:
            manager.shutdown(timeout=0)
        returned.set()

    thread = threading.Thread(target=act)
    try:
        assert entered.wait(5)
        thread.start()
        assert attempted.wait(5) and not returned.wait(0.05)
        release.set()
        thread.join(5)
        assert returned.is_set()
        wait_for(lambda: manager.get(job.id).status == DONE)
        assert not contexts[0].cancelled
        if responses:
            assert responses[0].status_code == 400
            assert 'already being saved' in responses[0].json()['detail']
        entry = library.get(job.id)
        assert entry and manager.get(job.id).result['library_id'] == job.id
        assert all(path.is_file() for path in outputs)
        assert json.loads((manager.job_dir(job.id) / 'job.json').read_text())['status'] == DONE
    finally:
        release.set()
        if thread.ident:
            thread.join(5)
        manager.shutdown()
        client.close()


def test_hash_checks_each_block_and_reuses_only_this_records_digest(tmp_path, monkeypatch):
    original = tmp_path / 'source.wav'
    original.write_bytes(b'x' * ((1 << 20) + 1))
    checks = 0

    def cancel_second_block():
        nonlocal checks
        checks += 1
        if checks == 3:  # initial check, first block, second block
            raise Cancelled()

    with pytest.raises(Cancelled):
        library.file_sha1(original, cancel_second_block)
    assert checks == 3
    sha1, reads = library._sha1, []

    def counted(path, check):
        reads.append(path)
        return sha1(path, check)

    monkeypatch.setattr(library, '_sha1', counted)
    outputs = []
    manager = manager_at(tmp_path, result_runner(original, outputs))
    manager.start()
    try:
        job = manager.submit('enhance', {'kind': 'file', 'path': str(original)}, {})
        wait_for(lambda: manager.get(job.id).status == DONE)
        assert reads == [original]  # source and kept original are the same file
        assert library.get(job.id)['group'] == f'file:{hashlib.sha1(original.read_bytes()).hexdigest()}'
    finally:
        manager.shutdown()


def test_cancelled_publication_preserves_shared_original_and_output(tmp_path, monkeypatch):
    original = tmp_path / 'source.wav'
    original.write_bytes(b'shared original')
    outputs = []
    manager = manager_at(tmp_path, result_runner(original, outputs))
    manager.start()
    entered, release = threading.Event(), threading.Event()
    try:
        first = manager.submit('enhance', {'kind': 'file', 'path': str(original)}, {})
        wait_for(lambda: manager.get(first.id).status == DONE)
        entry = library.get(first.id)
        kept = library.file_path(entry, library.ORIGINAL)
        contents = kept.read_bytes()
        copy = library.link_or_copy

        def pause_after_staging(*args):
            assert args[0] == kept  # reuse the kept file on its own volume rather than copying again
            staged = copy(*args)
            entered.set()
            assert release.wait(5)
            return staged

        monkeypatch.setattr(library, 'link_or_copy', pause_after_staging)
        second = manager.submit('enhance', {'kind': 'file', 'path': str(original)}, {})
        assert entered.wait(5)
        manager.cancel(second.id)
        release.set()
        wait_for(lambda: manager.get(second.id).status == CANCELLED)
        assert library.get(second.id) is None and library.get(first.id) == entry
        assert list(library.originals_dir().iterdir()) == [kept] and kept.read_bytes() == contents
        assert original.is_file() and all(path.is_file() for path in outputs)
    finally:
        release.set()
        manager.shutdown()


def test_old_attempt_callbacks_cannot_change_retry(tmp_path):
    entered, release, settled, release_finished = (threading.Event() for _ in range(4))
    contexts = []

    def runner(job, ctx):
        contexts.append(ctx)
        if len(contexts) == 1:
            raise AudioLabError('Please retry')
        entered.set()
        assert release.wait(5)
        return {}

    manager = manager_at(tmp_path, runner)

    def on_finished(job):
        if job.status == FAILED:
            settled.set()
            assert release_finished.wait(5)  # retain the old context until after retry is queued

    manager.on_finished = on_finished
    manager.start()
    try:
        job = manager.submit('analyze', {}, {})
        assert settled.wait(5)
        manager.retry(job.id)
        assert manager.get(job.id).status == 'queued'
        before, journal = manager.public(job.id), manager.journal(job.id)
        contexts[0].note('warning', 'old queued attempt')
        contexts[0]._shared.emit(99, 'old queued state')
        assert manager.public(job.id) == before and manager.journal(job.id) == journal
        release_finished.set()
        assert entered.wait(5)
        before = manager.public(job.id)
        journal = manager.journal(job.id)
        contexts[0].note('warning', 'old attempt')
        contexts[0]._shared.emit(99, 'old state')
        assert manager.public(job.id) == before and manager.journal(job.id) == journal
        release.set()
        wait_for(lambda: manager.get(job.id).status == DONE)
    finally:
        release_finished.set()
        release.set()
        manager.shutdown()


def test_shutdown_during_library_preparation_cannot_publish_late(tmp_path, monkeypatch):
    original = tmp_path / 'source.wav'
    original.write_bytes(b'tiny original')
    entered, release = threading.Event(), threading.Event()
    sha1 = library._sha1

    def paused_hash(path, check):
        entered.set()
        assert release.wait(5)
        return sha1(path, check)

    monkeypatch.setattr(library, '_sha1', paused_hash)
    outputs = []
    manager = manager_at(tmp_path, result_runner(original, outputs))
    manager.start()
    job = manager.submit('enhance', {'kind': 'file', 'path': str(original)}, {})
    try:
        assert entered.wait(5)
        manager.shutdown(timeout=0)
        assert manager.get(job.id).status == CANCELLED
        manager.delete(job.id)
        release.set()
        for thread in manager._threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in manager._threads)
        assert not manager.job_dir(job.id).exists() and library.get(job.id) is None
        assert list(library.originals_dir().iterdir()) == []
        assert original.is_file() and all(path.is_file() for path in outputs)
    finally:
        release.set()
        manager.shutdown()


@pytest.mark.skipif(os.name != 'posix', reason='real POSIX private process groups')
@pytest.mark.parametrize('leader_mode', ['cooperative', 'exited', 'stubborn'])
def test_owned_group_stops_descendant_after_leader_exit(tmp_path, monkeypatch, leader_mode):
    monkeypatch.setattr(system, '_TERM_GRACE_S', 0.2)
    heartbeat = tmp_path / 'heartbeat'
    ready = tmp_path / 'ready'
    child = (
        'import pathlib,signal,sys,time; '
        'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        'p=pathlib.Path(sys.argv[1]); pathlib.Path(sys.argv[2]).write_text("ready"); '
        'end=time.monotonic()+8; '
        '\nwhile time.monotonic()<end:\n p.write_text(str(time.monotonic())); time.sleep(.02)\n'
    )
    leader = (
        'import pathlib,signal,subprocess,sys,time; '
        + ('signal.signal(signal.SIGTERM,signal.SIG_IGN); ' if leader_mode == 'stubborn' else '')
        + 'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2],sys.argv[3]]); '
        '\nwhile not pathlib.Path(sys.argv[3]).exists(): time.sleep(.01)\n'
        + ('sys.exit(0)\n' if leader_mode == 'exited' else 'time.sleep(8)\n')
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', leader, child, str(heartbeat), str(ready)], **system.own_process_group()
    )
    system.register_process_group(proc)
    try:
        wait_for(lambda: ready.exists() and heartbeat.exists())
        if leader_mode == 'exited':
            assert proc.wait(timeout=3) == 0
        before = heartbeat.read_text()
        # Observe real work without assuming the runner schedules the child within 50 ms.
        wait_for(lambda: (current := heartbeat.read_text()) and current != before)
        start = time.monotonic()
        system.terminate_process(proc)
        assert time.monotonic() - start < 2 and proc.poll() is not None
        time.sleep(0.05)  # allow the delivered KILL to be scheduled before taking the baseline
        stopped = heartbeat.read_text()
        time.sleep(0.1)
        assert heartbeat.read_text() == stopped
        monkeypatch.setattr(system.os, 'killpg', lambda *args: pytest.fail('repeat teardown signalled again'))
        system.terminate_process(proc)
    finally:
        monkeypatch.undo()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX group safety guard')
def test_own_group_is_never_signalled(monkeypatch):
    class Child:
        pid = os.getpgrp()

        def poll(self):
            return 0

    child = Child()
    system.register_process_group(child)
    monkeypatch.setattr(system.os, 'killpg', lambda *args: pytest.fail('signalled app group'))
    system.terminate_process(child)


def test_fast_owned_process_exit_is_safe_to_repeat():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'], **system.own_process_group())
    system.register_process_group(proc)
    proc.wait(timeout=3)
    system.terminate_process(proc)
    system.terminate_process(proc)


def test_subprocess_runner_registers_private_identity(monkeypatch):
    from audiolab.media import proc

    registered = []
    register = proc.register_process_group

    def remember(child):
        registered.append(child)
        register(child)

    monkeypatch.setattr(proc, 'register_process_group', remember)
    result = proc.run([sys.executable, '-c', 'print("ready")'])
    assert result.returncode == 0 and result.stdout == 'ready'
    assert len(registered) == 1


@pytest.mark.skipif(os.name != 'posix', reason='POSIX unknown-group safety')
def test_unregistered_process_does_not_guess_a_group(monkeypatch):
    class Child:
        pid = 123
        returncode = None
        signals = []

        def poll(self):
            return self.returncode

        def send_signal(self, sig):
            self.signals.append(sig)
            self.returncode = -sig

        def wait(self, timeout):
            return self.returncode

    child = Child()
    monkeypatch.setattr(system.os, 'killpg', lambda *args: pytest.fail('guessed process group'))
    monkeypatch.setattr(system.os, 'getpgid', lambda *args: pytest.fail('looked up arbitrary process group'))
    system.terminate_process(child)
    assert child.signals == [signal.SIGTERM]


def test_windows_teardown_keeps_tree_kill_and_fallback(monkeypatch):
    commands = []

    class Child:
        pid = 123
        waits = 0
        killed = False

        def poll(self):
            return None

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired('owned child', timeout)
            return -1

        def kill(self):
            self.killed = True

    child = Child()
    with monkeypatch.context() as patch:
        patch.setattr(system.os, 'name', 'nt')
        patch.setattr(system.subprocess, 'run', lambda cmd, **kwargs: commands.append(cmd))
        system.terminate_process(child)
    assert commands == [['taskkill', '/T', '/F', '/PID', '123']]
    assert child.killed and child.waits == 2
