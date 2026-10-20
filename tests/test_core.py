"""Core plumbing: helpers, settings, progress/cancel, preflight, job queue, HTTP API.

Uses the per-test data/output folders from conftest and a fake task runner, so it
needs no network or audio models.
"""

import errno
import io
import os
import sys
import time
import wave
import zipfile
from pathlib import Path

import pytest

from audiolab.context import Progress, TaskContext
from audiolab.errors import AudioLabError, Cancelled, ResourceGuardError
from audiolab.jobs import JobManager
from audiolab.resources import estimate_peak_bytes, preflight
from audiolab.settings import SETTINGS
from audiolab.util import human_duration, sanitize_filename, unique_path

STAGES = [('a', 'First', 1), ('b', 'Second', 3)]


def wait_for(fn, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.02)
    raise AssertionError('condition not reached in time')


# ----------------------------------------------------------------- helpers
def test_sanitize_and_unique_path(tmp_path):
    assert sanitize_filename('../../etc/passwd') == 'etc passwd'
    assert sanitize_filename('a:b*c?"d<e>|f') == 'a b c d e f'
    assert sanitize_filename('   ...   ') == 'Untitled'
    assert sanitize_filename('CON') == '_CON'
    p = tmp_path / 'song.mp3'
    assert unique_path(p) == p
    p.write_text('x')
    assert unique_path(p).name == 'song (2).mp3'
    assert human_duration(3725) == '1:02:05' and human_duration(95) == '1:35'


def test_settings_roundtrip(tmp_path):
    s = SETTINGS.update(
        {'download_format': 'flac', 'ui_mode': 'advanced', 'nonsense': 1, 'enhance_formats': ['mp3_320']}
    )
    assert s.download_format == 'flac' and s.enhance_formats == ('mp3_320',)
    pub = SETTINGS.public()
    assert pub['output_dir'] == str(tmp_path / 'out') and 'output_dir' in pub['locked']
    assert 'nonsense' not in pub
    assert not (tmp_path / 'out').exists()  # reading the output folder never creates it


def test_settings_are_checked(tmp_path):
    assert SETTINGS.update({'keep_originals': 'false', 'embed_cover': 0}).keep_originals is False
    assert SETTINGS.get().embed_cover is False
    assert SETTINGS.update({'embed_cover': 'True', 'keep_work_files': 1}).embed_cover is True
    s = SETTINGS.update({'enhance_formats': ['flac24', 'mp3_320', 'flac24'], 'separate_format': 'wav24'})
    assert s.enhance_formats == ('flac24', 'mp3_320') and s.separate_format == 'wav24'
    for bad, message in [
        ({'embed_cover': 'maybe'}, 'true or false'),
        ({'keep_originals': None}, 'true or false'),
        ({'download_format': 'mp4'}, '"mp4" is not a valid choice for download_format'),
        ({'enhance_formats': []}, 'at least one'),
        ({'enhance_formats': 'mp3_320'}, 'must be a list'),
        ({'enhance_formats': ['mp3_320', 'bogus']}, '"bogus"'),
        ({'separate_format': 'opus_160'}, 'separate_format'),  # an Enhance format, not a Split one
        ({'theme': 'pink'}, 'theme'),
        ({'theme': 'dark', 'download_format': 7}, 'must be text'),  # nothing is applied
    ]:
        with pytest.raises(ValueError, match=message):
            SETTINGS.update(bad)
    assert SETTINGS.get().theme == 'auto' and SETTINGS.get().embed_cover is True


def test_settings_api_refuses_bad_values(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    monkeypatch.delenv('AUDIOLAB_OUTPUT_DIR')
    SETTINGS.update({'output_dir': str(tmp_path / 'music')})
    with TestClient(create_app(local=False, token='t', manager=new_manager(tmp_path / 'jobs'))) as c:
        h = {'X-AudioLab-Token': 't'}
        assert c.put('/api/settings', headers=h, json={'keep_originals': 'false'}).json()['keep_originals'] is False
        r = c.put('/api/settings', headers=h, json={'download_format': 'nope'})
        assert r.status_code == 400 and 'download_format' in r.json()['detail']
        r = c.put('/api/settings', headers=h, json={'output_dir': 'Music/AudioLab'})  # relative to what?
        assert r.status_code == 400 and r.json()['detail'] == 'Enter a full folder path, for example ~/Music/AudioLab.'
        assert not Path('Music/AudioLab').exists() and SETTINGS.output_dir == tmp_path / 'music'


@pytest.mark.skipif(os.name != 'posix' or os.geteuid() == 0, reason='needs POSIX permissions (and not root)')
def test_unreachable_output_folder(tmp_path, monkeypatch):
    """An output folder on a drive that isn't connected: the app keeps working and Settings can fix it."""
    from fastapi.testclient import TestClient

    from audiolab import system
    from audiolab.api import create_app

    monkeypatch.setattr(system, 'reveal', lambda path: None)
    monkeypatch.delenv('AUDIOLAB_OUTPUT_DIR')
    drive = tmp_path / 'drive'  # like /Volumes: there, but nobody may create folders in it
    drive.mkdir()
    gone = drive / 'Music' / 'AudioLab'
    SETTINGS.update({'output_dir': str(gone)})
    drive.chmod(0o555)
    try:
        app = create_app(local=True, manager=new_manager(tmp_path / 'jobs'))
        with TestClient(app, base_url='http://127.0.0.1:8765') as c:
            health = c.get('/api/health')
            assert health.status_code == 200 and health.json()['output_dir'] == str(gone)
            assert health.json()['output_dir_ok'] is False
            assert c.get('/api/settings').json()['output_dir'] == str(gone)
            assert c.put('/api/settings', json={'theme': 'dark'}).status_code == 200
            r = c.post('/api/reveal-output')
            assert r.status_code == 400 and r.json()['detail'] == f"The output folder isn't available: {gone}."
            with pytest.raises(AudioLabError, match="output folder isn't available") as refused:
                preflight(60.0, tmp_path, 'enhance', output_dir=SETTINGS.output_dir)
            assert 'Settings → Files' in refused.value.hints[0]
            preflight(60.0, tmp_path, 'analyze', output_dir=SETTINGS.output_dir)  # Check writes nothing there
            fixed = c.put('/api/settings', json={'output_dir': str(tmp_path / 'music')})
            assert fixed.status_code == 200 and fixed.json()['output_dir'] == str(tmp_path / 'music')
            assert c.get('/api/health').json()['output_dir_ok'] is True
            assert c.post('/api/reveal-output').json() == {'ok': True}
        assert not gone.exists()
    finally:
        drive.chmod(0o755)


# ------------------------------------------------------- progress & cancel
def test_progress_maps_stages_and_cancels(tmp_path):
    seen = []
    ctx = TaskContext('t', tmp_path / 'ctx', {}, STAGES, emit_state=lambda st, pct, msg: seen.append((st, pct)))
    p = ctx.stage('a')
    p(100)
    assert abs(seen[-1][1] - 25.0) < 1e-6
    q = ctx.stage('b').sub(50, 100)
    q(50)
    assert seen[-1][0] == 'b' and abs(seen[-1][1] - (25 + 75 * 0.75)) < 1e-6
    ctx.cancel()
    with pytest.raises(Cancelled):  # progress calls after a cancel stop the task
        q(60)
    Progress.null()(50)  # no-op reporter never raises


def test_cancel_kills_attached_process(tmp_path):
    import subprocess

    from audiolab.system import own_process_group

    ctx = TaskContext('t', tmp_path / 'ctx', {}, STAGES)
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], **own_process_group())
    ctx.progress.attach_process(proc)
    ctx.cancel()
    assert proc.wait(timeout=5) is not None


# ---------------------------------------------------------------- preflight
def test_preflight(tmp_path):
    one_h, two_h = estimate_peak_bytes(3600, 'enhance'), estimate_peak_bytes(7200, 'enhance')
    assert two_h > one_h and two_h - one_h > 0.5 * one_h  # grows with length (+ fixed overhead)
    assert estimate_peak_bytes(3600, 'enhance', 'best') > one_h
    assert estimate_peak_bytes(7200, 'download') < estimate_peak_bytes(60, 'enhance') * 10
    rep = preflight(180.0, tmp_path, 'download')
    assert rep['task'] == 'download'
    with pytest.raises(ResourceGuardError, match='long') as refused:  # a 10-day job
        preflight(10 * 24 * 3600.0, tmp_path, 'enhance')
    assert refused.value.hints


# --------------------------------------------------------------- job queue
def fake_runner(job, ctx):
    mode = job.options.get('mode')
    p = ctx.stage('a', 'working')
    if mode == 'fail':
        raise AudioLabError('Nope.', hints=['try X'], fix='retry')
    if mode == 'crash':
        raise ValueError('boom')
    if mode == 'disk_full':
        raise OSError(errno.ENOSPC, 'No space left on device', '/out/Song.wav')
    if mode == 'read_only':
        raise OSError(errno.EROFS, 'Read-only file system', '/Volumes/Stick/Song.wav')
    if mode == 'detail':
        raise AudioLabError('Plain words.', fix='retry', detail='raw tool output')
    if mode == 'slow':
        for i in range(500):
            p(i / 5)
            time.sleep(0.01)
    ctx.stage('b')(100)
    files = []
    if mode == 'files':
        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        for i, (name, group) in enumerate([('Song.mp3', None), ('01 - Intro.mp3', 'tracks'), ('Song.mp3', 'tracks')]):
            path = ctx.work_dir / f'{i}_{name}'
            path.write_bytes(name.encode() * 1000)
            files.append({'id': f'f{i}', 'name': name, 'path': str(path), 'group': group})
            if group is None:
                files[-1].pop('group')
    return {'headline': 'All good', 'files': files, 'summary': ['ok'], '_media': {'x': '/nope'}}


def new_manager(root):
    return JobManager(runner=fake_runner, stages_for=lambda job: STAGES, root=root, workers=1)


@pytest.fixture
def manager(tmp_path):
    m = new_manager(tmp_path / 'jobs')
    m.start()
    yield m
    m.shutdown()


def test_job_lifecycle(manager):
    m = manager
    events = []
    m.subscribe(lambda ev, payload: events.append((ev, payload.get('status'))))

    ok = m.submit('analyze', {'kind': 'url', 'url': 'https://example.com/x'}, {})
    wait_for(lambda: m.get(ok.id).status == 'done')
    pub = m.public(ok.id)
    assert pub['progress'] == 100.0 and pub['result']['headline'] == 'All good'
    assert '_media' not in pub['result'] and pub['stages'][0]['label'] == 'First'

    bad = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {'mode': 'fail'})
    wait_for(lambda: m.get(bad.id).status == 'failed')
    assert m.get(bad.id).error == {'message': 'Nope.', 'hints': ['try X'], 'fix': 'retry'}

    crash = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {'mode': 'crash'})
    wait_for(lambda: m.get(crash.id).status == 'failed')
    assert 'went wrong' in m.get(crash.id).error['message']
    assert any(e['kind'] == 'error' for e in m.journal(crash.id))
    assert 'Copy details for a bug report' in m.get(crash.id).error['hints'][1]

    # a full disk or a read-only drive: plain words, no "Try again" as the remedy, the raw error as detail
    for mode, message in (('disk_full', 'The disk is full.'), ('read_only', "AudioLab can't write there.")):
        failed = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {'mode': mode})
        wait_for(lambda j=failed: m.get(j.id).status == 'failed')
        error = m.get(failed.id).error
        assert error['message'] == message and error['fix'] is None, error
        assert 'Settings → Files' in ' '.join(error['hints']) and 'Errno' in error['detail']
    detailed = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {'mode': 'detail'})
    wait_for(lambda: m.get(detailed.id).status == 'failed')
    assert m.get(detailed.id).error == {
        'message': 'Plain words.',
        'hints': [],
        'fix': 'retry',
        'detail': 'raw tool output',
    }

    slow = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {'mode': 'slow'})
    queued = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {})
    wait_for(lambda: m.get(slow.id).status == 'running')
    assert m.public(queued.id)['queue_position'] == 1
    m.cancel(slow.id)
    wait_for(lambda: m.get(slow.id).status == 'cancelled')
    wait_for(lambda: m.get(queued.id).status == 'done')

    m.retry(bad.id)
    wait_for(lambda: m.get(bad.id).status == 'failed')
    assert ('job', 'done') in events
    removed = m.clear_finished()
    assert ok.id in removed and not m.job_dir(ok.id).exists()


def test_shutdown_settles_running_and_queued_jobs(tmp_path):
    import json

    root = tmp_path / 'jobs'
    m = new_manager(root)
    m.start()
    running = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {'mode': 'slow'})
    queued = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {})
    wait_for(lambda: m.get(running.id).status == 'running')
    t0 = time.monotonic()
    m.shutdown(timeout=5)
    assert time.monotonic() - t0 < 5
    m.shutdown()  # a second call (window closed, then the server stops) is harmless
    for late in (lambda: m.submit('analyze', {'kind': 'url', 'url': 'u'}, {}), lambda: m.retry(running.id)):
        with pytest.raises(AudioLabError, match='closing'):  # nothing would run it any more
            late()
    on_disk = {j.id: json.loads((root / j.id / 'job.json').read_text()) for j in (running, queued)}
    assert on_disk[running.id]['status'] == 'cancelled' and '"Try again"' in on_disk[running.id]['message']
    assert on_disk[queued.id]['status'] == 'cancelled' and on_disk[queued.id]['message'].startswith('Not started')
    assert not any(t.is_alive() for t in m._threads)
    again = new_manager(root)  # the instance lock was released
    again.start()
    assert again.get(running.id).status == 'cancelled'
    again.shutdown()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signals')
def test_shutdown_kills_a_stubborn_child(tmp_path):
    """A tool that ignores SIGTERM gets the forced kill before shutdown returns."""
    import subprocess

    from audiolab.system import own_process_group

    procs = []

    def runner(job, ctx):
        code = (
            'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(1, flush=True); time.sleep(60)'
        )
        proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, **own_process_group())
        proc.stdout.readline()  # SIGTERM is ignored from here on
        procs.append(proc)
        ctx.progress.attach_process(proc)
        while proc.poll() is None:  # busy without checking for cancellation
            time.sleep(0.05)
        return {}

    m = JobManager(runner=runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    job = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {})
    wait_for(lambda: procs)
    m.shutdown(timeout=0.5)
    assert procs[0].poll() is not None
    assert m.get(job.id).status == 'cancelled'


def test_restart_marks_interrupted_jobs(tmp_path):
    import json
    from dataclasses import asdict

    from audiolab.jobs import Job

    root = tmp_path / 'jobs'
    running = Job(id='j_20260101_000000_aaaaaa', task='analyze', source={}, options={}, status='running')
    kept = Job(id='j_20260101_000002_cccccc', task='analyze', source={}, options={}, status='running')
    queued = Job(id='j_20260101_000001_bbbbbb', task='analyze', source={}, options={}, status='queued')
    for job in (running, queued):  # what a crashed session leaves on disk
        (root / job.id).mkdir(parents=True)
        (root / job.id / 'job.json').write_text(json.dumps(asdict(job)))
    scratch = root / running.id / 'work' / 'scratch'
    scratch.mkdir(parents=True)
    (scratch / 'reference.wav').write_bytes(b'big' * 1000)  # gigabytes in real life
    (root / running.id / 'work' / 'previews').mkdir()
    m = new_manager(root)
    m._load()
    assert m.get(running.id).status == 'failed' and m.get(running.id).error['fix'] == 'retry'
    assert '"Try again"' in m.get(running.id).error['hints'][0]
    assert m.get(queued.id).status == 'cancelled' and '"Try again"' in m.get(queued.id).message
    assert not scratch.exists() and (root / running.id / 'work' / 'previews').is_dir()

    SETTINGS.update({'keep_work_files': True})  # kept on purpose: left for a look
    (root / kept.id / 'work' / 'scratch').mkdir(parents=True)
    (root / kept.id / 'job.json').write_text(json.dumps(asdict(kept)))
    new_manager(root)._load()
    assert (root / kept.id / 'work' / 'scratch').is_dir()


# --------------------------------------------------------------------- API
def _wav_bytes(seconds=1.0, sr=48000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b'\x00\x00' * 2 * int(seconds * sr))
    return buf.getvalue()


def test_http_api(tmp_path):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    mgr = new_manager(tmp_path / 'jobs')
    app = create_app(local=False, token='secret', manager=mgr)
    with TestClient(app) as c:
        assert c.get('/api/ping').status_code == 200
        assert c.get('/api/jobs').status_code == 401
        h = {'X-AudioLab-Token': 'secret'}
        health = c.get('/api/health', headers=h).json()
        assert health['app'] and 'download' in health['formats'] and health['local'] is False
        r = c.post(
            '/api/jobs',
            headers=h,
            data={'task': 'analyze', 'options': '{}'},
            files={'file': ('My Song.wav', _wav_bytes(), 'audio/wav')},
        )
        assert r.status_code == 200, r.text
        job = r.json()['jobs'][0]
        assert job['source']['name'] == 'My Song.wav' and 'path' not in job['source']
        wait_for(lambda: c.get(f'/api/jobs/{job["id"]}', headers=h).json()['status'] == 'done')
        assert c.get(f'/api/jobs/{job["id"]}/journal?token=secret').json()['events']
        again = c.post(
            '/api/jobs', headers=h, data={'task': 'analyze', 'options': '{}', 'source_job': job['id']}
        ).json()['jobs'][0]
        assert again['source']['name'] == 'My Song.wav'
        assert (
            c.post(
                '/api/jobs',
                headers=h,
                data={'task': 'analyze'},
                files={'file': ('evil.exe', b'MZ', 'application/octet-stream')},
            ).status_code
            == 400
        )
        assert c.post('/api/jobs', headers=h, data={'task': 'nope', 'url': 'https://x.y'}).status_code == 400
        assert c.post(f'/api/jobs/{job["id"]}/reveal', headers=h).status_code == 403
        assert c.get('/api/jobs/j_bad/media/x', headers=h).status_code == 404
        s = c.put('/api/settings', headers=h, json={'theme': 'dark'}).json()
        assert s['theme'] == 'dark'
        named = c.post(
            '/api/jobs',
            headers=h,
            data={
                'task': 'analyze',
                'source_job': job['id'],
                'options': '{"meta": {"title": "  My   Title ", "x": 1}}',
            },
        )
        assert named.json()['jobs'][0]['title'] == 'My Title'
        assert named.json()['jobs'][0]['options']['meta'] == {'title': 'My Title'}

        zjob = mgr.submit('analyze', {'kind': 'file', 'name': 'x.wav'}, {'mode': 'files'}, title='Zip me')
        wait_for(lambda: mgr.get(zjob.id).status == 'done')
        z = c.get(f'/api/jobs/{zjob.id}/zip', headers=h)
        assert z.status_code == 200 and 'Zip me.zip' in z.headers['content-disposition']
        with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
            assert zf.testzip() is None
            assert sorted(zf.namelist()) == ['Song.mp3', 'Tracks/Song.mp3', 'Tracks/01 - Intro.mp3'] or sorted(
                zf.namelist()
            ) == sorted(['0_Song.mp3', 'Tracks/1_01 - Intro.mp3', 'Tracks/2_Song.mp3'])
        assert c.get(f'/api/jobs/{job["id"]}/zip', headers=h).status_code == 404  # no files
        st = c.get('/api/storage', headers=h).json()
        assert st['job_count'] >= 1 and st['jobs_bytes'] > 0
        report = c.get(f'/api/diagnostics?job={job["id"]}', headers=h).text
        assert 'diagnostics' in report and job['id'] in report and 'secret' not in report
        assert c.get('/api/health', headers=h).json()['downloader'].keys() >= {'version', 'age_days'}

        assert c.delete('/api/jobs?scope=finished', headers=h).json()['deleted']
        assert c.get('/').status_code == 200


def test_diagnostics_redaction():
    from audiolab.diagnostics import _private

    text = _private(
        f'https://u:pw@cdn.example/a.mp3?token=abc&v=1&X-Amz-Signature=zz {Path.home()}/Music '
        'https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PL1'
    )
    assert 'pw' not in text and 'abc' not in text and 'zz' not in text and str(Path.home()) not in text
    assert 'v=1' in text and 'watch?v=jNQXAC9IVRw&list=PL1' in text and '~/Music' in text


def test_browser_safety(tmp_path):
    """Local mode has no token: other websites and DNS-rebinding hosts must be refused."""
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    mgr = new_manager(tmp_path / 'jobs')
    with TestClient(create_app(local=True, manager=mgr), base_url='http://127.0.0.1:8765') as c:
        assert c.get('/api/jobs').status_code == 200
        assert c.get('/api/jobs', headers={'Host': 'localhost:8765'}).status_code == 200
        assert c.get('/api/jobs', headers={'Host': 'evil.example:8765'}).status_code == 403
        assert c.get('/', headers={'Host': 'evil.example'}).status_code == 403
        same = {'Origin': 'http://127.0.0.1:8765'}
        assert c.put('/api/settings', headers=same, json={'theme': 'light'}).status_code == 200
        for bad in ('https://evil.example', 'null', 'http://127.0.0.1:3000'):
            r = c.post('/api/jobs', headers={'Origin': bad}, data={'task': 'analyze', 'url': 'https://x.y/z'})
            assert r.status_code == 403, (bad, r.status_code)


def test_queue_positions_sent_only_when_they_change(tmp_path):
    """A playlist of N items costs O(N) events, not one event per queued job per item added."""
    import threading

    gate = threading.Event()

    def runner(job, ctx):
        gate.wait(10)
        return {}

    m = JobManager(runner=runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    try:
        first = m.submit('analyze', {'kind': 'url', 'url': 'u'}, {})
        wait_for(lambda: m.get(first.id).status == 'running')
        events: list[dict] = []
        m.subscribe(lambda ev, payload: events.append(payload) if ev == 'job' else None)
        n = 60
        items = [m.submit('analyze', {'kind': 'url', 'url': f'u{i}'}, {}) for i in range(n)]
        assert len(events) == n  # one each (it used to be n * (n + 1) / 2)
        assert [e['queue_position'] for e in events] == list(range(1, n + 1))
        m.cancel(items[0].id)  # everyone behind it moves up: told once each
        moved = [e for e in events[n:] if e['id'] != items[0].id]
        assert [e['queue_position'] for e in moved] == list(range(1, n))
        assert m.public(items[-1].id)['queue_position'] == n - 1
    finally:
        gate.set()
        m.shutdown()


def test_link_job_takes_the_title_of_its_lookup(tmp_path, monkeypatch):
    """A link started without a known title (the lookup expired) shows the real one once it is looked up."""
    from audiolab.media import youtube
    from audiolab.tasks import common

    looked_up = {'kind': 'single', 'url': 'https://youtu.be/x', 'title': 'Real Title', 'duration_s': 60.0}
    monkeypatch.setattr(youtube, 'probe', lambda url, progress=None: dict(looked_up))

    def runner(job, ctx):
        common.probe_url(job.source, ctx.stage('a'))
        ctx.stage('b')(50)
        return {}

    m = JobManager(runner=runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    titles = []
    m.subscribe(lambda ev, payload: titles.append(payload['title']) if ev == 'job' else None)
    m.start()
    try:
        job = m.submit('download', {'kind': 'url', 'url': 'https://youtu.be/x'}, {})
        assert job.title == 'https://youtu.be/x'
        wait_for(lambda: m.get(job.id).status == 'done')
        assert m.get(job.id).title == 'Real Title' and 'Real Title' in titles
        assert m.get(job.id).source['duration_s'] == 60.0
        named = m.submit('download', {'kind': 'url', 'url': 'https://youtu.be/x'}, {}, title='My Name')
        wait_for(lambda: m.get(named.id).status == 'done')
        assert m.get(named.id).title == 'My Name'  # a name the user gave stays
    finally:
        m.shutdown()


def test_retry_looks_a_link_up_again(tmp_path, monkeypatch):
    """A premiere that was still live: "Try again" after it ended must not reuse the old lookup."""
    import shutil

    from audiolab.media import ffmpeg, youtube
    from audiolab.tasks import common

    wav = tmp_path / 'a.wav'
    ffmpeg.run_ffmpeg(['-f', 'lavfi', '-i', 'sine=d=1', str(wav)])
    lookups = []

    def probe(url, progress=None):
        lookups.append(url)
        return {'kind': 'single', 'url': url, 'title': 'Premiere', 'duration_s': 1.0, 'is_live': len(lookups) == 1}

    monkeypatch.setattr(youtube, 'probe', probe)
    monkeypatch.setattr(
        youtube, 'download_audio', lambda url, dest, progress=None: Path(shutil.copy(wav, tmp_path / 'b.wav'))
    )

    def runner(job, ctx):
        common.acquire(ctx, job.source, ctx.stage('a'), task='download')
        return {}

    m = JobManager(runner=runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    try:
        job = m.submit('download', {'kind': 'url', 'url': 'https://youtu.be/live'}, {})
        wait_for(lambda: m.get(job.id).status == 'failed')
        assert 'live stream' in m.get(job.id).error['message']
        m.retry(job.id)
        wait_for(lambda: m.get(job.id).status == 'done')
        assert len(lookups) == 2
    finally:
        m.shutdown()

    from fastapi.testclient import TestClient

    from audiolab.api import create_app
    from audiolab.api.jobs import _clone_source

    app = create_app(local=False, token='t', manager=new_manager(tmp_path / 'jobs2'))
    with TestClient(app) as c:
        h = {'X-AudioLab-Token': 't'}
        lookups.clear()
        assert c.post('/api/probe', headers=h, json={'url': 'https://youtu.be/live'}).json()['is_live'] is True
        assert c.post('/api/probe', headers=h, json={'url': 'https://youtu.be/live'}).json()['is_live'] is False
        assert len(lookups) == 2  # a live result is not kept: the second request looked again
        state = app.state.audiolab
        done = state.manager.submit('analyze', {'kind': 'url', 'url': 'https://youtu.be/x', '_probe': {'id': 'x'}}, {})
        assert '_probe' not in _clone_source(state, done.id, 'j_20260101_000000_dddddd')


def test_download_format_defaults_to_the_setting(tmp_path):
    from audiolab import tasks

    link, upload = {'kind': 'url', 'url': 'https://youtu.be/x'}, {'kind': 'file', 'path': '/x.wav'}
    SETTINGS.update({'download_format': 'flac'})
    assert tasks.validate('download', {}, link)['format'] == 'flac'
    assert tasks.validate('download', {'format': 'wav'}, link)['format'] == 'wav'
    SETTINGS.update({'download_format': 'original'})
    assert tasks.validate('download', {}, link)['format'] == 'original'
    assert tasks.validate('download', {}, upload)['format'] == 'mp3'  # a file is always converted


def test_separate_format_choices(monkeypatch):
    from audiolab.tasks import separate

    monkeypatch.setattr(separate.common, 'ai_device', lambda: 'cpu')
    source = {'kind': 'file', 'path': '/x.wav'}
    assert separate.validate({'format': 'wav24'}, source)['format'] == 'wav24'
    assert separate.validate({'format': 'alac16'}, source)['format'] == 'wav16'


def test_unexpected_errors_answer_in_plain_words(tmp_path, monkeypatch):
    """No bare "Internal Server Error": a JSON 500 with hints; a full disk or a read-only folder says so."""
    from fastapi.testclient import TestClient

    from audiolab.api import create_app
    from audiolab.media import export

    mgr = new_manager(tmp_path / 'jobs')
    app = create_app(local=False, token='t', manager=mgr)
    h = {'X-AudioLab-Token': 't'}
    with TestClient(app, raise_server_exceptions=False) as c:
        for exc, message in (
            (RuntimeError('boom'), 'Something went wrong in AudioLab.'),
            (OSError(errno.ENOSPC, 'No space left on device'), 'The disk is full.'),
            (PermissionError(errno.EACCES, 'Permission denied'), "AudioLab can't write there."),
            (OSError(errno.EIO, 'Input/output error'), 'Something went wrong in AudioLab.'),
        ):

            def fail(exc=exc):
                raise exc

            monkeypatch.setattr(mgr, 'list', fail)
            r = c.get('/api/jobs', headers=h)
            assert r.status_code == 500 and r.json()['detail'] == message, r.text
            assert r.json()['hints'] and r.json()['fix'] is None
            if 'went wrong' in message:
                assert '"Copy system report"' in r.json()['hints'][-1]

        # storing an own cover on a full disk: that, not "Some of the chosen options are not valid."
        def full(*a, **k):
            raise OSError(errno.ENOSPC, 'No space left on device')

        monkeypatch.setattr(export, 'prepare_cover', full)
        r = c.post(
            '/api/jobs',
            headers=h,
            data={'task': 'enhance', 'options': '{}'},
            files={'file': ('Song.wav', _wav_bytes(), 'audio/wav'), 'cover': ('art.png', b'x' * 300, 'image/png')},
        )
        assert r.status_code == 500 and r.json()['detail'] == 'The disk is full.', r.text
        assert not list((tmp_path / 'jobs').glob('j_*'))  # nothing left behind


def test_playlist_items_keep_their_titles(tmp_path):
    """Items picked long after the lookup (cache expired) still get their title, playlist folder and album."""
    import json

    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    mgr = new_manager(tmp_path / 'jobs')
    with TestClient(create_app(local=False, token='t', manager=mgr)) as c:
        h = {'X-AudioLab-Token': 't'}
        urls = [
            {'url': 'https://youtu.be/a', 'title': '  First   Song ', 'duration_s': 201},
            'https://youtu.be/b',
            {'url': 'https://youtu.be/c', 'title': 7, 'duration_s': 'long'},  # unusable values are ignored
        ]
        r = c.post(
            '/api/jobs',
            headers=h,
            data={'task': 'analyze', 'urls': json.dumps(urls), 'playlist_title': 'My Mix'},
        )
        assert r.status_code == 200, r.text
        jobs = r.json()['jobs']
        assert [j['title'] for j in jobs] == ['First Song', 'https://youtu.be/b', 'https://youtu.be/c']
        assert {j['source']['playlist_title'] for j in jobs} == {'My Mix'}
        assert jobs[0]['source']['duration_s'] == 201 and 'duration_s' not in jobs[2]['source']
        for bad in ('{"url": "x"}', '[{"title": "no link"}]', '[1]', 'nope'):
            r = c.post('/api/jobs', headers=h, data={'task': 'analyze', 'urls': bad})
            assert r.status_code == 400 and r.json()['detail'] == 'urls must be a JSON list of links.', bad
        health = c.get('/api/health', headers=h).json()
        assert health['upload_extensions'] == sorted(health['upload_extensions'])
        assert {'.mts', '.wav', '.m4b', '.3gp'} <= set(health['upload_extensions'])
        r = c.post(
            '/api/jobs', headers=h, data={'task': 'analyze'}, files={'file': ('clip.MTS', b'\0' * 100, 'video/mp2t')}
        )
        assert r.status_code == 200, r.text


def test_unique_path_numbers_a_folder_at_the_end_of_its_name(tmp_path):
    (tmp_path / 'Song (feat. X) (Enhanced)').mkdir()
    (tmp_path / 'Song (feat. X).mp3').touch()
    assert unique_path(tmp_path / 'Song (feat. X) (Enhanced)', folder=True).name == 'Song (feat. X) (Enhanced) (2)'
    assert unique_path(tmp_path / 'Song (feat. X).mp3').name == 'Song (feat. X) (2).mp3'
