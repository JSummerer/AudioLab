"""Moving files to the system Trash, only when the user opts in, and telling links from real folders.

OS-specific like system.py, and kept apart from it on purpose: the optional AI add-on is pinned to the
exact bytes of system.py it was built from (packaging/ai/published-helper.json), and the Trash has
nothing to do with it. Never a permanent delete: when no Trash is available, nothing is removed.
"""

import os
import sys
import time
from pathlib import Path

from . import APP_NAME


def is_link(path: Path) -> bool:
    """`path` itself is a symbolic link, or on Windows a junction or mount point (checked with lstat, so
    short 8.3 names and letter case don't matter). Missing or unreadable counts as a link: callers
    use this to refuse."""
    import stat

    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    tag = getattr(st, 'st_reparse_tag', 0)  # Windows only
    return sys.platform == 'win32' and tag in (stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT)


def move_to_trash(path: Path) -> None:
    """Move a file or folder to the system Trash (macOS Trash, Windows Recycle Bin, the freedesktop
    Trash on Linux), where the user can put it back. Never a permanent delete: when this item can't go
    to the Trash (no Recycle Bin on that drive, another disk on Linux, no permission) it stays where it
    is and OSError is raised. AudioLabError when this system has no Trash AudioLab can use."""
    path = Path(os.path.abspath(path))
    if not os.path.lexists(path):
        raise FileNotFoundError(str(path))
    if sys.platform == 'darwin':
        _trash_mac(path)
    elif sys.platform == 'win32':
        _trash_windows(path)
    else:
        _trash_freedesktop(path)


def _no_trash(why: str):
    from .errors import AudioLabError  # errors.py is a leaf as well; imported here, where it's needed

    return AudioLabError(
        f"{APP_NAME} can't use the Trash on this system ({why}), so nothing was removed.",
        hints=['Remove the entry without moving its files to the Trash, then delete the files yourself.'],
    )


def _trash_mac(path: Path) -> None:
    try:
        from Foundation import NSURL, NSFileManager
    except ImportError:
        raise _no_trash('the macOS bridge pyobjc is not installed') from None
    ok, _, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None
    )
    if not ok:
        raise OSError(str(error.localizedDescription()) if error is not None else f'Could not move {path} to the Trash')


def _trash_windows(path: Path) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        shell32, kernel32 = ctypes.windll.shell32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        raise _no_trash('the Windows shell is not available') from None
    # Only fixed drives have a Recycle Bin. On a USB stick or a network share Windows would delete
    # the file for good instead, so it stays where it is.
    drive = os.path.splitdrive(str(path))[0] + '\\'
    if kernel32.GetDriveTypeW(drive) != 3:  # DRIVE_FIXED
        raise OSError(f'The drive of {path} has no Recycle Bin')
    fields = [
        ('hwnd', wintypes.HWND),
        ('wFunc', wintypes.UINT),
        ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR),
        ('fFlags', ctypes.c_ushort),
        ('fAnyOperationsAborted', wintypes.BOOL),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', wintypes.LPCWSTR),
    ]
    layout = {'_fields_': fields, **({'_pack_': 1} if ctypes.sizeof(ctypes.c_void_p) == 4 else {})}
    op = type('SHFILEOPSTRUCTW', (ctypes.Structure,), layout)()
    op.wFunc = 3  # FO_DELETE, with FOF_ALLOWUNDO: to the Recycle Bin
    op.pFrom = str(path) + '\0'  # a list of paths ends with two NULs (ctypes adds the second)
    # FOF_ALLOWUNDO is best effort: with the Recycle Bin turned off or full, Windows would delete the file
    # for good. FOF_WANTNUKEWARNING overrides FOF_NOCONFIRMATION for exactly that case, so Windows asks
    # first; when the user cancels, the operation reports itself aborted and the file stays.
    # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x4000
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise OSError(f'Could not move {path} to the Recycle Bin (code {code})')


def _trash_freedesktop(path: Path) -> None:
    """The freedesktop.org Trash in the user's data folder: a `.trashinfo` claims a free name, then the
    item is renamed into `files/`. Items on another disk are refused (their own `.Trash-<uid>` is not
    used), so nothing is ever copied and deleted."""
    from urllib.parse import quote

    home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'Trash'
    try:
        for sub in ('files', 'info'):
            (home / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        raise _no_trash(f'its folder {home} could not be made: {e.strerror or e}') from None
    if os.lstat(path).st_dev != os.stat(home / 'files').st_dev:
        raise OSError(f'{path} is on another disk than the Trash')
    stem, suffix = (path.stem, path.suffix) if path.is_file() else (path.name, '')
    info = f'[Trash Info]\nPath={quote(str(path))}\nDeletionDate={time.strftime("%Y-%m-%dT%H:%M:%S")}\n'
    for n in range(1, 1000):
        name = path.name if n == 1 else f'{stem}.{n}{suffix}'
        record = home / 'info' / f'{name}.trashinfo'
        try:
            with open(record, 'x', encoding='utf-8') as f:
                f.write(info)
        except FileExistsError:
            continue
        if os.path.lexists(home / 'files' / name):  # left behind without its record: keep it, take another name
            record.unlink(missing_ok=True)
            continue
        try:
            os.rename(path, home / 'files' / name)
        except OSError:
            record.unlink(missing_ok=True)
            raise
        return
    raise OSError(f'No free name for {path.name} in the Trash')
