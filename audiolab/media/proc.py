"""Subprocess runner with live line callbacks, timeouts and cancellation.

Every external tool (ffmpeg, yt-dlp, pip) goes through `run()`: the child gets
its own process group so a cancel or timeout stops it *and* anything it
spawned (yt-dlp → ffmpeg), and long-running output is streamed line by line
to the caller instead of being buffered until exit.
"""

import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..context import Progress
from ..errors import AudioLabError, Cancelled, ToolMissingError
from ..system import own_process_group, register_process_group, terminate_process

_STDERR_TAIL_LINES = 60
_POLL_S = 0.2
# Shown when a tool is missing; the report lists which tools AudioLab found.
REPORT_HINT = 'Settings → About → "Copy system report" shows what AudioLab found — include it when you report this.'


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr_tail: str


def _pump(stream, sink: Callable[[str], None]):
    try:
        for line in iter(stream.readline, ''):
            sink(line.rstrip('\r\n'))
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run(
    cmd: list[str],
    progress: Progress | None = None,
    timeout_s: float | None = None,
    on_line: Callable[[str], None] | None = None,
    what: str = 'process',
    capture_stdout: bool = True,
    cwd=None,
    env=None,
    timeout_hint: str = 'Try again — the computer may have been very busy.',
    inherited_fds: tuple[int, ...] = (),
) -> ProcResult:
    """Run `cmd`, streaming every stdout/stderr line to `on_line`.

    Raises Cancelled when the job is cancelled, AudioLabError on timeout (with `timeout_hint`, what
    fits the caller) and ToolMissingError when the executable does not exist. A non-zero exit code
    is returned, not raised — callers know what a failure means for them.
    """
    progress = progress or Progress.null()
    progress.check_cancel()

    try:
        from ..system import inherited_descriptors

        proc = subprocess.Popen(
            [str(c) for c in cmd],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            cwd=str(cwd) if cwd else None,
            env=env,
            **own_process_group(),
            **inherited_descriptors(inherited_fds),
        )
    except FileNotFoundError as e:
        raise ToolMissingError(
            f'Could not start {what}: {Path(str(cmd[0])).name} was not found.',
            hints=[REPORT_HINT],
        ) from e
    except OSError as e:
        raise AudioLabError(f'Could not start {what}: {e}') from e

    register_process_group(proc)
    progress.attach_process(proc)

    stdout_lines: list[str] = []
    stderr_tail: deque = deque(maxlen=_STDERR_TAIL_LINES)
    lock = threading.Lock()

    def out_sink(line: str):
        if capture_stdout:
            with lock:
                stdout_lines.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:
                pass  # a broken callback must never kill the reader thread

    def err_sink(line: str):
        with lock:
            stderr_tail.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:
                pass

    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, out_sink), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err_sink), daemon=True),
    ]
    for t in readers:
        t.start()

    deadline = time.monotonic() + timeout_s if timeout_s else None
    try:
        while proc.poll() is None:
            if progress.cancelled:
                terminate_process(proc)
                raise Cancelled()
            if deadline is not None and time.monotonic() > deadline:
                terminate_process(proc)
                mins = int(round(timeout_s / 60.0))
                raise AudioLabError(
                    f'{what[:1].upper()}{what[1:]} took too long and was stopped (limit: {mins} min).'
                    if mins >= 1
                    else f'{what[:1].upper()}{what[1:]} took too long and was stopped.',
                    hints=[timeout_hint],
                )
            try:
                proc.wait(timeout=_POLL_S)  # returns as soon as it ends (short decodes for the players)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        terminate_process(proc)
        raise
    finally:
        for t in readers:
            t.join(timeout=2.0)
        progress.detach_process(proc)

    if progress.cancelled:
        terminate_process(proc)  # its leader may have exited while readers/descendants were still active
        raise Cancelled()

    with lock:
        return ProcResult(
            returncode=proc.returncode, stdout='\n'.join(stdout_lines), stderr_tail='\n'.join(stderr_tail)
        )
