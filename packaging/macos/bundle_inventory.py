"""Inventory the pinned core bundle and check the actual app before making a release.

This is a release-specific list, not an environment export: AI, test and unused
downloader extras installed in the build environment are not shipped.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import plistlib
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MINIMUM_MACOS = '14.0'
DENO_VERSION = '2.9.7'
DENO_TARGET = 'aarch64-apple-darwin'
DENO_SHA256 = 'b73737579d5a84c160e3316487594783fa5c15f4e13252a6a07050b755317f1a'
DENO_NOTICE_FILES = {
    'DENO-LICENSE.md',
    'GLIBC-MATH-NOTICE.txt',
    'RUST_NOTICES.txt',
    'RUST_CRATES.json',
    'NATIVE_NOTICES.txt',
    'NATIVE_COMPONENTS.json',
    'SOURCE-REBUILD.md',
    'deno-original/LICENSE.md',
    'deno-original/cli/tsc/dts/node/LICENSE',
    'deno-original/cli/tsc/dts/node/undici/LICENSE',
    'deno-original/ext/webgpu/LICENSE.md',
    'rust-standard/COPYRIGHT',
    'rust-standard/COPYRIGHT-library.html',
    'rust-standard/LICENSE-APACHE',
    'rust-standard/LICENSE-MIT',
    'typescript/LICENSE.txt',
    'typescript/NOTICE.txt',
}

# Actual third-party import roots in the core archive. PyInstaller contributes
# the executable bootloader, not its build-time Python package.
DISTRIBUTIONS = {
    'annotated-doc': ('annotated_doc',),
    'annotated-types': ('annotated_types',),
    'anyio': ('anyio',),
    'bottle': ('bottle',),
    'brotli': ('brotli', '_brotli'),
    'certifi': ('certifi',),
    'cffi': ('cffi', '_cffi_backend'),
    'charset-normalizer': ('charset_normalizer',),
    'click': ('click',),
    'deno': ('deno',),
    'fastapi': ('fastapi',),
    'h11': ('h11',),
    'idna': ('idna',),
    'numpy': ('numpy',),
    'opentelemetry-api': ('opentelemetry',),
    'packaging': ('packaging',),
    'proxy-tools': ('proxy_tools',),
    'pycparser': ('pycparser',),
    'pycryptodomex': ('Cryptodome',),
    'pydantic': ('pydantic',),
    'pydantic-core': ('pydantic_core',),
    'pyinstaller': (),
    'pyobjc-core': ('objc', 'PyObjCTools'),
    'pyobjc-framework-cocoa': ('AppKit', 'Foundation', 'CoreFoundation'),
    'pyobjc-framework-uniformtypeidentifiers': ('UniformTypeIdentifiers',),
    'pyobjc-framework-webkit': ('WebKit',),
    'python-multipart': ('multipart', 'python_multipart'),
    'pywebview': ('webview',),
    'requests': ('requests',),
    'scipy': ('scipy',),
    'setuptools': ('setuptools', '_distutils_hack'),
    'soundfile': ('soundfile', '_soundfile', '_soundfile_data'),
    'starlette': ('starlette',),
    'typing-extensions': ('typing_extensions',),
    'typing-inspection': ('typing_inspection',),
    'urllib3': ('urllib3',),
    'uvicorn': ('uvicorn',),
    'websockets': ('websockets',),
    'yt-dlp': ('yt_dlp',),
    'yt-dlp-ejs': ('yt_dlp_ejs',),
}
EXCLUDED_MODULES = (
    'torch',
    'torchaudio',
    'demucs',
    'mutagen',
    'imageio_ffmpeg',
    'pytest',
    '_pytest',
    'pluggy',
    'iniconfig',
    'pygments',
    'ruff',
    'PyInstaller',
    'altgraph',
    'macholib',
)
REQUIRED_MODULES = {
    'audiolab.runtime',
    'audiolab.cli',
    'audiolab.desktop',
    'audiolab.api.app',
    'audiolab.api.state',
    'audiolab.api.status',
    'audiolab.tasks.download',
    'audiolab.tasks.enhance',
    'audiolab.tasks.separate',
    'audiolab.tasks.analyze',
    'audiolab.tasks.common',
    'yt_dlp',
    'yt_dlp.extractor.youtube._video',
    'yt_dlp_ejs.yt.solver',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.lifespan.on',
    'uvicorn.loops.asyncio',
    'webview.platforms.cocoa',
}
REQUIRED_RESOURCES = (
    'audiolab/web/index.html',
    'audiolab/web/js/main.js',
    'audiolab/web/js/views/settings.js',
    'audiolab/web/css/tokens.css',
    'audiolab/web/fonts/source-serif-4-semibold-latin.woff2',
    'audiolab/web/fonts/source-serif-4-semibold-latin-ext.woff2',
    'audiolab/web/fonts/OFL.txt',
    'audiolab/assets/AppIcon.icns',
    'webview/js/api.js',
    'certifi/cacert.pem',
    'yt_dlp_ejs/yt/solver/core.min.js',
    'yt_dlp_ejs/yt/solver/lib.min.js',
    'LICENSE',
    'build-info.json',
    'ThirdParty/packages.json',
    'ThirdParty/build-info.json',
)
NATIVE_NOTICES = (
    'ffmpeg-7.1',
    'lame-3.100',
    'opus-1.5.2',
    'libsndfile-1.2.2',
    'libogg-1.3.5',
    'libvorbis-1.3.7',
    'flac-1.4.3',
    'opus-1.4',
    'mpg123-1.32.3',
    'gcc-13.4.0',
    'certifi-2026.10.19',
    'webpki-root-certs-0.26.6',
    'webpki-roots-0.26.1',
)


def canonical(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def source_commit() -> str | None:
    """Read Git identity without a Git command; a source archive may supply it explicitly."""
    value = os.environ.get('AUDIOLAB_PACKAGE_COMMIT')
    if value is not None:
        if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', value):
            raise ValueError('AUDIOLAB_PACKAGE_COMMIT must be a complete commit hash')
        return value.lower()
    git = ROOT / '.git'
    if git.is_file():
        git = (ROOT / git.read_text().removeprefix('gitdir:').strip()).resolve()
    if not git.is_dir():
        return None
    head = (git / 'HEAD').read_text().strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if not ref.startswith('refs/') or '..' in Path(ref).parts:
            raise ValueError('Unexpected Git HEAD reference')
        common = git
        if (git / 'commondir').is_file():
            common = (git / (git / 'commondir').read_text().strip()).resolve()
        loose = common / ref
        if loose.is_file():
            head = loose.read_text().strip()
        else:
            packed = common / 'packed-refs'
            rows = packed.read_text().splitlines() if packed.is_file() else []
            head = next((line.split()[0] for line in rows if line.endswith(' ' + ref)), '')
    return head if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head) else None


def source_digest() -> str:
    """Identify this working source as well as its base commit, without personal paths."""
    digest = hashlib.sha256()
    for folder in (ROOT / 'audiolab', HERE):
        for path in sorted(folder.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.name == '.DS_Store':
                continue
            digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def check_notices(notices: Path, versions: dict[str, str]) -> list[str]:
    errors = []
    records_path = notices / 'packages.json'
    if not records_path.is_file():
        return ['Missing ThirdParty/packages.json']
    records = json.loads(records_path.read_text())
    actual = {canonical(row['name']): row for row in records}
    if set(actual) != set(versions):
        errors.append(f'Notice package list differs: {sorted(set(actual) ^ set(versions))}')
    for name, version in versions.items():
        row = actual.get(name)
        if row is None:
            continue
        if row['version'] != version:
            errors.append(f'Notice version differs for {name}: {row["version"]} != {version}')
        folder = notices / f'{row["name"]}-{row["version"]}'
        originals = [
            path
            for path in folder.rglob('*')
            if path.is_file()
            and path.stat().st_size
            and any(word in path.name.lower() for word in ('license', 'copying', 'notice'))
        ]
        if not originals:
            errors.append(f'No original license/notice file for {name}')
    return errors


def check_native_notices(notices: Path) -> list[str]:
    errors = []
    for name in NATIVE_NOTICES:
        folder = notices / name
        if not (folder / 'PROVENANCE.txt').is_file():
            errors.append(f'Missing pinned native notice provenance: {name}')
        originals = [
            path
            for path in folder.rglob('*')
            if path.is_file()
            and path.stat().st_size
            and any(word in path.name.lower() for word in ('license', 'copying', 'notice'))
        ]
        if not originals:
            errors.append(f'Missing original native notice: {name}')
    return errors


def validate_deno(notices: Path, binary: Path | None = None) -> dict:
    """The reviewed notice asset must describe this exact upstream Deno input.

    PyInstaller may change binary headers/signatures, so the upstream hash is
    checked before freezing. It is retained as input provenance after signing.
    """
    folder = notices / f'deno-{DENO_VERSION}'
    provenance = folder / 'PROVENANCE.json'
    if not provenance.is_file():
        raise ValueError('Deno embedded notices are not assembled: missing reviewed PROVENANCE.json')
    record = json.loads(provenance.read_text())
    expected = {'name': 'deno', 'version': DENO_VERSION, 'target': DENO_TARGET, 'sha256': DENO_SHA256}
    if record.get('schema_version') != 1 or record.get('complete') is not True or record.get('runtime') != expected:
        raise ValueError('Deno embedded notices are incomplete or describe a different runtime')
    files = record.get('files')
    if not isinstance(files, dict) or DENO_NOTICE_FILES - files.keys():
        raise ValueError('Deno provenance omits required embedded-notice files')
    for name, checksum in files.items():
        relative = Path(name)
        path = folder / relative
        if relative.is_absolute() or '..' in relative.parts or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError(f'Deno notice path leaves its asset directory: {name}')
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Missing or empty Deno notice: {name}')
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError(f'Invalid Deno notice checksum: {name}')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != checksum:
            raise ValueError(f'Deno notice checksum failed: {name}')
    crates = json.loads((folder / 'RUST_CRATES.json').read_text())
    native = json.loads((folder / 'NATIVE_COMPONENTS.json').read_text())
    counts = [
        record.get(key)
        for key in ('production_rust_crates', 'original_rust_license_texts', 'native_original_notice_texts')
    ]
    if not all(isinstance(count, int) and count > 0 for count in counts):
        raise ValueError('Deno embedded-notice counts are missing or invalid')
    rust_text = (folder / 'RUST_NOTICES.txt').read_text()
    native_text = (folder / 'NATIVE_NOTICES.txt').read_text()
    rust_ids = list(map(int, re.findall(r'^===== (\d+): ', rust_text, re.MULTILINE)))
    native_ids = list(map(int, re.findall(r'^===== Native notice (\d+) =====$', native_text, re.MULTILINE)))
    if (
        not isinstance(crates, list)
        or not crates
        or len(crates) != record.get('production_rust_crates')
        or not all(
            isinstance(row, dict) and row.get('name') and row.get('version') and row.get('license') for row in crates
        )
        or not rust_ids
        or rust_ids != list(range(1, record.get('original_rust_license_texts', 0) + 1))
        or not isinstance(native, list)
        or not native
        or len(native) != record.get('native_original_notice_texts')
        or not all(isinstance(row, dict) for row in native)
        or native_ids != [row.get('notice') for row in native]
        or native_ids != list(range(1, len(native) + 1))
        or not all(row.get('sources') and row.get('sha256') for row in native)
    ):
        raise ValueError('Deno embedded-notice indexes and rendered texts disagree')
    if binary is not None:
        with binary.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != DENO_SHA256:
            raise ValueError('The build-input Deno binary differs from the reviewed notice asset')
    return expected


def prepare_notices(work: Path) -> None:
    notices = work / 'ThirdParty'
    versions = {name: importlib.metadata.version(name) for name in DISTRIBUTIONS}
    errors = check_notices(notices, versions)
    if errors:
        raise ValueError('\n'.join(errors))
    native = work / 'native-notices'
    if not native.is_dir() or not any(native.iterdir()):
        raise ValueError('Assemble the exact native-notices directory before preparing the bundle')
    errors = check_native_notices(native)
    if errors:
        raise ValueError('\n'.join(errors))
    shutil.copytree(native, notices, dirs_exist_ok=True)

    prefix = Path(sys.base_prefix)
    build = (prefix / 'BUILD').read_text().strip()
    python_notices = HERE / 'notices' / f'python-{platform.python_version()}-{build}'
    metadata = json.loads((python_notices / 'PYTHON.json').read_text())
    expected_triple = {'arm64': 'aarch64-apple-darwin', 'x86_64': 'x86_64-apple-darwin'}.get(platform.machine())
    if metadata['python_version'] != platform.python_version() or metadata['target_triple'] != expected_triple:
        raise ValueError('The reviewed Python runtime notices do not match the build interpreter')
    shutil.copytree(python_notices, notices / python_notices.name, dirs_exist_ok=True)
    deno_notices = HERE / 'notices' / f'deno-{DENO_VERSION}'
    validate_deno(HERE / 'notices')
    if versions['deno'] != DENO_VERSION:
        raise ValueError('The installed Deno wheel differs from the reviewed runtime version')
    import deno

    deno_input = validate_deno(HERE / 'notices', Path(deno.find_deno_bin()))
    shutil.copytree(deno_notices, notices / deno_notices.name, dirs_exist_ok=True)
    license_file = next(
        (
            path
            for path in (
                prefix / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'LICENSE.txt',
                prefix / 'LICENSE.txt',
                prefix / 'LICENSE',
            )
            if path.is_file()
        ),
        None,
    )
    if license_file is None:
        raise ValueError('The build interpreter has no Python LICENSE file')
    shutil.copy2(license_file, notices / python_notices.name / 'LICENSE.build-interpreter.txt')
    info = {
        'schema': 1,
        'app': 'AudioLab',
        'version': runpy.run_path(str(ROOT / 'audiolab/__init__.py'))['__version__'],
        'architecture': platform.machine(),
        'minimum_macos': MINIMUM_MACOS,
        'python': {
            'version': platform.python_version(),
            'implementation': platform.python_implementation(),
            'build': build,
        },
        'dependencies': versions,
        'native_inputs': {'deno': deno_input},
        'commit': source_commit(),
        'source_sha256': source_digest(),
    }
    (notices / 'build-info.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    print(f'Prepared {len(versions)} distribution notices, native notices and build-info.json')


def native_inventory(app: Path, architecture: str, minimum: str) -> tuple[dict, list[str]]:
    from macholib.mach_o import LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX
    from macholib.MachO import MachO

    magic = {
        bytes.fromhex(value)
        for value in (
            'feedface',
            'cefaedfe',
            'feedfacf',
            'cffaedfe',
            'cafebabe',
            'bebafeca',
            'cafebabf',
            'bfbafeca',
        )
    }
    cpu_names = {0x0100000C: 'arm64', 0x01000007: 'x86_64'}
    allowed = tuple((list(map(int, minimum.split('.'))) + [0, 0])[:3])
    count, architectures, versions, errors = 0, set(), {}, []
    for path in sorted(app.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) not in magic:
                continue
        count += 1
        relative = path.relative_to(app).as_posix()
        slices = set()
        for header in MachO(str(path)).headers:
            arch = cpu_names.get(header.header.cputype, str(header.header.cputype))
            architectures.add(arch)
            slices.add(arch)
            if arch != architecture:
                continue
            declarations = [
                command.minos if load.cmd == LC_BUILD_VERSION else command.version
                for load, command, _data in header.commands
                if load.cmd in (LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX)
            ]
            if not declarations:
                errors.append(f'No macOS minimum load command: {relative}')
                continue
            value = max(declarations)
            version = (value >> 16, (value >> 8) & 255, value & 255)
            versions[relative] = version
            if version > allowed:
                errors.append(f'{relative} requires macOS {".".join(map(str, version))}, above {minimum}')
        if architecture not in slices:
            errors.append(f'{relative} has no {architecture} code: {sorted(slices)}')
    highest = max(versions.values(), default=(0, 0, 0))
    return {
        'files': count,
        'architectures': sorted(architectures),
        'highest_declared_minimum_macos': '.'.join(map(str, highest)),
        'files_at_highest_minimum': sum(version == highest for version in versions.values()),
        'examples_at_highest_minimum': sorted(path for path, version in versions.items() if version == highest)[:8],
    }, errors


def verify_app(app: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    app = app.resolve()
    errors = []
    for path in app.rglob('*'):
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute():
                errors.append(f'Non-relocatable absolute symlink: {path.relative_to(app)}')
            try:
                if not path.resolve(strict=True).is_relative_to(app):
                    errors.append(f'Symlink leaves the app: {path.relative_to(app)}')
            except (OSError, RuntimeError):
                errors.append(f'Broken or circular symlink: {path.relative_to(app)}')
    contents = app / 'Contents'
    plist = plistlib.loads((contents / 'Info.plist').read_bytes())
    executable = contents / 'MacOS' / 'AudioLab'
    if plist.get('CFBundleExecutable') != 'AudioLab' or not os.access(executable, os.X_OK):
        errors.append('Missing or unexpected executable identity')
    modules = set(CArchiveReader(str(executable)).open_embedded_archive('PYZ.pyz').toc)
    missing = REQUIRED_MODULES - modules
    if missing:
        errors.append(f'Missing runtime modules: {sorted(missing)}')
    forbidden = sorted(name for name in modules if name.split('.')[0] in EXCLUDED_MODULES)
    if forbidden:
        errors.append(f'Excluded packages in the actual archive: {forbidden}')
    top = {name.split('.')[0] for name in modules}
    known = {root for roots in DISTRIBUTIONS.values() for root in roots}
    unknown = top - known - sys.stdlib_module_names - {'audiolab', 'native_smoke'}
    unknown = {name for name in unknown if not name.startswith('_sysconfigdata_')}
    if unknown:
        errors.append(f'Uninventoried third-party import roots: {sorted(unknown)}')
    for name, roots in DISTRIBUTIONS.items():
        if roots and not top.intersection(roots):
            errors.append(f'Listed distribution is absent from the actual archive: {name}')
    resources = contents / 'Resources'
    for relative in REQUIRED_RESOURCES:
        path = resources / relative
        if not path.is_file() or not path.stat().st_size:
            errors.append(f'Missing or empty resource: {relative}')
    for tool in ('ffmpeg', 'deno'):
        path = contents / 'Frameworks' / 'tools' / tool
        if not path.is_file() or not os.access(path, os.X_OK):
            errors.append(f'Missing executable tool: {tool}')
    for pattern in (
        'libpython3.12.dylib',
        '_soundfile_data/libsndfile*.dylib',
        'numpy/_core/_multiarray_umath*.so',
        'scipy/signal/_sigtools*.so',
        'pydantic_core/_pydantic_core*.so',
        'objc/_objc*.so',
        'WebKit/_WebKit*.so',
    ):
        if not any(path.is_file() for path in (contents / 'Frameworks').glob(pattern)):
            errors.append(f'Missing native runtime component: {pattern}')
    info_path = resources / 'build-info.json'
    info = json.loads(info_path.read_text()) if info_path.is_file() else {}
    versions = info.get('dependencies', {})
    if set(versions) != set(DISTRIBUTIONS):
        errors.append('Build-info distribution list differs from this core release inventory')
    errors += check_notices(resources / 'ThirdParty', versions)
    errors += check_native_notices(resources / 'ThirdParty')
    deno_version, deno_packaged_sha256 = None, None
    try:
        deno_input = validate_deno(resources / 'ThirdParty')
        if info.get('native_inputs', {}).get('deno') != deno_input:
            errors.append('Build-info Deno input identity differs from the reviewed notices')
        environment = dict(os.environ)
        environment.update(DENO_DIR=str(app.parent / '.deno-inventory-cache'), DENO_NO_UPDATE_CHECK='1')
        result = subprocess.run(
            [str(contents / 'Frameworks/tools/deno'), '--version'],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        match = re.search(r'^deno (\d+\.\d+\.\d+)\b', result.stdout)
        deno_version = match.group(1) if match else None
        if deno_version != DENO_VERSION:
            errors.append(f'The bundled Deno version differs: {deno_version}')
        with (contents / 'Frameworks/tools/deno').open('rb') as stream:
            deno_packaged_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        errors.append(f'Deno notice/input verification failed: {error}')
    if info:
        if info.get('version') != plist.get('CFBundleVersion'):
            errors.append('Build-info version differs from Info.plist')
        if info.get('minimum_macos') != plist.get('LSMinimumSystemVersion'):
            errors.append('Build-info minimum macOS differs from Info.plist')
        copy = resources / 'ThirdParty' / 'build-info.json'
        if copy.is_file() and json.loads(copy.read_text()) != info:
            errors.append('ThirdParty/build-info.json differs from the root build-info.json')
        python_folder = resources / 'ThirdParty' / f'python-{info["python"]["version"]}-{info["python"]["build"]}'
        for name in ('LICENSE.build-interpreter.txt', 'LICENSE.cpython.txt', 'LICENSE.openssl-3.txt', 'PYTHON.json'):
            if not (python_folder / name).is_file():
                errors.append(f'Missing reviewed Python runtime notice: {name}')
    architecture = info.get('architecture', platform.machine())
    native, native_errors = native_inventory(app, architecture, plist['LSMinimumSystemVersion'])
    errors += native_errors
    return {
        'ok': not errors,
        'module_count': len(modules),
        'notice_distributions': len(versions),
        'deno_version': deno_version,
        'deno_packaged_sha256': deno_packaged_sha256,
        'native': native,
        'errors': errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--distributions', action='store_true')
    action.add_argument('--prepare-notices', type=Path, metavar='WORK')
    action.add_argument('--verify-app', type=Path, metavar='APP')
    args = parser.parse_args()
    try:
        if args.distributions:
            print(','.join(DISTRIBUTIONS))
        elif args.prepare_notices:
            prepare_notices(args.prepare_notices.resolve())
        else:
            result = verify_app(args.verify_app)
            print(json.dumps(result, indent=2))
            return 0 if result['ok'] else 1
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f'Bundle inventory failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
