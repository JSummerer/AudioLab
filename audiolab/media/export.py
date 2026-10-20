"""Output formats: one registry of everything AudioLab can write, a single-file
`transcode()` and a multi-format `export()` used by every task.

16-bit targets are TPDF-dithered by ffmpeg's resampler; lossless and lossy
files get title/artist/album tags and, where the container supports it, the
cover image (always a JPEG of at most COVER_MAX_PX, see `cover_jpeg`). Files
are written under a hidden temporary name and moved into place without ever
replacing an existing file (`Name (2).ext` instead).

Each format carries a few plain-language `facts` (what it is, size, where it
plays, limits) — the one source for the format cards in the UI (/api/health).
"""

import os
import re
import shutil
import struct
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..context import Progress
from ..errors import AudioLabError
from ..util import sanitize_filename, unique_path
from . import proc
from .ffmpeg import ffmpeg_path, probe_media, run_ffmpeg


@dataclass(frozen=True)
class FormatSpec:
    key: str
    label: str  # 'MP3 · 320 kbps'
    short: str  # used to disambiguate file names: 'Title [ALAC].m4a'
    ext: str  # '.mp3'
    kind: str  # lossy | lossless | video | original
    description: str  # plain-language use case
    cover: bool  # container can carry cover art
    codec_args: tuple  # ffmpeg audio codec arguments
    sample_fmt: str | None = None  # dithered output sample format for 16-bit targets
    max_rate: int | None = None  # codec sample-rate ceiling (MP3: 48k, Opus: always 48k)
    fixed_rate: int | None = None
    facts: tuple = ()  # 2–4 short lines for the format card: what it is, size, plays on, limits


FORMATS: dict[str, FormatSpec] = {
    f.key: f
    for f in [
        FormatSpec(
            'mp3_320',
            'MP3 · 320 kbps',
            'MP3',
            '.mp3',
            'lossy',
            'Plays everywhere — phones, cars, old players.',
            True,
            ('-c:a', 'libmp3lame', '-b:a', '320k'),
            max_rate=48000,
            facts=(
                'Lossy MP3 at its highest setting (320 kbps).',
                'About 2.4 MB per minute.',
                'Plays on practically everything: phones, cars, DJ software, old players.',
            ),
        ),
        FormatSpec(
            'aac_256',
            'AAC · 256 kbps (.m4a)',
            'AAC',
            '.m4a',
            'lossy',
            'Small and great on Apple devices and phones.',
            True,
            ('-c:a', 'aac', '-b:a', '256k'),
            max_rate=96000,
            facts=(
                'Lossy AAC at 256 kbps in an .m4a file.',
                'About 1.9 MB per minute.',
                'Plays on iPhone, Mac, Android and Windows; not on players that only know MP3.',
            ),
        ),
        FormatSpec(
            'opus_160',
            'Opus · 160 kbps',
            'Opus',
            '.opus',
            'lossy',
            'Smallest files at high quality (not for older players).',
            False,
            ('-c:a', 'libopus', '-b:a', '160k'),
            fixed_rate=48000,
            facts=(
                'Lossy Opus at 160 kbps — a modern, efficient codec.',
                'About 1.2 MB per minute — the smallest files here.',
                'Not for Apple Music/iTunes or older players.',
            ),
        ),
        FormatSpec(
            'alac16',
            'Apple Lossless · 16-bit (.m4a)',
            'ALAC',
            '.m4a',
            'lossless',
            'Lossless for Apple Music on iPhone and Mac.',
            True,
            ('-c:a', 'alac'),
            sample_fmt='s16',
            facts=(
                'Apple Lossless, 16-bit (CD quality), in an .m4a file — nothing removed.',
                'About 5–8 MB per minute (depends on the music).',
                'Plays in Apple Music on iPhone, iPad and Mac.',
                "Bluetooth AirPods don't play lossless (they get AAC); AirPods Max (USB-C) only on the USB-C cable.",
            ),
        ),
        FormatSpec(
            'flac16',
            'FLAC · 16-bit',
            'FLAC 16-bit',
            '.flac',
            'lossless',
            'Lossless and compact.',
            True,
            ('-c:a', 'flac', '-compression_level', '8'),
            sample_fmt='s16',
            facts=(
                'Lossless FLAC, 16-bit (CD quality) — nothing removed.',
                'About 5–8 MB per minute (depends on the music).',
                "Plays on Android, Windows and in VLC; Apple Music can't import it.",
            ),
        ),
        FormatSpec(
            'flac24',
            'FLAC · 24-bit',
            'FLAC 24-bit',
            '.flac',
            'lossless',
            'Studio-quality lossless archive.',
            True,
            ('-c:a', 'flac', '-compression_level', '8', '-sample_fmt', 's32', '-bits_per_raw_sample', '24'),
            facts=(
                'Lossless FLAC, 24-bit — extra headroom for editing and archiving.',
                'About 10–14 MB per minute at 48 kHz (depends on the music).',
                "Apple Music can't import it.",
                "iPhone's built-in output plays up to 24-bit/48 kHz; Hi-Res (up to 192 kHz) needs an external DAC.",
            ),
        ),
        FormatSpec(
            'wav16',
            'WAV · 16-bit',
            'WAV 16-bit',
            '.wav',
            'lossless',
            'Uncompressed, works on CDJs, car stereos and editing software.',
            False,
            ('-c:a', 'pcm_s16le'),
            sample_fmt='s16',
            facts=(
                'Uncompressed WAV, 16-bit (CD quality).',
                'About 10–12 MB per minute.',
                'Works on CDJs, car stereos and in editing software.',
            ),
        ),
        FormatSpec(
            'wav24',
            'WAV · 24-bit',
            'WAV 24-bit',
            '.wav',
            'lossless',
            'Uncompressed 24-bit for editing and production.',
            False,
            ('-c:a', 'pcm_s24le'),
            facts=(
                'Uncompressed WAV, 24-bit — for editing and production.',
                'About 17 MB per minute at 48 kHz.',
            ),
        ),
    ]
}

# Simple download choices → format keys (None = handled without transcoding).
DOWNLOAD_FORMATS: dict[str, str | None] = {
    'mp3': 'mp3_320',
    'm4a': 'aac_256',
    'opus': 'opus_160',
    'flac': 'flac16',
    'wav': 'wav16',
    'original': None,
    'video': None,
}
# A link's audio is already lossy: converting it can't add quality (the facts say so per choice).
_NO_GAIN = "From a link it adds no quality: the site's stream is already lossy — it only avoids further loss."
_DOWNLOAD_CHOICES = [  # (key, label, description, facts)
    (
        'mp3',
        'MP3',
        'Plays everywhere (320 kbps).',
        (
            'Converted to MP3 at 320 kbps.',
            'About 2.4 MB per minute.',
            'Plays on practically everything.',
            "A converted copy can't sound better than its source; Original keeps a link's stream untouched.",
        ),
    ),
    (
        'm4a',
        'M4A',
        'Great for Apple devices (AAC 256 kbps).',
        (
            'Converted to AAC at 256 kbps (.m4a).',
            'About 1.9 MB per minute.',
            'Plays on iPhone, Mac, Android and Windows.',
        ),
    ),
    (
        'opus',
        'Opus',
        'Smallest files (160 kbps) — re-encoded, not the original stream.',
        (
            "Re-encoded to Opus at 160 kbps — a new encoding, not the site's own stream.",
            'About 1.2 MB per minute.',
            'Not for Apple Music/iTunes or older players.',
        ),
    ),
    (
        'flac',
        'FLAC',
        'Lossless copy — bigger files, no extra quality over the source.',
        ('Lossless FLAC (16-bit) of the decoded source.', _NO_GAIN, 'About 5–8 MB per minute.'),
    ),
    (
        'wav',
        'WAV',
        'Uncompressed — for DJ gear and editing software.',
        (
            'Uncompressed WAV (16-bit) of the decoded source.',
            _NO_GAIN,
            'About 10–12 MB per minute.',
            'Works on CDJs, car stereos and in editing software.',
        ),
    ),
    (
        'original',
        'Original',
        'Exactly what the site delivered, no conversion.',
        (
            "The site's own audio stream, untouched — no conversion, no loss.",
            'YouTube: Opus ~130–160 kbps (.webm) or AAC ~128 kbps (.m4a); about 256 kbps with a Premium sign-in.',
            'About 1 MB per minute.',
        ),
    ),
    (
        'video',
        'Video (MP4)',
        'The video with sound.',
        (
            'MP4 video with its sound (H.264 + AAC when the site offers it).',
            "Much bigger than audio — the size depends on the video's resolution.",
        ),
    ),
]
# One line about what a link's audio really is, shown under the format picker.
SOURCE_NOTE = (
    "YouTube audio is already compressed (about 130–160 kbps, 256 kbps with Premium): converting can't add quality."
)
_ENHANCE_ORDER = ['mp3_320', 'wav16', 'alac16', 'flac24', 'aac_256', 'wav24', 'flac16', 'opus_160']
SEPARATE_FORMATS = ('wav16', 'wav24', 'flac24', 'mp3_320')
_SEPARATE_ORDER = list(SEPARATE_FORMATS)
# Files that can be added (uploaded): audio and video containers ffmpeg reads. One list for the server and
# the UI's file picker (/api/health → upload_extensions).
UPLOAD_EXT = frozenset(
    '.3gp .aac .ac3 .aif .aifc .aiff .alac .amr .ape .avi .caf .dts .eac3 .flac .flv .m4a .m4b .m4v .mka .mkv '
    '.mov .mp2 .mp3 .mp4 .mpeg .mpg .mpga .mts .oga .ogg .opus .ts .w64 .wav .wave .webm .wma .wmv .wv'.split()
)


def format_choices(kind: str) -> list[dict[str, object]]:
    """Choices for the UI. kind: 'download' | 'enhance' | 'separate'."""
    if kind == 'download':
        out = []
        for k, label, desc, facts in _DOWNLOAD_CHOICES:
            spec = FORMATS[DOWNLOAD_FORMATS[k]] if DOWNLOAD_FORMATS[k] else None
            out.append(
                {
                    'key': k,
                    'label': label,
                    'description': desc,
                    'kind': spec.kind if spec else k,  # untouched: 'original' | 'video'
                    'facts': list(facts),
                    'cover': bool(spec and spec.cover),  # untouched streams get no cover added
                }
            )
        return out
    order = _ENHANCE_ORDER if kind == 'enhance' else _SEPARATE_ORDER if kind == 'separate' else list(FORMATS)
    return [
        {
            'key': k,
            'label': FORMATS[k].label,
            'description': FORMATS[k].description,
            'kind': FORMATS[k].kind,
            'ext': FORMATS[k].ext,
            'facts': list(FORMATS[k].facts),
            'cover': FORMATS[k].cover,
        }
        for k in order
    ]


def resolve_format(key: str) -> FormatSpec:
    """Accept a format key or a simple download alias ('mp3', 'wav'…)."""
    k = DOWNLOAD_FORMATS.get(key, key) if key in DOWNLOAD_FORMATS else key
    if not k or k not in FORMATS:
        raise AudioLabError(f'Unknown output format: {key!r}.')
    return FORMATS[k]


# ----------------------------------------------------------------- cover art
COVER_MAX_PX = 1400  # embedded covers: big enough for any player, small enough to repeat in every track
COVER_UPLOAD_EXT = ('.jpg', '.jpeg', '.png', '.webp')
COVER_MAX_PIXELS = 40_000_000  # 40 MP (e.g. 6000 × 6000) — far beyond any cover


def picture_size(path: Path) -> tuple[str, int, int] | None:
    """('jpeg' | 'png' | 'webp', width, height) read from the file's header, else None.

    Plain stdlib, before ffmpeg sees the file: a small file can declare a gigapixel picture that
    ffmpeg would try to decode in full (a "decompression bomb"), and ffmpeg also accepts other
    containers (e.g. an MJPEG video) under a picture's name — this only knows the three real formats.
    JPEG metadata (EXIF, colour profiles) must end within the first 2 MB."""
    with open(path, 'rb') as f:
        head = f.read(2 << 20)
    if head.startswith(b'\x89PNG\r\n\x1a\n') and head[12:16] == b'IHDR':
        w, h = struct.unpack('>II', head[16:24])
        return 'png', w, h
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP' and len(head) >= 30:
        chunk = head[12:16]
        if chunk == b'VP8 ' and head[23:26] == b'\x9d\x01\x2a':  # lossy: 14-bit sizes after the start code
            w, h = struct.unpack('<HH', head[26:30])
            return 'webp', w & 0x3FFF, h & 0x3FFF
        if chunk == b'VP8L' and head[20] == 0x2F:  # lossless: 14 bits each, minus one
            bits = int.from_bytes(head[21:25], 'little')
            return 'webp', (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b'VP8X':  # extended: 24-bit canvas size, minus one
            return 'webp', int.from_bytes(head[24:27], 'little') + 1, int.from_bytes(head[27:30], 'little') + 1
        return None
    i = 2 if head.startswith(b'\xff\xd8') else len(head)
    while i + 9 <= len(head):  # JPEG: walk the segments up to the frame header (SOF0…SOF15)
        if head[i] != 0xFF:
            return None
        marker = head[i + 1]
        if marker == 0xFF or marker == 0x01 or 0xD0 <= marker <= 0xD7:  # fill byte / no length
            i += 1 if marker == 0xFF else 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack('>HH', head[i + 5 : i + 9])
            return 'jpeg', w, h
        i += 2 + int.from_bytes(head[i + 2 : i + 4], 'big')
    return None


def cover_jpeg(src: Path, dst: Path, max_px: int | None = COVER_MAX_PX, square: bool = False) -> Path:
    """Write the picture in `src` (an image file, or the cover a media file carries) as a JPEG:
    optionally cropped to a square, at most `max_px` on its longer side (never enlarged)."""
    vf = ["crop='min(iw,ih)':'min(iw,ih)'"] if square else []
    if max_px:
        vf.append(
            f"scale='min({max_px},iw)':'min({max_px},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = ['-i', str(src), '-map', '0:v:0', '-frames:v', '1', *(['-vf', ','.join(vf)] if vf else [])]
    run_ffmpeg([*args, '-q:v', '2', '-update', '1', str(dst)], what='cover image conversion')
    if not dst.is_file() or dst.stat().st_size == 0:
        raise AudioLabError('The cover image could not be converted.')
    return dst


def check_picture(path: Path) -> tuple[str, int, int]:
    """`picture_size`, or AudioLabError for anything that is not a JPEG/PNG/WebP of sane size."""
    size = picture_size(path)
    if size is None or not size[1] or not size[2]:
        raise AudioLabError(
            'This file is not a JPG, PNG or WebP picture, so it cannot be the cover.',
            hints=['Pick a JPG, PNG or WebP image.'],
        )
    if size[1] * size[2] > COVER_MAX_PIXELS:
        raise AudioLabError(
            f'This picture is too large ({size[1]} × {size[2]} pixels).',
            hints=['Use a picture of at most 40 megapixels (e.g. 6000 × 6000) — covers are shown much smaller.'],
        )
    return size


def save_picture(src: Path, out_dir: Path, stem: str) -> Path:
    """Keep the picture in `src` as '<stem>.jpg' (or '.png') in `out_dir`, at full size: JPEG and PNG
    as they are, anything else (WebP) as a high-quality JPEG. Checked first (`check_picture`); never
    replaces an existing file — returns the path written (may be '<stem> (2).jpg')."""
    kind = check_picture(src)[0]
    if kind in ('jpeg', 'png'):
        return place_file(src, Path(out_dir) / f'{stem}.{"jpg" if kind == "jpeg" else "png"}')
    dst = Path(out_dir) / f'{stem}.jpg'
    staging = _staging_for(dst)
    try:
        cover_jpeg(src, staging, max_px=None)
        return _commit(staging, dst)
    finally:
        staging.unlink(missing_ok=True)


def prepare_cover(src: Path, dst: Path) -> Path:
    """Check that an uploaded file really is a JPEG, PNG or WebP picture (`check_picture`, before
    ffmpeg touches it) and store it as the JPEG that gets embedded (`cover_jpeg`)."""
    check_picture(src)
    try:
        return cover_jpeg(src, dst)
    except AudioLabError as e:
        raise AudioLabError(
            'This picture could not be read — it may be damaged.',
            hints=['Open it in an image viewer, or pick another.'],
        ) from e


@lru_cache(maxsize=4)
def _encoders(ffmpeg: str) -> frozenset:
    try:
        res = proc.run([ffmpeg, '-hide_banner', '-encoders'], timeout_s=30, what='encoder check')
    except Exception:
        return frozenset()
    names = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in 'AVS':
            names.add(parts[1])
    return frozenset(names)


def _check_encoder(spec: FormatSpec) -> None:
    codec = spec.codec_args[spec.codec_args.index('-c:a') + 1]
    enc = _encoders(ffmpeg_path())
    if enc and codec not in enc:
        raise AudioLabError(
            f'Your ffmpeg cannot write {spec.label} (missing encoder "{codec}").',
            hints=[
                'Pick another format.',
                'AudioLab uses the ffmpeg installed on this computer before its own: a full ffmpeg build '
                'can write this format.',
            ],
        )


def _staging_for(dst: Path) -> Path:
    """Hidden, per-call temp name next to `dst` (two jobs with the same title never share it);
    the real extension stays last so ffmpeg picks the right muxer."""
    return dst.with_name(f'.{dst.stem}.{uuid.uuid4().hex[:8]}.partial{dst.suffix}')


def place_file(src: Path, dst: Path, move: bool = False) -> Path:
    """Copy/move `src` to `dst` without ever replacing an existing file.
    Returns the path actually used (may be 'name (2).ext')."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_for(dst)
    if move:
        try:
            os.replace(src, staging)
        except OSError:  # different filesystem
            shutil.copy2(src, staging)
            src.unlink(missing_ok=True)
    else:
        shutil.copy2(src, staging)
    return _commit(staging, dst)


def _commit(staging: Path, dst: Path) -> Path:
    """Atomically move a finished temp file to a collision-free final name."""
    for _ in range(1000):
        final = unique_path(dst)
        try:
            os.link(staging, final)  # fails if `final` appeared meanwhile
            staging.unlink()
            return final
        except FileExistsError:
            continue
        except OSError:  # no hard links (exFAT/FAT USB sticks, some shares)
            try:  # reserve the name atomically, then move onto it
                os.close(os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            except FileExistsError:
                continue
            try:
                os.replace(staging, final)
            except OSError:
                final.unlink(missing_ok=True)  # give the reserved (empty) name back
                raise
            return final
    raise AudioLabError(f'Could not find a free file name for {dst.name}.')


def transcode(
    src: Path,
    dst: Path,
    fmt: str,
    tags: dict[str, str] | None = None,
    cover: Path | None = None,
    progress: Progress | None = None,
    duration_s: float | None = None,
    sample_rate: int | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
) -> Path:
    """Convert `src` into format `fmt` at `dst`. Never overwrites: returns the
    path actually written (may carry a ' (2)' suffix).

    `cover`: the JPEG to embed (in formats that can carry one). None = no picture: a picture `src`
    carries is never copied on its own — whether there is one is the caller's choice (`common.cover_art`
    takes an upload's picture when "Add cover art" is on).

    `start_s`/`end_s` cut a sample-accurate excerpt (atrim on the decoded audio:
    input-side seeking lands up to ~10 ms off in WebM/M4A, and an output-side -ss
    would also drop the cover frame)."""
    progress = progress or Progress.null()
    spec = resolve_format(fmt)
    _check_encoder(spec)
    src, dst = Path(src), Path(dst)
    if dst.suffix.lower() != spec.ext:
        dst = dst.with_name(dst.name + spec.ext)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = probe_media(src)
    if duration_s is None:
        duration_s = info.get('duration_s')
    trim = []
    if start_s or end_s:
        start = max(0.0, float(start_s or 0))
        trim = [f'atrim=start={start:.6f}' + (f':end={float(end_s):.6f}' if end_s else ''), 'asetpts=PTS-STARTPTS']
        if duration_s:
            duration_s = max(0.1, (float(end_s) if end_s else duration_s) - start)

    rate = sample_rate
    if spec.fixed_rate:
        rate = spec.fixed_rate
    elif rate and spec.max_rate and rate > spec.max_rate:
        rate = spec.max_rate

    resample = []
    if rate:
        resample.append(str(int(rate)))
    if spec.sample_fmt:
        resample += [f'osf={spec.sample_fmt}', 'dither_method=triangular']
    chain = trim + (['aresample=' + ':'.join(resample)] if resample else [])
    afilter = ['-af', ','.join(chain)] if chain else []

    # Global tags are copied from the source and then overridden here; an empty value ('') removes
    # a tag (e.g. the user cleared the artist). Per-stream tags (Ogg/Opus keep them on the stream,
    # where they would win over ours) are not copied.
    meta = ['-map_metadata:s:a', '-1']
    for k, v in (tags or {}).items():
        if v is not None:
            meta += ['-metadata', f'{k}={v}']

    staging = _staging_for(dst)

    def build(art: bool) -> list[str]:
        args = ['-i', str(src)]
        if art:  # always a JPEG from cover_jpeg: embedded as it is
            args += ['-i', str(cover), '-map', '0:a:0', '-map', '1:v:0', '-c:v', 'copy']
        else:
            args += ['-map', '0:a:0']
        if art:
            args += [
                '-disposition:v:0',
                'attached_pic',
                '-metadata:s:v',
                'title=Cover',
                '-metadata:s:v',
                'comment=Cover (front)',
            ]
        args += [*spec.codec_args, *afilter, *meta]
        if spec.ext == '.mp3':
            args += ['-id3v2_version', '3']
        if spec.ext == '.m4a':
            args += ['-movflags', '+faststart']
        args.append(str(staging))
        return args

    art = bool(spec.cover and cover and Path(cover).is_file())
    try:
        try:
            run_ffmpeg(build(art), progress=progress, duration_s=duration_s, what=f'{spec.label} export')
        except AudioLabError:
            if not art:
                raise
            staging.unlink(missing_ok=True)  # cover muxing failed: keep the audio, drop the art
            run_ffmpeg(build(False), progress=progress, duration_s=duration_s, what=f'{spec.label} export')
        if not staging.is_file() or staging.stat().st_size == 0:
            raise AudioLabError(f'{spec.label} export produced an empty file.')
        return _commit(staging, dst)
    finally:
        if staging.exists():
            staging.unlink(missing_ok=True)


def export(
    master: Path,
    out_dir: Path,
    basename: str,
    formats: list[str],
    tags: dict[str, str] | None = None,
    cover: Path | None = None,
    progress: Progress | None = None,
    sample_rate: int | None = None,
) -> list[dict[str, object]]:
    """Write `master` in every requested format into `out_dir`.

    Names are '<basename>.<ext>'; when two requested formats share an
    extension (e.g. AAC and ALAC → .m4a) the short format tag is appended:
    '<basename> [ALAC].m4a'. Existing files are never replaced. `cover`: the JPEG to
    embed; None = no picture (see `transcode`).
    """
    progress = progress or Progress.null()
    master, out_dir = Path(master), Path(out_dir)
    if not formats:
        raise AudioLabError('No output format selected.', hints=['Pick at least one format.'])
    specs = []
    for f in dict.fromkeys(formats):  # de-duplicate, keep order
        specs.append(resolve_format(f))
    ext_count: dict[str, int] = {}
    for s in specs:
        ext_count[s.ext] = ext_count.get(s.ext, 0) + 1
    base = sanitize_filename(basename, room=40)  # title + ' (Enhanced)', ' - Vocals' etc.
    duration = probe_media(master).get('duration_s')
    outputs = []
    n = len(specs)
    for i, spec in enumerate(specs):
        step = progress.sub(100.0 * i / n, 100.0 * (i + 1) / n)
        step(0, f'Saving {spec.label} ({i + 1}/{n})…')
        name = f'{base} [{spec.short}]{spec.ext}' if ext_count[spec.ext] > 1 else f'{base}{spec.ext}'
        path = transcode(
            master,
            out_dir / name,
            spec.key,
            tags=tags,
            cover=cover,
            progress=step,
            duration_s=duration,
            sample_rate=sample_rate,
        )
        outputs.append(
            {
                'format': spec.key,
                'label': spec.label,
                'name': path.name,
                'path': str(path),
                'size_bytes': path.stat().st_size,
                'kind': 'audio',
            }
        )
    progress(100)
    return outputs


def export_tracks(
    src: Path,
    tracks: list[dict[str, object]],
    folder: Path,
    fmt: str,
    tags: dict[str, str] | None = None,
    cover: Path | None = None,
    progress: Progress | None = None,
    sample_rate: int | None = None,
) -> list[dict[str, object]]:
    """Cut `src` into `tracks` ([{n, title, start_s, end_s}], see youtube.make_tracks) and
    save each one as 'NN - <title>.<ext>' in format `fmt`.

    `tags` describe the whole set: its title becomes the album, its artist the album
    artist; a track titled 'Artist - Track' gets its own artist. Tracks that start past
    the real end of the file are skipped and the last one always runs to the end (link
    durations are whole seconds). Existing files are never replaced. `cover`: the JPEG to
    embed in every track; None = no picture (see `transcode`)."""
    progress = progress or Progress.null()
    src, folder = Path(src), Path(folder)
    spec = resolve_format(fmt)
    dur = probe_media(src).get('duration_s')
    todo = [t for t in tracks if dur is None or float(t['start_s']) < dur - 0.05]
    total = len(todo)
    width = max(2, len(str(total)))
    set_tags = dict(tags or {})
    album = set_tags.pop('title', None) or set_tags.get('album')
    outputs = []
    for i, t in enumerate(todo):
        n = i + 1
        title = str(t.get('title') or f'Track {n}')
        start = float(t['start_s'])
        end = float(t['end_s']) if t.get('end_s') is not None else None
        if i == total - 1 and (end is None or dur is None or end >= dur - 1.0):
            end = None
        step = progress.sub(100.0 * i / total, 100.0 * (i + 1) / total)
        step(0, f'Saving track {n} of {total}…')
        m = re.match(r'^(.+?)\s+[-–—]\s+(.+)$', title)
        track_tags = {**set_tags, 'title': m.group(2) if m else title, 'album': album, 'track': f'{n}/{total}'}
        if m:
            track_tags['artist'] = m.group(1)
        if set_tags.get('artist'):
            track_tags['album_artist'] = set_tags['artist']
        name = f'{n:0{width}d} - {sanitize_filename(title, fallback=f"Track {n}")}{spec.ext}'
        path = transcode(
            src,
            folder / name,
            spec.key,
            tags=track_tags,
            cover=cover,
            progress=step,
            duration_s=dur,
            sample_rate=sample_rate,
            start_s=start,
            end_s=end,
        )
        outputs.append(
            {
                'format': spec.key,
                'label': f'{n:0{width}d} · {title.replace(" - ", " – ")}',
                'name': path.name,
                'path': str(path),
                'size_bytes': path.stat().st_size,
                'kind': 'audio',
                'group': 'tracks',
                'track': n,
                'title': title,
                'start_s': round(start, 3),
                'end_s': round(end if end is not None else (dur or start), 3),
            }
        )
    progress(100)
    return outputs
