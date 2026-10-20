"""Job routes: probe a link, check a pasted tracklist, create jobs (link, playlist items, upload,
re-run of an earlier job; optionally with an own cover image), list / inspect / cancel / retry /
delete them, serve their files (one file, a ZIP of all, inline media and decoded excerpt chunks for the
players) and save a finished job's cover into its folder — see docs/API.md."""

import json
import math
import re
import shutil
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import APP_NAME, library, tasks
from ..errors import AudioLabError
from ..jobs import Job, JobManager, new_job_id, public_file
from ..media import export, ffmpeg, youtube
from ..settings import LIMITS
from ..tasks import common
from ..util import link_or_copy, media_type, sanitize_filename
from .security import COVER_MAX_MB
from .state import AppState, State

COVER_TASKS = ('download', 'enhance')
COVER_EXT = {'jpeg': '.jpg', 'png': '.png', 'webp': '.webp'}

router = APIRouter()


# ----------------------------------------------------------------- probe
@router.post('/api/probe')
async def probe(state: State, body: dict[str, Any] = Body(...)):
    url = str((body or {}).get('url') or '').strip()
    if not youtube.is_url(url):
        raise HTTPException(400, 'That does not look like a web link. Paste a full link starting with https://')
    hit = state.cached_probe(url)
    if hit is None:
        hit = await run_in_threadpool(state.probe_with_repair, url)
        state.remember_probe(url, hit)
    # `tracks` covers chapters; yt-dlp's music fields only feed `artist`
    result = {k: v for k, v in hit.items() if k not in ('description', 'chapters', 'music_track', 'music_artists')}
    if hit.get('kind') == 'single':
        from ..analysis.classify import classify

        guess = classify(None, hit.get('duration_s') or 0, hit)
        result['guess'] = {k: guess.get(k) for k in ('type', 'label', 'confidence', 'confidence_label', 'reasons')}
    return result


@router.post('/api/tracklist')
def tracklist_preview(body: dict[str, Any] = Body(...)):
    text = (body or {}).get('text')
    if not isinstance(text, str):
        raise HTTPException(400, 'text must be the tracklist as a string.')
    if len(text) > youtube.TRACKLIST_MAX_CHARS:
        raise HTTPException(400, 'The tracklist is too long.')
    try:
        duration = float(body.get('duration_s', body.get('duration')) or 0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, 'duration_s must be a number of seconds.') from None
    duration = duration or None
    tracks, errors = youtube.parse_tracklist(text, duration)
    out = []
    for t in tracks:  # the title as the track's tags will have it: "Artist - Title" is split
        m = re.match(r'^(.+?)\s+[-–—]\s+(.+)$', t['title'])
        out.append(
            {
                'n': t['n'],
                'start_s': t['start_s'],
                'end_s': t['end_s'],
                'title': m.group(2) if m else t['title'],
                'artist': m.group(1) if m else None,
            }
        )
    return {'tracks': out, 'errors': errors}


# ---------------------------------------------------------------- create
def _source_from_url(state: AppState, url: str, given: dict[str, Any] | None = None) -> dict[str, Any]:
    """A link's source, with what the lookup said (probe cache). `given`: title / duration_s /
    playlist_title the UI sent along — used when the cache has expired (a playlist picked long after
    it was looked up), so the job keeps its name, playlist folder and album tag."""
    if not youtube.is_url(url):
        raise HTTPException(400, f'Not a web link: {url[:80]}')
    src: dict[str, Any] = {'kind': 'url', 'url': url}
    hit = state.cached_probe(url)
    if hit and hit.get('kind') == 'single':
        src.update(title=hit.get('title'), thumbnail=hit.get('thumbnail'), duration_s=hit.get('duration_s'), _probe=hit)
    entry = state.cached_probe(f'entry:{url}')
    if entry:
        src.setdefault('title', entry.get('title'))
        src.setdefault('duration_s', entry.get('duration_s'))
        src['playlist_title'] = entry.get('playlist_title')
    for key, value in (given or {}).items():
        if value and not src.get(key):
            src[key] = value
    return src


def _text(value: Any) -> str | None:
    """A title the UI sent: one line, at most 200 characters; None when there is none."""
    if not isinstance(value, str):
        return None
    return ' '.join(value.split())[:200] or None


def _url_items(urls: str) -> list[tuple[str, dict[str, Any]]]:
    """The `urls` field: a JSON list of links, each a string or {"url", "title", "duration_s"}."""
    try:
        raw = json.loads(urls)
        if not isinstance(raw, list):
            raise ValueError
        items = []
        for item in raw:
            if isinstance(item, str):
                items.append((item, {}))
                continue
            if not isinstance(item, dict) or not isinstance(item.get('url'), str):
                raise ValueError
            duration = item.get('duration_s')
            ok = isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 < duration < math.inf
            items.append((item['url'], {'title': _text(item.get('title')), 'duration_s': duration if ok else None}))
    except (ValueError, TypeError):
        raise HTTPException(400, 'urls must be a JSON list of links.') from None
    return items


def _clone_source(state: AppState, job_id: str, new_id: str) -> dict[str, Any]:
    old = state.job_or_404(job_id)
    src = {k: v for k, v in old.source.items() if k != '_probe'}  # a link is looked up again when it runs
    if src.get('kind') == 'file':
        old_path = Path(src.get('path') or '')
        if not old_path.is_file():
            raise HTTPException(410, 'The original file of that job is gone — add it again.')
        src['path'] = str(link_or_copy(old_path, state.manager.input_dir(new_id) / old_path.name))
    if src.get('_cover'):  # the own cover image comes along (a new upload replaces it)
        old_cover = Path(src['_cover'])
        if old_cover.is_file():
            src['_cover'] = str(link_or_copy(old_cover, _cover_dir(state.manager, new_id) / 'cover.jpg'))
        else:
            src.pop('_cover')
            src.pop('cover_name', None)
    old_original = Path(src.pop('_cover_original', None) or '')  # before 2.4: input/cover-original.<ext>
    if src.get('_cover') and old_original.is_file():
        name = 'original' + old_original.suffix.lower()
        src['_cover_original'] = str(link_or_copy(old_original, _cover_dir(state.manager, new_id) / name))
    return src


def _cover_dir(mgr: JobManager, job_id: str) -> Path:
    """Where a job's own cover image lives: input/cover/, apart from the uploaded file (whatever its name)."""
    p = mgr.input_dir(job_id) / 'cover'
    p.mkdir(exist_ok=True)
    return p


async def _save_upload(mgr: JobManager, file: UploadFile, job_id: str) -> dict[str, Any]:
    raw_name = Path(file.filename or '').name
    ext = Path(raw_name).suffix.lower()
    if ext not in export.UPLOAD_EXT:
        raise HTTPException(
            400,
            f'"{ext or raw_name}" files are not supported. Use an audio or video file '
            f'(MP3, WAV, FLAC, M4A, AAC, OGG, OPUS, AIFF, MP4, MOV, MKV…).',
        )
    dst = mgr.input_dir(job_id) / (sanitize_filename(Path(raw_name).stem, 'upload') + ext)
    limit = int(LIMITS.max_upload_mb * 1024 * 1024)
    written = 0
    try:
        with open(dst, 'wb') as f:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f'The file is larger than the {int(LIMITS.max_upload_mb)} MB limit.')
                f.write(chunk)
    except BaseException:
        shutil.rmtree(dst.parent.parent, ignore_errors=True)
        raise
    return {'kind': 'file', 'name': raw_name, 'path': str(dst), 'size_bytes': written, 'title': Path(raw_name).stem}


async def _save_cover(mgr: JobManager, cover: UploadFile, job_id: str) -> tuple[Path, Path]:
    """Store an own cover image in the job's input/cover/ (kept for Retry and re-runs), checked and
    converted to the JPEG that gets embedded — and the picture as uploaded, for `save_cover`.
    Returns (embedded JPEG, original). The caller cleans up the job folder on errors."""
    ext = Path(cover.filename or '').suffix.lower()
    if ext not in export.COVER_UPLOAD_EXT:
        raise HTTPException(400, 'Use a JPG, PNG or WebP picture as the cover.')
    folder = _cover_dir(mgr, job_id)
    raw = folder / f'.upload{ext}'
    written = 0
    with open(raw, 'wb') as f:
        while chunk := await cover.read(1 << 20):
            written += len(chunk)
            if written > COVER_MAX_MB * 1024 * 1024:
                raise HTTPException(413, f'The cover image is larger than {COVER_MAX_MB} MB.')
            f.write(chunk)

    def convert() -> tuple[Path, Path]:
        new = folder / '.new.jpg'  # then replace: cover.jpg may be a link to another job's
        try:
            export.prepare_cover(raw, new)
            for old in folder.glob('original.*'):  # a re-run's own, replaced (only a link)
                old.unlink()
            original = raw.rename(folder / f'original{COVER_EXT[export.picture_size(raw)[0]]}')
            return link_or_copy(new, folder / 'cover.jpg'), original
        finally:
            raw.unlink(missing_ok=True)
            new.unlink(missing_ok=True)

    return await run_in_threadpool(convert)


@router.post('/api/jobs')
async def create_job(
    state: State,
    task: str = Form(...),
    options: str = Form('{}'),
    url: str | None = Form(None),
    urls: str | None = Form(None),
    playlist_title: str | None = Form(None),
    source_job: str | None = Form(None),
    file: UploadFile | None = File(None),
    cover: UploadFile | None = File(None),
):
    mgr = state.manager
    tasks.get(task)  # unknown task → 400 before any upload is stored
    try:
        opts = json.loads(options or '{}')
        if not isinstance(opts, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, 'options must be a JSON object.') from None
    if cover is not None and cover.filename and task not in COVER_TASKS:
        raise HTTPException(400, 'A cover image can only be added when saving or enhancing.')

    pending: list[tuple] = []  # (job_id, source)
    if file is not None and file.filename:
        jid = new_job_id()
        pending.append((jid, await _save_upload(mgr, file, jid)))
    elif source_job:
        jid = new_job_id()
        pending.append((jid, _clone_source(state, source_job, jid)))
    elif urls:
        items = _url_items(urls)
        if not items:
            raise HTTPException(400, 'Select at least one item.')
        if len(items) > LIMITS.max_playlist_items:
            raise HTTPException(400, f'Too many items at once (limit {LIMITS.max_playlist_items}).')
        album = _text(playlist_title)
        pending = [
            (new_job_id(), _source_from_url(state, u.strip(), {**given, 'playlist_title': album})) for u, given in items
        ]
    elif url:
        pending.append((new_job_id(), _source_from_url(state, url.strip())))
    else:
        raise HTTPException(400, 'Paste a link or choose a file first.')

    if len(pending) > 1:  # one title or picture for many playlist items would make them all the same
        opts.pop('meta', None)
        cover = None
    created = []
    try:
        if cover is not None and cover.filename:
            jid, src = pending[0]
            embedded, original = await _save_cover(mgr, cover, jid)
            src.update(_cover=str(embedded), _cover_original=str(original), cover_name=Path(cover.filename).name)
        prepared = await run_in_threadpool(
            lambda: [(jid, src, tasks.validate(task, opts, src)) for jid, src in pending]
        )
    except Exception as e:
        for jid, _ in pending:  # none of them was submitted: their folders hold only this request's files
            shutil.rmtree(mgr.job_dir(jid), ignore_errors=True)
        if isinstance(e, (AudioLabError, HTTPException, OSError)):  # OSError: a full disk… (api/app.py)
            raise
        raise HTTPException(400, 'Some of the chosen options are not valid.') from e
    for jid, src, norm in prepared:
        title = (norm.get('meta') or {}).get('title') or src.get('title') or ''
        job = mgr.submit(task, src, norm, title=title, job_id=jid)
        created.append(mgr.public(job.id))
    return {'jobs': created}


# ------------------------------------------------------------- the queue
@router.get('/api/jobs')
def list_jobs(state: State):
    return {'jobs': state.manager.list()}


@router.delete('/api/jobs')
def clear_jobs(state: State, scope: str = 'finished'):
    if scope != 'finished':
        raise HTTPException(400, 'Only scope=finished is supported.')
    state.forget('storage')
    return {'deleted': state.manager.clear_finished()}


@router.get('/api/jobs/{job_id}')
def get_job(state: State, job_id: str):
    state.job_or_404(job_id)
    return state.manager.public(job_id)


@router.get('/api/jobs/{job_id}/journal')
def get_journal(state: State, job_id: str):
    state.job_or_404(job_id)
    return {'events': state.manager.journal(job_id)}


@router.post('/api/jobs/{job_id}/cancel')
def cancel_job(state: State, job_id: str):
    state.job_or_404(job_id)
    state.manager.cancel(job_id)
    return state.manager.public(job_id)


@router.post('/api/jobs/{job_id}/retry')
def retry_job(state: State, job_id: str):
    job = state.job_or_404(job_id)
    if job.status not in ('failed', 'cancelled'):
        raise HTTPException(409, 'Only failed or cancelled jobs can be retried.')
    state.manager.retry(job_id)
    return state.manager.public(job_id)


@router.delete('/api/jobs/{job_id}')
def delete_job(state: State, job_id: str):
    state.job_or_404(job_id)
    state.manager.delete(job_id)
    state.forget('storage')
    return {'deleted': job_id}


# ----------------------------------------------------------------- files
def _result_file(job: Job, file_id: str) -> Path:
    for f in (job.result or {}).get('files', []):
        if f.get('id') == file_id:
            p = Path(f.get('path') or '')
            if p.is_file():
                return p
            raise HTTPException(410, 'This file was moved or deleted.')
    raise HTTPException(404, 'File not found.')


def _attachment(name: str) -> str:
    fallback = name.encode('ascii', 'replace').decode().replace('"', "'").replace('?', '_')
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(name)}'


def serve_file(path: Path, download_name: str | None = None) -> FileResponse:
    """A file for a player (inline) or as a download (`download_name`), with the media type the
    players expect. Starlette answers Range requests (206) — seeking in long files needs that."""
    if download_name:
        return FileResponse(
            path, media_type=media_type(path), headers={'Content-Disposition': _attachment(download_name)}
        )
    return FileResponse(path, media_type=media_type(path), content_disposition_type='inline')


PCM_MAX_FRAMES = 20 * ffmpeg.PCM_RATE  # one request: at most 20 s


def serve_pcm(path: Path, offset: int, frames: int, stop: threading.Event | None = None) -> Response:
    """A chunk of a file as raw 48 kHz stereo float32 for the compare player (see `ffmpeg.decode_pcm`)."""
    grid = ffmpeg.PCM_GRID
    if offset < 0 or not 0 < frames <= PCM_MAX_FRAMES or offset % grid or frames % grid:
        raise HTTPException(400, f'offset and frames must be multiples of {grid}, frames at most {PCM_MAX_FRAMES}.')
    data = ffmpeg.decode_pcm(path, offset, frames, stop)
    return Response(data, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})


class _ChunkSink:
    """Write-only file object for zipfile that hands the written bytes to a generator."""

    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, b) -> int:
        self.chunks.append(bytes(b))
        return len(b)

    def flush(self) -> None:
        pass

    def drain(self) -> Iterator[bytes]:
        if self.chunks:
            data, self.chunks = b''.join(self.chunks), []
            yield data


def _zip_stream(files: list[dict[str, Any]]) -> Iterator[bytes]:
    """Stream a ZIP of the given result files without a temporary copy on disk.
    Stored, not compressed: audio formats are already compressed (or huge WAVs)."""
    sink = _ChunkSink()
    used = set()
    with zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for f in files:
            src = Path(f['path'])
            arc = f'Tracks/{src.name}' if f.get('group') == 'tracks' else src.name
            stem, n = arc, 2
            while arc in used:
                arc = f'{Path(stem).with_suffix("")} ({n}){src.suffix}'
                n += 1
            used.add(arc)
            info = zipfile.ZipInfo.from_file(src, arc)
            with open(src, 'rb') as fh, zf.open(info, 'w', force_zip64=True) as dst:
                while chunk := fh.read(1 << 20):
                    dst.write(chunk)
                    yield from sink.drain()
            yield from sink.drain()
    yield from sink.drain()


@router.get('/api/jobs/{job_id}/files/{file_id}')
def download_file(state: State, job_id: str, file_id: str):
    p = _result_file(state.job_or_404(job_id), file_id)
    return serve_file(p, p.name)


@router.get('/api/jobs/{job_id}/zip')
def download_zip(state: State, job_id: str):
    job = state.job_or_404(job_id)
    files = [f for f in (job.result or {}).get('files', []) if f.get('path') and Path(f['path']).is_file()]
    if job.status != 'done' or not files:
        raise HTTPException(404, 'There are no files to download for this job.')
    name = sanitize_filename(job.title or job.id, APP_NAME) + '.zip'
    return StreamingResponse(
        _zip_stream(files), media_type='application/zip', headers={'Content-Disposition': _attachment(name)}
    )


@router.get('/api/jobs/{job_id}/media/{key}')
def media(state: State, job_id: str, key: str):
    job = state.job_or_404(job_id)
    if key.startswith('file:'):
        return serve_file(_result_file(job, key[5:]))
    p = (job.result or {}).get('_media', {}).get(key)
    if not p or not Path(p).is_file():
        raise HTTPException(404, 'Preview not available.')
    return serve_file(Path(p))


@router.get('/api/jobs/{job_id}/pcm/{key}')
def media_pcm(state: State, job_id: str, key: str, offset: int = 0, frames: int = 0):
    job = state.job_or_404(job_id)
    p = (job.result or {}).get('_media', {}).get(key) if key in ('original', 'enhanced') else None
    if not p or not Path(p).is_file():
        raise HTTPException(404, 'Preview not available.')
    return serve_pcm(Path(p), offset, frames, state.pcm_stop)


_cover_lock = threading.Lock()  # one save at a time: a double click must not write two pictures


@router.post('/api/jobs/{job_id}/cover')
def save_job_cover(state: State, job_id: str):
    """`save_cover` for a finished job: its cover as a picture in its output folder, added to its
    files (and its Library entry). Already there → that file."""
    job = state.job_or_404(job_id)
    result = job.result or {}
    if job.status != 'done' or not result.get('output_dir'):
        raise HTTPException(404, 'This job has no output folder.')
    folder = Path(result['output_dir'])
    if not folder.is_dir():
        raise HTTPException(404, 'The output folder of this job was moved or deleted.')
    with _cover_lock:
        files = result.get('files') or []
        old = next((f for f in files if f.get('kind') == 'cover'), None)
        if old and Path(old.get('path') or '').is_file():
            return public_file(old)
        title = (result.get('_library') or {}).get('title') or job.title
        entry = common.save_cover(job.source, title, folder, start=len(files))
        if entry is None:
            raise HTTPException(409, 'This recording has no cover image to save.')
        if old:  # its file was moved or deleted: the new one takes its place
            entry['id'] = old['id']
        state.manager.add_result_file(job.id, entry)
        if result.get('library_id'):
            library.add_file(result['library_id'], entry)
    return public_file(entry)


@router.post('/api/jobs/{job_id}/reveal')
def reveal_job(state: State, job_id: str):
    job = state.job_or_404(job_id)
    folder = (job.result or {}).get('output_dir')
    return state.reveal(Path(folder) if folder else None)
