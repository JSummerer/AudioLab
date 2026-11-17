"""Window-only file selection and the identity of an external job source."""

import heapq
import secrets
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AudioLabError
from .media import export
from .settings import SETTINGS

MAX_FILES = 50
MAX_SELECTIONS = 4 * MAX_FILES
PURPOSES = {'files': 'job', 'batch': 'job', 'import': 'import', 'locate': 'locate', 'output': 'output'}


def _identity(path: Path, folder: bool) -> tuple[tuple[int, int], int]:
    try:
        st = path.stat()
        if path.resolve(strict=True) == path and (stat.S_ISDIR if folder else stat.S_ISREG)(st.st_mode):
            return (st.st_dev, st.st_ino), st.st_size
    except (OSError, RuntimeError):
        pass
    raise AudioLabError('The selection is no longer available.', hints=['Choose it again.'])


def checked_path(path: Path, identity: tuple | list, *, folder: bool = False) -> Path:
    current, _ = _identity(path, folder)
    if current != tuple(identity):
        raise AudioLabError('The selected file or folder was replaced.', hints=['Choose it again.'])
    return path


def source_path(source: dict) -> Path:
    path = Path(source.get('path') or '')
    if identity := source.get('_native_identity'):
        checked_path(path, identity)
    return path


@dataclass(frozen=True)
class Selection:
    path: Path
    purpose: str
    identity: tuple[int, int]
    size: int

    def checked_path(self) -> Path:
        return checked_path(self.path, self.identity, folder=self.purpose == 'output')

    def source(self) -> dict:
        path = self.checked_path()
        return {
            'kind': 'file',
            'name': path.name,
            'path': str(path),
            'size_bytes': self.size,
            'title': path.stem,
            '_native_identity': self.identity,
        }


def _selection(path: Path, purpose: str) -> Selection:
    path = path.resolve(strict=True)
    if purpose != 'output' and path.is_relative_to(SETTINGS.data_dir.resolve()):
        raise AudioLabError('Choose a file outside AudioLab’s data folder.')
    if purpose in ('job', 'import') and path.suffix.lower() not in export.UPLOAD_EXT:
        raise AudioLabError('Choose a supported audio or video file.')
    identity, size = _identity(path, folder=purpose == 'output')
    if purpose != 'output' and not size:
        raise AudioLabError('This file is empty (0 bytes).', hints=['Choose another file.'])
    return Selection(path, purpose, identity, size)


class NativeInputs:
    """One app window, one secret, and a bounded registry of explicit selections."""

    def __init__(self, url: str, webview):
        self.origin = url
        self.address = urlsplit(url).netloc
        self.secret = secrets.token_urlsafe(32)
        self.window = None
        dialogs = getattr(webview, 'FileDialog', None)
        self._open = dialogs.OPEN if dialogs else webview.OPEN_DIALOG
        self._folder = dialogs.FOLDER if dialogs else webview.FOLDER_DIALOG
        self._dialog_lock = threading.Lock()
        self._lock = threading.Lock()
        self._selections: OrderedDict[str, Selection] = OrderedDict()

    def authorizes(self, secret) -> bool:
        return isinstance(secret, str) and secret.isascii() and secrets.compare_digest(secret, self.secret)

    def _on_app_page(self) -> bool:
        if self.window is None:
            return False
        url = urlsplit(self.window.get_current_url() or '')
        return f'{url.scheme}://{url.netloc}' == self.origin and url.path in ('', '/', '/index.html')

    def bridge(self):
        # Expose just one method, never this registry, the window, or any path-accepting callable.
        class Picker:
            def choose(_, secret, kind):
                return self._choose(secret, kind)

        return Picker()

    def _choose(self, secret, kind) -> dict | None:
        if not self.authorizes(secret) or not isinstance(kind, str) or kind not in PURPOSES:
            return None
        if not self._dialog_lock.acquire(blocking=False):
            return None
        try:
            if not self._on_app_page():
                return None
            file_types = ()
            if kind in ('files', 'import'):
                patterns = ';'.join(f'*{ext}' for ext in sorted(export.UPLOAD_EXT))
                file_types = (f'Audio and video files ({patterns})',)
            picked = self.window.create_file_dialog(
                self._folder if kind in ('batch', 'output') else self._open,
                allow_multiple=kind == 'files',
                file_types=file_types,
            )
            if not picked or not self._on_app_page():
                return None
            if kind == 'batch':
                # Enumerate immediate regular files only. Aliases/symlinks in a folder do not
                # extend selection to another location. Keep only the first MAX_FILES by name.
                paths = (
                    child
                    for child in Path(picked[0]).resolve(strict=True).iterdir()
                    if not child.is_symlink() and child.is_file()
                )
            else:
                paths = map(Path, picked if kind == 'files' else picked[:1])
            result = self._register(paths, PURPOSES[kind], sort=kind == 'batch')
            return result if self._on_app_page() else None
        except (OSError, RuntimeError, ValueError):
            return {'error': 'AudioLab couldn’t open that selection. Choose it again.'}
        finally:
            self._dialog_lock.release()

    def _register(self, paths, purpose, *, sort: bool) -> dict:
        seen, skipped = set(), 0

        def selections():
            nonlocal skipped
            for path in paths:
                try:
                    item = _selection(path, purpose)
                except (AudioLabError, OSError, RuntimeError):
                    skipped += 1
                    continue
                if not sort and item.path in seen:
                    continue
                if not sort:
                    seen.add(item.path)
                yield item

        items = selections()
        first = (
            heapq.nsmallest(MAX_FILES + 1, items, key=lambda s: (s.path.name.casefold(), s.path.name)) if sort else []
        )
        if not sort:
            for item in items:
                first.append(item)
                if len(first) > MAX_FILES:
                    break
        files = []
        with self._lock:
            for item in first[:MAX_FILES]:
                key = next((key for key, old in self._selections.items() if old == item), None)
                if key is None:
                    key = secrets.token_urlsafe(24)
                    self._selections[key] = item
                else:
                    self._selections.move_to_end(key)
                files.append({'selection': key, 'name': item.path.name, 'size': item.size})
            while len(self._selections) > MAX_SELECTIONS:
                self._selections.popitem(last=False)
        return {'files': files, 'skipped': skipped, 'capped': len(first) > MAX_FILES}

    def get(self, selection, purpose: str) -> Selection | None:
        if not isinstance(selection, str):
            return None
        with self._lock:
            item = self._selections.get(selection)
            if item is not None and item.purpose == purpose:
                self._selections.move_to_end(selection)
                return item
        return None
