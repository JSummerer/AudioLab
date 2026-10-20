"""Helpers shared by all tasks: get the audio, decode it, publish results.

Conventions for task work folders (`ctx.work_dir`):
    scratch/    big intermediates (download, reference FLAC, stems) — deleted after success
    previews/   small files the UI plays (kept with the job)
"""

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..context import Progress, TaskContext
from ..errors import AudioLabError, storage_error
from ..media import export, ffmpeg, youtube
from ..resources import preflight
from ..runtime import is_packaged
from ..settings import SETTINGS, validate_output_location
from ..util import PLAYABLE_EXT, human_bytes, human_duration, sanitize_filename, unique_path


@dataclass
class Acquired:
    path: Path  # local media file (downloaded or uploaded)
    title: str  # clean display title
    artist: str | None = None
    duration_s: float | None = None
    url: str | None = None
    thumbnail: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # classifier hints (title, tags, …)
    playlist_title: str | None = None
    task: str = ''  # for re-running the preflight once the duration is known
    quality: str = 'standard'
    cover: Path | None = None  # the user's own cover image (a prepared JPEG in the job's input/)


def scratch_dir(ctx: TaskContext) -> Path:
    p = ctx.work_dir / 'scratch'
    p.mkdir(parents=True, exist_ok=True)
    return p


def previews_dir(ctx: TaskContext) -> Path:
    p = ctx.work_dir / 'previews'
    p.mkdir(parents=True, exist_ok=True)
    return p


def ai_device() -> str | None:
    try:
        from ..ai import ai_status

        st = ai_status()
        return st.get('device') if st.get('available') else None
    except Exception:
        return None


def probe_url(source: dict[str, Any], progress: Progress | None = None) -> dict[str, Any]:
    """The link's lookup: the one the job was started with (`source._probe`), else a new one — kept
    in the source, which also gets the title, thumbnail and length it lacked (a job started without a
    title then takes this one: JobManager._on_state)."""
    meta = source.get('_probe')
    if not meta:
        meta = youtube.probe(source['url'], progress=progress)
        source['_probe'] = meta
        for key in ('title', 'thumbnail', 'duration_s'):
            if meta.get(key) and not source.get(key) and meta.get('kind') == 'single':
                source[key] = meta[key]
    if meta.get('kind') == 'playlist':
        raise AudioLabError(
            'This link is a playlist. Pick the videos you want from the list first.',
            hints=['Paste the link again in AudioLab and choose items from the playlist.'],
        )
    return meta


def acquire(
    ctx: TaskContext,
    source: dict[str, Any],
    progress: Progress,
    task: str,
    quality: str = 'standard',
    video: bool = False,
) -> Acquired:
    """Download (URL) or locate (file) the source, then apply what the user gave: title/artist
    overrides (`options.meta`) used for file names and tags, an own cover image (`source._cover`)
    and a pasted tracklist (`options.tracklist`, replaces chapters / the description's list —
    checked again now that the real length is known)."""
    acq = _acquire(ctx, source, progress, task, quality, video)
    meta = ctx.options.get('meta') or {}
    if meta.get('title'):
        acq.title = meta['title']
    if 'artist' in meta:
        acq.artist = meta['artist'] or None
        acq.metadata['artist_cleared'] = not meta['artist']
    if source.get('_cover') and Path(source['_cover']).is_file():
        acq.cover = Path(source['_cover'])
    if ctx.options.get('tracklist'):
        tracks, errors = youtube.parse_tracklist(ctx.options['tracklist'], acq.duration_s)
        if errors:
            raise AudioLabError(
                f"The tracklist doesn't fit this recording — {youtube.tracklist_problem(errors)}.",
                hints=['Fix those lines and start again.'],
            )
        acq.metadata.update(tracks=tracks, tracks_source='pasted')
    return acq


def clean_tracklist(raw: Any, source: dict[str, Any]) -> str:
    """Validate the `tracklist` option (text the user pasted): '' when there is none; lines the
    parser can't use → AudioLabError naming them. The length is only known here for links."""
    if raw is None or raw == '':
        return ''
    if not isinstance(raw, str):
        raise AudioLabError('The tracklist must be text.')
    text = raw.strip()
    if len(text) > youtube.TRACKLIST_MAX_CHARS:
        raise AudioLabError('The tracklist is too long.', hints=['Paste one line per track.'])
    if text:
        _, errors = youtube.parse_tracklist(text, source.get('duration_s'))
        if errors:
            raise AudioLabError(
                f'The tracklist has a problem — {youtube.tracklist_problem(errors)}.',
                hints=['Fix those lines (one track per line, its start time anywhere on the line).'],
            )
    return text


def _acquire(
    ctx: TaskContext, source: dict[str, Any], progress: Progress, task: str, quality: str, video: bool
) -> Acquired:
    """Download (URL) or locate (file) the source, running the resource preflight
    as early as the duration is known — before any big download starts."""
    device = ai_device()
    if source.get('kind') == 'url':
        try:
            return _acquire_url(ctx, source, progress, task, quality, video, device)
        except AudioLabError as e:
            if is_packaged() or e.fix != 'update_downloader' or not SETTINGS.get().auto_update_downloader:
                raise
            progress(2, 'Updating the downloader and trying again…')
            update = youtube.self_repair_update()
            if not update:
                raise
            ctx.note('decision', f'Updated the downloader {update[0]} → {update[1]} and tried again')
        try:
            return _acquire_url(ctx, source, progress, task, quality, video, device)
        except AudioLabError as e:
            if e.fix == 'update_downloader':  # the update was just done — don't suggest it again
                e.fix = 'retry'
                e.hints = [f'AudioLab already updated the downloader (to {update[1]}) and tried again.'] + [
                    h for h in e.hints if h != youtube._UPDATE_HINT
                ]
            raise

    path = Path(source.get('path') or '')
    if not path.is_file():
        raise AudioLabError(
            'The uploaded file is no longer available.', hints=['Add the file again and start a new job.']
        )
    if path.stat().st_size == 0:
        raise AudioLabError(
            'This file is empty (0 bytes).', hints=['The upload or copy was probably interrupted — add the file again.']
        )
    progress(10, 'Reading the file…')
    info = ffmpeg.probe_media(path)
    if not info.get('has_audio'):
        if info.get('has_video'):
            raise AudioLabError(
                'This video has no sound, so there is nothing to work with.',
                hints=['Check that it plays with sound in a media player.'],
            )
        raise AudioLabError(
            "AudioLab can't find any audio in this file — it may be damaged, or it is not an audio or video file.",
            hints=[
                'Check that it plays in a media player.',
                'Most audio and video formats work: MP3, WAV, FLAC, M4A, OGG, MP4, MKV, WEBM…',
            ],
        )
    duration = info.get('duration_s')
    ctx.note(
        'decision',
        'Resource check passed',
        preflight(duration, ctx.work_dir, task, quality, device, SETTINGS.output_dir),
    )
    name = source.get('name') or path.name
    clean = youtube.clean_title(Path(name).stem)
    artist, _ = youtube.split_artist_title(clean, None)
    tags = info.get('tags') or {}
    # The file's own tags win; the file name is only the fallback ('track01.wav' says little).
    title = tags.get('title') if not _PLACEHOLDER_TITLE.match(tags.get('title') or '') else clean
    artist = tags.get('artist') or artist or None
    hints = ' – '.join(dict.fromkeys(x for x in (artist, title, tags.get('album'), clean) if x))
    progress(100)
    return Acquired(
        path=path,
        title=title,
        artist=artist,
        duration_s=duration,
        metadata={
            'title': title,
            'raw_title': hints,
            'duration_s': duration,
            'file_tags': tags,
            'has_cover': bool(info.get('has_cover') and not info.get('has_video')),
        },
        task=task,
        quality=quality,
    )


def reference_path(ctx: TaskContext) -> Path:
    return scratch_dir(ctx) / 'reference.wav'


def decode(ctx: TaskContext, acq: Acquired, progress: Progress) -> dict[str, Any]:
    """Decode once to the 48 kHz float reference every analysis works from."""
    ref = reference_path(ctx)
    try:  # what the source really is (the reference is always 48 kHz stereo float)
        src = ffmpeg.probe_media(acq.path)
        acq.metadata['source_audio'] = {
            k: src.get(k) for k in ('sample_rate', 'channels', 'audio_codec', 'bitrate_kbps')
        }
    except Exception:
        pass
    info = ffmpeg.decode_reference(acq.path, ref, progress=progress, duration_s=acq.duration_s)
    if not acq.duration_s:
        # Duration was unknown before decoding: the size/memory check only happens now.
        acq.duration_s = info.get('duration_s')
        ctx.note(
            'decision',
            'Resource check passed (after decoding)',
            preflight(
                acq.duration_s, ctx.work_dir, acq.task or 'enhance', acq.quality, ai_device(), SETTINGS.output_dir
            ),
        )
    return info


def source_format(acq: Acquired) -> dict[str, Any]:
    """`analyze_file` kwargs describing the source's own format (probed once in `decode`;
    the working reference is always 48 kHz stereo)."""
    src = (acq.metadata or {}).get('source_audio') or {}
    out = {'source_rate': src.get('sample_rate'), 'source_channels': src.get('channels')}
    return {k: v for k, v in out.items() if v}


def describe_source(analysis: dict[str, Any], acq: Acquired) -> dict[str, Any]:
    """Add the source's codec / bitrate to the analysis (sample rate and channels come
    from `analyze_file(**source_format(acq))`)."""
    src = (acq.metadata or {}).get('source_audio') or {}
    for key in ('audio_codec', 'bitrate_kbps'):
        if src.get(key):
            analysis[f'source_{key}'] = src[key]
    return analysis


def cover_art(ctx: TaskContext, acq: Acquired) -> Path | None:
    """The picture to embed, always a JPEG ≤ export.COVER_MAX_PX: the user's own (always used),
    else — with `embed_cover` on — the link's thumbnail or the cover an uploaded file carries
    (Enhance exports from a decoded master, which has none of its own)."""
    if acq.cover:
        return acq.cover
    if not ctx.options.get('embed_cover', SETTINGS.get().embed_cover):
        return None
    if acq.thumbnail:
        return youtube.download_thumbnail(acq.thumbnail, scratch_dir(ctx) / 'cover.jpg', progress=ctx.progress)
    if acq.metadata.get('has_cover'):
        try:
            return export.cover_jpeg(acq.path, scratch_dir(ctx) / 'cover.jpg')
        except AudioLabError as e:  # a broken picture must not cost the audio
            ctx.note('warning', f'Could not take over the cover image: {e}')
    return None


def cover_source(source: dict[str, Any]) -> str | None:
    """Where the job's cover comes from (the rule of `cover_art`): 'own' (the user's image), 'link'
    (the link's thumbnail) or 'file' (the picture an uploaded file carries); None when it has none."""
    if _own_cover(source):
        return 'own'
    if _thumbnail(source):
        return 'link'
    path = Path(source.get('path') or '')
    if source.get('kind') == 'file' and path.is_file():
        info = ffmpeg.probe_media(path)
        if info.get('has_cover') and not info.get('has_video'):
            return 'file'
    return None


def _own_cover(source: dict[str, Any]) -> Path | None:
    """The own image as uploaded (older jobs kept only the JPEG that gets embedded)."""
    for key in ('_cover_original', '_cover'):
        p = Path(source.get(key) or '')
        if p.is_file():
            return p
    return None


def _thumbnail(source: dict[str, Any]) -> str | None:
    return source.get('thumbnail') or (source.get('_probe') or {}).get('thumbnail')


def save_cover(
    source: dict[str, Any], title: str, folder: Path, start: int, progress: Progress | None = None
) -> dict[str, Any] | None:
    """The job's cover as a picture next to its outputs, '<Title> - cover.jpg' (.png for a PNG), at
    full size — not the downscaled copy that gets embedded: the own image as uploaded, else the link's
    thumbnail (WebP → JPEG), else the picture an uploaded file carries. Returns its Result.files entry
    (id f<start>, kind 'cover'), None when there is no cover; AudioLabError when it can't be had."""
    progress = progress or Progress.null()
    progress.check_cancel()
    kind = cover_source(source)
    if kind is None:
        return None
    stem = f'{sanitize_filename(title, "Cover")} - cover'
    with tempfile.TemporaryDirectory(prefix='.audiolab-cover-') as tmp:
        if kind == 'own':
            pic = _own_cover(source)
        elif kind == 'link':
            pic = youtube.fetch_thumbnail(_thumbnail(source), Path(tmp) / 'thumbnail.src', progress=progress)
        else:  # the picture as the file carries it; one that isn't a JPEG/PNG/WebP becomes a JPEG
            pic = Path(tmp) / 'embedded.src'
            media = ['-i', source['path'], '-map', '0:v:0', '-frames:v', '1']
            ffmpeg.run_ffmpeg(
                [*media, '-c:v', 'copy', '-f', 'image2', '-update', '1', str(pic)], progress=progress, what='cover copy'
            )
            if export.picture_size(pic) is None:
                pic = export.cover_jpeg(source['path'], Path(tmp) / 'embedded.jpg', max_px=None)
        progress.check_cancel()
        path = export.save_picture(pic, folder, stem)
    entry = file_entries(
        [{'path': str(path), 'label': 'Cover image', 'format': path.suffix[1:], 'kind': 'cover'}], start
    )
    return entry[0]


def cover_file(ctx: TaskContext, source: dict[str, Any], acq: Acquired, folder: Path, start: int) -> list:
    """`save_cover` at the end of a job: [its Result.files entry], or [] — no cover, or one that
    can't be had, is a note in the journal, never a failure."""
    if not ctx.options.get('save_cover'):
        return []
    try:
        entry = save_cover(source, acq.title, folder, start, progress=ctx.progress)
    except AudioLabError as e:
        ctx.note('warning', f'Could not save the cover image: {e}')
        return []
    if entry is None:
        ctx.note('info', 'No cover image to save — this recording has none.')
        return []
    return [entry]


def library_hints(acq: Acquired, keep_original: bool = False) -> dict[str, Any]:
    """The `_library` result key: what the Library entry needs beyond the result (library.record).
    `keep_original`: keep the acquired file as it is (Enhance) to compare results with it."""
    hints = {
        'title': acq.title,
        'artist': acq.artist,
        'duration_s': acq.duration_s,
        'markers': acq.metadata.get('tracks') or [],
        'markers_source': acq.metadata.get('tracks_source'),
    }
    if keep_original:
        hints['original'] = str(acq.path)  # the download or the upload — never the decoded reference
        hints['original_codec'] = (acq.metadata.get('source_audio') or {}).get('audio_codec')
    return hints


def tags_for(acq: Acquired, suffix: str = '') -> dict[str, str]:
    tags = {'title': acq.title + suffix}
    if acq.artist:
        tags['artist'] = acq.artist
    elif acq.metadata.get('artist_cleared'):
        tags['artist'] = ''  # explicitly removed: don't inherit the source's
    file_tags = acq.metadata.get('file_tags') or {}
    for key in ('album', 'track', 'date'):  # a converted file keeps its own tags
        if file_tags.get(key):
            tags[key] = file_tags[key]
    if acq.playlist_title:
        tags['album'] = acq.playlist_title
    if acq.url:
        tags['comment'] = acq.url
    return tags


def output_folder(name: str, parent: Path | None = None) -> Path:
    """A new, uniquely named folder inside the output folder."""
    root = Path(parent or SETTINGS.output_dir)
    validate_output_location(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        folder = unique_path(root / sanitize_filename(name, room=40), folder=True)
        folder.mkdir(parents=True)
        return folder
    except OSError as e:
        raise storage_error(e) or AudioLabError(
            f'Could not create a folder in {root}: {e.strerror or e}.',
            hints=[
                'Choose a different output folder in Settings → Files.',
                'Check that the folder exists and you are allowed to write to it.',
            ],
        ) from e


def output_root(acq: Acquired) -> Path:
    root = SETTINGS.output_dir
    validate_output_location(root)
    if acq.playlist_title:
        root = root / sanitize_filename(acq.playlist_title)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise storage_error(e) or AudioLabError(
            f'Could not write to {root}: {e.strerror or e}.',
            hints=['Choose a different output folder in Settings → Files.'],
        ) from e
    return root


def file_entries(outputs: list[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    """Normalise export() outputs into Result.files entries (ids f0, f1, …)."""
    files = []
    for i, o in enumerate(outputs, start=start):
        path = Path(o['path'])
        files.append(
            {
                'id': f'f{i}',
                'name': path.name,
                'label': o.get('label') or path.suffix[1:].upper(),
                'format': o.get('format'),
                'kind': o.get('kind', 'audio'),
                'size_bytes': path.stat().st_size if path.exists() else o.get('size_bytes', 0),
                'playable': path.suffix.lower() in PLAYABLE_EXT,
                'path': str(path),
            }
        )
    return files


def size_text(files: list[dict[str, Any]]) -> str:
    return human_bytes(sum(f.get('size_bytes', 0) for f in files))


def _acquire_url(
    ctx: TaskContext,
    source: dict[str, Any],
    progress: Progress,
    task: str,
    quality: str,
    video: bool,
    device: str | None,
) -> Acquired:
    progress(2, 'Looking up the link…')
    meta = probe_url(source, progress.sub(0, 5))
    if meta.get('is_live'):
        raise AudioLabError(
            'This is a live stream that is still running — it can only be saved once it has ended.',
            hints=['Try again after the stream is over.'],
        )
    duration = meta.get('duration_s')
    ctx.note(
        'decision',
        'Resource check passed',
        preflight(duration, ctx.work_dir, task, quality, device, SETTINGS.output_dir),
    )
    dest = scratch_dir(ctx) / 'download'
    dl = progress.sub(5, 100)
    path = (youtube.download_video if video else youtube.download_audio)(source['url'], dest, progress=dl)
    artist, _ = youtube.link_names(meta)  # the probe card's rule: the card and the files agree
    metadata = {
        k: meta.get(k)
        for k in (
            'title',
            'raw_title',
            'uploader',
            'tags',
            'categories',
            'description',
            'chapters',
            'duration_s',
            'extractor',
            'tracks',
            'tracks_source',
        )
    }
    if 'tracks' not in meta:  # probed by an older version (cached probe, re-run job)
        metadata['tracks'], metadata['tracks_source'] = youtube.make_tracks(
            meta.get('chapters'), meta.get('description'), duration
        )
    return Acquired(
        path=path,
        title=meta.get('title') or path.stem,
        artist=artist,
        duration_s=duration,
        url=source['url'],
        thumbnail=meta.get('thumbnail'),
        metadata=metadata,
        playlist_title=source.get('playlist_title'),
        task=task,
        quality=quality,
    )


def split_into_tracks(
    acq: Acquired,
    src: Path,
    parent: Path,
    folder_name: str,
    fmt: str,
    tags: dict[str, str],
    cover: Path | None,
    progress: Progress,
    start: int,
    sample_rate: int | None = None,
) -> tuple[list[dict[str, Any]], Path | None]:
    """`split_tracks`: cut `src` into the link's tracks (chapters or the description's
    tracklist) inside a new folder `folder_name` under `parent`. Returns Result.files
    entries (ids from f<start>, with group/track/start_s/end_s) and the folder —
    ([], None) when the source has no usable tracklist."""
    tracks = acq.metadata.get('tracks') or []
    if not tracks:
        progress(100)
        return [], None
    folder = output_folder(folder_name, parent=parent)
    outputs = export.export_tracks(
        src, tracks, folder, fmt, tags=tags, cover=cover, progress=progress, sample_rate=sample_rate
    )
    if not outputs:
        folder.rmdir()
        progress(100)
        return [], None
    files = file_entries(outputs, start=start)
    for f, o in zip(files, outputs):
        f.update({k: o[k] for k in ('group', 'track', 'start_s', 'end_s')})
    return files, folder


def tracks_summary(acq: Acquired, files: list[dict[str, Any]], folder: Path | None) -> str:
    if not files:
        return 'No tracklist found — saved as one file.'
    where = {
        'chapters': "the video's chapters",
        'pasted': 'your tracklist',
    }.get(acq.metadata.get('tracks_source'), 'the tracklist in the description')
    line = f'Split into {len(files)} tracks using {where} — saved to {folder}.'
    first = float(files[0].get('start_s') or 0)
    if first > 0:  # a pasted list may start late: nothing is invented for the part before it
        line += f' The first {human_duration(first)} before track 1 are only in the full file.'
    return line


# Embedded titles that say nothing (CD rips without a lookup, exports): use the file name instead.
_PLACEHOLDER_TITLE = re.compile(r'^\s*(?:track\s*\d*|untitled|unknown|audio(?: track)?|\d*)\s*$', re.IGNORECASE)
