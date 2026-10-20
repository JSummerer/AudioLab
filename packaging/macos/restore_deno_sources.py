#!/usr/bin/env python3
"""Restore the pinned Deno/V8 source set without downloading or running build tools."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath

GROUP = 'deno-reconstruction'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(value: str) -> Path:
    """Accept portable relative paths only, without normalization hiding traversal."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or ':' in value:
        raise ValueError(f'Unsafe relative path: {value!r}')
    return Path(*path.parts)


def contained(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve()
    except RuntimeError as error:
        raise ValueError(f'Unresolvable symbolic link: {path}') from error
    if not resolved.is_relative_to(root):
        raise ValueError(f'Path escapes restored source tree: {path}')


def prepare_parent(path: Path, root: Path) -> None:
    contained(path, root)
    # Reject even in-tree links as write destinations; extraction must never follow them.
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'Refusing to write through symbolic link: {parent}')
    path.parent.mkdir(parents=True, exist_ok=True)


def member_path(name: str, strip: int) -> Path | None:
    parts = relative_path(name).parts
    if len(parts) <= strip:
        return None
    return Path(*parts[strip:])


def extract(archive: Path, destination: Path, root: Path, strip: int) -> None:
    """Extract regular files and safe links; never use tarfile.extractall."""
    prepare_parent(destination, root)
    destination.mkdir(parents=True, exist_ok=True)
    links: list[tuple[Path, tarfile.TarInfo]] = []
    with tarfile.open(archive, 'r:*') as source:
        for member in source:
            relative = member_path(member.name, strip)
            if relative is None:
                continue
            target = destination / relative
            prepare_parent(target, root)
            if member.isdir():
                target.mkdir(exist_ok=True)
            elif member.isfile():
                stream = source.extractfile(member)
                if stream is None:
                    raise ValueError(f'No data for {member.name}')
                with stream, target.open('xb') as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            elif member.issym() or member.islnk():
                links.append((target, member))
            else:
                raise ValueError(f'Unsupported archive entry: {member.name}')
    # Links come last so no file extraction can accidentally follow a link.
    pending = links
    while pending:
        next_pending = []
        for target, member in pending:
            prepare_parent(target, root)
            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or '\\' in member.linkname or ':' in member.linkname:
                    raise ValueError(f'Unsafe symbolic link: {member.name}')
                contained(target.parent / member.linkname, root)
                target.symlink_to(member.linkname)
            else:
                relative = member_path(member.linkname, strip)
                if relative is None:
                    raise ValueError(f'Invalid hard link: {member.name}')
                original = destination / relative
                contained(original, root)
                if not original.exists():
                    next_pending.append((target, member))
                    continue
                if original.is_symlink() or not original.is_file():
                    raise ValueError(f'Invalid hard link target: {member.name}')
                os.link(original, target)
        if len(next_pending) == len(pending):
            raise ValueError('Archive has unresolved hard links')
        pending = next_pending


def checksum_crate(directory: Path, package_checksum: str) -> None:
    files = {}
    for path in sorted(directory.rglob('*')):
        if path.is_file() and path != directory / '.cargo-checksum.json':
            files[path.relative_to(directory).as_posix()] = sha256(path)
    (directory / '.cargo-checksum.json').write_text(
        json.dumps({'files': files, 'package': package_checksum}, sort_keys=True) + '\n', encoding='utf-8'
    )


def configure(output: Path) -> None:
    deno = output / 'deno'
    lockfile = deno / 'Cargo.lock'
    shutil.copyfile(lockfile, deno / 'Cargo.lock.upstream')
    manifest = deno / 'Cargo.toml'
    with manifest.open('a', encoding='utf-8') as target:
        target.write('\n# Source-companion override: rebuild the native V8 library.\n')
        target.write('[patch.crates-io]\nv8 = { path = "../rusty_v8" }\n')
    config = deno / '.cargo' / 'config.toml'
    config.parent.mkdir(exist_ok=True)
    with config.open('a', encoding='utf-8') as target:
        target.write('\n# Source companion: resolve registry dependencies from retained archives.\n')
        target.write('[source.crates-io]\nreplace-with = "source-companion"\n')
        target.write('[source.source-companion]\ndirectory = "vendor"\n')


def restore(archives: Path, output: Path, manifest: Path) -> int:
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError(f'Output must be a new or empty directory: {output}')
    document = json.loads(manifest.read_text(encoding='utf-8'))
    artifacts = [item for item in document['artifacts'] if GROUP in item.get('groups', [])]
    if not artifacts:
        raise ValueError(f'Manifest contains no {GROUP} artifacts')
    # Validate every pin before extracting anything; a missing source must fail closed.
    for item in artifacts:
        filename = relative_path(item['filename'])
        if len(filename.parts) != 1:
            raise ValueError(f'Archive filename must be a basename: {filename}')
        relative_path(item['destination'])
        strip = item['strip_components']
        if not isinstance(strip, int) or isinstance(strip, bool) or strip < 0:
            raise ValueError(f'Invalid strip_components for {filename}')
        archive = archives / filename
        if sha256(archive) != item['sha256']:
            raise ValueError(f'SHA256 mismatch: {archive}')
    output.mkdir(parents=True, exist_ok=True)
    root = output.resolve()
    for item in artifacts:
        destination = root / relative_path(item['destination'])
        extract(archives / item['filename'], destination, root, item['strip_components'])
        if item['filename'].endswith('.crate'):
            checksum_crate(destination, item['sha256'])
    configure(root)
    shutil.copyfile(manifest, root / 'source-manifest.json')
    print(f'Restored {len(artifacts)} pinned archives into {root}')
    print('Read DENO-SOURCE-README.md before invoking native build tools.')
    return len(artifacts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives', type=Path, required=True, help='Companion sources/ directory')
    parser.add_argument('--output', type=Path, required=True, help='New or empty reconstruction directory')
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('sources.json'))
    args = parser.parse_args()
    try:
        restore(args.archives.resolve(), args.output.absolute(), args.manifest.resolve())
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        parser.exit(
            1, f'Source restoration failed: {error}\nUse a new empty output directory after fixing the input.\n'
        )


if __name__ == '__main__':
    main()
