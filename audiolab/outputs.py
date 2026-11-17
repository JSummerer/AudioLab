"""What one job attempt created in the output folder, so a failed or cancelled attempt can remove exactly that.

JobManager and the CLI open a record around each attempt (`recording`). `media.export` notes every file as it gets its
final name, with the identity of the finished temporary file it came from (a link or a rename keeps the
inode, so nothing that appears at that name meanwhile can be taken for it), and `tasks.common.output_folder`
notes the folder it makes. Outside an attempt (for example, saving a cover later from the job page) nothing is
recorded. The record is a context variable, so it belongs to the worker thread running that attempt.

`Created.discard` never raises and removes only what is still the very thing recorded:
- a file: device, inode, size and modification time must match. It is first renamed to a private name in the
  same folder (atomic: whatever has the name at that instant is what moves), checked there, and deleted only
  on a match; anything else is put back under a free name (see `_put_back` for drives without hard links).
- a folder: device and inode must match, and it must hold nothing but what file managers leave (.DS_Store …);
  rmdir refuses anything else. The check and the removal are two steps, so a folder made at that name in the
  instant between them goes too if it is still empty (or holds only such leftovers).
A task's result is complete once it returned: from then on nothing is removed (jobs.JobManager._run). Files of
an attempt the app could not finish (a crash, a forced quit) stay: the record lives only in memory.
Residual risk: if the app is killed in the instant after a stranger's file was moved aside and before it is
put back, it stays under a hidden '.<name>.<id>.discard' name next to where it was; it is never deleted.
On a drive whose file IDs change when a file is renamed (some FAT/exFAT drivers), the attempt's files do not
match and stay.
"""

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .util import FOLDER_JUNK


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    # Inode numbers can be reused after a delete: size and time make a stranger's file at that name not "ours".
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


class Created:
    def __init__(self) -> None:
        self.files: list[tuple[Path, tuple[int, int, int, int]]] = []
        self.folders: list[tuple[Path, tuple[int, int]]] = []

    def discard(self) -> None:
        for path, identity in reversed(self.files):
            try:
                _discard_file(path, identity)
            except Exception:  # it runs while a job fails or is cancelled: never add an error of its own
                continue
        for folder, (dev, ino) in sorted(self.folders, key=lambda item: len(item[0].parts), reverse=True):
            try:
                st = os.lstat(folder)
                if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (dev, ino):
                    _remove_if_empty(folder)
            except Exception:
                continue
        self.files.clear()
        self.folders.clear()


def _remove_if_empty(folder: Path) -> None:
    """Remove the attempt's folder when it holds nothing, or only what file managers leave (.DS_Store …).
    Anything else keeps it untouched; rmdir refuses a file someone puts there meanwhile."""
    with os.scandir(folder) as it:
        entries = list(it)
    if not all(e.name in FOLDER_JUNK and e.is_file(follow_symlinks=False) for e in entries):
        return
    for e in entries:
        os.unlink(e.path)
    os.rmdir(folder)


def _discard_file(path: Path, identity: tuple[int, int, int, int]) -> None:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or _identity(st) != identity:
        return  # not ours (any more): left alone, not even moved
    aside = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.discard')
    os.rename(path, aside)  # whatever has the name at this instant moves; checked below, where nobody else looks
    try:
        st = os.lstat(aside)
        if stat.S_ISREG(st.st_mode) and _identity(st) == identity:
            os.unlink(aside)
            return
    except OSError:
        pass
    _put_back(aside, path)


def _put_back(aside: Path, path: Path) -> None:
    """Give what turned out not to be ours its name back: under a free variant of the name ('Song (2).wav')
    when something else took it meanwhile, and as a last resort under a visible name with a random part, so
    it is never left hidden. A name is claimed only if it is free: a hard link, else an empty placeholder
    that the file is then moved onto. On a drive without hard links, a file another program saves at that
    very name in the instant between the placeholder and the move is replaced; closing that would need an
    exclusive rename, which not every system offers."""
    mode = os.lstat(aside).st_mode
    folder = stat.S_ISDIR(mode)
    stem, suffix = (path.name, '') if folder else (path.stem, path.suffix)
    numbered = [path.with_name(f'{stem} ({n}){suffix}') for n in range(2, 100)]
    for target in [path, *numbered, path.with_name(f'{stem} ({uuid.uuid4().hex[:8]}){suffix}')]:
        if stat.S_ISREG(mode):
            try:
                os.link(aside, target)  # fails if the name was taken meanwhile
            except FileExistsError:
                continue
            except OSError:
                pass  # no hard links on this drive: reserve the name, then move onto it
            else:
                os.unlink(aside)
                return
        try:  # created only if free
            placed = placeholder(target, folder)
        except FileExistsError:
            continue
        try:
            os.replace(aside, target)
        except OSError:
            drop_placeholder(target, placed)  # it stays aside, intact
            raise
        return


def placeholder(target: Path, folder: bool = False) -> os.stat_result:
    """Claim `target` with an empty file (or folder) only if the name is free; FileExistsError otherwise."""
    if folder:
        os.mkdir(target)
        return os.lstat(target)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def drop_placeholder(target: Path, placed: os.stat_result) -> None:
    """Remove a placeholder `placeholder` made, but only while it is still that empty placeholder: another
    program may have saved its own file at the name since."""
    try:
        now = os.lstat(target)
    except OSError:
        return
    if (now.st_dev, now.st_ino) != (placed.st_dev, placed.st_ino):
        return
    if stat.S_ISDIR(now.st_mode):
        os.rmdir(target)  # refuses a folder that is no longer empty
    elif stat.S_ISREG(now.st_mode) and now.st_size == 0:
        os.unlink(target)


_current: ContextVar[Created | None] = ContextVar('audiolab_created_outputs', default=None)


@contextmanager
def recording(created: Created) -> Iterator[Created]:
    token = _current.set(created)
    try:
        yield created
    finally:
        _current.reset(token)


def created_file(path: Path, before: os.stat_result) -> None:
    """Note a file this attempt just gave its final name; `before`: the stat of the finished temporary file
    it was linked or renamed from, taken before it had that name (no-op outside an attempt)."""
    record = _current.get()
    if record is not None:
        record.files.append((Path(path), _identity(before)))


def created_folder(path: Path) -> None:
    """Note a folder this attempt just made (no-op outside an attempt)."""
    record = _current.get()
    if record is None:
        return
    try:
        st = os.lstat(path)
    except OSError:  # gone already: nothing to remove later
        return
    record.folders.append((Path(path), (st.st_dev, st.st_ino)))
