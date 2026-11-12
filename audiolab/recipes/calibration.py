"""Compact whole-recording state that a bounded audition can reuse."""

from dataclasses import dataclass
from typing import Any

from .base import ChainParams

CALIBRATION_VERSION = 1
_FINAL_LEVEL_FIELDS = {'target_lufs', 'tp_ceiling_db', 'sample_rate'}
_POST_LEVELER_FIELDS = _FINAL_LEVEL_FIELDS | {'deess_db', 'deess_mix'}
_LOWMID_INPUT_FIELDS = ('highpass_hz', 'denoise', 'adaptive_lowmid_db')


@dataclass(frozen=True)
class AuditionCalibration:
    """Inputs available to a contextual render, with None meaning unavailable."""

    pre_limiter_gain_db: float | None = None
    post_limiter_gain_db: float | None = None
    volume_dc: tuple[float, float] | None = None
    volume_peak_dbtp: float | None = None  # complete pre-gain peak, after DC removal
    volume_programme_lufs: float | None = None
    leveler_threshold_dbfs: float | None = None
    lowmid: dict[str, float] | None = None
    deess: dict[str, float] | None = None


def retained(value: Any, sr: int = 48000) -> dict[str, Any] | None:
    """Validate versioned state before keeping it in history or using it as gain."""
    if not isinstance(value, dict) or value.get('version') != CALIBRATION_VERSION or value.get('sample_rate_hz') != sr:
        return None

    def bounded(number: Any, lo: float, hi: float) -> bool:
        return type(number) in (float, int) and lo <= number <= hi

    if not bounded(value.get('pre_limiter_gain_db'), -900, 300):
        return None
    out = {
        'version': CALIBRATION_VERSION,
        'sample_rate_hz': sr,
        'pre_limiter_gain_db': value['pre_limiter_gain_db'],
    }
    for key, lo, hi in (
        ('post_limiter_gain_db', -900, 0),
        ('leveler_threshold_dbfs', -80, 900),
    ):
        if bounded(value.get(key), lo, hi):
            out[key] = value[key]
    volume = value.get('volume')
    if isinstance(volume, dict):
        dc = volume.get('dc')
        peak = volume.get('peak_dbtp')
        lufs = volume.get('programme_lufs')
        if (
            isinstance(dc, list)
            and len(dc) == 2
            and all(bounded(v, -3.4e38, 3.4e38) for v in dc)
            and bounded(peak, -900, 900)
            and bounded(lufs, -300, 900)
        ):
            out['volume'] = {'dc': dc, 'peak_dbtp': peak, 'programme_lufs': lufs}
    for key, second, lo, hi in (('lowmid', 'budget_scale', 0, 1), ('deess', 'threshold_db', -240, 240)):
        stage = value.get(key)
        if (
            isinstance(stage, dict)
            and bounded(stage.get('gate_dbfs'), -900, 900)
            and bounded(stage.get(second), lo, hi)
        ):
            out[key] = {'gate_dbfs': stage['gate_dbfs'], second: stage[second]}
    return out


def for_pair(saved: ChainParams, draft: ChainParams, stored: Any, sr: int):
    """Choose one calibration basis for both sides and report what it assumes."""
    data = retained(stored, sr)
    if data is None or (saved.volume_only and not data.get('volume')):
        return AuditionCalibration(), AuditionCalibration(), {'gain': 'section', 'retained_detectors': []}
    changed = {key for key, value in saved.to_dict().items() if value != getattr(draft, key)}
    upstream_changed = bool(changed - _FINAL_LEVEL_FIELDS)
    detectors = {
        'leveler_threshold_dbfs': data.get('leveler_threshold_dbfs') if changed <= _POST_LEVELER_FIELDS else None,
        'deess': data.get('deess') if not upstream_changed else None,
        'lowmid': data.get('lowmid') if changed.isdisjoint(_LOWMID_INPUT_FIELDS) else None,
    }
    volume = data.get('volume', {})
    gain = data['pre_limiter_gain_db']
    delta = draft.target_lufs - saved.target_lufs
    correction = data.get('post_limiter_gain_db')
    keep_correction = changed <= {'sample_rate'}
    shared = {
        **detectors,
        'volume_dc': tuple(volume['dc']) if volume else None,
        'volume_peak_dbtp': volume.get('peak_dbtp'),
        'volume_programme_lufs': volume.get('programme_lufs'),
        # Compare both sides with their own local safety check if the limiter input or ceiling changed.
        'post_limiter_gain_db': correction if keep_correction else None,
    }
    basis = 'shared_retained_estimate' if upstream_changed else 'retained_target_delta' if delta else 'retained'
    report = {'gain': basis, 'retained_detectors': [key for key, value in detectors.items() if value is not None]}
    if correction and correction < 0 and not keep_correction:
        # This records the omitted saved correction; the edit's own whole-master correction is unknown
        # until Apply, so this value does not predict either side's error against its full render.
        report['omitted_post_limiter_gain_db'] = correction
    return (
        AuditionCalibration(pre_limiter_gain_db=gain, **shared),
        AuditionCalibration(pre_limiter_gain_db=gain + delta, **shared),
        report,
    )
