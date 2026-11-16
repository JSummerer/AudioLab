"""Enhance delivery through the real renderer, preview writer and export path."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audiolab import library, tasks
from audiolab.context import TaskContext
from audiolab.errors import AudioLabError, Cancelled
from audiolab.jobs import Job, new_job_id
from audiolab.media import proc
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


def test_preview_uses_final_master_and_export_reading_reaches_library(delivery):
    source, ctx, _notes = delivery()
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
    source, ctx, notes = delivery(('mp3_320', 'wav16', 'wav24'))
    ctx.options['split_tracks'] = True  # tracks are cut in the first format, the MP3
    cuts = []
    monkeypatch.setattr(common, 'split_into_tracks', lambda *a, **kw: (cuts.append(kw), ([], None))[1])
    private_detail = '/private/work/meter.log'
    mp3_checks = []

    def measure(path, *, ceiling_dbtp, progress, duration_s):
        reading = {'integrated_lufs': -14.0, 'ceiling_dbtp': ceiling_dbtp, 'tolerance_db': PEAK_TOLERANCE_DB}
        if path.suffix == '.mp3':  # over by 0.5 dB as encoded; under once turned down
            mp3_checks.append(path)
            over = len(mp3_checks) == 1
            peak = ceiling_dbtp + (0.5 if over else -0.1)
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
    files = {f['format']: f for f in result['files']}
    assert files.keys() == {'wav16', 'wav24', 'mp3_320'}
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
    for check in (unverified, missed):
        assert check['message'] in result['warnings'] and ('warning', check['message']) in notes
    # The MP3 was encoded once more, turned down, before it was saved: no warning, a plain "What we did" line.
    assert len(mp3_checks) == 2 and files['mp3_320']['verification']['status'] == 'verified'
    assert files['mp3_320']['verification']['message'] is None and len(result['warnings']) == 2
    line = 'Turned the MP3 down slightly so its peaks stay under the limit (-0.6 dB; encoding raises peaks).'
    assert line in result['summary']
    assert [kw['trim_db'] for kw in cuts] == [0.6]  # its tracks are turned down just like the full MP3


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
