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


def write_lowmid_fixture(path: Path) -> None:
    """Generate a short, isolated low-mid rise against a steady stereo baseline."""
    rate = 44100
    duration = 16
    frames = bytearray()
    for frame in range(duration * rate):
        t = frame / rate
        fade = min(1.0, t / 0.05, (duration - t) / 0.05)
        lowmid = 0.175 if 6.3 <= t < 7.3 else 0.015
        sample = 0.09 * math.sin(math.tau * 1000 * t) + lowmid * math.sin(math.tau * 300 * t)
        value = round(32767 * fade * sample)
        frames.extend(struct.pack('<hh', value, value))
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(frames)


def check_enhance_output(stdout: str, label: str, formats: set[str]) -> dict:
    result = json.loads(stdout)
    files = [file for file in result['files'] if file['kind'] == 'audio']
    assert len(files) == len(formats) and {file['format'] for file in files} == formats, (
        f'{label}: missing or unexpected audio exports'
    )
    for file in files:
        check = file.get('verification', {})
        name = file['name']
        assert check.get('status') in {'verified', 'warning'}, f'{label}: export verification unavailable for {name}'
        for field in ('integrated_lufs', 'true_peak_dbtp'):
            reading = check.get(field)
            assert type(reading) in (int, float) and math.isfinite(reading), f'{label}: invalid {field} for {name}'
    return result


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
    check_enhance_output(run('enhance', 'enhance', str(tone), '--formats', 'wav16', '--json'), 'enhance', {'wav16'})
    lowmid = root / 'lowmid-rise.wav'
    write_lowmid_fixture(lowmid)
    result = check_enhance_output(
        run('enhance-adaptive', 'enhance', str(lowmid), '--type', 'music', '--formats', 'wav16', 'mp3_320', '--json'),
        'enhance-adaptive',
        {'wav16', 'mp3_320'},
    )
    processing = result['processing']
    assert processing['params']['adaptive_lowmid_db'] > 0, 'Adaptive low-mid settings were not resolved'
    assert processing['applied']['adaptive_lowmid']['engaged'] is True, 'Adaptive low-mid control did not engage'
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
