"""Published helper reuse with tiny assets, exact source provenance and fake transport."""

import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = json.loads((ROOT / 'packaging/ai/published-helper.json').read_text())


@pytest.fixture
def package(tmp_path):
    spec = importlib.util.spec_from_file_location('audiolab_ai_reuse', ROOT / 'packaging/ai/reuse.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root, assets = tmp_path / 'checkout', tmp_path / 'published'
    assets.mkdir()
    for name in PUBLISHED['inputs']:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    pin = copy.deepcopy(PUBLISHED)
    # This is a synthetic release of the copied test tree, independent of future real helper updates.
    pin['inputs'] = {name: module.input_digest(name, (root / name).read_bytes()) for name in pin['inputs']}
    runtime = pin['catalog']['url'].rsplit('/', 1)[1]
    (assets / runtime).write_bytes(b'tiny immutable helper archive')
    pin['catalog'].update(
        sha256=hashlib.sha256((assets / runtime).read_bytes()).hexdigest(),
        download_bytes=(assets / runtime).stat().st_size,
    )
    (assets / 'ai-catalog.json').write_text(json.dumps(pin['catalog'], indent=2) + '\n')
    with tarfile.open(assets / pin['source_companion'], 'w:gz') as archive:
        for name in pin['inputs']:
            archive.add(root / name, arcname=module.source_member(name))
    for name in pin['assets']:
        content = (assets / name).read_bytes()
        pin['assets'][name] = {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
    pin_path = tmp_path / 'pin.json'
    pin_path.write_text(json.dumps(pin))
    return module, root, assets, pin, pin_path


def test_reuse_keeps_exact_catalog_archive_and_source_bytes_without_retagging(package, monkeypatch, tmp_path):
    module, root, assets, pin, pin_path = package
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *_args, **_kwargs: pytest.fail('unexpected download'))
    archive = module.reuse(tmp_path / 'work', assets, root=root, pin_path=pin_path)
    assert archive.read_bytes() == (assets / archive.name).read_bytes()
    assert (archive.parent / 'catalog.json').read_bytes() == (assets / 'ai-catalog.json').read_bytes()
    assert (archive.parent / pin['source_companion']).read_bytes() == (assets / pin['source_companion']).read_bytes()
    assert json.loads((archive.parent / 'catalog.json').read_text())['url'] == PUBLISHED['catalog']['url']
    module.reuse(tmp_path / 'work', root=root, pin_path=pin_path)  # checked work cache also needs no network


@pytest.mark.parametrize('name', PUBLISHED['inputs'])
def test_changed_execution_contract_or_build_input_refuses_reuse_before_download(package, monkeypatch, tmp_path, name):
    module, root, _assets, _pin, pin_path = package
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *_args, **_kwargs: pytest.fail('unexpected download'))
    with (root / name).open('ab') as output:
        output.write(b'\n# changed helper input\n')
    with pytest.raises(ValueError, match='Intentionally build and review a new AI version'):
        module.reuse(tmp_path / 'work', root=root, pin_path=pin_path)
    assert not (tmp_path / 'work').exists()


def test_only_literal_app_version_is_ignored_but_new_imports_and_brand_changes_are_not(package):
    module, root, _assets, pin, _pin_path = package
    constants = root / 'audiolab/__init__.py'
    original = constants.read_text()
    version_line = next(line for line in original.splitlines() if line.startswith('__version__ = '))
    constants.write_text(original.replace(version_line, "__version__ = '2.4.0b999'"))
    assert constants.read_text() != original
    module.check_inputs(root, pin)
    brand_line = next(line for line in original.splitlines() if line.startswith('APP_NAME = '))
    constants.write_text(constants.read_text().replace(brand_line, "APP_NAME = 'Other fixture brand'"))
    with pytest.raises(ValueError, match='audiolab/__init__.py'):
        module.check_inputs(root, pin)
    constants.write_text(original + '\nfrom .another_module import new_worker_dependency\n')
    with pytest.raises(ValueError, match='audiolab/__init__.py'):
        module.check_inputs(root, pin)


def test_core_installer_and_web_changes_do_not_enter_worker_execution_guard(package):
    module, root, _assets, pin, _pin_path = package
    assert 'audiolab/ai/managed.py' not in pin['inputs']
    (root / 'audiolab/ai/managed.py').write_text('changed core installer, not executed by the frozen worker')
    web = root / 'audiolab/web'
    web.mkdir()
    (web / 'index.html').write_text('new core interface')
    module.check_inputs(root, pin)


def test_text_input_contract_accepts_crlf_checkout_without_changing_asset_identity(package):
    module, root, _assets, pin, _pin_path = package
    for name in pin['inputs']:
        path = root / name
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    module.check_inputs(root, pin)


@pytest.mark.parametrize('asset', PUBLISHED['assets'])
@pytest.mark.parametrize('corruption', ['size', 'checksum'])
def test_wrong_published_asset_is_rejected(package, tmp_path, asset, corruption):
    module, root, assets, _pin, pin_path = package
    target = assets / asset
    target.write_bytes(b'x' * target.stat().st_size if corruption == 'checksum' else b'bad')
    with pytest.raises(ValueError, match='size or type|checksum'):
        module.reuse(tmp_path / 'work', assets, root=root, pin_path=pin_path)


def test_missing_input_or_tampered_baseline_cannot_be_adopted(package):
    module, root, assets, pin, _pin_path = package
    path = root / 'audiolab/ai/worker.py'
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(ValueError, match='worker.py'):
        module.check_inputs(root, pin)
    path.write_bytes(original + b'\n# new worker implementation\n')
    pin['inputs']['audiolab/ai/worker.py'] = module.input_digest('audiolab/ai/worker.py', path.read_bytes())
    module.check_inputs(root, pin)
    with pytest.raises(ValueError, match='source companion differs'):
        module.check_companion(assets / pin['source_companion'], pin)


def test_downloads_retry_same_published_urls_and_keep_original_bytes(package, monkeypatch, tmp_path):
    module, root, assets, pin, pin_path = package
    requests, sleeps = [], []

    def download(request, timeout):
        requests.append((request.full_url, timeout))
        if len(requests) == 1:
            raise urllib.error.URLError(TimeoutError('temporary timeout'))
        return io.BytesIO((assets / request.full_url.rsplit('/', 1)[1]).read_bytes())

    monkeypatch.setattr(module.urllib.request, 'urlopen', download)
    monkeypatch.setattr(module.time, 'sleep', sleeps.append)
    archive = module.reuse(tmp_path / 'work', root=root, pin_path=pin_path)
    base = pin['catalog']['url'].rsplit('/', 1)[0]
    assert requests == [(f'{base}/ai-catalog.json', 120)] + [(f'{base}/{name}', 120) for name in pin['assets']]
    assert sleeps == [1]
    assert archive.read_bytes() == (assets / archive.name).read_bytes()
    assert not list(archive.parent.glob('*.partial'))


@pytest.mark.parametrize('error_code,expected_calls', [(503, 3), (404, 1)])
def test_download_failures_have_bounded_retry_and_no_partial_asset(
    package, monkeypatch, tmp_path, error_code, expected_calls
):
    module, root, _assets, _pin, pin_path = package
    calls = []

    def fail(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, error_code, 'fixture failure', {}, None)

    monkeypatch.setattr(module.urllib.request, 'urlopen', fail)
    monkeypatch.setattr(module.time, 'sleep', lambda _delay: None)
    with pytest.raises(RuntimeError, match='Could not fetch pinned AI asset'):
        module.reuse(tmp_path / 'work', root=root, pin_path=pin_path)
    assert len(calls) == expected_calls
    assert not list((tmp_path / 'work/release').iterdir())


@pytest.mark.parametrize('version', [None, PUBLISHED['catalog']['version']])
def test_helper_rebuild_requires_an_explicit_unpublished_version(version):
    bash = shutil.which('bash')
    if not bash or os.name == 'nt':
        pytest.skip('The macOS maintainer shell script needs a POSIX shell environment.')
    env = dict(os.environ, AUDIOLAB_BUILD_PYTHON=sys.executable)
    env.pop('AUDIOLAB_AI_VERSION', None)
    if version is not None:
        env['AUDIOLAB_AI_VERSION'] = version
    result = subprocess.run([bash, str(ROOT / 'packaging/ai/build.sh')], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert ('intentional new helper version' if version is None else 'already published and immutable') in result.stderr
