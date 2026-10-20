"""Progress reporting, journaling and cancellation for running tasks.

Library code (media/, analysis/, recipes/, ai/) only ever receives a
`Progress` object:

    def do_work(..., progress: Progress):
        progress(10, 'Loading audio...')          # pct of *this* scope, 0..100
        step = progress.sub(20, 80)                # child scope mapped to 20..80
        step(50)                                   # == 50 % overall of this scope
        progress.note('decision', 'Picked X', {'why': ...})
        progress.attach_process(popen)             # so cancel can kill it

Calling a Progress raises `Cancelled` once the user cancelled the job, so
long loops that report progress are automatically cancellable. Code with no
progress to report should still call `progress.check_cancel()` occasionally.

`Progress.null()` is a no-op reporter for tests and library use.

A `TaskContext` is the job-level object a task receives: it knows the job's
work folder and options and hands out one Progress per declared stage.
"""

import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .errors import Cancelled
from .system import terminate_process

EmitState = Callable[[str, float, str | None], None]  # (stage_key, overall_pct, message)
EmitNote = Callable[[str, str, dict[str, Any] | None], None]  # (kind, message, detail)


class _Shared:
    """State shared by every Progress scope of one job."""

    def __init__(
        self, emit: Callable[[float, str | None], None], note: EmitNote, cancel_event: threading.Event | None = None
    ):
        self.emit = emit
        self.note = note
        self.cancel_event = cancel_event or threading.Event()
        self.procs: set = set()
        self.lock = threading.Lock()


class Progress:
    def __init__(self, shared: _Shared, start: float = 0.0, end: float = 100.0):
        self._shared = shared
        self._start = float(start)
        self._end = float(end)

    @classmethod
    def null(cls, cancel_event: threading.Event | None = None) -> 'Progress':
        """Reports nothing; cancelled only through `cancel_event`, if given."""
        return cls(_Shared(lambda pct, msg: None, lambda kind, msg, detail: None, cancel_event))

    # -- reporting -----------------------------------------------------------
    def __call__(self, pct: float, message: str | None = None) -> None:
        self.check_cancel()
        pct = min(100.0, max(0.0, float(pct)))
        self._shared.emit(self._start + (self._end - self._start) * pct / 100.0, message)

    def sub(self, start: float, end: float) -> 'Progress':
        span = self._end - self._start
        return Progress(self._shared, self._start + span * start / 100.0, self._start + span * end / 100.0)

    def note(self, kind: str, message: str, detail: dict[str, Any] | None = None) -> None:
        """Journal an event without moving progress. kinds: info, decision, warning, verdict."""
        self._shared.note(kind, message, detail)

    # -- cancellation --------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self._shared.cancel_event.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        return self._shared.cancel_event

    def check_cancel(self) -> None:
        if self._shared.cancel_event.is_set():
            raise Cancelled()

    def attach_process(self, proc: subprocess.Popen) -> None:
        with self._shared.lock:
            self._shared.procs.add(proc)
        if self.cancelled:  # cancelled between spawn and attach
            terminate_process(proc)

    def detach_process(self, proc: subprocess.Popen) -> None:
        with self._shared.lock:
            self._shared.procs.discard(proc)


class TaskContext:
    """Job-level context handed to a task function.

    `stages` is the task's ordered plan: (key, label, weight). Weights only
    shape the overall progress bar; they need not sum to 100.
    """

    def __init__(
        self,
        job_id: str,
        work_dir: Path,
        options: dict[str, Any],
        stages: Sequence[tuple[str, str, float]],
        emit_state: EmitState | None = None,
        emit_note: EmitNote | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.job_id = job_id
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.options = dict(options or {})
        self.stages: list[tuple[str, str, float]] = list(stages)
        self._emit_state = emit_state or (lambda stage, pct, msg: None)
        self.current_stage: str | None = None
        self._shared = _Shared(self._emit, emit_note or (lambda kind, msg, detail: None), cancel_event)
        total = sum(max(0.0, w) for _, _, w in self.stages) or 1.0
        self._ranges: dict[str, tuple[float, float]] = {}
        acc = 0.0
        for key, _, weight in self.stages:
            start = acc
            acc += max(0.0, weight) / total * 100.0
            self._ranges[key] = (start, acc)

    def _emit(self, overall_pct: float, message: str | None) -> None:
        self._emit_state(self.current_stage or '', overall_pct, message)

    def stage(self, key: str, message: str | None = None) -> Progress:
        """Enter a declared stage and return the Progress scope for it."""
        if key not in self._ranges:
            raise KeyError(f'undeclared stage {key!r}')
        self.current_stage = key
        start, end = self._ranges[key]
        p = Progress(self._shared, start, end)
        p(0, message or dict((k, label) for k, label, _ in self.stages)[key])
        return p

    def note(self, kind: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self._shared.note(kind, message, detail)

    @property
    def progress(self) -> Progress:
        """Root scope (whole job). Prefer stage() inside tasks."""
        return Progress(self._shared)

    @property
    def cancelled(self) -> bool:
        return self._shared.cancel_event.is_set()

    def check_cancel(self) -> None:
        if self.cancelled:
            raise Cancelled()

    def request_cancel(self) -> None:
        """Flag cancellation without waiting for child processes to stop."""
        self._shared.cancel_event.set()

    def cancel(self) -> None:
        """Request cancellation: flags the token and stops running child processes."""
        self._shared.cancel_event.set()
        with self._shared.lock:
            procs = list(self._shared.procs)
        for proc in procs:
            terminate_process(proc)
