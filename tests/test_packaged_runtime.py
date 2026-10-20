"""Frozen/source runtime boundaries, with no external requests, installers or models."""

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
from audiolab.media import export, ffmpeg, proc, youtube
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
    command = youtube.ytdlp_cmd()
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
    with pytest.raises(AudioLabError, match='built into this AudioLab app'):
        youtube.update_ytdlp()
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
    assert status['device'] is None and 'core app' in status['reason']


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
        assert health['downloader']['update_supported'] is not packaged
        assert health['ai']['install_supported'] is not packaged
        assert not health['ai']['available']
        assert health['data_dir'] == str(SETTINGS.data_dir)
        assert health['logs_dir'] == str(system.log_dir(SETTINGS.data_dir))


def test_packaged_maintenance_api_rejects_before_any_subprocess(monkeypatch, app_factory):
    _packaged(monkeypatch)
    monkeypatch.setattr(proc, 'run', _no_process)
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        for path, message in (
            ('/api/system/update-downloader', 'built into this AudioLab app'),
            ('/api/system/install-ai', 'cannot be added'),
        ):
            response = client.post(path)
            assert response.status_code == 400 and message in response.json()['detail']
        assert client.get('/api/settings').json()['auto_update_downloader'] is True


def test_packaged_install_api_rejects_even_if_cached_ai_status_says_ready(monkeypatch, app_factory):
    _packaged(monkeypatch)
    monkeypatch.setattr(ai, 'ai_status', lambda: {'available': True})
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    with TestClient(app_factory(), base_url='http://127.0.0.1') as client:
        response = client.post('/api/system/install-ai')
        assert response.status_code == 400 and 'cannot be added' in response.json()['detail']


def test_packaged_download_errors_recommend_app_replacement(monkeypatch):
    _packaged(monkeypatch)
    for message in (
        'ERROR: HTTP Error 403: Forbidden',
        'ERROR: signature extraction failed',
        'ERROR: something unknown',
    ):
        error = youtube._friendly_error(message, 'https://example.test/audio')
        assert any('newer AudioLab app' in hint for hint in error.hints)
        assert not any('Settings → Downloads' in hint for hint in error.hints)


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
            assert response.status_code == 403


@pytest.mark.parametrize('command', ['update', 'install-ai'])
def test_packaged_maintenance_cli_explains_unavailable_commands(monkeypatch, capsys, command):
    _packaged(monkeypatch)
    monkeypatch.setattr(install.subprocess, 'Popen', _no_process)
    assert cli.main([command]) == 1
    message = capsys.readouterr().err
    assert 'AudioLab' in message and 'Traceback' not in message


def test_reveal_missing_logs_does_not_create_the_folder(tmp_path, monkeypatch):
    missing = tmp_path / 'no logs'
    monkeypatch.setattr(system, 'log_dir', lambda data: missing)
    monkeypatch.setattr(system, 'reveal', _no_process)
    state = AppState(local=True, token=None)
    with pytest.raises(AudioLabError, match='does not exist yet'):
        state.reveal_app_folder('logs')
    assert not missing.exists()
