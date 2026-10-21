"""Reuse only current, verified source bytes from a prior release companion."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeder(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / 'packaging/macos/seed_sources.py'
    module = load_helper('audiolab_source_seed', path)
    manifest = tmp_path / 'sources.json'
    manifest.write_text(
        json.dumps(
            {
                'artifacts': [
                    {
                        'filename': 'original.tar.gz',
                        'sha256': hashlib.sha256(b'current source').hexdigest(),
                        'url': 'https://sources.example.test/original.tar.gz',
                        'groups': ['test'],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(module, 'MANIFEST', manifest)
    return module


def companion(path, entries, kind=tarfile.REGTYPE):
    with tarfile.open(path, 'w:gz') as archive:
        for name, body in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = body.decode()
            elif member.isfile():
                member.size = len(body)
            archive.addfile(member, io.BytesIO(body) if member.isfile() else None)
    return path


def test_matching_source_is_seeded_but_archived_helpers_and_unknown_sources_are_ignored(seeder, tmp_path):
    archive = companion(
        tmp_path / 'prior.tar.gz',
        [
            ('sources/original.tar.gz', b'current source'),
            ('sources/retired.tar.gz', b'no longer selected'),
            ('packaging/macos/third_party.py', b'never execute archived code'),
            ('packaging/macos/sources.json', b'never adopt the old manifest'),
        ],
    )
    output = tmp_path / 'downloads'
    assert seeder.seed_sources(archive, output) == {'seeded': 1, 'existing': 0, 'changed': 0, 'missing': 0}
    assert {path.name for path in output.iterdir()} == {'original.tar.gz'}
    assert (output / 'original.tar.gz').read_bytes() == b'current source'


def test_hash_mismatch_cannot_be_seeded_even_if_archived_manifest_accepts_it(seeder, tmp_path):
    old_manifest = {'artifacts': [{'filename': 'original.tar.gz', 'sha256': hashlib.sha256(b'old source').hexdigest()}]}
    archive = companion(
        tmp_path / 'prior.tar.gz',
        [
            ('sources/original.tar.gz', b'old source'),
            ('packaging/macos/sources.json', json.dumps(old_manifest).encode()),
        ],
    )
    output = tmp_path / 'downloads'
    assert seeder.seed_sources(archive, output) == {'seeded': 0, 'existing': 0, 'changed': 1, 'missing': 0}
    assert list(output.iterdir()) == []


def test_normal_fetch_acquires_new_and_changed_sources_and_rechecks_the_seed(seeder, monkeypatch, tmp_path):
    bodies = {'original.tar.gz': b'current source', 'changed.tar.gz': b'new pin', 'added.tar.gz': b'new source'}
    manifest = json.loads(seeder.MANIFEST.read_text())
    original = manifest['artifacts'][0]
    for filename in ('changed.tar.gz', 'added.tar.gz'):
        manifest['artifacts'].append(
            dict(
                original,
                filename=filename,
                sha256=hashlib.sha256(bodies[filename]).hexdigest(),
                url=f'https://sources.example.test/{filename}',
            )
        )
    seeder.MANIFEST.write_text(json.dumps(manifest))
    archive = companion(
        tmp_path / 'prior.tar.gz',
        [('sources/original.tar.gz', bodies['original.tar.gz']), ('sources/changed.tar.gz', b'old pin')],
    )
    output = tmp_path / 'downloads'
    assert seeder.seed_sources(archive, output) == {'seeded': 1, 'existing': 0, 'changed': 1, 'missing': 1}
    assert not (output / 'changed.tar.gz').exists() and not (output / 'added.tar.gz').exists()

    path = Path(__file__).resolve().parents[1] / 'packaging/macos/third_party.py'
    monkeypatch.syspath_prepend(str(path.parent))
    fetcher = load_helper('audiolab_source_seed_fetch', path)
    monkeypatch.setattr(fetcher, 'MANIFEST', seeder.MANIFEST)
    # Acquisition's pinned builder uses 3.12; keep this fixture usable on repo Python 3.10.
    monkeypatch.setattr(fetcher, 'digest', seeder.digest)
    requests = []

    def source_response(request, timeout):
        filename = request.full_url.rsplit('/', 1)[-1]
        requests.append(filename)
        return io.BytesIO(bodies[filename])

    monkeypatch.setattr(fetcher.urllib.request, 'urlopen', source_response)
    fetcher.fetch('test', output)
    assert set(requests) == {'changed.tar.gz', 'added.tar.gz'} and len(requests) == 2
    assert {path.name: path.read_bytes() for path in output.iterdir()} == bodies
    (output / 'original.tar.gz').write_bytes(b'corrupt after seeding')
    with pytest.raises(RuntimeError, match='Source checksum failed'):
        fetcher.fetch('test', output)


@pytest.mark.parametrize('kind', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_selected_members_must_be_regular_files(seeder, tmp_path, kind):
    archive = companion(tmp_path / 'prior.tar.gz', [('sources/original.tar.gz', b'outside')], kind)
    output = tmp_path / 'downloads'
    with pytest.raises(ValueError, match='must be a regular file'):
        seeder.seed_sources(archive, output)
    assert not output.exists()


def test_duplicate_selected_member_is_rejected_before_writing(seeder, tmp_path):
    archive = companion(
        tmp_path / 'prior.tar.gz',
        [('sources/original.tar.gz', b'current source'), ('sources/original.tar.gz', b'current source')],
    )
    output = tmp_path / 'downloads'
    with pytest.raises(ValueError, match='Duplicate selected source'):
        seeder.seed_sources(archive, output)
    assert not output.exists()


@pytest.mark.parametrize(
    'name',
    [
        '../escape',
        '/absolute',
        'sources/../escape',
        'sources//original.tar.gz',
        'sources/./original.tar.gz',
        'sources\\original.tar.gz',
        'C:/escape',
    ],
)
def test_malicious_archive_paths_cannot_escape_or_seed_a_normalized_alias(seeder, tmp_path, name):
    archive = companion(
        tmp_path / 'prior.tar.gz', [('sources/original.tar.gz', b'current source'), (name, b'current source')]
    )
    output = tmp_path / 'downloads'
    with pytest.raises(ValueError, match='Unsafe source companion path'):
        seeder.seed_sources(archive, output)
    assert not output.exists() and not (tmp_path / 'escape').exists()


def test_valid_existing_source_is_verified_and_preserved(seeder, tmp_path):
    archive = companion(tmp_path / 'prior.tar.gz', [('sources/original.tar.gz', b'current source')])
    output = tmp_path / 'downloads'
    output.mkdir()
    target = output / 'original.tar.gz'
    target.write_bytes(b'current source')
    before = target.stat()
    assert seeder.seed_sources(archive, output) == {'seeded': 0, 'existing': 1, 'changed': 0, 'missing': 0}
    assert target.stat().st_ino == before.st_ino and target.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize('kind', ['corrupt', 'symlink', 'directory'])
def test_unexpected_existing_target_is_never_overwritten(seeder, tmp_path, kind):
    archive = companion(tmp_path / 'prior.tar.gz', [('sources/original.tar.gz', b'current source')])
    output = tmp_path / 'downloads'
    output.mkdir()
    target = output / 'original.tar.gz'
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside data')
    if kind == 'corrupt':
        target.write_bytes(b'existing unexpected data')
    elif kind == 'symlink':
        target.symlink_to(outside)
    else:
        target.mkdir()
    with pytest.raises(FileExistsError, match='Refusing to overwrite'):
        seeder.seed_sources(archive, output)
    assert outside.read_bytes() == b'outside data'
    if kind == 'corrupt':
        assert target.read_bytes() == b'existing unexpected data'
    elif kind == 'symlink':
        assert target.is_symlink()
    else:
        assert target.is_dir()


def test_destination_symlink_is_rejected(seeder, tmp_path):
    archive = companion(tmp_path / 'prior.tar.gz', [('sources/original.tar.gz', b'current source')])
    outside = tmp_path / 'outside'
    outside.mkdir()
    output = tmp_path / 'downloads'
    output.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='must not be a symlink'):
        seeder.seed_sources(archive, output)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('bound', ['size', 'members'])
def test_companion_limits_are_checked_before_copying(seeder, monkeypatch, tmp_path, bound):
    archive = companion(tmp_path / 'prior.tar.gz', [('sources/original.tar.gz', b'current source')])
    monkeypatch.setattr(seeder, 'MAX_SOURCE_BYTES' if bound == 'size' else 'MAX_MEMBERS', 0)
    output = tmp_path / 'downloads'
    with pytest.raises(ValueError, match='size bound|too many members'):
        seeder.seed_sources(archive, output)
    assert not output.exists()
