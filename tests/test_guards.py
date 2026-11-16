"""Memory-safety guards that keep long recordings from blowing up RAM or
silently breaking promises (kept from the prototype's hardening pass)."""

import numpy as np

from audiolab.resources import memory_report


def test_memory_report_shape():
    rep = memory_report()
    assert rep['source'] in ('container', 'host', 'unknown')
    for key in ('limit_bytes', 'current_bytes', 'available_bytes'):
        assert isinstance(rep[key], int) and rep[key] >= 0


def test_long_enhance_reserves_disk_for_mapped_working_buffers():
    from audiolab.dsp.scratch import DISK_BACKED_AFTER_SECONDS, MAX_BYTES_PER_FRAME
    from audiolab.resources import GB, estimate_disk_bytes

    boundary = DISK_BACKED_AFTER_SECONDS
    ordinary = lambda seconds: int(seconds / 3600 * 6 * GB + 0.25 * GB)
    assert estimate_disk_bytes(boundary, 'enhance') == ordinary(boundary)
    longer = boundary + 1
    assert estimate_disk_bytes(longer, 'enhance') == ordinary(longer) + longer * 48000 * MAX_BYTES_PER_FRAME
    assert estimate_disk_bytes(3600, 'enhance', 'best') >= estimate_disk_bytes(3600, 'enhance') + 7 * GB
    assert estimate_disk_bytes(3600, 'download') == int(1.75 * GB)


def test_a_long_enhance_is_sized_by_its_disk_backed_render(tmp_path, monkeypatch):
    """Above DISK_BACKED_AFTER_SECONDS the render works in mapped scratch files: a one-hour set fits an 8 GB
    Mac (measured 1.2 GB footprint), while the in-memory estimate up to that length and Best stay as they were."""
    import pytest

    from audiolab import resources
    from audiolab.dsp.scratch import DISK_BACKED_AFTER_SECONDS
    from audiolab.errors import ResourceGuardError
    from audiolab.resources import GB, estimate_peak_bytes, preflight

    def machine(gb):
        report = {'source': 'host', 'limit_bytes': gb * GB, 'current_bytes': 0, 'available_bytes': gb * GB}
        monkeypatch.setattr(resources, 'memory_report', lambda: report)

    monkeypatch.setattr(resources, 'disk_free_bytes', lambda p: 0)  # unknown: no disk check
    machine(8)
    preflight(3600.0, tmp_path, 'enhance')  # was refused: "needs about 6.8 GB … only about 6.8 GB"
    in_memory = lambda s, extra=0: int(4.3 * s * 48000 * 8) + int(1.3 * GB) + extra
    assert estimate_peak_bytes(600, 'enhance') == in_memory(600)
    for seconds in (60, 600, 3600, 3 * 3600):  # Best (the AI stage) is not refitted
        assert estimate_peak_bytes(seconds, 'enhance', 'best') == in_memory(seconds, GB)
    for quality in ('standard', 'light'):
        lengths = range(DISK_BACKED_AFTER_SECONDS - 120, 4 * 3600, 30)
        estimates = [estimate_peak_bytes(s, 'enhance', quality) for s in lengths]
        assert estimates == sorted(estimates)  # never smaller for a longer recording, across the boundary too
    machine(4)  # 3 h needs about 3.5 GiB; 85 % of 4 GiB is 3.4 GiB
    with pytest.raises(ResourceGuardError, match=r'of memory, but only about .* is the installed-memory safety budget'):
        preflight(3 * 3600.0, tmp_path, 'enhance')


def test_output_drive_needs_room_for_the_selected_results_not_scratch(tmp_path, monkeypatch):
    """A 2 h 40 set enhanced onto a 32 GB stick: the stick holds the exports, the scratch stays on the work
    drive. The exports are counted from the job's own formats, Tracks included."""
    import pytest

    from audiolab import resources
    from audiolab.errors import ResourceGuardError
    from audiolab.resources import GB, preflight

    work, out = tmp_path / 'work', tmp_path / 'stick'
    free = {work: 500 * GB, out: 28 * GB}
    monkeypatch.setattr(resources, 'disk_free_bytes', lambda p: free[p])
    monkeypatch.setattr(resources, 'memory_report', lambda: {'source': 'unknown', 'available_bytes': 0})
    monkeypatch.setattr(resources.system, 'folder_writable', lambda p: True)
    wav24 = {'formats': ['wav24'], 'advanced': {}}
    preflight(9632.0, work, 'enhance', output_dir=out, options=wav24)
    with pytest.raises(ResourceGuardError, match='output folder'):  # options unknown: as much as the work drive
        preflight(9632.0, work, 'enhance', output_dir=out)
    # 2 h at 96 kHz as WAV 16-bit + WAV 24-bit + the first one again as Tracks: 9.68 GB (9.0 GiB) of
    # results; the earlier bound of two 24-bit files (7.7 GiB + margin) would have let this start on 9.4 GiB.
    hi_res = {'formats': ['wav16', 'wav24'], 'split_tracks': True, 'advanced': {'sample_rate': 96000}}
    assert resources.estimate_output_bytes(7200, 'enhance', hi_res) == 7200 * 96000 * 2 * (2 + 3 + 2)
    free[out] = int(9.4 * GB)
    with pytest.raises(ResourceGuardError, match='output folder'):
        preflight(7200.0, work, 'enhance', output_dir=out, options=hi_res)
    free[out] = int(9.6 * GB)
    preflight(7200.0, work, 'enhance', output_dir=out, options=hi_res)
    free[out] = 1  # Check writes nothing there
    preflight(9632.0, work, 'analyze', output_dir=out)


def test_k_blocks_stream_matches_reference_implementation():
    """Chunked K-weighting (bounded memory on 2 h sets) must give the same block
    loudness as filtering the whole signal at once."""
    from scipy.signal import lfilter

    from audiolab.dsp.loudness import KA1, KA2, KB1, KB2, _block_loudness, _k_blocks

    sr = 48000
    rng = np.random.default_rng(7)
    t = np.linspace(0, 3.0, int(3 * sr), endpoint=False)
    x = (0.3 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.standard_normal(len(t))).astype(np.float32)
    x = np.column_stack([x, x])
    xk = lfilter(KB2, KA2, lfilter(KB1, KA1, x, axis=0), axis=0)
    b_len, h_len = int(0.4 * sr), int(0.1 * sr)
    expected = [_block_loudness(xk[s : s + b_len]) for s in range(0, x.shape[0] - b_len + 1, h_len)]
    got = _k_blocks(x, block_s=0.4, hop_s=0.1, sr=sr)
    assert len(got) == len(expected) and np.allclose(got, expected, atol=1e-9)


def test_limiter_reports_ceiling_met_honestly():
    """Within its gain-reduction budget the limiter reaches the ceiling; beyond it,
    it must report the miss instead of silently exceeding it."""
    from audiolab.dsp.limiter import tp_limit, true_peak_db

    sr = 48000
    t = np.linspace(0, 0.1, int(sr * 0.1), endpoint=False)
    sig = np.column_stack([np.sin(2 * np.pi * 12000 * t) * 1.2] * 2)
    out, info = tp_limit(sig, ceiling_db=-1.25, sr=sr)
    assert info['ceiling_met'] is True and true_peak_db(out) <= -1.24
    hot = np.column_stack([np.sin(2 * np.pi * 12000 * t) * 5.0] * 2)
    _, info2 = tp_limit(hot, ceiling_db=-1.25, sr=sr)
    assert info2['ceiling_met'] is False and 'warning' in info2
