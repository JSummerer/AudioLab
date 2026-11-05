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
    assert verification == measure_export(path, ceiling_dbtp=verification['ceiling_dbtp'])

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


def test_meter_error_and_ceiling_miss_keep_the_exports(delivery, monkeypatch):
    source, ctx, notes = delivery(('wav16', 'wav24'))
    private_detail = '/private/work/meter.log'

    def measure(path, *, ceiling_dbtp, progress, duration_s):
        info = sf.info(path)  # the verification stage receives each actual saved file
        assert info.frames > 0
        if info.subtype == 'PCM_16':
            raise AudioLabError('Meter failed', detail=private_detail)
        assert info.subtype == 'PCM_24'
        return {
            'status': 'warning',
            'integrated_lufs': -14.0,
            'true_peak_dbtp': ceiling_dbtp + 0.4,
            'ceiling_dbtp': ceiling_dbtp,
            'ceiling_met': False,
            'tolerance_db': PEAK_TOLERANCE_DB,
            'message': f'{path.name} peaks above the ceiling after export.',
        }

    monkeypatch.setattr(enhance, 'measure_export', measure)
    result = enhance.run(ctx, source)
    files = {f['format']: f for f in result['files']}
    assert files.keys() == {'wav16', 'wav24'}
    assert all(Path(f['path']).is_file() for f in files.values())
    unverified = files['wav16']['verification']
    assert unverified['status'] == 'unverified' and unverified['ceiling_met'] is None
    assert unverified['integrated_lufs'] is None and unverified['true_peak_dbtp'] is None
    assert 'could not be verified' in unverified['message'] and private_detail not in unverified['message']
    missed = files['wav24']['verification']
    assert missed['status'] == 'warning' and missed['ceiling_met'] is False
    assert missed['true_peak_dbtp'] > missed['ceiling_dbtp']
    for check in (unverified, missed):
        warning = next(w for w in result['warnings'] if w.startswith(check['message']))
        assert ('warning', warning) in notes


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
