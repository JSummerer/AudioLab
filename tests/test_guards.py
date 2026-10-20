"""Memory-safety guards that keep long recordings from blowing up RAM or
silently breaking promises (kept from the prototype's hardening pass)."""

import numpy as np

from audiolab.resources import memory_report


def test_memory_report_shape():
    rep = memory_report()
    assert rep['source'] in ('container', 'host', 'unknown')
    for key in ('limit_bytes', 'current_bytes', 'available_bytes'):
        assert isinstance(rep[key], int) and rep[key] >= 0


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
