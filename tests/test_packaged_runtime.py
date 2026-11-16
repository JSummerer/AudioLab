"""Frozen/source runtime boundaries, with no external requests, installers or models."""

import json
import os
import ssl
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import certifi
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from audiolab import ai, cli, runtime, system
from audiolab.ai import install
from audiolab.api import create_app
from audiolab.api.state import AppState
from audiolab.errors import AudioLabError, ToolMissingError
from audiolab.jobs import JobManager
from audiolab.media import export, ffmpeg, proc, youtube, ytdlp_update
from audiolab.settings import SETTINGS


def _packaged(monkeypatch, bundle: Path | None = None):
    monkeypatch.setattr(
        runtime,
        'sys',
        SimpleNamespace(
            frozen=True,
            executable='/Applications/Audio Lab.app/Contents/MacOS/AudioLab',
            _MEIPASS=str(bundle) if bundle else None,
        ),
    )
    monkeypatch.setattr(ai, '_CACHE', {})


def _no_process(*args, **kwargs):
    pytest.fail('A package installer or other unexpected subprocess was started')


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """Keep API tests independent of host FFmpeg and optional AI installations."""
    monkeypatch.setattr(youtube, 'ytdlp_version', lambda: '2026.08.19')
    monkeypatch.setattr(ffmpeg, 'ffmpeg_info', lambda: {'ok': True, 'source': 'bundled', 'version': '7.1'})
    monkeypatch.setattr(export, 'format_choices', lambda kind: [])
    monkeypatch.setattr(ai, '_CACHE', {})
    find_spec = ai.importlib.util.find_spec
    monkeypatch.setattr(
        ai.importlib.util, 'find_spec', lambda name: None if name in ('torch', 'demucs') else find_spec(name)
    )

    def make(local=True):
        manager = JobManager(
            runner=lambda job, ctx: {}, stages_for=lambda job: [('work', 'Working', 1)], root=tmp_path / 'jobs'
        )
        return create_app(local=local, token=None, manager=manager)

    return make


def test_source_runtime_preserves_environment_and_python_command(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    for name in ('SSL_CERT_FILE', 'DENO_DIR', 'TMPDIR'):
        monkeypatch.setenv(name, f'original-{name}')
    before = dict(os.environ)
    old_temp = tempfile.tempdir
    runtime.configure_packaged()
    assert dict(os.environ) == before and tempfile.tempdir == old_temp
    assert runtime.downloader_command() == [sys.executable, '-m', 'yt_dlp']
    assert runtime.bundled_tool('ffmpeg') is None
    assert not (tmp_path / 'data').exists() and not (tmp_path / 'out').exists()


def test_packaged_tools_are_executable_files_in_the_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / 'App With Spaces' / 'Contents' / 'Frameworks'
    tools = bundle / 'tools'
    tools.mkdir(parents=True)
    deno = tools / 'deno'
    deno.write_text('#!/bin/sh\nexit 0\n')
    deno.chmod(0o755)
    _packaged(monkeypatch, bundle)
    assert runtime.downloader_command() == [runtime.sys.executable, '--audiolab-downloader']
    assert runtime.bundled_tool('deno') == deno
    assert runtime.bundled_tool('ffmpeg') is None
    with pytest.raises(ValueError, match='Unknown bundled tool'):
        runtime.bundled_tool('../deno')


def test_packaged_configuration_uses_app_cache_and_bundled_certificates(tmp_path, monkeypatch):
    _packaged(monkeypatch)
    for name in ('SSL_CERT_FILE', 'DENO_DIR', 'TMPDIR'):
        monkeypatch.setenv(name, 'previous')
    monkeypatch.setattr(tempfile, 'tempdir', None)  # undo configure_packaged's assignment after this test
    runtime.configure_packaged()
    data = tmp_path / 'data'
    assert os.environ['SSL_CERT_FILE'] == certifi.where()
    assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0
    assert Path(os.environ['DENO_DIR']) == data / 'cache' / 'deno'
    assert Path(tempfile.gettempdir()) == data / 'cache' / 'tmp'
    assert not (tmp_path / 'out').exists()
    marker = data / 'cache' / 'deno' / 'kept'
    marker.write_text('cache')
    runtime.configure_packaged()
    assert marker.read_text() == 'cache'


def test_packaged_configuration_rejects_missing_certificates(tmp_path, monkeypatch):
    _packaged(monkeypatch)
    monkeypatch.setattr(certifi, 'where', lambda: str(tmp_path / 'missing.pem'))
    with pytest.raises(RuntimeError, match='missing its security certificates'):
        runtime.configure_packaged()
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('packaged', [False, True])
def test_downloader_command_uses_the_selected_runtime_and_app_cache(tmp_path, monkeypatch, packaged):
    if packaged:
        _packaged(monkeypatch)
    else:
        monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    monkeypatch.setattr(youtube, 'ffmpeg_dir', lambda: '/selected ffmpeg')
    monkeypatch.setattr(youtube, '_deno_path', lambda: '/selected deno')
    SETTINGS.get().cookies_browser = 'firefox'
    command = youtube.ytdlp_cmd(sign_in=True)
    prefix = runtime.downloader_command()
    assert command[: len(prefix)] == prefix
    assert command[command.index('--cache-dir') + 1] == str(tmp_path / 'data' / 'cache' / 'yt-dlp')
    assert command[command.index('--ffmpeg-location') + 1] == '/selected ffmpeg'
    assert command[command.index('--js-runtimes') + 1] == 'deno:/selected deno'
    assert command[-2:] == ['--cookies-from-browser', 'firefox']


def test_packaged_deno_does_not_fall_back_to_user_tools(tmp_path, monkeypatch):
    _packaged(monkeypatch, tmp_path / 'missing-bundle')
    monkeypatch.setattr(youtube.shutil, 'which', _no_process)
    with pytest.raises(ToolMissingError, match='missing its JavaScript engine'):
        youtube._deno_path()
    monkeypatch.setattr(runtime, 'bundled_tool', lambda name: tmp_path / 'deno')
    assert youtube._deno_path() == str(tmp_path / 'deno')


def test_packaged_downloader_version_exercises_the_worker(monkeypatch):
    _packaged(monkeypatch)
    monkeypatch.setattr(youtube.importlib.metadata, 'version', _no_process)
    calls = []
    monkeypatch.setattr(
        proc, 'run', lambda command, **kwargs: calls.append(command) or proc.ProcResult(0, '2026.08.19\n', '')
    )
    assert youtube.ytdlp_version() == '2026.08.19'
    assert calls == [[runtime.sys.executable, '--audiolab-downloader', '--version']]


def test_packaged_maintenance_never_starts_an_installer(monkeypatch):
    _packaged(monkeypatch)
    monkeypatch.setattr(proc, 'run', _no_process)
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    monkeypatch.setattr(ytdlp_update, 'install_latest', lambda progress=None: {'ok': True, 'via': 'app-owned update'})
    assert youtube.update_ytdlp() == {'ok': True, 'via': 'app-owned update'}  # never pip in the frozen app
    with pytest.raises(AudioLabError, match='cannot be added'):
        install.install_command()
    installer = install.AIInstaller()
    with pytest.raises(AudioLabError, match='cannot be added'):
        installer.start(_no_process)
    assert not installer.running
    monkeypatch.setattr(youtube, 'update_ytdlp', _no_process)
    assert youtube.self_repair_update() is None


def test_source_ai_installer_still_uses_its_python(monkeypatch):
    monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    monkeypatch.setattr(install.importlib.util, 'find_spec', lambda name: object())
    command = install.install_command()
    assert command[:4] == [sys.executable, '-m', 'pip', 'install']
    assert command[-2:] == install.AI_PACKAGES


def test_source_downloader_update_still_uses_pip(monkeypatch):
    monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    versions = iter(['old', 'new'])
    monkeypatch.setattr(youtube, 'ytdlp_version', lambda: next(versions))
    calls = []
    monkeypatch.setattr(proc, 'run', lambda command, **kwargs: calls.append(command) or proc.ProcResult(0, '', ''))
    result = youtube.update_ytdlp()
    assert result['ok'] and result['before'] == 'old' and result['after'] == 'new'
    assert calls[0][:4] == [sys.executable, '-m', 'pip', 'install']
    assert calls[0][-1] == 'yt-dlp[default]'


def test_packaged_ai_detection_does_not_import_optional_packages(monkeypatch):
    _packaged(monkeypatch)
    monkeypatch.setattr(ai.importlib.util, 'find_spec', _no_process)
    status = ai.ai_status(refresh=True)
    assert not status['available'] and not status['install_supported']
    assert status['device'] is None and 'compatible AI download' in status['reason']


def test_core_storage_does_not_inspect_shared_ai_caches(monkeypatch, app_factory):
    _packaged(monkeypatch)
    monkeypatch.setattr(ai, 'model_cache_dir', _no_process)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.get('/api/storage')
        assert response.status_code == 200 and response.json()['models_bytes'] == 0


def test_packaged_shortcut_refuses_before_touching_an_existing_app(tmp_path, monkeypatch, capsys):
    _packaged(monkeypatch)
    monkeypatch.setattr(system, 'install_shortcut', _no_process)
    target = tmp_path / 'AudioLab.app'
    target.mkdir()
    marker = target / 'valuable-file'
    marker.write_text('keep')
    assert cli.cmd_shortcut(SimpleNamespace(dest=tmp_path)) == 1
    assert marker.read_text() == 'keep'
    assert 'already a Mac app' in capsys.readouterr().err


def test_packaged_ffmpeg_never_falls_back_to_external_tools(tmp_path, monkeypatch):
    _packaged(monkeypatch, tmp_path / 'bundle')
    monkeypatch.setattr(ffmpeg.shutil, 'which', _no_process)
    monkeypatch.setenv('AUDIOLAB_FFMPEG', '/unrelated/ffmpeg')
    ffmpeg._locate.cache_clear()
    try:
        result = ffmpeg._locate()
        assert result['path'] is None and result['source'] == 'bundled'
        assert 'Download AudioLab again' in result['error']
    finally:
        ffmpeg._locate.cache_clear()


@pytest.mark.parametrize('packaged', [False, True])
def test_health_reports_maintenance_capabilities_and_fixed_folders(monkeypatch, app_factory, packaged):
    if packaged:
        _packaged(monkeypatch)
    else:
        monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        health = client.get('/api/health').json()
        assert health['packaged'] is packaged
        downloader = health['downloader']
        assert downloader['update_supported'] is True and downloader['fallback'] is False
        assert downloader['source'] == ('bundled' if packaged else 'environment')
        assert (downloader['bundled_version'] is not None) is packaged  # this environment's yt-dlp stands in
        assert health['ai']['install_supported'] is not packaged
        assert not health['ai']['available']
        assert health['data_dir'] == str(SETTINGS.data_dir)
        assert health['logs_dir'] == str(system.log_dir(SETTINGS.data_dir))


def test_packaged_maintenance_api_never_starts_a_package_installer(monkeypatch, app_factory):
    _packaged(monkeypatch)
    monkeypatch.setattr(proc, 'run', _no_process)
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    fetched, reverted = [], []
    monkeypatch.setattr(
        ytdlp_update,
        'install_latest',
        lambda progress=None: fetched.append(1) or {'ok': True, 'before': 'a', 'after': 'b', 'message': 'updated'},
    )
    monkeypatch.setattr(ytdlp_update, 'use_bundled', lambda: reverted.append(1))
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.post('/api/system/install-ai')
        assert response.status_code == 400 and 'compatible AI download' in response.json()['detail']
        assert client.post('/api/system/update-downloader').json()['ok'] and fetched == [1]  # the app-owned update
        assert client.delete('/api/system/downloader').json()['source'] == 'bundled' and reverted == [1]
        assert client.get('/api/settings').json()['auto_update_downloader'] is True
    assert not youtube.auto_fix_downloader()  # click-only in the packaged app (PACKAGED_AUTO_FIX)


def test_source_install_has_no_builtin_downloader_to_go_back_to(monkeypatch, app_factory):
    monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.delete('/api/system/downloader')
        assert response.status_code == 400 and 'built-in downloader' in response.json()['detail']
    assert youtube.auto_fix_downloader()  # source installs keep fixing themselves


def test_packaged_install_api_is_idempotent_when_ai_is_ready(monkeypatch, app_factory):
    _packaged(monkeypatch)
    monkeypatch.setattr(ai, 'ai_status', lambda: {'available': True})
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.post('/api/system/install-ai')
        assert response.status_code == 200 and 'already installed' in response.json()['message']


def test_managed_api_repairs_cancels_and_removes_only_via_manager(monkeypatch, app_factory):
    from audiolab.ai import managed

    _packaged(monkeypatch)
    calls = []

    class Manager:
        def status(self):
            return {'available': True, 'managed': True, 'device': 'cpu', 'install_supported': True}

        def start(self, done, repair=False):
            calls.append(('install', repair))
            return True

        def cancel(self):
            calls.append(('cancel',))

        def remove(self):
            calls.append(('remove',))
            return {'available': False, 'managed': True}

        def shutdown(self):
            pass

    manager = Manager()
    monkeypatch.setattr(managed, 'managed_ai', lambda: manager)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        assert client.post('/api/system/install-ai', json={'repair': True}).status_code == 200
        assert client.post('/api/system/cancel-ai').status_code == 200
        assert client.delete('/api/system/ai').status_code == 200
    assert calls == [('install', True), ('cancel',), ('remove',)]


def test_source_api_never_removes_unmanaged_ai(monkeypatch, app_factory):
    monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.delete('/api/system/ai')
        assert response.status_code == 400 and 'shared caches' in response.json()['detail']


@pytest.mark.parametrize('part', ['', 'ai', 'ai/finished', 'jobs/j_1/exports', 'library'])
def test_settings_rejects_output_overlapping_managed_storage(part, app_factory, monkeypatch):
    monkeypatch.delenv('AUDIOLAB_OUTPUT_DIR')
    target = SETTINGS.data_dir / part
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.put('/api/settings', json={'output_dir': str(target)})
        assert response.status_code == 400 and 'AI storage' in response.json()['detail']
        assert not (SETTINGS.data_dir / 'ai').exists()


def test_source_environment_cannot_publish_audio_into_managed_storage(tmp_path, monkeypatch):
    from audiolab.tasks import common

    monkeypatch.setattr(runtime, 'is_packaged', lambda: False)
    monkeypatch.setenv('AUDIOLAB_OUTPUT_DIR', str(SETTINGS.data_dir / 'ai' / 'finished'))
    with pytest.raises(AudioLabError, match='AI storage'):
        common.output_folder('Saved audio')
    with pytest.raises(AudioLabError, match='AI storage'):
        common.output_root(common.Acquired(path=tmp_path / 'tone.wav', title='Tone'))
    assert not (SETTINGS.data_dir / 'ai').exists()


class _WorkStarted(Exception):
    pass


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize(
    ('task', 'source', 'options'),
    [
        ('download', {'kind': 'url', 'url': 'https://example.test/watch?v=abc'}, {'format': 'mp3'}),
        ('enhance', {'kind': 'file', 'name': 'tone.wav'}, {}),
    ],
)
def test_a_saved_output_folder_inside_app_data_is_refused_before_any_work(
    tmp_path, monkeypatch, task, source, options, legacy
):
    from audiolab import tasks
    from audiolab.context import TaskContext
    from audiolab.tasks import common

    monkeypatch.delenv('AUDIOLAB_OUTPUT_DIR')  # the saved setting decides, as on a user's computer
    folder = SETTINGS.data_dir / 'finished' if legacy else tmp_path / 'Music'  # the first: accepted before 2.5
    saved = json.dumps({'output_dir': str(folder)})
    SETTINGS.settings_file.write_text(saved)
    SETTINGS._settings = None
    assert SETTINGS.output_dir == folder  # reloaded as saved

    def work(*args, **kwargs):
        raise _WorkStarted

    monkeypatch.setattr(common, 'probe_url', work)  # a link's lookup comes before its download
    monkeypatch.setattr(common.ffmpeg, 'probe_media', work)  # a file's first step
    source = {**source, 'path': str(tmp_path / 'tone.wav')} if source['kind'] == 'file' else source
    (tmp_path / 'tone.wav').write_bytes(b'RIFF')
    opts = tasks.validate(task, options, source)
    spec = tasks.get(task)
    ctx = TaskContext('t', tmp_path / 'job', opts, spec.stages(opts, source))
    if legacy:
        with pytest.raises(AudioLabError, match='separate from AudioLab’s app data folder') as refused:
            spec.run(ctx, source)
        assert any('Settings → Files' in hint for hint in refused.value.hints)
        assert not folder.exists() and SETTINGS.settings_file.read_text() == saved  # nothing made or rewritten
    else:
        with pytest.raises(_WorkStarted):  # unchanged: the work starts
            spec.run(ctx, source)


def test_packaged_download_errors_offer_the_downloader_update(monkeypatch):
    _packaged(monkeypatch)
    for message in (
        'ERROR: HTTP Error 403: Forbidden',
        'ERROR: signature extraction failed',
        'ERROR: something unknown',
    ):
        error = youtube._friendly_error(message, 'https://example.test/audio')
        assert youtube._UPDATE_HINT in error.hints
        assert not any('newer AudioLab app' in hint for hint in error.hints)


@pytest.mark.parametrize('source', ['updated', 'bundled'])
def test_download_errors_name_the_way_back_while_an_update_is_in_use(monkeypatch, source):
    _packaged(monkeypatch)
    monkeypatch.setattr(ytdlp_update, 'state', lambda: {'source': source, 'fallback': False, 'active': None})
    back = 'Use built-in version'
    for message in ('ERROR: signature extraction failed', 'Traceback (most recent call last):\nKeyError: x'):
        hints = youtube._friendly_error(message, 'https://example.test/audio').hints
        assert any(back in hint for hint in hints) is (source == 'updated'), hints
    private = youtube._friendly_error('ERROR: Private video', 'https://example.test/audio').hints
    assert not any(back in hint for hint in private)  # not the downloader's fault: no way back offered


def test_unused_downloader_archives_go_only_once_this_app_holds_the_data_folder(monkeypatch, app_factory, tmp_path):
    _packaged(monkeypatch)
    running = JobManager(runner=lambda job, ctx: {}, stages_for=lambda job: [], root=tmp_path / 'jobs')
    running.start()  # the app that is already open: one of its downloads still reads this archive
    folder = ytdlp_update.folder()
    folder.mkdir(parents=True)
    archive = folder / 'yt-dlp-2099.01.01.zip'
    archive.write_bytes(b'imported lazily by a running downloader')
    with pytest.raises(AudioLabError, match='already running'), TestClient(app_factory()):
        pass  # a second launch with the same data folder
    assert archive.exists()
    running.shutdown(1)
    with TestClient(app_factory()):
        assert not archive.exists()  # the next real start cleans up


def test_an_undeletable_unused_archive_never_stops_the_app(monkeypatch, app_factory, caplog):
    _packaged(monkeypatch)
    folder = ytdlp_update.folder()
    folder.mkdir(parents=True)
    archive = folder / 'yt-dlp-2099.01.01.zip'
    archive.write_bytes(b'unused')
    folder.chmod(0o555)  # readable, not writable: the archive can't be deleted
    try:
        with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
            assert client.get('/api/ping').json()['ok']
        assert archive.exists() and 'Could not delete an unused downloader update' in caplog.text
    finally:
        folder.chmod(0o755)


def test_reveal_endpoints_open_only_fixed_app_folders(tmp_path, monkeypatch, app_factory):
    opened = []
    monkeypatch.setattr(system, 'reveal', opened.append)
    arbitrary = tmp_path / 'other folder'
    arbitrary.mkdir()
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        expected = [SETTINGS.data_dir, system.log_dir(SETTINGS.data_dir)]
        for path in ('/api/reveal-data', '/api/reveal-logs'):
            response = client.post(path, params={'path': str(arbitrary)}, json={'path': str(arbitrary)})
            assert response.status_code == 200 and response.json() == {'ok': True}
        assert opened == expected and arbitrary not in opened
        with pytest.raises(HTTPException) as refused:
            client.app.state.audiolab.reveal_app_folder(str(arbitrary))
        assert refused.value.status_code == 404 and opened == expected


def test_reveal_app_folders_are_local_only(monkeypatch, app_factory):
    monkeypatch.setattr(system, 'reveal', _no_process)
    with TestClient(app_factory(local=False)) as client:
        for path in ('/api/reveal-data', '/api/reveal-logs'):
            response = client.post(path)
            assert response.status_code == 403 and 'Folders can only be opened' in response.json()['detail']


def test_packaged_downloader_changes_are_local_only_and_say_so(monkeypatch, app_factory):
    _packaged(monkeypatch)
    monkeypatch.setattr(ytdlp_update, 'install_latest', lambda progress=None: pytest.fail('updated remotely'))
    monkeypatch.setattr(ytdlp_update, 'use_bundled', lambda: pytest.fail('reset remotely'))
    with TestClient(app_factory(local=False)) as client:
        for response in (client.post('/api/system/update-downloader'), client.delete('/api/system/downloader')):
            assert response.status_code == 403
            assert response.json()['detail'] == 'The downloader can only be changed on the computer AudioLab runs on.'


def test_packaged_ai_changes_are_local_only_and_say_so(monkeypatch, app_factory):
    _packaged(monkeypatch)
    with TestClient(app_factory(local=False)) as client:
        calls = (
            client.post('/api/system/install-ai'),
            client.post('/api/system/cancel-ai'),
            client.delete('/api/system/ai'),
        )
        for response in calls:
            assert response.status_code == 403
            assert response.json()['detail'] == 'The AI add-on can only be changed on the computer AudioLab runs on.'


def test_packaged_maintenance_cli_explains_unavailable_commands(monkeypatch, capsys):
    _packaged(monkeypatch)
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    assert cli.main(['install-ai']) == 1
    message = capsys.readouterr().err
    assert 'AudioLab' in message and 'Traceback' not in message
    monkeypatch.setattr(proc, 'run', _no_process)
    monkeypatch.setattr(
        ytdlp_update, 'install_latest', lambda progress=None: {'ok': False, 'message': 'No internet connection.'}
    )
    assert cli.main(['update']) == 1 and 'No internet connection.' in capsys.readouterr().out


def test_reveal_missing_logs_does_not_create_the_folder(tmp_path, monkeypatch):
    missing = tmp_path / 'no logs'
    monkeypatch.setattr(system, 'log_dir', lambda data: missing)
    monkeypatch.setattr(system, 'reveal', _no_process)
    state = AppState(local=True, token=None)
    with pytest.raises(AudioLabError, match='does not exist yet'):
        state.reveal_app_folder('logs')
    assert not missing.exists()
