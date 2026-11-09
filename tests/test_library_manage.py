"""Managing the Library: reusing a kept original instead of downloading again, the opt-in Trash
(removing an entry, "keep only this version") and importing a file from this computer. The system
Trash is always faked here; the freedesktop implementation runs against a temporary data folder."""

import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from audiolab import library, system, tasks
from audiolab.api import create_app
from audiolab.errors import AudioLabError
from audiolab.jobs import JobManager
from audiolab.media import youtube
from audiolab.media.ffmpeg import run_ffmpeg
from audiolab.settings import SETTINGS
from audiolab.tasks import common

STAGES = [('a', 'Working', 1)]
LINK = 'https://youtu.be/abc'
PROBE = {'kind': 'single', 'id': 'abc', 'extractor': 'Youtube', 'title': 'Song', 'duration_s': 10.0}
STREAM = {'format_id': '251', 'codec': 'opus', 'kbps': 129, 'sample_rate': 48000, 'channels': 2}


def finished(m, job_id, timeout=20.0):
    t0 = time.time()
    while m.get(job_id).status not in ('done', 'failed', 'cancelled'):
        assert time.time() - t0 < timeout, 'job did not finish in time'
        time.sleep(0.02)
    job = m.get(job_id)
    assert job.status == 'done', job.error
    return job


def notes(m, job_id):
    return [e['message'] for e in m.journal(job_id) if e['kind'] == 'info']


# ------------------------------------------------------------ reuse kept originals
def test_reuse_a_kept_original_unless_off_or_not_this_links_download(tmp_path, monkeypatch):
    downloads = []
    master = tmp_path / 'Master.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=d=1', str(master)], what='test tone')

    def download_audio(url, dest, progress=None):
        dest.mkdir(parents=True, exist_ok=True)
        if url.endswith('/xyz'):  # another recording whose audio has the master's bytes
            return Path(shutil.copy(master, dest / 'audio.wav'))
        downloads.append(url)
        (dest / 'audio.webm').write_bytes(b'webm' * 1000)
        return dest / 'audio.webm'

    def enhance(ctx, source):  # the real link acquisition, recorded like Enhance (it keeps the original)
        acq = common.acquire(ctx, source, ctx.stage('a'), task='enhance')
        out = SETTINGS.output_dir / ctx.job_id / 'Song (Enhanced).flac'
        out.parent.mkdir(parents=True)
        out.write_bytes(b'flac')
        return {
            'headline': 'Enhanced',
            'output_dir': str(out.parent),
            'files': [{'id': 'f0', 'name': out.name, 'path': str(out), 'format': 'flac24'}],
            **common.link_result(acq),
            '_library': {
                **common.library_hints(acq, keep_original=True),
                'version': common.next_version(source, 'enhance'),
            },
        }

    monkeypatch.setattr(youtube, 'probe', lambda url, progress=None: dict(PROBE, url=url, id=url.rsplit('/', 1)[-1]))
    monkeypatch.setattr(youtube, 'download_audio', download_audio)
    monkeypatch.setattr(youtube, 'stream_info', lambda path: dict(STREAM))  # what yt-dlp printed for it
    monkeypatch.setattr(common, 'preflight', lambda *a, **k: {})
    monkeypatch.setattr(common, 'ai_device', lambda: None)
    monkeypatch.setattr(tasks, 'get', lambda task: SimpleNamespace(run=enhance))
    m = JobManager(runner=tasks.run_job, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    try:

        def job():
            return finished(m, m.submit('enhance', {'kind': 'url', 'url': LINK}, {}).id)

        # Another link's download with the master's bytes, then the master enhanced as a version of this
        # link: neither may stand in for this link's download (same bytes, other recording; not downloaded).
        xyz = finished(m, m.submit('enhance', {'kind': 'url', 'url': 'https://youtu.be/xyz'}, {}).id)
        assert library.get(xyz.id)['original']['downloaded'] is True
        source = {'kind': 'file', 'name': master.name, 'path': str(master), '_library_group': 'youtube:abc'}
        derived = library.get(finished(m, m.submit('enhance', source, {}).id).id)
        assert derived['original']['file'] == library.get(xyz.id)['original']['file']
        assert derived['original']['downloaded'] is False

        first = job()
        assert downloads == [LINK] and first.result['stream'] == STREAM and library.get(first.id)['stream'] == STREAM
        monkeypatch.setattr(youtube, 'stream_info', lambda path: None)  # a reused file carries its download's
        second = job()  # the kept original is used: no second download
        result = m.public(second.id)['result']
        assert downloads == [LINK] and result['reused_original'] is True and result['stream'] == STREAM
        assert common.REUSED_NOTE in notes(m, second.id)
        assert library.get(second.id)['original']['file'] == library.get(first.id)['original']['file']

        # tags name the version: three Enhance entries of this recording so far (an upload: not counted)
        link = {'kind': 'url', 'url': LINK, '_probe': PROBE}
        assert common.next_version(link, 'enhance') == 4 and common.version_label('Enhanced', 4) == '(Enhanced 4)'
        assert common.next_version({'kind': 'file', 'path': str(master)}, 'enhance') is None
        assert (
            library.public(library.get(first.id))['original']['key'] == library.get(first.id)['original']['sha1'][:16]
        )
        line = common.made_by('Club / DJ set', -12.5)
        assert re.fullmatch(r'AudioLab \S+ · Club / DJ set · -12\.5 LUFS · \d{4}-\d\d-\d\d', line)
        tags = common.tags_for(common.Acquired(path=master, title='Song', url=LINK), ' (Enhanced 4)', line)
        assert tags['title'] == 'Song (Enhanced 4)' and tags['comment'] == f'{LINK}\n{line}'

        SETTINGS.update({'reuse_originals': False})  # off: always download
        assert 'reused_original' not in job().result and len(downloads) == 2
        library.remove(first.id)  # version 2 of 4 removed: the next is still 5, a number is never given twice
        assert common.next_version(link, 'enhance') == 5

    finally:
        m.shutdown()


# ------------------------------------------------------------------ the opt-in Trash
def results_runner(job, ctx):
    """Like Enhance: '<title> (Enhanced)/' with the result, Tracks/ and a saved cover; the original kept."""
    folder = SETTINGS.output_dir / f'{job.title} (Enhanced)'
    files = []
    for i, name in enumerate([f'{job.title} (Enhanced).flac', f'Tracks/01 - {job.title}.flac', 'cover.jpg']):
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'x' * 100)
        files.append({'id': f'f{i}', 'name': p.name, 'path': str(p), 'format': 'flac24'})
    files[1].update(group='tracks', track=1, start_s=0.0, end_s=1.0)
    files[2].update(kind='cover', format='jpg')
    original = ctx.work_dir / 'scratch' / 'audio.webm'
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b'webm' * 1000)
    return {'headline': 'Done', 'output_dir': str(folder), 'files': files, '_library': {'original': str(original)}}


def test_trash_is_opt_in_and_never_a_permanent_delete(tmp_path, monkeypatch):
    moved = []
    unavailable = False
    bin_ = tmp_path / 'fake-trash'
    bin_.mkdir()

    def fake_trash(path):  # the files survive in the "Trash": nothing is ever deleted
        if unavailable:
            raise AudioLabError("AudioLab can't use the Trash on this system.", hints=['Do it yourself.'])
        moved.append(Path(path))
        os.replace(path, bin_ / f'{len(moved)}-{Path(path).name}')

    monkeypatch.setattr(system, 'trash', fake_trash)
    m = JobManager(runner=results_runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    source = {'kind': 'url', 'url': LINK, '_probe': PROBE}
    keep, song, other, linked = (
        library.get(finished(m, m.submit('enhance', source, {}, title=t).id).id) for t in 'ABCD'
    )
    music = tmp_path / 'music'
    master = music / 'Master.wav'
    master.parent.mkdir()
    master.write_bytes(b'riff')
    imported = dict(keep, id='j_20260101_000000_aaaaaa', task='import', original=None, output_dir=str(music))
    imported.update(title='Master', source={'kind': 'file', 'name': master.name}, created_at=time.time() + 1)
    imported['files'] = [{'id': 'f0', 'name': master.name, 'path': str(master), 'kind': 'imported'}]
    library._write(imported)
    (group,) = library.groups()  # the newest version is the import: the recording keeps its own name
    assert group['title'] == 'D' and group['source']['kind'] == 'url' and group['entries'][0]['task'] == 'import'

    def paths(entry):
        return [library.file_path(entry, f['id']) for f in entry['files']]

    with TestClient(create_app(local=False, manager=m)) as c:
        unavailable = True  # no Trash on this system: 409, nothing removed
        r = c.delete(f'/api/library/{song["id"]}?trash_files=1')
        assert r.status_code == 409 and r.json()['hints'] and library.get(song['id'])
        unavailable = False

        r = c.delete(f'/api/library/{song["id"]}').json()  # the default: forget only
        assert r['trashed'] == [] and all(p.is_file() for p in paths(song)) and moved == []

        r = c.delete(f'/api/library/{other["id"]}?trash_files=1').json()
        assert sorted(r['trashed']) == ['01 - C.flac', 'C (Enhanced).flac', 'cover.jpg'] and r['trash_failed'] == []
        folder = SETTINGS.output_dir / 'C (Enhanced)'
        assert moved[-2:] == [folder / 'Tracks', folder]  # emptied output folders follow, never the output root
        assert SETTINGS.output_dir.is_dir() and len(list(bin_.iterdir())) == 5

        if sys.platform != 'win32':  # links need extra rights there
            # an output folder replaced by a link to another music folder holding a file of the same name
            elsewhere = tmp_path / 'other music'
            elsewhere.mkdir()
            (elsewhere / 'D (Enhanced).flac').write_bytes(b'theirs')
            shutil.rmtree(SETTINGS.output_dir / 'D (Enhanced)')
            (SETTINGS.output_dir / 'D (Enhanced)').symlink_to(elsewhere, target_is_directory=True)
            count = len(moved)
            r = c.delete(f'/api/library/{linked["id"]}?trash_files=1').json()
            assert r['trashed'] == [] and r['trash_failed'] == ['D (Enhanced).flac'] and len(moved) == count
            assert (elsewhere / 'D (Enhanced).flac').read_bytes() == b'theirs'
            assert (SETTINGS.output_dir / 'D (Enhanced)').is_symlink()
            # an imported file whose folder was later replaced by a link: refused, the other file stays
            gone = tmp_path / 'masters'
            gone.mkdir()
            song_entry = dict(imported, id='j_20260101_000000_bbbbbb', output_dir=str(gone))
            song_entry['files'] = [{'id': 'f0', 'name': 'Song.wav', 'path': str(gone / 'Song.wav'), 'kind': 'imported'}]
            library._write(song_entry)
            (elsewhere / 'Song.wav').write_bytes(b'theirs')
            gone.rmdir()
            gone.symlink_to(elsewhere, target_is_directory=True)
            r = c.delete(f'/api/library/{song_entry["id"]}?trash_files=1').json()
            assert r['trash_failed'] == ['Song.wav'] and (elsewhere / 'Song.wav').read_bytes() == b'theirs'
        else:
            c.delete(f'/api/library/{linked["id"]}')

        r = c.post(f'/api/library/{keep["id"]}/keep-only', json={'trash_files': True}).json()
        assert r['removed'] == 1 and r['trashed'] == ['Master.wav']  # the imported file, never its folder
        assert music.is_dir() and moved[-1] == master and all(p.is_file() for p in paths(keep))
    m.shutdown()


def test_freedesktop_trash_moves_never_deletes(tmp_path, monkeypatch):
    if sys.platform == 'win32':
        pytest.skip('POSIX file layout')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
    song = tmp_path / 'song 1.wav'
    song.write_bytes(b'riff')
    system._trash_freedesktop(song)
    home = tmp_path / 'xdg' / 'Trash'
    assert not song.exists() and (home / 'files' / 'song 1.wav').read_bytes() == b'riff'
    info = (home / 'info' / 'song 1.wav.trashinfo').read_text(encoding='utf-8').splitlines()
    assert info[:2] == ['[Trash Info]', f'Path={str(song).replace(" ", "%20")}'] and info[2].startswith('DeletionDate=')

    blocker = tmp_path / 'not-a-folder'
    blocker.write_text('x')
    monkeypatch.setenv('XDG_DATA_HOME', str(blocker))  # no Trash can be made: an error, the file stays
    song.write_bytes(b'riff')
    with pytest.raises(AudioLabError):
        system._trash_freedesktop(song)
    assert song.is_file()


# ----------------------------------------------------------------------- importing
def test_import_measures_a_file_where_it_is(tmp_path):
    music = tmp_path / 'music'
    music.mkdir()
    master = music / 'My Master.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=f=440:d=2', '-ac', '2', str(master)], what='test tone')
    (music / 'notes.txt').write_text('not audio')
    with TestClient(create_app(local=False, token='t')) as c:  # only on this computer
        assert (
            c.post('/api/library/import', headers={'X-AudioLab-Token': 't'}, json={'path': str(master)}).status_code
            == 403
        )
    m = JobManager(runner=tasks.run_job, stages_for=tasks.stages_for, root=tmp_path / 'jobs', workers=1)
    m.start()
    with TestClient(create_app(local=True, manager=m), base_url='http://127.0.0.1:8765') as c:
        for path in (music / 'notes.txt', music / 'missing.wav'):
            r = c.post('/api/library/import', json={'path': str(path)})
            assert r.status_code == 400 and r.json()['hints']
        assert c.post('/api/jobs', data={'task': 'import', 'url': LINK}).status_code == 400  # only the Library route

        job = c.post('/api/library/import', json={'path': str(master), 'group': 'youtube:abc'}).json()
        assert job['task'] == 'import' and job['title'] == 'My Master'
        done = finished(m, job['id'])
        assert done.result['library_id'] == job['id']  # the Library view reloads on it
        entry = c.get(f'/api/library/{job["id"]}').json()
        (f,) = entry['files']
        assert f['kind'] == 'imported' and f['path'] == str(master) and f['exists']
        assert entry['group'] == 'youtube:abc' and entry['original']['kept'] is False
        assert library.groups()[0]['title'] == 'My Master'  # a group of imports only: named after the import
        # ffmpeg's sine (−18 dBFS) made stereo (−3 dB each side): about −21.8 LUFS, −21.1 dBTP
        measured = entry['measured']
        assert -23 < measured['integrated_lufs'] < -20.5 and -22 < measured['true_peak_dbtp'] < -20
        assert abs(measured['duration_s'] - 2.0) < 0.05
        assert entry['loudness'] == {'original_lufs': None, 'result_lufs': done.result['metrics']['after']['lufs']}
    m.shutdown()


def test_entries_keep_a_checked_tone_timeline(tmp_path):
    timeline = {
        'step_s': 60,
        'bands': [
            {'key': 'bass', 'label': 'Bass', 'hz': [80, 200]},
            {'key': 'top', 'label': 'Top', 'hz': [12000, 20000]},
        ],
        'values': [[0.43, -0.2], None, [1.0, -1.06]],
    }

    def runner(job, ctx):
        return {**results_runner(job, ctx), 'analysis': {'tone_timeline': job.source['_timeline']}}

    m = JobManager(runner=runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()

    def entry(value):
        source = {'kind': 'url', 'url': LINK, '_probe': PROBE, '_timeline': value}
        return library.get(finished(m, m.submit('enhance', source, {}, title='T').id).id)

    kept, broken = entry(timeline), entry(dict(timeline, values=[[0.1], None]))  # a row that doesn't fit
    assert kept['tone_timeline']['values'] == [[0.4, -0.2], None, [1.0, -1.1]] and 'tone_timeline' not in broken
    with TestClient(create_app(local=False, manager=m)) as c:  # the list stays small: the data on request
        listed = {e['id']: e for g in c.get('/api/library').json()['groups'] for e in g['entries']}
        assert listed[kept['id']]['has_tone_timeline'] is True and 'tone_timeline' not in listed[kept['id']]
        assert 'has_tone_timeline' not in listed[broken['id']]
        assert c.get(f'/api/library/{kept["id"]}/tone-timeline').json() == kept['tone_timeline']
        assert c.get(f'/api/library/{broken["id"]}/tone-timeline').status_code == 404


def test_a_result_file_as_the_next_jobs_source(tmp_path):
    m = JobManager(runner=results_runner, stages_for=lambda job: STAGES, root=tmp_path / 'jobs', workers=1)
    m.start()
    old = finished(m, m.submit('enhance', {'kind': 'url', 'url': LINK, '_probe': PROBE}, {}, title='Song').id)
    with TestClient(create_app(local=False, manager=m)) as c:

        def check(file_id):
            return c.post('/api/jobs', data={'task': 'analyze', 'source_job': old.id, 'source_file': file_id})

        (job,) = check('f0').json()['jobs']  # "Check the result"
        new = m.get(job['id'])
        assert new.source['kind'] == 'file' and new.title == 'Song (Enhanced)'
        assert Path(new.source['path']).parent == m.input_dir(new.id)
        assert Path(new.source['path']).read_bytes() == Path(old.result['files'][0]['path']).read_bytes()
        assert check('f9').status_code == 404
        Path(old.result['files'][1]['path']).unlink()
        assert check('f1').status_code == 410


def test_stream_line_and_chapters():
    assert youtube.parse_stream('140|mp4a.40.2|129.48|44100|2') == {
        'format_id': '140',
        'codec': 'aac',
        'kbps': 129,
        'sample_rate': 44100,
        'channels': 2,
    }
    assert youtube.parse_stream('NA|NA|NA|NA|NA') is None
    tracks = [{'n': 1, 'title': 'Intro', 'start_s': 0.0, 'end_s': 60.0, 'artist': 'x'}]
    acq = common.Acquired(path=Path('a.webm'), title='Set', metadata={'tracks': tracks, 'tracks_source': 'chapters'})
    assert common.chapters(acq) == [{'n': 1, 'title': 'Intro', 'start_s': 0.0, 'end_s': 60.0}]
    acq.metadata['tracks_source'] = 'description'  # a tracklist is not a chapter list
    assert common.chapters(acq) == []
