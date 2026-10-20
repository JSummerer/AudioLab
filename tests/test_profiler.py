"""Profile + classifier tests using generated signals."""

import numpy as np
import soundfile as sf

from audiolab.analysis.classify import audio_features, classify
from audiolab.analysis.key import camelot, estimate_key
from audiolab.analysis.profile import analyze_file, detect_bandwidth, profile

SR = 48000


def _club(seconds: float = 40.0) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(1)
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-((t % 0.476) * SR) / (0.12 * SR))
    hat = rng.standard_normal(n) * 0.04 * (((t + 0.238) % 0.476) < 0.03)
    pad = 0.08 * np.sin(2 * np.pi * 220 * t) + 0.08 * np.sin(2 * np.pi * 277 * t)
    sig = kick * 0.8 + 0.3 * np.sin(2 * np.pi * 41 * t) + hat
    st = np.column_stack([sig + pad, sig + 0.9 * pad])
    return (st / (np.abs(st).max() * 1.1)).astype(np.float32)


def _speechlike(seconds: float = 40.0) -> np.ndarray:
    """Formant-ish bursts at syllable rate with phrase pauses (no beat, little bass)."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(5)
    f0 = 140 + 25 * np.sin(2 * np.pi * 0.7 * t)
    voiced = sum(np.sin(2 * np.pi * h * np.cumsum(f0) / SR) / h for h in range(2, 12))
    syll = (np.sin(2 * np.pi * 4.3 * t + rng.uniform(0, 6)) > 0.1).astype(float)
    phrase = ((t % 3.1) < 2.2).astype(float)
    env = np.convolve(syll * phrase, np.ones(480) / 480, mode='same')
    sig = voiced * env * 0.2 + 0.0005 * rng.standard_normal(n)
    return np.column_stack([sig, sig]).astype(np.float32)


def _progression(chords, reps: int = 4, seconds: float = 2.0) -> np.ndarray:
    """Chords of harmonic-rich tones (6 partials, 1/h) plus a bass note an octave down."""
    t = np.arange(int(seconds * SR)) / SR
    env = np.minimum(1.0, t / 0.02) * np.exp(-t / 3.0)
    out = []
    for _ in range(reps):
        for chord in chords:
            seg = np.zeros_like(t)
            for note in list(chord) + [chord[0] - 12]:
                f = 440.0 * 2 ** ((note - 69) / 12)
                seg += sum(np.sin(2 * np.pi * f * h * t) / h for h in range(1, 7))
            out.append(seg * env)
    x = np.concatenate(out)
    x = x / np.abs(x).max() * 0.5
    return np.column_stack([x, x]).astype(np.float32)


def test_key_estimate():
    """Exact key on chord progressions in a known key, transposed; None for no-key material.
    (Am–F–C–G alone would be ambiguous with C major — the relative keys share all notes —
    so the minor test uses the E major dominant with its G#, as real minor-key music does.)"""
    assert camelot(0, False) == '8B' and camelot(9, True) == '8A' and camelot(4, False) == '12B'
    a_minor = [[57, 60, 64], [53, 57, 60], [50, 53, 57], [52, 56, 59], [57, 60, 64]]  # Am F Dm E Am
    c_major = [[60, 64, 67], [65, 69, 72], [67, 71, 74], [60, 64, 67]]  # C F G C
    for chords, shift, name, code in (
        (a_minor, 0, 'A minor', '8A'),
        (a_minor, 3, 'C minor', '5A'),
        (c_major, 0, 'C major', '8B'),
        (c_major, -5, 'G major', '9B'),
    ):
        k = estimate_key(_progression([[n + shift for n in c] for c in chords]), SR)
        assert k and k['name'] == name and k['camelot'] == code and k['confidence'] >= 0.5, (name, k)
    noise = (np.random.default_rng(4).standard_normal((SR * 20, 2)) * 0.1).astype(np.float32)
    assert estimate_key(noise, SR) is None
    assert estimate_key(_speechlike(), SR) is None
    assert estimate_key(np.zeros((SR * 10, 2), np.float32), SR) is None
    t = np.arange(SR * 20) / SR
    square = np.sign(np.sin(2 * np.pi * 110 * t)).astype(np.float32)  # one note, rich harmonics
    assert estimate_key(np.column_stack([square, square]), SR) is None


def test_edge_inputs():
    """Silence, a +6 dBFS float file, a big DC offset and plain noise get honest results."""
    silent = profile(np.zeros((SR * 5, 2), np.float32), SR, 5.0, 'whole file')
    assert silent['is_silent'] and silent['key'] is None
    assert [a['title'] for a in silent['alerts']] == ['No sound']
    hot = _club(10.0) * np.float32(2.0 / 0.91)  # peaks at +6 dBFS, nothing flat-topped
    hot_profile = profile(hot, SR, 10.0, 'whole file')
    assert hot_profile['clipped_samples_pct'] < 0.01 and not hot_profile['is_clipping']
    t = np.arange(SR * 5) / SR
    square = np.column_stack([np.sign(np.sin(2 * np.pi * 110 * t))] * 2).astype(np.float32)
    square_profile = profile(square, SR, 5.0, 'whole file')
    assert square_profile['clipped_samples_pct'] > 50 and square_profile['is_clipping']
    dc = profile(_club(10.0) * 0.5 + np.float32(0.3), SR, 10.0, 'whole file')
    assert dc['dc_offset'] > 0.25 and any('DC offset' in a['title'] for a in dc['alerts'])
    noise = (np.random.default_rng(6).standard_normal((SR * 30, 2)) * 0.2).astype(np.float32)
    assert classify(audio_features(noise, SR), 30.0, None)['type'] != 'speech'


def test_silence_is_confirmed_on_the_whole_file(tmp_path):
    """Long files are profiled on excerpts; sound only outside them must not read as silent."""
    late = np.zeros(SR * 240, np.float32)
    late[SR * 235 :] = 0.3 * np.sin(2 * np.pi * 440 * np.arange(SR * 5) / SR)
    sf.write(str(tmp_path / 'late.wav'), late, SR, subtype='PCM_16')
    rep, x, _ = analyze_file(tmp_path / 'late.wav')
    assert not rep['is_silent'] and rep['integrated_lufs'] > -30 and len(x) == SR * 5, rep['analysis_basis']
    sf.write(str(tmp_path / 'silent.wav'), np.zeros(SR * 240, np.float32), SR, subtype='PCM_16')
    assert analyze_file(tmp_path / 'silent.wav')[0]['is_silent']


def test_bandwidth_detects_lossy_cutoff():
    noise = np.random.default_rng(42).standard_normal(SR * 2)
    spec = np.fft.rfft(noise)
    spec[np.fft.rfftfreq(len(noise), 1.0 / SR) > 16000] = 0.0
    det = detect_bandwidth(np.fft.irfft(spec, n=len(noise)), sr=SR)
    assert det['is_lossy_cutoff'] is True
    assert 15500 <= det['cutoff_hz'] <= 16500, det
    full = detect_bandwidth(np.random.default_rng(43).standard_normal(SR * 2), sr=SR)
    assert full['is_lossy_cutoff'] is False and full['full_range'] is True, full
    # The source's own sample rate caps the range: say so, at its real Nyquist frequency.
    spec[np.fft.rfftfreq(len(noise), 1.0 / SR) > 4000] = 0.0
    low = detect_bandwidth(np.fft.irfft(spec, n=len(noise)), sr=SR, source_rate=8000)
    assert low['cutoff_hz'] == 4000 and low['is_lossy_cutoff'] and '8 kHz' in low['verdict'], low
    # A pure tone has no range to judge.
    t = np.arange(SR * 2) / SR
    tone = detect_bandwidth(np.sin(2 * np.pi * 1000 * t), sr=SR)
    assert tone['judged'] is False and tone['cutoff_hz'] is None and not tone['is_lossy_cutoff'], tone


def test_profile_keys_and_alerts():
    x = _club(20.0)
    rep = profile(x, SR, 20.0, 'whole file')
    for key in (
        'integrated_lufs',
        'true_peak_dbtp',
        'lra',
        'bandwidth',
        'bands_db',
        'mud_excess_db',
        'noise_floor_db',
        'stereo',
        'dynamics_style',
        'alerts',
    ):
        assert key in rep, key
    assert all(a['level'] in ('ok', 'info', 'warning', 'danger') for a in rep['alerts'])


def test_classifier_music_vs_speech():
    club = classify(audio_features(_club(), SR), 3600.0, None)
    assert club['type'] == 'dj_set', club
    assert club['bpm'] and abs(club['bpm'] - 126) < 3, club
    song = classify(audio_features(_club(), SR), 200.0, {'title': 'Artist - Track (Official Audio)'})
    assert song['type'] == 'song', song
    speech = classify(audio_features(_speechlike(), SR), 1800.0, None)
    assert speech['type'] == 'speech', speech
    for c in (club, song, speech):
        assert c['reasons'] and c['confidence_label'] in ('high', 'medium', 'low')


def test_classifier_is_conservative_without_evidence():
    c = classify(None, 600.0, {'title': 'upload'})
    assert c['type'] == 'unknown', c
