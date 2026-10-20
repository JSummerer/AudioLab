"""Start the local server and open the UI: in a native window when pywebview is installed
(by default on macOS; `pip install "audiolab-app[desktop]"` elsewhere), otherwise in the
default browser.

One app per data folder: starting it again brings the open window to the front (or opens
the running server in the browser). The window quits one way — close button, Cmd+Q, Quit
in the Dock, Ctrl+C in the terminal: it asks first while a job is running, then stops it
and the server before the process ends. Queued jobs are kept as not started.
"""

import io
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

from . import APP_NAME, __version__, system

DEFAULT_PORT = 8765
APP_ICON = Path(__file__).resolve().parent / 'assets' / 'AppIcon.icns'

log = logging.getLogger('audiolab')


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Windows: SO_REUSEADDR would let us "bind" next to another listener and pick a busy port.
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        s.setsockopt(socket.SOL_SOCKET, exclusive if exclusive is not None else socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, preferred: int = DEFAULT_PORT) -> int:
    for port in (preferred, *range(preferred + 1, preferred + 20)):
        if port_free(host, port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _wait_until_started(server, server_thread: threading.Thread | None = None, timeout_s: float = 30.0) -> bool:
    """True once *this* server serves — not just anything answering on its port (a second
    start that lost the race for it would otherwise take the winner for itself)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server.started:
            return True
        if server_thread is not None and not server_thread.is_alive():
            return False  # it gave up (port taken, data folder in use by another AudioLab)
        time.sleep(0.05)
    return False


def _other_instance(wait_s: float = 5.0) -> dict | None:
    """After a failed start: the AudioLab that won the race, once it is up."""
    deadline = time.monotonic() + wait_s
    while True:
        running = existing_instance()
        if running or time.monotonic() > deadline:
            return running
        time.sleep(0.25)


def make_server(host: str, port: int, token=None):
    import uvicorn

    from .api import create_app, is_loopback

    class Server(uvicorn.Server):
        async def shutdown(self, sockets=None):
            # Live-update streams (open windows and tabs) never finish on their own: end them
            # first, or every Ctrl+C / quit would wait for the whole graceful timeout.
            self.config.app.state.audiolab.hub.close()
            await super().shutdown(sockets)

    app = create_app(local=is_loopback(host), token=token)
    # The timeout bounds what else is still running (a big ZIP download) so stopping never hangs.
    config = uvicorn.Config(app, host=host, port=port, log_level='warning', timeout_graceful_shutdown=3)
    server = Server(config)
    _stop_on_hangup(server)
    return server


def _stop_on_hangup(server) -> None:
    """Closing the terminal window (SIGHUP) shuts down cleanly, cancelling running jobs."""
    import signal

    if hasattr(signal, 'SIGHUP') and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGHUP, lambda *_: setattr(server, 'should_exit', True))


# ---------------------------------------------------------------- one instance
def _instance_file():
    from .settings import SETTINGS

    return SETTINGS.data_dir / 'instance.json'


def existing_instance() -> dict | None:
    """The AudioLab already running with this data folder ({url, page, pid, window}), or None."""
    try:
        info = json.loads(_instance_file().read_text())
        with urllib.request.urlopen(info['url'] + '/api/ping', timeout=1.5) as r:
            return info if r.status == 200 else None
    except Exception:
        return None


def _remember_instance(url: str, page: str, window: bool) -> None:
    try:
        _instance_file().write_text(json.dumps({'url': url, 'page': page, 'pid': os.getpid(), 'window': window}))
    except OSError:
        pass


def _forget_instance() -> None:
    try:
        if json.loads(_instance_file().read_text()).get('pid') == os.getpid():
            _instance_file().unlink()
    except Exception:
        pass


def _show_running(info: dict) -> None:
    """Bring the running window to the front, or open the running server in the browser."""
    if info.get('window') and system.activate_process(int(info.get('pid') or 0)):
        print(f'\n  {APP_NAME} is already open.\n')
        return
    page = info.get('page') or info['url']
    print(f'\n  {APP_NAME} is already running — opening {page}\n')
    webbrowser.open(page)


# ---------------------------------------------------------- without a terminal
class _LogWriter(io.TextIOBase):
    """Stands in for stdout/stderr when there is no terminal: whole lines go to the app log."""

    _inside = threading.local()  # logging reports its own failures (disk full) on stderr: no loop

    def __init__(self, level: int):
        self._level = level
        self._buffer = ''
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if getattr(self._inside, 'active', False):
            try:
                if sys.__stderr__ is not None:
                    sys.__stderr__.write(text)
            except (OSError, ValueError):
                pass
            return len(text)
        with self._lock:
            *lines, self._buffer = (self._buffer + text).split('\n')
        self._inside.active = True
        try:
            for line in lines:
                if line.strip():
                    log.log(self._level, line.rstrip())
        finally:
            self._inside.active = False
        return len(text)


def _has_terminal() -> bool:
    return any(s is not None and s.isatty() for s in (sys.stdout, sys.stderr))


def _log_output_to_file() -> None:
    """Opened from the Finder/Dock: what would be printed (uvicorn messages, tracebacks)
    goes to the log file. Must run before make_server() — uvicorn binds sys.stderr then."""
    from .diagnostics import setup_logging

    setup_logging()
    if log.handlers:  # without a log file, logging itself would write to stderr
        sys.stdout = _LogWriter(logging.INFO)
        sys.stderr = _LogWriter(logging.WARNING)


def _fatal(message: str, headless: bool) -> None:
    from .diagnostics import log_file

    print(f'  {message}', file=sys.stderr, flush=True)
    if headless:
        system.alert(f'{APP_NAME} could not start', f'{message}\n\nDetails are in the log file:\n{log_file()}')


# ------------------------------------------------------------------------ app
def run_app(port: int = 0, window: bool = True, open_browser: bool = True) -> int:
    headless = not _has_terminal()
    if headless:
        _log_output_to_file()
    try:
        return _run_app(port, window, open_browser, headless)
    except Exception as e:
        if not headless:
            raise
        log.exception('%s could not start', APP_NAME)
        _fatal(f'Something went wrong: {e}', headless)
        return 1


def _import_webview():
    try:
        import webview  # type: ignore
    except ImportError:
        return None
    except Exception:  # installed but broken (e.g. a missing system library): use the browser
        log.exception('The app window is not available; using the browser instead')
        return None
    return webview


def _run_app(port: int, window: bool, open_browser: bool, headless: bool) -> int:
    from .settings import ENV_PREFIX

    running = existing_instance()
    if running:
        _show_running(running)
        return 0

    host = '127.0.0.1'
    port = pick_port(host, port or DEFAULT_PORT)
    url = f'http://{host}:{port}'
    token = os.environ.get(ENV_PREFIX + 'TOKEN') or None
    if token and token.lower() == 'none':
        token = None
    ui_url = f'{url}/?{urlencode({"token": token})}' if token else url
    webview = _import_webview() if window else None
    server = make_server(host, port, token=token)

    print(f'\n  {APP_NAME} {__version__} is running at {url}', flush=True)
    try:
        if webview is not None:
            return _run_window(webview, server, url, ui_url, headless)

        print('  Keep this window open while you use it. Press Ctrl+C to quit.\n', flush=True)

        def _open():
            if _wait_until_started(server):
                _remember_instance(url, ui_url, window=False)
                if open_browser:
                    webbrowser.open(ui_url)

        threading.Thread(target=_open, daemon=True).start()
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        _forget_instance()


def _run_window(webview, server, url: str, ui_url: str, headless: bool) -> int:
    state = server.config.app.state.audiolab
    thread = threading.Thread(target=server.run, name='server', daemon=True)
    thread.start()
    if not _wait_until_started(server, thread):
        running = _other_instance()
        if running:  # another start won the race for the port or the data folder
            _show_running(running)
            return 0
        _fatal(
            f'The local server did not start. If {APP_NAME} is running in a terminal, quit it there first.', headless
        )
        return 1
    _remember_instance(url, ui_url, window=True)
    log.info('App window (pid %s, %s)', os.getpid(), sys.executable)

    system.prepare_app_identity(APP_ICON)
    webview.settings['ALLOW_DOWNLOADS'] = True  # "Download" buttons ask where to save (pywebview ≥ 5)
    win = webview.create_window(APP_NAME, ui_url, width=1280, height=860, min_size=(380, 600))

    def closing() -> bool:
        # pywebview calls this on the main thread for the close button and for Cmd+Q, Quit in the
        # Dock and Ctrl+C; False keeps the window. It has to do all the work: after Cmd+Q the
        # process ends without webview.start() returning.
        try:
            if not _may_quit(state):
                return False
        except Exception:
            log.exception('Could not ask before quitting')
        _stop(server, state, thread)
        return True

    win.events.closing += closing
    # Not private (pywebview's default wipes the website data at every launch): the UI's remembered
    # choices (collapsed sidebar, "More options", "Show details", the cover switch) live in localStorage.
    # The UI files are served no-cache, so a kept web cache can't show an old version.
    webview.start(private_mode=False)
    _stop(server, state, thread)  # already done by closing(); kept for any other way out
    return 0


def _may_quit(state) -> bool:
    """Ask before stopping running jobs (True when none is running; queued ones are kept as
    "Not started" and can be retried, so they don't need a question)."""
    running = sum(j['status'] == 'running' for j in state.manager.list())
    if not running:
        return True
    it = 'it' if running == 1 else 'them'
    title = 'A job is still running.' if running == 1 else f'{running} jobs are still running.'
    message = f'Quit {APP_NAME} and stop {it}? You can run {it} again later with "Try again".'
    return system.confirm(title, message, ok='Quit', cancel='Cancel')


def _stop(server, state, thread: threading.Thread) -> None:
    """Jobs first (cancelled, their tools killed, saved — bounded; this also ends the live-update
    streams), then the server. Nothing is left to save after that, so its thread gets a second."""
    state.shutdown()
    server.should_exit = True
    thread.join(timeout=1)
    _forget_instance()
