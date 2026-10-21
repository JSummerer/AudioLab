"""Seed pinned source downloads from a checked companion, never its build helpers.

The caller verifies the companion's outer checksum. Only today's manifest decides
which source bytes are reusable; normal acquisition still verifies every input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

MANIFEST = Path(__file__).resolve().with_name('sources.json')
MAX_MEMBERS = 10_000
MAX_SOURCE_BYTES = 1024 * 1024 * 1024


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def current_sources() -> dict[str, str]:
    sources = {}
    for row in json.loads(MANIFEST.read_text())['artifacts']:
        filename, checksum = row['filename'], row['sha256']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', filename):
            raise ValueError(f'Invalid source filename in current manifest: {filename!r}')
        if filename in sources or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError(f'Duplicate source filename or invalid checksum: {filename!r}')
        sources[filename] = checksum
    return sources


def seed_sources(companion: Path, directory: Path) -> dict[str, int]:
    expected = current_sources()
    allowed = {f'sources/{filename}': filename for filename in expected}
    report = {'seeded': 0, 'existing': 0, 'changed': 0, 'missing': 0}
    with tarfile.open(companion, 'r:gz') as archive:
        selected = {}
        for count, member in enumerate(archive, start=1):
            if count > MAX_MEMBERS:
                raise ValueError('Source companion has too many members.')
            parts = member.name.split('/')
            if '\\' in member.name or ':' in parts[0] or any(part in ('', '.', '..') for part in parts):
                raise ValueError(f'Unsafe source companion path: {member.name!r}')
            if member.name not in allowed:
                continue
            if member.name in selected:
                raise ValueError(f'Duplicate selected source: {member.name}')
            if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None:
                raise ValueError(f'Selected source must be a regular file: {member.name}')
            if not 0 <= member.size <= MAX_SOURCE_BYTES:
                raise ValueError(f'Selected source exceeds the size bound: {member.name}')
            selected[member.name] = member

        report['missing'] = len(expected) - len(selected)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError(f'Source download directory must not be a symlink: {directory}')
        with tempfile.TemporaryDirectory(prefix='.source-seed-', dir=directory) as scratch:
            for name, member in selected.items():
                filename = allowed[name]
                target = directory / filename
                if target.exists() or target.is_symlink():
                    if not stat.S_ISREG(target.lstat().st_mode) or digest(target) != expected[filename]:
                        raise FileExistsError(f'Refusing to overwrite unexpected source: {target}')
                    report['existing'] += 1
                    continue
                staged = Path(scratch) / filename
                with archive.extractfile(member) as source, staged.open('xb') as output:
                    shutil.copyfileobj(source, output)
                if digest(staged) != expected[filename]:
                    # An older companion can legitimately contain a prior pin.
                    report['changed'] += 1
                    staged.unlink()
                    continue
                # Same-filesystem exclusive publication cannot replace an existing path.
                os.link(staged, target)
                staged.unlink()
                report['seeded'] += 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('companion', type=Path, help='source companion with a verified outer checksum')
    parser.add_argument('directory', type=Path, help='owned source download directory')
    args = parser.parse_args()
    report = seed_sources(args.companion, args.directory)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
