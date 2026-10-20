"""Headless desktop adapter contracts; no native window or OS notification is opened."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from audiolab import APP_NAME, desktop, system
from audiolab.api.state import AppState
from audiolab.jobs import Job


class ClosingEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        assert len(self.handlers) == 1, 'The adapter must register one closing handler before starting the window'
        return self.handlers[0]()


class WindowAdapter:
    """Run a finite close scenario in place of pywebview's native event loop."""

    def __init__(self, scenario):
        self.settings = {}
        self.window = SimpleNamespace(events=SimpleNamespace(closing=ClosingEvent()))
        self.scenario = scenario
        self.start_options = None

    def create_window(self, title, url, **kwargs):
        self.title, self.url = title, url
        return self.window

    def start(self, **kwargs):
        self.start_options = kwargs
        self.scenario(self.window.events.closing.fire)


class ServerAdapter:
    """A bounded in-memory server stand-in, with a real thread but no listener."""

    def __init__(self, statuses):
        self.ready = threading.Event()
        self.exit_requested = threading.Event()
        self.thread = None
        self.events = []
        self.jobs = [{'status': status} for status in statuses]
        self.state = SimpleNamespace(manager=SimpleNamespace(list=lambda: self.jobs), shutdown=self.shutdown)
        self.config = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(audiolab=self.state)))

    @property
    def should_exit(self):
        return self.exit_requested.is_set()

    @should_exit.setter
    def should_exit(self, value):
        assert value is True
        self.events.append('server exit')
        self.exit_requested.set()

    def run(self):
        self.thread = threading.current_thread()
        self.ready.set()
        self.exit_requested.wait(timeout=5)

    def shutdown(self):
        self.events.append('state shutdown')

    def cleanup(self):
        self.exit_requested.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
            assert not self.thread.is_alive()


@pytest.fixture(autouse=True)
def no_os_effects(monkeypatch):
    # Any accidentally unmocked OS process invocation fails instead of opening a prompt.
    monkeypatch.setattr(system.subprocess, 'Popen', Mock(side_effect=AssertionError('OS processes are forbidden')))
    monkeypatch.setattr(system, 'prepare_app_identity', Mock())
    monkeypatch.setattr(system, 'confirm', Mock(side_effect=AssertionError('Unexpected quit prompt')))
    monkeypatch.setattr(system, 'notify', Mock())
    monkeypatch.setattr(desktop, '_remember_instance', Mock())
    monkeypatch.setattr(desktop, '_forget_instance', Mock())


def run_window(monkeypatch, server, scenario):
    webview = WindowAdapter(scenario)
    monkeypatch.setattr(desktop, '_wait_until_started', lambda server, thread: server.ready.wait(timeout=1))
    try:
        result = desktop._run_window(
            webview, server, 'http://127.0.0.1:19999', 'http://127.0.0.1:19999/?token=test', False
        )
    finally:
        server.cleanup()
    assert result == 0
    assert webview.title == APP_NAME
    assert webview.url == 'http://127.0.0.1:19999/?token=test'
    return webview


@pytest.mark.parametrize('statuses', [[], ['queued'], ['done', 'queued']])
def test_idle_or_queued_window_closes_without_prompt(monkeypatch, statuses):
    server = ServerAdapter(statuses)

    def close_window(close):
        assert close() is True
        assert server.should_exit
        assert server.events[:2] == ['state shutdown', 'server exit']
        assert not server.thread.is_alive()

    webview = run_window(monkeypatch, server, close_window)
    system.confirm.assert_not_called()
    # These prove adapter configuration only, not WebKit storage or download behaviour.
    assert webview.start_options == {'private_mode': False}
    assert webview.settings['ALLOW_DOWNLOADS'] is True


@pytest.mark.parametrize('running', [1, 2])
def test_cancel_quit_keeps_server_and_jobs_alive_then_accept_stops_state_first(monkeypatch, running):
    statuses = ['running'] * running + ['queued']
    server = ServerAdapter(statuses)
    confirm = Mock(side_effect=[False, True])
    monkeypatch.setattr(system, 'confirm', confirm)

    def cancel_then_close(close):
        assert close() is False
        assert not server.should_exit
        assert server.thread.is_alive()
        assert server.events == []
        assert [job['status'] for job in server.jobs] == statuses
        assert close() is True
        assert server.events == ['state shutdown', 'server exit']
        assert not server.thread.is_alive()

    run_window(monkeypatch, server, cancel_then_close)
    assert confirm.call_count == 2
    title, message = confirm.call_args.args
    assert title == ('A job is still running.' if running == 1 else '2 jobs are still running.')
    assert 'stop it?' in message if running == 1 else 'stop them?' in message
    assert confirm.call_args.kwargs == {'ok': 'Quit', 'cancel': 'Cancel'}


@pytest.mark.parametrize(
    ('local', 'status', 'started_at', 'finished_at', 'expected'),
    [
        (True, 'done', 100, 120, 'Check finished: Test recording'),
        (True, 'failed', 100, 130, 'Failed: Test recording'),
        (True, 'done', 100, 119.9, None),
        (True, 'failed', 100, 119.9, None),
        (False, 'done', 100, 130, None),
        (False, 'failed', 100, 130, None),
        (True, 'cancelled', 100, 130, None),
        (True, 'done', None, 130, None),
        (True, 'done', 100, None, None),
    ],
)
def test_finished_notification_hook_is_local_long_and_terminal_only(local, status, started_at, finished_at, expected):
    manager = SimpleNamespace()
    AppState(local=local, token=None, manager=manager)
    job = Job(
        id='test',
        task='analyze',
        source={},
        options={},
        title='Test recording',
        status=status,
        started_at=started_at,
        finished_at=finished_at,
    )
    # Exercise the callback wired by AppState, as the worker does after finishing a job.
    manager.on_finished(job)
    if expected is None:
        system.notify.assert_not_called()
    else:
        system.notify.assert_called_once_with(APP_NAME, expected)
