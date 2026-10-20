"""Recipe model: what a recipe is and how its parameters are decided.

A recipe is a named starting point (club / live / music / voice). `resolve()`
turns recipe + measured profile + quality + user overrides into concrete
ChainParams and a list of decisions, each with a plain-language reason, so
both novices ("why does it sound like this?") and experts can audit "auto".

Defaults are preservation-first: only the validated `club` chain does the
heavy things (sub-harmonics, mono bass, glue) by default. Everything that
invents content (HF exciter) is opt-in.

Quality "light" is volume only: one fixed loudness per recipe (so a playlist
or folder plays evenly) plus true-peak safety — no EQ, compression, noise
reduction, bass or AI. The sound itself is not changed.
"""

import math
from dataclasses import asdict, dataclass
from typing import Any

SAMPLE_RATES = (44100, 48000, 96000)


@dataclass
class ChainParams:
    highpass_hz: float = 25.0
    mud_cut_db: float = 0.0  # peak cut @ 320 Hz (<= 0)
    bass_db: float = 0.0  # low shelf @ 85 Hz (>= 0)
    presence_db: float = 0.0  # peak @ 3.4 kHz
    air_db: float = 0.0  # high shelf @ 11 kHz
    sub_harmonics: float = 0.0  # wet amount of sub-bass harmonics, 0 = off
    mono_bass_hz: float | None = None
    hf_restore: bool = False  # exciter above a lossy cutoff
    denoise: str = 'off'  # off | light | strong
    compressor: str = 'off'  # off | glue | leveler
    target_lufs: float = -14.0
    tp_ceiling_db: float = -1.0
    ai_stage: str = 'none'  # none | stem_eq | voice_isolate
    ai_model: str = 'htdemucs'
    sample_rate: int = 48000  # delivery rate (the master itself is 48 kHz)
    volume_only: bool = False  # quality "light": gain + peak safety only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Recipe:
    key: str
    name: str
    short: str
    description: str

    def public(self) -> dict[str, str]:
        return {'key': self.key, 'name': self.name, 'short': self.short, 'description': self.description}


RECIPES: dict[str, Recipe] = {
    'club': Recipe(
        'club',
        'Club / DJ set',
        'Punchy and loud, tight bass — made for big speakers',
        'The original ClubReady chain for DJ sets and electronic mixes: loudness matched to the '
        "set's energy, cleaned-up low-mids, extra bass weight and sub-bass harmonics, bass below "
        '90 Hz folded to mono for club systems, gentle glue compression and a true-peak limiter.',
    ),
    'live': Recipe(
        'live',
        'Live concert',
        'Keeps the dynamics and the room, cleans up the rest',
        'For concerts, orchestras and live recordings: gentle EQ only, no extra compression, '
        'quiet passages stay quiet, loudness set conservatively, peaks kept safe.',
    ),
    'music': Recipe(
        'music',
        'Song / music',
        'Gentle polish that keeps the original character',
        'For songs and tracks: light tonal clean-up, loudness kept where it is unless it is far '
        'too quiet or too loud, peaks kept safe. Nothing is added that was not there.',
    ),
    'voice': Recipe(
        'voice',
        'Podcast / voice',
        'Clear, even speech at podcast loudness',
        'For podcasts, talks and interviews: rumble removed, voice clarity lifted, loud and quiet '
        'moments evened out, -16 LUFS podcast loudness. Noise reduction is available when you choose it.',
    ),
}

_TYPE_TO_RECIPE = {'dj_set': 'club', 'live_concert': 'live', 'song': 'music', 'speech': 'voice', 'unknown': 'music'}

_AI_STAGE = {'club': 'stem_eq', 'music': 'stem_eq', 'voice': 'voice_isolate', 'live': 'none'}

_TP_CEILING = {'club': -1.25, 'live': -1.0, 'music': -1.0, 'voice': -1.5}

# Light quality: a fixed level per recipe (consistency across many files, unlike
# Standard, which keeps a song's own loudness), and why that level.
LIGHT_TARGETS = {
    'music': (
        -14.0,
        'the level streaming services play music at (Spotify, YouTube, Tidal) — loud masters are simply turned down',
    ),
    'club': (
        -11.0,
        'club level: dance music keeps about 9–10 dB between its average and its peaks, '
        'so it gets this loud with the peaks still under the ceiling',
    ),
    'live': (
        -16.0,
        'concerts swing between quiet and loud — here the loud parts already match other music '
        'and the peaks keep their room',
    ),
    'voice': (-16.0, 'the usual podcast and spoken-word level (Apple Podcasts and most podcast apps)'),
}
# Light never limits the highest peak by more than this; if reaching the target
# would need more, the file stays a little quieter instead (see chain.render).
LIGHT_MAX_LIMIT_DB = 3.0
_SOUND_OVERRIDES = {
    'bass_db': lambda v: (_num(v) or 0) > 0,
    'glue': lambda v: v == 'on',
    'hf_restore': lambda v: v == 'on',
    'denoise': lambda v: v in ('light', 'strong'),
    'mono_bass': lambda v: v == 'on',
}


def recipe_for_type(content_type: str) -> str:
    return _TYPE_TO_RECIPE.get(content_type or 'unknown', 'music')


def list_recipes() -> list[dict[str, str]]:
    return [r.public() for r in RECIPES.values()]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _db(v: float, unit: str = 'dB') -> str:
    return f'{v:+.1f} {unit}' if unit == 'dB' else f'{v:.1f} {unit}'


class _Decisions:
    def __init__(self):
        self._items: dict[str, dict[str, Any]] = {}

    def set(self, key: str, label: str, value: str, source: str, why: str):
        self._items[key] = {'key': key, 'label': label, 'value': value, 'source': source, 'why': why}

    def list(self) -> list[dict[str, Any]]:
        return list(self._items.values())


def _num(v: Any) -> float | None:
    if v is None or (isinstance(v, str) and v.strip().lower() in ('', 'auto')):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _choice(v: Any, allowed: tuple[str, ...]) -> str | None:
    if isinstance(v, bool):
        v = 'on' if v else 'off'
    s = str(v).strip().lower() if v is not None else ''
    return s if s in allowed else None


def resolve(
    recipe_key: str, profile: dict[str, Any], quality: str = 'standard', advanced: dict[str, Any] | None = None
) -> tuple[ChainParams, list[dict[str, Any]]]:
    """Decide the chain parameters. Returns (params, decisions)."""
    key = recipe_key if recipe_key in RECIPES else 'music'
    prof = profile or {}
    lufs = float(prof.get('integrated_lufs', -14.0))
    lra_v = float(prof.get('lra', 5.0))
    bw = prof.get('bandwidth') or {}
    lossy = bool(bw.get('is_lossy_cutoff'))
    has_mud = bool(prof.get('has_mud_excess', False))
    stereo = prof.get('stereo') or {}
    antiphase = bool(stereo.get('antiphase_bass'))
    style = prof.get('dynamics_style') or {}
    p = ChainParams(tp_ceiling_db=_TP_CEILING[key])
    d = _Decisions()
    adv = advanced or {}
    if quality == 'light':
        return _resolve_light(key, lufs, adv, p, d)

    # --- recipe defaults + adaptive choices --------------------------------
    if key == 'club':
        p.highpass_hz = 25.0
        p.target_lufs = float(style.get('recommended_target_lufs', -12.5))
        d.set(
            'target_lufs',
            'Loudness target',
            _db(p.target_lufs, 'LUFS'),
            'auto',
            f"matched to this set's energy — {style.get('name', 'club dynamics').lower()} "
            f'(measured {lufs:.1f} LUFS, loudness range {lra_v:.1f} LU)',
        )
        p.bass_db = float(style.get('recommended_bass_punch_db', 1.5))
        d.set(
            'bass_db',
            'Bass punch',
            _db(p.bass_db),
            'auto',
            'less extra bass for wide-dynamics recordings'
            if p.bass_db < 1.0
            else 'club systems benefit from a little extra low-end weight',
        )
        glue = bool(style.get('recommended_glue_comp', True)) and lra_v >= 3.0
        p.compressor = 'glue' if glue else 'off'
        d.set(
            'compressor',
            'Glue compression',
            'on' if glue else 'off',
            'auto',
            'moderate dynamics: a gentle glue holds the mix together'
            if glue
            else f'skipped: the mix is already dense (LRA {lra_v:.1f} LU)'
            if lra_v < 3.2
            else 'skipped to keep the wide dynamics',
        )
        p.sub_harmonics = 0.25 if p.bass_db > 0 else 0.0
        d.set(
            'sub_harmonics',
            'Sub-bass harmonics',
            'on' if p.sub_harmonics else 'off',
            'recipe',
            'makes the sub-bass audible on smaller speakers too' if p.sub_harmonics else 'no bass boost requested',
        )
        p.mono_bass_hz = 90.0
        p.presence_db, p.air_db = (0.0, 0.0) if lossy else (0.75, 1.0)
    elif key == 'live':
        p.highpass_hz = 20.0
        p.target_lufs = round(max(-17.0, min(-14.2, lufs + 0.5)), 1)
        d.set(
            'target_lufs',
            'Loudness target',
            _db(p.target_lufs, 'LUFS'),
            'auto',
            f'stays close to the original ({lufs:.1f} LUFS) so quiet passages keep their quiet',
        )
        p.bass_db = 0.5
        p.presence_db, p.air_db = 0.5, (0.0 if lossy else 0.75)
        p.compressor = 'off'
        d.set('compressor', 'Compression', 'off', 'recipe', 'concert dynamics are part of the music')
    elif key == 'music':
        p.highpass_hz = 25.0
        target = min(-8.0, max(-16.0, lufs))
        p.target_lufs = round(target, 1)
        if abs(target - lufs) < 0.05:
            why = f'kept as it is ({lufs:.1f} LUFS is already in the normal range for music)'
        elif lufs < -16.0:
            why = f'raised from {lufs:.1f} LUFS — it was much quieter than typical music'
        else:
            why = f'lowered from {lufs:.1f} LUFS — it was squashed louder than useful'
        d.set('target_lufs', 'Loudness target', _db(p.target_lufs, 'LUFS'), 'auto', why)
        p.presence_db, p.air_db = 0.5, (0.0 if lossy else 0.75)
        p.compressor = 'off'
    elif key == 'voice':
        p.highpass_hz = 60.0
        d.set('highpass_hz', 'Rumble filter', '60 Hz', 'recipe', 'removes handling noise and hum below the voice')
        p.target_lufs = -16.0
        d.set(
            'target_lufs', 'Loudness target', '-16.0 LUFS', 'recipe', 'standard loudness for podcasts and spoken word'
        )
        p.presence_db, p.air_db = 1.5, (0.0 if lossy else 0.5)
        d.set(
            'presence_db',
            'Voice clarity',
            _db(p.presence_db),
            'recipe',
            'a lift around 3.4 kHz makes speech easier to understand',
        )
        p.compressor = 'leveler'
        d.set('compressor', 'Leveler', 'on', 'recipe', 'evens out loud and quiet speakers / moments')
        d.set(
            'denoise',
            'Noise reduction',
            p.denoise,
            'recipe',
            'off by default to preserve the recording — choose Light or Strong if you hear steady background noise',
        )

    if has_mud:
        p.mud_cut_db = -1.25 if key == 'club' else -1.0
        d.set(
            'mud_cut_db',
            'Low-mid clean-up',
            _db(p.mud_cut_db),
            'auto',
            f'low-mids sit {prof.get("mud_excess_db", 0):+.1f} dB above the overall balance',
        )
    if lossy:
        d.set(
            'air_db',
            'High-end lift',
            'off',
            'auto',
            f'the top end stops at {bw.get("cutoff_hz", 0) / 1000:.1f} kHz — boosting it would only lift noise',
        )
    if p.mono_bass_hz and antiphase:
        p.mono_bass_hz = None
        d.set(
            'mono_bass',
            'Mono bass',
            'off',
            'auto',
            'left and right bass are out of phase — folding them to mono would cancel the bass',
        )
    elif p.mono_bass_hz:
        d.set(
            'mono_bass',
            'Mono bass',
            f'below {p.mono_bass_hz:.0f} Hz',
            'recipe',
            'kick and bass hit harder and centred on club / car systems',
        )
    if not d._items.get('highpass_hz'):
        d.set(
            'highpass_hz',
            'Rumble filter',
            f'{p.highpass_hz:.0f} Hz',
            'recipe',
            'removes inaudible rumble that only eats headroom',
        )

    # --- quality / AI stage --------------------------------------------------
    stage = _AI_STAGE[key] if quality == 'best' else 'none'
    if quality == 'best':
        if stage == 'none':
            d.set(
                'ai_stage',
                'AI stage',
                'none',
                'recipe',
                'live recordings stay un-separated — splitting them would smear the room sound',
            )
        else:
            from ..ai import ai_status

            status = ai_status()
            if status.get('available'):
                d.set(
                    'ai_stage',
                    'AI stage',
                    'stem-aware EQ' if stage == 'stem_eq' else 'voice isolation',
                    'user',
                    'splits the mix into drums, bass, vocals and the rest and treats each one on its own'
                    if stage == 'stem_eq'
                    else 'keeps only the voice and removes music and background sounds',
                )
            else:
                stage = 'none'
                d.set(
                    'ai_stage',
                    'AI stage',
                    'none (fallback)',
                    'auto',
                    f'Best quality needs the AI pack ({status.get("reason") or "not available"}) — used Standard',
                )
    p.ai_stage = stage

    # --- expert overrides ----------------------------------------------------
    _level_overrides(adv, p, d)
    v = _num(adv.get('bass_db'))
    if v is not None:
        p.bass_db = round(max(0.0, min(6.0, v)), 2)
        if key == 'club':
            p.sub_harmonics = 0.25 if p.bass_db > 0 else 0.0
        d.set('bass_db', 'Bass punch', _db(p.bass_db), 'user', 'set by you')
    c = _choice(adv.get('glue'), ('on', 'off'))
    if c:
        p.compressor = ('leveler' if key == 'voice' else 'glue') if c == 'on' else 'off'
        d.set('compressor', 'Compression', c, 'user', 'set by you')
    c = _choice(adv.get('hf_restore'), ('on', 'off'))
    if c == 'on':
        if lossy:
            p.hf_restore = True
            d.set(
                'hf_restore',
                'Restore highs',
                'on',
                'user',
                f'synthesises plausible "air" above {bw.get("cutoff_hz", 0) / 1000:.1f} kHz (an exciter — '
                'it does not recover the original)',
            )
        else:
            d.set('hf_restore', 'Restore highs', 'off', 'auto', 'no lossy cutoff found — nothing to restore')
    c = _choice(adv.get('denoise'), ('off', 'light', 'strong'))
    if c:
        p.denoise = c
        d.set('denoise', 'Noise reduction', c, 'user', 'set by you')
    c = _choice(adv.get('mono_bass'), ('on', 'off'))
    if c:
        p.mono_bass_hz = 90.0 if c == 'on' else None
        d.set(
            'mono_bass',
            'Mono bass',
            'below 90 Hz' if c == 'on' else 'off',
            'user',
            'set by you'
            + (' — note: the bass is partly out of phase, mono may thin it' if c == 'on' and antiphase else ''),
        )
    model = adv.get('model')
    if isinstance(model, str) and model in ('htdemucs', 'htdemucs_ft'):
        p.ai_model = model

    if 'tp_ceiling_db' not in d._items:
        d.set(
            'tp_ceiling_db',
            'Peak ceiling',
            _db(p.tp_ceiling_db, 'dBTP'),
            'recipe',
            'keeps peaks safe on every speaker and after MP3/AAC conversion',
        )
    return p, d.list()


def _level_overrides(adv: dict[str, Any], p: ChainParams, d: _Decisions) -> None:
    """Expert overrides every quality honours: loudness, peak ceiling, delivery rate."""
    v = _num(adv.get('target_lufs'))
    if v is not None:
        p.target_lufs = round(max(-30.0, min(-5.0, v)), 1)
        d.set(
            'target_lufs',
            'Loudness target',
            _db(p.target_lufs, 'LUFS'),
            'user',
            'set by you' + (' (clamped to -30…-5 LUFS)' if p.target_lufs != round(v, 1) else ''),
        )
    v = _num(adv.get('tp_ceiling_db'))
    if v is not None:
        p.tp_ceiling_db = round(max(-3.0, min(-0.1, v)), 2)
        d.set('tp_ceiling_db', 'Peak ceiling', _db(p.tp_ceiling_db, 'dBTP'), 'user', 'set by you')
    v = _num(adv.get('sample_rate'))
    if v is not None and int(v) in SAMPLE_RATES:
        p.sample_rate = int(v)
        d.set(
            'sample_rate',
            'Sample rate',
            f'{p.sample_rate / 1000:g} kHz',
            'user',
            'set by you' + (' (higher rates add no detail that was not there)' if p.sample_rate > 48000 else ''),
        )


def _resolve_light(
    key: str, lufs: float, adv: dict[str, Any], p: ChainParams, d: _Decisions
) -> tuple[ChainParams, list[dict[str, Any]]]:
    """Quality "light": volume only. Every tone/dynamics stage stays off."""
    p.volume_only = True
    p.highpass_hz = 0.0
    p.target_lufs, why = LIGHT_TARGETS[key]
    d.set(
        'mode',
        'Mode',
        'Volume only',
        'user',
        'Light quality: volume only, plus a safety limiter on the few peaks that would go over the ceiling '
        '— no EQ, compression, noise reduction, bass or AI processing',
    )
    d.set(
        'target_lufs',
        'Loudness target',
        _db(p.target_lufs, 'LUFS'),
        'recipe',
        f'the same level for every {RECIPES[key].name.lower()} file, so a playlist or folder plays evenly '
        f'(this one measured {lufs:.1f} LUFS) — {why}',
    )
    d.set(
        'tp_ceiling_db',
        'Peak ceiling',
        _db(p.tp_ceiling_db, 'dBTP'),
        'recipe',
        f'a safety limiter only touches the few peaks that would go over it, by at most '
        f'{LIGHT_MAX_LIMIT_DB:.0f} dB; if more would be needed, the file stays a little quieter '
        'instead of changing its sound',
    )
    _level_overrides(adv, p, d)
    ignored = [k for k, active in _SOUND_OVERRIDES.items() if active(adv.get(k))]
    if ignored:
        d.set(
            'ignored',
            'Other settings',
            'not used',
            'auto',
            'Light changes the volume only — bass, compression, noise reduction, mono bass and '
            'restore highs need Standard quality',
        )
    return p, d.list()
