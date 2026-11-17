"""Job queue: persistent, cancellable, retryable.

Each job lives in `<data>/jobs/<id>/`:
    job.json        the Job record (atomically rewritten)
    journal.jsonl   append-only event log ("what happened and why")
    input/          uploaded source file (file jobs), own cover image
    work/           scratch space for the task (pruned after success)

A job that finishes with files is also recorded in the Library (library.py),
which outlives the job.

One JobManager owns the folder (an instance lock keeps a second process out).
Worker threads pull queued jobs in FIFO order (LIMITS.max_concurrent_jobs,
default 1, so heavy jobs never stack up in RAM). `shutdown()` is the one way to
stop: it settles every queued and running job before the app exits. Jobs a crash
left behind are marked honestly on the next start ("interrupted" / "not
started"); either way they can be retried.
"""

import builtins
import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import library, outputs
from .context import TaskContext
from .errors import AudioLabError, Cancelled, storage_error
from .media.export import UPLOAD_EXT
from .settings import LIMITS, SETTINGS
from .system import InstanceLock, replace_file
from .util import JOB_ID_RE

QUEUED, RUNNING, DONE, FAILED, CANCELLED = 'queued', 'running', 'done', 'failed', 'cancelled'
FINISHED = (DONE, FAILED, CANCELLED)
NOT_STARTED = 'Not started — AudioLab was closed. Click "Try again" to run it.'
STOPPED_ON_EXIT = 'Stopped because AudioLab was closed — click "Try again" to run it again.'
NOT_IN_LIBRARY = 'Not added to the Library. The saved files are complete.'
CLOSING = 'AudioLab is closing.'
# Long enough for system.terminate_process to reach its forced kill: SIGKILL after 5 s on
# POSIX; on Windows taskkill (up to 10 s), then kill() after another 5 s.
KILL_WAIT_S = 16.0
ORPHAN_AGE_S = 3600  # an unfinished job folder (or Library staging) untouched this long is left over
PRUNE_EVERY_S = 6 * 3600

Listener = Callable[[str, dict[str, Any]], None]  # (event, payload)

log = logging.getLogger('audiolab')


@dataclass
class Job:
    id: str
    task: str
    source: dict[str, Any]
    options: dict[str, Any]
    title: str = ''
    status: str = QUEUED
    stages: list[dict[str, str]] = field(default_factory=list)
    stage: str = ''
    progress: float = 0.0
    message: str = 'Waiting in line…'
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None

    def public(self, queue_position: int | None = None) -> dict[str, Any]:
        d = asdict(self)
        d['queue_position'] = queue_position
        d['source'] = {k: v for k, v in self.source.items() if not k.startswith('_') and k != 'path'}
        d['options'] = {k: v for k, v in d['options'].items() if not k.startswith('_')}
        if self.result is not None:
            d['result'] = _public_result(self.result)
        return d


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Strip internal keys (absolute paths, '_' prefixed) from a task result; say per file
    whether it is still there (the user may have moved or deleted it)."""
    out = {k: v for k, v in result.items() if not k.startswith('_')}
    if 'files' in out:
        out['files'] = [public_file(f) for f in out['files']]
    return out


def _error(e: AudioLabError) -> dict[str, Any]:
    """Job.error for a failure: message, hints, fix — and the raw text behind it as `detail` (for the
    bug report), when there is one."""
    error = {'message': str(e), 'hints': e.hints, 'fix': e.fix}
    if e.detail:
        error['detail'] = e.detail
    return error


def public_file(f: dict[str, Any]) -> dict[str, Any]:
    """A Result.files entry without its absolute path, with `exists` (checked now)."""
    return {**{k: v for k, v in f.items() if k != 'path'}, 'exists': bool(f.get('path')) and Path(f['path']).is_file()}


def new_job_id() -> str:
    return time.strftime('j_%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]


# What a request that never became a job can leave in its folder (api/jobs, api/library): input/ with the
# upload, a re-run's or a Library apply's linked input (an audio or video file ffmpeg reads) and the own
# cover in input/cover/; their temporary names; and job.json's temporary file. Nothing else qualifies.
_SUBMISSION_TOP = frozenset(('input', '.job.json.tmp'))
_LINK_TMP_RE = re.compile(r'^\..+\.[0-9a-f]{32}\.link-tmp$')  # util.link_or_copy
_COVER_FILE_RE = re.compile(r'^(cover\.jpg|original\.(jpg|png|webp)|\.upload\.(jpg|jpeg|png|webp)|\.new\.jpg)$')


def _only_a_submission(folder: Path) -> bool:
    """True when `folder` holds nothing but what an unfinished submission leaves (see above): regular files
    only, no links, no other names."""

    def files_only(path: Path, ok: Callable[[str], bool]) -> bool:
        with os.scandir(path) as it:
            return all(e.is_file(follow_symlinks=False) and (ok(e.name) or _LINK_TMP_RE.match(e.name)) for e in it)

    with os.scandir(folder) as it:
        top = list(it)
    for e in top:
        if e.name not in _SUBMISSION_TOP:
            return False
        if e.name == '.job.json.tmp' and not e.is_file(follow_symlinks=False):
            return False
        if e.name == 'input' and not e.is_dir(follow_symlinks=False):
            return False
    inputs = folder / 'input'
    if not os.path.lexists(inputs):
        return True
    with os.scandir(inputs) as it:
        entries = list(it)
    for e in entries:
        if e.name == 'cover' and e.is_dir(follow_symlinks=False):
            if not files_only(Path(e.path), lambda n: bool(_COVER_FILE_RE.match(n))):
                return False
        elif not (
            e.is_file(follow_symlinks=False)
            and (Path(e.name).suffix.lower() in UPLOAD_EXT or _LINK_TMP_RE.match(e.name))
        ):
            return False
    return True


def _protected_paths() -> frozenset[tuple[int, int]]:
    """The identities (device, inode) of what no removal inside the data folder may touch, and of every
    folder above it: the output folder as set, and every output folder and result file the Library knows
    (an output folder may once have been set inside the data folder). Identities, not path text: on a
    case-insensitive drive one folder has many spellings."""
    places: set[str] = set()
    for p in (SETTINGS.output_dir, *library.known_paths()):
        real = Path(os.path.realpath(p))
        places.update(str(q) for q in (real, *real.parents))
    found = set()
    for place in places:
        try:
            st = os.stat(place)
        except OSError:
            continue
        found.add((st.st_dev, st.st_ino))
    return frozenset(found)


def _protects(folder: Path, protected: frozenset[tuple[int, int]]) -> bool:
    """Whether removing `folder` would take a protected place with it: it is one, or one lies inside it
    (it is the same folder as one above it). A protected folder that merely contains `folder` (an output
    folder or an imported file's folder that holds the data folder, such as the home folder) loses nothing."""
    try:
        st = os.stat(folder)
    except OSError:
        return False  # nothing there to remove
    return (st.st_dev, st.st_ino) in protected


def _newest_mtime(folder: Path) -> float:
    """The latest modification time of a folder or anything inside it (links not followed)."""
    newest = os.lstat(folder).st_mtime
    for parent, dirs, files in os.walk(folder):
        for name in dirs + files:
            try:
                newest = max(newest, os.lstat(os.path.join(parent, name)).st_mtime)
            except OSError:
                continue
    return newest


class JobManager:
    def __init__(
        self,
        runner: Callable[[Job, TaskContext], dict[str, Any]],
        stages_for: Callable[[Job], list[tuple]],
        root: Path | None = None,
        workers: int | None = None,
    ):
        self.root = Path(root or SETTINGS.jobs_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._runner = runner
        self._stages_for = stages_for
        self._jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._contexts: dict[str, TaskContext] = {}
        # Attempts whose task returned with files in the output folder: Cancel can't take those back (_run).
        self._results_stand: set[TaskContext] = set()
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._last_emit: dict[str, float] = {}
        self._last_position: dict[str, int | None] = {}  # queue_position last sent per job
        self._last_save: dict[str, float] = {}
        self._workers = max(1, workers or LIMITS.max_concurrent_jobs)
        self._threads: list[threading.Thread] = []
        self._stopping = False
        self._stop_requested = threading.Event()
        self._stopped = threading.Event()
        self._instance_lock: InstanceLock | None = None
        self._awake_lock = threading.Lock()  # orders keep-awake changes; taken before _lock, never inside it
        self._awake = False
        self.on_running_changed: Callable[[bool], None] | None = None
        self.on_finished: Callable[[Job], None] | None = None

    # ------------------------------------------------------------------ paths
    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def work_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / 'work'

    def input_dir(self, job_id: str) -> Path:
        p = self.job_dir(job_id) / 'input'
        p.mkdir(parents=True, exist_ok=True)
        return p

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._instance_lock = InstanceLock(self.root / '.lock')
        if not self._instance_lock.acquire():
            raise AudioLabError(
                'AudioLab is already running with this data folder. Use the window '
                'that is already open, or quit it first.'
            )
        self._sweep_orphans()
        library.sweep_staging(ORPHAN_AGE_S)
        self._load()
        for i in range(self._workers):
            t = threading.Thread(target=self._worker, name=f'job-worker-{i}', daemon=True)
            t.start()
            self._threads.append(t)
        if LIMITS.retention_days > 0:  # also while the app stays open for weeks, not only at the next start
            threading.Thread(target=self._prune_daily, name='job-retention', daemon=True).start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """The one way to stop (server exit, window closed, Ctrl+C): queued jobs are cancelled,
        running ones are cancelled and their child processes killed, every outcome is saved,
        and the workers get `timeout` seconds in total to finish. Child processes that ignore
        the polite stop get the forced kill before this returns (at most KILL_WAIT_S). A job that
        is still busy after that (a step that never checks for cancellation) is recorded as
        stopped anyway — the process is about to exit. Releases the instance lock. Safe to call
        more than once."""
        if self._stopped.is_set():
            return
        self._stop_requested.set()
        with self._lock:
            self._stopping = True
            queued = [self._jobs[i] for i in self._queue if i in self._jobs]
            self._queue.clear()
            contexts = [
                ctx
                for job_id, ctx in self._contexts.items()
                if job_id in self._jobs and self._jobs[job_id].status == RUNNING
            ]
            for ctx in contexts:
                ctx.request_cancel()  # flag first: running code stops at its next progress call
            self._wake.notify_all()
        for job in queued:
            self._finish(job, CANCELLED, NOT_STARTED)
        killers = [threading.Thread(target=ctx.cancel, daemon=True) for ctx in contexts]
        for t in killers:  # each kill may wait for a stubborn tool, so in parallel
            t.start()
        start = time.monotonic()
        for threads, wait in ((self._threads, timeout), (killers, max(timeout, KILL_WAIT_S))):
            for t in threads:
                t.join(max(0.0, start + wait - time.monotonic()))
        with self._lock:
            stuck = [j for j in self._jobs.values() if j.status == RUNNING]
        for job in stuck:
            if not self._settle_standing(job):
                self._finish(job, CANCELLED, STOPPED_ON_EXIT)
        if self._instance_lock is not None:
            self._instance_lock.release()
        self._stopped.set()

    def _sweep_orphans(self) -> None:
        """A job folder without job.json is an upload or prepared input that never became a job (the app
        was killed during the request): nothing could show or clear it. Runs once at start, under the
        instance lock, so no request of this app is writing one. Removed only when all of this holds:
        a real folder named like a job, no job.json, nothing inside changed for an hour, it holds nothing
        but what a submission leaves (`_only_a_submission`), and it is not, inside or around the output
        folder as set, nor holds a file the Library knows. Anything else stays."""
        now = time.time()
        try:
            folders = list(self.root.iterdir())
        except OSError:
            return
        protected: frozenset[tuple[int, int]] | None = None  # looked up only when a folder qualifies
        for d in folders:
            try:
                if not JOB_ID_RE.fullmatch(d.name) or d.is_symlink() or not d.is_dir():
                    continue
                if os.path.lexists(d / 'job.json') or now - _newest_mtime(d) < ORPHAN_AGE_S:
                    continue
                if not _only_a_submission(d):
                    continue
                if protected is None:
                    protected = _protected_paths()
                if _protects(d, protected):
                    continue
            except OSError:
                continue
            log.info('removing the unfinished job folder %s (no job.json)', d.name)
            shutil.rmtree(d, ignore_errors=True)

    def _expired(self, job: Job, cutoff: float | None) -> bool:
        return bool(cutoff) and job.status in FINISHED and (job.finished_at or job.updated_at) < cutoff

    def _retention_cutoff(self) -> float | None:
        return time.time() - LIMITS.retention_days * 86400 if LIMITS.retention_days > 0 else None

    def _prune_daily(self) -> None:
        while not self._stop_requested.wait(PRUNE_EVERY_S):
            self.prune_expired()

    def prune_expired(self) -> builtins.list[str]:
        """Remove finished jobs older than the retention period (their Library entries stay)."""
        cutoff = self._retention_cutoff()
        ids = self._remove(lambda job: self._expired(job, cutoff))
        if ids:
            log.info('removed %d jobs older than %g days', len(ids), LIMITS.retention_days)
        return ids

    def _load(self) -> None:
        """Read jobs from disk; settle anything the previous session left unfinished."""
        cutoff = self._retention_cutoff()
        protected: frozenset[tuple[int, int]] | None = None  # looked up only when a folder goes
        for d in sorted(self.root.glob('j_*')):
            try:
                raw = json.loads((d / 'job.json').read_text(encoding='utf-8'))
                job = Job(**{k: v for k, v in raw.items() if k in Job.__dataclass_fields__})
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            # Every path of a job is built from its id (job_dir = root / id): only a job named exactly like
            # its own folder is trusted, never one whose id leads elsewhere ('../library').
            if not isinstance(job.id, str) or job.id != d.name or not JOB_ID_RE.fullmatch(job.id):
                log.warning('skipped %s: its job.json names another job', d.name)
                continue
            if job.status == RUNNING:
                job.status = FAILED
                if job.options.get('_preview'):
                    self._discard_preview(job)
                job.error = {
                    'message': 'Interrupted — AudioLab was closed while this was running.',
                    'hints': ['Click "Try again" to run it again.'],
                    'fix': 'retry',
                }
                job.message = job.error['message']
                self._journal(job.id, 'interrupted', job.error['message'])
                self._save(job)
                if not SETTINGS.get().keep_work_files:  # what _finish would have removed (often GBs)
                    shutil.rmtree(self.work_dir(job.id) / 'scratch', ignore_errors=True)
            elif job.status == QUEUED:
                job.status = CANCELLED
                job.message = NOT_STARTED
                self._save(job)
            expired = self._expired(job, cutoff)
            # An Adjust audition belongs to the draft in the tab that requested it. Closing the window skips
            # the page's own release, so its media would otherwise stay for weeks. A tab still open across a
            # restart says the audition is gone and offers to render it again.
            audition = isinstance(job.options.get('_preview'), dict) and job.options['_preview'].get('paired')
            if expired or (audition and job.status in FINISHED):
                protected = protected if protected is not None else _protected_paths()
                self._remove_folder(d, protected)
                continue
            self._jobs[job.id] = job

    # ---------------------------------------------------------------- queries
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [self._public(j) for j in jobs]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def public(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    def _public(self, job: Job) -> dict[str, Any]:
        pos = None
        if job.status == QUEUED and job.id in self._queue:
            pos = list(self._queue).index(job.id) + 1
        return job.public(pos)

    def journal(self, job_id: str) -> builtins.list[dict[str, Any]]:
        p = self.job_dir(job_id) / 'journal.jsonl'
        events = []
        try:
            for line in p.read_text(encoding='utf-8').splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tolerate a torn last line
        except OSError:
            pass
        return events

    # --------------------------------------------------------------- commands
    def submit(
        self, task: str, source: dict[str, Any], options: dict[str, Any], title: str = '', job_id: str | None = None
    ) -> Job:
        job = Job(
            id=job_id or new_job_id(),
            task=task,
            source=source,
            options=options,
            title=title or source.get('title') or source.get('name') or source.get('url') or 'Untitled',
        )
        job.stages = [{'key': k, 'label': label} for k, label, _ in self._stages_for(job)]
        with self._lock:
            if self._stopping:  # the workers are gone: it would wait forever
                raise AudioLabError(CLOSING)
            self._save(job, force=True, required=True)
            self._jobs[job.id] = job
            self._queue.append(job.id)
            self._journal(job.id, 'info', f'Queued: {task} — {job.title}', {'options': options})
            self._wake.notify()
        self._emit_job(job, force=True)
        self._emit_queue_positions()
        return job

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status == QUEUED:
                if job_id in self._queue:
                    self._queue.remove(job_id)
                self._finish(job, CANCELLED, 'Cancelled before it started.')
            elif job.status == RUNNING:
                job.message = 'Cancelling…'
                ctx = self._contexts.get(job_id)
                if ctx:
                    ctx.request_cancel()  # serialized with the final publication cutoff
                    threading.Thread(target=ctx.cancel, daemon=True).start()
            elif job.status == DONE:
                raise AudioLabError('This result is already being saved or has finished.')
        self._emit_job(job, force=True)
        self._emit_queue_positions()
        return job

    def retry(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in (FAILED, CANCELLED):
                return job
            if self._stopping:
                raise AudioLabError(CLOSING)
            job.status, job.error, job.result = QUEUED, None, None
            job.stage, job.progress, job.message = '', 0.0, 'Waiting in line…'
            job.source.pop('_probe', None)  # look the link up again: a live stream may have ended by now
            job.started_at = job.finished_at = None
            job.stages = [{'key': k, 'label': label} for k, label, _ in self._stages_for(job)]
            self._queue.append(job.id)
            self._save(job, force=True)
            self._journal(job.id, 'info', 'Retry requested')
            self._wake.notify()
        self._emit_job(job, force=True)
        self._emit_queue_positions()
        return job

    def add_result_file(self, job_id: str, entry: dict[str, Any]) -> None:
        """Add a file made after the job finished (the cover saved from the job page) to its
        result — one with the same id is replaced — then save the job and tell the clients."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.result is None:
                return
            files = [f for f in job.result.get('files') or [] if f.get('id') != entry['id']]
            job.result['files'] = files + [entry]
            job.updated_at = time.time()
            self._save(job, force=True)
        self._emit_job(job, force=True)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in (QUEUED, RUNNING):
                raise AudioLabError('This job is still active — cancel it first.')
            del self._jobs[job_id]
        self._remove_folder(self.job_dir(job_id), _protected_paths())
        with self._lock:
            self._emit('removed', {'ids': [job_id]})
        return True

    def _remove_folder(self, d: Path, protected: frozenset[tuple[int, int]]) -> None:
        """Remove a job's folder, unless that could touch a protected place (`_protects`: an output folder
        chosen there before such places were refused): finished audio would go with it."""
        if _protects(d, protected):
            log.warning('kept the job folder %s: it overlaps an output folder or a Library file', d.name)
            return
        shutil.rmtree(d, ignore_errors=True)

    def clear_finished(self) -> builtins.list[str]:
        return self._remove(lambda job: job.status in FINISHED)

    def _remove(self, which: Callable[[Job], bool]) -> builtins.list[str]:
        """Forget the finished jobs `which` picks and remove their folders (Clear, retention)."""
        with self._lock:
            ids = [j.id for j in self._jobs.values() if j.status in FINISHED and which(j)]
            for job_id in ids:
                del self._jobs[job_id]
        protected = _protected_paths() if ids else frozenset()
        for job_id in ids:
            self._remove_folder(self.job_dir(job_id), protected)
        if ids:
            with self._lock:
                self._emit('removed', {'ids': ids})
        return ids

    # ---------------------------------------------------------------- events
    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe():
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event, payload)
            except Exception:
                pass

    def _emit_job(self, job: Job, force: bool = False) -> None:
        with self._lock:  # snapshot and enqueue together so events stay in order
            if self._jobs.get(job.id) is not job:
                return
            now = time.monotonic()
            if not force and now - self._last_emit.get(job.id, 0.0) < 0.25:
                return
            self._last_emit[job.id] = now
            payload = self._public(job)
            self._last_position[job.id] = payload['queue_position']
            self._emit('job', payload)

    def _emit_queue_positions(self) -> None:
        """Tell the clients about queued jobs whose place in line changed — only those: a playlist of
        200 items would otherwise send 200 events per item added."""
        with self._lock:
            for pos, job_id in enumerate(self._queue, start=1):
                job = self._jobs.get(job_id)
                if job is not None and self._last_position.get(job_id) != pos:
                    self._emit_job(job, force=True)

    # --------------------------------------------------------------- storage
    def _save(self, job: Job, force: bool = False, required: bool = False) -> None:
        """Write job.json. A failure never breaks a running job; `required` (a new submission) raises it
        instead, so a job is never accepted that would vanish at the next start."""
        now = time.monotonic()
        if not force and now - self._last_save.get(job.id, 0.0) < 2.0:
            return
        self._last_save[job.id] = now
        d = self.job_dir(job.id)
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / '.job.json.tmp'
            tmp.write_text(json.dumps(asdict(job), indent=1, default=str), encoding='utf-8')
            replace_file(tmp, d / 'job.json')
        except OSError as e:
            if required:
                raise storage_error(e) or AudioLabError(
                    f'AudioLab could not save this job: {e.strerror or e}.',
                    hints=['Check free disk space and the permissions of the app data folder.'],
                ) from e

    def _journal(
        self, job_id: str, kind: str, message: str, detail: dict[str, Any] | None = None, stage: str | None = None
    ) -> None:
        rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'kind': kind, 'stage': stage, 'message': message}
        if detail is not None:
            rec['detail'] = detail
        try:
            d = self.job_dir(job_id)
            d.mkdir(parents=True, exist_ok=True)
            with open(d / 'journal.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
        except (OSError, TypeError, ValueError):
            pass

    # ---------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
                job_id = self._queue.popleft()
                job = self._jobs.get(job_id)
                if job is None or job.status != QUEUED:
                    continue
                job.status, job.started_at = RUNNING, time.time()
                try:
                    ctx = self._make_context(job)
                except Exception as e:
                    friendly = storage_error(e) if isinstance(e, OSError) else None
                    job.error = (
                        _error(friendly)
                        if friendly
                        else {
                            'message': f'Could not prepare a working folder: {e}',
                            'hints': ['Check free disk space and folder permissions.'],
                            'fix': 'retry',
                        }
                    )
                    self._finish(job, FAILED, job.error['message'])
                    continue
                self._contexts[job.id] = ctx
            self._emit_queue_positions()
            self._update_awake()
            self._run(job, ctx)
            with self._lock:
                if self._contexts.get(job.id) is ctx:
                    del self._contexts[job.id]
            self._update_awake()

    def _update_awake(self) -> None:
        """Tell `on_running_changed` (keep-awake) whether any job runs. Reading the state and calling back
        happen under one lock: with two workers, one finishing can never switch keep-awake off after the
        other has started (the later call always sees the later state)."""
        with self._awake_lock:
            with self._lock:
                running = any(j.status == RUNNING for j in self._jobs.values())
            if running != self._awake and self.on_running_changed:
                self._awake = running
                self.on_running_changed(running)

    def _make_context(self, job: Job) -> TaskContext:
        # Each closure retains this attempt's context, including after a retry or forced shutdown.
        ctx = TaskContext(
            job.id,
            self.work_dir(job.id),
            job.options,
            self._stages_for(job),
            emit_state=lambda stage, pct, msg: self._on_state(job, ctx, stage, pct, msg),
            emit_note=lambda kind, msg, detail: self._on_note(job, ctx, kind, msg, detail),
        )
        return ctx

    def _active(self, job: Job, ctx: TaskContext | None = None) -> bool:
        """Caller holds _lock. A retained Job/context cannot revive a removed or settled attempt."""
        return (
            self._jobs.get(job.id) is job
            and job.status not in FINISHED
            and (ctx is None or (job.status == RUNNING and self._contexts.get(job.id) is ctx))
        )

    def _on_note(self, job: Job, ctx: TaskContext, kind: str, message: str, detail=None) -> None:
        with self._lock:
            if self._active(job, ctx):
                self._journal(job.id, kind, message, detail, job.stage)

    def _on_state(self, job: Job, ctx: TaskContext, stage: str, pct: float, message: str | None) -> None:
        with self._lock:
            if not self._active(job, ctx):
                return
            stage_changed = stage != job.stage
            job.stage = stage
            job.progress = round(max(job.progress, min(99.9, pct)), 1)  # never move backwards
            if message:
                job.message = message
            job.updated_at = time.time()
            if stage_changed:
                self._journal(job.id, 'stage', message or stage, stage=stage)
            # A link started without a known title shows the bare link until it has been looked up.
            title = job.source.get('title')
            retitled = bool(title) and title != job.title and job.title == job.source.get('url')
            if retitled:
                job.title = title
            self._save(job, force=stage_changed or retitled)
            self._emit_job(job, force=stage_changed or retitled)

    def _run(self, job: Job, ctx: TaskContext) -> None:
        self._on_note(job, ctx, 'start', f'Started {job.task}')
        # Files and folders this attempt makes in the output folder. A failure or Cancel while the task runs
        # removes them (and nothing else): no half result, and Retry no "(2)". Once the task has returned its
        # result they are complete and never removed, whatever interrupts the Library step: the job then
        # finishes as done, only without its Library entry (_add_to_library). An attempt with nothing in the
        # output folder (Check, an audition) can still be cancelled until it is published.
        created = outputs.Created()
        returned = False
        try:
            try:
                with outputs.recording(created):
                    result = self._runner(job, ctx)
                returned = True
                with self._lock:
                    if created.files or created.folders:
                        self._results_stand.add(ctx)
                    ready = self._active(job, ctx)
                    if ready:
                        job.result = result
                        job.message = 'Finishing…'
                        self._emit_job(job, force=True)
                if ready:
                    self._finish(job, DONE, (result or {}).get('headline') or 'Done', ctx)
            finally:
                with self._lock:
                    self._results_stand.discard(ctx)
                if not returned:
                    created.discard()  # before the outcome is public: a Retry may reuse the names
        except Cancelled:
            self._finish(job, CANCELLED, STOPPED_ON_EXIT if self._stopping else 'Cancelled.', ctx)
        except AudioLabError as e:
            self._fail(job, ctx, _error(e))
        except MemoryError:
            smaller = (
                'Use Tone and volume processing or a shorter piece.'
                if job.task == 'enhance'
                else 'Try a shorter piece.'
            )
            error = {
                'message': 'The computer ran out of memory while processing this.',
                'hints': ['Close other apps and try again.', smaller],
                'fix': 'retry',
            }
            self._fail(job, ctx, error)
        except Exception as e:
            friendly = storage_error(e) if isinstance(e, OSError) else None
            if friendly is not None:  # a full disk, a folder it may not write to: nothing a retry fixes
                self._fail(job, ctx, _error(friendly))
                return
            # an unexpected bug: keep the details, stay friendly
            log.exception('job %s (%s) crashed in stage %s', job.id, job.task, job.stage)
            stage_label = dict((s['key'], s['label']) for s in job.stages).get(job.stage, job.stage)
            error = {
                'message': f'Something went wrong while {stage_label.lower() or "working"}.',
                'hints': [
                    'Try again — temporary problems often go away.',
                    'If it keeps failing, click "Copy details for a bug report" and include that when you '
                    'report the problem.',
                ],
                'fix': 'retry',
                'detail': f'{type(e).__name__}: {e}',
            }
            self._on_note(job, ctx, 'error', f'{type(e).__name__}: {e}', {'traceback': traceback.format_exc()})
            self._fail(job, ctx, error)

    def _fail(self, job: Job, ctx: TaskContext, error: dict[str, Any]) -> None:
        with self._lock:
            if not self._active(job, ctx):
                return
            job.error = error
        self._finish(job, FAILED, error['message'], ctx)

    def _finish(self, job: Job, status: str, message: str, ctx: TaskContext | None = None) -> None:
        # A job that is already settled (shutdown gave up waiting for its worker) stays as it is.
        # Scratch goes before the final status: once that is public, a retry may reuse the folder.
        with self._lock:
            if not self._active(job, ctx):
                return

        def check_cancel():
            with self._lock:
                if not self._active(job, ctx):
                    raise Cancelled()
                if ctx is not None:
                    ctx.check_cancel()

        with ExitStack() as commit:
            committing = False

            def begin_commit():
                nonlocal committing
                if committing:
                    return
                # Preparation is complete: a kept original has its own staged link/copy by now.
                # Slow cleanup stays outside the manager lock, before the cancellation cutoff.
                if not SETTINGS.get().keep_work_files:
                    shutil.rmtree(self.work_dir(job.id) / 'scratch', ignore_errors=True)
                if status != DONE and job.options.get('_preview'):
                    self._discard_preview(job)
                commit.enter_context(self._lock)
                if not self._active(job, ctx):  # removed, or settled by a shutdown that stopped waiting
                    raise Cancelled()
                if status == DONE and ctx is not None and ctx not in self._results_stand:
                    ctx.check_cancel()  # nothing in the output folder (Check, an audition): Cancel still works
                committing = True

            if status == DONE:
                self._add_to_library(job, ctx, check_cancel, begin_commit)
            try:
                begin_commit()
            except Cancelled:
                if status == DONE:
                    raise
                return
            job.status = status
            job.message = message
            job.finished_at = job.updated_at = time.time()
            if status == DONE:
                job.progress = 100.0
            self._journal(job.id, status, message, job.error if status == FAILED else None)
            log.info('job %s %s %s: %s', job.id, job.task, status, message)
            self._save(job, force=True)
            self._emit_job(job, force=True)
        if self.on_finished:
            try:
                self.on_finished(job)
            except Exception:
                pass

    def _settle_standing(self, job: Job) -> bool:
        """Shutdown stopped waiting for a job whose task had returned complete files (_run): it finished,
        only without its Library entry, as when Cancel stops that step. Its worker can no longer publish."""
        with self._lock:
            ctx = self._contexts.get(job.id)
            if ctx is None or ctx not in self._results_stand or not self._active(job, ctx):
                return False
            self._journal(job.id, 'warning', 'Could not add this result to the Library: AudioLab was closed')
            if isinstance(job.result, dict):
                job.result.setdefault('warnings', []).append(NOT_IN_LIBRARY)
            job.status, job.message = DONE, (job.result or {}).get('headline') or 'Done'
            job.progress, job.finished_at = 100.0, time.time()
            job.updated_at = job.finished_at
            self._journal(job.id, DONE, job.message, None)
            log.info('job %s %s %s: %s', job.id, job.task, DONE, job.message)
            self._save(job, force=True)
            self._emit_job(job, force=True)
        return True

    def _discard_preview(self, job: Job) -> None:
        shutil.rmtree(self.work_dir(job.id) / 'previews', ignore_errors=True)
        job.result = None

    def _add_to_library(
        self, job: Job, ctx: TaskContext | None, check_cancel: Callable[[], None], begin_commit: Callable[[], None]
    ) -> None:
        """Record the result in the Library — before scratch goes: it may hold the original to keep. An error
        only leaves the result out of the Library (noted on the job). So does a Cancel or quitting when the
        task's files are in the output folder (_run): they are complete, and the job still finishes as done.
        Cancelled escapes otherwise, and whenever the job can't be published at all (removed, or settled by a
        shutdown that stopped waiting)."""
        try:
            if library.record(job, check_cancel, begin_commit):
                job.result['library_id'] = job.id
            return
        except Cancelled:
            with self._lock:
                if ctx not in self._results_stand:
                    raise
            reason = 'it was stopped while being added'
        except Exception as e:
            log.exception('job %s: could not add the result to the library', job.id)
            reason = str(e)
        with self._lock:
            if not self._active(job):
                raise Cancelled()
            self._journal(job.id, 'warning', f'Could not add this result to the Library: {reason}')
            if isinstance(job.result, dict):
                job.result.setdefault('warnings', []).append(NOT_IN_LIBRARY)
