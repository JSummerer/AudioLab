"""Run the real shell launcher with fixture-local tools, never setup, network or OS UI."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash', path='/usr/bin:/bin')
pytestmark = pytest.mark.skipif(os.name != 'posix' or BASH is None, reason='POSIX shell launcher')

_TOOL = """import json
import os
from pathlib import Path
import sys

name = sys.argv[1]
args = sys.argv[2:]
with open(os.environ['LAUNCHER_TRACE'], 'a') as out:
    out.write(json.dumps({'tool': name, 'args': args, 'cwd': os.getcwd()}) + '\\n')
if name == 'uname':
    print('Darwin')
elif name == 'python':
    if args[:1] == ['-c']:
        sys.exit(int(os.environ.get('LAUNCHER_RUNTIME_EXIT', '0')))
    if args[:2] == ['-m', 'pip']:
        code = int(os.environ.get('LAUNCHER_PIP_EXIT', '0'))
        if code:
            print('simulated package installation failure', file=sys.stderr)
        sys.exit(code)
elif name == 'curl':
    print('fake installer, never executed')
    sys.exit(int(os.environ.get('LAUNCHER_CURL_EXIT', '0')))
elif name == 'sh':
    sys.stdin.read()  # consume the fake download without executing it
    sys.exit(int(os.environ.get('LAUNCHER_SH_EXIT', '0')))
elif name != 'osascript':
    sys.exit('Unexpected fake tool: ' + name)
"""


def _tool(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # The absolute interpreter is quoted so checkouts with spaces work too.
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(_TOOL)} {path.name} "$@"\n')
    path.chmod(0o755)


@pytest.fixture
def launcher(tmp_path):
    folder = tmp_path / "Audio Lab's copy"
    folder.mkdir()
    tools = folder / 'fake-bin'
    tools.mkdir()
    # Keep HOME unchanged. Only the copied script's fallback uv lookup is redirected,
    # so a real ~/.local/bin/uv cannot escape the tightly controlled PATH.
    source = (ROOT / 'start.sh').read_text().replace('$HOME/.local/bin', '$LAUNCHER_LOCAL_BIN')
    (folder / 'start.sh').write_text(source)
    shutil.copyfile(ROOT / 'pyproject.toml', folder / 'pyproject.toml')
    for name in ('dirname', 'cksum', 'cut', 'cat', 'rm'):
        tools.joinpath(name).symlink_to(shutil.which(name, path='/usr/bin:/bin'))
    for name in ('uname', 'osascript'):
        _tool(tools / name)
    env = {
        **os.environ,
        'PATH': str(tools),
        'LAUNCHER_TRACE': str(folder / 'trace.jsonl'),
        'LAUNCHER_LOCAL_BIN': str(folder / 'unused-local-bin'),
    }
    for name in ('BASH_ENV', 'ENV', 'PYTHONHOME', 'PYTHONPATH'):
        env.pop(name, None)
    return folder, env


def _runtime(folder, *, installed=True):
    _tool(folder / '.venv' / 'bin' / 'python')
    stamp = folder / '.venv' / '.audiolab-installed'
    if installed:
        checksum = subprocess.check_output([str(folder / 'fake-bin' / 'cksum'), 'pyproject.toml'], cwd=folder)
        stamp.write_bytes(checksum.split()[0] + b'\n')
    return stamp


def _run(folder, env, *args):
    return subprocess.run(
        [BASH, str(folder / 'start.sh'), *args],
        cwd=folder.parent,
        env=env,
        input='y\n',
        capture_output=True,
        text=True,
        timeout=10,
    )


def _events(folder, tool=None):
    events = [json.loads(line) for line in (folder / 'trace.jsonl').read_text().splitlines()]
    return [event for event in events if tool is None or event['tool'] == tool]


def _assert_no_launch(folder):
    assert not any(event['args'][:2] == ['-m', 'audiolab'] for event in _events(folder, 'python'))


@pytest.mark.parametrize('mode', ['--app', '--terminal'])
def test_broken_runtime_with_current_stamp_explains_repair(launcher, mode):
    folder, env = launcher
    stamp = _runtime(folder)
    previous_stamp = stamp.read_bytes()
    result = _run(folder, {**env, 'LAUNCHER_RUNTIME_EXIT': '127'}, mode)
    assert result.returncode != 0
    _assert_no_launch(folder)
    assert stamp.read_bytes() == previous_stamp
    assert len(_events(folder, 'python')) == 1  # preflight failed before trusting the install stamp
    alerts = _events(folder, 'osascript')
    if mode == '--app':
        assert len(alerts) == 1
        assert 'display alert' in ' '.join(alerts[0]['args'])
        assert '--reinstall' in alerts[0]['args'][-1]
    else:
        assert not alerts
        assert '--reinstall' in result.stderr
        assert 'Python environment' in result.stderr


def test_usable_environment_skips_install_and_preserves_arguments(launcher):
    folder, env = launcher
    stamp = _runtime(folder)
    previous_stamp = stamp.read_bytes()
    args = ['--browser', '--port', '8790', 'a "quoted" value $[*]', '']
    result = _run(folder, env, '--terminal', *args)
    assert result.returncode == 0, result.stderr
    calls = _events(folder, 'python')
    assert len(calls) == 2
    assert calls[0]['args'][0] == '-c'
    assert calls[1]['args'] == ['-m', 'audiolab', 'app', *args]
    assert all(Path(call['cwd']) == folder for call in calls)
    assert stamp.read_bytes() == previous_stamp
    assert not _events(folder, 'osascript')


def test_missing_curl_explains_manual_setup_without_launch(launcher):
    folder, env = launcher
    result = _run(folder, env, '--terminal')
    assert result.returncode != 0
    assert 'Setup needs curl' in result.stderr
    assert 'https://docs.astral.sh/uv/getting-started/installation/' in result.stderr
    assert 'run the launcher again' in result.stderr
    _assert_no_launch(folder)
    assert not (folder / '.venv').exists()


@pytest.mark.parametrize('failed_tool', ['curl', 'sh'])
def test_bootstrap_failure_explains_retry_without_launch(launcher, failed_tool):
    folder, env = launcher
    for name in ('curl', 'sh'):
        _tool(folder / 'fake-bin' / name)
    result = _run(folder, {**env, f'LAUNCHER_{failed_tool.upper()}_EXIT': '42'}, '--terminal')
    assert result.returncode != 0
    assert 'Could not install uv' in result.stderr
    assert 'run the launcher again' in result.stderr
    assert 'Manual setup: https://docs.astral.sh/uv/' in result.stderr
    assert _events(folder, 'curl') and _events(folder, 'sh')
    _assert_no_launch(folder)
    assert not (folder / '.venv').exists()


@pytest.mark.parametrize('mode', ['--app', '--terminal'])
def test_failed_install_does_not_stamp_success_or_launch(launcher, mode):
    folder, env = launcher
    stamp = _runtime(folder, installed=False)
    result = _run(folder, {**env, 'LAUNCHER_PIP_EXIT': '42'}, mode)
    assert result.returncode != 0
    assert not stamp.exists()
    _assert_no_launch(folder)
    assert _events(folder, 'python')[-1]['args'][:2] == ['-m', 'pip']
    if mode == '--app':
        alert = _events(folder, 'osascript')[-1]
        assert 'Installing an update failed' in alert['args'][-1]
        assert 'simulated package installation failure' in (folder / '.venv' / 'install.log').read_text()
    else:
        assert 'simulated package installation failure' in result.stderr
