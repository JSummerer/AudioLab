"""The packaged app's downloader update: tiny stand-in zip builds, a faked GitHub, real downloader-mode imports."""

import hashlib
import io
import json
import subprocess
import sys
import threading
import zipfile

import pytest

from audiolab import updates
from audiolab.errors import AudioLabError
from audiolab.media import ytdlp_update
from audiolab.settings import SETTINGS

NEW = '2099.01.01'  # newer than any yt-dlp installed in this environment
OLD = '2000.01.01'
LOAD = 'from audiolab.media.ytdlp_update import load_downloader; m = load_downloader(); print(m.__file__); '


def build(version: str, broken: bool = False) -> bytes:
    """A zipimport build like yt-dlp's: a yt_dlp package whose main() prints its version."""
    code = (
        "raise ImportError('this update does not work here')\n"
        if broken
        else f'def main(argv=None):\n    print({version!r})\n    raise SystemExit(0)\n'
    )
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr('yt_dlp/__init__.py', code)
        archive.writestr('yt_dlp_ejs/__init__.py', '')
    return target.getvalue()


def fake_github(monkeypatch, blob: bytes, version: str = NEW, digest: str | None = None):
    asset = f'{updates.YTDLP_DOWNLOAD}{version}/yt-dlp'
    release = {
        'tag_name': version,
        'draft': False,
        'prerelease': False,
        'assets': [
            {
                'name': 'yt-dlp',
                'browser_download_url': asset,
                'size': len(blob),
                'digest': 'sha256:' + (digest or hashlib.sha256(blob).hexdigest()),
            }
        ],
    }
    pages = {updates.YTDLP_RELEASE_API: json.dumps(release).encode(), asset: blob}
    monkeypatch.setattr(updates, 'open_github', lambda url, accept='': io.BytesIO(pages[url]))


def run_downloader(code: str) -> subprocess.CompletedProcess:
    """The downloader mode as the frozen entry runs it, in a fresh interpreter (same data folder)."""
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)


def record(version: str, blob: bytes) -> None:
    folder = ytdlp_update.folder()
    folder.mkdir(parents=True, exist_ok=True)
    name = f'yt-dlp-{version}.zip'
    (folder / name).write_bytes(blob)
    info = {'version': version, 'file': name, 'sha256': hashlib.sha256(blob).hexdigest(), 'size': len(blob)}
    (folder / 'active.json').write_text(json.dumps(info))


def test_update_installs_after_checks_runs_first_and_can_be_undone(monkeypatch):
    fake_github(monkeypatch, build(NEW))
    result = ytdlp_update.install_latest()
    assert result['ok'] and result['after'] == NEW and result['before'] != NEW, result
    assert ytdlp_update.state()['source'] == 'updated' and updates.downloader_latest() == NEW
    used = run_downloader(LOAD + 'm.main()')
    assert used.stdout.splitlines()[-1] == NEW and 'yt-dlp-2099.01.01.zip' in used.stdout
    again = ytdlp_update.install_latest()
    assert again['ok'] and again['before'] == again['after'] == NEW  # already newest: nothing fetched twice
    ytdlp_update.use_bundled()
    assert ytdlp_update.state()['source'] == 'bundled'
    assert NEW not in run_downloader(LOAD).stdout


@pytest.mark.parametrize('problem', ['checksum', 'does not start'])
def test_failed_update_changes_nothing(monkeypatch, problem):
    blob = build(NEW, broken=problem == 'does not start')
    fake_github(monkeypatch, blob, digest='0' * 64 if problem == 'checksum' else None)
    result = ytdlp_update.install_latest()
    assert not result['ok'] and result['after'] == result['before']
    assert ('security check' if problem == 'checksum' else 'did not start') in result['message']
    folder = ytdlp_update.folder()
    assert not (folder / 'active.json').exists() and not list(folder.glob('*.zip')) + list(folder.glob('*.part'))
    assert ytdlp_update.state()['source'] == 'bundled'


def test_broken_or_older_update_falls_back_to_the_builtin_copy():
    record(NEW, build(NEW, broken=True))
    first = run_downloader(LOAD)
    assert first.returncode == 0 and 'yt-dlp-2099' not in first.stdout
    assert ytdlp_update.FALLBACK_MARK in first.stderr and ytdlp_update.state()['fallback']
    failure = json.loads((ytdlp_update.folder() / 'failed.json').read_text())
    assert 'did not start' in failure['reason']
    record(OLD, build(OLD))  # e.g. after installing a newer app: its built-in copy wins, no failure
    older = run_downloader(LOAD)
    assert 'yt-dlp-2000' not in older.stdout and ytdlp_update.FALLBACK_MARK not in older.stderr
    assert ytdlp_update.state() == {
        'source': 'bundled',
        'fallback': False,
        'active': ytdlp_update.active(ytdlp_update.folder()),
    }


def test_damaged_update_file_is_not_run():
    record(NEW, build(NEW))
    path = SETTINGS.data_dir / 'downloader' / f'yt-dlp-{NEW}.zip'
    path.write_bytes(path.read_bytes()[:-10] + b'0123456789')  # same size, different bytes
    used = run_downloader(LOAD)
    assert 'yt-dlp-2099' not in used.stdout and 'damaged' in used.stderr
    assert ytdlp_update.state()['fallback']


def test_reset_while_an_update_runs_is_refused_and_the_update_finishes(monkeypatch):
    fake_github(monkeypatch, build(NEW))
    pages, writing, go_on = updates.open_github, threading.Event(), threading.Event()

    class Slow(io.BytesIO):
        def read(self, size=-1):
            writing.set()  # the .part file is open and being written
            go_on.wait(10)
            return super().read(size)

    def slow(url, accept=''):
        response = pages(url, accept)
        return Slow(response.getvalue()) if url.endswith('/yt-dlp') else response

    monkeypatch.setattr(updates, 'open_github', slow)
    results = []
    worker = threading.Thread(target=lambda: results.append(ytdlp_update.install_latest()))
    worker.start()
    assert writing.wait(10)
    with pytest.raises(AudioLabError, match='being updated'):
        ytdlp_update.use_bundled()
    go_on.set()
    worker.join(60)
    assert results and results[0]['ok'] and ytdlp_update.state()['source'] == 'updated'


def test_reset_only_deactivates_and_unused_archives_go_at_app_start(monkeypatch):
    second = '2099.02.01'
    record(NEW, build(NEW))  # a job may still be running from this archive…
    fake_github(monkeypatch, build(second), version=second)
    assert ytdlp_update.install_latest()['after'] == second
    ytdlp_update.use_bundled()  # …while the user goes back to the built-in copy
    folder = ytdlp_update.folder()
    assert ytdlp_update.state()['source'] == 'bundled' and not (folder / 'active.json').exists()
    assert {p.name for p in folder.glob('*.zip')} == {f'yt-dlp-{NEW}.zip', f'yt-dlp-{second}.zip'}
    record(second, build(second))
    ytdlp_update.prune_unused()  # app start: no downloader of this app can be running
    assert [p.name for p in folder.glob('*.zip')] == [f'yt-dlp-{second}.zip']
