"""Resource preflight: refuse jobs that would not fit *before* heavy work starts.

Background (prototype, Sept 2026): a 2 h set + AI separation inside a 4 GB
Docker container OOM-killed the whole VM. Since then every job estimates its
peak RAM and disk need up front and is refused with a plain explanation and
concrete alternatives instead of crashing the machine.

The memory model is calibrated on the float32 DSP chain (measured Oct 2026:
2.07 GB peak for 10 min, 3.72 GB for 30 min): ~3.8x one full-length stereo
float32 array (1.38 GB per hour at 48 kHz) + 1.3 GB fixed; AI stages are
windowed and add roughly 1 GB.
"""

import shutil
from pathlib import Path

from . import system
from .errors import AudioLabError, ResourceGuardError
from .settings import LIMITS
from .util import human_bytes, human_duration

BYTES_PER_STEREO_FRAME_F32 = 2 * 4
GB = 1024**3


def memory_report() -> dict[str, object]:
    """Memory awareness. Inside a container the cgroup limit wins; on a native
    host it is physical memory (preflight applies the safety fraction once)."""
    cg = system.container_memory()
    if cg:
        limit, current = cg
        return {
            'source': 'container',
            'limit_bytes': limit,
            'current_bytes': current,
            'available_bytes': max(0, limit - current),
        }
    total = system.physical_memory_bytes()
    if total:
        return {'source': 'host', 'limit_bytes': total, 'current_bytes': 0, 'available_bytes': int(total)}
    return {'source': 'unknown', 'limit_bytes': 0, 'current_bytes': 0, 'available_bytes': 0}


def _hours(seconds: float) -> str:
    """A limit in words: '8 hours', '1 hour', '90 minutes'."""
    if seconds >= 3600 and seconds % 3600 == 0:
        n = int(seconds // 3600)
        return f'{n} hour' + ('s' if n != 1 else '')
    return f'{round(seconds / 60)} minutes'


def disk_free_bytes(path: Path) -> int:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def estimate_peak_bytes(duration_s: float, task: str, quality: str = 'standard', sr: int = 48000) -> int:
    """Conservative peak-RAM estimate for one job."""
    stereo_array = max(1, int(duration_s * sr)) * BYTES_PER_STEREO_FRAME_F32
    # Factors measured on Apple Silicon (Oct 2026): 60 min Standard 7.2 GB, Light 2.5–4.5 GB,
    # 2 h Check 3.8 GB peak RSS.
    if task == 'enhance':
        if quality == 'light':  # volume only: gain + limiter passes
            return int(2.6 * stereo_array) + GB
        peak = int(4.3 * stereo_array) + int(1.3 * GB)
        if quality == 'best':
            peak += GB  # model weights + window activations
        return peak
    if task == 'separate':
        return int(1.2 * stereo_array) + int(1.3 * GB)  # windowed model + streamed stems
    if task == 'analyze':
        return int(1.4 * stereo_array) + GB // 2  # whole programme in memory for the profile
    return 256 * 1024 * 1024  # download / convert: subprocesses do the work


def estimate_disk_bytes(duration_s: float, task: str, quality: str = 'standard') -> int:
    # The working reference and AI stems are float32 WAV (≈1.4 GB per hour each).
    hours = max(0.0, duration_s) / 3600.0
    per_hour = {'download': 1.5, 'analyze': 2.0, 'enhance': 6.0, 'separate': 14.0}.get(task, 6.0)
    if task == 'enhance' and quality == 'best':
        per_hour += 7.0  # 4 float stems + their processed copies
    return int(hours * per_hour * GB + 0.25 * GB)


def preflight(
    duration_s: float | None,
    work_dir: Path,
    task: str,
    quality: str = 'standard',
    device: str | None = None,
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Raise ResourceGuardError (with hints) if the job would not fit; return the guard report.

    `duration_s` may be None when unknown (then only disk headroom is checked). An output folder that
    can't be written to (a drive that isn't connected) stops every task that writes there (not Check)."""
    if output_dir is not None and task != 'analyze' and not system.folder_writable(output_dir):
        raise AudioLabError(
            f"The output folder isn't available: {output_dir}.",
            hints=['Connect the drive it is on, or choose another folder in Settings → Files.'],
        )
    reasons, hints = [], []
    report: dict[str, object] = {'task': task, 'quality': quality, 'device': device}

    if duration_s is not None:
        if duration_s <= 0:
            raise ResourceGuardError(
                'Could not tell how long this audio is, so it was not processed.',
                hints=['Try a different file or link.'],
            )
        report['duration_s'] = round(duration_s, 1)
        if duration_s > LIMITS.max_duration_s:
            reasons.append(
                f'It is {human_duration(duration_s)} long; the limit is {human_duration(LIMITS.max_duration_s)}.'
            )
            hints.append(
                f'AudioLab is set to handle recordings of up to {_hours(LIMITS.max_duration_s)}: use a shorter '
                f'one, or cut it into parts with another app and add them one by one.'
            )

        uses_ai = task == 'separate' or (task == 'enhance' and quality == 'best')
        if (
            uses_ai
            and device == 'cpu'
            and LIMITS.ai_cpu_max_duration_s > 0
            and duration_s > LIMITS.ai_cpu_max_duration_s
            and not LIMITS.allow_unsafe_ai
        ):
            reasons.append(
                f'AI processing runs on the CPU here and would take many hours for '
                f'{human_duration(duration_s)} of audio (limit {human_duration(LIMITS.ai_cpu_max_duration_s)}).'
            )
            faster = 'a Mac with Apple silicon or a computer with an NVIDIA graphics card'
            hints.append(
                f'Split a shorter piece, or use {faster}.'
                if task == 'separate'  # Split has no other quality to fall back on
                else f'Use Standard quality (no AI) for long recordings, or use {faster}.'
            )

        mem = memory_report()
        peak = estimate_peak_bytes(duration_s, task, quality)
        report.update(
            estimated_peak_bytes=peak, memory_available_bytes=mem['available_bytes'], memory_source=mem['source']
        )
        if mem['source'] != 'unknown':
            budget = int(int(mem['available_bytes']) * LIMITS.memory_safety_fraction)
            if peak > budget:
                reasons.append(
                    f'It needs about {human_bytes(peak)} of memory, but only about '
                    f'{human_bytes(budget)} is safely available.'
                )
                hints.append(
                    'Close other apps, process a shorter piece, or (in Docker) give the container more memory.'
                )

    need = estimate_disk_bytes(duration_s or 600.0, task, quality) + int(LIMITS.min_free_disk_gb * GB)
    for label, path in (('working folder', work_dir), ('output folder', output_dir)):
        if path is None:
            continue
        free = disk_free_bytes(path)
        report[f'disk_free_{label.split()[0]}_bytes'] = free
        if free and need > free:
            reasons.append(f'The {label} needs about {human_bytes(need)} free, but only {human_bytes(free)} is left.')
            hints.append('Free up some disk space and try again.')

    if reasons:
        raise ResourceGuardError(
            'This job was stopped before it started: ' + ' '.join(reasons), hints=list(dict.fromkeys(hints))
        )
    return report
