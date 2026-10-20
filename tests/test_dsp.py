"""Unit tests for the DSP building blocks: biquads, mono fold, glue comp,
true-peak limiter, dither, BS.1770 loudness (vs ffmpeg), denoise, exciter."""

import re
import subprocess

import numpy as np
import pytest
import soundfile as sf

from audiolab.dsp.bus import glue_comp, mono_below
from audiolab.dsp.denoise import spectral_gate
from audiolab.dsp.dither import add_24bit_tpdf, quantize_to_16bit
from audiolab.dsp.exciter import hf_restore
from audiolab.dsp.filters import apply_sos_zero_phase, filtfilt_stream, highpass_fq, peak_fq
from audiolab.dsp.limiter import tp_limit, true_peak_db
from audiolab.dsp.loudness import integrated_lufs, lra, programme_loudness
from audiolab.media.ffmpeg import ffmpeg_info

SR = 48000


def test_zero_phase_biquads():
    t = np.linspace(0, 0.5, SR // 2, endpoint=False)
    sig = np.column_stack([np.sin(2 * np.pi * 1000 * t), np.cos(2 * np.pi * 1000 * t)])
    out = apply_sos_zero_phase(sig, peak_fq(1000.0, 3.0, q=1.0, sr=SR))
    assert out.shape == sig.shape
    assert np.max(np.abs(out)) > np.max(np.abs(sig))


def test_filtfilt_stream_matches_whole_signal():
    rng = np.random.default_rng(0)
    x = rng.standard_normal((SR * 4, 2)).astype(np.float32)
    sos = highpass_fq(25.0, sr=SR)
    a = filtfilt_stream(sos, x, block=1 << 16, margin=1 << 14)
    b = apply_sos_zero_phase(x, sos).astype(np.float32)
    assert float(np.abs(a - b).max()) < 1e-4


def test_mono_below():
    t = np.linspace(0, 1.0, SR, endpoint=False)
    sig = np.column_stack([np.sin(2 * np.pi * 40 * t), np.zeros_like(t)])
    out = mono_below(sig, fc=90.0, sr=SR)
    side_before = (sig[:, 0] - sig[:, 1]) / 2.0
    side_after = (out[:, 0] - out[:, 1]) / 2.0
    suppression_db = 10 * np.log10(np.mean(side_before**2) / np.mean(side_after**2))
    assert suppression_db >= 20.0, f'Expected >=20dB side suppression, got {suppression_db:.1f} dB'


def test_glue_comp():
    t = np.linspace(0, 1.0, SR, endpoint=False)
    sig = np.column_stack([np.sin(2 * np.pi * 440 * t) * 0.9] * 2)
    out, stats = glue_comp(sig, thresh_dbfs=-16.5, ratio=1.5, max_gr_db=1.5, sr=SR)
    assert out.shape == sig.shape
    assert stats['max_gr_db'] <= 1.51
    assert np.max(np.abs(out)) <= np.max(np.abs(sig))


def test_limiter_intersample_peaks():
    t = np.linspace(0, 0.1, int(SR * 0.1), endpoint=False)
    sig = np.column_stack([np.sin(2 * np.pi * 12000 * t) * 1.5] * 2)
    out, info = tp_limit(sig, ceiling_db=-1.25, sr=SR)
    assert true_peak_db(out) <= -1.20
    assert info['engaged'] is True


def test_true_peak_chunked_matches_whole_signal():
    """Chunk context edges must not be measured (they ring and used to read ~1 dB high)."""
    t = np.arange(SR * 6) / SR
    fade = np.minimum(1.0, t / 3.0)
    x = np.column_stack([0.8 * np.sin(2 * np.pi * 4000 * t) * fade] * 2).astype(np.float32)
    whole = true_peak_db(x, chunk=len(x) + 1)
    chunked = true_peak_db(x, chunk=1 << 15)
    assert abs(whole - chunked) < 0.05, (whole, chunked)
    _, info = tp_limit(x, ceiling_db=-1.5, sr=SR)
    assert info['tp_after'] <= -1.49


def test_quantize_to_16bit():
    x = np.array([[-0.5, 0.5], [-0.0001, 0.0001], [0.9999, -0.9999]], dtype=np.float64)
    q = quantize_to_16bit(x, dither=True, seed=123)
    assert q.dtype == np.int16


def test_loudness_matches_ffmpeg_ebur128(tmp_path):
    """Energy-domain gating (BS.1770-4 / EBU 3342) — the prototype averaged dB values."""
    exe = ffmpeg_info()['path']
    if not exe:
        pytest.skip('no ffmpeg')
    rng = np.random.default_rng(0)
    t = np.arange(SR * 40) / SR
    env = np.where((t // 5) % 2 == 0, 0.5, 0.05)  # very dynamic: 20 dB steps
    x = (rng.standard_normal((len(t), 2)) * env[:, None] * 0.3).astype(np.float32)
    p = tmp_path / 'x.wav'
    sf.write(str(p), x, SR, subtype='FLOAT')
    r = subprocess.run(
        [exe, '-hide_banner', '-nostats', '-i', str(p), '-filter_complex', 'ebur128', '-f', 'null', '-'],
        capture_output=True,
        text=True,
    )
    tail = r.stderr[r.stderr.rfind('Summary:') :]
    ff_i = float(re.search(r'I:\s*(-?[\d.]+)', tail).group(1))
    ff_lra = float(re.search(r'LRA:\s*(-?[\d.]+)', tail).group(1))
    assert abs(integrated_lufs(x, SR) - ff_i) < 0.15, (integrated_lufs(x, SR), ff_i)
    assert abs(lra(x, SR) - ff_lra) < 0.5, (lra(x, SR), ff_lra)


def test_programme_loudness_below_the_gate():
    """Whisper-quiet audio sits entirely under BS.1770's -70 LUFS gate; gain decisions
    still need its real level. Silence and empty input stay at the gate."""
    x = (np.random.default_rng(3).standard_normal((SR * 10, 2)) * 0.1).astype(np.float32)
    ref = integrated_lufs(x, SR)
    quiet = x * np.float32(10 ** (-65 / 20))
    assert integrated_lufs(quiet, SR) == -70.0
    assert abs(programme_loudness(quiet, SR) - (ref - 65)) < 0.05
    assert programme_loudness(np.zeros((SR, 2), np.float32), SR) == -70.0
    assert integrated_lufs(np.zeros((0, 2), np.float32), SR) == -70.0 and lra(np.zeros((0, 2)), SR) == 0.0


def test_spectral_gate_reduces_noise_in_pauses():
    rng = np.random.default_rng(1)
    n = SR * 12
    t = np.arange(n) / SR
    tone = 0.3 * np.sin(2 * np.pi * 300 * t) * ((t % 2.0) < 1.0)  # 1 s on / 1 s off
    noise = 0.003 * rng.standard_normal(n)
    x = np.column_stack([tone + noise, tone + noise]).astype(np.float32)
    y, stats = spectral_gate(x, sr=SR, mode='light', chunk_s=4.0)  # small chunks: exercise seams
    assert stats['applied'] and y.shape == x.shape
    pauses = (t % 2.0) > 1.2
    before = 10 * np.log10(np.mean(x[pauses] ** 2))
    after = 10 * np.log10(np.mean(y[pauses] ** 2))
    assert after < before - 4.0, (before, after)
    on = ((t % 2.0) > 0.2) & ((t % 2.0) < 0.8)
    loss = 10 * np.log10(np.mean(x[on] ** 2) / np.mean(y[on] ** 2))
    assert abs(loss) < 0.5, f'tone level changed by {loss:.2f} dB'


def test_exciter_adds_content_above_cutoff_only():
    rng = np.random.default_rng(2)
    noise = rng.standard_normal(SR * 3)
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(len(noise), 1 / SR)
    spec[freqs > 16000] = 0
    band = np.fft.irfft(spec, n=len(noise)) * 0.1
    x = np.column_stack([band, band]).astype(np.float32)
    before = x.copy()
    y, stats = hf_restore(x, SR, 16000.0)
    assert stats['applied']
    added = np.fft.rfft((y - before)[:, 0])
    above = np.abs(added[freqs > 16500]) ** 2
    below = np.abs(added[freqs < 15000]) ** 2
    assert above.sum() > 50 * below.sum()


def test_long_steps_offer_cancellation_points():
    """Full-length steps call `check_cancel` once per block, so a Cancel takes effect
    quickly whatever the programme length (and a raised Cancelled escapes)."""
    from functools import partial

    from audiolab.errors import Cancelled

    x = (np.random.default_rng(5).standard_normal((SR * 60, 2)) * 0.1).astype(np.float32)
    steps = {
        'filtfilt_stream': lambda c: filtfilt_stream(highpass_fq(25.0, sr=SR), x, block=1 << 18, check_cancel=c),
        'true_peak_db': lambda c: true_peak_db(x, chunk=1 << 18, check_cancel=c),
        'programme_loudness': lambda c: programme_loudness(x, SR, check_cancel=c),
        'lra': lambda c: lra(x, SR, check_cancel=c),
        'glue_comp': lambda c: glue_comp(x, sr=SR, check_cancel=c),
        'add_24bit_tpdf': lambda c: add_24bit_tpdf(x.copy(), check_cancel=c),
    }

    def stop():
        raise Cancelled()

    for name, step in steps.items():
        calls = []
        step(partial(calls.append, 1))
        assert calls, name
        if name in ('filtfilt_stream', 'true_peak_db', 'programme_loudness'):
            assert len(calls) >= 6, (name, len(calls))  # per block, not once per call
        with pytest.raises(Cancelled):
            step(stop)
