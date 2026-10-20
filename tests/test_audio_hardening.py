"""Focused regressions for bounded analysis, explicit denoise and AI ownership.

Long recordings use virtual SoundFile I/O; physical fixtures are short generated
signals. No optional model, network, user recording or running app is needed.
"""

import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from audiolab.analysis import classify as cls
from audiolab.analysis import profile as prof
from audiolab.context import Progress
from audiolab.errors import Cancelled
from audiolab.recipes import chain
from audiolab.recipes.base import RECIPES, ChainParams, resolve
from audiolab.tasks.enhance import validate

SR = 48000


def _tone(seconds=2.0):
    t = np.arange(int(seconds * SR)) / SR
    mono = 0.15 * np.sin(2 * np.pi * 180 * t) + 0.03 * np.sin(2 * np.pi * 880 * t)
    return np.column_stack((mono, mono)).astype(np.float32)


class _VirtualAudio:
    """A 240 s file with generated sound in selected one-second blocks."""

    def __init__(self, monkeypatch, audible_seconds, cancel_at=None):
        self.audible = set(audible_seconds)
        self.cancel_at = cancel_at
        self.cancel = threading.Event()
        self.reads = []
        self.blocks_seen = 0
        self.info = SimpleNamespace(samplerate=SR, frames=240 * SR, duration=240.0, channels=2)
        self.tone = _tone(1.0)
        monkeypatch.setattr(prof.sf, 'info', lambda *a, **kw: self.info)
        monkeypatch.setattr(prof.sf, 'read', self.read)
        monkeypatch.setattr(prof.sf, 'blocks', self.blocks)

    def read(self, path, start=0, stop=None, **kwargs):
        stop = self.info.frames if stop is None else stop
        self.reads.append((start, stop))
        x = np.zeros((stop - start, 2), np.float32)
        for second in self.audible:
            lo, hi = max(start, second * SR), min(stop, (second + 1) * SR)
            if lo < hi:
                x[lo - start : hi - start] = self.tone[lo - second * SR : hi - second * SR]
        return x, SR

    def blocks(self, path, **kwargs):
        assert kwargs['blocksize'] == SR
        silent = np.zeros((SR, 2), np.float32)
        for second in range(240):
            self.blocks_seen += 1
            if self.blocks_seen == self.cancel_at:
                self.cancel.set()
            yield self.tone if second in self.audible else silent


@pytest.mark.parametrize('audible,expected_span', [({0, 239}, (0, 180)), (range(235, 240), (235, 240))])
def test_fallback_contains_detected_sound_and_stays_bounded(monkeypatch, audible, expected_span):
    audio = _VirtualAudio(monkeypatch, audible)
    report, x, sr = prof.analyze_file(Path('virtual.wav'))
    assert not report['is_silent'] and np.abs(x).max() > 0.1
    assert sr == SR and x.dtype == np.float32 and x.shape[1] == 2
    assert audio.reads[-1] == tuple(int(s * SR) for s in expected_span)
    assert len(x) == (expected_span[1] - expected_span[0]) * SR <= 180 * SR
    assert all(stop - start <= 180 * SR for start, stop in audio.reads)
    assert len(audio.reads) == 4 and audio.blocks_seen == 240
    assert 'contiguous excerpt' in report['analysis_basis'] and 'detected sound' in report['analysis_basis']
    assert 'rest is silent' not in report['analysis_basis']


def test_silent_excerpts_need_a_complete_silence_scan(monkeypatch):
    audio = _VirtualAudio(monkeypatch, set())
    report, x, _ = prof.analyze_file(Path('virtual.wav'))
    assert report['is_silent'] and not x.any()
    assert audio.blocks_seen == 240 and len(audio.reads) == 3
    assert report['analysis_basis'] == 'whole-file silence confirmed by a streamed peak scan'


def test_fallback_scan_cancellation_stops_on_the_requested_block(monkeypatch):
    audio = _VirtualAudio(monkeypatch, {0, 239}, cancel_at=3)
    with pytest.raises(Cancelled):
        prof.analyze_file(Path('virtual.wav'), progress=Progress.null(audio.cancel))
    assert audio.blocks_seen == 3 and len(audio.reads) == 3


def test_audible_scan_works_without_progress(monkeypatch):
    audio = _VirtualAudio(monkeypatch, {2, 239})
    assert prof._audible_span(Path('virtual.wav')) == (2 * SR, 240 * SR)
    assert audio.blocks_seen == 240


@pytest.mark.parametrize('channels', [1, 3])
def test_sample_loading_keeps_sanitization_and_stereo_rules(tmp_path, channels):
    x = np.repeat(_tone(0.1)[:, :1], channels, axis=1)
    x[0, 0], x[1, 0], x[2, 0] = np.nan, np.inf, -np.inf
    path = tmp_path / 'broken.wav'
    sf.write(path, x, SR, subtype='FLOAT')
    got, sr, basis = prof.load_samples(path)
    assert sr == SR and basis == 'whole file' and got.shape == (len(x), 2)
    assert np.isfinite(got).all() and not got[:3, 0].any()
    expected = np.repeat(x, 2, axis=1) if channels == 1 else x[:, :2]
    assert np.array_equal(got, np.nan_to_num(expected, nan=0.0, posinf=0.0, neginf=0.0))


def test_successful_ai_buffer_is_released_during_real_post_processing(tmp_path, monkeypatch):
    src = tmp_path / 'generated.wav'
    sf.write(src, _tone(), SR, subtype='FLOAT')
    refs, observations = [], []

    def fake_ai(*args, **kwargs):
        x = _tone()
        refs.append(weakref.ref(x))
        return {'x': x, 'report': {'fallback': False, 'model': 'not-run'}, 'warning': None}

    real_filter, real_write = chain.filtfilt_stream, chain._write_master

    def observe_filter(sos, x, **kwargs):
        observations.append(('filter', refs[0]() is not None))
        return real_filter(sos, x, **kwargs)

    def observe_write(x, *args, **kwargs):
        observations.append(('write', refs[0]() is not None))
        return real_write(x, *args, **kwargs)

    monkeypatch.setattr(chain, '_ai_stage', fake_ai)
    monkeypatch.setattr(chain, 'filtfilt_stream', observe_filter)
    monkeypatch.setattr(chain, '_write_master', observe_write)
    dest = tmp_path / 'master.flac'
    params = ChainParams(ai_stage='voice_isolate', presence_db=0.5)
    report = chain.render(src, dest, params, {}, tmp_path / 'work')
    assert observations == [('filter', True), ('filter', False), ('write', False)]
    assert refs[0]() is None and not report['ai']['fallback']
    assert sf.info(dest).frames == sf.info(src).frames and report['limiter'].get('ceiling_met', True)


@pytest.mark.parametrize('outcome', ['failed_gate', 'exception', 'cancelled'])
def test_ai_fallback_and_cancellation_stay_safe(tmp_path, monkeypatch, outcome):
    src, dest = tmp_path / 'generated.wav', tmp_path / 'master.flac'
    sf.write(src, _tone(), SR, subtype='FLOAT')
    real_read = chain._read_ref
    reads = []

    def counted_read(*args, **kwargs):
        reads.append(args[0])
        return real_read(*args, **kwargs)

    def fake_ai(*args, **kwargs):
        if outcome == 'cancelled':
            raise Cancelled()
        if outcome == 'exception':
            raise RuntimeError('model stand-in failed')
        return {'x': None, 'report': {'fallback': True}, 'warning': 'AI gate failed — used standard'}

    monkeypatch.setattr(chain, '_read_ref', counted_read)
    monkeypatch.setattr(chain, '_ai_stage', fake_ai)
    if outcome == 'cancelled':
        with pytest.raises(Cancelled):
            chain.render(src, dest, ChainParams(ai_stage='stem_eq'), {}, tmp_path / 'work')
        assert len(reads) == 1 and not dest.exists()
        assert not dest.with_name('master.partial.flac').exists()
    else:
        report = chain.render(src, dest, ChainParams(ai_stage='stem_eq'), {}, tmp_path / 'work')
        assert len(reads) == 2 and report['ai']['fallback'] and report['warnings']
        assert sf.info(dest).frames == sf.info(src).frames and report['limiter'].get('ceiling_met', True)


@pytest.mark.parametrize('programme', ['steady', 'changing'])
def test_clean_programme_never_diagnoses_noise_or_selects_default_denoise(programme):
    x = _tone(8.0)
    if programme == 'changing':
        t = np.arange(len(x)) / SR
        env = np.where(t % 1.0 < 0.5, 1.0, 0.1)
        mono = 0.15 * env * np.sin(2 * np.pi * 180 * t) + 0.03 * np.sin(2 * np.pi * 880 * t)
        x[:] = mono[:, None]
    report = prof.profile(x, SR, 8.0, 'whole file')
    # Both programmes fall inside the former automatic denoise thresholds.
    assert report['snr_db'] < 35 and report['noise_floor_db'] > -65
    assert all('noise' not in alert['title'].lower() for alert in report['alerts'])
    for key in RECIPES:
        params, decisions = resolve(key, report)
        assert params.denoise == 'off'
        assert all('background is already quiet' not in d['why'] for d in decisions)
        assert all('background noise is audible' not in d['why'] for d in decisions)


@pytest.mark.parametrize('choice', [None, 'auto', 'off', 'light', 'strong'])
def test_denoise_requires_explicit_choice_and_light_quality_stays_volume_only(choice):
    advanced = {} if choice is None else {'denoise': choice}
    opts = validate({'advanced': advanced, 'formats': ['wav16']}, {'kind': 'file'})
    profile = {'integrated_lufs': -24.0, 'snr_db': 20.0, 'noise_floor_db': -50.0}
    for key in RECIPES:
        params, decisions = resolve(key, profile, advanced=opts['advanced'])
        assert params.denoise == (choice if choice in ('light', 'strong') else 'off')
        if choice in ('off', 'light', 'strong'):
            assert any(d['key'] == 'denoise' and d['source'] == 'user' for d in decisions)
        light, _ = resolve(key, profile, quality='light', advanced=opts['advanced'])
        assert light.volume_only and light.denoise == 'off'


def test_profile_reuses_welch_spectrum_for_bandwidth(monkeypatch):
    x = np.random.default_rng(42).standard_normal((2 * SR, 2)).astype(np.float32) * 0.1
    standalone = prof.detect_bandwidth(x, SR, source_rate=32000)
    calls = []
    real_psd = prof.average_psd

    def counted_psd(*args, **kwargs):
        calls.append(True)
        return real_psd(*args, **kwargs)

    monkeypatch.setattr(prof, 'average_psd', counted_psd)
    report = prof.profile(x, SR, 2.0, 'whole file', source_rate=32000)
    assert len(calls) == 1 and report['bandwidth'] == standalone
    assert standalone['cutoff_hz'] == 16000 and standalone['is_lossy_cutoff']


def test_features_reuse_known_lra_and_preserve_standalone_classification(monkeypatch):
    x = _tone(8.0)
    calls = []
    real_lra = cls.lra

    def counted_lra(*args, **kwargs):
        calls.append(True)
        return real_lra(*args, **kwargs)

    monkeypatch.setattr(cls, 'lra', counted_lra)
    standalone = cls.audio_features(x, SR)
    assert len(calls) == 1 and 'lufs' not in standalone
    supplied = cls.audio_features(x, SR, known_lra=standalone['lra'])
    assert len(calls) == 1 and supplied == standalone
    assert cls.classify(supplied, 180) == cls.classify(standalone, 180)
    zero = cls.audio_features(x, SR, known_lra=0.0)
    assert zero['lra'] == 0.0 and len(calls) == 1


def test_standalone_features_keep_unsupported_rate_behavior():
    assert cls.audio_features(_tone(0.5), 44100)['lra'] is None
