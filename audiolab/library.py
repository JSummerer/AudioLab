"""The Library: a small record of every finished result, independent of job retention.

    <data>/library/<job id>.json   one entry per job that finished with files (Save, Enhance, Split)
    <data>/library/originals/      the acquired source of Enhance jobs, kept as it came
                                   (the downloaded stream or the uploaded file — never a decoded WAV)
                                   so results can be compared with it later (setting `keep_originals`)

An entry is the original plus the final output files — nothing else is written for it (comparing
streams the full files).

JobManager writes the entry when a job finishes (CLI runs are not recorded); jobs expire or get
cleared, entries stay until the user removes them. Removing an entry never touches the output
files; a kept original goes with the last entry that refers to it, or on its own on request. Output paths are stored relative
to the output folder of that time (`root`), so a moved file shows as missing instead of breaking
the entry. Versions of one recording share a `group` (the site's video id, the link, or an
upload's content). A kept original is named by its content (SHA-1), so the same file is kept once
and a different one never takes its place (2.3 named it by group; those entries keep working).
"""

import hashlib
import json
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

from .media.export import FORMATS
from .settings import SETTINGS
from .system import replace_file
from .util import PLAYABLE_EXT, link_or_copy, media_type, sanitize_filename

ENTRY_ID_RE = re.compile(r'^j_\d{8}_\d{6}_[0-9a-f]{6}$')  # an entry is named after its job
ORIGINAL = 'original'  # file id of the kept original
# originals/ only ever holds these: 16 hex digits of the content's SHA-1 (of the group key before 2.4)
ORIGINAL_NAME_RE = re.compile(r'^[0-9a-f]{16}\.[a-z0-9]{1,5}$')
VERSION_KIND = {'download': 'saved', 'enhance': 'enhanced', 'separate': 'stem'}
KEEPS_ORIGINAL = ('enhance',)  # nothing compares stems with the original

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
    entry = {
        'id': job.id,
        'job_id': job.id,
        'task': job.task,
        'created_at': time.time(),
        'title': hints.get('title') or job.title,  # as used for names and tags
        'artist': hints.get('artist'),
        'source': {k: v for k, v in job.source.items() if not k.startswith('_') and k != 'path'},
        'group': source_key(job.source, check, digests),
        'duration_s': hints.get('duration_s') or job.source.get('duration_s'),
        'headline': result.get('headline'),
        'root': str(root),
        'output_dir': _relative(result['output_dir'], root) if result.get('output_dir') else None,
        'files': files,
        'loudness': {
            'original_lufs': (metrics.get('before') or {}).get('lufs'),
            'result_lufs': (metrics.get('after') or {}).get('lufs'),
        },
        'markers': [{k: t.get(k) for k in ('n', 'title', 'start_s', 'end_s')} for t in hints.get('markers') or []],
        'markers_source': hints.get('markers_source'),
        'original': None,
    }
    with ExitStack() as cleanup:
        item = _original_item(original, digest, entry, hints.get('original_codec')) if original and digest else None
        staged = None
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


def _file_item(f: dict[str, Any], root: Path, task: str, stem: str | None = None) -> dict[str, Any]:
    """An entry's record of one Result.files entry (`path` relative to the output folder `root`)."""
    kind = 'track' if f.get('group') == 'tracks' else 'cover' if f.get('kind') == 'cover' else VERSION_KIND[task]
    item = {
        'id': f['id'],
        'name': f.get('name') or Path(f['path']).name,
        'path': _relative(f['path'], root),
        'kind': kind,
        'format': f.get('format'),
        'label': f.get('label'),
        'size_bytes': f.get('size_bytes'),
    }
    item.update({k: f[k] for k in ('track', 'start_s', 'end_s') if f.get(k) is not None})
    if stem:
        item['stem'] = stem
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


def _original_item(src: Path, digest: str, entry: dict[str, Any], codec: str | None) -> dict[str, Any] | None:
    """Metadata for a kept original named by content; preparation/publication lives in record()."""
    ext = src.suffix.lower()
    dst = originals_dir() / (digest[:16] + ext)
    size = _size(src)
    if size is None or not ORIGINAL_NAME_RE.fullmatch(dst.name):  # gone, or a suffix nothing would find
        return None
    name = entry['source'].get('name') or f'{sanitize_filename(entry["title"])}{ext}'
    return {'file': dst.name, 'name': name, 'size_bytes': size, 'codec': codec, 'sha1': digest}


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
    out = {k: v for k, v in entry.items() if k not in ('files', 'original', 'root')}
    folder = _resolve(entry, entry.get('output_dir'))
    out['output_dir'] = str(folder) if folder else None
    out['files'] = files
    out['original'] = {
        'kept': kept,
        'size_bytes': size,
        'format': p.suffix[1:] if kept else None,
        'codec': orig.get('codec') if kept else None,
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


def groups(job_exists: Callable[[str], bool] = lambda job_id: False) -> list[dict[str, Any]]:
    """All entries as groups of versions of the same recording, newest first."""
    out: dict[str, dict[str, Any]] = {}
    for e in sorted(_all(), key=lambda e: e.get('created_at') or 0, reverse=True):
        g = out.get(e['group'])
        if g is None:
            g = out[e['group']] = {
                'key': e['group'],
                'title': e.get('title'),
                'artist': e.get('artist'),
                'source': e.get('source'),
                'latest_at': e.get('created_at'),
                'entries': [],
            }
        g['entries'].append(public(e, job_exists(e.get('job_id') or '')))
    return list(out.values())


# ---------------------------------------------------------------- removing
def remove(entry_id: str) -> dict[str, Any] | None:
    """Forget one entry (the output files stay). Its kept original goes when no other entry uses it."""
    with _lock:
        entry = get(entry_id)
        if entry is None:
            return None
        (SETTINGS.library_dir / f'{entry_id}.json').unlink(missing_ok=True)
        freed = _drop_unused_original(entry)
    return {'deleted': entry_id, 'original_removed': freed is not None, 'freed_bytes': freed or 0}


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
