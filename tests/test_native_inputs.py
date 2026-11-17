"""The desktop picker, HTTP capability scope, and private job references without opening a window."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from audiolab import library
from audiolab.api import create_app
from audiolab.errors import AudioLabError
from audiolab.jobs import JobManager
from audiolab.media import ffmpeg
from audiolab.native_inputs import MAX_FILES, NativeInputs, source_path

ORIGIN = 'http://127.0.0.1:19999'


class PickerWindow:
    def __init__(self):
        self.url = ORIGIN + '/'
        self.picked = None
        self.navigating_to = None
        self.calls = []

    def get_current_url(self):
        return self.url

    def create_file_dialog(self, kind, **options):
        self.calls.append((kind, options))
        if self.navigating_to:
            self.url = self.navigating_to
        return self.picked


@pytest.fixture
def native():
    adapter = NativeInputs(ORIGIN, SimpleNamespace(FileDialog=SimpleNamespace(OPEN=10, FOLDER=20)))
    adapter.window = PickerWindow()
    return adapter


def pick(native, kind, *paths):
    native.window.picked = tuple(str(p) for p in paths)
    return native.bridge().choose(native.secret, kind)


def test_bridge_rejects_wrong_secret_origin_and_navigation_during_dialog(native, tmp_path):
    path = tmp_path / 'Music.wav'
    path.write_bytes(b'audio')
    native.window.picked = (str(path),)
    bridge = native.bridge()

    assert bridge.choose('forged', 'files') is None
    assert bridge.choose(native.secret, str(path)) is None
    native.window.url = 'https://example.invalid/'
    assert bridge.choose(native.secret, 'files') is None
    native.window.url = ORIGIN + '/other'
    assert bridge.choose(native.secret, 'files') is None
    assert native.window.calls == []

    native.window.url = ORIGIN + '/'
    native.window.navigating_to = 'http://localhost:19999/'
    assert bridge.choose(native.secret, 'files') is None
    assert len(native.window.calls) == 1
    assert not native._selections


def test_folder_is_sorted_nonrecursive_and_capped(native, tmp_path):
    folder = tmp_path / 'music'
    folder.mkdir()
    for i in reversed(range(MAX_FILES + 2)):
        (folder / f'{i:03}.WAV').write_bytes(b'audio')
    (folder / 'notes.txt').write_text('test')
    nested = folder / 'Other album'
    nested.mkdir()
    (nested / 'inside.mp3').write_bytes(b'audio')
    (folder / 'alias.wav').symlink_to(nested / 'inside.mp3')

    picked = pick(native, 'batch', folder)

    assert native.window.calls == [(20, {'allow_multiple': False, 'file_types': ()})]
    assert picked['capped'] and picked['skipped'] == 1
    assert [item['name'] for item in picked['files']] == [f'{i:03}.WAV' for i in range(MAX_FILES)]
    assert str(folder) not in json.dumps(picked)
    again = pick(native, 'files', folder / '002.WAV', folder / '001.WAV')
    assert native.window.calls[-1][0] == 10 and native.window.calls[-1][1]['allow_multiple']
    assert again['files'][0] == picked['files'][2]  # selecting again keeps one grant for that file
    assert pick(native, 'files') is None


def window_client(native, *, local=True, peer='127.0.0.1', manager=None):
    app = create_app(local=local, manager=manager)
    app.state.native_inputs = native
    return TestClient(app, base_url=ORIGIN, client=(peer, 12345))


def desktop_headers(native):
    return {'Origin': ORIGIN, 'X-AudioLab-Desktop': native.secret}


def test_job_uses_selected_file_in_place_and_rejects_other_requests(native, tmp_path):
    path = tmp_path / 'Recording.wav'
    with path.open('wb') as f:
        f.truncate(5 * 1024**3)  # sparse; proves this route does not write/copy the payload
    key = pick(native, 'files', path)['files'][0]['selection']
    other = pick(native, 'import', path)['files'][0]['selection']
    headers = desktop_headers(native)
    form = {'task': 'analyze', 'native_file': key}

    with window_client(native, local=False) as remote:
        assert remote.post('/api/jobs', data=form, headers=headers).status_code == 403
    with window_client(native, peer='192.0.2.1') as proxy:
        assert proxy.post('/api/jobs', data=form, headers=headers).status_code == 403
    # No lifespan or worker so a large file can only be referenced and queued.
    client = window_client(native)
    manager = client.app.state.audiolab.manager
    try:
        assert client.post('/api/jobs', data=form).status_code == 403
        assert (
            client.post('/api/jobs', data=form, headers={**headers, 'Origin': 'https://example.invalid'}).status_code
            == 403
        )
        assert client.post('/api/jobs', data={**form, 'native_file': str(path)}, headers=headers).status_code == 410
        assert client.post('/api/jobs', data={**form, 'native_file': other}, headers=headers).status_code == 410
        queued = client.post('/api/jobs', data=form, headers=headers)
        assert queued.status_code == 200, queued.text
        public = queued.json()['jobs'][0]
        assert public['source']['size_bytes'] == 5 * 1024**3 and str(tmp_path) not in queued.text
        assert '_native_identity' not in public['source']
        stored = manager.get(public['id'])
        assert source_path(stored.source) == path
        assert not (manager.job_dir(stored.id) / 'input').exists()

        assert client.post(f'/api/jobs/{stored.id}/cancel').status_code == 200
        assert client.post(f'/api/jobs/{stored.id}/retry').json()['status'] == 'queued'
        clone = client.post('/api/jobs', data={'task': 'analyze', 'source_job': stored.id})
        assert clone.status_code == 200, clone.text
        clone_id = clone.json()['jobs'][0]['id']
        assert source_path(manager.get(clone_id).source) == path
        assert not (manager.job_dir(clone_id) / 'input').exists()

        assert client.post(f'/api/jobs/{stored.id}/cancel').status_code == 200
        assert client.delete(f'/api/jobs/{stored.id}').status_code == 200
        assert path.stat().st_size == 5 * 1024**3

        path.rename(tmp_path / 'Original.wav')
        path.write_bytes(b'not the selected file')
        with pytest.raises(AudioLabError, match='replaced'):
            source_path(manager.get(clone_id).source)
        assert client.post('/api/jobs', data=form, headers=headers).status_code == 400
    finally:
        client.close()


def test_import_and_settings_accept_only_their_picker_selection(native, tmp_path, monkeypatch):
    monkeypatch.delenv('AUDIOLAB_OUTPUT_DIR')
    monkeypatch.setattr(ffmpeg, 'probe_media', lambda _: {'has_audio': True})
    audio = tmp_path / 'Master.wav'
    audio.write_bytes(b'audio')
    folder = tmp_path / 'Exports'
    folder.mkdir()
    file_id = pick(native, 'import', audio)['files'][0]['selection']
    folder_id = pick(native, 'output', folder)['files'][0]['selection']
    headers = desktop_headers(native)

    client = window_client(native)
    try:
        imported = client.post('/api/library/import', json={'native_file': file_id, 'title': 'Master'}, headers=headers)
        assert imported.status_code == 200, imported.text
        assert imported.json()['task'] == 'import'
        assert client.post('/api/library/import', json={'path': str(audio)}).status_code == 200
        assert client.post('/api/library/import', json={'native_file': folder_id}, headers=headers).status_code == 410

        patch = {'native_output_folder': folder_id}
        assert client.put('/api/settings', json=patch).status_code == 403
        saved = client.put('/api/settings', json=patch, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()['output_dir'] == str(folder)
        assert 'native_output_folder' not in saved.json()
    finally:
        client.close()


def test_kept_original_of_native_job_is_an_independent_copy(native, tmp_path):
    source = tmp_path / 'Selected.wav'
    source.write_bytes(b'original audio')
    selection = pick(native, 'files', source)['files'][0]['selection']
    mgr = JobManager(runner=lambda *_: {}, stages_for=lambda *_: [])
    job = mgr.submit('enhance', native.get(selection, 'job').source(), {})
    result = tmp_path / 'Enhanced.wav'
    result.write_bytes(b'enhanced audio')
    job.result = {
        'files': [{'id': 'f0', 'path': str(result)}],
        '_library': {'original': str(source)},
    }

    assert library.record(job)
    kept = library.file_path(library.get(job.id), 'original')
    assert isinstance(kept, Path) and not kept.samefile(source)
    source.write_bytes(b'changed audio')
    assert kept.read_bytes() == b'original audio'
