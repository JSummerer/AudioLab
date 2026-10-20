"""The app shortcut (macOS app bundle): its launcher script and the bundle layout."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from audiolab import APP_NAME, system
from audiolab.desktop import APP_ICON

posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX shell launcher')


@pytest.mark.parametrize('token', [None, 'none', 'safe_123', 'a+b&c#d', 'a%2Bb', 'a%b'])
def test_desktop_token_reaches_ui_unchanged(monkeypatch, token):
    from audiolab import desktop

    if token is None:
        monkeypatch.delenv('AUDIOLAB_TOKEN', raising=False)
    else:
        monkeypatch.setenv('AUDIOLAB_TOKEN', token)
    expected = None if token in (None, 'none') else token
    captured = {}

    def make_server(host, port, token):
        captured['server_token'] = token
        return object()

    def window(webview, server, url, ui_url, headless):
        captured['ui_url'] = ui_url
        return 0

    monkeypatch.setattr(desktop, 'existing_instance', lambda: None)
    monkeypatch.setattr(desktop, 'pick_port', lambda *_: 19999)
    monkeypatch.setattr(desktop, 'make_server', make_server)
    monkeypatch.setattr(desktop, '_import_webview', lambda: object())
    monkeypatch.setattr(desktop, '_run_window', window)
    monkeypatch.setattr(desktop, '_forget_instance', lambda: None)
    assert desktop._run_app(0, True, False, False) == 0
    assert captured['server_token'] == expected
    assert parse_qs(urlsplit(captured['ui_url']).query).get('token', [None])[0] == expected


def _write_tool(path, body: str):
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)
    return path


@posix_only
def test_launcher_runs_the_command_in_its_folder(tmp_path):
    folder = tmp_path / "Audio Lab's folder"
    folder.mkdir()
    tool = _write_tool(folder / 'start.sh', 'echo "$(pwd)|$*"')
    script = _write_tool(tmp_path / 'launcher', system.launcher_script([str(tool), '--app', 'a b'], folder))
    out = subprocess.run([str(script)], capture_output=True, text=True, check=True).stdout
    assert out.strip() == f'{folder.resolve()}|--app a b'


@posix_only
def test_launcher_explains_a_moved_folder(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    _write_tool(bin_dir / 'osascript', f'printf "%s" "$2" > {tmp_path / "alert.txt"}')
    gone = tmp_path / 'moved' / 'start.sh'
    script = _write_tool(tmp_path / 'launcher', system.launcher_script([str(gone)], gone.parent, 'Moved "it"'))
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    assert subprocess.run([str(script)], env=env).returncode == 1
    expected = f'display alert "{APP_NAME} cannot open" message "Moved \\"it\\"" as critical'
    assert (tmp_path / 'alert.txt').read_text() == expected


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS app bundle')
def test_bundle_is_complete_and_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(system, 'LSREGISTER', str(tmp_path / 'no-lsregister'))  # keep LaunchServices out of it
    for _ in range(2):
        app = system.install_shortcut(['/bin/true'], icon=APP_ICON, version='1.2.3', dest=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f'{APP_NAME}.app']
    info = plistlib.loads((app / 'Contents' / 'Info.plist').read_bytes())
    assert info['CFBundleName'] == APP_NAME and info['CFBundlePackageType'] == 'APPL'
    assert info['CFBundleShortVersionString'] == '1.2.3'
    assert os.access(app / 'Contents' / 'MacOS' / info['CFBundleExecutable'], os.X_OK)
    assert (app / 'Contents' / 'Resources' / 'AppIcon.icns').read_bytes() == APP_ICON.read_bytes()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS app bundle')
def test_failed_swap_keeps_the_old_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(system, 'LSREGISTER', str(tmp_path / 'no-lsregister'))
    app = system.install_shortcut(['/bin/true'], icon=APP_ICON, version='1.0', dest=tmp_path)
    replace = os.replace

    def failing_replace(src, dst):
        if Path(dst) == app and not str(src).endswith('.old'):  # the new bundle cannot be moved in
            raise OSError('disk full')
        replace(src, dst)

    monkeypatch.setattr(system.os, 'replace', failing_replace)
    with pytest.raises(OSError):
        system.install_shortcut(['/bin/true'], icon=APP_ICON, version='2.0', dest=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f'{APP_NAME}.app']
    assert plistlib.loads((app / 'Contents' / 'Info.plist').read_bytes())['CFBundleVersion'] == '1.0'


def test_shortcut_elsewhere_is_not_available_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    with pytest.raises(NotImplementedError):
        system.install_shortcut(['x'], icon=APP_ICON, version='1', dest=tmp_path)
