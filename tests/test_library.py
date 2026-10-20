"""Library (records that outlive jobs, kept originals), file serving (Range, media types), own cover
images and the tracklist preview — through JobManager and the HTTP API, with a fake task runner."""

import base64
import hashlib
import io
import json
import shutil
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest

from audiolab import library
from audiolab.jobs import JobManager, new_job_id
from audiolab.media import ffmpeg, youtube
from audiolab.media.ffmpeg import run_ffmpeg
from audiolab.settings import SETTINGS

STAGES = [('a', 'Working', 1)]
URL_SOURCE = {'kind': 'url', 'url': 'https://youtu.be/abc', '_probe': {'id': 'abc', 'extractor': 'Youtube'}}
# A tiny lossless WebP (1×1), padded: real thumbnails are never this small.
WEBP = base64.b64decode('UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==') + b'\0' * 200


def wait_for(fn, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(0.02)
    raise AssertionError('condition not reached in time')


def lib_runner(job, ctx):
    """Like Enhance: files in the output folder, the downloaded original in scratch (an upload's is
    the uploaded file; `_content` in a link's source changes what was downloaded)."""
    ctx.stage('a')(100)
    out = SETTINGS.output_dir / f'{job.title} (Enhanced)'
    (out / 'Tracks').mkdir(parents=True, exist_ok=True)
    files = []
    for i, (rel, group) in enumerate([(f'{job.title} (Enhanced).m4a', None), ('Tracks/01 - Intro.m4a', 'tracks')]):
        p = out / rel
        p.write_bytes(bytes(range(256)) * 40)
        files.append({'id': f'f{i}', 'name': p.name, 'path': str(p), 'format': 'aac_256', 'size_bytes': 10240})
        if group:
            files[-1].update(group=group, track=1, start_s=0.0, end_s=5.0)
    excerpt = ctx.work_dir / 'previews' / 'preview_original.wav'  # stays when the job finishes
    excerpt.parent.mkdir(parents=True, exist_ok=True)
    excerpt.write_bytes(_wav())
    if job.source.get('kind') == 'file':
        original = Path(job.source['path'])
    else:
        original = ctx.work_dir / 'scratch' / 'download' / 'audio.webm'
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(job.source.get('_content', 'webm').encode() * 1000)
    return {
        'headline': 'Enhanced',
        'output_dir': str(out),
        'files': files,
        'metrics': {'before': {'lufs': -15.0}, 'after': {'lufs': -12.0}},
        '_media': {'original': str(excerpt)},
        '_library': {
            'artist': 'Artist',
            'duration_s': 10.0,
            'markers': [{'n': 1, 'title': 'Intro', 'start_s': 0.0, 'end_s': 5.0}],
            'markers_source': 'pasted',
            'original': str(original),
            'original_codec': 'opus',
        },
    }


@pytest.fixture
def manager(tmp_path):
    m = JobManager(runner=lib_runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    yield m
    m.shutdown()


def run_job(m, task='enhance', source=None, title='Song'):
    job = m.submit(task, dict(source or URL_SOURCE), {}, title=title)
    wait_for(lambda: m.get(job.id).status == 'done')
    return job


def upload_job(m, name, data, task='enhance'):
    """A finished job on an uploaded file (stored in the job's input/ as the upload route does); its entry."""
    jid = new_job_id()
    path = m.input_dir(jid) / name
    path.write_bytes(data)
    source = {'kind': 'file', 'name': name, 'path': str(path), 'size_bytes': len(data), 'title': Path(name).stem}
    m.submit(task, source, {}, title=Path(name).stem, job_id=jid)
    wait_for(lambda: m.get(jid).status == 'done')
    return library.get(jid)


def test_entry_written_on_finish_and_outlives_the_job(manager):
    job = run_job(manager)
    pub = manager.public(job.id)['result']
    assert pub['library_id'] == job.id and all(f['exists'] for f in pub['files'])
    assert not (manager.work_dir(job.id) / 'scratch').exists()  # scratch went, the original was kept first
    raw = json.loads((SETTINGS.library_dir / f'{job.id}.json').read_text())
    assert raw['group'] == 'youtube:abc' and raw['files'][0]['path'] == 'Song (Enhanced)/Song (Enhanced).m4a'
    assert [f['kind'] for f in raw['files']] == ['enhanced', 'track'] and raw['markers_source'] == 'pasted'
    assert raw['loudness'] == {'original_lufs': -15.0, 'result_lufs': -12.0}
    kept = library.originals_dir() / raw['original']['file']
    assert kept.read_bytes() == b'webm' * 1000 and raw['original']['name'] == 'Song.webm'

    manager.delete(job.id)
    manager.clear_finished()
    (group,) = library.groups()
    (entry,) = group['entries']
    assert entry['id'] == job.id and entry['job_exists'] is False and entry['artist'] == 'Artist'
    original = entry['files'][-1]
    assert original['id'] == 'original' and original['exists'] and original['mime'] == 'audio/webm'
    assert original['codec'] == 'opus'
    # the title is the one used for names and tags (e.g. an upload's own title tag), not the job's
    tagged = manager.submit('download', dict(URL_SOURCE), {}, title='Recording')
    wait_for(lambda: manager.get(tagged.id).status == 'done')
    assert library.get(tagged.id)['title'] == 'Recording'
    manager.get(tagged.id).result['_library']['title'] = 'Actual Song'
    library.record(manager.get(tagged.id))
    assert library.get(tagged.id)['title'] == 'Actual Song'
    # Check (analyze) results are not library material
    check = run_job(manager, task='analyze')
    assert 'library_id' not in manager.public(check.id)['result']


def test_remove_entry_keeps_files_and_drops_unreferenced_original(manager):
    first, second = run_job(manager), run_job(manager)  # same recording twice → one shared original
    e1, e2 = library.get(first.id), library.get(second.id)
    assert e1['original']['file'] == e2['original']['file']
    kept = library.originals_dir() / e1['original']['file']
    outputs = [library.file_path(e1, f['id']) for f in e1['files']]
    assert library.remove(first.id) == {'deleted': first.id, 'original_removed': False, 'freed_bytes': 0}
    assert kept.is_file() and all(p.is_file() for p in outputs)
    assert library.remove(second.id)['original_removed'] is True
    assert not kept.exists() and all(p.is_file() for p in outputs)  # the user's files are never touched
    assert library.remove(second.id) is None and library.groups() == []


def test_library_api_files_range_and_storage(tmp_path, manager):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    job = run_job(manager)
    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        lib = c.get('/api/library', headers=h).json()
        assert lib['count'] == 1 and lib['groups'][0]['key'] == 'youtube:abc'
        entry = lib['groups'][0]['entries'][0]
        assert entry['job_exists'] is True and 'root' not in entry
        assert entry['original'] == {
            'kept': True,
            'size_bytes': 4000,
            'format': 'webm',
            'codec': 'opus',
            'url': 'https://youtu.be/abc',
        }
        assert [f['id'] for f in entry['files']] == ['f0', 'f1', 'original']

        url = f'/api/library/{job.id}/files/f0?token=t'
        whole = c.get(url)
        assert whole.status_code == 200 and whole.headers['content-type'] == 'audio/mp4'  # not audio/mp4a-latm
        part = c.get(url, headers={'Range': 'bytes=100-199'})
        assert part.status_code == 206 and part.headers['content-range'] == 'bytes 100-199/10240'
        assert part.content == whole.content[100:200]
        media = c.get(f'/api/jobs/{job.id}/media/file:f0', headers={**h, 'Range': 'bytes=0-9'})
        assert media.status_code == 206 and len(media.content) == 10
        dl = c.get(url + '&download=1')
        assert (
            'attachment' in dl.headers['content-disposition']
            and 'Song (Enhanced).m4a' in dl.headers['content-disposition']
        )
        assert c.get(f'/api/library/{job.id}/files/original?token=t').content == b'webm' * 1000

        library.file_path(library.get(job.id), 'f0').unlink()  # the user moved it away
        files = c.get('/api/library', headers=h).json()['groups'][0]['entries'][0]['files']
        assert [f['exists'] for f in files] == [False, True, True]
        assert [f['exists'] for f in c.get(f'/api/jobs/{job.id}', headers=h).json()['result']['files']] == [
            False,
            True,
        ]
        assert c.get(url).status_code == 410 and c.get(f'/api/library/{job.id}/files/zz?token=t').status_code == 404

        st = c.get('/api/storage', headers=h).json()
        assert st['library_count'] == 1 and st['originals_count'] == 1 and st['originals_bytes'] == 4000
        foreign = library.originals_dir() / 'notes.txt'  # not ours: clearing leaves it alone
        foreign.write_text('x')
        assert c.delete('/api/library/originals', headers=h).json() == {'deleted': 1, 'freed_bytes': 4000}
        assert foreign.is_file() and library.get(job.id)['original'] is None  # detached, can't come back
        assert c.get('/api/storage', headers=h).json()['originals_bytes'] == 0
        entry = c.get('/api/library', headers=h).json()['groups'][0]['entries'][0]  # the entry stays
        assert entry['original']['kept'] is False and 'original' not in [f['id'] for f in entry['files']]

        assert c.delete(f'/api/library/{job.id}', headers=h).json()['deleted'] == job.id
        assert c.delete(f'/api/library/{job.id}', headers=h).status_code == 404
        assert c.delete('/api/library/j_bad', headers=h).status_code == 404
        assert c.get('/api/library/..%2F..%2Fsettings/files/f0?token=t').status_code == 404
        assert c.get('/api/jobs', headers=h).json()['jobs'][0]['id'] == job.id  # the job is untouched


def test_tracklist_preview_endpoint(manager):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        text = '0:00 Artist A - Opening\nno time here\n03:00 Second\n02:00 Back'
        r = c.post('/api/tracklist', headers=h, json={'text': text, 'duration_s': 300}).json()
        assert r['tracks'] == [
            {'n': 1, 'start_s': 0.0, 'end_s': 180.0, 'title': 'Opening', 'artist': 'Artist A'},
            {'n': 2, 'start_s': 180.0, 'end_s': 300.0, 'title': 'Second', 'artist': None},
        ]
        assert [e['line'] for e in r['errors']] == [2, 4] and 'backwards' in r['errors'][1]['message']
        assert c.post('/api/tracklist', headers=h, json={'text': 5}).status_code == 400
        for bad in ('Infinity', 'NaN', -5):
            assert c.post('/api/tracklist', headers=h, json={'text': '0:00 A', 'duration_s': bad}).status_code == 400
        bad = c.post(
            '/api/jobs', headers=h, data={'task': 'download', 'url': 'https://x.y/z', 'options': '{"tracklist": "x"}'}
        )
        assert bad.status_code == 400 and 'line 1' in bad.json()['detail']


def test_compare_file_and_pcm_routes(manager, monkeypatch):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app
    from audiolab.media import ffmpeg, proc

    monkeypatch.setattr(proc, '_POLL_S', 0.01)  # short ffmpeg runs: don't wait 0.2 s for each
    entry = upload_job(manager, 'mix.wav', _wav())  # 1 s, 8 kHz mono: a real original to decode
    eid = entry['id']
    result = library.file_path(entry, 'f0')
    result.write_bytes(_wav())
    assert library.public(entry)['compare_file_id'] == 'f0'  # the only full-length result (f1 is a track)
    lossless = result.with_name('Song (Enhanced).flac')
    run_ffmpeg(['-i', str(result), str(lossless)])
    path = Path(entry['files'][0]['path']).with_name(lossless.name).as_posix()
    entry['files'].append({**entry['files'][0], 'id': 'f2', 'name': lossless.name, 'path': path, 'format': 'flac16'})
    library._write(entry)
    assert library.public(library.get(eid))['compare_file_id'] == 'f2'  # lossless first, whatever the order
    assert library.public(upload_job(manager, 'saved.wav', _wav(), task='download'))['compare_file_id'] is None

    g = ffmpeg.PCM_GRID
    with TestClient(create_app(local=False, manager=manager)) as c:
        one = c.get(f'/api/library/{eid}').json()
        assert one['id'] == eid and one['job_exists'] is True and one['compare_file_id'] == 'f2'
        assert one['original']['kept'] is True and 'root' not in one
        assert c.get('/api/library/j_20990101_000000_abcdef').status_code == 404
        assert c.get('/api/library/originals').status_code == 404

        r = c.get(f'/api/library/{eid}/pcm/original?offset=0&frames={2 * g}')
        assert r.status_code == 200 and r.headers['content-type'] == 'application/octet-stream'
        assert r.headers['cache-control'] == 'no-store' and len(r.content) == 2 * g * 8
        x = np.frombuffer(r.content, np.float32).reshape(-1, 2)
        assert np.array_equal(x[:, 0], x[:, 1])  # mono on both sides
        assert r.content == c.get(f'/api/library/{eid}/pcm/f2?offset=0&frames={2 * g}').content  # same samples
        assert len(c.get(f'/api/library/{eid}/pcm/f0?offset={20 * g}&frames={10 * g}').content) == 5 * g * 8  # the end
        for bad in (
            'offset=640&frames=1920',
            'offset=0&frames=1000',
            'offset=0&frames=0',
            f'offset=-{g}&frames={g}',
            f'offset=0&frames={500 * g + g}',
        ):
            assert c.get(f'/api/library/{eid}/pcm/f0?{bad}').status_code == 400, bad
        assert c.get(f'/api/library/{eid}/pcm/zz?frames={g}').status_code == 404
        assert c.get(f'/api/library/j_20990101_000000_abcdef/pcm/f0?frames={g}').status_code == 404
        lossless.unlink()  # moved away: the comparison falls back to the other result
        assert c.get(f'/api/library/{eid}/pcm/f2?frames={g}').status_code == 410
        assert c.get(f'/api/library/{eid}').json()['compare_file_id'] == 'f0'

        jr = c.get(f'/api/jobs/{eid}/pcm/original?offset={g}&frames={g}')  # the job's excerpts
        assert jr.status_code == 200 and len(jr.content) == g * 8
        assert c.get(f'/api/jobs/{eid}/pcm/original?offset=7&frames={g}').status_code == 400
        for key in ('enhanced', 'difference', 'spectrogram'):
            assert c.get(f'/api/jobs/{eid}/pcm/{key}?frames={g}').status_code == 404
        assert c.get(f'/api/jobs/j_20990101_000000_abcdef/pcm/original?frames={g}').status_code == 404

        assert c.delete(f'/api/library/{eid}/original').status_code == 200  # no kept original: nothing to compare
        assert c.get(f'/api/library/{eid}').json()['compare_file_id'] is None


# ------------------------------------------------------------------ covers
def _png(path: Path, size: str = '2000x1000') -> Path:
    run_ffmpeg(['-f', 'lavfi', '-i', f'testsrc2=s={size}:d=1', '-frames:v', '1', str(path)])
    return path


def _wav() -> bytes:
    import wave

    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b'\0\0' * 8000)
    return buf.getvalue()


def test_own_cover_upload_checked_and_kept_for_reruns(tmp_path, manager):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    png = _png(tmp_path / 'art.png').read_bytes()
    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        r = c.post(
            '/api/jobs',
            headers=h,
            data={'task': 'download', 'options': '{"format": "mp3"}'},
            files={'file': ('My Song.wav', _wav(), 'audio/wav'), 'cover': ('art.png', png, 'image/png')},
        )
        assert r.status_code == 200, r.text
        job = r.json()['jobs'][0]
        assert job['source']['cover_name'] == 'art.png' and '_cover' not in job['source']
        source = manager.get(job['id']).source
        cover = Path(source['_cover'])
        assert cover == manager.input_dir(job['id']) / 'cover' / 'cover.jpg'
        info = ffmpeg.probe_media(cover)
        assert (info['video_codec'], info['width'], info['height']) == ('mjpeg', 1400, 700)  # downscaled
        original = Path(source['_cover_original'])  # kept as uploaded, for "save the cover"
        assert original == manager.input_dir(job['id']) / 'cover' / 'original.png' and original.read_bytes() == png

        # Save cover to folder (a finished job): the own image at full size, as one more result file
        wait_for(lambda: manager.get(job['id']).status == 'done')
        saved = c.post(f'/api/jobs/{job["id"]}/cover', headers=h).json()
        assert {k: saved[k] for k in ('id', 'name', 'kind', 'format', 'exists', 'playable')} == {
            'id': 'f2',
            'name': 'My Song - cover.png',
            'kind': 'cover',
            'format': 'png',
            'exists': True,
            'playable': False,
        }
        out = SETTINGS.output_dir / 'My Song (Enhanced)'
        assert (out / 'My Song - cover.png').read_bytes() == png and 'path' not in saved
        assert c.post(f'/api/jobs/{job["id"]}/cover', headers=h).json() == saved  # already there: the same file
        assert [p.name for p in out.glob('*.png')] == ['My Song - cover.png']
        files = c.get(f'/api/jobs/{job["id"]}', headers=h).json()['result']['files']
        assert [f['id'] for f in files] == ['f0', 'f1', 'f2'] and files[2] == saved
        lib = c.get('/api/library', headers=h).json()['groups'][0]['entries'][0]['files']
        cov = next(f for f in lib if f['id'] == 'f2')
        assert (cov['kind'], cov['path'], cov['mime'], cov['playable']) == (
            'cover',
            'My Song (Enhanced)/My Song - cover.png',
            'image/png',
            False,
        )
        with zipfile.ZipFile(io.BytesIO(c.get(f'/api/jobs/{job["id"]}/zip', headers=h).content)) as zf:
            assert 'My Song - cover.png' in zf.namelist()
        (out / 'My Song - cover.png').unlink()  # moved away: saving again writes it anew, same id
        assert c.post(f'/api/jobs/{job["id"]}/cover', headers=h).json()['id'] == 'f2'
        assert (out / 'My Song - cover.png').is_file()

        again = c.post('/api/jobs', headers=h, data={'task': 'enhance', 'source_job': job['id']}).json()['jobs'][0]
        copied = Path(manager.get(again['id']).source['_cover'])
        assert copied == manager.input_dir(again['id']) / 'cover' / 'cover.jpg'
        assert copied.read_bytes() == cover.read_bytes()
        assert Path(manager.get(again['id']).source['_cover_original']).read_bytes() == png
        manager.delete(job['id'])  # the re-run keeps its own copy
        assert copied.is_file()

        before = {p.name for p in manager.root.iterdir()}
        for name, data, task in [
            ('notes.png', b'this is not a picture' * 20, 'download'),  # pretends to be a picture
            ('art.gif', png, 'download'),
            ('art.png', png, 'analyze'),
        ]:
            bad = c.post(
                '/api/jobs',
                headers=h,
                data={'task': task, 'url': 'https://example.com/v'},
                files={'cover': (name, data, 'image/png')},
            )
            assert bad.status_code == 400, (name, bad.text)
        assert {p.name for p in manager.root.iterdir()} == before  # nothing left behind


def test_save_cover_of_a_link_job(manager, monkeypatch):
    """POST /api/jobs/{id}/cover for a link: its thumbnail at full size, WebP as a JPEG."""
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    fetched = []

    def urlopen(req, timeout=20):
        fetched.append(req.full_url)
        return io.BytesIO(WEBP)

    monkeypatch.setattr(youtube.urllib.request, 'urlopen', urlopen)
    with_thumb = run_job(manager, source={**URL_SOURCE, 'thumbnail': 'https://i.ytimg.com/x.webp'}, title='Thumb Song')
    without = run_job(manager, title='Plain')
    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        r = c.post(f'/api/jobs/{with_thumb.id}/cover', headers=h)
        assert r.status_code == 200, r.text
        assert (r.json()['name'], r.json()['format']) == ('Thumb Song - cover.jpg', 'jpg')
        saved = SETTINGS.output_dir / 'Thumb Song (Enhanced)' / 'Thumb Song - cover.jpg'
        assert saved.read_bytes()[:3] == b'\xff\xd8\xff' and fetched == ['https://i.ytimg.com/x.webp']
        assert any(f['kind'] == 'cover' for f in library.public(library.get(with_thumb.id))['files'])
        r = c.post(f'/api/jobs/{without.id}/cover', headers=h)
        assert r.status_code == 409 and 'no cover image' in r.json()['detail']
        assert c.post('/api/jobs/j_20260101_000000_aaaaaa/cover', headers=h).status_code == 404
        assert c.get(f'/api/jobs/{with_thumb.id}/cover', headers=h).status_code in (404, 405)  # no download any more


def test_drop_one_original_and_the_keep_originals_setting(manager):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    SETTINGS.update({'keep_originals': False})
    off = run_job(manager)
    assert library.public(library.get(off.id))['original']['kept'] is False
    assert library.originals_usage()['count'] == 0  # nothing kept, the entry is there anyway
    SETTINGS.update({'keep_originals': True})
    on, again = run_job(manager), run_job(manager)  # two versions sharing one kept original
    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        r = c.delete(f'/api/library/{on.id}/original', headers=h).json()
        assert r == {'id': on.id, 'original_removed': True, 'freed_bytes': 4000}
        entries = c.get('/api/library', headers=h).json()['groups'][0]['entries']
        assert [e['id'] for e in entries] == [again.id, on.id, off.id]  # the entries stay
        assert all(e['original']['kept'] is False and len(e['files']) == 2 for e in entries)  # outputs too
        assert entries[0]['original']['url'] == 'https://youtu.be/abc' and library.originals_usage()['count'] == 0
        assert c.get(f'/api/library/{again.id}/files/original?token=t').status_code == 404
        assert c.delete('/api/library/j_20260101_000000_aaaaaa/original', headers=h).status_code == 404


def test_reveal_entry_opens_its_folder(manager, monkeypatch):
    from fastapi.testclient import TestClient

    from audiolab import system
    from audiolab.api import create_app

    opened = []
    monkeypatch.setattr(system, 'reveal', opened.append)
    job = run_job(manager)
    folder, root = SETTINGS.output_dir / 'Song (Enhanced)', SETTINGS.output_dir
    with TestClient(create_app(local=True, manager=manager), base_url='http://127.0.0.1:8765') as c:
        assert c.post('/api/library/nope/reveal').status_code == 404
        assert c.post(f'/api/library/{job.id}/reveal').json() == {'ok': True}
        assert opened == [folder]
        shutil.rmtree(folder)  # moved away: the output folder instead, and the UI says so
        assert c.post(f'/api/library/{job.id}/reveal').json() == {'ok': True, 'fallback': 'output'}
        assert c.post(f'/api/jobs/{job.id}/reveal').json() == {'ok': True, 'fallback': 'output'}
        assert c.post('/api/reveal-output').json() == {'ok': True}
        assert opened[1:] == [root, root, root]
        shutil.rmtree(root)  # neither there: said, and a job's folder is never made again
        for path in (f'/api/library/{job.id}/reveal', f'/api/jobs/{job.id}/reveal'):
            r = c.post(path)
            assert r.status_code == 400 and r.json()['detail'] == f"This folder isn't there any more: {folder}."
            assert r.json()['hints']
        assert not root.exists()
        # "Open output folder" before the first result (a new user): the output folder is made and opened
        assert c.post('/api/reveal-output').json() == {'ok': True}
        assert root.is_dir() and not folder.exists() and opened[4:] == [root]


# ---------------------------------------------------- originals by content
def test_kept_originals_are_identified_by_content(manager):
    """Two different uploads with the same name and size are different recordings; the same content
    under another name is the same one (one kept file)."""
    a = upload_job(manager, 'mix.wav', b'A' * 5000)
    b = upload_job(manager, 'mix.wav', b'B' * 5000)  # a re-bounced mix: same name, same size
    c = upload_job(manager, 'Mix (final).wav', b'A' * 5000)
    assert a['group'] != b['group'] and a['group'] == c['group'] == f'file:{a["original"]["sha1"]}'
    assert a['original']['file'] != b['original']['file'] and a['original']['file'] == c['original']['file']
    assert library.file_path(a, 'original').read_bytes() == b'A' * 5000
    assert library.file_path(b, 'original').read_bytes() == b'B' * 5000
    assert a['original']['name'] == 'mix.wav' and c['original']['name'] == 'Mix (final).wav'
    assert [len(g['entries']) for g in library.groups()] == [2, 1]
    assert library.originals_usage() == {'count': 2, 'bytes': 10000}

    kept_a, kept_b = library.file_path(a, 'original'), library.file_path(b, 'original')
    assert library.remove(a['id']) == {'deleted': a['id'], 'original_removed': False, 'freed_bytes': 0}  # c uses it
    dropped = library.drop_original(b['id'])  # only b's recording: a's file stays
    assert dropped == {'id': b['id'], 'original_removed': True, 'freed_bytes': 0}  # b's job still holds the upload
    assert not kept_b.exists() and kept_a.is_file() and Path(manager.get(b['id']).source['path']).is_file()
    assert library.public(library.get(c['id']))['original']['kept'] is True
    manager.delete(a['id'])
    manager.delete(c['id'])  # the uploads are gone: the kept file is the last link
    assert library.remove(c['id']) == {'deleted': c['id'], 'original_removed': True, 'freed_bytes': 5000}
    assert not kept_a.exists() and library.originals_usage()['count'] == 0


def test_one_kept_file_shared_by_two_recordings(manager):
    """A link's download uploaded again later: two recordings, one kept file. Dropping one recording's
    original keeps the file for the other."""
    link = run_job(manager)
    upload = upload_job(manager, 'Song.webm', b'webm' * 1000)
    e_link = library.get(link.id)
    assert e_link['group'] != upload['group'] and e_link['original']['file'] == upload['original']['file']
    assert library.drop_original(link.id) == {'id': link.id, 'original_removed': True, 'freed_bytes': 0}
    assert library.get(link.id)['original'] is None and library.file_path(upload, 'original').is_file()
    # several different kept files in one group (the site re-encoded the stream): all go together
    again = run_job(manager, source={**URL_SOURCE, '_content': 'opus'})
    third = run_job(manager, source={**URL_SOURCE, '_content': 'mp4a'})
    files = {library.get(j.id)['original']['file'] for j in (again, third)}
    assert len(files) == 2
    assert library.drop_original(again.id) == {'id': again.id, 'original_removed': True, 'freed_bytes': 8000}
    assert not any((library.originals_dir() / f).exists() for f in files)
    assert library.originals_usage()['count'] == 1  # the upload's, untouched


def test_source_key_and_entries_from_before(manager):
    generic = {'id': 'audio', 'extractor': 'Generic'}  # yt-dlp's id for a direct file link: the file stem
    a = {'kind': 'url', 'url': 'https://a.example.com/files/audio.mp3', '_probe': generic}
    b = {'kind': 'url', 'url': 'https://b.example.org/x/audio.mp3', '_probe': generic}
    assert library.source_key(a) == 'url:https://a.example.com/files/audio.mp3' != library.source_key(b)
    assert library.source_key(URL_SOURCE) == 'youtube:abc'
    nfd = {'kind': 'file', 'name': 'Cafe\u0301.wav', 'size_bytes': 5}  # the upload is gone: name + size
    assert library.source_key(nfd) == library.source_key({**nfd, 'name': 'Caf\u00e9.wav'}) == 'file:Caf\u00e9.wav:5'

    # a 2.3 entry: grouped by name + size, its kept file named after that group
    entry = upload_job(manager, 'old.wav', b'O' * 3000)
    old_name = hashlib.sha1(b'file:old.wav:3000').hexdigest()[:16] + '.wav'
    library.file_path(entry, 'original').rename(library.originals_dir() / old_name)
    entry.update(group='file:old.wav:3000', original={'file': old_name, 'name': 'old.wav', 'size_bytes': 3000})
    library._write(entry)
    assert library.public(library.get(entry['id']))['original']['kept'] is True
    assert library.file_path(library.get(entry['id']), 'original').read_bytes() == b'O' * 3000
    assert library.remove(entry['id'])['original_removed'] is True
    assert not (library.originals_dir() / old_name).exists()


def test_split_keeps_no_original(manager):
    job = run_job(manager, task='separate')
    assert library.get(job.id)['original'] is None and library.originals_usage()['count'] == 0


def test_storage_counts_a_linked_original_once(manager):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    entry = upload_job(manager, 'big.wav', b'W' * 200_000)  # its kept original is a hard link to the upload
    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        st = c.get('/api/storage', headers=h).json()
        assert st['originals_bytes'] == 200_000 and st['originals_count'] == 1 and st['jobs_bytes'] < 200_000
        r = c.delete(f'/api/library/{entry["id"]}/original', headers=h).json()
        assert r == {'id': entry['id'], 'original_removed': True, 'freed_bytes': 0}  # the job still has it
        st = c.get('/api/storage', headers=h).json()
        assert st['originals_bytes'] == 0 and st['jobs_bytes'] > 200_000


def test_own_cover_next_to_an_upload_called_cover_original(tmp_path, manager):
    """An audio file named like the cover's own files must survive storing the cover (also on a re-run)."""
    from fastapi.testclient import TestClient

    from audiolab.api import create_app

    png = _png(tmp_path / 'art.png', '64x64').read_bytes()
    with TestClient(create_app(local=False, token='t', manager=manager)) as c:
        h = {'X-AudioLab-Token': 't'}
        files = {'file': ('cover-original.wav', _wav(), 'audio/wav'), 'cover': ('art.png', png, 'image/png')}
        r = c.post('/api/jobs', headers=h, data={'task': 'download', 'options': '{"format": "mp3"}'}, files=files)
        assert r.status_code == 200, r.text
        jid = r.json()['jobs'][0]['id']
        source = manager.get(jid).source
        assert Path(source['path']).read_bytes() == _wav() and Path(source['_cover_original']).read_bytes() == png
        wait_for(lambda: manager.get(jid).status == 'done')
        again = c.post(
            '/api/jobs',
            headers=h,
            data={'task': 'enhance', 'source_job': jid},
            files={'cover': ('new.png', png, 'image/png')},
        )
        assert again.status_code == 200, again.text
        src = manager.get(again.json()['jobs'][0]['id']).source
        assert Path(src['path']).name == 'cover-original.wav' and Path(src['path']).read_bytes() == _wav()
        assert Path(src['_cover']).parent.name == 'cover' and Path(src['_cover_original']).is_file()
