"""Recipe resolution, the mastering chain, previews and (gated) AI separation.

Hermetic: synthetic fixtures in temp dirs. The Demucs test downloads/loads a
model and is gated behind AUDIOLAB_RUN_AI_TESTS=1.
"""

import os
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from audiolab.analysis.profile import analyze_file
from audiolab.dsp.loudness import integrated_lufs
from audiolab.errors import AudioLabError
from audiolab.media.ffmpeg import ffmpeg_info
from audiolab.previews import make_previews
from audiolab.recipes import RECIPES, list_recipes, recipe_for_type, render, resolve
from audiolab.recipes.base import LIGHT_MAX_LIMIT_DB, LIGHT_TARGETS

SR = 48000


def make_synthetic_set(path: Path, duration_s: float = 12.0, level: float = 0.5) -> Path:
    """Club-ish fixture: kick pulses, sub bass, hats, stereo pad."""
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    kick = np.sin(2 * np.pi * 55 * t) * np.exp(-((t % 0.5) * SR) / (0.12 * SR))
    sub = 0.4 * np.sin(2 * np.pi * 40 * t)
    hat = np.random.default_rng(1).standard_normal(n) * 0.05 * (np.sin(2 * np.pi * 2 * t) > 0)
    pad = 0.08 * np.sin(2 * np.pi * 220 * t) + 0.08 * np.sin(2 * np.pi * 277 * t)
    st = np.column_stack([kick * 0.8 + sub + hat + pad, kick * 0.8 + sub + hat + 0.9 * pad])
    st = st / np.abs(st).max() * level
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), st.astype(np.float32), SR, subtype='PCM_24', format='FLAC')
    return path


def _ffmpeg_measure(path: Path):
    exe = ffmpeg_info()['path']
    if not exe:
        return None
    r = subprocess.run(
        [exe, '-hide_banner', '-nostats', '-i', str(path), '-filter_complex', 'ebur128=peak=true', '-f', 'null', '-'],
        capture_output=True,
        text=True,
    )
    tail = r.stderr[r.stderr.rfind('Summary:') :]
    i = float(re.search(r'I:\s*(-?[\d.]+)', tail).group(1))
    peak = float(re.search(r'Peak:\s*(-?[\d.]+)', tail).group(1))
    return i, peak


def test_registry():
    assert set(RECIPES) == {'club', 'live', 'music', 'voice'}
    assert recipe_for_type('dj_set') == 'club' and recipe_for_type('unknown') == 'music'
    assert all({'key', 'name', 'short', 'description'} <= set(r) for r in list_recipes())


def test_resolve_overrides_and_safety():
    prof = {
        'integrated_lufs': -14.0,
        'lra': 5.0,
        'has_mud_excess': True,
        'bandwidth': {'is_lossy_cutoff': True, 'cutoff_hz': 16000},
        'stereo': {'antiphase_bass': True},
        'dynamics_style': {
            'type': 'club',
            'recommended_target_lufs': -12.5,
            'recommended_glue_comp': True,
            'recommended_bass_punch_db': 1.5,
        },
    }
    p, d = resolve('club', prof)
    assert p.mono_bass_hz is None, 'antiphase bass must disable the mono fold'
    assert p.air_db == 0.0 and p.presence_db == 0.0, 'no top-end boost on lossy sources (club)'
    assert p.target_lufs == -12.5 and p.compressor == 'glue' and p.tp_ceiling_db == -1.25
    assert all(x['why'] for x in d)
    p, d = resolve(
        'club',
        prof,
        advanced={
            'target_lufs': -99,
            'hf_restore': 'on',
            'bass_db': 'auto',
            'tp_ceiling_db': '-0.5',
            'sample_rate': 96000,
        },
    )
    assert p.target_lufs == -30.0 and p.hf_restore is True and p.tp_ceiling_db == -0.5
    assert p.sample_rate == 96000
    p, _ = resolve('music', {'integrated_lufs': -22.0, 'lra': 6.0})
    assert p.target_lufs == -16.0 and p.sub_harmonics == 0 and p.mono_bass_hz is None
    p, _ = resolve('music', {'integrated_lufs': -10.3, 'lra': 6.0})
    assert p.target_lufs == -10.3, 'songs keep their loudness inside the sane window'
    p, _ = resolve('voice', {'integrated_lufs': -24.0, 'snr_db': 20.0, 'noise_floor_db': -50.0})
    assert p.denoise == 'off' and p.compressor == 'leveler' and p.target_lufs == -16.0


def test_render_each_recipe_hits_targets(tmp_path):
    src = make_synthetic_set(tmp_path / 'set.flac', level=0.3)
    prof, _, _ = analyze_file(src)
    for key in RECIPES:
        params, _ = resolve(key, prof)
        out = tmp_path / f'{key}.flac'
        rep = render(src, out, params, prof, tmp_path / f'work_{key}')
        assert out.exists() and rep['steps'] and 'input' in rep
        assert rep['limiter'].get('ceiling_met', True), rep['limiter']
        ff = _ffmpeg_measure(out)
        if ff:
            i, peak = ff
            assert abs(i - params.target_lufs) < 0.7, f'{key}: {i} vs {params.target_lufs}'
            assert peak <= params.tp_ceiling_db + 0.3, f'{key}: peak {peak} > {params.tp_ceiling_db}'
        print(
            f'RENDER {key:5s}: target {params.target_lufs:+.1f} -> {rep["integrated_lufs"]:+.2f} LUFS, '
            f'TP {rep["true_peak_dbtp"]:+.2f} (ffmpeg {ff})'
        )


def test_light_is_volume_only():
    prof = {
        'integrated_lufs': -20.0,
        'lra': 5.0,
        'has_mud_excess': True,
        'snr_db': 20.0,
        'noise_floor_db': -50.0,
        'bandwidth': {'is_lossy_cutoff': True, 'cutoff_hz': 16000},
        'dynamics_style': {'recommended_target_lufs': -12.5, 'recommended_bass_punch_db': 1.5},
    }
    for key in RECIPES:
        p, d = resolve(key, prof, quality='light')
        assert p.volume_only and p.target_lufs == LIGHT_TARGETS[key][0], key
        assert (p.highpass_hz, p.mud_cut_db, p.bass_db, p.presence_db, p.air_db, p.sub_harmonics) == (0,) * 6, key
        assert p.mono_bass_hz is None and not p.hf_restore and p.denoise == 'off' and p.compressor == 'off'
        assert p.ai_stage == 'none' and all(x['why'] for x in d)
        assert any('volume only, plus a safety limiter on the few peaks' in x['why'] for x in d)
    p, d = resolve(
        'club',
        prof,
        quality='light',
        advanced={'target_lufs': -9, 'tp_ceiling_db': -2, 'bass_db': 3, 'glue': 'on', 'sample_rate': 44100},
    )
    assert (p.target_lufs, p.tp_ceiling_db, p.sample_rate) == (-9.0, -2.0, 44100)
    assert p.bass_db == 0 and p.compressor == 'off' and any(x['key'] == 'ignored' for x in d)


def test_light_render_changes_only_the_volume(tmp_path):
    src = make_synthetic_set(tmp_path / 'quiet.flac', level=0.1)
    prof, _, _ = analyze_file(src)
    params, _ = resolve('live', prof, quality='light')  # -16 LUFS: no peak gets near the ceiling
    rep = render(src, tmp_path / 'light.flac', params, prof, tmp_path / 'w')
    assert abs(rep['integrated_lufs'] - params.target_lufs) < 0.1 and not rep['limiter']['engaged'], rep
    a, _ = sf.read(str(src), dtype='float64')
    b, _ = sf.read(str(tmp_path / 'light.flac'), dtype='float64')
    g = float((a * b).sum() / (a * a).sum())
    resid_db = 10 * np.log10(((b - g * a) ** 2).mean() / (b**2).mean())
    assert resid_db < -100, f'light changed more than the volume ({resid_db:.0f} dB residual)'
    pv = make_previews(src, tmp_path / 'light.flac', tmp_path / 'pv', duration_s=5.0)
    x, _ = sf.read(pv['original'], dtype='float64')
    y, _ = sf.read(pv['enhanced'], dtype='float64')
    assert abs(20 * np.log10(float((x * y).sum() / (x * x).sum()) / g)) < 0.01  # the excerpts keep the real gain

    # Peaky material: reaching the target would need more limiting than allowed → stays quieter.
    t = np.arange(SR * 10) / SR
    clicks = 0.002 * np.sin(2 * np.pi * 220 * t) + 0.5 * ((np.arange(len(t)) % SR) < 48)
    peaky = tmp_path / 'peaky.wav'
    sf.write(str(peaky), np.column_stack([clicks, clicks]).astype(np.float32), SR, subtype='FLOAT')
    prof, _, _ = analyze_file(peaky)
    params, _ = resolve('music', prof, quality='light')
    rep = render(peaky, tmp_path / 'peaky.flac', params, prof, tmp_path / 'w2')
    assert rep['integrated_lufs'] < params.target_lufs - 1 and any('Stayed at' in w for w in rep['warnings'])
    assert rep['limiter']['max_gr_db'] <= LIGHT_MAX_LIMIT_DB + 0.2 and rep['limiter']['ceiling_met'], rep['limiter']

    # Light removes a DC offset first; the gain must come from the level after that.
    t = np.arange(int(0.4 * SR)) / SR
    dc = 0.08 * np.sin(2 * np.pi * 440 * t) + 0.5
    sf.write(str(tmp_path / 'dc.wav'), np.column_stack([dc, dc]).astype(np.float32), SR, subtype='FLOAT')
    prof, _, _ = analyze_file(tmp_path / 'dc.wav')
    rep = render(
        tmp_path / 'dc.wav', tmp_path / 'dc.flac', resolve('music', prof, quality='light')[0], prof, tmp_path / 'w4'
    )
    y, _ = sf.read(str(tmp_path / 'dc.flac'), dtype='float32')
    assert abs(integrated_lufs(y, SR) - rep['integrated_lufs']) < 0.1 and abs(rep['integrated_lufs'] + 14) < 0.1, rep

    silent = tmp_path / 'silent.wav'
    sf.write(str(silent), np.zeros((SR * 3, 2), np.float32), SR)
    for quality in ('light', 'standard'):
        try:
            render(silent, tmp_path / 's.flac', resolve('music', {}, quality=quality)[0], {}, tmp_path / 'w3')
            raise AssertionError('silent input must be refused')
        except AudioLabError as e:
            assert 'silent' in str(e) and e.hints


def test_previews_keep_the_true_levels_and_never_clip(tmp_path):
    quiet = make_synthetic_set(tmp_path / 'quiet.flac', level=0.05)  # original much quieter
    loud = make_synthetic_set(tmp_path / 'loud.flac', level=0.98)
    pv = make_previews(quiet, loud, tmp_path / 'pv', duration_s=5.0)
    rms = {}
    for key in ('original', 'enhanced'):
        x, sr = sf.read(pv[key], dtype='float64')
        assert np.abs(x).max() <= 0.9, f'{key} preview too hot: {np.abs(x).max()}'
        assert sr == SR and len(x) == 5 * SR
        rms[key] = np.sqrt((x**2).mean())
    # not matched: the player matches the volume from the whole song, the excerpts keep the real difference
    assert abs(20 * np.log10(rms['enhanced'] / rms['original']) - 20 * np.log10(0.98 / 0.05)) < 0.05
    assert pv['shared_gain_db'] < 0 and sorted(p.name for p in (tmp_path / 'pv').iterdir()) == [
        'preview_enhanced.wav',
        'preview_original.wav',
    ]
    assert pv['duration_s'] == 5.0 and 2.4 <= pv['start_s'] <= 7.0  # a loud passage in the middle
    assert round(pv['start_s'] * 25, 6).is_integer()  # on the compare player's 1/25 s grid


def test_ai_best_quality(tmp_path):
    if os.environ.get('AUDIOLAB_RUN_AI_TESTS', '').lower() not in ('1', 'true', 'yes'):
        pytest.skip('set AUDIOLAB_RUN_AI_TESTS=1 to run Demucs (downloads a model)')
    pytest.importorskip('torch')
    pytest.importorskip('demucs')
    from audiolab.ai import ai_status
    from audiolab.ai.separation import mix_stems, separate, stems_peak

    assert ai_status()['available']
    src = make_synthetic_set(tmp_path / 'slice.flac', duration_s=20.0, level=0.4)
    sep = separate(src, tmp_path / 'stems')
    assert set(sep['stems']) == {'drums', 'bass', 'other', 'vocals'}
    assert sep['gate_passed'], sep
    assert all(sf.info(p).subtype == 'FLOAT' for p in sep['stems'].values()), 'stems must be unclipped float'
    inst = mix_stems([Path(sep['stems'][k]) for k in ('drums', 'bass', 'other')], tmp_path / 'inst.wav')
    assert sf.info(str(inst)).frames == sf.info(str(src)).frames
    assert 0.0 < stems_peak([Path(p) for p in sep['stems'].values()]) < 4.0
    prof, _, _ = analyze_file(src)
    params, dec = resolve('club', prof, quality='best')
    assert params.ai_stage == 'stem_eq'
    rep = render(src, tmp_path / 'm.flac', params, prof, tmp_path / 'w')
    assert rep['ai'] and not rep['ai'].get('fallback'), rep['ai']


def test_enhance_result_has_an_excerpt_and_no_difference(tmp_path, monkeypatch):
    from audiolab import tasks
    from audiolab.context import TaskContext
    from audiolab.media import proc
    from audiolab.tasks.enhance import _summary

    monkeypatch.setattr(proc, '_POLL_S', 0.01)  # short ffmpeg runs: don't wait 0.2 s for each

    src = make_synthetic_set(tmp_path / 'song.flac', duration_s=8.0, level=0.3)
    spec = tasks.get('enhance')
    source = {'kind': 'file', 'name': 'song.flac', 'path': str(src)}
    opts = tasks.validate('enhance', {'quality': 'light', 'formats': ['wav16']}, source)
    r = spec.run(TaskContext('t', tmp_path / 'job', opts, spec.stages(opts, source)), source)
    assert [p['key'] for p in r['previews']] == ['original', 'enhanced']
    assert sorted(r['_media']) == ['enhanced', 'original']
    assert r['excerpt'] == {'start_s': 0.0, 'duration_s': 8.0}  # shorter than 45 s: all of it
    assert 'difference_db' not in r['metrics'] and r['metrics']['before']['lufs'] is not None
    assert any(s.startswith('Volume only') for s in r['summary'])

    # Light says "the sound was not changed" only when the safety limiter didn't touch a peak.
    kind, recipe = {'label': 'Music', 'confidence_label': 'high'}, RECIPES['music']
    before, after = (
        {'integrated_lufs': -20.0, 'true_peak_dbtp': -3.0},
        {'integrated_lufs': -14.0, 'true_peak_dbtp': -1.0},
    )
    for engaged, line in (
        (False, 'Volume only — the sound itself was not changed.'),
        (True, 'Volume only, plus a safety limiter on a few peaks.'),
    ):
        report = {'params': {'volume_only': True}, 'limiter': {'engaged': engaged, 'max_gr_db': 1.9}}
        lines = _summary(kind, recipe, True, before, after, report, tmp_path, [])
        assert line in lines and (('1.9 dB' in ' '.join(lines)) is engaged)
