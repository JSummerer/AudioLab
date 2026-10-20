"""Small shared helpers: filesystem-safe naming, unique paths, hard links, media types,
human-readable sizes and durations."""

import mimetypes
import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from .system import MAX_NAME_LEN

# Characters Windows/macOS/Linux reject or that break path construction.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f/:\\*?"<>|]')
_MULTI_SPACE = re.compile(r'\s+')
_EDGE_DOTS = re.compile(r'^[\s.]+|[\s.]+$')
_MAX_NAME_BYTES = 160
_WINDOWS_RESERVED = {
    'CON',
    'PRN',
    'AUX',
    'NUL',
    'CONIN$',
    'CONOUT$',
    *(f'{dev}{i}' for dev in ('COM', 'LPT') for i in (*range(1, 10), '¹', '²', '³')),
}


def sanitize_filename(name: str, fallback: str = 'Untitled', room: int = 0) -> str:
    """Collapse any string (e.g. a video title) into one safe path component:
    no separators, no traversal, no leading/trailing dots, bounded length.
    `room` allows extra characters for a suffix the caller already added (" (Enhanced)")."""
    cleaned = _UNSAFE_CHARS.sub(' ', str(name or ''))
    cleaned = _MULTI_SPACE.sub(' ', cleaned).strip()
    cleaned = _EDGE_DOTS.sub('', cleaned)
    if len(cleaned) > MAX_NAME_LEN + room:
        cleaned = cleaned[: MAX_NAME_LEN + room].rstrip(' .')
    # File systems limit names to 255 *bytes*; CJK/emoji titles use 3-4 bytes per
    # character. Leave room for suffixes like " (Enhanced)", " - Vocals", " (2)" and
    # the hidden ".<id>.partial" staging name.
    while len(cleaned.encode('utf-8')) > _MAX_NAME_BYTES + room:
        cleaned = cleaned[:-1].rstrip(' .')
    if cleaned.split('.')[0].strip().upper() in _WINDOWS_RESERVED:
        cleaned = f'_{cleaned}'
    return cleaned or fallback


def unique_path(path: Path, folder: bool = False) -> Path:
    """Return `path`, or `name (2).ext`, `name (3).ext`... so user files are never overwritten. A `folder`
    has no extension: "Song (feat. X) (2)", not "Song (feat (2). X)"."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = (path.name, '') if folder else (path.stem, path.suffix)
    n = 2
    while True:
        candidate = path.with_name(f'{stem} ({n}){suffix}')
        if not candidate.exists():
            return candidate
        n += 1


def link_or_copy(src: Path, dst: Path, check_cancel: Callable[[], None] | None = None) -> Path:
    """Hard-link `src` as `dst` (no extra space; re-runs share their input), copy where links are
    impossible (another volume, FAT/exFAT). Replaces `dst` without writing through an old link."""
    src, dst = Path(src), Path(dst)
    tmp = dst.with_name(f'.{dst.name}.{uuid.uuid4().hex}.link-tmp')
    check = check_cancel or (lambda: None)
    try:
        check()
        try:
            os.link(src, tmp)
        except OSError:
            if check_cancel is None:
                shutil.copyfile(src, tmp)
            else:
                with src.open('rb') as source, tmp.open('xb') as target:
                    while block := source.read(1 << 20):
                        check()
                        target.write(block)
        check()
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


# File types browsers can usually play in the plain players (comparing decodes on the server).
PLAYABLE_EXT = frozenset(('.mp3', '.m4a', '.wav', '.flac', '.opus', '.ogg', '.mp4', '.webm', '.aac'))

# What the players need: Python's table (from the OS) says e.g. audio/mp4a-latm for .m4a.
_MEDIA_TYPES = {
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.flac': 'audio/flac',
    '.wav': 'audio/wav',
    '.opus': 'audio/ogg',
    '.ogg': 'audio/ogg',
    '.webm': 'audio/webm',  # AudioLab only ever writes or keeps audio-only WebM
}


def media_type(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    return _MEDIA_TYPES.get(ext) or mimetypes.guess_type(f'x{ext}')[0] or 'application/octet-stream'


def human_bytes(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024.0:
            return f'{n:.1f} {unit}'
        n /= 1024.0
    return f'{n:.1f} PB'


def human_duration(seconds: float) -> str:
    """3725 -> '1:02:05', 95 -> '1:35'."""
    s = int(round(max(0.0, float(seconds or 0))))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'
