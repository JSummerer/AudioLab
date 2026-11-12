"""The small, durable subset of an Enhance report saved in the Library.

Keep only defined fields: job options, analysis, and renderer reports can also
contain source metadata, paths, and tool errors that do not belong here.
"""

from typing import Any

from .media.export import FORMATS
from .recipes.base import eq_bands, section_eq
from .recipes.calibration import retained

_RECIPE_KEYS = ('club', 'live', 'music', 'voice')
_LEVELS = 'target_lufs tp_ceiling_db bass_db sample_rate'
_METRICS = 'lufs true_peak_dbtp lra'
_DYNAMICS = 'max_gr_db active_pct'


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any, limit: int) -> list:
    return value[:limit] if isinstance(value, list) else []


def _number(value: Any) -> bool:
    # Reject bools and non-finite values as well as values far beyond audio measurements.
    return type(value) in (int, float) and -1e9 <= value <= 1e9


def _text(value: Any, limit: int = 360) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= limit or not value.isprintable():
        return None
    # Report prose may contain " / " or name these two formats. A path or URL is not a reason.
    if '/' in value.replace(' / ', '').replace('MP3/AAC', '') or '\\' in value:
        return None
    return value.strip() or None


def _fields(
    value: Any, *, numbers: str = '', flags: str = '', text: str = '', choices: dict | None = None
) -> dict[str, Any]:
    obj = _mapping(value)
    out = {k: obj[k] for k in numbers.split() if _number(obj.get(k))}
    out.update({k: obj[k] for k in flags.split() if isinstance(obj.get(k), bool)})
    out.update({k: s for k in text.split() if (s := _text(obj.get(k)))})
    out.update(
        {k: obj[k] for k, allowed in (choices or {}).items() if isinstance(obj.get(k), str) and obj[k] in allowed}
    )
    return out


def _decisions(value: Any) -> list[dict[str, str]]:
    out = []
    for item in _list(value, 24):
        decision = _fields(item, text='key label value why', choices={'source': ('auto', 'recipe', 'user')})
        if all(k in decision for k in ('key', 'label', 'value', 'source')):
            if decision['key'] == 'ai_stage' and decision['value'] == 'none (fallback)':
                # The live explanation may name a local runtime error; keep the outcome instead.
                decision['why'] = 'The AI pack was unavailable, so Adaptive processing was used.'
            out.append(decision)
    return out


def _applied(value: Any) -> dict[str, Any]:
    report = _mapping(value)
    applied = _fields(report, numbers='loudness_gain_db')
    steps = [s for step in _list(report.get('steps'), 24) if (s := _text(step))]
    if steps:
        applied['steps'] = steps
    stages = {
        'compressor': _fields(
            report.get('compressor'),
            numbers=f'{_DYNAMICS} thresh_dbfs',
            flags='engaged',
            text='skip_reason',
            choices={'mode': ('glue', 'leveler')},
        ),
        'deess': _fields(
            report.get('deess'),
            numbers=f'{_DYNAMICS} band_change_db threshold_db budget_db',
            flags='engaged',
            text='skip_reason',
        ),
        'limiter': _fields(
            report.get('limiter'),
            numbers=f'{_DYNAMICS} tp_before tp_after corrective_gain_db',
            flags='engaged ceiling_met',
            text='skip_reason',
        ),
        'ai': _fields(
            report.get('ai'),
            numbers='residual_db',
            flags='fallback gate_passed',
            choices={'stage': ('stem_eq', 'voice_isolate'), 'model': ('htdemucs', 'htdemucs_ft')},
        ),
        'adaptive_lowmid': _adaptive(report.get('adaptive_lowmid')),
    }
    for key in ('compressor', 'deess', 'limiter', 'adaptive_lowmid'):
        if timeline := _timeline(_mapping(report.get(key)).get('timeline')):
            stages[key]['timeline'] = timeline
    applied.update({k: stage for k, stage in stages.items() if stage})
    return applied


def _timeline(value: Any) -> dict | None:
    """Bounded, path-free interval highlights, not a full-rate gain envelope."""
    raw = _mapping(value)
    duration = raw.get('duration_s')
    if not _number(duration) or duration <= 0 or not isinstance(raw.get('bins'), list):
        return None
    bins, previous_end = [], 0.0
    for item in _list(raw['bins'], 120):
        item = _fields(item, numbers='start_s end_s peak_s max_gr_db')
        if len(item) != 4:
            continue
        start, end, peak, reduction = (item[k] for k in ('start_s', 'end_s', 'peak_s', 'max_gr_db'))
        if previous_end <= start <= peak < end <= duration and 0.1 <= reduction <= 180:
            bins.append(item)
            previous_end = end
    return {'duration_s': duration, 'bins': bins}


def preview_bandwidth(value: Any) -> dict | None:
    """Keep only the source-bandwidth facts needed to repeat an HF treatment in a preview."""
    raw = _mapping(value)
    lossy = raw.get('is_lossy_cutoff')
    if not isinstance(lossy, bool):
        return None
    out = {'is_lossy_cutoff': lossy}
    cutoff = raw.get('cutoff_hz')
    if _number(cutoff) and 0 < cutoff <= 24000:
        out['cutoff_hz'] = cutoff
    return out


def _adaptive(value: Any) -> dict[str, Any]:
    stage = _mapping(value)
    out = _fields(
        stage,
        numbers=(
            f'{_DYNAMICS} band_change_db reference_window_s margin_db cap_db analysis_hop_s '
            'windows_total candidate_pct sustained_pct'
        ),
        flags='engaged',
        text='skip_reason',
    )
    band = _list(stage.get('band_hz'), 3)
    if len(band) == 2 and all(_number(v) for v in band):
        out['band_hz'] = band
    events = [_fields(event, numbers='start_s end_s max_gr_db') for event in _list(stage.get('events'), 12)]
    if spans := [e for e in events if len(e) == 3 and 0 <= e['start_s'] < e['end_s']]:
        out['events'] = spans
    return out


def _params(value: Any) -> dict[str, Any]:
    raw = _mapping(value)
    params = _fields(
        raw,
        numbers=(
            f'{_LEVELS} highpass_hz mud_cut_db presence_db air_db sub_harmonics mono_bass_hz deess_db '
            'adaptive_lowmid_db'
        ),
        flags='hf_restore deess_mix volume_only',
        choices={
            'denoise': ('off', 'light', 'strong'),
            'compressor': ('off', 'glue', 'leveler'),
            'ai_stage': ('none', 'stem_eq', 'voice_isolate'),
            'ai_model': ('htdemucs', 'htdemucs_ft'),
        },
    )
    if 'mono_bass_hz' in raw and raw['mono_bass_hz'] is None:
        params['mono_bass_hz'] = None  # explicitly off, rather than missing from an older report
    if eq := eq_bands(raw.get('eq_bands')):
        params['eq_bands'] = eq
    if sections := section_eq(raw.get('section_eq')):
        params['section_eq'] = sections
    return params


def report_details(report: dict, *, keep_calibration: bool = False) -> dict[str, Any]:
    """Expose only the resolved settings and applied audio steps of a renderer report."""
    details = {'params': _params(report.get('params')), 'applied': _applied(report)}
    if keep_calibration and (calibration := retained(report.get('calibration'))):
        details['calibration'] = calibration
    return details


def snapshot(options: dict, result: dict) -> dict[str, Any] | None:
    """Project a finished Enhance job onto the persisted and public processing fields."""
    report = _mapping(result.get('processing'))
    if not report:
        return None  # jobs from before processing details were recorded
    requested = _fields(
        options,
        flags='split_tracks embed_cover save_cover',
        choices={'recipe': ('auto', *_RECIPE_KEYS), 'quality': ('light', 'standard', 'best')},
    )
    formats = [f for f in _list(options.get('formats'), 24) if isinstance(f, str) and f in FORMATS]
    if formats:
        requested['formats'] = formats
    advanced = _fields(
        options.get('advanced'),
        numbers=_LEVELS,
        choices={
            'glue': ('on', 'off'),
            'hf_restore': ('on', 'off'),
            'mono_bass': ('on', 'off'),
            'denoise': ('off', 'light', 'strong'),
            'deess': ('auto', 'off', 'strong'),
            'model': ('htdemucs', 'htdemucs_ft'),
            'adaptive_lowmid': ('auto', 'off', 'strong'),
            'harmonics': ('on', 'off'),
        },
    )
    eq = eq_bands(_mapping(options.get('advanced')).get('eq'))
    if eq is not None:
        advanced['eq'] = eq  # [] stays: it clears a saved EQ when a Library version is applied again
    sections = section_eq(_mapping(options.get('advanced')).get('section_eq'))
    if sections is not None:
        advanced['section_eq'] = sections
    metrics = _mapping(result.get('metrics'))
    bandwidth = preview_bandwidth(_mapping(result.get('analysis')).get('bandwidth'))
    auto = _fields(report, choices={'auto_compressor': ('off', 'glue', 'leveler')})
    calibration = retained(report.get('calibration'))
    return {
        'requested': {**requested, 'advanced': advanced},
        'resolved': {
            'recipe': _fields(result.get('recipe'), text='name', choices={'key': _RECIPE_KEYS}),
            'params': _params(report.get('params')),
            'decisions': _decisions(result.get('decisions')),
            **({'bandwidth': bandwidth} if bandwidth is not None else {}),
            **auto,
            **({'calibration': calibration} if calibration is not None else {}),
        },
        'applied': _applied(report.get('applied')),
        'metrics': {k: _fields(metrics.get(k), numbers=_METRICS) for k in ('before', 'after')},
    }


def verification(value: Any) -> dict[str, Any] | None:
    """File checks describe the decoded export, not the master or a later copy."""
    fields = _fields(
        value,
        numbers='integrated_lufs true_peak_dbtp ceiling_dbtp tolerance_db',
        flags='ceiling_met',
        choices={'status': ('verified', 'warning', 'unverified')},
    )
    if 'status' not in fields:
        return None
    for key in ('integrated_lufs', 'true_peak_dbtp', 'ceiling_met'):
        fields.setdefault(key, None)  # unverified or unavailable
    return fields
