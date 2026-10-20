"""Verify pinned model inputs and prepare original notices for the frozen helper."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import runpy
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORE = ROOT / 'packaging/macos'

# These map the actual frozen import roots to their owning wheels. A release
# validates this mapping against the completed executable, rather than claiming
# that every package installed in the builder is shipped.
DISTRIBUTIONS = {
    'cffi': ('cffi', '_cffi_backend'),
    'demucs': ('demucs',),
    'einops': ('einops',),
    'filelock': ('filelock',),
    'fsspec': ('fsspec',),
    'jinja2': ('jinja2',),
    'julius': ('julius',),
    'markupsafe': ('markupsafe',),
    'mpmath': ('mpmath',),
    'networkx': ('networkx',),
    'numpy': ('numpy',),
    'packaging': ('packaging',),
    'pycparser': ('pycparser',),
    'pyinstaller': (),
    'pyyaml': ('yaml',),
    'safetensors': ('safetensors',),
    'scipy': ('scipy',),
    'setuptools': ('setuptools', '_distutils_hack'),
    'soundfile': ('soundfile', '_soundfile', '_soundfile_data'),
    'sympy': ('sympy',),
    'torch': ('torch', 'functorch', 'torchgen'),
    'tqdm': ('tqdm',),
    'typing-extensions': ('typing_extensions',),
}


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(row: dict, target: Path) -> None:
    """A pinned download may be reused only after checking its complete bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and digest(target) == row['sha256']:
        if 'size' not in row or target.stat().st_size == row['size']:
            return
    if target.exists():
        raise ValueError(f'Existing build input differs from its pin: {target}')
    partial = target.with_name(target.name + '.partial')
    request = urllib.request.Request(row['url'], headers={'User-Agent': 'AudioLab-AI-build/1'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as output:
                shutil.copyfileobj(response, output)
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            temporary = not isinstance(error, urllib.error.HTTPError) or error.code in (408, 429, 500, 502, 503, 504)
            if attempt == 2 or not temporary:
                raise
            time.sleep(2**attempt)
    if digest(partial) != row['sha256'] or ('size' in row and partial.stat().st_size != row['size']):
        raise ValueError(f'Build input failed its pin: {row["url"]}')
    partial.replace(target)


def prepare(work: Path, version: str, keep_source: bool = False) -> None:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', version):
        raise ValueError('The AI version must be a safe directory name')
    models = json.loads((HERE / 'models.json').read_text())
    for row in models['files']:
        fetch(row, work / 'pack-models' / row['path'])
    shutil.copy2(HERE / 'models.json', work / 'pack-models' / 'manifest.json')
    metadata = {'protocol': 1, 'version': version, 'models': ['htdemucs', 'htdemucs_ft']}
    (work / 'pack.json').write_text(json.dumps(metadata, indent=2) + '\n')
    snapshot = work / 'source-snapshot'
    if not keep_source or not snapshot.exists():
        if snapshot.is_symlink():
            raise ValueError('The owned source snapshot must not be a symlink')
        if snapshot.exists():
            shutil.rmtree(snapshot)
        snapshot.mkdir()
        shutil.copytree(ROOT / 'audiolab', snapshot / 'audiolab', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('pyproject.toml', 'LICENSE'):
            shutil.copy2(ROOT / name, snapshot / name)
        shutil.copy2(HERE / 'entry.py', snapshot / 'entry.py')
    source_hash = hashlib.sha256()
    for path in sorted(snapshot.rglob('*')):
        if path.is_file():
            source_hash.update(path.relative_to(snapshot).as_posix().encode() + b'\0')
            source_hash.update(path.read_bytes())

    # Reuse the reviewed source/native-notice machinery and matching pinned
    # LGPL/Fortran inputs of the same SoundFile/SciPy wheels in the core app.
    sys.path.insert(0, str(CORE))
    core = runpy.run_path(str(CORE / 'third_party.py'))
    core['native_notices']('soundfile,scipy-runtime', work / 'sources', work / 'native-notices')
    notices = work / 'ThirdParty'
    core['notices'](','.join(DISTRIBUTIONS), notices, [])
    shutil.copytree(work / 'native-notices', notices, dirs_exist_ok=True)
    python = CORE / 'notices/python-3.12.15-20261001'
    runtime = json.loads((python / 'PYTHON.json').read_text())
    if runtime['python_version'] != platform.python_version() or platform.machine() != 'arm64':
        raise ValueError('The reviewed Python notices differ from this build runtime')
    shutil.copytree(python, notices / python.name, dirs_exist_ok=True)
    for row in json.loads((HERE / 'sources.json').read_text())['artifacts']:
        fetch(row, work / 'sources' / row['filename'])
    # The safetensors wheel's main license omits its individual Rust dependency
    # texts. Retain the exact locked macOS production/build graph explicitly.
    native = runpy.run_path(str(CORE / 'third_party.py'))
    native['native_notices'].__globals__['MANIFEST'] = HERE / 'sources.json'
    native['native_notices']('safetensors-rust', work / 'sources', notices / 'safetensors-native')
    # Some objc2 crate archives omit their license files. Their exact repository
    # revisions are identified by the crates' .cargo_vcs_info.json and pinned.
    for row in json.loads((HERE / 'sources.json').read_text())['artifacts']:
        if 'safetensors-notice-overrides' not in row['groups']:
            continue
        with tarfile.open(work / 'sources' / row['filename']) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                if (
                    not member.isfile()
                    or path.is_absolute()
                    or '..' in path.parts
                    or not any(word in path.name.lower() for word in ('license', 'licence', 'copying', 'notice'))
                ):
                    continue
                for name in row['applies_to']:
                    target = notices / 'safetensors-native' / name / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open('wb') as destination:
                        shutil.copyfileobj(source, destination)
    shutil.copy2(HERE / 'sources.json', notices / 'safetensors-native/source-inputs.json')
    rust_standard = CORE / 'notices/deno-2.9.7/rust-standard'
    shutil.copytree(rust_standard, notices / 'safetensors-native/rust-standard', dirs_exist_ok=True)
    shutil.copy2(HERE / 'safetensors-native.json', notices / 'safetensors-native/provenance.json')
    shutil.copy2(HERE / 'models.json', notices / 'models.json')
    shutil.copy2(ROOT / 'LICENSE', work / 'LICENSE')
    info = {
        **metadata,
        'schema': 1,
        'platform': 'macos-arm64',
        'minimum_macos': '14.0',
        'python': {'version': platform.python_version(), 'build': '20261001'},
        'dependencies': {name: importlib.metadata.version(name) for name in DISTRIBUTIONS},
        'torch_git_version': __import__('torch').version.git_version,
        'source_sha256': source_hash.hexdigest(),
    }
    (notices / 'build-info.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')
    print(f'Prepared pinned offline models and {len(DISTRIBUTIONS)} package notices.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument(
        '--keep-source-snapshot', action='store_true', help='Refresh notices without changing frozen source'
    )
    args = parser.parse_args()
    prepare(args.work.resolve(), args.version, args.keep_source_snapshot)
