"""Exception types. Everything derived from AudioLabError carries a message
that is safe and useful to show to a non-technical user."""

import errno

_DISK_FULL = {errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC)}  # Windows has no EDQUOT
_NOT_WRITABLE = {errno.EACCES, errno.EPERM, errno.EROFS}


class AudioLabError(RuntimeError):
    """User-facing failure.

    `hints` are short "what you can try" suggestions shown under the error.
    `fix` names a one-click remedy the UI can offer (e.g. 'update_downloader',
    'install_ai', 'retry'); None when there is nothing to click.
    `detail` is the raw technical text behind it (a tool's error output), for bug
    reports only — never part of the message.
    """

    def __init__(self, message: str, hints: list[str] | None = None, fix: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.hints = list(hints or [])
        self.fix = fix
        self.detail = detail


class ResourceGuardError(AudioLabError):
    """A job was refused before heavy work started (memory/disk/duration guard)."""


class DownloadError(AudioLabError):
    """yt-dlp could not fetch the media (network, removed video, site changes...)."""


class ToolMissingError(AudioLabError):
    """A required external tool (ffmpeg, yt-dlp, torch/demucs) is unavailable."""


class Cancelled(Exception):
    """Raised inside a running task when the user cancelled the job.
    Deliberately NOT an AudioLabError: it is a normal outcome, not a failure."""


def storage_error(e: OSError) -> AudioLabError | None:
    """A full disk or a place AudioLab may not write to, in plain words (the raw error as `detail`);
    None for any other OSError."""
    if e.errno in _DISK_FULL:
        return _storage('full', f'{type(e).__name__}: {e}')
    if e.errno in _NOT_WRITABLE:
        return _storage('not_writable', f'{type(e).__name__}: {e}')
    return None


def storage_error_in(text: str, detail: str | None = None) -> AudioLabError | None:
    """The same for a tool's error output (ffmpeg: "No space left on device", "Permission denied")."""
    low = (text or '').lower()
    if 'no space left on device' in low or 'disk quota exceeded' in low:
        return _storage('full', detail or text)
    if 'permission denied' in low or 'read-only file system' in low or 'operation not permitted' in low:
        return _storage('not_writable', detail or text)
    return None


def _storage(kind: str, detail: str) -> AudioLabError:
    if kind == 'full':
        return AudioLabError(
            'The disk is full.',
            hints=[
                'Free up space on that disk (and empty the Trash), then start it again.',
                'Or choose an output folder on another disk in Settings → Files.',
            ],
            detail=detail,
        )
    return AudioLabError(
        "AudioLab can't write there.",
        hints=[
            'Choose another output folder in Settings → Files.',
            "Or check that you may write to that folder and that the drive isn't read-only.",
        ],
        detail=detail,
    )
