"""The operating-system layer: the only module that knows whether it runs on macOS,
Windows or Linux. Callers use these functions and never branch on the OS themselves.

Folders (data, logs), opening folders, notifications, keeping the computer awake,
the one-instance lock, child-process groups, memory size, file-name limits,
atomic file replacement, moving files to the Trash, and the desktop app's native parts
(app name and icon, dialogs, bringing a running app to the front, the app shortcut).

A leaf module (standard library, the brand and errors.py only), so every module can import it.
The macOS window functions use pyobjc (installed with pywebview) when it is there.
"""

import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from . import APP_NAME, APP_SLUG

# Windows paths are limited to 260 characters (output root + playlist folder + result
# folder + file name + staging suffix), so file names are kept shorter there.
MAX_NAME_LEN = 60 if os.name == 'nt' else 120


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
def default_data_dir() -> Path:
    """Per-user app data: macOS Application Support, Windows %LOCALAPPDATA%, Linux $XDG_DATA_HOME."""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / APP_NAME
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / APP_NAME
    base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(base) / APP_SLUG


def log_dir(data_dir: Path) -> Path:
    """macOS keeps app logs in ~/Library/Logs (Console.app shows them); elsewhere, and for a
    custom data folder (tests, Docker, a second setup), the logs stay in `<data>/logs`."""
    data_dir = Path(data_dir)
    if sys.platform == 'darwin' and data_dir == default_data_dir():
        return Path.home() / 'Library' / 'Logs' / APP_NAME
    return data_dir / 'logs'


def folder_writable(path: Path) -> bool:
    """True when files can be written into the folder `path`: it is there and writable, or it can be
    made (its nearest existing parent is). Nothing is created. A path on a drive that isn't connected
    is False (macOS: /Volumes is not writable). Windows folders have no write bit, so there it is weak:
    writing may still fail later (with a plain error)."""
    p = Path(path)
    while not os.path.exists(p):  # never raises: a folder we may not look into counts as missing
        if p == p.parent:
            return False
        p = p.parent
    return os.path.isdir(p) and os.access(p, os.W_OK | os.X_OK)


def reveal(path: Path) -> None:
    """Open a folder in Finder / Explorer / the Linux file manager. Never creates it: a folder that
    isn't there raises FileNotFoundError (callers check first and say so in plain words)."""
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    if sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    elif sys.platform == 'win32':
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(['xdg-open', str(path)])


def notify(title: str, message: str) -> None:
    """Best-effort desktop notification (macOS / Linux with notify-send)."""
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(
                ['osascript', '-e', f'display notification {_as_text(message)} with title {_as_text(title)}'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform.startswith('linux') and shutil.which('notify-send'):
            subprocess.Popen(['notify-send', title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _as_text(s: str) -> str:
    """An AppleScript string literal."""
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def replace_file(src: Path, dst: Path, attempts: int = 20) -> None:
    """os.replace that rides out Windows sharing violations (a virus scanner, backup tool or
    editor briefly holding `dst` open) instead of silently losing the write."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != 'nt' or i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


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


def trash(path: Path) -> None:
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


class InstanceLock:
    """Exclusive, non-blocking lock on a file — one AudioLab process per data folder.
    Released automatically when the process exits."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, 'a+')
        try:
            if os.name == 'nt':
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def fileno(self) -> int:
        """Keep the same lock alive in an owned helper if its parent exits."""
        if self._fh is None:
            raise RuntimeError('The lock is not held.')
        return self._fh.fileno()


def inherited_descriptors(fds: tuple[int, ...]) -> dict[str, Any]:
    if not fds:
        return {}
    if os.name != 'posix':
        raise RuntimeError('Inherited helper locks are only supported on POSIX.')
    return {'pass_fds': fds}


def managed_ai_supported() -> bool:
    """Initial qualified add-on target; source installs keep their own device support."""
    import platform

    return sys.platform == 'darwin' and platform.machine() == 'arm64' and int(platform.mac_ver()[0].split('.')[0]) >= 14


# ---------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------
def own_process_group() -> dict[str, Any]:
    """Popen keyword arguments that start a child in its own process group, so
    terminate_process() stops it together with everything it spawns (yt-dlp → ffmpeg)."""
    if os.name == 'posix':
        return {'start_new_session': True}
    return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}


class _OwnedProcessGroup:
    def __init__(self, pgid: int):
        self.pgid = pgid
        self.lock = threading.Lock()
        self.stopped = False


_process_groups: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_process_groups_lock = threading.Lock()
_TERM_GRACE_S = 5.0


def register_process_group(proc: subprocess.Popen) -> None:
    """Remember the private group of a child just created with own_process_group().
    The new session's PGID is its PID, even if its leader exits before cancellation.
    Never use this for an arbitrary process; unregistered children are stopped individually."""
    if os.name == 'posix':
        with _process_groups_lock:
            _process_groups.setdefault(proc, _OwnedProcessGroup(proc.pid))


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def terminate_process(proc: subprocess.Popen) -> None:
    """Stop a child process and everything it started (its process group on
    POSIX, its process tree on Windows)."""
    if os.name == 'nt':
        if proc.poll() is not None:
            return
        try:
            subprocess.run(
                ['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # taskkill failed or was refused: kill at least the child
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return
    with _process_groups_lock:
        group = _process_groups.get(proc)
    if group is not None:
        with group.lock:  # cancellation and the subprocess runner may both request teardown
            if group.stopped:
                return
            pgid = group.pgid
            if pgid > 0 and pgid != os.getpgrp():  # never signal the app's own group
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except OSError:
                    pass
                deadline = time.monotonic() + _TERM_GRACE_S
                while _group_exists(pgid) and time.monotonic() < deadline:
                    proc.poll()  # reap the leader without mistaking its exit for group completion
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                if _group_exists(pgid) and pgid != os.getpgrp():
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                group.stopped = True  # a repeat call must not signal a later reuse of this PGID
                return
    if proc.poll() is not None:
        return
    # Without an explicitly registered private identity, stop only the known child.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            proc.send_signal(sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def process_alive(pid: int) -> bool:
    """True while a process with this id is running (for the pid file of a detached helper)."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class KeepAwake:
    """Prevent idle sleep while jobs run: macOS `caffeinate`, Windows SetThreadExecutionState,
    Linux `systemd-inhibit` (when available). Helpers exit by themselves if AudioLab dies."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._stop: threading.Event | None = None

    def set(self, active: bool) -> None:
        if os.name == 'nt':
            return self._windows(active)
        if active and self._proc is None:
            cmd = self._command()
            if cmd:
                try:
                    self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    self._proc = None
        elif not active and self._proc is not None:
            self._proc.terminate()
            self._proc = None

    @staticmethod
    def _command() -> list[str] | None:
        pid = str(os.getpid())
        if sys.platform == 'darwin' and shutil.which('caffeinate'):
            return ['caffeinate', '-i', '-w', pid]
        if sys.platform.startswith('linux') and shutil.which('systemd-inhibit') and shutil.which('tail'):
            return [
                'systemd-inhibit',
                '--what=idle:sleep',
                f'--who={APP_NAME}',
                '--why=Processing audio',
                '--mode=block',
                'tail',
                f'--pid={pid}',
                '-f',
                '/dev/null',
            ]
        return None

    def _windows(self, active: bool) -> None:
        # The execution state belongs to the calling thread, so a small thread holds it.
        if active and self._stop is None:
            self._stop = threading.Event()
            threading.Thread(target=self._hold_windows, args=(self._stop,), daemon=True).start()
        elif not active and self._stop is not None:
            self._stop.set()
            self._stop = None

    @staticmethod
    def _hold_windows(stop: threading.Event) -> None:
        try:
            import ctypes

            set_state = ctypes.windll.kernel32.SetThreadExecutionState
        except (ImportError, AttributeError, OSError):
            return
        es_continuous, es_system_required = 0x80000000, 0x00000001
        set_state(es_continuous | es_system_required)
        try:
            stop.wait()
        finally:
            set_state(es_continuous)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def container_memory() -> tuple[int, int] | None:
    """(limit, used) bytes when running under a Linux cgroup memory limit (Docker), else None."""
    return _cgroup_v2() or _cgroup_v1()


def _cgroup_v2() -> tuple[int, int] | None:
    try:
        limit = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        current = int(Path('/sys/fs/cgroup/memory.current').read_text().strip())
        if limit in ('max', ''):
            return None
        return int(limit), current
    except (OSError, ValueError):
        return None


def _cgroup_v1() -> tuple[int, int] | None:
    try:
        limit = int(Path('/sys/fs/cgroup/memory/memory.limit_in_bytes').read_text().strip())
        current = int(Path('/sys/fs/cgroup/memory/memory.usage_in_bytes').read_text().strip())
        if limit <= 0 or limit > (1 << 50):  # huge value means "unlimited"
            return None
        return limit, current
    except (OSError, ValueError):
        return None


def physical_memory_bytes() -> int | None:
    """Installed RAM of this computer, or None when it cannot be determined."""
    if os.name == 'nt':
        return _windows_memory_bytes()
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (AttributeError, OSError, ValueError):
        pass
    try:  # macOS fallback
        out = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _windows_memory_bytes() -> int | None:
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return int(st.ullTotalPhys)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The desktop app
# ---------------------------------------------------------------------------
LSREGISTER = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'


def prepare_app_identity(icon: Path) -> None:
    """Show the window app under its own name and icon (Dock, "Quit <name>" in the app menu)
    although the process is a Python interpreter. Call before the window opens.
    macOS only; elsewhere the window title is enough."""
    if sys.platform != 'darwin':
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
    except ImportError:
        return
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info['CFBundleName'] = APP_NAME
    image = NSImage.alloc().initWithContentsOfFile_(str(icon))
    if image is not None:
        NSApplication.sharedApplication().setApplicationIconImage_(image)


def confirm(title: str, message: str, ok: str = 'OK', cancel: str = 'Cancel') -> bool:
    """Ask in a native dialog; True when the user picks `ok`. macOS: call it on the main
    thread (the window's closing handler runs there). Other platforms return True."""
    if sys.platform != 'darwin':
        return True
    try:
        from AppKit import NSAlert, NSAlertFirstButtonReturn, NSApplication
    except ImportError:
        return True
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_(cancel)  # "Cancel" also answers the Escape key
    return alert.runModal() == NSAlertFirstButtonReturn


def alert(title: str, message: str) -> None:
    """Show an error that must not go unnoticed when there is no terminal to print it to
    (the app was opened from the Finder). Blocks until dismissed; needs no window."""
    try:
        if sys.platform == 'darwin':
            script = f'display alert {_as_text(title)} message {_as_text(message)} as critical'
            subprocess.run(
                ['osascript', '-e', 'activate', '-e', script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        elif sys.platform == 'win32':
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
        else:
            notify(title, message)
    except Exception:
        pass


def activate_process(pid: int) -> bool:
    """Bring the windows of another running app (by process id) to the front.
    False when that is not possible here (macOS only so far)."""
    if sys.platform != 'darwin':
        return False
    try:
        from AppKit import (
            NSApplicationActivateAllWindows,
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )
    except ImportError:
        return False
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    options = NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps
    return bool(app is not None and app.activateWithOptions_(options))


def install_shortcut(
    command: list[str],
    *,
    icon: Path,
    version: str,
    folder: Path | None = None,
    missing: str = '',
    dest: Path | None = None,
) -> Path:
    """Add the app to the system's app list so it opens like any other app. macOS: writes
    `~/Applications/<name>.app` (or into `dest`), a small bundle whose launcher runs `command`
    (in `folder`, when given) and shows `missing` when command[0] is gone (folder moved or
    deleted). Re-running replaces only a recognized source launcher. Returns its path.
    Raises FileExistsError if another app already occupies the destination.
    Raises NotImplementedError elsewhere (Windows .lnk / Linux .desktop: not yet)."""
    if sys.platform != 'darwin':
        raise NotImplementedError('An app shortcut can only be created on macOS so far.')
    dest = Path(dest) if dest else Path.home() / 'Applications'
    app = dest / f'{APP_NAME}.app'
    if os.path.lexists(app):
        try:
            with (app / 'Contents/Info.plist').open('rb') as stream:
                identifier = plistlib.load(stream).get('CFBundleIdentifier')
        except (OSError, ValueError, TypeError, AttributeError):
            identifier = None
        if app.is_symlink() or identifier != f'org.{APP_SLUG}.app':
            raise FileExistsError(
                f'An existing app at {app} is not an {APP_NAME} source launcher. '
                'It was left untouched. Move it yourself if you want a source launcher there.'
            )
    dest.mkdir(parents=True, exist_ok=True)
    # Built under a unique hidden name (no ".app" ending, so not listed as an app), then swapped in.
    tmp = Path(tempfile.mkdtemp(dir=dest, prefix=f'.{APP_NAME}.app.'))
    old = tmp.with_name(tmp.name + '.old')
    try:
        tmp.chmod(0o755)
        _write_bundle(tmp / 'Contents', command, icon, version, folder, missing)
        try:  # a directory cannot be replaced in one step: move the old one aside first
            os.replace(app, old)
        except FileNotFoundError:
            pass
        try:
            os.replace(tmp, app)
        except OSError:
            if old.exists():
                os.replace(old, app)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # only still there when something failed
    os.utime(app)  # Finder and Launchpad notice the change
    if os.path.exists(LSREGISTER):
        try:
            subprocess.run(
                [LSREGISTER, '-f', str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
            )
        except (OSError, subprocess.SubprocessError):
            pass  # only speeds up Launchpad/Spotlight noticing it
    return app


def _write_bundle(
    contents: Path, command: list[str], icon: Path, version: str, folder: Path | None, missing: str
) -> None:
    (contents / 'MacOS').mkdir(parents=True)
    (contents / 'Resources').mkdir()
    shutil.copyfile(icon, contents / 'Resources' / 'AppIcon.icns')
    with open(contents / 'Info.plist', 'wb') as f:
        plistlib.dump(
            {
                'CFBundleName': APP_NAME,
                'CFBundleDisplayName': APP_NAME,
                'CFBundleIdentifier': f'org.{APP_SLUG}.app',
                'CFBundleExecutable': APP_NAME,
                'CFBundleIconFile': 'AppIcon',
                'CFBundlePackageType': 'APPL',
                'CFBundleShortVersionString': version,
                'CFBundleVersion': version,
                'CFBundleInfoDictionaryVersion': '6.0',
                'LSMinimumSystemVersion': '11.3',  # pywebview's window uses WebKit API from macOS 11.3
                'NSHighResolutionCapable': True,
            },
            f,
        )
    launcher = contents / 'MacOS' / APP_NAME
    launcher.write_text(launcher_script(command, folder, missing), encoding='utf-8')
    launcher.chmod(0o755)


def launcher_script(command: list[str], folder: Path | None = None, missing: str = '') -> str:
    """The POSIX shell launcher of the macOS app bundle (see install_shortcut)."""
    q = shlex.quote
    title = f'{APP_NAME} cannot open'
    lines = [
        '#!/bin/sh',
        f'# Opens {APP_NAME}. Written by `{APP_SLUG} shortcut` - run it again after moving {APP_NAME}.',
        '# Apps opened from the Finder get a minimal PATH: Homebrew (ffmpeg) and uv go first.',
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"',
        f'if [ ! -x {q(command[0])} ]; then',
        f'  osascript -e {q(f"display alert {_as_text(title)} message {_as_text(missing)} as critical")}',
        '  exit 1',
        'fi',
    ]
    if folder is not None:
        lines.append(f'cd {q(str(folder))} || exit 1')
    lines.append('exec ' + ' '.join(q(str(c)) for c in command))
    return '\n'.join(lines) + '\n'
