"""Media layer: tracklists → tracks, sample-accurate track export, downloader self-repair.

Offline: synthetic audio from ffmpeg's lavfi, yt-dlp and pip are mocked.
"""

import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audiolab.context import TaskContext
from audiolab.errors import DownloadError
from audiolab.media import export, ffmpeg, proc, youtube
from audiolab.media.ffmpeg import run_ffmpeg
from audiolab.tasks import common

SR = 44100


@pytest.fixture(scope='session')
def noise_set(tmp_path_factory) -> Path:
    """10 s of stereo 16-bit noise: every sample differs, so any misplaced cut shows."""
    seconds = 10.0
    path = tmp_path_factory.mktemp('media') / 'set.wav'
    run_ffmpeg(
        [
            '-f',
            'lavfi',
            '-i',
            f'anoisesrc=d={seconds}:c=white:r={SR}:a=0.3',
            '-ac',
            '2',
            '-c:a',
            'pcm_s16le',
            str(path),
        ]
    )
    return path


# ------------------------------------------------------------ tracklists
def test_tracks_from_chapters():
    chapters = [
        {'start_s': 0.0, 'end_s': 200.0, 'title': '01. Intro'},
        {'start_s': 200.0, 'end_s': 412.5, 'title': 'Artist A - Song (Official Video)'},
        {'start_s': 412.5, 'end_s': 600.0, 'title': '▶ Artist B – Other'},
    ]
    tracks, src = youtube.make_tracks(chapters, '00:00 ignored\n01:00 ignored\n02:00 ignored', 600.0)
    assert src == 'chapters'
    assert [t['title'] for t in tracks] == ['Intro', 'Artist A - Song', 'Artist B – Other']
    assert [(t['start_s'], t['end_s']) for t in tracks] == [(0.0, 200.0), (200.0, 412.5), (412.5, 600.0)]
    assert [t['n'] for t in tracks] == [1, 2, 3]
    # one chapter is not a tracklist; unknown duration falls back to the chapter ends
    assert youtube.make_tracks(chapters[:1], '', 600.0) == ([], None)
    assert youtube.make_tracks(chapters, '', None)[0][-1]['end_s'] == 600.0


def test_tracks_from_description():
    desc = (
        'Recorded live! Best moment at 3:45 lol\n\nTracklist:\n'
        '01. 00:12 Artist One - First Track\n'
        '[05:30] Artist Two – Second (Extended Mix)\n'
        '12:00 - 15:00 Third Artist - Third\n'
        '12:00 Duplicate start\n'
        '1:02:03 | Fourth - Beyond the end\n'
    )
    tracks, src = youtube.make_tracks([], desc, 1500.0)
    assert src == 'description'
    assert [t['title'] for t in tracks] == [
        'Artist One - First Track',
        'Artist Two – Second (Extended Mix)',
        'Third Artist - Third',
    ]
    # lead-in ≤ 30 s joins track 1, end = next start, last = duration, past-the-end entry dropped
    assert [(t['start_s'], t['end_s']) for t in tracks] == [(0.0, 330.0), (330.0, 720.0), (720.0, 1500.0)]
    assert youtube.make_tracks([], '00:00 A\n01:00 B', 600.0) == ([], None)  # fewer than 3
    assert youtube.make_tracks([], '00:00 A\n01:00 B\n02:00 C', None) == ([], None)  # no duration
    assert youtube.make_tracks([], '', 600.0) == ([], None)


# ------------------------------------------------------------ track export
def test_export_tracks_sample_accurate(noise_set, tmp_path):
    src = noise_set
    tracks = [
        {'n': 1, 'title': 'Artist A - Opening', 'start_s': 0.0, 'end_s': 3.25},
        {'n': 2, 'title': 'Interlude', 'start_s': 3.25, 'end_s': 7.5},
        {'n': 3, 'title': 'Artist C - Closer', 'start_s': 7.5, 'end_s': 10.0},
    ]
    folder = tmp_path / 'tracks_wav'
    out = export.export_tracks(src, tracks, folder, 'wav16', tags={'title': 'My Set', 'artist': 'DJ X'})
    assert [o['name'] for o in out] == [
        '01 - Artist A - Opening.wav',
        '02 - Interlude.wav',
        '03 - Artist C - Closer.wav',
    ]
    assert [o['track'] for o in out] == [1, 2, 3] and all(o['group'] == 'tracks' for o in out)
    whole, _ = sf.read(str(src), dtype='int16')
    parts = [sf.read(o['path'], dtype='int16')[0] for o in out]
    for part, t in zip(parts, tracks):
        assert abs(len(part) / SR - (t['end_s'] - t['start_s'])) <= 0.001, (len(part), t)
    joined = np.concatenate(parts)
    assert joined.shape == whole.shape and np.array_equal(joined, whole), 'cuts must be gapless and exact'
    # never overwrite: a second run gets " (2)" names
    again = export.export_tracks(src, tracks[:1], folder, 'wav16')
    assert again[0]['name'] == '01 - Artist A - Opening (2).wav'


def test_export_tracks_tags_and_long_names(noise_set, tmp_path):
    from mutagen.id3 import ID3

    src = noise_set
    cjk = '東京の夜明け' * 40 + ' 🎧'
    tracks = [
        {'n': 1, 'title': 'Artist A - Opening', 'start_s': 0.0, 'end_s': 4.0},
        {'n': 2, 'title': cjk, 'start_s': 4.0, 'end_s': 11.0},
    ]  # end past the file: runs to the end
    out = export.export_tracks(
        src,
        tracks,
        tmp_path / 'tracks_mp3',
        'mp3_320',
        tags={'title': 'My Set', 'artist': 'DJ X', 'comment': 'https://example.com/v'},
    )
    first = ID3(out[0]['path'])
    assert str(first['TIT2']) == 'Opening' and str(first['TPE1']) == 'Artist A'
    assert str(first['TALB']) == 'My Set' and str(first['TPE2']) == 'DJ X' and str(first['TRCK']) == '1/2'
    second = ID3(out[1]['path'])
    assert str(second['TPE1']) == 'DJ X' and str(second['TRCK']) == '2/2'
    name = Path(out[1]['path']).name
    assert name.startswith('02 - 東京の夜明け') and len(name.encode('utf-8')) < 200
    assert out[1]['end_s'] == 10.0


def test_commit_without_hard_links(tmp_path, monkeypatch):
    folder = tmp_path / 'nolink'
    folder.mkdir()
    (folder / 'song.mp3').write_text('existing')
    staging = folder / '.song.partial.mp3'
    staging.write_text('new')

    def no_link(src, dst):
        raise PermissionError(1, 'Operation not permitted')  # exFAT / FAT / some network shares

    monkeypatch.setattr(os, 'link', no_link)
    final = export._commit(staging, folder / 'song.mp3')
    assert final.name == 'song (2).mp3' and final.read_text() == 'new'
    assert (folder / 'song.mp3').read_text() == 'existing' and not staging.exists()


@pytest.mark.skipif(os.name == 'nt', reason='Windows copies ffmpeg instead of linking it')
def test_ffmpeg_shim_concurrent():
    import threading

    results = []
    threads = [threading.Thread(target=lambda: results.append(ffmpeg._ensure_shim(sys.executable))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8 and len(set(results)) == 1
    assert os.readlink(results[0]) == sys.executable
    assert not [p for p in results[0].parent.iterdir() if p.name.endswith('.tmp')]


# ------------------------------------------------------------ PCM for the compare player
@pytest.mark.parametrize(
    'name,rate,args',
    [
        ('48.wav', 48000, ['-c:a', 'pcm_s16le']),
        ('48.flac', 48000, []),
        ('48.mp3', 48000, ['-b:a', '192k']),
        ('44.wav', 44100, ['-c:a', 'pcm_s16le']),
        ('44.mp3', 44100, ['-b:a', '192k']),
        # AAC starts decoding a little after a seek point (without noise filling, which is random by design)
        ('44.m4a', 44100, ['-c:a', 'aac', '-aac_pns', '0', '-b:a', '192k']),
        # WebM keeps whole milliseconds: a seek lands 1 ms early (YouTube's originals)
        ('48.webm', 48000, ['-c:a', 'libopus', '-b:a', '128k']),
    ],
)
def test_decode_pcm_chunks_join_like_a_full_decode(name, rate, args, tmp_path, monkeypatch):
    """Chunk after chunk is bit-identical to the same slice of one full decode, also at positions that
    aren't whole seconds and across the switch from decoding the start to seeking (after 0.48 s)."""
    monkeypatch.setattr(proc, '_POLL_S', 0.01)  # short ffmpeg runs: don't wait 0.2 s for each
    src = tmp_path / name
    noise, tone = f'anoisesrc=d=3:c=pink:r={rate}:a=0.3', f'sine=f=441:r={rate}:d=3'
    run_ffmpeg(['-f', 'lavfi', '-i', noise, '-f', 'lavfi', '-i', tone, '-filter_complex', 'amerge', *args, str(src)])
    g = ffmpeg.PCM_GRID  # 1920 frames = 0.04 s
    pcm = lambda offset, frames: np.frombuffer(ffmpeg.decode_pcm(src, offset, frames), np.float32).reshape(-1, 2)
    full = pcm(0, 100 * g)
    assert len(full) == 3 * 48000 and np.abs(full).max() > 0.1
    # Opus decoders can differ at -150 dB (float rounding of their filter state); every other format: not at all
    same = (lambda a, b: a.shape == b.shape and np.abs(a - b).max() < 1e-6) if name.endswith('webm') else np.array_equal
    first, second = pcm(5 * g, 10 * g), pcm(15 * g, 41 * g)  # 0.2–0.6 s from the start, 0.6–2.24 s by a seek
    assert same(np.concatenate([first, second]), full[5 * g : 56 * g])
    last = pcm(70 * g, 10 * g)  # 2.8 s: only 0.2 s left
    assert len(last) == 5 * g and same(last, full[70 * g :])
    if name == '48.wav':
        assert ffmpeg.decode_pcm(src, 80 * g, g) == b''  # past the end
        for offset, frames in ((640, g), (0, 640), (0, 0), (-g, g)):
            with pytest.raises(ValueError):
                ffmpeg.decode_pcm(src, offset, frames)


# ------------------------------------------------------------ file tags
def test_converted_file_keeps_its_tags(tmp_path):
    from mutagen.id3 import ID3

    from audiolab import tasks

    meta = tmp_path / 'chapters.txt'
    meta.write_text(
        ';FFMETADATA1\ntitle=Real Title\nartist=Real Artist\nalbum=Real Album\ntrack=3/12\n'
        'date=2021\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Chapter One\n',
        encoding='utf-8',
    )
    src = tmp_path / 'track01.opus'  # Ogg/Opus keeps its tags on the audio stream, not the file
    run_ffmpeg(
        [
            '-f',
            'lavfi',
            '-i',
            'sine=f=440:d=2',
            '-i',
            str(meta),
            '-map_metadata',
            '1',
            '-map_chapters',
            '1',
            '-c:a',
            'libopus',
            str(src),
        ]
    )
    info = ffmpeg.probe_media(src)
    assert info['tags'] == {
        'title': 'Real Title',
        'artist': 'Real Artist',
        'album': 'Real Album',
        'track': '3/12',
        'date': '2021',
    }, info['tags']
    source = {'kind': 'file', 'name': src.name, 'path': str(src)}
    opts = tasks.validate('download', {'format': 'mp3'}, source)
    spec = tasks.get('download')
    ctx = TaskContext('t', tmp_path / 'tagjob', opts, spec.stages(opts, source))
    out = Path(spec.run(ctx, source)['files'][0]['path'])
    tags = ID3(out)
    assert out.name == 'Real Title.mp3'
    assert [str(tags[k]) for k in ('TIT2', 'TPE1', 'TALB', 'TRCK')] == [
        'Real Title',
        'Real Artist',
        'Real Album',
        '3/12',
    ]
    # an MP3 with ID3 title/artist under an ugly file name keeps them when converted
    ugly = tmp_path / 'AUD-20260105-WA0007 (1).mp3'
    run_ffmpeg(
        [
            '-f',
            'lavfi',
            '-i',
            'sine=f=330:d=2',
            '-c:a',
            'libmp3lame',
            '-id3v2_version',
            '3',
            '-metadata',
            'title=Nice Song',
            '-metadata',
            'artist=Nice Artist',
            str(ugly),
        ]
    )
    source = {'kind': 'file', 'name': ugly.name, 'path': str(ugly)}
    opts = tasks.validate('download', {'format': 'm4a'}, source)
    ctx = TaskContext('t', tmp_path / 'tagjob2', opts, spec.stages(opts, source))
    out = Path(spec.run(ctx, source)['files'][0]['path'])
    got = ffmpeg.probe_media(out)['tags']
    assert out.name == 'Nice Song.m4a' and (got.get('title'), got.get('artist')) == ('Nice Song', 'Nice Artist'), got
    # placeholder titles fall back to the file name
    assert common._PLACEHOLDER_TITLE.match('Track 01') and not common._PLACEHOLDER_TITLE.match('Track One')


# ------------------------------------------------------------ self-repair
def test_self_repair_retries_once(noise_set, tmp_path, monkeypatch):
    calls = {'probe': 0, 'update': 0}
    wav = noise_set
    meta = {
        'kind': 'single',
        'title': 'Artist - Song',
        'uploader': 'Artist',
        'duration_s': 10.0,
        'chapters': [],
        'description': '00:00 A\n00:03 B\n00:06 C',
    }  # no 'tracks': older probe

    def probe(url, progress=None):
        calls['probe'] += 1
        if calls['probe'] == 1:
            raise DownloadError('The site changed something', fix='update_downloader')
        return dict(meta)

    def download_audio(url, dest, progress=None):
        dest.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(wav, dest / 'audio.wav'))

    def update_ytdlp(progress=None):
        calls['update'] += 1
        return {'ok': True, 'before': '2026.08.19', 'after': '2026.10.01', 'message': ''}

    monkeypatch.setattr(youtube, 'probe', probe)
    monkeypatch.setattr(youtube, 'download_audio', download_audio)
    monkeypatch.setattr(youtube, 'update_ytdlp', update_ytdlp)
    monkeypatch.setattr(youtube, '_last_repair', {})
    notes = []
    ctx = TaskContext(
        't',
        tmp_path / 'job',
        {},
        [('acquire', 'Getting audio', 1)],
        emit_note=lambda kind, msg, detail: notes.append(msg),
    )
    acq = common.acquire(ctx, {'kind': 'url', 'url': 'https://youtu.be/x'}, ctx.stage('acquire'), task='download')
    assert calls == {'probe': 2, 'update': 1}
    assert any('2026.08.19 → 2026.10.01' in n for n in notes)
    assert [t['title'] for t in acq.metadata['tracks']] == ['A', 'B', 'C']
    # within 6 h: no second update, the original error comes back
    calls['probe'] = 0
    with pytest.raises(DownloadError) as original:
        common.acquire(ctx, {'kind': 'url', 'url': 'https://youtu.be/y'}, ctx.stage('acquire'), task='download')
    assert original.value.fix == 'update_downloader' and calls['update'] == 1
    # the retry fails too: no "update the downloader" advice again, other hints kept
    youtube._last_repair.clear()

    def blocked(url, progress=None):
        raise DownloadError('Bot check', hints=['Wait a while.', youtube._UPDATE_HINT], fix='update_downloader')

    monkeypatch.setattr(youtube, 'probe', blocked)
    with pytest.raises(DownloadError) as retried:
        common.acquire(ctx, {'kind': 'url', 'url': 'https://youtu.be/z'}, ctx.stage('acquire'), task='download')
    assert retried.value.fix == 'retry' and calls['update'] == 2
    assert 'already updated' in retried.value.hints[0] and retried.value.hints[1:] == ['Wait a while.']


def test_sign_in_errors():
    from audiolab.settings import SETTINGS

    bot = 'ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies'
    age = 'ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.'
    s = SETTINGS.get()  # conftest gives every test fresh preferences
    s.cookies_browser = ''
    assert '--cookies-from-browser' not in youtube.ytdlp_cmd()
    for text in (bot, age):
        e = youtube._friendly_error(text, 'u')
        assert e.fix == 'use_cookies' and 'YouTube sign-in' in e.hints[0], (e, e.hints)
    s.cookies_browser = 'firefox'
    assert youtube.ytdlp_cmd()[-2:] == ['--cookies-from-browser', 'firefox']
    assert youtube._friendly_error(bot, 'u').fix == 'update_downloader'  # signed in, still blocked
    assert youtube._friendly_error(age, 'u').fix is None
    e = youtube._friendly_error('ERROR: could not find firefox cookies database in /x', 'u')
    assert e.fix == 'use_cookies' and str(e).startswith(
        'Could not read the YouTube sign-in from Firefox — close Firefox'
    )
    s.cookies_browser = 'chrome'
    e = youtube._friendly_error('WARNING: find-generic-password failed\n' + bot, 'u')
    assert e.fix == 'use_cookies' and 'Chrome' in str(e) and any('keychain' in h for h in e.hints)


def test_age_days():
    assert youtube.ytdlp_age_days('2020.01.02') > 2000
    assert youtube.ytdlp_age_days('2026.13.40') is None and youtube.ytdlp_age_days(None) is None


# ------------------------------------------------------------ pasted tracklists
def test_parse_tracklist_accepts_common_forms():
    text = (
        '0:00 Artist A - Opening\n'
        '\n'
        '[00:05:30] Artist Two – Second (Extended Mix)\n'
        '3. 12:00 Third\n'
        'Fourth Track 15:30\n'
        '16:00 - 20:00 Ranged\n'
        'Artist - 21:15 Track\n'
        'Artist - Title [22:15] (Live)\n'
        '75:00\n'
    )
    tracks, errors = youtube.parse_tracklist(text, 5000.0)
    assert errors == []
    assert [t['title'] for t in tracks] == [
        'Artist A - Opening',
        'Artist Two – Second (Extended Mix)',
        'Third',
        'Fourth Track',
        'Ranged',
        'Artist - Track',  # text on both sides of a time in the middle is kept
        'Artist - Title (Live)',
        'Track 8',
    ]
    assert [t['start_s'] for t in tracks] == [0.0, 330.0, 720.0, 930.0, 960.0, 1275.0, 1335.0, 4500.0]
    assert tracks[-1]['end_s'] == 5000.0 and [t['n'] for t in tracks] == [1, 2, 3, 4, 5, 6, 7, 8]
    # one line is a list; a late first start stays where it is (nothing invented before it)
    assert youtube.parse_tracklist('Only Song 2:00', 300.0) == (
        [{'n': 1, 'title': 'Only Song', 'start_s': 120.0, 'end_s': 300.0}],
        [],
    )
    assert youtube.parse_tracklist('0:10 A\n0:20 B', None)[0][-1]['end_s'] is None  # length unknown
    assert youtube.parse_tracklist('  \n', None) == ([], [])


def test_parse_tracklist_reports_problems_by_line():
    text = 'Tracklist:\n0:00 A\n10:00 B\n07:00 C\n10:00 D\n1:75:00 E\n12:61 F\n1:02:00 G\n58:10 H\n15:00 I'
    tracks, errors = youtube.parse_tracklist(text, 3490.0)  # 58:10 long
    assert [t['title'] for t in tracks] == ['A', 'B', 'I']  # the good lines, starts untouched
    assert errors == [
        {'line': 1, 'message': 'no time found (expected e.g. "12:30 Artist - Title")'},
        {'line': 4, 'message': 'time goes backwards (07:00 comes after 10:00 on line 3)'},
        {'line': 5, 'message': '10:00 is the same time as line 3'},
        {'line': 6, 'message': '1:75:00 is not a valid time'},
        {'line': 7, 'message': '12:61 is not a valid time'},
        {'line': 8, 'message': '1:02:00 is after the end (58:10)'},
        {'line': 9, 'message': '58:10 is after the end (58:10)'},
    ]
    assert youtube.tracklist_problem(errors).startswith('line 1: no time found') and '(and 4 more)' in (
        youtube.tracklist_problem(errors)
    )


def test_tracklist_option_validated(tmp_path):
    from audiolab import tasks
    from audiolab.errors import AudioLabError

    file_src = {'kind': 'file', 'name': 'set.wav', 'path': str(tmp_path / 'set.wav')}
    opts = tasks.validate('download', {'format': 'mp3', 'split_tracks': True}, file_src)
    assert opts['split_tracks'] is False and 'tracklist' not in opts  # a file alone has no tracks
    opts = tasks.validate(
        'download', {'format': 'mp3', 'split_tracks': True, 'tracklist': ' 0:00 A\n1:00 B '}, file_src
    )
    assert opts['split_tracks'] is True and opts['tracklist'] == '0:00 A\n1:00 B'
    with pytest.raises(AudioLabError, match='line 2: time goes backwards'):
        tasks.validate('enhance', {'tracklist': '5:00 A\n1:00 B'}, file_src)
    url_src = {'kind': 'url', 'url': 'https://youtu.be/x', 'duration_s': 120.0}
    with pytest.raises(AudioLabError, match=r'3:00 is after the end \(2:00\)'):
        tasks.validate('download', {'tracklist': '0:00 A\n3:00 B'}, url_src)
    assert tasks.validate('enhance', {'tracklist': '0:00 A'}, url_src)['tracklist'] == '0:00 A'


def test_file_split_with_pasted_tracklist(noise_set, tmp_path):
    from audiolab import tasks
    from audiolab.errors import AudioLabError

    spec = tasks.get('download')
    source = {'kind': 'file', 'name': 'set.wav', 'path': str(noise_set)}
    opts = tasks.validate(
        'download', {'format': 'flac', 'split_tracks': True, 'tracklist': '0:02 A - One\n0:06 Two'}, source
    )
    ctx = TaskContext('t', tmp_path / 'job', opts, spec.stages(opts, source))
    result = spec.run(ctx, source)
    tracks = [f for f in result['files'] if f.get('group') == 'tracks']
    assert [(t['start_s'], t['end_s']) for t in tracks] == [(2.0, 6.0), (6.0, 10.0)]
    assert any('your tracklist' in s and 'first 0:02' in s for s in result['summary'])
    assert result['_library']['markers_source'] == 'pasted' and 'original' not in result['_library']
    # the length of a file is only known when it runs: a start past the end fails readably
    opts = tasks.validate('download', {'format': 'mp3', 'tracklist': '0:00 A\n0:20 B'}, source)
    ctx = TaskContext('t', tmp_path / 'job2', opts, spec.stages(opts, source))
    with pytest.raises(AudioLabError, match=r"doesn't fit this recording — line 2: 0:20 is after the end"):
        spec.run(ctx, source)


# ------------------------------------------------------------ format facts
def test_format_facts_in_health(tmp_path):
    from fastapi.testclient import TestClient

    from audiolab.api import create_app
    from audiolab.jobs import JobManager

    mgr = JobManager(runner=lambda job, ctx: {}, stages_for=lambda job: [], root=tmp_path / 'jobs')
    with TestClient(create_app(local=False, token='t', manager=mgr)) as c:
        health = c.get('/api/health?token=t').json()
    for kind, choices in health['formats'].items():
        for choice in choices:
            assert 2 <= len(choice['facts']) <= 4 and isinstance(choice['cover'], bool), (kind, choice)
            assert {'key', 'label', 'description', 'kind'} <= choice.keys()  # the 2.2 shape stays
    download = {c['key']: c for c in health['formats']['download']}
    assert [k for k, c in download.items() if not c['cover']] == ['opus', 'wav', 'original', 'video']
    assert 'same sound' not in download['opus']['description'] and 're-encoded' in download['opus']['description']
    assert all('no quality' in ' '.join(download[k]['facts']).lower() for k in ('flac', 'wav'))
    enhance = {c['key']: c for c in health['formats']['enhance']}
    assert not enhance['wav16']['cover'] and not enhance['opus_160']['cover'] and enhance['flac24']['cover']
    assert 'AirPods' not in enhance['alac16']['description']
    assert "don't play lossless" in ' '.join(enhance['alac16']['facts'])
    assert '256 kbps' in health['format_notes']['source']


# ------------------------------------------------------------ cover images
def test_prepare_cover_checks_and_downscales(tmp_path):
    from audiolab.errors import AudioLabError

    png = tmp_path / 'wide.png'
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=s=2000x1000:d=1', '-frames:v', '1', str(png)])
    out = export.prepare_cover(png, tmp_path / 'cover.jpg')
    info = ffmpeg.probe_media(out)
    assert (info['video_codec'], info['width'], info['height']) == ('mjpeg', 1400, 700)
    small = export.cover_jpeg(out, tmp_path / 'small.jpg', max_px=1000, square=True)
    assert (ffmpeg.probe_media(small)['width'], ffmpeg.probe_media(small)['height']) == (700, 700)  # never enlarged
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=d=1', str(tmp_path / 'tone.mp3')])
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=s=64x64:d=0.2', '-c:v', 'mjpeg', str(tmp_path / 'clip.avi')])
    for name, made in [('fake.png', None), ('song.jpg', 'tone.mp3'), ('clip.jpg', 'clip.avi')]:
        bad = tmp_path / name  # text, audio, an MJPEG video — each named like a picture
        if made:
            (tmp_path / made).rename(bad)
        else:
            bad.write_bytes(b'not a picture at all' * 10)
        with pytest.raises(AudioLabError, match='not a JPG, PNG or WebP'):
            export.prepare_cover(bad, tmp_path / 'x.jpg')
    bomb = tmp_path / 'bomb.png'  # a header declaring 50 000 × 50 000 pixels: refused before ffmpeg
    bomb.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + (50000).to_bytes(4, 'big') * 2 + b'\x08\x02\x00\x00\x00')
    with pytest.raises(AudioLabError, match='too large'):
        export.prepare_cover(bomb, tmp_path / 'x.jpg')
    assert export.picture_size(png) == ('png', 2000, 1000) and export.picture_size(out) == ('jpeg', 1400, 700)
    # the prepared JPEG goes into every format that can carry one, as it is
    mp3 = export.transcode(tmp_path / 'song.jpg', tmp_path / 'with_cover', 'mp3_320', cover=out)
    assert ffmpeg.probe_media(mp3)['has_cover']


def test_cover_art_takes_over_an_uploads_picture(tmp_path):
    """Enhance exports from a decoded master: the upload's own cover used to get lost."""
    src = tmp_path / 'tagged.mp3'
    art = tmp_path / 'art.png'
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=s=1600x1600:d=1', '-frames:v', '1', str(art)])
    run_ffmpeg(
        ['-f', 'lavfi', '-i', 'sine=d=2', '-i', str(art), '-map', '0:a', '-map', '1:v', '-c:v', 'png']
        + ['-disposition:v', 'attached_pic', '-id3v2_version', '3', str(src)]
    )
    source = {'kind': 'file', 'name': src.name, 'path': str(src)}
    for opts, expect in [({'embed_cover': True}, 'taken over'), ({'embed_cover': False}, None)]:
        ctx = TaskContext('t', tmp_path / f'job{len(opts)}{expect}', opts, [('acquire', 'Getting audio', 1)])
        acq = common.acquire(ctx, source, ctx.stage('acquire'), task='enhance')
        cover = common.cover_art(ctx, acq)
        if expect:
            info = ffmpeg.probe_media(cover)
            assert (info['video_codec'], info['width']) == ('mjpeg', 1400)
        else:
            assert cover is None
    own = tmp_path / 'own.jpg'
    export.cover_jpeg(art, own)
    ctx = TaskContext('t', tmp_path / 'job_own', {'embed_cover': False}, [('acquire', 'Getting audio', 1)])
    acq = common.acquire(ctx, {**source, '_cover': str(own)}, ctx.stage('acquire'), task='enhance')
    assert common.cover_art(ctx, acq) == own  # an own picture is always used


def test_save_cover_option_keeps_the_full_size_picture(tmp_path):
    """`save_cover`: the cover as a file next to the outputs, at full size (not the 1400 px embed copy)."""
    from audiolab import tasks

    art = tmp_path / 'art.png'
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=s=1600x1600:d=1', '-frames:v', '1', str(art)])
    for name, codec in [('tagged.mp3', 'png'), ('jpeg.mp3', 'mjpeg')]:  # most MP3 covers are JPEGs
        run_ffmpeg(
            ['-f', 'lavfi', '-i', 'sine=d=1', '-i', str(art), '-map', '0:a', '-map', '1:v', '-c:v', codec]
            + ['-disposition:v', 'attached_pic', '-id3v2_version', '3', str(tmp_path / name)]
        )
    plain = tmp_path / 'plain.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=d=1', str(plain)])
    spec = tasks.get('download')
    notes = []
    for src, expect in [
        (tmp_path / 'tagged.mp3', ('tagged - cover.png', 'png')),
        (tmp_path / 'jpeg.mp3', ('jpeg - cover.jpg', 'jpeg')),
        (plain, None),
    ]:
        source = {'kind': 'file', 'name': src.name, 'path': str(src)}
        assert tasks.validate('download', {'format': 'mp3'}, source)['save_cover'] is False  # off by default
        opts = tasks.validate('download', {'format': 'mp3', 'save_cover': True}, source)
        ctx = TaskContext(
            't', tmp_path / f'job_{src.stem}', opts, spec.stages(opts, source), emit_note=lambda *a: notes.append(a)
        )
        result = spec.run(ctx, source)
        covers = [f for f in result['files'] if f['kind'] == 'cover']
        if expect:  # the picture the file carries, as it is (a PNG stays a PNG)
            assert result['cover_source'] == 'file' and [(f['id'], f['name']) for f in covers] == [('f1', expect[0])]
            assert export.picture_size(Path(covers[0]['path'])) == (expect[1], 1600, 1600)
        else:  # nothing to save: a note, not a failure
            assert result['cover_source'] is None and not covers and len(result['files']) == 1
            assert any(kind == 'info' and 'No cover image' in msg for kind, msg, _ in notes)
    assert tasks.validate('enhance', {'save_cover': True}, source)['save_cover'] is True


def test_cover_art_off_keeps_a_files_own_picture_out(tmp_path):
    """ "Add cover art" off: converting a file that carries a picture writes none (full files and tracks)."""
    from audiolab import tasks

    art = tmp_path / 'art.png'
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc2=s=320x320:d=1', '-frames:v', '1', str(art)])
    src = tmp_path / 'pictured.mp3'
    run_ffmpeg(
        ['-f', 'lavfi', '-i', 'sine=d=4', '-i', str(art), '-map', '0:a', '-map', '1:v', '-c:v', 'png']
        + ['-disposition:v', 'attached_pic', '-id3v2_version', '3', str(src)]
    )
    assert ffmpeg.probe_media(src)['has_cover']
    source = {'kind': 'file', 'name': src.name, 'path': str(src)}
    spec = tasks.get('download')
    for embed in (False, True):
        for fmt in ('mp3', 'm4a', 'flac'):
            options = {'format': fmt, 'embed_cover': embed, 'split_tracks': True, 'tracklist': '0:00 One\n0:02 Two'}
            opts = tasks.validate('download', options, source)
            ctx = TaskContext('t', tmp_path / f'job_{fmt}_{embed}', opts, spec.stages(opts, source))
            files = spec.run(ctx, source)['files']
            assert [f.get('group') for f in files] == [None, 'tracks', 'tracks'], files
            covers = [ffmpeg.probe_media(Path(f['path']))['has_cover'] for f in files]
            assert covers == [embed] * 3, (fmt, embed, covers)


# ------------------------------------------------------------ the artist of a link
def _ytdlp_json(**fields):
    return {'id': 'abc', 'extractor_key': 'Youtube', 'duration': 200, **fields}


@pytest.mark.parametrize(
    'info, artist',
    [
        # auto-generated "Topic" upload: yt-dlp's music fields name the one track the video is
        (_ytdlp_json(title='Song', uploader='Alpha - Topic', track='Song', artists=['Alpha', 'Beta']), 'Alpha, Beta'),
        # "feat." in the title, no music fields: the title's artist part, all of it
        (_ytdlp_json(title='Alpha feat. Beta - Song (Official Video)', uploader='AlphaVEVO'), 'Alpha feat. Beta'),
        # the same video with a music section naming it
        (
            _ytdlp_json(title='Alpha feat. Beta - Song (Official Video)', uploader='AlphaVEVO', track='Song')
            | {'artists': ['Alpha', 'Beta']},
            'Alpha, Beta',
        ),
        # a 2 h DJ set whose music section lists one licensed song: the channel, not that song's artist
        (
            _ytdlp_json(
                title='Sunset Session 2026 | Deep House Mix',
                uploader='Beach Radio',
                track='Licensed Song',
                artists=['Licensed Artist'],
                duration=7200,
            ),
            'Beach Radio',
        ),
        # a label's upload "Artist - Title"
        (_ytdlp_json(title='Some Artist - Some Title [Official Audio]', uploader='Big Label Records'), 'Some Artist'),
    ],
)
def test_link_artist_one_rule_for_card_and_files(info, artist, tmp_path, monkeypatch, noise_set):
    """The probe card and the job's tags use one rule (they used to differ for "feat." titles and sets)."""
    import json

    from audiolab.media import proc

    monkeypatch.setattr(youtube, 'ytdlp_cmd', lambda: ['yt-dlp'])
    monkeypatch.setattr(
        youtube.proc, 'run', lambda *a, **k: proc.ProcResult(returncode=0, stdout=json.dumps(info), stderr_tail='')
    )
    card = youtube.probe('https://www.youtube.com/watch?v=abc')
    assert card['artist'] == artist
    assert 'music_track' in card  # kept for the job, not sent to the UI (api/jobs.py probe)

    monkeypatch.setattr(youtube, 'probe', lambda url, progress=None: dict(card))
    monkeypatch.setattr(
        youtube, 'download_audio', lambda url, dest, progress=None: Path(shutil.copy(noise_set, tmp_path / 'a.wav'))
    )
    monkeypatch.setattr(common, 'preflight', lambda *a, **k: {})
    ctx = TaskContext('t', tmp_path / 'job', {}, [('acquire', 'Getting audio', 1)])
    acq = common.acquire(ctx, {'kind': 'url', 'url': card['url']}, ctx.stage('acquire'), task='download')
    assert acq.artist == artist and common.tags_for(acq)['artist'] == artist
    # an artist the user cleared on the card stays cleared
    ctx = TaskContext('t', tmp_path / 'job2', {'meta': {'artist': ''}}, [('acquire', 'Getting audio', 1)])
    acq = common.acquire(ctx, {'kind': 'url', 'url': card['url']}, ctx.stage('acquire'), task='download')
    assert acq.artist is None and common.tags_for(acq)['artist'] == ''


# ------------------------------------------------------------ errors in plain words
def test_download_errors_by_kind():
    from audiolab.settings import SETTINGS

    SETTINGS.get().cookies_browser = ''
    for text in (
        'ERROR: [youtube] abc: Unable to download webpage: HTTP Error 503: Service Unavailable',
        'ERROR: [generic] Unable to download webpage: HTTP Error 502: Bad Gateway (caused by <HTTPError 502>)',
        'ERROR: HTTP Error 500: Internal Server Error',
        'ERROR: [soundcloud] x: Unable to download JSON metadata: HTTP Error 504: Gateway Time-out',
    ):
        e = youtube._friendly_error(text, 'u')
        assert str(e) == 'The site is having trouble right now. Try again in a few minutes.', text
        assert e.fix == 'retry' and e.detail and 'HTTP Error 5' in e.detail
    e = youtube._friendly_error('ERROR: [vimeo] 123: Unable to download webpage: HTTP Error 401: Unauthorized', 'u')
    assert e.fix == 'use_cookies' and 'YouTube sign-in' in e.hints[0]
    SETTINGS.get().cookies_browser = 'firefox'
    e = youtube._friendly_error('ERROR: HTTP Error 401: Unauthorized', 'u')
    assert e.fix is None and 'Firefox' in str(e)
    SETTINGS.get().cookies_browser = ''
    # something unknown: plain words, yt-dlp's text only as the detail — and no automatic downloader update
    e = youtube._friendly_error('ERROR: [foo] bar: Something odd happened (code 7)', 'u')
    assert str(e) == "The download didn't work." and e.fix is None
    assert e.detail == 'Something odd happened (code 7)' and 'Downloads & sign-in' in e.hints[-1]
    assert youtube._UPDATE_HINT == 'Update the downloader: Settings → Downloads & sign-in → "Update downloader".'


def test_unknown_download_error_does_not_update_the_downloader(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube, '_last_repair', {})
    monkeypatch.setattr(youtube, 'update_ytdlp', lambda progress=None: calls.append(1) or {'ok': True})

    def probe(url, progress=None):
        raise youtube._friendly_error('ERROR: [foo] bar: Something odd happened', url)

    monkeypatch.setattr(youtube, 'probe', probe)
    ctx = TaskContext('t', tmp_path / 'job', {}, [('acquire', 'Getting audio', 1)])
    with pytest.raises(DownloadError, match="didn't work"):
        common.acquire(ctx, {'kind': 'url', 'url': 'https://youtu.be/x'}, ctx.stage('acquire'), task='download')
    assert calls == []


@pytest.mark.parametrize(
    'stderr, message',
    [
        ('[out#0/mp3] Error opening output /x/Song.mp3: No space left on device', 'The disk is full.'),
        ('av_interleaved_write_frame(): Disk quota exceeded', 'The disk is full.'),
        ('/Volumes/Stick/Song.mp3: Permission denied', "AudioLab can't write there."),
        ('Error opening output file /x/y.flac: Read-only file system', "AudioLab can't write there."),
    ],
)
def test_ffmpeg_write_failures_in_plain_words(stderr, message, monkeypatch):
    from audiolab.errors import AudioLabError
    from audiolab.media import proc

    monkeypatch.setattr(
        ffmpeg.proc, 'run', lambda *a, **k: proc.ProcResult(returncode=1, stdout='', stderr_tail=stderr)
    )
    with pytest.raises(AudioLabError) as failed:
        run_ffmpeg(['-i', 'x.wav', 'y.mp3'], what='MP3 export')
    e = failed.value
    assert str(e) == message and e.fix is None and stderr.split(': ')[-1] in e.detail
    assert any('Settings → Files' in h for h in e.hints)
