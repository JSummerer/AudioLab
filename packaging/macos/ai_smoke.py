"""Install the real optional archive and exercise the frozen app, without user data.

Prepublication transport uses a localhost fixture; catalog/hash/probe/activation
remain the production implementation. Public HTTPS installation is checked after
release upload. No external Python is available to the app or its helper.
"""

import argparse
import functools
import http.server
import json
import math
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    parser.add_argument('assets', type=Path)
    parser.add_argument('evidence', type=Path)
    args = parser.parse_args()
    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=False)
    assets = args.assets.resolve()
    executable = args.app.resolve() / 'Contents/MacOS/AudioLab'
    data, output = root / 'data', root / 'output'
    os.environ.update(AUDIOLAB_DATA_DIR=str(data), AUDIOLAB_OUTPUT_DIR=str(output))
    from audiolab.ai.managed import ManagedAI

    manager = ManagedAI(data, assets / 'catalog.json')
    catalog = manager.catalog
    assert catalog, manager.catalog_error
    asset = catalog['url'].rsplit('/', 1)[1]
    assert (assets / asset).is_file()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(assets)))
    fixture_thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    fixture_thread.start()

    def fixture_open(request):
        response = urllib.request.urlopen(
            urllib.request.Request(
                f'http://127.0.0.1:{fixture.server_port}/{asset}', headers=dict(request.header_items())
            ),
            timeout=10,
        )
        response.geturl = lambda: request.full_url
        return response

    manager._open = fixture_open

    def install():
        done = threading.Event()
        manager.start(lambda ok: done.set())
        if not done.wait(240):
            manager.shutdown()
            raise RuntimeError('The real AI pack did not install within four minutes.')
        status = manager.status()
        (root / 'install-status.json').write_text(json.dumps(status, indent=2) + '\n')
        assert status['available'] and not status['installing'], status
        assert not (data / 'ai/download.part').exists()

    env = dict(os.environ, PATH='/usr/bin:/bin')
    for key in ('PYTHONPATH', 'PYTHONHOME', 'AUDIOLAB_FFMPEG', 'AUDIOLAB_TOKEN'):
        env.pop(key, None)
    tone = root / 'tone.wav'
    with wave.open(str(tone), 'wb') as stream:
        stream.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        stream.writeframes(
            b''.join(
                struct.pack('<hh', *(2 * [int(5000 * math.sin(2 * math.pi * 440 * i / 48000))]))
                for i in range(3 * 48000)
            )
        )
    results = []

    def run(label, *arguments):
        completed = subprocess.run(
            [str(executable), *arguments], cwd=root, env=env, capture_output=True, text=True, timeout=180
        )
        (root / f'{label}.log').write_text(completed.stdout + '\n' + completed.stderr)
        assert completed.returncode == 0, f'{label} failed; see {root / f"{label}.log"}'
        result = json.loads(completed.stdout)
        assert result['files'], result
        results.append(label)
        return result

    server = None
    try:
        install()
        results.append('real-archive-install-and-probe')
        run('split', 'separate', str(tone), '--mode', 'stems4', '--format', 'wav16', '--json')
        enhanced = run(
            'enhance-best', 'enhance', str(tone), '--type', 'music', '--best', '--formats', 'wav16', '--json', '--keep'
        )
        assert not any('AI stage failed' in warning for warning in enhanced['warnings']), enhanced['warnings']
        stems = list((data / 'jobs').glob('*/work/**/stems/vocals.wav'))
        assert stems, 'Best did not invoke the AI helper'
        # Preserve valuable-output and Library markers through all managed cleanup.
        marker = data / 'library' / 'keep.txt'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('library is independent')
        saved = sorted(path for path in output.rglob('*') if path.is_file())
        assert saved
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'

        def api(path, method='GET'):
            request = urllib.request.Request(base + path, method=method)
            with urllib.request.urlopen(request, timeout=100) as response:
                return json.load(response)

        def ready_health():
            # App startup probes AI in the background. A busy first health reply
            # is expected while that process holds the installation lease.
            deadline = time.monotonic() + 100
            while True:
                health = api('/api/health')
                (root / 'health.json').write_text(json.dumps(health, indent=2) + '\n')
                if health['ai']['available']:
                    assert health['packaged'] and health['ai']['managed']
                    return health
                assert health['ai']['state'] in ('busy', 'checking'), health['ai']
                assert time.monotonic() < deadline, 'Frozen app AI check timed out'
                time.sleep(0.2)

        with (root / 'server.log').open('w') as log:
            server = subprocess.Popen(
                [str(executable), 'serve', '--port', str(port)], cwd=root, env=env, stdout=log, stderr=log
            )
            deadline = time.monotonic() + 30
            while True:
                assert server.poll() is None, 'Frozen server exited'
                try:
                    api('/api/ping')
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            ready_health()
            assert api('/api/system/install-ai', 'POST')['ok']
            api('/api/system/ai', 'DELETE')
            assert not (data / 'ai').exists()
            assert not api('/api/health')['ai']['available']
            assert marker.read_text() == 'library is independent' and all(path.is_file() for path in saved)
            results.append('frozen-health-idempotent-install-and-removal')
            install()
            ready_health()
            api('/api/system/ai', 'DELETE')
            assert not (data / 'ai').exists() and all(path.is_file() for path in saved)
            results.append('reinstall-and-clean-removal-preserve-output-and-library')
    finally:
        manager.shutdown()
        fixture.shutdown()
        fixture.server_close()
        fixture_thread.join(5)
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    report = {
        'ok': True,
        'checks': results,
        'transport': 'localhost fixture before publication',
        'app_external_python': False,
        'model': 'htdemucs',
        'audio_seconds': 3,
        'best_warnings': enhanced['warnings'],
    }
    (root / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
