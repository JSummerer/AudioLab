"""The desktop picker, HTTP capability scope, and private job references without opening a window."""

import json
import os
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from audiolab import library, system, tasks
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


def finished_file(path, task='download'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'original audio')
    mgr = JobManager(runner=lambda *_: {}, stages_for=lambda *_: [])
    job = mgr.submit(task, {'kind': 'file', 'name': path.name, 'path': str(path)}, {})
    job.result = {'output_dir': str(path.parent), 'files': [{'id': 'f0', 'path': str(path)}]}
    assert library.record(job)
    return mgr, job


def test_locate_updates_library_only_and_protects_result_outside_output_root(native, tmp_path, monkeypatch):
    old = tmp_path / 'out' / 'Recording.wav'
    mgr, job = finished_file(old)
    new = old.rename(tmp_path / 'Moved.wav')
    copied = tmp_path / 'Copy.wav'
    shutil.copy2(new, copied)
    link = tmp_path / 'Alias.wav'
    link.symlink_to(new)
    trashed = []
    monkeypatch.setattr(library.trash, 'move_to_trash', trashed.append)
    revealed = Mock()
    monkeypatch.setattr(system, 'reveal', revealed)
    headers = desktop_headers(native)
    client = window_client(native, manager=mgr)
    entry_url = f'/api/library/{job.id}'
    media_url = f'{entry_url}/files/f0'
    endpoint = f'{media_url}/locate'
    try:
        missing = client.get(entry_url).json()['files'][0]
        assert not missing['exists'] and missing['locatable'] and '_identity' not in missing
        assert client.post(endpoint, json={'path': str(new)}, headers=headers).status_code == 400
        key = pick(native, 'locate', copied)['files'][0]['selection']
        assert client.post(endpoint, json={'native_file': key}, headers=headers).status_code == 400
        key = pick(native, 'files', new)['files'][0]['selection']
        assert client.post(endpoint, json={'native_file': key}, headers=headers).status_code == 410
        key = pick(native, 'locate', link)['files'][0]['selection']
        assert client.post(endpoint, json={'native_file': key}).status_code == 403

        old.write_bytes(b'occupied')
        body = {'native_file': key}
        assert client.post(endpoint, json=body, headers=headers).status_code == 400
        assert client.get(entry_url).json()['files'][0]['path'] == old.name
        old.unlink()

        fixed = client.post(endpoint, json=body, headers=headers)
        assert fixed.status_code == 200, fixed.text
        file = fixed.json()['files'][0]
        assert file['name'] == new.name and file['path'] == str(new) and file['exists'] and not file['can_trash']
        assert client.get(media_url).content == b'original audio'
        assert client.post(f'{entry_url}/reveal').json() == {'ok': True}
        revealed.assert_called_once_with(new.parent)
        assert file['located'] and library.folder(library.get(job.id)) == old.parent

        second = old.with_name('second.wav')
        second.write_bytes(b'other audio')
        assert library.add_file(job.id, {'id': 'f1', 'path': str(second)})
        assert client.post(f'{entry_url}/reveal').json() == {'ok': True}
        assert revealed.call_args.args == (old.parent,)
        assert client.post(f'{media_url}/reveal').json() == {'ok': True}
        assert revealed.call_args.args == (new.parent,)
        second.unlink()
        assert job.result['files'][0]['path'] == str(old)
        assert client.post(endpoint, json=body, headers=headers).status_code == 400

        removed = client.delete(entry_url, params={'trash_files': True}).json()
        assert removed['trashed'] == [] and new.is_file() and trashed == []
        assert client.post(endpoint, json=body, headers=headers).status_code == 404
    finally:
        client.close()


def test_located_file_inside_root_can_be_trashed_but_replacement_cannot(tmp_path, monkeypatch):
    first = tmp_path / 'out' / 'First.wav'
    second = first.with_name('Second.wav')
    _, first_job = finished_file(first)
    _, second_job = finished_file(second)
    first_moved = first.rename(first.with_name('First new.wav'))
    second_moved = second.rename(second.with_name('Second new.wav'))
    trashed = []
    monkeypatch.setattr(library.trash, 'move_to_trash', trashed.append)

    assert library.locate_file(first_job.id, 'f0', first_moved)
    assert library.locate_file(second_job.id, 'f0', second_moved)
    first_moved.rename(first.with_name('Kept.wav'))
    first_moved.write_bytes(b'unrelated replacement')

    entry = library.get(first_job.id)
    assert not library.public(entry)['files'][0]['exists']
    assert library.remove(first_job.id, trash_files=True)['trash_failed'] == [first_moved.name]
    assert library.remove(second_job.id, trash_files=True)['trashed'] == [second_moved.name]
    assert trashed == [second_moved]


def test_locate_refuses_legacy_entry_without_independent_file_identity(tmp_path):
    old = tmp_path / 'out' / 'Master.wav'
    _, job = finished_file(old, task='import')
    entry = library.get(job.id)
    # A group may be an imported file's content key, or it may have been supplied
    # when adding a version of some other file. It does not identify the old import.
    assert entry['group'].startswith('file:')
    entry['files'][0].pop('_identity')
    library._write(entry)
    candidate = old.rename(old.with_name('Master new.wav'))

    assert not library.public(entry)['files'][0]['locatable']
    with pytest.raises(AudioLabError, match='no saved identity') as exc:
        library.locate_file(job.id, 'f0', candidate)
    assert exc.value.hints == ['Add the file to the Library again.']


def rewrite_in_place(path, data):
    original = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000))
    assert path.stat().st_ino == original.st_ino


def test_native_import_checks_the_same_file_during_queue_and_publication(native, tmp_path, monkeypatch):
    audio = tmp_path / 'Master.wav'
    audio.write_bytes(b'first recording')
    client = window_client(native)
    monkeypatch.setattr(ffmpeg, 'probe_media', lambda _: {'has_audio': True})

    def enqueue():
        selection = pick(native, 'import', audio)['files'][0]['selection']
        response = client.post('/api/library/import', json={'native_file': selection}, headers=desktop_headers(native))
        assert response.status_code == 200, response.text
        assert '_native_identity' not in response.json()['source']
        return client.app.state.audiolab.manager.get(response.json()['id'])

    try:
        job = enqueue()
        assert len(job.source['_native_identity']) == 4

        rewrite_in_place(audio, b'other recording')
        with monkeypatch.context() as mac:
            mac.setattr(tasks, 'get', Mock(side_effect=AssertionError('Do not run a task for a changed file')))
            with pytest.raises(AudioLabError, match='changed or was replaced'):
                tasks.run_job(job, None)

        job = enqueue()
        fake_result = {'files': [{'id': 'f0', 'path': str(audio)}]}
        with monkeypatch.context() as mac:

            def run(_ctx, _source):
                rewrite_in_place(audio, b'after recording')
                return fake_result

            mac.setattr(tasks, 'get', lambda _: SimpleNamespace(run=run))
            with pytest.raises(AudioLabError, match='changed or was replaced'):
                tasks.run_job(job, None)

        job = enqueue()
        job.result = fake_result
        with pytest.raises(AudioLabError, match='changed or was replaced'):
            library.record(job, begin_commit=lambda: rewrite_in_place(audio, b'final recording'))
        assert library.get(job.id) is None
    finally:
        client.close()


def test_native_import_through_jobs_and_library_refuses_a_later_replacement(native, tmp_path, monkeypatch):
    audio = tmp_path / 'Master.wav'
    audio.write_bytes(b'first recording')
    selection = pick(native, 'import', audio)['files'][0]['selection']
    mgr = JobManager(runner=lambda *_: {}, stages_for=lambda *_: [])
    job = mgr.submit('import', native.get(selection, 'import').source(), {})
    job.result = {'files': [{'id': 'f0', 'path': str(audio)}]}
    assert library.record(job)
    monkeypatch.setattr(library.trash, 'move_to_trash', Mock())
    client = window_client(native, manager=mgr)
    try:
        assert client.get(f'/api/library/{job.id}/files/f0').content == b'first recording'
        assert client.get(f'/api/jobs/{job.id}/files/f0').content == b'first recording'
        rewrite_in_place(audio, b'other recording')
        assert client.get(f'/api/library/{job.id}/files/f0').status_code == 410
        assert client.get(f'/api/jobs/{job.id}/files/f0').status_code == 410
        assert (
            client.post('/api/jobs', data={'task': 'analyze', 'source_job': job.id, 'source_file': 'f0'}).status_code
            == 410
        )
        assert library.remove(job.id, trash_files=True)['trashed'] == []
        library.trash.move_to_trash.assert_not_called()
        assert audio.read_bytes() == b'other recording'
    finally:
        client.close()


def test_native_kept_original_validates_source_staging_and_existing_copy(native, tmp_path, monkeypatch):
    picked = tmp_path / 'Source.wav'
    picked.write_bytes(b'first recording')
    result = tmp_path / 'Result.wav'
    result.write_bytes(b'result audio')
    mgr = JobManager(runner=lambda *_: {}, stages_for=lambda *_: [])

    def finished():
        selected = pick(native, 'files', picked)['files'][0]['selection']
        job = mgr.submit('enhance', native.get(selected, 'job').source(), {})
        job.result = {'files': [{'id': 'f0', 'path': str(result)}], '_library': {'original': str(picked)}}
        return job

    job = finished()
    with pytest.raises(AudioLabError, match='changed or was replaced'):
        library.record(job, begin_commit=lambda: rewrite_in_place(picked, b'other recording'))
    assert library.get(job.id) is None
    assert not list(library.originals_dir().iterdir())

    job = finished()
    copy = library.link_or_copy
    with monkeypatch.context() as mac:

        def corrupt(*args, **kwargs):
            staged = copy(*args, **kwargs)
            staged.write_bytes(b'first recording')  # same byte count, different audio
            return staged

        mac.setattr(library, 'link_or_copy', corrupt)
        with pytest.raises(AudioLabError, match='could not verify'):
            library.record(job)
    assert library.get(job.id) is None
    assert not list(library.originals_dir().iterdir())

    assert library.record(job)
    kept = library.file_path(library.get(job.id), 'original')
    later = finished()
    assert library.record(later)
    assert library.file_path(library.get(later.id), 'original') == kept
    replacement = finished()
    kept.write_bytes(b'first recording')
    with pytest.raises(AudioLabError, match='could not verify'):
        library.record(replacement)
    assert library.get(replacement.id) is None


def test_older_two_field_native_fingerprint_requires_a_new_selection(tmp_path):
    path = tmp_path / 'Old.wav'
    path.write_bytes(b'original')
    st = path.stat()
    with pytest.raises(AudioLabError, match='verified again'):
        source_path({'path': str(path), '_native_identity': [st.st_dev, st.st_ino]})


def test_duration_reads_one_explicitly_selected_file_with_a_bounded_header_probe(native, tmp_path, monkeypatch):
    path = tmp_path / 'Long.wav'
    sample_rate = 8000
    size = 620 * sample_rate * 2
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + size,
        b'WAVE',
        b'fmt ',
        16,
        1,
        1,
        sample_rate,
        2 * sample_rate,
        2,
        16,
        b'data',
        size,
    )
    with path.open('wb') as f:
        f.write(header)
        f.truncate(len(header) + size)
    probe = Mock(wraps=ffmpeg.probe_media)
    monkeypatch.setattr(ffmpeg, 'probe_media', probe)
    bridge = native.bridge()
    folder_items = pick(native, 'batch', tmp_path)['files']
    assert not folder_items[0].get('can_measure_duration')
    assert bridge.duration(native.secret, folder_items[0]['selection']) is None
    probe.assert_not_called()

    picked = pick(native, 'files', path)['files'][0]
    assert picked['can_measure_duration']
    selection = picked['selection']
    assert bridge.duration('wrong secret', selection) is None
    assert bridge.duration(native.secret, str(path)) is None
    probe.assert_not_called()
    assert bridge.duration(native.secret, selection) == pytest.approx(620)
    probe.assert_called_once_with(path, header_only=True)
    assert bridge.duration(native.secret, selection) is None
    assert probe.call_count == 1

    picked = pick(native, 'files', path)['files'][0]['selection']
    rewrite_in_place(path, b'changed')
    assert bridge.duration(native.secret, picked) is None
    assert probe.call_count == 1
