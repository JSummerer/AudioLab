"""Exercise the frozen Cocoa/WebKit window on a disposable CI runner, then quit."""

import json
import os
import threading
import time
import urllib.request
from pathlib import Path


def _require_ci() -> None:
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('The native-window smoke check runs only on disposable GitHub Actions runners.')
    if os.environ.get('RUNNER_OS') != 'macOS':
        raise RuntimeError('The native-window smoke check requires a macOS runner.')
    from audiolab import runtime

    if not runtime.is_packaged():
        raise RuntimeError('Run this check through the actual packaged executable.')
    temporary = os.environ.get('RUNNER_TEMP')
    if not temporary:
        raise RuntimeError('The smoke check requires the runner temporary folder.')
    root = Path(temporary).resolve()
    targets = []
    for name in ('AUDIOLAB_DATA_DIR', 'AUDIOLAB_OUTPUT_DIR'):
        raw = os.environ.get(name)
        if not raw or not Path(raw).is_absolute():
            raise RuntimeError(f'{name} must be an isolated absolute folder beneath RUNNER_TEMP.')
        target = Path(raw).resolve()
        if target == root or not target.is_relative_to(root):
            raise RuntimeError(f'{name} must be beneath RUNNER_TEMP.')
        targets.append(target)
    if targets[0] == targets[1]:
        raise RuntimeError('The smoke check requires separate data and output folders.')


def _wait_for(window, script: str, message: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if window.evaluate_js(script):
            return
        time.sleep(0.2)
    raise RuntimeError(message)


def main() -> int:
    # This command is for disposable CI, never the user's normal desktop or data folder.
    _require_ci()
    import webview

    from audiolab import desktop

    host = '127.0.0.1'
    port = desktop.pick_port(host, 18790)
    server = desktop.make_server(host, port)
    thread = threading.Thread(target=server.run, daemon=True)
    url = f'http://{host}:{port}'
    result = {'ok': False, 'url': url}
    window = None

    def inspect():
        try:
            if window.gui.renderer != 'wkwebview':
                raise RuntimeError(f'The native check requires WebKit, got {window.gui.renderer}.')
            _wait_for(
                window,
                '!!document.querySelector(\'[data-task="download"]\')',
                'The native window did not render the application.',
            )
            with urllib.request.urlopen(url + '/api/health', timeout=10) as response:
                health = json.load(response)
            assert health['packaged'] and health['ffmpeg']['ok']
            assert health['ffmpeg']['source'] == 'bundled'
            assert health['downloader']['ok'] and not health['downloader']['update_supported']
            assert health['ai']['available'] is False
            assert health['ai']['install_supported'] is True and health['ai']['managed']
            viewport = window.evaluate_js(
                '({width: innerWidth, height: innerHeight, dpr: devicePixelRatio, '
                'visualViewport: window.visualViewport ? {width: visualViewport.width, '
                'height: visualViewport.height, scale: visualViewport.scale} : null, '
                'cssZoom: getComputedStyle(document.documentElement).zoom, '
                'screen: {width: screen.width, height: screen.height}, '
                'userAgent: navigator.userAgent})'
            )
            assert viewport['width'] >= 380 and viewport['height'] >= 400 and viewport['dpr'] > 0
            assert 'AppleWebKit' in viewport['userAgent']
            window.evaluate_js("location.hash = '#/settings/system'; true")
            _wait_for(
                window,
                "!!document.querySelector('#enableAi')",
                'The native window did not render the optional AI download action.',
            )
            assert window.evaluate_js("!document.querySelector('#updateDownloader')")
            result.update(
                rendered=True,
                backend=window.gui.renderer,
                viewport=viewport,
                health=health,
            )
        except Exception as error:
            result['error'] = repr(error)
        finally:
            try:
                window.destroy()
            except Exception as error:
                result['error'] = f'Could not close the native window: {error!r}'

    try:
        thread.start()
        if not desktop._wait_until_started(server, thread):
            raise RuntimeError('Packaged local server did not start.')
        window = webview.create_window('AudioLab smoke check', url, width=1280, height=860, hidden=True)
        webview.start(inspect, gui='cocoa', private_mode=False)
        result['window_closed'] = window.events.closed.is_set()
    except Exception as error:
        result['error'] = repr(error)
    finally:
        try:
            server.config.app.state.audiolab.shutdown()
        except Exception as error:
            result['error'] = f'Could not shut down app state: {error!r}'
        finally:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=5)
        result['server_stopped'] = not thread.is_alive()
    result['ok'] = bool(
        result.get('rendered') and result.get('window_closed') and result['server_stopped'] and not result.get('error')
    )
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result['ok'] else 1
