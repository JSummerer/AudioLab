"""Download / Convert: save a link (or convert a file) as MP3, M4A, FLAC, WAV, Opus,
the untouched original stream, or an MP4 video — optionally also cut into tracks."""

from pathlib import Path
from typing import Any

from ..context import TaskContext
from ..errors import AudioLabError
from ..media import export, ffmpeg
from ..settings import SETTINGS
from ..util import sanitize_filename
from . import common

STAGES = [('acquire', 'Downloading', 70), ('convert', 'Converting', 25), ('save', 'Saving', 5)]
FILE_STAGES = [('acquire', 'Reading file', 5), ('convert', 'Converting', 90), ('save', 'Saving', 5)]
TRACKS_STAGE = ('tracks', 'Splitting into tracks', 30)

FORMATS = tuple(export.DOWNLOAD_FORMATS)  # mp3, m4a, opus, flac, wav, original, video

# Cutting needs re-encoding, so tracks of an untouched original/video are saved in the
# audio format closest to the source stream.
_TRACK_FORMAT_FOR_CODEC = {'opus': 'opus_160', 'vorbis': 'opus_160', 'aac': 'aac_256'}


def validate(options: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    default = SETTINGS.get().download_format
    if source.get('kind') == 'file' and default in ('original', 'video'):
        default = 'mp3'  # a file is converted: "keep the original" means nothing for it
    fmt = str(options.get('format') or default).lower()
    if fmt not in FORMATS:
        raise AudioLabError(f'Unknown format "{fmt}".')
    if source.get('kind') == 'file' and fmt in ('original', 'video'):
        raise AudioLabError('For files, pick a format to convert to (MP3, M4A, FLAC, WAV or Opus).')
    tracklist = common.clean_tracklist(options.get('tracklist'), source)
    return {
        'format': fmt,
        'embed_cover': bool(options.get('embed_cover', SETTINGS.get().embed_cover)),
        'save_cover': bool(options.get('save_cover')),
        # files have no chapters: they can only be split with a pasted tracklist
        'split_tracks': bool(options.get('split_tracks')) and (source.get('kind') == 'url' or bool(tracklist)),
        **({'tracklist': tracklist} if tracklist else {}),
    }


def stages(options: dict[str, Any], source: dict[str, Any]):
    plan = FILE_STAGES if source.get('kind') == 'file' else STAGES
    return plan + [TRACKS_STAGE] if options.get('split_tracks') else plan


def run(ctx: TaskContext, source: dict[str, Any]) -> dict[str, Any]:
    fmt = ctx.options['format']
    video = fmt == 'video'
    acq = common.acquire(ctx, source, ctx.stage('acquire'), task='download', video=video)
    out_root = common.output_root(acq)
    base = sanitize_filename(acq.title)
    split = ctx.options.get('split_tracks')
    untouched = fmt in ('original', 'video')
    # None: no picture at all — "Add cover art" off also keeps a converted file's own picture out
    cover = common.cover_art(ctx, acq) if split or not untouched else None

    p = ctx.stage('convert')
    src = acq.path
    if untouched:
        p(50, 'Keeping the original quality (no re-encoding)…')
        ctx.stage('save', 'Saving…')
        dst = src = export.place_file(acq.path, out_root / (base + acq.path.suffix.lower()), move=True)
        label = 'Video (MP4)' if video else f'Original ({acq.path.suffix[1:].upper()})'
        outputs = [{'path': str(dst), 'label': label, 'format': fmt, 'kind': 'video' if video else 'audio'}]
    else:
        key = export.DOWNLOAD_FORMATS[fmt]
        outputs = export.export(acq.path, out_root, base, [key], tags=common.tags_for(acq), cover=cover, progress=p)
        ctx.stage('save', 'Saving…')

    files = common.file_entries(outputs)
    f = files[0]
    headline = f'Saved as {f["label"]}'
    summary = [f'{headline} — {common.size_text(files)}.', f'Saved to {Path(f["path"]).parent}.']
    result: dict[str, Any] = {}
    if split:
        track_fmt = export.DOWNLOAD_FORMATS.get(fmt) or _TRACK_FORMAT_FOR_CODEC.get(
            ffmpeg.probe_media(src).get('audio_codec'), 'mp3_320'
        )
        tracks, folder = common.split_into_tracks(
            acq,
            src,
            out_root,
            f'{base} (Tracks)',
            track_fmt,
            common.tags_for(acq),
            cover,
            ctx.stage('tracks', 'Splitting into tracks…'),
            start=len(files),
        )
        summary.append(common.tracks_summary(acq, tracks, folder))
        if tracks and untouched:
            summary.append(
                f'The tracks are {export.FORMATS[track_fmt].label} — cutting needs re-encoding, '
                f'so only the full {"video" if video else "file"} keeps the original stream.'
            )
        if folder:
            files += tracks
            result['tracks_dir'] = str(folder)
            headline += f' + {len(tracks)} tracks'
    files += common.cover_file(ctx, source, acq, Path(f['path']).parent, start=len(files))
    result.update(
        {
            'headline': headline,
            'output_dir': str(Path(f['path']).parent),
            'files': files,
            'cover_source': common.cover_source(source),
            'previews': [{'key': 'file:f0', 'label': 'Play'}] if f['playable'] else [],
            'summary': summary,
            'warnings': [],
            '_library': common.library_hints(acq),
        }
    )
    return result
