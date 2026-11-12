"""Preview and rerender from a Library original through the real job queue."""

import json
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from audiolab import ai, library, tasks
from audiolab.analysis import profile
from audiolab.api.app import create_app
from audiolab.jobs import Job, JobManager, new_job_id
from audiolab.media import ffmpeg, proc
from audiolab.recipes import resolve
from audiolab.settings import SETTINGS
from audiolab.tasks import preview

SR = 48000


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, '_POLL_S', 0.01)
    monkeypatch.setattr(ai, 'ai_status', lambda: {})
    manager = JobManager(tasks.run_job, tasks.stages_for, root=tmp_path / 'jobs', workers=1)
    with TestClient(create_app(local=False, manager=manager)) as client:
        yield client, manager


def retained(tmp_path, seconds=8, *, quality='standard', known_duration=None, varying_level=False):
    original = tmp_path / 'fixture.wav'
    with sf.SoundFile(original, 'w', SR, 2, subtype='PCM_16') as out:
        t = np.arange(SR) / SR
        for second in range(seconds):
            hz = 880 if 37 <= second < 42 else 220
            x = 0.08 * np.sin(2 * np.pi * hz * t)
            if varying_level and second < seconds // 2:
                x *= 0.1
            out.write(np.column_stack((x, x)))
    exported = SETTINGS.output_dir / 'Existing.wav'
    exported.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(original, exported)
    markers = [
        {'n': 1, 'title': 'Intro', 'start_s': 0.0, 'end_s': 2.0},
        {'n': 2, 'title': 'Second', 'start_s': 2.0, 'end_s': float(seconds)},
    ]
    source = {
        'kind': 'url',
        'url': 'https://example.invalid/fixture',
        '_probe': {'id': 'fixture', 'extractor': 'Youtube'},
    }
    opts = {
        'recipe': 'music',
        'quality': quality,
        'formats': ['wav16'],
        'advanced': {'target_lufs': -18, 'deess': 'strong'},
        'split_tracks': True,
        'embed_cover': False,
    }
    params, _ = resolve('music', {}, quality='light' if quality == 'light' else 'standard', advanced=opts['advanced'])
    job = Job(
        new_job_id(),
        'enhance',
        source,
        opts,
        result={
            'recipe': {'key': 'music', 'name': 'Song / music'},
            'processing': {'params': params.to_dict()},
            'analysis': {'bandwidth': {'is_lossy_cutoff': False}},
            'files': [{'id': 'f0', 'path': str(exported), 'name': exported.name, 'format': 'wav16'}],
            '_library': {
                'title': 'Fixture',
                'artist': 'Artist',
                'original': str(original),
                'duration_s': known_duration or seconds,
                'markers': markers,
                'markers_source': 'chapters',
            },
        },
    )
    assert library.record(job)
    return library.get(job.id), exported


def wait_result(client, job_id, expect='done'):
    until = time.monotonic() + 20
    while time.monotonic() < until:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in ('done', 'cancelled', 'failed'):
            assert job['status'] == expect, job.get('error')
            return job
        time.sleep(0.02)
    pytest.fail(f'Job {job_id} did not finish')


def test_preview_seeks_decodes_bounded_audio_and_publishes_only_excerpt(app, tmp_path, monkeypatch):
    client, manager = app
    entry, exported = retained(tmp_path, seconds=82)
    original = library.file_path(entry, 'original')
    before_original = original.read_bytes()
    before_export = exported.read_bytes()
    decode = ffmpeg.decode_reference
    called = []

    def decode_excerpt(*args, **kwargs):
        called.append(kwargs['excerpt'])
        info = decode(*args, **kwargs)
        assert info['frames'] == 45 * SR
        return info

    def forbid_full_analysis(*_args, **_kwargs):
        pytest.fail('A preview must avoid whole-recording analysis')

    monkeypatch.setattr(ffmpeg, 'decode_reference', decode_excerpt)
    monkeypatch.setattr(profile, 'analyze_file', forbid_full_analysis)
    r = client.post(
        f'/api/library/{entry["id"]}/preview',
        json={'start_s': 37, 'duration_s': 5, 'advanced': {'bass_db': 3, 'deess': 'auto'}},
    )
    assert r.status_code == 200, r.text
    job = wait_result(client, r.json()['id'])
    result = job['result']
    assert called == [(17.0, 45.0)]
    assert result['preview'] is True and result['files'] == []
    assert result['preview_basis'] == 'section_estimate'
    assert result['excerpt'] == {'start_s': 37.0, 'duration_s': 5.0}
    assert 'library_id' not in result
    assert 'path' not in job['source'] and '_preview' not in job['options'] and '_media' not in result
    assert job['options']['advanced']['target_lufs'] == -18
    assert result['processing']['params']['bass_db'] == 3
    assert result['processing']['params']['deess_db'] == 3
    assert result['metrics']['before']['lufs'] != result['metrics']['after']['lufs']
    assert set(result['metrics']) == {'before', 'after'}

    for key in ('original', 'enhanced'):
        wav = client.get(f'/api/jobs/{job["id"]}/media/{key}')
        assert wav.status_code == 200 and wav.content.startswith(b'RIFF')
        pcm = client.get(f'/api/jobs/{job["id"]}/pcm/{key}', params={'frames': SR})
        assert pcm.status_code == 200 and len(pcm.content) == 8 * SR
        if key == 'original':
            samples = np.frombuffer(pcm.content, dtype='<f4').reshape((-1, 2))[:, 0]
            hz = np.argmax(np.abs(np.fft.rfft(samples)))
            assert hz == 880  # inside the excerpt; all surrounding context uses a different tone
    assert not (manager.work_dir(job['id']) / 'scratch').exists()
    assert original.read_bytes() == before_original
    assert exported.read_bytes() == before_export
    assert client.get('/api/library').json()['count'] == 1
    assert list(SETTINGS.output_dir.iterdir()) == [exported]


def test_unchanged_preview_uses_saved_render_gain_and_timeline(app, tmp_path, monkeypatch):
    client, manager = app
    entry, exported = retained(tmp_path, seconds=64, quality='light', varying_level=True)
    original = library.file_path(entry, 'original')
    audio, _ = sf.read(original, dtype='float32', always_2d=True)
    # A saved full render uses one gain even in this quiet passage. Re-normalising
    # just that passage would make it much louder than this actual output.
    sf.write(exported, audio * 2, SR, subtype='PCM_16')
    before = exported.read_bytes()
    entry['processing']['applied'] = {
        'loudness_gain_db': 6.02,
        'limiter': {
            'timeline': {
                'duration_s': 64,
                'bins': [
                    {'start_s': 4, 'peak_s': 5, 'end_s': 6, 'max_gr_db': 1.2},
                ],
            }
        },
    }
    (SETTINGS.library_dir / f'{entry["id"]}.json').write_text(json.dumps(entry))
    calls = []
    decode = ffmpeg.decode_reference

    def decode_excerpt(*args, **kwargs):
        calls.append(kwargs['excerpt'])
        return decode(*args, **kwargs)

    def no_render(*_args, **_kwargs):
        pytest.fail('Unchanged settings must audition the saved full render')

    monkeypatch.setattr(ffmpeg, 'decode_reference', decode_excerpt)
    monkeypatch.setattr(preview, 'render', no_render)
    # Repeating an already-saved value is also unchanged after normalization.
    r = client.post(
        f'/api/library/{entry["id"]}/preview',
        json={
            'start_s': 3.123,
            'duration_s': 5,
            'advanced': {'target_lufs': -18},
        },
    )
    assert r.status_code == 200, r.text
    job = wait_result(client, r.json()['id'])
    result = job['result']
    assert result['preview_basis'] == 'saved_export'
    assert 'saved WAV' in result['summary'][0]
    assert calls == [(3.12, 5.003), (3.12, 5.003)]
    pcm = client.get(f'/api/jobs/{job["id"]}/pcm/enhanced', params={'frames': 5 * SR})
    actual = np.frombuffer(pcm.content, dtype='<f4').reshape(-1, 2)
    expected, _ = sf.read(exported, start=round(3.123 * SR), frames=5 * SR, dtype='float32', always_2d=True)
    np.testing.assert_array_equal(actual, expected)
    assert result['processing']['applied']['limiter']['timeline']['bins'][0]['peak_s'] == pytest.approx(1.877)
    assert result['metrics']['after']['lufs'] - result['metrics']['before']['lufs'] == pytest.approx(6.02, abs=0.02)
    assert exported.read_bytes() == before
    assert client.get('/api/library').json()['count'] == 1
    assert not (manager.work_dir(job['id']) / 'scratch').exists()
    assert 'saved_export' not in job['options'] and str(exported) not in json.dumps(job)


def test_preview_without_available_export_is_explicitly_an_estimate(app, tmp_path):
    client, _ = app
    entry, exported = retained(tmp_path, quality='light')
    exported.rename(tmp_path / 'moved.wav')
    r = client.post(f'/api/library/{entry["id"]}/preview', json={'start_s': 1, 'duration_s': 5})
    assert r.status_code == 200, r.text
    result = wait_result(client, r.json()['id'])['result']
    assert result['preview_basis'] == 'section_estimate'
    assert 'may differ' in result['summary'][0]


def test_paired_audition_reuses_one_bounded_decode_and_aligns_all_sides(app, tmp_path, monkeypatch):
    client, manager = app
    entry, exported = retained(tmp_path, seconds=60)
    call = client.post
    decode = ffmpeg.decode_reference
    decodes = []

    def record_decode(*args, **kwargs):
        decodes.append(kwargs['excerpt'])
        return decode(*args, **kwargs)

    monkeypatch.setattr(ffmpeg, 'decode_reference', record_decode)
    exported.rename(tmp_path / 'moved.wav')
    start = 37.12343
    body = {'start_s': start, 'duration_s': 5, 'paired': True}
    response = call(f'/api/library/{entry["id"]}/preview', json=body)
    assert response.status_code == 200, response.text
    job = wait_result(client, response.json()['id'])
    result = job['result']
    first = round(start * SR)
    assert decodes == [(17.16, pytest.approx((first - round(17.16 * SR)) / SR + 25))]
    assert result['preview_basis'] == 'section_estimate'
    assert result['comparison_basis'] == 'section_gain'
    assert result['calibration']['gain'] == 'section'
    assert result['metrics']['saved'] == result['metrics']['after']
    assert result['processing'] == result['baseline_processing']
    assert result['excerpt'] == {
        'start_s': first / SR,
        'duration_s': 5,
        'start_frame': first,
        'frames': 5 * SR,
        'sample_rate_hz': SR,
    }

    def pcm(key):
        response = client.get(f'/api/jobs/{job["id"]}/pcm/{key}', params={'frames': 5 * SR})
        assert response.status_code == 200
        return response.content

    assert pcm('enhanced') == pcm('saved') != pcm('original')
    assert len(pcm('saved')) == 5 * SR * 8
    assert client.get('/api/library').json()['count'] == 1
    assert client.get(f'/api/library/{entry["id"]}/overview').status_code == 404
    assert client.delete(f'/api/jobs/{job["id"]}').status_code == 200
    assert not manager.job_dir(job['id']).exists()


def test_legacy_reset_to_auto_applies_and_retains_audition_state(app, tmp_path):
    client, _ = app
    entry, exported = retained(tmp_path, seconds=12)
    original = library.file_path(entry, 'original')
    old_bytes = (original.read_bytes(), exported.read_bytes())
    entry['processing']['requested']['advanced']['glue'] = 'on'
    entry['processing']['resolved']['params']['compressor'] = 'glue'
    entry['processing']['requested']['split_tracks'] = False
    (SETTINGS.library_dir / f'{entry["id"]}.json').write_text(json.dumps(entry))
    url = f'/api/library/{entry["id"]}'
    body = {'start_s': 0, 'duration_s': 5, 'paired': True, 'advanced': {'glue': None}}
    blocked = client.post(url + '/preview', json=body)
    assert blocked.status_code == 400 and 'when you save a new version' in blocked.text
    applied = client.post(url + '/enhance', json={'advanced': {'glue': None}})
    assert applied.status_code == 200, applied.text
    job = wait_result(client, applied.json()['id'])
    saved = library.get(job['id'])
    assert 'glue' not in saved['processing']['requested']['advanced']
    resolved = saved['processing']['resolved']
    assert resolved['auto_compressor'] == resolved['params']['compressor'] == 'off'
    assert resolved['calibration']['sample_rate_hz'] == SR
    assert job['result']['files'][0]['verification']['status'] == 'verified'
    assert client.get('/api/library').json()['count'] == 2
    assert (original.read_bytes(), exported.read_bytes()) == old_bytes

    saved_url = f'/api/library/{job["id"]}'
    public = client.get(saved_url).json()
    assert public['has_overview'] and 'overview' not in public
    overview = client.get(saved_url + '/overview').json()
    assert overview['basis'] == 'original_sample_peak'
    assert overview['duration_s'] == 12
    assert overview['bin_duration_s'] == 0.1
    assert overview['peaks'] == pytest.approx([0.080017] * 120, abs=0.00002)

    # A saved explicit override can preview Auto when its automatic decision was retained.
    saved['processing']['requested']['advanced']['glue'] = 'on'
    saved['processing']['resolved']['params']['compressor'] = 'glue'
    (SETTINGS.library_dir / f'{saved["id"]}.json').write_text(json.dumps(saved))
    response = client.post(saved_url + '/preview', json=body)
    assert response.status_code == 200, response.text
    result = wait_result(client, response.json()['id'])['result']
    assert result['comparison_basis'] == 'retained_gain'
    assert result['calibration']['gain'] == 'shared_retained_estimate'
    assert result['processing']['params']['compressor'] == 'off'
    assert result['baseline_processing']['params']['compressor'] == 'glue'


def test_unchanged_preview_prefers_full_lossless_output_and_never_uses_tracks(app, tmp_path):
    from audiolab.media.export import transcode

    client, manager = app
    entry, exported = retained(tmp_path, quality='light')
    lossy = transcode(exported, exported.with_name('Lossy'), 'mp3_320')
    full = entry['files'][0]
    track = {**full, 'id': 'track', 'kind': 'track'}
    cover = {**full, 'id': 'cover', 'kind': 'cover'}
    mp3 = {**full, 'id': 'mp3', 'format': 'mp3_320', 'path': str(lossy)}
    entry['files'] = [track, cover, mp3, full]
    record = SETTINGS.library_dir / f'{entry["id"]}.json'
    record.write_text(json.dumps(entry))
    url = f'/api/library/{entry["id"]}/preview'
    r = client.post(url, json={'start_s': 1, 'duration_s': 5})
    assert r.status_code == 200, r.text
    job_id = r.json()['id']
    assert manager.get(job_id).options['_preview']['saved_export'] == {'path': str(exported), 'format': 'wav16'}
    assert wait_result(client, job_id)['result']['preview_basis'] == 'saved_export'

    entry['files'] = [track, cover]
    record.write_text(json.dumps(entry))
    r = client.post(url, json={'start_s': 1, 'duration_s': 5})
    assert r.status_code == 200, r.text
    assert 'saved_export' not in manager.get(r.json()['id']).options['_preview']
    assert wait_result(client, r.json()['id'])['result']['preview_basis'] == 'section_estimate'


def test_apply_uses_original_and_saves_a_new_version_in_same_group(app, tmp_path):
    client, manager = app
    entry, exported = retained(tmp_path, quality='light')
    original = library.file_path(entry, 'original')
    before = original.read_bytes()
    before_export = exported.read_bytes()
    url = f'/api/library/{entry["id"]}'
    changed = {'advanced': {'target_lufs': -20, 'sample_rate': 44100}}
    trial = client.post(url + '/preview', json={'start_s': 1, 'duration_s': 5, **changed})
    assert trial.status_code == 200, trial.text
    audition = wait_result(client, trial.json()['id'])['result']
    assert audition['processing']['params']['target_lufs'] == -20
    eq = {'advanced': {'eq': [{'hz': 160, 'db': -3.31, 'q': 2.865}]}}
    trial = client.post(url + '/preview', json={'start_s': 1, 'duration_s': 5, **eq})
    assert trial.status_code == 200, trial.text
    assert manager.get(trial.json()['id']).options['advanced']['eq'] == [{'hz': 160.0, 'db': -3.31, 'q': 2.87}]
    wait_result(client, trial.json()['id'])
    assert audition['excerpt']['duration_s'] == 5

    r = client.post(url + '/enhance', json=changed)
    assert r.status_code == 200, r.text
    job = wait_result(client, r.json()['id'])
    result = job['result']
    assert result['library_id'] == job['id'] and 'preview' not in result
    saved = library.get(job['id'])
    assert saved['processing']['requested']['advanced'] == {
        'target_lufs': -20,
        'sample_rate': 44100,
        'deess': 'strong',
    }
    assert saved['processing']['resolved']['params']['target_lufs'] == -20
    assert saved['markers'] == entry['markers']
    assert saved['markers_source'] == 'chapters'
    assert (saved['title'], saved['artist'], saved['source'], saved['group']) == (
        entry['title'],
        entry['artist'],
        entry['source'],
        entry['group'],
    )
    files = manager.get(job['id']).result['files']
    assert len(files) == 3
    info = sf.info(Path(files[0]['path']))
    assert info.samplerate == 44100 and info.duration == 8
    assert original.read_bytes() == before and exported.read_bytes() == before_export
    (group,) = client.get('/api/library').json()['groups']
    assert len(group['entries']) == 2


def test_invalid_missing_and_unsupported_preview_requests(app, tmp_path):
    client, manager = app
    entry, _ = retained(tmp_path)
    url = f'/api/library/{entry["id"]}'
    invalid = [
        [],
        {'start_s': -1, 'duration_s': 5},
        {'start_s': 7.5, 'duration_s': 5},
        {'start_s': 0, 'duration_s': 31},
        {'start_s': 0, 'duration_s': 4},
        {'start_s': True, 'duration_s': 5},
        {'start_s': float('inf'), 'duration_s': 5},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'target_lufs': float('nan')}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'bass_db': 10**400}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'bass_db': None}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'sample_rate': 48000.5}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'other': 'value'}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'eq': [{'hz': 160, 'db': -3}]}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'eq': [{'hz': 160, 'db': 'x', 'q': 1}]}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'eq': [{'hz': 100, 'db': 1, 'q': 1}] * 9}},
        {'start_s': 0, 'duration_s': 5, 'advanced': {'eq': [{'hz': 10**400, 'db': 1, 'q': 1}]}},
        {'start_s': 0, 'duration_s': 5, 'paired': 'true'},
    ]
    for body in invalid:
        r = client.post(url + '/preview', content=json.dumps(body), headers={'Content-Type': 'application/json'})
        assert r.status_code == 400, (body, r.text)
    missing = client.delete(url + '/original')
    assert missing.status_code == 200
    assert client.post(url + '/preview', json={'start_s': 0, 'duration_s': 5}).status_code == 410
    assert client.post(url + '/enhance', json={}).status_code == 410

    best, _ = retained(tmp_path, quality='best')
    rejected = client.post(f'/api/library/{best["id"]}/preview', json={'start_s': 0, 'duration_s': 5})
    assert rejected.status_code == 400 and 'Volume only and Adaptive' in rejected.text
    assert manager.list() == []


def test_preview_clips_to_actual_eof_and_rejects_too_little_audio(app, tmp_path):
    client, manager = app
    entry, _ = retained(tmp_path, quality='light', known_duration=9.0)
    url = f'/api/library/{entry["id"]}/preview'
    good = client.post(url, json={'start_s': 6.5, 'duration_s': 5})
    assert good.status_code == 200, good.text
    assert wait_result(client, good.json()['id'])['result']['excerpt'] == {'start_s': 6.5, 'duration_s': 1.5}

    bad = client.post(url, json={'start_s': 7.5, 'duration_s': 5})
    assert bad.status_code == 200, bad.text
    failed = wait_result(client, bad.json()['id'], expect='failed')
    assert 'one second' in failed['error']['message']
    assert failed['result'] is None
    assert not (manager.work_dir(failed['id']) / 'scratch').exists()
    assert not (manager.work_dir(failed['id']) / 'previews').exists()


def test_cancel_after_render_discards_preview_and_retry_keeps_its_input(app, tmp_path, monkeypatch):
    client, manager = app
    entry, _ = retained(tmp_path, quality='light')
    finishing, release = threading.Event(), threading.Event()
    finish = manager._finish

    def intercept(job, status, message, ctx=None):
        if status == 'done' and not finishing.is_set():
            finishing.set()
            release.wait(20)
        return finish(job, status, message, ctx)

    monkeypatch.setattr(manager, '_finish', intercept)
    url = f'/api/library/{entry["id"]}'
    r = client.post(url + '/preview', json={'start_s': 1, 'duration_s': 5})
    assert r.status_code == 200, r.text
    job_id = r.json()['id']
    try:
        assert finishing.wait(20)
        assert (manager.work_dir(job_id) / 'previews' / 'preview_original.wav').exists()
        assert client.delete(url + '/original').status_code == 200
        assert client.post(f'/api/jobs/{job_id}/cancel').status_code == 200
    finally:
        release.set()
    job = wait_result(client, job_id, expect='cancelled')
    assert job['result'] is None
    assert not (manager.work_dir(job_id) / 'previews').exists()
    assert client.get(f'/api/jobs/{job_id}/media/original').status_code == 404
    assert client.post(f'/api/jobs/{job_id}/retry').status_code == 200
    assert wait_result(client, job_id)['result']['preview'] is True


def test_preview_job_options_cannot_be_injected_by_normal_enhance():
    assert '_preview' not in tasks.validate('enhance', {'_preview': {'start_s': 0}}, {})


def test_context_timeline_uses_excerpt_time():
    report = {
        'limiter': {
            'timeline': {
                'duration_s': 12,
                'bins': [
                    {'start_s': 2, 'peak_s': 3, 'end_s': 4, 'max_gr_db': 2},
                    {'start_s': 4, 'peak_s': 5.5, 'end_s': 6, 'max_gr_db': 1},
                    {'start_s': 8, 'peak_s': 9.5, 'end_s': 10, 'max_gr_db': 1.5},
                ],
            },
        },
    }
    out = preview._excerpt_processing(report, 5.0, 4.0)['applied']['limiter']['timeline']
    assert out == {
        'duration_s': 4.0,
        'bins': [{'start_s': 0.0, 'peak_s': 0.5, 'end_s': 1.0, 'max_gr_db': 1}],
    }
