"""Verify the completed helper, then write its checked archive and catalog."""

from __future__ import annotations

import argparse
import json
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

from prepare import CORE, DISTRIBUTIONS, HERE, digest

LIBRARY_ALIASES = {
    f'_internal/{name}': f'_internal/torch/lib/{name}'
    for name in (
        'libtorch_cpu.dylib',
        'libtorch_python.dylib',
        'libc10.dylib',
        'libomp.dylib',
        'libshm.dylib',
        'libtorch.dylib',
    )
}


def checked_aliases(bundle: Path) -> dict[str, str]:
    """Only these six pinned PyInstaller file aliases become archive hardlinks."""
    root = bundle.resolve(strict=True)
    aliases = {}
    for path in root.rglob('*'):
        if not path.is_symlink():
            continue
        name = path.relative_to(root).as_posix()
        if name not in LIBRARY_ALIASES:
            raise ValueError(f'Unexpected helper symlink: {name}')
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f'Helper alias does not name an internal regular file: {name}')
        target = resolved.relative_to(root).as_posix()
        if target != LIBRARY_ALIASES[name] or (root / target).is_symlink():
            raise ValueError(f'Helper alias differs from its pinned target: {name}')
        aliases[name] = target
    if aliases != LIBRARY_ALIASES:
        raise ValueError('The pinned helper library aliases are incomplete')
    return aliases


def verify(bundle: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    modules = set(CArchiveReader(str(bundle / 'AudioLabAI')).open_embedded_archive('PYZ.pyz').toc)
    top = {name.split('.')[0] for name in modules}
    known = {name for names in DISTRIBUTIONS.values() for name in names}
    unknown = top - known - sys.stdlib_module_names - {'audiolab'}
    unknown = {name for name in unknown if not name.startswith('_sysconfigdata_')}
    errors = []
    if unknown:
        errors.append(f'Uninventoried helper imports: {sorted(unknown)}')
    for name in ('audiolab.ai.worker', 'audiolab.ai.separation', 'torch', 'demucs.hf', 'demucs.htdemucs'):
        if name not in modules:
            errors.append(f'Missing helper module: {name}')
    for name, roots in DISTRIBUTIONS.items():
        if roots and not top.intersection(roots):
            errors.append(f'Inventoried distribution is absent from the helper: {name}')
    for path in bundle.rglob('*'):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(bundle.resolve()):
            errors.append(f'Helper symlink leaves the bundle: {path.relative_to(bundle)}')
    sys.path.insert(0, str(CORE))
    core = runpy.run_path(str(CORE / 'bundle_inventory.py'))
    native, native_errors = core['native_inventory'](bundle, 'arm64', '14.0')
    errors += native_errors
    versions = json.loads((bundle / 'ThirdParty/build-info.json').read_text())['dependencies']
    notices = bundle / 'ThirdParty'
    records = json.loads((notices / 'packages.json').read_text())
    by_name = {row['name'].lower().replace('_', '-'): row for row in records}
    if set(by_name) != set(versions):
        errors.append('The helper notice package list differs from its distribution inventory')
    for name, version in versions.items():
        row = by_name.get(name, {})
        originals = [notices / path for path in row.get('files', [])]
        if row.get('version') != version or not any(path.is_file() and path.stat().st_size for path in originals):
            errors.append(f'Missing exact original package notices: {name} {version}')
    native_meta = json.loads((HERE / 'safetensors-native.json').read_text())
    rust = next((bundle / '_internal/safetensors').glob('_safetensors_rust*.so'))
    if f'/rustc/{native_meta["rustc_commit"]}'.encode() not in rust.read_bytes():
        errors.append('Safetensors compiler identity differs from the reviewed Rust standard notices')
    for row in native_meta['crates']:
        directory = notices / 'safetensors-native' / f'{row["name"]}-{row["version"]}'
        if not (directory / 'PROVENANCE.txt').is_file() or not any(
            path.is_file() and any(word in path.name.lower() for word in ('license', 'licence', 'copying', 'notice'))
            for path in directory.rglob('*')
        ):
            errors.append(f'Missing original safetensors dependency notices: {directory.name}')
    for row in json.loads((HERE / 'models.json').read_text())['files']:
        model = bundle / 'models' / row['path']
        if model.stat().st_size != row['size'] or digest(model) != row['sha256']:
            errors.append(f'Changed offline model input: {row["path"]}')
    # Every Mach-O runtime object has its own ad-hoc signature. No user keychain
    # identity, administrator authorization or native UI is involved.
    magic = {bytes.fromhex(value) for value in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca')}
    signed = 0
    for path in bundle.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) not in magic:
                continue
        subprocess.run(['codesign', '--verify', '--strict', str(path)], check=True, capture_output=True)
        signed += 1
    result = {
        'ok': not errors,
        'module_count': len(modules),
        'native': native,
        'signed_native_files': signed,
        'notice_distributions': len(versions),
        'errors': errors,
    }
    if errors:
        raise ValueError(json.dumps(result, indent=2))
    return result


def archive(bundle: Path, destination: Path) -> dict:
    """Write payload once, with six checked direct-file hardlinks and no symlinks."""
    aliases = checked_aliases(bundle)
    with tarfile.open(destination, 'w:gz') as output:
        for path in sorted(bundle.rglob('*')):
            name = path.relative_to(bundle).as_posix()
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            if name in aliases:
                info.type = tarfile.LNKTYPE
                info.linkname = aliases[name]
                info.mode = 0o755
                output.addfile(info)
                continue
            attributes = path.lstat()
            if stat.S_ISDIR(attributes.st_mode):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                output.addfile(info)
            elif stat.S_ISREG(attributes.st_mode):
                info.size = attributes.st_size
                info.mode = 0o755 if attributes.st_mode & 0o111 else 0o644
                with path.open('rb') as stream:
                    output.addfile(info, stream)
            else:
                raise ValueError(f'Unsupported helper archive input: {name}')
    with tarfile.open(destination) as source:
        members = source.getmembers()
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate release archive paths')
    by_name = {member.name: member for member in members}
    for member in members:
        if member.islnk() and (member.linkname not in by_name or not by_name[member.linkname].isfile()):
            raise ValueError(f'Helper hardlink does not name a direct regular member: {member.name}')
    return {
        'installed_bytes': sum(member.size for member in members if member.isfile()),
        'download_bytes': destination.stat().st_size,
        'sha256': digest(destination),
        'archive_members': len(members),
        'archive_hardlinks': len(aliases),
    }


def companion(work: Path, output: Path) -> None:
    def neutral(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if '__pycache__' in Path(info.name).parts or info.name.endswith(('.pyc', '.pyo')):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info

    with tarfile.open(output, 'w:gz') as archive:
        for path in sorted((work / 'sources').iterdir()):
            if path.is_file() and not path.name.endswith('.partial'):
                archive.add(path, arcname=f'sources/{path.name}', filter=neutral)
        archive.add(HERE, arcname='packaging/ai', filter=neutral)
        archive.add(work / 'source-snapshot', arcname='source', filter=neutral)
        for name in ('sources.json', 'third_party.py', 'source_archive.py', 'SOURCE-README.md'):
            archive.add(CORE / name, arcname=f'packaging/macos/{name}', filter=neutral)
        archive.add(
            CORE / 'notices/python-3.12.15-20261001',
            arcname='packaging/macos/notices/python-3.12.15-20261001',
            filter=neutral,
        )
        archive.add(work / 'ThirdParty', arcname='ThirdParty', filter=neutral)


def release(work: Path, version: str, tag: str, output: Path | None = None) -> None:
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value) for value in (version, tag)):
        raise ValueError('Unsafe release version or tag')
    bundle = work / 'dist/AudioLabAI'
    shutil.copytree(work / 'pack-models', bundle / 'models', dirs_exist_ok=True)
    shutil.copytree(work / 'ThirdParty', bundle / 'ThirdParty', dirs_exist_ok=True)
    shutil.copy2(work / 'pack.json', bundle / 'pack.json')
    shutil.copy2(work / 'LICENSE', bundle / 'LICENSE')
    result = verify(bundle)
    release_dir = output or work / 'release'
    release_dir.mkdir(exist_ok=True)
    destination = release_dir / f'AudioLab-AI-{version}-macOS-arm64.tar.gz'
    if destination.exists():
        raise FileExistsError(f'Refusing to replace prepared release: {destination}')
    identity = archive(bundle, destination)
    catalog = {key: identity[key] for key in ('installed_bytes', 'download_bytes', 'sha256')}
    catalog.update(
        schema=1,
        protocol=1,
        version=version,
        platform='macos-arm64',
        executable='AudioLabAI',
        models=['htdemucs', 'htdemucs_ft'],
        url=f'https://github.com/JSummerer/AudioLab/releases/download/{tag}/{destination.name}',
    )
    (release_dir / 'catalog.json').write_text(json.dumps(catalog, indent=2) + '\n')
    (release_dir / 'inventory.json').write_text(json.dumps({**result, **identity}, indent=2) + '\n')
    sources = release_dir / f'AudioLab-AI-{version}-third-party-sources-arm64.tar.gz'
    companion(work, sources)
    (release_dir / 'SHA256SUMS.txt').write_text(
        f'{digest(destination)}  {destination.name}\n{digest(sources)}  {sources.name}\n'
    )
    print(json.dumps({'archive': str(destination), **identity}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--release-tag', required=True)
    parser.add_argument(
        '--output-directory', type=Path, help='Prepare a separate release without replacing prior assets'
    )
    args = parser.parse_args()
    release(
        args.work.resolve(),
        args.version,
        args.release_tag,
        args.output_directory.resolve() if args.output_directory else None,
    )
