"""Small release helper for pinned sources and original third-party notices.

Run notices with the clean bundle environment's Python. Explicit package names
are required: the developer environment also contains optional AI/dev packages.
Native notices and missing-wheel overrides remain explicit reviewed inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import tarfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / 'sources.json'


def artifacts(groups: str) -> list[dict]:
    selected = set(groups.split(','))
    rows = json.loads(MANIFEST.read_text())['artifacts']
    return [row for row in rows if selected.intersection(row['groups'])]


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(groups: str, directory: Path) -> list[dict]:
    directory.mkdir(parents=True, exist_ok=True)
    rows = artifacts(groups)

    def fetch_one(row: dict) -> None:
        target = directory / row['filename']
        if not target.exists():
            partial = target.with_suffix(target.suffix + '.partial')
            request = urllib.request.Request(row['url'], headers={'User-Agent': 'AudioLab-source-archive/1'})
            with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as output:
                shutil.copyfileobj(response, output)
            if digest(partial) != row['sha256']:
                raise RuntimeError(f'Source checksum failed: {row["filename"]}; retained {partial}')
            partial.replace(target)
        if digest(target) != row['sha256']:
            raise RuntimeError(f'Source checksum failed: {target}')

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(fetch_one, rows))
    print(f'Verified {len(rows)} pinned source archives.', flush=True)
    return rows


def companion(groups: str, directory: Path, output: Path) -> None:
    rows = fetch(groups, directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f'Refusing to overwrite {output}')

    def neutral_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info

    with tarfile.open(output, 'w:gz') as archive:
        for row in rows:
            archive.add(directory / row['filename'], arcname=f'sources/{row["filename"]}', filter=neutral_owner)
        for path in (MANIFEST, HERE / 'build_ffmpeg.sh', Path(__file__), HERE / 'SOURCE-README.md'):
            archive.add(path, arcname=f'packaging/macos/{path.name}', filter=neutral_owner)
        archive.add(HERE / 'notices', arcname='packaging/macos/notices', filter=neutral_owner)
        for name in ('restore_deno_sources.py', 'DENO-SOURCE-README.md', 'generate_deno_notices.py'):
            archive.add(HERE / name, arcname=f'packaging/macos/{name}', filter=neutral_owner)
    print(f'Source companion: {output}\nSHA256: {digest(output)}')


def notices(packages: str, output: Path, extra: list[Path]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    records = []
    missing = []
    for name in sorted({name.strip() for name in packages.split(',') if name.strip()}):
        distribution = importlib.metadata.distribution(name)
        title = f'{distribution.metadata["Name"]}-{distribution.version}'
        target = output / title
        files = []
        declared = distribution.metadata.get_all('License-File') or []
        for entry in distribution.files or []:
            source = distribution.locate_file(entry)
            is_license = any(term in source.name.lower() for term in ('license', 'copying', 'notice'))
            is_declared = any(str(entry).endswith('/' + path) for path in declared)
            if not source.is_file() or not (is_license or is_declared):
                continue
            # Preserve the complete license tree and avoid package collisions.
            relative = Path(str(entry))
            if relative.is_absolute() or '..' in relative.parts:
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            files.append(str(destination.relative_to(output)))
        if not files and not any(path.name == title for path in extra):
            missing.append(title)
        records.append({'name': distribution.metadata['Name'], 'version': distribution.version, 'files': files})
    for source in extra:
        if not source.is_dir():
            raise ValueError(f'Native/override notice input must be a directory: {source}')
        shutil.copytree(source, output / source.name, dirs_exist_ok=True)
    (output / 'packages.json').write_text(json.dumps(records, indent=2) + '\n')
    if missing:
        raise RuntimeError('License files absent from these wheels; supply reviewed overrides: ' + ', '.join(missing))
    print(f'Original package notices: {output}')


def native_notices(groups: str, directory: Path, output: Path) -> None:
    """Copy original license/notice files from the exact retained source archives."""
    rows = fetch(groups, directory)
    output.mkdir(parents=True, exist_ok=True)
    for row in rows:
        source = directory / row['filename']
        if not tarfile.is_tarfile(source):
            continue
        destination = output / f'{row["name"]}-{row["version"]}'
        with tarfile.open(source) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                if not member.isfile() or path.is_absolute() or '..' in path.parts:
                    continue
                if not any(word in path.name.lower() for word in ('license', 'copying', 'notice')):
                    continue
                target = destination / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream, target.open('wb') as result:
                    shutil.copyfileobj(stream, result)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'PROVENANCE.txt').write_text(row['url'] + '\nSHA256 ' + row['sha256'] + '\n')
    print(f'Native source notices: {output}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for action in ('fetch', 'companion', 'native-notices'):
        command = sub.add_parser(action)
        default_groups = (
            'media,soundfile,certifi,scipy-runtime,deno-roots'
            if action == 'native-notices'
            else 'media,build-tool,soundfile,certifi,notice-overrides,scipy-runtime,deno-roots,deno-reconstruction'
        )
        command.add_argument('--groups', default=default_groups)
        command.add_argument('--directory', type=Path, required=True)
        if action == 'companion':
            command.add_argument('--output', type=Path, required=True)
        elif action == 'native-notices':
            command.add_argument('--output', type=Path, required=True)
    command = sub.add_parser('notices')
    command.add_argument('--packages', required=True, help='Comma-separated exact bundle distribution names')
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--extra', type=Path, action='append', default=[])
    args = parser.parse_args()
    if args.command == 'fetch':
        fetch(args.groups, args.directory)
    elif args.command == 'companion':
        companion(args.groups, args.directory, args.output)
    elif args.command == 'native-notices':
        native_notices(args.groups, args.directory, args.output)
    else:
        notices(args.packages, args.output, args.extra)


if __name__ == '__main__':
    main()
