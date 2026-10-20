"""Canonical containers for the explicitly pinned Gitiles source archives.

Gitiles assigns generation-time tar mtimes even for an immutable Git revision.
This transform preserves source identities while normalizing container metadata;
the caller must still verify the canonical archive's exact SHA256 manifest pin.
"""

from __future__ import annotations

import argparse
import gzip
import posixpath
import tarfile
from pathlib import Path, PurePosixPath

NORMALIZATION = 'gitiles-tar-v1'


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or '..' in path.parts
        or '\\' in name
        or ':' in name
        or path.as_posix() != name.rstrip('/')
        or path.as_posix() == '.'
    ):
        raise ValueError(f'Unsafe or noncanonical archive path: {name!r}')
    return path.as_posix()


def checked_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    by_name = {}
    for member in members:
        name = safe_name(member.name)
        if name in by_name:
            raise ValueError(f'Duplicate archive path: {name}')
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE):
            raise ValueError(f'Unsupported archive member type: {name}')
        if set(member.pax_headers) - {'mtime', 'path', 'linkpath'}:
            raise ValueError(f'Unsupported PAX metadata: {name}')
        file_type = 0o040000 if member.isdir() else 0o120000 if member.issym() else 0o100000
        if member.mode & ~0o7777 not in (0, file_type):
            raise ValueError(f'Unexpected file mode bits: {name}')
        by_name[name] = member
    for name, member in by_name.items():
        if any(
            parent.as_posix() in by_name and not by_name[parent.as_posix()].isdir()
            for parent in PurePosixPath(name).parents
            if parent.as_posix() != '.'
        ):
            raise ValueError(f'Archive member has a nondirectory parent: {name}')
        if member.issym():
            target = member.linkname
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if (
                not target
                or target.startswith('/')
                or '\\' in target
                or ':' in target
                or '..' in PurePosixPath(target).parts
                or resolved == '..'
                or resolved.startswith('../')
            ):
                raise ValueError(f'Unsafe symbolic link: {name} -> {target}')
        elif member.islnk():
            target = safe_name(member.linkname)
            if target not in by_name or not by_name[target].isfile():
                raise ValueError(f'Hard link must name an archived regular file: {name} -> {target}')
    return sorted(members, key=lambda member: member.name)


def normalize(source: Path, destination: Path) -> None:
    """Write v1 canonical tar/gzip bytes; refuse overwrite and remove own partial output.

    Retains member name, type, mode, link target and regular-file bytes/size.
    Ownership, timestamps, archive ordering and optional PAX container metadata
    are replaced by one fixed layout. Long paths use deterministic PAX records.
    """
    created = False
    try:
        with tarfile.open(source, 'r:gz') as original:
            members = checked_members(original)
            with destination.open('xb') as raw:
                created = True
                with gzip.GzipFile(filename='', mode='wb', compresslevel=9, mtime=0, fileobj=raw) as compressed:
                    with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as result:
                        for member in members:
                            entry = tarfile.TarInfo(member.name)
                            entry.type = member.type
                            entry.mode = member.mode & 0o7777
                            entry.linkname = member.linkname
                            entry.size = member.size if member.isfile() else 0
                            entry.uid = entry.gid = 0
                            entry.uname = entry.gname = ''
                            entry.mtime = 0
                            entry.pax_headers = {}
                            if member.isfile():
                                with original.extractfile(member) as content:
                                    result.addfile(entry, content)
                            else:
                                result.addfile(entry)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    arguments = parser.parse_args()
    normalize(arguments.source, arguments.destination)
