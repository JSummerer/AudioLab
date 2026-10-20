"""ffmpeg discovery and the handful of ffmpeg jobs AudioLab needs.

Discovery order: AUDIOLAB_FFMPEG env var → `ffmpeg` on PATH → common Homebrew/
/usr/local locations (GUI launches on macOS often miss them on PATH) → the
binary bundled with the `imageio-ffmpeg` wheel. The bundled build has no
ffprobe, so nothing here depends on ffprobe: media probing parses `ffmpeg -i`.
"""

import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..context import Progress
from ..errors import AudioLabError, ToolMissingError, storage_error_in
from ..runtime import bundled_tool, is_packaged
from ..settings import ENV_PREFIX, LIMITS, SETTINGS
from . import proc

_EXE = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
_EXTRA_LOCATIONS = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/usr/bin/ffmpeg']
_PROGRESS_EVERY_S = 0.25
_shim_lock = threading.Lock()
# ffmpeg messages that mean the input itself is broken (not a bug or a missing encoder).
_DAMAGED_MARKERS = (
    'invalid data found when processing input',
    'decode error rate',
    'header missing',
    'error while decoding',
    'could not find codec parameters',
    'moov atom not found',
    'ebml header parsing failed',
    'received no packets',
    'invalid frame',
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _runs(path: str) -> str | None:
    """Return the version string if `path` is a working ffmpeg, else None."""
    try:
        res = proc.run([path, '-hide_banner', '-version'], timeout_s=20, what='ffmpeg check')
    except Exception:
        return None
    if res.returncode != 0:
        return None
    m = re.search(r'ffmpeg version (\S+)', res.stdout)
    return m.group(1) if m else 'unknown'


@lru_cache(maxsize=1)
def _locate() -> dict[str, str | None]:
    if is_packaged():
        # A self-contained app uses its tested engine, not another application's installation.
        path = bundled_tool('ffmpeg')
        version = _runs(str(path)) if path else None
        return {
            'path': str(path) if path and version else None,
            'version': version,
            'source': 'bundled',
            'error': None if version else 'The built-in audio engine could not start. Download AudioLab again.',
        }
    bundled = None
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    candidates = []
    env = os.environ.get(ENV_PREFIX + 'FFMPEG')
    if env:
        candidates.append(('bundled' if env == bundled else 'system', env))
    which = shutil.which('ffmpeg')
    if which:
        candidates.append(('system', which))
    for p in _EXTRA_LOCATIONS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            candidates.append(('system', p))
    if bundled:
        candidates.append(('bundled', bundled))

    seen = set()
    for source, path in candidates:
        if path in seen:
            continue
        seen.add(path)
        version = _runs(path)
        if version:
            return {'path': path, 'version': version, 'source': source, 'error': None}
    return {
        'path': None,
        'version': None,
        'source': None,
        'error': 'ffmpeg was not found. Reinstall AudioLab (it ships a bundled ffmpeg) '
        'or install ffmpeg from https://ffmpeg.org.',
    }


def ffmpeg_path() -> str:
    loc = _locate()
    if not loc['path']:
        raise ToolMissingError(loc['error'], hints=[proc.REPORT_HINT])
    return loc['path']


def ffmpeg_info() -> dict[str, str | None]:
    loc = _locate()
    return {
        'ok': bool(loc['path']),
        'path': loc['path'],
        'version': loc['version'],
        'source': loc['source'],
        'error': loc['error'],
    }


def _ensure_shim(target: str) -> Path:
    """Expose `target` as <bin_dir>/ffmpeg(.exe) so tools that want a directory
    (yt-dlp --ffmpeg-location) find an executable with the expected name."""
    shim = SETTINGS.bin_dir / _EXE
    if _shim_ok(shim, target):
        return shim
    with _shim_lock:
        if _shim_ok(shim, target):  # another thread made it while we waited
            return shim
        tmp = shim.with_name(f'{shim.name}.{uuid.uuid4().hex[:8]}.tmp')  # unique across processes too
        try:
            if os.name == 'nt':
                shutil.copy2(target, tmp)
            else:
                os.symlink(target, tmp)
            os.replace(tmp, shim)
        except OSError as e:
            if _shim_ok(shim, target):  # another process won the race (or the old shim is in use)
                return shim
            raise ToolMissingError(f'Could not prepare the bundled ffmpeg: {e}') from e
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return shim


def _shim_ok(shim: Path, target: str) -> bool:
    try:
        if os.name == 'nt':
            src, dst = os.stat(target), os.stat(shim)
            return dst.st_size == src.st_size and dst.st_mtime >= src.st_mtime
        return shim.is_symlink() and os.readlink(shim) == target
    except OSError:
        return False


def ffmpeg_dir() -> str:
    """Directory holding an executable literally named ffmpeg(.exe)."""
    path = ffmpeg_path()
    if Path(path).name.lower() == _EXE:
        return str(Path(path).parent)
    return str(_ensure_shim(path).parent)


# ---------------------------------------------------------------------------
# Running ffmpeg
# ---------------------------------------------------------------------------
def _tail_message(stderr_tail: str, limit: int = 300) -> str:
    lines = [ln.strip() for ln in stderr_tail.splitlines() if ln.strip()]
    # Skip progress/statistics noise; keep the last meaningful lines.
    lines = [ln for ln in lines if not ln.startswith(('size=', 'frame=', 'video:', 'audio:'))]
    text = ' | '.join(lines[-3:])
    return text[-limit:]


def run_ffmpeg(
    args: list[str], progress: Progress | None = None, duration_s: float | None = None, what: str = 'ffmpeg'
) -> proc.ProcResult:
    """Run ffmpeg with `args` (inputs/outputs/filters). Reports live progress
    when `duration_s` is known. Raises AudioLabError on failure."""
    progress = progress or Progress.null()
    cmd = [ffmpeg_path(), '-hide_banner', '-nostdin', '-y', '-progress', 'pipe:1', '-nostats', *[str(a) for a in args]]
    last = [0.0]

    def on_line(line: str):
        if not duration_s or duration_s <= 0:
            return
        if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
            try:
                t = int(line.split('=', 1)[1]) / 1e6
            except ValueError:
                return
            now = time.monotonic()
            if now - last[0] >= _PROGRESS_EVERY_S:
                last[0] = now
                progress(min(99.0, 100.0 * t / duration_s))

    res = proc.run(
        cmd,
        progress=progress,
        timeout_s=LIMITS.ffmpeg_timeout_s,
        on_line=on_line,
        what=what,
        capture_stdout=False,
        timeout_hint='Try a shorter piece, or close other apps that keep the computer busy.',
    )
    if res.returncode != 0:
        detail = _tail_message(res.stderr_tail)
        storage = storage_error_in(res.stderr_tail, detail)  # a full disk, a folder it may not write to
        if storage is not None:
            raise storage
        if any(m in res.stderr_tail.lower() for m in _DAMAGED_MARKERS):
            raise AudioLabError(
                f'{what[:1].upper()}{what[1:]} failed: the file seems to be damaged, '
                'or it is not in a format AudioLab can read.',
                hints=[
                    'Check that it plays all the way through in a media player.',
                    'If it does, save it as WAV or FLAC with another app and try that.',
                ],
                detail=detail,
            )
        raise AudioLabError(f'{what[:1].upper()}{what[1:]} failed: {detail}', detail=detail)
    progress(100)
    return res


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
_DURATION_RE = re.compile(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)')
_BITRATE_RE = re.compile(r'Duration:.*?bitrate:\s*(\d+)\s*kb/s')
_STREAM_RE = re.compile(r'Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*(Audio|Video):\s*([^\s,]+)(.*)')
_CHANNEL_WORDS = {
    'mono': 1,
    'stereo': 2,
    '2.1': 3,
    'quad': 4,
    '4.0': 4,
    '5.0': 5,
    '5.1': 6,
    '5.1(side)': 6,
    '6.1': 7,
    '7.1': 8,
}


def probe_media(path: Path) -> dict[str, object]:
    """Describe a media file by parsing `ffmpeg -i` (works without ffprobe)."""
    info: dict[str, object] = {
        'duration_s': None,
        'has_audio': False,
        'has_video': False,
        'has_cover': False,
        'audio_codec': None,
        'video_codec': None,
        'sample_rate': None,
        'channels': None,
        'bitrate_kbps': None,
        'width': None,  # of the first video stream (or picture)
        'height': None,
        'tags': {},
    }
    path = Path(path)
    if not path.is_file():
        return info
    lines: list[str] = []  # collect everything: long chapter/tag lists overflow the stderr tail
    proc.run(
        [ffmpeg_path(), '-hide_banner', '-nostdin', '-i', str(path)],
        timeout_s=120,
        what='media probe',
        capture_stdout=False,
        on_line=lines.append,
    )
    text = '\n'.join(lines)
    m = _DURATION_RE.search(text)
    if m:
        h, mi, s = m.groups()
        info['duration_s'] = round(int(h) * 3600 + int(mi) * 60 + float(s), 3)
    m = _BITRATE_RE.search(text)
    if m:
        info['bitrate_kbps'] = int(m.group(1))
    for line in text.splitlines():
        sm = _STREAM_RE.search(line)
        if not sm:
            continue
        kind, codec, rest = sm.groups()
        if kind == 'Audio' and re.search(r'\b0 channels\b', rest):
            continue  # random bytes that merely look like an MP3 header
        if kind == 'Video' and 'attached pic' in rest:
            info['has_cover'] = True
        elif kind == 'Audio' and not info['has_audio']:
            info['has_audio'] = True
            info['audio_codec'] = codec
            rm = re.search(r'(\d+)\s*Hz', rest)
            if rm:
                info['sample_rate'] = int(rm.group(1))
            parts = [p.strip() for p in rest.split(',')]
            for p in parts:
                if p in _CHANNEL_WORDS:
                    info['channels'] = _CHANNEL_WORDS[p]
                    break
                cm = re.match(r'(\d+)\s+channels', p)
                if cm:
                    info['channels'] = int(cm.group(1))
                    break
        elif kind == 'Video' and 'attached pic' not in rest and not info['has_video']:
            info['has_video'] = True
            info['video_codec'] = codec
            sm = re.search(r'\b(\d{1,5})x(\d{1,5})\b', rest)
            if sm:
                info['width'], info['height'] = int(sm.group(1)), int(sm.group(2))
    info['tags'] = _parse_tags(lines)
    return info


_TAG_KEYS = ('title', 'artist', 'album', 'track', 'date')


def _parse_tags(lines: list[str]) -> dict[str, str]:
    """title/artist/album/track/date from the 'Metadata:' blocks of `ffmpeg -i`: the
    file's own block, else the first audio stream's (Ogg/Opus keep them there).
    Chapter titles and the cover image's 'title: Cover' are ignored."""
    found: dict[str, dict[str, str]] = {'file': {}, 'audio': {}}
    section, target, meta_indent, audio_seen = None, None, None, False
    for line in lines:
        indent = len(line) - len(line.lstrip(' '))
        text = line.strip()
        if meta_indent is not None and indent > meta_indent:
            key, sep, value = text.partition(':')
            key, value = key.strip().lower(), value.strip()
            if target is not None and sep and key in _TAG_KEYS and value:
                target.setdefault(key, value)
            continue
        meta_indent = None
        if text.startswith('Input #'):
            section = 'file'
        elif text.startswith('Chapter #'):
            section = None
        elif text.startswith('Stream #'):
            section = 'audio' if ': Audio:' in text and not audio_seen else None
            audio_seen = audio_seen or ': Audio:' in text
        elif text == 'Metadata:':
            meta_indent, target = indent, (found[section] if section else None)
    return {**found['audio'], **found['file']}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _tmp_for(dst: Path) -> Path:
    # Keep the real extension last so ffmpeg picks the right muxer.
    return dst.with_name(f'.{dst.stem}.partial{dst.suffix}')


def decode_reference(
    src: Path, dst: Path, progress: Progress | None = None, duration_s: float | None = None
) -> dict[str, object]:
    """Decode any audio/video file once to the 48 kHz stereo working reference.

    The reference is 32-bit float WAV (RF64 when > 4 GB): decoded lossy audio and
    down-mixes often peak above 0 dBFS, and an integer format would hard-clip
    those peaks before the mastering chain can handle them gracefully."""
    import soundfile as sf

    progress = progress or Progress.null()
    src, dst = Path(src), Path(dst)
    meta = probe_media(src)
    if not meta['has_audio']:
        raise AudioLabError(
            'This file has no audio track AudioLab can read.',
            hints=[
                'Check that the file plays in a media player.',
                'Supported: most audio and video formats (MP3, WAV, FLAC, M4A, OGG, MP4, MKV, WEBM…).',
            ],
        )
    duration_s = duration_s or meta.get('duration_s')
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(dst)
    try:
        run_ffmpeg(
            [
                '-i',
                str(src),
                '-map',
                '0:a:0',
                '-vn',
                '-sn',
                '-dn',
                '-ar',
                '48000',
                '-ac',
                '2',
                '-c:a',
                'pcm_f32le',
                '-rf64',
                'auto',
                str(tmp),
            ],
            progress=progress,
            duration_s=duration_s,
            what='audio decoding',
        )
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    info = sf.info(str(dst))
    if info.frames <= 0:
        raise AudioLabError('The audio track is empty.')
    return {
        'path': str(dst),
        'frames': info.frames,
        'samplerate': info.samplerate,
        'channels': info.channels,
        'duration_s': round(info.duration, 3),
        'subtype': info.subtype,
    }


PCM_RATE = 48000
# Every chunk starts and ends on this grid (1/25 s): there the 48 kHz output and every common source rate
# (8k … 192k, 44.1k and 32k included) meet on whole samples, so the resampler runs in the same phase as in a
# full decode and consecutive chunks join without a seam.
PCM_GRID = 1920
PCM_PREROLL = 12 * PCM_GRID  # 0.48 s decoded and dropped: decoders and the resampler warm up
PCM_TIMEOUT_S = 60
_MS_TIMESTAMPS = ('.webm', '.mkv', '.mka')  # Matroska: timestamps in whole milliseconds
# A seek abandons its chunk in the browser, not here: at most this many decodes run at once, so fast
# scrubbing can't pile up ffmpeg processes.
_PCM_SLOTS = threading.BoundedSemaphore(4)


def _seconds(frames: int) -> str:
    return f'{frames / PCM_RATE:.6f}'  # exact: grid steps are whole milliseconds


def decode_pcm(path: Path, offset: int, frames: int, cancel_event: threading.Event | None = None) -> bytes:
    """`frames` frames of `path` from frame `offset` as 48 kHz stereo float32 (little-endian, interleaved),
    for the compare player; fewer only at the end of the file. With `offset` and `frames` on PCM_GRID it is
    bit-identical to the same slice of a full decode, so chunk after chunk joins seamlessly (AAC: up to its
    noise-filling bands, which are random by design). Stereo as the working reference is made (`-ac 2`).
    `cancel_event` stops it (the app quits: its ffmpeg is killed, see AppState.shutdown)."""
    if offset < 0 or frames <= 0 or offset % PCM_GRID or frames % PCM_GRID:
        raise ValueError('offset and frames must be on the 1920-frame grid')
    path = Path(path)
    if not _PCM_SLOTS.acquire(timeout=PCM_TIMEOUT_S):
        raise AudioLabError('AudioLab is busy reading other audio.', hints=['Try again in a moment.'])
    try:
        stop = Progress.null(cancel_event)
        if offset <= PCM_PREROLL:
            return _decode(path, offset, frames, stop)
        shift = _seek_shift(path, cancel_event) if path.suffix.lower() in _MS_TIMESTAMPS else 0
        if shift is None:  # couldn't be measured (a short file, or silent where it looks): exact from the start
            return _decode(path, offset, frames, stop)
        return _decode(path, offset, frames, stop, seek=True, shift=shift)
    finally:
        _PCM_SLOTS.release()


def _decode(path: Path, offset: int, frames: int, progress: Progress, seek: bool = False, shift: int = 0) -> bytes:
    cmd = [ffmpeg_path(), '-hide_banner', '-nostdin', '-v', 'error']
    if not seek:  # from the start, like a full decode
        cmd += ['-i', str(path), '-map', '0:a:0', '-vn', '-sn', '-dn']
        # A chunk offset is relative to the first decoded sample, not container
        # timestamps (older FFmpeg retains a WebM/Opus start offset here).
        cmd += ['-af', 'asetpts=PTS-STARTPTS']
        if offset:
            cmd += ['-ss', _seconds(offset)]
    else:
        # Seek 0.48 s early. Some decoders (AAC) start a little after the seek point, which would put the
        # resampler out of phase: it only starts at the grid point halfway, and the output from `offset` on.
        cmd += ['-ss', _seconds(offset - PCM_PREROLL), '-i', str(path), '-map', '0:a:0', '-vn', '-sn', '-dn']
        cmd += ['-af', f'atrim=start={_seconds(PCM_PREROLL // 2)}', '-ss', _seconds(PCM_PREROLL + shift)]
    # A step longer than asked: the read below makes it exact, ffmpeg's rounding can't leave a frame out.
    cmd += ['-t', _seconds(frames + PCM_GRID), '-ac', '2', '-ar', str(PCM_RATE), '-c:a', 'pcm_f32le', '-f', 'f32le']
    fd, tmp = tempfile.mkstemp(prefix='.audiolab-pcm-', suffix='.f32')
    os.close(fd)
    try:
        # proc.run reads text: the samples go through a temporary file.
        res = proc.run(
            [*cmd, '-y', tmp],
            progress=progress,
            timeout_s=PCM_TIMEOUT_S,
            what='reading the audio',
            capture_stdout=False,
        )
        if res.returncode != 0:
            raise AudioLabError(
                'This file could not be read for playing.',
                hints=['Check that it plays in a media player, or download it and listen there.'],
                detail=_tail_message(res.stderr_tail),
            )
        with open(tmp, 'rb') as f:
            return f.read(frames * 8)  # 2 channels × 4 bytes
    finally:
        Path(tmp).unlink(missing_ok=True)


def _seek_shift(path: Path, cancel_event: threading.Event | None) -> int | None:
    try:
        st = path.stat()
    except OSError:
        return 0
    return _measured_shift(str(path), st.st_size, st.st_mtime_ns, cancel_event)


@lru_cache(maxsize=32)
def _measured_shift(path: str, size: int, mtime_ns: int, cancel_event: threading.Event | None) -> int | None:
    """Frames to trim later after a seek into this file, measured once. Matroska keeps timestamps in whole
    milliseconds, so ffmpeg can place the audio a little off after a seek (YouTube's WebM Opus, whose frames
    start at x.5 ms: 1 ms early). The same passage is decoded from the start and by a seek and lined up.
    None when there is nothing to measure — the file ends first or is silent at every place tried — so
    chunks are decoded from the start instead (exact; such files are short). 0 when a passage with sound
    lines up with no whole-frame shift (a fractional one stays: under a millisecond)."""
    n, stop = 10 * PCM_GRID, Progress.null(cancel_event)
    for at in (25, 250, 1500, 7500, 22500):  # × PCM_GRID: at 1 s, 10 s, 1 min, 5 min, 15 min
        at *= PCM_GRID
        via = np.frombuffer(_decode(Path(path), at, n, stop, seek=True), np.float32)
        if len(via) < 2 * n:
            return None  # the file ends before this passage
        if np.abs(via).max() < 1e-3:
            continue  # silence lines up with any shift
        ref = np.frombuffer(_decode(Path(path), at - PCM_GRID, n + 2 * PCM_GRID, stop), np.float32)
        for lag in sorted(range(-PCM_GRID, PCM_GRID + 1), key=abs):
            i = 2 * (PCM_GRID + lag)
            if len(ref) >= i + len(via) and np.abs(ref[i : i + 256] - via[:256]).max() < 1e-5:
                if np.abs(ref[i : i + len(via)] - via).max() < 1e-5:  # (Opus decoders round at -150 dB)
                    return -lag
        return 0
    return None


def render_spectrogram(src: Path, dst_png: Path, progress: Progress | None = None) -> Path:
    """Render a whole-file spectrogram (frequency over time) to PNG."""
    progress = progress or Progress.null()
    src, dst_png = Path(src), Path(dst_png)
    dst_png.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(dst_png)
    duration = probe_media(src).get('duration_s')
    try:
        run_ffmpeg(
            [
                '-i',
                str(src),
                '-filter_complex',
                '[0:a:0]showspectrumpic=s=1200x480:legend=1:mode=combined:color=intensity:scale=log:fscale=lin[v]',
                '-map',
                '[v]',
                '-frames:v',
                '1',
                str(tmp),
            ],
            progress=progress,
            duration_s=duration,
            what='spectrogram rendering',
        )
        os.replace(tmp, dst_png)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return dst_png
