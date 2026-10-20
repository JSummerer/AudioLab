"""Cross-platform contracts that must not depend on cached metadata or clock resolution."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from audiolab.api import status
from audiolab.media import youtube


def test_storage_uses_file_identity_when_directory_metadata_omits_it(tmp_path, monkeypatch):
    job = tmp_path / 'job'
    job.mkdir()
    upload = job / 'upload.wav'
    kept = tmp_path / 'kept.wav'
    upload.write_bytes(b'W' * 200_000)
    os.link(upload, kept)
    (job / 'job.json').write_bytes(b'{}')
    assert upload.samefile(kept)
    kept_stat = kept.stat()
    seen = {(kept_stat.st_dev, kept_stat.st_ino)}
    original_scandir = os.scandir

    class CachedEntry:
        def __init__(self, entry):
            self.entry, self.path = entry, entry.path

        def is_dir(self, **kwargs):
            return self.entry.is_dir(**kwargs)

        def is_file(self, **kwargs):
            return self.entry.is_file(**kwargs)

        def stat(self, **kwargs):
            # Windows DirEntry.stat() has zeroed link identity; os.stat() has it.
            values = list(self.entry.stat(**kwargs))
            values[1:4] = [0, 0, 0]  # st_ino, st_dev, st_nlink
            return os.stat_result(values)

    class CachedScandir:
        def __init__(self, path):
            self.entries = original_scandir(path)

        def __enter__(self):
            return (CachedEntry(entry) for entry in self.entries.__enter__())

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    monkeypatch.setattr(status.os, 'scandir', CachedScandir)
    assert status._dir_bytes(job, seen) == 2


def test_self_repair_does_not_grant_sequential_retries_at_the_same_clock_tick(monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, '_last_repair', {})
    monkeypatch.setattr(youtube, 'time', SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(
        youtube,
        'update_ytdlp',
        lambda: calls.append(1) or {'ok': True, 'before': 'old', 'after': 'new'},
    )
    assert youtube.self_repair_update() == ('old', 'new')
    assert youtube.self_repair_update() is None
    assert calls == [1]


def test_self_repair_shares_an_update_with_a_waiting_caller_at_the_same_clock_tick(monkeypatch):
    update_started, finish_update, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class ObservedLock:
        """Expose lock contention so the test releases the updater only after its caller waits."""

        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                waiting.set()
                assert self.lock.acquire(timeout=5), 'Waiting caller did not acquire the repair lock'
            return self

        def __exit__(self, *args):
            self.lock.release()

    def update():
        calls.append(1)
        update_started.set()
        assert finish_update.wait(5), 'Updater was not released'
        return {'ok': True, 'before': 'old', 'after': 'new'}

    monkeypatch.setattr(youtube, '_last_repair', {})
    monkeypatch.setattr(youtube, '_repair_lock', ObservedLock())
    monkeypatch.setattr(youtube, 'time', SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(youtube, 'update_ytdlp', update)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(youtube.self_repair_update)
        try:
            assert update_started.wait(5), 'Updater did not start'
            second = pool.submit(youtube.self_repair_update)
            assert waiting.wait(5), 'Second caller did not wait for the updater'
        finally:
            finish_update.set()
        assert first.result(timeout=5) == second.result(timeout=5) == ('old', 'new')
    assert youtube.self_repair_update() is None
    assert calls == [1]
