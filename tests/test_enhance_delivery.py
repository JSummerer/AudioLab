"""Enhance delivery through the real renderer, preview writer and export path."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audiolab import library, tasks
from audiolab.context import TaskContext
from audiolab.errors import AudioLabError, Cancelled
from audiolab.jobs import Job, new_job_id
from audiolab.media import export, proc
from audiolab.media.verification import PEAK_TOLERANCE_DB, measure_export
from audiolab.tasks import common, enhance

SR = 48000


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(proc, '_POLL_S', 0.01)  # short FFmpeg runs
    t = np.arange(8 * SR) / SR
    base = 0.18 * np.sin(2 * np.pi * 110 * t) + 0.06 * np.sin(2 * np.pi * 1100 * t)
    lowmid_rise = 0.19 * np.sin(2 * np.pi * 300 * t) * np.exp(-(((t - 3.7) / 0.35) ** 2))
    audio = base + lowmid_rise
    audio[0] = 0.98  # original preview requires shared attenuation to fit its headroom
    path = tmp_path / 'Generated.wav'
    sf.write(path, np.column_stack([audio, audio]), SR, subtype='FLOAT')
    source = {'kind': 'file', 'name': path.name, 'path': str(path)}

    def prepare(formats=('wav16',)):
        options = tasks.validate(
            'enhance',
            {
                'recipe': 'music',
                'quality': 'standard',
                'formats': list(formats),
                'advanced': {'adaptive_lowmid': 'strong', 'sample_rate': 44100},
            },
            source,
        )
        notes = []
        ctx = TaskContext(
            new_job_id(),
            tmp_path / 'job',
            options,
            enhance.stages(options, source),
            emit_note=lambda kind, message, _detail: notes.append((kind, message)),
        )
        return source, ctx, notes

    return prepare


def test_preview_uses_final_master_and_export_reading_reaches_library(delivery, monkeypatch):
    from audiolab import ai
    from audiolab.recipes import chain

    source, ctx, _notes = delivery()
    ctx.options['quality'] = 'best'
    monkeypatch.setattr(ai, 'ai_status', lambda: {'available': True, 'device': 'cpu'})

    def fallback(_ref, params, *_args):
        return {'x': None, 'report': {'stage': params.ai_stage, 'fallback': True}, 'warning': None}

    monkeypatch.setattr(chain, '_ai_stage', fallback)
    result = enhance.run(ctx, source)
    assert result['processing']['applied']['adaptive_lowmid']['engaged']
    (file,) = result['files']
    path = Path(file['path'])
    assert sf.info(path).samplerate == 44100
    verification = file['verification']
    assert verification['status'] in {'verified', 'warning'}  # resampling the sharp transient can raise its peak
    saved = measure_export(path, ceiling_dbtp=verification['ceiling_dbtp'])  # measured before it had its name
    assert {k: v for k, v in verification.items() if k != 'message'} == {
        k: v for k, v in saved.items() if k != 'message'
    }

    original, original_rate = sf.read(common.reference_path(ctx), dtype='float32', always_2d=True)
    master, master_rate = sf.read(ctx.work_dir / 'scratch' / 'master.flac', dtype='float32', always_2d=True)
    before, before_rate = sf.read(result['_media']['original'], dtype='float32', always_2d=True)
    after, after_rate = sf.read(result['_media']['enhanced'], dtype='float32', always_2d=True)
    assert original_rate == master_rate == before_rate == after_rate == SR
    start = round(result['excerpt']['start_s'] * SR)
    frames = round(result['excerpt']['duration_s'] * SR)
    segment = slice(start, start + frames)
    shared_peak = max(np.abs(original[segment]).max(), np.abs(master[segment]).max())
    gain = 10 ** (-1 / 20) / shared_peak
    assert 0 < gain < 1  # exercise common attenuation, including on the enhanced side
    np.testing.assert_allclose(before, original[segment] * gain, rtol=0, atol=2**-15)
    np.testing.assert_allclose(after, master[segment] * gain, rtol=0, atol=2**-15)

    job = Job(ctx.job_id, 'enhance', source, ctx.options, title='Generated', result=result)
    assert library.record(job)
    entry = library.public(library.get(job.id))
    retained = entry['processing']
    assert retained['requested']['advanced']['adaptive_lowmid'] == 'strong'
    assert retained['requested']['quality'] == 'best'
    assert retained['applied']['ai'] == {'stage': 'stem_eq', 'fallback': True}
    assert retained['resolved']['params']['ai_stage'] == 'stem_eq'
    assert retained['resolved']['calibration']['version'] == 1
    assert retained['resolved']['auto_compressor'] == 'off'
    assert entry['has_overview'] and entry['adjust_quality'] == 'standard'
    assert retained['resolved']['params']['adaptive_lowmid_db'] == 3.0
    assert retained['applied']['adaptive_lowmid'] == result['processing']['applied']['adaptive_lowmid']
    assert retained['metrics'] == result['metrics']
    assert entry['files'][0]['verification'] == {k: v for k, v in verification.items() if k != 'message'}


def test_master_reuses_the_analysis_measurement_of_the_reference(delivery, monkeypatch):
    from audiolab.recipes import chain

    source, ctx, _notes = delivery()
    measure, calls = chain._measure, []
    monkeypatch.setattr(chain, '_measure', lambda x, sr, **kw: calls.append(len(x)) or measure(x, sr, **kw))
    result = enhance.run(ctx, source)
    assert len(calls) == 1  # only the finished master: the input was measured by the analysis
    analysis = result['analysis']
    levels = {k: analysis[k] for k in ('integrated_lufs', 'true_peak_dbtp', 'lra')}
    before = result['metrics']['before']
    assert (before['lufs'], before['true_peak_dbtp'], before['lra']) == tuple(levels.values())
    x, _ = sf.read(common.reference_path(ctx), dtype='float32', always_2d=True)
    assert measure(x, SR) == levels  # exactly what the renderer would have measured itself


def test_meter_error_and_ceiling_miss_keep_the_exports(delivery, monkeypatch):
    source, ctx, notes = delivery(('mp3_320', 'wav16', 'wav24', 'aac_256'))
    ctx.options['split_tracks'] = True  # tracks are cut in the first format, the MP3
    ctx.options['tracklist'] = '0:00 Within\n0:02 Over\n0:04 Missing\n0:06 Slightly over'
    actual_encode = export.run_ffmpeg
    track_encodes = {i: [] for i in range(1, 5)}

    def encode(args, **kwargs):
        path = Path(args[-1])
        if path.parent.name != 'Tracks':
            return actual_encode(args, **kwargs)
        track = int(path.name[1:3])
        track_encodes[track].append(args[args.index('-af') + 1])
        assert kwargs['duration_s'] == 2.0
        path.write_bytes(b'encoded track fixture')

    monkeypatch.setattr(export, 'run_ffmpeg', encode)
    private_detail = '/private/work/meter.log'
    lossy_checks = []

    def measure(path, *, ceiling_dbtp, progress, duration_s):
        reading = {'integrated_lufs': -14.0, 'ceiling_dbtp': ceiling_dbtp, 'tolerance_db': PEAK_TOLERANCE_DB}
        if path.parent.name == 'Tracks':
            assert duration_s == 2.0 and ceiling_dbtp == export.LOSSY_LIMIT_DBTP
            track = int(path.name[1:3])
            if track == 3:
                raise AudioLabError('Meter failed', detail=private_detail)
            excess = {1: -0.1, 2: 0.6, 4: 0.3}[track]
            return {
                **reading,
                'true_peak_dbtp': ceiling_dbtp + excess,
                'status': 'warning' if excess > 0 else 'verified',
                'ceiling_met': excess <= 0,
            }
        if path.suffix in ('.mp3', '.m4a'):  # over their limit by 0.5 / 0.3 dB as encoded; the AAC stays over
            lossy_checks.append(path)
            over = path.suffix == '.m4a' or len(lossy_checks) == 1
            peak = ceiling_dbtp + (0.3 if path.suffix == '.m4a' else 0.5 if over else -0.1)
            return {
                **reading,
                'status': 'warning' if over else 'verified',
                'true_peak_dbtp': peak,
                'ceiling_met': not over,
            }
        info = sf.info(path)  # the verification stage receives each actual saved file
        assert info.frames > 0
        if info.subtype == 'PCM_16':
            raise AudioLabError('Meter failed', detail=private_detail)
        assert info.subtype == 'PCM_24'
        return {**reading, 'status': 'warning', 'true_peak_dbtp': ceiling_dbtp + 0.4, 'ceiling_met': False}

    monkeypatch.setattr(enhance, 'measure_export', measure)
    result = enhance.run(ctx, source)
    files = {f['format']: f for f in result['files'] if f.get('group') != 'tracks'}
    assert files.keys() == {'wav16', 'wav24', 'mp3_320', 'aac_256'}
    assert all(Path(f['path']).is_file() for f in files.values())
    unverified = files['wav16']['verification']
    assert unverified['status'] == 'unverified' and unverified['ceiling_met'] is None
    assert unverified['integrated_lufs'] is None and unverified['true_peak_dbtp'] is None
    assert 'could not be verified' in unverified['message'] and private_detail not in unverified['message']
    missed = files['wav24']['verification']  # lossless: kept as it is, with the warning
    assert missed['status'] == 'warning' and missed['ceiling_met'] is False
    assert missed['true_peak_dbtp'] > missed['ceiling_dbtp']
    assert missed['message'].startswith('The WAV 24-bit file goes over the peak ceiling. For more room,')
    assert missed['message'].endswith('(-0.60 dBTP after saving; ceiling -1.00 dBTP).')
    # Lossy files are checked against the lossy limit, not the -1.0 dBTP ceiling. The AAC stays over it even
    # after its one re-encode: it is saved, with a warning, and no "turned down" line.
    aac = files['aac_256']['verification']
    assert aac['status'] == 'warning' and aac['ceiling_dbtp'] == export.LOSSY_LIMIT_DBTP
    assert aac['message'] == (
        'The AAC file goes over the limit for lossy files, even after it was turned down. For more room, use a lower '
        'peak ceiling or a lossless format (+0.20 dBTP after saving, turned down 0.8 dB; limit -0.10 dBTP).'
    )
    for check in (unverified, missed, aac):
        assert check['message'] in result['warnings'] and ('warning', check['message']) in notes
    # The MP3 was encoded once more, turned down, before it was saved: no warning, a plain "What we did" line.
    mp3 = files['mp3_320']['verification']
    assert len(lossy_checks) == 4 and mp3['status'] == 'verified' and mp3['ceiling_dbtp'] == export.LOSSY_LIMIT_DBTP
    assert mp3['message'] is None and len(result['warnings']) == 4
    line = 'Turned the MP3 down slightly so its peaks stay under the limit (-1.0 dB; encoding raises peaks).'
    assert line in result['summary'] and not any('Turned the AAC' in s for s in result['summary'])
    tracks = [f for f in result['files'] if f.get('group') == 'tracks']
    assert [(f['start_s'], f['end_s']) for f in tracks] == [(0, 2), (2, 4), (4, 6), (6, 8)]
    assert all(Path(f['path']).is_file() for f in tracks)
    assert len(list(Path(result['tracks_dir']).iterdir())) == 4
    within, over, missing, slightly_over = [f['verification'] for f in tracks]
    assert within['status'] == 'verified' and within['message'] is None
    assert over['status'] == slightly_over['status'] == 'warning'
    assert over['true_peak_dbtp'] == pytest.approx(0.5) and slightly_over['true_peak_dbtp'] == pytest.approx(0.2)
    assert missing['status'] == 'unverified' and missing['true_peak_dbtp'] is None
    assert '03 - Missing.mp3' in missing['message'] and private_detail not in missing['message']
    group = 'Track checks — 1 within the peak limit, 2 over the peak limit, 1 could not be checked.'
    assert result['warnings'].count(group) == 1 and group not in result['summary']
    assert notes.count(('warning', group)) == 1
    for check in (over, missing, slightly_over):
        assert check['message'] not in result['warnings'] and ('warning', check['message']) not in notes
    assert all(f['verification']['trim_db'] == 1.0 for f in tracks)
    assert all(len(commands) == 1 and 'volume=-1.00dB' in commands[0] for commands in track_encodes.values())

    job = Job(ctx.job_id, 'enhance', source, ctx.options, title='Generated', result=result)
    assert library.record(job)
    entry = library.public(library.get(job.id))
    assert entry['track_check_summary'] == group
    saved_tracks = [f for f in entry['files'] if f['kind'] == 'track']
    assert [f['verification'] for f in saved_tracks] == [
        {k: v for k, v in f['verification'].items() if k != 'message'} for f in tracks
    ]


@pytest.mark.parametrize(('helper', 'stage'), [('make_previews', 'preview'), ('measure_export', 'export')])
def test_cancellation_from_delivery_helper_is_propagated(delivery, monkeypatch, helper, stage):
    source, ctx, _notes = delivery()

    def cancel(*args, **kwargs):
        # Raise at the helper boundary so a later progress check cannot mask a swallowed error.
        raise Cancelled()

    monkeypatch.setattr(enhance, helper, cancel)
    with pytest.raises(Cancelled):
        enhance.run(ctx, source)
    assert ctx.current_stage == stage
