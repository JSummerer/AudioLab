"""Updates of the packaged app's built-in downloader (yt-dlp), kept in the app data folder.

The frozen app cannot pip-install. Instead "Update downloader" fetches yt-dlp's official zipimport build
(one file that also carries the matching YouTube challenge-solver package) into `<data>/downloader/`, and the
app's private downloader mode (packaging/macos/entry.py → `load_downloader`) puts that file first on
`sys.path` before importing yt_dlp. The copy built into the app stays the fallback: an update is used only
when it is newer than the built-in version, matches its recorded checksum and imports cleanly; otherwise the
built-in copy runs and `failed.json` records why (Health shows `downloader.fallback`).

What the checks cover: the size and checksum (both from GitHub) catch damage in transit and on disk, not a
compromised release; the recorded checksum lives in the same user-writable data folder, so nothing vouches for
the archive against another process running as this user (the same exposure as the AI pack). An update that
imports but breaks at run time is not rolled back by itself; download errors then name "Use built-in version".

Folder layout: `active.json` ({version, file, sha256, size}), the active `yt-dlp-<version>.zip`, and
`failed.json` after a failed start. Archives no longer named by `active.json` stay while the app runs (a running
downloader imports lazily from its archive) and are deleted at the next app start (`prune_unused`).
Keep this module's top level light: the downloader subprocess imports it on every start.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from ..settings import SETTINGS

CANDIDATE_ENV = 'AUDIOLAB_DOWNLOADER_CANDIDATE'  # the installer's start check: run exactly this file
FALLBACK_MARK = 'AL_DOWNLOADER_FALLBACK'
_ACTIVE, _FAILED = 'active.json', 'failed.json'
_FILE = re.compile(r'yt-dlp-(\d{4}\.\d{2}\.\d{2}(?:\.\d{1,6})?)\.zip\Z')
_VERSION = re.compile(r'\d{4}\.\d{2}\.\d{2}(?:\.\d{1,6})?\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_BLOCK = 256 * 1024
_install_lock = threading.Lock()  # one update at a time; a second click waits and then finds it up to date


def folder(data_dir: Path | None = None) -> Path:
    return Path(data_dir if data_dir is not None else SETTINGS.data_dir) / 'downloader'


def _key(version: Any) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not re.fullmatch(r'\d{1,4}(?:\.\d{1,6}){1,3}', version.strip()):
        return None
    return tuple(int(part) for part in version.strip().split('.'))


def newer(candidate: Any, than: Any) -> bool:
    new, old = _key(candidate), _key(than)
    return new is not None and (old is None or new > old)


@functools.lru_cache(maxsize=1)
def bundled_version() -> str | None:
    """The yt-dlp shipped with this app (or installed in this Python), from its package metadata, written the way
    yt-dlp itself prints it (metadata says 2026.8.19, `yt-dlp --version` 2026.08.19)."""
    try:
        import importlib.metadata

        version = importlib.metadata.version('yt-dlp')
    except Exception:  # noqa: BLE001 - metadata missing or unreadable: treat as unknown
        return None
    m = re.fullmatch(r'(\d{4})\.(\d{1,2})\.(\d{1,2})((?:\.\d+)?)', version)
    return f'{m[1]}.{int(m[2]):02d}.{int(m[3]):02d}{m[4]}' if m else version


def _read(path: Path, limit: int = 4096) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f'.{path.name}-{uuid.uuid4().hex}')
    try:
        temporary.write_text(json.dumps(value, sort_keys=True), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def active(directory: Path) -> dict[str, Any] | None:
    """The recorded update, if its record is well-formed (the file itself is checked by `_usable`)."""
    info = _read(directory / _ACTIVE)
    match = _FILE.fullmatch(info.get('file') or '') if isinstance(info.get('file'), str) else None
    if (
        not match
        or info.get('version') != match.group(1)
        or not isinstance(info.get('sha256'), str)
        or not _SHA256.fullmatch(info['sha256'])
        or type(info.get('size')) is not int
        or info['size'] <= 0
    ):
        return None
    return info


def failed(directory: Path, info: dict[str, Any] | None) -> bool:
    """This exact update already failed to start (`failed.json` names its checksum)."""
    return bool(info) and _read(directory / _FAILED).get('sha256') == info['sha256']


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _record_failure(directory: Path, info: dict[str, Any], reason: str) -> None:
    try:
        _write(directory / _FAILED, {'version': info['version'], 'sha256': info['sha256'], 'reason': reason[:300]})
    except OSError:
        pass  # the built-in downloader runs either way
    sys.stderr.write(f'{FALLBACK_MARK} {info["version"]}: {reason[:300]}\n')


def _usable(directory: Path) -> Path | None:
    """The update file to run, or None for the built-in copy (recording a failure when the file is bad)."""
    info = active(directory)
    if info is None or failed(directory, info) or not newer(info['version'], bundled_version()):
        return None  # none, already failed, or a newer app's built-in copy wins
    path = directory / info['file']
    try:
        ok = path.is_file() and not path.is_symlink() and path.stat().st_size == info['size']
        ok = ok and _sha256(path) == info['sha256']
    except OSError:
        ok = False
    if not ok:
        _record_failure(directory, info, 'The downloaded update is missing or damaged.')
        return None
    return path


def _forget(path: Path) -> None:
    """Undo a failed import of the update so the built-in copy imports cleanly."""
    entry = str(path)
    while entry in sys.path:
        sys.path.remove(entry)
    sys.path_importer_cache.pop(entry, None)
    for name in [n for n in sys.modules if n.split('.')[0] in ('yt_dlp', 'yt_dlp_ejs')]:
        del sys.modules[name]
    importlib.invalidate_caches()


def load_downloader(data_dir: Path | None = None):
    """Import yt_dlp for the downloader mode: a usable newer update first, else the built-in copy."""
    candidate = os.environ.get(CANDIDATE_ENV)
    if candidate:  # the installer's start check runs exactly this file and must fail loudly, never fall back
        sys.path.insert(0, candidate)
        import yt_dlp

        return yt_dlp
    directory = folder(data_dir)
    path = _usable(directory)
    if path is not None:
        sys.path.insert(0, str(path))
        try:
            import yt_dlp

            return yt_dlp
        except Exception as exc:  # noqa: BLE001 - whatever breaks in the update, the built-in copy must run
            _forget(path)
            info = active(directory)
            if info is not None:
                _record_failure(directory, info, f'It did not start: {type(exc).__name__}: {exc}')
    import yt_dlp

    return yt_dlp


def state(data_dir: Path | None = None) -> dict[str, Any]:
    """What the packaged app uses, from the records only (Health; the actual version comes from the worker)."""
    directory = folder(data_dir)
    info = active(directory)
    fallback = failed(directory, info)
    updated = bool(info) and not fallback and newer(info['version'], bundled_version())
    return {'source': 'updated' if updated else 'bundled', 'fallback': fallback, 'active': info}


def current_version(data_dir: Path | None = None) -> str | None:
    """The version the packaged downloader runs, as far as the records tell."""
    st = state(data_dir)
    return st['active']['version'] if st['source'] == 'updated' else bundled_version()


# ------------------------------------------------------------------ install
def _check_command() -> list[str]:
    """The downloader start, honouring CANDIDATE_ENV: the app's own downloader mode when packaged."""
    from .. import runtime

    if runtime.is_packaged():
        return [*runtime.downloader_command(), '--version']
    code = 'import sys; from audiolab.media.ytdlp_update import load_downloader; load_downloader().main(sys.argv[1:])'
    return [sys.executable, '-c', code, '--version']


def _download(release: dict[str, Any], target: Path) -> None:
    """Fetch the zipimport build into `target`, checking its size and checksum (GitHub's own digest, else the
    release's SHA2-256SUMS). Both come from GitHub: they catch damage, not a compromised release."""
    from .. import updates

    expected = release.get('sha256')
    if expected is None:
        with updates.open_github(release['url'].rsplit('/', 1)[0] + '/SHA2-256SUMS', 'text/plain') as response:
            sums = updates.read_limited(response, 64 * 1024).decode('utf-8', 'replace')
        found = re.search(r'^([0-9a-fA-F]{64})\s+\*?' + re.escape(updates.YTDLP_ASSET) + r'$', sums, re.MULTILINE)
        if not found:
            raise updates.UpdateCheckError('The downloader release has no checksum, so AudioLab did not use it.')
        expected = found.group(1).lower()
    digest, size = hashlib.sha256(), 0
    with updates.open_github(release['url'], 'application/octet-stream') as response:
        with target.open('wb') as out:
            while True:
                try:
                    block = response.read(_BLOCK)
                except Exception as exc:  # noqa: BLE001
                    raise updates.UpdateCheckError('The download was interrupted. Try again.') from exc
                if not block:
                    break
                size += len(block)
                if size > release['size']:
                    raise updates.UpdateCheckError('The downloader file was larger than announced, so it was not used.')
                digest.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
    if size != release['size'] or digest.hexdigest() != expected:
        raise updates.UpdateCheckError('The downloaded file failed its security check, so it was not used.')


def _starts(path: Path, version: str) -> str | None:
    """Run the candidate's `--version`; None when it reports `version`, else a short reason."""
    from . import proc

    try:
        result = proc.run(
            _check_command(),
            timeout_s=120,
            what='downloader check',
            env={**os.environ, CANDIDATE_ENV: str(path)},
        )
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:200]
    reported = (result.stdout or '').strip().splitlines()[-1:] or ['']
    if result.returncode == 0 and _key(reported[0]) == _key(version):
        return None
    return (result.stderr_tail or reported[0] or f'exit code {result.returncode}').strip()[-200:]


def prune_unused(data_dir: Path | None = None) -> None:
    """Delete update archives and partial downloads that `active.json` does not name. Only at app start: a
    downloader that is running imports lazily from its archive, so while the app runs no archive is deleted.
    Optional cleanup: a file or folder that can't be read or deleted is logged and left, never raised."""
    import logging

    log = logging.getLogger('audiolab')
    directory = folder(data_dir)
    with _install_lock:
        try:
            if not directory.is_dir():
                return
            info = active(directory)
            items = list(directory.iterdir())
        except OSError as exc:
            log.warning('Could not look for unused downloader updates: %s', exc)
            return
        for item in items:
            unused = _FILE.fullmatch(item.name) or item.name.endswith('.part')
            if not unused or (info is not None and item.name == info['file']):
                continue
            try:
                if item.is_file() and not item.is_symlink():
                    item.unlink(missing_ok=True)
            except OSError as exc:
                log.warning('Could not delete an unused downloader update: %s', exc)


def install_latest(progress=None, data_dir: Path | None = None) -> dict[str, Any]:
    """Fetch and switch to yt-dlp's newest stable release: {ok, before, after, message}. The downloader in use
    changes only after the new file passed its size, checksum and start checks."""
    with _install_lock:
        return _install(progress, data_dir)


def _install(progress, data_dir: Path | None) -> dict[str, Any]:
    from .. import updates
    from ..context import Progress

    progress = progress or Progress.null()
    directory = folder(data_dir)
    before = current_version(data_dir)
    progress(5, 'Looking for a newer downloader…')
    try:
        release = updates.ytdlp_release()
    except updates.UpdateCheckError as exc:
        return {'ok': False, 'before': before, 'after': before, 'message': f'Could not update the downloader: {exc}'}
    updates.note_downloader_latest(release['version'])
    if not newer(release['version'], before):
        return {
            'ok': True,
            'before': before,
            'after': before,
            'message': f'The downloader is already up to date ({before}).',
        }
    directory.mkdir(parents=True, exist_ok=True)
    name = f'yt-dlp-{release["version"]}.zip'
    partial = directory / f'{name}.{uuid.uuid4().hex}.part'
    try:
        progress(20, f'Downloading the downloader {release["version"]}…')
        try:
            _download(release, partial)
        except updates.UpdateCheckError as exc:
            return {
                'ok': False,
                'before': before,
                'after': before,
                'message': f'Could not update the downloader: {exc}',
            }
        target = directory / name
        # The same release fetched again (e.g. after "Use built-in version") has the same bytes; a downloader
        # started earlier may still read the existing file, so it stays as it is.
        reused = target.is_file() and not target.is_symlink() and _sha256(target) == _sha256(partial)
        if not reused:
            os.replace(partial, target)
        progress(70, 'Checking that it starts…')
        problem = _starts(target, release['version'])
        if problem:
            if not reused:
                target.unlink(missing_ok=True)  # only what this install created; it never ran outside the check
            return {
                'ok': False,
                'before': before,
                'after': before,
                'message': f'The new downloader did not start on this computer, so AudioLab keeps {before}. ({problem})',
            }
        _write(
            directory / _ACTIVE,
            {'version': release['version'], 'file': name, 'sha256': _sha256(target), 'size': target.stat().st_size},
        )
        (directory / _FAILED).unlink(missing_ok=True)  # older archives stay until the next app start
    finally:
        partial.unlink(missing_ok=True)
    progress(100)
    return {
        'ok': True,
        'before': before,
        'after': release['version'],
        'message': f'Downloader updated: {before} → {release["version"]}.',
    }


def use_bundled(data_dir: Path | None = None) -> None:
    """Back to the built-in downloader: only deactivates the update. Its archive stays (a running job may still
    import from it) until `prune_unused` at the next app start. Refused while an update is being installed."""
    from ..errors import AudioLabError

    if not _install_lock.acquire(blocking=False):
        raise AudioLabError(
            'The downloader is being updated right now.',
            hints=['Wait until the update has finished, then choose "Use built-in version" again.'],
        )
    try:
        directory = folder(data_dir)
        (directory / _ACTIVE).unlink(missing_ok=True)
        (directory / _FAILED).unlink(missing_ok=True)
    finally:
        _install_lock.release()
