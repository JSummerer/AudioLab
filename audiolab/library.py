"""The Library: a small record of every finished result, independent of job retention.

    <data>/library/<job id>.json   one entry per job that finished with files (Save, Enhance, Split)
                                   and per imported file (task `import`: the file stays where it is)
    <data>/library/originals/      the acquired source of Enhance jobs, kept as it came
                                   (the downloaded stream or the uploaded file — never a decoded WAV)
                                   so results can be compared with it later (setting `keep_originals`)

An entry records the original and final output files. Enhance also keeps processing details and
any export checks with the entry (comparing streams the full files).

JobManager writes the entry when a job finishes (CLI runs are not recorded); jobs expire or get
cleared, entries stay until the user removes them. Removing an entry leaves the output files alone
unless the user opts in: then they go to the system Trash (trash.move_to_trash), never a permanent delete.
A kept original goes with the last entry that refers to it, or on its own on request. A new link
job may reuse a kept original instead of downloading the recording again (`kept_original`).
Output paths are stored relative to the output folder of that time (`root`), so a moved file shows
as missing instead of breaking the entry; an imported file's path is absolute. Versions of one
recording share a `group` (the site's video id, the link, or an upload's content). A kept original
is named by its content (SHA-1), so the same file is kept once and a different one never takes its
place (2.3 named it by group; those entries keep working).
"""

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from . import processing, trash
from .errors import AudioLabError
from .media.export import FORMATS
from .settings import SETTINGS
from .system import replace_file
from .util import PLAYABLE_EXT, link_or_copy, media_type, sanitize_filename

ENTRY_ID_RE = re.compile(r'^j_\d{8}_\d{6}_[0-9a-f]{6}$')  # an entry is named after its job
ORIGINAL = 'original'  # file id of the kept original
# originals/ only ever holds these: 16 hex digits of the content's SHA-1 (of the group key before 2.4)
ORIGINAL_NAME_RE = re.compile(r'^[0-9a-f]{16}\.[a-z0-9]{1,5}$')
VERSION_KIND = {'download': 'saved', 'enhance': 'enhanced', 'separate': 'stem', 'import': 'imported'}
KEEPS_ORIGINAL = ('enhance',)  # nothing compares stems with the original
TIMELINE_TASKS = ('enhance', 'import')  # entries that keep the analysis `tone_timeline`
# What file managers leave in a folder: one holding only these counts as empty.
FOLDER_JUNK = frozenset(('.DS_Store', 'Thumbs.db', 'desktop.ini', '.directory'))

_lock = threading.Lock()  # entry files and originals: workers write, routes read and remove


def originals_dir() -> Path:
    p = SETTINGS.library_dir / 'originals'
    p.mkdir(parents=True, exist_ok=True)
    return p


def source_key(
    source: dict[str, Any], check_cancel: Callable[[], None] | None = None, digests: dict | None = None
) -> str:
    """What makes two results versions of the same recording: a link's id on its site (a direct file
    link by the link itself — yt-dlp's generic id is only the file name), an upload's content."""
    if source.get('kind') == 'url':
        probe = source.get('_probe') or {}
        extractor = str(probe.get('extractor') or '').lower()
        if probe.get('id') and extractor and extractor != 'generic':
            return f'{extractor}:{probe["id"]}'
        return f'url:{source.get("url") or ""}'
    digest = file_sha1(Path(source['path']), check_cancel, digests) if source.get('path') else None
    if digest:
        return f'file:{digest}'
    name = unicodedata.normalize('NFC', str(source.get('name') or ''))  # the upload is gone: by name and size
    return f'file:{name}:{source.get("size_bytes") or 0}'


def file_sha1(path: Path, check_cancel: Callable[[], None] | None = None, digests: dict | None = None) -> str | None:
    """SHA-1 of a file's bytes, None when it can't be read. The optional per-record digest map
    avoids rereading an upload that is both source and kept original, without retaining callbacks."""
    try:
        check = check_cancel or (lambda: None)
        check()
        st = path.stat()
        key = (st.st_dev, st.st_ino or str(path), st.st_size, st.st_mtime_ns)
        if digests is not None and key in digests:
            return digests[key]
        digest = _sha1(path, check)
        if digests is not None:
            digests[key] = digest
        return digest
    except OSError:
        return None


def _sha1(path: Path, check_cancel: Callable[[], None]) -> str:
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        while block := f.read(1 << 20):  # streamed: uploads can be gigabytes
            check_cancel()
            h.update(block)
    check_cancel()
    return h.hexdigest()


# ----------------------------------------------------------------- writing
def record(job, check_cancel: Callable[[], None] | None = None, begin_commit: Callable[[], None] | None = None) -> bool:
    """Write the entry for a finished job (and keep its original). Called by JobManager before the
    job's scratch folder is deleted. Hashing/copying stays cancellable; begin_commit is called
    after preparation, under the Library lock, immediately before short file publication.
    Jobs without output files (Check) get no entry."""
    result = job.result or {}
    outputs = [f for f in result.get('files') or [] if f.get('path')]
    if job.task not in VERSION_KIND or not outputs:
        return False
    hints = result.get('_library') or {}
    keep = job.task in KEEPS_ORIGINAL and hints.get('original') and SETTINGS.get().keep_originals
    original = Path(hints['original']) if keep else None
    check = check_cancel or (lambda: None)
    digests: dict = {}  # reuse an upload's digest only within this record; never retain its callbacks
    digest = file_sha1(original, check, digests) if original else None
    root = SETTINGS.output_dir
    stems = {
        p['file_id']: p['key'][5:]
        for p in result.get('previews') or []
        if p.get('file_id') and str(p.get('key', '')).startswith('stem:')
    }
    files = [_file_item(f, root, job.task, stems.get(f['id'])) for f in outputs]
    metrics = result.get('metrics') or {}
    out_dir = result.get('output_dir')
    entry = {
        'id': job.id,
        'job_id': job.id,
        'task': job.task,
        'created_at': time.time(),
        'title': hints.get('title') or job.title,  # as used for names and tags
        'artist': hints.get('artist'),
        'source': job.source.get('_library_source')
        or {k: v for k, v in job.source.items() if not k.startswith('_') and k != 'path'},
        'group': job.source.get('_library_group') or source_key(job.source, check, digests),
        'duration_s': hints.get('duration_s') or job.source.get('duration_s'),
        'headline': result.get('headline'),
        'root': str(root),
        'output_dir': (str(out_dir) if job.task == 'import' else _relative(out_dir, root)) if out_dir else None,
        'files': files,
        'loudness': {
            'original_lufs': (metrics.get('before') or {}).get('lufs'),
            'result_lufs': (metrics.get('after') or {}).get('lufs'),
        },
        'markers': [{k: t.get(k) for k in ('n', 'title', 'start_s', 'end_s')} for t in hints.get('markers') or []],
        'markers_source': hints.get('markers_source'),
        'original': None,
    }
    if job.task == 'enhance' and (details := processing.snapshot(job.options, result)) is not None:
        entry['processing'] = details
    if job.task == 'enhance' and (excerpt := _excerpt(result.get('excerpt'))):
        entry['excerpt'] = excerpt  # the loud passage the job page compares first
    if job.task == 'enhance' and (overview := original_overview(result.get('_overview'))):
        entry['overview'] = overview
    if job.task == 'enhance' and type(hints.get('version')) is int and hints['version'] > 0:
        entry['version'] = hints['version']  # "(Enhanced 2)" in its tags: tasks.common.next_version
    timeline = _tone_timeline((result.get('analysis') or {}).get('tone_timeline'))
    if job.task in TIMELINE_TASKS and timeline:
        entry['tone_timeline'] = timeline
    stream = _stream(result.get('stream'))
    if job.task == 'import':
        measured = result.get('measured') if isinstance(result.get('measured'), dict) else {}
        entry['measured'] = {k: _rounded(measured.get(k)) for k in ('integrated_lufs', 'true_peak_dbtp', 'duration_s')}
    with ExitStack() as cleanup:
        item = _original_item(original, digest, entry, hints) if original and digest else None
        staged = None
        if item:
            item['stream'] = stream if item['downloaded'] else None  # the stream this download is
        if item and not item['downloaded']:
            # The same bytes are the same stream: a Library apply keeps a downloaded original again.
            # Only within this recording: an imported copy of another link's audio must not inherit it.
            same = [
                e
                for e in _all()
                if e.get('group') == entry['group'] and _downloaded(e) and e['original'].get('file') == item['file']
            ]
            item['downloaded'] = bool(same)
            item['stream'] = next((x for e in same if (x := _stream(e['original'].get('stream')))), None)
            stream = stream or item['stream']  # an apply's entry shows the stream of the kept download
        if stream:
            entry['stream'] = stream
        if item:
            # Always prepare our own link/copy: a concurrent Storage action may remove an existing
            # original before publication. Only this temporary directory belongs to this attempt.
            folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix='.original-', dir=originals_dir()))
            dst = originals_dir() / item['file']
            candidate = dst if _size(dst) == item['size_bytes'] else original
            try:
                staged = link_or_copy(candidate, Path(folder) / item['file'], check)
            except FileNotFoundError:
                if candidate == original:
                    raise
                staged = link_or_copy(original, Path(folder) / item['file'], check)
        check()
        with _lock:  # Library -> manager is the only nested lock order
            if begin_commit:
                begin_commit()
            else:
                check()
            created = None
            try:
                if item:
                    dst = originals_dir() / item['file']
                    if not dst.exists():
                        replace_file(staged, dst)
                        created = dst
                    # An unexpected preexisting file is never overwritten or removed.
                    if _size(dst) == item['size_bytes']:
                        entry['original'] = item
                _write(entry)
            except BaseException:
                if created is not None:
                    created.unlink(missing_ok=True)
                raise
    return True


def _stream(value: Any) -> dict[str, Any] | None:
    """A link's delivered stream (youtube.parse_stream) as an entry keeps it, else None."""
    if not isinstance(value, dict):
        return None
    fmt, codec = value.get('format_id'), value.get('codec')
    out = {
        'format_id': fmt[:40] if isinstance(fmt, str) else None,
        'codec': codec[:20] if isinstance(codec, str) else None,
        **{
            k: value[k] if type(value.get(k)) is int and value[k] > 0 else None
            for k in ('kbps', 'sample_rate', 'channels')
        },
    }
    return out if any(v is not None for v in out.values()) else None


def _rounded(value: Any, digits: int = 2) -> float | None:
    """A finite number, rounded; else None."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def original_overview(value: Any) -> dict | None:
    """Bound and validate the overview stored outside the public Library list."""
    from .analysis.levels import OVERVIEW_MAX_BINS

    if not isinstance(value, dict) or value.get('basis') != 'original_sample_peak':
        return None
    duration, step = (_rounded(value.get(key), 9) for key in ('duration_s', 'bin_duration_s'))
    peaks = value.get('peaks')
    if not duration or not step or min(duration, step) < 0 or not isinstance(peaks, list):
        return None
    if not 0 < len(peaks) <= OVERVIEW_MAX_BINS or not (len(peaks) - 1) * step < duration <= len(peaks) * step + 1e-6:
        return None
    peaks = [_rounded(v, 6) for v in peaks]
    if any(v is None or v < 0 for v in peaks):
        return None
    return {'basis': 'original_sample_peak', 'duration_s': duration, 'bin_duration_s': step, 'peaks': peaks}


def _tone_timeline(value: Any) -> dict[str, Any] | None:
    """The analysis `tone_timeline` as an entry keeps it (docs/API.md): checked, values in 0.1 dB
    steps, at most 1000 points; None when it is missing or not of that shape."""
    if not isinstance(value, dict):
        return None
    step, bands, values = _rounded(value.get('step_s'), 3), value.get('bands'), value.get('values')
    if not step or step <= 0 or not isinstance(bands, list) or not 0 < len(bands) <= 16:
        return None
    if not isinstance(values, list) or not 0 < len(values) <= 1000:
        return None
    out_bands = []
    for b in bands:
        hz = b.get('hz') if isinstance(b, dict) else None
        if not (isinstance(b, dict) and isinstance(b.get('key'), str) and isinstance(b.get('label'), str)):
            return None
        if not (isinstance(hz, list) and len(hz) == 2 and all(_rounded(f) is not None for f in hz)):
            return None
        out_bands.append({'key': b['key'][:40], 'label': b['label'][:80], 'hz': [float(f) for f in hz]})
    rows: list[list[float] | None] = []
    for row in values:
        if row is None:
            rows.append(None)
            continue
        cells = [_rounded(v, 1) for v in row] if isinstance(row, list) and len(row) == len(bands) else [None]
        if None in cells:
            return None
        rows.append(cells)
    out: dict[str, Any] = {'step_s': step, 'bands': out_bands, 'values': rows}
    if isinstance(value.get('basis'), str):
        out['basis'] = value['basis'][:300]
    return out


def _excerpt(value: Any) -> dict[str, float] | None:
    """`{start_s, duration_s}` with finite, nonnegative numbers, else None."""
    if not isinstance(value, dict):
        return None
    out = {k: value.get(k) for k in ('start_s', 'duration_s')}
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in out.values()):
        return None
    return {k: round(float(v), 3) for k, v in out.items()}


def _file_item(f: dict[str, Any], root: Path, task: str, stem: str | None = None) -> dict[str, Any]:
    """An entry's record of one Result.files entry (`path` relative to the output folder `root`)."""
    kind = 'track' if f.get('group') == 'tracks' else 'cover' if f.get('kind') == 'cover' else VERSION_KIND[task]
    item = {
        'id': f['id'],
        'name': f.get('name') or Path(f['path']).name,
        'path': str(f['path']) if kind == 'imported' else _relative(f['path'], root),  # an import stays where it is
        'kind': kind,
        'format': f.get('format'),
        'label': f.get('label'),
        'size_bytes': f.get('size_bytes'),
    }
    item.update({k: f[k] for k in ('track', 'start_s', 'end_s') if f.get(k) is not None})
    if stem:
        item['stem'] = stem
    if kind == 'enhanced' and (verification := processing.verification(f.get('verification'))) is not None:
        item['verification'] = verification
    return item


def add_file(entry_id: str, f: dict[str, Any]) -> bool:
    """Add a file made after the entry was written (the cover saved later from the job page);
    one with the same id is replaced. False when there is no such entry."""
    with _lock:
        entry = get(entry_id)
        if entry is None:
            return False
        item = _file_item(f, Path(entry['root']), entry['task'])
        entry['files'] = [x for x in entry.get('files') or [] if x.get('id') != item['id']] + [item]
        _write(entry)
    return True


def _relative(path: str, root: Path) -> str:
    p = Path(path)
    try:
        return p.relative_to(root).as_posix()
    except ValueError:  # outside the output folder: keep it absolute
        return str(p)


def _original_item(src: Path, digest: str, entry: dict[str, Any], hints: dict[str, Any]) -> dict[str, Any] | None:
    """Metadata for a kept original named by content; preparation/publication lives in record().
    `downloaded`: a link's download (only those are reused for later link jobs, `kept_original`)."""
    ext = src.suffix.lower()
    dst = originals_dir() / (digest[:16] + ext)
    size = _size(src)
    if size is None or not ORIGINAL_NAME_RE.fullmatch(dst.name):  # gone, or a suffix nothing would find
        return None
    name = entry['source'].get('name') or f'{sanitize_filename(entry["title"])}{ext}'
    return {
        'file': dst.name,
        'name': name,
        'size_bytes': size,
        'codec': hints.get('original_codec'),
        'sha1': digest,
        'downloaded': bool(hints.get('original_downloaded')),
    }


def _write(entry: dict[str, Any]) -> None:
    d = SETTINGS.library_dir
    tmp = d / f'.{entry["id"]}.json.tmp'
    tmp.write_text(json.dumps(entry, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
    replace_file(tmp, d / f'{entry["id"]}.json')


# ----------------------------------------------------------------- reading
def _all() -> list[dict[str, Any]]:
    out = []
    for p in SETTINGS.library_dir.glob('j_*.json'):
        try:
            entry = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue  # a torn or foreign file
        if isinstance(entry, dict) and entry.get('id') == p.stem:
            out.append(entry)
    return out


def get(entry_id: str) -> dict[str, Any] | None:
    if not ENTRY_ID_RE.fullmatch(entry_id or ''):
        return None
    try:
        entry = json.loads((SETTINGS.library_dir / f'{entry_id}.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def file_path(entry: dict[str, Any], file_id: str) -> Path | None:
    """Where a file of the entry lives (it may be gone); None for an unknown id."""
    if file_id == ORIGINAL:
        name = (entry.get('original') or {}).get('file')
        return originals_dir() / name if name and ORIGINAL_NAME_RE.fullmatch(name) else None
    for f in entry.get('files') or []:
        if f.get('id') == file_id:
            return _resolve(entry, f.get('path'))
    return None


def folder(entry: dict[str, Any]) -> Path | None:
    """The entry's output folder (it may be gone)."""
    return _resolve(entry, entry.get('output_dir'))


def _resolve(entry: dict[str, Any], stored: str | None) -> Path | None:
    p = Path(stored or '')
    if not stored or '..' in p.parts:
        return None
    return p if p.is_absolute() else Path(entry['root']) / p


def public(entry: dict[str, Any], job_exists: bool = False) -> dict[str, Any]:
    """The entry as the UI sees it: every file with `exists` (checked now) and its media type."""
    files = []
    for f in entry.get('files') or []:
        p = file_path(entry, f['id'])
        files.append({**f, **_file_state(p)})
    orig = entry.get('original') or {}
    p = file_path(entry, ORIGINAL)
    size = _size(p)  # one look: it may be deleted at any moment
    kept = size is not None
    if kept:  # listed with the files only while it is there
        files.append(
            {
                'id': ORIGINAL,
                'name': orig.get('name'),
                'kind': ORIGINAL,
                'format': p.suffix[1:],
                'label': 'Original (as downloaded)' if entry['source'].get('kind') == 'url' else 'Original file',
                'size_bytes': size,
                'codec': orig.get('codec'),
                **_file_state(p),
                'exists': True,
            }
        )
    out = {k: v for k, v in entry.items() if k not in ('files', 'original', 'root', 'tone_timeline', 'overview')}
    if entry.get('tone_timeline'):
        out['has_tone_timeline'] = True  # the data: GET /api/library/{id}/tone-timeline (keeps the list small)
    if entry.get('overview') and kept:
        out['has_overview'] = True
    folder = _resolve(entry, entry.get('output_dir'))
    out['output_dir'] = str(folder) if folder else None
    out['files'] = files
    out['original'] = {
        'kept': kept,
        'size_bytes': size,
        'format': p.suffix[1:] if kept else None,
        'codec': orig.get('codec') if kept else None,
        'key': p.stem if kept else None,  # the kept file's content name: versions sharing one count once
        'url': entry['source'].get('url') if entry['source'].get('kind') == 'url' else None,  # to fetch it again
    }
    out['compare_file_id'] = _compare_file(files) if kept else None
    out['job_exists'] = job_exists
    return out


def _compare_file(files: list[dict[str, Any]]) -> str | None:
    """The result an Enhance entry's kept original is compared with: the first lossless one that is
    still there (the closest to the master), else the first one that is still there."""
    results = [f for f in files if f['kind'] == 'enhanced' and f['exists']]
    lossless = [f for f in results if getattr(FORMATS.get(f.get('format') or ''), 'kind', None) == 'lossless']
    return (lossless or results or [{'id': None}])[0]['id']


def _size(p: Path | None) -> int | None:
    """Size of a regular file, None when it is not there (also if it vanishes while we look)."""
    try:
        st = p.stat() if p is not None else None
    except OSError:
        return None
    return st.st_size if st is not None and stat.S_ISREG(st.st_mode) else None


def _unlink(p: Path) -> int:
    """Delete a file; the bytes freed: 0 when it was already gone, or when another hard link still
    holds them (an upload's kept original is also its job's input file)."""
    try:
        st = p.stat()
        p.unlink()
    except FileNotFoundError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) and st.st_nlink == 1 else 0


def _file_state(p: Path | None) -> dict[str, Any]:
    return {
        'exists': bool(p and p.is_file()),
        'playable': bool(p and p.suffix.lower() in PLAYABLE_EXT),
        'mime': media_type(p) if p else None,
    }


def kept_original(group: str) -> tuple[Path, dict[str, Any] | None] | None:
    """The kept original of a recording (its `group`) that a link download made and is still there,
    the newest entry's first, with the stream of that download: a new link job uses it instead of
    downloading the recording again (setting `reuse_originals`)."""
    for e in sorted(_all(), key=_created, reverse=True):
        if e.get('group') != group or not _downloaded(e):
            continue
        p = file_path(e, ORIGINAL)
        if (_size(p) or 0) > 0:
            return p, _stream(e['original'].get('stream')) or _stream(e.get('stream'))
    return None


def next_version(group: str, task: str) -> int:
    """One more than the highest version number among the recording's (`group`) entries of `task`, so
    a removed version's number is not given again. An entry from before numbers counts as its place
    in created order."""
    entries = sorted((e for e in _all() if e.get('group') == group and e.get('task') == task), key=_created)
    numbers = [e['version'] if type(e.get('version')) is int else i for i, e in enumerate(entries, 1)]
    return max(numbers, default=0) + 1


def _created(entry: dict[str, Any]) -> float:
    return entry.get('created_at') or 0


def _downloaded(entry: dict[str, Any]) -> bool:
    """The entry's kept original is a link's download, not a file (an imported master enhanced as a
    version of a link must never stand in for that link). Before the flag: an entry of a link."""
    item = entry.get('original') or {}
    if 'downloaded' in item:
        return item['downloaded'] is True
    return bool(item) and (entry.get('source') or {}).get('kind') == 'url'


def groups(job_exists: Callable[[str], bool] = lambda job_id: False) -> list[dict[str, Any]]:
    """All entries as groups of versions of the same recording, newest first. A group is named after
    its newest entry that isn't an import (a master added as a version must not rename the
    recording); a group of imports only, after its newest import."""
    out: dict[str, dict[str, Any]] = {}
    named: set[str] = set()  # groups that already took their name from an entry that isn't an import
    for e in sorted(_all(), key=_created, reverse=True):
        g = out.get(e['group'])
        if g is None:
            g = out[e['group']] = {'key': e['group'], 'latest_at': e.get('created_at'), 'entries': []}
        if 'title' not in g or (e.get('task') != 'import' and e['group'] not in named):
            g.update(title=e.get('title'), artist=e.get('artist'), source=e.get('source'))
            if e.get('task') != 'import':
                named.add(e['group'])
        g['entries'].append(public(e, job_exists(e.get('job_id') or '')))
    return list(out.values())


# ---------------------------------------------------------------- removing
def remove(entry_id: str, trash_files: bool = False) -> dict[str, Any] | None:
    """Forget one entry. Its kept original goes when no other entry uses it. The output files stay,
    unless `trash_files`: then they go to the system Trash (see `_trash_files`). AudioLabError when
    this system has no Trash — then nothing is removed."""
    with _lock:
        entry = get(entry_id)
        if entry is None:
            return None
        others = [e for e in _all() if e.get('id') != entry_id]
        trashed, failed = _trash_files([entry], others) if trash_files else ([], [])
        (SETTINGS.library_dir / f'{entry_id}.json').unlink(missing_ok=True)
        freed = _drop_unused_original(entry)
    return {
        'deleted': entry_id,
        'original_removed': freed is not None,
        'freed_bytes': freed or 0,
        'trashed': trashed,
        'trash_failed': failed,
    }


def keep_only(entry_id: str, trash_files: bool = False) -> dict[str, Any] | None:
    """Keep only this version: forget every other entry of the same recording (`group`), their kept
    originals as in `remove`; with `trash_files` their files go to the Trash. AudioLabError when this
    system has no Trash — then nothing is removed."""
    with _lock:
        entry = get(entry_id)
        if entry is None:
            return None
        entries = _all()
        dropped = [e for e in entries if e.get('group') == entry.get('group') and e.get('id') != entry_id]
        ids = {e.get('id') for e in dropped}
        remaining = [e for e in entries if e.get('id') not in ids]
        trashed, failed = _trash_files(dropped, remaining) if trash_files else ([], [])
        for e in dropped:
            (SETTINGS.library_dir / f'{e["id"]}.json').unlink(missing_ok=True)
        for e in dropped:  # after all of them are gone: a file they shared goes once
            _drop_unused_original(e)
    return {'kept': entry_id, 'removed': len(dropped), 'trashed': trashed, 'trash_failed': failed}


def _trash_files(entries: list[dict[str, Any]], remaining: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Move the files of `entries` that are still there (results, tracks, saved cover, an imported
    file) to the system Trash, then their folders that are left empty. Returns the names of the
    files trashed and of those that couldn't be (they stay). Never touched: a file an entry in
    `remaining` still uses, anything in AudioLab's data folder (the kept originals), and any folder
    but an entry's own output folder and the folders of its files inside it, strictly inside the
    entry's output root (never the root itself, never for imports, never one a remaining entry
    uses). Nothing through or of a symbolic link: such a file is refused (listed as failed).
    AudioLabError (no Trash on this system) is raised only before anything moved. The caller holds
    the lock."""
    data = _real(SETTINGS.data_dir)
    in_use = {_real(p) for e in remaining for f in e.get('files') or [] if (p := file_path(e, f.get('id') or ''))}
    used_folders = {_real(p) for e in remaining if (p := folder(e))} | {_real(SETTINGS.output_dir)}
    trashed: list[str] = []
    failed: list[str] = []
    folders: dict[Path, Path] = {}  # a folder that may go when empty -> its entry's output root

    def move(p: Path) -> bool:
        try:
            trash.move_to_trash(p)
        except AudioLabError:
            if not trashed:  # no Trash at all: nothing has moved, nothing is removed
                raise
            return False
        except OSError:
            return False
        return True

    for e in entries:
        imported = e.get('task') == 'import'
        root = Path(e['root']) if e.get('root') else None
        own = folder(e) if not imported and root is not None else None
        for f in e.get('files') or []:
            p = file_path(e, f.get('id') or '')
            if p is None or not os.path.lexists(p) or _real(p) in in_use or _inside(_real(p), data, strict=False):
                continue
            # Never through or of a link: no link anywhere on an imported file's path (it was stored
            # resolved), and an AudioLab output lies in its root with no link on the way (a folder or the
            # output folder replaced by a link to another music folder).
            safe = _no_link(p) if imported else root is not None and _plain_inside(p, root)
            if not safe or not p.is_file() or not move(p):
                failed.append(p.name)
                continue
            trashed.append(p.name)
            if own is not None and p.parent.is_relative_to(own):
                folders[p.parent] = root  # its folder, or Tracks/ inside it
        if own is not None:
            folders[own] = root
    for d in sorted(folders, key=lambda d: len(d.parts), reverse=True):  # Tracks/ before its folder
        if not _plain_inside(d, folders[d]) or _real(d) in used_folders or _inside(data, _real(d), strict=False):
            continue
        if _empty_folder(d):
            move(d)  # a folder that stays is not worth a message: its files are what the user asked about
    return trashed, failed


def _plain_inside(p: Path, root: Path) -> bool:
    """`p` lies strictly inside `root` and no part of its path, `root` included, is a symbolic link."""
    try:
        if not p.relative_to(root).parts:
            return False
    except ValueError:
        return False
    return _no_link(p)


def _no_link(p: Path) -> bool:
    """No part of the absolute path `p` is a symbolic link or junction, each part checked on its own
    (trash.is_link). A path under a linked folder fails here, which only keeps a file where it is."""
    p = Path(os.path.abspath(p))
    return not any(trash.is_link(part) for part in (p, *p.parents) if part != Path(part.anchor))


def _real(p: Path) -> Path:
    return Path(os.path.normcase(os.path.realpath(p)))


def _inside(p: Path, parent: Path, strict: bool = True) -> bool:
    """`p` lies in `parent` (strict: and is not `parent` itself); both already `_real`."""
    return p.is_relative_to(parent) and not (strict and p == parent)


def _empty_folder(d: Path) -> bool:
    """A real folder (not a link) holding nothing but what file managers leave behind."""
    try:
        return d.is_dir() and not d.is_symlink() and all(c.name in FOLDER_JUNK and c.is_file() for c in d.iterdir())
    except OSError:
        return False


def drop_original(entry_id: str) -> dict[str, Any] | None:
    """Stop keeping the original of this entry's recording: every entry of the group shows
    `original.kept: false` (entries and output files stay), and their kept files — usually one — are
    deleted, except one that an entry of another recording still uses (the same content)."""
    with _lock:
        entry = get(entry_id)
        if entry is None:
            return None
        dropped = [e for e in _all() if e.get('group') == entry.get('group') and e.get('original')]
        kept = any(_size(file_path(e, ORIGINAL)) is not None for e in dropped)
        for e in dropped:
            _write({**e, 'original': None})
        freed = sum(_drop_unused_original(e) or 0 for e in dropped)  # a shared file: deleted once
    return {'id': entry_id, 'original_removed': kept, 'freed_bytes': freed}


def _drop_unused_original(entry: dict[str, Any]) -> int | None:
    """Delete the entry's kept original unless a stored entry still refers to it; bytes freed, else
    None (also when it is already gone). The caller holds the lock and has already removed or
    detached `entry` on disk."""
    name = (entry.get('original') or {}).get('file')
    p = file_path(entry, ORIGINAL)
    if p is None or _size(p) is None or any((e.get('original') or {}).get('file') == name for e in _all()):
        return None
    return _unlink(p)


def count() -> int:
    return sum(1 for _ in SETTINGS.library_dir.glob('j_*.json'))


def _originals() -> list[Path]:
    """The kept originals: only files named like ours — anything else in the folder is left alone."""
    return [p for p in originals_dir().iterdir() if ORIGINAL_NAME_RE.fullmatch(p.name)]


def originals_usage(seen: set | None = None) -> dict[str, int]:
    """The kept originals: how many, and their bytes. A file's disk space is counted once: its
    (st_dev, st_ino) goes into `seen`, so the caller can leave the same file out elsewhere (an
    upload's original is a hard link to its job's input file)."""
    seen = set() if seen is None else seen
    count = size = 0
    for p in _originals():
        try:
            st = p.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        count += 1
        if (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            size += st.st_size
    return {'count': count, 'bytes': size}


def clear_originals() -> dict[str, int]:
    """Delete every kept original; the entries stay and show `original.kept: false`."""
    count = size = 0
    with _lock:
        for e in _all():
            if e.get('original'):
                _write({**e, 'original': None})
        for p in _originals():
            if _size(p) is not None:
                count += 1
                size += _unlink(p)
    return {'deleted': count, 'freed_bytes': size}
