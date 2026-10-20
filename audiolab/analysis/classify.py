"""Content-type detection: what kind of audio is this?

Types: dj_set, live_concert, song, speech, unknown.

Evidence comes from two places and every piece that matters is turned into a
plain-language reason:
- metadata (title, tags, categories, description keywords, chapters/tracklist,
  duration) — available before downloading, used alone by /api/probe;
- audio features on the profile excerpts — speech-vs-music cues (pauses,
  low-energy ratio, syllable-rate modulation, bass share) and rhythm
  (beat strength / BPM from onset autocorrelation), plus dynamics (LRA).

Scores are summed log-odds-style evidence, turned into probabilities with a
softmax. The result is deliberately conservative: when the winner is not
clearly ahead, the answer is 'unknown' and the safe recipe is used.
"""

import math
import re
from typing import Any

import numpy as np
from scipy.ndimage import maximum_filter1d, median_filter
from scipy.signal import resample_poly

from ..dsp.loudness import lra

TYPES = {
    'dj_set': 'DJ set / club mix',
    'live_concert': 'Live concert',
    'song': 'Song / track',
    'speech': 'Podcast / speech',
    'unknown': 'Mixed / unsure',
}
_REAL_TYPES = ('dj_set', 'live_concert', 'song', 'speech')

_FRAME_RATE = 100  # onset/energy frames per second
_NFFT = 1024


# ---------------------------------------------------------------------------
# Audio features
# ---------------------------------------------------------------------------
def _frame_spectra(mono: np.ndarray, sr: int) -> np.ndarray:
    """Magnitude spectra at 100 frames/s (float32, n_frames x bins)."""
    hop = sr // _FRAME_RATE
    n_frames = 1 + (len(mono) - _NFFT) // hop
    if n_frames < 8:
        return np.zeros((0, _NFFT // 2 + 1), np.float32)
    win = np.hanning(_NFFT).astype(np.float32)
    out = np.empty((n_frames, _NFFT // 2 + 1), np.float32)
    step = 2048
    for f0 in range(0, n_frames, step):
        f1 = min(n_frames, f0 + step)
        idx = np.arange(_NFFT)[None, :] + hop * np.arange(f0, f1)[:, None]
        out[f0:f1] = np.abs(np.fft.rfft(mono[idx] * win, axis=1))
    return out


def _autocorr(onset: np.ndarray) -> np.ndarray | None:
    """Normalised autocorrelation of the onset envelope after removing its slow
    trend (a 1 s moving average), so smooth swells don't read as rhythm."""
    if len(onset) < _FRAME_RATE * 8:
        return None
    o = onset - np.convolve(onset, np.ones(_FRAME_RATE) / _FRAME_RATE, mode='same')
    n = len(o)
    ac = np.fft.irfft(np.abs(np.fft.rfft(o, n=2 * n)) ** 2)[:n]
    if ac[0] <= 0:
        return None
    return ac / ac[0]


def _tempo(onset: np.ndarray) -> tuple[float, float | None]:
    """(beat_strength 0..1, bpm) from the onset envelope autocorrelation."""
    ac = _autocorr(onset)
    if ac is None:
        return 0.0, None
    lag_min = int(_FRAME_RATE * 60 / 200)  # 200 BPM
    lag_max = int(_FRAME_RATE * 60 / 55)  # 55 BPM
    lags = np.arange(lag_min, min(lag_max, len(ac) - 1))
    if len(lags) == 0:
        return 0.0, None
    vals = ac[lags]
    strength = float(max(0.0, vals.max()))
    # Tempo choice: autocorrelation weighted by a log-normal prior around 120 BPM
    bpm_axis = 60.0 * _FRAME_RATE / lags
    prior = np.exp(-0.5 * (np.log2(bpm_axis / 120.0) / 0.9) ** 2)
    i = int(np.argmax(np.maximum(vals, 0) * prior))
    lag = float(lags[i])
    if 0 < i < len(lags) - 1:  # parabolic refinement
        a, b, c = vals[i - 1], vals[i], vals[i + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-12:
            lag += 0.5 * (a - c) / d
    return strength, round(60.0 * _FRAME_RATE / lag, 1)


def _consensus_bpm(onset: np.ndarray, window_s: float = 20.0) -> float | None:
    """BPM only when 20 s windows agree (±4 %, octave-folded) — DJ sets drift."""
    w = int(window_s * _FRAME_RATE)
    est = []
    for s0 in range(0, len(onset) - w + 1, w):
        strength, bpm = _tempo(onset[s0 : s0 + w])
        if bpm and strength >= 0.25:
            est.append(bpm)
    if not est:
        return None
    ref = float(np.median(est))
    folded = [b * 2 if b < ref / 1.5 else b / 2 if b > ref * 1.5 else b for b in est]
    agree = [b for b in folded if abs(b - ref) <= 0.04 * ref]
    if len(agree) < max(1, int(math.ceil(0.6 * len(est)))):
        return None
    return round(float(np.median(agree)), 1)


def tonal_persistence(mono: np.ndarray, sr: int, blocks: int = 8, block_s: float = 6.0) -> float | None:
    """Share of spectral peaks (100 Hz–4 kHz, ≥10 dB above the local spectrum)
    still present ~200 ms later. Sustained notes and chords keep their partials
    in place (music ≈ 0.4–0.8); speech harmonics glide with the voice's pitch
    and formants move (≈ 0.1–0.16), and noise has no stable peaks at all.
    The most robust speech-vs-music cue here, including noisy, non-studio speech.
    Measured on up to `blocks` spread-out 6 s blocks to stay cheap."""
    fs, nfft, hop, lag = 16000, 2048, 160, 20
    if sr % fs == 0:
        y = resample_poly(mono, 1, sr // fs)
    else:
        y = resample_poly(mono, fs, sr)
    blen = int(block_s * fs)
    if len(y) < nfft + hop * (lag + 10):
        return None
    starts = np.linspace(0, max(0, len(y) - blen), min(blocks, max(1, len(y) // blen))).astype(int)
    win = np.hanning(nfft).astype(np.float32)
    lo, hi = int(100 * nfft / fs), int(4000 * nfft / fs)
    kept = total = rich_frames = frames = 0
    for b0 in starts:
        seg = y[b0 : b0 + blen].astype(np.float32)
        nf = 1 + (len(seg) - nfft) // hop
        if nf <= lag + 1:
            continue
        idx = np.arange(nfft)[None, :] + hop * np.arange(nf)[:, None]
        spec = 20.0 * np.log10(np.abs(np.fft.rfft(seg[idx] * win, axis=1))[:, lo:hi] + 1e-9)
        base = median_filter(spec, size=(1, 31))
        peaks = (spec == maximum_filter1d(spec, 3, axis=1)) & (spec > base + 10.0)
        later = maximum_filter1d(peaks[lag:].astype(np.uint8), 3, axis=1).astype(bool)
        now = peaks[:-lag]
        rich = now.sum(axis=1) >= 3  # frames with real tonal content
        kept += int((now & later)[rich].sum())
        total += int(now[rich].sum())
        rich_frames += int(rich.sum())
        frames += len(rich)
    # Noise has only chance peaks (≈ 7 % of frames look "rich"; speech ≈ 60 %,
    # music ≈ 45–80 %): that is no evidence for speech or music.
    if total < 200 or rich_frames < 0.2 * frames:
        return None
    return kept / total


def audio_features(x: np.ndarray, sr: int, check_cancel=None, *, known_lra: float | None = None) -> dict[str, Any]:
    """Speech/music/rhythm/dynamics features on (excerpt) audio, float32 (N, 2).
    `check_cancel` (optional) is called between the feature groups. Pass `known_lra`
    to reuse the profile's measurement on these same excerpts (zero is valid)."""
    check = check_cancel or (lambda: None)
    mono = (x.mean(axis=1) if x.ndim > 1 else x).astype(np.float32)
    spec = _frame_spectra(mono, sr)
    check()
    feats: dict[str, Any] = {}
    if spec.shape[0] == 0:
        return {'valid': False}
    freqs = np.fft.rfftfreq(_NFFT, 1.0 / sr)
    power = spec.astype(np.float64) ** 2
    frame_e = power.sum(axis=1) + 1e-12
    live = frame_e > 1e-8 * frame_e.max()

    # Low-energy ratio (Scheirer & Slaney): frames under 50 % of their 1 s local mean.
    win = _FRAME_RATE
    if len(frame_e) >= win:
        local = np.convolve(frame_e, np.ones(win) / win, mode='same')
        feats['low_energy_ratio'] = round(float((frame_e[live] < 0.5 * local[live]).mean()), 3)
    else:
        feats['low_energy_ratio'] = 0.0

    # Pauses: frames ≥ 25 dB below the loud level (speech has many, music few).
    edb = 10 * np.log10(frame_e)
    loud = np.percentile(edb[live], 90) if live.any() else 0.0
    feats['pause_ratio'] = round(float((edb[live] < loud - 25.0).mean()) if live.any() else 0.0, 3)

    # Bass share (< 150 Hz) and spectral centroid.
    tot = power.sum()
    feats['bass_ratio'] = round(float(power[:, freqs < 150].sum() / (tot + 1e-20)), 3)
    feats['centroid_hz'] = round(float((power.sum(axis=0) * freqs).sum() / (tot + 1e-20)), 0)

    # Syllable-rate modulation of the speech band envelope (3–6 Hz share of 0.5–16 Hz).
    band = (freqs >= 300) & (freqs <= 3400)
    env = np.log(power[:, band].sum(axis=1) + 1e-10)
    env = env - env.mean()
    m = np.abs(np.fft.rfft(env * np.hanning(len(env)))) ** 2
    mf = np.fft.rfftfreq(len(env), 1.0 / _FRAME_RATE)
    total_mod = m[(mf >= 0.5) & (mf <= 16)].sum() + 1e-20
    feats['mod_4hz'] = round(float(m[(mf >= 3) & (mf <= 6)].sum() / total_mod), 3)

    # Onsets: half-wave rectified log-spectral flux → tempo.
    check()
    logspec = np.log1p(spec[:, freqs < 8000] * 10.0)
    flux = np.maximum(0.0, np.diff(logspec, axis=0)).sum(axis=1)
    strength, _ = _tempo(flux.astype(np.float64))
    feats['beat_strength'] = round(strength, 3)
    feats['bpm'] = _consensus_bpm(flux.astype(np.float64)) if strength >= 0.25 else None
    # Low-frequency pulse (kick drums): same, restricted to < 150 Hz.
    low_flux = np.maximum(0.0, np.diff(np.log1p(spec[:, freqs < 150] * 10.0), axis=0)).sum(axis=1)
    feats['kick_strength'] = round(_tempo(low_flux.astype(np.float64))[0], 3)
    check()
    tp = tonal_persistence(mono, sr)
    feats['tonal_persistence'] = round(tp, 3) if tp is not None else None

    # Dynamics of the excerpts.
    try:
        check()
        feats['lra'] = round(known_lra if known_lra is not None else lra(x, sr=sr, check_cancel=check_cancel), 2)
    except ValueError:  # non-48 kHz input
        feats['lra'] = None
    feats['valid'] = True
    return feats


# ---------------------------------------------------------------------------
# Metadata evidence
# ---------------------------------------------------------------------------
_KEYWORDS = {
    'dj_set': [
        r'\bdj[ -]?set\b',
        r'\bboiler ?room\b',
        r'\bessential mix\b',
        r'\bb2b\b',
        r'\blive set\b',
        r'\bdj mix\b',
        r'\bmixtape\b',
        r'\bmixed by\b',
        r'\bcontinuous mix\b',
        r'\bset\b',
        r'\bmix\b',
        r'\btomorrowland\b',
        r'\bcercle\b',
        r'\bultra\b',
        r'\bedc\b',
        r'\bawakenings\b',
        r'\bradio show\b',
        r'\bclub\b',
        r'\bfestival\b',
        r'\btechno\b',
        r'\bhouse\b',
        r'\btrance\b',
    ],
    'live_concert': [
        r'\bconcert\b',
        r'\borchestra\b',
        r'\bsymphon',
        r'\bphilharmoni',
        r'\bunplugged\b',
        r'\blive (at|in|from)\b',
        r'\bfull concert\b',
        r'\blive performance\b',
        r'\bopera\b',
        r'\brecital\b',
        r'\btour\b',
        r'\bin concert\b',
        r'\bquartet\b',
        r'\bensemble\b',
        r'\blive\b',
    ],
    'speech': [
        r'\bpodcast\b',
        r'\binterview\b',
        r'\bepisode\b',
        r'\bep\.? ?\d+',
        r'\btalk\b',
        r'\blecture\b',
        r'\bkeynote\b',
        r'\baudiobook\b',
        r'\bsermon\b',
        r'\bwebinar\b',
        r'\bted\b',
        r'\bdiscussion\b',
        r'\bdebate\b',
        r'\bnews\b',
        r'\bq ?& ?a\b',
        r'\bpanel\b',
        r'\bspeech\b',
        r'\bconference\b',
        r'\btutorial\b',
        r'\bexplained\b',
        r'\bhörbuch\b',
        r'\bvortrag\b',
    ],
    'song': [
        r'\bofficial (music )?video\b',
        r'\bofficial audio\b',
        r'\blyrics?\b',
        r'\blyric video\b',
        r'\bmusic video\b',
        r'\bremix\b',
        r'\bfeat\.?\b',
        r'\bft\.\b',
        r'\bradio edit\b',
        r'\bextended mix\b',
        r'\bsingle\b',
        r'\bvisualizer\b',
        r'\bvisualiser\b',
        r'\bcover\b',
    ],
}
_STRONG = {
    r'\bdj[ -]?set\b',
    r'\bboiler ?room\b',
    r'\bessential mix\b',
    r'\bpodcast\b',
    r'\binterview\b',
    r'\bconcert\b',
    r'\borchestra\b',
    r'\bsymphon',
    r'\bofficial (music )?video\b',
    r'\bofficial audio\b',
    r'\blecture\b',
    r'\baudiobook\b',
    r'\bfull concert\b',
    r'\bmixtape\b',
    r'\bdj mix\b',
}
_SPEECH_CATEGORIES = {
    'Education',
    'News & Politics',
    'People & Blogs',
    'Science & Technology',
    'Howto & Style',
    'Nonprofits & Activism',
}


def _keyword_hits(text: str) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {t: [] for t in _KEYWORDS}
    for t, pats in _KEYWORDS.items():
        for p in pats:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                hits[t].append((p, m.group(0).strip()))
    return hits


def _count_timestamps(text: str) -> int:
    return len(re.findall(r'(?m)^\s*(?:\d+[.)]\s*)?[\[(]?(?:\d{1,2}:)?\d{1,2}:\d{2}[\])]?\s+\S', text or ''))


def _human_minutes(seconds: float) -> str:
    m = int(round(seconds / 60.0))
    if m >= 60:
        return f'{m // 60} h {m % 60:02d} min'
    if seconds < 60:
        return f'{int(seconds)} s'
    return f'{m} min'


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
class _Evidence:
    def __init__(self):
        self.score = {t: 0.0 for t in _REAL_TYPES}
        self.why: dict[str, list[tuple[float, str]]] = {t: [] for t in _REAL_TYPES}

    def add(self, t: str, w: float, reason: str | None = None):
        self.score[t] += w
        if reason and w > 0:
            self.why[t].append((w, reason))


def classify(
    features: dict[str, Any] | None, duration_s: float, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    ev = _Evidence()
    dur = float(duration_s or 0.0)
    meta = metadata or {}

    # --- duration -----------------------------------------------------------
    long_txt = f'{_human_minutes(dur)} long'
    if dur >= 40 * 60:
        ev.add('dj_set', 1.2, long_txt)
        ev.add('live_concert', 1.0, long_txt)
        ev.add('speech', 0.8, long_txt)
        ev.add('song', -3.0)
    elif dur >= 20 * 60:
        ev.add('dj_set', 0.8, long_txt)
        ev.add('live_concert', 0.7, long_txt)
        ev.add('speech', 0.6, long_txt)
        ev.add('song', -2.0)
    elif dur >= 10 * 60:
        ev.add('dj_set', 0.1)
        ev.add('live_concert', 0.3)
        ev.add('speech', 0.3)
        ev.add('song', -0.6)
    elif dur >= 90:
        ev.add('song', 1.2, f'{_human_minutes(dur)} long — typical song length')
        ev.add('dj_set', -2.0)
        ev.add('live_concert', -0.4)
    elif dur > 0:
        ev.add('song', 0.3)
        ev.add('dj_set', -2.5)
        ev.add('live_concert', -0.8)

    # --- metadata -----------------------------------------------------------
    title = str(meta.get('raw_title') or meta.get('title') or '')
    tags = ' '.join(str(t) for t in (meta.get('tags') or []))
    desc = str(meta.get('description') or '')[:5000]
    cats = set(meta.get('categories') or [])
    title_hits = _keyword_hits(title)
    other_hits = _keyword_hits(tags + ' ' + desc[:1500])
    for t in _KEYWORDS:
        for pat, word in title_hits[t][:2]:
            w = 1.6 if pat in _STRONG else 0.6
            ev.add(t, w, f"title mentions '{word}'")
        for pat, word in other_hits[t][:2]:
            w = 0.6 if pat in _STRONG else 0.2
            ev.add(t, w, f"description/tags mention '{word}'" if w >= 0.5 else None)
    chapters = meta.get('chapters') or []
    n_ts = max(len(chapters), _count_timestamps(desc))
    if n_ts >= 6 and dur >= 20 * 60:
        ev.add('dj_set', 1.2, f'has a {n_ts}-entry tracklist / chapter list')
        ev.add('live_concert', 0.4)
    if 'Music' in cats:
        ev.add('song', 0.3)
        ev.add('dj_set', 0.3)
        ev.add('live_concert', 0.3)
        ev.add('speech', -0.6)
    if cats & _SPEECH_CATEGORIES:
        ev.add('speech', 0.8, f"YouTube category '{sorted(cats & _SPEECH_CATEGORIES)[0]}'")

    # --- audio --------------------------------------------------------------
    bpm = None
    audio_ambiguous = False
    have_audio = bool(features and features.get('valid'))
    if have_audio:
        f = features
        ler, pause = f.get('low_energy_ratio', 0.0), f.get('pause_ratio', 0.0)
        bass, mod4 = f.get('bass_ratio', 0.0), f.get('mod_4hz', 0.0)
        beat, kick = f.get('beat_strength', 0.0), f.get('kick_strength', 0.0)
        tonal = f.get('tonal_persistence')
        lra_v = f.get('lra') or 0.0
        # Speech vs music. The strongest cue is tonal persistence (sustained notes
        # vs gliding speech harmonics) — it survives background noise, where
        # "silence between words" does not. Missing pauses are NOT counted
        # against speech (noise fills them); a missing beat is not counted
        # against music (piano, ambient, classical).
        tonal_term = 0.0 if tonal is None else float(np.clip((0.27 - tonal) / 0.12, -1.5, 1.5)) * 1.5
        speech_ev = (
            float(np.clip((ler - 0.30) / 0.15, -1, 1)) * 0.8
            + float(np.clip((pause - 0.05) / 0.10, 0, 1)) * 0.6
            + float(np.clip((mod4 - 0.20) / 0.10, -1, 1)) * 0.3
            + (0.5 if beat < 0.15 else -0.8 if beat >= 0.3 else 0.0)
            + tonal_term
        )
        music_ev = (
            (1.2 * min(1.0, beat / 0.6) if beat >= 0.3 else 0.0)
            + (0.5 if bass >= 0.4 else 0.0)
            + (0.4 if kick >= 0.3 else 0.0)
        )
        d = speech_ev - music_ev
        if d >= 1.0:
            why = ['no sustained musical notes'] if tonal_term > 1.0 else []
            if pause >= 0.08:
                why.append('pauses between phrases')
            if beat < 0.15:
                why.append('no beat')
            ev.add('speech', 1.5 + 0.8 * min(d, 4.0), 'sounds like talking' + (f' ({", ".join(why)})' if why else ''))
            for t in ('dj_set', 'live_concert', 'song'):
                ev.add(t, -(1.0 + 0.5 * min(d, 4.0)))
        elif d <= -1.0:
            reason = 'sounds like music (sustained notes)' if tonal_term < -1.0 else 'sounds like music, not talking'
            for t in ('dj_set', 'live_concert', 'song'):
                ev.add(t, 0.8, reason)
            ev.add('speech', -(1.5 + 0.8 * min(-d, 4.0)))
        else:
            audio_ambiguous = True

        if f.get('bpm') and beat >= 0.3:
            bpm = f['bpm']
        if beat >= 0.3:
            ev.add('dj_set', 1.6 * min(1.0, beat / 0.6), f'steady {bpm:.0f} BPM beat' if bpm else 'steady beat')
            ev.add('song', 0.6 * min(1.0, beat / 0.6))
            ev.add('live_concert', -1.2 * min(1.0, beat / 0.6))
        elif beat < 0.15:
            ev.add('live_concert', 0.9, 'no steady beat')
            ev.add('dj_set', -1.2)
        if kick >= 0.3 and bass >= 0.18:
            ev.add('dj_set', 0.7, 'heavy, regular kick drum')
        if lra_v >= 6.5:
            ev.add('live_concert', 1.0 if lra_v >= 8.0 else 0.6, f'wide dynamics (loudness range {lra_v:.0f} LU)')
            ev.add('dj_set', -0.6)
        elif 0 < lra_v <= 5.0:
            ev.add('dj_set', 0.4)
            ev.add('live_concert', -0.6)

    # --- decision -------------------------------------------------------------
    temp = 1.0 if have_audio else 1.4  # metadata-only guesses are softer
    logits = np.array([ev.score[t] for t in _REAL_TYPES]) / temp
    probs = np.exp(logits - logits.max())
    probs /= probs.sum()
    order = np.argsort(probs)[::-1]
    best = _REAL_TYPES[int(order[0])]
    conf = float(probs[order[0]])
    margin = conf - float(probs[order[1]])
    cap = 0.95 if have_audio else 0.75
    conf = min(conf, cap)

    # Audio that is neither clearly speech nor clearly music only gets a type when
    # strong metadata (title keywords / category) backs it up.
    strong_meta = any(p in _STRONG for hits in title_hits.values() for p, _ in hits) or bool(cats & _SPEECH_CATEGORIES)
    unsure = (
        conf < 0.5
        or margin < 0.15
        or (not have_audio and not meta and dur <= 0)
        or (audio_ambiguous and not strong_meta)
    )
    reasons = [r for _, r in sorted(ev.why[best], key=lambda wr: -wr[0])]
    seen, uniq = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    out_type = 'unknown' if unsure else best
    if unsure:
        runner = _REAL_TYPES[int(order[1])]
        uniq = [f'could be {TYPES[best].lower()} or {TYPES[runner].lower()} — not sure, so playing it safe'] + uniq[:2]
    label = 'high' if conf >= 0.8 else 'medium' if conf >= 0.6 else 'low'
    return {
        'type': out_type,
        'label': TYPES[out_type],
        'confidence': round(conf, 2),
        'confidence_label': 'low' if unsure else label,
        'reasons': uniq[:4],
        'scores': {t: round(float(p), 3) for t, p in zip(_REAL_TYPES, probs)},
        'best_guess': best,
        'bpm': bpm,
        'source': 'audio+metadata' if have_audio and meta else 'audio' if have_audio else 'metadata',
    }
