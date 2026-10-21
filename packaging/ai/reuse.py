"""Reuse the exact published helper only while its reviewed input contract is unchanged."""

from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PIN = HERE / 'published-helper.json'


def input_digest(path: str, content: bytes) -> str:
    # These explicitly listed inputs are text; checkout line endings do not alter the helper contract.
    content = content.replace(b'\r\n', b'\n')
    if path == 'audiolab/__init__.py':
        tree = ast.parse(content)
        versions = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == '__version__'
        ]
        if len(versions) != 1 or not isinstance(versions[0].value, ast.Constant):
            raise ValueError('The app version must remain one literal assignment before helper reuse.')
        value = versions[0].value
        if not isinstance(value.value, str):
            raise ValueError('The app version must be a string literal before helper reuse.')
        lines = content.splitlines(keepends=True)
        start = sum(map(len, lines[: value.lineno - 1])) + value.col_offset
        end = sum(map(len, lines[: value.end_lineno - 1])) + value.end_col_offset
        content = content[:start] + b"'CORE_VERSION'" + content[end:]
    return hashlib.sha256(content).hexdigest()


def check_inputs(root: Path, pin: dict) -> None:
    changed = [
        name
        for name, expected in pin['inputs'].items()
        if not (root / name).is_file() or input_digest(name, (root / name).read_bytes()) != expected
    ]
    if changed:
        raise ValueError(
            'Published AI helper inputs changed: '
            + ', '.join(changed)
            + '. Intentionally build and review a new AI version; do not repin from current source alone.'
        )


def source_member(path: str) -> str:
    if path == 'packaging/ai/entry.py':
        return 'source/entry.py'
    return f'source/{path}' if path.startswith('audiolab/') or path == 'LICENSE' else path


def check_companion(path: Path, pin: dict) -> None:
    """The baseline comes from the exact retained companion, never the current tree."""
    expected = {source_member(name): (name, digest) for name, digest in pin['inputs'].items()}
    found = set()
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            if member.name not in expected:
                continue
            if member.name in found or not member.isfile() or member.size > 2 * 1024**2:
                raise ValueError(f'Invalid pinned helper source member: {member.name}')
            name, digest = expected[member.name]
            if input_digest(name, archive.extractfile(member).read()) != digest:
                raise ValueError(f'The helper source companion differs from its input pin: {name}')
            found.add(member.name)
    if found != expected.keys():
        raise ValueError(f'The helper source companion is missing inputs: {sorted(expected.keys() - found)}')


def checked_asset(path: Path, row: dict) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
        raise ValueError(f'Published helper asset has the wrong size or type: {path.name}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != row['sha256']:
        raise ValueError(f'Published helper asset failed its checksum: {path.name}')


def obtain(name: str, row: dict, base_url: str, target: Path, source: Path | None) -> None:
    if target.exists():
        checked_asset(target, row)
        return
    partial = target.with_name(target.name + '.partial')
    try:
        if source is not None:
            checked_asset(source / name, row)
            shutil.copyfile(source / name, partial)
        else:
            url = f'{base_url}/{name}'
            for attempt in range(3):
                try:
                    request = urllib.request.Request(url, headers={'User-Agent': 'AudioLab-AI-reuse/1'})
                    with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as output:
                        shutil.copyfileobj(response, output)
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as error:
                    temporary = not isinstance(error, urllib.error.HTTPError) or error.code in (
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                    )
                    if attempt == 2 or not temporary:
                        raise RuntimeError(
                            f'Could not fetch pinned AI asset {url} after attempt {attempt + 1}/3'
                        ) from error
                    time.sleep(2**attempt)
        checked_asset(partial, row)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def reuse(work: Path, source: Path | None = None, *, root: Path = ROOT, pin_path: Path = PIN) -> Path:
    pin = json.loads(pin_path.read_text())
    if pin['schema'] != 1:
        raise ValueError('Unsupported published helper pin.')
    check_inputs(root, pin)
    release = work / 'release'
    release.mkdir(parents=True, exist_ok=True)
    base_url = pin['catalog']['url'].rsplit('/', 1)[0]
    for name, row in pin['assets'].items():
        target = release / ('catalog.json' if name == 'ai-catalog.json' else name)
        obtain(name, row, base_url, target, source)
    catalog = json.loads((release / 'catalog.json').read_text())
    if catalog != pin['catalog']:
        raise ValueError('The published helper catalog differs from its reviewed pin.')
    check_companion(release / pin['source_companion'], pin)
    archive = release / catalog['url'].rsplit('/', 1)[1]
    if pin['assets'][archive.name] != {'size': catalog['download_bytes'], 'sha256': catalog['sha256']}:
        raise ValueError('The published helper archive pin differs from its catalog.')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument(
        '--source-directory', type=Path, help='Reuse retained release assets instead of downloading them'
    )
    args = parser.parse_args()
    selected = reuse(args.work.resolve(), args.source_directory)
    print(f'Reusing the exact published helper: {selected.name}', flush=True)
    subprocess.run(
        [sys.executable, str(HERE / 'smoke.py'), str(selected), str(args.work.resolve() / 'smoke')], check=True
    )
