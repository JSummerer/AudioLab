"""Render the reviewed Deno 2.9.7 original notices without local build metadata.

Input cargo-about JSON and downloaded source archives stay in the private work
directory. Only original notice texts and compact, path-free provenance enter
the application. Run after cargo-about's target-filtered generation succeeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path, PurePosixPath

import tomllib

HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def notice_name(path: PurePosixPath) -> bool:
    name = path.name.lower()
    return name.startswith(('license', 'copying', 'copyright', 'notice')) and path.suffix.lower() not in {
        '.py',
        '.rs',
        '.c',
        '.h',
        '.cc',
        '.cpp',
        '.m',
        '.go',
        '.js',
        '.ts',
        '.html',
    }


def generate(work: Path, downloads: Path, output: Path, complete: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads((work / 'deno-rust-notices.json').read_text())
    manifest = json.loads((HERE / 'sources.json').read_text())['artifacts']
    lock = tomllib.loads((work / 'extracted/deno-2.9.7/Cargo.lock').read_text())
    packages = {(row['name'], row['version']): row for row in lock['package']}
    deno_source_hash = next(row['sha256'] for row in manifest if row['name'] == 'deno-source')
    records = []
    rust_text = ['Deno 2.9.7: original production Rust dependency notices\n']
    for index, license_row in enumerate(data['licenses'], 1):
        users = sorted({(item['crate']['name'], item['crate']['version']) for item in license_row['used_by']})
        rust_text.append(f'\n===== {index}: {license_row["name"]} ({license_row["id"]}) =====\n')
        rust_text.append('Used by: ' + ', '.join(f'{name} {version}' for name, version in users) + '\n\n')
        rust_text.append(license_row['text'] + '\n')
    for row in data['crates']:
        package = row['package']
        name, version = package['name'], package['version']
        selected = row['license']
        if selected == 'Unknown':
            if (name, version) != ('libuv-sys-lite', '1.48.4'):
                raise ValueError(f'Unresolved original notice: {name} {version}')
            selected = 'MIT; original LICENSE also preserves upstream libuv BSD/ISC notices'
        records.append(
            {
                'name': name,
                'version': version,
                'license': selected,
                'repository': package.get('repository'),
                'source_sha256': packages[(name, version)].get('checksum', deno_source_hash),
                'source_kind': 'registry crate' if package.get('source') else 'Deno source archive',
            }
        )
    registry = next((work / 'cargo-home/registry/src').iterdir())
    rust_text.append('\n===== libuv-sys-lite 1.48.4: original omitted-path notice =====\n')
    rust_text.append((registry / 'libuv-sys-lite-1.48.4/LICENSE').read_text())
    # Apache NOTICE/COPYRIGHT material can supplement the harvested license text.
    for row in records:
        source = registry / f'{row["name"]}-{row["version"]}'
        for path in sorted(source.rglob('*')):
            if (
                path.is_file()
                and path.name.upper().startswith(('NOTICE', 'COPYRIGHT'))
                and notice_name(PurePosixPath(path.name))
            ):
                rust_text.append(f'\n===== {row["name"]} {row["version"]}: {path.relative_to(source)} =====\n')
                rust_text.append(path.read_text(errors='replace') + '\n')
    (output / 'RUST_NOTICES.txt').write_text(''.join(rust_text))
    (output / 'RUST_CRATES.json').write_text(json.dumps(records, indent=2, ensure_ascii=False) + '\n')

    # Retain a compact union of original native component notices. Identical
    # texts are stored once, with all source paths listed in the public index.
    native = {}
    glibc_notice = None
    for row in manifest:
        if row.get('source_kind') != 'native-archive':
            continue
        destination = row['destination']
        if destination in {'rusty_v8/build', 'rusty_v8/buildtools', 'rusty_v8/tools/clang'} or any(
            name in destination for name in ('jinja2', 'markupsafe')
        ):
            continue
        source = downloads / row['filename']
        if sha256(source) != row['sha256']:
            raise ValueError(f'Native source checksum mismatch: {row["filename"]}')
        with tarfile.open(source) as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                relative = PurePosixPath(*path.parts[row.get('strip_components', 0) :])
                if member.isfile() and str(relative).endswith('third_party/glibc/src/sysdeps/ieee754/dbl-64/s_sin.c'):
                    glibc_notice = archive.extractfile(member).read().split(b'*/', 1)[0] + b'*/\n'
                if (
                    not member.isfile()
                    or not notice_name(relative)
                    or any(part in {'test', 'tests', 'testdata', 'benchmarks', 'samples'} for part in relative.parts)
                ):
                    continue
                original = archive.extractfile(member).read()
                digest = hashlib.sha256(original).hexdigest()
                item = native.setdefault(digest, {'text': original.decode(errors='replace'), 'sources': []})
                item['sources'].append({'component': destination, 'path': str(relative), 'revision': row['version']})
    chromium = (work / 'CHROMIUM-LICENSE.txt').read_bytes()
    native[hashlib.sha256(chromium).hexdigest()] = {
        'text': chromium.decode(),
        'sources': [{'component': 'Chromium generated Rust wrappers', 'path': 'LICENSE'}],
    }
    native_text = ['Deno 2.9.7 / Rusty V8 150.4.0: original native notices\n']
    native_index = []
    for index, (digest, item) in enumerate(sorted(native.items()), 1):
        native_text.append(f'\n===== Native notice {index} =====\n')
        native_text.extend(f'{source["component"]}: {source["path"]}\n' for source in item['sources'])
        native_text.append('\n' + item['text'] + '\n')
        native_index.append({'notice': index, 'sha256': digest, 'sources': item['sources']})
    (output / 'NATIVE_NOTICES.txt').write_text(''.join(native_text))
    (output / 'NATIVE_COMPONENTS.json').write_text(json.dumps(native_index, indent=2) + '\n')
    if glibc_notice is None:
        raise ValueError('Missing original glibc math copyright notice')
    (output / 'GLIBC-MATH-NOTICE.txt').write_bytes(glibc_notice)
    deno_source = work / 'extracted/deno-2.9.7'
    shutil.copy2(deno_source / 'LICENSE.md', output / 'DENO-LICENSE.md')
    for name in ('COPYRIGHT', 'LICENSE-APACHE', 'LICENSE-MIT'):
        destination = output / 'rust-standard' / name
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(work / 'extracted/rust-std-1.95.0-aarch64-apple-darwin' / name, destination)
    shutil.copy2(
        work / 'toolchain/share/doc/rust/COPYRIGHT-library.html', output / 'rust-standard/COPYRIGHT-library.html'
    )
    destination = output / 'typescript'
    destination.mkdir(exist_ok=True)
    shutil.copy2(work / 'typescript/LICENSE.txt', destination / 'LICENSE.txt')
    banner = (deno_source / 'cli/tsc/00_typescript.js').read_text().split('*/', 1)[0] + '*/\n'
    (destination / 'NOTICE.txt').write_text(banner)
    for path in deno_source.rglob('*'):
        relative = path.relative_to(deno_source)
        if path.is_file() and notice_name(PurePosixPath(str(relative))) and relative.parts[0] not in {'tests', '.git'}:
            target = output / 'deno-original' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    shutil.copy2(HERE / 'DENO-SOURCE-README.md', output / 'SOURCE-REBUILD.md')
    provenance = {
        'schema_version': 1,
        'runtime': {
            'name': 'deno',
            'version': '2.9.7',
            'target': 'aarch64-apple-darwin',
            'sha256': 'b73737579d5a84c160e3316487594783fa5c15f4e13252a6a07050b755317f1a',
        },
        'complete': complete,
        'generator': {'cargo_about': '0.9.2', 'rust_toolchain': '1.95.0', 'target': 'aarch64-apple-darwin'},
        'production_rust_crates': len(records),
        'original_rust_license_texts': len(data['licenses']),
        'native_original_notice_texts': len(native),
        'v8_revision': 'ac1e23989121713ca642f6650b34deff7b686896',
        'typescript': json.loads((work / 'typescript/PROVENANCE.json').read_text()),
        'coverage': 'Default V8 production Rust graph; original native notice union at exact source pins, with duplicate texts deduplicated. Source union does not assert every optional native file is linked.',
        'source_reconstruction_group': 'deno-reconstruction',
        'files': {
            str(path.relative_to(output)): sha256(path)
            for path in sorted(output.rglob('*'))
            if path.is_file() and path.name != 'PROVENANCE.json'
        },
    }
    for path in output.rglob('*'):
        if path.is_file() and b'/Users/' in path.read_bytes():
            raise ValueError(f'Local build path in public notice: {path.name}')
    (output / 'PROVENANCE.json').write_text(json.dumps(provenance, indent=2) + '\n')
    print(f'Rendered {len(records)} Rust crate records and {len(native)} original native notice texts.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--complete', action='store_true')
    arguments = parser.parse_args()
    generate(arguments.work, arguments.downloads, arguments.output, arguments.complete)
