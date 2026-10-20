"""Preferences keep their last saved values when persistence or old data is invalid."""

import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from audiolab import settings
from audiolab.api import create_app
from audiolab.settings import SETTINGS


@pytest.mark.parametrize('failure_point', ['write', 'replace'])
def test_failed_save_keeps_active_and_persisted_preferences(monkeypatch, failure_point):
    SETTINGS.update({'keep_originals': True, 'theme': 'auto'})
    original = SETTINGS.settings_file.read_bytes()

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'synthetic full disk')

    with monkeypatch.context() as failing:
        if failure_point == 'write':
            failing.setattr(type(SETTINGS.settings_file), 'write_text', fail)
        else:
            failing.setattr(settings, 'replace_file', fail)
        # No lifespan: no worker, AI import or OS behavior is needed to check the route.
        client = TestClient(create_app(), base_url='http://localhost')
        response = client.put('/api/settings', json={'keep_originals': False, 'theme': 'dark'})
        assert response.status_code == 500
        assert response.json()['detail'] == 'The disk is full.'
        assert client.get('/api/settings').json()['keep_originals'] is True
        assert SETTINGS.get().theme == 'auto'
        assert SETTINGS.settings_file.read_bytes() == original
        client.close()
    SETTINGS.update({'theme': 'dark'})  # a rejected save must not poison later saves
    assert SETTINGS.get().keep_originals is True
    assert SETTINGS.get().theme == 'dark'
    SETTINGS._settings = None
    assert SETTINGS.get().theme == 'dark'


@pytest.mark.parametrize('payload', ['[]', 'null', '"text"', '1', '{invalid', b'\xff'])
def test_malformed_settings_fall_back_without_overwriting_file(payload):
    path = SETTINGS.settings_file
    raw = payload.encode() if isinstance(payload, str) else payload
    path.write_bytes(raw)
    SETTINGS._settings = None
    assert SETTINGS.get().theme == 'auto'
    assert SETTINGS.public()['enhance_formats'] == ['mp3_320', 'wav16']
    assert path.read_bytes() == raw


@pytest.mark.parametrize(
    'folder, expansion_fails',
    [('relative/path', False), ('~audiolab-missing-user-for-test/music', True)],
    ids=['relative-folder', 'unexpandable-folder'],
)
def test_invalid_saved_fields_do_not_discard_valid_preferences(folder, expansion_fails, monkeypatch):
    if expansion_fails:
        # Windows guesses a sibling profile path for ~user, even if no such user
        # exists. Exercise an expansion failure explicitly on every platform.
        original_expanduser = settings.Path.expanduser
        unexpandable = settings.Path(folder)

        def expanduser(path):
            if path == unexpandable:
                raise RuntimeError('Synthetic unknown user')
            return original_expanduser(path)

        monkeypatch.setattr(settings.Path, 'expanduser', expanduser)
    saved = {
        'theme': 'dark',
        'keep_originals': 'false',
        'enhance_formats': None,
        'download_format': 'unknown',
        'output_dir': folder,
        'unknown_future_setting': {'ignored': True},
    }
    SETTINGS.settings_file.write_text(json.dumps(saved))
    SETTINGS._settings = None
    current = SETTINGS.get()
    assert current.theme == 'dark' and current.keep_originals is False
    assert current.enhance_formats == ('mp3_320', 'wav16')
    assert current.download_format == 'mp3' and current.output_dir == ''
    assert json.loads(SETTINGS.settings_file.read_text()) == saved


def test_concurrent_patches_build_on_latest_persisted_snapshot(monkeypatch):
    SETTINGS.update({'theme': 'auto', 'keep_originals': True})
    ready = threading.Barrier(2, timeout=5)
    get = SETTINGS.get

    def overlapping_get():
        snapshot = get()
        ready.wait()
        return snapshot

    # Both writers observe the same initial snapshot before acquiring the update lock.
    with monkeypatch.context() as overlapping:
        overlapping.setattr(SETTINGS, 'get', overlapping_get)
        with ThreadPoolExecutor(max_workers=2) as pool:
            theme = pool.submit(SETTINGS.update, {'theme': 'dark'})
            originals = pool.submit(SETTINGS.update, {'keep_originals': False})
            theme.result(timeout=5)
            originals.result(timeout=5)
    assert SETTINGS.get().theme == 'dark'
    assert SETTINGS.get().keep_originals is False
    SETTINGS._settings = None
    assert SETTINGS.get().theme == 'dark'
    assert SETTINGS.get().keep_originals is False
