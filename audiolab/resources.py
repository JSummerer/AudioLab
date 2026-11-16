"""Resource preflight: refuse jobs that would not fit *before* heavy work starts.

Background (prototype, Sept 2026): a 2 h set + AI separation inside a 4 GB
Docker container OOM-killed the whole VM. Since then every job estimates its
peak RAM and disk need up front and is refused with a plain explanation and
concrete alternatives instead of crashing the machine.

The conservative memory model is calibrated on the earlier in-memory float32 DSP chain (Oct 2026:
2.07 GB peak for 10 min, 3.72 GB for 30 min): ~3.8x one full-length stereo
float32 array (1.38 GB per hour at 48 kHz) + 1.3 GB fixed; AI stages are
windowed and add roughly 1 GB. That model still applies up to DISK_BACKED_AFTER_SECONDS;
beyond it, Standard and Light Enhance are sized by their disk-backed render (`estimate_peak_bytes`).
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


# Above DISK_BACKED_AFTER_SECONDS a Standard or Light render keeps its working arrays in memory-mapped
# scratch files (dsp.scratch). Measured 16 Nov 2026 on a 48 GB Apple silicon Mac, default Enhance of
# generated 44.1 kHz music: 10 min (in memory) 1.71 GB peak footprint / 1.74 GB max RSS; 20 min 1.13 / 2.30 GB;
# 60 min 1.24 / 4.73 GB. The footprint grows about 0.17 GB per hour; the RSS on top of it is pages of the mapped
# scratch files, which are treated as reclaimable (the system writes them back to disk). So past the boundary
# the estimate is the one at the boundary plus this much per hour, about 3x the measured growth.
DISK_BACKED_BYTES_PER_HOUR = GB // 2


def estimate_peak_bytes(duration_s: float, task: str, quality: str = 'standard', sr: int = 48000) -> int:
    """Conservative peak-RAM estimate for one job."""
    if task == 'enhance' and quality in ('standard', 'light'):
        from .dsp.scratch import DISK_BACKED_AFTER_SECONDS

        if duration_s > DISK_BACKED_AFTER_SECONDS:
            longer_h = (duration_s - DISK_BACKED_AFTER_SECONDS) / 3600
            at_boundary = estimate_peak_bytes(DISK_BACKED_AFTER_SECONDS, task, quality, sr)
            return at_boundary + int(longer_h * DISK_BACKED_BYTES_PER_HOUR)
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
        return int(1.4 * stereo_array) + GB // 2  # legacy conservative guard; current level scan is bounded
    return 256 * 1024 * 1024  # download / convert: subprocesses do the work


def estimate_disk_bytes(duration_s: float, task: str, quality: str = 'standard') -> int:
    # The working reference and AI stems are float32 WAV (≈1.4 GB per hour each).
    hours = max(0.0, duration_s) / 3600.0
    per_hour = {'download': 1.5, 'analyze': 2.0, 'enhance': 6.0, 'separate': 14.0}.get(task, 6.0)
    if task == 'enhance' and quality == 'best':
        per_hour += 7.0  # 4 float stems + their processed copies
    scratch = 0
    if task == 'enhance':
        from .dsp.scratch import DISK_BACKED_AFTER_SECONDS, MAX_BYTES_PER_FRAME

        if duration_s > DISK_BACKED_AFTER_SECONDS:
            # 48 kHz working reference, irrespective of the selected export rate.
            scratch = int(duration_s * 48000) * MAX_BYTES_PER_FRAME
    return int(hours * per_hour * GB + 0.25 * GB) + scratch


OUTPUT_MARGIN_BYTES = GB // 2  # covers, tags, container overhead (FLAC can exceed PCM by a fraction of 1 %)
MASTER_RATE = 48000  # Enhance delivers this unless `advanced.sample_rate` says otherwise; stems are made at it


def format_bytes_per_s(key: str, rate: int) -> float:
    """At most this many bytes per second of audio in format `key` delivered at `rate`: the bitrate of a
    lossy format; the PCM size for lossless ones (FLAC and ALAC compress, never by a negative amount)."""
    from .media.export import FORMATS

    spec = FORMATS[key]
    args = list(spec.codec_args)
    if spec.kind == 'lossy' and '-b:a' in args:
        return int(args[args.index('-b:a') + 1].rstrip('k')) * 1000 / 8
    rate = spec.fixed_rate or min(rate, spec.max_rate or rate)
    return rate * 2 * (2 if spec.sample_fmt == 's16' else 3)


def estimate_output_bytes(duration_s: float, task: str, options: dict | None) -> int | None:
    """What a job leaves in the output folder (no scratch), from its validated options: Enhance every
    selected format plus the first one again as Tracks, Split two or four stems. None when that isn't known
    (no options, Save, an unknown format): then the output folder needs as much as the working folder."""
    seconds = max(0.0, duration_s)
    if task == 'analyze':
        return 0  # Check writes nothing there
    try:
        if task == 'enhance' and options is not None:
            rate = int((options.get('advanced') or {}).get('sample_rate') or MASTER_RATE)
            formats = list(options.get('formats') or [])
            if options.get('split_tracks') and formats:
                formats.append(formats[0])  # tasks.enhance cuts the first format into "Tracks"
            return int(seconds * sum(format_bytes_per_s(k, rate) for k in formats)) if formats else None
        if task == 'separate' and options is not None:
            stems = 2 if options.get('mode') == 'karaoke' else 4
            return int(seconds * stems * format_bytes_per_s(options.get('format') or 'wav16', MASTER_RATE))
    except (KeyError, ValueError, TypeError):
        return None
    return None


def preflight(
    duration_s: float | None,
    work_dir: Path,
    task: str,
    quality: str = 'standard',
    device: str | None = None,
    output_dir: Path | None = None,
    options: dict | None = None,
) -> dict[str, object]:
    """Raise ResourceGuardError (with hints) if the job would not fit; return the guard report.

    `duration_s` may be None when unknown (then only disk headroom is checked). An output folder that
    can't be written to (a drive that isn't connected) stops every task that writes there (not Check).
    `options` (the job's validated options) let the output folder's check count only the results."""
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
                else f'Use Adaptive processing (no AI) for long recordings, or use {faster}.'
            )

        mem = memory_report()
        peak = estimate_peak_bytes(duration_s, task, quality)
        report.update(
            estimated_peak_bytes=peak, memory_available_bytes=mem['available_bytes'], memory_source=mem['source']
        )
        if mem['source'] != 'unknown':
            budget = int(int(mem['available_bytes']) * LIMITS.memory_safety_fraction)
            if peak > budget:
                basis = 'the installed-memory safety budget' if mem['source'] == 'host' else 'safely available'
                reasons.append(
                    f'It needs about {human_bytes(peak)} of memory, but only about {human_bytes(budget)} is {basis}.'
                )
                hints.append(
                    'Close other apps, process a shorter piece, or (in Docker) give the container more memory.'
                )

    need = estimate_disk_bytes(duration_s or 600.0, task, quality) + int(LIMITS.min_free_disk_gb * GB)
    # The output folder only receives the results; never ask it for more than the working folder needs.
    results = estimate_output_bytes(duration_s or 600.0, task, options)
    out_need = need if results is None else min(need, results + OUTPUT_MARGIN_BYTES)
    checks = [('working folder', work_dir, need)]
    if task != 'analyze':
        checks.append(('output folder', output_dir, out_need))
    for label, path, needed in checks:
        if path is None:
            continue
        free = disk_free_bytes(path)
        report[f'disk_free_{label.split()[0]}_bytes'] = free
        if free and needed > free:
            reasons.append(f'The {label} needs about {human_bytes(needed)} free, but only {human_bytes(free)} is left.')
            hints.append('Free up some disk space and try again.')

    if reasons:
        raise ResourceGuardError(
            'This job was stopped before it started: ' + ' '.join(reasons), hints=list(dict.fromkeys(hints))
        )
    return report
