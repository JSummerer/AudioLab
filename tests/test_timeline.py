"""The changes UI receives bounded observations of actual gain, never invented spans."""

import numpy as np
import pytest

from audiolab import processing
from audiolab.dsp.timeline import MAX_BINS, ReductionTimeline, reduction_timeline


def test_highlights_keep_actual_peak_time_across_blocks():
    values = np.zeros(48_000, dtype=np.float32)
    values[12_345] = 2.0
    values[12_399] = 1.0
    values[-1] = 0.5
    whole = reduction_timeline(values, len(values), 48000)
    chunked = ReductionTimeline(len(values), 48000)
    for start in range(0, len(values), 137):
        chunked.add(values[start : start + 137], start)
    assert chunked.result() == whole
    assert len(whole['bins']) == 2
    assert whole['bins'][0]['peak_s'] == round(12345 / 48000, 6)
    assert whole['bins'][0]['max_gr_db'] == 2.0
    assert whole['bins'][-1]['peak_s'] < whole['bins'][-1]['end_s'] == 1.0


def test_control_centres_and_bounded_storage():
    controls = np.linspace(0.2, 3.0, 10_001)
    summary = reduction_timeline(controls, 100_015, 1000, step_frames=10, start_frame=5)
    assert len(summary['bins']) <= MAX_BINS
    last = summary['bins'][-1]
    assert last['peak_s'] == 100.005 and last['max_gr_db'] == 3.0
    assert last['end_s'] == 100.015
    assert all(b['start_s'] <= b['peak_s'] < b['end_s'] for b in summary['bins'])


def test_silence_tiny_reductions_and_invalid_measurements_have_no_markers():
    values = np.array([0.0, 0.099, -1.0, np.nan, np.inf, -np.inf])
    assert reduction_timeline(values, len(values), 10)['bins'] == []


def test_processing_retains_only_valid_bounded_highlights():
    good = {'start_s': 0, 'end_s': 2, 'peak_s': 1, 'max_gr_db': 2}
    timeline = {
        'duration_s': 300,
        'bins': [
            {**good, 'max_gr_db': float('nan')},
            {**good, 'path': '/secret/file'},
            {**good, 'peak_s': 2},  # outside its half-open interval
            {**good, 'start_s': 1},  # overlaps the already accepted interval
            *[{'start_s': i, 'end_s': i + 1, 'peak_s': i + 0.5, 'max_gr_db': 1} for i in range(2, 200)],
        ],
    }
    report = processing.report_details({'limiter': {'timeline': timeline}})
    kept = report['applied']['limiter']['timeline']
    assert kept['bins'][0] == good
    assert len(kept['bins']) <= MAX_BINS
    assert '/secret' not in str(kept) and 'nan' not in str(kept)
    snapshot = processing.snapshot({}, {'processing': report})
    assert snapshot['applied']['limiter']['timeline'] == kept
    assert (
        'timeline'
        not in processing.report_details(
            {
                'limiter': {
                    'timeline': {'duration_s': float('inf'), 'bins': []},
                    'engaged': False,
                }
            }
        )['applied']['limiter']
    )


def test_preview_bandwidth_keeps_no_unbounded_or_private_fields():
    assert processing.preview_bandwidth(None) is None
    assert processing.preview_bandwidth({'is_lossy_cutoff': 'yes'}) is None
    assert processing.preview_bandwidth({'is_lossy_cutoff': True, 'cutoff_hz': float('nan')}) == {
        'is_lossy_cutoff': True,
    }
    bandwidth = {'is_lossy_cutoff': True, 'cutoff_hz': 15000, 'file': '/private/source.mp3'}
    kept = processing.snapshot({}, {'processing': {'params': {}}, 'analysis': {'bandwidth': bandwidth}})
    assert kept['resolved']['bandwidth'] == {'is_lossy_cutoff': True, 'cutoff_hz': 15000}


@pytest.mark.parametrize('stage', ['compressor', 'limiter'])
def test_full_band_markers_agree_with_actual_applied_gain(stage):
    from audiolab.dsp.bus import glue_comp
    from audiolab.dsp.limiter import tp_limit

    sr = 48000
    t = np.arange(sr * 2) / sr
    # The signal has no zero samples, making its applied gain directly measurable.
    mono = (0.45 + 0.3 * np.sin(2 * np.pi * 100 * t)).astype(np.float32)
    mono[sr : sr + sr // 4] *= 2
    source = np.column_stack([mono, mono])
    if stage == 'compressor':
        result, stats = glue_comp(source, thresh_dbfs=-14, sr=sr)
    else:
        result, stats = tp_limit(source, ceiling_db=-3, sr=sr)
    bins = stats['timeline']['bins']
    assert bins and len(bins) <= MAX_BINS
    gain_db = -20 * np.log10(result[:, 0] / source[:, 0])
    for item in bins:
        start, end, peak = (round(item[k] * sr) for k in ('start_s', 'end_s', 'peak_s'))
        assert gain_db[peak] == pytest.approx(item['max_gr_db'], abs=0.002)
        assert gain_db[start:end].max() == pytest.approx(item['max_gr_db'], abs=0.002)


def test_band_control_markers_locate_generated_events():
    from test_adaptive import SR as LOWMID_SR
    from test_adaptive import fixture
    from test_dsp import SR, _voice_with_s

    from audiolab.dsp.adaptive import control_lowmids
    from audiolab.dsp.deess import deess

    _, low = control_lowmids(fixture(), LOWMID_SR)
    highlights = low['timeline']['bins']
    assert highlights and all(8.8 <= b['peak_s'] < 10.2 for b in highlights)
    _, sharp = deess(_voice_with_s()[0], SR, mix=False)
    highlights = sharp['timeline']['bins']
    assert highlights and all(0.60 <= b['peak_s'] % 1 < 0.77 for b in highlights)
