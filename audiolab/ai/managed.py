"""App-owned, optional AI helper: pinned download, staged validation and exclusive use.

The stable operation lock deliberately lives outside the removable AI directory.
An inference worker inherits its descriptor, so a crashed parent cannot unlock a
helper that is still reading its models. Nothing here adopts a source Python's
packages or shared model caches.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .. import system
from ..context import Progress
from ..errors import AudioLabError, Cancelled
from ..media import proc
from ..settings import SETTINGS

CATALOG_PATH = Path(__file__).with_name('catalog.json')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z')
_TRANSACTION = re.compile(r'[a-f0-9]{32}\Z')
_MAX_DOWNLOAD = 20 * 1024**3
_MAX_INSTALLED = 40 * 1024**3
_MAX_FILES = 100_000
_BLOCK = 256 * 1024
_MODELS = ['htdemucs', 'htdemucs_ft']
_DOWNLOAD_HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
_RESERVED = {'cache', 'tmp', 'staging', 'active.json', 'activation.json', 'download.json', 'download.part'}
_OWNER_FILE = '.managed-root.json'
_OWNER = {'schema': 1, 'owner': 'io.github.jsummerer.audiolab.ai'}


def _download_url(url: str, *, catalog: bool = False) -> None:
    if not isinstance(url, str):
        raise ValueError('AI download URL is invalid.')
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != 'https'
        or parsed.hostname not in _DOWNLOAD_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError('AI download must use the official HTTPS release asset.')
    if catalog and (
        parsed.hostname != 'github.com'
        or parsed.query
        or not parsed.path.startswith('/JSummerer/AudioLab/releases/download/')
        or len(parsed.path.split('/')) != 7
        or any(p in ('.', '..') for p in urllib.parse.unquote(parsed.path).split('/'))
    ):
        raise ValueError('AI catalog does not name an official release asset.')


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_catalog(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.stat().st_size > 16_384:
            raise ValueError('AI catalog is too large.')
        item = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(item, dict):
            raise ValueError('AI catalog must be an object.')
        if type(item.get('schema')) is not int or type(item.get('protocol')) is not int:
            raise ValueError('AI catalog protocol is invalid.')
        if item.get('schema') != 1 or item.get('protocol') != 1:
            raise ValueError('AI catalog protocol is unsupported.')
        version = item.get('version')
        if not isinstance(version, str) or not _VERSION.fullmatch(version) or version in ('.', '..'):
            raise ValueError('AI catalog version is invalid.')
        if version in _RESERVED:
            raise ValueError('AI catalog version uses a reserved name.')
        if item.get('platform') != 'macos-arm64' or item.get('executable') != 'AudioLabAI':
            raise ValueError('AI catalog platform or executable is unsupported.')
        if item.get('models') != _MODELS:
            raise ValueError('AI catalog must include both supported models.')
        if not isinstance(item.get('sha256'), str) or not re.fullmatch(r'[a-fA-F0-9]{64}', item['sha256']):
            raise ValueError('AI catalog checksum is invalid.')
        for field, limit in [('download_bytes', _MAX_DOWNLOAD), ('installed_bytes', _MAX_INSTALLED)]:
            if type(item.get(field)) is not int or not 0 < item[field] <= limit:
                raise ValueError(f'AI catalog {field} is invalid.')
        _download_url(item['url'], catalog=True)
        return item, None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f'This app has no compatible AI download ({exc}).'


def _json(path: Path) -> dict[str, Any]:
    """Metadata is advisory only; never interpret its values as filesystem paths."""
    try:
        if path.is_symlink() or path.stat().st_size > 32_768:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _sync_directory(path: Path) -> None:
    """Persist rename ordering where the filesystem supports directory fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ManagedAI:
    def __init__(self, data_dir: Path | None = None, catalog_path: Path | None = None):
        self.data_dir = Path(data_dir if data_dir is not None else SETTINGS.data_dir).absolute()
        self.root = self.data_dir / 'ai'
        self.catalog_path = Path(catalog_path if catalog_path is not None else CATALOG_PATH)
        self.catalog, self.catalog_error = _read_catalog(self.catalog_path)
        self.running = False
        self.ok: bool | None = None
        self.log: deque[str] = deque(maxlen=40)
        self._guard = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._phase: str | None = None
        self._probe_cache: tuple[tuple, dict[str, Any]] | None = None
        self._storage_cache: tuple[float, int] | None = None

    def _lock(self) -> system.InstanceLock:
        path = self.data_dir / '.ai-operation.lock'
        if path.is_symlink():
            raise AudioLabError('The AI operation lock is a link. Restore the app data folder before trying again.')
        return system.InstanceLock(path)

    def _check_output(self) -> None:
        """Existing settings and environment overrides can predate API path validation."""
        try:
            output, root = SETTINGS.output_dir.resolve(), self.root.resolve()
        except (OSError, RuntimeError) as exc:
            raise AudioLabError('The output and AI folders could not be checked safely.') from exc
        if output == root or output in root.parents or root in output.parents:
            raise AudioLabError(
                'The output folder overlaps the managed AI folder. AudioLab will not change the AI pack.',
                hints=['Choose an output folder outside the app data folder in Settings → Files.'],
            )

    def _mark_root(self) -> None:
        """Claim only an empty directory; a crashed incomplete marker is never adopted."""
        temporary = self.root / f'{_OWNER_FILE}-{uuid.uuid4().hex}'
        try:
            with temporary.open('x', encoding='utf-8') as out:
                json.dump(_OWNER, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            system.replace_file(temporary, self.root / _OWNER_FILE)
            _sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def _root(self, create: bool = False) -> bool:
        if self.root.is_symlink():
            raise AudioLabError('The AI folder is a link. AudioLab will not change it.')
        if self.root.exists() and not self.root.is_dir():
            raise AudioLabError('The AI folder is not a folder.')
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.exists():
            return False
        marker = self.root / _OWNER_FILE
        if marker.exists() or marker.is_symlink():
            info = marker.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _json(marker) != _OWNER:
                raise AudioLabError('The AI ownership marker is invalid. AudioLab will not change this folder.')
        elif any(self.root.iterdir()):
            raise AudioLabError('The AI folder contains files not owned by AudioLab. AudioLab will not change it.')
        elif create:
            self._mark_root()
        return True

    def _directory(self, path: Path, create: bool = False) -> None:
        self._root(create=create)
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise AudioLabError(
                    'An AI runtime folder is a link or an invalid folder. Remove the AI pack and retry.'
                )
            if create:
                current.mkdir(exist_ok=True)

    def _file(self, path: Path, *, allow_hardlinks: bool = False) -> None:
        self._directory(path.parent)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or (not allow_hardlinks and info.st_nlink != 1):
            raise AudioLabError('An AI runtime file is a link or an invalid file. Remove the AI pack and retry.')

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._file(path)
        temporary = path.with_name(f'.{path.name}-{uuid.uuid4().hex}')
        try:
            with temporary.open('x', encoding='utf-8') as out:
                json.dump(value, out, sort_keys=True)
                out.flush()
                os.fsync(out.fileno())
            system.replace_file(temporary, path)
            _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _delete(self, path: Path) -> None:
        """Delete only a direct owned descendant, without following directory links."""
        self._check_output()
        self._root()
        if path == self.root or self.root not in path.parents:
            raise AudioLabError('Refused to remove a folder outside the AI pack.')
        self._directory(path.parent)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def _owned_bytes(self) -> int:
        if not self._root():
            return 0
        total = 0
        seen: set[tuple[int, int]] = set()
        for folder, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
            for name in files:
                info = (Path(folder) / name).lstat()
                identity = (info.st_dev, info.st_ino)
                if stat.S_ISREG(info.st_mode) and identity not in seen:
                    total += info.st_size
                    seen.add(identity)
        return total

    def storage_bytes(self) -> int:
        """Owned runtime, model, cache and interrupted-download bytes; no shared caches."""
        now = time.monotonic()
        if self._storage_cache is not None and now - self._storage_cache[0] < 5:
            return self._storage_cache[1]
        try:
            value = self._owned_bytes()
            self._storage_cache = (now, value)
            return value
        except (AudioLabError, OSError):
            return 0

    def environment(self, directory: Path) -> dict[str, str]:
        """The helper may write caches/temp only into the removable, app-owned root."""
        self._directory(directory)
        for name in ('cache', 'tmp'):
            self._directory(self.root / name, create=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {'PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'LD_LIBRARY_PATH', 'LD_PRELOAD'}
            and not key.startswith('DYLD_')
        }
        env.update(
            {
                'AUDIOLAB_AI_MODELS_DIR': str(directory / 'models'),
                'TORCH_HOME': str(self.root / 'cache' / 'torch'),
                'HF_HOME': str(self.root / 'cache' / 'huggingface'),
                'XDG_CACHE_HOME': str(self.root / 'cache'),
                'TMPDIR': str(self.root / 'tmp'),
                'TEMP': str(self.root / 'tmp'),
                'TMP': str(self.root / 'tmp'),
                'HF_HUB_OFFLINE': '1',
                'TRANSFORMERS_OFFLINE': '1',
                'PYTHONNOUSERSITE': '1',
            }
        )
        return env

    def _probe(self, directory: Path, lock: system.InstanceLock, progress: Progress) -> dict[str, Any]:
        assert self.catalog is not None
        self._directory(directory)
        executable = directory / self.catalog['executable']
        self._file(executable, allow_hardlinks=True)
        if not executable.is_file():
            raise AudioLabError('The AI helper is missing. Repair the AI pack in Settings.')
        lines: deque[str] = deque(maxlen=20)
        result = proc.run(
            [str(executable), '--probe'],
            progress=progress,
            timeout_s=90,
            capture_stdout=False,
            on_line=lambda line: lines.append(line[:16_384]),
            env=self.environment(directory),
            cwd=directory,
            what='AI pack check',
            inherited_fds=(lock.fileno,),
        )
        probe = {}
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict) and value.get('protocol') == 1 and 'available' in value:
                    probe = value
            except ValueError:
                pass
        if (
            result.returncode != 0
            or probe.get('available') is not True
            or probe.get('version') != self.catalog['version']
            or probe.get('models') != _MODELS
            or probe.get('device') not in ('cpu', 'mps', 'cuda')
        ):
            raise AudioLabError('The AI pack could not be checked. Repair it in Settings.', detail=result.stderr_tail)
        return {key: probe.get(key) for key in ('device', 'torch_version', 'demucs_version')}

    def _recover(self) -> None:
        """Finish or roll back a replacement interrupted between directory/receipt renames."""
        transaction = _json(self.root / 'activation.json')
        if not transaction:
            return
        token, version = transaction.get('id'), transaction.get('version')
        if not isinstance(token, str) or not _TRANSACTION.fullmatch(token):
            raise AudioLabError('AI repair metadata is damaged. Remove the AI pack and retry.')
        if not isinstance(version, str) or not _VERSION.fullmatch(version) or version in {'.', '..'} | _RESERVED:
            raise AudioLabError('AI repair metadata is damaged. Remove the AI pack and retry.')
        target = self.root / version
        backup = self.root / 'staging' / f'previous-{token}'
        self._directory(target)
        self._directory(backup)
        receipt = _json(self.root / 'active.json')
        if receipt.get('transaction') != token:
            marker = _json(target / '.managed-receipt.json')
            if marker.get('transaction') == token:
                self._delete(target)
            if backup.exists():
                if target.exists():
                    raise AudioLabError('AI repair could not restore the previous pack. Remove the AI pack and retry.')
                system.replace_file(backup, target)
                _sync_directory(self.root)
                _sync_directory(backup.parent)
        if backup.exists():
            self._delete(backup)
        self._delete(self.root / 'activation.json')
        _sync_directory(self.root)
        self._probe_cache = None

    def _ready(self, lock: system.InstanceLock, progress: Progress) -> tuple[Path, dict[str, Any]]:
        if not self.catalog:
            raise AudioLabError(self.catalog_error or 'This app has no compatible AI download.')
        receipt = _json(self.root / 'active.json')
        if receipt.get('catalog') != self.catalog:
            raise AudioLabError('The compatible AI pack is not installed.')
        directory = self.root / self.catalog['version']
        self._directory(directory)
        marker = _json(directory / '.managed-receipt.json')
        if marker != receipt:
            raise AudioLabError('The AI pack is incomplete. Repair it in Settings.')
        executable = directory / self.catalog['executable']
        self._file(executable, allow_hardlinks=True)
        stamp = (receipt.get('transaction'), executable.stat().st_mtime_ns, executable.stat().st_size)
        if self._probe_cache is None or self._probe_cache[0] != stamp:
            self._probe_cache = (stamp, self._probe(directory, lock, progress))
        return directory, self._probe_cache[1]

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            'available': False,
            'device': None,
            'torch_version': None,
            'demucs_version': None,
            'reason': self.catalog_error or 'AI pack not installed',
            'managed': True,
            'install_supported': bool(self.catalog and system.managed_ai_supported()),
            'installing': self.running,
            'install_ok': self.ok,
            'install_log': '\n'.join(self.log),
            'state': 'unsupported' if not self.catalog else 'missing',
            'version': None,
            'target_version': self.catalog['version'] if self.catalog else None,
            'download_bytes': self.catalog['download_bytes'] if self.catalog else 0,
            'target_installed_bytes': self.catalog['installed_bytes'] if self.catalog else 0,
            'installed_bytes': 0,
            'downloaded_bytes': 0,
            'removable': False,
        }
        lock = None
        try:
            self._check_output()
            if not self._root():
                return result
            result['removable'] = any(self.root.iterdir())
            result['installed_bytes'] = self.storage_bytes()
            part = self.root / 'download.part'
            if part.is_file() and not part.is_symlink():
                result['downloaded_bytes'] = part.stat().st_size
            receipt = _json(self.root / 'active.json')
            version = receipt.get('catalog', {}).get('version') if isinstance(receipt.get('catalog'), dict) else None
            result['version'] = version if isinstance(version, str) and _VERSION.fullmatch(version) else None
            result['state'] = (
                'incompatible' if result['version'] else 'partial' if result['removable'] else result['state']
            )
            lock = self._lock()
            if not lock.acquire():
                result['state'] = self._phase or ('installing' if self.running else 'busy')
                result['reason'] = 'AI pack is in use or being changed.'
                if not self.running and receipt.get('catalog') == self.catalog and self._probe_cache is not None:
                    result.update(self._probe_cache[1], available=True, reason='AI pack is in use.')
                return result
            self._recover()
            if self.catalog:
                try:
                    _, probe = self._ready(lock, Progress.null())
                    result.update(probe, available=True, reason=None, state='ready')
                except (AudioLabError, OSError) as exc:
                    if receipt.get('catalog') == self.catalog:
                        result.update(state='broken', reason=str(exc))
        except (AudioLabError, OSError) as exc:
            result.update(state='broken', reason=str(exc))
        finally:
            if lock is not None:
                lock.release()
        return result

    def start(self, on_done: Callable[[bool], None], repair: bool = False) -> bool:
        if not self.catalog or not system.managed_ai_supported():
            raise AudioLabError(self.catalog_error or 'This computer does not support the managed AI pack.')
        with self._guard:
            if self.running:
                return False
            self._check_output()
            lock = self._lock()
            if not lock.acquire():
                raise AudioLabError('The AI pack is in use. Stop AI jobs before installing or repairing it.')
            try:
                self._root()
            except Exception:
                lock.release()
                raise
            self.running, self.ok = True, None
            self._cancel = threading.Event()
            self.log.clear()
            self._thread = threading.Thread(target=self._run, args=(lock, on_done, repair), daemon=True)
            self._thread.start()
        return True

    def _open(self, request: urllib.request.Request):
        return urllib.request.build_opener(_ReleaseRedirect()).open(request, timeout=10)

    def _download(self, progress: Progress) -> Path:
        assert self.catalog is not None
        part, metadata = self.root / 'download.part', self.root / 'download.json'
        self._file(part)
        self._file(metadata)
        identity = {key: self.catalog[key] for key in ('url', 'sha256', 'download_bytes')}
        if _json(metadata) != identity:
            self._delete(part)
            self._write_json(metadata, identity)
        offset = part.stat().st_size if part.exists() else 0
        size = self.catalog['download_bytes']
        if offset > size:
            self._delete(part)
            offset = 0
        needed = (
            size - offset + self.catalog['installed_bytes'] + max(64 * 1024**2, self.catalog['installed_bytes'] // 10)
        )
        if shutil.disk_usage(self.root).free < needed:
            raise AudioLabError(
                'There is not enough free space to install the AI pack.', hints=['Free up disk space and retry.']
            )
        if offset < size:
            headers = {'Accept-Encoding': 'identity', 'User-Agent': 'AudioLab-AI-Installer'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            request = urllib.request.Request(self.catalog['url'], headers=headers)
            with self._open(request) as response:
                _download_url(response.geturl())
                code = response.status
                if code == 200:
                    offset = 0  # a server may ignore Range; never append a second full archive
                elif code == 206:
                    expected = f'bytes {offset}-{size - 1}/{size}'
                    if response.headers.get('Content-Range') != expected:
                        raise AudioLabError('The AI download resumed at the wrong position. Retry the download.')
                else:
                    raise AudioLabError('The AI download server returned an unexpected response. Retry later.')
                length = response.headers.get('Content-Length')
                if length is not None and (not length.isdigit() or int(length) != size - offset):
                    raise AudioLabError('The AI download size did not match this app. Retry later.')
                if response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise AudioLabError('The AI download encoding is unsupported.')
                flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                flags |= os.O_APPEND if offset else os.O_TRUNC
                with os.fdopen(os.open(part, flags, 0o600), 'ab' if offset else 'wb') as out:
                    while True:
                        progress.check_cancel()
                        chunk = response.read(min(_BLOCK, size - offset + 1))
                        if not chunk:
                            break
                        if offset + len(chunk) > size:
                            self._delete(part)
                            raise AudioLabError('The AI download exceeded its expected size. Retry later.')
                        out.write(chunk)
                        out.flush()
                        offset += len(chunk)
                    os.fsync(out.fileno())
        if not part.exists() or part.stat().st_size != size:
            raise AudioLabError('The AI download was interrupted. Retry to resume it.')
        digest = hashlib.sha256()
        with part.open('rb') as source:
            while chunk := source.read(_BLOCK):
                progress.check_cancel()
                digest.update(chunk)
        if digest.hexdigest() != self.catalog['sha256'].lower():
            self._delete(part)
            raise AudioLabError('The AI download failed its security check. Retry to download a fresh copy.')
        return part

    def _extract(self, archive: Path, directory: Path, progress: Progress) -> None:
        assert self.catalog is not None
        self._directory(directory, create=True)
        seen: set[str] = set()
        regulars: dict[str, Path] = {}
        hardlinks: list[tuple[Path, str]] = []
        total = 0

        def safe_parts(name: str) -> list[str]:
            pieces = name.split('/')
            if (
                not name
                or PurePosixPath(name).is_absolute()
                or '\\' in name
                or ':' in name
                or any(piece in ('', '.', '..') for piece in pieces)
                or len(name) > 1024
                or len(pieces) > 32
            ):
                raise AudioLabError('The AI archive contains an unsafe file. The pack was not installed.')
            return pieces

        with tarfile.open(archive, mode='r|*') as source:
            for member in source:
                progress.check_cancel()
                name = member.name.rstrip('/') if member.isdir() else member.name
                pieces = safe_parts(name)
                if not (member.isdir() or member.isreg() or member.islnk()):
                    raise AudioLabError('The AI archive contains an unsafe file. The pack was not installed.')
                key = name.casefold()  # reject aliases on the default case-insensitive macOS filesystem
                if key in seen or len(seen) >= _MAX_FILES:
                    raise AudioLabError('The AI archive contains duplicate or too many files.')
                seen.add(key)
                destination = directory.joinpath(*pieces)
                if member.isdir():
                    self._directory(destination, create=True)
                    continue
                if member.islnk():
                    if member.size != 0:
                        raise AudioLabError('The AI archive contains a hardlink with unexpected file data.')
                    safe_parts(member.linkname)
                    hardlinks.append((destination, member.linkname))
                    continue
                total += member.size
                if member.size < 0 or total > self.catalog['installed_bytes']:
                    raise AudioLabError('The AI archive exceeded its expected installed size.')
                self._directory(destination.parent, create=True)
                stream = source.extractfile(member)
                if stream is None:
                    raise AudioLabError('The AI archive is incomplete.')
                with stream, destination.open('xb') as out:
                    remaining = member.size
                    while remaining:
                        progress.check_cancel()
                        chunk = stream.read(min(_BLOCK, remaining))
                        if not chunk:
                            raise AudioLabError('The AI archive is incomplete.')
                        out.write(chunk)
                        remaining -= len(chunk)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
                regulars[name] = destination
        if total != self.catalog['installed_bytes']:
            raise AudioLabError('The AI archive did not match its expected installed size.')
        # Resolve only direct regular members, after extraction, so forward references
        # are safe and neither link chains nor arbitrary pre-existing files are adopted.
        for _, target in hardlinks:
            if target not in regulars:
                raise AudioLabError('The AI archive hardlink does not point to a regular archive file.')
        for destination, target in hardlinks:
            progress.check_cancel()
            self._directory(destination.parent, create=True)
            source = regulars[target]
            self._directory(source.parent)
            if not stat.S_ISREG(source.lstat().st_mode):
                raise AudioLabError('The AI archive hardlink target is not a regular file.')
            os.link(source, destination, follow_symlinks=False)

    def _activate(self, staged: Path, token: str, probe: dict[str, Any]) -> None:
        assert self.catalog is not None
        self._check_output()
        target = self.root / self.catalog['version']
        self._directory(target)
        receipt = {'catalog': self.catalog, 'transaction': token, 'probe': probe}
        self._write_json(staged / '.managed-receipt.json', receipt)
        backup = self.root / 'staging' / f'previous-{token}'
        self._write_json(self.root / 'activation.json', {'id': token, 'version': self.catalog['version']})
        if target.exists():
            system.replace_file(target, backup)
            _sync_directory(backup.parent)
        system.replace_file(staged, target)
        _sync_directory(self.root)
        _sync_directory(staged.parent)
        self._write_json(self.root / 'active.json', receipt)
        self._recover()

    def _cleanup(self) -> None:
        assert self.catalog is not None
        self._check_output()
        self._root()
        for item in self.root.iterdir():
            if item.name in {self.catalog['version'], 'active.json', 'cache', 'tmp', _OWNER_FILE}:
                continue
            self._delete(item)

    def _run(self, lock: system.InstanceLock, on_done: Callable[[bool], None], repair: bool) -> None:
        ok = False
        staged = None
        progress = Progress.null(self._cancel)
        try:
            self._check_output()
            self._root(create=True)
            self._recover()
            if not repair:
                try:
                    self._ready(lock, progress)
                    ok = True
                    return
                except (AudioLabError, OSError):
                    pass
            self._phase = 'downloading'
            self.log.append('Downloading the optional AI pack…')
            archive = self._download(progress)
            self._phase = 'installing'
            self.log.append('Checking and unpacking the AI pack…')
            self._directory(self.root / 'staging', create=True)
            for item in (self.root / 'staging').iterdir():
                self._delete(item)
            token = uuid.uuid4().hex
            staged = self.root / 'staging' / f'install-{token}'
            self._extract(archive, staged, progress)
            self._phase = 'checking'
            probe = self._probe(staged, lock, progress)
            progress.check_cancel()
            self._activate(staged, token, probe)
            self._cleanup()
            self._probe_cache = None
            self._storage_cache = None
            ok = True
            self.log.append('AI pack installed and checked.')
        except Cancelled:
            self.log.append('AI installation stopped. Retry to resume the download.')
        except Exception as exc:
            self.log.append(f'AI installation failed: {str(exc)[:500]}')
        finally:
            try:
                if staged is not None and staged.exists() and not (self.root / 'activation.json').exists():
                    self._delete(staged)
            except (AudioLabError, OSError):
                pass
            self._phase = None
            lock.release()
            with self._guard:
                self.ok, self.running = ok, False
            try:
                on_done(ok)
            except Exception:
                pass

    def cancel(self) -> None:
        self._cancel.set()

    def shutdown(self) -> None:
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def remove(self) -> dict[str, Any]:
        self._check_output()
        lock = self._lock()
        if not lock.acquire():
            raise AudioLabError('The AI pack is in use. Stop AI jobs or installation before removing it.')
        try:
            if self._root():
                self._root(create=True)
                for item in self.root.iterdir():
                    if _VERSION.fullmatch(item.name) and item.is_symlink():
                        raise AudioLabError('An AI runtime folder is a link. AudioLab will not change it.')
                for item in self.root.iterdir():
                    if item.name != _OWNER_FILE:
                        self._delete(item)
                self._delete(self.root / _OWNER_FILE)
                self.root.rmdir()
            self._probe_cache = None
            self._storage_cache = None
            self.ok = None
            self.log.clear()
        finally:
            lock.release()
        return self.status()

    @contextmanager
    def lease(self, progress: Progress) -> Iterator[tuple[Path, system.InstanceLock]]:
        """One AI use at a time; a queued use waits without blocking cancellation."""
        self._check_output()
        lock = self._lock()
        while not lock.acquire():
            progress.check_cancel()
            time.sleep(0.1)
        try:
            progress.check_cancel()
            self._check_output()
            self._root()
            self._recover()
            directory, _ = self._ready(lock, progress)
            yield directory, lock
        finally:
            lock.release()


_MANAGERS: dict[tuple[Path, Path], ManagedAI] = {}
_MANAGERS_LOCK = threading.Lock()


def managed_ai() -> ManagedAI:
    key = (SETTINGS.data_dir.absolute(), CATALOG_PATH.absolute())
    with _MANAGERS_LOCK:
        if key not in _MANAGERS:
            _MANAGERS[key] = ManagedAI(*key)
        return _MANAGERS[key]


def environment(directory: Path) -> dict[str, str]:
    return managed_ai().environment(directory)
