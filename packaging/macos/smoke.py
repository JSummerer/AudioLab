"""Smoke-test the actual frozen app with generated audio and no external Python on PATH."""

import argparse
import functools
import hashlib
import http.server
import io
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

LOWMID_SECONDS = 16


def write_lowmid_fixture(path: Path) -> None:
    """Generate a short, isolated low-mid rise against a steady stereo baseline."""
    rate = 44100
    frames = bytearray()
    for frame in range(LOWMID_SECONDS * rate):
        t = frame / rate
        fade = min(1.0, t / 0.05, (LOWMID_SECONDS - t) / 0.05)
        lowmid = 0.175 if 6.3 <= t < 7.3 else 0.015
        sample = 0.09 * math.sin(math.tau * 1000 * t) + lowmid * math.sin(math.tau * 300 * t)
        value = round(32767 * fade * sample)
        frames.extend(struct.pack('<hh', value, value))
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(frames)


def check_enhance_output(result: dict, label: str, formats: set[str]) -> None:
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


def check_timeline(timeline: dict, duration_s: float) -> None:
    assert timeline['duration_s'] == duration_s, 'Reduction timeline uses the wrong duration'
    assert 0 < len(timeline['bins']) <= 120, 'Expected bounded reduction highlights for the low-mid rise'
    previous_end = 0
    for item in timeline['bins']:
        assert previous_end <= item['start_s'] <= item['peak_s'] < item['end_s'] <= duration_s, item
        assert math.isfinite(item['max_gr_db']) and item['max_gr_db'] >= 0.1, item
        previous_end = item['end_s']


def check_library_workflow(base: str, fixture: Path) -> dict:
    def request(path, *, data=None, content_type='application/json', method=None):
        headers = {'Content-Type': content_type} if data is not None else {}
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.read()

    def api(path, body=None, *, method=None):
        data = json.dumps(body).encode() if body is not None else None
        return json.loads(request(path, data=data, method=method))

    def wait_result(job_id):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = api(f'/api/jobs/{job_id}')
            if job['status'] == 'done':
                return job['result']
            assert job['status'] in {'queued', 'running'}, job.get('error')
            time.sleep(0.2)
        raise AssertionError(f'Frozen HTTP job {job_id} did not finish')

    formats = ['wav16', 'mp3_320']
    options = json.dumps({'recipe': 'music', 'quality': 'standard', 'formats': formats, 'embed_cover': False})
    boundary = 'AudioLabFrozenSmokeFixture'
    audio = fixture.read_bytes()
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="task"\r\n\r\nenhance\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="options"\r\n\r\n{options}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fixture.name}"\r\n'
        'Content-Type: audio/wav\r\n\r\n'
    ).encode()
    body = head + audio + f'\r\n--{boundary}--\r\n'.encode()
    (initial,) = json.loads(request('/api/jobs', data=body, content_type=f'multipart/form-data; boundary={boundary}'))[
        'jobs'
    ]
    initial_id = initial['id']
    result = wait_result(initial_id)
    assert result['library_id'] == initial_id
    timeline = result['processing']['applied']['adaptive_lowmid']['timeline']
    check_timeline(timeline, LOWMID_SECONDS)
    api(f'/api/jobs/{initial_id}', method='DELETE')

    entry_url = f'/api/library/{initial_id}'
    entry = api(entry_url)
    assert entry['original']['kept'] and not entry['job_exists']
    assert entry['processing']['applied']['adaptive_lowmid']['timeline'] == timeline
    original_hash = hashlib.sha256(audio).hexdigest()
    assert hashlib.sha256(request(entry_url + '/files/original')).hexdigest() == original_hash
    assert api('/api/library')['count'] == 1

    advanced = {'target_lufs': -20, 'sample_rate': 44100}
    excerpt = {'start_s': 4, 'duration_s': 5}
    trial_id = api(entry_url + '/preview', {**excerpt, 'advanced': advanced})['id']
    preview = wait_result(trial_id)
    assert preview['preview'] and preview['files'] == [] and 'library_id' not in preview
    assert preview['excerpt'] == excerpt and preview['processing']['params']['target_lufs'] == advanced['target_lufs']
    preview_timeline = preview['processing']['applied']['adaptive_lowmid']['timeline']
    check_timeline(preview_timeline, excerpt['duration_s'])
    pcm = {key: request(f'/api/jobs/{trial_id}/pcm/{key}?frames={6 * 48000}') for key in ('original', 'enhanced')}
    # Ask for six seconds. Both streams must end at five seconds of stereo float32 PCM.
    assert all(len(samples) == excerpt['duration_s'] * 48000 * 2 * 4 and any(samples) for samples in pcm.values())
    assert pcm['original'] != pcm['enhanced'], 'Preview processing did not change the fixture'
    preview_count = api('/api/library')['count']
    assert preview_count == 1 and api(entry_url) == entry
    assert hashlib.sha256(request(entry_url + '/files/original')).hexdigest() == original_hash

    applied_id = api(entry_url + '/enhance', {'advanced': advanced})['id']
    applied = wait_result(applied_id)
    assert applied['library_id'] == applied_id and 'preview' not in applied
    check_enhance_output(applied, 'Library Apply', set(formats))
    assert all(file['exists'] and file['size_bytes'] > 0 for file in applied['files'])
    saved = api(f'/api/library/{applied_id}')
    assert saved['group'] == entry['group'] and saved['output_dir'] != entry['output_dir']
    assert saved['processing']['requested']['advanced'] == advanced
    params = saved['processing']['resolved']['params']
    assert all(params[key] == value for key, value in advanced.items())
    library = api('/api/library')
    (group,) = library['groups']
    assert library['count'] == 2 and {e['id'] for e in group['entries']} == {initial_id, applied_id}
    (wav,) = [file for file in applied['files'] if file['format'] == 'wav16']
    with wave.open(io.BytesIO(request(f'/api/jobs/{applied_id}/files/{wav["id"]}')), 'rb') as export:
        sample_rate = export.getframerate()
        duration_s = export.getnframes() / sample_rate
        assert sample_rate == advanced['sample_rate'] and duration_s == LOWMID_SECONDS
    for entry_id in (initial_id, applied_id):
        assert hashlib.sha256(request(f'/api/library/{entry_id}/files/original')).hexdigest() == original_hash
    return {
        'retained_timeline_bins': len(timeline['bins']),
        'preview_timeline_bins': len(preview_timeline['bins']),
        'excerpt': preview['excerpt'],
        'pcm_bytes': {key: len(samples) for key, samples in pcm.items()},
        'versions_after_preview': preview_count,
        'versions_after_apply': library['count'],
        'applied_advanced': saved['processing']['requested']['advanced'],
        'applied_wav': {'sample_rate': sample_rate, 'duration_s': duration_s},
        'export_status': {file['format']: file['verification']['status'] for file in applied['files']},
        'original_sha256': original_hash,
    }


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
    check_enhance_output(
        json.loads(run('enhance', 'enhance', str(tone), '--formats', 'wav16', '--json')), 'enhance', {'wav16'}
    )
    lowmid = root / 'lowmid-rise.wav'
    write_lowmid_fixture(lowmid)
    result = json.loads(
        run('enhance-adaptive', 'enhance', str(lowmid), '--type', 'music', '--formats', 'wav16', 'mp3_320', '--json'),
    )
    check_enhance_output(result, 'enhance-adaptive', {'wav16', 'mp3_320'})
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
    base = f'http://127.0.0.1:{port}'
    with (root / 'server.log').open('w') as log:
        server = subprocess.Popen(
            [str(executable), 'serve', '--port', str(port)], env=env, cwd=root, stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                assert server.poll() is None, 'The packaged HTTP server exited early'
                try:
                    with urllib.request.urlopen(base + '/api/health', timeout=2) as response:
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
            for route in ('/', '/js/main.js', '/js/quick-preview.js', '/css/tokens.css'):
                with urllib.request.urlopen(base + route, timeout=5) as response:
                    assert response.status == 200 and response.read()
            (root / 'health.json').write_text(json.dumps(health, indent=2) + '\n')
            results.append('http-health-and-ui-assets')
            library_preview = check_library_workflow(base, lowmid)
            results.append('http-library-retained-timeline-preview-and-apply')
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
                raise
    assert not list((root / 'data/cache/tmp').iterdir()), 'Temporary work was left after successful operations'
    report = {'ok': True, 'checks': results, 'library_preview': library_preview}
    (root / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
