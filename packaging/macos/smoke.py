"""Smoke-test the actual frozen app with generated audio and no external Python on PATH."""

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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    parser.add_argument('evidence', type=Path)
    args = parser.parse_args()
    app = args.app.resolve()
    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=False)
    executable = app / 'Contents/MacOS/AudioLab'
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'AUDIOLAB_FFMPEG', 'AUDIOLAB_TOKEN'):
        env.pop(key, None)
    env.update(
        PATH='/usr/bin:/bin',
        AUDIOLAB_DATA_DIR=str(root / 'data'),
        AUDIOLAB_OUTPUT_DIR=str(root / 'output'),
    )
    results = []

    def run(label, *argv):
        completed = subprocess.run(
            [str(executable), *argv], env=env, cwd=root, capture_output=True, text=True, timeout=120
        )
        (root / f'{label}.log').write_text(completed.stdout + '\n' + completed.stderr)
        assert completed.returncode == 0, f'{label} failed; see {root / f"{label}.log"}'
        results.append(label)
        return completed.stdout

    tone = root / 'tone.wav'
    with wave.open(str(tone), 'wb') as output:
        output.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
        output.writeframes(
            b''.join(
                struct.pack('<hh', *(2 * [int(4500 * math.sin(2 * math.pi * 440 * frame / 44100))]))
                for frame in range(3 * 44100)
            )
        )
    run('version', '--version')
    run('downloader', '--audiolab-downloader', '--version')
    run('doctor', 'doctor', '--report')
    for fmt in ('mp3', 'm4a', 'flac', 'wav', 'opus'):
        run(f'convert-{fmt}', 'download', str(tone), '--format', fmt, '--json')
    run('enhance', 'enhance', str(tone), '--formats', 'wav16', '--json')
    run('analyze', 'analyze', str(tone), '--json')

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    fixture = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(root)))
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    try:
        run(
            'save-local-link',
            'download',
            f'http://127.0.0.1:{fixture.server_port}/tone.wav',
            '--format',
            'mp3',
            '--json',
        )
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=5)

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    with (root / 'server.log').open('w') as log:
        server = subprocess.Popen(
            [str(executable), 'serve', '--port', str(port)], env=env, cwd=root, stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert server.poll() is None, 'The packaged HTTP server exited early'
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
                        health = json.load(response)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            assert health['packaged'] and health['ffmpeg']['ok']
            assert health['ffmpeg']['source'] == 'bundled'
            assert health['downloader']['ok'] and not health['downloader']['update_supported']
            assert not health['ai']['available'] and health['ai']['install_supported']
            assert health['ai']['managed'] and health['ai']['download_bytes'] > 0
            for route in ('/', '/js/main.js', '/css/tokens.css'):
                with urllib.request.urlopen(f'http://127.0.0.1:{port}{route}', timeout=5) as response:
                    assert response.status == 200 and response.read()
            (root / 'health.json').write_text(json.dumps(health, indent=2) + '\n')
            results.append('http-health-and-ui-assets')
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
                raise
    assert not list((root / 'data/cache/tmp').iterdir()), 'Temporary work was left after successful operations'
    (root / 'results.json').write_text(json.dumps({'ok': True, 'checks': results}, indent=2) + '\n')
    print(json.dumps({'ok': True, 'checks': results}))


if __name__ == '__main__':
    main()
